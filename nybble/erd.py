"""
erd.py - ERD HTML Generator (CLI adapter)

Generates a self-contained interactive HTML Entity-Relationship Diagram
using an embedded ELK-js layout engine (inlined as minified JS).

This module is the CLI adapter layer. The data-building logic lives in
nybble.core.erd_data.build_erd_data() and is shared with the Flask server.

Features:
  • Nodes are tables with column lists, PK/FK annotations, data-type labels
  • Edges are FK relationships with animated SVG particle flows
  • Deep-linkable ?highlight=<table> URL parameter
  • Iframe-embed mode via ?embed=1

Public API
----------
  erd(relations=None, output=None, open_browser=None) → Path
"""
from __future__ import annotations

import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import Optional, Union

from jinja2 import Environment, FileSystemLoader

from nybble.core.erd_data import build_erd_data
from nybble.core.schema import Column, ForeignKey, Schema, Table
from nybble.infer_fk import infer_fks
from nybble.resolver import _REGISTERED, Relation, session, _SESSION_LOCK

_TEMPLATES_DIR = Path(__file__).parent / "templates"
# Base directory for Nybble output files.
# Override with NYBBLE_DATA_DIR to fix cwd-relative paths in embedded contexts.
_DATA_DIR = Path(os.environ.get("NYBBLE_DATA_DIR", ".nybble"))
_DEFAULT_OUTPUT = _DATA_DIR / "erd.html"


def _get_schema(name: str) -> list:
    """Return column list as [{"name": str, "type": str}] for a DuckDB relation."""
    con = session()
    with _SESSION_LOCK:
        try:
            rows = con.execute(
                f'DESCRIBE SELECT * FROM "{name}" LIMIT 0'
            ).fetchall()
            return [{"name": r[0], "type": r[1]} for r in rows]
        except Exception:
            return []


def _get_row_count(name: str) -> int:
    con = session()
    with _SESSION_LOCK:
        try:
            return con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        except Exception:
            return 0


def _display_name(view_name: str) -> str:
    """Strip the ``prefix__`` portion from a multi-table view name.

    When a SQL dump or SQLite DB is loaded, DuckDB views are registered as
    ``<source_name>__<table_name>`` (e.g. ``dump__board``).  The ERD should
    show only the short table name.

    Examples
    --------
    ``"umami_backup_20261018_020002_sql__board"`` → ``"board"``
    ``"orders"`` → ``"orders"``  (single-table - unchanged)
    """
    if "__" in view_name:
        return view_name.split("__", 1)[1]
    return view_name


def _duckdb_schema_to_core(
    relations: list[Relation],
) -> tuple[Schema, dict[str, int], dict[str, dict]]:
    """Convert DuckDB-registered relations into a core.Schema + row-count map + table_meta.

    This is the CLI adapter that bridges the DuckDB world (Relation objects,
    live session) to the framework-agnostic nybble.core.schema.Schema dataclass
    consumed by build_erd_data().

    Display names strip the ``source__`` prefix produced by multi-table loaders
    (SQL dumps, SQLite DBs) so the ERD shows ``board`` not
    ``umami_backup_20261018_020002_sql__board``.

    Returns
    -------
    (Schema, row_counts, table_meta)
        Schema      - canonical schema dataclass with all tables and columns.
        row_counts  - {display_name: int} for annotating node sizes in the ERD.
        table_meta  - {display_name: {source_type, view_name}} adapter-specific extras.
    """
    tables = []
    row_counts: dict[str, int] = {}
    table_meta: dict[str, dict] = {}

    for rel in relations:
        display = _display_name(rel.name)
        duckdb_cols = _get_schema(rel.name)
        row_counts[display] = _get_row_count(rel.name)
        table_meta[display] = {
            "source_type": rel.source_type,
            "view_name": rel.name,    # keep the real DuckDB name for SQL snippets
        }

        core_cols = [
            Column(name=c["name"], data_type=c["type"])
            for c in duckdb_cols
        ]
        tables.append(Table(name=display, columns=core_cols))

    schema = Schema(name="duckdb_session", tables=tables)
    return schema, row_counts, table_meta


# Maximum FK tier to surface in the ERD visualisation.
# Tier 1 = declared constraints (DDL / PRAGMA / SQLAlchemy)
# Tier 2 = _id suffix heuristic (e.g. orders.customer_id → customers.id)
# Tier 3 = equality-join cardinality match  ← noise; skip
# Tier 4 = statistical column-value overlap  ← noise; skip
_MAX_FK_TIER_FOR_ERD = 2


def _erd_bundle_to_schema(erd_data: dict) -> list:
    """Translate the shared ERD bundle to the Flask SCHEMA list format.

    The shared build_erd_data() uses {"is_pk", "is_fk", "fk_ref": {...}} column
    keys and a separate "edges" array.  The Cytoscape renderer (both Flask and
    the CLI Jinja template) expects each column to carry "pk", "fk",
    "fk_target" ("RefTable.col") and derives edges from those per-column refs.

    Only FK references at tier ≤ _MAX_FK_TIER_FOR_ERD are surfaced.  Tier-3
    (equality-join cardinality) and tier-4 (statistical overlap) candidates
    generate too many spurious bidirectional edges and noise column annotations
    that make the ERD unreadable.  The Docker/Flask pipeline only ever has
    tier-1 FKs in its schema.json, so this aligns CLI output with the UI.

    Also copies view_name from table_meta into each table dict so the SQL
    snippet in the detail panel can reference the actual DuckDB view name.
    """
    tables = []
    for t in erd_data.get("tables", []):
        cols_out = []
        for c in t.get("columns", []):
            fk_ref = c.get("fk_ref") or {}
            # Only propagate FK annotation for high-confidence, low-tier inferences.
            tier = fk_ref.get("tier", 1) if fk_ref else 1
            use_fk = bool(fk_ref) and (tier <= _MAX_FK_TIER_FOR_ERD)
            fk_target = ""
            if use_fk:
                fk_target = f"{fk_ref['ref_table']}.{fk_ref['ref_col']}"
            cols_out.append({
                "name":      c["name"],
                "type":      c["type"],
                "pk":        c.get("is_pk", False),
                "fk":        use_fk,
                "not_null":  False,
                "fk_target": fk_target,
            })
        tbl: dict = {"name": t["name"], "columns": cols_out, "row_count": t.get("row_count", 0)}
        # Preserve adapter extras (view_name, source_type) for the detail panel
        for key in ("view_name", "source_type"):
            if key in t:
                tbl[key] = t[key]
        tables.append(tbl)
    return tables


def erd(
    relations: Optional[list[Union[str, Relation]]] = None,
    output: Optional[Union[str, Path]] = None,
    open_browser: Optional[bool] = None,
) -> Path:
    """Generate a self-contained interactive ERD HTML file.

    Parameters
    ----------
    relations:
        List of Relation objects or table name strings.
        Defaults to all registered relations.
    output:
        Output file path. Defaults to .nybble/erd.html.
    open_browser:
        Automatically open the HTML file in the default browser.
        Pass ``True`` or ``False`` to override explicitly.
        Defaults to ``True`` only when stdout is attached to a TTY
        (i.e. interactive terminal); ``False`` in headless/server contexts.

    Returns
    -------
    Path
        Path to the generated HTML file.
    """
    # Resolve effective open_browser: honour explicit caller override; fall back
    # to TTY detection so headless servers never accidentally launch a browser.
    if open_browser is None:
        open_browser = sys.stdout.isatty()

    # Resolve relations
    if relations is None:
        rels = list(_REGISTERED.values())
    else:
        rels = []
        for r in relations:
            if isinstance(r, str):
                if r in _REGISTERED:
                    rels.append(_REGISTERED[r])
                else:
                    from nybble.resolver import load
                    rels.append(load(r))
            else:
                rels.append(r)

    if not rels:
        raise ValueError("No relations loaded. Use nybble.load() first.")

    # Convert DuckDB relations to core.Schema (adapter layer)
    schema, row_counts, table_meta = _duckdb_schema_to_core(rels)

    # Infer FK relationships using display names so FK candidate table names
    # match the display-name Schema built by _duckdb_schema_to_core() above.
    # Without this, relations loaded from SQL dumps have raw DuckDB names
    # (e.g. "umami_backup__website") while the Schema uses "website" - causing
    # every FK candidate to be silently dropped by build_erd_data().
    fk_candidates = infer_fks(rels, display_name_fn=_display_name)

    # Build data payload using the shared core builder
    erd_data = build_erd_data(
        schema, fk_candidates, row_counts=row_counts, table_meta=table_meta
    )

    # Translate ERD bundle to Flask-compatible SCHEMA format
    # (pk/fk/fk_target keys expected by the Cytoscape renderer)
    schema_tables = _erd_bundle_to_schema(erd_data)

    # Render template (CLI-specific self-contained HTML using Cytoscape+ELK)
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=False)
    template = env.get_template("erd.html.jinja")
    html = template.render(
        schema_json=json.dumps(schema_tables, ensure_ascii=False),
        table_count=len(schema_tables),
        edge_count=len(erd_data["edges"]),
        source_name=schema.name,
    )

    # Write output
    out_path = Path(output) if output else _DEFAULT_OUTPUT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    if open_browser:
        webbrowser.open(out_path.resolve().as_uri())  # only reached when TTY or explicitly True

    return out_path
