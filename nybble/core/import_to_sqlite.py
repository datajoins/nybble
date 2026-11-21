"""Phase 4a - SQL dump → SQLite importer.

Reads a PostgreSQL or MySQL plain-SQL dump (optionally gzip-compressed),
translates DDL to SQLite-compatible syntax, parses COPY...FROM stdin
and INSERT blocks, and writes a queryable SQLite database file.

The resulting .db file is placed alongside schema.json in the output dir
so that api_rows() and api_query() can serve Browse and Query requests
without a live database connection.

Usage (called from run_pipeline.py):
    from import_to_sqlite import import_dump
    db_path = import_dump(dump_path, out_dir, vendor="postgresql")

Returns:
    Path to the written .db file, or None if import failed / no data found.
"""

from __future__ import annotations

import gzip
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Literal

Vendor = Literal["postgresql", "mysql"]

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def import_dump(
    dump_path: str | Path,
    out_dir: str | Path,
    vendor: Vendor = "postgresql",
    progress=None,          # callable(str) for log lines, optional
) -> Path | None:
    """Parse *dump_path* and write a SQLite DB to *out_dir/<stem>.db*.

    Returns the Path of the written database, or None on total failure.
    *progress* is an optional callable that receives human-readable log strings.
    """
    dump_path = Path(dump_path)
    out_dir   = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def log(msg: str):
        if progress:
            progress(msg)

    log(f"INFO  [import] Reading {dump_path.name} ...")
    sql_text = _read_dump(dump_path)
    if not sql_text:
        log("ERROR [import] Could not read dump file")
        return None

    log(f"INFO  [import] Translating DDL to SQLite syntax ...")
    ddl_stmts = _extract_create_tables(sql_text, vendor)
    if not ddl_stmts:
        log("WARN  [import] No CREATE TABLE statements found - skipping SQLite import")
        return None

    # Determine output path: <out_dir>/<stem>.db
    # Strip double extension: foo.sql.gz → foo, foo.sql → foo
    stem = dump_path.stem
    if stem.endswith(".sql"):
        stem = stem[:-4]
    db_path = out_dir / f"{stem}.db"

    log(f"INFO  [import] Creating SQLite database: {db_path.name}")

    try:
        # Remove stale DB from a previous run
        if db_path.exists():
            db_path.unlink()

        con = sqlite3.connect(str(db_path))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA foreign_keys=OFF")  # skip FK checks during bulk load

        # Create tables
        tables_created = 0
        for stmt in ddl_stmts:
            try:
                con.execute(stmt)
                tables_created += 1
            except sqlite3.Error as e:
                log(f"WARN  [import] CREATE TABLE skipped: {e} - {stmt[:80].strip()}")
        con.commit()
        log(f"INFO  [import] {tables_created} table(s) created")

        if tables_created == 0:
            con.close()
            db_path.unlink(missing_ok=True)
            return None

        # Load data rows
        total_rows = 0
        if vendor == "postgresql":
            total_rows = _load_pg_copy_blocks(sql_text, con, log)
        # Also pick up any INSERT statements (present in both PG and MySQL dumps)
        total_rows += _load_insert_statements(sql_text, con, log, vendor)

        con.execute("PRAGMA foreign_keys=ON")
        con.commit()
        con.close()

        log(f"INFO  [import] Import complete - {total_rows} row(s) loaded into {db_path.name}")
        return db_path

    except Exception as exc:
        log(f"ERROR [import] Fatal error: {exc}")
        try:
            db_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def _read_dump(path: Path) -> str:
    """Read dump as text, decompressing gzip if needed."""
    if path.suffix.lower() == ".gz":
        try:
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            pass
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# DDL translation: PostgreSQL/MySQL → SQLite
# ---------------------------------------------------------------------------

# Types that map directly (case-insensitive prefix match)
_TYPE_MAP: list[tuple[str, str]] = [
    # Integers
    ("BIGSERIAL",          "INTEGER"),
    ("SMALLSERIAL",        "INTEGER"),
    ("SERIAL",             "INTEGER"),
    ("BIGINT",             "INTEGER"),
    ("INT8",               "INTEGER"),
    ("INT4",               "INTEGER"),
    ("INT2",               "INTEGER"),
    ("SMALLINT",           "INTEGER"),
    ("TINYINT",            "INTEGER"),
    ("MEDIUMINT",          "INTEGER"),
    ("INTEGER",            "INTEGER"),
    ("INT",                "INTEGER"),
    # Reals
    ("DOUBLE PRECISION",   "REAL"),
    ("DOUBLE",             "REAL"),
    ("FLOAT8",             "REAL"),
    ("FLOAT4",             "REAL"),
    ("FLOAT",              "REAL"),
    ("REAL",               "REAL"),
    ("NUMERIC",            "REAL"),
    ("DECIMAL",            "REAL"),
    ("MONEY",              "REAL"),
    # Booleans → stored as INTEGER 0/1 in SQLite
    ("BOOLEAN",            "INTEGER"),
    ("BOOL",               "INTEGER"),
    # Date/time - kept as TEXT in SQLite (ISO 8601 sortable)
    ("TIMESTAMP",          "TEXT"),
    ("DATETIME",           "TEXT"),
    ("DATE",               "TEXT"),
    ("TIME",               "TEXT"),
    ("INTERVAL",           "TEXT"),
    # Binary
    ("BYTEA",              "BLOB"),
    ("BINARY",             "BLOB"),
    ("VARBINARY",          "BLOB"),
    ("LONGBLOB",           "BLOB"),
    ("MEDIUMBLOB",         "BLOB"),
    ("TINYBLOB",           "BLOB"),
    ("BLOB",               "BLOB"),
    # Text variants → all TEXT in SQLite
    ("CHARACTER VARYING",  "TEXT"),
    ("VARCHAR",            "TEXT"),
    ("CHARACTER",          "TEXT"),
    ("CHAR",               "TEXT"),
    ("NVARCHAR",           "TEXT"),
    ("NCHAR",              "TEXT"),
    ("LONGTEXT",           "TEXT"),
    ("MEDIUMTEXT",         "TEXT"),
    ("TINYTEXT",           "TEXT"),
    ("CLOB",               "TEXT"),
    ("TEXT",               "TEXT"),
    # PostgreSQL-specific
    ("JSONB",              "TEXT"),
    ("JSON",               "TEXT"),
    ("UUID",               "TEXT"),
    ("CITEXT",             "TEXT"),
    ("INET",               "TEXT"),
    ("CIDR",               "TEXT"),
    ("MACADDR",            "TEXT"),
    ("TSQUERY",            "TEXT"),
    ("TSVECTOR",           "TEXT"),
    ("XML",                "TEXT"),
    ("POINT",              "TEXT"),
    ("LINE",               "TEXT"),
    ("LSEG",               "TEXT"),
    ("BOX",                "TEXT"),
    ("PATH",               "TEXT"),
    ("POLYGON",            "TEXT"),
    ("CIRCLE",             "TEXT"),
    ("BIT",                "TEXT"),
    ("VARBIT",             "TEXT"),
    ("OID",                "INTEGER"),
    ("REGCLASS",           "TEXT"),
    ("REGTYPE",            "TEXT"),
    # NOTE: "NAME" (PG system catalog identifier type) intentionally omitted -
    # the word-boundary regex would incorrectly replace common column identifiers
    # like `name` that appear in column definitions.  Unrecognised types fall
    # back to TEXT in SQLite anyway.
    ("HSTORE",             "TEXT"),
    # MySQL-specific
    ("ENUM",               "TEXT"),
    ("SET",                "TEXT"),
    ("YEAR",               "INTEGER"),
]

# Pre-compile a single regex that matches any mapped type (longest first)
_TYPE_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(src) for src, _ in _TYPE_MAP) + r")\b"
    r"(?:\s*\([^)]*\))?",          # optional size/precision e.g. VARCHAR(255)
    re.IGNORECASE,
)
_TYPE_LOOKUP = {src.upper(): dst for src, dst in _TYPE_MAP}


def _translate_type(match: re.Match) -> str:
    key = match.group(1).upper()
    return _TYPE_LOOKUP.get(key, "TEXT")


# Regexes for whole-line/whole-statement transformations
_STRIP_PG_CASTS      = re.compile(r"::[a-z_][a-z0-9_ ]*(?:\[\])?", re.IGNORECASE)
_STRIP_COLLATE       = re.compile(r"\bCOLLATE\s+\S+", re.IGNORECASE)
_STRIP_TABLESPACE    = re.compile(r"\bTABLESPACE\s+\S+", re.IGNORECASE)
_STRIP_STORAGE       = re.compile(r"\bSTORAGE\s+\S+", re.IGNORECASE)
_STRIP_WITH_OPTS     = re.compile(r"\bWITH\s*\([^)]*\)", re.IGNORECASE)
_STRIP_INHERITS      = re.compile(r"\bINHERITS\s*\([^)]*\)", re.IGNORECASE)
_STRIP_ON_CONFLICT   = re.compile(r"\bON\s+CONFLICT\s+DO\s+\w+", re.IGNORECASE)
_STRIP_DEFERRED      = re.compile(r"\bINITIALLY\s+(?:DEFERRED|IMMEDIATE)\b", re.IGNORECASE)
_STRIP_DEFERRABLE    = re.compile(r"\b(?:NOT\s+)?DEFERRABLE\b", re.IGNORECASE)
_STRIP_ON_DELETE_SET = re.compile(r"\bON\s+DELETE\s+SET\s+DEFAULT\b", re.IGNORECASE)
_MYSQL_ENGINE        = re.compile(r"\)\s*ENGINE\s*=\s*\S+[^;]*", re.IGNORECASE)
_MYSQL_CHARSET       = re.compile(r"\bDEFAULT\s+CHARSET\s*=\s*\S+", re.IGNORECASE)
_MYSQL_COND_COMMENT  = re.compile(r"/\*!.*?\*/", re.DOTALL)
_BACKTICK            = re.compile(r"`([^`]+)`")
_SERIAL_DEFAULT_RE   = re.compile(
    r"\bDEFAULT\s+nextval\s*\([^)]*\)",
    re.IGNORECASE,
)
_PG_GENERATED        = re.compile(
    r"\bGENERATED\s+(?:ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY(?:\s*\([^)]*\))?",
    re.IGNORECASE,
)

# Block comment and line comment
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT  = re.compile(r"--[^\n]*")

# Dollar-quote: $$...$$ or $tag$...$tag$
_DOLLAR_QUOTE  = re.compile(r"\$([^$]*)\$.*?\$\1\$", re.DOTALL)

# COPY block regex (used to skip the block in DDL translation phase)
_COPY_BLOCK_RE = re.compile(
    r"COPY\s+\S+.*?FROM\s+stdin\s*;.*?^\\\.",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)

# CREATE TABLE regex (identical to extract_sql_dump.py's version)
_SCHEMA_PREFIX_P = r"(?:(?:\"[^\"]+\"|`[^`]+`|\w+)\.)?"
_TBL_NAME_P      = r"(?:\"([^\"]+)\"|`([^`]+)`|(\w+))"
_CREATE_TABLE_RE  = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    + _SCHEMA_PREFIX_P + _TBL_NAME_P
    + r"\s*\((.+?)\)\s*(?:[^;]*);",
    re.DOTALL | re.IGNORECASE,
)


def _unquote(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] in ('"', '`', '[') and name[-1] in ('"', '`', ']'):
        return name[1:-1]
    return name


# Strip schema-qualified table name: CREATE TABLE schema.table → CREATE TABLE table
# Handles double-quoted schema names and plain identifiers.
_STRIP_SCHEMA_PREFIX = re.compile(
    r"(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)"
    r"(?:\"[^\"]+\"|`[^`]+`|\w+)\."     # schema name + dot  (discarded)
    r"((?:\"[^\"]+\"|`[^`]+`|\w+))",    # table name          (kept)
    re.IGNORECASE,
)


def _translate_create_table(stmt: str, vendor: Vendor) -> str:
    """Translate a single CREATE TABLE statement to SQLite-compatible DDL."""
    # Strip schema prefix (e.g. public.tablename → tablename)
    stmt = _STRIP_SCHEMA_PREFIX.sub(r"\1\2", stmt)
    # Remove MySQL conditional comments
    stmt = _MYSQL_COND_COMMENT.sub(" ", stmt)
    # Remove PostgreSQL dollar-quoted strings (function bodies in defaults)
    stmt = _DOLLAR_QUOTE.sub("''", stmt)
    # Strip PG-specific type casts (::type)
    stmt = _STRIP_PG_CASTS.sub("", stmt)
    # Replace GENERATED … AS IDENTITY with PRIMARY KEY AUTOINCREMENT equivalent
    stmt = _PG_GENERATED.sub("", stmt)
    # Strip DEFAULT nextval(...) - becomes AUTOINCREMENT implicitly for INTEGER PKs
    stmt = _SERIAL_DEFAULT_RE.sub("", stmt)
    # Map all types
    stmt = _TYPE_PATTERN.sub(_translate_type, stmt)
    # Strip SQLite-unsupported clauses
    stmt = _STRIP_COLLATE.sub("", stmt)
    stmt = _STRIP_TABLESPACE.sub("", stmt)
    stmt = _STRIP_STORAGE.sub("", stmt)
    stmt = _STRIP_WITH_OPTS.sub("", stmt)
    stmt = _STRIP_INHERITS.sub("", stmt)
    stmt = _STRIP_ON_CONFLICT.sub("", stmt)
    stmt = _STRIP_DEFERRED.sub("", stmt)
    stmt = _STRIP_DEFERRABLE.sub("", stmt)
    stmt = _STRIP_ON_DELETE_SET.sub("ON DELETE SET NULL", stmt)
    # Strip AUTO_INCREMENT - SQLite doesn't support it (INTEGER PRIMARY KEY is auto-seq)
    stmt = re.sub(r"\bAUTO_INCREMENT\b", "", stmt, flags=re.IGNORECASE)
    # Backticks → double-quotes (MySQL identifiers)
    stmt = _BACKTICK.sub(r'"\1"', stmt)
    if vendor == "mysql":
        # Strip ENGINE=, CHARSET= trailer
        stmt = _MYSQL_ENGINE.sub(")", stmt)
        stmt = _MYSQL_CHARSET.sub("", stmt)
        # Ensure stmt ends with semicolon
        stmt = stmt.rstrip()
        if not stmt.endswith(";"):
            stmt += ";"
    # Collapse excess whitespace
    stmt = re.sub(r" {2,}", " ", stmt)
    return stmt


def _extract_create_tables(sql_text: str, vendor: Vendor) -> list[str]:
    """Return a list of SQLite-ready CREATE TABLE statements."""
    # Strip COPY blocks and block/line comments before matching
    clean = _COPY_BLOCK_RE.sub("", sql_text)
    clean = _BLOCK_COMMENT.sub(" ", clean)
    if vendor == "postgresql":
        clean = _DOLLAR_QUOTE.sub("''", clean)
    else:
        clean = _MYSQL_COND_COMMENT.sub(" ", clean)
    clean = _LINE_COMMENT.sub("", clean)

    stmts = []
    for m in _CREATE_TABLE_RE.finditer(clean):
        translated = _translate_create_table(m.group(0), vendor)
        stmts.append(translated)
    return stmts


# ---------------------------------------------------------------------------
# Data loading: PostgreSQL COPY … FROM stdin
# ---------------------------------------------------------------------------

# Matches:  COPY [schema.]tablename (col1, col2, ...) FROM stdin;
_COPY_HEADER_RE = re.compile(
    r"COPY\s+"
    + r"(?:(?:\"[^\"]+\"|\w+)\.)?"   # optional schema prefix
    + r"(?:\"([^\"]+)\"|(\w+))"       # table name
    + r"\s*\(([^)]+)\)"               # column list
    + r"\s+FROM\s+stdin\s*;",
    re.IGNORECASE,
)

# Terminator for COPY blocks
_COPY_TERM = re.compile(r"^\\\.$", re.MULTILINE)


def _load_pg_copy_blocks(sql_text: str, con: sqlite3.Connection, log) -> int:
    """Parse COPY…FROM stdin blocks and INSERT rows into the SQLite connection.

    Returns the total number of rows inserted.
    """
    total = 0
    # Split into COPY blocks by finding headers, then extracting data up to \.
    pos = 0
    for hm in _COPY_HEADER_RE.finditer(sql_text):
        tbl_name = _unquote(hm.group(1) or hm.group(2) or "")
        cols_raw = hm.group(3)
        col_names = [_unquote(c.strip()) for c in cols_raw.split(",")]

        # Data starts after the semicolon that closes the COPY header
        data_start = hm.end()
        # Find the \. terminator
        term_m = _COPY_TERM.search(sql_text, data_start)
        if not term_m:
            continue
        data_block = sql_text[data_start:term_m.start()]

        rows_batch: list[tuple] = []
        for line in data_block.splitlines():
            if not line or line == "\\.":
                continue
            row = _parse_copy_row(line, len(col_names))
            if row is not None:
                rows_batch.append(row)

        if not rows_batch:
            continue

        placeholders = ", ".join(["?"] * len(col_names))
        quoted_cols  = ", ".join(f'"{c}"' for c in col_names)
        insert_sql   = f'INSERT OR IGNORE INTO "{tbl_name}" ({quoted_cols}) VALUES ({placeholders})'

        try:
            con.executemany(insert_sql, rows_batch)
            con.commit()
            total += len(rows_batch)
            log(f"INFO  [import] {tbl_name}: {len(rows_batch)} row(s) from COPY block")
        except sqlite3.Error as e:
            log(f"WARN  [import] {tbl_name}: COPY insert failed: {e}")

    return total


def _parse_copy_row(line: str, n_cols: int) -> tuple | None:
    """Parse one tab-delimited COPY data line into a Python tuple.

    PostgreSQL COPY text format:
    - Fields are tab-separated
    - NULL is represented as \\N
    - Backslash escapes: \\t → tab, \\n → newline, \\\\ → backslash
    """
    parts = line.split("\t")
    if len(parts) != n_cols:
        return None

    result = []
    for p in parts:
        if p == "\\N":
            result.append(None)
        else:
            # Unescape PostgreSQL COPY escape sequences
            p = p.replace("\\\\", "\x00BACKSLASH\x00")
            p = p.replace("\\t", "\t")
            p = p.replace("\\n", "\n")
            p = p.replace("\\r", "\r")
            p = p.replace("\x00BACKSLASH\x00", "\\")
            result.append(p)
    return tuple(result)


# ---------------------------------------------------------------------------
# Data loading: INSERT statements (PostgreSQL and MySQL)
# ---------------------------------------------------------------------------

# Match single multi-value INSERT: INSERT INTO [schema.]table (cols) VALUES (...),(...),...;
_INSERT_RE = re.compile(
    r"INSERT\s+(?:IGNORE\s+)?INTO\s+"
    + r"(?:(?:\"[^\"]+\"|`[^`]+`|\w+)\.)?"      # optional schema prefix
    + r"(?:\"([^\"]+)\"|`([^`]+)`|(\w+))"         # table name
    + r"\s*\(([^)]+)\)"                            # column list
    + r"\s+VALUES\s+(.+?);",                       # VALUES clause
    re.DOTALL | re.IGNORECASE,
)

# Match individual VALUES row tuples from the VALUES clause
_VALUES_ROW_RE = re.compile(r"\(([^()]*)\)", re.DOTALL)

# Match a quoted string value: 'text with ''escapes'''
_QUOTED_VAL_RE = re.compile(r"'((?:[^']|'')*)'|(\bNULL\b)|(-?[\d.]+(?:e[+-]?\d+)?)", re.IGNORECASE)


def _load_insert_statements(
    sql_text: str,
    con: sqlite3.Connection,
    log,
    vendor: Vendor,
) -> int:
    """Find INSERT…VALUES statements and load them into the SQLite connection.

    Returns the total number of rows inserted.
    """
    total = 0
    for m in _INSERT_RE.finditer(sql_text):
        tbl_name = _unquote(m.group(1) or m.group(2) or m.group(3) or "")
        cols_raw = m.group(4)
        values_clause = m.group(5)

        col_names = [_unquote(c.strip()) for c in cols_raw.split(",")]
        n_cols = len(col_names)

        rows_batch: list[tuple] = []
        for row_m in _VALUES_ROW_RE.finditer(values_clause):
            row = _parse_values_row(row_m.group(1), n_cols)
            if row is not None:
                rows_batch.append(row)

        if not rows_batch:
            continue

        placeholders = ", ".join(["?"] * n_cols)
        quoted_cols  = ", ".join(f'"{c}"' for c in col_names)
        insert_sql   = f'INSERT OR IGNORE INTO "{tbl_name}" ({quoted_cols}) VALUES ({placeholders})'

        try:
            con.executemany(insert_sql, rows_batch)
            con.commit()
            total += len(rows_batch)
        except sqlite3.Error as e:
            log(f"WARN  [import] {tbl_name}: INSERT failed: {e}")

    if total:
        log(f"INFO  [import] {total} row(s) from INSERT statements")
    return total


def _parse_values_row(row_body: str, n_cols: int) -> tuple | None:
    """Parse the interior of a VALUES (...) tuple.

    Handles: NULL, 'quoted strings' with '' escapes, numbers, booleans.
    """
    values = []
    for vm in _QUOTED_VAL_RE.finditer(row_body):
        if vm.group(1) is not None:
            # Quoted string - unescape '' → '
            values.append(vm.group(1).replace("''", "'"))
        elif vm.group(2) is not None:
            values.append(None)
        elif vm.group(3) is not None:
            raw = vm.group(3)
            try:
                values.append(int(raw))
            except ValueError:
                try:
                    values.append(float(raw))
                except ValueError:
                    values.append(raw)
    if len(values) != n_cols:
        return None
    return tuple(values)
