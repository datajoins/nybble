"""server/kg/kg_mcp.py - MCP bridge for Knowledge Graph tools.

Exposes KG search, entity resolution, and subgraph APIs as callable tool
implementations suitable for use with any MCP server host.

No MCP SDK is imported at module level. The tool functions are plain Python
callables that accept and return JSON-serialisable dicts. An MCP server
(e.g., in .bob/datahub/) can wrap these as MCP tool handlers.

MCP architecture split (Phase 3-E integration note)
----------------------------------------------------
* nybble/mcp_server.py - CLI-facing MCP server using the full MCP SDK
  (Server class from the ``mcp`` package).  Backed by nybble.core.* via
  the nybble public API (nybble.erd, nybble.kg, nybble.infer_fks, etc.).
  This is what AI agents (Claude Desktop, Cursor, etc.) connect to when
  the CLI is installed.

* server/kg/kg_mcp.py  - Flask-facing MCP bridge (this file).  Plain Python
  callables with no SDK dependency.  Backed by nybble.core.* where the graph
  needs to be built from a Schema + SQLite source (server context).  The
  function build_graph_from_schema() in server/kg/kg_graph.py is the adapter.

No new unauthenticated HTTP endpoints are introduced by this bridge.
Authentication controls, if/when added, belong in the Flask route handlers
(server/app/api_server.py), not here.

# MARKER:kg-mcp-bridge
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

# Prompt registry - all LLM prompts for MCP tools live here.
# MARKER:kg-prompt-registry
from .kg_prompts import kg_prompt_registry  # noqa: F401

logger = logging.getLogger(__name__)


# ── Tool definitions (MCP-compatible schema) ──────────────────────────────────

KG_TOOLS = [
    {
        "name": "kg_search",
        "description": (
            "Search the Knowledge Graph for entities matching a text query. "
            "Returns ranked results from schema and data layers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query":  {"type": "string",  "description": "Search query text"},
                "layer":  {"type": "string",  "description": "Search layer: 'schema' or 'data'", "default": "schema"},
                "limit":  {"type": "integer", "description": "Maximum results to return", "default": 20},
                "mode":   {"type": "string",  "description": "Search mode: 'keyword', 'semantic', or 'hybrid'", "default": "hybrid"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "kg_resolve",
        "description": (
            "Resolve an entity in the Knowledge Graph by following FK relationships. "
            "Returns connected nodes and edges up to the specified depth."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_key": {"type": "string", "description": "Data source identifier"},
                "table":      {"type": "string", "description": "Table name"},
                "pk_col":     {"type": "string", "description": "Primary key column name"},
                "pk_val":     {"type": "string", "description": "Primary key value"},
                "depth":      {"type": "integer", "description": "FK traversal depth (default 2)", "default": 2},
            },
            "required": ["source_key", "table", "pk_col", "pk_val"],
        },
    },
    {
        "name": "kg_subgraph",
        "description": (
            "Return a depth-limited subgraph centred on a named entity. "
            "Returns Cytoscape.js-format nodes and edges."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root_id": {"type": "string",  "description": "Root entity node ID"},
                "depth":   {"type": "integer", "description": "Traversal depth (default 2)", "default": 2},
            },
            "required": ["root_id"],
        },
    },
    {
        "name": "kg_propose_link",
        "description": (
            "Propose a new cross-database entity link for human review. "
            "The link is recorded as a candidate edge with confidence < 1.0 and "
            "does NOT automatically modify the Knowledge Graph."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "src_id":     {"type": "string", "description": "Source entity node ID"},
                "tgt_id":     {"type": "string", "description": "Target entity node ID"},
                "relation":   {"type": "string", "description": "Proposed relation type"},
                "confidence": {"type": "number", "description": "Confidence score 0-1", "default": 0.7},
                "actor_id":   {"type": "string", "description": "Agent or user proposing the link"},
            },
            "required": ["src_id", "tgt_id", "relation"],
        },
    },
]


# ── Tool implementations ───────────────────────────────────────────────────────

def tool_kg_search(
    query: str,
    layer: str = "schema",
    limit: int = 20,
    mode: str = "hybrid",
) -> Dict[str, Any]:
    """Execute a KG search and return serialisable results."""
    try:
        from .kg_search import hybrid_search, keyword_search, semantic_search

        if mode == "keyword":
            hits = keyword_search(query, layer=layer, limit=limit)
        elif mode == "semantic":
            hits = semantic_search(query, layer=layer, limit=limit)
        else:
            hits = hybrid_search(query, layer=layer, limit=limit)

        return {
            "ok": True,
            "count": len(hits),
            "results": [
                {
                    "source_key":  getattr(h, "source_key", ""),
                    "table_name":  getattr(h, "table_name", ""),
                    "column_name": getattr(h, "column_name", ""),
                    "score":       round(getattr(h, "score", 0.0), 6),
                    "snippet":     getattr(h, "snippet", ""),
                    "layer":       getattr(h, "layer", layer),
                }
                for h in hits
            ],
        }
    except Exception as exc:
        logger.warning(
            "kg_mcp tool_kg_search failed",
            extra={"query": query[:80], "error": str(exc), "outcome": "error"},
        )
        return {"ok": False, "error": str(exc), "results": []}


def tool_kg_resolve(
    source_key: str,
    table: str,
    pk_col: str,
    pk_val: str,
    depth: int = 2,
) -> Dict[str, Any]:
    """Resolve an entity and return its FK neighbourhood."""
    try:
        from .kg_resolver import resolve_entity
        nodes, edges = resolve_entity(source_key, table, pk_col, pk_val, depth=depth)
        return {
            "ok": True,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "nodes": [
                {
                    "id":          n.id,
                    "label":       n.label,
                    "entity_type": n.entity_type,
                    "source_key":  n.source_key,
                    "table":       n.table,
                }
                for n in nodes
            ],
            "edges": [
                {
                    "src_id":     e.src_id,
                    "tgt_id":     e.tgt_id,
                    "relation":   e.relation,
                    "label":      e.label,
                    "confidence": e.confidence,
                }
                for e in edges
            ],
        }
    except Exception as exc:
        logger.warning(
            "kg_mcp tool_kg_resolve failed",
            extra={"source_key": source_key, "error": str(exc), "outcome": "error"},
        )
        return {"ok": False, "error": str(exc), "nodes": [], "edges": []}


def tool_kg_subgraph(
    root_id: str,
    depth: int = 2,
    graph=None,
) -> Dict[str, Any]:
    """Return a depth-limited subgraph centred on root_id."""
    if graph is None:
        return {"ok": False, "error": "No graph provided", "elements": {"nodes": [], "edges": []}}
    try:
        sub = graph.subgraph(root_id, depth=depth)
        cy = sub.to_cytoscape_json()
        return {"ok": True, **cy}
    except Exception as exc:
        logger.warning(
            "kg_mcp tool_kg_subgraph failed",
            extra={"root_id": root_id, "error": str(exc), "outcome": "error"},
        )
        return {"ok": False, "error": str(exc), "elements": {"nodes": [], "edges": []}}


def tool_kg_propose_link(
    src_id: str,
    tgt_id: str,
    relation: str,
    confidence: float = 0.7,
    actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a candidate cross-DB entity link proposal for human review."""
    try:
        from .kg_store import _open_store_raw

        # source_confidence=0.7 marks proposed nodes as LLM-originating so that
        # remove_nodes_by_source() can preserve them when externally referenced.
        # MARKER:kg-source-confidence
        proposal = {
            "src_id":            src_id,
            "tgt_id":            tgt_id,
            "relation":          relation,
            "confidence":        confidence,
            "source_confidence": 0.7,
            "actor_id":          actor_id,
            "status":            "pending_review",
        }

        # Store as a data doc in the KG store under a special "proposals" source.
        # cell_value carries the serialised proposal; full_text is derived internally.
        with _open_store_raw() as store:
            store.data_doc_upsert(
                source_key = "__proposals__",
                table_name = "link_proposals",
                pk_col     = "id",
                pk_val     = f"{src_id}::{tgt_id}::{relation}",
                col_name   = "proposal",
                cell_value = str(proposal),
            )

        logger.info(
            "Link proposal recorded",
            extra={
                "src_id":   src_id,
                "tgt_id":   tgt_id,
                "relation": relation,
                "actor_id": actor_id,
                "outcome":  "ok",
            },
        )
        return {"ok": True, "proposal": proposal}
    except Exception as exc:
        logger.warning(
            "kg_mcp tool_kg_propose_link failed",
            extra={"src_id": src_id, "tgt_id": tgt_id, "error": str(exc), "outcome": "error"},
        )
        return {"ok": False, "error": str(exc)}


def get_tools() -> List[Dict[str, Any]]:
    """Return the list of MCP tool definitions for this KG bridge."""
    return KG_TOOLS


def dispatch_tool(name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a named tool call to its implementation.

    This is the single entry-point for an MCP server host to call.
    """
    handlers = {
        "kg_search":       lambda p: tool_kg_search(**p),
        "kg_resolve":      lambda p: tool_kg_resolve(**p),
        "kg_subgraph":     lambda p: tool_kg_subgraph(**p),
        "kg_propose_link": lambda p: tool_kg_propose_link(**p),
    }
    handler = handlers.get(name)
    if handler is None:
        return {"ok": False, "error": f"Unknown tool: {name!r}"}
    try:
        return handler(params)
    except TypeError as exc:
        return {"ok": False, "error": f"Invalid parameters for {name!r}: {exc}"}
