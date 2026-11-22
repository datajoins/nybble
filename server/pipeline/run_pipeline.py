"""Pipeline orchestrator.

Usage:
    python run_pipeline.py <input_file> [--output-dir DIR] [--schema-only]

The pipeline:
  1. Detect format   (detect_format.py)
  2. Extract schema  (extract_sqlite.py  or  extract_sql_dump.py)
  3. Write schema.json + schema.sql to output dir

schema.json is the internal Liam-keyed-object format:
  { "tables": { tableName: { "columns": {...}, "constraints": {...} } } }
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Detect database format, extract schema, render ERD.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "input",
        help=(
            "Input database file "
            "(.db, .sqlite, .sqlite3, .sql, .sql.gz, .csv, .json, .jsonl, .ndjson, .xlsx, .parquet)"
        ),
    )
    p.add_argument(
        "--output-dir", default="/output/erd",
        help="Directory where schema.json and schema.sql will be written.",
    )
    p.add_argument(
        "--schema-only", action="store_true",
        help="Alias for default behaviour - kept for backward compatibility.",
    )
    p.add_argument(
        "--no-infer-fk", action="store_true",
        help="Disable FK inference from column naming conventions.",
    )
    return p


def schema_to_graph(schema) -> dict:
    """Convert a Schema dataclass to the Liam keyed-object JSON format.

    Output shape:
        {
          "tables": {
            "tableName": {
              "name": "tableName",
              "columns": {
                "colName": { "name": ..., "type": ..., "notNull": ..., "default": ... }
              },
              "constraints": {
                "pk_tableName": { "type": "PRIMARY KEY", "columnNames": [...] },
                "fk_tableName_col": { "type": "FOREIGN KEY", "columnNames": [...],
                                      "targetTableName": ..., "targetColumnNames": [...] }
              }
            }
          },
          "enums": {}
        }
    """
    tables_out: dict = {}
    for table in schema.tables:
        cols_out: dict = {}
        for col in table.columns:
            cols_out[col.name] = {
                "name":     col.name,
                "type":     col.data_type,
                "notNull":  not col.nullable,
                "default":  col.default,
            }

        constraints_out: dict = {}

        # Primary key
        pk_cols = table.primary_keys or [c.name for c in table.columns if c.primary_key]
        if pk_cols:
            constraints_out[f"pk_{table.name}"] = {
                "type":        "PRIMARY KEY",
                "columnNames": pk_cols,
            }

        # Foreign keys
        for i, fk in enumerate(table.foreign_keys):
            key = f"fk_{table.name}_{'_'.join(fk.constrained_columns)}_{i}"
            constraints_out[key] = {
                "type":              "FOREIGN KEY",
                "columnNames":       fk.constrained_columns,
                "targetTableName":   fk.referred_table,
                "targetColumnNames": fk.referred_columns,
            }

        tables_out[table.name] = {
            "name":        table.name,
            "columns":     cols_out,
            "constraints": constraints_out,
        }

    return {"tables": tables_out, "enums": {}}


def _process_zip_multi(zip_path: Path, out_dir: Path, args) -> None:
    """Handle a multi-file ZIP by merging all inner tables into one unified schema.

    Each supported inner file becomes a separate table in the combined schema.
    All flat files (csv/json/jsonl/xlsx/parquet) are loaded into a single shared
    SQLite database so Browse and Query work across every table at once.
    SQL dumps are schema-extracted only (no row data).

    This function calls sys.exit(0) on success and sys.exit(1) on failure.
    """
    import tempfile

    from server.ingestion.detect_format import list_zip_members
    from server.ingestion.schema_model import Schema
    from server.ingestion.infer_fk import infer_missing_fks
    from server.pipeline.generate_erd import schema_to_ddl

    out_dir.mkdir(parents=True, exist_ok=True)
    members = list_zip_members(zip_path)

    if not members:
        print("[!!] ZIP archive contains no supported files.", file=sys.stderr)
        sys.exit(1)

    print(f"[ok] ZIP contains {len(members)} supported file(s): "
          f"{', '.join(Path(m).name for m, _ in members)}")

    combined = Schema(name=zip_path.stem, tables=[])

    # Track which flat files need data loading (fmt → path)
    flat_files: list[tuple[Path, str]] = []

    import zipfile
    with zipfile.ZipFile(zip_path, "r") as zf, tempfile.TemporaryDirectory() as td:
        td_path = Path(td)

        for member_filename, member_fmt in members:
            # Extract to temp dir (flatten paths)
            inner_path = td_path / Path(member_filename).name
            inner_path.write_bytes(zf.read(member_filename))
            print(f"[ok] Processing {inner_path.name}  (format: {member_fmt})")

            try:
                if member_fmt == "sqlite":
                    from server.ingestion.extract_sqlite import extract
                    schema = extract(inner_path)
                elif member_fmt == "csv":
                    from server.ingestion.extract_csv import extract
                    schema = extract(inner_path)
                elif member_fmt in ("json", "jsonl"):
                    from server.ingestion.extract_json import extract
                    schema = extract(inner_path)
                elif member_fmt == "xlsx":
                    from server.ingestion.extract_xlsx import extract
                    schema = extract(inner_path)
                elif member_fmt == "parquet":
                    from server.ingestion.extract_parquet import extract
                    schema = extract(inner_path)
                elif member_fmt in ("postgresql", "mysql", "sql_script"):
                    from server.ingestion.extract_sql_dump import extract
                    vendor = member_fmt if member_fmt != "sql_script" else "generic"
                    schema = extract(inner_path, vendor=vendor)
                else:
                    print(f"[..] Skipping {inner_path.name} - unhandled format {member_fmt}")
                    continue

                print(f"[ok] {inner_path.name}: {len(schema.tables)} table(s)")

            except Exception as exc:
                print(f"[..] {inner_path.name} extraction failed ({exc}) - skipping")
                continue

            # De-duplicate table names across files using the filename stem as prefix
            # only when a collision exists, to keep names clean in the common case.
            existing_names = {t.name for t in combined.tables}
            for table in schema.tables:
                final_name = table.name
                if final_name in existing_names:
                    stem = Path(member_filename).stem
                    safe_stem = re.sub(r"[^\w]", "_", stem)
                    final_name = f"{safe_stem}__{table.name}"
                table.name = final_name
                combined.tables.append(table)
                existing_names.add(final_name)

            # Queue flat files for data loading
            if member_fmt in ("csv", "json", "jsonl", "xlsx", "parquet"):
                flat_files.append((inner_path, member_fmt))

        # ── Infer missing FK relationships across all tables ────────────
        if not args.no_infer_fk:
            added = infer_missing_fks(combined)
            if added:
                print(f"[ok] Inferred {added} FK relationship(s) from column names")

        if not combined.tables:
            print("[!!] No tables found across all ZIP members.", file=sys.stderr)
            sys.exit(1)

        print(combined.summary())

        # ── Write schema.json ─────────────────────────────────────────────
        graph_data = schema_to_graph(combined)
        json_path  = out_dir / "schema.json"
        json_path.write_text(
            json.dumps(graph_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        n_tables = len(graph_data.get("tables", {}))
        n_fks    = sum(
            1 for t in graph_data.get("tables", {}).values()
            for c in t.get("constraints", {}).values()
            if c.get("type") == "FOREIGN KEY"
        )
        print(f"[ok] schema.json written -> {json_path}  ({n_tables} tables, {n_fks} FK edges)")

        # ── Write schema.sql ──────────────────────────────────────────────
        ddl_path = out_dir / "schema.sql"
        ddl_path.write_text(schema_to_ddl(combined), encoding="utf-8")
        print(f"[ok] schema.sql written -> {ddl_path}")

        # ── Load all flat files into one shared SQLite DB ─────────────────
        if flat_files:
            import sqlite3 as _sqlite3

            safe_stem = re.sub(r"[^\w]", "_", zip_path.stem)
            db_path = out_dir / f"{safe_stem}.db"
            if db_path.exists():
                db_path.unlink()

            con = _sqlite3.connect(str(db_path))
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")

            total_rows = 0
            for flat_path, flat_fmt in flat_files:
                from server.ingestion.flat_file_to_sqlite import (
                    _load_csv, _load_json, _load_xlsx, _load_parquet,
                )

                def _log(msg: str):
                    # Strip the [flat-to-sqlite] prefix for cleaner pipeline output
                    clean = msg.replace("[flat-to-sqlite] ", "")
                    if clean.startswith("INFO  "):
                        print(f"[ok] {clean[6:].strip()}")
                    elif clean.startswith("WARN  "):
                        print(f"[..] {clean[6:].strip()}")
                    elif clean.startswith("ERROR "):
                        print(f"[!!] {clean[6:].strip()}")
                    else:
                        print(f"[..] {clean}")

                try:
                    if flat_fmt == "csv":
                        n = _load_csv(flat_path, con, _log)
                    elif flat_fmt in ("json", "jsonl"):
                        n = _load_json(flat_path, flat_fmt, con, _log)
                    elif flat_fmt == "xlsx":
                        n = _load_xlsx(flat_path, con, _log)
                    elif flat_fmt == "parquet":
                        n = _load_parquet(flat_path, con, _log)
                    else:
                        n = 0
                    total_rows += (n or 0)
                except Exception as exc:
                    print(f"[..] {flat_path.name} data load failed ({exc}) - skipping rows")

            con.commit()
            con.close()
            print(f"[ok] Shared SQLite DB written -> {db_path}  "
                  f"({total_rows} total rows across {len(flat_files)} file(s))")


def main() -> None:
    args = build_parser().parse_args()
    input_path = Path(args.input)

    if not input_path.exists():
        print(f"[!!] Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 0: Transparently handle ZIP archives
    # ------------------------------------------------------------------
    from server.ingestion.detect_format import detect, maybe_extract_zip

    if input_path.suffix.lower() == ".zip":
        out_dir = Path(args.output_dir)
        zip_fmt = detect(input_path)

        if zip_fmt == "zip_multi":
            # Special case: multiple supported files → merge into one schema
            print(f"[ok] Detected format: zip_multi  ({input_path.name})")
            _process_zip_multi(input_path, out_dir, args)
            sys.exit(0)

        # Single-file ZIP: extract and continue with normal pipeline
        result = maybe_extract_zip(input_path, out_dir)
        if result is None:
            print(
                f"[!!] Could not determine file format.\n"
                f"     '{input_path.name}' is a ZIP archive but its contents could not be "
                f"identified as a supported format. Supported inner-file types: "
                f".csv, .json, .jsonl, .sql, .db/.sqlite, .parquet, .xlsx",
                file=sys.stderr,
            )
            sys.exit(1)
        extracted_path, fmt = result
        print(f"[ok] Extracted from ZIP: {extracted_path.name}  (format: {fmt})")
        input_path = extracted_path
    else:
        fmt = None  # will be detected below

    # ------------------------------------------------------------------
    # Step 1: Detect format
    # ------------------------------------------------------------------
    if fmt is None:
        fmt = detect(input_path)
    print(f"[ok] Detected format: {fmt}  ({input_path.name})")

    if fmt == "unknown":
        # Produce a helpful hint based on what we can infer from the extension
        suffix     = input_path.suffix.lower()
        stem_suf   = Path(input_path.stem).suffix.lower()
        extra_hint = ""
        if suffix in (".gz", ".bz2") and stem_suf not in (".sql",):
            extra_hint = (
                f" The file appears to be a compressed archive ({suffix}) whose inner "
                f"file is not a .sql dump. Only .sql.gz (PostgreSQL/MySQL plain-text "
                f"dumps compressed with gzip) are supported."
            )
        elif suffix in (".gz", ".bz2") and stem_suf == ".sql":
            extra_hint = (
                " The file has a .sql.gz extension but its content could not be "
                "classified as a PostgreSQL or MySQL dump. Check that the file is a "
                "valid plain-text SQL dump (not a pg_restore binary format) and was "
                "not corrupted or replaced by an HTML auth-error page during download."
            )
        elif suffix in (".sql",):
            extra_hint = (
                " The file has a .sql extension but contains no recognisable "
                "PostgreSQL or MySQL signatures. Verify it is a plain-text SQL dump."
            )
        print(
            f"[!!] Could not determine file format.{extra_hint}\n"
            "     Supported formats: .db/.sqlite (SQLite), .sql/.sql.gz (PostgreSQL/MySQL "
            "plain-text dump, optionally gzip-compressed), .csv, .json, .jsonl, .xlsx, "
            ".parquet, or any of the above wrapped in a .zip archive",
            file=sys.stderr,
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 2: Extract schema (DDL-only - fast, no DML/data rows)
    # ------------------------------------------------------------------
    if fmt == "sqlite":
        from server.ingestion.extract_sqlite import extract
        schema = extract(input_path)
    elif fmt == "csv":
        from server.ingestion.extract_csv import extract
        schema = extract(input_path)
    elif fmt in ("json", "jsonl"):
        from server.ingestion.extract_json import extract
        schema = extract(input_path)
    elif fmt == "xlsx":
        from server.ingestion.extract_xlsx import extract
        schema = extract(input_path)
    elif fmt == "parquet":
        from server.ingestion.extract_parquet import extract
        schema = extract(input_path)
    elif fmt == "sql_script":
        from server.ingestion.extract_sql_dump import extract
        schema = extract(input_path, vendor="generic")
        print(f"[..] Generic SQL script - inferred schema from table references "
              f"(no CREATE TABLE found; columns shown are referenced in SELECT/UPDATE/ALTER)")
    else:
        from server.ingestion.extract_sql_dump import extract
        schema = extract(input_path, vendor=fmt)

    print(f"[ok] Schema extracted: {len(schema.tables)} table(s)")

    # ------------------------------------------------------------------
    # Step 2b: For SQL dumps, import data into a queryable SQLite file
    # ------------------------------------------------------------------
    if fmt == "sql_script":
        # A SQL script has no data rows - create a schema-only SQLite DB so
        # that Browse and Query work (Browse shows 0 rows; Query has the full
        # schema sidebar for writing queries against a real connected database).
        try:
            import sqlite3 as _sqlite3

            out_dir  = Path(args.output_dir)
            stem     = Path(input_path.stem).stem  # strip .sql from foo.sql.gz
            db_path  = out_dir / f"{stem}.db"

            con = _sqlite3.connect(str(db_path))
            cur = con.cursor()
            cur.execute("PRAGMA journal_mode=WAL")

            for table in schema.tables:
                # Build a minimal CREATE TABLE with one column per inferred name.
                # All columns are TEXT (the inferred type for script references).
                safe_name = table.name.replace('"', '""')
                if not table.columns:
                    # Fallback: rowid-only table so the table at least appears
                    col_defs = '"_id" INTEGER PRIMARY KEY'
                else:
                    col_defs = ", ".join(
                        f'"{c.name.replace(chr(34), chr(34)*2)}" TEXT'
                        for c in table.columns
                    )
                cur.execute(
                    f'CREATE TABLE IF NOT EXISTS "{safe_name}" ({col_defs})'
                )

            con.commit()
            con.close()
            print(f"[ok] Schema-only SQLite DB written -> {db_path}  "
                  f"(Browse & Query enabled; 0 rows - script has no data)")
        except Exception as exc:
            print(f"[..] Schema-only SQLite DB skipped: {exc}")

    if fmt in ("postgresql", "mysql"):
        try:
            from server.ingestion.import_to_sqlite import import_dump

            def _pipeline_log(msg: str):
                # Translate import_to_sqlite log prefixes to pipeline stdout tags
                if msg.startswith("ERROR"):
                    print(f"[!!] {msg[5:].strip()}")
                elif msg.startswith("WARN"):
                    print(f"[..] {msg[4:].strip()}")
                else:
                    print(f"[ok] {msg[5:].strip()}" if msg.startswith("INFO") else msg)

            out_dir = Path(args.output_dir)
            db_path = import_dump(input_path, out_dir, vendor=fmt, progress=_pipeline_log)
            if db_path:
                print(f"[ok] SQLite database written -> {db_path}  (queryable via Browse & Query)")
            else:
                print("[..] No data rows imported (dump may be schema-only)")
        except Exception as exc:
            print(f"[..] SQLite import skipped: {exc}")

    # ------------------------------------------------------------------
    # Step 2c: For flat files, load row data into a queryable SQLite file
    # ------------------------------------------------------------------
    if fmt in ("csv", "json", "jsonl", "xlsx", "parquet"):
        try:
            from server.ingestion.flat_file_to_sqlite import load_flat_file

            def _flat_log(msg: str):
                if msg.startswith("ERROR"):
                    print(f"[!!] {msg[5:].strip()}")
                elif msg.startswith("WARN"):
                    print(f"[..] {msg[4:].strip()}")
                else:
                    print(f"[ok] {msg[5:].strip()}" if msg.startswith("INFO") else msg)

            out_dir = Path(args.output_dir)
            db_path = load_flat_file(input_path, out_dir, fmt, progress=_flat_log)
            if db_path:
                print(f"[ok] SQLite database written -> {db_path}  (queryable via Browse & Query)")
            else:
                print("[..] No rows loaded from flat file")
        except Exception as exc:
            print(f"[..] Flat-file SQLite load skipped: {exc}")

    # Infer missing FK relationships from column naming conventions
    if not args.no_infer_fk:
        from server.ingestion.infer_fk import infer_missing_fks
        added = infer_missing_fks(schema)
        if added:
            print(f"[ok] Inferred {added} FK relationship(s) from column names")

    print(schema.summary())

    if not schema.tables:
        print("[!!] No tables found in the input file.", file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 3: Write schema.json (Liam keyed-object format)
    # ------------------------------------------------------------------
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    graph_data = schema_to_graph(schema)
    json_path  = out_dir / "schema.json"
    json_path.write_text(json.dumps(graph_data, ensure_ascii=False, indent=2), encoding="utf-8")

    n_tables = len(graph_data.get("tables", {}))
    n_fks    = sum(
        1 for t in graph_data.get("tables", {}).values()
        for c in t.get("constraints", {}).values()
        if c.get("type") == "FOREIGN KEY"
    )
    print(f"[ok] schema.json written -> {json_path}  ({n_tables} tables, {n_fks} FK edges)")

    # ------------------------------------------------------------------
    # Step 4: Write schema.sql (DDL for reference / debugging)
    # ------------------------------------------------------------------
    from server.pipeline.generate_erd import schema_to_ddl
    ddl_path = out_dir / "schema.sql"
    ddl_path.write_text(schema_to_ddl(schema), encoding="utf-8")
    print(f"[ok] schema.sql written -> {ddl_path}")


if __name__ == "__main__":
    main()
