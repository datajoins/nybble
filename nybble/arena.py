"""
arena.py - Cross-Source SQL Arena

Interactive SQL REPL and programmatic query interface backed by the shared
DuckDB session.  All relations registered via resolver.load() are available
as virtual tables.

Public API
----------
  query(sql, limit=None)          → QueryResult
  explain(sql, analyze=False)     → str
  save_query(name, sql)
  run_query(name)                 → QueryResult
  list_saved_queries()            → list[dict]
  delete_saved_query(name)
  search_history(term, limit)     → list[dict]
  repl()                          - launch interactive REPL
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

import polars as pl
import pyarrow as pa
from pygments import highlight
from pygments.formatters import TerminalTrueColorFormatter
from pygments.lexers import SqlLexer
from rich.console import Console
from rich.table import Table
from rich import box

from nybble.resolver import session, _SESSION_LOCK

console = Console()

# Base directory for all Nybble persistent state.
# Override with NYBBLE_DATA_DIR to fix cwd-relative paths in embedded contexts
# (Airflow workers, Django management commands, containerised deployments).
_DATA_DIR = Path(os.environ.get("NYBBLE_DATA_DIR", ".nybble"))

# Persistent query store
_QUERIES_FILE = _DATA_DIR / "queries.json"
# Persistent history (append-only JSONL, capped at _HISTORY_MAX_LINES).
# Override via the nybble.yaml config key ``history.max_lines`` (must be a
# positive integer).  Call _reload_history_cap() after changing nybble.yaml to
# pick up the new value without restarting.
_HISTORY_FILE = _DATA_DIR / "history.jsonl"
_HISTORY_MAX_LINES = 10_000


def _reload_history_cap() -> int:
    """Read ``history.max_lines`` from nybble.yaml and update _HISTORY_MAX_LINES.

    Returns the effective cap (validated positive int).  Falls back to the
    existing module-level value when the key is absent or the file is unreadable.
    Raises ``ValueError`` if the configured value is not a positive integer.
    """
    global _HISTORY_MAX_LINES
    try:
        from nybble.config import find_config
        cfg_path = find_config()
        if cfg_path is None:
            return _HISTORY_MAX_LINES
        import yaml
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        val = raw.get("history", {}).get("max_lines")
        if val is None:
            return _HISTORY_MAX_LINES
        try:
            val = int(val)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"history.max_lines in nybble.yaml must be a positive integer, got {val!r}"
            ) from exc
        if val <= 0:
            raise ValueError(
                f"history.max_lines in nybble.yaml must be a positive integer, got {val!r}"
            )
        _HISTORY_MAX_LINES = val
    except ValueError:
        raise
    except Exception:
        pass  # yaml absent/unreadable - keep current value
    return _HISTORY_MAX_LINES


# ── QueryResult ────────────────────────────────────────────────────────────────

class QueryResult:
    """Wraps DuckDB query results with multi-format conversion methods.

    Attributes
    ----------
    columns    : list[str]
    row_count  : int
    elapsed_ms : float
    plan_ms    : float   - time until execute() returned (planning phase)
    exec_ms    : float   - time to fetch rows after execute()
    sql        : str
    truncated  : bool
    """

    def __init__(
        self,
        columns: list[str],
        arrow_table: pa.Table,
        elapsed_ms: float,
        sql: str,
        truncated: bool = False,
        plan_ms: float = 0.0,
        exec_ms: float = 0.0,
    ) -> None:
        self.columns = columns
        self._arrow = arrow_table
        self.row_count = len(arrow_table)
        self.elapsed_ms = elapsed_ms
        self.plan_ms = plan_ms
        self.exec_ms = exec_ms
        self.sql = sql
        self.truncated = truncated

    def to_polars(self) -> pl.DataFrame:
        """Return results as a polars DataFrame."""
        return pl.from_arrow(self._arrow)

    def to_pandas(self):
        """Return results as a pandas DataFrame."""
        return self._arrow.to_pandas()

    def to_arrow(self) -> pa.Table:
        """Return results as a PyArrow Table."""
        return self._arrow

    def to_dict(self) -> list[dict]:
        """Return results as a list of row dicts."""
        df = pl.from_arrow(self._arrow)
        return df.to_dicts()

    def rows(self) -> list[list]:
        """Return results as a list of row lists."""
        df = pl.from_arrow(self._arrow)
        return [list(r) for r in df.iter_rows()]

    def _repr_table(self, max_rows: int = 40) -> Table:
        tbl = Table(box=box.MINIMAL_DOUBLE_HEAD, show_header=True, header_style="bold cyan")
        for col in self.columns:
            tbl.add_column(col, overflow="fold", max_width=40)
        rows = self.rows()
        for r in rows[:max_rows]:
            tbl.add_row(*[str(v) if v is not None else "[dim]NULL[/dim]" for v in r])
        if len(rows) > max_rows:
            tbl.add_row(*["[dim]…[/dim]"] * len(self.columns))
        return tbl

    def __repr__(self) -> str:
        trunc = " (truncated)" if self.truncated else ""
        return (
            f"QueryResult({self.row_count} rows × {len(self.columns)} cols, "
            f"{self.elapsed_ms:.1f} ms{trunc})"
        )


# ── SQL utilities ──────────────────────────────────────────────────────────────

def _fingerprint_sql(sql: str) -> str:
    """Return a short SHA-256 fingerprint of normalised SQL for history dedup."""
    normalised = re.sub(r"\s+", " ", sql).lower().strip()
    return hashlib.sha256(normalised.encode()).hexdigest()[:12]


_STMT_SPLIT_RE = re.compile(
    r"""
    (?:
        '(?:[^'\\]|\\.)*'          # single-quoted strings
      | "(?:[^"\\]|\\.)*"          # double-quoted identifiers
      | \$\$.*?\$\$                # dollar-quoted PL/pgSQL blocks  (non-greedy)
      | --[^\n]*                   # line comments
      | /\*.*?\*/                  # block comments
      | ;                          # actual statement separator
    )
    """,
    re.VERBOSE | re.DOTALL,
)


def _split_statements(sql: str) -> list[str]:
    """Split SQL on `;` while respecting quoted strings and PL/pgSQL blocks.

    Returns a list of non-empty statement strings (without trailing semicolons).
    """
    statements: list[str] = []
    current_start = 0

    for m in _STMT_SPLIT_RE.finditer(sql):
        if m.group() == ";":
            stmt = sql[current_start : m.start()].strip()
            if stmt:
                statements.append(stmt)
            current_start = m.end()

    # Remainder after last `;` (or entire string if no `;` was found)
    tail = sql[current_start:].strip()
    if tail:
        statements.append(tail)

    return statements


# ── Core query execution ──────────────────────────────────────────────────────

def query(sql: str, limit: Optional[int] = None) -> QueryResult:
    """Execute SQL against the shared DuckDB session.

    Multi-statement SQL (separated by ``;``) is split and executed in order;
    the result from the last statement is returned.

    Parameters
    ----------
    sql:
        Any DuckDB-compatible SQL statement(s).
    limit:
        Optional maximum row count returned.  When None, all rows are returned.

    Returns
    -------
    QueryResult
    """
    stmts = _split_statements(sql)
    if not stmts:
        stmts = [sql.strip().rstrip(";")]

    result: Optional[QueryResult] = None
    for stmt in stmts:
        result = _execute_single(stmt, limit=limit if stmt is stmts[-1] else None)

    assert result is not None
    _append_history(result)
    return result


def _execute_single(sql: str, limit: Optional[int] = None) -> QueryResult:
    """Execute a single SQL statement and return a QueryResult."""
    con = session()
    sql = sql.strip().rstrip(";")

    # Wrap in LIMIT subquery when requested and not already limited
    first_token = sql.split()[0].upper() if sql.split() else ""
    truncated = False
    exec_sql = sql
    if limit is not None and first_token in ("SELECT", "WITH"):
        if "LIMIT" not in sql.upper():
            exec_sql = f"SELECT * FROM ({sql}) _q LIMIT {limit + 1}"

    t0 = time.perf_counter()
    with _SESSION_LOCK:
        result_cursor = con.execute(exec_sql)
        t1 = time.perf_counter()
        arrow = result_cursor.arrow()
    t2 = time.perf_counter()

    plan_ms = (t1 - t0) * 1000
    exec_ms = (t2 - t1) * 1000
    elapsed_ms = (t2 - t0) * 1000

    if limit is not None and len(arrow) > limit:
        arrow = arrow.slice(0, limit)
        truncated = True

    columns = arrow.schema.names
    return QueryResult(
        columns=columns,
        arrow_table=arrow,
        elapsed_ms=elapsed_ms,
        sql=sql,
        truncated=truncated,
        plan_ms=plan_ms,
        exec_ms=exec_ms,
    )


# ── EXPLAIN ───────────────────────────────────────────────────────────────────

def explain(sql: str, analyze: bool = False) -> str:
    """Return DuckDB's query plan for *sql*.

    Parameters
    ----------
    sql:
        A SELECT or similar query to explain.
    analyze:
        When True, runs ``EXPLAIN ANALYZE`` (executes the query and reports
        actual row counts and timings).

    Returns
    -------
    str
        The query plan as a formatted string.
    """
    con = session()
    keyword = "EXPLAIN ANALYZE" if analyze else "EXPLAIN"
    plan_sql = f"{keyword} {sql.strip().rstrip(';')}"
    with _SESSION_LOCK:
        rows = con.execute(plan_sql).fetchall()
    # DuckDB returns plan lines as (key, value) or just (value,) rows
    lines = []
    for row in rows:
        lines.append("  ".join(str(c) for c in row if c is not None))
    return "\n".join(lines)


# ── Query history ─────────────────────────────────────────────────────────────

def _append_history(result: QueryResult) -> None:
    """Append a history entry for *result* to the JSONL history file."""
    fp = _fingerprint_sql(result.sql)
    entry = {
        "sql": result.sql,
        "ts": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": round(result.elapsed_ms, 2),
        "rows": result.row_count,
        "cols": len(result.columns),
        "fp": fp,
    }
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(_HISTORY_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _rotate_history()
    except Exception:
        pass  # Never crash on history write failure


def _rotate_history() -> None:
    """Trim the history file to at most _HISTORY_MAX_LINES lines."""
    try:
        lines = _HISTORY_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) > _HISTORY_MAX_LINES:
            _HISTORY_FILE.write_text(
                "".join(lines[-_HISTORY_MAX_LINES:]),
                encoding="utf-8",
            )
    except Exception:
        pass


def _load_history() -> list[dict]:
    """Return all history entries as a list of dicts (newest last)."""
    if not _HISTORY_FILE.exists():
        return []
    entries: list[dict] = []
    for line in _HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    return entries


def search_history(term: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Return recent history entries, optionally filtered by *term*.

    Parameters
    ----------
    term:
        Case-insensitive substring to filter SQL text.  None returns all.
    limit:
        Maximum number of entries to return (most recent first).

    Returns
    -------
    list[dict]
        Each dict: {sql, ts, elapsed_ms, rows, cols, fp}
    """
    entries = _load_history()
    if term:
        term_lower = term.lower()
        entries = [e for e in entries if term_lower in e.get("sql", "").lower()]
    return list(reversed(entries))[:limit]


# ── Saved queries ─────────────────────────────────────────────────────────────

def _load_saved() -> dict[str, str]:
    if _QUERIES_FILE.exists():
        try:
            data = json.loads(_QUERIES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_saved(queries: dict[str, str]) -> None:
    _QUERIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _QUERIES_FILE.write_text(
        json.dumps(queries, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def save_query(name: str, sql: str) -> None:
    """Persist a named SQL query to .nybble/queries.json."""
    queries = _load_saved()
    queries[name] = sql.strip()
    _save_saved(queries)


def run_query(name: str, limit: Optional[int] = None) -> QueryResult:
    """Execute a previously saved query by name."""
    queries = _load_saved()
    if name not in queries:
        raise KeyError(f"No saved query named {name!r}. Use save_query() to create one.")
    return query(queries[name], limit=limit)


def list_saved_queries() -> list[dict]:
    """Return a list of {name, sql} dicts for all saved queries."""
    return [{"name": k, "sql": v} for k, v in _load_saved().items()]


def delete_saved_query(name: str) -> None:
    """Remove a saved query by name."""
    queries = _load_saved()
    if name not in queries:
        raise KeyError(f"No saved query named {name!r}.")
    del queries[name]
    _save_saved(queries)


# ── Interactive REPL ──────────────────────────────────────────────────────────

def _highlight_sql(sql: str) -> str:
    return highlight(
        sql,
        SqlLexer(),
        TerminalTrueColorFormatter(style="monokai"),
    )


def repl() -> None:
    """Launch the interactive SQL REPL.

    Features:
    - Multi-line input terminated by ;
    - Multi-statement execution (each ; executes the preceding statement)
    - Syntax-highlighted SQL display
    - Query time and row count in result header
    - Column type display
    - Tab completion is provided by readline where available
    - .history [term]  - show/search query history
    - .explain <sql>   - show query plan
    """
    try:
        import readline
        readline.parse_and_bind("tab: complete")
    except ImportError:
        pass

    from nybble.resolver import list_relations
    from rich.rule import Rule

    console.print(
        "[bold cyan]nybble SQL Arena[/bold cyan]  "
        "[dim]DuckDB-powered cross-source SQL REPL[/dim]"
    )
    console.print(
        "[dim]Terminate statements with ; · Type .exit to quit · .tables to list sources · "
        ".history [term] to search history · .explain <sql> for query plan[/dim]\n"
    )

    buf: list[str] = []
    prompt_main = "[bold cyan]sql>[/bold cyan] "
    prompt_cont = "[dim]  ->[/dim] "

    while True:
        try:
            prompt = prompt_main if not buf else prompt_cont
            console.print(prompt, end="")
            line = input()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        # Meta-commands (only recognised on their own line)
        stripped = line.strip()
        if stripped.lower() in (".exit", ".quit", "\\q"):
            console.print("[dim]Goodbye.[/dim]")
            break
        if stripped.lower() in (".tables", "\\dt"):
            tables = list_relations()
            console.print("[cyan]Registered sources:[/cyan]")
            for t in tables:
                console.print(f"  • {t}")
            continue
        if stripped.lower().startswith(".history"):
            parts = stripped.split(None, 1)
            term = parts[1] if len(parts) > 1 else None
            entries = search_history(term=term, limit=20)
            if not entries:
                console.print("[dim](no history)[/dim]")
            else:
                for i, e in enumerate(reversed(entries), 1):
                    ts = e.get("ts", "")[:19].replace("T", " ")
                    console.print(
                        f"[dim]{i:3d}[/dim]  [cyan]{ts}[/cyan]  "
                        f"[dim]{e.get('rows',0):,} rows · {e.get('elapsed_ms',0):.0f} ms[/dim]"
                        f"\n      {e.get('sql','')[:100]}"
                    )
            continue
        if stripped.lower().startswith(".explain"):
            rest = stripped[8:].strip()
            if not rest:
                console.print("[red]Usage: .explain <sql>[/red]")
                continue
            try:
                plan = explain(rest)
                console.print(plan)
            except Exception as exc:
                console.print(f"[red]Error:[/red] {exc}")
            continue
        if stripped.lower().startswith(".save "):
            parts = stripped.split(None, 2)
            if len(parts) == 3:
                save_query(parts[1], parts[2])
                console.print(f"[green]Saved query {parts[1]!r}[/green]")
            continue
        if stripped.lower().startswith(".run "):
            name = stripped[5:].strip()
            try:
                result = run_query(name)
                _print_result(result)
            except KeyError as exc:
                console.print(f"[red]{exc}[/red]")
            continue

        buf.append(line)
        full = " ".join(buf)

        if ";" in full:
            # Execute all complete statements in the buffer
            stmts = _split_statements(full)
            buf = []
            for stmt in stmts:
                if not stmt:
                    continue
                console.print(_highlight_sql(stmt + ";"))
                try:
                    result = _execute_single(stmt)
                    _append_history(result)
                    _print_result(result)
                except Exception as exc:
                    console.print(f"[red]Error:[/red] {exc}")


def _print_result(result: QueryResult) -> None:
    """Pretty-print a QueryResult to the console."""
    from rich.rule import Rule

    timing = f"{result.elapsed_ms:.1f} ms"
    if result.plan_ms > 0 and result.exec_ms > 0:
        timing = (
            f"plan: {result.plan_ms:.1f} ms  "
            f"exec: {result.exec_ms:.1f} ms  "
            f"total: {result.elapsed_ms:.1f} ms"
        )

    console.print(
        Rule(
            f"[dim]{result.row_count:,} rows · "
            f"{len(result.columns)} cols · "
            f"{timing}[/dim]"
        )
    )
    # Column type header
    schema_str = "  ".join(
        f"[dim]{c}[/dim]" for c in result.columns
    )
    console.print(schema_str)
    console.print(result._repr_table())
    if result.truncated:
        console.print(f"[dim](results truncated)[/dim]")
    console.print()
