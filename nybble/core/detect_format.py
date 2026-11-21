"""Format detection module.

Inspects an input file and returns one of several format tokens:
  "sqlite"     - SQLite database file
  "postgresql" - PostgreSQL pg_dump (plain SQL, compressed or not)
  "mysql"      - MySQL mysqldump (plain SQL, compressed or not)
  "sql_script" - Generic SQL script (SELECT/UPDATE/ALTER TABLE etc., no dump header)
  "csv"        - CSV (comma/semicolon/tab-delimited) file
  "json"       - JSON file (array or object root)
  "jsonl"      - Newline-delimited JSON (JSONL / NDJSON)
  "xlsx"       - Excel workbook (.xlsx / .xls)
  "parquet"    - Apache Parquet file
  "zip_multi"  - ZIP archive containing ≥2 supported files (merged into one schema)
  "unknown"    - could not determine

Detection strategy (layered):
  1. Magic bytes  - SQLite header is unambiguous; gzip magic is 0x1f 0x8b;
                    ZIP (xlsx) magic is PK\\x03\\x04; Parquet magic is PAR1
  2. File extension hints (definitive for csv / json / jsonl / xlsx / parquet)
  3. ZIP container: if the file is a plain .zip (not .xlsx), peek at the single
                    inner member - if its extension matches a supported format,
                    return that format.  Multi-file ZIPs are rejected (unknown).
  4. Content heuristics on the SQL text to distinguish PostgreSQL from MySQL

ZIP support:
  maybe_extract_zip(path, dest_dir) - extracts the single supported inner file
  from a .zip archive to dest_dir and returns (inner_path, format_token).
  Callers should use this to materialise the inner file before processing.
"""

from __future__ import annotations

import gzip
import os
import zipfile
from pathlib import Path
from typing import Literal

FormatToken = Literal[
    "sqlite", "postgresql", "mysql", "sql_script",
    "csv", "json", "jsonl", "xlsx", "parquet",
    "zip_multi",
    "unknown",
]

# SQLite database files always start with this 16-byte header string.
_SQLITE_MAGIC = b"SQLite format 3\x00"

# Bytes that appear near the top of a pg_dump plain-SQL file.
_PG_SIGNATURES = [
    b"-- PostgreSQL database dump",
    b"SET standard_conforming_strings",
    b"SET search_path",
    b"pg_dump",
]

# Bytes that appear near the top of a mysqldump file.
_MYSQL_SIGNATURES = [
    b"-- MySQL dump",
    b"ENGINE=",
    b"AUTO_INCREMENT",
    b"/*!",           # MySQL conditional comments
]


# ZIP magic bytes (first 4 bytes of every .xlsx / .xls workbook)
_ZIP_MAGIC = b"PK\x03\x04"

# Apache Parquet magic bytes (first 4 bytes)
_PARQUET_MAGIC = b"PAR1"


# Extensions that can be usefully carried inside a .zip archive.
# Maps lowercase extension → format token.  This is the allowlist.
_ZIP_ALLOWED_EXTS: dict[str, FormatToken] = {
    ".csv":     "csv",
    ".json":    "json",
    ".jsonl":   "jsonl",
    ".ndjson":  "jsonl",
    ".sql":     None,     # needs content-based classification - handled specially
    ".db":      "sqlite",
    ".sqlite":  "sqlite",
    ".sqlite3": "sqlite",
    ".parquet": "parquet",
    ".xlsx":    "xlsx",
    ".xls":     "xlsx",
}


def _fmt_for_member(zf: "zipfile.ZipFile", member: "zipfile.ZipInfo") -> "FormatToken | None":
    """Return the format token for a single ZipInfo member, or None if unsupported."""
    inner_suffix = Path(member.filename).suffix.lower()
    fmt = _ZIP_ALLOWED_EXTS.get(inner_suffix)
    if fmt is None and inner_suffix != ".sql":
        return None  # extension not in allowlist
    if inner_suffix == ".sql":
        sample = zf.read(member.filename)[:8192]
        sql_fmt = _classify_sql(sample)
        return sql_fmt if sql_fmt != "unknown" else None
    return fmt  # type: ignore[return-value]


def _detect_zip(path: Path) -> FormatToken:
    """Peek inside a plain .zip archive and classify it.

    Rules:
      - Exactly ONE file with a supported extension  → return that format token.
      - Multiple files where ALL have supported extensions → return "zip_multi".
      - Any unsupported extension present             → return "unknown".
      - Zero supported files                          → return "unknown".
      - For .sql inner files, read a sample to distinguish pg/mysql/sql_script.
    """
    try:
        with zipfile.ZipFile(path, "r") as zf:
            # Only count actual files (skip directory entries)
            members = [m for m in zf.infolist() if not m.is_dir()]
            if not members:
                return "unknown"

            # Classify every member; bail immediately on any unsupported file
            fmts: list[FormatToken] = []
            for member in members:
                member_fmt = _fmt_for_member(zf, member)
                if member_fmt is None:
                    return "unknown"   # unsupported file → whole ZIP is unknown
                fmts.append(member_fmt)

            if len(members) == 1:
                return fmts[0]

            # Multiple files - all supported → "zip_multi"
            return "zip_multi"

    except (zipfile.BadZipFile, Exception):
        return "unknown"


def list_zip_members(path: Path) -> list[tuple[str, "FormatToken"]]:
    """Return [(member_filename, format_token), ...] for all supported files in *path*.

    Skips directories and files with unsupported extensions.
    Returns an empty list if *path* is not a valid ZIP or has no supported members.
    Used by the multi-file ZIP pipeline to iterate inner files.
    """
    result: list[tuple[str, FormatToken]] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                member_fmt = _fmt_for_member(zf, member)
                if member_fmt is not None:
                    result.append((member.filename, member_fmt))
    except Exception:
        pass
    return result


def detect(path: str | Path) -> FormatToken:
    """Return the format token for *path*."""
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    # ------------------------------------------------------------------
    # 1. Read first bytes for magic detection
    # ------------------------------------------------------------------
    with open(path, "rb") as fh:
        header = fh.read(64)

    # SQLite: unambiguous 16-byte magic
    if header[:16] == _SQLITE_MAGIC:
        return "sqlite"

    # Apache Parquet: PAR1 magic at start of file
    if header[:4] == _PARQUET_MAGIC:
        return "parquet"

    # gzip-compressed: magic bytes 0x1f 0x8b
    is_gzip = header[:2] == b"\x1f\x8b"

    # ------------------------------------------------------------------
    # 2. Extension shortcuts for clear cases (checked before SQL heuristics)
    # ------------------------------------------------------------------
    suffix = path.suffix.lower()
    stem_suffix = Path(path.stem).suffix.lower()  # handles foo.sql.gz → .sql

    if suffix in (".db", ".sqlite", ".sqlite3"):
        return "sqlite"

    # CSV
    if suffix == ".csv":
        return "csv"

    # JSON / JSONL / NDJSON
    if suffix in (".jsonl", ".ndjson"):
        return "jsonl"
    if suffix == ".json":
        return "json"

    # Excel workbooks
    # .xlsx/.xls with ZIP magic → modern OOXML format
    # .xlsx/.xls by extension alone → treat as xlsx (openpyxl handles both; xlrd covers
    #   legacy BIFF .xls when openpyxl cannot)
    if suffix in (".xlsx", ".xls"):
        return "xlsx"

    # Parquet by extension (magic may be at end of file for some writers)
    if suffix == ".parquet":
        return "parquet"

    # ------------------------------------------------------------------
    # 2b. Plain ZIP archives (not .xlsx/.xls - those are handled above)
    # ------------------------------------------------------------------
    if suffix == ".zip" and header[:4] == _ZIP_MAGIC:
        return _detect_zip(path)

    # JSON by content: if file starts with [ or { (after optional UTF-8 BOM)
    try:
        text_head = header.lstrip(b"\xef\xbb\xbf")  # strip UTF-8 BOM
        if text_head and text_head[0:1] in (b"[", b"{"):
            return "json"
    except Exception:
        pass

    # ------------------------------------------------------------------
    # 2c. Compressed-file extension shortcuts (foo.sql.gz, foo.sql.bz2, etc.)
    #     stem_suffix is the extension of the name without the compression suffix
    #     e.g.  vuom_backup.sql.gz  → suffix=.gz, stem_suffix=.sql
    # ------------------------------------------------------------------
    if stem_suffix == ".sql" and suffix in (".gz", ".bz2", ".z"):
        # The inner file is SQL - decompress and classify
        is_gzip = is_gzip or (suffix == ".gz")

    # ------------------------------------------------------------------
    # 3. Read a sample of the SQL text (decompressing if necessary)
    # ------------------------------------------------------------------
    sample = _read_sql_sample(path, is_gzip, max_bytes=8192)

    if sample is None:
        # Couldn't read as text - try extension fallback
        if is_gzip or suffix in (".sql", ".gz") or stem_suffix == ".sql":
            return "unknown"
        return "unknown"

    # Reject non-SQL content early: if the sample looks like HTML/XML (e.g.
    # an auth-required error page downloaded from a cloud storage URL), report
    # "unknown" with a clear indicator rather than misclassifying as SQL.
    _stripped = sample.lstrip(b" \t\r\n")
    if _stripped[:5] in (b"<!DOC", b"<!doc") or _stripped[:5] == b"<?xml":
        return "unknown"

    return _classify_sql(sample)


def maybe_extract_zip(path: Path, dest_dir: Path) -> tuple[Path, FormatToken] | None:
    """If *path* is a .zip archive containing exactly one supported file, extract
    that file to *dest_dir* and return ``(extracted_path, format_token)``.

    Returns ``None`` if:
      - The file is not a .zip archive.
      - The ZIP contains 0 real files.
      - The single inner file's extension is not in the allowed list.
      - The ZIP contains multiple files (use list_zip_members + extract loop instead).

    The caller is responsible for using the returned path in place of the
    original zip path for all subsequent processing steps.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix != ".zip":
        return None

    fmt = detect(path)
    if fmt == "unknown":
        return None

    try:
        with zipfile.ZipFile(path, "r") as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            if len(members) != 1:
                return None
            member = members[0]
            # Extract preserving just the filename (no subdirectory structure)
            dest_dir = Path(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            out_path = dest_dir / Path(member.filename).name
            out_path.write_bytes(zf.read(member.filename))
            return out_path, fmt
    except Exception:
        return None


def _read_sql_sample(path: Path, is_gzip: bool, max_bytes: int) -> bytes | None:
    """Return up to *max_bytes* of raw SQL bytes from the file.

    Falls back to plain-text reading if gzip decompression fails - this handles
    the common case of a file named *.sql.gz that is actually plain SQL text.
    """
    if is_gzip:
        try:
            with gzip.open(path, "rb") as fh:
                return fh.read(max_bytes)
        except Exception:
            pass  # not actually gzip - fall through to plain read
    try:
        with open(path, "rb") as fh:
            return fh.read(max_bytes)
    except Exception:
        return None


# SQL keywords that signal a generic SQL script even without dump-style headers
_SQL_SCRIPT_KEYWORDS = [
    b"SELECT ", b"SELECT\t", b"SELECT\n",
    b"INSERT INTO", b"UPDATE ", b"UPDATE\t",
    b"DELETE FROM", b"ALTER TABLE", b"DROP TABLE",
    b"CREATE VIEW", b"CREATE OR REPLACE",
    b"WITH ", b"WITH\t",  # CTEs
]


def _classify_sql(sample: bytes) -> FormatToken:
    """Classify a SQL text sample as postgresql, mysql, sql_script, or unknown."""
    sample_upper = sample.upper()

    # Score each vendor by number of signature hits
    pg_score = sum(1 for sig in _PG_SIGNATURES if sig.upper() in sample_upper)
    my_score = sum(1 for sig in _MYSQL_SIGNATURES if sig.upper() in sample_upper)

    # Strong vendor signal → classify immediately
    if pg_score > 0 or my_score > 0:
        return "postgresql" if pg_score >= my_score else "mysql"

    # No vendor-specific dump headers.
    # Check for CREATE TABLE (generic DDL dump without vendor header)
    if b"CREATE TABLE" in sample_upper:
        # Tiebreak: PostgreSQL-specific dollar-quoting or SERIAL
        if b"$$" in sample or b"SERIAL" in sample_upper or b"RETURNING" in sample_upper:
            return "postgresql"
        return "mysql"  # safer fallback for generic DDL

    # Check for any recognisable SQL statement keyword - this is a script file
    for kw in _SQL_SCRIPT_KEYWORDS:
        if kw in sample_upper:
            return "sql_script"

    return "unknown"
