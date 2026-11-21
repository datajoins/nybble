# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [4.0.0] - 2026-07-11

### Added

#### Phase 5a - Row counts on ERD nodes

- **`api_server.py`** - new `GET /api/rowcounts/<key>` endpoint: runs `SELECT COUNT(*)`
  for every table in the schema's SQLite database in a single connection pass and returns
  a `{tableName: count}` map.  Returns `{}` gracefully if no queryable DB exists yet (e.g.
  live-connection schemas where the pipeline has not created a local SQLite file).
- **`templates/erd.html`** - row counts loaded **lazily** via a `fetch()` call after the
  layout completes (non-blocking, never slows layout).  Two display surfaces updated:
  - **Node header cards** - count rendered in the top-right corner of each card header
    in dimmed monospace text; numbers ≥1 000 are formatted as `1.2k`, ≥1 000 000 as `1.4M`.
  - **Sidebar table list** - column-count badge replaced with `N rows` once counts arrive;
    full count shown as `title` tooltip on hover.  Falls back silently if the fetch fails.

#### Phase 5b - Column profiling panel in Browse view

- **`api_server.py`** - new `GET /api/profile/<key>/<table>/<col>` endpoint: executes two
  SQLite aggregation queries per call and returns `{total, null_count, distinct_count, min,
  max, avg, top_values[{val,count}×10]}`.  Validates table + column existence before
  querying; read-only SQLite URI connection.  Zero new dependencies.
- **`templates/browse.html`** - **Column profile pane** (slide-in from right, z-index 310,
  above the cell pane):
  - Triggered from the `⠿ Column profile` item in the column header ⠿ popover menu.
  - Displays a 2×3 stat grid: Total rows, Null count (red if > 0), Distinct, Avg, Min, Max.
  - Null-presence bar: a thin red bar showing the null percentage at a glance.
  - Top-values frequency chart: pure CSS horizontal bars (no Chart.js) for the top 10
    values ordered by row count.  Values truncated to 22 chars; NULL shown as a pill.
  - Loading spinner while the API call is in flight; inline error message on failure.
  - Closed by `✕` button or `Escape` key (checked before cell pane on Escape).

#### Phase 5c - Server-side saved queries

- **`api_server.py`** - four new REST endpoints for persistent saved-query storage:
  - `GET  /api/queries/<key>` - list all saved queries (array of `{id, name, sql, created}`)
  - `POST /api/queries/<key>` - create a new saved query; body: `{name, sql}`
  - `PATCH /api/queries/<key>/<id>` - update `name` and/or `sql` of an existing query
  - `DELETE /api/queries/<key>/<id>` - delete a saved query
  Storage: a simple JSON array in `/output/erd/<key>/queries.json` alongside `schema.json`.
  Helper functions `_queries_path / _load_queries / _save_queries` added.
- **`templates/query.html`** - saved-queries sidebar upgraded:
  - **Server is the source of truth**: `_fetchSaved()` loads from `GET /api/queries/<key>`
    on page load and keeps `localStorage` in sync as an offline read cache.
  - **Backward-compatible migration**: existing localStorage entries (pre-Phase-5c, which
    had no `id` field) are automatically shaped into the new `{id, name, sql}` format on
    first load so no user data is lost.
  - **Optimistic deletion**: `deleteSaved(qid)` removes from local state immediately, then
    fires `DELETE /api/queries/<key>/<id>` asynchronously (best-effort).
  - **Offline fallback**: `saveCurrentQuery()` posts to the server; if the request fails,
    saves to localStorage only and shows `"Query saved (offline)"` toast.

### Tests

- **`_smoke_test.py`** - 6 new Phase 5 tests added; total now **30/30 passing**:
  - `api_server: /api/rowcounts route present`
  - `api_server: /api/profile route present`
  - `api_server: saved queries CRUD routes present`
  - `templates/erd.html: row counts JS present`
  - `templates/browse.html: column profile pane present`
  - `templates/query.html: server-side saved queries present`

---

## [3.1.0] - 2026-07-09

### Added

#### Phase 4b - Flat-file → SQLite data loader (CSV, JSON, JSONL, XLSX, Parquet)

- **`flat_file_to_sqlite.py`** *(new)* - zero-extra-dependency data loader for all five
  flat-file formats.  Runs after schema extraction in the pipeline and writes a queryable
  `.db` file into the output dir, making Browse and Query available for every file type:

  | Format | Source | Loader behaviour |
  |---|---|---|
  | **CSV** | stdlib `csv` | Auto-detects delimiter; empty cells → NULL |
  | **JSON** | stdlib `json` | Handles `[…]` array and `{key:[…]}` envelope; list/dict values → JSON text |
  | **JSONL** | stdlib `json` | One record per line; schema widened on first encounter of new keys |
  | **XLSX** | `openpyxl` (already in requirements) | All sheets → individual tables; `datetime/date/time` objects coerced to ISO strings |
  | **Parquet** | `pyarrow` (already in requirements) | Streams row batches; Arrow list/struct → JSON text |

  All loaders: 500-row `executemany` batches (memory-bounded); `WAL` mode for write safety;
  empty-value strings from CSV coerced to NULL; bool → 0/1; nested objects → JSON text.

- **`run_pipeline.py`** - Step 2c: after schema extraction for `csv`, `json`, `jsonl`,
  `xlsx`, `parquet` formats, calls `flat_file_to_sqlite.load_flat_file()` and logs row
  counts to the live pipeline log stream; gracefully no-ops on errors without failing the
  schema step.

### Result

Every file type uploadable to the ERD system now produces a fully queryable SQLite
database automatically:

| Uploaded file | ERD | Browse rows | SQL Query |
|---|---|---|---|
| `.db` / `.sqlite` | ✅ | ✅ | ✅ |
| `.sql` / `.sql.gz` (PG or MySQL) | ✅ | ✅ (Phase 4a) | ✅ (Phase 4a) |
| `.csv` | ✅ | ✅ **(Phase 4b)** | ✅ **(Phase 4b)** |
| `.json` / `.jsonl` | ✅ | ✅ **(Phase 4b)** | ✅ **(Phase 4b)** |
| `.xlsx` | ✅ | ✅ **(Phase 4b)** | ✅ **(Phase 4b)** |
| `.parquet` | ✅ | ✅ **(Phase 4b)** | ✅ **(Phase 4b)** |

---

## [3.0.0] - 2026-07-09

### Added

#### Phase 4a - SQL dump → SQLite auto-import (Browse & Query now work for `.sql` / `.sql.gz`)

- **`import_to_sqlite.py`** *(new)* - zero-dependency (stdlib-only) SQL dump importer:
  - Reads PostgreSQL and MySQL dumps, optionally gzip-compressed
  - **DDL translation layer**: maps 50+ PostgreSQL/MySQL types to their SQLite equivalents
    (`SERIAL/BIGSERIAL → INTEGER`, `DOUBLE PRECISION/NUMERIC → REAL`,
    `BOOLEAN/BOOL → INTEGER`, `TIMESTAMP/DATE/TIME → TEXT`, `BYTEA → BLOB`,
    `CHARACTER VARYING/VARCHAR → TEXT`, `JSONB/JSON/UUID/INET/… → TEXT`,
    MySQL `ENUM/SET → TEXT`, `DECIMAL → REAL`)
  - Strips SQLite-unsupported syntax: `schema.` prefixes, `COLLATE`, `TABLESPACE`,
    `STORAGE`, `WITH (…)`, `INHERITS`, `DEFERRABLE`, `AUTO_INCREMENT`, `ENGINE=`,
    `DEFAULT CHARSET=`, PostgreSQL type casts (`::type`), `GENERATED … AS IDENTITY`,
    `DEFAULT nextval(…)`
  - **PostgreSQL `COPY … FROM stdin` parser**: loads tab-delimited data blocks including
    `\N` → NULL and backslash escape sequences (`\t`, `\n`, `\\`)
  - **`INSERT … VALUES` parser**: handles multi-row `(…),(…)` syntax, quoted strings
    with `''` escapes, NULL, integer and float literals - works for both PG and MySQL
  - Writes a queryable `.db` file into the output dir alongside `schema.json`
- **`run_pipeline.py`** - after extracting the schema from a `postgresql` or `mysql` dump,
  automatically calls `import_to_sqlite.import_dump()` and logs row counts to the live
  pipeline log stream; gracefully skips if the dump is schema-only (no data rows)
- **`api_server.py`** - new `_find_sqlite_db(key)` helper searches in two locations:
  1. `UPLOAD_DIR/<key>/` for an uploaded `.db`/`.sqlite`/`.sqlite3` file (existing behaviour)
  2. `OUTPUT_DIR/<key>/` for a pipeline-generated `.db` file *(new - Phase 4a)*
  Both `api_rows()` and `api_query()` now use this helper, so **Browse and Query work
  automatically for any SQL dump uploaded via the UI - no manual conversion needed**

### Fixed

- `api_rows()`: improved "no queryable source" error message - now explains that flat files
  (CSV/JSON/XLSX/Parquet) have no row data and directs the user to re-process SQL dumps
- `api_query()`: same error path now uses `_find_sqlite_db()` and reports a consistent message

---

## [2.2.0] - 2026-07-09

### Added

#### Phase 3 - Semantic badges, cell detail pane, CTE builder, Parquet extractor

- **`extract_parquet.py`** *(new)* - schema extraction from `.parquet` files via `pyarrow`:
  Arrow type → SQL type mapping (`INT8`-`INT64` → `INTEGER`, `FLOAT`/`DOUBLE` → `REAL`,
  `DATE32` → `DATE`, `TIMESTAMP` → `TIMESTAMP`, `TIME` → `TIME`, `BOOL` → `BOOLEAN`,
  `BINARY`/`LARGE_BINARY` → `BLOB`, all others → `TEXT`); first integer column heuristic
  marks the primary key.
- **`run_pipeline.py`** - `.parquet` format now dispatched to `extract_parquet.extract()`.
- **`requirements.txt`** - `pyarrow>=14.0.0` added for Parquet support.
- **`templates/browse.html`** - **semantic type badges** appear inline for recognised cell
  patterns: `@ email`, `🔗 url`, `🖼 img`, `📞 phone`, `TRUE/FALSE` (bool), `uuid`, `ip`;
  **cell detail pane** slides in from the right on any cell click - shows full value,
  JSON pretty-print for objects, character count, semantic hint, Copy/Filter/Query actions;
  **Filter action** sets `col = value` filter instantly; **Query action** drills through
  to the Query view with a pre-filled `SELECT … WHERE col = 'val'` statement.
- **`templates/query.html`** - same **semantic type badges** and **cell detail pane**
  as browse view (Copy + Query actions; no Filter since there is no table state);
  **CTE builder** - "✛ CTE" button in results toolbar wraps the current SQL as
  `WITH _result AS (…) SELECT * FROM _result` so the user can immediately layer a
  second query on top; **Escape** key closes the cell pane before clearing other panels.

---

## [2.1.0] - 2026-07-08

### Added

#### Phase 2 - Excel support, viz tabs, stale indicator, history timestamps, date presets

- **`extract_xlsx.py`** - schema extraction from `.xlsx` workbooks via `openpyxl`:
  each sheet becomes a table, column types inferred from cell values
  (`BOOLEAN / TIMESTAMP / DATE / TIME / INTEGER / REAL / TEXT`), first integer column
  marked as primary key, sanitized sheet names used as table names.
- **`run_pipeline.py`** - `.xlsx` format now dispatched to `extract_xlsx.extract()`;
  Parquet error message updated to suggest `pip install pyarrow`.
- **`requirements.txt`** - `openpyxl>=3.1.0` added for Phase 2 Excel support.
- **`api_server.py`** - `/api/rows?_format=xlsx` returns a true `.xlsx` file via
  `openpyxl` write-only workbook; graceful `501` if openpyxl is not installed.
- **`templates/query.html`** - multiple viz tabs (Bar / Line / Scatter / Pivot);
  stale-result amber banner when SQL changes after last run; history entries now
  include timestamps with human-relative display ("just now", "5m ago", "3h ago",
  "Jan 5"); "Clear all" button in history panel; `↓ XLSX` export button in results toolbar.
- **`templates/browse.html`** - relative date filter preset chips (Today / Yesterday /
  Last 7 days / Last 30 days / This month / This year / Clear) auto-shown when a date
  column is detected; `↓ XLSX` export button in topbar routed through `/api/rows?_format=xlsx`.

### Changed

- **`templates/query.html`** - "Quick chart" `<details>` replaced with persistent tabbed
  viz panel; chart instances keyed by type so all four tabs retain their own state.
- History deduplication now compares `sql` field instead of full entry; old plain-string
  history entries (from v2.0) are still rendered gracefully.

---

## [2.0.0] - 2026-07-02

### Added

#### Phase 0 - New format extractors (stdlib only, no new pip deps)
- **`extract_csv.py`** - schema inference from CSV / TSV files: BOM-safe (`utf-8-sig`),
  auto-delimiter detection via `csv.Sniffer`, samples up to 1 000 rows, infers
  `INTEGER / REAL / DATE / TEXT` per column, marks first non-empty column as primary key.
- **`extract_json.py`** - schema inference from JSON and JSONL/NDJSON files: handles list root,
  dict-with-list root, and plain dict; JSONL streams first 500 lines; same type-inference rules
  as CSV extractor; uses `utf-8` with `errors="replace"`.
- **`detect_format.py`** - extended with five new format tokens: `"csv"`, `"json"`, `"jsonl"`,
  `"xlsx"` (ZIP magic `PK\x03\x04`), `"parquet"` (`PAR1` magic); content-sniff fallback for
  JSON files without a recognised extension; all existing `sqlite / postgresql / mysql / unknown`
  tokens unchanged.
- **`run_pipeline.py`** - dispatch for `csv` and `json/jsonl` tokens wired to new extractors;
  graceful `sys.exit(1)` with an install hint for `xlsx` / `parquet` (requires optional deps);
  updated `--help` text for `input` argument.

#### Phase 1 - Table browser, SQL editor, search, metadata

##### API routes (`api_server.py`)
- **`GET /browse/<key>`** - serves the new table browser page.
- **`GET /query/<key>`**  - serves the new SQL query editor page.
- **`GET /api/search?q=`** - cross-schema full-text search over table and column names
  (returns up to 100 results, restricted to `status == "ready"` schemas).
- **`GET /api/metadata/<key>`** - returns the JSON metadata sidecar for a schema.
- **`PATCH /api/metadata/<key>`** - deep-merges provided fields into the metadata sidecar.
- **`GET /api/rows/<key>/<table>`** - paginated row data with offset pagination (`page` / `page_size`),
  sort (`_sort` / `-col`), column filters (`col__op=val`, ops: `exact contains gt gte lt lte neq`),
  and CSV download (`_format=csv`); supports both live connstring and SQLite file sources.
- **`POST /api/query/<key>`** - read-only SQL execution (`SELECT / WITH / EXPLAIN / PRAGMA`);
  wraps queries in a LIMIT subquery; sets PostgreSQL `statement_timeout = 10s`; returns
  `columns`, `rows`, `row_count`, `truncated`, `elapsed_ms`.
- **Metadata sidecar** - `_finalize()` now creates `/output/erd/<key>/metadata.json` on first
  pipeline completion; helper functions `_meta_path / _load_meta / _save_meta` added.
- **`GET /erd/<key>`** - now passes `key=key` to the Jinja template (required for new nav links).

##### Table browser (`templates/browse.html`) - NEW FILE
- Full-page dark-theme table browser matching the ERD viewer design language.
- Sidebar table list with column counts; click to load data.
- Paginated data grid fetched from `/api/rows/`; sticky header; right-aligned numerics;
  NULL pills; JSON cell expand via `<details>`.
- Column header sort buttons (↑/↓) and popover menu (sort / focus filter).
- Filter row with debounced inputs; operator prefixes: `>`, `<`, `>=`, `<=`, `=`, `!=`, bare text → `contains`.
- Auto value formatting: USD amounts, percentages, ISO dates.
- CSV download and Copy-as-Markdown-table export.
- Quick chart (`<details>` section): Chart.js 4 from CDN, X/Y column pickers, bar/line/scatter.
- Row permalink (copies `/browse/<key>?table=…&col=val` to clipboard).
- Keyboard: `Escape` clears all filters.

##### SQL query editor (`templates/query.html`) - NEW FILE
- Full-page dark-theme SQL editor; CodeMirror 5 from CDN with `monokai` theme, SQL mode,
  `Ctrl+Enter` / `Cmd+Enter` run binding, and SQL hint autocomplete (`sql-hint` addon).
- Schema sidebar: table list with column tree (click table → inserts `SELECT * … LIMIT 100`;
  click column → inserts at cursor).
- Resizable split pane (editor top / results bottom); ratio persisted in `localStorage`.
- Parameter widgets: scans SQL for `{{varname}}` and renders `<input>` fields inline.
- EXPLAIN button: prepends `EXPLAIN` and shows result in a collapsible panel.
- Error panel with Escape-to-dismiss.
- Result grid: sticky header, NULL pills, JSON cell expand, conditional row highlighting
  (hsl scale on first numeric column), column type badges.
- Export: CSV Blob download, JSON Blob download, Copy as Markdown.
- Query history: `localStorage` ring buffer (50 entries, keyed per schema); clickable history panel.
- Saved queries: `localStorage` save/load/delete; listed in sidebar.
- Share URL: encodes SQL as `btoa()` in `?sql=` param; restores on page load.
- Quick chart: Chart.js, auto-update column pickers after each query run.
- Keyboard shortcuts: `Ctrl/Cmd+Enter` run, `Escape` dismiss, `?` shortcuts dialog, `Ctrl+H` history.

##### Index UI (`templates/index.html`) - EXTENDED
- **Cmd+K / Ctrl+K command palette**: full-screen overlay; searches schema cards, dispatches
  to `GET /api/search` for table/column results; arrow-key navigation; Enter activates.
- **Global search bar** in topbar: clicking opens the palette.
- **Cross-view nav buttons** on schema cards (server-rendered and JS-generated):
  "Browse →" and "Query →" alongside existing "View ERD →" (visible when `status == "ready"`).
- **Toast notification system**: fixed bottom-right `showToast(msg, kind)` global function;
  auto-dismisses at 3 s; `kind ∈ {success, error, info}`; triggered on upload completion.
- **File type hints** updated: upload zone now lists `.csv · .json · .jsonl`;
  `<input accept>` extended to include `.csv, .json, .jsonl, .ndjson`.

##### ERD viewer (`templates/erd.html`) - EXTENDED
- **PNG export button** ("⬇ PNG") in topbar: `cy.png({output:'blob', scale:2, bg:'#141616'})`.
- **SVG export button** ("⬇ SVG") in topbar: `cy.svg({scale:1, bg:'#141616', full:true})`.
- **Cross-view nav links** ("Browse →" / "Query →") in topbar, before zoom controls.
- **Deep-link hash URL**: `focusTable()` now calls `history.replaceState` to update
  `#table=<name>`; `boot()` reads `location.hash` on startup and auto-focuses the named table.
- **`e` keyboard shortcut**: pressing `e` (not in an input) triggers PNG export.

### Changed
- `run_pipeline.py` help text for `input` argument updated to list all supported extensions.
- `api_server.py` route comment block updated to document new routes and storage paths.

### Notes
- **No new pip dependencies** for Phase 0 or Phase 1.  All CSV/JSON extraction uses Python
  stdlib (`csv`, `json`, `re`, `pathlib`).  Chart.js and CodeMirror 5 are loaded via CDN
  `<script>` tags.  No Node.js, no npm, no build step.

---

## [1.0.0] - 2026-06-07

### Added

#### Core pipeline
- Automatic schema introspection for **SQLite** (via `PRAGMA` - zero extra deps),
  **PostgreSQL** dumps (streaming DDL parser, plain or gzip-compressed),
  and **MySQL** dumps (streaming DDL parser, plain or gzip-compressed).
- Live database introspection for PostgreSQL and MySQL via SQLAlchemy `inspect()`.
- Foreign-key inference from column-naming conventions (e.g. `txn_firm_id` → `kb_firms.firm_id`)
  for schemas that declare no explicit FK constraints; ambiguous matches are silently skipped.
- Magic-byte + content-heuristic format detection (`detect_format.py`) - no file extension required.
- Normalised schema dataclass model (`schema_model.py`) shared by all extractors and the renderer.

#### Interactive ERD viewer (`templates/erd.html`)
- **Cytoscape.js** graph rendering - no Node.js, no npm, no build step; loaded from CDN at runtime.
- **ELK layered** hierarchical layout (jsDelivr CDN): direction `RIGHT`, `BRANDES_KOEPF` node placement,
  `LAYER_SWEEP` crossing minimisation - source tables on the left, terminal tables on the right.
- **Topological-sort fallback** layout (pure JavaScript, no CDN dependency): Kahn's algorithm over the FK
  graph; assigns each table to a depth column and spaces columns evenly across the canvas.
- **Smooth curved edges**: Cytoscape `unbundled-bezier` curve style with Canvas `bezierCurveTo` glow
  animation for pixel-perfect match.
- Animated FK edge particles (`requestAnimationFrame`) with colour-coded glow (purple `#a78bfa`).
- Three **view modes**: All fields / Keys only / Name - toggled from the top bar.
- **Sidebar search**: live filter by table name or column name; press `/` to focus, `Escape` to clear.
- **Detail panel**: click any node to open a slide-in column list with FK jump links.
- **Minimap**: toggleable overview (bottom-right corner).
- Fully **shareable URLs**: each processed schema gets a stable `GET /erd/<key>` route.

#### Web UI and API (`templates/index.html`, `api_server.py`)
- Selector UI with four upload tabs: File Upload, Connection String, DB Form, Remote URL.
- Schema cards with colour-coded state borders and per-card View ERD / Retry / Delete / Logs actions.
- Real-time log panel (800 ms polling during processing) with `[HH:MM:SS]` timestamps.
- REST API:
  - `GET  /api/files` - list all registered schemas
  - `POST /api/upload` - upload a file (`multipart/form-data`, field `file`)
  - `POST /api/connect` - register live DB or remote URL
  - `POST /api/process/<key>` - trigger or re-trigger the pipeline
  - `GET  /api/status/<key>` - poll status; returns registry record with log tail
  - `GET  /api/log/<key>` - full pipeline log (supports `?stream=1` chunked response)
  - `DELETE /api/delete/<key>` - remove schema from registry and disk
  - `GET  /erd/<key>` - full-page interactive ERD viewer

#### Docker
- Two-stage Dockerfile: `python:3.12-slim` builder + `nginx:1.27-alpine` server.
- Three operating modes:
  - **Mode A** - bake a database into the image at build time.
  - **Mode B** - volume-mount a file processed automatically on first container start.
  - **Mode C** - upload files at runtime via the browser UI (default).
- Published to Docker Hub as `datajoins/nybble:1.0.0`.
- Named Docker volumes `erd-uploads` and `erd-output` for persistent data across restarts.
- Health check: `wget -q -O /dev/null http://127.0.0.1:80/`.

#### Standalone demo data seeder (`../scripts/seed_demo_standalone.py`)
- Generates a realistic synthetic Vyapar-style Indian trading database (12 tables, 17 FK edges)
  without any Django dependency; targets SQLite (default) or PostgreSQL.
