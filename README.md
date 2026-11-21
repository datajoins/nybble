<div align="center">

# Nybble 🍱

### *You don't need a Googol of infrastructure just to see your data.*

[![CI](https://img.shields.io/github/actions/workflow/status/datajoins/nybble/ci.yml?branch=main&label=CI&logo=github)](https://github.com/datajoins/nybble/actions/workflows/ci.yml)
[![PyPI Version](https://img.shields.io/pypi/v/datajoins-nybble)](https://pypi.org/project/datajoins-nybble/)
[![Python Versions](https://img.shields.io/pypi/pyversions/datajoins-nybble)](https://pypi.org/project/datajoins-nybble/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/datajoins/nybble?style=social)](https://github.com/datajoins/nybble)

**[datajoins.com](https://datajoins.com) · [PyPI](https://pypi.org/project/datajoins-nybble/) · [Changelog](CHANGELOG.md) · [Documentation](website/documentation.html) · [Docs](docs/) · [Issues](https://github.com/datajoins/nybble/issues)**

</div>

---

## Why Nybble?

In computer science, a **nybble** is exactly 4 bits - half a byte, one of the smallest
functional units of digital storage.

A **googol** is 10¹⁰⁰ - a number larger than the estimated count of elementary particles
in the entire observable universe.

Every developer working with data eventually ends up wrestling with a googol of
complexity: heavy enterprise BI platforms, sprawling cloud data warehouses, multi-service
ETL pipelines, and schema documentation that's always out of date. Tools that charge
tens of thousands of dollars a year for capabilities that should take ten seconds to
invoke.

**Nybble exists because you don't need any of that just to understand your own data.**

You need something the exact opposite size - something small enough to install in one
command, fast enough to produce results before you finish your coffee, and honest enough
to show you exactly what's in your database without a sales call, a YAML sprawl, or a
Node.js build pipeline.

> *A nybble is half a byte. It is also exactly enough.*

---

## What Nybble Does

Nybble is an ultra-lightweight Python engine that takes a single, effortless bite out of
any data source and gives you back four things that normally require four separate tools:

| Capability | What you get | Replaces |
|---|---|---|
| **Browse** | Full-terminal paginated data grid, live filter, column sort | TablePlus, DBeaver (desktop) |
| **SQL Arena** | Cross-source SQL via DuckDB - join a CSV to a Parquet to a live DB | Redash, DataGrip |
| **ERD** | Auto-generated interactive Entity-Relationship Diagram with FK inference | DbDiagram.io (paid), SchemaSpy |
| **Knowledge Graph** | Semantic entity graph with GraphML, JSON-LD, and HTML export | DataHub, Neo4j (enterprise) |

All four capabilities work from the same `pip install`. No Docker required. No server to
run. No Node.js. No API keys.

---

## Installation

```bash
pip install datajoins-nybble
```

**With optional connectors and extras:**

```bash
# PostgreSQL
pip install "datajoins-nybble[postgres]"

# MySQL / MariaDB
pip install "datajoins-nybble[mysql]"

# MCP server (AI agent integration - Claude, Cursor, VS Code)
pip install "datajoins-nybble[mcp]"

# Semantic node search (sentence-transformers)
pip install "datajoins-nybble[semantic]"

# Everything (postgres + mysql + mcp + rdflib)
pip install "datajoins-nybble[all]"
```

**Requirements:** Python ≥ 3.9

---

## 30-Second Smoke Test

Copy and paste this block — it uses the demo CSV files in `.nybble-test-fixtures/`.
No database required, no configuration needed.

```bash
# 1. Install
pip install datajoins-nybble

# 2. Use the demo fixtures (copy from .nybble-test-fixtures/ in the repo)
#    Or create them: customers.csv, orders.csv, products.csv - see documentation.html

# 3. Inspect a source
nybble info customers.csv

# 4. Profile column statistics
nybble profile customers.csv

# 5. Browse a file in the terminal (arrow keys, / to filter, q to quit)
nybble browse customers.csv

# 6. Run a cross-source SQL query
nybble query "SELECT c.name, COUNT(o.order_id) AS orders, SUM(o.amount) AS revenue
              FROM customers c JOIN orders o ON c.customer_id = o.customer_id
              GROUP BY c.name ORDER BY revenue DESC" \
  --source customers.csv \
  --source orders.csv

# 7. Diff two schemas
nybble diff customers.csv orders.csv

# 8. Generate a self-contained ERD HTML (opens in browser automatically)
nybble erd --source customers.csv --source orders.csv --source products.csv --no-browser

# 9. Build a Knowledge Graph
nybble kg --source customers.csv --source orders.csv --source products.csv --no-browser

# 10. Search the Knowledge Graph
nybble kg search "Alice" --source customers.csv
nybble kg search "shipped" --source orders.csv

# 11. Generate DuckDB SQL/PGQ DDL
nybble kg pgq --source customers.csv --source orders.csv --source products.csv

# 12. Print KG summary JSON
nybble kg --summary-only --source customers.csv --source orders.csv --source products.csv

# 13. Generate a Markdown data report
nybble report --source customers.csv --output customers_report.md

# 14. View query history
nybble history --limit 5
```

Every command exits cleanly. No side effects outside the `.nybble/` directory.

---

## Workspace Config (`nybble.yaml`)

Instead of repeating `--source` flags on every command, declare your sources once in a
`nybble.yaml` file. Any `nybble` command run in the same directory will load them
automatically.

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
# All sources from nybble.yaml are loaded automatically - no --source flags needed
nybble erd
nybble kg
nybble query "SELECT * FROM customers LIMIT 10"
nybble repl

# Or point to an explicit config file path
nybble erd --config ./configs/staging.yaml
```

- **`${ENV_VAR}`** tokens in the config are substituted from your shell environment at
  load time. Nybble raises a clear error if a referenced variable is not set.
- The config file is discovered by searching for `nybble.yaml`, `nybble.yml`,
  `.nybble.yaml`, or `.nybble.yml` in the current working directory.
- See [`nybble.yaml.example`](nybble.yaml.example) for a fully annotated reference.

---

## MCP Server - AI Agent Integration

Nybble ships a full [Model Context Protocol](https://modelcontextprotocol.io/) server
that exposes the complete Nybble API to any MCP-compatible AI agent - Claude Desktop,
Cursor, VS Code Copilot, Raycast, and others.

```bash
pip install "datajoins-nybble[mcp]"
```

**Register in your AI client config** (e.g. `~/.config/claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "nybble": { "command": "nybble-mcp" }
  }
}
```

**Or start manually:**

```bash
nybble-mcp
```

**Available MCP tools:**

| Tool | Description |
|---|---|
| `nybble_load` | Load any source: file path, DB URI, S3/GCS URL, HTTP API |
| `nybble_list_tables` | List all loaded sources |
| `nybble_schema` | Column names and types for a loaded table |
| `nybble_query` | Execute DuckDB SQL across all sources |
| `nybble_explain` | Show DuckDB query execution plan |
| `nybble_profile` | Column statistics (null%, cardinality, min/max/top value) |
| `nybble_infer_schema` | Infer FK relationships - deterministic, zero LLM calls |
| `nybble_kg_summary` | Knowledge-graph summary (node/edge counts, entity types) |
| `nybble_diff` | Schema diff between two sources |
| `nybble_erd` | Generate interactive ERD HTML, return path |
| `nybble_kg` | Build and export Knowledge Graph, return paths |
| `nybble_search_history` | Search the persistent query history log |

**How it works - no LLM calls on the Nybble side:**

```
AI agent (LLM decides what to call)
    └─ nybble_load("sales.csv")     → resolver._load_file()   ← DuckDB CREATE VIEW
    └─ nybble_query("SELECT ...")   → arena.query()           ← duckdb.execute()
    └─ nybble_infer_schema()        → infer_fk.py Tier 1-4   ← column matching + stats
    └─ nybble_kg_summary()          → kg._build_graph()       ← NetworkX + PageRank
```

The MCP extra adds ~6 transitive packages (`mcp`, `anyio`, `httpx`, `starlette`,
`uvicorn`, `pydantic`). It is always optional - `pip install datajoins-nybble` stays lean.

---

## Quick Start

### Browse any data source in the terminal

```bash
nybble browse ./sales_data.csv
nybble browse ./warehouse.parquet
nybble browse "postgresql://user:pass@localhost:5432/mydb"
```

### Run SQL across multiple sources at once

```bash
nybble query "SELECT o.id, c.name, o.total
              FROM orders o JOIN customers c ON o.customer_id = c.id
              ORDER BY o.total DESC LIMIT 20" \
  --source orders=orders.csv \
  --source customers=customers.parquet
```

### Generate an ERD from a live database

```bash
# Produces a self-contained HTML file and opens it in your browser
nybble erd --source "postgresql://user:pass@localhost/mydb"
```

### Build a Knowledge Graph

```bash
# Entity graph from two related files, exported as GraphML + JSON-LD + HTML
nybble kg \
  --source customers=customers.csv \
  --source orders=orders.parquet
```

### Profile column statistics

```bash
nybble profile orders.csv
```

### Compare two schema versions

```bash
nybble diff schema_v1.csv schema_v2.csv
nybble diff "sqlite:///old.db" "sqlite:///new.db"
```

### Generate a Markdown data report

```bash
nybble report --source orders.csv --output report.md
nybble report --source mydb.sqlite --output docs/schema.html
```

### Python API

```python
from nybble import load, query, erd, kg, kg_summary, profile_relation, schema_diff

# Load any source - file, JSON, database URI, or HTTP URL
customers = load("customers.csv")
orders    = load("orders.parquet")

# Query across both sources using DuckDB SQL
result = query("""
    SELECT c.name, COUNT(o.id) AS order_count, SUM(o.total) AS revenue
    FROM customers c
    JOIN orders o ON c.id = o.customer_id
    GROUP BY c.name
    ORDER BY revenue DESC
""")
print(result.to_polars())

# Profile a table
for col in profile_relation("customers"):
    print(col.name, col.null_pct, col.distinct)

# Compare two schemas
diff = schema_diff("v1.csv", "v2.csv")
print(diff.added, diff.removed, diff.changed)

# Generate a self-contained ERD HTML file
erd(output=".nybble/schema.html")

# Build a semantic Knowledge Graph
result = kg()
print(kg_summary())
# {'node_count': 1840, 'edge_count': 4122, 'avg_degree': 4.48, ...}

# Load from a nybble.yaml config file
from nybble import load_config, apply_config
cfg = load_config("nybble.yaml")
apply_config(cfg)   # all sources in config are now loaded
```

---

## Features

### Universal source loading

Nybble's resolver accepts anything you throw at it without configuration:

| Source type | Examples |
|---|---|
| CSV / TSV | `customers.csv`, `data.tsv` |
| Parquet | `events.parquet` |
| JSON / JSONL | `records.json`, `stream.jsonl` |
| Excel | `report.xlsx`, `legacy.xls` |
| Avro | `kafka-export.avro` |
| SQLite | `local.db`, `sqlite:///path/to/db` |
| PostgreSQL | `postgresql://user:pass@host/db` |
| MySQL / MariaDB | `mysql://user:pass@host/db` |
| S3 / GCS / R2 | `s3://bucket/data.parquet`, `gs://bucket/file.csv` |
| HTTP URL | `https://api.example.com/data.json` |
| Inline Python | `load([{"id": 1, "name": "Alice"}, ...])` |

All sources register as virtual DuckDB tables in a shared, thread-safe, in-process
session - no intermediate files, no network calls, no copies.

### ERD generation with automatic FK inference - *Nybble Sense*

When a schema has no declared foreign-key constraints (common in CSV exports, ORMs that
enforce at the application layer, and cloud-sync snapshots), Nybble's four-tier inference
engine fills the gap:

1. **Tier 1** - Explicit constraints from database introspection (short-circuits here when found)
2. **Tier 2** - Column name suffix matching (`_id`, `_fk`, `_key`, `_ref`, `_uuid`, `_code`)
3. **Tier 3** - Column name equality across tables
4. **Tier 4** - Statistical uniqueness sampling (≥95% unique source → candidate FK)

Every inferred FK carries a confidence score. The ERD output is a single self-contained
HTML file - no CDN dependencies, no server required to view it. Deep-linkable via URL
parameters (`?highlight=<table>`, `?embed=1` for iframe embedding).

### Knowledge Graph - *Nybble GraphAI*

Nybble builds a semantic `MultiDiGraph` (NetworkX) from any set of loaded relations.
Every row becomes a node. Every FK relationship becomes a directed edge. Co-occurring
values across unlinked tables become inferred cross-source edges.

**Export formats:**
- **GraphML** - importable by Gephi, yEd, NetworkX
- **JSON-LD** - schema.org vocabulary, consumable by AI/ML pipelines and graph databases
- **Self-contained HTML** - interactive force-directed visualisation, no server required
- **SQL/PGQ DDL** - `nybble kg --export pgq` emits DuckDB `CREATE PROPERTY GRAPH` DDL

Node-level graph metrics are computed for every entity: PageRank, degree centrality,
betweenness centrality, and community membership (Louvain algorithm).

### SQL Arena

Every source loaded by Nybble becomes a virtual DuckDB table, joinable with any other
source in a single SQL statement. The `query()` function returns a `QueryResult` with
conversion methods to Polars, Pandas, PyArrow, and plain Python dicts. Queries are
persisted to `.nybble/queries.json` with `save_query()`; query history is persisted to
`.nybble/history.jsonl`.

The interactive REPL (`nybble repl`) supports meta-commands: `.tables` to list sources,
`.save <name> <sql>` to persist a query, `.run <name>` to replay it.

### Terminal browser

`nybble browse` renders any source as a full-terminal data grid powered by Rich:
keyboard navigation (`↑↓`, `PgUp/PgDn`, `g`/`G`), a live filter bar (`/`), column
sorting, and a status bar showing source metadata, row count, active page, and current
filters.

---

## Repository Layout

```
nybble/                          ← repository root
│
├── nybble/                      ← pip package  (pip install datajoins-nybble)
│   ├── resolver.py              ← universal source loader, DuckDB session
│   ├── browse.py                ← Rich terminal browser
│   ├── arena.py                 ← SQL REPL, query API, history
│   ├── erd.py                   ← ERD HTML generator
│   ├── infer_fk.py              ← Nybble Sense: FK inference engine
│   ├── kg.py                    ← Nybble GraphAI: Knowledge Graph
│   ├── config.py                ← nybble.yaml declarative config loader
│   ├── diff.py                  ← schema diff between two sources
│   ├── profiler.py              ← column-level statistics profiler
│   ├── report.py                ← Markdown report builder
│   ├── highlight_store.py       ← persistent cell-highlight rules
│   ├── mcp_server.py            ← MCP server (AI agent integration)
│   ├── cli.py                   ← `nybble` CLI entry point
│   └── templates/               ← self-contained Jinja2 HTML templates
│
├── server/                      ← Flask/nginx server (Docker deployable)
│   ├── app/                     ← Flask application + HTML templates
│   ├── ingestion/               ← format extraction + import pipeline
│   ├── pipeline/                ← orchestrator + ERD renderer
│   └── kg/                      ← server-side Knowledge Graph subsystem
│
├── deploy/                      ← Dockerfile, docker-compose.yml, nginx.conf
├── docs/                        ← ROADMAP.md, COMPETITION.md, research/
├── tests/                       ← pytest suite
│
├── nybble.yaml.example          ← fully annotated workspace config reference
├── CHANGELOG.md
├── LICENSE
├── pyproject.toml
└── README.md
```

---

## CLI Reference

```
nybble [OPTIONS] COMMAND [ARGS]...

Commands:
  browse   Launch interactive terminal browser for any data source
  diff     Compare schemas of two sources (added / removed / type-changed columns)
  erd      Generate a self-contained interactive ERD HTML file
  explain  Show DuckDB query execution plan for a SQL statement
  history  Show recent query history, optionally filtered by search term
  info     Print schema, row count, source type, and sample rows for a source
  kg       Build and export a semantic Knowledge Graph
  lineage  Build an SQL query lineage DAG and render it as an HTML graph
  profile  Print column statistics (null%, cardinality, min, max, top value)
  query    Execute SQL across all loaded sources
  repl     Launch the interactive SQL REPL
  report   Generate a Markdown or HTML data report: schema, samples, FK joins
  serve    Start the Nybble web UI (Flask) without Docker
  share    Share an ERD or Knowledge Graph as a public URL via GitHub Gist

Options:
  --version  Show version and exit.
  --help     Show help and exit.
```

Every multi-source command (`query`, `erd`, `kg`, `explain`, `report`, `repl`, `serve`, `lineage`)
accepts:

- `--source name=path` - pre-load a source (may be repeated)
- `--config / -c PATH` - path to a `nybble.yaml` workspace config file; if omitted,
  `nybble.yaml` in the current directory is loaded automatically when present

Run `nybble <command> --help` for full option details.

---

## Self-Hosting with Docker

Nybble ships a full Flask/nginx server for browser-based access:

```bash
# Mode C - upload files and connect databases via the browser UI (default)
docker compose -f deploy/docker-compose.yml up

# Mode B - pre-load a database at startup
DB_FILE=/absolute/path/to/my.db docker compose -f deploy/docker-compose.yml up

# Mode A - bake a database into a custom image at build time
#   Place input.db in the repository root, then:
docker build -f deploy/Dockerfile -t my-org/nybble-analytics .
docker run -p 7700:80 my-org/nybble-analytics
```

Open [http://localhost:7700](http://localhost:7700) for the full browser UI.

Or run without Docker after `pip install "datajoins-nybble[dev]"`:

```bash
nybble serve --port 7700 --source mydb.sqlite
```

---

## Contributing

Contributions are welcome. Before opening a pull request for a significant change,
please open an issue first so the approach can be discussed.

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

**Commit message format:** `type(scope): description`  
Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

Please ensure:
- [ ] All existing tests pass (`pytest tests/`)
- [ ] New behaviour is covered by tests
- [ ] `ruff check` passes with no new warnings
- [ ] Public API changes are reflected in `nybble/__init__.py`

This project follows the [Contributor Covenant](https://www.contributor-covenant.org/)
Code of Conduct.

---

## Roadmap

**Shipped (v1.0.0):**
- [x] `nybble browse` - full-terminal interactive data grid
- [x] `nybble query` - cross-source DuckDB SQL with `table` / `csv` / `json` output
- [x] `nybble erd` - self-contained interactive ERD HTML with 4-tier FK inference
- [x] `nybble kg` - semantic Knowledge Graph with GraphML, JSON-LD, HTML, SQL/PGQ export
- [x] `nybble kg search` - substring and semantic node search (sentence-transformers)
- [x] `nybble kg pgq` - DuckDB `CREATE PROPERTY GRAPH` DDL export
- [x] `nybble diff` - structural schema diff between two sources
- [x] `nybble report` - Markdown + HTML data report builder
- [x] `nybble serve` - start the Flask UI directly from the CLI
- [x] `nybble profile` - column statistics panel (null%, cardinality, min/max/top)
- [x] `nybble info` - schema, row count, source type, and sample rows
- [x] `nybble explain` - DuckDB query execution plan
- [x] `nybble history` - persistent query history log
- [x] `nybble lineage` - SQL lineage DAG rendered as HTML graph
- [x] `nybble repl` - interactive SQL REPL with `.save` / `.run` meta-commands
- [x] `nybble share` - publish ERD/KG as a public URL via GitHub Gist
- [x] `nybble.yaml` declarative workspace config (files, databases, HTTP APIs)
- [x] MCP server (`nybble-mcp`) - AI agent integration (Claude, Cursor, VS Code)
- [x] Python API - `load()`, `query()`, `erd()`, `kg()`, `kg_summary()`, `schema_diff()`

**Upcoming:**
- [ ] Native Snowflake, BigQuery, and ClickHouse source support
- [ ] `nybble dashboard` - pin saved queries as auto-refreshing KPI tiles
- [ ] DuckDB-WASM in-browser query execution for the web UI
- [ ] `nybble-sense` and `nybble-graphai` as independently versioned sub-packages

---

## DataJoins Ecosystem

Nybble is part of the [DataJoins](https://datajoins.com) open-source portfolio.
Visit [datajoins.com](https://datajoins.com) for the full ecosystem.

---

## License

Apache License 2.0 - Copyright © 2025 [DataJoinsNow](https://datajoins.com)

See [LICENSE](LICENSE) for the full text.
