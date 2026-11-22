<div align="center">

# Nybble

### Browse, Query, ERD & Knowledge Graph. One `pip install`.

[![CI](https://img.shields.io/github/actions/workflow/status/datajoins/nybble/ci.yml?branch=main&label=CI&logo=github)](https://github.com/datajoins/nybble/actions/workflows/ci.yml)
[![PyPI Version](https://img.shields.io/pypi/v/datajoins-nybble)](https://pypi.org/project/datajoins-nybble/)
[![Python Versions](https://img.shields.io/pypi/pyversions/datajoins-nybble)](https://pypi.org/project/datajoins-nybble/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/datajoins/nybble?style=social)](https://github.com/datajoins/nybble)

**[Website](https://nybble.datajoins.com) · [Documentation](https://nybble.datajoins.com/documentation.html) · [PyPI](https://pypi.org/project/datajoins-nybble/) · [Changelog](CHANGELOG.md) · [Known Issues](KNOWN_ISSUES.md) · [Issues](https://github.com/datajoins/nybble/issues)**

</div>

---

Nybble is an ultra-lightweight Python data engine. A CSV here. A Parquet file there. A live Postgres database. A REST API. `pip install` and you see how they all connect — no six-container platform required.

It unifies four things that normally demand four separate tools:

| Capability | What you get |
|---|---|
| **Browse** | Full-terminal paginated data grid — live filter, column sort, keyboard nav |
| **SQL Arena** | Cross-source DuckDB SQL — join a CSV to a Parquet to a live database |
| **ERD** | Auto-generated interactive Entity-Relationship Diagram with 4-tier FK inference |
| **Knowledge Graph** | Semantic entity graph with GraphML, JSON-LD, PGQ DDL, and HTML export |

---

## Installation

```bash
pip install datajoins-nybble
```

**Requirements:** Python ≥ 3.10

### Optional extras

```bash
# Database connectors
pip install "datajoins-nybble[postgres]"    # PostgreSQL (psycopg2 + connectorx)
pip install "datajoins-nybble[mysql]"       # MySQL / MariaDB (pymysql)
pip install "datajoins-nybble[mssql]"       # SQL Server (pyodbc)
pip install "datajoins-nybble[oracle]"      # Oracle Database (oracledb)
pip install "datajoins-nybble[mongo]"       # MongoDB (pymongo)
pip install "datajoins-nybble[redis]"       # Redis (redis-py)
pip install "datajoins-nybble[cassandra]"   # Cassandra (cassandra-driver)
pip install "datajoins-nybble[elastic]"     # Elasticsearch (elasticsearch)
pip install "datajoins-nybble[kafka]"       # Kafka topic streaming (confluent-kafka)
pip install "datajoins-nybble[dynamo]"      # Amazon DynamoDB (boto3)

# Features
pip install "datajoins-nybble[mcp]"         # MCP server for AI agents (Claude, Cursor, VS Code)
pip install "datajoins-nybble[serve]"       # Web UI production server (waitress)
pip install "datajoins-nybble[semantic]"    # Neural node search (sentence-transformers)
pip install "datajoins-nybble[kuzu]"        # Embedded graph DB for cypher queries
pip install "datajoins-nybble[rdf]"         # RDF/Turtle + SPARQL (rdflib)
pip install "datajoins-nybble[textual]"     # Enhanced TUI browser (textual)

# Recommended bundle
pip install "datajoins-nybble[all]"         # postgres + mysql + mcp + rdflib + waitress
```

---

## Quickstart

### 1. Browse any data source in the terminal

```bash
nybble browse ./sales.csv
nybble browse ./warehouse.parquet
nybble browse "postgresql://user:pass@localhost:5432/mydb"
```

Arrow keys to navigate, `/` to filter, `q` to quit.

### 2. Run SQL across multiple sources

```bash
nybble query "
  SELECT c.name, COUNT(o.id) AS orders, SUM(o.total) AS revenue
  FROM customers c JOIN orders o ON c.id = o.customer_id
  GROUP BY c.name ORDER BY revenue DESC
" \
  --source customers=customers.csv \
  --source orders=orders.parquet
```

### 3. Generate an ERD

```bash
# Produces a self-contained HTML file and opens it in your browser
nybble erd \
  --source customers=customers.csv \
  --source orders=orders.parquet \
  --source products=products.csv
```

No declared foreign keys? Nybble's 4-tier inference engine fills in the relationships automatically.

### 4. Build a Knowledge Graph

```bash
# Entity graph exported as GraphML + JSON-LD + interactive HTML
nybble kg \
  --source customers=customers.csv \
  --source orders=orders.parquet
```

### 5. Profile column statistics

```bash
nybble profile orders.csv
```

### 6. Compare two schema versions

```bash
nybble diff schema_v1.csv schema_v2.csv
nybble diff "sqlite:///old.db" "sqlite:///new.db"
```

### 7. Start the web UI

```bash
pip install "datajoins-nybble[serve]"
nybble serve --port 7700 --source mydb.sqlite
```

Open [http://localhost:7700](http://localhost:7700) for browser-based Browse, SQL Arena, ERD, and Knowledge Graph.

---

## Workspace Config (`nybble.yaml`)

Declare sources once. Every `nybble` command in the same directory loads them automatically — no `--source` flags required.

```yaml
# nybble.yaml
version: "1.0"

workspace:
  name: "my-project"

files:
  - id: customers
    type: csv
    path: ./data/customers.csv

  - id: orders
    type: parquet
    path: ./data/orders.parquet

databases:
  - id: production
    type: postgresql
    connection_string: "postgresql://${DB_USER}:${DB_PASS}@localhost:5432/mydb"

streams:
  - id: github-issues
    type: api
    url: "https://api.github.com/repos/myorg/myrepo/issues"
    headers:
      Authorization: "Bearer ${GITHUB_TOKEN}"
    format: json
```

```bash
# All sources loaded automatically — no --source flags needed
nybble erd
nybble kg
nybble query "SELECT * FROM customers LIMIT 10"
nybble repl

# Or point to an explicit config
nybble erd --config ./configs/staging.yaml
```

- `${ENV_VAR}` tokens are substituted from shell environment at load time.
- Config is auto-discovered by searching for `nybble.yaml`, `nybble.yml`, `.nybble.yaml`, or `.nybble.yml` in the current directory.
- See [`nybble.yaml.example`](nybble.yaml.example) for a fully annotated reference.

---

## Python API

```python
from nybble import load, query, erd, kg, kg_summary, profile_relation, schema_diff

# Load any source — file, DB URI, S3/GCS URL, or inline data
customers = load("customers.csv")
orders    = load("orders.parquet")
live_db   = load("postgresql://user:pass@localhost/mydb")

# Cross-source SQL via DuckDB
result = query("""
    SELECT c.name, COUNT(o.id) AS order_count, SUM(o.total) AS revenue
    FROM customers c
    JOIN orders o ON c.id = o.customer_id
    GROUP BY c.name
    ORDER BY revenue DESC
""")
print(result.to_polars())   # also: .to_pandas(), .to_arrow(), .to_dicts()

# Column statistics
for col in profile_relation("customers"):
    print(col.name, col.null_pct, col.distinct, col.top_value)

# Schema diff
diff = schema_diff("v1.csv", "v2.csv")
print(diff.added, diff.removed, diff.changed)

# Self-contained ERD HTML
erd(output=".nybble/schema.html")

# Semantic Knowledge Graph
result = kg()
summary = kg_summary()
# {'node_count': 1840, 'edge_count': 4122, 'entity_types': [...], ...}

# Load from nybble.yaml
from nybble import load_config, apply_config
cfg = load_config("nybble.yaml")
apply_config(cfg)   # all declared sources are now available to query/erd/kg
```

---

## Source Types

Nybble's resolver accepts anything without configuration:

| Source | Example |
|---|---|
| CSV / TSV | `customers.csv`, `data.tsv` |
| Parquet | `events.parquet` |
| JSON / JSONL | `records.json`, `stream.jsonl` |
| Excel | `report.xlsx`, `legacy.xls` |
| Avro | `kafka-export.avro` |
| SQLite | `local.db`, `sqlite:///path/to/db` |
| PostgreSQL | `postgresql://user:pass@host/db` |
| MySQL / MariaDB | `mysql://user:pass@host/db` |
| SQL Server | `mssql+pyodbc://user:pass@host/db` |
| Oracle | `oracle+oracledb://user:pass@host/db` |
| MongoDB | `mongodb://user:pass@host/db` |
| S3 / GCS / R2 | `s3://bucket/data.parquet`, `gs://bucket/file.csv` |
| HTTP URL | `https://api.example.com/data.json` |
| Inline Python | `load([{"id": 1, "name": "Alice"}])` |

All sources register as virtual DuckDB tables in a shared in-process session — no copies, no intermediate files.

---

## ERD — Nybble Sense

When a schema has no declared foreign-key constraints (CSV exports, ORMs that enforce at the application layer, cloud-sync snapshots), Nybble's four-tier heuristic engine infers them:

| Tier | Method | Notes |
|---|---|---|
| **1** | Explicit DB constraints | Short-circuits here when found |
| **2** | Column name suffix matching | `_id`, `_fk`, `_key`, `_ref`, `_uuid`, `_code` |
| **3** | Column name equality across tables | Same name = candidate join |
| **4** | Statistical value containment | FK values ⊆ PK values, ≥ 85% threshold |

Every inferred FK carries a confidence score. The output is a single self-contained HTML file — deep-linkable via `?highlight=<table>` and embeddable via `?embed=1`.

---

## Knowledge Graph — Nybble GraphAI

Builds a semantic `MultiDiGraph` (NetworkX) from any set of loaded sources. Every row becomes a node; every FK relationship becomes a directed edge; co-occurring values across unlinked tables become inferred cross-source edges.

**Export formats:**
- **GraphML** — importable by Gephi, yEd, NetworkX
- **JSON-LD** — schema.org vocabulary, consumable by AI/ML pipelines and graph databases
- **Self-contained HTML** — interactive Cytoscape force-directed visualisation
- **SQL/PGQ DDL** — `nybble kg --export pgq` emits DuckDB `CREATE PROPERTY GRAPH` DDL

Node-level metrics computed for every entity: PageRank, degree centrality, betweenness centrality, and community membership (Louvain algorithm).

```bash
# Search the graph
nybble kg search "Alice" --source customers.csv
nybble kg search "shipped" --source orders.csv

# Export DuckDB CREATE PROPERTY GRAPH DDL
nybble kg pgq --source customers.csv --source orders.csv

# Print summary JSON only
nybble kg --summary-only --source customers.csv --source orders.csv
```

---

## MCP Server — AI Agent Integration

Nybble ships a full [Model Context Protocol](https://modelcontextprotocol.io/) server exposing 72 tools to any MCP-compatible agent — Claude Desktop, Claude Code, Cursor, VS Code Copilot, Raycast, and others.

```bash
pip install "datajoins-nybble[mcp]"
```

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "nybble": { "command": "nybble-mcp" }
  }
}
```

**Claude Code:**

```bash
# Register once in your project directory
claude mcp add nybble -- nybble-mcp
```

**Or start manually:**

```bash
nybble-mcp
```

### Available tools (72 total)

**Works offline — no server needed (18 tools):**

| Tool | Description |
|---|---|
| `nybble_load` | Load any source: file, DB URI, S3/GCS URL, HTTP API |
| `nybble_list_tables` | List all loaded sources |
| `nybble_schema` | Column names and types for a loaded table |
| `nybble_query` | Execute DuckDB SQL across all loaded sources |
| `nybble_explain` | DuckDB query execution plan |
| `nybble_profile` | Column statistics: null%, cardinality, min/max/top value |
| `nybble_infer_schema` | Infer FK relationships — deterministic, zero LLM calls |
| `nybble_erd` | Generate interactive ERD HTML, return path |
| `nybble_kg` | Build and export Knowledge Graph, return paths |
| `nybble_kg_summary` | Node/edge counts, entity types |
| `nybble_kg_graph` | Full KG as degree-sorted node and edge lists |
| `nybble_kg_search` | Free-text node search (semantic or substring) |
| `nybble_kg_frd_scores` | Rank nodes by Frequency-Relative Degree score |
| `nybble_kg_frd_expand` | BFS expansion from a seed node, ranked by FRD |
| `nybble_kg_validate` | KG structural consistency checks |
| `nybble_kg_export_pgq` | Generate DuckDB CREATE PROPERTY GRAPH DDL |
| `nybble_diff` | Schema diff between two sources |
| `nybble_search_history` | Search persistent query history log |

**Requires `nybble serve` (54 tools):** full Browse, SQL Arena, ERD studio, and KG UI tools — `nybble_sources_list`, `nybble_browse_rows`, `nybble_run_query`, `nybble_source_connect`, `nybble_erd_detail`, `nybble_kg_ui_*`, and more.

**How it works:**

```
AI agent (LLM decides what to call)
    └─ nybble_load("sales.csv")     → resolver._load_file()   ← DuckDB CREATE VIEW
    └─ nybble_query("SELECT ...")   → arena.query()           ← duckdb.execute()
    └─ nybble_infer_schema()        → infer_fk.py Tier 1-4   ← column matching + stats
    └─ nybble_kg_summary()          → kg._build_graph()       ← NetworkX + PageRank
```

No LLM calls on the Nybble side. Every tool is deterministic and reproducible.

---

## CLI Reference

```
nybble [OPTIONS] COMMAND [ARGS]...

Commands:
  browse           Interactive terminal browser — arrow keys, / filter, q quit
  diff             Schema diff: added / removed / type-changed columns
  erd              Self-contained interactive ERD HTML with FK inference
  explain          DuckDB query execution plan (--analyze for actual timing)
  history          Recent query history log (--search, --limit)
  info             Schema, row count, source type, and sample rows
  kg               Build and export a semantic Knowledge Graph
  kg search        Search KG nodes by text (substring or semantic)
  kg pgq           Emit DuckDB CREATE PROPERTY GRAPH DDL
  kg configure-cols  Configure column roles (node/edge/weight/ignore)
  lineage          Build SQL query lineage DAG → HTML graph
  profile          Column statistics: null%, cardinality, min, max, top value
  query            Execute SQL across all loaded sources
  repl             Interactive SQL REPL (.tables, .save, .run meta-commands)
  report           Markdown or HTML data report: schema + samples + FK joins
  serve            Start the Nybble web UI (Browse, SQL, ERD, KG) on :7700
  share            Share ERD or KG as a public URL via GitHub Gist or DataJoins

Options:
  --version  Show version and exit.
  --help     Show help and exit.
```

Every multi-source command (`query`, `erd`, `kg`, `explain`, `report`, `repl`, `serve`, `lineage`) accepts:

- `--source name=path` — pre-load a source (repeatable)
- `--config / -c PATH` — path to `nybble.yaml`; auto-discovered in current directory when present

Run `nybble <command> --help` for full option details.

---

## Web UI (`nybble serve`)

```bash
pip install "datajoins-nybble[serve]"
nybble serve --port 7700
```

The web UI runs locally at [http://localhost:7700](http://localhost:7700) and provides:

- **Catalog** — upload files, connect databases (PostgreSQL, MySQL, SQLite, S3, MongoDB, and more), manage the source registry
- **Browse** — paginated data grid with value facets, column filtering, and cell highlighting
- **SQL Arena** — multi-tab SQL editor with query history, lineage DAG visualisation, and saved queries
- **ERD Studio** — interactive Cytoscape ERD with layout controls, focus mode, and schema diff overlay
- **Knowledge Graph** — Cytoscape force-directed KG with FRD semantic projection, node detail panel, link proposals, and re-indexing
- **Reports** — executable data dictionaries with schema, samples, and auto-generated FK joins
- **Dashboard** — pinned KPI tiles from saved queries

---

## Repository Layout

```
nybble/                          ← repository root
│
├── nybble/                      ← pip package (pip install datajoins-nybble)
│   ├── resolver.py              ← universal source loader, DuckDB session
│   ├── browse.py                ← Rich terminal browser
│   ├── arena.py                 ← SQL REPL, query API, history
│   ├── erd.py                   ← ERD HTML generator
│   ├── infer_fk.py              ← Nybble Sense: FK inference engine (Tiers 1–4)
│   ├── kg.py                    ← Nybble GraphAI: Knowledge Graph (NetworkX)
│   ├── config.py                ← nybble.yaml declarative config loader
│   ├── diff.py                  ← schema diff between two sources
│   ├── profiler.py              ← column-level statistics profiler
│   ├── report.py                ← Markdown/HTML report builder
│   ← lineage.py                 ← SQL lineage DAG builder (sqlglot)
│   ├── share.py                 ← GitHub Gist and DataJoins share backends
│   ├── mcp_server.py            ← MCP server: 72 tools for AI agents
│   ├── serve.py                 ← Flask web UI launcher
│   ├── cli.py                   ← nybble CLI entry point
│   ├── __main__.py              ← python -m nybble <command>
│   └── templates/               ← self-contained Jinja2 HTML templates
│
├── server/                      ← Flask web UI server (included in wheel)
│   ├── app/api_server.py        ← REST API and UI routes
│   ├── app/templates/           ← Browse, SQL Arena, ERD, KG, Report pages
│   ├── app/static/              ← bundled JS (Cytoscape, dagre, cytoscape-dagre)
│   ├── ingestion/               ← upload pipeline, FK inference for serve
│   ├── kg/                      ← KG store (KuZu), indexer, FRD engine
│   └── pipeline/run_pipeline.py ← file ingestion pipeline
│
├── deploy/                      ← Dockerfile, docker-compose.yml, nginx.conf
├── tests/                       ← pytest suite
│
├── nybble.yaml.example          ← fully annotated workspace config reference
├── CHANGELOG.md
├── KNOWN_ISSUES.md
├── LICENSE
├── pyproject.toml
└── README.md
```

---

## Docker Deployment

For teams that prefer a containerised deployment:

```bash
# Default — upload files and connect databases via the browser UI
docker compose -f deploy/docker-compose.yml up

# Pre-load a database at startup
DB_FILE=/absolute/path/to/my.db docker compose -f deploy/docker-compose.yml up

# Bake a database into a custom image at build time
#   Place input.db in the repository root, then:
docker build -f deploy/Dockerfile -t my-org/nybble-analytics .
docker run -p 7700:80 my-org/nybble-analytics
```

> **Note:** A pre-built `datajoins/nybble` image on Docker Hub is not yet available ([KNOWN_ISSUES #10](KNOWN_ISSUES.md)). The instructions above apply only to self-built images from this repository.

---

## Contributing

Contributions are welcome. Please open an issue before a pull request for any significant change.

```bash
# Fork and clone
git clone https://github.com/your-username/nybble.git
cd nybble

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Run the test suite
pytest tests/

# Run linting
pip install ruff
ruff check nybble/ tests/
```

**Commit format:** `type(scope): description`  
Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

Please ensure:
- [ ] All existing tests pass (`pytest tests/`)
- [ ] New behaviour is covered by tests
- [ ] `ruff check` passes with no new warnings
- [ ] Public API additions are reflected in `nybble/__init__.py`

This project follows the [Contributor Covenant](https://www.contributor-covenant.org/) Code of Conduct.

---

## Roadmap

**Shipped:**
- [x] `nybble browse` — full-terminal interactive data grid
- [x] `nybble query` — cross-source DuckDB SQL with `table` / `csv` / `json` output
- [x] `nybble erd` — self-contained interactive ERD HTML with 4-tier FK inference
- [x] `nybble kg` — semantic Knowledge Graph with GraphML, JSON-LD, HTML, SQL/PGQ export
- [x] `nybble kg search` — substring and semantic node search (sentence-transformers)
- [x] `nybble kg pgq` — DuckDB `CREATE PROPERTY GRAPH` DDL export
- [x] `nybble diff` — structural schema diff between two sources
- [x] `nybble report` — Markdown + HTML data report builder
- [x] `nybble serve` — Flask web UI directly from the CLI
- [x] `nybble profile` — column statistics: null%, cardinality, min/max/top
- [x] `nybble info` — schema, row count, source type, and sample rows
- [x] `nybble explain` — DuckDB query execution plan
- [x] `nybble history` — persistent query history log
- [x] `nybble lineage` — SQL lineage DAG rendered as HTML graph
- [x] `nybble repl` — interactive SQL REPL with `.save` / `.run` meta-commands
- [x] `nybble share` — publish ERD/KG as a public URL via GitHub Gist (`--yes` for CI)
- [x] `nybble.yaml` — declarative workspace config: files, databases, HTTP APIs
- [x] MCP server (`nybble-mcp`) — 72 tools for Claude, Cursor, VS Code Copilot
- [x] Python API — `load()`, `query()`, `erd()`, `kg()`, `kg_summary()`, `schema_diff()`
- [x] Web UI — Browse, SQL Arena, ERD Studio, Knowledge Graph, Reports, Dashboard

**Upcoming:**
- [ ] Native Snowflake, BigQuery, and ClickHouse source support
- [ ] `nybble dashboard` — pin saved queries as auto-refreshing KPI tiles
- [ ] DuckDB-WASM in-browser query execution for the web UI
- [ ] `nybble-sense` and `nybble-graphai` as independently versioned sub-packages

---

## DataJoins Ecosystem

Nybble is part of the [DataJoins](https://datajoins.com) open-source portfolio.

---

## License

Apache License 2.0 — Copyright © 2026 [DataJoins](https://datajoins.com)

See [LICENSE](LICENSE) for the full text.
