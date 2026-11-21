"""
nybble.core.infer_fk - Unified 4-Tier FK Inference Engine.

Applies four inference tiers in strict priority order:

  Tier 1 - Declared FK constraints already present in the Schema dataclass
            (populated by server/ingestion extractors or by nybble/infer_fk.py
            when reading live DB PRAGMA / SQLAlchemy inspection results)
  Tier 2 - Column name suffix matching (_id, _fk, _key, _ref, _uuid, _code)
            with System B's ambiguity resolver: longest-PK-name wins, noise-word
            stripping, multi-table ambiguity rejection
  Tier 3 - Column name equality across tables (only for 'id' / '*_id' names)
  Tier 4 - Statistical uniqueness sampling (>95% unique src → 100% unique tgt)
            Only runs when a fetch_stats callback is provided.

Short-circuits at Tier 1 for any (table, col) pair that already has a declared
constraint, so higher tiers never duplicate confirmed relationships.

Rules (enforced by the integration plan)
-----------------------------------------
* No Flask, DuckDB, SQLite, click, or argparse imports.
* fetch_stats callback is the ONLY interface to real data; passing None skips Tier 4.
* Operates entirely on nybble.core.schema.Schema dataclasses.
* Callable identically from the CLI (DuckDB-backed) and Flask (DDL-only).

Public API
----------
  infer_fks(schema, fetch_stats=None) → list[FKCandidate]
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from nybble.core.schema import Schema

# ---------------------------------------------------------------------------
# FK suffix set (System A's full list)
# ---------------------------------------------------------------------------
_FK_SUFFIXES = ("_id", "_fk", "_key", "_ref", "_uuid", "_code")

# Minimum uniqueness ratio for Tier 4 (source column must be >= this unique)
_TIER4_SRC_THRESHOLD = 0.95

# Uniqueness ratio required for the candidate PK column (target must be 100% unique)
_TIER4_TGT_THRESHOLD = 1.00

# Maximum distinct values to consider for Tier 3/4 to prevent combinatorial explosion
_MAX_DISTINCT = 10_000

# Generic short PK names too common to trust without a column-prefix hint (System B)
_GENERIC_PK_NAMES = frozenset({"id", "pk"})


@dataclass
class FKCandidate:
    """A single inferred (or confirmed) foreign key relationship.

    Attributes
    ----------
    src_table  : str - source (FK-holding) table
    src_col    : str - column in src_table that holds the FK value
    tgt_table  : str - target (PK-holding) table
    tgt_col    : str - column in tgt_table that the FK points to
    tier       : int - inference tier (1-4)
    confidence : float - 0.0-1.0 confidence score
    """
    src_table:  str
    src_col:    str
    tgt_table:  str
    tgt_col:    str
    tier:       int   = 1
    confidence: float = 1.0


# ---------------------------------------------------------------------------
# Type compatibility helper
# ---------------------------------------------------------------------------

def _is_numeric_or_string(type_str: str) -> bool:
    """Return True if the column type is suitable for FK matching."""
    t = type_str.upper()
    return any(k in t for k in (
        "INT", "BIGINT", "SMALLINT", "TINYINT", "HUGEINT",
        "VARCHAR", "TEXT", "CHAR", "STRING", "UUID",
        "SERIAL", "NUMERIC", "DECIMAL",
    ))


# ---------------------------------------------------------------------------
# Tier 1 - Declared FK constraints from Schema dataclass
# ---------------------------------------------------------------------------

def _tier1_declared(schema: Schema) -> list[FKCandidate]:
    """Extract FK constraints already present in the Schema (Tier 1).

    These come from DDL parsing (server path) or live DB introspection
    (nybble/infer_fk.py CLI path which populates schema.tables[].foreign_keys
    before calling us).
    """
    candidates: list[FKCandidate] = []
    for table in schema.tables:
        for fk in table.foreign_keys:
            for src_col, tgt_col in zip(
                fk.constrained_columns,
                fk.referred_columns,
            ):
                candidates.append(FKCandidate(
                    src_table=table.name,
                    src_col=src_col,
                    tgt_table=fk.referred_table,
                    tgt_col=tgt_col,
                    tier=1,
                    confidence=1.0,
                ))
    return candidates


# ---------------------------------------------------------------------------
# System B's ambiguity resolver - used by Tier 2
# ---------------------------------------------------------------------------

def _table_hint_from_col(col_name: str) -> Optional[str]:
    """Extract a likely table-name fragment from a column name (System B logic).

    Strips the trailing ``_id`` (and PK suffix if longer) and common verb-like
    noise prefixes, then returns the innermost meaningful token.

    Examples:
      txn_firm_id        → "firm"
      txn_payment_type_id → "payment_type"  (multi-word)
      firm_id            → None  (noise prefix stripped, nothing left)
    """
    parts = col_name.split("_")

    # Remove trailing "id" token(s)
    while parts and parts[-1] == "id":
        parts.pop()

    if not parts:
        return None

    # Drop well-known noise prefixes that don't name a table (System B's list).
    # Note: "order" is intentionally excluded - "order_id" legitimately references
    # an "orders" table. Only generic transaction-level prefixes that do NOT
    # themselves name a table are included.
    NOISE = {
        "txn", "lineitem", "kb", "adjustment", "serial", "mapping",
        "party", "data", "auth", "adj", "ist", "udf",
    }
    while parts and parts[0] in NOISE:
        parts.pop(0)

    if not parts:
        return None

    return "_".join(parts)


def _resolve_target(
    col_name: str,
    pk_index: Dict[str, List[Tuple[str, str]]],
    src_table: str,
) -> Optional[Tuple[str, str]]:
    """Return (ref_table, ref_pk_col) for col_name, or None (System B logic).

    Preference order:
      1. Longer (more specific) PK names over generic ones like 'id'
      2. Table-name hint from column prefix when generic PKs are the only match
      3. Single unambiguous match - otherwise skip (no guess > wrong guess)
    """
    # Collect candidate (ref_table, ref_pk) pairs where pk_col is a suffix of col_name
    by_length: Dict[int, List[Tuple[str, str]]] = {}
    for pk_col, targets in pk_index.items():
        if col_name == pk_col or col_name.endswith("_" + pk_col):
            for ref_table, ref_pk in targets:
                if ref_table != src_table:
                    key = len(pk_col)
                    by_length.setdefault(key, []).append((ref_table, ref_pk))

    if not by_length:
        return None

    # Take the tier with the longest (most specific) pk_col matches
    best_key = max(by_length)
    best_tier = by_length[best_key]
    best_pk_names = {pk for _, pk in best_tier}

    # If the best tier uses only generic PK names, try a table-name hint lookup
    if best_pk_names <= _GENERIC_PK_NAMES:
        hint = _table_hint_from_col(col_name)
        if hint:
            seen: Dict[str, str] = {}
            for pk_col, targets in pk_index.items():
                for tbl, pk in targets:
                    if tbl != src_table and hint in tbl.lower():
                        if tbl not in seen:
                            seen[tbl] = pk

            if len(seen) == 1:
                tbl, pk = next(iter(seen.items()))
                return tbl, pk

            if len(seen) > 1:
                # Multiple matches → ambiguous; do not guess
                return None

    if len(best_tier) == 1:
        return best_tier[0]

    # Still ambiguous - try table-name hint at best tier
    hint = _table_hint_from_col(col_name)
    if hint:
        preferred = [c for c in best_tier if hint in c[0].lower()]
        if len(preferred) == 1:
            return preferred[0]

    return None


# ---------------------------------------------------------------------------
# Tier 2 - Suffix matching with System B's ambiguity resolver
# ---------------------------------------------------------------------------

def _tier2_suffix(
    schema: Schema,
    explicit_covered: set,
) -> list[FKCandidate]:
    """Match _id/_fk/_key/_ref/_uuid/_code columns against PK-named columns.

    Uses System A's suffix list + System B's _resolve_target ambiguity resolver.
    """
    candidates: list[FKCandidate] = []

    # Build PK index: pk_col_name_lower → [(table_name, actual_pk_col)]
    # Only single-column PKs (composite PKs are ambiguous targets).
    pk_index: Dict[str, List[Tuple[str, str]]] = {}
    for table in schema.tables:
        pks = list(table.primary_keys or [])
        if not pks:
            pks = [c.name for c in table.columns if c.primary_key]
        if len(pks) == 1:
            pk_col = pks[0]
            pk_index.setdefault(pk_col.lower(), []).append((table.name, pk_col))
        elif not pks:
            # Heuristic: column named 'id' or '<table>id' or '<table>_id'
            for c in table.columns:
                cname_lower = c.name.lower()
                if cname_lower in ("id", f"{table.name.lower()}_id", f"{table.name.lower()}id"):
                    pk_index.setdefault(cname_lower, []).append((table.name, c.name))
                    break

    for table in schema.tables:
        for col in table.columns:
            if (table.name, col.name) in explicit_covered:
                continue
            if not _is_numeric_or_string(col.data_type):
                continue
            col_lower = col.name.lower()

            # Check any FK suffix
            has_suffix = any(
                col_lower.endswith(suffix) and len(col_lower) > len(suffix)
                for suffix in _FK_SUFFIXES
            )
            if not has_suffix:
                continue

            target = _resolve_target(col_lower, pk_index, table.name)
            if target is None:
                continue

            ref_table, ref_pk = target
            candidates.append(FKCandidate(
                src_table=table.name,
                src_col=col.name,
                tgt_table=ref_table,
                tgt_col=ref_pk,
                tier=2,
                confidence=0.85,
            ))

    return candidates


# ---------------------------------------------------------------------------
# Tier 3 - Column name equality across tables
# ---------------------------------------------------------------------------

def _tier3_equality(
    schema: Schema,
    already_covered: set,
) -> list[FKCandidate]:
    """Match identically-named columns across tables (System A logic)."""
    candidates: list[FKCandidate] = []

    # Build: col_name_lower → [(table, col_name, type)]
    col_index: Dict[str, List[Tuple[str, str, str]]] = {}
    for table in schema.tables:
        for col in table.columns:
            key = col.name.lower()
            col_index.setdefault(key, []).append((table.name, col.name, col.data_type))

    for col_lower, entries in col_index.items():
        if len(entries) < 2:
            continue
        # Only consider ID-like columns for equality matching
        if not (col_lower == "id" or col_lower.endswith("_id")):
            continue
        for i, (src_tbl, src_col, src_type) in enumerate(entries):
            for j, (tgt_tbl, tgt_col, _tgt_type) in enumerate(entries):
                if i == j or src_tbl == tgt_tbl:
                    continue
                if (src_tbl, src_col) in already_covered:
                    continue
                if not _is_numeric_or_string(src_type):
                    continue
                candidates.append(FKCandidate(
                    src_table=src_tbl,
                    src_col=src_col,
                    tgt_table=tgt_tbl,
                    tgt_col=tgt_col,
                    tier=3,
                    confidence=0.70,
                ))

    return candidates


# ---------------------------------------------------------------------------
# Tier 4 - Statistical uniqueness sampling (callback-gated)
# ---------------------------------------------------------------------------

def _tier4_statistical(
    schema: Schema,
    already_covered: set,
    fetch_stats: Callable[[str, str], Tuple[int, int]],
) -> list[FKCandidate]:
    """Match high-uniqueness src columns against 100%-unique tgt columns (System A logic).

    fetch_stats(table_name, col_name) → (distinct_count, total_non_null_count)
    """
    candidates: list[FKCandidate] = []

    # Pre-compute uniqueness ratios
    stats: Dict[Tuple[str, str], float] = {}  # (table, col) → ratio

    for table in schema.tables:
        for col in table.columns:
            if not _is_numeric_or_string(col.data_type):
                continue
            try:
                distinct, non_null = fetch_stats(table.name, col.name)
            except Exception:
                continue
            if distinct > _MAX_DISTINCT or non_null == 0:
                continue
            stats[(table.name, col.name)] = distinct / non_null

    # Find fully-unique columns as PK candidates
    pk_candidates: List[Tuple[str, str]] = [
        (tbl, col) for (tbl, col), ratio in stats.items()
        if ratio >= _TIER4_TGT_THRESHOLD
    ]

    for (src_tbl, src_col), src_ratio in stats.items():
        if src_ratio < _TIER4_SRC_THRESHOLD:
            continue
        if (src_tbl, src_col) in already_covered:
            continue
        for (tgt_tbl, tgt_col) in pk_candidates:
            if src_tbl == tgt_tbl or (src_tbl, src_col) == (tgt_tbl, tgt_col):
                continue
            # Check type compatibility
            src_type = next(
                (c.data_type for t in schema.tables if t.name == src_tbl
                 for c in t.columns if c.name == src_col), ""
            )
            tgt_type = next(
                (c.data_type for t in schema.tables if t.name == tgt_tbl
                 for c in t.columns if c.name == tgt_col), ""
            )
            if not _is_numeric_or_string(src_type) or not _is_numeric_or_string(tgt_type):
                continue
            confidence = round(0.5 * src_ratio, 4)
            candidates.append(FKCandidate(
                src_table=src_tbl,
                src_col=src_col,
                tgt_table=tgt_tbl,
                tgt_col=tgt_col,
                tier=4,
                confidence=confidence,
            ))

    return candidates


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def infer_fks(
    schema: Schema,
    fetch_stats: Optional[Callable[[str, str], Tuple[int, int]]] = None,
) -> list[FKCandidate]:
    """Run all four FK inference tiers and return deduplicated candidates.

    Parameters
    ----------
    schema:
        Canonical Schema dataclass (nybble.core.schema.Schema).
    fetch_stats:
        Optional callback: ``fetch_stats(table_name, col_name)``
        → ``(distinct_count, total_non_null_count)``.
        When None, Tier 4 (statistical sampling) is skipped.
        The CLI passes a DuckDB-backed callback; the server pipeline passes None
        (DDL-only mode, no row data available at pipeline time).

    Returns
    -------
    list[FKCandidate]
        Sorted by tier (ascending) then confidence (descending).
        Deduplicated: highest-confidence kept for identical quadruplets.
    """
    all_candidates: list[FKCandidate] = []

    # Tier 1 - declared constraints in the Schema dataclass
    tier1 = _tier1_declared(schema)
    explicit_covered: set = {(c.src_table, c.src_col) for c in tier1}
    all_candidates.extend(tier1)

    # Tier 2 - suffix matching + ambiguity resolver
    tier2 = _tier2_suffix(schema, explicit_covered)
    tier2_covered: set = {(c.src_table, c.src_col) for c in tier2}
    all_candidates.extend(tier2)

    covered_so_far = explicit_covered | tier2_covered

    # Tier 3 - column name equality
    tier3 = _tier3_equality(schema, covered_so_far)
    tier3_covered: set = {(c.src_table, c.src_col) for c in tier3}
    all_candidates.extend(tier3)

    covered_so_far = covered_so_far | tier3_covered

    # Tier 4 - statistical sampling (only when fetch_stats provided)
    if fetch_stats is not None:
        tier4 = _tier4_statistical(schema, covered_so_far, fetch_stats)
        all_candidates.extend(tier4)

    # Deduplicate: keep highest-confidence for identical (src, col, tgt, col) quadruplets
    seen: Dict[Tuple[str, str, str, str], FKCandidate] = {}
    for c in all_candidates:
        key = (c.src_table, c.src_col, c.tgt_table, c.tgt_col)
        if key not in seen or c.confidence > seen[key].confidence:
            seen[key] = c

    return sorted(seen.values(), key=lambda c: (c.tier, -c.confidence))
