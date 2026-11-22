"""
kg.py - Knowledge Graph Builder and Exporters (CLI adapter)

Builds a semantic MultiDiGraph from loaded relations, exports it in
GraphML, JSON-LD, and self-contained HTML formats.

This module is the CLI adapter layer. The graph-construction logic lives in
nybble.core.kg_builder.build_kg() and is shared with the Flask server.

Public API
----------
  kg(relations=None, output_dir=None, open_browser=True) → KGResult
  kg_summary(relations=None)                              → dict
  search_nodes(query, graph=None, top_k=10)               → list[NodeSearchResult]
  generate_pgq_ddl(G, fk_candidates)                      → str
"""
from __future__ import annotations

import json
import logging
import os
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

import networkx as nx
from jinja2 import Environment, FileSystemLoader

from nybble.core.kg_builder import (
    build_kg as _core_build_kg,
    build_search_index as _build_search_index,
    index_search as _index_search,
)
from nybble.core.schema import Column, Schema, Table
from nybble.infer_fk import infer_fks, FKCandidate
from nybble.resolver import _REGISTERED, Relation, session, _SESSION_LOCK

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_DEFAULT_OUTPUT_DIR = Path(".nybble")

_logger = logging.getLogger(__name__)

# Maximum rows fetched per relation when building the knowledge graph.
# Override via the nybble.yaml config key ``knowledge_graph.max_rows_per_relation``.
# The ``kg()`` function reads this key and passes the resolved value through
# ``_build_graph`` → ``_fetch_rows``.
_KG_MAX_ROWS_PER_RELATION: int = 5_000

# Maximum distinct values in a string column to trigger co-occurrence linking
_MAX_COOCCURRENCE_CARDINALITY = 10_000


@dataclass
class KGResult:
    """Return value from kg().

    Attributes
    ----------
    graph       : networkx.MultiDiGraph
    graphml_path, jsonld_path, html_path : Path
    pgq_path    : Path | None  - SQL/PGQ DDL file (only when export_format includes 'pgq')
    summary     : dict
    """
    graph: nx.MultiDiGraph
    graphml_path: Path
    jsonld_path: Path
    html_path: Path
    summary: dict
    pgq_path: Optional[Path] = None


# ── Graph construction ────────────────────────────────────────────────────────

# ---------------------------------------------------------------------------
# DuckDB callbacks for the shared kg_builder
# ---------------------------------------------------------------------------

def _duckdb_fetch_rows(relation_name: str, limit: int) -> list:
    """Fetch up to *limit* rows from a DuckDB relation as list[dict]."""
    con = session()
    with _SESSION_LOCK:
        try:
            arrow = con.execute(
                f'SELECT * FROM "{relation_name}" LIMIT {limit}'
            ).arrow().read_all()
        except Exception:
            return []
    import polars as pl
    rows = pl.from_arrow(arrow).to_dicts()
    if len(rows) == limit:
        _logger.debug(
            "KG row cap applied: relation=%r capped at %d rows",
            relation_name, limit,
        )
    return rows


def _duckdb_distinct_count(table_name: str, col_name: str) -> int:
    """Return COUNT(DISTINCT col) from DuckDB for co-occurrence guard."""
    con = session()
    with _SESSION_LOCK:
        try:
            return con.execute(
                f'SELECT COUNT(DISTINCT "{col_name}") FROM "{table_name}"'
            ).fetchone()[0]
        except Exception:
            return 0


def _duckdb_get_schema(name: str) -> list:
    """Return [{name, type}] for a DuckDB relation (used by schema conversion)."""
    con = session()
    with _SESSION_LOCK:
        try:
            rows = con.execute(
                f'DESCRIBE SELECT * FROM "{name}" LIMIT 0'
            ).fetchall()
            return [{"name": r[0], "type": r[1]} for r in rows]
        except Exception:
            return []


def _relations_to_kg_schema(relations: list) -> Schema:
    """Convert DuckDB Relation list to a core.Schema for the shared builder."""
    tables = []
    for rel in relations:
        duckdb_cols = _duckdb_get_schema(rel.name)
        core_cols = [Column(name=c["name"], data_type=c["type"]) for c in duckdb_cols]
        tables.append(Table(name=rel.name, columns=core_cols))
    return Schema(name="duckdb_session", tables=tables)


def _build_graph(
    relations: list[Relation],
    fk_candidates: list[FKCandidate],
    max_rows: int = _KG_MAX_ROWS_PER_RELATION,
) -> nx.MultiDiGraph:
    """Build the KG graph - CLI adapter that calls the shared core builder."""
    schema = _relations_to_kg_schema(relations)
    return _core_build_kg(
        schema=schema,
        fk_candidates=fk_candidates,
        fetch_rows=_duckdb_fetch_rows,
        max_rows=max_rows,
        fetch_distinct_count=_duckdb_distinct_count,
    )


# ── Exporters ─────────────────────────────────────────────────────────────────

def _export_graphml(G: nx.MultiDiGraph, path: Path) -> None:
    # networkx GraphML serialiser requires string node attributes
    H = nx.MultiDiGraph()
    for nid, attrs in G.nodes(data=True):
        safe_attrs = {
            k: str(v) if not isinstance(v, (str, int, float, bool)) else v
            for k, v in attrs.items()
            if k != "row"  # exclude row dict from GraphML
        }
        H.add_node(str(nid), **safe_attrs)
    for src, tgt, attrs in G.edges(data=True):
        safe_attrs = {k: str(v) for k, v in attrs.items()}
        H.add_edge(str(src), str(tgt), **safe_attrs)
    nx.write_graphml(H, str(path))


def _export_jsonld(G: nx.MultiDiGraph, path: Path) -> None:
    graph_entries = []

    for nid, attrs in G.nodes(data=True):
        entry = {
            "@id": f"urn:nybble:{nid}",
            "@type": "schema:Thing",
            "schema:name": attrs.get("label", nid),
            "schema:identifier": str(nid),
            "nybble:entityType": attrs.get("entity_type", ""),
            "nybble:pkCol": str(attrs.get("pk_col", "")),
            "nybble:pkVal": str(attrs.get("pk_val", "")),
        }
        graph_entries.append(entry)

    for src, tgt, attrs in G.edges(data=True):
        graph_entries.append({
            "@type": "schema:PropertyValue",
            "schema:name": attrs.get("relation", "related"),
            "nybble:source": f"urn:nybble:{src}",
            "nybble:target": f"urn:nybble:{tgt}",
            "nybble:confidence": attrs.get("confidence", 1.0),
        })

    doc = {
        "@context": {
            "schema": "https://schema.org/",
            "nybble": "https://github.com/datajoins/nybble/kg#",
        },
        "@graph": graph_entries,
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def _export_pgq_ddl(
    relations: list,
    fk_candidates: list,
    path: Path,
) -> None:
    """Export a DuckDB SQL/PGQ ``CREATE PROPERTY GRAPH`` DDL statement.

    The generated DDL is compatible with DuckDB's experimental SQL/PGQ
    extension (available from DuckDB 0.10+). Users can pipe it into DuckDB
    directly to enable Cypher-like graph traversal over their relational data::

        nybble kg --export pgq | duckdb mydb.duckdb

    Parameters
    ----------
    relations:
        The list of Relation objects in the graph.
    fk_candidates:
        FK candidates returned by infer_fks().
    path:
        Destination ``.sql`` file.
    """
    from nybble.infer_fk import FKCandidate

    lines: list[str] = [
        "-- Nybble SQL/PGQ DDL - generated by `nybble kg --export pgq`",
        "-- DuckDB 0.10+ required.  Pipe into DuckDB: duckdb mydb.duckdb < kg.sql",
        "",
        "CREATE OR REPLACE PROPERTY GRAPH nybble_graph",
        "VERTEX TABLES (",
    ]

    # Vertex tables - one per relation
    rel_names = [r.name for r in relations]
    for i, rel in enumerate(relations):
        comma = "" if i == len(relations) - 1 else ","
        # Determine PK column heuristic (same as kg._pk_col_for)
        from nybble.resolver import session, _SESSION_LOCK
        con = session()
        with _SESSION_LOCK:
            try:
                cols = [r[0] for r in con.execute(
                    f'DESCRIBE SELECT * FROM "{rel.name}" LIMIT 0'
                ).fetchall()]
            except Exception:
                cols = []
        pk_col = None
        for c in cols:
            if c.lower() in ("id", f"{rel.name.lower()}_id", f"{rel.name.lower()}id"):
                pk_col = c
                break
        if pk_col is None and cols:
            pk_col = cols[0]
        key_clause = f" KEY ({pk_col})" if pk_col else ""
        lines.append(f'  "{rel.name}"{key_clause}{comma}')

    lines.append(")")
    lines.append("EDGE TABLES (")

    # Edge tables - one per FK candidate (only within the loaded relations)
    table_names = set(rel_names)
    edges = [
        fk for fk in fk_candidates
        if fk.src_table in table_names and fk.tgt_table in table_names
    ]
    for i, fk in enumerate(edges):
        comma = "" if i == len(edges) - 1 else ","
        # DuckDB PGQ edge table: SOURCE KEY (col) REFERENCES table DESTINATION KEY (col) REFERENCES table
        lines.append(
            f'  "{fk.src_table}" '
            f"SOURCE KEY ({fk.src_col}) REFERENCES \"{fk.src_table}\" "
            f"DESTINATION KEY ({fk.tgt_col}) REFERENCES \"{fk.tgt_table}\"{comma}"
        )

    if not edges:
        # DuckDB requires at least one edge table; emit a stub
        lines.append("  -- No FK edges detected; add edge tables manually if needed")

    lines.extend([");", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def _select_display_nodes(
    G: nx.MultiDiGraph,
    display_limit: int = 500,
) -> list:
    """Return up to *display_limit* node dicts using a degree-sorted, entity-spread strategy.

    Strategy (no extra dependencies):
    1. Rank all nodes by degree descending — high-connectivity nodes first.
    2. Keep at least ``_MIN_PER_TYPE`` nodes per entity type so no table is
       invisible (up to the limit).
    3. Fill remaining slots from the ranked list.

    This ensures that the visible portion of a large graph is the most
    *structurally interesting* part (hub nodes and their neighbours) rather than
    an arbitrary leading slice.
    """
    _MIN_PER_TYPE = 5

    # Pre-compute degrees once
    degree_map = dict(G.degree())
    all_nodes = sorted(
        G.nodes(data=True),
        key=lambda x: -degree_map.get(x[0], 0),
    )

    # Guarantee minimum representation per entity type
    selected: list = []
    selected_ids: set = set()
    per_type: dict = {}

    for nid, attrs in all_nodes:
        etype = attrs.get("entity_type", "")
        if per_type.get(etype, 0) < _MIN_PER_TYPE:
            selected.append({"id": nid, "attrs": attrs})
            selected_ids.add(nid)
            per_type[etype] = per_type.get(etype, 0) + 1
        if len(selected) >= display_limit:
            break

    # Fill remaining slots with highest-degree nodes not yet included
    if len(selected) < display_limit:
        for nid, attrs in all_nodes:
            if nid not in selected_ids:
                selected.append({"id": nid, "attrs": attrs})
                selected_ids.add(nid)
            if len(selected) >= display_limit:
                break

    return [
        {
            "id": n["id"],
            "label": n["attrs"].get("label", n["id"])[:50],
            "entity_type": n["attrs"].get("entity_type", ""),
            "degree": degree_map.get(n["id"], 0),
        }
        for n in selected
    ]


def _export_html(G: nx.MultiDiGraph, path: Path) -> None:
    """Render a D3 force-directed graph HTML visualization."""
    nodes = _select_display_nodes(G, display_limit=500)
    display_node_ids = {n["id"] for n in nodes}

    edges_data = []
    for src, tgt, attrs in G.edges(data=True):
        if src in display_node_ids and tgt in display_node_ids:
            edges_data.append({
                "source": src,
                "target": tgt,
                "relation": attrs.get("relation", ""),
                "confidence": attrs.get("confidence", 1.0),
            })
        if len(edges_data) >= 1000:
            break

    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=False)
    template = env.get_template("kg.html.jinja")
    html = template.render(
        kg_nodes_json=json.dumps(nodes),
        kg_edges_json=json.dumps(edges_data),
        node_count=G.number_of_nodes(),
        edge_count=G.number_of_edges(),
    )
    path.write_text(html, encoding="utf-8")


# ── Summary ───────────────────────────────────────────────────────────────────

def _build_summary(G: nx.MultiDiGraph) -> dict:
    if G.number_of_nodes() == 0:
        return {
            "node_count": 0, "edge_count": 0,
            "entity_type_counts": {}, "relation_type_counts": {},
            "avg_degree": 0.0, "isolated_node_count": 0,
        }

    entity_counts: dict[str, int] = {}
    for _, attrs in G.nodes(data=True):
        etype = attrs.get("entity_type", "unknown")
        entity_counts[etype] = entity_counts.get(etype, 0) + 1

    relation_counts: dict[str, int] = {}
    for _, _, attrs in G.edges(data=True):
        rel = attrs.get("relation", "related").split(":")[0]  # prefix only
        relation_counts[rel] = relation_counts.get(rel, 0) + 1

    degrees = [d for _, d in G.degree()]
    avg_deg = sum(degrees) / len(degrees) if degrees else 0.0
    isolated = sum(1 for d in degrees if d == 0)

    return {
        "node_count": G.number_of_nodes(),
        "edge_count": G.number_of_edges(),
        "entity_type_counts": entity_counts,
        "relation_type_counts": relation_counts,
        "avg_degree": round(avg_deg, 3),
        "isolated_node_count": isolated,
    }


# ── Pure-Python KG analytics (no server dependency) ──────────────────────────
# These functions mirror what the Flask /api/kg/* routes do, but operate
# directly on a networkx graph so they work in the CLI and MCP contexts
# without needing nybble serve to be running.


def _build_adjacency(G: nx.MultiDiGraph) -> dict:
    """Return {node_id: set(neighbour_ids)} for undirected neighbourhood queries."""
    adj: dict = {}
    for src, tgt in G.edges():
        adj.setdefault(src, set()).add(tgt)
        adj.setdefault(tgt, set()).add(src)
    return adj


def frd_scores(
    relations: Optional[list[Union[str, "Relation"]]] = None,
    top_k: int = 50,
) -> list[dict]:
    """Compute Frequency-Relative Degree (FRD) scores for all KG nodes.

    FRD(v) = degree(v) × N / count(v)

    where N is the total row count of the primary source table and count(v)
    is the empirical occurrence frequency of the entity value.  High FRD
    nodes are structurally surprising — they appear rarely in the data but
    are highly connected in the graph, making them important conceptual hubs.

    Parameters
    ----------
    relations:
        Sources to include. Defaults to all registered.
    top_k:
        Return only the top-k nodes by FRD score (default 50; pass 0 for all).

    Returns
    -------
    list of dicts with keys:
        node_id, label, entity_type, frd_score, degree, count, N
    sorted by frd_score descending.
    """
    if relations is None:
        rels = list(_REGISTERED.values())
    else:
        rels = [
            _REGISTERED[r] if isinstance(r, str) and r in _REGISTERED else r
            for r in relations
        ]
    if not rels:
        raise ValueError("No relations loaded. Use nybble.load() first.")

    fk_candidates = infer_fks(rels)
    G = _build_graph(rels, fk_candidates)
    adj = _build_adjacency(G)

    # N = total rows across all relations (best proxy for entity universe size)
    N = max(sum(r.row_count for r in rels if hasattr(r, "row_count")), 1)

    results = []
    for nid, attrs in G.nodes(data=True):
        count = max(int(attrs.get("count", 1) or 1), 1)
        deg = len(adj.get(nid, set()))
        frd = round((deg * N) / count, 2)
        results.append({
            "node_id":     nid,
            "label":       attrs.get("label", nid),
            "entity_type": attrs.get("entity_type", ""),
            "frd_score":   frd,
            "degree":      deg,
            "count":       count,
            "N":           N,
        })

    results.sort(key=lambda x: -x["frd_score"])
    return results[:top_k] if top_k > 0 else results


def frd_expand(
    seed_node_id: str,
    depth: int = 1,
    max_nodes: int = 200,
    relations: Optional[list[Union[str, "Relation"]]] = None,
) -> dict:
    """BFS expansion from *seed_node_id*, ranking each hop's neighbours by FRD.

    Mirrors the logic of ``/api/kg/expand`` without any Flask or SQLite
    dependency — works directly on the networkx graph.

    Parameters
    ----------
    seed_node_id:
        Node ID to start from.  Partial substring match is attempted if the
        exact ID is not found.
    depth:
        BFS hops to expand (1–4).
    max_nodes:
        Safety cap on returned nodes (default 200).
    relations:
        Sources to build the graph from.  Defaults to all registered.

    Returns
    -------
    dict with keys:
        seed, depth, N, node_count, edge_count, frd_max,
        nodes: list of node dicts with frd_score
        edges: list of {source, target, relation} dicts
    """
    if relations is None:
        rels = list(_REGISTERED.values())
    else:
        rels = [
            _REGISTERED[r] if isinstance(r, str) and r in _REGISTERED else r
            for r in relations
        ]
    if not rels:
        raise ValueError("No relations loaded. Use nybble.load() first.")

    depth = max(1, min(4, depth))
    fk_candidates = infer_fks(rels)
    G = _build_graph(rels, fk_candidates)
    adj = _build_adjacency(G)

    N = max(sum(r.row_count for r in rels if hasattr(r, "row_count")), 1)
    degree_map = {nid: len(nbrs) for nid, nbrs in adj.items()}

    # Fuzzy seed resolution
    if seed_node_id not in G:
        seed_node_id = next(
            (nid for nid in G.nodes() if seed_node_id in nid),
            seed_node_id,
        )

    visited: set = {seed_node_id}
    frontier: list = [seed_node_id]
    hop_order: list = [seed_node_id]
    traversal_pairs: set = set()  # frozenset({a, b}) pairs actually traversed

    for _hop in range(depth):
        candidates = []
        for nid in frontier:
            for nb in adj.get(nid, set()):
                if nb not in visited:
                    nb_attrs = G.nodes[nb] if nb in G else {}
                    nb_count = max(int(nb_attrs.get("count", 1) or 1), 1)
                    nb_deg   = degree_map.get(nb, 1)
                    frd      = (nb_deg * N) / nb_count
                    candidates.append((frd, nb, nid))

        candidates.sort(key=lambda x: -x[0])
        next_frontier = []
        for frd_score, nb, parent in candidates:
            if nb not in visited and len(hop_order) < max_nodes:
                visited.add(nb)
                next_frontier.append(nb)
                hop_order.append(nb)
                traversal_pairs.add(frozenset({parent, nb}))
        frontier = next_frontier
        if not frontier:
            break

    # Build output nodes with FRD scores
    out_nodes = []
    for nid in hop_order:
        if nid not in G:
            continue
        attrs      = G.nodes[nid]
        count      = max(int(attrs.get("count", 1) or 1), 1)
        deg        = degree_map.get(nid, 0)
        frd        = round((deg * N) / count, 2)
        out_nodes.append({
            "node_id":     nid,
            "label":       attrs.get("label", nid),
            "entity_type": attrs.get("entity_type", ""),
            "frd_score":   frd,
            "degree":      deg,
            "count":       count,
            "is_seed":     nid == seed_node_id,
        })

    # Only include traversal edges
    out_edges = []
    for src, tgt, attrs in G.edges(data=True):
        if frozenset({src, tgt}) in traversal_pairs:
            out_edges.append({
                "source":   src,
                "target":   tgt,
                "relation": attrs.get("relation", ""),
            })

    frd_max = max((n["frd_score"] for n in out_nodes), default=1.0)

    return {
        "seed":       seed_node_id,
        "depth":      depth,
        "N":          N,
        "node_count": len(out_nodes),
        "edge_count": len(out_edges),
        "frd_max":    frd_max,
        "nodes":      out_nodes,
        "edges":      out_edges,
    }


def node_detail(
    node_id: str,
    relations: Optional[list[Union[str, "Relation"]]] = None,
) -> dict:
    """Return full detail for a single KG node: metadata + all connections.

    Parameters
    ----------
    node_id:
        Exact node ID.  Partial substring match attempted if not found.
    relations:
        Sources to build the graph from.  Defaults to all registered.

    Returns
    -------
    dict with keys:
        node_id, label, entity_type, degree, count,
        attributes (all node attrs),
        connections: list of {direction, neighbour_id, neighbour_label,
                              neighbour_type, relation}
    """
    if relations is None:
        rels = list(_REGISTERED.values())
    else:
        rels = [
            _REGISTERED[r] if isinstance(r, str) and r in _REGISTERED else r
            for r in relations
        ]
    if not rels:
        raise ValueError("No relations loaded. Use nybble.load() first.")

    fk_candidates = infer_fks(rels)
    G = _build_graph(rels, fk_candidates)

    if node_id not in G:
        node_id = next((nid for nid in G.nodes() if node_id in nid), node_id)
    if node_id not in G:
        raise KeyError(f"Node {node_id!r} not found in the Knowledge Graph.")

    attrs = G.nodes[node_id]
    degree = G.degree(node_id)

    connections = []
    for _, tgt, eattrs in G.out_edges(node_id, data=True):
        tgt_attrs = G.nodes.get(tgt, {})
        connections.append({
            "direction":       "→",
            "neighbour_id":    tgt,
            "neighbour_label": tgt_attrs.get("label", tgt),
            "neighbour_type":  tgt_attrs.get("entity_type", ""),
            "relation":        eattrs.get("relation", ""),
        })
    for src, _, eattrs in G.in_edges(node_id, data=True):
        src_attrs = G.nodes.get(src, {})
        connections.append({
            "direction":       "←",
            "neighbour_id":    src,
            "neighbour_label": src_attrs.get("label", src),
            "neighbour_type":  src_attrs.get("entity_type", ""),
            "relation":        eattrs.get("relation", ""),
        })

    safe_attrs = {
        k: v for k, v in attrs.items()
        if isinstance(v, (str, int, float, bool, type(None)))
    }

    return {
        "node_id":     node_id,
        "label":       attrs.get("label", node_id),
        "entity_type": attrs.get("entity_type", ""),
        "degree":      degree,
        "count":       attrs.get("count", None),
        "attributes":  safe_attrs,
        "connections": connections,
    }


def kg_graph(
    relations: Optional[list[Union[str, "Relation"]]] = None,
    display_limit: int = 500,
) -> dict:
    """Return the KG as node and edge lists, suitable for rendering or analysis.

    Parameters
    ----------
    relations:
        Sources to include. Defaults to all registered.
    display_limit:
        Maximum nodes to return (degree-sorted, entity-spread).

    Returns
    -------
    dict with keys:
        node_count (total), edge_count (total),
        nodes: list of {node_id, label, entity_type, degree}
        edges: list of {source, target, relation, confidence}
    """
    if relations is None:
        rels = list(_REGISTERED.values())
    else:
        rels = [
            _REGISTERED[r] if isinstance(r, str) and r in _REGISTERED else r
            for r in relations
        ]
    if not rels:
        raise ValueError("No relations loaded. Use nybble.load() first.")

    fk_candidates = infer_fks(rels)
    G = _build_graph(rels, fk_candidates)
    display_nodes = _select_display_nodes(G, display_limit=display_limit)
    display_ids = {n["id"] for n in display_nodes}

    edges = []
    for src, tgt, attrs in G.edges(data=True):
        if src in display_ids and tgt in display_ids:
            edges.append({
                "source":     src,
                "target":     tgt,
                "relation":   attrs.get("relation", ""),
                "confidence": attrs.get("confidence", 1.0),
            })
        if len(edges) >= 2000:
            break

    return {
        "node_count": G.number_of_nodes(),
        "edge_count": G.number_of_edges(),
        "nodes": display_nodes,
        "edges": edges,
    }


def kg_validate(
    relations: Optional[list[Union[str, "Relation"]]] = None,
) -> dict:
    """Run structural consistency checks on the Knowledge Graph.

    Checks performed
    ----------------
    - Isolated nodes (degree 0) — entities with no relationships
    - Self-loops — edges where source == target
    - Duplicate edges — multiple edges with the same relation between same nodes
    - Weakly connected components — how fragmented the graph is
    - Missing labels — nodes with no label attribute

    Returns
    -------
    dict with keys:
        passed: bool (True if no warnings)
        node_count, edge_count,
        issues: list of {severity, check, description, affected_nodes}
    """
    if relations is None:
        rels = list(_REGISTERED.values())
    else:
        rels = [
            _REGISTERED[r] if isinstance(r, str) and r in _REGISTERED else r
            for r in relations
        ]
    if not rels:
        raise ValueError("No relations loaded. Use nybble.load() first.")

    fk_candidates = infer_fks(rels)
    G = _build_graph(rels, fk_candidates)

    issues = []

    # Isolated nodes
    isolated = [nid for nid, deg in G.degree() if deg == 0]
    if isolated:
        issues.append({
            "severity":      "warning",
            "check":         "isolated_nodes",
            "description":   f"{len(isolated)} node(s) have no relationships",
            "affected_count": len(isolated),
            "sample":        isolated[:5],
        })

    # Self-loops
    self_loops = [(s, t) for s, t in G.edges() if s == t]
    if self_loops:
        issues.append({
            "severity":      "warning",
            "check":         "self_loops",
            "description":   f"{len(self_loops)} self-loop edge(s) detected",
            "affected_count": len(self_loops),
            "sample":        [s for s, _ in self_loops[:5]],
        })

    # Missing labels
    no_label = [nid for nid, attrs in G.nodes(data=True) if not attrs.get("label")]
    if no_label:
        issues.append({
            "severity":      "info",
            "check":         "missing_labels",
            "description":   f"{len(no_label)} node(s) have no label",
            "affected_count": len(no_label),
            "sample":        no_label[:5],
        })

    # Weakly connected components
    undirected = G.to_undirected()
    components = list(nx.connected_components(undirected))
    if len(components) > 1:
        sizes = sorted([len(c) for c in components], reverse=True)
        issues.append({
            "severity":      "info",
            "check":         "disconnected_components",
            "description":   (
                f"Graph has {len(components)} weakly connected components "
                f"(largest: {sizes[0]} nodes, smallest: {sizes[-1]} nodes)"
            ),
            "affected_count": len(components),
            "component_sizes": sizes[:10],
        })

    return {
        "passed":      len([i for i in issues if i["severity"] == "warning"]) == 0,
        "node_count":  G.number_of_nodes(),
        "edge_count":  G.number_of_edges(),
        "issues":      issues,
    }


# ── Serve-UI helpers ──────────────────────────────────────────────────────────

_DEFAULT_UI_PORT = 7700  # mirrors cli.py _DEFAULT_UI_PORT


def _serve_ui_kg_url(port: int = _DEFAULT_UI_PORT) -> Optional[str]:
    """Return the /kg URL if ``nybble serve`` is already running, else None.

    This lets ``open_browser=True`` open the full Cytoscape interactive UI
    instead of the static jinja export when the server is already up.
    """
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return f"http://localhost:{port}/kg"
    except OSError:
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def kg(
    relations: Optional[list[Union[str, Relation]]] = None,
    output_dir: Optional[Union[str, Path]] = None,
    open_browser: bool = True,
    export_pgq: bool = False,
) -> KGResult:
    """Build a Knowledge Graph from loaded relations and export in all formats.

    Parameters
    ----------
    relations:
        List of Relation objects or table names. Defaults to all registered.
    output_dir:
        Directory for output files. Defaults to .nybble/.
    open_browser:
        Automatically open the HTML visualisation.
    export_pgq:
        When True, also write a ``kg.sql`` file containing a DuckDB
        ``CREATE PROPERTY GRAPH`` DDL statement compatible with SQL/PGQ.

    Returns
    -------
    KGResult
    """
    # Resolve relations
    if relations is None:
        rels = list(_REGISTERED.values())
    else:
        rels = []
        for r in relations:
            if isinstance(r, str):
                if r in _REGISTERED:
                    rels.append(_REGISTERED[r])
                else:
                    from nybble.resolver import load
                    rels.append(load(r))
            else:
                rels.append(r)

    if not rels:
        raise ValueError("No relations loaded. Use nybble.load() first.")

    # Read max_rows from nybble.yaml if present; fall back to module default.
    max_rows = _KG_MAX_ROWS_PER_RELATION
    try:
        from nybble.config import find_config
        cfg_path = find_config()
        if cfg_path is not None:
            import yaml
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            val = raw.get("knowledge_graph", {}).get("max_rows_per_relation")
            if val is not None:
                max_rows = int(val)
    except Exception:
        pass  # config absent, pyyaml not installed, or invalid value - use default

    fk_candidates = infer_fks(rels)
    G = _build_graph(rels, fk_candidates, max_rows=max_rows)
    summary = _build_summary(G)

    out_dir = Path(output_dir) if output_dir else _DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    graphml_path = out_dir / "kg.graphml"
    jsonld_path  = out_dir / "kg.jsonld"
    html_path    = out_dir / "kg.html"

    _export_graphml(G, graphml_path)
    _export_jsonld(G, jsonld_path)
    _export_html(G, html_path)

    pgq_path: Optional[Path] = None
    if export_pgq:
        pgq_path = out_dir / "kg.sql"
        _export_pgq_ddl(rels, fk_candidates, pgq_path)

    if open_browser:
        # Prefer the full interactive serve UI over the static export.
        # Check if `nybble serve` is already running on the default port and
        # open /kg there; fall back to the static file URI if it isn't.
        _url = _serve_ui_kg_url()
        if _url:
            webbrowser.open(_url)
        else:
            webbrowser.open(html_path.resolve().as_uri())

    return KGResult(
        graph=G,
        graphml_path=graphml_path,
        jsonld_path=jsonld_path,
        html_path=html_path,
        summary=summary,
        pgq_path=pgq_path,
    )


def kg_summary(
    relations: Optional[list[Union[str, Relation]]] = None,
) -> dict:
    """Return a summary dict suitable for LLM context injection.

    Builds a lightweight knowledge graph (schema-only, no row data)
    and returns counts and metrics without writing any files.
    """
    if relations is None:
        rels = list(_REGISTERED.values())
    else:
        rels = [
            (_REGISTERED[r] if isinstance(r, str) and r in _REGISTERED
             else (Relation(r, r, "file") if isinstance(r, str) else r))
            for r in relations
        ]

    if not rels:
        return {
            "node_count": 0, "edge_count": 0,
            "entity_type_counts": {}, "relation_type_counts": {},
            "avg_degree": 0.0, "isolated_node_count": 0,
        }

    fk_candidates = infer_fks(rels)
    # Lightweight summary: just count entities and relationships from schema
    entity_counts = {r.name: r.row_count for r in rels}
    relation_counts = {}
    for fk in fk_candidates:
        rel_key = f"FK:tier{fk.tier}"
        relation_counts[rel_key] = relation_counts.get(rel_key, 0) + 1

    return {
        "sources": [r.name for r in rels],
        "source_count": len(rels),
        "entity_type_counts": entity_counts,
        "inferred_fk_count": len(fk_candidates),
        "fk_by_tier": relation_counts,
        "total_rows_estimate": sum(r.row_count or 0 for r in rels),
    }


# ── Semantic / substring node search ─────────────────────────────────────────

@dataclass
class NodeSearchResult:
    """A single result from search_nodes().

    Attributes
    ----------
    node_id     : internal graph node ID (e.g. ``"customers:1"``)
    label       : human-readable label stored on the node
    entity_type : the source table name the node came from
    score       : relevance score - cosine similarity (0-1) in semantic mode;
                  1.0 for exact substring match, 0.5 for case-insensitive match
    match_mode  : ``"semantic"`` when sentence-transformers was used,
                  ``"substring"`` when falling back to text search
    row         : original row dict (may be empty when graph was built without rows)
    """
    node_id: str
    label: str
    entity_type: str
    score: float
    match_mode: str
    row: dict = field(default_factory=dict)


def _has_sentence_transformers() -> bool:
    """Return True if sentence-transformers is importable."""
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def _substring_search(
    query: str,
    G: nx.MultiDiGraph,
    top_k: int,
) -> list[NodeSearchResult]:
    """Token-index search over node labels and all string column values.

    Uses the inverted token index (built by ``build_search_index``) when the
    graph nodes carry ``_search_tokens`` attributes (all graphs built with the
    current kg_builder).  Falls back to a linear label scan for older graphs.

    Scoring (per token, then normalised):
      1.0  exact match on label
      0.8  case-insensitive exact on label
      0.5  token found in label (substring)
      0.3  token found in another string column value

    Multi-token queries ("Alice US") accumulate scores so nodes matching all
    tokens rank above nodes matching only one.
    """
    # Fast path: use the inverted index if nodes have _search_tokens.
    # Check the first node only — avoids a full scan just to detect presence.
    first_node_data = next(iter(G.nodes(data=True)), (None, {}))[1]
    has_tokens = "_search_tokens" in first_node_data

    if has_tokens:
        index = _build_search_index(G)
        hits = _index_search(query, index, G, top_k=top_k)
        return [
            NodeSearchResult(
                node_id=node_id,
                label=label,
                entity_type=entity_type,
                score=score,
                match_mode="substring",
                row=row,
            )
            for score, label, node_id, entity_type, row in hits
        ]

    # Legacy fallback: linear O(n) label-only scan for graphs without _search_tokens
    q_lower = query.lower()
    scored: list[tuple[float, str, str, str, dict]] = []

    for node_id, attrs in G.nodes(data=True):
        label: str = attrs.get("label", "") or ""
        entity_type: str = attrs.get("entity_type", "")
        row: dict = attrs.get("row", {})
        label_lower = label.lower()

        if label == query:
            score = 1.0
        elif label_lower == q_lower:
            score = 0.8
        elif q_lower in label_lower:
            score = 0.5
        else:
            continue

        scored.append((score, label, node_id, entity_type, row))

    scored.sort(key=lambda x: (-x[0], x[1]))

    return [
        NodeSearchResult(
            node_id=node_id,
            label=label,
            entity_type=entity_type,
            score=score,
            match_mode="substring",
            row=row,
        )
        for score, label, node_id, entity_type, row in scored[:top_k]
    ]


def _semantic_search(
    query: str,
    G: nx.MultiDiGraph,
    top_k: int,
) -> list[NodeSearchResult]:
    """Semantic search using sentence-transformers cosine similarity.

    Encodes all node labels into a matrix, encodes the query, then returns
    the top-K nodes by cosine similarity.  All computation is local - no
    network calls, no API keys, no token spend.

    The model is loaded once per Python process via the standard
    sentence-transformers model cache (``~/.cache/torch/sentence_transformers``).
    """
    import sys as _sys
    # Use sys.modules lookups so that test mocks injected via patch.dict are
    # respected even when the real package is already cached in sys.modules.
    _st_mod   = _sys.modules.get("sentence_transformers")
    _util_mod = _sys.modules.get("sentence_transformers.util")
    if _st_mod is None or _util_mod is None:
        raise ImportError("sentence_transformers not available")
    SentenceTransformer = _st_mod.SentenceTransformer  # type: ignore[attr-defined]
    cos_sim             = _util_mod.cos_sim             # type: ignore[attr-defined]

    node_ids: list[str] = []
    labels: list[str] = []
    entity_types: list[str] = []
    rows: list[dict] = []

    for node_id, attrs in G.nodes(data=True):
        label: str = attrs.get("label", "") or ""
        node_ids.append(node_id)
        labels.append(label)
        entity_types.append(attrs.get("entity_type", ""))
        rows.append(attrs.get("row", {}))

    if not node_ids:
        return []

    # Load a small, fast model (22 MB).  Users can override via
    # NYBBLE_EMBEDDING_MODEL env var for higher accuracy at the cost of speed.
    model_name = os.environ.get(
        "NYBBLE_EMBEDDING_MODEL",
        "sentence-transformers/all-MiniLM-L6-v2",
    )
    model = SentenceTransformer(model_name)

    # Encode all labels + the query in one batch for efficiency
    corpus_embeddings = model.encode(labels, convert_to_tensor=True, show_progress_bar=False)
    query_embedding = model.encode([query], convert_to_tensor=True, show_progress_bar=False)

    # cos_sim returns a (1 × N) tensor
    scores_tensor = cos_sim(query_embedding, corpus_embeddings)[0]
    scores: list[float] = scores_tensor.tolist()

    # Pair and sort
    indexed = sorted(
        zip(scores, node_ids, labels, entity_types, rows),
        key=lambda x: -x[0],
    )

    return [
        NodeSearchResult(
            node_id=node_id,
            label=label,
            entity_type=entity_type,
            score=round(float(score), 4),
            match_mode="semantic",
            row=row,
        )
        for score, node_id, label, entity_type, row in indexed[:top_k]
    ]


def search_nodes(
    query: str,
    graph: Optional[nx.MultiDiGraph] = None,
    top_k: int = 10,
) -> list[NodeSearchResult]:
    """Search KG nodes by text query.

    Uses sentence-transformers for local semantic similarity when the
    ``[semantic]`` extra is installed, and falls back to case-insensitive
    substring matching otherwise.

    The structural FK inference and graph topology are **never affected** by
    this function - embeddings are additive for search only.

    Parameters
    ----------
    query:
        Free-text search string, e.g. ``"customer from Germany"``.
    graph:
        A ``networkx.MultiDiGraph`` built by :func:`kg` or :func:`_build_graph`.
        When ``None``, a lightweight graph is built from all currently loaded
        relations (equivalent to calling ``kg()`` without writing files).
    top_k:
        Maximum number of results to return.  Defaults to 10.

    Returns
    -------
    list[NodeSearchResult]
        Results sorted by relevance score descending.

    Notes
    -----
    Install sentence-transformers for semantic mode::

        pip install "datajoins-nybble[semantic]"

    Without it, substring matching is used automatically - no error is raised.
    The ``match_mode`` field on each result indicates which backend was used.

    Override the default embedding model via environment variable::

        NYBBLE_EMBEDDING_MODEL=all-mpnet-base-v2 nybble kg search "..."
    """
    if not query or not query.strip():
        return []

    # Build or use the supplied graph
    G: nx.MultiDiGraph
    if graph is not None:
        G = graph
    else:
        rels = list(_REGISTERED.values())
        if not rels:
            raise ValueError("No relations loaded. Use nybble.load() first.")
        fk_candidates = infer_fks(rels)
        G = _build_graph(rels, fk_candidates)

    if G.number_of_nodes() == 0:
        return []

    if _has_sentence_transformers():
        try:
            return _semantic_search(query, G, top_k)
        except Exception:
            # Any runtime failure (model download error, OOM, etc.) falls back
            # gracefully rather than surfacing a confusing traceback.
            pass

    return _substring_search(query, G, top_k)


# ── DuckDB SQL/PGQ DDL generation ────────────────────────────────────────────
#
# This section emits DuckDB-compatible ``CREATE PROPERTY GRAPH`` DDL from
# the graph and FK candidates already produced by Nybble Sense.  It is NOT a
# general-purpose graph-export utility - the output is specifically formatted
# for DuckDB's experimental SQL/PGQ extension (DuckDB 0.10+) and is only
# meaningful when the underlying relational tables exist in a DuckDB session.
#
# Syntax reference (DuckDB SQL/PGQ):
#
#   CREATE [OR REPLACE] PROPERTY GRAPH graph_name
#   VERTEX TABLES (
#       table_name [KEY (pk_col)] [, ...]
#   )
#   EDGE TABLES (
#       edge_table
#           SOURCE KEY (fk_col) REFERENCES vertex_table (pk_col)
#           DESTINATION KEY (pk_col) REFERENCES dest_vertex_table (pk_col)
#       [, ...]
#   );
#
# Each FK candidate ``src_table.src_col → tgt_table.tgt_col`` becomes one
# entry in EDGE TABLES, using ``src_table`` as the edge table.  The ``src_col``
# is the FK column in the edge table and ``tgt_col`` is the PK column in the
# destination vertex table.  The SOURCE side self-references the source table
# via its inferred PK column (kept in the graph's ``pk_col`` node attribute).


def generate_pgq_ddl(
    G: nx.MultiDiGraph,
    fk_candidates: list,
    graph_name: str = "nybble_graph",
) -> str:
    """Generate a DuckDB ``CREATE PROPERTY GRAPH`` DDL string.

    Converts the NetworkX graph and FK candidates already produced by Nybble
    Sense into a DuckDB SQL/PGQ DDL statement.  The result can be piped
    directly into DuckDB to enable Cypher-like graph traversal::

        nybble kg pgq --source mydb.sqlite | duckdb mydb.duckdb

    Unlike the file-writing :func:`_export_pgq_ddl`, this function returns a
    plain string and does **not** write to disk, re-infer FK relationships, or
    modify any state.

    Parameters
    ----------
    G:
        A ``networkx.MultiDiGraph`` built by :func:`_build_graph` or
        :func:`kg`.  Each node must have an ``entity_type`` attribute
        (the source table name) and optionally a ``pk_col`` attribute.
    fk_candidates:
        FK candidates as returned by :func:`~nybble.infer_fk.infer_fks`.
        Must be passed in - this function does **not** re-run inference.
    graph_name:
        Name for the ``CREATE PROPERTY GRAPH`` statement.  Defaults to
        ``"nybble_graph"``.  Must be a valid SQL identifier.

    Returns
    -------
    str
        A DuckDB-compatible ``CREATE OR REPLACE PROPERTY GRAPH`` DDL string,
        or an empty string when ``G`` contains no nodes.

    Notes
    -----
    DuckDB 0.10+ is required.  The ``CREATE PROPERTY GRAPH`` syntax is
    experimental in DuckDB and subject to change in future DuckDB releases.
    The generated DDL assumes all vertex tables already exist in the DuckDB
    session (i.e. they have been loaded via ``nybble.load()`` beforehand).
    """
    if G.number_of_nodes() == 0:
        return ""

    # ── Collect vertex tables from graph node entity_type attributes ──────────
    # Each unique entity_type is one vertex table.  Preserve the first PK
    # column seen for each table (consistent within a single _build_graph run).
    vertex_tables: dict[str, Optional[str]] = {}   # table_name → pk_col | None
    for _node_id, attrs in G.nodes(data=True):
        table = attrs.get("entity_type", "")
        if not table:
            continue
        if table not in vertex_tables:
            vertex_tables[table] = attrs.get("pk_col") or None

    if not vertex_tables:
        return ""

    # ── Collect edge tables from FK candidates ─────────────────────────────
    # Only include FKs where both source and destination tables are vertex tables.
    table_set = set(vertex_tables)
    edges = [
        fk for fk in fk_candidates
        if (getattr(fk, "src_table", None) or fk.get("src_table", "")) in table_set
        and (getattr(fk, "tgt_table", None) or fk.get("tgt_table", "")) in table_set
    ]

    # ── Build DDL lines ────────────────────────────────────────────────────────
    lines: list[str] = [
        "-- Nybble SQL/PGQ DDL",
        "-- Generated by nybble.generate_pgq_ddl()",
        "-- DuckDB 0.10+ required.",
        "-- Pipe into DuckDB:  nybble kg pgq | duckdb mydb.duckdb",
        "",
        f"CREATE OR REPLACE PROPERTY GRAPH {graph_name}",
        "VERTEX TABLES (",
    ]

    vtable_list = sorted(vertex_tables.items())   # stable ordering
    for i, (table, pk_col) in enumerate(vtable_list):
        comma = "" if i == len(vtable_list) - 1 else ","
        key_clause = f" KEY ({pk_col})" if pk_col else ""
        lines.append(f'    "{table}"{key_clause}{comma}')

    lines.append(")")

    if edges:
        lines.append("EDGE TABLES (")
        for i, fk in enumerate(edges):
            # Support both FKCandidate dataclass and plain dict
            src_table = getattr(fk, "src_table", None) or fk.get("src_table", "")
            src_col   = getattr(fk, "src_col",   None) or fk.get("src_col",   "")
            tgt_table = getattr(fk, "tgt_table", None) or fk.get("tgt_table", "")
            tgt_col   = getattr(fk, "tgt_col",   None) or fk.get("tgt_col",   "")

            # The PK of the source table (for the SOURCE side of the edge)
            src_pk = vertex_tables.get(src_table)

            comma = "" if i == len(edges) - 1 else ","

            # DuckDB SQL/PGQ edge syntax:
            #   edge_table
            #     SOURCE KEY (fk_col) REFERENCES src_vertex_table (src_pk_col)
            #     DESTINATION KEY (tgt_pk_col) REFERENCES tgt_vertex_table (tgt_pk_col)
            src_key_clause  = f"SOURCE KEY ({src_col}) REFERENCES \"{src_table}\" ({src_pk})" if src_pk else f"SOURCE KEY ({src_col}) REFERENCES \"{src_table}\""
            dest_key_clause = f"DESTINATION KEY ({tgt_col}) REFERENCES \"{tgt_table}\" ({tgt_col})"

            lines.append(f'    "{src_table}"')
            lines.append(f'        {src_key_clause}')
            lines.append(f'        {dest_key_clause}{comma}')

        lines.append(")")

    lines.append(";")
    lines.append("")   # trailing newline

    return "\n".join(lines)
