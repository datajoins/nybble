"""extract_xlsx.py - Extract schema from an Excel workbook (.xlsx).

Each sheet becomes a table.  Column types are inferred by scanning every
cell in the column (up to MAX_SAMPLE_ROWS rows).  The first row is always
treated as the header.

Requires: openpyxl  (pure-Python, no C extension needed)
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from .schema_model import Column, Schema, Table

MAX_SAMPLE_ROWS = 2_000  # cells per column to scan for type inference


# ── Type inference ────────────────────────────────────────────────────────────

def _infer_type(samples: list[Any]) -> str:
    """Return a SQL-ish type string from a list of non-None sample values."""
    non_null = [v for v in samples if v is not None and v != ""]
    if not non_null:
        return "TEXT"

    def _is_int(v: Any) -> bool:
        if isinstance(v, bool):
            return False
        if isinstance(v, int):
            return True
        if isinstance(v, float):
            return v == int(v)
        if isinstance(v, str):
            stripped = v.strip()
            if "." in stripped:
                return False
            try:
                int(stripped)
                return True
            except ValueError:
                return False
        return False

    def _is_float(v: Any) -> bool:
        if isinstance(v, bool):
            return False
        if isinstance(v, (int, float)):
            return True
        if isinstance(v, str):
            try:
                float(v.strip())
                return True
            except ValueError:
                return False
        return False

    if all(isinstance(v, bool) for v in non_null):
        return "BOOLEAN"
    if all(isinstance(v, time) and not isinstance(v, datetime) for v in non_null):
        return "TIME"
    if all(isinstance(v, (date, datetime)) for v in non_null):
        # openpyxl always returns datetime even for date-only cells.
        # Treat as DATE when every value has midnight time (00:00:00).
        if all(
            isinstance(v, datetime) and v.hour == 0 and v.minute == 0 and v.second == 0
            or isinstance(v, date) and not isinstance(v, datetime)
            for v in non_null
        ):
            return "DATE"
        return "TIMESTAMP"
    if all(_is_int(v) for v in non_null):
        return "INTEGER"
    if all(_is_float(v) for v in non_null):
        return "REAL"
    return "TEXT"


# ── Workbook loading (openpyxl for .xlsx; xlrd fallback for legacy .xls) ─────

def _iter_sheets_openpyxl(path: Path):
    """Yield (sheet_name, rows_list) pairs using openpyxl.

    Each *rows_list* contains tuples of cell values (first row = headers).
    Reads at most MAX_SAMPLE_ROWS + 1 rows per sheet.
    """
    import openpyxl as _opx
    wb = _opx.load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows: list[tuple] = []
            for row in ws.iter_rows(values_only=True):
                rows.append(row)
                if len(rows) > MAX_SAMPLE_ROWS + 1:
                    break
            yield sheet_name, rows
    finally:
        wb.close()


def _iter_sheets_xlrd(path: Path):
    """Yield (sheet_name, rows_list) pairs using xlrd (legacy .xls only).

    Each *rows_list* contains tuples of raw Python values.
    Reads at most MAX_SAMPLE_ROWS + 1 rows per sheet.
    """
    import xlrd as _xlrd
    wb = _xlrd.open_workbook(str(path))
    for sheet_name in wb.sheet_names():
        ws = wb.sheet_by_name(sheet_name)
        rows: list[tuple] = []
        for ri in range(min(ws.nrows, MAX_SAMPLE_ROWS + 1)):
            rows.append(tuple(ws.row_values(ri)))
        yield sheet_name, rows


def _iter_sheets(path: Path):
    """Yield (sheet_name, rows_list) for every sheet in *path*.

    Tries openpyxl first (handles .xlsx and modern .xls saved as OOXML).
    Falls back to xlrd for true legacy BIFF .xls files if openpyxl raises.
    Raises ImportError with install instructions if neither library is available.
    """
    import importlib.util as _ilu

    have_openpyxl = _ilu.find_spec("openpyxl") is not None
    have_xlrd     = _ilu.find_spec("xlrd")     is not None

    if not have_openpyxl and not have_xlrd:
        raise ImportError(
            "openpyxl (or xlrd for legacy .xls) is required to process Excel files.\n"
            "Install with:  pip install openpyxl\n"
            "For legacy .xls support also install:  pip install xlrd"
        )

    if have_openpyxl:
        try:
            yield from _iter_sheets_openpyxl(path)
            return
        except Exception:
            # openpyxl cannot read this file - likely a legacy BIFF .xls
            if not have_xlrd:
                raise ImportError(
                    "This appears to be a legacy .xls file (Excel 97-2003 BIFF format).\n"
                    "Install xlrd to enable .xls support:  pip install xlrd"
                )
    # openpyxl not available or failed → use xlrd
    yield from _iter_sheets_xlrd(path)


# ── Main extractor ────────────────────────────────────────────────────────────

def extract(path: str | Path) -> Schema:
    """Parse *path* (.xlsx or .xls) and return a Schema with one Table per sheet."""
    path = Path(path)
    tables: list[Table] = []

    for sheet_name, raw_rows in _iter_sheets(path):
        if not raw_rows:
            continue

        # First row = headers; sanitize names
        header_row = raw_rows[0]
        data_rows = raw_rows[1:]

        col_names: list[str] = []
        seen: dict[str, int] = {}
        for i, hdr in enumerate(header_row):
            raw = str(hdr).strip() if hdr is not None else ""
            name = re.sub(r"[^\w]", "_", raw) if raw else f"col_{i}"
            if name in seen:
                seen[name] += 1
                name = f"{name}_{seen[name]}"
            else:
                seen[name] = 0
            col_names.append(name)

        if not col_names:
            continue

        # Per-column samples for type inference
        col_samples: list[list[Any]] = [[] for _ in col_names]
        for row in data_rows:
            for ci, val in enumerate(row):
                if ci < len(col_names):
                    col_samples[ci].append(val)

        columns: list[Column] = []
        pk_name: str | None = None
        for ci, name in enumerate(col_names):
            col_type = _infer_type(col_samples[ci])
            # First column is a candidate PK if it looks like an integer id
            is_pk = ci == 0 and col_type == "INTEGER"
            if is_pk:
                pk_name = name
            columns.append(Column(
                name=name,
                data_type=col_type,
                nullable=True,
                primary_key=is_pk,
            ))

        # Use the sheet name as the table name (sanitize)
        tbl_name = re.sub(r"[^\w]", "_", sheet_name.strip()) or f"sheet_{len(tables)}"
        tables.append(Table(
            name=tbl_name,
            columns=columns,
            primary_keys=[pk_name] if pk_name else [],
        ))

    if not tables:
        raise ValueError("No sheets with data found in the Excel workbook.")

    return Schema(name=path.stem, tables=tables)
