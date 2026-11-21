"""
nybble.core.kg_builder - Shared Knowledge Graph builder.

Extracts _build_graph() from nybble/kg.py and parameterises the row-fetch
and schema-introspection operations via callbacks so that the function is
independent of DuckDB, SQLite, or any other data access layer.

Rules (enforced by the integration plan)
-----------------------------------------
* No Flask, DuckDB, SQLite, click, or argparse imports.
* All data access goes through the provided callbacks.
* Operates on nybble.core.schema.Schema + list[FKCandidate].

Public API
----------
  build_kg(schema, fk_candidates, fetch_rows, max_rows=5000) → nx.MultiDiGraph
  build_search_index(G) → dict   (inverted token index for O(1) token search)
  search_index(index, query) → list[tuple[float, str]]  (node_id, score pairs)
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Set, Tuple

import networkx as nx

from nybble.core.schema import Schema

# Maximum distinct values in a string column before co-occurrence linking is skipped
_MAX_COOCCURRENCE_CARDINALITY = 10_000


def _is_string_col(type_str: str) -> bool:
    t = type_str.upper()
    return any(k in t for k in ("VARCHAR", "TEXT", "CHAR", "STRING"))


# ── Tokeniser (shared by index build and query) ───────────────────────────────

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenise(text: str) -> List[str]:
    """Lowercase word-boundary tokens from any string value.

    'Alice Smith / US-101' → ['alice', 'smith', 'us', '101']
    """
    return _TOKEN_RE.findall(text.lower())


def _pk_col_for(table_name: str, schema: Schema) -> Optional[str]:
    """Best-guess PK column name for a table in the schema."""
    table = schema.get_table(table_name)
    if table is None:
        return None
    # Explicitly declared PKs
    if table.primary_keys:
        return table.primary_keys[0]
    for col in table.columns:
        if col.primary_key:
            return col.name
    # Heuristic: column named 'id' or '<table>_id' or '<table>id'
    tname_lower = table_name.lower()
    for col in table.columns:
        if col.name.lower() in ("id", f"{tname_lower}_id", f"{tname_lower}id"):
            return col.name
    return table.columns[0].name if table.columns else None


def build_kg(
    schema: Schema,
    fk_candidates: list,
    fetch_rows: Callable[[str, int], List[dict]],
    max_rows: int = 5_000,
    fetch_distinct_count: Optional[Callable[[str, str], int]] = None,
) -> nx.MultiDiGraph:
    """Build a row-level Knowledge Graph from a Schema and FK candidates.

    Parameters
    ----------
    schema:
        Canonical Schema dataclass (nybble.core.schema.Schema).
    fk_candidates:
        List of FKCandidate objects (tier, confidence, src/tgt table/col).
    fetch_rows:
        Callback: ``fetch_rows(table_name, limit) → list[row_dicts]``.
        The CLI provides a DuckDB-backed implementation; the server provides
        a SQLite-backed one.
    max_rows:
        Maximum rows fetched per table. Default 5,000.
    fetch_distinct_count:
        Optional callback for co-occurrence cardinality guard:
        ``fetch_distinct_count(table_name, col_name) → int``.
        When None, co-occurrence edges are built without the cardinality guard.

    Returns
    -------
    nx.MultiDiGraph
        Nodes: one per data row.  Edges: FK relationships + string co-occurrence.

    Notes
    -----
    Row data is fetched once per table and cached for the entire build to avoid
    redundant DuckDB round-trips across the FK edge and co-occurrence phases.
    """
    G = nx.MultiDiGraph()
    table_names = {t.name for t in schema.tables}

    # Build pk_cols lookup from schema
    pk_cols: Dict[str, Optional[str]] = {
        t.name: _pk_col_for(t.name, schema)
        for t in schema.tables
    }

    # ── Row cache: fetch each table exactly once ──────────────────────────────
    # Previously fetch_rows was called once per entity phase, once per FK
    # candidate (src_table), and once per co-occurrence column pair — meaning
    # a table could be fetched 5-10× for the same limit.  Cache here so all
    # three phases share a single fetch per table.
    _row_cache: Dict[str, List[dict]] = {}

    def _cached_rows(table_name: str) -> List[dict]:
        if table_name not in _row_cache:
            _row_cache[table_name] = fetch_rows(table_name, max_rows)
        return _row_cache[table_name]

    # ── 1. Add entity nodes (one node per row) ────────────────────────────────
    pk_val_index: Dict[str, Dict[str, str]] = {}  # table → {pk_val → node_id}

    for table in schema.tables:
        rows = _cached_rows(table.name)
        pk_col = pk_cols[table.name]

        # Identify best label column
        label_col: Optional[str] = None
        name_hints = ("name", "title", "label", "description",
                      f"{table.name}_name", f"{table.name}name")
        col_names_lower = {c.name.lower(): c.name for c in table.columns}
        for hint in name_hints:
            if hint in col_names_lower:
                label_col = col_names_lower[hint]
                break
        if label_col is None:
            str_cols = [c.name for c in table.columns if _is_string_col(c.data_type)]
            if str_cols:
                label_col = str_cols[0]
            elif table.columns:
                label_col = table.columns[0].name

        # Collect all string column names for this table (used to build search
        # tokens stored on the node — avoids re-scanning the schema later).
        str_col_names: List[str] = [
            c.name for c in table.columns if _is_string_col(c.data_type)
        ]

        pk_val_index[table.name] = {}

        for i, row in enumerate(rows):
            pk_val = str(row.get(pk_col, i)) if pk_col else str(i)
            node_id = f"{table.name}:{pk_val}"
            label = str(row.get(label_col, pk_val)) if label_col else pk_val

            # Build a compact set of search tokens from all string columns.
            # Stored as a frozenset of lowercase tokens so substring search can
            # also match on email, status, country etc. — not just the label —
            # without storing the full row dict in the NetworkX node attrs.
            search_tokens: Set[str] = set(_tokenise(label))
            for col_name in str_col_names:
                val = row.get(col_name)
                if val and col_name != label_col:
                    search_tokens.update(_tokenise(str(val)))

            G.add_node(
                node_id,
                entity_type=table.name,
                label=label[:80],
                pk_col=pk_col,
                pk_val=pk_val,
                row=row,
                _search_tokens=frozenset(search_tokens),
            )
            pk_val_index[table.name][pk_val] = node_id

    # ── 2. FK-based edges ─────────────────────────────────────────────────────
    for fk in fk_candidates:
        if fk.src_table not in table_names or fk.tgt_table not in table_names:
            continue
        src_rows = _cached_rows(fk.src_table)   # ← cache hit, no second fetch
        src_pk = pk_cols.get(fk.src_table)
        tgt_idx = pk_val_index.get(fk.tgt_table, {})

        for i, row in enumerate(src_rows):
            src_pk_val = str(row.get(src_pk, i)) if src_pk else str(i)
            src_node_id = f"{fk.src_table}:{src_pk_val}"
            fk_val = row.get(fk.src_col)
            if fk_val is None:
                continue
            tgt_node_id = tgt_idx.get(str(fk_val))
            if tgt_node_id and src_node_id in G and tgt_node_id in G:
                G.add_edge(
                    src_node_id,
                    tgt_node_id,
                    relation=f"FK:{fk.src_table}.{fk.src_col}→{fk.tgt_table}.{fk.tgt_col}",
                    tier=fk.tier,
                    confidence=fk.confidence,
                )

    # ── 3. Co-occurrence edges ────────────────────────────────────────────────
    table_list = list(schema.tables)
    for i, tbl_a in enumerate(table_list):
        for tbl_b in table_list[i + 1:]:
            str_cols_a = [c for c in tbl_a.columns if _is_string_col(c.data_type)]
            str_cols_b = [c for c in tbl_b.columns if _is_string_col(c.data_type)]

            for col_a in str_cols_a:
                # Cardinality guard
                if fetch_distinct_count is not None:
                    try:
                        card = fetch_distinct_count(tbl_a.name, col_a.name)
                        if card > _MAX_COOCCURRENCE_CARDINALITY:
                            continue
                    except Exception:
                        continue

                rows_a = _cached_rows(tbl_a.name)   # ← cache hit
                vals_a: Dict[str, str] = {}  # col_a_value → pk_val
                pk_a = pk_cols.get(tbl_a.name)
                for ri, row in enumerate(rows_a):
                    v = row.get(col_a.name)
                    if v is not None:
                        pk_v = str(row.get(pk_a, ri)) if pk_a else str(ri)
                        vals_a[str(v)] = pk_v

                for col_b in str_cols_b:
                    if fetch_distinct_count is not None:
                        try:
                            card_b = fetch_distinct_count(tbl_b.name, col_b.name)
                            if card_b > _MAX_COOCCURRENCE_CARDINALITY:
                                continue
                        except Exception:
                            continue

                    pk_b = pk_cols.get(tbl_b.name)
                    rows_b = _cached_rows(tbl_b.name)   # ← cache hit
                    tgt_idx_a = pk_val_index.get(tbl_a.name, {})
                    tgt_idx_b = pk_val_index.get(tbl_b.name, {})

                    matches_seen = 0
                    for ri, row in enumerate(rows_b):
                        v = row.get(col_b.name)
                        if v is None:
                            continue
                        shared_val = str(v)
                        if shared_val not in vals_a:
                            continue
                        if matches_seen >= 1000:
                            break

                        a_pk_val = vals_a[shared_val]
                        b_pk_val = str(row.get(pk_b, ri)) if pk_b else str(ri)

                        src_node_id = tgt_idx_a.get(a_pk_val)
                        tgt_node_id = tgt_idx_b.get(b_pk_val)
                        if src_node_id and tgt_node_id and src_node_id in G and tgt_node_id in G:
                            G.add_edge(
                                src_node_id,
                                tgt_node_id,
                                relation=(
                                    f"cooccurrence:{tbl_a.name}.{col_a.name}"
                                    f"={tbl_b.name}.{col_b.name}"
                                ),
                                shared_value=shared_val[:100],
                                confidence=0.6,
                            )
                            matches_seen += 1

    return G


# ── Inverted token index ──────────────────────────────────────────────────────

def build_search_index(G: nx.MultiDiGraph) -> Dict[str, List[str]]:
    """Build an inverted token index from the graph's ``_search_tokens`` attrs.

    Returns a dict mapping each lowercase token → list of node_ids that contain
    it.  This turns a search from O(n_nodes) scan into O(1) token lookup +
    O(|hits|) scoring.

    The index is pure Python — no new dependency, ~200 bytes per unique token,
    ~40 bytes per (token, node_id) entry.  On 50k nodes with 10 tokens each the
    index takes roughly 15-20 MB vs the graph's own ~100 MB.

    Falls back gracefully: nodes without ``_search_tokens`` are indexed by their
    ``label`` tokens only, so graphs built before this change still work.
    """
    index: Dict[str, List[str]] = {}
    for node_id, attrs in G.nodes(data=True):
        tokens: Set[str] = set(attrs.get("_search_tokens") or set())
        if not tokens:
            # Fallback for graphs built without _search_tokens
            tokens = set(_tokenise(attrs.get("label", "") or ""))
        for tok in tokens:
            if tok not in index:
                index[tok] = []
            index[tok].append(node_id)
    return index


def index_search(
    query: str,
    index: Dict[str, List[str]],
    G: nx.MultiDiGraph,
    top_k: int = 10,
) -> List[Tuple[float, str, str, str, dict]]:
    """Search the inverted token index.  Returns (score, label, node_id, entity_type, row) tuples.

    Scoring:
      +1.0  exact whole-token match on the label field
      +0.8  case-insensitive exact on label
      +0.5  token is in the label (substring)
      +0.3  token found in any other string column value (via _search_tokens)

    Multi-token queries accumulate scores across tokens so a 2-token query
    "Alice US" ranks nodes that match both tokens higher than nodes matching one.
    """
    query_tokens = _tokenise(query)
    if not query_tokens:
        return []

    q_lower = query.lower()
    scores: Dict[str, float] = {}

    for tok in query_tokens:
        # Exact token lookup — O(1) dict access
        candidates: List[str] = index.get(tok, [])
        for node_id in candidates:
            attrs = G.nodes[node_id]
            label: str = attrs.get("label", "") or ""
            label_lower = label.lower()

            if label == query or label_lower == q_lower:
                increment = 1.0
            elif q_lower in label_lower or tok in label_lower:
                increment = 0.5
            else:
                # Token matched in a non-label column
                increment = 0.3

            scores[node_id] = scores.get(node_id, 0.0) + increment

    # Normalise: cap at 1.0 per token so multi-token queries stay in [0,1] range
    n_tokens = len(query_tokens)
    normed = {nid: min(s / n_tokens, 1.0) for nid, s in scores.items()}

    ranked = sorted(normed.items(), key=lambda x: -x[1])[:top_k]

    result = []
    for node_id, score in ranked:
        attrs = G.nodes[node_id]
        result.append((
            round(score, 4),
            attrs.get("label", node_id),
            node_id,
            attrs.get("entity_type", ""),
            attrs.get("row", {}),
        ))
    return result
