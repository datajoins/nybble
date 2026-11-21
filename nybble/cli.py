"""
cli.py - Nybble Click CLI Entry Point

Commands:
  nybble browse <source>
  nybble query "<sql>" [--source <s>...] [--config nybble.yaml]
  nybble explain "<sql>" [--analyze]
  nybble erd [--source <s>...] [--config nybble.yaml]
  nybble kg [--source <s>...] [--config nybble.yaml]
  nybble kg search <query>
  nybble kg pgq [--source <s>...]
  nybble kg configure-cols [--source <key>...] [--col-roles JSON] [--relationships JSON]
  nybble info <source> [--list-tables]
  nybble profile <source>
  nybble diff <source_a> <source_b> [--format table|json]
  nybble history [--search TERM] [--limit N] [--source KEY]
  nybble repl [--source <s>...] [--config nybble.yaml]

UI-first commands (delegate to nybble serve when running, fallback to pure-Python):
  nybble browse, query, diff, history
"""
from __future__ import annotations

import json as _json
import socket
import sys
from pathlib import Path
from typing import Any, Optional

import click
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()
err_console = Console(stderr=True)


# ── UI hint helpers ───────────────────────────────────────────────────────────

_DEFAULT_UI_PORT = 7700


def _ui_running(port: int = _DEFAULT_UI_PORT) -> bool:
    """Return True if something is listening on the Nybble UI port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.15):
            return True
    except OSError:
        return False


def _ui_hint(path: str = "", port: int = _DEFAULT_UI_PORT) -> None:
    """Print a muted 'Open in UI' link when the web UI is running.

    Parameters
    ----------
    path:
        URL sub-path to append, e.g. ``"/erd/abc123"`` or ``"/kg"``.
        Leave empty to link to the UI root.
    port:
        Port the UI is expected to be listening on.
    """
    if _ui_running(port):
        url = f"http://localhost:{port}{path}"
        try:
            console.print(f"[dim]-> Open in UI:[/dim] [link={url}]{url}[/link]")
        except (UnicodeEncodeError, Exception):
            # Fallback for terminals that can't render Rich markup (e.g. Windows cp1252)
            click.echo(f"-> Open in UI: {url}", err=False)


# ── Serve UI REST client helpers ──────────────────────────────────────────────
# When nybble serve is running, CLI commands prefer to delegate to the same
# REST endpoints the browser UI uses — giving agents and users a single source
# of truth.  Pure-Python fallbacks are used when the server is not running.

def _ui_base(port: int = _DEFAULT_UI_PORT) -> str:
    return f"http://localhost:{port}"


def _serve_get(path: str, params: Optional[dict] = None,
               port: int = _DEFAULT_UI_PORT) -> Any:
    """GET {ui_base}{path} and return parsed JSON, or None on failure."""
    import urllib.request, urllib.parse
    url = _ui_base(port) + path
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    try:
        resp = urllib.request.urlopen(url, timeout=10)
        return _json.loads(resp.read().decode())
    except Exception:
        return None


def _serve_post(path: str, body: dict, port: int = _DEFAULT_UI_PORT) -> Any:
    """POST {ui_base}{path} with JSON body and return parsed JSON, or None on failure."""
    import urllib.request
    url  = _ui_base(port) + path
    data = _json.dumps(body, ensure_ascii=False).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        return _json.loads(resp.read().decode())
    except Exception:
        return None

def _render_ui_diff(data: dict, name_a: str, name_b: str) -> None:
    """Render a /api/diff response as Rich tables."""
    tables_a = data.get("tables_a", {})
    tables_b = data.get("tables_b", {})
    all_tables = sorted(set(list(tables_a.keys()) + list(tables_b.keys())))

    added_tbl   = [t for t in tables_b if t not in tables_a]
    removed_tbl = [t for t in tables_a if t not in tables_b]
    common_tbl  = [t for t in all_tables if t in tables_a and t in tables_b]

    if added_tbl:
        console.print(f"[green]Tables added in {name_b}:[/green] " + ", ".join(added_tbl))
    if removed_tbl:
        console.print(f"[red]Tables removed from {name_a}:[/red] " + ", ".join(removed_tbl))

    any_col_diff = False
    for t in common_tbl:
        cols_a = set(tables_a[t].get("columns", {}).keys()) if isinstance(tables_a.get(t), dict) else set()
        cols_b = set(tables_b[t].get("columns", {}).keys()) if isinstance(tables_b.get(t), dict) else set()
        added   = cols_b - cols_a
        removed = cols_a - cols_b
        if added or removed:
            any_col_diff = True
            tbl = Table(title=f"Table: {t}", box=box.MINIMAL, header_style="bold cyan")
            tbl.add_column("Change"); tbl.add_column("Column"); tbl.add_column("Type")
            for c in sorted(added):
                typ = tables_b[t]["columns"][c].get("type", "") if isinstance(tables_b[t].get("columns"), dict) else ""
                tbl.add_row("[green]+[/green]", c, typ)
            for c in sorted(removed):
                typ = tables_a[t]["columns"][c].get("type", "") if isinstance(tables_a[t].get("columns"), dict) else ""
                tbl.add_row("[red]-[/red]", c, typ)
            console.print(tbl)

    if not added_tbl and not removed_tbl and not any_col_diff:
        console.print(f"[green][OK] Schemas are identical[/green] ({name_a} vs {name_b})")


def _render_query_result(data: dict, output_format: str) -> None:
    """Render a /api/query response (or local QueryResult dict) in the requested format."""
    columns  = data.get("columns", [])
    rows_raw = data.get("rows", [])
    row_count = data.get("row_count", len(rows_raw))
    elapsed_ms = data.get("elapsed_ms", 0)
    truncated  = data.get("truncated", False)

    # Normalise rows: list[dict] → list[list] using column order
    def _row_vals(row):
        if isinstance(row, dict):
            return [row.get(c) for c in columns]
        return list(row)

    if output_format == "csv":
        import csv, io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        for r in rows_raw:
            writer.writerow([str(v) if v is not None else "" for v in _row_vals(r)])
        click.echo(buf.getvalue())
    elif output_format == "json":
        click.echo(_json.dumps(data, default=str))
    else:
        tbl = Table(box=box.MINIMAL, show_header=True, header_style="bold cyan")
        for col in columns:
            tbl.add_column(col, overflow="fold", max_width=40)
        for r in rows_raw:
            tbl.add_row(*[str(v) if v is not None else "NULL" for v in _row_vals(r)])
        console.print(tbl)
        trunc = " (truncated)" if truncated else ""
        timing = f"{elapsed_ms:.1f} ms" if elapsed_ms else ""
        console.print(f"[dim]{row_count:,} rows{(' · ' + timing) if timing else ''}{trunc}[/dim]")


def _print_ui_rows(data: dict, title: str) -> None:
    """Print rows from a UI /api/rows response as a Rich table."""
    columns = data.get("columns") or (list(data["rows"][0].keys()) if data.get("rows") else [])
    rows    = data.get("rows", [])
    total   = data.get("total", len(rows))
    tbl = Table(title=title, box=box.MINIMAL, show_header=True, header_style="bold cyan")
    for col in columns:
        tbl.add_column(str(col), overflow="fold", max_width=35)
    for row in rows:
        if isinstance(row, dict):
            tbl.add_row(*[str(row.get(c, "")) if row.get(c) is not None else "NULL" for c in columns])
        else:
            tbl.add_row(*[str(v) if v is not None else "NULL" for v in row])
    console.print(tbl)
    page     = data.get("page", 1)
    page_size = data.get("page_size", len(rows))
    console.print(f"[dim]{total:,} rows total · page {page} · {len(rows)} shown[/dim]")


def _find_ui_source_key(name_or_path: str) -> Optional[str]:
    """Return the serve UI source key for a given name, path, or key string.

    Calls /api/files and matches on name, key, or filename stem.
    Returns None if serve is not running or no match found.
    """
    if not _ui_running():
        return None
    sources = _serve_get("/api/files")
    if not isinstance(sources, list):
        return None
    stem = Path(name_or_path).stem
    for r in sources:
        if (r.get("key") == name_or_path
                or r.get("name") == name_or_path
                or Path(r.get("name", "")).stem == stem):
            return r["key"]
    return None


# Config option reused across all multi-source commands
_CONFIG_OPTION = click.option(
    "--config", "-c",
    default=None,
    metavar="PATH",
    help=(
        "Path to a nybble.yaml workspace config file. "
        "If omitted, nybble.yaml / nybble.yml in the current directory "
        "is loaded automatically when present."
    ),
)


def _load_sources(sources: tuple[str, ...]) -> None:
    """Pre-load a tuple of source strings via resolver.load() with a progress spinner."""
    import time
    from nybble.resolver import load as _load
    from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

    from rich.console import Console as _Console
    stderr_console = _Console(stderr=True)
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=stderr_console,
        transient=True,
    ) as progress:
        for s in sources:
            display = s if len(s) <= 60 else "…" + s[-57:]
            task = progress.add_task(f"Loading {display}", total=None)
            t0 = time.perf_counter()
            try:
                if "=" in s:
                    name, path = s.split("=", 1)
                    rel = _load(path, name=name.strip())
                else:
                    rel = _load(s)
            except (FileNotFoundError, ValueError, ConnectionError) as exc:
                progress.remove_task(task)
                stderr_console.print(f"[red]Error loading[/red] {display!r}: {exc}")
                raise SystemExit(1)
            except Exception as exc:
                progress.remove_task(task)
                _root = type(exc).__name__
                stderr_console.print(f"[red]Error loading[/red] {display!r} ({_root}): {exc}")
                raise SystemExit(1)
            elapsed = (time.perf_counter() - t0) * 1000
            progress.remove_task(task)
            stderr_console.print(
                f"[dim]Loaded [cyan]{rel.name}[/cyan] "
                f"({rel.source_type}) · "
                f"{rel.row_count:,} rows · {elapsed:.0f} ms[/dim]"
            )


def _resolve_source(source: str, config_path: Optional[str] = None) -> "Relation":
    """Return a Relation for *source*, loading nybble.yaml first if needed.

    Resolution order:
      1. Already registered in _REGISTERED (e.g. loaded by a previous command
         in this process, or by the auto-discovered nybble.yaml).
      2. Auto-load nybble.yaml (or the explicitly supplied config), then
         re-check _REGISTERED.  This covers names like ``medicine_data`` that
         exist only after the config is applied.
      3. Fall back to resolver.load(source) which treats source as a file path,
         URI, or connection string.
    """
    from nybble.resolver import _REGISTERED, load as _load

    # Fast path: already in session (e.g. called after _load_config_sources)
    if source in _REGISTERED:
        return _REGISTERED[source]

    # Load config (auto-discover or explicit), then re-check
    _load_config_sources(config_path)
    if source in _REGISTERED:
        return _REGISTERED[source]

    # Treat as a literal file/URI/connection string
    try:
        return _load(source)
    except FileNotFoundError:
        # Give a more helpful error when the source looks like a registered name
        if _REGISTERED:
            names = ", ".join(sorted(_REGISTERED)[:8])
            hint = f"\n  Loaded sources: {names}"
            if len(_REGISTERED) > 8:
                hint += f" … ({len(_REGISTERED)} total)"
        else:
            hint = "\n  No sources are loaded. Run 'nybble erd' or pass --config to auto-load."
        raise FileNotFoundError(
            f"Source not found: {source!r}{hint}\n"
            f"  Tip: use the registered table name, not the original file path."
        )


def _load_config_sources(config_path: Optional[str]) -> None:
    """Load sources from a nybble.yaml config file with a progress spinner.

    If config_path is None, auto-discovers nybble.yaml / nybble.yml in cwd.
    Silently does nothing if no config file is found and config_path is None.
    """
    import time
    from nybble.config import load_config, apply_config, find_config
    from pathlib import Path
    from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

    if config_path is not None:
        cfg_file = Path(config_path)
        if not cfg_file.exists():
            console.print(f"[red]Config file not found:[/red] {config_path}")
            raise SystemExit(1)
    else:
        cfg_file = find_config()
        if cfg_file is None:
            return   # no config in cwd - silent, use --source flags only

    try:
        cfg = load_config(cfg_file)
    except KeyError as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        raise SystemExit(1)
    except Exception as exc:
        console.print(f"[red]Failed to parse {cfg_file}:[/red] {exc}")
        raise SystemExit(1)

    console.print(f"[dim]Config: [cyan]{cfg_file}[/cyan] · workspace [cyan]{cfg.name}[/cyan][/dim]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        t0_all = time.perf_counter()
        # apply_config loads all sources; wrap with a single spinner
        task = progress.add_task(f"Loading workspace {cfg.name}…", total=None)
        relations = apply_config(cfg)
        progress.remove_task(task)

    elapsed_all = (time.perf_counter() - t0_all) * 1000
    for rel in relations:
        console.print(
            f"[dim]  Loaded [cyan]{rel.name}[/cyan] "
            f"({rel.source_type}) · {rel.row_count:,} rows[/dim]"
        )
    if relations:
        console.print(f"[dim]  {len(relations)} source{'s' if len(relations) != 1 else ''} in {elapsed_all:.0f} ms[/dim]")


@click.group()
@click.version_option(package_name="datajoins-nybble", prog_name="nybble")
def cli() -> None:
    """nybble - ultra-lightweight data engine.

    Browse, query, diagram, and semantically traverse any data source.

    \b
    Multi-source commands accept a --config / -c flag pointing to a
    nybble.yaml workspace file. If nybble.yaml exists in the current
    directory it is loaded automatically without any flag.
    """


@cli.command("browse")
@click.argument("source")
@click.option("--page-size", "-n", default=50, show_default=True,
              help="Rows per page.")
@click.option("--table", "-t", default=None, metavar="TABLE",
              help="Table to browse (for multi-table sources).")
@_CONFIG_OPTION
def cmd_browse(source: str, page_size: int, table: Optional[str],
               config: Optional[str]) -> None:
    """Launch the interactive table browser for SOURCE.

    When nybble serve is running, opens the full UI browse view.
    Otherwise falls back to the terminal pager.

    SOURCE can be a file path, connection string, HTTP URL, or the
    registered name of a relation loaded by nybble.yaml.

    \b
    Examples:
      nybble browse orders.csv
      nybble browse medicine_data
      nybble browse medicine_dataset --config nybble.yaml
    """
    # UI-first: if serve is running, resolve source key and open the browse URL
    if _ui_running():
        sources_data = _serve_get("/api/files")
        if isinstance(sources_data, list):
            # Match by name or key
            match = next(
                (r for r in sources_data
                 if r.get("name") == source or r.get("key") == source
                 or Path(r.get("name", "")).stem == Path(source).stem),
                None,
            )
            if match:
                key = match["key"]
                if table:
                    _ui_hint(f"/browse/{key}?table={table}")
                else:
                    _ui_hint(f"/browse/{key}")
                # Also print first page of data for agents
                tbl_name = table or source
                data = _serve_get(f"/api/rows/{key}/{tbl_name}",
                                  params={"page": 1, "page_size": page_size})
                if data and "rows" in data:
                    _print_ui_rows(data, tbl_name)
                    return
    # Fallback: terminal pager
    from nybble.browse import browse
    try:
        rel = _resolve_source(source, config)
        browse(rel, page_size=page_size)
    except (FileNotFoundError, ValueError, ConnectionError) as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)
    except Exception as exc:
        err_console.print(f"[red]Error ({type(exc).__name__}):[/red] {exc}")
        raise SystemExit(1)


@cli.command("query")
@click.argument("sql")
@_CONFIG_OPTION
@click.option("--source", "-s", multiple=True,
              help="Pre-load a source before querying. May be repeated. "
                   "Use name=path to assign a custom table name.")
@click.option("--limit", "-l", default=None, type=int,
              help="Maximum rows to display.")
@click.option("--format", "-f", "output_format",
              type=click.Choice(["table", "csv", "json"], case_sensitive=False),
              default="table", show_default=True,
              help="Output format.")
def cmd_query(
    sql: str,
    config: Optional[str],
    source: tuple[str, ...],
    limit: Optional[int],
    output_format: str,
) -> None:
    """Execute SQL across all loaded sources.

    When nybble serve is running and a single --source is provided, the query
    is executed via the UI's /api/query endpoint (same path as the browser).
    Otherwise falls back to the local DuckDB engine.

    \b
    Examples:
      nybble query "SELECT * FROM orders LIMIT 10"
      nybble query "SELECT * FROM o" --source orders.csv
      nybble query "SELECT * FROM users" --config nybble.yaml
      nybble query "SELECT a.*, b.name FROM a JOIN b ON a.id=b.id" \\
        --source a=table_a.csv --source b=table_b.parquet
    """
    # UI-first: prefer the serve UI query engine when it's running and one source is specified
    if _ui_running() and len(source) == 1 and not config:
        key = _find_ui_source_key(source[0])
        if key:
            payload: dict = {"sql": sql}
            if limit:
                payload["limit"] = limit
            result_data = _serve_post(f"/api/query/{key}", payload)
            if result_data and "columns" in result_data:
                _render_query_result(result_data, output_format)
                _ui_hint(f"/query/{key}")
                return

    # Fallback: local DuckDB engine
    _load_config_sources(config)
    if source:
        _load_sources(source)

    from nybble.arena import query as _query
    try:
        result = _query(sql, limit=limit)
    except Exception as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    if output_format == "csv":
        import csv, io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(result.columns)
        writer.writerows(result.rows())
        click.echo(buf.getvalue())
    elif output_format == "json":
        import json
        click.echo(json.dumps(result.to_dict(), default=str))
    else:
        tbl = Table(box=box.MINIMAL, show_header=True, header_style="bold cyan")
        for col in result.columns:
            tbl.add_column(col, overflow="fold", max_width=40)
        for row in result.rows():
            tbl.add_row(*[str(v) if v is not None else "NULL" for v in row])
        console.print(tbl)
        trunc = " (truncated)" if result.truncated else ""
        # Show plan+exec breakdown when both are available (non-zero)
        if getattr(result, "plan_ms", 0) or getattr(result, "exec_ms", 0):
            plan_ms = getattr(result, "plan_ms", 0)
            exec_ms = getattr(result, "exec_ms", 0)
            fetch_ms = result.elapsed_ms - plan_ms - exec_ms
            timing = (
                f"plan: {plan_ms:.1f} ms  "
                f"exec: {exec_ms:.1f} ms  "
                f"fetch: {max(0.0, fetch_ms):.1f} ms"
            )
        else:
            timing = f"{result.elapsed_ms:.1f} ms"
        console.print(
            f"[dim]{result.row_count:,} rows · {timing}{trunc}[/dim]"
        )


def _print_erd_no_sources_guide() -> None:
    """Print a friendly, step-by-step guide when no relations are loaded."""
    NL = "\n"
    sep = click.style("-" * 60, fg="bright_black")

    click.echo(NL + click.style("No data sources loaded", bold=True, fg="yellow")
               + click.style(" - nybble erd needs at least one source.", fg="yellow"))
    click.echo()

    # ── Option 1: --source flag ───────────────────────────────────────────────
    click.echo(click.style("Option 1 · Pass sources on the command line", bold=True))
    click.echo(sep)
    click.echo("  Load one or more files, databases, or URLs directly:")
    click.echo()
    click.echo("  " + click.style("nybble erd --source orders.csv", fg="cyan"))
    click.echo("  " + click.style(
        "nybble erd --source orders.csv --source customers.parquet", fg="cyan"))
    click.echo("  " + click.style(
        "nybble erd --source sqlite:///mydb.sqlite", fg="cyan"))
    click.echo("  " + click.style(
        "nybble erd --source postgresql://user:pass@host/dbname", fg="cyan"))
    click.echo()

    # ── Option 2: workspace config ────────────────────────────────────────────
    click.echo(click.style("Option 2 · Create a workspace config file", bold=True))
    click.echo(sep)
    click.echo(
        "  Create " + click.style("nybble.yaml", bold=True) + " in your project root "
        "(nybble auto-discovers it):"
    )
    click.echo()

    cfg_example = """\
  # nybble.yaml - place this file in your project root
  version: "1.0"

  workspace:
    name: "my-project"

  files:
    - id: orders
      type: csv
      path: ./data/orders.csv

    - id: customers
      type: parquet
      path: ./data/customers.parquet

  # databases:
  #   - id: mydb
  #     type: sqlite
  #     connection_string: sqlite:///path/to/mydb.sqlite"""

    click.echo(click.style(cfg_example, fg="bright_black"))
    click.echo()
    click.echo(
        "  Then run " + click.style("nybble erd", fg="cyan")
        + "  (no extra flags needed - the config is loaded automatically)."
    )
    click.echo()

    # ── Option 3: Python API ──────────────────────────────────────────────────
    click.echo(click.style("Option 3 · Load programmatically before calling erd()", bold=True))
    click.echo(sep)
    click.echo()

    py_example = """\
  import nybble

  nybble.load("orders.csv")
  nybble.load("customers.parquet")
  nybble.erd()          # opens .nybble/erd.html in your browser"""

    click.echo(click.style(py_example, fg="bright_black"))
    click.echo()


@cli.command("erd")
@_CONFIG_OPTION
@click.option("--source", "-s", multiple=True,
              help="Pre-load a source. May be repeated.")
@click.option("--output", "-o", default=None,
              help="Output file path (default: .nybble/erd.html).")
@click.option("--no-browser", is_flag=True, default=False,
              help="Do not open the HTML file automatically.")
def cmd_erd(
    config: Optional[str],
    source: tuple[str, ...],
    output: Optional[str],
    no_browser: bool,
) -> None:
    """Generate an interactive ERD from all loaded sources.

    \b
    Examples:
      nybble erd --source mydb.sqlite
      nybble erd --config nybble.yaml
      nybble erd --source orders.csv --source customers.parquet --no-browser
    """
    _load_config_sources(config)
    if source:
        _load_sources(source)

    from nybble.erd import erd as _erd
    try:
        out_path = _erd(output=output, open_browser=not no_browser)
    except ValueError as exc:
        if "No relations loaded" in str(exc):
            _print_erd_no_sources_guide()
            sys.exit(1)
        # Any other ValueError bubbles up as before
        err_console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    console.print(f"[green]ERD written to:[/green] {out_path}")
    _ui_hint()


@cli.group("kg", invoke_without_command=True)
@_CONFIG_OPTION
@click.option("--source", "-s", multiple=True,
              help="Pre-load a source. May be repeated.")
@click.option("--output-dir", "-o", default=None,
              help="Output directory (default: .nybble/).")
@click.option("--no-browser", is_flag=True, default=False,
              help="Do not open the HTML file automatically.")
@click.option("--summary-only", is_flag=True, default=False,
              help="Print summary JSON only, do not generate files.")
@click.option("--export", "export_format", default=None,
              type=click.Choice(["pgq", "graphml", "jsonld", "html", "all"], case_sensitive=False),
              help="Additional export format. 'pgq' emits DuckDB SQL/PGQ DDL.")
@click.pass_context
def cmd_kg(
    ctx: click.Context,
    config: Optional[str],
    source: tuple[str, ...],
    output_dir: Optional[str],
    no_browser: bool,
    summary_only: bool,
    export_format: Optional[str],
) -> None:
    """Build and export a semantic Knowledge Graph.

    When called without a subcommand, builds the graph and writes output files.
    Use 'nybble kg search' to search node labels without writing files.

    \b
    Examples:
      nybble kg --source customers.csv --source orders.parquet
      nybble kg --config nybble.yaml
      nybble kg --summary-only --source mydb.sqlite
      nybble kg --export pgq --source mydb.sqlite   # emit SQL/PGQ DDL
      nybble kg search "Alice" --source customers.csv
      nybble kg search "customer from Germany" --source customers.csv
    """
    # If a subcommand was invoked (e.g. 'search'), let it run instead.
    if ctx.invoked_subcommand is not None:
        # Store source-loading params in the context for subcommands to use.
        ctx.ensure_object(dict)
        ctx.obj["config"] = config
        ctx.obj["source"] = source
        return

    _load_config_sources(config)
    if source:
        _load_sources(source)

    if summary_only:
        from nybble.kg import kg_summary
        import json
        summary = kg_summary()
        click.echo(json.dumps(summary, indent=2))
        return

    export_pgq = export_format == "pgq" if export_format else False

    from nybble.kg import kg as _kg
    result = _kg(output_dir=output_dir, open_browser=not no_browser, export_pgq=export_pgq)
    console.print(f"[green]Knowledge Graph written:[/green]")
    console.print(f"  GraphML:  {result.graphml_path}")
    console.print(f"  JSON-LD:  {result.jsonld_path}")
    console.print(f"  HTML (static export):  {result.html_path}")
    if result.pgq_path:
        console.print(f"  SQL/PGQ:  {result.pgq_path}")
        console.print(
            f"  [dim]Tip: pipe into DuckDB ->[/dim]  "
            f"duckdb mydb.duckdb < {result.pgq_path}"
        )
    console.print(
        f"\n[dim]Summary:[/dim] "
        f"{result.summary['node_count']:,} nodes, "
        f"{result.summary['edge_count']:,} edges"
    )
    # Always surface the full interactive UI path — it has FRD Explore,
    # Path Filter, layout switching, and live re-indexing that the static
    # export does not. If the server is already running, _ui_hint prints
    # the clickable URL; if not, suggest starting it.
    if _ui_running():
        _ui_hint("/kg")
    else:
        console.print(
            "[dim]Tip: run [bold]nybble serve[/bold] for the full interactive "
            "Knowledge Graph UI (Cytoscape, FRD Explore, layout modes, live search).[/dim]"
        )


@cmd_kg.command("search")
@click.argument("query")
@click.option("--top-k", "-k", default=10, show_default=True,
              help="Maximum number of results to return.")
@click.option("--source", "-s", multiple=True,
              help="Pre-load a source before searching. May be repeated.")
@_CONFIG_OPTION
@click.option("--format", "-f", "output_format",
              type=click.Choice(["table", "json"], case_sensitive=False),
              default="table", show_default=True,
              help="Output format.")
@click.pass_context
def cmd_kg_search(
    ctx: click.Context,
    query: str,
    top_k: int,
    source: tuple[str, ...],
    config: Optional[str],
    output_format: str,
) -> None:
    """Search Knowledge Graph nodes by text query.

    Uses local semantic similarity (sentence-transformers) when the [semantic]
    extra is installed; falls back to substring matching automatically.

    The match_mode column shows which backend was used: 'semantic' or 'substring'.
    FK inference and graph topology are never affected - embeddings are additive
    for search only.

    \b
    Examples:
      nybble kg search "Alice" --source customers.csv
      nybble kg search "customer from Germany" --source customers.csv
      nybble kg search "shipped order" --source orders.csv --top-k 5
      nybble kg search "Alice" --source customers.csv --format json
    """
    # Sources may have been set by the parent kg group; supplement with any
    # --source flags given directly to this subcommand.
    parent_obj = ctx.obj or {}
    parent_config: Optional[str] = parent_obj.get("config")
    parent_sources: tuple[str, ...] = parent_obj.get("source", ())

    effective_config = config or parent_config
    effective_sources = parent_sources + source

    _load_config_sources(effective_config)
    if effective_sources:
        _load_sources(effective_sources)

    from nybble.kg import search_nodes, _has_sentence_transformers
    import json as _json

    results = search_nodes(query, top_k=top_k)

    if not results:
        console.print("[dim]No matching nodes found.[/dim]")
        return

    if output_format == "json":
        out = [
            {
                "node_id": r.node_id,
                "label": r.label,
                "entity_type": r.entity_type,
                "score": r.score,
                "match_mode": r.match_mode,
            }
            for r in results
        ]
        click.echo(_json.dumps(out, indent=2))
        return

    from rich.table import Table
    from rich import box

    mode_label = "semantic" if _has_sentence_transformers() else "substring"
    tbl = Table(
        title=f'KG node search: "{query}"  [{mode_label} mode]',
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    tbl.add_column("#", justify="right", style="dim", width=4)
    tbl.add_column("Score", justify="right", width=7)
    tbl.add_column("Label", style="cyan", overflow="fold", max_width=40)
    tbl.add_column("Entity type", style="yellow", no_wrap=True)
    tbl.add_column("Node ID", style="dim", overflow="fold", max_width=30)

    for i, r in enumerate(results, 1):
        tbl.add_row(
            str(i),
            f"{r.score:.4f}",
            r.label,
            r.entity_type,
            r.node_id,
        )

    console.print(tbl)
    console.print(
        f"[dim]{len(results)} result{'s' if len(results) != 1 else ''} · "
        f"mode: {mode_label}[/dim]"
    )

    if not _has_sentence_transformers():
        console.print(
            "[dim]Tip: install sentence-transformers for semantic search: "
            'pip install "datajoins-nybble[semantic]"[/dim]'
        )


@cmd_kg.command("pgq")
@click.option("--source", "-s", multiple=True,
              help="Pre-load a source before generating DDL. May be repeated.")
@_CONFIG_OPTION
@click.option("--out", "-o", default=None, metavar="FILE",
              help="Write DDL to FILE instead of stdout. Confirms path on stderr.")
@click.option("--graph-name", default="nybble_graph", show_default=True,
              help="Name for the CREATE PROPERTY GRAPH statement.")
@click.pass_context
def cmd_kg_pgq(
    ctx: click.Context,
    source: tuple[str, ...],
    config: Optional[str],
    out: Optional[str],
    graph_name: str,
) -> None:
    """Generate DuckDB SQL/PGQ DDL from inferred schema relationships.

    Produces a CREATE PROPERTY GRAPH statement compatible with DuckDB 0.10+.
    All computation is local - no LLM calls, no network calls.

    By default, DDL is written to stdout so it can be piped into DuckDB:

    \b
    Examples:
      nybble kg pgq --source mydb.sqlite | duckdb mydb.duckdb
      nybble kg pgq --source customers.csv --source orders.csv
      nybble kg pgq --source mydb.sqlite --out kg.sql
      nybble kg pgq --config nybble.yaml --graph-name my_graph
    """
    # Inherit sources set by the parent kg group (if any)
    parent_obj = ctx.obj or {}
    parent_config: Optional[str] = parent_obj.get("config")
    parent_sources: tuple[str, ...] = parent_obj.get("source", ())

    effective_config = config or parent_config
    effective_sources = parent_sources + source

    _load_config_sources(effective_config)
    if effective_sources:
        _load_sources(effective_sources)

    from nybble.resolver import _REGISTERED
    from nybble.infer_fk import infer_fks
    from nybble.kg import _build_graph, generate_pgq_ddl

    rels = list(_REGISTERED.values())
    if not rels:
        console.print("[red]Error:[/red] No sources loaded. Use --source or --config.")
        raise SystemExit(1)

    fk_candidates = infer_fks(rels)
    G = _build_graph(rels, fk_candidates)

    ddl = generate_pgq_ddl(G, fk_candidates, graph_name=graph_name)

    if not ddl.strip():
        console.print("[red]Error:[/red] Graph is empty - no DDL generated.")
        raise SystemExit(1)

    if out is not None:
        from pathlib import Path
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(ddl, encoding="utf-8")
        # Confirmation to stderr so piping stdout still works cleanly
        err_console.print(f"[dim]SQL/PGQ DDL written to: {out_path}[/dim]")
    else:
        click.echo(ddl, nl=False)


@cmd_kg.command("configure-cols")
@click.option("--source", "-s", multiple=True,
              help="Source key(s) to configure. May be repeated.")
@_CONFIG_OPTION
@click.option("--col-roles", "col_roles_json", default=None, metavar="JSON",
              help=(
                  "Column role overrides as JSON. "
                  'Shape: {"source_key": {"table": {"column": "node|edge|weight|ignore|auto"}}}. '
                  "Example: --col-roles '{\"abc123\": {\"medicine_dataset\": {\"Category\": \"node\"}}}'."
              ))
@click.option("--relationships", "relationships_json", default=None, metavar="JSON",
              help=(
                  "Explicit relationship rules as JSON array. Each item: "
                  "{source_key, table, src_col, tgt_col, label_col?, weight_col?}."
              ))
@click.option("--mode", default="data", show_default=True,
              type=click.Choice(["data", "schema", "auto"], case_sensitive=False),
              help="Graph mode.")
@click.option("--format", "-f", "output_format",
              type=click.Choice(["table", "json"], case_sensitive=False),
              default="table", show_default=True,
              help="Output format for the result summary.")
@click.pass_context
def cmd_kg_configure_cols(
    ctx: click.Context,
    source: tuple[str, ...],
    config: Optional[str],
    col_roles_json: Optional[str],
    relationships_json: Optional[str],
    mode: str,
    output_format: str,
) -> None:
    """Configure column roles and rebuild the KG graph.

    Calls GET /api/kg/columns to inspect column metadata, then POSTs to
    /api/kg/graph with the supplied role overrides.  Requires nybble serve
    to be running.

    \b
    Workflow:
      1. Run without flags to see columns and suggested roles:
           nybble kg configure-cols --source skincare_data
      2. Set roles and rebuild:
           nybble kg configure-cols --source skincare_data \\
             --col-roles '{"abc123": {"skincare": {"Category": "node", "Name": "node"}}}'

    \b
    Examples:
      nybble kg configure-cols --source abc123
      nybble kg configure-cols --source abc123 \\
        --col-roles '{"abc123": {"medicine_dataset": {"Category": "node"}}}'
      nybble kg configure-cols --source abc123 --source def456 \\
        --relationships '[{"source_key":"abc123","table":"orders","src_col":"from","tgt_col":"to"}]'
    """
    if not _ui_running():
        err_console.print(
            "[red]Error:[/red] nybble serve is not running. "
            "Start it with [cyan]nybble serve[/cyan] then retry."
        )
        raise SystemExit(1)

    # Resolve sources: use --source flags, then fall back to all ready sources
    keys = list(source)
    if not keys:
        # Try to pick from currently loaded UI sources
        sources_data = _serve_get("/api/files") or []
        keys = [r["key"] for r in (sources_data if isinstance(sources_data, list) else [])
                if r.get("status") == "ready"]
        if not keys:
            err_console.print("[red]Error:[/red] No sources available. Load one first via nybble serve.")
            raise SystemExit(1)

    # If no col_roles supplied, just show column metadata for the first source
    if not col_roles_json:
        # Print column info for each source key
        for key in keys:
            meta = _serve_get(f"/api/kg/columns?source={key}")
            if not meta or "tables" not in meta:
                console.print(f"[yellow]No column metadata for {key}[/yellow]")
                continue
            src_name = key
            # Try to get display name
            sources_list = _serve_get("/api/files") or []
            for r in (sources_list if isinstance(sources_list, list) else []):
                if r.get("key") == key:
                    src_name = r.get("name", key)
                    break
            console.print(f"\n[bold]Source:[/bold] [cyan]{src_name}[/cyan] [dim]({key})[/dim]")
            for tbl_name, cols in (meta.get("tables") or {}).items():
                tbl = Table(title=f"Table: {tbl_name}", box=box.ROUNDED,
                            header_style="bold cyan")
                tbl.add_column("Column", style="cyan", no_wrap=True)
                tbl.add_column("Type / Distinct", style="yellow")
                tbl.add_column("Sample values", style="dim", overflow="fold", max_width=35)
                tbl.add_column("Suggested role", style="green")
                for col in (cols if isinstance(cols, list) else []):
                    dist_text = (f"{col.get('type', '')} · {col.get('distinct')} distinct"
                                 if col.get("distinct") is not None else col.get("type", ""))
                    sample = ", ".join((col.get("sample") or [])[:4])
                    tbl.add_row(col.get("name", ""), dist_text, sample,
                                col.get("suggested_role", "auto"))
                console.print(tbl)
        console.print(
            "\n[dim]Pass [cyan]--col-roles JSON[/cyan] to set roles and rebuild the graph.[/dim]"
        )
        _ui_hint("/kg")
        return

    # Parse JSON args
    try:
        col_roles     = _json.loads(col_roles_json)
    except _json.JSONDecodeError as exc:
        err_console.print(f"[red]Invalid --col-roles JSON:[/red] {exc}")
        raise SystemExit(1)

    relationships: list = []
    if relationships_json:
        try:
            relationships = _json.loads(relationships_json)
        except _json.JSONDecodeError as exc:
            err_console.print(f"[red]Invalid --relationships JSON:[/red] {exc}")
            raise SystemExit(1)

    # POST to /api/kg/graph
    result = _serve_post("/api/kg/graph", {
        "sources":       ",".join(keys),
        "mode":          mode,
        "col_roles":     col_roles,
        "relationships": relationships,
    })

    if not result:
        err_console.print("[red]Error:[/red] No response from serve UI.")
        raise SystemExit(1)
    if isinstance(result, dict) and result.get("error"):
        err_console.print(f"[red]Error:[/red] {result['error']}")
        raise SystemExit(1)

    if output_format == "json":
        click.echo(_json.dumps(result, default=str, indent=2))
        return

    nc = result.get("node_count", 0) if isinstance(result, dict) else 0
    ec = result.get("edge_count", 0) if isinstance(result, dict) else 0
    m  = result.get("mode", mode)    if isinstance(result, dict) else mode
    console.print(f"[green]Graph rebuilt:[/green] {nc:,} nodes · {ec:,} edges · mode={m}")
    _ui_hint("/kg")


@cli.command("info")
@click.argument("source")
@click.option("--list-tables", "list_tables", is_flag=True, default=False,
              help="List all tables in a multi-table database source.")
@click.option("--table", "-t", default=None, metavar="TABLE",
              help="Show schema and sample rows for a specific table within a multi-table source.")
@_CONFIG_OPTION
def cmd_info(source: str, list_tables: bool, table: Optional[str], config: Optional[str]) -> None:
    """Print schema, row count, source type, and sample rows for SOURCE.

    SOURCE can be a registered table name (from nybble.yaml), a file path,
    or a database URI.  For multi-table sources use --table to inspect a
    specific table's columns and sample rows.

    \b
    Examples:
      nybble info medicine_data
      nybble info orders.csv
      nybble info data/mydb.sql.gz --table website
      nybble info "postgresql://user:pass@localhost/mydb" --table users
    """
    from nybble.resolver import _REGISTERED
    from pathlib import Path as _Path

    try:
        rel = _resolve_source(source, config)
    except (FileNotFoundError, ValueError) as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    # Multi-table sources (SQLite DBs, SQL dumps, PostgreSQL): always show the
    # table list.  rel.name is the registered prefix (e.g. "umami_backup_20261018_020002_sql"),
    # which is the correct base for child view names like "<prefix>__<table>".
    tables = getattr(rel, "tables", None)
    if tables:
        prefix = rel.name  # always correct - set by resolver, not derived from filename

        # --table: drill into a specific child table
        if table is not None:
            view_name = f"{prefix}__{table}"
            child = _REGISTERED.get(view_name)
            if child is None:
                err_console.print(
                    f"[red]Table [bold]{table!r}[/bold] not found in {_Path(source).name}.[/red]\n"
                    f"Run [cyan]nybble info {source}[/cyan] to see available tables."
                )
                sys.exit(1)
            # Fall through to the single-table display below using the child relation
            rel = child
            tables = None   # suppress multi-table branch
        else:
            console.print(
                f"[bold]Tables in {_Path(source).name}:[/bold]  "
                f"[dim]({len(tables)} table{'s' if len(tables) != 1 else ''})[/dim]"
            )
            for t in tables:
                console.print(f"  • {t}")
            console.print(
                f"\n[dim]Tip: inspect a table's columns with  "
                f"[cyan]nybble info {source} --table {tables[0]}[/cyan][/dim]"
            )
            console.print(
                f"[dim]     query a table with               "
                f'[cyan]nybble query "SELECT * FROM {prefix}__{tables[0]}" '
                f"--source {source}[/cyan][/dim]"
            )
            return

    # If --list-tables was passed for a single-table source, note it and continue
    if list_tables:
        console.print("[dim]No multi-table index available for this source.[/dim]")

    # Schema table
    schema = rel.schema()
    tbl = Table(title=f"Schema: {rel.name}", box=box.ROUNDED)
    tbl.add_column("Column", style="cyan")
    tbl.add_column("Type", style="yellow")
    for col in schema:
        tbl.add_row(col["name"], col["type"])
    console.print(tbl)

    console.print(f"\n[bold]Registered name:[/bold] [cyan]{rel.name}[/cyan]")
    console.print(f"[bold]Source:[/bold]          {rel.source}")
    console.print(f"[bold]Source type:[/bold]     {rel.source_type}")
    console.print(f"[bold]Row count:[/bold]       {rel.row_count:,}")
    console.print(
        f"\n[dim]Use [cyan]{rel.name}[/cyan] in queries, browse, diff, and profile - e.g.[/dim]"
    )
    console.print(f'[dim]  nybble query "SELECT * FROM {rel.name} LIMIT 10"[/dim]')
    console.print(f"[dim]  nybble browse {rel.name}[/dim]")
    console.print(f"[dim]  nybble profile {rel.name}[/dim]")

    # Sample rows
    sample = rel.sample(5)
    if not sample.is_empty():
        sample_tbl = Table(title="Sample rows (5)", box=box.MINIMAL)
        for col in sample.columns:
            sample_tbl.add_column(col, overflow="fold", max_width=30)
        for row in sample.iter_rows():
            sample_tbl.add_row(*[str(v) if v is not None else "NULL" for v in row])
        console.print(sample_tbl)


@cli.command("explain")
@click.argument("sql")
@click.option("--analyze", "-a", is_flag=True, default=False,
              help="Run EXPLAIN ANALYZE (executes the query; reports actual stats).")
@click.option("--source", "-s", multiple=True,
              help="Pre-load a source before explaining. May be repeated.")
@_CONFIG_OPTION
def cmd_explain(sql: str, analyze: bool, source: tuple[str, ...], config: Optional[str]) -> None:
    """Show DuckDB's query execution plan for SQL.

    \b
    Examples:
      nybble explain "SELECT * FROM orders WHERE amount > 100"
      nybble explain --analyze "SELECT * FROM orders" --source orders.csv
    """
    _load_config_sources(config)
    if source:
        _load_sources(source)

    from nybble.arena import explain as _explain
    try:
        plan = _explain(sql, analyze=analyze)
    except Exception as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    # DuckDB explain output contains box-drawing chars (┌, │, └, etc.) that can't
    # be encoded in cp1252.  Write via the binary buffer with UTF-8 encoding to
    # avoid UnicodeEncodeError on Windows terminals.
    try:
        sys.stdout.buffer.write((plan + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except AttributeError:
        # TextIO that has no .buffer (e.g. Click's test runner)
        click.echo(plan)


@cli.command("profile")
@click.argument("source")
@_CONFIG_OPTION
def cmd_profile(source: str, config: Optional[str]) -> None:
    """Print column statistics for SOURCE (null%, cardinality, min, max, top value).

    SOURCE can be a registered table name (from nybble.yaml), a file path,
    or a database URI.

    \b
    Examples:
      nybble profile medicine_data
      nybble profile orders.csv
      nybble profile "s3://my-bucket/data.parquet"
    """
    from nybble.profiler import profile_relation
    from nybble.resolver import _REGISTERED

    try:
        rel = _resolve_source(source, config)
    except (FileNotFoundError, ValueError) as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    # Multi-table sources (PostgreSQL, SQLite, etc.): profile each child table.
    child_tables = getattr(rel, "tables", None)
    if child_tables:
        names_to_profile = [f"{rel.name}__{t}" for t in child_tables
                            if f"{rel.name}__{t}" in _REGISTERED]
        if not names_to_profile:
            err_console.print(
                f"[red]Error:[/red] No child tables registered for {rel.name!r}. "
                "Try running 'nybble info <source>' first."
            )
            sys.exit(1)
    else:
        names_to_profile = [rel.name]

    def _print_profile(name: str) -> None:
        child_rel = _REGISTERED.get(name)
        row_count = getattr(child_rel, "row_count", None) or 0
        profiles = profile_relation(name)
        if not profiles:
            console.print(f"[yellow]No columns found or relation is empty:[/yellow] {name}")
            return
        tbl = Table(
            title=f"Column Profile - {name}  ({row_count:,} rows)",
            box=box.ROUNDED,
            header_style="bold cyan",
        )
        tbl.add_column("Column", style="cyan", no_wrap=True)
        tbl.add_column("Type", style="yellow")
        tbl.add_column("Non-null%", justify="right")
        tbl.add_column("Distinct", justify="right")
        tbl.add_column("Card%", justify="right")
        tbl.add_column("Min", overflow="fold", max_width=22)
        tbl.add_column("Max", overflow="fold", max_width=22)
        tbl.add_column("Top value", overflow="fold", max_width=22)
        for p in profiles:
            non_null_pct = f"{100 - p.null_pct:.1f}%"
            distinct_str = f"~{p.distinct:,}" if p.approx else f"{p.distinct:,}"
            tbl.add_row(
                p.name, p.dtype, non_null_pct, distinct_str,
                f"{p.cardinality_pct:.1f}%",
                p.min_val or "-",
                p.max_val or "-",
                p.top_val or "-",
            )
        console.print(tbl)

    for name in names_to_profile:
        _print_profile(name)
    _ui_hint()


@cli.command("diff")
@click.argument("source_a")
@click.argument("source_b")
@click.option("--format", "-f", "output_format",
              type=click.Choice(["table", "json"], case_sensitive=False),
              default="table", show_default=True,
              help="Output format.")
@click.option("--name-a", default=None, help="Display name for SOURCE_A.")
@click.option("--name-b", default=None, help="Display name for SOURCE_B.")
@_CONFIG_OPTION
def cmd_diff(
    source_a: str,
    source_b: str,
    output_format: str,
    name_a: Optional[str],
    name_b: Optional[str],
    config: Optional[str],
) -> None:
    """Compare schemas of two sources and show added/removed/changed columns.

    When nybble serve is running and both sources are loaded in the UI, the
    diff is computed via the UI's /api/diff endpoint and the interactive diff
    view URL is printed.

    SOURCE_A and SOURCE_B can be registered table names (from nybble.yaml),
    file paths, or database URIs.

    \b
    Examples:
      nybble diff medicine_data medicine_dataset
      nybble diff v1.csv v2.csv
      nybble diff "sqlite:///old.db" "sqlite:///new.db"
      nybble diff medicine_data medicine_dataset --format json
    """
    # UI-first: use /api/diff when both sources are found in the serve UI
    if _ui_running():
        key_a = _find_ui_source_key(source_a)
        key_b = _find_ui_source_key(source_b)
        if key_a and key_b:
            diff_data = _serve_get(f"/api/diff/{key_a}/{key_b}")
            if diff_data and "error" not in diff_data:
                if output_format == "json":
                    click.echo(_json.dumps(diff_data, indent=2, default=str))
                else:
                    _render_ui_diff(diff_data,
                                    name_a or source_a, name_b or source_b)
                _ui_hint(f"/diff/{key_a}/{key_b}")
                return

    # Fallback: local schema diff
    from nybble.diff import schema_diff

    try:
        rel_a = _resolve_source(source_a, config)
        rel_b = _resolve_source(source_b, config)
    except (FileNotFoundError, ValueError) as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    diff = schema_diff(rel_a, rel_b, name_a=name_a, name_b=name_b)

    if output_format == "json":
        import json
        console.print(json.dumps(diff.to_dict(), indent=2))
        return

    if diff.is_identical:
        console.print(
            f"[green][OK] Schemas are identical[/green] "
            f"({len(diff.common)} columns, "
            f"{diff.name_a} vs {diff.name_b})"
        )
        return

    # Added columns
    if diff.added:
        tbl = Table(title=f"[green]Added in {diff.name_b}[/green]",
                    box=box.MINIMAL, header_style="bold green")
        tbl.add_column("Column")
        tbl.add_column("Type")
        for c in diff.added:
            tbl.add_row(f"[green]+[/green] {c['name']}", c["type"])
        console.print(tbl)

    # Removed columns
    if diff.removed:
        tbl = Table(title=f"[red]Removed from {diff.name_a}[/red]",
                    box=box.MINIMAL, header_style="bold red")
        tbl.add_column("Column")
        tbl.add_column("Type")
        for c in diff.removed:
            tbl.add_row(f"[red]-[/red] {c['name']}", c["type"])
        console.print(tbl)

    # Type changes
    if diff.changed:
        tbl = Table(title="[yellow]Type changes[/yellow]",
                    box=box.MINIMAL, header_style="bold yellow")
        tbl.add_column("Column")
        tbl.add_column(f"Type in {diff.name_a}")
        tbl.add_column(f"Type in {diff.name_b}")
        for c in diff.changed:
            tbl.add_row(
                f"[yellow]~[/yellow] {c.name}",
                f"[dim]{c.type_a}[/dim]",
                f"[yellow]{c.type_b}[/yellow]",
            )
        console.print(tbl)

    console.print(
        f"\n[dim]{len(diff.added)} added · "
        f"{len(diff.removed)} removed · "
        f"{len(diff.changed)} type changes · "
        f"{len(diff.common)} unchanged[/dim]"
    )
    _ui_hint()


@cli.command("history")
@click.option("--search", "-s", "term", default=None,
              help="Filter history by SQL substring.")
@click.option("--limit", "-n", default=20, show_default=True,
              help="Maximum entries to display.")
@click.option("--source", default=None,
              help="Source key or name to show history for (UI mode only).")
def cmd_history(term: Optional[str], limit: int, source: Optional[str]) -> None:
    """Show recent query history, optionally filtered by search term.

    When nybble serve is running, history is fetched from the UI's /api/history
    endpoint. Pass --source to scope to a specific source.

    \b
    Examples:
      nybble history
      nybble history --search orders
      nybble history --limit 50
      nybble history --source skincare_data
    """
    # UI-first: fetch from the serve UI when running
    if _ui_running():
        key = _find_ui_source_key(source) if source else None
        if key:
            entries = _serve_get(f"/api/history/{key}")
        else:
            # Aggregate history across all ready sources
            sources_data = _serve_get("/api/files") or []
            entries = []
            seen = set()
            for rec in (sources_data if isinstance(sources_data, list) else []):
                if rec.get("status") == "ready":
                    h = _serve_get(f"/api/history/{rec['key']}") or []
                    for e in (h if isinstance(h, list) else []):
                        sig = (e.get("sql", ""), e.get("ts", ""))
                        if sig not in seen:
                            seen.add(sig)
                            entries.append(e)
        if isinstance(entries, list) and entries:
            entries = entries[:limit]
            if term:
                entries = [e for e in entries if term.lower() in e.get("sql", "").lower()]
            tbl = Table(box=box.MINIMAL, header_style="bold cyan",
                        title=f"Query history{' · ' + source if source else ''}")
            tbl.add_column("#", justify="right", style="dim", width=4)
            tbl.add_column("Timestamp", style="cyan", no_wrap=True)
            tbl.add_column("Rows", justify="right")
            tbl.add_column("ms", justify="right")
            tbl.add_column("SQL", overflow="fold", max_width=60)
            for i, e in enumerate(entries, 1):
                raw_ts = e.get("ts", "")
                # UI endpoint returns unix timestamp (float); local history returns ISO string
                if isinstance(raw_ts, (int, float)):
                    from datetime import datetime, timezone
                    ts = datetime.fromtimestamp(raw_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    ts = str(raw_ts)[:19].replace("T", " ")
                tbl.add_row(str(i), ts, str(e.get("row_count", e.get("rows", ""))),
                            f"{e.get('elapsed_ms', 0):.0f}", e.get("sql", "")[:120])
            console.print(tbl)
            _ui_hint(f"/query/{key}" if key else "")
            return
        elif isinstance(entries, list):
            console.print("[dim](no history)[/dim]")
            return

    # Fallback: local DuckDB history
    from nybble.arena import search_history

    entries = search_history(term=term, limit=limit)
    if not entries:
        console.print("[dim](no history)[/dim]")
        return

    tbl = Table(box=box.MINIMAL, header_style="bold cyan")
    tbl.add_column("#", justify="right", style="dim", width=4)
    tbl.add_column("Timestamp", style="cyan", no_wrap=True)
    tbl.add_column("Rows", justify="right")
    tbl.add_column("ms", justify="right")
    tbl.add_column("SQL", overflow="fold", max_width=60)

    for i, e in enumerate(entries, 1):
        ts = e.get("ts", "")[:19].replace("T", " ")
        tbl.add_row(
            str(i),
            ts,
            str(e.get("rows", "")),
            f"{e.get('elapsed_ms', 0):.0f}",
            e.get("sql", "")[:120],
        )

    console.print(tbl)
    _ui_hint()


@cli.command("report")
@_CONFIG_OPTION
@click.option("--source", "-s", default=None,
              help="Data source path or connection string.")
@click.option("--output", "-o", default="report.md", show_default=True,
              help="Output file (.md or .html).")
@click.option("--no-sample-rows", is_flag=True, default=False,
              help="Omit sample data section.")
@click.option("--sample-limit", default=5, show_default=True,
              help="Maximum sample rows per table.")
@click.option("--no-fk-queries", is_flag=True, default=False,
              help="Omit auto-generated FK JOIN queries section.")
@click.option("--no-schema", is_flag=True, default=False,
              help="Omit schema tables section.")
def cmd_report(
    config: Optional[str],
    source: Optional[str],
    output: str,
    no_sample_rows: bool,
    sample_limit: int,
    no_fk_queries: bool,
    no_schema: bool,
) -> None:
    """Generate a Markdown/HTML data report: schema, samples, FK joins.

    \b
    Examples:
      nybble report --source orders.duckdb --output report.md
      nybble report --source mydb.sqlite --output docs/schema.html
      nybble report --source data.csv --no-sample-rows
    """
    import sys as _sys
    from pathlib import Path
    from nybble.report import build_report

    _load_config_sources(config)

    # Resolve source: CLI flag → env var → registered relations fallback
    resolved_source: Optional[str] = source
    if resolved_source is None:
        import os
        resolved_source = os.environ.get("NYBBLE_SOURCE")
    if resolved_source is None:
        err_console.print("[red]Error:[/red] --source is required (or set NYBBLE_SOURCE).")
        raise SystemExit(1)

    try:
        report = build_report(
            resolved_source,
            output=output,
            include_sample_rows=not no_sample_rows,
            sample_limit=sample_limit,
            include_fk_queries=not no_fk_queries,
            include_schema=not no_schema,
        )
    except Exception as exc:
        err_console.print(f"[red]Error building report:[/red] {exc}")
        raise SystemExit(1)

    if not isinstance(report, __import__("nybble.report", fromlist=["NybbleReport"]).NybbleReport):
        # Legacy path returned a string - should not happen with str source
        err_console.print("[red]Unexpected report type.[/red]")
        raise SystemExit(1)

    saved = report.save(output)
    console.print(f"Report written to {saved}")
    _ui_hint()


@cli.command("serve")
@click.option("--port", "-p", default=7700, show_default=True,
              help="Port to listen on (1-65535).")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Host to bind to. Use 0.0.0.0 to expose on the network.")
@_CONFIG_OPTION
@click.option("--source", "-s", multiple=True,
              help="Pre-load a source before starting the server. May be repeated. "
                   "Use name=path to assign a custom table name.")
@click.option("--no-browser", is_flag=True, default=False,
              help="Do not open the browser automatically.")
def cmd_serve(
    port: int,
    host: str,
    config: Optional[str],
    source: tuple[str, ...],
    no_browser: bool,
) -> None:
    """Start the Nybble web UI (Flask) without Docker.

    Uses Waitress when installed (pip install "datajoins-nybble[serve]"),
    falls back to Flask's built-in dev server automatically.

    \b
    Examples:
      nybble serve
      nybble serve --port 8080 --source orders.csv --source customers.parquet
      nybble serve --config nybble.yaml --no-browser
      nybble serve --host 0.0.0.0 --port 7700
    """
    from nybble.serve import serve_app, validate_port

    try:
        port = validate_port(port)
    except ValueError as exc:
        console.print(f"[red]Invalid port:[/red] {exc}")
        raise SystemExit(1)

    serve_app(
        host=host,
        port=port,
        sources=source,
        config_path=config,
        open_browser=not no_browser,
    )


@cli.command("share")
@_CONFIG_OPTION
@click.option("--source", "-s", default=None,
              help="Data source path, connection string, or pre-rendered HTML file.")
@click.option("--backend", "-b", default="gist",
              type=click.Choice(["gist", "datajoins"], case_sensitive=False),
              show_default=True,
              help="Sharing backend.")
@click.option("--output", "-o", default="erd",
              type=click.Choice(["erd", "kg"], case_sensitive=False),
              show_default=True,
              help="Visualisation to share when source is a data file.")
@click.option("--gist-token", default=None, envvar="GITHUB_TOKEN",
              help="GitHub personal access token (also read from GITHUB_TOKEN env var).")
@click.option("--endpoint", default=None, envvar="NYBBLE_SHARE_ENDPOINT",
              help="DataJoins share endpoint URL.")
@click.option("--description", default="Nybble shared export", show_default=True,
              help="Human-readable description for the shared resource.")
@click.option("--expires-hours", default=None, type=int,
              help="TTL in hours (DataJoins backend only). Omit for permanent.")
def cmd_share(
    config: Optional[str],
    source: Optional[str],
    backend: str,
    output: str,
    gist_token: Optional[str],
    endpoint: Optional[str],
    description: str,
    expires_hours: Optional[int],
) -> None:
    """Share an ERD or Knowledge Graph as a public URL.

    Pushes the self-contained HTML output to a temporary hosted URL using
    GitHub Gist (default) or the DataJoins share endpoint.

    \b
    Examples:
      nybble share --source orders.csv
      nybble share --source mydb.sqlite --backend datajoins --output kg
      nybble share --source erd.html           # share a pre-rendered file
      nybble share --source orders.csv --backend gist --gist-token ghp_...
    """
    import sys as _sys
    from nybble.share import share as _share

    _load_config_sources(config)

    resolved_source: Optional[str] = source
    if resolved_source is None:
        import os as _os
        resolved_source = _os.environ.get("NYBBLE_SOURCE")
    if resolved_source is None:
        err_console.print(
            "[red]Error:[/red] --source is required (or set NYBBLE_SOURCE).",
        )
        raise SystemExit(1)

    share_kwargs: dict = dict(
        backend=backend,
        output=output,
        description=description,
    )
    if gist_token:
        share_kwargs["gist_token"] = gist_token
    if endpoint:
        share_kwargs["endpoint"] = endpoint
    if expires_hours is not None:
        share_kwargs["expires_hours"] = expires_hours

    try:
        result = _share(resolved_source, **share_kwargs)
    except Exception as exc:
        err_console.print(f"[red]Share failed:[/red] {exc}")
        raise SystemExit(1)

    console.print(result.url)


@cli.command("lineage")
@_CONFIG_OPTION
@click.option("--source", "-s", multiple=True,
              help="Pre-load a source. May be repeated.")
@click.option("--queries", default=None, metavar="PATH",
              help="Path to saved queries JSON (default: .nybble/queries.json).")
@click.option("--output", "-o", default=None, metavar="PATH",
              help="Output HTML path (default: .nybble/lineage.html).")
@click.option("--dialect", default=None,
              help="sqlglot dialect for SQL parsing (e.g. bigquery, snowflake).")
@click.option("--open", "open_browser", is_flag=True, default=False,
              help="Open the rendered HTML in the default browser after saving.")
def cmd_lineage(
    config: Optional[str],
    source: tuple[str, ...],
    queries: Optional[str],
    output: Optional[str],
    dialect: Optional[str],
    open_browser: bool,
) -> None:
    """Build an SQL query lineage DAG and render it as an HTML graph.

    Reads saved queries from .nybble/queries.json, parses each SQL with
    sqlglot to extract referenced tables, and renders a DAG showing which
    source tables feed into which queries.  When sources are provided (via
    --source or nybble.yaml), table cards show real column data.

    \b
    Examples:
      nybble lineage
      nybble lineage --source mydb.sqlite --open
      nybble lineage --config nybble.yaml --output docs/lineage.html
      nybble lineage --queries custom/queries.json --dialect bigquery
    """
    import sys as _sys
    from pathlib import Path
    from nybble.lineage import save_lineage_html, build_lineage_dag

    # Load data sources so that _load_schema_from_session() can build column
    # metadata for the table cards in the lineage ERD.
    _load_config_sources(config)
    if source:
        _load_sources(source)

    queries_path = Path(queries) if queries else Path(".nybble") / "queries.json"
    output_path = Path(output) if output else Path(".nybble") / "lineage.html"

    if not queries_path.exists():
        err_console.print(
            f"[red]Error:[/red] Queries file not found: {queries_path}\n"
            "Save a query first with:  nybble query \"SELECT ...\" --save <name>",
        )
        raise SystemExit(1)

    try:
        dag = build_lineage_dag(queries_path=queries_path, dialect=dialect)
        saved = save_lineage_html(
            output_path=output_path,
            queries_path=queries_path,
            dialect=dialect,
        )
    except ImportError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)
    except Exception as exc:
        err_console.print(f"[red]Error building lineage:[/red] {exc}")
        raise SystemExit(1)

    node_count = len(dag.get("nodes", []))
    edge_count = len(dag.get("edges", []))
    console.print(
        f"Lineage DAG written to {saved} "
        f"({node_count} nodes, {edge_count} edges)"
    )
    _ui_hint()

    if open_browser:
        import webbrowser
        webbrowser.open(saved.as_uri())


@cli.command("repl")
@_CONFIG_OPTION
@click.option("--source", "-s", multiple=True,
              help="Pre-load a source before starting REPL. May be repeated.")
def cmd_repl(config: Optional[str], source: tuple[str, ...]) -> None:
    """Launch the interactive SQL REPL.

    All pre-loaded sources are available as virtual tables.

    \b
    REPL meta-commands:
      .exit / .quit      - exit
      .tables            - list loaded sources
      .save <name> <sql> - save a named query
      .run <name>        - execute a saved query
    """
    _load_config_sources(config)
    if source:
        _load_sources(source)

    from nybble.arena import repl
    repl()
