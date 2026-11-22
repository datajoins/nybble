"""
api_server.py - Nybble  (by DataJoins, https://datajoins.com)
====================================
Single Flask application.  No iframe, no inner Flask-within-Flask, no Node.js.

Routes
------
  GET  /                              → selector UI (schema list + upload + connect)
  GET  /erd/<key>                     → full-page ERD viewer (also handles join keys)
  GET  /browse/<key>                  → table browser page
  GET  /query/<key>                   → SQL query editor page
  GET  /join/<join_key>               → SQL query editor for a cross-schema join
  GET  /report/<key>                  → report builder page
  GET  /report/<key>/<rid>            → render a saved report as HTML
  GET  /diff/<key_a>/<key_b>          → schema diff viewer (Phase 9)
  GET  /dashboard/<key>               → query dashboard tiles (Phase 10)
  POST /api/upload                    → upload a file → {key, name, status}
  POST /api/connect                   → live DB or URL connection → {key, name, status}
  POST /api/process/<key>             → run / re-run the pipeline
  DELETE /api/delete/<key>            → remove entry from registry + disk
  GET  /api/status/<key>              → poll pipeline status + recent log tail
  GET  /api/files                     → list all registered schemas
  GET  /api/log/<key>                 → full pipeline log as plain text (streaming)
  GET  /api/search                    → search table/column names across all schemas
  GET  /api/metadata/<key>            → get metadata sidecar for a schema
  PATCH /api/metadata/<key>           → merge-update metadata sidecar
  GET  /api/rows/<key>/<table>        → paginated row data from a table
  PATCH /api/rows/<key>/<table>/<rowid> → update a single cell (ALLOW_WRITES only)
  POST /api/rows/<key>/<table>        → insert a new row         (ALLOW_WRITES only)
  DELETE /api/rows/<key>/<table>/<rowid> → delete a row by rowid (ALLOW_WRITES only)
  POST /api/connect (type=mongodb)    → connect MongoDB via pymongo
  POST /api/connect (type=snowflake)  → connect Snowflake via snowflake-sqlalchemy
  POST /api/connect (type=bigquery)   → connect BigQuery via sqlalchemy-bigquery
  POST /api/connect (type=supabase)   → connect Supabase (PostgreSQL alias)
  POST /api/query/<key>               → execute a read-only SQL query
  POST /api/joins                     → create a cross-schema join entry
  GET  /api/joins                     → list all join entries
  DELETE /api/joins/<join_key>        → delete a join entry
  GET  /api/sqlite_size/<key>         → return {size_bytes} for the SQLite file
  GET  /api/reports/<key>             → list reports for a schema
  POST /api/reports/<key>             → create a new report
  PATCH /api/reports/<key>/<rid>      → update report title / blocks
  DELETE /api/reports/<key>/<rid>     → delete a report
  GET  /api/diff/<key_a>/<key_b>      → schema diff JSON (Phase 9)
  PATCH /api/queries/<key>/<qid>/tile → set/clear dashboard tile config (Phase 10)
  GET  /api/audit/<key>               → last 500 audit log entries (Rank 9)
  POST /api/preview                   → first 5 rows preview of an uploaded file (Rank 10)

Storage
-------
  /uploads/<key>/<filename>           raw uploaded files
  /output/erd/<key>/                  pipeline output (schema.json, schema.sql)
  /output/erd/<key>/metadata.json     metadata sidecar (description, tags, …)
  /output/erd/<key>/reports.json      saved reports (Phase 6)
  /output/registry.json               persistent file registry
"""
from __future__ import annotations

import hashlib
import json
import os
import re as _re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict

# ── Input-validation helpers (Item 4 - connection-field safety) ───────────────
# Patterns mirror Databasement's SafeHost / SafeUsername / SafePath rules.
# Only characters valid in a hostname, IPv4, IPv6 bracket, or FQDN are allowed.
# Colons are required for IPv6 (e.g. [::1]) and port separators.
_SAFE_HOST_RE     = _re.compile(r'\A[A-Za-z0-9._:\[\]-]+\Z')
_SAFE_USERNAME_RE = _re.compile(r'\A[A-Za-z0-9._@\-]+\Z')
_SAFE_DBNAME_RE   = _re.compile(r'\A[A-Za-z0-9._\-/]+\Z')


def _validate_conn_field(value: str, pattern: _re.Pattern, field_name: str) -> None:
    """Raise ValueError if *value* is non-empty and doesn't match *pattern*.

    Empty values are allowed - callers already enforce required-field checks.
    """
    if not value:
        return
    if not pattern.match(value):
        raise ValueError(
            f"Invalid {field_name}: contains characters not allowed in a "
            f"database connection string (matched against {pattern.pattern})"
        )

from flask import Flask, Response, jsonify, make_response, render_template, request, stream_with_context

# ── Paths ─────────────────────────────────────────────────────────────────────
UPLOAD_DIR  = Path(os.environ.get("UPLOAD_DIR",  "/uploads"))
OUTPUT_DIR  = Path(os.environ.get("OUTPUT_DIR",  "/output/erd"))
REGISTRY    = Path(os.environ.get("REGISTRY",    "/output/registry.json"))
PIPELINE    = Path(os.environ.get("PIPELINE",    "/app/run_pipeline.py"))

# Pipeline timeout (seconds).  Override via env PIPELINE_TIMEOUT.
PIPELINE_TIMEOUT = int(os.environ.get("PIPELINE_TIMEOUT", "120"))

# Per-query execution timeout (seconds).  Override via env QUERY_TIMEOUT_SECONDS.
QUERY_TIMEOUT_SECONDS = int(os.environ.get("QUERY_TIMEOUT_SECONDS", "30"))

# Write operations gate.  Set ALLOW_WRITES=1 to enable INSERT / UPDATE / DELETE
# via the browse UI.  Default off - the system is read-only by default.
ALLOW_WRITES = os.environ.get("ALLOW_WRITES", "0").strip() in ("1", "true", "yes", "on")

# BASE_PATH for reverse-proxy sub-path deployments.  E.g. BASE_PATH=/sqlviewer
# All routes will be available under that prefix.  Default "" = no prefix.
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")

# ── Docker host resolution ────────────────────────────────────────────────────
def _resolve_conn_url(conn_url: str) -> str:
    """Rewrite localhost/127.0.0.1 → host.docker.internal when inside Docker.

    When the ERD system runs inside a Docker container, ``localhost`` resolves
    to the container itself, not the Docker host.  Databases running on the
    host (or in sibling containers exposed on the host) are reachable via the
    special DNS name ``host.docker.internal``.

    The rewrite is only applied when a ``/.dockerenv`` sentinel file is
    present (i.e. we are actually inside a Docker container).  Outside Docker
    the connection string is returned unchanged so local development is
    unaffected.
    """
    import re as _re
    if not Path("/.dockerenv").exists():
        return conn_url
    # Replace @localhost: and @127.0.0.1: in the authority portion only.
    return _re.sub(
        r"(@)(localhost|127\.0\.0\.1)(:\d+/|/)",
        r"\1host.docker.internal\3",
        conn_url,
    )

# Create runtime directories only when the process has permission to do so.
# In CI and unit-test environments the default paths (/uploads, /output/erd)
# are Docker-only and unwritable; the mkdir is a no-op in those contexts.
for _d in (UPLOAD_DIR, OUTPUT_DIR, REGISTRY.parent):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

# ── Flask app ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
app = Flask(__name__, template_folder=str(_HERE / "templates"))
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
app.config["TEMPLATES_AUTO_RELOAD"] = True   # always reload templates from disk
# If BASE_PATH is set, tell Flask's url_for() and redirects to honour it.
if BASE_PATH:
    app.config["APPLICATION_ROOT"] = BASE_PATH


# ── Jinja2 custom filter: minimal Markdown → HTML ────────────────────────────
import re as _re

def md_to_html(text: str) -> str:
    """Convert a subset of Markdown to HTML for report text blocks.

    Supports: headings (#-######, with or without trailing space), bold (**),
    italic (*), inline code (`), fenced code blocks (```), blockquotes (>),
    unordered lists (- / *), ordered lists (1.), horizontal rules (---),
    GFM pipe tables (| col | col | with |---|---| separator row), paragraphs.
    No external dependencies - pure Python re.
    """
    if not text:
        return ""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    in_code = False
    code_buf: list[str] = []
    in_list: str | None = None   # "ul" or "ol"
    # Table accumulator: list of raw lines forming a pending table block
    table_buf: list[str] = []

    def _escape(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _inline(s: str) -> str:
        # Split on backtick spans so their content is preserved verbatim.
        # For each non-code segment: HTML-escape first, then apply bold/italic.
        parts = _re.split(r'(`[^`]+`)', s)
        result = []
        for part in parts:
            if part.startswith('`') and part.endswith('`') and len(part) > 1:
                inner = _escape(part[1:-1])
                result.append(f'<code>{inner}</code>')
            else:
                esc = _escape(part)
                esc = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', esc)
                esc = _re.sub(r'\*(.+?)\*',     r'<em>\1</em>',         esc)
                result.append(esc)
        return ''.join(result)

    def _flush_list():
        nonlocal in_list
        if in_list:
            out.append(f'</{in_list}>')
            in_list = None

    def _is_table_sep(line: str) -> bool:
        """True if line is a GFM table separator row like | --- | :---: | ---: |"""
        stripped = line.strip()
        if not stripped.startswith('|') and not stripped.endswith('|'):
            return False
        cells = [c.strip() for c in stripped.strip('|').split('|')]
        return bool(cells) and all(_re.match(r'^:?-{1,}:?\s*$', c) for c in cells if c)

    def _flush_table():
        nonlocal table_buf
        if len(table_buf) < 2:
            # Not a real table - emit as paragraphs
            for ln in table_buf:
                out.append(f'<p>{_inline(ln)}</p>')
            table_buf = []
            return
        # Validate: second line must be a separator row
        if not _is_table_sep(table_buf[1]):
            for ln in table_buf:
                out.append(f'<p>{_inline(ln)}</p>')
            table_buf = []
            return
        # Parse alignment from separator row
        sep_cells = [c.strip() for c in table_buf[1].strip().strip('|').split('|')]
        aligns = []
        for c in sep_cells:
            c = c.strip()
            if c.startswith(':') and c.endswith(':'):
                aligns.append('center')
            elif c.endswith(':'):
                aligns.append('right')
            else:
                aligns.append('left')

        def _row_cells(line: str) -> list[str]:
            return [c.strip() for c in line.strip().strip('|').split('|')]

        # Header row
        hcells = _row_cells(table_buf[0])
        thead = '<thead><tr>' + ''.join(
            f'<th style="text-align:{aligns[i] if i < len(aligns) else "left"}">{_inline(c)}</th>'
            for i, c in enumerate(hcells)
        ) + '</tr></thead>'
        # Body rows (skip separator at index 1)
        tbody_rows = []
        for ln in table_buf[2:]:
            cells = _row_cells(ln)
            tbody_rows.append(
                '<tr>' + ''.join(
                    f'<td style="text-align:{aligns[i] if i < len(aligns) else "left"}">{_inline(c)}</td>'
                    for i, c in enumerate(cells)
                ) + '</tr>'
            )
        tbody = '<tbody>' + ''.join(tbody_rows) + '</tbody>' if tbody_rows else ''
        out.append(f'<table class="md-table">{thead}{tbody}</table>')
        table_buf = []

    def _is_table_line(line: str) -> bool:
        return line.strip().startswith('|')

    for line in lines:
        # ── Inside a fenced code block ────────────────────────────────────────
        if line.startswith("```"):
            if in_code:
                out.append('<pre><code>' + '\n'.join(code_buf) + '</code></pre>')
                code_buf = []
                in_code = False
            else:
                _flush_table()
                _flush_list()
                in_code = True
            continue
        if in_code:
            code_buf.append(_escape(line))
            continue

        # ── Table line? ───────────────────────────────────────────────────────
        if _is_table_line(line):
            # Flush any open list/paragraphs before starting a table
            if not table_buf:
                _flush_list()
            table_buf.append(line)
            continue
        else:
            # Non-table line - flush any accumulated table first
            if table_buf:
                _flush_table()

        # ── Heading (1-6 # chars, optional trailing space before text) ───────
        hm = _re.match(r'^(#{1,6})\s?(.*)', line)
        if hm and hm.group(1):
            level = len(hm.group(1))
            content = hm.group(2).strip()
            # Only treat as heading if the line starts with # and nothing else
            # before the #. Disambiguate: bare '###' with no content is still h3.
            _flush_list()
            out.append(f'<h{level}>{_inline(content)}</h{level}>')
            continue

        # ── HR ────────────────────────────────────────────────────────────────
        if _re.match(r'^[-*_]{3,}\s*$', line):
            _flush_list(); out.append('<hr/>'); continue

        # ── Blockquote ────────────────────────────────────────────────────────
        if line.startswith('> '):
            _flush_list()
            out.append(f'<blockquote>{_inline(line[2:])}</blockquote>')
            continue

        # ── Unordered list ────────────────────────────────────────────────────
        m = _re.match(r'^[-*]\s+(.*)', line)
        if m:
            if in_list != 'ul':
                _flush_list()
                out.append('<ul>')
                in_list = 'ul'
            out.append(f'<li>{_inline(m.group(1))}</li>')
            continue

        # ── Ordered list ──────────────────────────────────────────────────────
        m2 = _re.match(r'^\d+\.\s+(.*)', line)
        if m2:
            if in_list != 'ol':
                _flush_list()
                out.append('<ol>')
                in_list = 'ol'
            out.append(f'<li>{_inline(m2.group(1))}</li>')
            continue

        # ── Blank line ────────────────────────────────────────────────────────
        if not line.strip():
            _flush_list()
            out.append('')
            continue

        # ── Regular paragraph ─────────────────────────────────────────────────
        _flush_list()
        out.append(f'<p>{_inline(line)}</p>')

    # Flush any trailing open blocks
    _flush_table()
    _flush_list()
    if in_code and code_buf:
        out.append('<pre><code>' + '\n'.join(code_buf) + '</code></pre>')

    return '\n'.join(out)


app.jinja_env.filters['md_to_html'] = md_to_html

# ── Registry helpers ──────────────────────────────────────────────────────────
_lock: threading.Lock = threading.Lock()
_live_logs: Dict[str, list] = {}


def _load_reg() -> Dict[str, Any]:
    if REGISTRY.exists():
        try:
            return json.loads(REGISTRY.read_text())
        except Exception:
            pass
    return {}


def _save_reg(reg: Dict[str, Any]) -> None:
    REGISTRY.write_text(json.dumps(reg, indent=2))


# ── Metadata sidecar helpers ──────────────────────────────────────────────────

def _meta_path(key: str) -> Path:
    """Return path to the metadata sidecar for a schema key."""
    return OUTPUT_DIR / key / "metadata.json"


# ── Rank 12: Metadata sidecar in-memory cache (60-second TTL) ────────────────
_METADATA_CACHE: "dict[str, tuple[float, dict]]" = {}


def _load_meta(key: str) -> dict:
    now = time.time()
    if key in _METADATA_CACHE:
        ts, data = _METADATA_CACHE[key]
        if now - ts < 60:
            return data
    p = _meta_path(key)
    if p.exists():
        try:
            result = json.loads(p.read_text(encoding="utf-8"))
            _METADATA_CACHE[key] = (now, result)
            return result
        except Exception:
            pass
    default = {
        "description": "", "tags": [], "tables": {}, "columns": {},
        "models": [], "saved_queries": [], "canned_queries": [],
    }
    _METADATA_CACHE[key] = (now, default)
    return default


def _save_meta(key: str, meta: dict) -> None:
    _meta_path(key).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    # Invalidate cache on write
    _METADATA_CACHE.pop(key, None)


def _ts() -> str:
    """Return a compact ISO-like timestamp string for log lines."""
    return time.strftime("%H:%M:%S")


# Flat-file extensions that flat_file_to_sqlite can convert to SQLite on-demand.
_FLAT_FILE_EXTS: dict[str, str] = {
    ".xlsx":   "xlsx",
    ".xls":    "xlsx",
    ".csv":    "csv",
    ".json":   "json",
    ".jsonl":  "jsonl",
    ".ndjson": "jsonl",
    ".parquet": "parquet",
}


def _find_sqlite_db(key: str) -> Path | None:
    """Locate the queryable SQLite database for *key*.

    Search order:
      1. Uploaded SQLite file in UPLOAD_DIR/<key>/   (.db / .sqlite / .sqlite3)
      2. Imported SQLite file in OUTPUT_DIR/<key>/   (.db)   ← Phase 4a: SQL dump import
      3. Flat-file upload (xlsx/csv/json/jsonl/parquet) in UPLOAD_DIR/<key>/ - if the
         pipeline-generated .db is absent, convert the flat file to SQLite on-demand
         and write it to OUTPUT_DIR/<key>/ so subsequent calls find it immediately.
    Returns None if no database is found.
    """
    # 1. Uploaded SQLite file
    upload_dir = UPLOAD_DIR / key
    if upload_dir.exists():
        for f in sorted(upload_dir.iterdir()):
            if f.suffix.lower() in (".db", ".sqlite", ".sqlite3"):
                return f
    # 2. Imported SQLite file produced by the pipeline (SQL dump → SQLite)
    output_dir = OUTPUT_DIR / key
    if output_dir.exists():
        for f in sorted(output_dir.iterdir()):
            if f.suffix.lower() == ".db":
                return f
    # 3. Flat-file upload present but no .db yet - generate on-demand
    if upload_dir.exists():
        for f in sorted(upload_dir.iterdir()):
            fmt = _FLAT_FILE_EXTS.get(f.suffix.lower())
            if fmt:
                try:
                    from server.ingestion.flat_file_to_sqlite import load_flat_file
                    db_path = load_flat_file(f, output_dir, fmt)
                    if db_path and db_path.exists():
                        return db_path
                except Exception:
                    pass
    return None


# ── Schema → ERD data ─────────────────────────────────────────────────────────

def _erd_bundle_to_flask_tables(erd_data: dict) -> list:
    """Translate the shared ERD bundle format to the shape expected by erd.html.

    The shared build_erd_data() uses {"is_pk", "is_fk", "fk_ref": {...}}
    column keys.  The Flask ERD template (server/app/templates/erd.html) expects
    {"pk", "fk", "not_null", "fk_target": "RefTable.col"} column keys.

    This adapter is the ONLY place that performs this translation.
    No business logic here - pure field renaming.
    """
    tables = []
    for t in erd_data.get("tables", []):
        cols_out = []
        for c in t.get("columns", []):
            fk_ref = c.get("fk_ref") or {}
            fk_target = ""
            if fk_ref:
                fk_target = f"{fk_ref['ref_table']}.{fk_ref['ref_col']}"
            cols_out.append({
                "name":      c["name"],
                "type":      c["type"],
                "pk":        c.get("is_pk", False),
                "fk":        c.get("is_fk", False),
                "not_null":  False,   # schema.json DDL path has notNull; ERD bundle does not carry it
                "fk_target": fk_target,
            })
        tables.append({"name": t["name"], "columns": cols_out})
    return tables


def _schema_to_erd_data(schema_json_path: Path) -> list | None:
    """Load schema.json and convert to flat list for the ERD template JS.

    Delegates to nybble.core.erd_data.build_erd_data() for the data-building
    logic, then translates the result to the shape expected by erd.html.

    Handles both the Liam keyed-object format and the older array format.
    Returns None if the file doesn't exist or can't be parsed.

    Security note: this is a pure function - no Flask context, no HTTP access.
    The route handler (erd_view) is the correct auth-insertion point if/when
    authentication is added; this function must remain framework-agnostic.
    """
    if not schema_json_path.exists():
        return None
    try:
        raw = json.loads(schema_json_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    from nybble.core.schema import Column, ForeignKey, Schema, Table
    from nybble.core.erd_data import build_erd_data

    # ── Liam keyed-object format ──────────────────────────────────────────────
    # { "tables": { "tableName": { "columns": {colName:{...}}, "constraints":{...} } } }
    if isinstance(raw, dict) and isinstance(raw.get("tables"), dict):
        tables = []
        for tname, tdata in raw["tables"].items():
            pk_col_names = []
            fk_constraints = []
            for c in tdata.get("constraints", {}).values():
                if c.get("type") == "PRIMARY KEY":
                    pk_col_names.extend(c.get("columnNames", []))
                elif c.get("type") == "FOREIGN KEY":
                    fk_constraints.append(ForeignKey(
                        constrained_columns=c.get("columnNames", []),
                        referred_table=c.get("targetTableName", ""),
                        referred_columns=c.get("targetColumnNames", []),
                    ))
            cols = [
                Column(
                    name=cname,
                    data_type=cdata.get("type", "TEXT"),
                    nullable=not cdata.get("notNull", False),
                    primary_key=(cname in pk_col_names),
                )
                for cname, cdata in tdata.get("columns", {}).items()
            ]
            tables.append(Table(
                name=tname,
                columns=cols,
                primary_keys=pk_col_names,
                foreign_keys=fk_constraints,
            ))

        schema = Schema(name=schema_json_path.parent.name, tables=tables)
        erd_data = build_erd_data(schema, fk_candidates=[])
        return _erd_bundle_to_flask_tables(erd_data)

    # ── Old array format ──────────────────────────────────────────────────────
    # { "tables": [ { "name", "columns": [...] }, ... ], "edges": [...] }
    if isinstance(raw, dict) and isinstance(raw.get("tables"), list):
        # Build a lightweight Schema from the legacy format then use the shared builder
        fk_map2: dict[str, dict] = {}
        for edge in raw.get("edges", []):
            src_t = edge.get("source_table", "") or edge.get("source", "")
            src_c = edge.get("source_col", "")
            tgt_t = edge.get("target_table", "") or edge.get("target", "")
            tgt_c = edge.get("target_col", "")
            if src_t and src_c:
                fk_map2.setdefault(src_t, {})[src_c] = (tgt_t, tgt_c)

        tables = []
        for t in raw["tables"]:
            tname = t["name"]
            t_fk = fk_map2.get(tname, {})
            fk_constraints = [
                ForeignKey(constrained_columns=[sc], referred_table=tt, referred_columns=[tc])
                for sc, (tt, tc) in t_fk.items()
            ]
            cols = [
                Column(
                    name=c["name"],
                    data_type=c.get("type", "TEXT"),
                    nullable=not c.get("not_null", False),
                    primary_key=c.get("pk", False),
                )
                for c in t.get("columns", [])
            ]
            tables.append(Table(name=tname, columns=cols, foreign_keys=fk_constraints))

        schema = Schema(name=schema_json_path.parent.name, tables=tables)
        erd_data = build_erd_data(schema, fk_candidates=[])
        return _erd_bundle_to_flask_tables(erd_data)

    return None


def _merge_join_schema(record: dict, reg: dict) -> list[dict]:
    """Return merged flat table list for a join entry.

    Each member's tables are prefixed as ``alias.tablename`` so they can be
    distinguished in the ERD and query sidebar.  The shape of each dict matches
    the output of :func:`_schema_to_erd_data`.

    FK edges are only preserved when both the source and the referenced table
    belong to the **same member schema** (same alias).  Cross-schema FK
    references are suppressed (fk set to False, fk_target cleared) so the ERD
    never draws edges between tables from different schemas.
    """
    merged: list[dict] = []
    for m in record.get("members", []):
        alias       = m.get("alias", m["key"])
        schema_path = OUTPUT_DIR / m["key"] / "schema.json"
        tables      = _schema_to_erd_data(schema_path) or []

        # Build a set of original (un-prefixed) table names for THIS member only.
        own_tables: set[str] = {t["name"] for t in tables}

        for t in tables:
            new_cols = []
            for c in t.get("columns", []):
                nc = dict(c)
                if nc.get("fk") and nc.get("fk_target"):
                    dot     = nc["fk_target"].find(".")
                    ref_tbl = nc["fk_target"][:dot] if dot >= 0 else nc["fk_target"]
                    ref_col = nc["fk_target"][dot + 1:] if dot >= 0 else ""

                    if ref_tbl in own_tables:
                        # Same-schema FK - rewrite target to alias-prefixed form
                        nc["fk_target"] = f"{alias}.{ref_tbl}.{ref_col}"
                    else:
                        # Cross-schema FK - suppress the edge entirely
                        nc["fk"]        = False
                        nc["fk_target"] = ""
                new_cols.append(nc)
            prefixed            = dict(t)
            prefixed["name"]    = f"{alias}.{t['name']}"
            prefixed["alias"]   = alias
            prefixed["columns"] = new_cols
            merged.append(prefixed)
    return merged


# ── Routes ────────────────────────────────────────────────────────────────────

def _fmt_size(n_bytes: int | None) -> str:
    """Return a human-readable file-size string, e.g. '1.4 MB'."""
    if n_bytes is None:
        return ""
    v = float(n_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(v)} B"
            return f"{v:.1f} {unit}"
        v /= 1024
    return ""  # unreachable


def _record_file_size(record: dict) -> int | None:
    """Return the byte size of the primary source file for a registry record.

    For file/url sources: the uploaded raw file stored in UPLOAD_DIR.
    For connstring sources: None (no local file).
    For join sources: None (virtual, no file).
    """
    source = record.get("source", "file")
    if source in ("connstring", "join"):
        return None
    path = record.get("path")
    if path:
        p = Path(path)
        if p.exists():
            return p.stat().st_size
    # Fallback: check UPLOAD_DIR/<key> for any file
    key = record.get("key", "")
    if key:
        udir = UPLOAD_DIR / key
        if udir.exists():
            for f in sorted(udir.iterdir()):
                if f.is_file():
                    return f.stat().st_size
    return None


@app.route("/")
def index():
    """Selector UI - file list + upload + connect."""
    with _lock:
        reg = _load_reg()
    files = sorted(reg.values(), key=lambda r: r.get("uploaded", 0), reverse=True)
    # Annotate each record with a human-readable file size
    for f in files:
        raw = _record_file_size(f)
        f["file_size_bytes"] = raw
        f["file_size"] = _fmt_size(raw)
    return render_template("index.html", files=files)


@app.route("/erd/<key>")
def erd_view(key: str):
    """Full-page ERD viewer for a given schema key (also handles join keys).

    Optional query params:
      ?tables=A,B,C  - restrict diagram to these tables plus their direct FK
                       neighbours (used by the lineage pane in query.html)
      ?embedded=1    - strip topbar/sidebar chrome for iframe embedding
    """
    with _lock:
        reg = _load_reg()
    record = reg.get(key)
    if not record:
        return "Schema not found", 404

    # Join keys use the merged schema instead of a single schema.json
    if record.get("source") == "join":
        all_tables = _merge_join_schema(record, reg)
    else:
        schema_path = OUTPUT_DIR / key / "schema.json"
        all_tables  = _schema_to_erd_data(schema_path) or []

    # Optional table filter for embedded lineage view
    tables_param = request.args.get("tables", "").strip()
    embedded     = request.args.get("embedded", "0") in ("1", "true", "yes")

    if tables_param:
        requested = {t.strip().lower() for t in tables_param.split(",") if t.strip()}
        # Build set: requested tables + any table that shares an FK with them.
        # fk_target is "RefTable.col" for plain schemas and "alias.RefTable.col"
        # for join schemas - use lastIndexOf to extract the table name correctly.
        included = set(requested)
        for t in all_tables:
            tlo = t["name"].lower()
            for col in t.get("columns", []):
                if col.get("fk") and col.get("fk_target"):
                    ft = col["fk_target"]
                    last_dot = ft.rfind(".")
                    ref_tbl  = ft[:last_dot].lower() if last_dot >= 0 else ft.lower()
                    if tlo in requested or ref_tbl in requested:
                        included.add(tlo)
                        included.add(ref_tbl)
        tables = [t for t in all_tables if t["name"].lower() in included]
    else:
        tables = all_tables

    fk_count    = sum(1 for t in tables for c in t["columns"] if c["fk"])
    schema_json = json.dumps(tables, ensure_ascii=False)

    resp = make_response(render_template(
        "erd.html",
        key         = key,
        name        = record.get("name", key),
        table_count = len(tables),
        fk_count    = fk_count,
        schema_json = schema_json,
        embedded    = embedded,
    ))
    # Allow the ERD page to be embedded in iframes on same-origin pages.
    # Required because /query/ is served with COEP: require-corp (for DuckDB-WASM
    # SharedArrayBuffer), which blocks subresources that lack a CORP header.
    # Setting same-origin here satisfies the COEP requirement in all deployment
    # modes (nginx proxy, direct Flask, Docker) without relying on nginx alone.
    resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    return resp


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/files")
def api_files():
    with _lock:
        reg = _load_reg()
    return jsonify(list(reg.values()))


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    raw = f.read()
    if not raw:
        return jsonify({"error": "Empty file"}), 400

    key  = hashlib.sha256(raw).hexdigest()[:16]
    name = Path(f.filename).name

    with _lock:
        reg = _load_reg()
        if key in reg:
            existing = reg[key]
            return jsonify({
                "key":    key,
                "name":   existing["name"],
                "status": existing["status"],
                "cached": True,
            })
        dest = UPLOAD_DIR / key
        dest.mkdir(parents=True, exist_ok=True)
        fp = dest / name
        fp.write_bytes(raw)
        reg[key] = {
            "key": key, "name": name, "path": str(fp),
            "source": "file",
            "status": "uploaded", "tables": 0, "fks": 0,
            "log": [], "uploaded": time.time(), "processed": None,
            "error_msg": None,
        }
        _save_reg(reg)

    return jsonify({"key": key, "name": name, "status": "uploaded", "cached": False})


@app.route("/api/connect", methods=["POST"])
def api_connect():
    """Accept a connection string / URL / structured form and run introspection.

    Body (JSON):
        { "type": "connstring"|"url"|"form"|"libsql",
          "connstring": "postgresql://...",   # for type=connstring
          "url": "https://example.com/dump.sql.gz",  # for type=url
          "host": "...", "port": 5432, "user": "...", "password": "...",
          "database": "...", "engine": "postgresql"|"mysql"|"sqlite"|"mssql"|"clickhouse"  # for type=form
          "libsql_url": "libsql://...", "auth_token": "..."  # for type=libsql
        }
    """
    data = request.get_json(force=True, silent=True) or {}
    conn_type = data.get("type", "connstring")

    if conn_type == "url":
        return _handle_url_connect(data)
    elif conn_type == "form":
        return _handle_form_connect(data)
    elif conn_type == "libsql":
        return _handle_libsql_connect(data)
    elif conn_type == "mongodb":
        return _handle_mongodb_connect(data)
    elif conn_type == "snowflake":
        return _handle_snowflake_connect(data)
    elif conn_type == "bigquery":
        return _handle_bigquery_connect(data)
    elif conn_type == "supabase":
        return _handle_supabase_connect(data)
    elif conn_type == "restapi":
        return _handle_restapi_connect(data)
    else:
        return _handle_connstring_connect(data)


def _handle_connstring_connect(data: dict):
    """Queue a live-DB introspection job from a raw connection string."""
    conn_url = (data.get("connstring") or "").strip()
    if not conn_url:
        return jsonify({"error": "connstring is required"}), 400

    # Derive display name from URL (callers may override via data["_name"])
    import re
    m = re.search(r"/([^/?#:@]+)(?:[?#].*)?$", conn_url)
    name = data.get("_name") or (m.group(1) if m else "live_db")

    key = hashlib.sha256(conn_url.encode()).hexdigest()[:16]

    # Allow callers (e.g. _handle_form_connect) to tag a specific source
    source_override = data.get("_source_override", "connstring")

    with _lock:
        reg = _load_reg()
        if key in reg and reg[key]["status"] in ("ready", "processing"):
            return jsonify({"key": key, "name": reg[key]["name"], "status": reg[key]["status"], "cached": True})
        reg[key] = {
            "key": key, "name": name, "path": None,
            "source": source_override, "conn_url": conn_url,
            "status": "uploaded", "tables": 0, "fks": 0,
            "log": [], "uploaded": time.time(), "processed": None,
            "error_msg": None,
        }
        _save_reg(reg)

    return jsonify({"key": key, "name": name, "status": "uploaded", "cached": False})


def _handle_form_connect(data: dict):
    """Build a connection string from form fields and queue introspection."""
    engine   = data.get("engine", "postgresql").lower()
    host     = str(data.get("host", "localhost") or "localhost")
    port     = data.get("port") or _default_port(engine)
    user     = str(data.get("user", "") or "")
    password = data.get("password", "")
    database = str(data.get("database", "") or "")

    if not database:
        return jsonify({"error": "database name is required"}), 400

    # Validate connection fields against safe-character patterns.
    # Prevents connection-string injection via DSN delimiter characters.
    try:
        _validate_conn_field(host,     _SAFE_HOST_RE,     "host")
        _validate_conn_field(user,     _SAFE_USERNAME_RE, "user")
        _validate_conn_field(database, _SAFE_DBNAME_RE,   "database")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if engine == "sqlite":
        conn_url = f"sqlite:///{database}"
    elif engine == "mysql":
        conn_url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}"
    elif engine == "mssql":
        import urllib.parse as _up
        conn_url = (
            f"mssql+pyodbc://{_up.quote(user)}:{_up.quote(password)}"
            f"@{host}:{port}/{database}"
            "?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
        )
    elif engine == "clickhouse":
        conn_url = f"clickhouse+native://{user}:{password}@{host}:{port}/{database}"
    else:  # postgresql default
        conn_url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"

    # For ClickHouse, tag the source explicitly so the pipeline uses the
    # native introspection path instead of the SQLAlchemy generic path.
    if engine == "clickhouse":
        data["_source_override"] = "clickhouse"

    # Delegate to connstring handler
    data["connstring"] = conn_url
    data["type"] = "connstring"
    return _handle_connstring_connect(data)


def _handle_libsql_connect(data: dict):
    """Queue a libSQL / Turso remote introspection job.

    Body: {"type": "libsql", "libsql_url": "libsql://...", "auth_token": "..."}
    The libsql_url may use the libsql:// scheme or https://.
    """
    libsql_url = (data.get("libsql_url") or "").strip()
    auth_token = (data.get("auth_token") or "").strip()
    if not libsql_url:
        return jsonify({"error": "libsql_url is required"}), 400

    import re
    m = re.search(r"//([^/?#]+)", libsql_url)
    name = m.group(1) if m else "libsql_db"

    key = hashlib.sha256((libsql_url + auth_token).encode()).hexdigest()[:16]

    with _lock:
        reg = _load_reg()
        if key in reg and reg[key]["status"] in ("ready", "processing"):
            return jsonify({"key": key, "name": reg[key]["name"], "status": reg[key]["status"], "cached": True})
        reg[key] = {
            "key": key, "name": name, "path": None,
            "source": "libsql",
            "libsql_url": libsql_url,
            "auth_token": auth_token,
            "status": "uploaded", "tables": 0, "fks": 0,
            "log": [], "uploaded": time.time(), "processed": None,
            "error_msg": None,
        }
        _save_reg(reg)

    return jsonify({"key": key, "name": name, "status": "uploaded", "cached": False})


def _handle_url_connect(data: dict):
    """Download a remote SQL/schema file and process it as a file upload."""
    raw_input = (data.get("url") or "").strip()
    if not raw_input:
        return jsonify({"error": "url is required"}), 400

    # ── Clean the URL input ───────────────────────────────────────────────
    # Users sometimes paste "<url> <filename>" (e.g. copy-pasted from a
    # browser download dialog or a curl command).  Extract just the URL part:
    # the first whitespace-delimited token that starts with http:// or https://
    import re as _re_url
    url_tokens = raw_input.split()
    url = ""
    for tok in url_tokens:
        if tok.startswith("http://") or tok.startswith("https://"):
            url = tok
            break
    if not url:
        # Fallback: use the full input if no http(s) prefix found
        url = url_tokens[0] if url_tokens else ""
    if not url:
        return jsonify({"error": "url is required"}), 400

    # Derive filename from URL path (handles foo.sql.gz, foo.sql, etc.)
    from urllib.parse import urlparse
    path_part = urlparse(url).path
    filename  = Path(path_part).name or "remote_schema.sql"
    # Strip any query-string auth tokens that got appended to the filename
    if "?" in filename:
        filename = filename.split("?")[0]
    if not filename:
        filename = "remote_schema.sql"

    # Download to memory (max 200 MB)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Nybble/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            status_code = getattr(resp, "status", 200)
            content_type = (resp.headers.get("Content-Type") or "").lower()

            # Read up to 200 MB
            raw = resp.read(200 * 1024 * 1024)

    except urllib.error.HTTPError as exc:
        status = exc.code
        if status == 401:
            return jsonify({"error": (
                f"HTTP 401 Unauthorised - the URL requires authentication. "
                f"Download the file manually and use File Upload instead."
            )}), 400
        if status == 403:
            return jsonify({"error": (
                f"HTTP 403 Forbidden - access denied. "
                f"If this is a Google Cloud Storage or S3 URL, the bucket is private. "
                f"Generate a signed/pre-signed URL or download the file locally and use File Upload."
            )}), 400
        if status == 404:
            return jsonify({"error": f"HTTP 404 - file not found at that URL."}), 400
        return jsonify({"error": f"Download failed: HTTP {status} {exc.reason}"}), 400
    except urllib.error.URLError as exc:
        return jsonify({"error": f"Download failed: {exc.reason}"}), 400
    except Exception as exc:
        return jsonify({"error": f"Download failed: {exc}"}), 400

    if not raw:
        return jsonify({"error": "Downloaded file is empty"}), 400

    # ── Detect and reject auth-error HTML/XML responses served as 200 OK ────
    # GCS, S3, and similar CDNs sometimes return an HTML or XML error page with
    # HTTP 200 when a bucket is public but the object requires credentials.
    _head = raw.lstrip(b" \t\r\n")
    _is_html = _head[:5] in (b"<!DOC", b"<!doc", b"<html", b"<HTML")
    _is_xml  = _head[:5] == b"<?xml" or _head[:4] == b"<Err"  # AWS/GCS XML error
    if _is_html or _is_xml:
        # Try to extract a human-readable message from the XML error body
        _msg = ""
        try:
            import re as _re
            m = _re.search(rb"<Message>([^<]{1,200})</Message>", raw, _re.IGNORECASE)
            if m:
                _msg = " - " + m.group(1).decode("utf-8", errors="replace")
            m2 = _re.search(rb"<Code>([^<]{1,80})</Code>", raw, _re.IGNORECASE)
            if m2:
                _msg = " (" + m2.group(1).decode("utf-8", errors="replace") + ")" + _msg
        except Exception:
            pass
        return jsonify({"error": (
            f"The URL returned an HTML/XML error page instead of a SQL file{_msg}. "
            f"This usually means the file is not publicly accessible. "
            f"For Google Cloud Storage or S3 private buckets, generate a signed URL "
            f"(valid for a few hours) or download the file and use File Upload."
        )}), 400

    # ── Content-type sanity check for clearly wrong MIME types ──────────────
    if content_type and any(t in content_type for t in ("text/html", "application/xhtml")):
        return jsonify({"error": (
            f"The server returned Content-Type: {content_type.split(';')[0].strip()} - "
            f"expected a SQL dump or compressed file. "
            f"The URL may point to a login page or redirect."
        )}), 400

    key  = hashlib.sha256(raw).hexdigest()[:16]
    name = filename

    with _lock:
        reg = _load_reg()
        if key in reg and reg[key]["status"] in ("ready", "processing"):
            return jsonify({"key": key, "name": name, "status": reg[key]["status"], "cached": True})
        dest = UPLOAD_DIR / key
        dest.mkdir(parents=True, exist_ok=True)
        fp = dest / name
        fp.write_bytes(raw)
        reg[key] = {
            "key": key, "name": name, "path": str(fp),
            "source": "url", "source_url": url,
            "status": "uploaded", "tables": 0, "fks": 0,
            "log": [], "uploaded": time.time(), "processed": None,
            "error_msg": None,
        }
        _save_reg(reg)

    return jsonify({"key": key, "name": name, "status": "uploaded", "cached": False})


def _default_port(engine: str) -> int:
    return {
        "postgresql": 5432,
        "mysql":      3306,
        "mariadb":    3306,
        "mssql":      1433,
        "clickhouse": 9000,
        "mongodb":    27017,
        "snowflake":  443,
        "bigquery":   443,
        "supabase":   5432,
    }.get(engine, 5432)


# ── Phase Q: New connector handlers ──────────────────────────────────────────

def _handle_mongodb_connect(data: dict):
    """Queue a MongoDB introspection job.

    Body: {"type": "mongodb", "uri": "mongodb://...",
           "database": "<db_name>", "sample_size": 100}
    The URI defaults to mongodb://localhost:27017 if omitted.
    """
    uri      = (data.get("uri") or "mongodb://localhost:27017").strip()
    database = (data.get("database") or "").strip()
    if not database:
        return jsonify({"error": "database name is required"}), 400

    import re as _re
    name = database

    key = hashlib.sha256((uri + database).encode()).hexdigest()[:16]

    with _lock:
        reg = _load_reg()
        if key in reg and reg[key]["status"] in ("ready", "processing"):
            return jsonify({"key": key, "name": reg[key]["name"], "status": reg[key]["status"], "cached": True})
        reg[key] = {
            "key": key, "name": name, "path": None,
            "source": "mongodb",
            "mongo_uri": uri,
            "mongo_database": database,
            "mongo_sample_size": int(data.get("sample_size") or 100),
            "status": "uploaded", "tables": 0, "fks": 0,
            "log": [], "uploaded": time.time(), "processed": None,
            "error_msg": None,
        }
        _save_reg(reg)

    return jsonify({"key": key, "name": name, "status": "uploaded", "cached": False})


def _handle_snowflake_connect(data: dict):
    """Queue a Snowflake introspection job via snowflake-sqlalchemy.

    Body: {"type": "snowflake", "account": "xyz.us-east-1",
           "user": "...", "password": "...",
           "database": "...", "schema": "PUBLIC", "warehouse": "..."}
    Constructs a snowflake:// SQLAlchemy URL and delegates to the live-DB path.
    """
    account   = (data.get("account") or "").strip()
    user      = (data.get("user")     or "").strip()
    password  = (data.get("password") or "").strip()
    database  = (data.get("database") or "").strip()
    schema    = (data.get("schema")   or "PUBLIC").strip()
    warehouse = (data.get("warehouse") or "").strip()

    if not account or not database:
        return jsonify({"error": "account and database are required"}), 400

    import urllib.parse as _up
    wh_part = f"?warehouse={_up.quote(warehouse)}" if warehouse else ""
    conn_url = (
        f"snowflake://{_up.quote(user)}:{_up.quote(password)}"
        f"@{_up.quote(account)}/{_up.quote(database)}/{_up.quote(schema)}{wh_part}"
    )
    data["connstring"] = conn_url
    data["_source_override"] = "snowflake"
    data["_name"] = f"{database}.{schema}"
    return _handle_connstring_connect(data)


def _handle_bigquery_connect(data: dict):
    """Queue a BigQuery introspection job via sqlalchemy-bigquery.

    Body: {"type": "bigquery", "project": "my-project",
           "dataset": "my_dataset",
           "credentials_json": "<path-or-inline-JSON>"}
    Constructs a bigquery:// SQLAlchemy URL and delegates to the live-DB path.
    credentials_json may be a file path or the raw JSON content of a service-
    account key file.  If omitted, Application Default Credentials are used.
    """
    project  = (data.get("project")  or "").strip()
    dataset  = (data.get("dataset")  or "").strip()
    creds    = (data.get("credentials_json") or "").strip()

    if not project or not dataset:
        return jsonify({"error": "project and dataset are required"}), 400

    conn_url = f"bigquery://{project}/{dataset}"

    # If credentials_json is raw JSON content, write it to a temp file so
    # sqlalchemy-bigquery can find it via the GOOGLE_APPLICATION_CREDENTIALS env var.
    if creds and creds.strip().startswith("{"):
        import tempfile as _tf
        tmp_creds = _tf.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
            dir=str(OUTPUT_DIR), encoding="utf-8",
        )
        tmp_creds.write(creds)
        tmp_creds.close()
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp_creds.name
    elif creds:
        # Treat as a file path
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds

    data["connstring"] = conn_url
    data["_source_override"] = "bigquery"
    data["_name"] = f"{project}.{dataset}"
    return _handle_connstring_connect(data)


def _handle_supabase_connect(data: dict):
    """Queue a Supabase introspection job.

    Supabase exposes standard PostgreSQL on port 5432.
    Body: {"type": "supabase", "project_ref": "abcxyz",
           "password": "...", "region": "us-east-1"}
    Builds the standard Supabase connection string and delegates to the
    PostgreSQL (connstring) path tagged with source=supabase.
    """
    project_ref = (data.get("project_ref") or "").strip()
    password    = (data.get("password")    or "").strip()
    region      = (data.get("region")      or "us-east-1").strip()

    if not project_ref or not password:
        return jsonify({"error": "project_ref and password are required"}), 400

    import urllib.parse as _up
    host = f"db.{project_ref}.supabase.co"
    conn_url = (
        f"postgresql://postgres:{_up.quote(password)}"
        f"@{host}:5432/postgres"
    )
    name = f"{project_ref}.supabase"
    data["connstring"] = conn_url
    data["_source_override"] = "supabase"
    return _handle_connstring_connect(data)


def _flatten_json(obj: Any, prefix: str = "") -> dict:
    """Recursively flatten a nested dict/list into dot-notation key/value pairs.

    Lists of primitives are JSON-serialised to a single string value.
    Lists of objects are not flattened (caller handles those separately).
    """
    out: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            full_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                out.update(_flatten_json(v, full_key))
            elif isinstance(v, list):
                # Store non-object lists as compact JSON strings
                out[full_key] = json.dumps(v, ensure_ascii=False)
            else:
                out[full_key] = v
    else:
        out[prefix] = obj
    return out


def _handle_restapi_connect(data: dict):
    """Fetch a REST JSON API endpoint, flatten the response, and ingest as JSONL.

    Body JSON:
      {
        "type":        "restapi",
        "url":         "https://api.example.com/v1/items",   ← required
        "data_path":   "items",    ← dot-path into response JSON to the records array
                                    (empty = use root if it is an array, else wrap root)
        "headers":     {"Authorization": "Bearer <token>"},  ← optional extra headers
        "name":        "My API"    ← optional display name
      }

    The handler:
      1. Fetches the URL (GET, max 50 MB).
      2. Walks `data_path` inside the JSON to find the records array.
      3. Flattens each record with _flatten_json() to produce flat row dicts.
      4. Writes them as JSONL to UPLOAD_DIR/<key>/api_data.jsonl.
      5. Registers the key in the registry and returns {key, name, status}.

    No new dependencies: uses urllib.request (stdlib) and json (stdlib).
    The existing JSONL → SQLite pipeline converts the JSONL to a queryable .db.
    """
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"error": "url must start with http:// or https://"}), 400

    data_path  = (data.get("data_path") or "").strip()
    extra_hdrs = data.get("headers") or {}
    name       = (data.get("name") or "").strip()

    # ── Fetch ────────────────────────────────────────────────────────────────
    try:
        req_hdrs = {"User-Agent": "Nybble/1.0", "Accept": "application/json"}
        if isinstance(extra_hdrs, dict):
            req_hdrs.update(extra_hdrs)
        req = urllib.request.Request(url, headers=req_hdrs)
        with urllib.request.urlopen(req, timeout=30) as resp:
            status_code  = getattr(resp, "status", 200)
            content_type = (resp.headers.get("Content-Type") or "").lower()
            raw_bytes    = resp.read(50 * 1024 * 1024)   # max 50 MB
    except urllib.error.HTTPError as exc:
        return jsonify({"error": f"HTTP {exc.code} fetching API: {exc.reason}"}), 400
    except urllib.error.URLError as exc:
        return jsonify({"error": f"Network error fetching API: {exc.reason}"}), 400
    except Exception as exc:
        return jsonify({"error": f"Error fetching API: {exc}"}), 400

    if not raw_bytes:
        return jsonify({"error": "API returned an empty response"}), 400

    # ── Parse JSON ───────────────────────────────────────────────────────────
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        snippet = raw_bytes[:200].decode("utf-8", errors="replace")
        return jsonify({"error": f"API response is not valid JSON: {exc} - got: {snippet}"}), 400

    # ── Locate the records array ─────────────────────────────────────────────
    records_obj = payload
    if data_path:
        for part in data_path.split("."):
            if isinstance(records_obj, dict) and part in records_obj:
                records_obj = records_obj[part]
            else:
                return jsonify({
                    "error": f"data_path '{data_path}' not found in API response. "
                             f"Top-level keys are: {list(payload.keys()) if isinstance(payload, dict) else 'root is not an object'}"
                }), 400

    # Normalise: wrap a single object in a list; handle root-level array
    if isinstance(records_obj, dict):
        records_obj = [records_obj]
    elif not isinstance(records_obj, list):
        return jsonify({
            "error": f"Expected a JSON array at data_path='{data_path}' but got {type(records_obj).__name__}. "
                     f"Set data_path to the key that holds the array of records."
        }), 400

    if not records_obj:
        return jsonify({"error": "API returned an empty array - nothing to import"}), 400

    # ── Flatten records ──────────────────────────────────────────────────────
    flat_rows = [_flatten_json(r) for r in records_obj if isinstance(r, (dict, list, int, float, str))]

    # ── Derive display name ──────────────────────────────────────────────────
    from urllib.parse import urlparse
    parsed_url = urlparse(url)
    if not name:
        # Use hostname + path tail (e.g. "api.example.com/matches")
        path_tail = parsed_url.path.rstrip("/").split("/")[-1] or "api"
        name = f"{parsed_url.netloc.split('.')[0]}.{path_tail}"

    # ── Derive a stable key from URL + data_path ────────────────────────────
    key_src = f"{url}||{data_path}".encode("utf-8")
    key     = hashlib.sha256(key_src).hexdigest()[:16]

    # ── Check cache ──────────────────────────────────────────────────────────
    with _lock:
        reg = _load_reg()
        if key in reg and reg[key]["status"] in ("ready", "processing"):
            return jsonify({"key": key, "name": reg[key]["name"], "status": reg[key]["status"], "cached": True})

    # ── Write JSONL to upload directory ──────────────────────────────────────
    dest = UPLOAD_DIR / key
    dest.mkdir(parents=True, exist_ok=True)
    jsonl_path = dest / "api_data.jsonl"
    try:
        lines = [json.dumps(row, ensure_ascii=False) for row in flat_rows]
        jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:
        return jsonify({"error": f"Failed to write JSONL: {exc}"}), 500

    # ── Register in registry ─────────────────────────────────────────────────
    with _lock:
        reg = _load_reg()
        reg[key] = {
            "key":       key,
            "name":      name,
            "path":      str(jsonl_path),
            "source":    "file",
            "status":    "uploaded",
            "tables":    0,
            "fks":       0,
            "log":       [],
            "uploaded":  time.time(),
            "processed": None,
            "error_msg": None,
            # Extra metadata for display
            "api_url":        url,
            "api_data_path":  data_path,
            "api_row_count":  len(flat_rows),
        }
        _save_reg(reg)

    return jsonify({
        "key":    key,
        "name":   name,
        "status": "uploaded",
        "cached": False,
        "rows":   len(flat_rows),
    })


@app.route("/api/process/<key>", methods=["POST"])
def api_process(key: str):
    with _lock:
        reg = _load_reg()
        if key not in reg:
            return jsonify({"error": "Unknown key"}), 404
        if reg[key]["status"] == "processing":
            return jsonify({"key": key, "status": "processing"})
        reg[key]["status"]    = "processing"
        reg[key]["log"]       = []
        reg[key]["error_msg"] = None
        _live_logs[key] = []
        _save_reg(reg)

    threading.Thread(target=_run_pipeline, args=(key,), daemon=True).start()
    return jsonify({"key": key, "status": "processing"})


@app.route("/api/registry/<key>", methods=["PATCH"])
def api_registry_patch(key: str):
    """Update mutable fields in a registry record.

    Currently supports updating ``name`` only.  Connection strings and source
    type are immutable (the key is derived from them).

    Body: {"name": "<new display name>"}
    """
    data = request.get_json(force=True, silent=True) or {}
    new_name = (data.get("name") or "").strip()
    if not new_name:
        return jsonify({"error": "name is required"}), 400
    with _lock:
        reg = _load_reg()
        if key not in reg:
            return jsonify({"error": "Unknown key"}), 404
        reg[key]["name"] = new_name
        _save_reg(reg)
    return jsonify({"key": key, "name": new_name})


@app.route("/api/delete/<key>", methods=["DELETE"])
def api_delete(key: str):
    """Remove a registry entry and all associated files from disk."""
    with _lock:
        reg = _load_reg()
        if key not in reg:
            return jsonify({"error": "Unknown key"}), 404
        record = reg.pop(key)
        _save_reg(reg)
        _live_logs.pop(key, None)

    # Remove uploaded source file
    upload_dir = UPLOAD_DIR / key
    if upload_dir.exists():
        shutil.rmtree(upload_dir, ignore_errors=True)

    # Remove processed output
    erd_dir = OUTPUT_DIR / key
    if erd_dir.exists():
        shutil.rmtree(erd_dir, ignore_errors=True)

    return jsonify({"key": key, "deleted": True})


@app.route("/api/status/<key>")
def api_status(key: str):
    with _lock:
        reg = _load_reg()
        if key not in reg:
            return jsonify({"error": "Unknown key"}), 404
        rec = dict(reg[key])
        if key in _live_logs:
            rec["log"] = _live_logs[key][-100:]  # last 100 lines only in status
    rec["query_timeout_seconds"] = QUERY_TIMEOUT_SECONDS
    return jsonify(rec)


@app.route("/api/log/<key>")
def api_log(key: str):
    """Stream the full pipeline log for a key as plain text.

    Supports ?stream=1 for a chunked streaming response (EventSource-compatible
    plain text lines) while the pipeline is still running.
    """
    streaming = request.args.get("stream") == "1"

    with _lock:
        reg = _load_reg()
        if key not in reg:
            return Response("Unknown key\n", status=404, mimetype="text/plain")

    if not streaming:
        with _lock:
            lines = _live_logs.get(key) or reg.get(key, {}).get("log", [])
        return Response("\n".join(lines) + "\n", mimetype="text/plain")

    # Streaming: yield lines as they arrive (Server-Sent Events style text)
    def _generate():
        sent = 0
        for _ in range(600):  # max ~60 s at 0.1 s intervals
            with _lock:
                lines = _live_logs.get(key) or []
            while sent < len(lines):
                yield lines[sent] + "\n"
                sent += 1
            with _lock:
                reg2 = _load_reg()
                status = reg2.get(key, {}).get("status", "")
            if status not in ("processing", "uploaded"):
                # Pipeline finished - yield any remaining saved lines
                with _lock:
                    final = reg2.get(key, {}).get("log", [])
                while sent < len(final):
                    yield final[sent] + "\n"
                    sent += 1
                break
            time.sleep(0.1)

    return Response(stream_with_context(_generate()), mimetype="text/plain")


# ── New API routes (Phase 0 / 1) ──────────────────────────────────────────────

@app.route("/api/metadata/<key>", methods=["GET", "PATCH"])
def api_metadata(key: str):
    """GET or PATCH the metadata sidecar for a schema."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    if request.method == "GET":
        return jsonify(_load_meta(key))
    # PATCH - deep-merge provided fields
    data = request.get_json(force=True, silent=True) or {}
    with _lock:
        meta = _load_meta(key)
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(meta.get(k), dict):
                meta[k].update(v)
            else:
                meta[k] = v
        _save_meta(key, meta)
    return jsonify(meta)


@app.route("/api/search")
def api_search():
    """Search table and column names across all registered schemas."""
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify([])
    with _lock:
        reg = _load_reg()
    results = []
    for key, record in reg.items():
        if record.get("status") != "ready":
            continue
        schema_path = OUTPUT_DIR / key / "schema.json"
        if not schema_path.exists():
            continue
        try:
            raw = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        tables = raw.get("tables", {})
        if isinstance(tables, dict):
            for tname, tdata in tables.items():
                if q in tname.lower():
                    results.append({
                        "type": "table", "key": key,
                        "schema_name": record.get("name", key),
                        "table": tname,
                    })
                for cname in (tdata.get("columns") or {}).keys():
                    if q in cname.lower():
                        results.append({
                            "type": "column", "key": key,
                            "schema_name": record.get("name", key),
                            "table": tname, "column": cname,
                        })
        if len(results) >= 100:
            break
    return jsonify(results[:100])


def _parse_filter_params(args, valid_cols):
    """Parse ?col__op=val query params into (where_clauses, bind_params) for SQLAlchemy.

    Multiple col__contains params with the *same* value (FTS across all columns)
    are grouped into a single ``(col1 LIKE :p OR col2 LIKE :p …)`` clause so that
    a row matching *any* column is returned (OR semantics).
    """
    OP_MAP = {
        "exact": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
        "contains": "LIKE",
    }
    clauses, params = [], {}

    # Collect __contains by search value to enable FTS OR grouping
    contains_by_val: dict[str, list[str]] = {}  # val → [col, …]
    non_contains_items = []

    for k, vals_list in args.lists():
        if "__" not in k:
            continue
        col, op = k.rsplit("__", 1)
        if col not in valid_cols:
            continue
        if op == "contains" and vals_list:
            contains_by_val.setdefault(vals_list[0], []).append(col)
        else:
            non_contains_items.append((col, op, vals_list))

    # Build OR clause for each unique FTS search value
    for fts_idx, (fts_val, cols) in enumerate(contains_by_val.items()):
        param_key = f"p_fts_{fts_idx}"
        params[param_key] = f"%{fts_val}%"
        if len(cols) == 1:
            clauses.append(f'"{cols[0]}" LIKE :{param_key}')
        else:
            parts = " OR ".join(f'"{c}" LIKE :{param_key}' for c in cols)
            clauses.append(f"({parts})")

    # Process remaining (non-contains) filters
    for col, op, vals_list in non_contains_items:
        if op == "in" and vals_list:
            for i, v in enumerate(vals_list):
                pk = f"p_{col}_in_{i}"
                params[pk] = v
            placeholders = ", ".join(f":p_{col}_in_{i}" for i in range(len(vals_list)))
            clauses.append(f'"{col}" IN ({placeholders})')
        elif op in OP_MAP:
            v = vals_list[0]
            param_key = f"p_{col}_{op}"
            params[param_key] = f"%{v}%" if op == "contains" else v
            clauses.append(f'"{col}" {OP_MAP[op]} :{param_key}')
    return clauses, params


def _parse_filter_params_sqlite(args, valid_cols):
    """Parse ?col__op=val query params into (where_clauses, bind_list) for sqlite3.

    Multiple col__contains params with the *same* value (FTS across all columns)
    are grouped into a single ``(col1 LIKE ? OR col2 LIKE ? …)`` clause so that
    a row matching *any* column is returned (OR semantics), not only rows that
    match *every* column (AND semantics).
    """
    OP_MAP = {
        "exact": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
        "contains": "LIKE",
    }
    clauses, vals = [], []

    # Collect all __contains entries keyed by their search value so we can
    # detect when the browser sends the same term against every column (FTS).
    contains_by_val: dict[str, list[str]] = {}  # val → [col, …]
    non_contains_items = []

    for k, vals_list in args.lists():
        if "__" not in k:
            continue
        col, op = k.rsplit("__", 1)
        if col not in valid_cols:
            continue
        if op == "contains" and vals_list:
            v = vals_list[0]
            contains_by_val.setdefault(v, []).append(col)
        else:
            non_contains_items.append((col, op, vals_list))

    # Build OR clause for each unique FTS search value
    for fts_val, cols in contains_by_val.items():
        if len(cols) == 1:
            # Single column - keep the simple AND clause
            clauses.append(f'"{cols[0]}" LIKE ?')
            vals.append(f"%{fts_val}%")
        else:
            # Multiple columns with the same value → OR across all of them
            parts = " OR ".join(f'"{c}" LIKE ?' for c in cols)
            clauses.append(f"({parts})")
            vals.extend(f"%{fts_val}%" for _ in cols)

    # Process remaining (non-contains) filters
    for col, op, vals_list in non_contains_items:
        if op == "in" and vals_list:
            placeholders = ",".join(["?"] * len(vals_list))
            clauses.append(f'"{col}" IN ({placeholders})')
            vals.extend(vals_list)
        elif op in OP_MAP:
            v = vals_list[0]
            vals.append(f"%{v}%" if op == "contains" else v)
            clauses.append(f'"{col}" {OP_MAP[op]} ?')
    return clauses, vals


def _safe_sort(sort_raw: str | None, allowed: list[str], default: str) -> tuple[str, str]:
    """Return (column, direction) validated against *allowed*.

    Falls back to (*default*, 'asc') for any missing, empty, or invalid input.
    Mirrors Databasement's ``Formatters::sortColumn()`` / ``sortDirection()``
    pattern - a single, centralised guard used by every route that accepts a
    sort parameter.

    Parameters
    ----------
    sort_raw:
        Raw sort string, e.g. ``"-name"`` (DESC) or ``"created_at"`` (ASC).
        A leading ``-`` signals descending order.
    allowed:
        Whitelist of column names accepted for this route.
    default:
        Fallback column when *sort_raw* is absent or invalid.

    Returns
    -------
    tuple[str, str]
        ``(column, direction)`` where direction is ``'asc'`` or ``'desc'``.
    """
    if not sort_raw:
        return default, "asc"
    raw = sort_raw.strip()
    desc = raw.startswith("-")
    col  = raw.lstrip("-").strip()
    if col not in allowed:
        col = default
    return col, "desc" if desc else "asc"


def _build_order_clause(sort_raw: str, col_names: list[str]) -> str:
    """Build an ORDER BY clause from a comma-separated _sort string.

    Supports single column (legacy): ``-col`` or ``col``
    Supports multi-column (Phase 2): ``col1,-col2,col3``
    Each token is validated against col_names; invalid tokens are skipped.
    Returns an empty string when no valid sort columns remain.
    """
    if not sort_raw:
        return ""
    parts = []
    for token in sort_raw.split(","):
        token = token.strip()
        if not token:
            continue
        desc = token.startswith("-")
        col  = token.lstrip("-").strip()
        if col and col in col_names:
            parts.append(f'"{col}" {"DESC" if desc else "ASC"}')
    return (" ORDER BY " + ", ".join(parts)) if parts else ""


@app.route("/api/rows/<key>/<table_name>")
def api_rows(key: str, table_name: str):
    """Return paginated rows from a table.

    Query params:
      page_size   int  (default 50, max 500)
      page        int  (default 1, offset-based)
      _sort       str  column name(s) - comma-separated, prefix - for DESC
                       e.g. "name,-created_at"  (multi-column, Phase 2)
      _format     str  "json" (default) | "csv"
      col__op=val      filter params: op ∈ {exact, contains, gt, gte, lt, lte, neq}
    """
    import base64 as _base64
    import csv as _csv
    import io

    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    record = reg[key]

    source = record.get("source", "file")
    rowids_raw: list | None = None   # set only for file-backed schemas with ALLOW_WRITES
    try:
        if source == "connstring":
            conn_url = _resolve_conn_url(record.get("conn_url", ""))
            from sqlalchemy import create_engine, inspect as sa_inspect, text as sa_text
            engine = create_engine(conn_url, connect_args={"connect_timeout": 10})
            inspector = sa_inspect(engine)
            all_tables = inspector.get_table_names()
            if table_name not in all_tables:
                return jsonify({"error": "Table not found"}), 404
            page_size = min(int(request.args.get("page_size", 50)), 500)
            page = max(int(request.args.get("page", 1)), 1)
            offset = (page - 1) * page_size
            sort_raw = request.args.get("_sort", "")
            col_names = [c["name"] for c in inspector.get_columns(table_name)]
            order_clause = _build_order_clause(sort_raw, col_names)
            filters, bind_params = _parse_filter_params(request.args, col_names)
            where_clause = f" WHERE {' AND '.join(filters)}" if filters else ""
            sql = (
                f'SELECT * FROM "{table_name}"{where_clause}{order_clause}'
                f" LIMIT :lim OFFSET :off"
            )
            bind_params.update({"lim": page_size + 1, "off": offset})
            with engine.connect() as conn:
                result = conn.execute(sa_text(sql), bind_params)
                col_names_out = list(result.keys())
                rows_raw = result.fetchall()
            engine.dispose()
        elif source == "join":
            # Join browse: table_name is "alias.real_table" - route to the member's DB
            dot = table_name.find('.')
            if dot < 0:
                return jsonify({"error": "Table name must be 'alias.table' for join schemas"}), 422
            alias_part = table_name[:dot]
            real_table  = table_name[dot + 1:]
            with _lock:
                join_reg = _load_reg()
            join_record = join_reg.get(key, record)
            member_key = None
            for m in join_record.get("members", []):
                if m.get("alias") == alias_part:
                    member_key = m["key"]
                    break
            if not member_key:
                return jsonify({"error": f"No member with alias '{alias_part}'"}), 404
            db_file = _find_sqlite_db(member_key)
            if not db_file:
                return jsonify({"error": f"No queryable database for member '{alias_part}'"}), 422
            import sqlite3
            con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            tables_q = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            valid_tables = {r[0] for r in tables_q}
            if real_table not in valid_tables:
                con.close()
                return jsonify({"error": "Table not found"}), 404
            col_info = cur.execute(
                f'PRAGMA table_info("{real_table}")'
            ).fetchall()
            col_names = [c[1] for c in col_info]
            page_size = min(int(request.args.get("page_size", 50)), 500)
            page = max(int(request.args.get("page", 1)), 1)
            offset = (page - 1) * page_size
            sort_raw = request.args.get("_sort", "")
            order_clause = _build_order_clause(sort_raw, col_names)
            filters, bind_list = _parse_filter_params_sqlite(request.args, col_names)
            where_clause = f" WHERE {' AND '.join(filters)}" if filters else ""
            sql = (
                f'SELECT * FROM "{real_table}"{where_clause}{order_clause}'
                f" LIMIT ? OFFSET ?"
            )
            bind_list += [page_size + 1, offset]
            rows_raw = cur.execute(sql, bind_list).fetchall()
            col_names_out = col_names
            con.close()
        else:
            # File-based - find the SQLite database (uploaded or pipeline-imported)
            db_file = _find_sqlite_db(key)
            if not db_file:
                return jsonify({
                    "error": (
                        "No queryable source - the pipeline may still be processing. "
                        "Try clicking the ↺ Process button on the schema card to re-run."
                    )
                }), 422
            import sqlite3
            con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            tables_q = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            valid_tables = {r[0] for r in tables_q}
            if table_name not in valid_tables:
                con.close()
                return jsonify({"error": "Table not found"}), 404
            col_info = cur.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()
            col_names = [c[1] for c in col_info]
            page_size = min(int(request.args.get("page_size", 50)), 500)
            page = max(int(request.args.get("page", 1)), 1)
            offset = (page - 1) * page_size
            sort_raw = request.args.get("_sort", "")
            order_clause = _build_order_clause(sort_raw, col_names)
            filters, bind_list = _parse_filter_params_sqlite(request.args, col_names)
            where_clause = f" WHERE {' AND '.join(filters)}" if filters else ""
            # When writes are enabled, also fetch the SQLite rowid so the UI
            # can send targeted UPDATE / DELETE requests.
            select_prefix = "rowid, " if ALLOW_WRITES else ""
            sql = (
                f'SELECT {select_prefix}* FROM "{table_name}"{where_clause}{order_clause}'
                f" LIMIT ? OFFSET ?"
            )
            bind_list += [page_size + 1, offset]
            rows_raw = cur.execute(sql, bind_list).fetchall()
            col_names_out = col_names
            # Extract rowids from the first column when present
            if ALLOW_WRITES:
                rowids_raw  = [r[0] for r in rows_raw]
                rows_raw    = [r[1:] for r in rows_raw]
            else:
                rowids_raw = None
            con.close()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    truncated   = len(rows_raw) > page_size
    rows_out    = [list(r) for r in rows_raw[:page_size]]
    rowids_out  = rowids_raw[:page_size] if rowids_raw is not None else None

    fmt = request.args.get("_format", "json")
    if fmt == "csv":
        buf = io.StringIO()
        w = _csv.writer(buf)
        w.writerow(col_names_out)
        w.writerows(rows_out)
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={table_name}.csv"},
        )

    if fmt == "xlsx":
        try:
            import openpyxl as _openpyxl
        except ImportError:
            return jsonify({"error": "openpyxl not installed - cannot export Excel"}), 501
        wb = _openpyxl.Workbook(write_only=True)
        ws = wb.create_sheet(title=table_name[:31])  # Excel sheet name limit
        ws.append(col_names_out)
        for row in rows_out:
            ws.append([str(v) if v is not None else None for v in row])
        buf_xl = io.BytesIO()
        wb.save(buf_xl)
        buf_xl.seek(0)
        return Response(
            buf_xl.read(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={table_name}.xlsx"},
        )

    resp: dict = {
        "columns": col_names_out,
        "rows": rows_out,
        "page": page,
        "page_size": page_size,
        "truncated": truncated,
    }
    if rowids_out is not None:
        resp["rowids"] = rowids_out
    return jsonify(resp)


@app.route("/api/rowcounts/<key>")
def api_rowcounts(key: str):
    """Return row counts for all tables in a schema's SQLite database.

    Returns: {"tableName": count, ...}
    Gracefully returns {} if no queryable source exists yet.
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404

    db_file = _find_sqlite_db(key)
    if not db_file:
        return jsonify({})

    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        cur = con.cursor()
        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]

        # Build a map of approximate counts from sqlite_stat1 (populated by
        # ANALYZE).  The first token of the "stat" column is the row estimate.
        # We use these estimates for large tables (> 100 000 rows) to avoid a
        # full scan; exact COUNT(*) is used for everything smaller.
        stat1: dict[str, int] = {}
        try:
            stat_rows = cur.execute(
                "SELECT tbl, stat FROM sqlite_stat1"
            ).fetchall()
            for tbl, stat_str in stat_rows:
                if stat_str:
                    first_token = stat_str.split()[0]
                    try:
                        stat1[tbl] = int(first_token)
                    except ValueError:
                        pass
        except Exception:
            pass  # sqlite_stat1 may not exist if ANALYZE was never run

        APPROX_THRESHOLD = 100_000
        counts = {}
        for tname in tables:
            try:
                approx = stat1.get(tname)
                if approx is not None and approx > APPROX_THRESHOLD:
                    counts[tname] = {"count": approx, "approximate": True}
                else:
                    row = cur.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()
                    counts[tname] = {"count": row[0] if row else 0, "approximate": False}
            except Exception:
                counts[tname] = {"count": None, "approximate": False}
        con.close()
        return jsonify(counts)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Write operations (ALLOW_WRITES=1 required) ────────────────────────────────
# All three endpoints share the same guard helper and only work on file-backed
# SQLite schemas (source != "connstring" and source != "join").

def _writes_open_db(key: str):
    """Open the schema's SQLite database in read-write mode.

    Returns (db_path, con) on success, or raises RuntimeError with a
    human-readable message on any validation failure.
    """
    if not ALLOW_WRITES:
        raise RuntimeError("Write operations are disabled (set ALLOW_WRITES=1 to enable)")
    with _lock:
        reg = _load_reg()
    if key not in reg:
        raise RuntimeError("Unknown schema key")
    record = reg[key]
    source = record.get("source", "file")
    if source in ("connstring", "join"):
        raise RuntimeError(
            "Write operations are only supported for file-backed (SQLite) schemas"
        )
    db_file = _find_sqlite_db(key)
    if not db_file:
        raise RuntimeError("No queryable SQLite database found for this schema")
    con = sqlite3.connect(str(db_file))
    con.row_factory = sqlite3.Row
    return db_file, con


@app.route("/api/rows/<key>/<path:table_name>", methods=["PATCH"])
def api_row_update(key: str, table_name: str):
    """Update a single cell value.

    URL param: rowid (SQLite internal rowid of the row to update)
    Body JSON: {"column": "<col_name>", "value": <new_value>}

    Returns: {"ok": true, "rowid": <int>}
    """
    rowid_raw = request.args.get("rowid", "")
    try:
        rowid = int(rowid_raw)
    except (ValueError, TypeError):
        return jsonify({"error": "rowid query param must be an integer"}), 400

    data = request.get_json(force=True, silent=True) or {}
    col = (data.get("column") or "").strip()
    if not col:
        return jsonify({"error": "Missing 'column' in request body"}), 400
    value = data.get("value")  # None is a valid value (NULL)

    try:
        _, con = _writes_open_db(key)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 403

    try:
        cur = con.cursor()
        # Validate column exists
        col_info = cur.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        valid_cols = {r[1] for r in col_info}
        if col not in valid_cols:
            return jsonify({"error": f"Column '{col}' not found in table '{table_name}'"}), 400

        cur.execute(
            f'UPDATE "{table_name}" SET "{col}" = ? WHERE rowid = ?',
            (value, rowid),
        )
        if cur.rowcount == 0:
            return jsonify({"error": f"No row with rowid={rowid} in '{table_name}'"}), 404
        con.commit()
        _write_audit(key, "update", f'UPDATE "{table_name}" SET "{col}"=? WHERE rowid={rowid}', 1, True)
        return jsonify({"ok": True, "rowid": rowid})
    except Exception as exc:
        _write_audit(key, "update", f'UPDATE "{table_name}" SET "{col}" WHERE rowid={rowid}', 0, False)
        return jsonify({"error": str(exc)}), 500
    finally:
        con.close()


# Phase 4 integration: batch multi-cell update endpoint for clipboard paste
@app.route("/api/rows/<key>/<path:table_name>/batch", methods=["PATCH"])
def api_rows_batch_update(key: str, table_name: str):
    """Batch-update multiple cells in one transaction.

    Body JSON: {"updates": [{"rowid": int, "column": str, "value": any}, ...]}
    Returns:   {"ok": true, "updated": int, "errors": [...]}
    """
    data = request.get_json(force=True, silent=True) or {}
    updates: list = data.get("updates") or []
    if not updates:
        return jsonify({"error": "No updates provided"}), 400

    try:
        _, con = _writes_open_db(key)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 403

    try:
        cur = con.cursor()
        # Validate column list once
        col_info = cur.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        valid_cols = {r[1] for r in col_info}
        updated = 0
        errors = []
        for upd in updates:
            rowid = upd.get("rowid")
            col   = (upd.get("column") or "").strip()
            value = upd.get("value")
            if not col or col not in valid_cols:
                errors.append({"rowid": rowid, "col": col, "error": "Column not found"})
                continue
            try:
                rowid = int(rowid)
            except (TypeError, ValueError):
                errors.append({"rowid": rowid, "col": col, "error": "Invalid rowid"})
                continue
            try:
                cur.execute(
                    f'UPDATE "{table_name}" SET "{col}" = ? WHERE rowid = ?',
                    (value, rowid),
                )
                if cur.rowcount > 0:
                    updated += 1
                else:
                    errors.append({"rowid": rowid, "col": col, "error": "Row not found"})
            except Exception as exc2:
                errors.append({"rowid": rowid, "col": col, "error": str(exc2)})
        con.commit()
        return jsonify({"ok": True, "updated": updated, "errors": errors})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        con.close()


@app.route("/api/rows/<key>/<path:table_name>", methods=["POST"])
def api_row_insert(key: str, table_name: str):
    """Insert a new row.

    Body JSON: {"values": {"col1": val1, "col2": val2, ...}}
    Columns omitted from values will be NULL / DEFAULT.

    Returns: {"ok": true, "rowid": <int>}
    """
    data = request.get_json(force=True, silent=True) or {}
    values: dict = data.get("values") or {}

    try:
        _, con = _writes_open_db(key)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 403

    try:
        cur = con.cursor()
        col_info = cur.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        valid_cols = {r[1] for r in col_info}
        # Filter to only known columns
        insert_cols = [c for c in values if c in valid_cols]
        if not insert_cols:
            # Insert a blank row using DEFAULT / NULL for everything
            cur.execute(f'INSERT INTO "{table_name}" DEFAULT VALUES')
        else:
            placeholders = ", ".join("?" for _ in insert_cols)
            col_list     = ", ".join(f'"{c}"' for c in insert_cols)
            cur.execute(
                f'INSERT INTO "{table_name}" ({col_list}) VALUES ({placeholders})',
                [values[c] for c in insert_cols],
            )
        new_rowid = cur.lastrowid
        con.commit()
        _write_audit(key, "insert", f'INSERT INTO "{table_name}"', 1, True)
        return jsonify({"ok": True, "rowid": new_rowid})
    except Exception as exc:
        _write_audit(key, "insert", f'INSERT INTO "{table_name}"', 0, False)
        return jsonify({"error": str(exc)}), 500
    finally:
        con.close()


@app.route("/api/rows/<key>/<path:table_name>", methods=["DELETE"])
def api_row_delete(key: str, table_name: str):
    """Delete a row by its SQLite rowid.

    URL param: rowid (required)

    Returns: {"ok": true, "rowid": <int>}
    """
    rowid_raw = request.args.get("rowid", "")
    try:
        rowid = int(rowid_raw)
    except (ValueError, TypeError):
        return jsonify({"error": "rowid query param must be an integer"}), 400

    try:
        _, con = _writes_open_db(key)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 403

    try:
        cur = con.cursor()
        cur.execute(f'DELETE FROM "{table_name}" WHERE rowid = ?', (rowid,))
        if cur.rowcount == 0:
            return jsonify({"error": f"No row with rowid={rowid} in '{table_name}'"}), 404
        con.commit()
        _write_audit(key, "delete", f'DELETE FROM "{table_name}" WHERE rowid={rowid}', 1, True)
        return jsonify({"ok": True, "rowid": rowid})
    except Exception as exc:
        _write_audit(key, "delete", f'DELETE FROM "{table_name}" WHERE rowid={rowid}', 0, False)
        return jsonify({"error": str(exc)}), 500
    finally:
        con.close()


# ── Import preview (Rank 10) ─────────────────────────────────────────────────

@app.route("/api/preview", methods=["POST"])
def api_preview():
    """Return the first 5 rows and column names of an uploaded flat file.

    Accepts multipart form data with a single "file" field.
    Supports .csv, .xlsx, .xls, .json, .jsonl, .ndjson.
    SQLite/DB files return a list of table names instead of rows.

    Returns:
        For tabular files:
            {"type": "tabular", "columns": [...], "rows": [[...], ...], "total_cols": N}
        For SQLite files:
            {"type": "sqlite", "tables": [...]}
        On error:
            {"error": "..."}
    """
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    raw = f.read(4 * 1024 * 1024)  # read at most 4 MB for preview
    name = Path(f.filename).name.lower()
    ext = Path(name).suffix.lower()

    try:
        if ext in (".db", ".sqlite", ".sqlite3"):
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(raw)
                tmp_path = tmp.name
            try:
                con = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
                tables = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()]
                con.close()
            finally:
                try:
                    Path(tmp_path).unlink()
                except Exception:
                    pass
            return jsonify({"type": "sqlite", "tables": tables})

        elif ext == ".csv" or name.endswith(".csv.gz"):
            import csv, io, gzip, zipfile as _zf
            if name.endswith(".gz"):
                text = gzip.decompress(raw).decode("utf-8", errors="replace")
            elif raw[:2] == b"PK":
                # File has a .csv extension but is actually a ZIP archive.
                # Extract the first .csv entry and decode that instead.
                try:
                    with _zf.ZipFile(io.BytesIO(raw)) as zf:
                        csv_entries = [e for e in zf.infolist()
                                       if e.filename.lower().endswith(".csv")]
                        if not csv_entries:
                            return jsonify({"error": "ZIP archive contains no .csv entry"}), 400
                        inner_bytes = zf.read(csv_entries[0].filename)
                    # Strip UTF-8 BOM if present, then decode
                    text = inner_bytes.lstrip(b"\xef\xbb\xbf").decode("utf-8", errors="replace")
                except _zf.BadZipFile:
                    text = raw.decode("utf-8", errors="replace")
            else:
                text = raw.lstrip(b"\xef\xbb\xbf").decode("utf-8", errors="replace")
            # newline="" is required by the csv module to handle \r\n correctly
            reader = csv.reader(io.StringIO(text, newline=""))
            headers = next(reader, [])
            rows = [row for _, row in zip(range(5), reader)]
            return jsonify({
                "type": "tabular",
                "columns": headers,
                "rows": rows,
                "total_cols": len(headers),
            })

        elif ext in (".xlsx", ".xls"):
            import io as _io
            try:
                import openpyxl
                wb = openpyxl.load_workbook(_io.BytesIO(raw), read_only=True, data_only=True)
                ws = wb.active
                rows_iter = ws.iter_rows(values_only=True)
                headers = [str(c) if c is not None else "" for c in (next(rows_iter, ()) or [])]
                preview_rows = []
                for _, row in zip(range(5), rows_iter):
                    preview_rows.append([str(c) if c is not None else "" for c in row])
                wb.close()
                return jsonify({
                    "type": "tabular",
                    "columns": headers,
                    "rows": preview_rows,
                    "total_cols": len(headers),
                })
            except ImportError:
                return jsonify({"error": "openpyxl not available"}), 500

        elif ext in (".json",):
            import io
            text = raw.decode("utf-8", errors="replace")
            data = json.loads(text)
            if isinstance(data, list) and data:
                # Array of objects
                headers = list(data[0].keys()) if isinstance(data[0], dict) else []
                rows = [
                    [str(row.get(h, "")) for h in headers]
                    for row in data[:5]
                    if isinstance(row, dict)
                ]
                return jsonify({"type": "tabular", "columns": headers, "rows": rows, "total_cols": len(headers)})
            return jsonify({"type": "tabular", "columns": [], "rows": [], "total_cols": 0})

        elif ext in (".jsonl", ".ndjson"):
            import io
            text = raw.decode("utf-8", errors="replace")
            objs = []
            for line in text.splitlines():
                line = line.strip()
                if line:
                    try:
                        objs.append(json.loads(line))
                    except Exception:
                        pass
                if len(objs) >= 5:
                    break
            if objs and isinstance(objs[0], dict):
                headers = list(objs[0].keys())
                rows = [[str(o.get(h, "")) for h in headers] for o in objs]
                return jsonify({"type": "tabular", "columns": headers, "rows": rows, "total_cols": len(headers)})
            return jsonify({"type": "tabular", "columns": [], "rows": [], "total_cols": 0})

        else:
            # Unknown extension - skip preview
            return jsonify({"type": "unsupported"})

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/write_status")
def api_write_status():
    """Return whether write operations are enabled.

    Used by the browse UI to conditionally show / hide the write toolbar.
    Returns: {"allow_writes": bool}
    """
    return jsonify({"allow_writes": ALLOW_WRITES})


@app.route("/api/profile/<key>/<table_name>/<col_name>")
def api_profile(key: str, table_name: str, col_name: str):
    """Return column-level statistics for a single column.

    Handles join schemas: when ``key`` is a join entry and ``table_name`` is
    ``"alias.real_table"``, routes to the member schema's SQLite database.

    Returns:
      {
        "col": str,
        "total": int,
        "null_count": int,
        "distinct_count": int,
        "min": any,
        "max": any,
        "avg": float | null,
        "top_values": [{"val": any, "count": int}, ...]   # up to 10
      }
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404

    record = reg[key]

    # Join routing: resolve "alias.real_table" → member key + real table name
    if record.get("source") == "join":
        dot = table_name.find(".")
        if dot < 0:
            return jsonify({"error": "Table name must be 'alias.table' for join schemas"}), 422
        alias_part  = table_name[:dot]
        real_table  = table_name[dot + 1:]
        member_key  = next(
            (m["key"] for m in record.get("members", []) if m.get("alias") == alias_part),
            None,
        )
        if not member_key:
            return jsonify({"error": f"No member with alias '{alias_part}'"}), 404
        db_file    = _find_sqlite_db(member_key)
        table_name = real_table
    else:
        db_file = _find_sqlite_db(key)

    if not db_file:
        return jsonify({"error": "No queryable source for this schema"}), 422

    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        cur = con.cursor()

        # Verify table + column exist
        valid_tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        col_info = cur.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        valid_cols = {r[1] for r in col_info}
        if table_name not in valid_tables:
            con.close()
            return jsonify({"error": "Table not found"}), 404
        if col_name not in valid_cols:
            con.close()
            return jsonify({"error": "Column not found"}), 404

        q = f'"{col_name}"'
        t = f'"{table_name}"'

        # Basic stats in one pass
        row = cur.execute(
            f'SELECT COUNT(*), COUNT({q}), COUNT(DISTINCT {q}), '
            f'MIN({q}), MAX({q}), AVG(CAST({q} AS REAL)) FROM {t}'
        ).fetchone()
        total        = row[0]
        non_null     = row[1]
        distinct     = row[2]
        mn           = row[3]
        mx           = row[4]
        avg          = row[5]
        null_count   = total - non_null

        # Top 10 values by frequency
        top_rows = cur.execute(
            f'SELECT {q}, COUNT(*) AS cnt FROM {t} WHERE {q} IS NOT NULL '
            f'GROUP BY {q} ORDER BY cnt DESC LIMIT 10'
        ).fetchall()
        top_values = [{"val": r[0], "count": r[1]} for r in top_rows]

        con.close()
        return jsonify({
            "col":            col_name,
            "total":          total,
            "null_count":     null_count,
            "distinct_count": distinct,
            "min":            mn,
            "max":            mx,
            "avg":            round(avg, 4) if avg is not None else None,
            "top_values":     top_values,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/colstats/<key>/<table_name>")
def api_colstats(key: str, table_name: str):
    """Return lightweight per-column stats for all columns in one pass.

    Used by the Browse UI to classify columns as categorical and annotate
    column headers with distinct-value counts, without firing N per-column
    profile requests.

    Handles join schemas: when ``key`` is a join entry and ``table_name`` is
    ``"alias.real_table"``, routes to the member schema's SQLite database.

    Returns:
      {
        "total": int,
        "columns": {
          "colName": {
            "declared_type": str,   # from PRAGMA table_info (TEXT/INTEGER/REAL/…)
            "distinct_count": int,
            "is_categorical": bool  # TEXT + distinct_count <= 50 + ratio < 0.5
          }, ...
        }
      }
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404

    record = reg[key]

    # Join routing: resolve "alias.real_table" → member key + real table name
    if record.get("source") == "join":
        dot = table_name.find(".")
        if dot >= 0:
            alias_part = table_name[:dot]
            real_table = table_name[dot + 1:]
            member_key = next(
                (m["key"] for m in record.get("members", []) if m.get("alias") == alias_part),
                None,
            )
            if member_key:
                db_file    = _find_sqlite_db(member_key)
                table_name = real_table
            else:
                db_file = None
        else:
            db_file = None
    else:
        db_file = _find_sqlite_db(key)

    if not db_file:
        return jsonify({"error": "No queryable source for this schema"}), 422

    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        cur = con.cursor()

        valid_tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if table_name not in valid_tables:
            con.close()
            return jsonify({"error": "Table not found"}), 404

        # Declared types from PRAGMA (one lightweight metadata call)
        col_info = cur.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        # col_info rows: (cid, name, type, notnull, dflt, pk)
        declared_types = {r[1]: (r[2] or "").upper() for r in col_info}
        col_names = [r[1] for r in col_info]

        if not col_names:
            con.close()
            return jsonify({"total": 0, "columns": {}})

        # Row count (single query)
        total = cur.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]

        # All COUNT(DISTINCT col) in one query
        select_exprs = ", ".join(
            f'COUNT(DISTINCT "{c}")' for c in col_names
        )
        distinct_row = cur.execute(
            f'SELECT {select_exprs} FROM "{table_name}"'
        ).fetchone()
        con.close()

        columns = {}
        for i, col in enumerate(col_names):
            dc = distinct_row[i] if distinct_row else 0
            dtype = declared_types.get(col, "")
            # Categorical: explicitly TEXT declared type, low cardinality, low ratio
            is_cat = (
                dtype == "TEXT"
                and dc > 0
                and dc <= 50
                and (total == 0 or dc / total < 0.5)
            )
            columns[col] = {
                "declared_type":  dtype,
                "distinct_count": dc,
                "is_categorical": is_cat,
            }

        return jsonify({"total": total, "columns": columns})

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/facets/<key>/<table_name>")
def api_facets(key: str, table_name: str):
    """Return colstats AND top-10 values for every categorical column in one round-trip.

    This is the fast path used by the Browse facet panel.  It replaces the old
    pattern of: one ``/api/colstats/`` call + N ``/api/profile/`` calls (one per
    categorical column).  Here we do everything in a single SQLite connection with
    at most two queries per categorical column - all inside one HTTP request.

    Handles join schemas identically to ``api_colstats``: ``table_name`` may be
    ``"alias.real_table"`` and is resolved to the member DB transparently.

    Returns::

        {
          "total": int,
          "columns": {
            "colName": {
              "declared_type": str,
              "distinct_count": int,
              "is_categorical": bool,
              "top_values": [{"val": any, "count": int}, ...]   # only if is_categorical
            }, ...
          }
        }
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404

    record = reg[key]

    # Join routing: resolve "alias.real_table" → member key + real table name
    if record.get("source") == "join":
        dot = table_name.find(".")
        if dot >= 0:
            alias_part = table_name[:dot]
            real_table = table_name[dot + 1:]
            member_key = next(
                (m["key"] for m in record.get("members", []) if m.get("alias") == alias_part),
                None,
            )
            if member_key:
                db_file    = _find_sqlite_db(member_key)
                table_name = real_table
            else:
                db_file = None
        else:
            db_file = None
    else:
        db_file = _find_sqlite_db(key)

    if not db_file:
        return jsonify({"error": "No queryable source for this schema"}), 422

    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        cur = con.cursor()

        valid_tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if table_name not in valid_tables:
            con.close()
            return jsonify({"error": "Table not found"}), 404

        # ── Pass 1: colstats (same single-query approach as api_colstats) ─────
        col_info = cur.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        if not col_info:
            con.close()
            return jsonify({"total": 0, "columns": {}})

        declared_types = {r[1]: (r[2] or "").upper() for r in col_info}
        col_names = [r[1] for r in col_info]

        total = cur.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]

        select_exprs = ", ".join(f'COUNT(DISTINCT "{c}")' for c in col_names)
        distinct_row = cur.execute(
            f'SELECT {select_exprs} FROM "{table_name}"'
        ).fetchone()

        columns = {}
        cat_cols = []
        for i, col in enumerate(col_names):
            dc = distinct_row[i] if distinct_row else 0
            dtype = declared_types.get(col, "")
            is_cat = (
                dtype == "TEXT"
                and dc > 0
                and dc <= 50
                and (total == 0 or dc / total < 0.5)
            )
            columns[col] = {
                "declared_type":  dtype,
                "distinct_count": dc,
                "is_categorical": is_cat,
                "top_values":     [],
            }
            if is_cat:
                cat_cols.append(col)

        # ── Pass 2: top_values for each categorical column ────────────────────
        # One GROUP BY query per categorical column - these are cheap because
        # cardinality is ≤ 50 by definition (is_categorical gate above).
        for col in cat_cols:
            rows = cur.execute(
                f'SELECT "{col}", COUNT(*) AS cnt FROM "{table_name}" '
                f'WHERE "{col}" IS NOT NULL GROUP BY "{col}" ORDER BY cnt DESC LIMIT 10'
            ).fetchall()
            columns[col]["top_values"] = [{"val": r[0], "count": r[1]} for r in rows]

        con.close()
        return jsonify({"total": total, "columns": columns})

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Server-side saved queries ─────────────────────────────────────────────────

def _queries_path(key: str) -> Path:
    return OUTPUT_DIR / key / "queries.json"


def _load_queries(key: str) -> list:
    p = _queries_path(key)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


@app.route("/api/aggregate/<key>/<table_name>")
def api_aggregate(key: str, table_name: str):
    """Return GROUP BY aggregation for Browse Phase 3 summary panel.

    Query params:
      group_by   str   column to group by (required)
      agg_func   str   COUNT | SUM | AVG | MIN | MAX  (default COUNT)
      agg_col    str   column to aggregate; ignored for COUNT(*) unless specified
      limit      int   max groups to return (default 50)
      + all standard col__op=val filter params forwarded from the grid

    Returns:
      {"groups": [{"label": str, "value": float|int}, ...]}
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    record = reg[key]

    group_by  = request.args.get("group_by", "").strip()
    agg_func  = request.args.get("agg_func", "COUNT").upper().strip()
    agg_col   = request.args.get("agg_col",  "").strip()
    limit     = min(int(request.args.get("limit", 50)), 200)

    if not group_by:
        return jsonify({"error": "group_by is required"}), 400
    if agg_func not in {"COUNT", "SUM", "AVG", "MIN", "MAX"}:
        return jsonify({"error": "Invalid agg_func"}), 400

    # Resolve to a sqlite db file (file-based and join schemas both use SQLite)
    source = record.get("source", "file")
    if source == "join":
        dot = table_name.find(".")
        if dot < 0:
            return jsonify({"error": "Table name must be 'alias.table' for join schemas"}), 422
        alias_part = table_name[:dot]
        real_table = table_name[dot + 1:]
        member_key = next(
            (m["key"] for m in record.get("members", []) if m.get("alias") == alias_part),
            None,
        )
        if not member_key:
            return jsonify({"error": f"No member with alias '{alias_part}'"}), 404
        db_file    = _find_sqlite_db(member_key)
        table_name = real_table
    elif source == "connstring":
        # For connstring sources fall back to a simple GROUP BY via SQLAlchemy
        try:
            from sqlalchemy import create_engine, text as sa_text
            conn_url = _resolve_conn_url(record.get("conn_url", ""))
            engine   = create_engine(conn_url, connect_args={"connect_timeout": 10})
            with engine.connect() as conn:
                col_names = [c["name"] for c in engine.dialect.get_columns(conn, table_name)]
                if group_by not in col_names:
                    return jsonify({"error": "group_by column not found"}), 400
                filters, bind_params = _parse_filter_params(request.args, col_names)
                where = f"WHERE {' AND '.join(filters)}" if filters else ""
                agg_expr = (
                    f'COUNT(*)' if agg_func == "COUNT" and not agg_col
                    else f'{agg_func}("{agg_col}")' if agg_col in col_names
                    else "COUNT(*)"
                )
                sql = (
                    f'SELECT "{group_by}", {agg_expr} AS _val FROM "{table_name}" {where} '
                    f'GROUP BY "{group_by}" ORDER BY _val DESC LIMIT :lim'
                )
                bind_params["lim"] = limit
                rows = conn.execute(sa_text(sql), bind_params).fetchall()
            engine.dispose()
            return jsonify({"groups": [{"label": str(r[0] if r[0] is not None else "NULL"), "value": r[1] or 0} for r in rows]})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
    else:
        db_file = _find_sqlite_db(key)

    if not db_file:
        return jsonify({"error": "No queryable source for this schema"}), 422

    try:
        import sqlite3
        con  = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        cur  = con.cursor()

        col_info  = cur.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        col_names = [c[1] for c in col_info]
        if group_by not in col_names:
            con.close()
            return jsonify({"error": "group_by column not found"}), 400

        filters, bind_list = _parse_filter_params_sqlite(request.args, col_names)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""

        agg_expr = (
            "COUNT(*)" if agg_func == "COUNT" and not agg_col
            else f'{agg_func}("{agg_col}")' if agg_col and agg_col in col_names
            else "COUNT(*)"
        )
        sql = (
            f'SELECT "{group_by}", {agg_expr} AS _val FROM "{table_name}" {where} '
            f'GROUP BY "{group_by}" ORDER BY _val DESC LIMIT ?'
        )
        bind_list.append(limit)
        rows = cur.execute(sql, bind_list).fetchall()
        con.close()
        return jsonify({
            "groups": [
                {"label": str(r[0]) if r[0] is not None else "NULL", "value": r[1] or 0}
                for r in rows
            ]
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/column_stats/<key>/<table_name>")
def api_column_stats(key: str, table_name: str):
    """Return basic statistics for a single column - used by Browse Phase 4 stats popover.

    Query params:
      col   str   column name (required)

    Returns:
      { count, nulls, distinct, min, max, avg, fill_pct }
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    record = reg[key]

    col = request.args.get("col", "").strip()
    if not col:
        return jsonify({"error": "col is required"}), 400

    source = record.get("source", "file")

    # ── connstring path ───────────────────────────────────────────────────────
    if source == "connstring":
        try:
            from sqlalchemy import create_engine, text as sa_text
            conn_url = _resolve_conn_url(record.get("conn_url", ""))
            engine   = create_engine(conn_url, connect_args={"connect_timeout": 10})
            with engine.connect() as conn:
                col_names = [c["name"] for c in engine.dialect.get_columns(conn, table_name)]
                if col not in col_names:
                    return jsonify({"error": "Column not found"}), 400
                sql = sa_text(
                    f'SELECT COUNT(*), COUNT("{col}"), COUNT(DISTINCT "{col}"), '
                    f'MIN("{col}"), MAX("{col}"), AVG(CAST("{col}" AS FLOAT)) FROM "{table_name}"'
                )
                row = conn.execute(sql).fetchone()
            engine.dispose()
            total, non_null, distinct, mn, mx, avg = row
            fill_pct = (non_null / total * 100) if total else 0
            return jsonify({
                "count": total, "nulls": total - non_null,
                "distinct": distinct, "min": mn, "max": mx,
                "avg": round(float(avg), 4) if avg is not None else None,
                "fill_pct": round(fill_pct, 1),
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ── file / join path (SQLite) ─────────────────────────────────────────────
    if source == "join":
        dot = table_name.find(".")
        if dot < 0:
            return jsonify({"error": "Table name must be 'alias.table' for join schemas"}), 422
        alias_part  = table_name[:dot]
        real_table  = table_name[dot + 1:]
        member_key  = next(
            (m["key"] for m in record.get("members", []) if m.get("alias") == alias_part), None
        )
        if not member_key:
            return jsonify({"error": f"No member with alias '{alias_part}'"}), 404
        db_file    = _find_sqlite_db(member_key)
        table_name = real_table
    else:
        db_file = _find_sqlite_db(key)

    if not db_file:
        return jsonify({"error": "No queryable source for this schema"}), 422

    try:
        import sqlite3
        con      = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        cur      = con.cursor()
        col_info = cur.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        col_names = [c[1] for c in col_info]
        if col not in col_names:
            con.close()
            return jsonify({"error": "Column not found"}), 400

        row = cur.execute(
            f'SELECT COUNT(*), COUNT("{col}"), COUNT(DISTINCT "{col}"), '
            f'MIN("{col}"), MAX("{col}"), AVG(CAST("{col}" AS REAL)) FROM "{table_name}"'
        ).fetchone()
        con.close()
        total, non_null, distinct, mn, mx, avg = row
        fill_pct = (non_null / total * 100) if total else 0
        return jsonify({
            "count":    total,
            "nulls":    total - non_null,
            "distinct": distinct,
            "min":      mn,
            "max":      mx,
            "avg":      round(float(avg), 4) if avg is not None else None,
            "fill_pct": round(fill_pct, 1),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _save_queries(key: str, queries: list) -> None:
    p = _queries_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(queries, ensure_ascii=False, indent=2), encoding="utf-8")


@app.route("/api/queries/<key>", methods=["GET"])
def api_queries_list(key: str):
    """List all saved queries for a schema."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    return jsonify(_load_queries(key))


@app.route("/api/queries/<key>", methods=["POST"])
def api_queries_create(key: str):
    """Create a new saved query.  Body: {"name": str, "sql": str}"""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    sql  = (data.get("sql")  or "").strip()
    if not name or not sql:
        return jsonify({"error": "name and sql are required"}), 400
    queries = _load_queries(key)
    qid = str(int(time.time() * 1000))
    entry = {"id": qid, "name": name, "sql": sql, "created": time.time()}
    queries.append(entry)
    _save_queries(key, queries)
    return jsonify(entry), 201


@app.route("/api/queries/<key>/<qid>", methods=["PATCH"])
def api_queries_update(key: str, qid: str):
    """Update name and/or sql of a saved query."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    data = request.get_json(force=True, silent=True) or {}
    queries = _load_queries(key)
    for q in queries:
        if q["id"] == qid:
            if "name" in data:
                q["name"] = str(data["name"]).strip()
            if "sql" in data:
                q["sql"] = str(data["sql"]).strip()
            q["updated"] = time.time()
            _save_queries(key, queries)
            return jsonify(q)
    return jsonify({"error": "Query not found"}), 404


@app.route("/api/queries/<key>/<qid>", methods=["DELETE"])
def api_queries_delete(key: str, qid: str):
    """Delete a saved query by id."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    queries = _load_queries(key)
    before = len(queries)
    queries = [q for q in queries if q["id"] != qid]
    if len(queries) == before:
        return jsonify({"error": "Query not found"}), 404
    _save_queries(key, queries)
    return jsonify({"ok": True})


# ── Saved Charts ──────────────────────────────────────────────────────────────

def _charts_path(key: str) -> Path:
    return OUTPUT_DIR / key / "charts.json"


def _load_charts(key: str) -> list:
    p = _charts_path(key)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_charts(key: str, charts: list) -> None:
    p = _charts_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(charts, ensure_ascii=False, indent=2), encoding="utf-8")


@app.route("/api/charts/<key>", methods=["GET"])
def api_charts_list(key: str):
    """List all saved charts for a schema."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    return jsonify(_load_charts(key))


@app.route("/api/charts/<key>", methods=["POST"])
def api_charts_create(key: str):
    """Save a chart config.
    Body: {name, table, group_by_col, agg_func, agg_col, agg_limit, chart_type, value_label}
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    charts = _load_charts(key)
    cid = str(int(time.time() * 1000))
    entry = {
        "id":           cid,
        "name":         name,
        "table":        (data.get("table")        or "").strip(),
        "group_by_col": (data.get("group_by_col") or "").strip(),
        "agg_func":     (data.get("agg_func")     or "COUNT").upper().strip(),
        "agg_col":      (data.get("agg_col")      or "").strip(),
        "agg_limit":    int(data.get("agg_limit") or 50),
        "chart_type":   (data.get("chart_type")   or "bar").strip(),
        "value_label":  (data.get("value_label")  or "").strip(),
        "created":      time.time(),
    }
    charts.append(entry)
    _save_charts(key, charts)
    return jsonify(entry), 201


@app.route("/api/charts/<key>/<cid>", methods=["DELETE"])
def api_charts_delete(key: str, cid: str):
    """Delete a saved chart by id."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    charts = _load_charts(key)
    before = len(charts)
    charts = [c for c in charts if c["id"] != cid]
    if len(charts) == before:
        return jsonify({"error": "Chart not found"}), 404
    _save_charts(key, charts)
    return jsonify({"ok": True})




@app.route("/api/query/<key>", methods=["POST"])
def api_query(key: str):
    """Execute a read-only SQL query against the schema's source database.

    Body: {"sql": "SELECT ...", "params": {}, "limit": 1000}
    Returns: {"columns": [...], "rows": [[...]], "row_count": N, "truncated": bool, "elapsed_ms": N}
    """
    import time as _time

    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    record = reg[key]

    data = request.get_json(force=True, silent=True) or {}
    sql = (data.get("sql") or "").strip()
    if not sql:
        return jsonify({"error": "sql is required"}), 400

    # Safety: reject non-SELECT statements
    first_token = sql.upper().split()[0] if sql.split() else ""
    if first_token not in ("SELECT", "WITH", "EXPLAIN", "PRAGMA"):
        return jsonify({"error": "Only SELECT/WITH/EXPLAIN queries are permitted"}), 400

    raw_limit = int(data.get("limit", 1000))
    # 0 means "all rows" (no limit imposed by the client)
    limit = None if raw_limit == 0 else min(raw_limit, 10000)
    params = data.get("params") or {}

    # Wrap in a limit if not already present
    sql_exec = sql
    if first_token in ("SELECT", "WITH") and "LIMIT" not in sql.upper():
        if limit is not None:
            sql_exec = f"SELECT * FROM ({sql}) _q LIMIT {limit + 1}"

    t0 = _time.time()
    try:
        source = record.get("source", "file")
        if source == "connstring":
            from sqlalchemy import create_engine, text as sa_text
            engine = create_engine(
                _resolve_conn_url(record.get("conn_url", "")),
                connect_args={"connect_timeout": 10},
            )
            with engine.connect() as conn:
                try:
                    conn.execute(sa_text(f"SET statement_timeout = '{QUERY_TIMEOUT_SECONDS}s'"))
                except Exception:
                    pass
                result = conn.execute(sa_text(sql_exec), params)
                cols = list(result.keys())
                rows = [list(r) for r in result.fetchall()]
            engine.dispose()
        elif source == "duckdb":
            # Phase B: DuckDB server-side - accepts a Parquet path stored in record
            try:
                import duckdb as _duckdb
            except ImportError:
                return jsonify({"error": "duckdb package not installed"}), 501
            parquet_path = record.get("parquet_path", "")
            if not parquet_path or not Path(parquet_path).exists():
                return jsonify({"error": "No Parquet file configured for this schema"}), 422
            con = _duckdb.connect(database=":memory:", read_only=False)
            # Register the Parquet file as a virtual table named after the key
            con.execute(f"CREATE VIEW data AS SELECT * FROM read_parquet({parquet_path!r})")
            rel = con.execute(sql_exec)
            cols = [d[0] for d in rel.description]
            rows = [list(r) for r in rel.fetchall()]
            con.close()
        elif source == "join":
            # Cross-schema join - open in-memory SQLite and ATTACH each member DB.
            # _find_sqlite_db() generates the .db on-demand for flat-file members
            # (CSV, XLSX, etc.) that have never been queried directly.  We call it
            # here unconditionally so the first join query always succeeds without
            # requiring the user to visit each member source first.
            with _lock:
                join_reg = _load_reg()
            join_record = join_reg.get(key, record)
            con = sqlite3.connect(":memory:")
            missing = []
            for m in join_record.get("members", []):
                db_path = _find_sqlite_db(m["key"])
                if db_path:
                    con.execute(
                        f'ATTACH DATABASE "{str(db_path)}" AS "{m["alias"]}"'
                    )
                else:
                    missing.append(m.get("alias", m["key"]))
            if missing:
                con.close()
                raise sqlite3.OperationalError(
                    f"Could not locate a queryable database for join member(s): "
                    f"{', '.join(missing)}. "
                    f"Try processing the source schema first."
                )
            cur = con.cursor()
            cur.execute(sql_exec)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [list(r) for r in cur.fetchall()]
            con.close()
        elif source == "libsql":
            # libSQL / Turso - via HTTP API (stdlib only)
            import json as _jmod
            import urllib.request as _ulr2
            libsql_url = record.get("libsql_url", "").strip()
            auth_token = record.get("auth_token", "").strip()
            http_url2  = libsql_url
            if http_url2.startswith("libsql://"):
                http_url2 = "https://" + http_url2[len("libsql://"):]
            ep2 = http_url2.rstrip("/") + "/v2/pipeline"
            payload2 = _jmod.dumps({
                "requests": [
                    {"type": "execute", "stmt": {"sql": sql_exec}},
                    {"type": "close"},
                ]
            }).encode()
            req2 = _ulr2.Request(ep2, data=payload2, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {auth_token}" if auth_token else "",
            })
            with _ulr2.urlopen(req2, timeout=30) as resp2:
                body2 = _jmod.loads(resp2.read())
            res2 = body2["results"][0]
            if res2.get("type") == "error":
                raise RuntimeError(res2.get("error", {}).get("message", "libSQL error"))
            cols_raw2  = res2["response"]["result"]["cols"]
            rows_raw2  = res2["response"]["result"]["rows"]
            cols = [c["name"] for c in cols_raw2]
            rows = [[cell.get("value") for cell in row] for row in rows_raw2]
        elif source == "clickhouse":
            # ClickHouse - via clickhouse-connect (pure Python)
            import re as _re2
            conn_url_ch = _resolve_conn_url(record.get("conn_url", ""))
            m_ch = _re2.match(
                r"(?:clickhouse(?:\+\w+)?://)(?:([^:@]+)(?::([^@]*))?@)?([^:/?]+)(?::(\d+))?(?:/([^?]*))?",
                conn_url_ch,
            )
            if not m_ch:
                raise ValueError(f"Cannot parse ClickHouse URL: {conn_url_ch}")
            ch_user = m_ch.group(1) or "default"
            ch_pass = m_ch.group(2) or ""
            ch_host = m_ch.group(3) or "localhost"
            ch_port = int(m_ch.group(4) or 8123)
            ch_db   = m_ch.group(5) or "default"
            try:
                import clickhouse_connect as _ch2
            except ImportError:
                return jsonify({"error": "clickhouse-connect not installed; run: pip install clickhouse-connect"}), 501
            ch_client = _ch2.get_client(
                host=ch_host, port=ch_port,
                username=ch_user, password=ch_pass,
                database=ch_db,
                send_receive_timeout=30,
            )
            ch_result = ch_client.query(sql_exec)
            cols = list(ch_result.column_names)
            rows = [list(r) for r in ch_result.result_rows]
        elif source == "mssql":
            # MSSQL - re-uses the SQLAlchemy connstring path
            from sqlalchemy import create_engine, text as sa_text
            engine_ms = create_engine(
                _resolve_conn_url(record.get("conn_url", "")),
                connect_args={"timeout": 10},
            )
            with engine_ms.connect() as conn_ms:
                result_ms = conn_ms.execute(sa_text(sql_exec), params)
                cols = list(result_ms.keys())
                rows = [list(r) for r in result_ms.fetchall()]
            engine_ms.dispose()
        else:
            # File-based - find the SQLite database (uploaded or pipeline-imported)
            db_file = _find_sqlite_db(key)
            if not db_file:
                return jsonify({"error": "No queryable source for this schema"}), 422
            con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            # Per-query timeout via threading.Timer → con.interrupt()
            _timed_out = [False]
            _timer = threading.Timer(QUERY_TIMEOUT_SECONDS, lambda: (con.interrupt(), _timed_out.__setitem__(0, True)))
            _timer.start()
            try:
                cur.execute(sql_exec)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = [list(r) for r in cur.fetchall()]
            finally:
                _timer.cancel()
            if _timed_out[0]:
                con.close()
                return jsonify({"error": f"Query timed out after {QUERY_TIMEOUT_SECONDS} seconds"}), 408
            con.close()
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            return jsonify({"error": f"Query timed out after {QUERY_TIMEOUT_SECONDS} seconds"}), 408
        return jsonify({
            "error": str(exc),
            "elapsed_ms": round((_time.time() - t0) * 1000),
        }), 400
    except Exception as exc:
        return jsonify({
            "error": str(exc),
            "elapsed_ms": round((_time.time() - t0) * 1000),
        }), 400

    elapsed_ms = round((_time.time() - t0) * 1000)
    truncated = limit is not None and len(rows) > limit
    out_rows = rows[:limit] if limit is not None else rows
    # Persist to server-side history and audit log (best-effort, non-blocking)
    try:
        _append_history(key, sql, len(out_rows), elapsed_ms)
    except Exception:
        pass
    try:
        _write_audit(key, "query", sql, len(out_rows), True)
    except Exception:
        pass
    return jsonify({
        "columns": cols,
        "rows": out_rows,
        "row_count": len(out_rows),
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
    })


@app.route("/api/sqlite_file/<key>")
def api_sqlite_file(key: str):
    """Serve the SQLite .db file for DuckDB-WASM in-browser VFS loading.

    The DuckDB-WASM toggle in query.html fetches this endpoint to download
    the raw SQLite bytes into the OPFS/memory VFS so it can run SQL in-browser.
    Returns the file as application/octet-stream; 404 if none exists.

    Response headers (Item 8 - integrity):
      X-Content-SHA256   hex SHA-256 of the file bytes, computed via chunked read
                         so large databases don't load entirely into memory.
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    db_file = _find_sqlite_db(key)
    if not db_file:
        return jsonify({"error": "No SQLite database found for this schema"}), 404

    # Compute SHA-256 via chunked read (safe for large files).
    h = hashlib.sha256()
    with open(db_file, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    checksum = h.hexdigest()

    from flask import send_file
    response = send_file(
        db_file,
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=f"{key}.db",
    )
    response.headers["X-Content-SHA256"] = checksum
    # Expose the header to browser JS (CORS pre-flight safe for same-origin).
    response.headers["Access-Control-Expose-Headers"] = "X-Content-SHA256"
    return response


@app.route("/api/sqlite_size/<key>")
def api_sqlite_size(key: str):
    """Return the byte size of the SQLite file for a schema.

    Used by the auto engine-selection logic in query.html to decide whether
    the database is small enough to load into DuckDB-WASM in the browser.
    Returns {"size_bytes": N} or {"size_bytes": null} if no file exists.
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    db_file = _find_sqlite_db(key)
    if not db_file:
        return jsonify({"size_bytes": None})
    return jsonify({"size_bytes": db_file.stat().st_size})


# ── Autocomplete cache ────────────────────────────────────────────────────────
# { key: (timestamp, payload) }  - payload = {"tables": [...], "columns": {...}}
_autocomplete_cache: dict[str, tuple[float, dict]] = {}
_AUTOCOMPLETE_TTL = 60.0  # seconds


def _build_autocomplete(key: str, record: dict) -> dict:
    """Build autocomplete payload for *key*.

    Returns {"tables": [str, ...], "columns": {tbl: [col, ...]}}.
    Includes views alongside tables where the source supports it.
    """
    source = record.get("source", "file")
    tables: list[str] = []
    columns: dict[str, list[str]] = {}

    if source == "join":
        # Build the alias-prefixed table/column list from each member schema.
        # This mirrors _merge_join_schema but only produces the autocomplete payload.
        # Ensures the query sidebar is populated on first load without needing to
        # visit each member source individually first.
        with _lock:
            join_reg = _load_reg()
        for m in record.get("members", []):
            alias      = m.get("alias", m["key"])
            member_key = m["key"]
            member_rec = join_reg.get(member_key, {})
            member_source = member_rec.get("source", "file")

            member_tables: list[str] = []
            member_cols:   dict[str, list[str]] = {}

            # Try SQLite first (generates .db on-demand for flat-file members,
            # so the first join query will succeed immediately after this call)
            db_file = _find_sqlite_db(member_key)
            if db_file:
                try:
                    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
                    cur = con.cursor()
                    cur.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type IN ('table','view') ORDER BY name"
                    )
                    for (tname,) in cur.fetchall():
                        prefixed = f"{alias}.{tname}"
                        member_tables.append(prefixed)
                        cur2 = con.cursor()
                        cur2.execute(f'PRAGMA table_info("{tname}")')
                        member_cols[prefixed] = [row[1] for row in cur2.fetchall()]
                    con.close()
                except Exception:
                    pass

            # Fall back to schema.json if SQLite unavailable
            if not member_tables:
                schema_path = OUTPUT_DIR / member_key / "schema.json"
                erd_tables = _schema_to_erd_data(schema_path) or []
                for t in erd_tables:
                    prefixed = f"{alias}.{t['name']}"
                    member_tables.append(prefixed)
                    member_cols[prefixed] = [c["name"] for c in t.get("columns", [])]

            tables.extend(member_tables)
            columns.update(member_cols)

        return {"tables": tables, "columns": columns}

    if source in ("file", ""):
        # SQLite - read directly for live column list (includes views)
        db_file = _find_sqlite_db(key)
        if db_file:
            try:
                con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
                cur = con.cursor()
                cur.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type IN ('table','view') ORDER BY name"
                )
                for (tname,) in cur.fetchall():
                    tables.append(tname)
                    cur2 = con.cursor()
                    cur2.execute(f'PRAGMA table_info("{tname}")')
                    columns[tname] = [row[1] for row in cur2.fetchall()]
                con.close()
            except Exception:
                pass
        # Fall back to schema.json if SQLite unavailable
        if not tables:
            schema_path = OUTPUT_DIR / key / "schema.json"
            erd_tables = _schema_to_erd_data(schema_path) or []
            for t in erd_tables:
                tables.append(t["name"])
                columns[t["name"]] = [c["name"] for c in t.get("columns", [])]
    elif source in ("connstring", "mssql"):
        # SQLAlchemy - use information_schema
        try:
            from sqlalchemy import create_engine, text as sa_text
            engine = create_engine(
                _resolve_conn_url(record.get("conn_url", "")),
                connect_args={"connect_timeout": 5},
            )
            with engine.connect() as conn:
                try:
                    res = conn.execute(sa_text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema NOT IN ('information_schema','pg_catalog') "
                        "UNION ALL "
                        "SELECT table_name FROM information_schema.views "
                        "WHERE table_schema NOT IN ('information_schema','pg_catalog') "
                        "ORDER BY 1"
                    ))
                    tables = [r[0] for r in res]
                except Exception:
                    pass
            engine.dispose()
        except Exception:
            pass
    elif source == "libsql":
        try:
            import json as _jmod, urllib.request as _ulr
            libsql_url = record.get("libsql_url", "").strip()
            auth_token = record.get("auth_token", "").strip()
            http_url = libsql_url
            if http_url.startswith("libsql://"):
                http_url = "https://" + http_url[len("libsql://"):]
            ep = http_url.rstrip("/") + "/v2/pipeline"
            payload = _jmod.dumps({
                "requests": [
                    {"type": "execute", "stmt": {
                        "sql": "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
                    }},
                    {"type": "close"},
                ]
            }).encode()
            req = _ulr.Request(ep, data=payload, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {auth_token}" if auth_token else "",
            })
            with _ulr.urlopen(req, timeout=10) as resp:
                body = _jmod.loads(resp.read())
            res = body["results"][0]
            if res.get("type") != "error":
                rows = res["response"]["result"]["rows"]
                tables = [row[0].get("value", "") for row in rows if row]
        except Exception:
            pass
    elif source == "clickhouse":
        try:
            import re as _re2
            conn_url_ch = _resolve_conn_url(record.get("conn_url", ""))
            m_ch = _re2.match(
                r"(?:clickhouse(?:\+\w+)?://)(?:([^:@]+)(?::([^@]*))?@)?([^:/?]+)(?::(\d+))?(?:/([^?]*))?",
                conn_url_ch,
            )
            if m_ch:
                import clickhouse_connect as _ch
                ch_client = _ch.get_client(
                    host=m_ch.group(3) or "localhost",
                    port=int(m_ch.group(4) or 8123),
                    username=m_ch.group(1) or "default",
                    password=m_ch.group(2) or "",
                    database=m_ch.group(5) or "default",
                    send_receive_timeout=10,
                )
                res = ch_client.query("SHOW TABLES")
                tables = [r[0] for r in res.result_rows]
        except Exception:
            pass

    # Fall back to schema.json for tables list if still empty
    if not tables:
        schema_path = OUTPUT_DIR / key / "schema.json"
        erd_tables = _schema_to_erd_data(schema_path) or []
        for t in erd_tables:
            if t["name"] not in tables:
                tables.append(t["name"])
            if t["name"] not in columns:
                columns[t["name"]] = [c["name"] for c in t.get("columns", [])]

    return {"tables": tables, "columns": columns}


@app.route("/api/autocomplete/<key>")
def api_autocomplete(key: str):
    """Return table and column names for CodeMirror SQL autocomplete.

    Response: {"tables": [str], "columns": {tableName: [colName]}}
    Results are cached in-memory for AUTOCOMPLETE_TTL seconds.
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    record = reg[key]

    now = time.time()
    cached = _autocomplete_cache.get(key)
    if cached and now - cached[0] < _AUTOCOMPLETE_TTL:
        return jsonify(cached[1])

    payload = _build_autocomplete(key, record)
    _autocomplete_cache[key] = (now, payload)
    return jsonify(payload)


# ── ERD table detail ──────────────────────────────────────────────────────────

@app.route("/api/erd/table-detail/<key>/<path:table_name>")
def api_erd_table_detail(key: str, table_name: str):
    """Return extended detail for a single table/view in the ERD.

    Response:
      {
        "index_count": int | null,   # number of indexes (null for views / live DBs)
        "is_virtual":  bool,         # true for views and virtual tables
        "ddl":         str | null,   # CREATE statement from sqlite_master / info_schema
      }
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    record = reg[key]
    source = record.get("source", "file")

    index_count: int | None = None
    is_virtual = False
    ddl: str | None = None

    # For join schemas, resolve alias.TableName → member key + real table name
    lookup_key = key
    lookup_table = table_name
    if source == "join":
        dot = table_name.find(".")
        if dot >= 0:
            alias       = table_name[:dot]
            lookup_table = table_name[dot + 1:]
            for m in record.get("members", []):
                if m.get("alias") == alias:
                    lookup_key = m["key"]
                    break
        with _lock:
            full_reg = _load_reg()
        lookup_record = full_reg.get(lookup_key, record)
        lookup_source = lookup_record.get("source", "file")
    else:
        lookup_source = source

    try:
        if lookup_source in ("file", ""):
            db_file = _find_sqlite_db(lookup_key)
            if db_file:
                con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
                cur = con.cursor()
                # Determine type from sqlite_master
                cur.execute(
                    "SELECT type, sql FROM sqlite_master WHERE name=? LIMIT 1",
                    (lookup_table,),
                )
                row = cur.fetchone()
                if row:
                    obj_type, ddl = row
                    is_virtual = obj_type in ("view", "virtual")
                    ddl = ddl or ""
                    if not is_virtual:
                        cur2 = con.cursor()
                        cur2.execute(f'PRAGMA index_list("{lookup_table}")')
                        index_count = len(cur2.fetchall())
                    else:
                        index_count = None
                con.close()
        elif lookup_source in ("connstring", "mssql"):
            from sqlalchemy import create_engine, text as sa_text
            with _lock:
                lr = _load_reg()
            lookup_rec = lr.get(lookup_key, record)
            engine = create_engine(
                _resolve_conn_url(lookup_rec.get("conn_url", "")),
                connect_args={"connect_timeout": 5},
            )
            with engine.connect() as conn:
                try:
                    res = conn.execute(sa_text(
                        "SELECT table_type FROM information_schema.tables "
                        "WHERE table_name = :t LIMIT 1"
                    ), {"t": lookup_table})
                    r = res.fetchone()
                    if r:
                        is_virtual = str(r[0]).upper() == "VIEW"
                except Exception:
                    pass
            engine.dispose()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({
        "index_count": index_count,
        "is_virtual":  is_virtual,
        "ddl":         ddl,
    })


# ── T1-3 support: per-table freshness timestamps ─────────────────────────────

@app.route("/api/freshness/<key>")
def api_freshness(key: str):
    """Return per-table last-updated timestamps for T1-3 freshness badges.

    Derives timestamps from the most recent row modification time available:
      - SQLite: max(rowid) converted to approximate time via file mtime fallback,
        or explicit last_updated columns when present (common naming patterns).
      - Falls back to metadata sidecar last_updated fields.
      - Returns {} for connstring / join schemas without a local SQLite file.

    Response: { "tableName": <unix_ms_int> | null, ... }
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404

    # Start from metadata sidecar values (user-set or previously stored)
    meta = _load_meta(key)
    result: dict[str, int | None] = {}
    for tname, tmeta in (meta.get("tables") or {}).items():
        lu = tmeta.get("last_updated")
        if lu is not None:
            if isinstance(lu, (int, float)):
                result[tname] = int(lu)
            else:
                try:
                    from datetime import datetime
                    ts = datetime.fromisoformat(str(lu)).timestamp()
                    result[tname] = int(ts * 1000)
                except Exception:
                    pass

    # Supplement from SQLite file if available
    db_file = _find_sqlite_db(key)
    if db_file:
        try:
            con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            cur = con.cursor()
            tables_res = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            for (tname,) in tables_res:
                if tname in result:
                    continue  # sidecar value takes priority
                # Look for common last-modified column patterns
                try:
                    cur2 = con.cursor()
                    cols_res = cur2.execute(f'PRAGMA table_info("{tname}")').fetchall()
                    ts_cols = [
                        c[1] for c in cols_res
                        if any(p in c[1].lower() for p in
                               ('updated_at', 'modified_at', 'last_modified',
                                'changed_at', 'timestamp', 'created_at'))
                    ]
                    if ts_cols:
                        col = ts_cols[0]
                        row = cur2.execute(
                            f'SELECT MAX("{col}") FROM "{tname}"'
                        ).fetchone()
                        if row and row[0] is not None:
                            raw = row[0]
                            if isinstance(raw, (int, float)):
                                # Could be unix seconds or ms - heuristic
                                if raw > 1_000_000_000_000:
                                    result[tname] = int(raw)
                                else:
                                    result[tname] = int(raw * 1000)
                            else:
                                try:
                                    from datetime import datetime
                                    ts = datetime.fromisoformat(str(raw)).timestamp()
                                    result[tname] = int(ts * 1000)
                                except Exception:
                                    result[tname] = None
                    else:
                        result[tname] = None
                except Exception:
                    result[tname] = None
            con.close()
        except Exception:
            pass

    return jsonify(result)


# ── Triggers ──────────────────────────────────────────────────────────────────

def _parse_trigger_meta(ddl: str) -> tuple[str, str, str]:
    """Extract (event, timing, table) from a SQLite trigger DDL string.

    Returns ("", "", "") if parsing fails.
    """
    import re as _re_t
    m = _re_t.search(
        r"CREATE\s+(?:TEMP(?:ORARY)?\s+)?TRIGGER\s+\S+\s+"
        r"(BEFORE|AFTER|INSTEAD\s+OF)\s+"
        r"(INSERT|UPDATE|DELETE|UPDATE\s+OF\s+\S+)\s+"
        r"ON\s+['\"`]?(\w+)['\"`]?",
        ddl, _re_t.IGNORECASE
    )
    if not m:
        return ("", "", "")
    timing = m.group(1).upper()
    event  = m.group(2).upper().split()[0]  # strip "OF col"
    table  = m.group(3)
    return (event, timing, table)


@app.route("/api/triggers/<key>")
def api_triggers(key: str):
    """Return all triggers for a schema.

    Response: [{"name": str, "table": str, "event": str, "timing": str, "ddl": str}]
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    record = reg[key]
    source = record.get("source", "file")

    triggers: list[dict] = []

    try:
        if source in ("file", ""):
            db_file = _find_sqlite_db(key)
            if db_file:
                con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
                cur = con.cursor()
                cur.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
                )
                for (trig_name, ddl) in cur.fetchall():
                    event, timing, tbl = _parse_trigger_meta(ddl or "")
                    triggers.append({
                        "name":   trig_name,
                        "table":  tbl,
                        "event":  event,
                        "timing": timing,
                        "ddl":    ddl or "",
                    })
                con.close()
        elif source in ("connstring", "mssql"):
            from sqlalchemy import create_engine, text as sa_text
            engine = create_engine(
                _resolve_conn_url(record.get("conn_url", "")),
                connect_args={"connect_timeout": 5},
            )
            try:
                with engine.connect() as conn:
                    res = conn.execute(sa_text(
                        "SELECT trigger_name, event_object_table, event_manipulation, "
                        "action_timing "
                        "FROM information_schema.triggers ORDER BY trigger_name"
                    ))
                    for row in res:
                        triggers.append({
                            "name":   row[0],
                            "table":  row[1],
                            "event":  row[2],
                            "timing": row[3],
                            "ddl":    "",
                        })
            finally:
                engine.dispose()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(triggers)


# ── Join CRUD ─────────────────────────────────────────────────────────────────

@app.route("/api/joins", methods=["POST"])
def api_joins_create():
    """Create a cross-schema join entry.

    Body: {"name": str, "members": [{"key": str, "alias": str}, ...]}
    Validates that all member keys are file-based and ready, then creates
    a join entry in the registry with source="join".
    """
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    members = data.get("members") or []
    if not name:
        return jsonify({"error": "name is required"}), 400
    if len(members) < 2:
        return jsonify({"error": "At least 2 members are required"}), 400

    with _lock:
        reg = _load_reg()
        # Validate all members
        for m in members:
            mk = m.get("key", "")
            if mk not in reg:
                return jsonify({"error": f"Unknown member key: {mk}"}), 400
            rec = reg[mk]
            if rec.get("status") != "ready":
                return jsonify({"error": f"Member '{rec.get('name', mk)}' is not ready"}), 400
            if rec.get("source") not in ("file", None, ""):
                # Allow only file-based schemas
                if rec.get("source") not in ("file",):
                    return jsonify({"error": f"Member '{rec.get('name', mk)}' is not a file-based schema"}), 400

        # Compute stable join key from members list
        join_key = "jk_" + hashlib.sha256(
            json.dumps(members, sort_keys=True).encode()
        ).hexdigest()[:12]

        # Compute table count from merged schema
        table_count = sum(
            len(_schema_to_erd_data(OUTPUT_DIR / m["key"] / "schema.json") or [])
            for m in members
        )

        if join_key not in reg:
            reg[join_key] = {
                "key":      join_key,
                "name":     name,
                "source":   "join",
                "status":   "ready",
                "members":  members,
                "tables":   table_count,
                "fks":      0,
                "uploaded": time.time(),
                "processed": time.time(),
                "log":      [],
                "error_msg": None,
            }
            _save_reg(reg)

    # Invalidate autocomplete cache so the join sidebar shows fresh tables
    _autocomplete_cache.pop(join_key, None)
    return jsonify({"join_key": join_key, "name": name, "status": "ready"})


@app.route("/api/joins", methods=["GET"])
def api_joins_list():
    """Return all join entries from the registry."""
    with _lock:
        reg = _load_reg()
    joins = [r for r in reg.values() if r.get("source") == "join"]
    return jsonify(joins)


@app.route("/api/joins/<join_key>", methods=["PUT"])
def api_joins_update(join_key: str):
    """Update an existing join entry's name and/or member set.

    Body: {"name": str, "members": [{"key": str, "alias": str}, ...]}
    Validates members and updates the registry in-place.  The join_key itself
    is preserved so existing bookmarks / open tabs stay valid.
    """
    data    = request.get_json(force=True, silent=True) or {}
    name    = (data.get("name") or "").strip()
    members = data.get("members") or []
    if not name:
        return jsonify({"error": "name is required"}), 400
    if len(members) < 2:
        return jsonify({"error": "At least 2 members are required"}), 400

    with _lock:
        reg = _load_reg()
        if join_key not in reg:
            return jsonify({"error": "Unknown join key"}), 404
        if reg[join_key].get("source") != "join":
            return jsonify({"error": "Not a join entry"}), 400
        # Validate all members
        for m in members:
            mk = m.get("key", "")
            if mk not in reg:
                return jsonify({"error": f"Unknown member key: {mk}"}), 400
            rec = reg[mk]
            if rec.get("status") != "ready":
                return jsonify({"error": f"Member '{rec.get('name', mk)}' is not ready"}), 400
            if rec.get("source") not in ("file", None, ""):
                if rec.get("source") not in ("file",):
                    return jsonify({"error": f"Member '{rec.get('name', mk)}' is not a file-based schema"}), 400
        # Recompute table count
        table_count = sum(
            len(_schema_to_erd_data(OUTPUT_DIR / m["key"] / "schema.json") or [])
            for m in members
        )
        reg[join_key]["name"]      = name
        reg[join_key]["members"]   = members
        reg[join_key]["tables"]    = table_count
        reg[join_key]["processed"] = time.time()
        _save_reg(reg)
    # Invalidate autocomplete cache so the join sidebar shows updated tables
    _autocomplete_cache.pop(join_key, None)
    return jsonify({"join_key": join_key, "name": name, "status": "ready"})


@app.route("/api/joins/<join_key>", methods=["DELETE"])
def api_joins_delete(join_key: str):
    """Remove a join entry from the registry (does not touch member schemas)."""
    with _lock:
        reg = _load_reg()
        if join_key not in reg:
            return jsonify({"error": "Unknown join key"}), 404
        if reg[join_key].get("source") != "join":
            return jsonify({"error": "Not a join entry"}), 400
        del reg[join_key]
        _save_reg(reg)
    return jsonify({"deleted": join_key})


@app.route("/browse/<key>")
def browse_view(key: str):
    """Table browser page.  Join keys get a merged-schema browse view."""
    with _lock:
        reg = _load_reg()
    record = reg.get(key)
    if not record:
        return "Schema not found", 404
    if record.get("source") == "join":
        tables  = _merge_join_schema(record, reg)
        members = record.get("members", [])   # [{key, alias}, ...]
        return render_template(
            "browse.html",
            key=key,
            name=record.get("name", key),
            tables=tables,
            source="join",
            members=members,
            conn_url="",
            allow_writes=False,  # join schemas are read-only
        )
    schema_path = OUTPUT_DIR / key / "schema.json"
    tables = _schema_to_erd_data(schema_path) or []
    source = record.get("source", "file")
    # Writes only available for file-backed SQLite schemas when ALLOW_WRITES=1
    writes_ok = ALLOW_WRITES and source not in ("connstring", "join")
    return render_template(
        "browse.html",
        key=key,
        name=record.get("name", key),
        tables=tables,
        source=source,
        members=[],
        conn_url=record.get("conn_url", ""),
        allow_writes=writes_ok,
    )


@app.route("/query/<key>")
def query_view(key: str):
    """SQL query editor page."""
    with _lock:
        reg = _load_reg()
    record = reg.get(key)
    if not record:
        return "Schema not found", 404
    schema_path = OUTPUT_DIR / key / "schema.json"
    tables = _schema_to_erd_data(schema_path) or []
    schema_json = json.dumps(tables, ensure_ascii=False)
    return render_template(
        "query.html",
        key=key,
        name=record.get("name", key),
        tables=tables,
        schema_json=schema_json,
        source=record.get("source", "file"),
    )


@app.route("/join/<join_key>")
def join_query_view(join_key: str):
    """SQL query editor for a cross-schema join.

    Renders query.html with the merged (alias-prefixed) schema so the sidebar
    shows all tables from all member schemas grouped under their aliases.

    Also eagerly triggers _find_sqlite_db() for every member so that flat-file
    members (CSV, XLSX, …) have their .db generated before the page is served.
    This prevents the "no such table" error on the very first query in a new
    session, which previously required visiting each member source individually
    to trigger the lazy .db conversion.
    """
    with _lock:
        reg = _load_reg()
    record = reg.get(join_key)
    if not record:
        return "Join not found", 404
    if record.get("source") != "join":
        return "Not a join entry", 400

    # Eagerly ensure every member's SQLite .db exists before rendering.
    # This is a no-op for members whose .db is already present.
    for m in record.get("members", []):
        try:
            _find_sqlite_db(m["key"])
        except Exception:
            pass  # best-effort; errors surface at query-execution time

    tables = _merge_join_schema(record, reg)
    schema_json = json.dumps(tables, ensure_ascii=False)
    return render_template(
        "query.html",
        key=join_key,
        name=record.get("name", join_key),
        tables=tables,
        schema_json=schema_json,
        source="join",
    )


# ── Phase 6: Report builder ───────────────────────────────────────────────────

def _reports_path(key: str) -> Path:
    return OUTPUT_DIR / key / "reports.json"


def _load_reports(key: str) -> list:
    p = _reports_path(key)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_reports(key: str, reports: list) -> None:
    p = _reports_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")


def _exec_query_for_report(
    key: str,
    sql: str,
    limit: int = 500,
    group_by_col: str = "",
    agg_func: str = "COUNT",
    agg_col: str = "",
    value_label: str = "",
) -> dict:
    """Execute a read-only SQL query and return {columns, rows, error}.

    When group_by_col is set the user's SQL is used as a subquery and the
    result is aggregated: SELECT group_by, AGG(agg_col) … GROUP BY group_by.
    value_label overrides the default '_value' column alias.
    """
    if not sql or not sql.strip():
        return {"columns": [], "rows": [], "error": None}

    with _lock:
        reg = _load_reg()
    record = reg.get(key)
    if not record:
        return {"columns": [], "rows": [], "error": "Unknown schema key"}

    sql_exec = sql.strip()
    first_token = sql_exec.upper().split()[0] if sql_exec.split() else ""
    if first_token not in ("SELECT", "WITH", "EXPLAIN", "PRAGMA"):
        return {"columns": [], "rows": [], "error": "Only SELECT/WITH queries are permitted"}

    # Build aggregation wrapper when group_by is requested
    agg_func = (agg_func or "COUNT").upper().strip()
    if agg_func not in {"COUNT", "SUM", "AVG", "MIN", "MAX"}:
        agg_func = "COUNT"
    if group_by_col:
        agg_expr = (
            "COUNT(*)"
            if agg_func == "COUNT" and not agg_col
            else f'{agg_func}("{agg_col}")'
            if agg_col
            else "COUNT(*)"
        )
        val_alias = value_label.strip() if value_label.strip() else "_value"
        sql_exec = (
            f'SELECT "{group_by_col}" AS _label, {agg_expr} AS "{val_alias}" '
            f'FROM ({sql_exec}) _src '
            f'GROUP BY "{group_by_col}" ORDER BY "{val_alias}" DESC LIMIT {limit}'
        )
    elif first_token in ("SELECT", "WITH") and "LIMIT" not in sql_exec.upper():
        sql_exec = f"SELECT * FROM ({sql_exec}) _q LIMIT {limit + 1}"

    try:
        source = record.get("source", "file")
        if source == "connstring":
            from sqlalchemy import create_engine, text as sa_text
            engine = create_engine(_resolve_conn_url(record.get("conn_url", "")),
                                   connect_args={"connect_timeout": 10})
            with engine.connect() as conn:
                result = conn.execute(sa_text(sql_exec))
                cols = list(result.keys())
                rows = [list(r) for r in result.fetchall()]
            engine.dispose()
        else:
            db_file = _find_sqlite_db(key)
            if not db_file:
                return {"columns": [], "rows": [], "error": "No queryable source"}
            import sqlite3
            con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute(sql_exec)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [list(r) for r in cur.fetchall()]
            con.close()
        truncated = len(rows) > limit
        return {"columns": cols, "rows": rows[:limit], "truncated": truncated, "error": None}
    except Exception as exc:
        return {"columns": [], "rows": [], "error": str(exc)}


@app.route("/api/reports/<key>", methods=["GET"])
def api_reports_list(key: str):
    """List all reports for a schema."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    return jsonify(_load_reports(key))


@app.route("/api/reports/<key>", methods=["POST"])
def api_reports_create(key: str):
    """Create a new report.
    Body: {"title": str, "blocks": [{"type": "text"|"query", ...}]}
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip() or "Untitled Report"
    blocks = data.get("blocks") or []
    rid = str(int(time.time() * 1000))
    entry = {"id": rid, "title": title, "created": time.time(), "blocks": blocks}
    reports = _load_reports(key)
    reports.append(entry)
    _save_reports(key, reports)
    return jsonify(entry), 201


@app.route("/api/reports/<key>/<rid>", methods=["PATCH"])
def api_reports_update(key: str, rid: str):
    """Update title and/or blocks of a report."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    data = request.get_json(force=True, silent=True) or {}
    reports = _load_reports(key)
    for r in reports:
        if r["id"] == rid:
            if "title" in data:
                r["title"] = str(data["title"]).strip() or "Untitled Report"
            if "blocks" in data:
                r["blocks"] = data["blocks"]
            r["updated"] = time.time()
            _save_reports(key, reports)
            return jsonify(r)
    return jsonify({"error": "Report not found"}), 404


@app.route("/api/reports/<key>/<rid>", methods=["DELETE"])
def api_reports_delete(key: str, rid: str):
    """Delete a report by id."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    reports = _load_reports(key)
    before = len(reports)
    reports = [r for r in reports if r["id"] != rid]
    if len(reports) == before:
        return jsonify({"error": "Report not found"}), 404
    _save_reports(key, reports)
    return jsonify({"ok": True})


@app.route("/report/<key>")
def report_builder(key: str):
    """Report builder page - create and edit reports for a schema."""
    with _lock:
        reg = _load_reg()
    record = reg.get(key)
    if not record:
        return "Schema not found", 404
    schema_path = OUTPUT_DIR / key / "schema.json"
    tables = _schema_to_erd_data(schema_path) or []
    return render_template(
        "report.html",
        key=key,
        name=record.get("name", key),
        tables=tables,
    )


@app.route("/report/<key>/<rid>")
def report_view(key: str, rid: str):
    """Render a report: execute all query blocks and return a print-ready HTML page.

    Query param ?_format=html → download as self-contained .html file.
    """
    with _lock:
        reg = _load_reg()
    record = reg.get(key)
    if not record:
        return "Schema not found", 404

    reports = _load_reports(key)
    report = next((r for r in reports if r["id"] == rid), None)
    if not report:
        return "Report not found", 404

    # Load saved queries index so query blocks can look up SQL by qid
    saved_queries = {q["id"]: q for q in _load_queries(key)}

    # Hydrate each block
    rendered_blocks = []
    for block in report.get("blocks", []):
        btype = block.get("type", "text")
        if btype == "text":
            rendered_blocks.append({
                "type": "text",
                "content": block.get("content", ""),
            })
        elif btype in ("query", "chart"):
            # "chart" is a legacy type - treat same as query with display_mode=chart
            qid = block.get("qid", "")
            q = saved_queries.get(qid)
            sql = (q["sql"] if q else block.get("sql", ""))
            # Saved-chart blocks have no SQL - reconstruct from table name
            chart_table = block.get("chart_table", "").strip()
            if not sql and chart_table:
                sql = f'SELECT * FROM "{chart_table}"'
            title = block.get("title") or (q["name"] if q else "Query")
            display_mode = block.get("display_mode", "chart" if btype == "chart" else "table")
            group_by_col = block.get("group_by_col", "").strip()
            agg_func     = block.get("agg_func", "COUNT").strip()
            agg_col      = block.get("agg_col", "").strip()
            agg_limit    = min(int(block.get("agg_limit") or 50), 500)
            value_label  = block.get("value_label", "").strip()
            # For chart/both mode with a group-by set, run the aggregation query
            use_agg = bool(group_by_col) and display_mode in ("chart", "both")
            result = (
                _exec_query_for_report(key, sql,
                                       limit=agg_limit,
                                       group_by_col=group_by_col,
                                       agg_func=agg_func,
                                       agg_col=agg_col,
                                       value_label=value_label)
                if sql and use_agg
                else _exec_query_for_report(key, sql)
                if sql
                else {"columns": [], "rows": [], "error": None}
            )
            rendered_blocks.append({
                "type": "query",
                "title": title,
                "sql": sql,
                "display_mode": display_mode,
                "chart_type": block.get("chart_type", "bar"),
                "group_by_col": group_by_col,
                "agg_func": agg_func,
                "agg_col": agg_col,
                "agg_limit": agg_limit,
                "value_label": value_label,
                # When aggregated, columns are always [_label, _value]
                "columns": result.get("columns", []),
                "rows": result.get("rows", []),
                "truncated": result.get("truncated", False),
                "error": result.get("error"),
            })

    download = request.args.get("_format") == "html"
    rendered = render_template(
        "report_view.html",
        key=key,
        name=record.get("name", key),
        report=report,
        blocks=rendered_blocks,
        generated_at=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        download=download,
    )
    if download:
        safe_title = "".join(c if c.isalnum() or c in "-_ " else "_"
                             for c in report.get("title", "report"))[:60]
        return Response(
            rendered,
            mimetype="text/html",
            headers={"Content-Disposition": f'attachment; filename="{safe_title}.html"'},
        )
    return rendered


# ── Phase 9: Schema diff ──────────────────────────────────────────────────────

def _load_schema_tables(key: str) -> dict[str, dict]:
    """Return {tableName: {columns: {colName: type}, ...}} for a schema key.

    Used by the diff engine.  Returns {} if the schema cannot be read.
    """
    path = OUTPUT_DIR / key / "schema.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    # Liam keyed-object format
    if isinstance(raw, dict) and isinstance(raw.get("tables"), dict):
        out = {}
        for tname, tdata in raw["tables"].items():
            cols = {}
            for cname, cdata in (tdata.get("columns") or {}).items():
                cols[cname] = (cdata.get("type") or "TEXT").upper()
            out[tname] = cols
        return out

    # Old array format
    if isinstance(raw, dict) and isinstance(raw.get("tables"), list):
        out = {}
        for t in raw["tables"]:
            cols = {}
            for c in t.get("columns", []):
                cols[c["name"]] = (c.get("type") or "TEXT").upper()
            out[t["name"]] = cols
        return out

    return {}


def _compute_diff(tables_a: dict, tables_b: dict) -> dict:
    """Compare two {tableName: {colName: type}} dicts.

    Returns:
      {
        "added":    [{"name": str, "columns": {colName: type}}],
        "removed":  [{"name": str, "columns": {colName: type}}],
        "modified": [{"name": str, "added_cols": [...], "removed_cols": [...],
                      "changed_cols": [{"name", "from", "to"}]}],
        "unchanged": int,
      }
    """
    names_a = set(tables_a)
    names_b = set(tables_b)

    added   = [{"name": n, "columns": tables_b[n]} for n in sorted(names_b - names_a)]
    removed = [{"name": n, "columns": tables_a[n]} for n in sorted(names_a - names_b)]

    modified = []
    unchanged = 0
    for name in sorted(names_a & names_b):
        ca, cb = tables_a[name], tables_b[name]
        keys_a, keys_b = set(ca), set(cb)
        added_cols   = sorted(keys_b - keys_a)
        removed_cols = sorted(keys_a - keys_b)
        changed_cols = [
            {"name": c, "from": ca[c], "to": cb[c]}
            for c in sorted(keys_a & keys_b)
            if ca[c] != cb[c]
        ]
        if added_cols or removed_cols or changed_cols:
            modified.append({
                "name": name,
                "added_cols":   [{"name": c, "type": cb[c]} for c in added_cols],
                "removed_cols": [{"name": c, "type": ca[c]} for c in removed_cols],
                "changed_cols": changed_cols,
            })
        else:
            unchanged += 1

    return {
        "added":     added,
        "removed":   removed,
        "modified":  modified,
        "unchanged": unchanged,
    }


@app.route("/api/diff/<key_a>/<key_b>")
def api_diff(key_a: str, key_b: str):
    """Return a schema diff JSON between two registered schemas.

    Returns:
      {
        "schema_a": {"key": str, "name": str},
        "schema_b": {"key": str, "name": str},
        "diff": { "added": [...], "removed": [...], "modified": [...], "unchanged": int }
      }
    """
    with _lock:
        reg = _load_reg()
    rec_a = reg.get(key_a)
    rec_b = reg.get(key_b)
    if not rec_a:
        return jsonify({"error": f"Schema '{key_a}' not found"}), 404
    if not rec_b:
        return jsonify({"error": f"Schema '{key_b}' not found"}), 404

    tables_a = _load_schema_tables(key_a)
    tables_b = _load_schema_tables(key_b)
    diff = _compute_diff(tables_a, tables_b)

    return jsonify({
        "schema_a": {"key": key_a, "name": rec_a.get("name", key_a)},
        "schema_b": {"key": key_b, "name": rec_b.get("name", key_b)},
        "diff": diff,
    })


@app.route("/diff/<key_a>/<key_b>")
def diff_view(key_a: str, key_b: str):
    """Render the schema diff viewer page."""
    with _lock:
        reg = _load_reg()
    rec_a = reg.get(key_a)
    rec_b = reg.get(key_b)
    if not rec_a:
        return f"Schema '{key_a}' not found", 404
    if not rec_b:
        return f"Schema '{key_b}' not found", 404

    tables_a = _load_schema_tables(key_a)
    tables_b = _load_schema_tables(key_b)
    diff = _compute_diff(tables_a, tables_b)

    return render_template(
        "diff.html",
        key_a      = key_a,
        key_b      = key_b,
        name_a     = rec_a.get("name", key_a),
        name_b     = rec_b.get("name", key_b),
        diff       = diff,
        diff_json  = json.dumps(diff, ensure_ascii=False),
        tables_a_json = json.dumps(tables_a, ensure_ascii=False),
        tables_b_json = json.dumps(tables_b, ensure_ascii=False),
    )


# ── Phase 10: Dashboard tiles (independent tile objects) ──────────────────────

# Tiles are stored independently from queries so one query can power many tiles.
# Storage: /output/erd/<key>/tiles.json  - list of tile dicts.

def _tiles_path(key: str) -> Path:
    return OUTPUT_DIR / key / "tiles.json"


def _load_tiles(key: str) -> list:
    p = _tiles_path(key)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_tiles(key: str, tiles: list) -> None:
    p = _tiles_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tiles, ensure_ascii=False, indent=2), encoding="utf-8")


@app.route("/api/tiles/<key>", methods=["GET"])
def api_tiles_list(key: str):
    """List all dashboard tiles for a schema."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    return jsonify(_load_tiles(key))


@app.route("/api/tiles/<key>", methods=["POST"])
def api_tiles_create(key: str):
    """Create a new dashboard tile.
    Body: {"query_id": str, "title": str, "type": str,
           "x_col": str, "y_col": str, "agg_fn": str,
           "num_col": str, "color": str}
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    data = request.get_json(force=True, silent=True) or {}
    if not data.get("query_id"):
        return jsonify({"error": "query_id is required"}), 400
    tid = str(int(time.time() * 1000))
    entry = {
        "id":       tid,
        "query_id": data.get("query_id"),
        "title":    data.get("title", "Tile"),
        "type":     data.get("type", "bar"),
        "x_col":    data.get("x_col", ""),
        "y_col":    data.get("y_col", ""),
        "agg_fn":   data.get("agg_fn", "none"),
        "num_col":  data.get("num_col", ""),
        "color":    data.get("color", ""),
        "created":  time.time(),
    }
    tiles = _load_tiles(key)
    tiles.append(entry)
    _save_tiles(key, tiles)
    return jsonify(entry), 201


@app.route("/api/tiles/<key>/<tid>", methods=["PATCH"])
def api_tiles_update(key: str, tid: str):
    """Update a dashboard tile's config."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    data = request.get_json(force=True, silent=True) or {}
    tiles = _load_tiles(key)
    for t in tiles:
        if t["id"] == tid:
            for field in ("title", "type", "x_col", "y_col", "agg_fn", "num_col", "color"):
                if field in data:
                    t[field] = data[field]
            t["updated"] = time.time()
            _save_tiles(key, tiles)
            return jsonify(t)
    return jsonify({"error": "Tile not found"}), 404


@app.route("/api/tiles/<key>/<tid>", methods=["DELETE"])
def api_tiles_delete(key: str, tid: str):
    """Delete a dashboard tile."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    tiles = _load_tiles(key)
    before = len(tiles)
    tiles = [t for t in tiles if t["id"] != tid]
    if len(tiles) == before:
        return jsonify({"error": "Tile not found"}), 404
    _save_tiles(key, tiles)
    return jsonify({"ok": True})


# Legacy: keep the old PATCH /api/queries/<key>/<qid>/tile route so any
# existing pinned-query tiles continue to work (backward compat).
@app.route("/api/queries/<key>/<qid>/tile", methods=["PATCH"])
def api_query_tile(key: str, qid: str):
    """Legacy: set or clear the dashboard tile config on a query object."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    data = request.get_json(force=True, silent=True) or {}
    queries = _load_queries(key)
    for q in queries:
        if q["id"] == qid:
            if data.get("tile") is None:
                q.pop("tile", None)
            else:
                q["tile"] = data["tile"]
            q["updated"] = time.time()
            _save_queries(key, queries)
            return jsonify(q)
    return jsonify({"error": "Query not found"}), 404


@app.route("/dashboard/<key>")
def dashboard_view(key: str):
    """Query dashboard - shows pinned saved queries as live chart tiles."""
    with _lock:
        reg = _load_reg()
    record = reg.get(key)
    if not record:
        return "Schema not found", 404
    schema_path = OUTPUT_DIR / key / "schema.json"
    tables = _schema_to_erd_data(schema_path) or []
    schema_json = json.dumps(tables, ensure_ascii=False)
    return render_template(
        "dashboard.html",
        key         = key,
        name        = record.get("name", key),
        tables      = tables,
        schema_json = schema_json,
        source      = record.get("source", "file"),
    )


# ── Phase H: Query history log ────────────────────────────────────────────────

def _history_path(key: str) -> Path:
    return OUTPUT_DIR / key / "history.json"


# ── Audit log (Rank 9) ────────────────────────────────────────────────────────

def _audit_path(key: str) -> Path:
    return OUTPUT_DIR / key / "audit.jsonl"


def _write_audit(
    key: str,
    action: str,
    sql: str,
    rows: int,
    ok: bool,
) -> None:
    """Append one audit entry to the per-schema JSONL log (max 500 lines).

    Each entry is a single JSON object on its own line:
      {"ts": <unix float>, "action": str, "sql": str, "rows": int, "ok": bool}

    The file is capped at 500 lines to prevent unbounded growth.  Writing is
    best-effort; any failure is silently swallowed so callers are never blocked.
    """
    p = _audit_path(key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({
            "ts":     time.time(),
            "action": action,
            "sql":    sql,
            "rows":   rows,
            "ok":     ok,
        }, ensure_ascii=False)
        # Read existing lines, prepend new entry, cap to 500
        if p.exists():
            lines = p.read_text(encoding="utf-8").splitlines()
        else:
            lines = []
        lines.insert(0, entry)
        lines = lines[:500]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def _load_history(key: str) -> list:
    p = _history_path(key)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _append_history(key: str, sql: str, row_count: int, elapsed_ms: int) -> None:
    """Append one entry to the query history log (max 500 entries)."""
    p = _history_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        hist = _load_history(key)
    except Exception:
        hist = []
    hist.insert(0, {
        "ts":         time.time(),
        "sql":        sql,
        "row_count":  row_count,
        "elapsed_ms": elapsed_ms,
    })
    hist = hist[:500]
    try:
        p.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


@app.route("/api/history/<key>", methods=["GET"])
def api_history_list(key: str):
    """Return the last 500 query executions for a schema.

    Returns: [{"ts": float, "sql": str, "row_count": int, "elapsed_ms": int}, ...]
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    return jsonify(_load_history(key))


@app.route("/api/history/<key>", methods=["DELETE"])
def api_history_clear(key: str):
    """Clear the query history for a schema."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    p = _history_path(key)
    if p.exists():
        p.write_text("[]", encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/api/audit/<key>", methods=["GET"])
def api_audit_list(key: str):
    """Return the last 500 audit log entries for a schema.

    Each entry is a JSON object: {"ts": float, "action": str, "sql": str,
    "rows": int, "ok": bool}.  Returns a JSON array sorted newest-first.

    Returns: [{"ts": float, "action": str, "sql": str, "rows": int, "ok": bool}, ...]
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    p = _audit_path(key)
    if not p.exists():
        return jsonify([])
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        entries = []
        for line in lines:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
        return jsonify(entries[:500])
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Phase G: Metrics layer ────────────────────────────────────────────────────

def _metrics_path(key: str) -> Path:
    return OUTPUT_DIR / key / "metrics.json"


def _load_metrics(key: str) -> list:
    p = _metrics_path(key)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_metrics(key: str, metrics: list) -> None:
    p = _metrics_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")


@app.route("/api/metrics/<key>", methods=["GET"])
def api_metrics_list(key: str):
    """List all named metric definitions for a schema.

    Each metric: {"id": str, "name": str, "sql": str, "table": str,
                  "format": str, "created": float}
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    return jsonify(_load_metrics(key))


@app.route("/api/metrics/<key>", methods=["POST"])
def api_metrics_create(key: str):
    """Create a named metric definition.
    Body: {"name": str, "sql": str, "table": str, "format": str}
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    sql  = (data.get("sql")  or "").strip()
    if not name or not sql:
        return jsonify({"error": "name and sql are required"}), 400
    metrics = _load_metrics(key)
    mid = str(int(time.time() * 1000))
    entry = {
        "id":      mid,
        "name":    name,
        "sql":     sql,
        "table":   (data.get("table") or "").strip(),
        "format":  (data.get("format") or "").strip(),
        "created": time.time(),
    }
    metrics.append(entry)
    _save_metrics(key, metrics)
    return jsonify(entry), 201


@app.route("/api/metrics/<key>/<mid>", methods=["PATCH"])
def api_metrics_update(key: str, mid: str):
    """Update a metric definition."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    data = request.get_json(force=True, silent=True) or {}
    metrics = _load_metrics(key)
    for m in metrics:
        if m["id"] == mid:
            for field in ("name", "sql", "table", "format"):
                if field in data:
                    m[field] = str(data[field]).strip()
            m["updated"] = time.time()
            _save_metrics(key, metrics)
            return jsonify(m)
    return jsonify({"error": "Metric not found"}), 404


@app.route("/api/metrics/<key>/<mid>", methods=["DELETE"])
def api_metrics_delete(key: str, mid: str):
    """Delete a metric definition."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    metrics = _load_metrics(key)
    before = len(metrics)
    metrics = [m for m in metrics if m["id"] != mid]
    if len(metrics) == before:
        return jsonify({"error": "Metric not found"}), 404
    _save_metrics(key, metrics)
    return jsonify({"ok": True})


# ── Dashboard snapshots (save/load named dashboard layouts) ───────────────────

def _snapshots_path(key: str) -> Path:
    return OUTPUT_DIR / key / "dashboard_snapshots.json"


def _load_snapshots(key: str) -> list:
    p = _snapshots_path(key)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_snapshots(key: str, snapshots: list) -> None:
    p = _snapshots_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snapshots, ensure_ascii=False, indent=2), encoding="utf-8")


@app.route("/api/dashboard_snapshots/<key>", methods=["GET"])
def api_snapshots_list(key: str):
    """List all saved dashboard snapshots for a schema."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    return jsonify(_load_snapshots(key))


@app.route("/api/dashboard_snapshots/<key>", methods=["POST"])
def api_snapshots_create(key: str):
    """Save a named dashboard snapshot (captures the current tile list).
    Body: {"name": str, "tile_ids": [str, ...]}
    """
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip() or "Dashboard"
    tile_ids = data.get("tile_ids") or []
    sid = str(int(time.time() * 1000))
    entry = {
        "id":       sid,
        "name":     name,
        "tile_ids": tile_ids,
        "created":  time.time(),
    }
    snapshots = _load_snapshots(key)
    snapshots.append(entry)
    _save_snapshots(key, snapshots)
    return jsonify(entry), 201


@app.route("/api/dashboard_snapshots/<key>/<sid>", methods=["PATCH"])
def api_snapshots_update(key: str, sid: str):
    """Update a snapshot name or tile_ids."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    data = request.get_json(force=True, silent=True) or {}
    snapshots = _load_snapshots(key)
    for s in snapshots:
        if s["id"] == sid:
            if "name" in data:
                s["name"] = str(data["name"]).strip() or "Dashboard"
            if "tile_ids" in data:
                s["tile_ids"] = data["tile_ids"]
            s["updated"] = time.time()
            _save_snapshots(key, snapshots)
            return jsonify(s)
    return jsonify({"error": "Snapshot not found"}), 404


@app.route("/api/dashboard_snapshots/<key>/<sid>", methods=["DELETE"])
def api_snapshots_delete(key: str, sid: str):
    """Delete a dashboard snapshot."""
    with _lock:
        reg = _load_reg()
    if key not in reg:
        return jsonify({"error": "Unknown key"}), 404
    snapshots = _load_snapshots(key)
    before = len(snapshots)
    snapshots = [s for s in snapshots if s["id"] != sid]
    if len(snapshots) == before:
        return jsonify({"error": "Snapshot not found"}), 404
    _save_snapshots(key, snapshots)
    return jsonify({"ok": True})


# ── Pipeline runner ───────────────────────────────────────────────────────────

def _run_pipeline(key: str) -> None:
    with _lock:
        reg = _load_reg()
        record = reg.get(key)
    if not record:
        return

    erd_dir   = OUTPUT_DIR / key
    log_lines: list = _live_logs.setdefault(key, [])
    source    = record.get("source", "file")

    def log(msg: str):
        stamped = f"[{_ts()}] {msg}"
        log_lines.append(stamped)

    log(f"INFO  Starting pipeline for '{record.get('name', key)}' (source={source})")

    try:
        if source == "connstring":
            _run_live_introspection(key, record, erd_dir, log)
        elif source == "libsql":
            _run_libsql_introspection(key, record, erd_dir, log)
        elif source == "clickhouse":
            _run_clickhouse_introspection(key, record, erd_dir, log)
        elif source == "mssql":
            _run_live_introspection(key, record, erd_dir, log)
        elif source == "mongodb":
            _run_mongodb_introspection(key, record, erd_dir, log)
        elif source in ("snowflake", "bigquery", "supabase"):
            # All three use standard SQLAlchemy introspection via conn_url
            _run_live_introspection(key, record, erd_dir, log)
        else:
            _run_file_pipeline(key, record, erd_dir, log)
    except Exception as exc:
        log(f"ERROR Unexpected error: {exc}")
        _finalize(key, False, log_lines)
        return


def _run_file_pipeline(key: str, record: dict, erd_dir: Path, log) -> None:
    """Run run_pipeline.py as a subprocess for file-based sources.

    If the uploaded file is a .zip archive, it is transparently extracted to
    the erd_dir before the pipeline subprocess is launched.  The subprocess
    receives the path of the extracted inner file, not the original .zip.
    """
    log_lines = _live_logs[key]
    input_path = record.get("path", "")

    if not input_path or not Path(input_path).exists():
        log(f"ERROR Source file not found: {input_path}")
        _finalize(key, False, log_lines)
        return

    pipeline_input = input_path  # may be overwritten by single-file ZIP extraction

    # Handle ZIP archives before running the pipeline subprocess.
    # Check by magic bytes (PK header) so files like "data.csv" that are actually
    # ZIP archives are caught regardless of their extension.
    _input_path_obj = Path(input_path)
    _is_zip = False
    try:
        with open(_input_path_obj, "rb") as _fh:
            _is_zip = _fh.read(2) == b"PK"
    except Exception:
        pass

    if _is_zip:
        try:
            import zipfile as _zipfile, tempfile as _tmpfile, io as _io

            with _zipfile.ZipFile(_input_path_obj) as zf:
                # Filter real files; skip directories
                members = [m for m in zf.infolist() if not m.is_dir()]

            _supported_exts = {".csv", ".json", ".jsonl", ".ndjson", ".sql",
                                ".db", ".sqlite", ".sqlite3", ".parquet", ".xlsx", ".xls"}
            supported = [m for m in members
                         if Path(m.filename).suffix.lower() in _supported_exts]

            if not supported:
                log("ERROR ZIP archive contains no recognised file type. "
                    "Supported inner formats: .csv, .json, .jsonl, .sql, "
                    ".db/.sqlite, .parquet, .xlsx")
                _finalize(key, False, log_lines)
                return

            if len(supported) > 1:
                # Multi-file ZIP: pass the .zip directly - run_pipeline.py handles it
                log("INFO  Multi-file ZIP detected - merging all inner tables into one schema")
                pipeline_input = input_path  # subprocess handles extraction
            else:
                # Single-file ZIP (or ZIP-disguised CSV): extract before subprocess call
                erd_dir.mkdir(parents=True, exist_ok=True)
                member = supported[0]
                out_path = erd_dir / Path(member.filename).name
                with _zipfile.ZipFile(_input_path_obj) as zf:
                    out_path.write_bytes(zf.read(member.filename))
                pipeline_input = str(out_path)
                inner_fmt = Path(member.filename).suffix.lower().lstrip(".")
                log(f"INFO  Extracted '{out_path.name}' from ZIP archive "
                    f"(original: {_input_path_obj.name}, format: {inner_fmt})")

        except Exception as exc:
            log(f"ERROR ZIP processing failed: {exc}")
            _finalize(key, False, log_lines)
            return

    cmd = [sys.executable, str(PIPELINE), pipeline_input, "--output-dir", str(erd_dir)]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        assert proc.stdout

        start = time.time()
        for line in proc.stdout:
            stripped = line.rstrip("\n")
            # Tag lines with level prefix
            if stripped.startswith("[!!]"):
                log(f"ERROR {stripped[4:].strip()}")
            elif stripped.startswith("[ok]"):
                log(f"INFO  {stripped[4:].strip()}")
            elif stripped.startswith("[.."):
                log(f"INFO  {stripped[4:].strip()}")
            else:
                log(f"INFO  {stripped}")

            # Enforce timeout
            if time.time() - start > PIPELINE_TIMEOUT:
                proc.kill()
                log(f"ERROR Pipeline timed out after {PIPELINE_TIMEOUT}s")
                _finalize(key, False, log_lines)
                return

        proc.wait()
        success = proc.returncode == 0

    except Exception as exc:
        log(f"ERROR Pipeline exception: {exc}")
        success = False

    _finalize(key, success, log_lines)


def _run_live_introspection(key: str, record: dict, erd_dir: Path, log) -> None:
    """Run SQLAlchemy introspection for connstring sources (in-process)."""
    import json as _json
    log_lines = _live_logs[key]
    conn_url  = _resolve_conn_url(record.get("conn_url", ""))

    log(f"INFO  Connecting to database …")
    try:
        from server.ingestion.extract_live_db import extract_from_url
        from server.ingestion.infer_fk import infer_missing_fks
        from server.pipeline.run_pipeline import schema_to_graph
        from server.pipeline.generate_erd import schema_to_ddl
    except ImportError as exc:
        log(f"ERROR Missing dependency: {exc}. Install sqlalchemy.")
        _finalize(key, False, log_lines)
        return

    try:
        schema = extract_from_url(conn_url)
        log(f"INFO  Schema extracted: {len(schema.tables)} table(s)")
    except Exception as exc:
        log(f"ERROR Connection failed: {exc}")
        _finalize(key, False, log_lines)
        return

    # FK inference
    added = infer_missing_fks(schema)
    if added:
        log(f"INFO  Inferred {added} FK relationship(s) from column names")

    if not schema.tables:
        log("ERROR No tables found in the connected database")
        _finalize(key, False, log_lines)
        return

    erd_dir.mkdir(parents=True, exist_ok=True)

    graph_data = schema_to_graph(schema)
    json_path  = erd_dir / "schema.json"
    json_path.write_text(_json.dumps(graph_data, ensure_ascii=False, indent=2), encoding="utf-8")
    n_tables = len(graph_data.get("tables", {}))
    n_fks    = sum(
        1 for t in graph_data.get("tables", {}).values()
        for c in t.get("constraints", {}).values()
        if c.get("type") == "FOREIGN KEY"
    )
    log(f"INFO  schema.json written ({n_tables} tables, {n_fks} FK edges)")

    ddl_path = erd_dir / "schema.sql"
    ddl_path.write_text(schema_to_ddl(schema), encoding="utf-8")
    log(f"INFO  schema.sql written")

    _finalize(key, True, log_lines)


def _run_libsql_introspection(key: str, record: dict, erd_dir: Path, log) -> None:
    """Introspect a remote libSQL / Turso database via its HTTP API.

    libSQL exposes a simple HTTP API:
        POST https://<host>/v2/pipeline
        Authorization: Bearer <auth_token>
        Body: {"requests": [{"type": "execute", "stmt": {"sql": "..."}}]}

    We query sqlite_master to get table/column metadata without any external
    library dependency - only the stdlib urllib.request is needed.
    """
    import json as _json
    import urllib.request as _ulr
    import urllib.error as _ule
    log_lines = _live_logs[key]

    libsql_url = record.get("libsql_url", "").strip()
    auth_token = record.get("auth_token", "").strip()

    # Normalise scheme: libsql:// → https://
    http_url = libsql_url
    if http_url.startswith("libsql://"):
        http_url = "https://" + http_url[len("libsql://"):]

    endpoint = http_url.rstrip("/") + "/v2/pipeline"

    def _execute_sql(sql: str) -> list[list]:
        """Run a SQL statement via the libSQL HTTP API and return rows."""
        payload = _json.dumps({
            "requests": [
                {"type": "execute", "stmt": {"sql": sql}},
                {"type": "close"},
            ]
        }).encode()
        hdrs = {"Content-Type": "application/json"}
        if auth_token:
            hdrs["Authorization"] = f"Bearer {auth_token}"
        req = _ulr.Request(endpoint, data=payload, method="POST", headers=hdrs)
        with _ulr.urlopen(req, timeout=15) as resp:
            body = _json.loads(resp.read())
        result = body["results"][0]
        if result.get("type") == "error":
            raise RuntimeError(result.get("error", {}).get("message", "libSQL error"))
        rows_raw = result["response"]["result"]["rows"]
        cols_raw = result["response"]["result"]["cols"]
        # rows_raw is list of lists of {"type":..., "value":...}
        col_names = [c["name"] for c in cols_raw]
        return col_names, [[cell.get("value") for cell in row] for row in rows_raw]

    log("INFO  Connecting to libSQL / Turso endpoint …")
    try:
        col_names, rows = _execute_sql(
            "SELECT name, type FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    except Exception as exc:
        log(f"ERROR Connection failed: {exc}")
        _finalize(key, False, log_lines)
        return

    table_names = [r[0] for r in rows]
    log(f"INFO  Found {len(table_names)} table(s)")
    if not table_names:
        log("ERROR No tables found in the libSQL database")
        _finalize(key, False, log_lines)
        return

    try:
        from server.ingestion.infer_fk import infer_missing_fks
        from server.pipeline.run_pipeline import schema_to_graph
        from server.pipeline.generate_erd import schema_to_ddl
        from server.ingestion.schema_model import Schema, Table, Column, ForeignKey
    except ImportError as exc:
        log(f"ERROR Missing dependency: {exc}")
        _finalize(key, False, log_lines)
        return

    schema = Schema(name=record.get("name", key))

    for tbl_name in table_names:
        table = Table(name=tbl_name)
        try:
            # PRAGMA table_info returns: cid, name, type, notnull, dflt_value, pk
            _, col_rows = _execute_sql(f"PRAGMA table_info({tbl_name})")
            for row in col_rows:
                cid, col_name, col_type, notnull, dflt, pk = row[:6]
                table.columns.append(Column(
                    name=col_name,
                    data_type=(col_type or "TEXT").upper(),
                    nullable=not bool(int(notnull or 0)),
                    primary_key=bool(int(pk or 0)),
                    default=dflt,
                    unique=False,
                ))
                if bool(int(pk or 0)):
                    table.primary_keys.append(col_name)
        except Exception as exc:
            log(f"INFO  Could not introspect columns for {tbl_name}: {exc}")
        try:
            # PRAGMA foreign_key_list returns: id, seq, table, from, to, ...
            _, fk_rows = _execute_sql(f"PRAGMA foreign_key_list({tbl_name})")
            fk_map: dict = {}
            for row in fk_rows:
                fk_id, seq, ref_tbl, from_col, to_col = row[:5]
                if fk_id not in fk_map:
                    fk_map[fk_id] = {"ref": ref_tbl, "from": [], "to": []}
                fk_map[fk_id]["from"].append(from_col)
                fk_map[fk_id]["to"].append(to_col)
            for fk_info in fk_map.values():
                table.foreign_keys.append(ForeignKey(
                    constrained_columns=fk_info["from"],
                    referred_table=fk_info["ref"],
                    referred_columns=fk_info["to"],
                ))
        except Exception:
            pass
        schema.tables.append(table)

    added = infer_missing_fks(schema)
    if added:
        log(f"INFO  Inferred {added} FK relationship(s) from column names")

    erd_dir.mkdir(parents=True, exist_ok=True)
    graph_data = schema_to_graph(schema)
    json_path  = erd_dir / "schema.json"
    json_path.write_text(_json.dumps(graph_data, ensure_ascii=False, indent=2), encoding="utf-8")
    n_tables = len(graph_data.get("tables", {}))
    n_fks    = sum(
        1 for t in graph_data.get("tables", {}).values()
        for c in t.get("constraints", {}).values()
        if c.get("type") == "FOREIGN KEY"
    )
    log(f"INFO  schema.json written ({n_tables} tables, {n_fks} FK edges)")
    ddl_path = erd_dir / "schema.sql"
    ddl_path.write_text(schema_to_ddl(schema), encoding="utf-8")
    log("INFO  schema.sql written")
    _finalize(key, True, log_lines)


def _run_clickhouse_introspection(key: str, record: dict, erd_dir: Path, log) -> None:
    """Introspect a ClickHouse database via clickhouse-connect (pure Python).

    Falls back gracefully to the SQLAlchemy connstring path if
    clickhouse-connect is not installed.
    """
    import json as _json
    log_lines = _live_logs[key]
    conn_url  = _resolve_conn_url(record.get("conn_url", ""))

    log("INFO  Connecting to ClickHouse …")
    try:
        from server.ingestion.infer_fk import infer_missing_fks
        from server.pipeline.run_pipeline import schema_to_graph
        from server.pipeline.generate_erd import schema_to_ddl
        from server.ingestion.schema_model import Schema, Table, Column
    except ImportError as exc:
        log(f"ERROR Missing dependency: {exc}")
        _finalize(key, False, log_lines)
        return

    # Parse host/port/user/password/database from the conn_url
    # Accepted formats: clickhouse+native://user:pass@host:port/database
    #                   clickhouse://user:pass@host:port/database
    import re as _re
    m = _re.match(
        r"(?:clickhouse(?:\+\w+)?://)(?:([^:@]+)(?::([^@]*))?@)?([^:/?]+)(?::(\d+))?(?:/([^?]*))?",
        conn_url,
    )
    if not m:
        log("ERROR Could not parse ClickHouse connection URL")
        _finalize(key, False, log_lines)
        return

    user     = m.group(1) or "default"
    password = m.group(2) or ""
    host     = m.group(3) or "localhost"
    port     = int(m.group(4) or 8123)   # clickhouse-connect uses HTTP (8123)
    database = m.group(5) or "default"

    try:
        import clickhouse_connect as _ch
        client = _ch.get_client(
            host=host, port=port,
            username=user, password=password,
            database=database,
            connect_timeout=15,
            send_receive_timeout=30,
        )
    except ImportError:
        log("WARN  clickhouse-connect not installed - falling back to SQLAlchemy path")
        _run_live_introspection(key, record, erd_dir, log)
        return
    except Exception as exc:
        log(f"ERROR ClickHouse connection failed: {exc}")
        _finalize(key, False, log_lines)
        return

    try:
        result = client.query(
            "SELECT table, name, type, is_in_primary_key, is_in_sorting_key "
            "FROM system.columns "
            "WHERE database = {db:String} "
            "ORDER BY table, position",
            parameters={"db": database},
        )
    except Exception as exc:
        log(f"ERROR Failed to query ClickHouse system.columns: {exc}")
        _finalize(key, False, log_lines)
        return

    schema = Schema(name=record.get("name", key))
    tables_map: dict = {}
    for row in result.result_rows:
        tbl_name, col_name, col_type, in_pk, in_sk = row
        if tbl_name not in tables_map:
            tables_map[tbl_name] = Table(name=tbl_name)
        tables_map[tbl_name].columns.append(Column(
            name=col_name,
            data_type=col_type,
            nullable=False,
            primary_key=bool(in_pk),
            default=None,
            unique=False,
        ))
        if bool(in_pk):
            tables_map[tbl_name].primary_keys.append(col_name)

    schema.tables = list(tables_map.values())

    added = infer_missing_fks(schema)
    if added:
        log(f"INFO  Inferred {added} FK relationship(s) from column names")

    if not schema.tables:
        log("ERROR No tables found in the ClickHouse database")
        _finalize(key, False, log_lines)
        return

    erd_dir.mkdir(parents=True, exist_ok=True)
    graph_data = schema_to_graph(schema)
    json_path  = erd_dir / "schema.json"
    json_path.write_text(_json.dumps(graph_data, ensure_ascii=False, indent=2), encoding="utf-8")
    n_tables = len(graph_data.get("tables", {}))
    n_fks    = sum(
        1 for t in graph_data.get("tables", {}).values()
        for c in t.get("constraints", {}).values()
        if c.get("type") == "FOREIGN KEY"
    )
    log(f"INFO  schema.json written ({n_tables} tables, {n_fks} FK edges)")
    ddl_path = erd_dir / "schema.sql"
    ddl_path.write_text(schema_to_ddl(schema), encoding="utf-8")
    log("INFO  schema.sql written")
    _finalize(key, True, log_lines)


def _run_mongodb_introspection(key: str, record: dict, erd_dir: Path, log) -> None:
    """Introspect a MongoDB database by sampling each collection.

    Strategy
    --------
    * Connect via pymongo (sync driver).
    * List all non-system collections.
    * Sample up to ``mongo_sample_size`` documents per collection.
    * Merge all keys seen across the sample into a column list; infer a
      simplified type (STRING / INT / FLOAT / BOOL / ARRAY / OBJECT / DATE).
    * Emit the schema as a Schema/Table/Column object and write schema.json +
      schema.sql the same way every other connector does.

    No FK inference is attempted for MongoDB because the document model does
    not carry explicit references (we still run ``infer_missing_fks`` on column
    names, which finds _id / _ref patterns).
    """
    import json as _json
    log_lines = _live_logs[key]

    mongo_uri     = record.get("mongo_uri", "mongodb://localhost:27017")
    database_name = record.get("mongo_database", "")
    sample_size   = int(record.get("mongo_sample_size") or 100)

    log(f"INFO  Connecting to MongoDB at {mongo_uri!r}, database={database_name!r} …")

    try:
        import pymongo as _pymongo
    except ImportError:
        log("ERROR pymongo is not installed. "
            "Add 'pymongo' to requirements.txt or install it in the container.")
        _finalize(key, False, log_lines)
        return

    try:
        client = _pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=10_000)
        # Force a connection attempt
        client.admin.command("ping")
    except Exception as exc:
        log(f"ERROR Could not connect to MongoDB: {exc}")
        _finalize(key, False, log_lines)
        return

    try:
        db = client[database_name]
        collection_names = [
            n for n in db.list_collection_names()
            if not n.startswith("system.")
        ]
    except Exception as exc:
        log(f"ERROR Failed to list collections: {exc}")
        _finalize(key, False, log_lines)
        return

    log(f"INFO  Found {len(collection_names)} collection(s)")
    if not collection_names:
        log("ERROR No collections found in the MongoDB database")
        _finalize(key, False, log_lines)
        return

    try:
        from server.ingestion.infer_fk import infer_missing_fks
        from server.pipeline.run_pipeline import schema_to_graph
        from server.pipeline.generate_erd import schema_to_ddl
        from server.ingestion.schema_model import Schema, Table, Column
    except ImportError as exc:
        log(f"ERROR Missing pipeline module: {exc}")
        _finalize(key, False, log_lines)
        return

    def _mongo_type(val) -> str:
        """Map a Python value to a simplified SQL-ish type name."""
        import datetime as _dt
        if val is None:
            return "TEXT"
        if isinstance(val, bool):
            return "BOOLEAN"
        if isinstance(val, int):
            return "INTEGER"
        if isinstance(val, float):
            return "FLOAT"
        if isinstance(val, _dt.datetime):
            return "DATETIME"
        if isinstance(val, list):
            return "ARRAY"
        if isinstance(val, dict):
            return "OBJECT"
        return "TEXT"

    schema = Schema(name=record.get("name", key))

    for coll_name in collection_names:
        log(f"INFO  Sampling collection '{coll_name}' (up to {sample_size} docs) …")
        table = Table(name=coll_name)
        try:
            docs = list(db[coll_name].find({}, limit=sample_size))
        except Exception as exc:
            log(f"WARN  Could not sample '{coll_name}': {exc}")
            schema.tables.append(table)
            continue

        # Collect all keys and their observed types across the sample
        key_types: dict[str, set] = {}
        for doc in docs:
            for k, v in doc.items():
                key_types.setdefault(k, set()).add(_mongo_type(v))

        # Always surface _id as the first column
        ordered_keys = sorted(key_types.keys(), key=lambda k: (0 if k == "_id" else 1, k))

        for col_name in ordered_keys:
            seen_types = key_types[col_name]
            # Pick the most specific non-TEXT type if unambiguous
            if len(seen_types) == 1:
                col_type = next(iter(seen_types))
            elif seen_types <= {"INTEGER", "FLOAT"}:
                col_type = "FLOAT"
            else:
                col_type = "TEXT"
            is_pk = col_name == "_id"
            col = Column(
                name=col_name,
                data_type=col_type,
                nullable=not is_pk,
                primary_key=is_pk,
                default=None,
                unique=is_pk,
            )
            table.columns.append(col)
            if is_pk:
                table.primary_keys.append(col_name)

        schema.tables.append(table)

    client.close()

    added = infer_missing_fks(schema)
    if added:
        log(f"INFO  Inferred {added} FK relationship(s) from column names")

    erd_dir.mkdir(parents=True, exist_ok=True)
    graph_data = schema_to_graph(schema)
    json_path  = erd_dir / "schema.json"
    json_path.write_text(_json.dumps(graph_data, ensure_ascii=False, indent=2), encoding="utf-8")
    n_tables = len(graph_data.get("tables", {}))
    n_fks    = sum(
        1 for t in graph_data.get("tables", {}).values()
        for c in t.get("constraints", {}).values()
        if c.get("type") == "FOREIGN KEY"
    )
    log(f"INFO  schema.json written ({n_tables} collections, {n_fks} inferred FK edges)")
    ddl_path = erd_dir / "schema.sql"
    ddl_path.write_text(schema_to_ddl(schema), encoding="utf-8")
    log("INFO  schema.sql written")
    _finalize(key, True, log_lines)


# ── Knowledge Graph API routes ────────────────────────────────────────────────


@app.route("/kg")
@app.route("/knowledge-graph")
def kg_page():
    """Full-page Knowledge Graph search UI."""
    return render_template("kg.html")


@app.route("/api/kg/sources")
def api_kg_sources():
    """GET /api/kg/sources - list all registered ready sources for the UI picker."""
    try:
        reg = _load_reg()
        sources = []
        for key, rec in reg.items():
            if rec.get("status") == "ready":
                schema_exists = (OUTPUT_DIR / key / "schema.json").exists()
                db_path       = _find_sqlite_db(key)
                db_exists     = db_path is not None and db_path.exists()
                # Skip sources that have neither schema.json nor a queryable SQLite DB
                if not schema_exists and not db_exists:
                    continue
                sources.append({
                    "key":        key,
                    "name":       rec.get("name", key),
                    "source":     rec.get("source", "file"),
                    "tables":     rec.get("tables", 0),
                    "has_schema": schema_exists,
                })
        sources.sort(key=lambda x: x["name"].lower())
        return jsonify({"sources": sources})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/kg/search")
def api_kg_search():
    """GET /api/kg/search?q=<query>&mode=schema|data|hybrid&limit=20&sources=k1,k2"""
    q       = request.args.get("q", "").strip()
    mode    = request.args.get("mode", "hybrid").lower()
    limit   = min(int(request.args.get("limit", "30")), 200)
    sources = request.args.get("sources", "").strip()
    # Comma-separated source keys to restrict results; empty = all indexed sources
    source_filter = [s for s in sources.split(",") if s.strip()] if sources else []

    if not q:
        return jsonify({"error": "q is required"}), 400

    try:
        from server.kg.kg_search import keyword_search, hybrid_search, data_search
        from server.kg.kg_store import _open_store_raw

        if mode == "data":
            hits = data_search(q, source_keys=source_filter or None, limit=limit)
        elif mode in ("hybrid", "schema"):
            store = _open_store_raw()
            try:
                raw_hits = hybrid_search(q, layer="schema", limit=limit * 2, store=store)
            finally:
                store.close()
            # Filter to selected sources server-side
            if source_filter:
                raw_hits = [h for h in raw_hits if h.source_key in source_filter]
            hits = raw_hits[:limit]
        else:
            store = _open_store_raw()
            try:
                raw_hits = keyword_search(q, layer="schema", limit=limit * 2, store=store)
            finally:
                store.close()
            if source_filter:
                raw_hits = [h for h in raw_hits if h.source_key in source_filter]
            hits = raw_hits[:limit]

        def _serialise(h):
            d = h.__dict__.copy()
            d.pop("row_dict", None)
            return d

        return jsonify({
            "q":       q,
            "mode":    mode,
            "sources": source_filter,
            "hits":    [_serialise(h) for h in hits],
            "count":   len(hits),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/kg/entity")
def api_kg_entity():
    """GET /api/kg/entity?key=K&table=T&pk_col=C&pk_val=V&depth=2"""
    key    = request.args.get("key", "").strip()
    table  = request.args.get("table", "").strip()
    pk_col = request.args.get("pk_col", "").strip()
    pk_val = request.args.get("pk_val", "").strip()
    depth  = min(int(request.args.get("depth", "2")), 4)

    if not (key and table and pk_col and pk_val):
        return jsonify({"error": "key, table, pk_col, pk_val are required"}), 400

    try:
        from server.kg.kg_resolver import resolve_entity, tag_entities
        from server.kg.kg_graph import build_graph

        nodes, edges = resolve_entity(key, table, pk_col, pk_val, depth=depth)
        nodes = tag_entities(nodes)
        g = build_graph(nodes, edges)

        return jsonify({
            "key":        key,
            "table":      table,
            "pk_col":     pk_col,
            "pk_val":     pk_val,
            "node_count": g.node_count,
            "edge_count": g.edge_count,
            "graph":      g.to_cytoscape_json(),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _kg_schema_graph(source_keys, src_color_fn):
    """Build a schema-level graph (one node per table, edges from FK constraints).

    Returns (all_nodes, all_edges).  Used by api_kg_graph when mode='schema'.
    """
    from server.kg.kg_resolver import EntityNode, EntityEdge

    all_nodes: list = []
    all_edges: list = []

    for src_key in source_keys:
        schema_path = OUTPUT_DIR / src_key / "schema.json"
        if not schema_path.exists():
            # Fallback: derive schema from the SQLite DB directly (no schema.json needed).
            # _find_sqlite_db will generate a .db from CSV/xlsx/parquet on-demand if needed.
            db_path = _find_sqlite_db(src_key)
            if db_path and db_path.exists():
                import sqlite3 as _sq3
                try:
                    conn3 = _sq3.connect(str(db_path), timeout=5)
                    tbls = [r[0] for r in conn3.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()]
                    tbl_schema: dict = {}
                    for tbl in tbls:
                        cols = conn3.execute(f'PRAGMA table_info("{tbl}")').fetchall()
                        tbl_schema[tbl] = {
                            "columns": {r[1]: {"type": r[2] or "TEXT"} for r in cols},
                            "constraints": {},
                        }
                    conn3.close()
                    raw = {"tables": tbl_schema}
                except Exception:
                    continue
            else:
                continue
        else:
            try:
                raw = json.loads(schema_path.read_text(encoding="utf-8"))
            except Exception:
                continue

        tables = raw.get("tables", {})

        # One node per table — collect this source's IDs in a local set
        src_node_ids: set = set()
        for table_name in tables:
            node = EntityNode(
                id          = f"{src_key}:{table_name}",
                label       = table_name,
                entity_type = "TABLE",
                source_key  = src_key,
                table       = table_name,
                pk_col      = "",
                pk_val      = "",
                row_dict    = {},
            )
            all_nodes.append(node)
            src_node_ids.add(node.id)

        # One node per column — TABLE→COLUMN edge for each
        for table_name, tdata in tables.items():
            tbl_id = f"{src_key}:{table_name}"
            # Identify PK columns from constraints
            pk_cols: set = set()
            for cname, cdata in tdata.get("constraints", {}).items():
                if cdata.get("type") == "PRIMARY KEY":
                    for c in cdata.get("columnNames", cdata.get("columns", [])):
                        pk_cols.add(c)

            for col_name, col_meta in tdata.get("columns", {}).items():
                col_id   = f"{src_key}:{table_name}.{col_name}"
                col_type = col_meta.get("type", "TEXT") if isinstance(col_meta, dict) else str(col_meta)
                col_node = EntityNode(
                    id          = col_id,
                    label       = col_name,
                    entity_type = "COLUMN",
                    source_key  = src_key,
                    table       = table_name,
                    pk_col      = col_name,
                    pk_val      = "",
                    row_dict    = {
                        "node_kind": "column",
                        "data_type": col_type,
                        "is_pk":     col_name in pk_cols,
                    },
                )
                all_nodes.append(col_node)
                # TABLE → COLUMN edge
                all_edges.append(EntityEdge(
                    src_id   = tbl_id,
                    tgt_id   = col_id,
                    relation = "HAS_COLUMN",
                    label    = col_type,
                ))

        # FK edges between TABLE nodes (cross-table relationships)
        for table_name, tdata in tables.items():
            for cname, cdata in tdata.get("constraints", {}).items():
                if cdata.get("type") != "FOREIGN KEY":
                    continue
                ref_table = cdata.get("targetTableName", cdata.get("ref_table", ""))
                fk_cols   = cdata.get("columnNames",     cdata.get("columns", []))
                if not ref_table or f"{src_key}:{ref_table}" not in src_node_ids:
                    continue
                edge_label = ", ".join(fk_cols) if fk_cols else cname
                all_edges.append(EntityEdge(
                    src_id   = f"{src_key}:{table_name}",
                    tgt_id   = f"{src_key}:{ref_table}",
                    relation = f"FK:{table_name}→{ref_table}",
                    label    = edge_label,
                ))

    return all_nodes, all_edges


# How many sample rows to materialise per table in data-graph mode
# How many sample rows to materialise as ROW nodes in data-graph mode.
# Distinct-value scanning for VALUE nodes always uses the full table (up to
# _KG_VALUE_SCAN_LIMIT rows) so that categorical columns with many unique
# values are not silently dropped just because the ROW sample is small.
_KG_DATA_GRAPH_ROWS = int(os.environ.get("KG_DATA_GRAPH_ROWS", "40"))

# Maximum rows scanned to collect distinct values for VALUE-node generation.
# Setting this higher improves recall for large CSVs; it is a read-only
# SELECT so it has no write cost.  Default 2 000 rows.
_KG_VALUE_SCAN_LIMIT = int(os.environ.get("KG_VALUE_SCAN_LIMIT", "2000"))

# Cardinality ratio ceiling for VALUE-node columns.
# A column whose (distinct / total_scanned) ratio exceeds this is treated as
# high-cardinality (identifier-like) and gets no value nodes.
# 0.40 means: "if more than 40 % of scanned rows have a unique value, skip."
_KG_CATEGORICAL_RATIO = float(os.environ.get("KG_CATEGORICAL_RATIO", "0.40"))

# Hard cap on VALUE nodes per column (prevents graph explosions for wide-open
# columns that still pass the ratio test).
_KG_VALUE_NODES_PER_COL = int(os.environ.get("KG_VALUE_NODES_PER_COL", "200"))


def _is_categorical_column(distinct: int, total_scanned: int,
                            ratio_ceil: float = _KG_CATEGORICAL_RATIO) -> bool:
    """Return True when a column looks categorical rather than identifier-like.

    Logic (all conditions must hold):
      1. At least 2 distinct values (otherwise the column is constant).
      2. Cardinality ratio ≤ ratio_ceil  (not identifier-like).
      3. Not all values unique (distinct < total_scanned).

    This replaces the old absolute-count threshold (`distinct <= 15`) which
    silently dropped team/country/city columns once the sample grew beyond
    15 rows.
    """
    if total_scanned == 0 or distinct <= 1:
        return False
    ratio = distinct / total_scanned
    return ratio <= ratio_ceil and distinct < total_scanned


def _kg_data_graph(source_keys, row_limit: int = None, col_roles: dict = None):
    """Build a data-content graph for flat/denormalised sources.

    Graph topology (star-of-stars):
      TABLE node
        └─ COLUMN nodes  (one per column)
             └─ VALUE nodes  (one per distinct cell value, shared across rows)
      ROW nodes  (one per sampled row, up to row_limit)
        └─ edges to COLUMN nodes / VALUE nodes

    Categorical column detection uses a **ratio-based** heuristic over a
    larger scan window (_KG_VALUE_SCAN_LIMIT rows) rather than an absolute
    count over the small ROW sample.  This fixes the silent dropping of
    team/country/city columns in flat CSVs.

    *col_roles*  - optional per-source per-table column-role overrides from
                   the UI panel (Part 2).  Shape:
                   {src_key: {table_name: {col_name: "node"|"edge"|"weight"|"ignore"}}}
    *row_limit*  - overrides _KG_DATA_GRAPH_ROWS for ROW node generation.

    Returns (all_nodes, all_edges).
    """
    import sqlite3 as _sq
    from server.kg.kg_resolver import EntityNode, EntityEdge, tag_entities, _best_label

    _n_rows = row_limit if row_limit is not None else _KG_DATA_GRAPH_ROWS
    _col_roles = col_roles or {}

    all_nodes: list = []
    all_edges: list = []
    seen_node_ids: set = set()

    def _add_node(node: EntityNode) -> None:
        if node.id not in seen_node_ids:
            seen_node_ids.add(node.id)
            all_nodes.append(node)

    for src_key in source_keys:
        schema_path = OUTPUT_DIR / src_key / "schema.json"
        db_path     = _find_sqlite_db(src_key)

        if not schema_path.exists():
            # Fallback: derive schema from the SQLite DB directly.
            # _find_sqlite_db generates a .db from flat files (csv/xlsx/parquet) on-demand.
            if db_path and db_path.exists():
                import sqlite3 as _sq3
                try:
                    conn3 = _sq3.connect(str(db_path), timeout=5)
                    tbls = [r[0] for r in conn3.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()]
                    tbl_schema2: dict = {}
                    for tbl in tbls:
                        cols = conn3.execute(f'PRAGMA table_info("{tbl}")').fetchall()
                        tbl_schema2[tbl] = {
                            "columns": {r[1]: {"type": r[2] or "TEXT"} for r in cols},
                            "constraints": {},
                        }
                    conn3.close()
                    raw = {"tables": tbl_schema2}
                except Exception:
                    continue
            else:
                continue
        else:
            try:
                raw = json.loads(schema_path.read_text(encoding="utf-8"))
            except Exception:
                continue

        tables = raw.get("tables", {})
        # col_roles keys come from schema.json (original CSV headers, e.g. "Excellent Review %").
        # SQLite column names are sanitized by flat_file_to_sqlite._safe_col()
        # (all non-word chars → "_").  Build a normalised alias so ignore/node/edge
        # roles survive the schema→SQLite name mismatch.
        import re as _re
        def _norm_col(name: str) -> str:
            return _re.sub(r"[^\w]", "_", str(name).strip())

        raw_src_roles = _col_roles.get(src_key, {})
        # Rebuild each per-table dict with both the original key AND the sanitized key
        # so lookups work regardless of which form the caller used.
        src_roles: dict = {}
        for tbl_name_raw, tbl_dict in raw_src_roles.items():
            normalised: dict = {}
            for col_raw, role in tbl_dict.items():
                normalised[col_raw] = role           # original key (from schema.json)
                normalised[_norm_col(col_raw)] = role  # sanitized key (from SQLite)
            src_roles[tbl_name_raw] = normalised
            src_roles[_norm_col(tbl_name_raw)] = normalised  # table name normalised too

        for table_name, tdata in tables.items():
            tbl_id = f"{src_key}:{table_name}"

            # ── TABLE node ────────────────────────────────────────────────────
            tbl_node = EntityNode(
                id          = tbl_id,
                label       = table_name,
                entity_type = "TABLE",
                source_key  = src_key,
                table       = table_name,
                pk_col      = "",
                pk_val      = "",
                row_dict    = {"node_kind": "table"},
            )
            _add_node(tbl_node)

            # Determine PK columns from schema
            pk_cols: set = set()
            for cname, cdata in tdata.get("constraints", {}).items():
                if cdata.get("type") == "PRIMARY KEY":
                    for col in cdata.get("columnNames", cdata.get("columns", [])):
                        pk_cols.add(col)

            schema_cols = list(tdata.get("columns", {}).keys())

            # ── COLUMN nodes ──────────────────────────────────────────────────
            # Resolve role overrides for this table up-front so we can skip
            # ignored columns before they ever become nodes.
            tbl_roles_early = src_roles.get(table_name, {})
            for col_name in schema_cols:
                if tbl_roles_early.get(col_name) == "ignore":
                    continue  # user explicitly excluded this column - no node
                col_id = f"{src_key}:{table_name}:col:{col_name}"
                is_pk  = col_name in pk_cols
                col_node = EntityNode(
                    id          = col_id,
                    label       = col_name,
                    entity_type = "COLUMN",
                    source_key  = src_key,
                    table       = table_name,
                    pk_col      = col_name,
                    pk_val      = "",
                    row_dict    = {
                        "node_kind": "column",
                        "data_type": tdata["columns"][col_name].get("type", "TEXT"),
                        "is_pk":     is_pk,
                    },
                )
                _add_node(col_node)
                all_edges.append(EntityEdge(
                    src_id   = tbl_id,
                    tgt_id   = col_id,
                    relation = "HAS_COLUMN",
                    label    = "has column",
                    weight   = 1.0,
                ))

            # ── ROW + VALUE nodes (needs SQLite) ──────────────────────────────
            if db_path is None or not db_path.exists():
                continue  # schema-only for live sources

            try:
                conn = _sq.connect(str(db_path), timeout=5)
                conn.row_factory = _sq.Row

                # ── Pass 1: sample a window to detect categorical columns ──────
                # We use _KG_VALUE_SCAN_LIMIT rows to decide WHICH columns are
                # categorical (ratio-based heuristic).  The actual value counts
                # for categorical columns are then fetched via full-table GROUP BY
                # queries so the counts on graph nodes reflect the whole dataset,
                # not just the sample.  This fixes the "Eucerin shows 19 instead
                # of 76" class of bugs where a brand/category value appears many
                # times but is spread across more rows than the scan window.
                sample_rows = conn.execute(
                    f'SELECT * FROM "{table_name}" LIMIT ?',
                    (_KG_VALUE_SCAN_LIMIT,),
                ).fetchall()

                if not sample_rows:
                    conn.close()
                    continue

                actual_cols    = list(sample_rows[0].keys())
                total_scanned  = len(sample_rows)

                # tbl_roles is the same dict resolved later; grab it now for pk_col selection.
                _tbl_roles_now = src_roles.get(table_name, {})

                # Choose pk_col: prefer schema PK, then first non-ignored col, then first col.
                # Never use an ignored column as the row-identity/label anchor.
                pk_col = next(
                    (c for c in actual_cols if c in pk_cols and _tbl_roles_now.get(c) != "ignore"),
                    next(
                        (c for c in actual_cols if _tbl_roles_now.get(c) != "ignore"),
                        actual_cols[0]  # last resort: use whatever is there
                    )
                )

                # Collect distinct values over the sample to determine cardinality ratios
                sample_value_counts: dict = {}   # col_name → {value: count}
                for row in sample_rows:
                    for col_name in actual_cols:
                        val = row[col_name]
                        if val is None:
                            continue
                        val_str = str(val).strip()
                        if not val_str:
                            continue
                        sample_value_counts.setdefault(col_name, {})
                        sample_value_counts[col_name][val_str] = \
                            sample_value_counts[col_name].get(val_str, 0) + 1

                # ── Decide which columns get value-nodes ─────────────────────
                # Per-column override from the UI takes precedence; otherwise
                # use the ratio-based heuristic (applied against sample counts).
                tbl_roles = src_roles.get(table_name, {})
                value_node_cols: set = set()
                edge_cols: dict = {}    # col_name → "edge" role
                weight_cols: set = set()

                for col_name, vc in sample_value_counts.items():
                    explicit_role = tbl_roles.get(col_name, "auto")
                    if explicit_role == "ignore":
                        continue
                    if explicit_role == "node":
                        value_node_cols.add(col_name)
                    elif explicit_role == "edge":
                        edge_cols[col_name] = True
                    elif explicit_role == "weight":
                        weight_cols.add(col_name)
                    else:
                        # Auto-detect: ratio-based categorical check
                        distinct = len(vc)
                        if _is_categorical_column(distinct, total_scanned):
                            value_node_cols.add(col_name)

                # ── Cap and build merged entity nodes ─────────────────────────
                #
                # KG MERGE DESIGN:
                #   The same real-world value (e.g. "Germany") can appear in
                #   multiple columns (team1, team2, winner).  Creating a
                #   separate node per (column, value) pair produces duplicates
                #   that fragment the graph and obscure connectivity.
                #
                #   Instead we use a single ENTITY node per unique value within
                #   a table, with ID:  {src_key}:{table}:entity:{value}
                #   Each column that contains that value contributes an edge
                #   labelled with the column name and its per-column count:
                #
                #     team1  ──[team1 · 6×]──> Germany
                #     team2  ──[team2 · 4×]──> Germany
                #     winner ──[winner · 8×]──> Germany
                #
                #   The node's aggregate count = sum across all contributing
                #   columns, giving a fair importance signal.
                #
                # "merge_namespace" columns share the same entity ID pool.
                # All value_node_cols in the same table share one pool by
                # default (correct when columns represent the same domain,
                # e.g. team1/team2/winner all refer to football teams).

                # ── Pass 2: full-table GROUP BY for accurate counts ───────────
                # For each column identified as categorical, fetch the real
                # occurrence count from the whole table.  This is a single
                # aggregation query per column — fast even on 100k-row tables.
                col_value_counts: dict = {}   # col_name → {value: real_count}
                for col_name in value_node_cols:
                    try:
                        rows_gb = conn.execute(
                            f'SELECT CAST("{col_name}" AS TEXT), COUNT(*)'
                            f' FROM "{table_name}"'
                            f' WHERE "{col_name}" IS NOT NULL AND CAST("{col_name}" AS TEXT) != ""'
                            f' GROUP BY CAST("{col_name}" AS TEXT)'
                            f' ORDER BY COUNT(*) DESC'
                            f' LIMIT ?',
                            (_KG_VALUE_NODES_PER_COL,),
                        ).fetchall()
                        col_value_counts[col_name] = {r[0]: r[1] for r in rows_gb}
                    except Exception:
                        # Fall back to sample counts if the GROUP BY fails
                        col_value_counts[col_name] = sample_value_counts.get(col_name, {})

                # Step 1: collect aggregate counts per unique value across ALL
                # value-node columns (for node sizing/importance).
                entity_total: dict = {}   # value_str → total count across cols
                entity_cols:  dict = {}   # value_str → {col_name: count}
                for col_name in value_node_cols:
                    vc = col_value_counts[col_name]
                    # Already capped to _KG_VALUE_NODES_PER_COL in the GROUP BY LIMIT above
                    for val_str, count in vc.items():
                        entity_total[val_str] = entity_total.get(val_str, 0) + count
                        entity_cols.setdefault(val_str, {})[col_name] = count

                # Global cap: if we still have too many unique entities, keep
                # the most frequent ones.
                max_entities = _KG_VALUE_NODES_PER_COL
                if len(entity_total) > max_entities:
                    top_vals = sorted(entity_total, key=lambda v: -entity_total[v])[:max_entities]
                    entity_total = {v: entity_total[v] for v in top_vals}
                    entity_cols  = {v: entity_cols[v]  for v in top_vals}

                # Step 2: create one merged ENTITY node per unique value, then
                # one typed edge per column that contains it.
                for val_str, total_count in entity_total.items():
                    # Stable shared ID - col-independent
                    ent_id = f"{src_key}:{table_name}:entity:{val_str[:60]}"
                    col_names_for_val = sorted(entity_cols[val_str].keys())
                    val_node = EntityNode(
                        id          = ent_id,
                        label       = val_str[:50],
                        entity_type = "ENTITY",
                        source_key  = src_key,
                        table       = table_name,
                        pk_col      = col_names_for_val[0],  # primary role
                        pk_val      = val_str,
                        row_dict    = {
                            "node_kind":  "value",
                            "value":      val_str,
                            "count":      total_count,
                            # which columns this entity appeared in
                            "columns":    col_names_for_val,
                            "col_counts": entity_cols[val_str],
                        },
                    )
                    _add_node(val_node)

                    # One edge per contributing column, labelled with role+count
                    for col_name, col_count in entity_cols[val_str].items():
                        col_id = f"{src_key}:{table_name}:col:{col_name}"
                        all_edges.append(EntityEdge(
                            src_id   = col_id,
                            tgt_id   = ent_id,
                            relation = col_name,
                            label    = f"{col_count}×",
                            weight   = min(1.0, col_count / total_scanned),
                        ))

                # Keep a mapping so the ROW pass (if active) can find entity IDs
                # by (col_name, val_str) lookup.
                _entity_id_for: dict = {
                    (col, val): f"{src_key}:{table_name}:entity:{val[:60]}"
                    for val, col_map in entity_cols.items()
                    for col in col_map
                }

                # ── Pass 1b: ENTITY↔ENTITY co-occurrence edges ───────────────
                # For each pair of categorical columns, count how many rows
                # contain value A in col_i and value B in col_j simultaneously.
                # These "co-occurrence" edges make ENTITY nodes reachable from
                # each other, which is essential for FRD exploration and for
                # correct upstream/downstream path highlighting.
                #
                # Only emitted for entity values that were kept after capping.
                # Uses a full-table GROUP BY to count co-occurrences accurately.
                cat_cols = list(value_node_cols)
                seen_cooc_edges: set = set()   # (ent_id_a, ent_id_b) dedup
                if len(cat_cols) >= 2:
                    for i, col_a in enumerate(cat_cols):
                        for col_b in cat_cols[i + 1:]:
                            try:
                                cooc_rows = conn.execute(
                                    f'SELECT CAST("{col_a}" AS TEXT),'
                                    f'       CAST("{col_b}" AS TEXT),'
                                    f'       COUNT(*) AS cnt'
                                    f' FROM "{table_name}"'
                                    f' WHERE "{col_a}" IS NOT NULL'
                                    f'   AND "{col_b}" IS NOT NULL'
                                    f'   AND CAST("{col_a}" AS TEXT) != ""'
                                    f'   AND CAST("{col_b}" AS TEXT) != ""'
                                    f' GROUP BY 1, 2'
                                    f' ORDER BY cnt DESC',
                                ).fetchall()
                            except Exception:
                                continue

                            for val_a, val_b, cnt in cooc_rows:
                                eid_a = _entity_id_for.get((col_a, val_a))
                                eid_b = _entity_id_for.get((col_b, val_b))
                                if not eid_a or not eid_b:
                                    continue
                                if eid_a not in seen_node_ids or eid_b not in seen_node_ids:
                                    continue
                                # Dedup — use sorted pair so A-B and B-A collapse
                                pair = (min(eid_a, eid_b), max(eid_a, eid_b))
                                if pair in seen_cooc_edges:
                                    continue
                                seen_cooc_edges.add(pair)
                                all_edges.append(EntityEdge(
                                    src_id   = eid_a,
                                    tgt_id   = eid_b,
                                    relation = f"{col_a}↔{col_b}",
                                    label    = f"{cnt}×",
                                    weight   = min(1.0, cnt / max(entity_total.get(val_a, 1),
                                                                  entity_total.get(val_b, 1))),
                                ))

                # ── Pass 2: materialise ROW nodes (limited to _n_rows) ────────
                # Skip ROW nodes entirely when the user has supplied explicit
                # column roles for this table.  In that case they want a
                # value-centric graph (category nodes), not individual row
                # samples alongside them.
                if tbl_roles:
                    conn.close()
                    continue

                row_sample = conn.execute(
                    f'SELECT * FROM "{table_name}" LIMIT ?',
                    (_n_rows,),
                ).fetchall()

                for row in row_sample:
                    row_dict  = dict(row)
                    pk_val    = str(row_dict.get(pk_col, ""))
                    row_label = _best_label(row_dict, pk_val)
                    row_id    = f"{src_key}:{table_name}:row:{pk_col}={pk_val}"

                    row_node = EntityNode(
                        id          = row_id,
                        label       = row_label[:50],
                        entity_type = "ENTITY",
                        source_key  = src_key,
                        table       = table_name,
                        pk_col      = pk_col,
                        pk_val      = pk_val,
                        row_dict    = row_dict,
                    )
                    _add_node(row_node)

                    # Edge: TABLE → ROW
                    all_edges.append(EntityEdge(
                        src_id   = tbl_id,
                        tgt_id   = row_id,
                        relation = "HAS_ROW",
                        label    = "",
                        weight   = 0.5,
                    ))

                    # Edge: ROW → ENTITY (merged) or ROW → COLUMN (inline)
                    for col_name in actual_cols:
                        # Never emit edges to/from ignored columns
                        if tbl_roles.get(col_name) == "ignore":
                            continue

                        val = row_dict.get(col_name)
                        if val is None:
                            continue
                        val_str = str(val).strip()
                        if not val_str:
                            continue

                        col_id = f"{src_key}:{table_name}:col:{col_name}"
                        ent_id = _entity_id_for.get((col_name, val_str))
                        if ent_id and ent_id in seen_node_ids:
                            # Categorical column: connect row → merged entity node
                            all_edges.append(EntityEdge(
                                src_id   = row_id,
                                tgt_id   = ent_id,
                                relation = col_name,
                                label    = col_name,
                                weight   = 1.0,
                            ))
                        elif col_id in seen_node_ids:
                            # High-cardinality col: connect row → column node
                            # (only if the column node wasn't itself skipped)
                            all_edges.append(EntityEdge(
                                src_id   = row_id,
                                tgt_id   = col_id,
                                relation = col_name,
                                label    = val_str[:30],
                                weight   = 0.3,
                            ))

                conn.close()
            except Exception:
                continue

    # NER-tag all nodes to assign entity_type
    all_nodes = tag_entities(all_nodes)
    return all_nodes, all_edges


def _inject_approved_proposals(all_nodes, all_edges, source_keys):
    """Append edges for every 'approved' link proposal that connects nodes
    present in the current graph.  Called after _kg_data_graph() builds nodes.

    An approved proposal adds a directed EntityEdge between the two node IDs
    recorded in the proposal.  If either endpoint is not yet in the node set,
    a lightweight ghost node is created so the edge can be visualised.
    """
    from server.kg.kg_resolver import EntityNode, EntityEdge

    try:
        proposals = _proposals_load_all()
    except Exception:
        return all_nodes, all_edges

    approved = [p for p in proposals if p.get("status") == "approved"]
    if not approved:
        return all_nodes, all_edges

    # Quick lookup of existing node IDs
    existing_ids = {n.id for n in all_nodes}
    new_edges = []

    for p in approved:
        src_id  = p.get("src_id", "")
        tgt_id  = p.get("tgt_id", "")
        rel     = p.get("relation", "cross_db:approved")
        if not src_id or not tgt_id:
            continue

        # Only inject if at least one endpoint belongs to the active source set
        involved = [src_id, tgt_id]
        relevant = any(
            any(nid.startswith(sk + ":") for sk in source_keys)
            for nid in involved
        )
        if not relevant:
            continue

        # Create ghost nodes for missing endpoints so the edge is visible
        for nid in [src_id, tgt_id]:
            if nid not in existing_ids:
                parts = nid.split(":")
                sk  = parts[0] if parts else "unknown"
                lbl = parts[-1][:40] if len(parts) > 1 else nid[:40]
                ghost = EntityNode(
                    id          = nid,
                    label       = lbl,
                    entity_type = "ENTITY",
                    source_key  = sk,
                    table       = parts[1] if len(parts) > 1 else "",
                    pk_col      = "",
                    pk_val      = "",
                    row_dict    = {"node_kind": "ghost", "proposed_link": True},
                )
                all_nodes.append(ghost)
                existing_ids.add(nid)

        new_edges.append(EntityEdge(
            src_id     = src_id,
            tgt_id     = tgt_id,
            relation   = rel,
            label      = rel.split(":")[-1],
            weight     = 1.0,
            confidence = 1.0,
        ))

    all_edges.extend(new_edges)
    return all_nodes, all_edges


@app.route("/api/kg/graph", methods=["GET", "POST"])
def api_kg_graph():
    """GET/POST /api/kg/graph - build a graph from a source's schema or data.

    GET params:
      sources=k1,k2   required: comma-separated source key(s) to graph
      mode=auto|schema|data
      q=term          optional: for export labelling
      format=json|jsonld|csv
      limit=N         max rows in data mode (default: KG_DATA_GRAPH_ROWS env var)

    POST body (JSON, optional):
      Same fields as GET params, plus:
      col_roles: {src_key: {table: {col: "node"|"edge"|"weight"|"ignore"|"auto"}}}
      relationships: [{src_col, tgt_col, label_col?, weight_col?, source_key, table}]
    """
    if request.method == "POST":
        body    = request.get_json(silent=True) or {}
        q       = body.get("q", "").strip()
        sources = body.get("sources", "").strip()
        fmt     = body.get("format", "json").lower()
        mode    = body.get("mode", "auto").lower()
        limit   = str(body.get("limit", ""))
        col_roles     = body.get("col_roles", {})      # Part 2 column roles
        relationships = body.get("relationships", [])  # Part 2 relationship definitions
    else:
        q       = request.args.get("q", "").strip()
        sources = request.args.get("sources", "").strip()
        fmt     = request.args.get("format", "json").lower()
        mode    = request.args.get("mode", "auto").lower()
        limit   = request.args.get("limit", "")
        col_roles     = {}
        relationships = []

    source_keys = [s.strip() for s in sources.split(",") if s.strip()]

    try:
        from server.kg.kg_resolver import tag_entities
        from server.kg.kg_graph import build_graph

        _SRC_PALETTE = [
            "#3b82d4","#7c5cd8","#e66d00","#22863a",
            "#0366d6","#735c0f","#d73a49","#6f42c1",
        ]
        def _src_color(key):
            return _SRC_PALETTE[hash(key) % len(_SRC_PALETTE)]

        # ── Resolve mode=auto ─────────────────────────────────────────────────
        if mode == "auto" and len(source_keys) == 1:
            src_key     = source_keys[0]
            schema_path = OUTPUT_DIR / src_key / "schema.json"
            use_data    = False
            if schema_path.exists():
                try:
                    raw    = json.loads(schema_path.read_text(encoding="utf-8"))
                    tables = raw.get("tables", {})
                    total_fks = sum(
                        1
                        for tdata in tables.values()
                        for cdata in tdata.get("constraints", {}).values()
                        if cdata.get("type") == "FOREIGN KEY"
                    )
                    use_data = (len(tables) <= 1) or (total_fks == 0)
                except Exception:
                    pass
            if use_data:
                mode = "data"
            else:
                mode = "schema"
        elif mode == "auto":
            mode = "schema"

        # ── Build graph ───────────────────────────────────────────────────────
        if mode == "data":
            row_limit = _KG_DATA_GRAPH_ROWS
            if limit.isdigit():
                row_limit = min(int(limit), 500)
            all_nodes, all_edges = _kg_data_graph(
                source_keys,
                row_limit  = row_limit,
                col_roles  = col_roles if col_roles else None,
            )
            # Part 2: apply explicit relationship definitions on top of auto graph
            if relationships:
                all_nodes, all_edges = _apply_relationship_rules(
                    all_nodes, all_edges, source_keys, relationships,
                )
        else:
            all_nodes, all_edges = _kg_schema_graph(source_keys, _src_color)
            all_nodes = tag_entities(all_nodes)
            # Auto-fallback: if schema graph is empty (schema.json absent for all
            # sources), try data mode so the graph still renders.
            if not all_nodes and mode in ("schema", "auto"):
                row_limit = _KG_DATA_GRAPH_ROWS
                if limit.isdigit():
                    row_limit = min(int(limit), 500)
                all_nodes, all_edges = _kg_data_graph(
                    source_keys,
                    row_limit = row_limit,
                    col_roles = col_roles if col_roles else None,
                )
                mode = "data"

        # Inject approved link proposals as edges (works for both data + schema mode)
        all_nodes, all_edges = _inject_approved_proposals(all_nodes, all_edges, source_keys)

        g = build_graph(all_nodes, all_edges)

        if fmt == "jsonld":
            resp = make_response(json.dumps(g.to_jsonld(), indent=2), 200)
            resp.headers["Content-Type"] = "application/ld+json"
            resp.headers["Content-Disposition"] = 'attachment; filename="kg-export.jsonld"'
            return resp
        elif fmt == "csv":
            resp = make_response(g.to_csv_edgelist(), 200)
            resp.headers["Content-Type"] = "text/csv"
            resp.headers["Content-Disposition"] = 'attachment; filename="kg-edges.csv"'
            return resp
        else:
            return jsonify({
                "q":          q,
                "mode":       mode,
                "node_count": g.node_count,
                "edge_count": g.edge_count,
                "graph":      g.to_cytoscape_json(),
            })

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _apply_relationship_rules(all_nodes, all_edges, source_keys, relationships):
    """Apply explicit user-defined relationship rules on top of the auto graph.

    Each relationship dict must have:
      source_key  - which source
      table       - which table in that source
      src_col     - column whose distinct values become source nodes
      tgt_col     - column whose distinct values become target nodes
      label_col   - (optional) column whose value becomes the edge label
      weight_col  - (optional) column whose numeric value becomes edge weight

    Uses the merged entity ID scheme ({src}:{table}:entity:{val}) so that nodes
    created here are automatically unified with any nodes already in the graph
    from the same values in other columns.

    Edges are deduplicated by (src_node, tgt_node) pair - one weighted edge
    per unique pairing, with weight = number of rows that produced it.
    """
    import sqlite3 as _sq
    from server.kg.kg_resolver import EntityNode, EntityEdge

    seen_node_ids = {n.id for n in all_nodes}

    def _ensure_entity_node(src_key, table, col, val):
        # Use the merged entity ID - same as _kg_data_graph
        nid = f"{src_key}:{table}:entity:{val[:60]}"
        if nid not in seen_node_ids:
            node = EntityNode(
                id=nid, label=val[:50], entity_type="ENTITY",
                source_key=src_key, table=table, pk_col=col, pk_val=val,
                row_dict={"node_kind": "value", "value": val, "columns": [col]},
            )
            all_nodes.append(node)
            seen_node_ids.add(nid)
        return nid

    for rule in relationships:
        src_key    = rule.get("source_key", "")
        table      = rule.get("table", "")
        src_col    = rule.get("src_col", "")
        tgt_col    = rule.get("tgt_col", "")
        label_col  = rule.get("label_col", "")
        weight_col = rule.get("weight_col", "")

        if not (src_key and table and src_col and tgt_col):
            continue

        db_path = _find_sqlite_db(src_key)
        if not db_path or not db_path.exists():
            continue

        try:
            conn = _sq.connect(str(db_path), timeout=5)
            conn.row_factory = _sq.Row
            rows = conn.execute(f'SELECT * FROM "{table}" LIMIT 2000').fetchall()
            conn.close()
        except Exception:
            continue

        # Accumulate edge weights: (src_nid, tgt_nid, relation) → {count, label}
        edge_acc: dict = {}

        for row in rows:
            row_d = dict(row)
            src_val = str(row_d.get(src_col, "") or "").strip()
            tgt_val = str(row_d.get(tgt_col, "") or "").strip()
            if not src_val or not tgt_val or src_val == tgt_val:
                continue

            src_nid = _ensure_entity_node(src_key, table, src_col, src_val)
            tgt_nid = _ensure_entity_node(src_key, table, tgt_col, tgt_val)

            edge_label = ""
            if label_col and row_d.get(label_col) is not None:
                edge_label = str(row_d[label_col])[:40]

            numeric_weight = 1.0
            if weight_col and row_d.get(weight_col) is not None:
                try:
                    numeric_weight = float(row_d[weight_col])
                except (ValueError, TypeError):
                    pass

            relation = f"{src_col}→{tgt_col}"
            key = (src_nid, tgt_nid, relation)
            if key not in edge_acc:
                edge_acc[key] = {"count": 0, "label": edge_label, "weight": 0.0}
            edge_acc[key]["count"]  += 1
            edge_acc[key]["weight"] += numeric_weight

        # Emit one deduplicated edge per (src, tgt, relation) pair
        for (src_nid, tgt_nid, relation), info in edge_acc.items():
            count = info["count"]
            all_edges.append(EntityEdge(
                src_id   = src_nid,
                tgt_id   = tgt_nid,
                relation = relation,
                label    = f"{count}×" if not info["label"] else info["label"],
                weight   = info["weight"] / count if count else 1.0,
            ))

    return all_nodes, all_edges


@app.route("/api/kg/columns")
def api_kg_columns():
    """GET /api/kg/columns?source=KEY - return column metadata for the column-role panel.

    Returns per-table, per-column info including distinct count, inferred
    categorical status, and sample values so the UI can build the role selector.
    """
    src_key = request.args.get("source", "").strip()
    if not src_key:
        return jsonify({"error": "source is required"}), 400

    import sqlite3 as _sq

    schema_path = OUTPUT_DIR / src_key / "schema.json"
    db_path     = _find_sqlite_db(src_key)

    if not schema_path.exists():
        return jsonify({"error": "schema not found for source"}), 404

    try:
        raw    = json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    tables_out = {}
    tables     = raw.get("tables", {})

    for table_name, tdata in tables.items():
        cols_out = []
        schema_cols = list(tdata.get("columns", {}).keys())

        # Default info from schema
        col_types = {c: tdata["columns"][c].get("type", "TEXT") for c in schema_cols}

        # Enhance with live distinct-value stats if SQLite is available
        col_stats: dict = {}  # col_name → {distinct, sample, categorical}
        if db_path and db_path.exists():
            try:
                conn = _sq.connect(str(db_path), timeout=5)
                conn.row_factory = _sq.Row
                scan = conn.execute(
                    f'SELECT * FROM "{table_name}" LIMIT ?',
                    (_KG_VALUE_SCAN_LIMIT,),
                ).fetchall()
                conn.close()

                total = len(scan)
                col_value_counts: dict = {}
                for row in scan:
                    for col_name in schema_cols:
                        v = row[col_name] if col_name in row.keys() else None
                        if v is None:
                            continue
                        vs = str(v).strip()
                        if not vs:
                            continue
                        col_value_counts.setdefault(col_name, {})
                        col_value_counts[col_name][vs] = col_value_counts[col_name].get(vs, 0) + 1

                for col_name, vc in col_value_counts.items():
                    distinct    = len(vc)
                    categorical = _is_categorical_column(distinct, total)
                    # Top-5 most frequent values as sample
                    sample_vals = [v for v, _ in
                                   sorted(vc.items(), key=lambda x: -x[1])[:5]]
                    col_stats[col_name] = {
                        "distinct":    distinct,
                        "total":       total,
                        "categorical": categorical,
                        "sample":      sample_vals,
                    }
            except Exception:
                pass

        for col_name in schema_cols:
            stats = col_stats.get(col_name, {})
            cols_out.append({
                "name":        col_name,
                "type":        col_types.get(col_name, "TEXT"),
                "distinct":    stats.get("distinct", None),
                "total":       stats.get("total", None),
                "categorical": stats.get("categorical", None),
                "sample":      stats.get("sample", []),
                "suggested_role": (
                    "node"   if stats.get("categorical") else
                    "ignore" if stats.get("distinct") == stats.get("total") else
                    "auto"
                ),
            })

        tables_out[table_name] = cols_out

    return jsonify({"source": src_key, "tables": tables_out})


# ── FRD: Frequency-Relative Degree expand endpoint ────────────────────────────
# Computes FRD = deg(v) * N / count(v) for neighbours of a seed node and returns
# them ranked highest-surprise-first.  Used by the UI's FRD explore mode.

def _frd_expand(source_keys, seed_node_id: str, depth: int = 1):
    """Return FRD-ranked neighbours of seed_node_id up to `depth` hops.

    source_keys may be a single key string OR a list of key strings.
    When multiple sources are given, the full combined data graph is built
    and FRD scores are computed against the aggregate N (sum of row counts).

    Algorithm (incremental BFS up to `depth` levels):
      1. Load the full data graph for all source_keys.
      2. Walk BFS from the seed node up to `depth` hops.
      3. For each visited node compute:
           count(v)  = row_dict['count']  (set during indexing by _kg_data_graph)
           deg(v)    = number of distinct neighbours in the full graph (not just BFS subtree)
           N         = total rows across all source tables (sum of per-source row counts)
           FRD(v)    = deg(v) * N / count(v)
      4. Return the set of reached nodes + edges between them, with frd_score on each node.
    """
    import sqlite3 as _sq

    # Normalise: accept either a string or a list
    if isinstance(source_keys, str):
        source_keys = [k.strip() for k in source_keys.split(",") if k.strip()]
    source_keys = source_keys or []

    # ── Step 1: build full combined data graph for all sources ───────────────
    all_nodes, all_edges = _kg_data_graph(source_keys)
    from server.kg.kg_graph import build_graph
    g = build_graph(all_nodes, all_edges)
    cyto = g.to_cytoscape_json()
    cy_nodes = {n["data"]["id"]: n["data"] for n in cyto.get("elements", {}).get("nodes", [])}
    cy_edges  = cyto.get("elements", {}).get("edges", [])

    # ── Step 2: build adjacency list from full graph ───────────────────────────
    adj: dict = {}  # node_id → set of neighbour ids
    for e in cy_edges:
        s, t = e["data"].get("source"), e["data"].get("target")
        if s and t:
            adj.setdefault(s, set()).add(t)
            adj.setdefault(t, set()).add(s)

    # ── Step 3: compute N = sum of row counts across all sources ─────────────
    # For multi-source graphs, N is the aggregate total so FRD scores are
    # comparable across sources — a node appearing 10× in a 1 000-row source
    # and a node appearing 10× in a 10 000-row source get different scores.
    N = 0
    import sqlite3 as _sq2
    for sk in source_keys:
        try:
            db_path = _find_sqlite_db(sk)
            if db_path and db_path.exists():
                conn_n = _sq2.connect(str(db_path), timeout=3)
                tables = [r[0] for r in conn_n.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()]
                if tables:
                    row_count = conn_n.execute(f'SELECT COUNT(*) FROM "{tables[0]}"').fetchone()[0]
                    N += max(int(row_count), 0)
                conn_n.close()
        except Exception:
            pass

    # Fallback: use sum of entity count attributes in the graph
    if N <= 0:
        entity_counts = [
            d.get("count") or (d.get("row_dict") or {}).get("count") or 0
            for d in cy_nodes.values()
            if d.get("entity_type") == "ENTITY" or (d.get("row_dict") or {}).get("node_kind") == "value"
        ]
        N = sum(entity_counts) if entity_counts else 1

    N = max(N, 1)

    # ── Step 4: per-node degree (in the full graph) ────────────────────────────
    node_deg = {nid: len(nbrs) for nid, nbrs in adj.items()}

    # ── Step 5: BFS from seed up to `depth` hops ─────────────────────────────
    if seed_node_id not in cy_nodes:
        # Try to find closest match by label substring
        seed_node_id = next(
            (nid for nid, d in cy_nodes.items() if seed_node_id in nid), seed_node_id
        )

    visited_ids: set  = {seed_node_id}
    frontier:    list = [seed_node_id]

    # For each hop, expand neighbours; each hop's candidates are FRD-sorted
    hop_nodes:  list = [seed_node_id]   # ordered list of all reached nodes
    # Track BFS parent→child pairs so we only emit traversal edges (not all
    # induced edges between visited nodes, which causes O(N²) edge explosion).
    traversal_pairs: set = set()  # set of frozenset({parent_id, child_id})

    for _hop in range(depth):
        next_frontier = []
        candidates = []
        for nid in frontier:
            for nb_id in adj.get(nid, set()):
                if nb_id not in visited_ids:
                    nb_data   = cy_nodes.get(nb_id, {})
                    nb_count  = nb_data.get("count") or (nb_data.get("row_dict") or {}).get("count") or 1
                    nb_deg    = node_deg.get(nb_id, 1)
                    nb_count  = max(int(nb_count), 1)
                    frd       = (nb_deg * N) / nb_count
                    candidates.append((frd, nb_id, nid))  # include parent nid

        # Sort by FRD descending — highest structural surprise first
        candidates.sort(key=lambda x: -x[0])

        for frd_score, nb_id, parent_id in candidates:
            if nb_id not in visited_ids:
                visited_ids.add(nb_id)
                next_frontier.append(nb_id)
                hop_nodes.append(nb_id)
                traversal_pairs.add(frozenset({parent_id, nb_id}))
                # Store computed FRD back into node data for the UI
                if nb_id in cy_nodes:
                    nb_count  = cy_nodes[nb_id].get("count") or \
                                (cy_nodes[nb_id].get("row_dict") or {}).get("count") or 1
                    nb_count  = max(int(nb_count), 1)
                    nb_deg    = node_deg.get(nb_id, 1)
                    cy_nodes[nb_id]["frd_score"] = round((nb_deg * N) / nb_count, 1)
                    cy_nodes[nb_id]["frd_deg"]   = nb_deg
                    cy_nodes[nb_id]["frd_count"] = nb_count
                    cy_nodes[nb_id]["frd_N"]     = N

        frontier = next_frontier
        if not frontier:
            break

    # Seed node gets its own FRD score
    if seed_node_id in cy_nodes:
        seed_data  = cy_nodes[seed_node_id]
        seed_count = seed_data.get("count") or (seed_data.get("row_dict") or {}).get("count") or 1
        seed_count = max(int(seed_count), 1)
        seed_deg   = node_deg.get(seed_node_id, 1)
        seed_data["frd_score"] = round((seed_deg * N) / seed_count, 1)
        seed_data["frd_deg"]   = seed_deg
        seed_data["frd_count"] = seed_count
        seed_data["frd_N"]     = N
        seed_data["is_seed"]   = True

    # ── Step 6: collect subgraph nodes + traversal edges ────────────────────
    # Only emit edges that were actually traversed during BFS (parent→child pairs).
    # Returning all induced edges (both endpoints in visited_ids) produces O(N²)
    # edges in dense entity co-occurrence graphs, making every node appear connected
    # to every other node.
    result_nodes = [{"data": cy_nodes[nid]} for nid in hop_nodes if nid in cy_nodes]
    result_edges = [
        e for e in cy_edges
        if frozenset({e["data"].get("source"), e["data"].get("target")}) in traversal_pairs
    ]

    # ── Step 7: compute FRD stats for response metadata ──────────────────────
    frd_scores = [cy_nodes[nid].get("frd_score", 0) for nid in hop_nodes if nid in cy_nodes]
    frd_max    = max(frd_scores) if frd_scores else 1

    return {
        "seed":       seed_node_id,
        "depth":      depth,
        "N":          N,
        "node_count": len(result_nodes),
        "edge_count": len(result_edges),
        "frd_max":    frd_max,
        "graph": {
            "elements": {
                "nodes": result_nodes,
                "edges": result_edges,
            }
        }
    }


@app.route("/api/kg/expand", methods=["GET"])
def api_kg_expand():
    """GET /api/kg/expand?sources=KEY1,KEY2&node=NODE_ID&depth=N

    Returns a FRD-ranked subgraph rooted at node up to depth hops.
    Works with any number of sources — the combined data graph is built
    and FRD scores computed against the aggregate row count.

    Parameters:
      sources  — comma-separated source keys (preferred)
      source   — single source key (backward-compat alias for sources)
      node     — seed node ID
      depth    — BFS depth 1-4 (default 1)

    Response JSON:
      seed, depth, N, node_count, edge_count, frd_max,
      sources (list of keys used),
      graph: {elements: {nodes: [...], edges: [...]}}

    Each node data includes:
      frd_score  — Frequency-Relative Degree score
      frd_deg    — node degree in full graph
      frd_count  — empirical occurrence count
      frd_N      — total row count of source (N)
      is_seed    — true for the seed node
    """
    # Accept sources= (comma-sep) or source= (single, backward-compat)
    sources_raw  = request.args.get("sources", "").strip() or request.args.get("source", "").strip()
    seed_node_id = request.args.get("node",    "").strip()
    try:
        depth = max(1, min(4, int(request.args.get("depth", "1"))))
    except ValueError:
        depth = 1

    source_keys = [s.strip() for s in sources_raw.split(",") if s.strip()]
    if not source_keys or not seed_node_id:
        return jsonify({"error": "sources (or source) and node parameters are required"}), 400

    try:
        result = _frd_expand(source_keys, seed_node_id, depth)
        result["sources"] = source_keys   # echo back the keys used
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/kg/frd-scores", methods=["GET"])
def api_kg_frd_scores():
    """GET /api/kg/frd-scores?sources=KEY1,KEY2  (or ?source=KEY for single-source compat)

    Returns FRD scores for all nodes in the combined graph of one or more sources.
    Used by the FRD toggle to colorise the full graph by FRD score.

    Response JSON:
      N, node_count, frd_max, sources (list),
      nodes: [{id, label, frd_score, frd_deg, frd_count, entity_type}, ...]
             sorted by frd_score descending
    """
    # Accept sources= (comma-sep) or source= (single, backward-compat)
    sources_raw = request.args.get("sources", "").strip() or request.args.get("source", "").strip()
    if not sources_raw:
        return jsonify({"error": "sources (or source) parameter is required"}), 400

    source_keys = [s.strip() for s in sources_raw.split(",") if s.strip()]

    try:
        all_nodes, all_edges = _kg_data_graph(source_keys)
        from server.kg.kg_graph import build_graph
        g    = build_graph(all_nodes, all_edges)
        cyto = g.to_cytoscape_json()

        cy_nodes = cyto.get("elements", {}).get("nodes", [])
        cy_edges = cyto.get("elements", {}).get("edges", [])

        # Build degree map
        adj: dict = {}
        for e in cy_edges:
            s, t = e["data"].get("source"), e["data"].get("target")
            if s and t:
                adj.setdefault(s, set()).add(t)
                adj.setdefault(t, set()).add(s)

        # N = sum of row counts across all sources for comparable FRD scores
        import sqlite3 as _sq3
        N = 0
        for sk in source_keys:
            try:
                db_path = _find_sqlite_db(sk)
                if db_path and db_path.exists():
                    conn_n = _sq3.connect(str(db_path), timeout=3)
                    tables = [r[0] for r in conn_n.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()]
                    if tables:
                        row_count = conn_n.execute(f'SELECT COUNT(*) FROM "{tables[0]}"').fetchone()[0]
                        N += max(int(row_count), 0)
                    conn_n.close()
            except Exception:
                pass

        # Fallback N from sum of entity counts
        if N <= 0:
            ec = [
                d["data"].get("count") or (d["data"].get("row_dict") or {}).get("count") or 0
                for d in cy_nodes
            ]
            N = max(sum(int(c) for c in ec if c), 1)
        N = max(N, 1)

        result_nodes = []
        for n in cy_nodes:
            d        = n["data"]
            nid      = d.get("id", "")
            count    = d.get("count") or (d.get("row_dict") or {}).get("count") or 1
            count    = max(int(count), 1)
            deg      = len(adj.get(nid, set()))
            frd      = round((deg * N) / count, 1)
            result_nodes.append({
                "id":          nid,
                "label":       d.get("label", nid),
                "entity_type": d.get("entity_type", ""),
                "frd_score":   frd,
                "frd_deg":     deg,
                "frd_count":   count,
            })

        result_nodes.sort(key=lambda x: -x["frd_score"])
        frd_max = result_nodes[0]["frd_score"] if result_nodes else 1

        return jsonify({
            "sources":    source_keys,
            "N":          N,
            "node_count": len(result_nodes),
            "frd_max":    frd_max,
            "nodes":      result_nodes,
        })

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/kg/reindex", methods=["POST"])
def api_kg_reindex():
    """POST /api/kg/reindex - trigger background re-index.

    Optional JSON body: {"keys": ["key1", "key2"]}
    If omitted, all ready sources are indexed.
    """
    try:
        body = request.get_json(silent=True) or {}
        keys = body.get("keys") or None  # None means all

        from server.kg.kg_indexer import index_source, index_all
        from server.kg.kg_store import _open_store_raw

        def _run():
            store = _open_store_raw()
            try:
                if keys:
                    for k in keys:
                        index_source(k, store=store)
                else:
                    index_all(store=store)
            finally:
                store.close()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return jsonify({"status": "started", "keys": keys or "all"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/kg/reset", methods=["POST"])
def api_kg_reset():
    """POST /api/kg/reset - wipe all KG index state so sources can be re-indexed clean.

    Deletes all rows from schema_docs, data_docs, index_state, and kg_index_state
    tables in search.db.  The source files themselves are untouched.
    Optional JSON body: {"keys": ["key1"]} to reset specific sources only.
    Omit body (or keys=null) to reset ALL indexed sources.
    """
    try:
        from server.kg.kg_store import _open_store_raw

        body = request.get_json(silent=True) or {}
        keys = body.get("keys") or None   # None = reset all

        store = _open_store_raw()
        try:
            if keys:
                for k in keys:
                    store.schema_docs_delete_source(k)
                    store.data_docs_delete_source(k)
                    store._conn.execute(
                        "DELETE FROM index_state WHERE source_key=?", (k,)
                    )
                    store._conn.execute(
                        "DELETE FROM kg_index_state WHERE source_key=?", (k,)
                    )
            else:
                store._conn.execute("DELETE FROM schema_docs")
                store._conn.execute("DELETE FROM data_docs")
                store._conn.execute("DELETE FROM index_state")
                store._conn.execute("DELETE FROM kg_index_state")
            store._conn.commit()
        finally:
            store.close()

        return jsonify({"status": "reset", "keys": keys or "all"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/kg/capabilities")
def kg_capabilities():
    """Return KG search capabilities for the current configuration.
    # MARKER:kg-capabilities-endpoint
    """
    try:
        from server.kg.kg_store import get_capabilities
        caps = get_capabilities()
        return jsonify({
            "supports_dense_vectors": caps.supports_dense_vectors,
            "supports_sparse_vectors": caps.supports_sparse_vectors,
            "supports_server_side_text": caps.supports_server_side_text,
            "embed_model_name": caps.embed_model_name,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/kg/status")
def api_kg_status():
    """GET /api/kg/status - index health per source.  Never throws - returns empty defaults."""
    caps_dict = {
        "dense_vectors": False,
        "sparse_vectors": False,
        "server_side_text": False,
        "embed_model": "none",
    }
    states: list = []
    ready_count  = 0

    # Capabilities - optional, ignore failures
    try:
        from server.kg.kg_store import get_capabilities
        caps = get_capabilities()
        caps_dict = {
            "dense_vectors":    caps.supports_dense_vectors,
            "sparse_vectors":   caps.supports_sparse_vectors,
            "server_side_text": caps.supports_server_side_text,
            "embed_model":      caps.embed_model_name,
        }
    except Exception:
        pass

    # Index states - optional, ignore failures
    try:
        from server.kg.kg_store import _open_store_raw
        store = _open_store_raw()
        try:
            states = store.all_index_states()
        finally:
            store.close()
    except Exception:
        pass

    # Ready source count from registry
    try:
        reg = _load_reg()
        ready_count = sum(1 for v in reg.values() if v.get("status") == "ready")
    except Exception:
        pass

    return jsonify({
        "capabilities":  caps_dict,
        "sources_ready": ready_count,
        "index_states":  states,
    })


@app.route("/api/kg/validate")
def api_kg_validate():
    """GET /api/kg/validate?sources=KEY1,KEY2
    Run GraphValidator on the actual built graph for the given sources and return issues.
    Uses the same build pipeline as /api/kg/graph so the validated graph matches exactly
    what the user sees.
    # MARKER:kg-validate-endpoint
    """
    try:
        sources = [s.strip() for s in request.args.get("sources", "").split(",") if s.strip()]
        if not sources:
            return jsonify({"error": "sources is required"}), 400

        from server.kg.kg_graph import build_graph
        from server.kg.kg_resolver import tag_entities
        from server.kg.kg_validator import validate_graph

        _SRC_PALETTE = [
            "#3b82d4","#7c5cd8","#e66d00","#22863a",
            "#0366d6","#735c0f","#d73a49","#6f42c1",
        ]
        def _src_color(key):
            return _SRC_PALETTE[hash(key) % len(_SRC_PALETTE)]

        # Build the same graph the UI renders: prefer schema, fall back to data
        all_nodes, all_edges = _kg_schema_graph(sources, _src_color)
        all_nodes = tag_entities(all_nodes)
        if not all_nodes:
            all_nodes, all_edges = _kg_data_graph(sources, row_limit=_KG_DATA_GRAPH_ROWS)

        g = build_graph(all_nodes, all_edges)

        result = validate_graph(g)

        issues_out = []
        for issue in result.issues:
            issues_out.append({
                "severity": issue.severity.value if hasattr(issue.severity, "value") else str(issue.severity),
                "code":     issue.code,
                "message":  issue.message,
                "node_id":  issue.element_id,
            })

        return jsonify({
            "sources":    sources,
            "node_count": g.node_count,
            "edge_count": g.edge_count,
            "issue_count": len(issues_out),
            "issues":     issues_out,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/kg/suggest_links", methods=["GET"])
def api_kg_suggest_links():
    """GET /api/kg/suggest_links?sources=KEY1,KEY2

    Compare columns across two or more sources and return ranked cross-source
    link candidates.  Scoring uses three signals, combined additively:

      1. Name similarity  — exact / prefix / stem match of column names
      2. Type match       — same SQLite affinity (TEXT, INTEGER, REAL, …)
      3. Value overlap    — Jaccard similarity of top-N sample values

    Response JSON:
      {
        "sources": [...],
        "candidates": [
          {
            "score":      float,          # 0-1, higher = better
            "source_col": "KEY:TABLE:COL",
            "target_col": "KEY:TABLE:COL",
            "relation":   "cross_db:<col>",
            "rationale":  "human-readable reason",
            "sample_overlap": ["val1", "val2"]
          }, ...
        ]
      }
    # MARKER:kg-suggest-links-endpoint
    """
    sources_raw = request.args.get("sources", "").strip()
    if not sources_raw:
        return jsonify({"error": "sources parameter is required"}), 400

    source_keys = [s.strip() for s in sources_raw.split(",") if s.strip()]
    if len(source_keys) < 2:
        return jsonify({"error": "At least two sources are required for link suggestions"}), 400

    import sqlite3 as _sq
    import difflib

    # Collect column metadata for every source
    # col_data[src_key] = {table: [{name, type, sample: set()}]}
    col_data: dict = {}

    for sk in source_keys:
        schema_path = OUTPUT_DIR / sk / "schema.json"
        db_path     = _find_sqlite_db(sk)
        if not schema_path.exists():
            continue
        try:
            raw = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        col_data[sk] = {}
        for tbl, tdata in raw.get("tables", {}).items():
            schema_cols = list(tdata.get("columns", {}).keys())
            col_types   = {c: tdata["columns"][c].get("type", "TEXT") for c in schema_cols}
            col_samples: dict = {}

            if db_path and db_path.exists():
                try:
                    conn = _sq.connect(str(db_path), timeout=5)
                    desc_row = conn.execute(f'SELECT * FROM "{tbl}" LIMIT 1').fetchone()
                    if desc_row is not None:
                        col_names_db = [d[0] for d in conn.execute(
                            f'SELECT * FROM "{tbl}" LIMIT 1'
                        ).description]
                        rows = conn.execute(f'SELECT * FROM "{tbl}" LIMIT 500').fetchall()
                        for col_name in schema_cols:
                            if col_name not in col_names_db:
                                continue
                            idx = col_names_db.index(col_name)
                            vals: dict = {}
                            for row in rows:
                                v = row[idx]
                                if v is None:
                                    continue
                                vs = str(v).strip()
                                if vs:
                                    vals[vs] = vals.get(vs, 0) + 1
                            top = sorted(vals, key=lambda x: -vals[x])[:20]
                            col_samples[col_name] = set(top)
                    conn.close()
                except Exception:
                    pass

            col_data[sk][tbl] = [
                {"name": c, "type": col_types.get(c, "TEXT"), "sample": col_samples.get(c, set())}
                for c in schema_cols
            ]

    def _type_family(t: str) -> str:
        t = (t or "TEXT").upper()
        for fam, keywords in [
            ("INT",  ["INT", "INTEGER", "SMALLINT", "BIGINT", "TINYINT"]),
            ("REAL", ["REAL", "FLOAT", "DOUBLE", "NUMERIC", "DECIMAL"]),
            ("BLOB", ["BLOB", "BINARY"]),
        ]:
            if any(k in t for k in keywords):
                return fam
        return "TEXT"

    def _name_score(a: str, b: str) -> float:
        a, b = a.lower(), b.lower()
        if a == b:
            return 1.0
        def _stem(s):
            for suf in ("_id", "_key", "_code", "_no", "_num", "_name", "id", "key"):
                if s.endswith(suf):
                    return s[:-len(suf)]
            return s
        if _stem(a) == _stem(b):
            return 0.9
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        return ratio if ratio >= 0.6 else 0.0

    candidates = []
    for i, sk1 in enumerate(source_keys):
        for sk2 in source_keys[i + 1:]:
            if sk1 not in col_data or sk2 not in col_data:
                continue
            for tbl1, cols1 in col_data[sk1].items():
                for tbl2, cols2 in col_data[sk2].items():
                    for c1 in cols1:
                        for c2 in cols2:
                            ns = _name_score(c1["name"], c2["name"])
                            if ns == 0.0:
                                continue

                            ts = 1.0 if _type_family(c1["type"]) == _type_family(c2["type"]) else 0.3

                            s1, s2 = c1["sample"], c2["sample"]
                            if s1 and s2:
                                inter = s1 & s2
                                union = s1 | s2
                                vs = len(inter) / len(union) if union else 0.0
                                overlap_vals = sorted(inter)[:5]
                            else:
                                vs = 0.0
                                overlap_vals = []

                            score = round(ns * 0.5 + ts * 0.2 + vs * 0.3, 3)
                            if score < 0.3:
                                continue

                            parts = []
                            if ns >= 1.0:
                                parts.append(f'column names match ("{c1["name"]}")')
                            elif ns >= 0.9:
                                parts.append(f'column names are equivalent ("{c1["name"]}" ~ "{c2["name"]}")')
                            else:
                                parts.append(f'similar names ("{c1["name"]}" ~ "{c2["name"]}", {round(ns*100)}%)')
                            if _type_family(c1["type"]) == _type_family(c2["type"]):
                                parts.append(f'same type ({_type_family(c1["type"])})')
                            if overlap_vals:
                                parts.append(f'shared values: {", ".join(overlap_vals[:3])}')

                            candidates.append({
                                "score":          score,
                                "source_col":     f"{sk1}:{tbl1}:{c1['name']}",
                                "target_col":     f"{sk2}:{tbl2}:{c2['name']}",
                                "relation":       f"cross_db:{c1['name'].lower()}",
                                "rationale":      "; ".join(parts),
                                "sample_overlap": overlap_vals,
                            })

    seen: set = set()
    unique = []
    for c in sorted(candidates, key=lambda x: -x["score"]):
        key = (c["source_col"], c["target_col"])
        if key not in seen:
            seen.add(key)
            unique.append(c)
        if len(unique) >= 20:
            break

    # ── Build columns_by_source for the drag-and-drop Connect canvas ─────────
    # Shape: {src_key: {table: [{name, type, sample_preview}]}}
    # sample sets are serialised to lists (not JSON-serialisable as sets)
    columns_by_source: dict = {}
    for sk, tbls in col_data.items():
        columns_by_source[sk] = {}
        for tbl, cols in tbls.items():
            columns_by_source[sk][tbl] = [
                {
                    "name":    c["name"],
                    "type":    c["type"],
                    "sample":  sorted(c["sample"])[:5],
                    "col_id":  f"{sk}:{tbl}:{c['name']}",
                }
                for c in cols
            ]

    # ── Build score_matrix: col_id → {other_col_id: score} ───────────────────
    # Only store entries that scored > 0 (kept above threshold 0.3 already in candidates)
    score_matrix: dict = {}
    for c in candidates:  # use full undeduped list for complete coverage
        sc, tc, sc_ = c["source_col"], c["target_col"], c["score"]
        score_matrix.setdefault(sc, {})[tc] = sc_
        score_matrix.setdefault(tc, {})[sc] = sc_

    return jsonify({
        "sources":            source_keys,
        "candidates":         unique,
        "columns_by_source":  columns_by_source,
        "score_matrix":       score_matrix,
    })




def _proposals_load_all():
    """Read all proposals from the KG store, return list of dicts."""
    import ast
    from server.kg.kg_store import _open_store_raw
    results = []
    with _open_store_raw() as store:
        rows = store._conn.execute(
            "SELECT pk_val, cell_value FROM data_docs"
            " WHERE source_key='__proposals__' AND table_name='link_proposals'"
        ).fetchall()
    for row in rows:
        pk_val     = row["pk_val"] if isinstance(row, dict) else row[0]
        cell_value = row["cell_value"] if isinstance(row, dict) else row[1]
        try:
            proposal = ast.literal_eval(cell_value)
        except Exception:
            proposal = {"raw": cell_value}
        proposal.setdefault("id", pk_val)
        proposal.setdefault("status", "pending_review")
        results.append(proposal)
    return results


def _proposal_set_status(proposal_id: str, status: str) -> bool:
    """Update the status field of a stored proposal. Returns True if found."""
    import ast
    from server.kg.kg_store import _open_store_raw
    with _open_store_raw() as store:
        row = store._conn.execute(
            "SELECT cell_value FROM data_docs"
            " WHERE source_key='__proposals__' AND table_name='link_proposals'"
            " AND pk_val=?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            return False
        cell_value = row[0]
        try:
            proposal = ast.literal_eval(cell_value)
        except Exception:
            proposal = {"raw": cell_value}
        proposal["status"] = status
        new_val = str(proposal)
        store._conn.execute(
            "UPDATE data_docs SET cell_value=?, full_text=?"
            " WHERE source_key='__proposals__' AND table_name='link_proposals'"
            " AND pk_val=?",
            (new_val, f"link_proposals proposal {new_val}", proposal_id),
        )
        store._conn.commit()
    return True


def _proposal_delete(proposal_id: str) -> bool:
    """Hard-delete a proposal by id. Returns True if found."""
    from server.kg.kg_store import _open_store_raw
    with _open_store_raw() as store:
        cur = store._conn.execute(
            "DELETE FROM data_docs"
            " WHERE source_key='__proposals__' AND table_name='link_proposals'"
            " AND pk_val=?",
            (proposal_id,),
        )
        store._conn.commit()
        return cur.rowcount > 0


@app.route("/api/kg/proposals", methods=["GET"])
def api_kg_proposals():
    """GET /api/kg/proposals[?sources=K1,K2]
    Return all stored link proposals.  Optional sources filter restricts to
    proposals whose src_id or tgt_id starts with one of the given source keys.
    # MARKER:kg-proposals-list-endpoint
    """
    try:
        sources_raw = request.args.get("sources", "").strip()
        source_keys = [s.strip() for s in sources_raw.split(",") if s.strip()]

        proposals = _proposals_load_all()

        if source_keys:
            def _matches(p):
                src = p.get("src_id", "")
                tgt = p.get("tgt_id", "")
                return any(
                    src.startswith(sk + ":") or tgt.startswith(sk + ":")
                    for sk in source_keys
                )
            proposals = [p for p in proposals if _matches(p)]

        return jsonify({"proposals": proposals, "total": len(proposals)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/kg/proposals/approve", methods=["POST"])
def api_kg_proposals_approve():
    """POST /api/kg/proposals/approve  Body: {id}
    Mark a proposal as 'approved'.  Approved proposals are injected as edges
    on the next graph build.
    # MARKER:kg-proposals-approve-endpoint
    """
    try:
        body = request.get_json(silent=True) or {}
        pid  = body.get("id", "").strip()
        if not pid:
            return jsonify({"error": "id is required"}), 400
        found = _proposal_set_status(pid, "approved")
        if not found:
            return jsonify({"error": "proposal not found"}), 404
        return jsonify({"status": "approved", "id": pid})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/kg/proposals/reject", methods=["POST"])
def api_kg_proposals_reject():
    """POST /api/kg/proposals/reject  Body: {id}
    Mark a proposal as 'rejected'.
    # MARKER:kg-proposals-reject-endpoint
    """
    try:
        body = request.get_json(silent=True) or {}
        pid  = body.get("id", "").strip()
        if not pid:
            return jsonify({"error": "id is required"}), 400
        found = _proposal_set_status(pid, "rejected")
        if not found:
            return jsonify({"error": "proposal not found"}), 404
        return jsonify({"status": "rejected", "id": pid})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/kg/proposals/delete", methods=["POST"])
def api_kg_proposals_delete():
    """POST /api/kg/proposals/delete  Body: {id}
    Hard-delete a proposal.
    # MARKER:kg-proposals-delete-endpoint
    """
    try:
        body = request.get_json(silent=True) or {}
        pid  = body.get("id", "").strip()
        if not pid:
            return jsonify({"error": "id is required"}), 400
        found = _proposal_delete(pid)
        if not found:
            return jsonify({"error": "proposal not found"}), 404
        return jsonify({"status": "deleted", "id": pid})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/kg/propose_link", methods=["POST"])
def api_kg_propose_link():
    """POST /api/kg/propose_link - record a cross-DB link proposal for human review.
    Body: {source_id, target_id, relation, rationale}
    # MARKER:kg-propose-link-endpoint
    """
    try:
        body       = request.get_json(silent=True) or {}
        source_id  = body.get("source_id", "").strip()
        target_id  = body.get("target_id", "").strip()
        relation   = body.get("relation",  "cross_db:manual").strip()
        rationale  = body.get("rationale", "").strip()

        if not source_id or not target_id:
            return jsonify({"error": "source_id and target_id are required"}), 400

        from server.kg.kg_mcp import dispatch_tool
        result = dispatch_tool("kg_propose_link", {
            "src_id":   source_id,
            "tgt_id":   target_id,
            "relation": relation,
        })
        # Patch rationale into the stored cell_value after the fact
        if rationale and result.get("ok"):
            try:
                pid = f"{source_id}::{target_id}::{relation}"
                _proposal_set_status.__doc__  # ensure helper is in scope
                import ast as _ast
                from server.kg.kg_store import _open_store_raw as _osr
                with _osr() as _st:
                    row = _st._conn.execute(
                        "SELECT cell_value FROM data_docs"
                        " WHERE source_key='__proposals__' AND table_name='link_proposals'"
                        " AND pk_val=?", (pid,)
                    ).fetchone()
                    if row:
                        try:
                            p = _ast.literal_eval(row[0])
                        except Exception:
                            p = {}
                        p["rationale"] = rationale
                        nv = str(p)
                        _st._conn.execute(
                            "UPDATE data_docs SET cell_value=?, full_text=?"
                            " WHERE source_key='__proposals__' AND table_name='link_proposals'"
                            " AND pk_val=?",
                            (nv, f"link_proposals proposal {nv}", pid),
                        )
                        _st._conn.commit()
            except Exception:
                pass
        return jsonify({"status": "proposed", "detail": result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/kg/temporal")
def api_kg_temporal():
    """GET /api/kg/temporal?sources=KEY&from=DATE&to=DATE
    Return a graph filtered to edges whose valid_from/valid_until spans the requested window.
    # MARKER:kg-temporal-endpoint
    """
    try:
        sources  = [s.strip() for s in request.args.get("sources", "").split(",") if s.strip()]
        from_str = request.args.get("from", "").strip()
        to_str   = request.args.get("to",   "").strip()

        if not sources:
            return jsonify({"error": "sources is required"}), 400

        from server.kg.kg_temporal import parse_temporal_value, relationship_overlaps, TemporalBound
        from server.kg.kg_store import _open_store_raw
        from server.kg.kg_graph import build_graph
        from server.kg.kg_resolver import EntityNode, EntityEdge

        start_dt = parse_temporal_value(from_str) if from_str else None
        end_dt   = parse_temporal_value(to_str)   if to_str   else TemporalBound.OPEN

        store = _open_store_raw()
        all_nodes = []
        all_edges = []
        try:
            for sk in sources:
                nodes, edges = _build_kg_from_store(sk, store)
                # Filter edges to those overlapping the temporal window
                if start_dt is not None or end_dt is not TemporalBound.OPEN:
                    edges = [e for e in edges if relationship_overlaps(
                        getattr(e, "__dict__", {}), start_dt, end_dt
                    )]
                all_nodes.extend(nodes)
                all_edges.extend(edges)
        finally:
            store.close()

        g    = build_graph(all_nodes, all_edges)
        data = g.to_cytoscape_json()
        return jsonify({
            "graph":      data,
            "node_count": g.node_count,
            "edge_count": g.edge_count,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/kg/versions")
def api_kg_versions():
    """GET /api/kg/versions?source=KEY - list stored graph snapshots for a source.
    # MARKER:kg-versions-endpoint
    """
    try:
        source_key = request.args.get("source", "").strip()
        if not source_key:
            return jsonify({"error": "source is required"}), 400

        from server.kg.kg_temporal import TemporalVersionManager
        mgr = TemporalVersionManager()
        versions = mgr.list_versions(source_key)
        return jsonify({"source": source_key, "versions": versions})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/kg/diff")
def api_kg_diff():
    """GET /api/kg/diff?source=KEY&before_id=N&after_id=M - structural diff between two snapshots.
    # MARKER:kg-diff-endpoint
    """
    try:
        source_key = request.args.get("source", "").strip()
        if not source_key:
            return jsonify({"error": "source is required"}), 400

        before_id_raw = request.args.get("before_id", "").strip()
        after_id_raw  = request.args.get("after_id",  "").strip()
        before_id = int(before_id_raw) if before_id_raw.isdigit() else None
        after_id  = int(after_id_raw)  if after_id_raw.isdigit()  else None

        from server.kg.kg_temporal import TemporalVersionManager
        mgr  = TemporalVersionManager()
        diff = mgr.diff_snapshots(source_key, before_id=before_id, after_id=after_id)
        return jsonify(diff)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _build_kg_from_store(source_key, store):
    """Helper: reconstruct EntityNode + EntityEdge lists from the KG store for a source.
    Used by temporal filtering endpoints that need the raw object lists.
    """
    from server.kg.kg_indexer import index_source
    from server.kg.kg_resolver import EntityNode, EntityEdge, EdgeKind
    # Try to get entities from the store's entity table
    try:
        entities = store.search_entities(
            source_key=source_key, layer="schema", limit=2000, query=""
        ) if hasattr(store, "search_entities") else []
    except Exception:
        entities = []

    nodes = []
    seen_ids = set()
    for e in entities:
        nid = getattr(e, "id", None) or f"{source_key}:{getattr(e, 'table_name', '')}:id={getattr(e, 'pk_val', '')}"
        if nid in seen_ids:
            continue
        seen_ids.add(nid)
        nodes.append(EntityNode(
            id          = nid,
            label       = getattr(e, "label", nid),
            entity_type = getattr(e, "entity_type", "UNKNOWN"),
            source_key  = source_key,
            table       = getattr(e, "table_name", ""),
            pk_col      = getattr(e, "pk_col", "id"),
            pk_val      = str(getattr(e, "pk_val", "")),
            row_dict    = {},
        ))
    return nodes, []


def _finalize(key: str, success: bool, log_lines: list) -> None:
    """Persist final status and counts to the registry."""
    # Parse counts from log
    tables = fks = 0
    for line in log_lines:
        if "schema.json written" in line:
            import re
            m = re.search(r"\((\d+)\s+table", line)
            if m:
                tables = int(m.group(1))
            m2 = re.search(r"(\d+)\s+FK", line)
            if m2:
                fks = int(m2.group(1))
            break

    error_msg = None
    if not success:
        # Grab the last ERROR line as the short error message
        for line in reversed(log_lines):
            if "ERROR" in line:
                error_msg = line.split("ERROR", 1)[-1].strip()
                break

    with _lock:
        reg = _load_reg()
        if key in reg:
            reg[key].update({
                "status":    "ready" if success else "error",
                "log":       log_lines,
                "tables":    tables,
                "fks":       fks,
                "processed": time.time(),
                "error_msg": error_msg,
            })
            _save_reg(reg)
        # Create metadata sidecar if it doesn't exist yet
        mp = _meta_path(key)
        if not mp.exists():
            mp.parent.mkdir(parents=True, exist_ok=True)
            _save_meta(key, _load_meta(key))
        _live_logs.pop(key, None)

    # Background KG indexing: fire-and-forget after successful pipeline
    if success:
        def _bg_index():
            try:
                from server.kg.kg_indexer import index_source
                index_source(key)
            except Exception:
                pass  # indexing failure never affects the pipeline result
        threading.Thread(target=_bg_index, daemon=True).start()


# ── Startup: register baked-in ERD directories ────────────────────────────────

def _register_baked_in() -> None:
    """On startup, scan OUTPUT_DIR for pre-built schema.json directories."""
    with _lock:
        reg = _load_reg()
        changed = False
        if OUTPUT_DIR.exists():
            for child in OUTPUT_DIR.iterdir():
                if not child.is_dir():
                    continue
                key = child.name
                if len(key) != 16 or not all(c in "0123456789abcdef" for c in key):
                    continue
                if not (child / "schema.json").exists():
                    continue
                if key in reg:
                    continue
                name_file = child / ".name"
                name = name_file.read_text().strip() if name_file.exists() else f"schema-{key[:6]}"
                reg[key] = {
                    "key": key, "name": name, "path": "",
                    "source": "file",
                    "status": "ready", "tables": 0, "fks": 0,
                    "log": [], "uploaded": time.time(), "processed": time.time(),
                    "error_msg": None,
                }
                changed = True
        if changed:
            _save_reg(reg)


_register_baked_in()


# ── BASE_PATH WSGI wrapper ────────────────────────────────────────────────────
# When BASE_PATH is set (e.g. "/sqlviewer"), wrap the WSGI app so that all
# routes are accessible under that prefix.  Flask's own url_for() respects
# APPLICATION_ROOT (set above), so internal links stay correct.
# Routes are registered without the prefix on the Flask side - the
# DispatcherMiddleware strips the prefix before Flask sees the request.

if BASE_PATH:
    try:
        from werkzeug.middleware.dispatcher import DispatcherMiddleware
        from werkzeug.exceptions import NotFound
        app.wsgi_app = DispatcherMiddleware(NotFound(), {BASE_PATH: app.wsgi_app})
    except ImportError:
        # werkzeug not available (shouldn't happen with Flask, but guard anyway)
        pass


# ── Inject window.BASE_PATH into all templates ────────────────────────────────
@app.context_processor
def _inject_base_path():
    return {"base_path": BASE_PATH}


if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
