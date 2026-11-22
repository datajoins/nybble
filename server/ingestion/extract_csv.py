"""CSV extractor - infers schema from a flat CSV file.

Uses only the Python standard library (csv, re, pathlib).
Samples up to 1000 rows for type inference.

Type inference rules (applied per column, considering all non-empty sample values):
  1. All values are valid ``int()``         → INTEGER
  2. All values are valid ``float()``       → REAL
  3. All values match ``YYYY-MM-DD`` regex  → DATE
  4. Otherwise                              → TEXT

The first non-empty column header is marked as primary_key=True.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from .schema_model import Column, Schema, Table

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAMPLE_ROWS = 1000


def _infer_type(values: list[str]) -> str:
    """Return the most specific SQL type that fits all non-empty *values*."""
    non_empty = [v for v in values if v != ""]
    if not non_empty:
        return "TEXT"

    # Try INTEGER
    try:
        for v in non_empty:
            int(v)
        return "INTEGER"
    except (ValueError, OverflowError):
        pass

    # Try REAL
    try:
        for v in non_empty:
            float(v)
        return "REAL"
    except (ValueError, OverflowError):
        pass

    # Try DATE
    if all(_DATE_RE.match(v) for v in non_empty):
        return "DATE"

    return "TEXT"


def extract(path: str | Path) -> Schema:
    """Return a :class:`Schema` containing a single table inferred from *path*."""
    path = Path(path)
    # Sanitize: replace any non-word character (space, dash, dot, …) with underscore
    # so the schema table name always matches the SQLite table name created by flat_file_to_sqlite.
    table_name = re.sub(r"[^\w]", "_", path.stem.strip()) or "data"

    # Try to detect delimiter via Sniffer on first 8 KB; fall back to comma.
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        sample_text = fh.read(8192)

    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel  # type: ignore[assignment]

    # Read rows
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh, dialect=dialect)
        headers = reader.fieldnames or []
        rows: list[dict] = []
        for row in reader:
            rows.append(row)
            if len(rows) >= _SAMPLE_ROWS:
                break

    # Build column value lists for type inference
    col_values: dict[str, list[str]] = {h: [] for h in headers}
    for row in rows:
        for h in headers:
            col_values[h].append((row.get(h) or "").strip())

    # Identify first non-empty header as PK
    pk_col = next((h for h in headers if h), None)

    columns: list[Column] = []
    for h in headers:
        if not h:
            continue
        dtype = _infer_type(col_values[h])
        columns.append(Column(
            name=h,
            data_type=dtype,
            nullable=True,
            primary_key=(h == pk_col),
        ))

    table = Table(
        name=table_name,
        columns=columns,
        primary_keys=[pk_col] if pk_col else [],
    )
    return Schema(name=table_name, tables=[table])
