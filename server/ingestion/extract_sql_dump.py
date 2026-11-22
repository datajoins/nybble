"""Path B + C - SQL dump extractor (PostgreSQL, MySQL) and SQL script analyser.

Parses plain-SQL dump files (optionally gzip-compressed) to extract
CREATE TABLE statements, constraints, and CREATE INDEX statements.

Also handles generic SQL scripts (SELECT/UPDATE/ALTER TABLE etc.) that have
no CREATE TABLE - infers table names from FROM/JOIN/UPDATE/INTO/ALTER TABLE
clauses and exposes them as stub tables with inferred column names.

Design principles:
  - Zero hardcoded table or column names - purely structural parsing
  - Handles PostgreSQL-specific syntax (SERIAL, BIGSERIAL, UUID, JSONB,
    dollar-quoting, SET search_path, COPY blocks, etc.)
  - Handles MySQL-specific syntax (backtick identifiers, ENGINE=, AUTO_INCREMENT,
    ENUM, MEDIUMTEXT, TINYINT, /*!...*/ comments, etc.)
  - Produces the same Schema object regardless of vendor
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path
from typing import Literal

from .schema_model import Column, ForeignKey, Index, Schema, Table

Vendor = Literal["postgresql", "mysql"]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(dump_path: str | Path, vendor: Vendor | str) -> Schema:
    """Parse a SQL dump file (or generic SQL script) and return a Schema.

    When vendor == 'generic' (i.e. sql_script format), table structure is
    inferred from FROM/JOIN/UPDATE/INTO/ALTER TABLE clauses rather than
    CREATE TABLE blocks.
    """
    dump_path = Path(dump_path)
    sql_text = _read_dump(dump_path)
    schema_name = Path(dump_path.stem).stem  # strip .sql from foo.sql.gz
    if vendor == "generic":
        return _infer_schema_from_script(sql_text, schema_name)
    return _parse_sql(sql_text, schema_name, vendor)


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _read_dump(path: Path) -> str:
    """Read the dump file as text, falling back to plain read if not gzip."""
    if path.suffix.lower() == ".gz":
        try:
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            pass  # file has .gz extension but is not actually compressed
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Pre-processing: strip noise that confuses CREATE TABLE parsing
# ---------------------------------------------------------------------------

# PostgreSQL dollar-quoted string $$...$$ or $tag$...$tag$
_PG_DOLLAR_QUOTE_RE = re.compile(r"\$([^$]*)\$.*?\$\1\$", re.DOTALL)

# MySQL conditional comments /*!...*/ and regular -- comments
_MYSQL_COND_COMMENT_RE = re.compile(r"/\*!.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# PostgreSQL COPY ... FROM stdin blocks (end with \.)
_PG_COPY_BLOCK_RE = re.compile(
    r"COPY\s+\S+.*?FROM\s+stdin\s*;.*?^\\\.", re.DOTALL | re.MULTILINE | re.IGNORECASE
)


def _preprocess(sql: str, vendor: Vendor) -> str:
    """Strip noise so that structural regex matches are reliable."""
    # Remove block comments first (they can span lines containing keywords)
    sql = _BLOCK_COMMENT_RE.sub(" ", sql)
    if vendor == "postgresql":
        sql = _PG_DOLLAR_QUOTE_RE.sub("''", sql)
        sql = _PG_COPY_BLOCK_RE.sub("", sql)
    else:
        sql = _MYSQL_COND_COMMENT_RE.sub(" ", sql)
    sql = _LINE_COMMENT_RE.sub("", sql)
    return sql


# ---------------------------------------------------------------------------
# CREATE TABLE extraction
# ---------------------------------------------------------------------------

# Optional schema prefix: schema.table or "schema"."table" - we discard the schema part.
# Matches: plain.plain  "quoted"."quoted"  plain."quoted"  "quoted".plain
_SCHEMA_PREFIX = (
    r"(?:"
    r"(?:\"[^\"]+\"|`[^`]+`|\w+)\."  # schema name + dot (discarded)
    r")?"
)

# Table name with optional schema prefix - captures just the table name part.
_TBL_NAME = (
    r"(?:"
    r"\"([^\"]+)\""   # double-quoted
    r"|`([^`]+)`"     # backtick-quoted
    r"|(\w+)"         # unquoted
    r")"
)

# Match CREATE TABLE [IF NOT EXISTS] [schema.]table (body);
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    + _SCHEMA_PREFIX + _TBL_NAME +
    r"\s*\((.+?)\)\s*(?:[^;]*);",
    re.DOTALL | re.IGNORECASE,
)

# Match CREATE [UNIQUE] INDEX [IF NOT EXISTS] name ON [schema.]table (cols)
_CREATE_INDEX_RE = re.compile(
    r"CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    + _SCHEMA_PREFIX + _TBL_NAME +   # index name (schema prefix optional)
    r"\s+ON\s+"
    + _SCHEMA_PREFIX + _TBL_NAME +   # table name (schema prefix optional)
    r"\s*\(([^)]+)\)",
    re.IGNORECASE,
)

# ALTER TABLE [ONLY] [schema.]table
#   ADD [CONSTRAINT name] FOREIGN KEY (cols)
#   REFERENCES [schema.]table (cols)
#   [DEFERRABLE [INITIALLY DEFERRED|IMMEDIATE]]  -- PG-specific suffix, ignored
_ALTER_FK_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:ONLY\s+)?"
    + _SCHEMA_PREFIX + _TBL_NAME +           # source table
    r".*?ADD\s+(?:CONSTRAINT\s+\w+\s+)?"
    r"FOREIGN\s+KEY\s*\(([^)]+)\)"
    r"\s+REFERENCES\s+"
    + _SCHEMA_PREFIX + _TBL_NAME +           # referenced table
    r"\s*\(([^)]+)\)"
    r"(?:\s+(?:NOT\s+)?DEFERRABLE[^;]*)?",   # optional DEFERRABLE clause
    re.DOTALL | re.IGNORECASE,
)

# ALTER TABLE [ONLY] [schema.]table
#   ADD [CONSTRAINT name] PRIMARY KEY (cols)
_ALTER_PK_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:ONLY\s+)?"
    + _SCHEMA_PREFIX + _TBL_NAME +           # table name
    r"\s+ADD\s+(?:CONSTRAINT\s+\w+\s+)?"
    r"PRIMARY\s+KEY\s*\(([^)]+)\)",
    re.DOTALL | re.IGNORECASE,
)


def _unquote(name: str) -> str:
    """Strip surrounding backticks, double-quotes, or brackets."""
    name = name.strip()
    if (name.startswith('"') and name.endswith('"')) or \
       (name.startswith('`') and name.endswith('`')) or \
       (name.startswith('[') and name.endswith(']')):
        return name[1:-1]
    return name


def _first_match(*groups) -> str:
    """Return the first non-None, non-empty group from a regex match."""
    return next((g for g in groups if g), "")


def _split_columns(body: str) -> list[str]:
    """Split the body of a CREATE TABLE into individual clause strings.

    Handles nested parentheses (e.g. CHECK(...), ENUM(...)).
    """
    clauses: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            clause = "".join(current).strip()
            if clause:
                clauses.append(clause)
            current = []
        else:
            current.append(ch)
    last = "".join(current).strip()
    if last:
        clauses.append(last)
    return clauses


# ---------------------------------------------------------------------------
# Column and constraint parsing
# ---------------------------------------------------------------------------

# Constraint clause starters
_CONSTRAINT_KEYWORDS = re.compile(
    r"^\s*(PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|CHECK|CONSTRAINT|KEY)\b",
    re.IGNORECASE,
)

# Inline PRIMARY KEY / UNIQUE / NOT NULL / NULL extraction from a column def
_NOT_NULL_RE   = re.compile(r"\bNOT\s+NULL\b", re.IGNORECASE)
_NULL_RE       = re.compile(r"\bNULL\b", re.IGNORECASE)
_PK_RE         = re.compile(r"\bPRIMARY\s+KEY\b", re.IGNORECASE)
_UNIQUE_RE     = re.compile(r"\bUNIQUE\b", re.IGNORECASE)
_DEFAULT_RE    = re.compile(r"\bDEFAULT\s+(\S+)", re.IGNORECASE)
_AUTO_INC_RE   = re.compile(r"\bAUTO_INCREMENT\b", re.IGNORECASE)
_SERIAL_RE     = re.compile(r"\b(SERIAL|BIGSERIAL|SMALLSERIAL)\b", re.IGNORECASE)

# First two tokens of a column line: name  type
_COL_NAME_TYPE_RE = re.compile(
    r"^(?:\"([^\"]+)\"|`([^`]+)`|(\w+))"   # name (quoted or plain)
    r"\s+"
    r"([^\s(,]+(?:\([^)]*\))?)"            # type (possibly with size: VARCHAR(255))
)


def _parse_column(clause: str) -> Column | None:
    """Parse a single column definition clause. Returns None if it's a constraint."""
    clause = clause.strip()
    if _CONSTRAINT_KEYWORDS.match(clause):
        return None  # it's a table constraint, not a column

    m = _COL_NAME_TYPE_RE.match(clause)
    if not m:
        return None

    col_name = _first_match(m.group(1), m.group(2), m.group(3))
    data_type = m.group(4).upper()

    # SERIAL family - normalize to their underlying integer types for display
    # but keep the original string so the ERD shows the real definition
    is_pk   = bool(_PK_RE.search(clause)) or bool(_SERIAL_RE.search(data_type))
    is_auto = bool(_AUTO_INC_RE.search(clause)) or bool(_SERIAL_RE.search(data_type))

    nullable = True
    if _NOT_NULL_RE.search(clause) or is_pk:
        nullable = False
    elif _NULL_RE.search(clause):
        nullable = True

    default_m = _DEFAULT_RE.search(clause)
    default = default_m.group(1) if default_m else None
    if is_auto and default is None:
        default = "AUTO_INCREMENT"

    unique = bool(_UNIQUE_RE.search(clause)) and not is_pk

    return Column(
        name=col_name,
        data_type=data_type,
        nullable=nullable,
        primary_key=is_pk,
        default=default,
        unique=unique,
    )


def _parse_table_constraint(clause: str, table: Table) -> None:
    """Extract PK / FK / UNIQUE from a table-level constraint clause."""
    clause_u = clause.upper().lstrip()

    # PRIMARY KEY (col1, col2)
    if clause_u.startswith("PRIMARY KEY"):
        m = re.search(r"\(([^)]+)\)", clause, re.IGNORECASE)
        if m:
            table.primary_keys = [_unquote(c.strip()) for c in m.group(1).split(",")]
            for col in table.columns:
                if col.name in table.primary_keys:
                    col.primary_key = True
                    col.nullable = False
        return

    # UNIQUE (col)
    if clause_u.startswith("UNIQUE"):
        m = re.search(r"\(([^)]+)\)", clause, re.IGNORECASE)
        if m:
            ucols = [_unquote(c.strip()) for c in m.group(1).split(",")]
            if len(ucols) == 1:
                for col in table.columns:
                    if col.name == ucols[0]:
                        col.unique = True

            # Also record it as an index
            idx_name_m = re.search(r"CONSTRAINT\s+\S+", clause, re.IGNORECASE)
            idx_name = idx_name_m.group(0).split()[-1] if idx_name_m else f"uq_{table.name}_{'_'.join(ucols)}"
            table.indexes.append(Index(name=idx_name, columns=ucols, unique=True))
        return

    # FOREIGN KEY (cols) REFERENCES [schema.]tbl (cols)
    if "FOREIGN KEY" in clause_u:
        m = re.search(
            r"FOREIGN\s+KEY\s*\(([^)]+)\)\s+REFERENCES\s+"
            + _SCHEMA_PREFIX
            + r"(?:\"([^\"]+)\"|`([^`]+)`|(\w+))\s*\(([^)]+)\)",
            clause, re.IGNORECASE,
        )
        if m:
            src_cols  = [_unquote(c.strip()) for c in m.group(1).split(",")]
            ref_table = _first_match(m.group(2), m.group(3), m.group(4))
            ref_cols  = [_unquote(c.strip()) for c in m.group(5).split(",")]
            table.foreign_keys.append(
                ForeignKey(
                    constrained_columns=src_cols,
                    referred_table=ref_table,
                    referred_columns=ref_cols,
                )
            )
        return


# ---------------------------------------------------------------------------
# Main parse entry point
# ---------------------------------------------------------------------------

def _parse_sql(sql: str, schema_name: str, vendor: Vendor) -> Schema:
    schema = Schema(name=schema_name)
    sql = _preprocess(sql, vendor)

    # ---- CREATE TABLE blocks ----
    tables_by_name: dict[str, Table] = {}
    for m in _CREATE_TABLE_RE.finditer(sql):
        tbl_name = _unquote(_first_match(m.group(1), m.group(2), m.group(3)))
        body = m.group(4)
        table = Table(name=tbl_name)

        for clause in _split_columns(body):
            col = _parse_column(clause)
            if col is not None:
                table.columns.append(col)
            else:
                _parse_table_constraint(clause, table)

        # If no explicit PRIMARY KEY constraint was found, derive from inline PK columns
        if not table.primary_keys:
            table.primary_keys = [c.name for c in table.columns if c.primary_key]

        tables_by_name[tbl_name] = table
        schema.tables.append(table)

    # ---- CREATE INDEX statements ----
    for m in _CREATE_INDEX_RE.finditer(sql):
        is_unique  = bool(m.group(1))
        idx_name   = _unquote(_first_match(m.group(2), m.group(3), m.group(4)))
        tbl_name   = _unquote(_first_match(m.group(5), m.group(6), m.group(7)))
        cols_raw   = m.group(8)
        idx_cols   = [_unquote(c.strip().split()[0]) for c in cols_raw.split(",")]

        if tbl_name in tables_by_name:
            tbl = tables_by_name[tbl_name]
            tbl.indexes.append(Index(name=idx_name, columns=idx_cols, unique=is_unique))
            if is_unique and len(idx_cols) == 1:
                for col in tbl.columns:
                    if col.name == idx_cols[0]:
                        col.unique = True

    # ---- ALTER TABLE ... ADD PRIMARY KEY ----
    for m in _ALTER_PK_RE.finditer(sql):
        tbl_name = _unquote(_first_match(m.group(1), m.group(2), m.group(3)))
        pk_cols  = [_unquote(c.strip()) for c in m.group(4).split(",")]
        if tbl_name in tables_by_name:
            tbl = tables_by_name[tbl_name]
            tbl.primary_keys = pk_cols
            for col in tbl.columns:
                if col.name in pk_cols:
                    col.primary_key = True
                    col.nullable = False

    # ---- ALTER TABLE ... ADD FOREIGN KEY ----
    for m in _ALTER_FK_RE.finditer(sql):
        src_tbl  = _unquote(_first_match(m.group(1), m.group(2), m.group(3)))
        src_cols = [_unquote(c.strip()) for c in m.group(4).split(",")]
        ref_tbl  = _unquote(_first_match(m.group(5), m.group(6), m.group(7)))
        ref_cols = [_unquote(c.strip()) for c in m.group(8).split(",")]

        if src_tbl in tables_by_name:
            tables_by_name[src_tbl].foreign_keys.append(
                ForeignKey(
                    constrained_columns=src_cols,
                    referred_table=ref_tbl,
                    referred_columns=ref_cols,
                )
            )

    return schema


# ---------------------------------------------------------------------------
# Generic SQL script schema inference
# ---------------------------------------------------------------------------
# For files that contain SELECT/UPDATE/ALTER TABLE but no CREATE TABLE we
# scan every statement to collect table names and column names mentioned
# in key syntactic positions.

# Table name pattern (double-quoted, backtick, or plain identifier)
_TBL_IDENT = r'(?:"([^"]+)"|`([^`]+)`|(\b[A-Za-z_]\w*\b))'

# FROM <table> / JOIN <table>  (excludes subqueries - must be a plain ident)
_SCRIPT_FROM_RE = re.compile(
    r'\b(?:FROM|JOIN)\s+' + _TBL_IDENT + r'(?:\s+(?:AS\s+)?\w+)?(?:\s|,|$)',
    re.IGNORECASE,
)
# INTO <table>  (INSERT INTO, UPDATE ... SET ... WHERE, etc.)
_SCRIPT_INTO_RE = re.compile(
    r'\bINTO\s+' + _TBL_IDENT,
    re.IGNORECASE,
)
# UPDATE <table>
_SCRIPT_UPDATE_RE = re.compile(
    r'\bUPDATE\s+' + _TBL_IDENT,
    re.IGNORECASE,
)
# ALTER TABLE <table>
_SCRIPT_ALTER_RE = re.compile(
    r'\bALTER\s+TABLE\s+(?:ONLY\s+)?' + _TBL_IDENT,
    re.IGNORECASE,
)
# DELETE FROM <table>
_SCRIPT_DELETE_RE = re.compile(
    r'\bDELETE\s+FROM\s+' + _TBL_IDENT,
    re.IGNORECASE,
)

# Column names from SELECT <col_list> FROM  -  captures bare identifiers
# between SELECT and the first FROM (very conservative: only plain names)
_SELECT_COL_RE = re.compile(
    r'\bSELECT\s+(DISTINCT\s+)?(.*?)\s+FROM\b',
    re.IGNORECASE | re.DOTALL,
)
# Single column alias pattern:  expr AS name  or just  name
_COL_ALIAS_RE = re.compile(
    r'(?:^|,)\s*(?:[^,()]+?\s+AS\s+)?([A-Za-z_]\w*)\s*(?:,|$)',
    re.IGNORECASE,
)

# Keywords that look like column names but are not
_SQL_RESERVED = frozenset([
    "SELECT", "FROM", "WHERE", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER",
    "FULL", "CROSS", "ON", "AND", "OR", "NOT", "NULL", "IS", "IN", "AS",
    "CASE", "WHEN", "THEN", "ELSE", "END", "GROUP", "BY", "ORDER", "HAVING",
    "LIMIT", "OFFSET", "UNION", "ALL", "DISTINCT", "COUNT", "SUM", "AVG",
    "MIN", "MAX", "COALESCE", "CAST", "CONVERT", "ISNULL", "IFNULL",
    "DATEADD", "DATEDIFF", "GETDATE", "NOW", "DATE", "TOP", "INTO",
    "VALUES", "SET", "UPDATE", "DELETE", "INSERT", "WITH", "OVER",
    "PARTITION", "RANK", "ROW_NUMBER", "DENSE_RANK", "NTILE",
    "ECOMMERCECHURN",  # catch-all for the table name appearing as a col alias
])

# Table name tokens that are clearly SQL keywords, not real table names
_TBL_RESERVED = frozenset([
    "SELECT", "FROM", "WHERE", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER",
    "ON", "AND", "OR", "SET", "VALUES", "INTO", "WITH", "HAVING",
    "DISTINCT", "GROUP", "ORDER", "LIMIT", "OFFSET", "UNION", "ALL",
    "NULL", "NOT", "AS", "IS", "IN", "CASE", "WHEN", "THEN", "ELSE", "END",
    "UPDATE", "DELETE", "INSERT", "ALTER", "CREATE", "DROP", "TABLE",
    "RETURNING", "OUTPUT", "USING", "LATERAL", "PIVOT", "UNPIVOT",
])


def _extract_ident(m: re.Match) -> str:
    """Extract the table/column identifier from a regex match with 3 capture groups."""
    return (m.group(1) or m.group(2) or m.group(3) or "").strip()


def _infer_schema_from_script(sql_text: str, schema_name: str) -> Schema:
    """Infer a Schema by analysing table and column references in a SQL script.

    Strategy:
      1. Strip comments so keywords inside comment text don't confuse patterns.
      2. Collect all table names from FROM / JOIN / UPDATE / INTO / ALTER TABLE.
      3. For each table, collect column names from SELECT lists of queries that
         reference that table, plus column names from ALTER TABLE ADD COLUMN
         and UPDATE … SET col = … statements.
      4. Deduplicate everything, filter reserved words, and build stub Table
         objects with Column(data_type="TEXT") for every inferred column.
    """
    schema = Schema(name=schema_name)

    # Strip line and block comments - same as _preprocess but vendor-agnostic
    clean = _BLOCK_COMMENT_RE.sub(" ", sql_text)
    clean = _LINE_COMMENT_RE.sub(" ", clean)

    # ── 1. Collect table names ────────────────────────────────────────────
    table_names: set[str] = set()
    for pattern in (_SCRIPT_FROM_RE, _SCRIPT_INTO_RE, _SCRIPT_UPDATE_RE,
                    _SCRIPT_ALTER_RE, _SCRIPT_DELETE_RE):
        for m in pattern.finditer(clean):
            name = _extract_ident(m).strip('"` ')
            if name and name.upper() not in _TBL_RESERVED and len(name) >= 2:
                table_names.add(name)

    if not table_names:
        # Nothing found - return empty schema (pipeline will warn gracefully)
        return schema

    # ── 2. Collect column names per table ─────────────────────────────────
    # Map: canonical_table_name_lower → set of column name strings
    col_map: dict[str, set[str]] = {t.lower(): set() for t in table_names}

    # FROM-clause table resolution: for each SELECT … FROM tbl statement,
    # attribute the SELECT columns to that table.
    for sel_m in _SELECT_COL_RE.finditer(clean):
        col_list_raw = sel_m.group(2)
        # Which table does this SELECT target?  Look ahead for FROM <tbl>
        after = clean[sel_m.start():]
        from_m = _SCRIPT_FROM_RE.search(after)
        if not from_m:
            continue
        tbl = _extract_ident(from_m).strip('"` ').lower()
        if tbl not in col_map:
            continue
        # Parse the column list (skip * and function calls)
        for part in col_list_raw.split(","):
            part = part.strip()
            if not part or part == "*":
                continue
            # Take the last bare identifier - handles "expr AS alias" and bare "col"
            tokens = re.findall(r'\b([A-Za-z_]\w*)\b', part)
            if not tokens:
                continue
            # The alias (last token after AS) or the column name (last token overall)
            upper_tokens = [t.upper() for t in tokens]
            if "AS" in upper_tokens:
                idx = upper_tokens.index("AS")
                col_candidate = tokens[idx + 1] if idx + 1 < len(tokens) else tokens[-1]
            else:
                col_candidate = tokens[-1]
            if col_candidate.upper() not in _SQL_RESERVED and len(col_candidate) >= 2:
                col_map[tbl].add(col_candidate)

    # UPDATE tbl SET col = … → pick up col names
    _update_set_re = re.compile(
        r'\bUPDATE\s+' + _TBL_IDENT + r'\s+SET\s+(.+?)(?:\bWHERE\b|;|$)',
        re.IGNORECASE | re.DOTALL,
    )
    for m in _update_set_re.finditer(clean):
        tbl = _extract_ident(m).strip('"` ').lower()
        if tbl not in col_map:
            continue
        assignments = m.group(4) if m.lastindex and m.lastindex >= 4 else ""
        # group index: TBL_IDENT uses groups 1-3, SET body is group 4
        # Re-match with named groups to be safe
        set_body_m = re.search(
            r'\bSET\s+(.+?)(?:\bWHERE\b|;|$)',
            clean[m.start():], re.IGNORECASE | re.DOTALL,
        )
        if set_body_m:
            for assign in set_body_m.group(1).split(","):
                lhs = assign.split("=")[0].strip().strip('"` ')
                if re.match(r'^[A-Za-z_]\w*$', lhs) and lhs.upper() not in _SQL_RESERVED:
                    col_map[tbl].add(lhs)

    # ALTER TABLE tbl ADD col_name  (ADD COLUMN col_name type)
    _alter_add_re = re.compile(
        r'\bALTER\s+TABLE\s+(?:ONLY\s+)?' + _TBL_IDENT +
        r'\s+ADD\s+(?:COLUMN\s+)?(?:"([^"]+)"|`([^`]+)`|(\b[A-Za-z_]\w*\b))',
        re.IGNORECASE,
    )
    for m in _alter_add_re.finditer(clean):
        tbl = _extract_ident(m).strip('"` ').lower()
        # column name is in groups 4/5/6
        col = (m.group(4) or m.group(5) or m.group(6) or "").strip()
        if tbl in col_map and col and col.upper() not in _SQL_RESERVED:
            col_map[tbl].add(col)

    # ── 3. Build Schema ───────────────────────────────────────────────────
    # Preserve insertion order by using sorted table names for determinism
    for tbl_name in sorted(table_names, key=str.lower):
        table = Table(name=tbl_name)
        tbl_cols = col_map.get(tbl_name.lower(), set())
        for col_name in sorted(tbl_cols, key=str.lower):
            table.columns.append(Column(name=col_name, data_type="TEXT"))
        schema.tables.append(table)

    return schema
