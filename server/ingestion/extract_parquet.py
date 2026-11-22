"""extract_parquet.py - Extract schema from an Apache Parquet file.

Each Parquet file has exactly one schema (the Arrow schema embedded in its
footer metadata).  Column physical types are mapped to SQL-ish type strings.

Requires: pyarrow  (install with: pip install pyarrow)
"""

from __future__ import annotations

import re
from pathlib import Path

from .schema_model import Column, Schema, Table


# ── Arrow → SQL type mapping ──────────────────────────────────────────────────

def _arrow_to_sql(pa_type_str: str) -> str:
    """Map a pyarrow type string to a SQL-ish type name."""
    t = str(pa_type_str).lower()
    if t.startswith("int") or t.startswith("uint"):
        return "INTEGER"
    if t in ("float", "float16", "float32", "half_float"):
        return "REAL"
    if t in ("double", "float64"):
        return "REAL"
    if t.startswith("decimal"):
        return "DECIMAL"
    if t in ("bool", "boolean"):
        return "BOOLEAN"
    if t.startswith("date"):
        return "DATE"
    if t.startswith("timestamp") or t.startswith("time"):
        return "TIMESTAMP"
    if t.startswith("duration"):
        return "INTERVAL"
    if t in ("string", "utf8", "large_utf8", "large_string"):
        return "TEXT"
    if t in ("binary", "large_binary"):
        return "BLOB"
    if t.startswith("list") or t.startswith("large_list"):
        return "JSON"
    if t.startswith("struct") or t.startswith("map"):
        return "JSON"
    if t == "null":
        return "TEXT"
    return "TEXT"


# ── Main extractor ────────────────────────────────────────────────────────────

def extract(path: str | Path) -> Schema:
    """Parse *path* (.parquet) and return a single-table Schema."""
    try:
        import pyarrow.parquet as pq  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "pyarrow is required to process Parquet files.  "
            "Install it with:  pip install pyarrow"
        ) from exc

    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    arrow_schema = pf.schema_arrow

    table_name = re.sub(r"[^\w]", "_", Path(path).stem.strip()) or "parquet"

    columns: list[Column] = []
    pk_name: str | None = None

    for i, field in enumerate(arrow_schema):
        col_name = re.sub(r"[^\w]", "_", str(field.name).strip()) or f"col_{i}"
        col_type = _arrow_to_sql(str(field.type))
        nullable = field.nullable

        # Heuristic: first INTEGER column named *id* or ending in _id → primary key
        is_pk = (
            i == 0
            and col_type == "INTEGER"
            and ("id" in col_name.lower())
        )
        if is_pk:
            pk_name = col_name

        columns.append(Column(
            name=col_name,
            data_type=col_type,
            nullable=nullable,
            primary_key=is_pk,
        ))

    if not columns:
        raise ValueError("No columns found in Parquet file.")

    table = Table(
        name=table_name,
        columns=columns,
        primary_keys=[pk_name] if pk_name else [],
    )
    return Schema(name=Path(path).stem, tables=[table])
