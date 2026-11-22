"""Server-side entity resolver and cross-database linker for the Nybble Flask application.
The pip-installable equivalent is ``nybble.kg`` (marketed as Nybble GraphAI).
Do not import this file from the ``nybble`` package - use ``nybble.kg`` instead.

kg_resolver.py - Entity Resolver + Cross-DB Linker (Sub-Task 4)

Given a set of search hits (source key + table + PK), walks FK edges in
schema.json to discover connected rows and finds cross-database entity links
where the same value appears in different sources.

All operations are read-only - no writes to any source database.

Public API
----------
  resolve_entity(source_key, table, pk_col, pk_val, depth=2)
      → (list[EntityNode], list[EntityEdge])

  cross_db_links(nodes)
      → list[EntityEdge]   (candidate links across databases)

  build_entity_nodes(hit_list)
      → list[EntityNode]

  tag_entities(nodes)
      → nodes with entity_type populated (uses spaCy NER or regex fallback)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class EdgeKind(str, Enum):
    """Typed edge-relation prefixes for EntityEdge.relation.

    # MARKER:kg-edge-kind-typed
    """
    FK_FORWARD  = "FK"
    FK_REVERSE  = "REV_FK"
    CROSS_DB    = "cross_db"
    INFERRED    = "inferred"
    CO_OCCURS   = "co_occurs"
    SCHEMA_FK   = "schema_fk"

# ── Paths ─────────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/output/erd"))
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/uploads"))

# Maximum nodes returned per resolve_entity call
MAX_RESOLVE_NODES = 200

# ── Cross-DB endpoint policy ──────────────────────────────────────────────────
#
# MARKER:kg-cross-db-endpoint-policy
#
# Controls how unknown relation endpoints (nodes referenced by a cross-DB edge
# but not yet resolved into the current graph) are handled.
#
# Values:
#   "skip"   (default) - dangling cross-DB edges are silently dropped
#   "stub"             - a minimal stub EntityNode is created for the endpoint
#                        with entity_type="UNKNOWN" and an empty row_dict
#   "error"            - raise a RuntimeError when an unknown endpoint is found
#
# Set via environment: CROSS_DB_ENDPOINT_POLICY=stub
#
_VALID_ENDPOINT_POLICIES = frozenset({"skip", "stub", "error"})
CROSS_DB_ENDPOINT_POLICY: str = os.environ.get(
    "CROSS_DB_ENDPOINT_POLICY", "skip"
).lower().strip()
if CROSS_DB_ENDPOINT_POLICY not in _VALID_ENDPOINT_POLICIES:
    logger.warning(
        "CROSS_DB_ENDPOINT_POLICY has unrecognised value %r - defaulting to 'skip'",
        CROSS_DB_ENDPOINT_POLICY,
    )
    CROSS_DB_ENDPOINT_POLICY = "skip"


# ── Entity data types ─────────────────────────────────────────────────────────

@dataclass
class EntityNode:
    """A single entity node in the knowledge graph."""
    id:          str            # stable id: "<source_key>:<table>:<pk_col>=<pk_val>"
    label:       str            # human-readable label (best-guess name column or pk_val)
    entity_type: str            # UNKNOWN / PERSON / ORG / DATE / EMAIL / URL / ID / NUM
    source_key:  str
    table:       str
    pk_col:      str
    pk_val:      str
    row_dict:    Dict[str, Any] = field(default_factory=dict)


@dataclass
class EntityEdge:
    """A directed edge between two entity nodes."""
    src_id:   str
    tgt_id:   str
    relation: str   # e.g. "FK:orders→users", "cross_db:email", "inferred"
    label:    str   # short display label
    weight:   float = 1.0
    confidence: float = 1.0


# ── FK adjacency builder ──────────────────────────────────────────────────────

def _load_fk_map(source_key: str) -> Dict[str, List[Tuple[str, str, str]]]:
    """Return FK adjacency map from schema.json.

    Returns: {table_name: [(fk_col, ref_table, ref_col), ...]}
    """
    schema_path = OUTPUT_DIR / source_key / "schema.json"
    if not schema_path.exists():
        return {}
    try:
        raw = json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception as _exc:
        logger.debug(
            "_load_fk_map: cannot parse schema.json",
            extra={"error": str(_exc), "outcome": "skip"},
        )
        return {}

    fk_map: Dict[str, List[Tuple[str, str, str]]] = {}
    tables = raw.get("tables", {})
    for table_name, tdata in tables.items():
        fk_map.setdefault(table_name, [])
        for cname, cdata in tdata.get("constraints", {}).items():
            if cdata.get("type") != "FOREIGN KEY":
                continue
            # schema.json uses "targetTableName" / "columnNames" / "targetColumnNames"
            ref_table = cdata.get("targetTableName", cdata.get("ref_table", ""))
            cols      = cdata.get("columnNames",     cdata.get("columns", []))
            ref_cols  = cdata.get("targetColumnNames", cdata.get("ref_columns", []))
            for col, rcol in zip(cols, ref_cols or cols):
                fk_map[table_name].append((col, ref_table, rcol))
    return fk_map


def _load_reverse_fk_map(source_key: str) -> Dict[str, List[Tuple[str, str, str]]]:
    """Return reverse FK map: {ref_table: [(ref_col, src_table, fk_col), ...]}"""
    fk_map = _load_fk_map(source_key)
    rev: Dict[str, List[Tuple[str, str, str]]] = {}
    for table_name, fks in fk_map.items():
        for fk_col, ref_table, ref_col in fks:
            rev.setdefault(ref_table, []).append((ref_col, table_name, fk_col))
    return rev


# ── SQLite helpers ────────────────────────────────────────────────────────────

def _find_sqlite_db(key: str) -> Optional[Path]:
    for base in (UPLOAD_DIR / key, OUTPUT_DIR / key):
        if base.exists():
            for f in sorted(base.iterdir()):
                if f.suffix.lower() in (".db", ".sqlite"):
                    return f
    return None


def _fetch_row(conn: sqlite3.Connection, table: str, pk_col: str, pk_val: str) -> Optional[dict]:
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f'SELECT * FROM "{table}" WHERE "{pk_col}" = ? LIMIT 1',
            (pk_val,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as _exc:
        logger.debug(
            "_fetch_row failed",
            extra={"table": table, "error": str(_exc), "outcome": "skip"},
        )
        return None


def _best_label(row_dict: dict, pk_val: str) -> str:
    """Pick the most human-readable label from a row dict."""
    label_cols = ["name", "title", "username", "email", "first_name", "label", "description"]
    for col in label_cols:
        if col in row_dict and row_dict[col]:
            return str(row_dict[col])[:80]
    return str(pk_val)[:80]


# ── Entity node ID ────────────────────────────────────────────────────────────

def _node_id(source_key: str, table: str, pk_col: str, pk_val: str) -> str:
    return f"{source_key}:{table}:{pk_col}={pk_val}"


# ── BFS entity resolver ───────────────────────────────────────────────────────

def resolve_entity(
    source_key: str,
    table: str,
    pk_col: str,
    pk_val: str,
    depth: int = 2,
) -> Tuple[List[EntityNode], List[EntityEdge]]:
    """Walk FK edges up to `depth` hops from the starting row.

    Returns (nodes, edges).  Read-only.  Capped at MAX_RESOLVE_NODES.
    """
    import time as _time
    t0 = _time.monotonic()

    db_path = _find_sqlite_db(source_key)
    if db_path is None or not db_path.exists():
        # For live sources (no local SQLite) return just the root node
        root = EntityNode(
            id          = _node_id(source_key, table, pk_col, pk_val),
            label       = pk_val,
            entity_type = "UNKNOWN",
            source_key  = source_key,
            table       = table,
            pk_col      = pk_col,
            pk_val      = pk_val,
        )
        return [root], []

    fk_map   = _load_fk_map(source_key)
    rev_map  = _load_reverse_fk_map(source_key)

    nodes:  Dict[str, EntityNode] = {}
    edges:  List[EntityEdge]      = []
    visited: set                  = set()

    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row

        # BFS queue: (table, pk_col, pk_val, current_depth)
        queue = [(table, pk_col, pk_val, 0)]

        while queue and len(nodes) < MAX_RESOLVE_NODES:
            tbl, pkc, pkv, d = queue.pop(0)
            node_id = _node_id(source_key, tbl, pkc, pkv)
            if node_id in visited:
                continue
            visited.add(node_id)

            row = _fetch_row(conn, tbl, pkc, pkv)
            row_dict = row or {}

            node = EntityNode(
                id          = node_id,
                label       = _best_label(row_dict, pkv),
                entity_type = "UNKNOWN",
                source_key  = source_key,
                table       = tbl,
                pk_col      = pkc,
                pk_val      = pkv,
                row_dict    = row_dict,
            )
            nodes[node_id] = node

            if d >= depth:
                continue

            # Forward FKs: this table → referenced tables
            for (fk_col, ref_table, ref_col) in fk_map.get(tbl, []):
                ref_val = str(row_dict.get(fk_col, "")) if row_dict else ""
                if not ref_val or ref_val == "None":
                    continue
                target_id = _node_id(source_key, ref_table, ref_col, ref_val)
                edges.append(EntityEdge(
                    src_id   = node_id,
                    tgt_id   = target_id,
                    relation = EdgeKind.FK_FORWARD.value + f":{tbl}→{ref_table}",
                    label    = fk_col,
                ))
                if target_id not in visited:
                    queue.append((ref_table, ref_col, ref_val, d + 1))

            # Reverse FKs: tables that reference this table
            for (ref_col, src_table, src_fk_col) in rev_map.get(tbl, []):
                try:
                    ref_rows = conn.execute(
                        f'SELECT "{src_fk_col}", * FROM "{src_table}"'
                        f' WHERE "{src_fk_col}" = ? LIMIT 20',
                        (pkv,),
                    ).fetchall()
                    for rr in ref_rows:
                        rr_dict = dict(rr)
                        rr_pk   = str(rr_dict.get(src_fk_col, ""))
                        # Use rowid as pk if src_fk_col is the FK col (not PK of src table)
                        rr_pk_col = src_fk_col
                        target_id = _node_id(source_key, src_table, rr_pk_col, rr_pk)
                        edges.append(EntityEdge(
                            src_id   = node_id,
                            tgt_id   = target_id,
                            relation = EdgeKind.FK_REVERSE.value + f":{src_table}→{tbl}",
                            label    = src_fk_col,
                        ))
                        if target_id not in visited:
                            queue.append((src_table, rr_pk_col, rr_pk, d + 1))
                except Exception as _exc:
                    logger.debug(
                        "resolve_entity: reverse FK query failed",
                        extra={
                            "source_key": source_key,
                            "table_name": src_table,
                            "error": str(_exc),
                            "outcome": "skip",
                        },
                    )
                    continue

        conn.close()
    except Exception as _exc:
        logger.warning(
            "resolve_entity: BFS walk failed",
            extra={
                "source_key": source_key,
                "table_name": table,
                "error": str(_exc),
                "outcome": "warn",
            },
        )

    elapsed_ms = round((_time.monotonic() - t0) * 1000)
    logger.debug(
        "resolve_entity: complete",
        extra={
            "source_key":  source_key,
            "table_name":  table,
            "node_count":  len(nodes),
            "edge_count":  len(edges),
            "elapsed_ms":  elapsed_ms,
            "outcome":     "ok",
        },
    )
    return list(nodes.values()), edges


# ── Cross-DB linker ────────────────────────────────────────────────────────────

def cross_db_links(nodes: List[EntityNode]) -> List[EntityEdge]:
    """Detect potential cross-database entity links.

    Algorithm: group nodes by (col_name, cell_value) pairs; if the same value
    appears in the same column across different source_keys, emit a candidate
    edge with confidence proportional to how unique the value appears to be.

    Shared values in columns like "id", "rowid", "created_at" are excluded
    (too generic to be meaningful cross-DB links).
    """
    SKIP_COLS = frozenset({
        "id", "rowid", "created_at", "updated_at", "created", "updated",
        "timestamp", "date", "time", "status", "type", "flag", "active",
    })

    # Build index: (col_name_lower, cell_value) → [EntityNode]
    index: Dict[Tuple[str, str], List[EntityNode]] = {}
    for node in nodes:
        for col_name, val in node.row_dict.items():
            if col_name.lower() in SKIP_COLS:
                continue
            if val is None or val == "" or val == "None":
                continue
            val_str = str(val).strip()
            if len(val_str) < 3:
                continue
            key = (col_name.lower(), val_str)
            index.setdefault(key, []).append(node)

    edges: List[EntityEdge] = []
    seen_pairs: set = set()
    skipped = 0

    for (col_name, val), matching_nodes in index.items():
        # Only emit edges when nodes are from different source_keys
        sources = {n.source_key for n in matching_nodes}
        if len(sources) < 2:
            continue

        for i, na in enumerate(matching_nodes):
            for nb in matching_nodes[i + 1:]:
                if na.source_key == nb.source_key:
                    continue
                pair = (min(na.id, nb.id), max(na.id, nb.id))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                # Higher confidence for semantically meaningful columns
                confidence = _link_confidence(col_name, val)
                edges.append(EntityEdge(
                    src_id     = na.id,
                    tgt_id     = nb.id,
                    relation   = EdgeKind.CROSS_DB.value + f":{col_name}",
                    label      = f"{col_name}={val[:40]}",
                    weight     = confidence,
                    confidence = confidence,
                ))

    if skipped:
        logger.debug(
            "cross_db_links: skipped intra-source pairs",
            extra={"skipped": skipped, "outcome": "ok"},
        )
    return edges


def apply_cross_db_endpoint_policy(
    nodes: List[EntityNode],
    edges: List[EntityEdge],
    *,
    policy: Optional[str] = None,
) -> Tuple[List[EntityNode], List[EntityEdge]]:
    """Enforce CROSS_DB_ENDPOINT_POLICY for edges whose endpoints are not in *nodes*.

    # MARKER:kg-cross-db-endpoint-policy

    Called after ``cross_db_links()`` to handle edges that reference node IDs
    not present in the current resolved node set.

    Parameters
    ----------
    nodes : list[EntityNode]
        Current set of resolved nodes.
    edges : list[EntityEdge]
        Cross-DB edges returned by ``cross_db_links()``.
    policy : str or None
        Override policy string.  If None, uses ``CROSS_DB_ENDPOINT_POLICY``.

    Returns
    -------
    (nodes, edges)
        Updated tuple.  With ``"skip"`` policy nodes is unchanged and dangling
        edges are removed.  With ``"stub"`` policy, stub nodes are appended.
        With ``"error"`` policy, raises RuntimeError on any dangling endpoint.
    """
    active_policy = (policy or CROSS_DB_ENDPOINT_POLICY).lower().strip()
    known_ids = {n.id for n in nodes}
    dangling_ids: set = set()

    for edge in edges:
        if edge.src_id not in known_ids:
            dangling_ids.add(edge.src_id)
        if edge.tgt_id not in known_ids:
            dangling_ids.add(edge.tgt_id)

    if not dangling_ids:
        return nodes, edges

    if active_policy == "error":
        raise RuntimeError(
            f"CROSS_DB_ENDPOINT_POLICY=error: {len(dangling_ids)} unknown endpoint(s): "
            + ", ".join(sorted(dangling_ids))
        )

    if active_policy == "stub":
        stub_nodes = []
        for nid in dangling_ids:
            # Parse source_key from id prefix (format: "<source_key>:<table>:...")
            parts = nid.split(":", 2)
            sk = parts[0] if len(parts) >= 1 else "unknown"
            stub_nodes.append(EntityNode(
                id          = nid,
                label       = nid.split(":")[-1],
                entity_type = "UNKNOWN",
                source_key  = sk,
                table       = parts[1] if len(parts) >= 2 else "unknown",
                pk_col      = "id",
                pk_val      = parts[2] if len(parts) >= 3 else nid,
                row_dict    = {},
            ))
        logger.debug(
            "cross_db_endpoint_policy: created %d stub nodes",
            len(stub_nodes),
            extra={"outcome": "stub"},
        )
        return nodes + stub_nodes, edges

    # Default: "skip" - drop edges that reference unknown endpoints
    clean_edges = [
        e for e in edges
        if e.src_id not in dangling_ids and e.tgt_id not in dangling_ids
    ]
    logger.debug(
        "cross_db_endpoint_policy: dropped %d dangling edges",
        len(edges) - len(clean_edges),
        extra={"outcome": "skip"},
    )
    return nodes, clean_edges


def _link_confidence(col_name: str, val: str) -> float:
    """Return a 0-1 confidence score for a cross-DB link."""
    col_lower = col_name.lower()
    # High-confidence: email, UUID-like, phone
    if col_lower in ("email", "email_address", "e_mail"):
        return 0.95
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", val, re.I):
        return 0.90
    if col_lower in ("phone", "mobile", "telephone"):
        return 0.85
    # Medium-confidence: username, national ID
    if col_lower in ("username", "user_name", "login", "handle"):
        return 0.75
    # Generic name-alike column
    if "name" in col_lower or "code" in col_lower:
        return 0.60
    return 0.40


# ── Entity type tagger ─────────────────────────────────────────────────────────

# Regex patterns for common entity types
_RE_EMAIL   = re.compile(r"^[\w.+\-]+@[\w.\-]+\.[a-z]{2,}$", re.I)
_RE_URL     = re.compile(r"^https?://", re.I)
_RE_UUID    = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_RE_DATE    = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2})?")
_RE_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


# Entity types that have been deliberately set during graph construction and
# must not be overwritten by the NER/regex tagger.
_FIXED_TYPES = frozenset({"TABLE", "COLUMN"})


def tag_entities(nodes: List[EntityNode]) -> List[EntityNode]:
    """Assign entity_type to each node using NER (if spaCy available) or regex.

    Nodes whose entity_type is already a structural graph type (TABLE, COLUMN)
    are left unchanged - they were typed explicitly during graph construction
    and regex-matching their label would produce incorrect results.
    """
    # Try spaCy NER first
    nlp = _get_nlp()

    for node in nodes:
        # Skip nodes that already have a meaningful non-UNKNOWN type
        if node.entity_type in _FIXED_TYPES:
            continue

        if nlp is not None:
            try:
                doc = nlp(node.label[:512])
                ents = doc.ents
                if ents:
                    node.entity_type = ents[0].label_
                    continue
            except Exception as _exc:
                logger.debug(
                    "tag_entities: spaCy NER failed for node",
                    extra={"doc_id": node.id, "error": str(_exc), "outcome": "skip"},
                )

        # Regex fallback on label and pk_val
        for val in (node.label, node.pk_val):
            if _RE_EMAIL.match(val):
                node.entity_type = "EMAIL"; break
            if _RE_URL.match(val):
                node.entity_type = "URL"; break
            if _RE_UUID.match(val):
                node.entity_type = "ID"; break
            if _RE_DATE.match(val):
                node.entity_type = "DATE"; break
            if _RE_NUMERIC.match(val):
                node.entity_type = "NUM"; break
        else:
            if node.entity_type == "UNKNOWN":
                node.entity_type = "ENTITY"

    return nodes


_nlp = None


def _get_nlp():
    """Lazily load spaCy model.  Returns None if not installed."""
    global _nlp
    if _nlp is not None:
        return _nlp
    try:
        import spacy
        _nlp = spacy.load("en_core_web_sm")
        return _nlp
    except Exception as _exc:
        logger.debug(
            "_get_nlp: spaCy not available",
            extra={"error": str(_exc), "outcome": "skip"},
        )
        return None


# ── Higher-level helper ───────────────────────────────────────────────────────

def build_entity_nodes(hit_list) -> List[EntityNode]:
    """Convert a list of DataHit or SearchHit objects into EntityNode objects.

    For DataHits: one EntityNode per hit.
    For SearchHits (schema hits): one EntityNode per table/source pair.
    """
    from .kg_search import DataHit, SearchHit  # avoid circular at module level

    nodes: Dict[str, EntityNode] = {}

    for h in hit_list:
        if isinstance(h, DataHit):
            nid = _node_id(h.source_key, h.table_name, h.pk_col, h.pk_val)
            if nid not in nodes:
                nodes[nid] = EntityNode(
                    id          = nid,
                    label       = h.cell_value[:80],
                    entity_type = "UNKNOWN",
                    source_key  = h.source_key,
                    table       = h.table_name,
                    pk_col      = h.pk_col,
                    pk_val      = h.pk_val,
                    row_dict    = {},
                )
        elif isinstance(h, SearchHit):
            # Schema hit → represent as a "table" node
            nid = f"{h.source_key}:{h.table_name}"
            if nid not in nodes:
                nodes[nid] = EntityNode(
                    id          = nid,
                    label       = h.table_name,
                    entity_type = "TABLE",
                    source_key  = h.source_key,
                    table       = h.table_name,
                    pk_col      = "",
                    pk_val      = "",
                    row_dict    = {"column": h.column_name, "type": h.data_type},
                )

    return tag_entities(list(nodes.values()))
