"""Phase 4b - Flat-file → SQLite data loader.

Reads CSV / JSON / JSONL / XLSX / Parquet files and writes every data row into
a queryable SQLite database file, using only stdlib + the deps already present
in requirements.txt (openpyxl, pyarrow).

The resulting .db is placed in *out_dir* alongside schema.json so that
api_rows() and api_query() can serve Browse and Query requests immediately.

Usage (called from run_pipeline.py):
    from flat_file_to_sqlite import load_flat_file
    db_path = load_flat_file(input_path, out_dir, fmt, progress=log)

Returns:
    Path to the written .db file, or None if loading failed.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterator, Literal

FlatFormat = Literal["csv", "json", "jsonl", "xlsx", "parquet"]

# How many rows to insert per executemany batch (keeps memory bounded)
_BATCH_SIZE = 500


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def load_flat_file(
    input_path: str | Path,
    out_dir: str | Path,
    fmt: FlatFormat,
    progress=None,      # optional callable(str) for log messages
) -> Path | None:
    """Load all rows from *input_path* into a SQLite .db in *out_dir*.

    Returns the Path of the written database, or None on failure.
    """
    input_path = Path(input_path)
    out_dir    = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def log(msg: str):
        if progress:
            progress(msg)

    # Sanitize stem: spaces/dashes/dots → underscore, so the DB filename and all
    # SQLite table names match the sanitized names used in _create_table/_insert_batch.
    stem    = re.sub(r"[^\w]", "_", input_path.stem.strip()) or "data"
    db_path = out_dir / f"{stem}.db"

    log(f"INFO  [flat-to-sqlite] Loading {input_path.name} ({fmt}) ...")

    try:
        if db_path.exists():
            db_path.unlink()

        con = sqlite3.connect(str(db_path))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")

        if fmt == "csv":
            total = _load_csv(input_path, con, log)
        elif fmt in ("json", "jsonl"):
            total = _load_json(input_path, fmt, con, log)
        elif fmt == "xlsx":
            total = _load_xlsx(input_path, con, log)
        elif fmt == "parquet":
            total = _load_parquet(input_path, con, log)
        else:
            log(f"WARN  [flat-to-sqlite] Unsupported format: {fmt}")
            con.close()
            db_path.unlink(missing_ok=True)
            return None

        con.commit()
        con.close()

        if total == 0:
            log("WARN  [flat-to-sqlite] No rows loaded - empty or unrecognised structure")
            # Keep the (empty) .db so Browse can show an informative empty-state
            # rather than a "No queryable source" error.  The db has the correct
            # schema (columns) even with 0 rows.
            return db_path

        log(f"INFO  [flat-to-sqlite] Done - {total} row(s) written to {db_path.name}")
        return db_path

    except Exception as exc:
        log(f"ERROR [flat-to-sqlite] Fatal error: {exc}")
        try:
            db_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _safe_col(name: str, idx: int) -> str:
    """Return a safe SQLite column name from a header string."""
    cleaned = re.sub(r"[^\w]", "_", str(name).strip())
    return cleaned if cleaned else f"col_{idx}"


def _dedup_col_names(raw_names: list[str]) -> list[str]:
    """Deduplicate a list of column names in a case-insensitive way.

    SQLite treats column names case-insensitively, so 'Name' and 'name' would
    both map to the same column and cause a 'duplicate column name' error.
    When a collision is detected the later occurrence gets a numeric suffix
    (_2, _3, …) to make it unique.
    """
    seen: dict[str, int] = {}   # lowercase name → count of times seen
    result: list[str] = []
    for name in raw_names:
        key = name.lower()
        if key not in seen:
            seen[key] = 0
            result.append(name)
        else:
            seen[key] += 1
            candidate = f"{name}_{seen[key] + 1}"
            # Keep incrementing until the candidate is also unique
            while candidate.lower() in seen:
                seen[key] += 1
                candidate = f"{name}_{seen[key] + 1}"
            seen[candidate.lower()] = 0
            result.append(candidate)
    return result


def _create_table(con: sqlite3.Connection, table_name: str, col_names: list[str]) -> None:
    """CREATE TABLE IF NOT EXISTS with all TEXT columns (data loaded verbatim)."""
    safe_tbl  = re.sub(r"[^\w]", "_", table_name.strip()) or "data"
    col_defs  = ", ".join(f'"{c}" TEXT' for c in col_names)
    con.execute(f'CREATE TABLE IF NOT EXISTS "{safe_tbl}" ({col_defs})')
    con.commit()


def _insert_batch(
    con: sqlite3.Connection,
    table_name: str,
    col_names: list[str],
    batch: list[tuple],
) -> None:
    safe_tbl    = re.sub(r"[^\w]", "_", table_name.strip()) or "data"
    placeholders = ", ".join(["?"] * len(col_names))
    quoted_cols  = ", ".join(f'"{c}"' for c in col_names)
    con.executemany(
        f'INSERT INTO "{safe_tbl}" ({quoted_cols}) VALUES ({placeholders})',
        batch,
    )
    con.commit()


def _coerce(val: Any) -> Any:
    """Coerce a Python value to something SQLite accepts cleanly."""
    if val is None:
        return None
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, (list, dict)):
        return json.dumps(val, ensure_ascii=False)
    # datetime / date / time objects from openpyxl
    s = str(val)
    return s if s != "None" else None


# ---------------------------------------------------------------------------
# CSV loader
# ---------------------------------------------------------------------------

def _load_csv(path: Path, con: sqlite3.Connection, log) -> int:
    """Load all rows from a CSV file into SQLite. Returns row count."""
    # Detect delimiter
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        sample = fh.read(8192)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel  # type: ignore[assignment]

    table_name = re.sub(r"[^\w]", "_", path.stem.strip()) or "data"
    total = 0

    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.reader(fh, dialect=dialect)
        headers_raw = next(reader, None)
        if not headers_raw:
            return 0
        col_names = _dedup_col_names([_safe_col(h, i) for i, h in enumerate(headers_raw)])
        _create_table(con, table_name, col_names)

        batch: list[tuple] = []
        for row in reader:
            # Pad or trim to match header count
            padded = list(row) + [""] * max(0, len(col_names) - len(row))
            tpl = tuple(v if v != "" else None for v in padded[:len(col_names)])
            batch.append(tpl)
            if len(batch) >= _BATCH_SIZE:
                _insert_batch(con, table_name, col_names, batch)
                total += len(batch)
                batch = []
        if batch:
            _insert_batch(con, table_name, col_names, batch)
            total += len(batch)

    log(f"INFO  [flat-to-sqlite] {table_name}: {total} row(s)")
    return total


# ---------------------------------------------------------------------------
# JSON / JSONL loader
# ---------------------------------------------------------------------------

def _records_from_json(path: Path, fmt: str) -> Iterator[dict]:
    """Yield record dicts from a JSON or JSONL file."""
    if fmt == "jsonl":
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        yield obj
                except json.JSONDecodeError:
                    continue
        return

    # Regular JSON
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError:
            return
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item
    elif isinstance(data, dict):
        # Find the best list using the same scoring heuristic as extract_json._load_records:
        # score each list-valued key by length × (dict-item fraction + key uniformity + scalar fraction).
        best_list: list | None = None
        best_score: float = -1.0
        for v in data.values():
            if not isinstance(v, list) or not v:
                continue
            n = len(v)
            dict_items = [x for x in v if isinstance(x, dict)]
            dict_frac = len(dict_items) / n
            if dict_items:
                first_keys = frozenset(dict_items[0].keys())
                uniform = sum(1 for d in dict_items if frozenset(d.keys()) == first_keys)
                uniformity = uniform / len(dict_items)
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
            for item in best_list:
                if isinstance(item, dict):
                    yield item
        else:
            # No suitable list - yield the dict itself as a single record
            yield data


def _load_json(path: Path, fmt: str, con: sqlite3.Connection, log) -> int:
    """Load all records from a JSON/JSONL file into SQLite. Returns row count."""
    table_name = re.sub(r"[^\w]", "_", path.stem.strip()) or "data"
    safe_tbl   = re.sub(r"[^\w]", "_", table_name.strip()) or "data"

    # Stable mapping: original key string → safe column name.
    # Built incrementally as new keys are encountered across records.
    key_to_safe: dict[str, str] = {}
    col_names: list[str] = []
    batch: list[tuple] = []
    total = 0
    table_created = False

    for rec in _records_from_json(path, fmt):
        # Discover any keys in this record that haven't been seen yet
        new_raw_keys = [k for k in rec.keys() if k not in key_to_safe]
        if new_raw_keys:
            if batch:
                # Flush pending rows before altering the schema so every INSERT
                # uses the column set that was active when those rows were built.
                _insert_batch(con, table_name, col_names, batch)
                total += len(batch)
                batch = []

            for k in new_raw_keys:
                safe = _safe_col(k, len(key_to_safe))
                key_to_safe[k] = safe
                col_names.append(safe)

            if not table_created:
                # First time we have columns - create the table
                _create_table(con, table_name, col_names)
                table_created = True
            else:
                # Table already exists - add only the genuinely new columns
                for k in new_raw_keys:
                    try:
                        con.execute(
                            f'ALTER TABLE "{safe_tbl}" ADD COLUMN "{key_to_safe[k]}" TEXT'
                        )
                    except sqlite3.OperationalError:
                        pass  # column already exists (safe to ignore)

        # Build row aligned to the full known key set (missing keys → None)
        tpl = tuple(_coerce(rec.get(raw_k)) for raw_k in key_to_safe)
        batch.append(tpl)

        if len(batch) >= _BATCH_SIZE:
            _insert_batch(con, table_name, col_names, batch)
            total += len(batch)
            batch = []

    if col_names and batch:
        _insert_batch(con, table_name, col_names, batch)
        total += len(batch)

    if total:
        log(f"INFO  [flat-to-sqlite] {table_name}: {total} row(s)")
    return total


# ---------------------------------------------------------------------------
# XLSX loader
# ---------------------------------------------------------------------------

def _load_xlsx(path: Path, con: sqlite3.Connection, log) -> int:
    """Load all sheets from an XLSX or XLS workbook into SQLite. Returns total row count.

    Uses openpyxl for .xlsx and modern .xls files; falls back to xlrd for legacy
    BIFF .xls files if openpyxl cannot open them.
    """
    try:
        from extract_xlsx import _iter_sheets
    except ImportError:
        log("ERROR [flat-to-sqlite] extract_xlsx module not found - cannot load Excel file")
        return 0

    total = 0
    sheet_index = 0

    try:
        sheets = list(_iter_sheets(path))
    except ImportError as exc:
        log(f"ERROR [flat-to-sqlite] {exc}")
        return 0
    except Exception as exc:
        log(f"ERROR [flat-to-sqlite] Cannot open workbook: {exc}")
        return 0

    for sheet_name, all_rows in sheets:
        if not all_rows:
            sheet_index += 1
            continue

        header_row = all_rows[0]
        data_rows  = all_rows[1:]

        if not any(h is not None and str(h).strip() for h in header_row):
            sheet_index += 1
            continue

        col_names = _dedup_col_names([_safe_col(h, i) for i, h in enumerate(header_row)])
        tbl_name  = re.sub(r"[^\w]", "_", sheet_name.strip()) or f"sheet_{sheet_index}"
        _create_table(con, tbl_name, col_names)

        batch: list[tuple] = []
        sheet_total = 0
        for row in data_rows:
            padded = list(row) + [None] * max(0, len(col_names) - len(row))
            tpl = tuple(_coerce(v) for v in padded[:len(col_names)])
            batch.append(tpl)
            if len(batch) >= _BATCH_SIZE:
                _insert_batch(con, tbl_name, col_names, batch)
                sheet_total += len(batch)
                batch = []
        if batch:
            _insert_batch(con, tbl_name, col_names, batch)
            sheet_total += len(batch)

        log(f"INFO  [flat-to-sqlite] {tbl_name}: {sheet_total} row(s)")
        total += sheet_total
        sheet_index += 1

    return total


# ---------------------------------------------------------------------------
# Parquet loader
# ---------------------------------------------------------------------------

def _load_parquet(path: Path, con: sqlite3.Connection, log) -> int:
    """Load all row groups from a Parquet file into SQLite. Returns row count."""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        log("ERROR [flat-to-sqlite] pyarrow not installed - cannot load Parquet")
        return 0

    import pyarrow as pa

    pf        = pq.ParquetFile(path)
    tbl_name  = re.sub(r"[^\w]", "_", path.stem.strip()) or "parquet"
    col_names: list[str] | None = None
    total = 0

    for batch in pf.iter_batches(batch_size=_BATCH_SIZE):
        if col_names is None:
            col_names = [_safe_col(f.name, i) for i, f in enumerate(batch.schema)]
            _create_table(con, tbl_name, col_names)

        # Convert batch to Python rows
        rows: list[tuple] = []
        cols_data = [batch.column(i).to_pylist() for i in range(batch.num_columns)]
        for row_vals in zip(*cols_data):
            rows.append(tuple(_coerce(v) for v in row_vals))

        _insert_batch(con, tbl_name, col_names, rows)
        total += len(rows)

    if total:
        log(f"INFO  [flat-to-sqlite] {tbl_name}: {total} row(s)")
    return total
