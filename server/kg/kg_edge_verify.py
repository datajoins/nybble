"""server/kg/kg_edge_verify.py - Post-index cross-source FK edge verification.

Mirrors UA ``assemble-reviewer.md`` cross-batch edge gap fill.

After the mechanical indexing pass, FK references between tables in different
source keys may produce no ``cross_db`` edges in the EntityGraph (because edges
are normally constructed lazily at query time by ``kg_resolver.resolve_entity``).
This module provides a one-pass verification function that proactively checks
all FK maps for cross-source references and adds missing ``cross_db`` edges.

Intended usage: CI pipelines and development tooling.  NOT intended for the
production Flask request path (the full FK-map scan is O(FK-maps × tables) in
memory and should not run on every HTTP request).

Public API
----------
  EdgeVerifyResult        - Dataclass: edges_added, edges_already_present,
                            pairs_checked, added_edges
  verify_cross_source_fk_edges(graph, registry) → EdgeVerifyResult

Zero new ``[dependencies]`` entries - uses only:
  - ``networkx`` (already in ``[dependencies]``)
  - ``kg_resolver._load_fk_map`` (already in the codebase)
  - Python stdlib: logging, dataclasses, typing, os

# MARKER:kg-cross-source-edge-verify
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class EdgeVerifyResult:
    """Summary of a cross-source FK edge verification pass.

    Attributes
    ----------
    edges_added : int
        Number of ``cross_db`` edges added to the graph (were absent).
    edges_already_present : int
        Number of FK pairs that already had a corresponding edge (no action).
    pairs_checked : int
        Total FK (from_table, from_col, to_table) triples examined.
    added_edges : list of (src_node_id, tgt_node_id, relation) tuples
        Details of every edge that was added.  Empty when ``edges_added == 0``.
    """
    edges_added:          int = 0
    edges_already_present: int = 0
    pairs_checked:        int = 0
    added_edges: List[Tuple[str, str, str]] = field(default_factory=list)


# ── Public API ────────────────────────────────────────────────────────────────

def verify_cross_source_fk_edges(
    graph,
    registry: Dict,
) -> EdgeVerifyResult:
    """Check all cross-source FK references and add missing ``cross_db`` edges.

    Mirrors UA ``assemble-reviewer.md`` cross-batch edge gap fill.

    For each FK column in every ready source's FK map, checks whether the
    referenced table lives in a *different* source key.  If a ``cross_db`` edge
    from the source node to the target node does not already exist in *graph*,
    one is added.

    Parameters
    ----------
    graph    : EntityGraph
        The in-memory entity graph to inspect and update.
    registry : dict
        The loaded ``registry.json`` dict: ``{source_key: {"status": "ready", …}}``.
        Only entries with ``status == "ready"`` are processed.

    Returns
    -------
    EdgeVerifyResult
        Summary including counts of edges added, already-present, pairs checked,
        and a list of added ``(src_node_id, tgt_node_id, relation)`` tuples.

    Notes
    -----
    - The function does NOT add nodes; it only adds edges between nodes that
      already exist in *graph*.  If either endpoint is absent from the graph, the
      edge is silently skipped (logged at DEBUG level).
    - This is intentionally conservative: only FK-backed edges from
      ``schema.json`` are added, never speculative edges.
    """
    from .kg_resolver import _load_fk_map, EntityEdge

    result = EdgeVerifyResult()

    if not registry:
        logger.debug("verify_cross_source_fk_edges: empty registry - nothing to do")
        return result

    ready_keys: List[str] = [
        k for k, v in registry.items()
        if isinstance(v, dict) and v.get("status") == "ready"
    ]

    if not ready_keys:
        logger.debug("verify_cross_source_fk_edges: no ready sources in registry")
        return result

    # Build a mapping: table_name → [source_keys that contain this table]
    # (used to find which source a referenced table belongs to)
    table_to_sources: Dict[str, List[str]] = {}
    for key in ready_keys:
        fk_map = _load_fk_map(key)
        for table_name in fk_map:
            table_to_sources.setdefault(table_name, [])
            if key not in table_to_sources[table_name]:
                table_to_sources[table_name].append(key)

    # Determine which node IDs are present in the graph (to skip missing endpoints)
    G = getattr(graph, "_G", None)

    for src_key in ready_keys:
        fk_map = _load_fk_map(src_key)
        for from_table, fk_list in fk_map.items():
            for (from_col, to_table, to_col) in fk_list:
                result.pairs_checked += 1

                # Only care about FK references to tables in OTHER source keys
                tgt_keys = [
                    k for k in table_to_sources.get(to_table, [])
                    if k != src_key
                ]
                if not tgt_keys:
                    continue  # same-source FK or unregistered table - skip

                for tgt_key in tgt_keys:
                    src_node_id = f"{src_key}:{from_table}"
                    tgt_node_id = f"{tgt_key}:{to_table}"

                    # Skip if either endpoint is not in the graph
                    if G is not None:
                        if not G.has_node(src_node_id) or not G.has_node(tgt_node_id):
                            logger.debug(
                                "verify_cross_source_fk_edges: endpoint absent - skipping",
                                extra={
                                    "src_node_id": src_node_id,
                                    "tgt_node_id": tgt_node_id,
                                    "outcome": "skip",
                                },
                            )
                            continue
                        # Check if cross_db edge already exists
                        if G.has_edge(src_node_id, tgt_node_id):
                            result.edges_already_present += 1
                            continue
                    else:
                        # dict-fallback graph: skip endpoint-presence check
                        nodes = getattr(graph, "_nodes", {})
                        if src_node_id not in nodes or tgt_node_id not in nodes:
                            continue
                        edges = getattr(graph, "_edges", [])
                        if any(u == src_node_id and v == tgt_node_id for u, v, _ in edges):
                            result.edges_already_present += 1
                            continue

                    # Add the missing cross_db edge
                    relation = f"cross_db:{from_col}"
                    label = f"FK {from_table}.{from_col} → {to_table}.{to_col}"
                    edge = EntityEdge(
                        src_id=src_node_id,
                        tgt_id=tgt_node_id,
                        relation=relation,
                        label=label,
                        weight=0.8,
                        confidence=0.9,
                    )
                    graph.add_edge(edge)
                    result.edges_added += 1
                    result.added_edges.append((src_node_id, tgt_node_id, relation))
                    logger.debug(
                        "verify_cross_source_fk_edges: added cross_db edge",
                        extra={
                            "src_node_id": src_node_id,
                            "tgt_node_id": tgt_node_id,
                            "relation":    relation,
                            "outcome":     "added",
                        },
                    )

    logger.info(
        "Cross-source FK edge verification complete",
        extra={
            "edges_added":          result.edges_added,
            "edges_already_present": result.edges_already_present,
            "pairs_checked":        result.pairs_checked,
            "outcome":              "ok",
        },
    )
    return result
