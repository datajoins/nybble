"""
browse.py - Rich-based Interactive Table Browser

Renders any Relation as a full-terminal paginated data grid with:
  • Keyboard navigation (arrows, PgUp/PgDn, g/G, q)
  • Live filter bar triggered by /
  • Column sorting (s key or click header)
  • Status bar with source info, row count, page, active filters
  • Column profiling panel on i key

Entry points
------------
  browse(source_or_relation, page_size=50)
"""
from __future__ import annotations

import re
from typing import Optional, Union

import polars as pl
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from nybble.resolver import Relation, load, session

console = Console()

# Maximum display width per column (chars) before truncation
_MAX_COL_WIDTH = 30
_TRUNCATE_MARKER = "…"


# ── Data helpers ──────────────────────────────────────────────────────────────

def _fetch_page(
    relation_name: str,
    offset: int,
    page_size: int,
    sort_col: Optional[str],
    sort_asc: bool,
    filter_text: str,
    filter_regex: bool,
    schema: list[dict],
) -> tuple[list[list], int]:
    """Fetch one page of data from DuckDB, applying sort and filter."""
    from nybble.resolver import _SESSION_LOCK

    con = session()

    # Build WHERE clause for filter
    where_parts: list[str] = []
    if filter_text:
        col_casts = " OR ".join(
            f'CAST("{c["name"]}" AS VARCHAR) ILIKE \'%{_escape_like(filter_text)}%\''
            for c in schema
        )
        where_parts.append(f"({col_casts})")

    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    # ORDER BY
    order_sql = ""
    if sort_col:
        direction = "ASC" if sort_asc else "DESC"
        order_sql = f'ORDER BY "{sort_col}" {direction} NULLS LAST'

    count_sql = f'SELECT COUNT(*) FROM "{relation_name}" {where_sql}'
    data_sql = (
        f'SELECT * FROM "{relation_name}" {where_sql} {order_sql} '
        f"LIMIT {page_size} OFFSET {offset}"
    )

    with _SESSION_LOCK:
        total = con.execute(count_sql).fetchone()[0]
        rows = con.execute(data_sql).fetchall()

    return [list(r) for r in rows], total


def _escape_like(text: str) -> str:
    return text.replace("'", "''").replace("%", "\\%").replace("_", "\\_")


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return value[: width - 1] + _TRUNCATE_MARKER


# ── Rendering ─────────────────────────────────────────────────────────────────

def _render_table(
    rows: list[list],
    schema: list[dict],
    sort_col: Optional[str],
    sort_asc: bool,
    highlight_rules: Optional[list] = None,
) -> Table:
    from nybble.highlight_store import apply_rules
    tbl = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        expand=True,
        highlight=True,
    )
    for col_info in schema:
        col_name = col_info["name"]
        header = col_name
        if sort_col == col_name:
            header += " ▲" if sort_asc else " ▼"
        tbl.add_column(header, no_wrap=False, max_width=_MAX_COL_WIDTH, overflow="fold")

    rules = highlight_rules or []
    for i, row in enumerate(rows):
        row_style = apply_rules(rules, row, schema)
        if row_style is None:
            row_style = "" if i % 2 == 0 else "dim"
        tbl.add_row(
            *[_truncate(str(v) if v is not None else "NULL", _MAX_COL_WIDTH) for v in row],
            style=row_style,
        )

    return tbl


def _render_status(
    name: str,
    source_type: str,
    total: int,
    page: int,
    total_pages: int,
    filter_text: str,
    sort_col: Optional[str],
    highlight_count: int = 0,
    multi_table: bool = False,
) -> Panel:
    parts = [
        f"[bold]{name}[/bold]",
        f"[dim]{source_type}[/dim]",
        f"rows: [cyan]{total:,}[/cyan]",
        f"page: [cyan]{page}/{total_pages}[/cyan]",
    ]
    if filter_text:
        parts.append(f"filter: [yellow]{filter_text!r}[/yellow]")
    if sort_col:
        parts.append(f"sort: [green]{sort_col}[/green]")
    if highlight_count:
        parts.append(f"[bold yellow]hl: {highlight_count} rule{'s' if highlight_count != 1 else ''}[/bold yellow]")
    status_text = "  │  ".join(parts)
    back_hint = "  [bold cyan]b back[/bold cyan]" if multi_table else ""
    hints = f"  [dim]↑↓ navigate  /  filter  s sort  i profile  h highlight  g/G first/last  q quit[/dim]{back_hint}"
    return Panel(status_text + hints, style="bold", padding=(0, 1))


# ── Main browse function ──────────────────────────────────────────────────────

# Sentinel returned by the REPL when the user wants to return to the picker
_BACK = object()


def _pick_table(relation: "Relation") -> "Relation":
    """Interactive numbered-menu picker for multi-table sources.

    When a SQL dump, SQLite DB, or multi-table database is loaded, the
    resolver registers individual tables as ``<source>__<table>`` views and
    returns a parent Relation whose ``.tables`` attribute lists the short
    table names.  This function presents a numbered list and returns the
    chosen child Relation from ``_REGISTERED``.
    """
    from nybble.resolver import _REGISTERED

    tables: list[str] = relation.tables  # short names
    source_prefix = relation.name        # e.g. "umami_backup_20261018_020002_sql"

    console.print(
        f"\n[bold cyan]{source_prefix}[/bold cyan] contains "
        f"[bold]{len(tables)}[/bold] tables. Pick one to browse:\n"
    )
    for i, tname in enumerate(tables, 1):
        console.print(f"  [bold cyan]{i:3}[/bold cyan]  {tname}")

    console.print("\n[dim]Enter a number (or q to quit):[/dim] ", end="")
    while True:
        try:
            choice = input().strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(0)
        if choice.lower() in ("q", "quit", "exit"):
            raise SystemExit(0)
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(tables):
                short = tables[idx]
                view_name = f"{source_prefix}__{short}"
                if view_name in _REGISTERED:
                    return _REGISTERED[view_name]
                # Fallback: view might equal short when name == table
                if short in _REGISTERED:
                    return _REGISTERED[short]
                raise ValueError(f"View {view_name!r} not found in registry.")
        except ValueError:
            pass
        console.print(f"[yellow]Please enter a number between 1 and {len(tables)}:[/yellow] ", end="")


def _browse_one(relation: "Relation", page_size: int, multi_table: bool) -> object:
    """Browse a single relation.

    Returns _BACK when the user signals "go back to picker" (b or q while in
    multi-table mode), or None to exit entirely.
    """
    import sys

    schema = relation.schema()
    if not schema:
        console.print("[red]No columns found in relation.[/red]")
        return None

    page = 1
    sort_col: Optional[str] = None
    sort_asc = True
    filter_text = ""
    in_filter = False
    filter_buf = ""

    from nybble.highlight_store import load_rules as _load_hl_rules
    highlight_rules: list = _load_hl_rules(relation.name)

    def get_total_pages(total: int) -> int:
        return max(1, (total + page_size - 1) // page_size)

    # Prompt shown on Windows (non-raw) fallback
    _back_hint = "  b back" if multi_table else ""
    _win_prompt = f"\n[q quit{_back_hint} / n next / p prev / / filter / i profile] > "

    def _render(rows: list[list], total: int) -> None:
        total_pages = get_total_pages(total)
        tbl = _render_table(rows, schema, sort_col, sort_asc, highlight_rules)
        status = _render_status(
            relation.name, relation.source_type, total,
            page, total_pages, filter_text, sort_col,
            highlight_count=len(highlight_rules),
            multi_table=multi_table,
        )
        console.clear()
        console.print(status)
        console.print(tbl)
        if in_filter:
            console.print(f"\n[bold yellow]Filter:[/bold yellow] {filter_buf}_")

    def _load() -> tuple[list[list], int]:
        offset = (page - 1) * page_size
        return _fetch_page(
            relation.name, offset, page_size,
            sort_col, sort_asc, filter_text, False, schema,
        )

    use_raw = sys.platform != "win32"
    if use_raw:
        import tty, termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

    result = None
    try:
        if use_raw:
            tty.setraw(fd)

        while True:
            rows, total = _load()
            total_pages = get_total_pages(total)
            _render(rows, total)

            if use_raw:
                ch = sys.stdin.read(1)
            else:
                ch = input(_win_prompt)[:1]

            if in_filter:
                if ch in ("\r", "\n"):
                    filter_text = filter_buf
                    filter_buf = ""
                    in_filter = False
                    page = 1
                elif ch in ("\x7f", "\x08"):
                    filter_buf = filter_buf[:-1]
                elif ch == "\x1b":
                    filter_buf = ""
                    in_filter = False
                else:
                    filter_buf += ch
                continue

            if ch == "b" and multi_table:
                # Explicit "back to picker" shortcut
                result = _BACK
                break
            elif ch == "q":
                # In multi-table mode q also means "back to picker" - one level
                # up per press, matching the mental model of navigating a tree.
                # Press q at the picker itself to exit entirely.
                result = _BACK if multi_table else None
                break
            elif ch == "i":
                _show_profile(relation.name, use_raw)
            elif ch == "h":
                _prompt_highlight_rule(relation.name, schema, highlight_rules, use_raw)
            elif ch == "/":
                in_filter = True
                filter_buf = ""
            elif ch == "s":
                col_names = [c["name"] for c in schema]
                if sort_col is None:
                    sort_col = col_names[0]
                    sort_asc = True
                elif sort_asc:
                    sort_asc = False
                else:
                    cur_idx = col_names.index(sort_col) if sort_col in col_names else -1
                    next_idx = cur_idx + 1
                    if next_idx >= len(col_names):
                        sort_col = None
                        sort_asc = True
                    else:
                        sort_col = col_names[next_idx]
                        sort_asc = True
                page = 1
            elif ch == "g":
                page = 1
            elif ch == "G":
                page = total_pages
            elif ch == "n" or ch == "\x1b":
                if use_raw and ch == "\x1b":
                    rest = sys.stdin.read(2)
                    if rest == "[B":
                        page = min(page + 1, total_pages)
                    elif rest == "[A":
                        page = max(page - 1, 1)
                    elif rest == "[6":
                        sys.stdin.read(1)
                        page = min(page + 1, total_pages)
                    elif rest == "[5":
                        sys.stdin.read(1)
                        page = max(page - 1, 1)
                else:
                    page = min(page + 1, total_pages)
            elif ch == "p":
                page = max(page - 1, 1)

    finally:
        if use_raw:
            import termios
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        console.clear()
        if result is not _BACK:
            console.print(f"[dim]Browse closed - {relation.name}[/dim]")

    return result


def browse(source: Union[str, Relation], page_size: int = 50) -> None:
    """Launch the interactive terminal table browser.

    Parameters
    ----------
    source:
        A Relation, a source string accepted by nybble.load(), or a table name.
    page_size:
        Rows per page (default 50).
    """
    # Resolve to Relation
    if isinstance(source, str):
        from nybble.resolver import _REGISTERED
        if source in _REGISTERED:
            relation = _REGISTERED[source]
        else:
            relation = load(source)
    else:
        relation = source

    multi_table = hasattr(relation, "tables") and bool(relation.tables)
    parent = relation if multi_table else None

    if multi_table:
        # Outer picker loop - keeps running until the user presses q at the
        # picker level (which raises SystemExit) rather than inside a table view.
        while True:
            child = _pick_table(parent)      # raises SystemExit on 'q'
            ret = _browse_one(child, page_size, multi_table=True)
            if ret is not _BACK:
                break
    else:
        _browse_one(relation, page_size, multi_table=False)


# ── Column profiling overlay ───────────────────────────────────────────────────

def _show_profile(relation_name: str, use_raw: bool) -> None:
    """Display a column-profile panel and wait for any key to dismiss."""
    from rich.panel import Panel as _Panel
    from rich.table import Table as _Table
    from rich import box as _box

    try:
        from nybble.profiler import profile_relation
        profiles = profile_relation(relation_name)
    except Exception as exc:
        console.clear()
        console.print(f"[red]Profile error:[/red] {exc}")
        if use_raw:
            import sys
            sys.stdin.read(1)
        return

    tbl = _Table(
        title=f"Column Profile - {relation_name}",
        box=_box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    tbl.add_column("Column", style="cyan", no_wrap=True)
    tbl.add_column("Type", style="yellow")
    tbl.add_column("Non-null%", justify="right")
    tbl.add_column("Distinct", justify="right")
    tbl.add_column("Card%", justify="right")
    tbl.add_column("Min", overflow="fold", max_width=20)
    tbl.add_column("Max", overflow="fold", max_width=20)
    tbl.add_column("Top value", overflow="fold", max_width=20)

    for p in profiles:
        non_null_pct = f"{100 - p.null_pct:.1f}%"
        distinct_str = f"~{p.distinct:,}" if p.approx else f"{p.distinct:,}"
        tbl.add_row(
            p.name,
            p.dtype,
            non_null_pct,
            distinct_str,
            f"{p.cardinality_pct:.1f}%",
            p.min_val or "[dim]-[/dim]",
            p.max_val or "[dim]-[/dim]",
            p.top_val or "[dim]-[/dim]",
        )

    console.clear()
    console.print(tbl)
    console.print("\n[dim]Press any key to return…[/dim]")
    if use_raw:
        import sys
        sys.stdin.read(1)
    else:
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass


def _prompt_highlight_rule(
    relation_name: str,
    schema: list[dict],
    highlight_rules: list,
    use_raw: bool,
) -> None:
    """Interactively add or clear highlight rules for *relation_name*.

    Prompts: ``column:value:color`` or ``clear`` to remove all rules.
    Mutates *highlight_rules* in-place and persists to disk.
    """
    from nybble.highlight_store import add_rule, clear_rules, _VALID_COLORS
    import sys

    col_names = [c["name"] for c in schema]

    # Temporarily leave raw mode for input
    if use_raw:
        import tty, termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    console.clear()
    console.print("[bold cyan]Highlight Rules[/bold cyan]")
    if highlight_rules:
        console.print("[dim]Current rules:[/dim]")
        for i, r in enumerate(highlight_rules):
            console.print(f"  {i+1}. [cyan]{r['column']}[/cyan] contains [yellow]{r['pattern']}[/yellow] → [bold {r['color']}]{r['color']}[/bold {r['color']}]")
    else:
        console.print("[dim]No rules active.[/dim]")

    console.print()
    console.print("Enter [bold]column:value:color[/bold] to add a rule  |  [bold]clear[/bold] to remove all  |  [bold]Enter[/bold] to cancel")
    console.print(f"[dim]Columns: {', '.join(col_names)}[/dim]")
    console.print(f"[dim]Colors: red, green, yellow, cyan, bold green, bold red, …[/dim]")

    try:
        raw_input = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        raw_input = ""

    if use_raw:
        import tty, termios
        tty.setraw(sys.stdin.fileno())

    if not raw_input:
        return

    if raw_input.lower() == "clear":
        clear_rules(relation_name)
        highlight_rules.clear()
        console.print("[dim]All highlight rules cleared.[/dim]")
        return

    parts = raw_input.split(":", 2)
    if len(parts) != 3:
        console.print("[red]Invalid format. Expected column:value:color[/red]")
        return

    col, pattern, color = [p.strip() for p in parts]
    if col not in col_names:
        console.print(f"[red]Column '{col}' not found. Available: {', '.join(col_names)}[/red]")
        return
    if not pattern:
        console.print("[red]Pattern cannot be empty.[/red]")
        return
    # Accept any color string - Rich will ignore unknown ones gracefully
    new_rules = add_rule(relation_name, col, pattern, color or "bold yellow")
    highlight_rules.clear()
    highlight_rules.extend(new_rules)
    console.print(f"[dim]Rule added: [cyan]{col}[/cyan] contains [yellow]{pattern}[/yellow] → {color}[/dim]")
