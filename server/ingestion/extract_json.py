"""JSON / JSONL extractor - infers schema from JSON or NDJSON files.

Uses only the Python standard library (json, re, pathlib).
Samples up to 500 records for type inference.

Supported inputs:
  • ``.jsonl`` / ``.ndjson`` - one JSON object per line (NDJSON / JSONL)
  • ``.json``              - full JSON file; the root may be:
      - a list of objects  → treated as records
      - a dict with a list value  → first list found is used as records
      - any other dict     → wrapped as [data]

Type inference (same rules as extract_csv):
  1. All non-empty values are valid ``int()``        → INTEGER
  2. All non-empty values are valid ``float()``      → REAL
  3. All non-empty values match ``YYYY-MM-DD``       → DATE
  4. Otherwise                                       → TEXT

First key in the union of all record keys → primary_key=True.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .schema_model import Column, Schema, Table

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAMPLE_ROWS = 500


def _infer_type(values: list[Any]) -> str:
    """Return the most specific SQL type that fits all non-None *values*."""
    non_empty = [v for v in values if v is not None and str(v) != ""]
    if not non_empty:
        return "TEXT"

    # If any value is a native Python float, it is REAL (not INTEGER).
    # int(9.5) would succeed but would silently truncate, which is wrong.
    if any(isinstance(v, float) for v in non_empty):
        return "REAL"

    # Try INTEGER (native int or string representation of an integer)
    try:
        for v in non_empty:
            # Reject float strings like "9.5" - only bare integers allowed here
            sv = str(v)
            if "." in sv:
                raise ValueError("float string")
            int(sv)
        return "INTEGER"
    except (ValueError, TypeError, OverflowError):
        pass

    # Try REAL
    try:
        for v in non_empty:
            float(v)
        return "REAL"
    except (ValueError, TypeError, OverflowError):
        pass

    # Try DATE (string values only)
    str_vals = [str(v) for v in non_empty]
    if all(_DATE_RE.match(sv) for sv in str_vals):
        return "DATE"

    return "TEXT"


def _load_records(path: Path) -> list[dict]:
    """Parse the file and return a flat list of record dicts."""
    suffix = path.suffix.lower()

    if suffix in (".jsonl", ".ndjson"):
        # Newline-delimited JSON - one object per line
        records: list[dict] = []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        records.append(obj)
                except json.JSONDecodeError:
                    continue
                if len(records) >= _SAMPLE_ROWS:
                    break
        return records

    # Regular JSON file
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError:
            return []

    if isinstance(data, list):
        return [r for r in data[:_SAMPLE_ROWS] if isinstance(r, dict)]

    if isinstance(data, dict):
        # Find the best list: score all list-valued keys and pick the highest-scoring one.
        # Scoring criteria (higher = more likely to be tabular data records):
        #   +length of list (more items → more likely to be the data)
        #   +fraction of items that are dicts (tabular row = dict)
        #   +uniformity of keys across dict items (all share same keys → tabular)
        best_list: list | None = None
        best_score: float = -1.0
        for v in data.values():
            if not isinstance(v, list) or not v:
                continue
            n = len(v)
            dict_items = [x for x in v if isinstance(x, dict)]
            dict_frac = len(dict_items) / n
            # Key uniformity: fraction of dict items that share the most-common key set
            if dict_items:
                # count how many items have the same key set as the first item
                first_keys = frozenset(dict_items[0].keys())
                uniform = sum(1 for d in dict_items if frozenset(d.keys()) == first_keys)
                uniformity = uniform / len(dict_items)
                # check that items have only scalar leaf values (not deeply nested)
                scalar_frac = sum(
                    1 for d in dict_items[:20]
                    for val in d.values()
                    if not isinstance(val, (dict, list))
                ) / max(1, sum(len(d) for d in dict_items[:20]))
            else:
                uniformity = 0.0
                scalar_frac = 0.0
            score = n * (dict_frac * 2 + uniformity + scalar_frac)
            if score > best_score:
                best_score = score
                best_list = v
        if best_list is not None and best_score > 0:
            return [r for r in best_list[:_SAMPLE_ROWS] if isinstance(r, dict)]
        # No suitable list found - wrap the dict itself as a single record
        return [data]

    return []


def extract(path: str | Path) -> Schema:
    """Return a :class:`Schema` containing a single table inferred from *path*."""
    path = Path(path)
    # Sanitize: replace non-word chars with underscore so the name matches the SQLite table.
    table_name = re.sub(r"[^\w]", "_", path.stem.strip()) or "data"

    records = _load_records(path)

    # Union all keys across all records (preserving first-seen order)
    all_keys: list[str] = []
    seen_keys: set[str] = set()
    for rec in records:
        for k in rec.keys():
            if k not in seen_keys:
                all_keys.append(k)
                seen_keys.add(k)

    # Build per-column value lists
    col_values: dict[str, list[Any]] = {k: [] for k in all_keys}
    for rec in records:
        for k in all_keys:
            col_values[k].append(rec.get(k))

    pk_col = all_keys[0] if all_keys else None

    columns: list[Column] = []
    for k in all_keys:
        dtype = _infer_type(col_values[k])
        columns.append(Column(
            name=k,
            data_type=dtype,
            nullable=True,
            primary_key=(k == pk_col),
        ))

    table = Table(
        name=table_name,
        columns=columns,
        primary_keys=[pk_col] if pk_col else [],
    )
    return Schema(name=table_name, tables=[table])
