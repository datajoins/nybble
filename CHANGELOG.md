# Changelog

All notable changes to **datajoins-nybble** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.5] - 2026-11-22

### Fixed

- **Upload pipeline path** (`nybble/serve.py`): `nybble serve` now sets the
  `PIPELINE` environment variable to `server/pipeline/run_pipeline.py` relative
  to the installed package before importing `api_server.py`.  Previously the
  default was Docker's `/app/run_pipeline.py`, causing every upload to fail with
  "can't open file" outside Docker.

- **`[serve]` extra now includes Flask** (`pyproject.toml`): `pip install
  "datajoins-nybble[serve]"` previously only added Waitress.  Flask is now
  included, so a single extra is sufficient to run `nybble serve` without Docker.

- **Flask missing error message** (`nybble/serve.py`): the `ImportError` now
  correctly says `pip install "datajoins-nybble[serve]"` instead of `[dev]`.

- **`python -m nybble serve` works** (`nybble/__main__.py`): a new
  `__main__.py` module delegates to the CLI, so `python -m nybble <command>`
  works correctly.  The MCP `nybble_serve_start` tool uses this form.

- **`nybble share` confirms before uploading** (`nybble/cli.py`): the share
  command now prints a warning and requires explicit confirmation before sending
  any data to GitHub Gist or share.datajoins.com.

### Documentation

- **README Python requirement** corrected: `≥ 3.9` → `≥ 3.10`.

- **MCP docs updated** (`README.md`):
  - Correct Claude Desktop config paths (macOS and Windows).
  - Added Claude Code instructions (`claude mcp add` and `.mcp.json`).
  - Tool count corrected to 72.
  - Tools split into "works offline" (18) and "requires `nybble serve`" (48+)
    so users know which tools are available from a plain pip install.

---

## [1.0.4] - 2026-11-22

### Changed

- Version bump only — required because the `1.0.3` wheel filename was already
  accepted by PyPI on the first publish attempt; PyPI permanently rejects
  re-uploads of the same filename regardless of content.

---

## [1.0.3] - 2026-11-22

### Added

- **`server/` included in wheel and repo** (`server/app/`, `server/ingestion/`,
  `server/kg/`, `server/pipeline/`): the full Flask web UI is now published to PyPI
  and present in the GitHub repo.  `nybble serve` works from a plain `pip install`
  without cloning — no Docker required.
  Install the optional WSGI server for production use:
  ```
  pip install "datajoins-nybble[serve]"   # adds Waitress; Flask dev server used otherwise
  nybble serve
  ```

### Fixed

- **`nybble-mcp` auto-loads `nybble.yaml`** (`nybble/mcp_server.py`): the MCP server
  now calls `find_config()` / `load_config()` / `apply_config()` at startup, mirroring
  the behaviour of `nybble serve`.  If `nybble.yaml` is present in the working directory
  the configured sources are pre-loaded before the first tool call.  Startup never fails
  if the config file is absent or any source fails to load.

---

## [1.0.2] - 2026-11-22

### Fixed

- **MCP server (`nybble/mcp_server.py`)**: the `[all]` optional-dependency group
  previously declared `mcp>=1.0` with no upper bound, allowing pip to pull in
  `mcp 2.x` which dropped the `Server.list_tools()` / `Server.call_tool()` decorator
  API that nybble relies on.  The `[all]` group is now pinned to `mcp>=1.0,<2.0`,
  matching the existing `[mcp]` group.  Users who had `mcp 2.x` installed should run:
  ```
  pip install "datajoins-nybble[mcp]" "mcp>=1.0,<2.0"
  ```

- **PyPI Changelog link (404)**: the `CHANGELOG.md` relative link in `README.md`
  resolved to a non-existent PyPI URL (`pypi.org/project/datajoins-nybble/CHANGELOG.md/`).
  The link is now an absolute GitHub URL that works on both PyPI and GitHub:
  `https://github.com/datajoins/nybble/blob/main/CHANGELOG.md`

- **`pyproject.toml` metadata**: added `Changelog` to `[project.urls]` so PyPI
  renders a direct changelog link on the project sidebar; added the
  `License :: OSI Approved :: Apache Software License` classifier so the
  Apache-2.0 licence badge on PyPI resolves correctly; promoted
  `Development Status` to `5 - Production/Stable`.

### Notes

- `nybble serve` starts the full browser UI from a standard `pip install`.
  Run `pip install "datajoins-nybble[serve]"` to also get Waitress (recommended
  for multi-user or production use); Flask's built-in dev server is used
  automatically as a fallback when Waitress is absent.
  The ~30 MCP tools that call the serve UI (`nybble_serve_start`,
  `nybble_sources_*`, `nybble_browse_rows`, `nybble_write_row`,
  `nybble_delete_row`, `nybble_kg_ui_*`, etc.) require the server to be
  running first.  All other tools — `nybble_load`, `nybble_query`,
  `nybble_schema`, `nybble_profile`, `nybble_erd`, `nybble_kg`, `nybble_diff`,
  `nybble_kg_search`, `nybble_kg_frd_scores`, etc. — work fully offline.

---

## [1.0.1] - 2026-11-01

### Added

- **Knowledge Graph analytics** (`nybble/kg.py`): `frd_scores`, `frd_expand`,
  `node_detail`, `kg_graph`, `kg_validate` — all exposed in the public API and
  as MCP tools (`nybble_kg_frd_scores`, `nybble_kg_frd_expand`, `nybble_kg_node`,
  `nybble_kg_graph`, `nybble_kg_validate`).
- **MCP server** (`nybble/mcp_server.py`): 72-tool MCP server covering data ingestion,
  SQL, ERD, KG, serve-UI integration, write operations, charts, metrics, and lineage.
  Entry point: `nybble-mcp` (registered in `[project.scripts]`).
- **Workspace config** (`nybble/config.py`): `load_config` / `apply_config` / `WorkspaceConfig`
  for YAML-driven multi-source workspaces (`nybble.yaml`).
- **Lineage DAG** (`nybble/lineage.py`): `build_lineage_dag`, `dag_to_html`,
  `save_lineage_html` — parse SQL files to extract table-level lineage, export as HTML.
- **Share / collaborative exports** (`nybble/share.py`): `share`, `NybbleShareResult`,
  token-based share registry for exporting ERD/KG to Gist or datajoins backends.
- **Semantic node search** (`nybble/kg.py`): `search_nodes` falls back gracefully from
  `sentence-transformers` semantic search to substring matching when the extra is absent.
- **Column profiler** (`nybble/profiler.py`): `profile_relation`, `ColumnProfile` —
  null%, cardinality, min/max/top value per column.
- **Schema diff** (`nybble/diff.py`): `schema_diff`, `SchemaDiff` — added/removed/
  type-changed/unchanged columns between two sources.
- **Report builder** (`nybble/report.py`): `build_report`, `NybbleReport`,
  `ReportSection` — programmatic HTML/Markdown report generation.
- **CLI** (`nybble/cli.py`): `browse`, `query`, `erd`, `kg` (with sub-commands),
  `info`, `explain`, `profile`, `diff`, `history`, `report`, `serve`, `share`,
  `lineage`, `repl`.
- **Optional extras**: `mcp`, `semantic`, `serve`, `postgres`, `mysql`, `mssql`,
  `oracle`, `mongo`, `redis`, `cassandra`, `elastic`, `kafka`, `dynamo`, `kuzu`,
  `rdf`, `textual`, `all`.

### Changed

- `mcp` optional extra pinned to `mcp>=1.0,<2.0` to guard against breaking API
  changes in the MCP SDK.

---

## [1.0.0] - 2026-10-15

### Added

#### Core engine

- **`nybble.load(source)`** — single-call data loading for local files
  (`.csv`, `.parquet`, `.json`, `.jsonl`, `.xlsx`, `.avro`), database URIs
  (`sqlite://`, `postgresql://`, `mysql://`, `duckdb://`, `clickhouse://`),
  object-storage URIs (`s3://`, `gs://`, `r2://`), and HTTP/HTTPS JSON APIs.
  Returns a `Relation` with `.name`, `.row_count`, `.source_type`.
- **`nybble.query(sql)`** — DuckDB SQL over all loaded sources; supports
  cross-source JOINs, CTEs, window functions, and all DuckDB extensions.
  Returns `QueryResult` with `.columns`, `.rows`, `.to_dict()`, `.to_df()`.
- **`nybble.erd()`** — self-contained interactive ERD HTML from loaded sources;
  Cytoscape.js rendering with ELK hierarchical layout; FK inference from naming
  conventions; PNG/SVG export; deep-link URLs.
- **`nybble.kg()`** — Knowledge Graph export (GraphML, JSON-LD, static HTML);
  entity-type classification; FRD (Frequency-Relative Degree) node scoring.
- **`nybble.browse(source)`** — full-terminal paginated data grid with live
  filters, column sort, and CSV export.
- **`nybble serve`** — full browser UI (Flask + optional Waitress); upload files,
  browse tables, run SQL queries, view interactive ERD and Knowledge Graph.

#### Package

- Published to PyPI as `datajoins-nybble`.
- Entry points `nybble` CLI and `nybble-mcp` MCP server registered.
- Apache-2.0 licence.
- Supports Python 3.10, 3.11, 3.12.
