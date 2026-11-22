"""
mcp_server.py - Nybble MCP Server

Exposes the Nybble public API as Model Context Protocol tools so that
AI agents (Claude Desktop, Cursor, VS Code, Raycast, etc.) can load data,
run queries, inspect schemas, infer relationships, and build knowledge graphs
without any manual Python code.

Usage
-----
Install the optional MCP extra::

    pip install "datajoins-nybble[mcp]"

Then register in your AI client config (e.g. ``~/.config/claude/claude_desktop_config.json``)::

    {
      "mcpServers": {
        "nybble": { "command": "nybble-mcp" }
      }
    }

Or start manually::

    nybble-mcp

All Nybble computation is local - zero LLM calls, zero network calls beyond
your own data sources.

Tools
-----
Data ingestion & query
  nybble_load          - Load any source: file path, DB URI, S3/GCS URL, HTTP API
  nybble_list_tables   - List all loaded sources
  nybble_schema        - Column names + types for a loaded table
  nybble_query         - Execute DuckDB SQL across all sources
  nybble_explain       - Show DuckDB query execution plan
  nybble_profile       - Column statistics (null%, cardinality, min/max/top value)
  nybble_search_history- Search query history
  nybble_infer_schema  - Infer FK relationships (deterministic, zero LLM)
  nybble_diff          - Schema diff between two sources

ERD & Knowledge Graph
  nybble_erd           - Generate interactive ERD HTML and return its path
  nybble_kg            - Build and export Knowledge Graph; return paths + serve hint
  nybble_kg_summary    - Lightweight summary: node/edge counts, entity types
  nybble_kg_graph      - Full graph as node/edge lists (degree-sorted, up to 500 nodes)
  nybble_kg_search     - Free-text node search (semantic or substring)
  nybble_kg_node       - All connections + metadata for a single node
  nybble_kg_frd_scores - Rank ALL nodes by FRD (Frequency-Relative Degree) score
  nybble_kg_frd_expand - BFS expansion from a seed node, ranked by FRD surprise
  nybble_kg_validate   - Structural consistency checks (isolated, loops, components)
  nybble_kg_export_pgq - Generate DuckDB CREATE PROPERTY GRAPH DDL

Serve UI integration (require nybble serve to be running)
  nybble_serve_start         - Start nybble serve in the background, return the UI URL
  nybble_serve_status        - Check whether nybble serve is running and return the URL
  nybble_sources_list        - List all sources loaded in the serve UI
  nybble_source_tables       - Tables + row counts for a specific source
  nybble_source_connect      - Register a new source (connstring / URL / form / restapi / …)
  nybble_source_process      - Trigger background introspection pipeline for a source
  nybble_source_status       - Poll processing status + log for a source
  nybble_source_delete       - Delete a source and all its files from the registry
  nybble_source_rename       - Update the display name of a source
  nybble_browse_rows         - Browse rows with pagination, sorting, and column filtering
  nybble_run_query           - Execute SQL against a source in the serve UI
  nybble_col_profile         - Per-column stats for a table in the serve UI
  nybble_col_profile_detail  - Single-column detailed stats (null/distinct/min/max/top-N)
  nybble_aggregate           - GROUP BY aggregation on a table column
  nybble_facets              - Faceted value counts for one or more columns
  nybble_autocomplete        - Table/column autocomplete for SQL editing
  nybble_freshness           - Data freshness metrics for a source
  nybble_triggers            - Database triggers defined in a SQLite source
  nybble_ui_search           - Search table/column names across all schemas
  nybble_write_row           - Insert or update a row (ALLOW_WRITES required)
  nybble_delete_row          - Delete a row (ALLOW_WRITES required)
  nybble_erd_detail          - ERD detail (columns, FK links) for one table
  nybble_history             - Query execution history for a source
  nybble_audit               - Write audit log for a source
  nybble_saved_queries       - List / create / delete saved queries for a source
  nybble_charts              - List / create / delete saved charts for a source
  nybble_metrics             - List / create / update / delete data quality metrics
  nybble_joins               - List / create / update / delete cross-source virtual joins
  nybble_reports             - List / create / get (with executed blocks) / delete reports
  nybble_dashboard           - Tiles list (with SQL) / run tile / snapshots for a source

KG UI tools — full /api/kg/* coverage (require nybble serve to be running)
  nybble_kg_ui_sources       - List sources available for KG (processed, with schema/DB)
  nybble_kg_ui_search        - Search KG index (schema / data / hybrid mode)
  nybble_kg_ui_graph         - Build KG graph (schema/data/auto, with column roles)
  nybble_kg_ui_entity        - Resolve an entity node and return its neighbourhood graph
  nybble_kg_ui_columns       - Per-column metadata for column-role configuration
  nybble_kg_configure_cols   - Set col roles + relationships and rebuild the graph (UI-equivalent)
  nybble_kg_ui_expand        - FRD-expand KG from a seed node (UI /api/kg/expand)
  nybble_kg_ui_frd_scores    - FRD scores for all nodes of a source
  nybble_kg_ui_validate      - Validate graph structure for one or more sources
  nybble_kg_ui_propose_link  - Propose a cross-source link for human review
  nybble_kg_ui_temporal      - Graph filtered to a temporal window
  nybble_kg_ui_versions      - List stored KG snapshots for a source
  nybble_kg_ui_diff          - Structural diff between two KG snapshots
  nybble_kg_ui_reindex       - Trigger background re-index (one or all sources)
  nybble_kg_ui_reset         - Wipe KG index state for one or all sources
  nybble_kg_ui_status        - KG index health + capabilities
  nybble_kg_ui_capabilities  - Search capabilities (dense/sparse vectors, embed model)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp import types as mcp_types
except ImportError as _exc:
    raise ImportError(
        "The MCP extra is required to run the Nybble MCP server.\n"
        "Install it with:  pip install 'datajoins-nybble[mcp]'"
    ) from _exc

import nybble
from nybble.resolver import _REGISTERED

# ── Server instance ───────────────────────────────────────────────────────────
app = Server("nybble")


# ── Tool definitions ──────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[mcp_types.Tool]:
    return [
        mcp_types.Tool(
            name="nybble_load",
            description=(
                "Load any data source into the shared DuckDB session. "
                "Accepts: local file paths (.csv .parquet .json .xlsx .avro .dat), "
                "database URIs (sqlite:// postgresql:// mysql:// duckdb:// clickhouse://), "
                "object-storage URIs (s3:// gs:// r2://), or HTTP/HTTPS JSON APIs. "
                "Returns the table name and row count."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "File path, URI, or URL to load"},
                    "name": {"type": "string", "description": "Optional custom table name"},
                    "headers": {
                        "type": "object",
                        "description": "Optional HTTP headers (e.g. Authorization) for URL sources",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["source"],
            },
        ),
        mcp_types.Tool(
            name="nybble_list_tables",
            description="List all currently loaded data sources as table names.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        mcp_types.Tool(
            name="nybble_schema",
            description="Get column names and types for a loaded table.",
            inputSchema={
                "type": "object",
                "properties": {
                    "table": {"type": "string", "description": "Table name (from nybble_list_tables)"},
                },
                "required": ["table"],
            },
        ),
        mcp_types.Tool(
            name="nybble_query",
            description=(
                "Execute DuckDB SQL across all loaded sources. "
                "Supports cross-source JOINs, CTEs, window functions, and all DuckDB extensions. "
                "Returns rows as a list of dicts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SQL query to execute"},
                    "limit": {
                        "type": "integer",
                        "description": "Maximum rows to return (default 500)",
                        "default": 500,
                    },
                },
                "required": ["sql"],
            },
        ),
        mcp_types.Tool(
            name="nybble_explain",
            description="Show DuckDB's query execution plan for a SQL statement.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {"type": "string"},
                    "analyze": {
                        "type": "boolean",
                        "description": "If true, runs EXPLAIN ANALYZE (executes query, shows actual stats)",
                        "default": False,
                    },
                },
                "required": ["sql"],
            },
        ),
        mcp_types.Tool(
            name="nybble_profile",
            description=(
                "Return column statistics for a loaded table: "
                "null%, distinct count, cardinality%, min, max, most-frequent value."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                },
                "required": ["table"],
            },
        ),
        mcp_types.Tool(
            name="nybble_search_history",
            description="Search the persistent query history log.",
            inputSchema={
                "type": "object",
                "properties": {
                    "term": {"type": "string", "description": "Substring to search for (optional)"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": [],
            },
        ),
        mcp_types.Tool(
            name="nybble_infer_schema",
            description=(
                "Infer foreign-key relationships across all loaded sources using "
                "4-tier heuristics (explicit constraints → suffix matching → name equality → statistics). "
                "Deterministic - no LLM calls."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        mcp_types.Tool(
            name="nybble_kg_summary",
            description=(
                "Return a lightweight knowledge-graph summary: "
                "node count, edge count, entity type distribution, inferred FK count. "
                "Does not write any files."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        mcp_types.Tool(
            name="nybble_diff",
            description=(
                "Compare schemas of two sources. "
                "Returns added columns, removed columns, type-changed columns, and unchanged columns."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_a": {"type": "string", "description": "First source (table name or path)"},
                    "source_b": {"type": "string", "description": "Second source (table name or path)"},
                },
                "required": ["source_a", "source_b"],
            },
        ),
        mcp_types.Tool(
            name="nybble_erd",
            description=(
                "Generate a self-contained interactive ERD HTML file from all loaded sources. "
                "Returns the path to the generated file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "output": {"type": "string", "description": "Output file path (optional)"},
                },
                "required": [],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg",
            description=(
                "Build and export a Knowledge Graph from all loaded sources. "
                "Returns paths to the GraphML, JSON-LD, and HTML (static export) files. "
                "NOTE: The HTML returned here is a static self-contained export. "
                "For the full interactive Knowledge Graph UI with Cytoscape layout, "
                "FRD Explore, Path Filter, Schema/Data modes, and live search, "
                "the user should run `nybble serve` and open http://localhost:7700/kg "
                "in a browser. The static HTML is useful for sharing or offline viewing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "output_dir": {"type": "string", "description": "Output directory (optional)"},
                },
                "required": [],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_search",
            description=(
                "Search Knowledge Graph nodes by free-text query. "
                "Uses local sentence-transformers semantic similarity when installed "
                "(pip install 'datajoins-nybble[semantic]'); falls back to substring matching automatically. "
                "The structural FK inference is deterministic and never affected by this tool - "
                "embeddings are additive for search only. "
                "Returns a ranked list of matching nodes with score and match_mode."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search string, e.g. 'customer from Germany'",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_export_pgq",
            description=(
                "Generate a DuckDB CREATE PROPERTY GRAPH DDL string from automatically "
                "inferred schema relationships across all loaded sources. "
                "The output is valid DuckDB SQL/PGQ DDL (DuckDB 0.10+) that can be piped "
                "directly into DuckDB to enable Cypher-like graph traversal over relational data. "
                "All computation is local - no LLM calls, no network calls. "
                "Returns the DDL string. If no sources are loaded, raises an error."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graph_name": {
                        "type": "string",
                        "description": "Name for the CREATE PROPERTY GRAPH statement (default: 'nybble_graph')",
                        "default": "nybble_graph",
                    },
                },
                "required": [],
            },
        ),
        # ── New KG analytics tools ─────────────────────────────────────────────
        mcp_types.Tool(
            name="nybble_kg_graph",
            description=(
                "Return the full Knowledge Graph as node and edge lists. "
                "Nodes are degree-sorted and entity-spread (up to display_limit, default 500). "
                "Each node has: node_id, label, entity_type, degree. "
                "Each edge has: source, target, relation, confidence. "
                "Also returns total node_count and edge_count before the display limit is applied. "
                "Use this to programmatically analyse graph structure, find hubs, or render the graph."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "display_limit": {
                        "type": "integer",
                        "description": "Max nodes to return (default 500)",
                        "default": 500,
                    },
                },
                "required": [],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_node",
            description=(
                "Return full detail for a single Knowledge Graph node: "
                "label, entity_type, degree, empirical count, all node attributes, "
                "and the complete list of incoming/outgoing connections with relation labels. "
                "Partial substring matching is used if the exact node_id is not found. "
                "This is the programmatic equivalent of clicking a node in the UI detail drawer."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "Node ID (or unique substring) to look up",
                    },
                },
                "required": ["node_id"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_frd_scores",
            description=(
                "Compute Frequency-Relative Degree (FRD) scores for all KG nodes and return "
                "the top-k ranked results. FRD(v) = degree(v) × N / count(v). "
                "High FRD nodes appear rarely in the data but are highly connected — they are "
                "structurally surprising hubs that often represent important cross-cutting concepts. "
                "This is the same scoring the UI uses to colour nodes in FRD Explore mode. "
                "Returns: node_id, label, entity_type, frd_score, degree, count, N."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "top_k": {
                        "type": "integer",
                        "description": "Number of top nodes to return (default 50; pass 0 for all)",
                        "default": 50,
                    },
                },
                "required": [],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_frd_expand",
            description=(
                "BFS-expand the Knowledge Graph from a seed node, ranking each hop's neighbours "
                "by FRD (Frequency-Relative Degree) score so the most surprising connections come first. "
                "This mirrors the UI's FRD Explore mode: click a node to see its highest-surprise neighbours. "
                "depth=1 returns direct neighbours; depth=2 adds their neighbours, etc. (max 4). "
                "Returns: seed, depth, N, frd_max, nodes (with frd_score, is_seed), edges (traversal only)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "Seed node ID (or unique substring) to start exploration from",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "BFS depth (1–4, default 1)",
                        "default": 1,
                    },
                    "max_nodes": {
                        "type": "integer",
                        "description": "Safety cap on returned nodes (default 200)",
                        "default": 200,
                    },
                },
                "required": ["node_id"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_validate",
            description=(
                "Run structural consistency checks on the Knowledge Graph. "
                "Checks: isolated nodes (degree 0), self-loops, missing labels, "
                "and weakly connected components (graph fragmentation). "
                "Returns: passed (bool), node_count, edge_count, "
                "issues (list of {severity, check, description, affected_count, sample}). "
                "severity is 'warning' (structural problem) or 'info' (informational)."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        # ── Serve UI integration tools ─────────────────────────────────────────
        mcp_types.Tool(
            name="nybble_serve_status",
            description=(
                "Check whether 'nybble serve' is currently running and return its URL. "
                "Returns: running (bool), url (http://localhost:PORT/kg or null), port. "
                "Use this before nybble_serve_start to avoid launching a second instance. "
                "When running=true the user can open `url` in a browser for the full "
                "interactive Knowledge Graph UI with Cytoscape, FRD Explore, Path Filter, etc."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {
                        "type": "integer",
                        "description": "Port to check (default 7700)",
                        "default": 7700,
                    },
                },
                "required": [],
            },
        ),
        mcp_types.Tool(
            name="nybble_serve_start",
            description=(
                "Start 'nybble serve' as a background process and return the UI URL. "
                "If the server is already running on the target port, returns the existing URL "
                "without launching a second process. "
                "The full interactive UI (Cytoscape KG, FRD Explore, layout modes, live search, "
                "ERD, Browse, Query) will be available at the returned url within ~2 seconds. "
                "Returns: url, port, already_running (bool), pid (if newly started)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {
                        "type": "integer",
                        "description": "Port to bind (default 7700)",
                        "default": 7700,
                    },
                    "host": {
                        "type": "string",
                        "description": "Bind address (default '127.0.0.1')",
                        "default": "127.0.0.1",
                    },
                },
                "required": [],
            },
        ),
        # ── Sources & Browse & Query UI tools ─────────────────────────────────
        mcp_types.Tool(
            name="nybble_sources_list",
            description=(
                "List all data sources currently loaded in the nybble serve UI. "
                "Returns each source's key, name, file path/URI, type, and table count. "
                "Requires nybble serve to be running (call nybble_serve_start if needed)."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        mcp_types.Tool(
            name="nybble_source_tables",
            description=(
                "List all tables in a specific data source loaded in the nybble serve UI, "
                "with row counts for each table. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {
                        "type": "string",
                        "description": "Source key from nybble_sources_list",
                    },
                },
                "required": ["source_key"],
            },
        ),
        mcp_types.Tool(
            name="nybble_browse_rows",
            description=(
                "Browse rows from a table in the nybble serve UI with pagination, sorting, and filtering. "
                "Equivalent to the Browse tab in the UI. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {
                        "type": "string",
                        "description": "Source key from nybble_sources_list",
                    },
                    "table": {
                        "type": "string",
                        "description": "Table name from nybble_source_tables",
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number (1-based, default 1)",
                        "default": 1,
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "Rows per page (default 50, max 200)",
                        "default": 50,
                    },
                    "sort": {
                        "type": "string",
                        "description": "Column name to sort by (prefix with - for descending, e.g. '-price')",
                    },
                    "filters": {
                        "type": "object",
                        "description": "Column filters as {col__op: value}, e.g. {'price__gte': '10'}",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["source_key", "table"],
            },
        ),
        mcp_types.Tool(
            name="nybble_run_query",
            description=(
                "Execute SQL against a specific data source in the nybble serve UI. "
                "Returns columns, rows, and row count. "
                "Equivalent to the Query tab in the UI. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {
                        "type": "string",
                        "description": "Source key from nybble_sources_list",
                    },
                    "sql": {
                        "type": "string",
                        "description": "SQL query to execute",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum rows to return (default 1000)",
                        "default": 1000,
                    },
                },
                "required": ["source_key", "sql"],
            },
        ),
        mcp_types.Tool(
            name="nybble_col_profile",
            description=(
                "Get per-column statistics for a table in the nybble serve UI: "
                "null%, distinct count, min, max, top value. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {
                        "type": "string",
                        "description": "Source key from nybble_sources_list",
                    },
                    "table": {
                        "type": "string",
                        "description": "Table name from nybble_source_tables",
                    },
                },
                "required": ["source_key", "table"],
            },
        ),
        mcp_types.Tool(
            name="nybble_erd_detail",
            description=(
                "Get ERD detail for a specific table in the nybble serve UI: "
                "columns, inferred foreign keys to/from other tables, and primary key info. "
                "Equivalent to clicking a table in the ERD view. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {
                        "type": "string",
                        "description": "Source key from nybble_sources_list",
                    },
                    "table": {
                        "type": "string",
                        "description": "Table name to get ERD detail for",
                    },
                },
                "required": ["source_key", "table"],
            },
        ),
        mcp_types.Tool(
            name="nybble_history",
            description=(
                "Return the query execution history for a source in the nybble serve UI. "
                "Each entry includes the SQL, timestamp, row count, and elapsed time. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {
                        "type": "string",
                        "description": "Source key from nybble_sources_list",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum entries to return (default 50)",
                        "default": 50,
                    },
                },
                "required": ["source_key"],
            },
        ),
        mcp_types.Tool(
            name="nybble_saved_queries",
            description=(
                "Manage saved queries for a source in the nybble serve UI. "
                "action='list': return all saved queries. "
                "action='create': save a new named query (requires name + sql). "
                "action='delete': delete a saved query (requires query_id). "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {
                        "type": "string",
                        "description": "Source key from nybble_sources_list",
                    },
                    "action": {
                        "type": "string",
                        "description": "Operation: 'list', 'create', or 'delete' (default 'list')",
                        "enum": ["list", "create", "delete"],
                        "default": "list",
                    },
                    "name": {
                        "type": "string",
                        "description": "Query name (required for action='create')",
                    },
                    "sql": {
                        "type": "string",
                        "description": "SQL text (required for action='create')",
                    },
                    "query_id": {
                        "type": "string",
                        "description": "Saved query ID (required for action='delete')",
                    },
                },
                "required": ["source_key"],
            },
        ),
        mcp_types.Tool(
            name="nybble_reports",
            description=(
                "Manage reports for a source in the nybble serve UI. "
                "action='list': list all reports. "
                "action='create': create a new report (requires title; blocks is optional list of {type, sql, title}). "
                "action='get': get a report with all query blocks executed (requires report_id). "
                "action='delete': delete a report (requires report_id). "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {
                        "type": "string",
                        "description": "Source key from nybble_sources_list",
                    },
                    "action": {
                        "type": "string",
                        "description": "Operation: 'list', 'create', 'get', or 'delete' (default 'list')",
                        "enum": ["list", "create", "get", "delete"],
                        "default": "list",
                    },
                    "title": {
                        "type": "string",
                        "description": "Report title (required for action='create')",
                    },
                    "blocks": {
                        "type": "array",
                        "description": "Report blocks for action='create' (each: {type, sql, title})",
                        "items": {"type": "object"},
                    },
                    "report_id": {
                        "type": "string",
                        "description": "Report ID (required for action='get' and action='delete')",
                    },
                },
                "required": ["source_key"],
            },
        ),
        mcp_types.Tool(
            name="nybble_dashboard",
            description=(
                "Interact with the dashboard for a source in the nybble serve UI. "
                "action='tiles': list all dashboard tiles with their SQL annotations and UI URL. "
                "action='run_tile': execute the query for a specific tile (requires tile_id). "
                "action='snapshots': list saved dashboard snapshots. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {
                        "type": "string",
                        "description": "Source key from nybble_sources_list",
                    },
                    "action": {
                        "type": "string",
                        "description": "Operation: 'tiles', 'run_tile', or 'snapshots' (default 'tiles')",
                        "enum": ["tiles", "run_tile", "snapshots"],
                        "default": "tiles",
                    },
                    "tile_id": {
                        "type": "string",
                        "description": "Tile ID (required for action='run_tile')",
                    },
                },
                "required": ["source_key"],
            },
        ),
        # ── Source management tools ────────────────────────────────────────────
        mcp_types.Tool(
            name="nybble_source_connect",
            description=(
                "Register a new data source in the nybble serve UI by connection string, "
                "URL, or structured form. "
                "type='connstring': pass connstring (e.g. 'postgresql://user:pass@host/db'). "
                "type='url': pass url (e.g. 'https://example.com/dump.sql.gz'). "
                "type='form': pass engine/host/port/user/password/database fields. "
                "type='restapi': pass url + optional headers. "
                "Returns: key, name, status ('uploaded' or 'ready'). "
                "Call nybble_source_process to trigger introspection after connecting. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "description": "Connection type: 'connstring', 'url', 'form', 'restapi', 'libsql', 'mongodb', 'snowflake', 'bigquery', 'supabase'",
                        "default": "connstring",
                    },
                    "connstring": {"type": "string", "description": "Connection string (for type='connstring')"},
                    "url": {"type": "string", "description": "URL to a remote dump or REST API (for type='url' or 'restapi')"},
                    "engine": {"type": "string", "description": "DB engine: postgresql/mysql/sqlite/mssql/clickhouse (for type='form')"},
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                    "user": {"type": "string"},
                    "password": {"type": "string"},
                    "database": {"type": "string"},
                    "headers": {
                        "type": "object",
                        "description": "HTTP headers for type='restapi'",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": [],
            },
        ),
        mcp_types.Tool(
            name="nybble_source_process",
            description=(
                "Trigger background introspection/processing of a source that was just "
                "uploaded or connected. This runs the pipeline that extracts schema.json, "
                "infers FK relationships, and indexes the source for the KG. "
                "Poll nybble_source_status until status='ready'. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string", "description": "Source key from nybble_sources_list"},
                },
                "required": ["source_key"],
            },
        ),
        mcp_types.Tool(
            name="nybble_source_status",
            description=(
                "Get the processing status and log for a source in the nybble serve UI. "
                "status values: 'uploaded' | 'processing' | 'ready' | 'error'. "
                "Also returns table count, FK count, and last 100 log lines. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string", "description": "Source key from nybble_sources_list"},
                },
                "required": ["source_key"],
            },
        ),
        mcp_types.Tool(
            name="nybble_source_delete",
            description=(
                "Delete a source from the nybble serve UI registry and remove all "
                "associated files and indexes. This is irreversible. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string", "description": "Source key to delete"},
                },
                "required": ["source_key"],
            },
        ),
        mcp_types.Tool(
            name="nybble_source_rename",
            description=(
                "Rename (update the display name of) a source in the nybble serve UI registry. "
                "The key remains unchanged. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string", "description": "Source key to rename"},
                    "name": {"type": "string", "description": "New display name"},
                },
                "required": ["source_key", "name"],
            },
        ),
        # ── Data analytics tools ───────────────────────────────────────────────
        mcp_types.Tool(
            name="nybble_aggregate",
            description=(
                "Run a column aggregate query (GROUP BY) on a table in the nybble serve UI. "
                "Returns grouped counts/sums for a column, useful for distributions. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                    "table": {"type": "string"},
                    "column": {"type": "string", "description": "Column to group by"},
                    "agg": {
                        "type": "string",
                        "description": "Aggregation: 'count' | 'sum' | 'avg' | 'min' | 'max' (default 'count')",
                        "default": "count",
                    },
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["source_key", "table", "column"],
            },
        ),
        mcp_types.Tool(
            name="nybble_facets",
            description=(
                "Get faceted value counts for one or more columns of a table in the UI. "
                "Equivalent to the filter sidebar in the Browse view. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                    "table": {"type": "string"},
                    "columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Columns to facet (omit for all categorical columns)",
                    },
                },
                "required": ["source_key", "table"],
            },
        ),
        mcp_types.Tool(
            name="nybble_col_profile_detail",
            description=(
                "Get detailed statistics for a single column in a table (via the UI). "
                "Returns: total rows, null count, distinct count, min, max, avg, top 10 values. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                    "table": {"type": "string"},
                    "column": {"type": "string"},
                },
                "required": ["source_key", "table", "column"],
            },
        ),
        mcp_types.Tool(
            name="nybble_autocomplete",
            description=(
                "Get autocomplete suggestions for table names and column names in a source. "
                "Used to help write SQL queries and explore schema structure. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                },
                "required": ["source_key"],
            },
        ),
        mcp_types.Tool(
            name="nybble_freshness",
            description=(
                "Check data freshness metrics for a source: last updated timestamp, "
                "staleness per table, and row count changes over time. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                },
                "required": ["source_key"],
            },
        ),
        mcp_types.Tool(
            name="nybble_triggers",
            description=(
                "List database triggers defined in a source (SQLite sources only). "
                "Returns trigger names, event types, table, and DDL. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                },
                "required": ["source_key"],
            },
        ),
        mcp_types.Tool(
            name="nybble_ui_search",
            description=(
                "Search table and column names across all registered schemas in the serve UI. "
                "Returns matches with their source, table, and column names. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search term for table/column names"},
                },
                "required": ["query"],
            },
        ),
        # ── Write operations ───────────────────────────────────────────────────
        mcp_types.Tool(
            name="nybble_write_row",
            description=(
                "Insert or update a row in a table via the nybble serve UI. "
                "action='insert': POST a new row (pass row as a dict of col→value). "
                "action='update': PATCH an existing row (pass row_id and updates dict). "
                "Only available when ALLOW_WRITES is enabled on the server. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                    "table": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["insert", "update"],
                        "default": "insert",
                    },
                    "row": {
                        "type": "object",
                        "description": "Column values to insert (action='insert') or update (action='update')",
                        "additionalProperties": True,
                    },
                    "row_id": {
                        "type": "integer",
                        "description": "Row rowid to update (required for action='update')",
                    },
                },
                "required": ["source_key", "table", "row"],
            },
        ),
        mcp_types.Tool(
            name="nybble_delete_row",
            description=(
                "Delete a row from a table via the nybble serve UI. "
                "Only available when ALLOW_WRITES is enabled on the server. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                    "table": {"type": "string"},
                    "row_id": {
                        "type": "integer",
                        "description": "Row rowid to delete",
                    },
                },
                "required": ["source_key", "table", "row_id"],
            },
        ),
        # ── Charts ─────────────────────────────────────────────────────────────
        mcp_types.Tool(
            name="nybble_charts",
            description=(
                "Manage saved charts for a source in the nybble serve UI. "
                "action='list': list all charts. "
                "action='create': create a chart (requires type, query_id or sql, title). "
                "action='delete': delete a chart (requires chart_id). "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["list", "create", "delete"],
                        "default": "list",
                    },
                    "chart_type": {
                        "type": "string",
                        "description": "Chart type: bar/line/pie/scatter (for action='create')",
                    },
                    "title": {"type": "string"},
                    "query_id": {"type": "string"},
                    "x_col": {"type": "string", "description": "X-axis column"},
                    "y_col": {"type": "string", "description": "Y-axis column"},
                    "chart_id": {"type": "string", "description": "Chart ID (for action='delete')"},
                },
                "required": ["source_key"],
            },
        ),
        # ── Metrics ────────────────────────────────────────────────────────────
        mcp_types.Tool(
            name="nybble_metrics",
            description=(
                "Manage data quality metrics for a source in the nybble serve UI. "
                "action='list': list all metrics. "
                "action='create': create a metric (requires name + sql). "
                "action='update': update a metric (requires metric_id + name/sql). "
                "action='delete': delete a metric (requires metric_id). "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["list", "create", "update", "delete"],
                        "default": "list",
                    },
                    "name": {"type": "string"},
                    "sql": {"type": "string"},
                    "metric_id": {"type": "string"},
                },
                "required": ["source_key"],
            },
        ),
        # ── Joins ──────────────────────────────────────────────────────────────
        mcp_types.Tool(
            name="nybble_joins",
            description=(
                "Manage cross-source virtual joins in the nybble serve UI. "
                "action='list': list all defined joins. "
                "action='create': create a join (requires source_keys list + join_def). "
                "action='update': update a join (requires join_key + join_def). "
                "action='delete': delete a join (requires join_key). "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "create", "update", "delete"],
                        "default": "list",
                    },
                    "join_key": {"type": "string", "description": "Join key (for update/delete)"},
                    "source_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Source keys to join (for create)",
                    },
                    "join_def": {
                        "type": "object",
                        "description": "Join definition: {name, tables: [{key, table, alias, join_col}]}",
                        "additionalProperties": True,
                    },
                },
                "required": [],
            },
        ),
        # ── Audit ──────────────────────────────────────────────────────────────
        mcp_types.Tool(
            name="nybble_audit",
            description=(
                "Return the write audit log for a source in the nybble serve UI. "
                "Each entry records who changed what, when, and what the old/new values were. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                    "limit": {"type": "integer", "default": 100},
                },
                "required": ["source_key"],
            },
        ),
        # ── KG UI tools (all /api/kg/* endpoints) ──────────────────────────────
        mcp_types.Tool(
            name="nybble_kg_ui_sources",
            description=(
                "List all sources available for the KG in the nybble serve UI. "
                "Returns sources that have been processed and have a schema or SQLite DB. "
                "Requires nybble serve to be running."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        mcp_types.Tool(
            name="nybble_kg_ui_search",
            description=(
                "Search the KG index in the nybble serve UI. "
                "mode='schema' (default) searches table/column names; "
                "mode='data' searches actual row values; "
                "mode='hybrid' combines both. "
                "Optionally restrict to specific source keys. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["schema", "data", "hybrid"],
                        "default": "hybrid",
                    },
                    "limit": {"type": "integer", "default": 30},
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Source keys to restrict search to (empty = all)",
                    },
                },
                "required": ["query"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_ui_graph",
            description=(
                "Build and return a KG graph from one or more sources in the nybble serve UI. "
                "mode='auto' (default): schema graph if FKs exist, data graph otherwise. "
                "mode='schema': table/column/FK nodes. "
                "mode='data': row/value/entity nodes from actual data. "
                "Returns Cytoscape-compatible JSON with node_count, edge_count, graph elements. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Source keys to include in the graph",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "schema", "data"],
                        "default": "auto",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max rows per table in data mode (default 40)",
                        "default": 40,
                    },
                    "col_roles": {
                        "type": "object",
                        "description": "Column role overrides: {src_key: {table: {col: 'node'|'edge'|'weight'|'ignore'|'auto'}}}",
                        "additionalProperties": True,
                    },
                    "relationships": {
                        "type": "array",
                        "description": "Explicit relationship rules: [{source_key, table, src_col, tgt_col, label_col?, weight_col?}]",
                        "items": {"type": "object"},
                    },
                },
                "required": ["sources"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_ui_entity",
            description=(
                "Resolve a single entity (row) in the KG and return its neighbourhood graph. "
                "Equivalent to clicking an entity node in the KG UI to see its connections. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                    "table": {"type": "string"},
                    "pk_col": {"type": "string", "description": "Primary key column name"},
                    "pk_val": {"type": "string", "description": "Primary key value"},
                    "depth": {"type": "integer", "description": "BFS depth (1-4, default 2)", "default": 2},
                },
                "required": ["source_key", "table", "pk_col", "pk_val"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_ui_columns",
            description=(
                "Return per-column metadata for a source, used to configure column roles "
                "in the KG UI (node/edge/weight/ignore). Includes distinct count, "
                "categorical detection, and sample values per column per table. "
                "Use nybble_kg_configure_cols to then apply roles and rebuild the graph. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                },
                "required": ["source_key"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_configure_cols",
            description=(
                "Configure column roles and explicit relationships for one or more KG sources, "
                "then rebuild and return the new graph. This is the programmatic equivalent of "
                "opening 'Configure Graph — Column Roles & Relationships' in the UI, setting "
                "roles, and clicking 'Rebuild Graph'. "
                "\n\nWorkflow: "
                "1. Call nybble_kg_ui_columns to discover columns and their suggested roles. "
                "2. Build col_roles: {source_key: {table: {column: 'node'|'edge'|'weight'|'ignore'|'auto'}}}. "
                "3. Optionally add relationships: [{source_key, table, src_col, tgt_col, label_col?, weight_col?}]. "
                "4. Call this tool — it POSTs to /api/kg/graph and returns the rebuilt graph. "
                "\n\nReturns: node_count, edge_count, mode, and the full Cytoscape-compatible graph JSON. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Source keys to include in the rebuilt graph",
                    },
                    "col_roles": {
                        "type": "object",
                        "description": (
                            "Column role overrides per source per table per column. "
                            "Shape: {source_key: {table_name: {column_name: role}}}. "
                            "Role values: 'node' | 'edge' | 'weight' | 'ignore' | 'auto'. "
                            "Only specify columns you want to override — omitted columns use 'auto'."
                        ),
                        "additionalProperties": True,
                    },
                    "relationships": {
                        "type": "array",
                        "description": (
                            "Explicit edge-definition rules. Each rule creates graph edges "
                            "between distinct values of src_col and tgt_col. "
                            "Required fields: source_key, table, src_col, tgt_col. "
                            "Optional: label_col (edge label), weight_col (numeric edge weight)."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_key": {"type": "string"},
                                "table":      {"type": "string"},
                                "src_col":    {"type": "string"},
                                "tgt_col":    {"type": "string"},
                                "label_col":  {"type": "string"},
                                "weight_col": {"type": "string"},
                            },
                            "required": ["source_key", "table", "src_col", "tgt_col"],
                        },
                    },
                    "mode": {
                        "type": "string",
                        "description": "Graph mode: 'data' (default when col_roles set) | 'schema' | 'auto'",
                        "enum": ["data", "schema", "auto"],
                        "default": "data",
                    },
                },
                "required": ["sources"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_ui_expand",
            description=(
                "FRD-expand the KG from a seed node using the UI's /api/kg/expand endpoint. "
                "Returns a BFS subgraph ranked by FRD (Frequency-Relative Degree) score. "
                "Each node carries frd_score, frd_deg, frd_count, frd_N, is_seed. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                    "node_id": {"type": "string", "description": "Seed node ID"},
                    "depth": {"type": "integer", "default": 1},
                },
                "required": ["source_key", "node_id"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_ui_frd_scores",
            description=(
                "Get FRD (Frequency-Relative Degree) scores for all nodes of a source "
                "from the nybble serve UI's /api/kg/frd-scores endpoint. "
                "Returns nodes sorted by FRD score descending. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                },
                "required": ["source_key"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_ui_validate",
            description=(
                "Validate the KG graph for one or more sources using the UI's "
                "/api/kg/validate endpoint. Returns issues with severity, code, message. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Source keys to validate",
                    },
                },
                "required": ["sources"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_ui_propose_link",
            description=(
                "Propose a cross-source link in the KG for human review. "
                "Records source_id, target_id, relation type, and rationale. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "target_id": {"type": "string"},
                    "relation": {"type": "string", "default": "cross_db:manual"},
                    "rationale": {"type": "string"},
                },
                "required": ["source_id", "target_id"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_ui_temporal",
            description=(
                "Return a KG graph filtered to edges whose valid_from/valid_until "
                "spans the requested time window. Useful for exploring how the graph "
                "changes over time. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "from_date": {"type": "string", "description": "Start of window (ISO date string)"},
                    "to_date": {"type": "string", "description": "End of window (ISO date string)"},
                },
                "required": ["sources"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_ui_versions",
            description=(
                "List stored graph snapshots (versions) for a source in the KG. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                },
                "required": ["source_key"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_ui_diff",
            description=(
                "Compute a structural diff between two KG snapshots for a source. "
                "Returns added/removed nodes and edges. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {"type": "string"},
                    "before_id": {"type": "integer", "description": "Snapshot ID for 'before' state"},
                    "after_id": {"type": "integer", "description": "Snapshot ID for 'after' state"},
                },
                "required": ["source_key"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_ui_reindex",
            description=(
                "Trigger a background re-index of one or all sources in the KG store. "
                "Pass source_keys to re-index specific sources; omit to re-index all. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Source keys to re-index (omit for all)",
                    },
                },
                "required": [],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_ui_reset",
            description=(
                "Wipe all KG index state so sources can be re-indexed clean. "
                "Pass source_keys to reset specific sources; omit to reset all. "
                "Source files are NOT deleted — only the search index. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Source keys to reset (omit for all)",
                    },
                },
                "required": [],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_ui_status",
            description=(
                "Get KG index health: capabilities (dense/sparse vectors, embed model), "
                "per-source index states, and count of ready sources. "
                "Requires nybble serve to be running."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        mcp_types.Tool(
            name="nybble_kg_ui_capabilities",
            description=(
                "Return the KG search capabilities for the current server configuration: "
                "whether dense/sparse vector search is enabled and which embedding model is active. "
                "Requires nybble serve to be running."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),

        # ── New: suggest links, proposals pipeline, col-role inline ──────────
        mcp_types.Tool(
            name="nybble_kg_suggest_links",
            description=(
                "Analyse two or more KG sources and return ranked cross-source column-pair "
                "candidates for linking, scored by name similarity, type compatibility, and "
                "shared sample values (Jaccard). "
                "Also returns columns_by_source (full column inventory per source/table) and "
                "score_matrix ({col_id: {other_col_id: score}}) for downstream drag-and-drop "
                "or automated link creation. "
                "\n\nTypical workflow: "
                "1. Call this tool to discover candidates. "
                "2. Call nybble_kg_ui_propose_link for each pair worth connecting. "
                "3. Call nybble_kg_proposals_approve to approve. "
                "4. Call nybble_kg_configure_cols (or nybble_kg_ui_graph) to rebuild — "
                "   approved proposals are automatically injected as edges. "
                "\n\nRequires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Two or more source keys to compare",
                        "minItems": 2,
                    },
                },
                "required": ["sources"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_proposals_list",
            description=(
                "List all stored cross-source link proposals. "
                "Each proposal has an id, src_id, tgt_id, relation, rationale, and "
                "status ('pending_review' | 'approved' | 'rejected'). "
                "Filter to proposals relevant to specific sources with the optional sources list. "
                "Only approved proposals are injected as graph edges on the next rebuild. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: restrict to proposals whose src_id or tgt_id belongs to these source keys",
                    },
                },
                "required": [],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_proposals_approve",
            description=(
                "Approve a stored link proposal, changing its status to 'approved'. "
                "Approved proposals are automatically injected as edges the next time "
                "the KG graph is built for the relevant sources. "
                "Use nybble_kg_proposals_list to find proposal ids. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Proposal id (from nybble_kg_proposals_list)",
                    },
                },
                "required": ["id"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_proposals_reject",
            description=(
                "Reject a stored link proposal, changing its status to 'rejected'. "
                "Rejected proposals are excluded from graph builds. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Proposal id"},
                },
                "required": ["id"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_proposals_delete",
            description=(
                "Permanently delete a link proposal. "
                "Unlike reject, the proposal is removed from storage entirely. "
                "Requires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Proposal id"},
                },
                "required": ["id"],
            },
        ),
        mcp_types.Tool(
            name="nybble_kg_col_role",
            description=(
                "Set the role of a single column in the KG graph, then immediately rebuild "
                "and return the updated graph. This is the programmatic equivalent of clicking "
                "a node in the graph, choosing a role in the detail panel, and clicking Apply. "
                "\n\nRole values: "
                "'node'   — column values become entity nodes (best for categorical columns), "
                "'edge'   — column values become edge labels between row nodes, "
                "'weight' — column values set edge weights (numeric columns), "
                "'ignore' — column is excluded from the graph entirely, "
                "'auto'   — let Nybble decide (default). "
                "\n\nTo set roles on multiple columns at once use nybble_kg_configure_cols instead. "
                "\n\nRequires nybble serve to be running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {
                        "type": "string",
                        "description": "Source key (from nybble_kg_ui_sources or nybble_sources_list)",
                    },
                    "table": {
                        "type": "string",
                        "description": "Table name within the source",
                    },
                    "column": {
                        "type": "string",
                        "description": "Column name to configure",
                    },
                    "role": {
                        "type": "string",
                        "enum": ["node", "edge", "weight", "ignore", "auto"],
                        "description": "Role to assign to this column",
                    },
                    "additional_sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Other source keys to include in the same graph rebuild (optional)",
                    },
                },
                "required": ["source_key", "table", "column", "role"],
            },
        ),
    ]


# ── Tool call dispatcher ──────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(
    name: str,
    arguments: dict[str, Any],
) -> list[mcp_types.TextContent]:
    try:
        result = await _dispatch(name, arguments)
        return [mcp_types.TextContent(type="text", text=_serialise(result))]
    except Exception as exc:
        return [mcp_types.TextContent(type="text", text=f"Error: {exc}")]


async def _dispatch(name: str, args: dict) -> Any:
    if name == "nybble_load":
        rel = nybble.load(
            args["source"],
            name=args.get("name"),
            headers=args.get("headers"),
        )
        return {
            "table": rel.name,
            "source_type": rel.source_type,
            "row_count": rel.row_count,
            "tables": getattr(rel, "tables", None),
        }

    if name == "nybble_list_tables":
        return nybble.list_relations()

    if name == "nybble_schema":
        rel = _REGISTERED.get(args["table"])
        if rel is None:
            raise KeyError(f"Table {args['table']!r} not loaded. Call nybble_load first.")
        return rel.schema()

    if name == "nybble_query":
        result = nybble.query(args["sql"], limit=args.get("limit", 500))
        return {
            "columns": result.columns,
            "rows": result.to_dict(),
            "row_count": result.row_count,
            "elapsed_ms": result.elapsed_ms,
            "truncated": result.truncated,
        }

    if name == "nybble_explain":
        return nybble.explain(args["sql"], analyze=args.get("analyze", False))

    if name == "nybble_profile":
        profiles = nybble.profile_relation(args["table"])
        return [
            {
                "column": p.name,
                "type": p.dtype,
                "non_null_pct": round(100 - p.null_pct, 2),
                "null_count": p.null_count,
                "distinct": p.distinct,
                "approx": p.approx,
                "cardinality_pct": p.cardinality_pct,
                "min": p.min_val,
                "max": p.max_val,
                "top_value": p.top_val,
            }
            for p in profiles
        ]

    if name == "nybble_search_history":
        return nybble.search_history(
            term=args.get("term"),
            limit=args.get("limit", 20),
        )

    if name == "nybble_infer_schema":
        return [
            {
                "from_table": f.src_table,
                "from_col": f.src_col,
                "to_table": f.tgt_table,
                "to_col": f.tgt_col,
                "confidence": f.confidence,
                "tier": f.tier,
            }
            for f in nybble.infer_fks()
        ]

    if name == "nybble_kg_summary":
        return nybble.kg_summary()

    if name == "nybble_diff":
        diff = nybble.schema_diff(args["source_a"], args["source_b"])
        return diff.to_dict()

    if name == "nybble_erd":
        path = nybble.erd(output=args.get("output"), open_browser=False)
        return {"path": str(path)}

    if name == "nybble_kg":
        result = nybble.kg(output_dir=args.get("output_dir"), open_browser=False)
        from nybble.kg import _serve_ui_kg_url
        interactive_url = _serve_ui_kg_url()
        resp: dict = {
            "graphml_path": str(result.graphml_path),
            "jsonld_path":  str(result.jsonld_path),
            "html_path":    str(result.html_path),
            "summary":      result.summary,
            "note": (
                f"Static export written. For the full interactive UI open: {interactive_url}"
                if interactive_url
                else (
                    "Static export written. Run `nybble serve` then open "
                    "http://localhost:7700/kg for the full interactive Knowledge Graph UI."
                )
            ),
        }
        if interactive_url:
            resp["interactive_url"] = interactive_url
        return resp

    if name == "nybble_kg_search":
        results = nybble.search_nodes(
            query=args["query"],
            top_k=args.get("top_k", 10),
        )
        return [
            {
                "node_id": r.node_id,
                "label": r.label,
                "entity_type": r.entity_type,
                "score": r.score,
                "match_mode": r.match_mode,
            }
            for r in results
        ]

    if name == "nybble_kg_export_pgq":
        from nybble.infer_fk import infer_fks
        from nybble.kg import _build_graph, generate_pgq_ddl as _gen_pgq

        rels = list(_REGISTERED.values())
        if not rels:
            raise ValueError("No sources loaded. Call nybble_load first.")
        graph_name = args.get("graph_name", "nybble_graph")
        fk_candidates = infer_fks(rels)
        G = _build_graph(rels, fk_candidates)
        ddl = _gen_pgq(G, fk_candidates, graph_name=graph_name)
        if not ddl.strip():
            raise ValueError("Graph is empty - no DDL generated.")
        return {"ddl": ddl}

    # ── New KG analytics tools ─────────────────────────────────────────────────

    if name == "nybble_kg_graph":
        return nybble.kg_graph(display_limit=args.get("display_limit", 500))

    if name == "nybble_kg_node":
        return nybble.node_detail(args["node_id"])

    if name == "nybble_kg_frd_scores":
        return nybble.frd_scores(top_k=args.get("top_k", 50))

    if name == "nybble_kg_frd_expand":
        return nybble.frd_expand(
            seed_node_id=args["node_id"],
            depth=args.get("depth", 1),
            max_nodes=args.get("max_nodes", 200),
        )

    if name == "nybble_kg_validate":
        return nybble.kg_validate()

    # ── Serve UI integration tools ─────────────────────────────────────────────

    if name == "nybble_serve_status":
        from nybble.kg import _serve_ui_kg_url
        port = args.get("port", 7700)
        url = _serve_ui_kg_url(port=port)
        return {
            "running": url is not None,
            "url":     url,
            "port":    port,
            "note": (
                f"nybble serve is running. Open {url} for the full interactive UI."
                if url else
                "nybble serve is not running. Call nybble_serve_start to launch it."
            ),
        }

    if name == "nybble_serve_start":
        import subprocess, sys
        from nybble.kg import _serve_ui_kg_url
        port = args.get("port", 7700)
        host = args.get("host", "127.0.0.1")

        # Check if already running — do not start a second instance
        existing = _serve_ui_kg_url(port=port)
        if existing:
            return {
                "url":             existing,
                "port":            port,
                "already_running": True,
                "pid":             None,
                "note":            f"nybble serve is already running. UI: {existing}",
            }

        # Launch as a detached subprocess so the MCP server keeps responding
        cmd = [sys.executable, "-m", "nybble", "serve",
               "--host", host, "--port", str(port), "--no-browser"]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=(sys.platform != "win32"),
                **( {"start_new_session": True} if sys.platform != "win32"
                    else {"creationflags": subprocess.DETACHED_PROCESS
                                         | subprocess.CREATE_NEW_PROCESS_GROUP} ),
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to start nybble serve: {exc}. "
                "Try running 'nybble serve' manually in a terminal."
            ) from exc

        url = f"http://{host}:{port}/kg"
        return {
            "url":             url,
            "port":            port,
            "already_running": False,
            "pid":             proc.pid,
            "note": (
                f"nybble serve started (pid {proc.pid}). "
                f"UI will be ready at {url} in ~2 seconds."
            ),
        }

    # ── Sources & Browse & Query UI tools ─────────────────────────────────────

    if name == "nybble_sources_list":
        return _api_get("/api/files")

    if name == "nybble_source_tables":
        key = args["source_key"]
        counts = _api_get(f"/api/rowcounts/{key}")
        schema = _api_get(f"/api/metadata/{key}")
        tables_list = []
        for tbl, cnt in (counts.items() if isinstance(counts, dict) else {}.items()):
            tables_list.append({"table": tbl, "row_count": cnt})
        return {
            "source_key": key,
            "name":       schema.get("name", key) if isinstance(schema, dict) else key,
            "tables":     tables_list,
        }

    if name == "nybble_browse_rows":
        key        = args["source_key"]
        table      = args["table"]
        page       = args.get("page", 1)
        page_size  = min(args.get("page_size", 50), 200)
        sort       = args.get("sort", "")
        filters    = args.get("filters", {})   # {"col__op": "value", ...}
        params: dict = {"page": page, "page_size": page_size}
        if sort:
            params["_sort"] = sort
        params.update(filters)
        return _api_get(f"/api/rows/{key}/{table}", params=params)

    if name == "nybble_run_query":
        key    = args["source_key"]
        sql    = args["sql"]
        limit  = args.get("limit", 1000)
        return _api_post(f"/api/query/{key}", {"sql": sql, "limit": limit})

    if name == "nybble_col_profile":
        key    = args["source_key"]
        table  = args["table"]
        return _api_get(f"/api/colstats/{key}/{table}")

    if name == "nybble_erd_detail":
        key    = args["source_key"]
        table  = args["table"]
        return _api_get(f"/api/erd/table-detail/{key}/{table}")

    if name == "nybble_history":
        key   = args["source_key"]
        limit = args.get("limit", 50)
        rows  = _api_get(f"/api/history/{key}")
        if isinstance(rows, list):
            rows = rows[:limit]
        return rows

    # ── Saved queries ──────────────────────────────────────────────────────────

    if name == "nybble_saved_queries":
        action = args.get("action", "list")
        key    = args["source_key"]
        if action == "list":
            return _api_get(f"/api/queries/{key}")
        if action == "create":
            return _api_post(f"/api/queries/{key}",
                             {"name": args["name"], "sql": args["sql"]})
        if action == "delete":
            return _api_delete(f"/api/queries/{key}/{args['query_id']}")
        raise ValueError(f"Unknown action {action!r}. Use 'list', 'create', or 'delete'.")

    # ── Reports ────────────────────────────────────────────────────────────────

    if name == "nybble_reports":
        action = args.get("action", "list")
        key    = args["source_key"]
        if action == "list":
            return _api_get(f"/api/reports/{key}")
        if action == "create":
            return _api_post(f"/api/reports/{key}",
                             {"title": args.get("title", "Untitled"),
                              "blocks": args.get("blocks", [])})
        if action == "get":
            # Execute all query blocks and return the rendered report JSON
            reports = _api_get(f"/api/reports/{key}")
            rid = args["report_id"]
            report = next((r for r in reports if r["id"] == rid), None)
            if not report:
                raise KeyError(f"Report {rid!r} not found.")
            # Execute each query block against the source
            executed_blocks = []
            for block in report.get("blocks", []):
                b = dict(block)
                if block.get("type") == "query" and block.get("sql"):
                    try:
                        result = _api_post(f"/api/query/{key}",
                                           {"sql": block["sql"], "limit": 500})
                        b["result"] = result
                    except Exception as exc:
                        b["error"] = str(exc)
                executed_blocks.append(b)
            return {**report, "blocks": executed_blocks,
                    "ui_url": f"{_serve_base()}/report/{key}/{rid}"}
        if action == "delete":
            return _api_delete(f"/api/reports/{key}/{args['report_id']}")
        raise ValueError(f"Unknown action {action!r}. Use 'list', 'create', 'get', or 'delete'.")

    # ── Dashboard ──────────────────────────────────────────────────────────────

    if name == "nybble_dashboard":
        action = args.get("action", "tiles")
        key    = args["source_key"]
        if action == "tiles":
            tiles   = _api_get(f"/api/tiles/{key}")
            queries = _api_get(f"/api/queries/{key}")
            q_map   = {q["id"]: q for q in (queries if isinstance(queries, list) else [])}
            # Annotate each tile with its SQL for agent convenience
            for tile in (tiles if isinstance(tiles, list) else []):
                qid = tile.get("query_id")
                if qid and qid in q_map:
                    tile["sql"]  = q_map[qid].get("sql", "")
                    tile["query_name"] = q_map[qid].get("name", "")
            return {
                "source_key": key,
                "tiles":      tiles,
                "ui_url":     f"{_serve_base()}/dashboard/{key}",
            }
        if action == "run_tile":
            tile_id = args["tile_id"]
            tiles   = _api_get(f"/api/tiles/{key}")
            tile    = next((t for t in (tiles if isinstance(tiles, list) else [])
                            if t["id"] == tile_id), None)
            if not tile:
                raise KeyError(f"Tile {tile_id!r} not found.")
            queries = _api_get(f"/api/queries/{key}")
            q_map   = {q["id"]: q for q in (queries if isinstance(queries, list) else [])}
            qid     = tile.get("query_id")
            sql     = q_map.get(qid, {}).get("sql", "") if qid else ""
            if not sql:
                raise ValueError("Tile has no associated SQL query.")
            result = _api_post(f"/api/query/{key}", {"sql": sql, "limit": 5000})
            return {"tile": tile, "result": result}
        if action == "snapshots":
            return _api_get(f"/api/dashboard_snapshots/{key}")
        raise ValueError(f"Unknown action {action!r}. Use 'tiles', 'run_tile', or 'snapshots'.")

    # ── Source management ──────────────────────────────────────────────────────

    if name == "nybble_source_connect":
        # Remove None values and pass through to /api/connect
        body = {k: v for k, v in args.items() if v is not None}
        if "type" not in body:
            body["type"] = "connstring"
        return _api_post("/api/connect", body)

    if name == "nybble_source_process":
        return _api_post(f"/api/process/{args['source_key']}", {})

    if name == "nybble_source_status":
        return _api_get(f"/api/status/{args['source_key']}")

    if name == "nybble_source_delete":
        return _api_delete(f"/api/delete/{args['source_key']}")

    if name == "nybble_source_rename":
        return _api_post(f"/api/registry/{args['source_key']}", {"name": args["name"]})

    # ── Data analytics ─────────────────────────────────────────────────────────

    if name == "nybble_aggregate":
        key    = args["source_key"]
        table  = args["table"]
        column = args["column"]
        agg    = args.get("agg", "count")
        limit  = args.get("limit", 50)
        return _api_get(f"/api/aggregate/{key}/{table}",
                        params={"col": column, "agg": agg, "limit": limit})

    if name == "nybble_facets":
        key   = args["source_key"]
        table = args["table"]
        params: dict = {}
        cols  = args.get("columns")
        if cols:
            params["cols"] = ",".join(cols)
        return _api_get(f"/api/facets/{key}/{table}", params=params or None)

    if name == "nybble_col_profile_detail":
        key    = args["source_key"]
        table  = args["table"]
        column = args["column"]
        return _api_get(f"/api/profile/{key}/{table}/{column}")

    if name == "nybble_autocomplete":
        return _api_get(f"/api/autocomplete/{args['source_key']}")

    if name == "nybble_freshness":
        return _api_get(f"/api/freshness/{args['source_key']}")

    if name == "nybble_triggers":
        return _api_get(f"/api/triggers/{args['source_key']}")

    if name == "nybble_ui_search":
        return _api_get("/api/search", params={"q": args["query"]})

    # ── Write operations ───────────────────────────────────────────────────────

    if name == "nybble_write_row":
        import urllib.request
        key    = args["source_key"]
        table  = args["table"]
        action = args.get("action", "insert")
        row    = args["row"]
        if action == "insert":
            return _api_post(f"/api/rows/{key}/{table}", row)
        if action == "update":
            row_id = args.get("row_id")
            if row_id is None:
                raise ValueError("row_id is required for action='update'")
            # PATCH uses a list of [{rowid, ...updates}]
            base = _serve_base()
            url  = base + f"/api/rows/{key}/{table}"
            data = json.dumps([{"rowid": row_id, **row}], ensure_ascii=False).encode()
            req  = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            resp = urllib.request.urlopen(req, timeout=15)
            return json.loads(resp.read().decode())
        raise ValueError(f"Unknown action {action!r}. Use 'insert' or 'update'.")

    if name == "nybble_delete_row":
        key    = args["source_key"]
        table  = args["table"]
        row_id = args["row_id"]
        return _api_delete(f"/api/rows/{key}/{table}?rowid={row_id}")

    # ── Charts ─────────────────────────────────────────────────────────────────

    if name == "nybble_charts":
        action = args.get("action", "list")
        key    = args["source_key"]
        if action == "list":
            return _api_get(f"/api/charts/{key}")
        if action == "create":
            body = {
                "type":     args.get("chart_type", "bar"),
                "title":    args.get("title", "Untitled chart"),
                "query_id": args.get("query_id"),
                "x_col":    args.get("x_col"),
                "y_col":    args.get("y_col"),
            }
            return _api_post(f"/api/charts/{key}", {k: v for k, v in body.items() if v is not None})
        if action == "delete":
            return _api_delete(f"/api/charts/{key}/{args['chart_id']}")
        raise ValueError(f"Unknown action {action!r}. Use 'list', 'create', or 'delete'.")

    # ── Metrics ────────────────────────────────────────────────────────────────

    if name == "nybble_metrics":
        import urllib.request
        action = args.get("action", "list")
        key    = args["source_key"]
        if action == "list":
            return _api_get(f"/api/metrics/{key}")
        if action == "create":
            return _api_post(f"/api/metrics/{key}",
                             {"name": args["name"], "sql": args["sql"]})
        if action == "update":
            mid  = args["metric_id"]
            body = {k: args[k] for k in ("name", "sql") if k in args}
            base = _serve_base()
            url  = base + f"/api/metrics/{key}/{mid}"
            data = json.dumps(body, ensure_ascii=False).encode()
            req  = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            resp = urllib.request.urlopen(req, timeout=15)
            return json.loads(resp.read().decode())
        if action == "delete":
            return _api_delete(f"/api/metrics/{key}/{args['metric_id']}")
        raise ValueError(f"Unknown action {action!r}. Use 'list', 'create', 'update', or 'delete'.")

    # ── Joins ──────────────────────────────────────────────────────────────────

    if name == "nybble_joins":
        import urllib.request
        action = args.get("action", "list")
        if action == "list":
            return _api_get("/api/joins")
        if action == "create":
            body = {"source_keys": args.get("source_keys", []),
                    **(args.get("join_def") or {})}
            return _api_post("/api/joins", body)
        if action == "update":
            jk   = args["join_key"]
            body = args.get("join_def") or {}
            base = _serve_base()
            url  = base + f"/api/joins/{jk}"
            data = json.dumps(body, ensure_ascii=False).encode()
            req  = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
                method="PUT",
            )
            resp = urllib.request.urlopen(req, timeout=15)
            return json.loads(resp.read().decode())
        if action == "delete":
            return _api_delete(f"/api/joins/{args['join_key']}")
        raise ValueError(f"Unknown action {action!r}. Use 'list', 'create', 'update', or 'delete'.")

    # ── Audit ──────────────────────────────────────────────────────────────────

    if name == "nybble_audit":
        key   = args["source_key"]
        limit = args.get("limit", 100)
        rows  = _api_get(f"/api/audit/{key}")
        if isinstance(rows, list):
            rows = rows[:limit]
        return rows

    # ── KG UI tools ────────────────────────────────────────────────────────────

    if name == "nybble_kg_ui_sources":
        return _api_get("/api/kg/sources")

    if name == "nybble_kg_ui_search":
        params = {
            "q":      args["query"],
            "mode":   args.get("mode", "hybrid"),
            "limit":  args.get("limit", 30),
        }
        srcs = args.get("sources")
        if srcs:
            params["sources"] = ",".join(srcs)
        return _api_get("/api/kg/search", params=params)

    if name == "nybble_kg_ui_graph":
        body = {
            "sources":       ",".join(args["sources"]),
            "mode":          args.get("mode", "auto"),
            "limit":         args.get("limit", 40),
            "col_roles":     args.get("col_roles", {}),
            "relationships": args.get("relationships", []),
        }
        return _api_post("/api/kg/graph", body)

    if name == "nybble_kg_ui_entity":
        return _api_get("/api/kg/entity", params={
            "key":    args["source_key"],
            "table":  args["table"],
            "pk_col": args["pk_col"],
            "pk_val": args["pk_val"],
            "depth":  args.get("depth", 2),
        })

    if name == "nybble_kg_ui_columns":
        return _api_get("/api/kg/columns", params={"source": args["source_key"]})

    if name == "nybble_kg_configure_cols":
        # Equivalent of clicking "Rebuild Graph" in the Configure Columns panel.
        # Accepts the same body as /api/kg/graph POST, but with friendlier defaults.
        sources       = args["sources"]           # list[str]
        col_roles     = args.get("col_roles", {})
        relationships = args.get("relationships", [])
        mode          = args.get("mode", "data")
        body = {
            "sources":       ",".join(sources),
            "mode":          mode,
            "col_roles":     col_roles,
            "relationships": relationships,
        }
        result = _api_post("/api/kg/graph", body)
        # Return a clean summary plus the full graph for agents that want to
        # inspect the result without scrolling through Cytoscape JSON.
        if isinstance(result, dict) and "graph" in result:
            return {
                "node_count":    result.get("node_count", 0),
                "edge_count":    result.get("edge_count", 0),
                "mode":          result.get("mode", mode),
                "sources":       sources,
                "col_roles":     col_roles,
                "relationships": relationships,
                "graph":         result["graph"],
                "note": (
                    f"Graph rebuilt successfully: {result.get('node_count', 0)} nodes, "
                    f"{result.get('edge_count', 0)} edges. "
                    f"Open http://localhost:7700/kg in a browser to see the result."
                ),
            }
        return result

    if name == "nybble_kg_ui_expand":
        return _api_get("/api/kg/expand", params={
            "source": args["source_key"],
            "node":   args["node_id"],
            "depth":  args.get("depth", 1),
        })

    if name == "nybble_kg_ui_frd_scores":
        return _api_get("/api/kg/frd-scores", params={"source": args["source_key"]})

    if name == "nybble_kg_ui_validate":
        return _api_get("/api/kg/validate",
                        params={"sources": ",".join(args["sources"])})

    if name == "nybble_kg_ui_propose_link":
        return _api_post("/api/kg/propose_link", {
            "source_id": args["source_id"],
            "target_id": args["target_id"],
            "relation":  args.get("relation", "cross_db:manual"),
            "rationale": args.get("rationale", ""),
        })

    if name == "nybble_kg_ui_temporal":
        params = {"sources": ",".join(args["sources"])}
        if args.get("from_date"):
            params["from"] = args["from_date"]
        if args.get("to_date"):
            params["to"] = args["to_date"]
        return _api_get("/api/kg/temporal", params=params)

    if name == "nybble_kg_ui_versions":
        return _api_get("/api/kg/versions", params={"source": args["source_key"]})

    if name == "nybble_kg_ui_diff":
        params = {"source": args["source_key"]}
        if "before_id" in args:
            params["before_id"] = args["before_id"]
        if "after_id" in args:
            params["after_id"] = args["after_id"]
        return _api_get("/api/kg/diff", params=params)

    if name == "nybble_kg_ui_reindex":
        body: dict = {}
        keys = args.get("source_keys")
        if keys:
            body["keys"] = keys
        return _api_post("/api/kg/reindex", body)

    if name == "nybble_kg_ui_reset":
        body = {}
        keys = args.get("source_keys")
        if keys:
            body["keys"] = keys
        return _api_post("/api/kg/reset", body)

    if name == "nybble_kg_ui_status":
        return _api_get("/api/kg/status")

    if name == "nybble_kg_ui_capabilities":
        return _api_get("/api/kg/capabilities")

    # ── Link proposals pipeline ────────────────────────────────────────────────

    if name == "nybble_kg_suggest_links":
        body = {"sources": ",".join(args["sources"])}
        return _api_post("/api/kg/suggest_links", body)

    if name == "nybble_kg_proposals_list":
        params: dict = {}
        srcs = args.get("sources")
        if srcs:
            params["sources"] = ",".join(srcs)
        return _api_get("/api/kg/proposals", params=params or None)

    if name == "nybble_kg_proposals_approve":
        return _api_post("/api/kg/proposals/approve", {"id": args["id"]})

    if name == "nybble_kg_proposals_reject":
        return _api_post("/api/kg/proposals/reject", {"id": args["id"]})

    if name == "nybble_kg_proposals_delete":
        return _api_delete(f"/api/kg/proposals/{args['id']}")

    if name == "nybble_kg_col_role":
        body = {
            "source_key":        args["source_key"],
            "table":             args["table"],
            "column":            args["column"],
            "role":              args["role"],
            "additional_sources": args.get("additional_sources", []),
        }
        return _api_post("/api/kg/col_role", body)

    raise ValueError(f"Unknown tool: {name!r}")


# ── Serve UI REST client helpers ───────────────────────────────────────────────
# All serve-dependent tools call these instead of directly importing Flask/SQLite.
# When nybble serve is not running they raise a clear actionable error.

def _serve_base(port: int = 7700) -> str:
    """Return the base URL of the running serve UI, or raise if not running."""
    from nybble.kg import _serve_ui_kg_url
    if not _serve_ui_kg_url(port=port):
        raise RuntimeError(
            f"nybble serve is not running on port {port}. "
            "Call nybble_serve_start first (or run 'nybble serve' in a terminal), "
            "then retry this tool."
        )
    return f"http://localhost:{port}"


def _api_get(path: str, params: Optional[dict] = None) -> Any:
    """GET {serve_base}{path} and return parsed JSON."""
    base = _serve_base()
    url  = base + path
    try:
        import urllib.request, urllib.parse
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        req  = urllib.request.urlopen(url, timeout=15)
        return json.loads(req.read().decode())
    except Exception as exc:
        raise RuntimeError(f"GET {path} failed: {exc}") from exc


def _api_post(path: str, body: dict) -> Any:
    """POST {serve_base}{path} with JSON body and return parsed JSON."""
    base = _serve_base()
    url  = base + path
    try:
        import urllib.request
        data = json.dumps(body, ensure_ascii=False).encode()
        req  = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode())
    except Exception as exc:
        raise RuntimeError(f"POST {path} failed: {exc}") from exc


def _api_delete(path: str) -> Any:
    """DELETE {serve_base}{path} and return parsed JSON."""
    base = _serve_base()
    url  = base + path
    try:
        import urllib.request
        req  = urllib.request.Request(url, method="DELETE")
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode())
    except Exception as exc:
        raise RuntimeError(f"DELETE {path} failed: {exc}") from exc


def _serialise(obj: Any) -> str:
    """JSON-serialise, converting non-serialisable types to strings."""
    return json.dumps(obj, default=str, indent=2, ensure_ascii=False)


# ── Entry point ───────────────────────────────────────────────────────────────

async def _run_server() -> None:
    """Run the MCP server over stdio (async context-manager pattern)."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def _load_config_at_startup() -> None:
    """Auto-load sources from nybble.yaml (if present) before the server loop starts.

    Silently skips if the file is missing, unreadable, or any source fails to load —
    the MCP server must always start regardless of workspace state.
    """
    try:
        from nybble.config import find_config, load_config, apply_config
        cfg_path = find_config()
        if cfg_path is not None:
            cfg = load_config(cfg_path)
            apply_config(cfg)
    except Exception:
        pass  # Never block server startup on config errors


def main() -> None:
    """Start the Nybble MCP server (stdio transport).

    Auto-loads sources from ``nybble.yaml`` in the current directory (if present)
    before entering the stdio server loop.  Equivalent to the sources a
    ``nybble serve`` instance would pre-load from the same config file.
    """
    _load_config_at_startup()
    asyncio.run(_run_server())


if __name__ == "__main__":
    main()
