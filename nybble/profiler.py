"""
profiler.py - Column-level profiling for any loaded Relation

Computes null counts, distinct cardinality, min/max values, and the most
frequent value for every column in a relation using DuckDB SQL aggregates.

For large tables (>1M rows) approximate distinct counts are used automatically
via DuckDB's ``approx_count_distinct()`` to keep latency acceptable.

Public API
----------
  profile_relation(name, *, approx_threshold=1_000_000) → list[ColumnProfile]
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from nybble.resolver import session, _SESSION_LOCK


_APPROX_THRESHOLD = 1_000_000   # rows above which we use approx_count_distinct


@dataclass
class ColumnProfile:
    """Statistics for a single column.

    Attributes
    ----------
    name            : column name
    dtype           : DuckDB type string
    total_rows      : total rows in the relation
    non_null        : count of non-null values
    null_count      : total_rows - non_null
    null_pct        : null percentage (0-100)
    distinct        : distinct value count (exact or approximate)
    approx          : True when distinct was computed with approx_count_distinct
    cardinality_pct : distinct / non_null * 100
    min_val         : minimum value as string (None when all null)
    max_val         : maximum value as string (None when all null)
    top_val         : most-frequent non-null value as string (None when all null)
    """
    name: str
    dtype: str
    total_rows: int
    non_null: int
    null_count: int
    null_pct: float
    distinct: int
    approx: bool
    cardinality_pct: float
    min_val: Optional[str]
    max_val: Optional[str]
    top_val: Optional[str]


def profile_relation(
    name: str,
    *,
    approx_threshold: int = _APPROX_THRESHOLD,
) -> list[ColumnProfile]:
    """Compute per-column statistics for a registered relation.

    Parameters
    ----------
    name:
        Name of a virtual table already registered in the DuckDB session.
    approx_threshold:
        Row count above which ``approx_count_distinct`` is used instead of
        the exact ``count(distinct …)`` to bound execution time.

    Returns
    -------
    list[ColumnProfile]
        One entry per column, in schema order.
    """
    con = session()

    # ── 1. Schema and row count ───────────────────────────────────────────────
    with _SESSION_LOCK:
        schema_rows = con.execute(
            f'DESCRIBE SELECT * FROM "{name}" LIMIT 0'
        ).fetchall()
        total_rows: int = con.execute(
            f'SELECT COUNT(*) FROM "{name}"'
        ).fetchone()[0]

    if not schema_rows:
        return []

    # Empty table: return schema-only profiles so callers can still display columns.
    if total_rows == 0:
        columns = [(r[0], r[1]) for r in schema_rows]
        return [
            ColumnProfile(
                name=col, dtype=dtype, total_rows=0,
                non_null=0, null_count=0, null_pct=0.0,
                distinct=0, approx=False, cardinality_pct=0.0,
                min_val=None, max_val=None, top_val=None,
            )
            for col, dtype in columns
        ]

    use_approx = total_rows > approx_threshold
    columns = [(r[0], r[1]) for r in schema_rows]   # (col_name, dtype)

    # ── 2. Per-column aggregate query ─────────────────────────────────────────
    #
    # We build a single SELECT with one set of aggregates per column and run
    # it once, avoiding N round-trips.
    #
    # For top_val we use a sub-select with MODE() which DuckDB supports.
    # For min/max we cast to VARCHAR so all types are comparable.

    select_parts: list[str] = []
    for col, _dtype in columns:
        c = f'"{col}"'
        distinct_expr = (
            f"CAST(approx_count_distinct({c}) AS BIGINT)"
            if use_approx
            else f"COUNT(DISTINCT {c})"
        )
        select_parts.extend([
            f"COUNT({c})",                          # non_null
            distinct_expr,                          # distinct
            f"MIN(CAST({c} AS VARCHAR))",           # min_val
            f"MAX(CAST({c} AS VARCHAR))",           # max_val
        ])

    agg_sql = f'SELECT {", ".join(select_parts)} FROM "{name}"'

    with _SESSION_LOCK:
        agg_row = con.execute(agg_sql).fetchone()

    # ── 3. Per-column MODE (most frequent value) ──────────────────────────────
    #
    # MODE is cheap for small cardinality; skip for approx to avoid full scans.
    top_vals: list[Optional[str]] = []
    for col, _dtype in columns:
        c = f'"{col}"'
        try:
            with _SESSION_LOCK:
                tv = con.execute(
                    f'SELECT MODE({c}) FROM "{name}"'
                ).fetchone()
            top_vals.append(str(tv[0]) if tv and tv[0] is not None else None)
        except Exception:
            top_vals.append(None)

    # ── 4. Assemble ColumnProfile objects ────────────────────────────────────
    profiles: list[ColumnProfile] = []
    stride = 4   # aggregates per column
    for i, (col, dtype) in enumerate(columns):
        base = i * stride
        non_null: int = agg_row[base] or 0
        distinct: int = agg_row[base + 1] or 0
        min_v: Optional[str] = agg_row[base + 2]
        max_v: Optional[str] = agg_row[base + 3]

        null_count = total_rows - non_null
        null_pct = round(null_count / total_rows * 100, 2) if total_rows else 0.0
        cardinality_pct = round(distinct / non_null * 100, 2) if non_null else 0.0

        profiles.append(ColumnProfile(
            name=col,
            dtype=dtype,
            total_rows=total_rows,
            non_null=non_null,
            null_count=null_count,
            null_pct=null_pct,
            distinct=distinct,
            approx=use_approx,
            cardinality_pct=cardinality_pct,
            min_val=str(min_v) if min_v is not None else None,
            max_val=str(max_v) if max_v is not None else None,
            top_val=top_vals[i],
        ))

    return profiles
