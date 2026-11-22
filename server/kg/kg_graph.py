"""Server-side Knowledge Graph builder and serialisers for the Nybble Flask application.
The pip-installable equivalent is ``nybble.kg`` (marketed as Nybble GraphAI).
Do not import this file from the ``nybble`` package - use ``nybble.kg`` instead.

kg_graph.py - Knowledge Graph Builder + Serialisers (Sub-Task 6)

Converts EntityNode + EntityEdge lists (from kg_resolver) into a NetworkX
DiGraph, then serialises to Cytoscape.js JSON (same format as erd.html),
JSON-LD, or CSV edge list.

Graph backend env var
---------------------
  GRAPH_BACKEND=networkx  (default) - uses this module
  GRAPH_BACKEND=kuzu      - stub; raises NotImplementedError (see SCALING.md)

Public API
----------
  EntityGraph.add_node(entity_node)
  EntityGraph.add_edge(entity_edge)
  EntityGraph.to_cytoscape_json()
  EntityGraph.to_jsonld()
  EntityGraph.to_csv_edgelist()
  EntityGraph.subgraph(root_id, depth)
  EntityGraph.community_detect()

  build_graph(nodes, edges) → EntityGraph
  to_cytoscape_json(G)      → dict
"""
from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ── Backend selection ─────────────────────────────────────────────────────────
GRAPH_BACKEND = os.environ.get("GRAPH_BACKEND", "networkx").lower()


# ── Complexity normalisation ──────────────────────────────────────────────────
#
# MARKER:kg-numeric-complexity-normalise
#
# Ported from UA normalize-graph.ts normalizeComplexity().
# String aliases cover all mappings from the prior P1-B roadmap (not yet wired
# in the codebase) plus the new aliases added in the current roadmap.
#
_COMPLEXITY_ALIASES: Dict[str, str] = {
    # "simple" family
    "low":        "simple",
    "easy":       "simple",
    "trivial":    "simple",
    "basic":      "simple",
    # "moderate" family
    "medium":     "moderate",
    "intermediate": "moderate",
    "mid":        "moderate",
    "average":    "moderate",
    # "complex" family
    "high":       "complex",
    "hard":       "complex",
    "difficult":  "complex",
    "advanced":   "complex",
}


def _normalise_complexity(value) -> object:
    """Normalise a complexity value to 'simple', 'moderate', or 'complex'.

    Handles:
    - Numeric scale 1-10: ≤3 → 'simple', ≤6 → 'moderate', >6 → 'complex'
      (mirrors UA normalize-graph.ts normalizeComplexity numeric branch)
    - String aliases: looked up in _COMPLEXITY_ALIASES
    - Already-canonical strings ('simple', 'moderate', 'complex'): passed through
    - Anything else: returned unchanged

    Parameters
    ----------
    value : int | float | str | Any
        Raw complexity value from a node attribute or row_dict.

    Returns
    -------
    str | original type
        Normalised canonical string, or *value* unchanged if not recognised.
    """
    # Numeric scale (int or float, but not bool)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        if v >= 1:
            if v <= 3:
                return "simple"
            if v <= 6:
                return "moderate"
            return "complex"
        # Out-of-range numeric (< 1): return unchanged
        return value

    # String alias lookup
    if isinstance(value, str):
        return _COMPLEXITY_ALIASES.get(value.lower().strip(), value)

    return value


# ── Display-name title-casing ─────────────────────────────────────────────────
#
# MARKER:kg-display-name
#
# Ported from ai-knowledge-graph visualization.py display_name().
# Applied to node labels in add_node() to improve KG visualisation readability.
#
_DISPLAY_SMALL_WORDS = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from",
    "in", "of", "on", "or", "the", "to", "via", "with",
})


def display_name(name: str) -> str:
    """Apply title-casing to an entity label for display.

    Rules (mirrors ai-knowledge-graph visualization.py display_name):

    - Names already containing uppercase letters are returned unchanged
      (preserves proper nouns and acronyms like "IBM", "CRM_Contacts").
    - Underscores are replaced with spaces.
    - Words containing digits are capitalised per hyphen-separated component
      (e.g. ``"mid-20th"`` → ``"Mid-20th"``).
    - Small words (``a``, ``an``, ``and``, ``as``, ``at``, ``by``, ``for``,
      ``from``, ``in``, ``of``, ``on``, ``or``, ``the``, ``to``, ``via``,
      ``with``) are lowercase except when they are the first word.
    - Empty string is returned unchanged.

    Examples::

        display_name("steam engine")      → "Steam Engine"
        display_name("internet of things") → "Internet of Things"
        display_name("user_id")           → "User Id"
        display_name("IBM")               → "IBM"
        display_name("mid-20th century")  → "Mid-20th Century"
    """
    if not name:
        return name
    # Already mixed-case → preserve as-is (proper nouns, acronyms)
    if name != name.lower():
        return name
    # Replace underscores with spaces for display
    display = name.replace("_", " ")

    def _cap_word(word: str) -> str:
        # Capitalise each hyphen-separated component unless it contains a digit
        return "-".join(
            part if any(ch.isdigit() for ch in part) else (part[:1].upper() + part[1:])
            for part in word.split("-")
        )

    words = display.split()
    return " ".join(
        _cap_word(w) if i == 0 or w.lower() not in _DISPLAY_SMALL_WORDS else w
        for i, w in enumerate(words)
    )


# ── Relation-type normalisation ───────────────────────────────────────────────
#
# MARKER:kg-relation-type-normalise
#
# Ported from ai-knowledge-graph entity_standardization.py normalize_predicates().
# Maps known surface-form variants of edge relation types to a canonical form,
# preventing FK/fk/is_fk proliferation from inflating NetworkX edge-type counts.
#
_RELATION_NORMALISER: Dict[str, str] = {
    # FK family
    "fk":                "FK",
    "is_fk":             "FK",
    "foreign_key":       "FK",
    "foreign key":       "FK",
    "references":        "FK",
    # cross-DB family
    "cross_db_fk":       "cross_db",
    "cross db":          "cross_db",
    "cross-db":          "cross_db",
    # similarity family
    "similar":           "similar_to",
    "similar to":        "similar_to",
    "is similar to":     "similar_to",
    # related family
    "related":           "related_to",
    "is related to":     "related_to",
    "relates to":        "related_to",
}


def _normalise_relation(relation: str) -> str:
    """Normalise an edge relation string to its canonical form.

    Unknown relations are returned with only leading/trailing whitespace stripped.
    Mirrors ai-knowledge-graph entity_standardization.py normalize_predicates().

    # MARKER:kg-relation-type-normalise
    """
    if not isinstance(relation, str):
        return relation
    stripped = relation.strip()
    return _RELATION_NORMALISER.get(stripped.lower(), stripped)


# ── Entity type → colour mapping (mirrors erd.html design tokens) ─────────────
_TYPE_COLORS: Dict[str, str] = {
    "TABLE":   "#3b82d4",   # accent blue   — schema table nodes
    "COLUMN":  "#7c5cd8",   # purple        — column nodes in data-graph mode
    "PERSON":  "#1aad8d",   # teal          — extracted person entities
    "ORG":     "#e66d00",   # orange        — extracted organisation entities
    "EMAIL":   "#22863a",   # green         — email addresses
    "DATE":    "#6a737d",   # muted grey    — date values
    "URL":     "#0366d6",   # link blue     — URL values
    "ID":      "#735c0f",   # brown         — identifier values
    "NUM":     "#586069",   # grey          — numeric values
    "ENTITY":  "#e8963a",   # amber/orange  — data-graph ENTITY value nodes
                            #   (distinct from TABLE blue so type-coloring is visible)
    "UNKNOWN": "#586069",   # muted         — fallback
}

_SOURCE_PALETTE = [
    "#3b82d4", "#7c5cd8", "#e66d00", "#22863a",
    "#0366d6", "#735c0f", "#d73a49", "#6f42c1",
]


def _source_color(source_key: str) -> str:
    idx = hash(source_key) % len(_SOURCE_PALETTE)
    return _SOURCE_PALETTE[idx]


# ── Graph metric helpers ──────────────────────────────────────────────────────

def _safe_pagerank(G) -> dict:
    """Run PageRank, returning uniform fallback on convergence failure."""
    try:
        import networkx as nx
        return nx.pagerank(G, alpha=0.85, max_iter=200)
    except Exception:
        n = G.number_of_nodes()
        uniform = 1.0 / n if n else 0.0
        return {nid: uniform for nid in G.nodes()}


def _compute_depths(G) -> dict:
    """Assign topological depth to each node (max distance from any root).

    Roots are nodes with no incoming edges (in-degree == 0).
    Works on DAGs; cycles are handled gracefully via a visited guard.

    # MARKER:kg-depths-root-memoized
    """
    depth: Dict[str, int] = {}  # absence = uncomputed; distinguishes 0 from "not visited"
    computing: set = set()

    def dfs(nid: str) -> int:
        if nid in depth:            # already computed (even if 0)
            return depth[nid]
        if nid in computing:
            return 0                # cycle guard
        computing.add(nid)
        parents = list(G.predecessors(nid))
        depth[nid] = 0 if not parents else max(dfs(p) for p in parents) + 1
        computing.discard(nid)
        return depth[nid]

    for node in list(G.nodes()):
        dfs(node)
    return depth


# ── EntityGraph ────────────────────────────────────────────────────────────────

class EntityGraph:
    """NetworkX-backed entity graph with Cytoscape / JSON-LD / CSV serialisation."""

    def __init__(self) -> None:
        if GRAPH_BACKEND == "kuzu":
            raise NotImplementedError(
                "KùzuDB graph backend is not yet implemented as a drop-in for EntityGraph. "
                "Use KuzuGraph directly, or install `pip install 'datajoins-nybble[kuzu]'`. "
                "See SCALING.md § Tier 2 for upgrade instructions."
            )
        try:
            import networkx as nx
            self._G = nx.DiGraph()
        except ImportError:
            # Fallback: plain dict-based graph when networkx not installed
            self._G = None
            self._nodes: Dict[str, Any] = {}
            self._edges: List[tuple]    = []

    # ── Node / edge management ────────────────────────────────────────────────

    def add_node(self, node) -> None:
        """Add an EntityNode to the graph.

        The node's ``row_dict`` is inspected for:
        - ``complexity``: normalised via ``_normalise_complexity()`` before storage.
        - ``source_confidence``: float in [0, 1] indicating extraction confidence.
          ``1.0`` (deterministic / structurally extracted) is the default;
          LLM-proposed nodes should pass ``0.7`` via ``row_dict``.

        # MARKER:kg-source-confidence
        """
        row = node.row_dict or {}
        attrs = {
            # display_name() applies title-casing for improved readability.
            # MARKER:kg-display-name
            "label":       display_name(node.label),
            "entity_type": node.entity_type,
            "source_key":  node.source_key,
            "table":       node.table,
            "pk_col":      node.pk_col,
            "pk_val":      node.pk_val,
            "color":       _TYPE_COLORS.get(node.entity_type, "#586069"),
            "source_color": _source_color(node.source_key),
            "row_dict":    node.row_dict,
            # Source-confidence: defaults to 1.0 (deterministic extraction).
            # Set to < 1.0 for nodes proposed by LLMs / external agents so that
            # remove_nodes_by_source() can preserve them when externally referenced.
            "source_confidence": float(row.get("source_confidence", 1.0)),
        }
        # Normalise complexity if present in row_dict
        raw_complexity = row.get("complexity")
        if raw_complexity is not None:
            attrs["complexity"] = _normalise_complexity(raw_complexity)
        if self._G is not None:
            self._G.add_node(node.id, **attrs)
        else:
            self._nodes[node.id] = attrs

    def add_edge(self, edge) -> None:
        """Add an EntityEdge to the graph.

        The edge's ``relation`` is normalised via ``_normalise_relation()``
        before storage to prevent FK/fk/is_fk proliferation.
        # MARKER:kg-relation-type-normalise
        """
        attrs = {
            "relation":   _normalise_relation(edge.relation),
            "label":      edge.label,
            "weight":     edge.weight,
            "confidence": edge.confidence,
        }
        if self._G is not None:
            self._G.add_edge(edge.src_id, edge.tgt_id, **attrs)
        else:
            self._edges.append((edge.src_id, edge.tgt_id, attrs))

    @property
    def node_count(self) -> int:
        if self._G is not None:
            return self._G.number_of_nodes()
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        if self._G is not None:
            return self._G.number_of_edges()
        return len(self._edges)

    # ── Cytoscape.js JSON ─────────────────────────────────────────────────────

    def to_cytoscape_json(self) -> dict:
        """Return Cytoscape.js elements dict compatible with kg.html format.

        Node data schema:
          {id, label, entity_type, source_key, table, pk_col, pk_val, color,
           source_color, category, depth, ancestor_count, descendant_count,
           _pagerank, _degree_centrality, _betweenness_centrality,
           _descendant_ratio, _prerequisite_ratio, _reachability_ratio}

        Edge data schema:
          {id, source, target, relation, label, weight, confidence}
        """
        cy_nodes = []
        cy_edges = []

        if self._G is not None:
            import networkx as nx

            n_nodes = self._G.number_of_nodes()

            # ── Pre-compute graph metrics (P2-B) ──────────────────────────────
            # All metrics are O(V+E) or O(V·E); skip if graph is empty.
            if n_nodes > 0:
                pr_map  = _safe_pagerank(self._G)
                dc_map  = nx.degree_centrality(self._G)
                # Betweenness: expensive on large graphs - use approximation
                # when node count > 300 to keep indexing fast.
                if n_nodes <= 300:
                    bc_map = nx.betweenness_centrality(self._G, normalized=True)
                else:
                    bc_map = nx.betweenness_centrality(
                        self._G, k=min(100, n_nodes), normalized=True
                    )
                depth_map = _compute_depths(self._G)
            else:
                pr_map = dc_map = bc_map = depth_map = {}

            # Category mapping: entity_type is used as category fallback.
            # If the graph has an explicit "category" attribute use it;
            # otherwise fall back to entity_type.
            for nid, attrs in self._G.nodes(data=True):
                rd = attrs.get("row_dict") or {}

                # Reachability metrics
                if n_nodes > 1:
                    desc_count = len(nx.descendants(self._G, nid))
                    anc_count  = len(nx.ancestors(self._G, nid))
                    desc_ratio = desc_count / (n_nodes - 1)
                    anc_ratio  = anc_count  / (n_nodes - 1)
                    reach_ratio = (desc_ratio + anc_ratio) / 2.0
                else:
                    desc_count = anc_count = 0
                    desc_ratio = anc_ratio = reach_ratio = 0.0

                category = attrs.get("category") or attrs.get("entity_type", "UNKNOWN")

                cy_nodes.append({
                    "data": {
                        "id":           nid,
                        "label":        attrs.get("label", nid),
                        "entity_type":  attrs.get("entity_type", "UNKNOWN"),
                        "category":     category,
                        "source_key":   attrs.get("source_key", ""),
                        "table":        attrs.get("table", ""),
                        "pk_col":       attrs.get("pk_col", ""),
                        "pk_val":       attrs.get("pk_val", ""),
                        "color":        attrs.get("color", "#586069"),
                        "source_color": attrs.get("source_color", "#3b82d4"),
                        # Topology
                        "depth":            depth_map.get(nid, 0),
                        "ancestor_count":   anc_count,
                        "descendant_count": desc_count,
                        # Graph metrics (P2-B)
                        "_pagerank":               round(pr_map.get(nid, 0.0), 8),
                        "_degree_centrality":      round(dc_map.get(nid, 0.0), 8),
                        "_betweenness_centrality": round(bc_map.get(nid, 0.0), 8),
                        "_descendant_ratio":       round(desc_ratio,  8),
                        "_prerequisite_ratio":     round(anc_ratio,   8),
                        "_reachability_ratio":     round(reach_ratio, 8),
                        # Detail-drawer extras (only set when present)
                        "row_dict": {
                            k: rd[k] for k in (
                                "node_kind", "column", "value", "count",
                                "data_type", "is_pk",
                                "columns", "col_counts",  # merged entity fields
                            ) if k in rd
                        },
                    }
                })
            for i, (src, tgt, attrs) in enumerate(self._G.edges(data=True)):
                cy_edges.append({
                    "data": {
                        "id":         f"e{i}",
                        "source":     src,
                        "target":     tgt,
                        "relation":   attrs.get("relation", ""),
                        "label":      attrs.get("label", ""),
                        "weight":     attrs.get("weight", 1.0),
                        "confidence": attrs.get("confidence", 1.0),
                    }
                })
        else:
            # Fallback dict-based (no networkx - metrics unavailable)
            for nid, attrs in self._nodes.items():
                cy_nodes.append({"data": {"id": nid, **attrs}})
            for i, (src, tgt, attrs) in enumerate(self._edges):
                cy_edges.append({"data": {"id": f"e{i}", "source": src, "target": tgt, **attrs}})

        return {"elements": {"nodes": cy_nodes, "edges": cy_edges}}

    # ── JSON-LD ────────────────────────────────────────────────────────────────

    def to_jsonld(self) -> dict:
        """Return a JSON-LD document using schema.org vocabulary.

        Includes all node properties (pk_col, pk_val, row_dict fields) and all
        edge properties (relation, label, weight, confidence) so that the full
        graph - schema nodes, data nodes, column nodes, value nodes and every
        edge between them - is represented in the export.
        """
        _TYPE_MAP = {
            "PERSON":   "schema:Person",
            "ORG":      "schema:Organization",
            "DATE":     "schema:Date",
            "URL":      "schema:URL",
            "TABLE":    "schema:Dataset",
            "COLUMN":   "kg:Column",
            "ROW":      "kg:Row",
            "VALUE":    "kg:Value",
            "UNKNOWN":  "schema:Thing",
        }

        graph = []
        if self._G is not None:
            nodes_iter = list(self._G.nodes(data=True))
            edges_iter = list(self._G.edges(data=True))
        else:
            nodes_iter = list(self._nodes.items())
            edges_iter = [(s, t, a) for s, t, a in self._edges]

        for nid, attrs in nodes_iter:
            etype    = attrs.get("entity_type", "UNKNOWN")
            sch_type = _TYPE_MAP.get(etype, "schema:Thing")
            rd       = attrs.get("row_dict") or {}

            entry: Dict[str, Any] = {
                "@id":               f"urn:kg:{nid}",
                "@type":             sch_type,
                "schema:name":       attrs.get("label", nid),
                "schema:identifier": nid,
                "kg:sourceKey":      attrs.get("source_key", ""),
                "kg:tableName":      attrs.get("table", ""),
                "kg:entityType":     etype,
            }

            # pk fields (present on data-mode nodes)
            if attrs.get("pk_col"):
                entry["kg:pkColumn"] = attrs["pk_col"]
            if attrs.get("pk_val") not in (None, ""):
                entry["kg:pkValue"] = str(attrs["pk_val"])

            # node_kind discriminator (table / column / row / value)
            if rd.get("node_kind"):
                entry["kg:nodeKind"] = rd["node_kind"]

            # column-node extras
            if rd.get("data_type"):
                entry["kg:dataType"] = rd["data_type"]
            if "is_pk" in rd:
                entry["kg:isPrimaryKey"] = bool(rd["is_pk"])
            if rd.get("column"):
                entry["kg:columnName"] = rd["column"]

            # value-node extras
            if "value" in rd:
                entry["schema:value"] = rd["value"]
            if "count" in rd:
                entry["kg:valueCount"] = rd["count"]

            # merged-entity extras
            if rd.get("columns"):
                entry["kg:mergedColumns"] = rd["columns"]
            if rd.get("col_counts"):
                entry["kg:columnValueCounts"] = rd["col_counts"]

            # graph-metric extras (pagerank, centrality - included when present)
            for metric_key in (
                "source_confidence",
                "_pagerank", "_degree_centrality", "_betweenness_centrality",
            ):
                v = attrs.get(metric_key)
                if v is not None:
                    entry[f"kg:{metric_key.lstrip('_')}"] = v

            graph.append(entry)

        for src, tgt, attrs in edges_iter:
            graph.append({
                "@type":         "kg:Edge",
                "kg:source":     f"urn:kg:{src}",
                "kg:target":     f"urn:kg:{tgt}",
                "kg:relation":   attrs.get("relation", "related"),
                "schema:name":   attrs.get("label", ""),
                "kg:weight":     attrs.get("weight", 1.0),
                "kg:confidence": attrs.get("confidence", 1.0),
            })

        return {
            "@context": {
                "schema":  "https://schema.org/",
                "kg":      "https://github.com/datajoins/nybble/kg#",
                # kg: term definitions so consumers can dereference them
                "kg:sourceKey":          {"@type": "xsd:string"},
                "kg:tableName":          {"@type": "xsd:string"},
                "kg:entityType":         {"@type": "xsd:string"},
                "kg:nodeKind":           {"@type": "xsd:string"},
                "kg:pkColumn":           {"@type": "xsd:string"},
                "kg:pkValue":            {"@type": "xsd:string"},
                "kg:dataType":           {"@type": "xsd:string"},
                "kg:isPrimaryKey":       {"@type": "xsd:boolean"},
                "kg:columnName":         {"@type": "xsd:string"},
                "kg:valueCount":         {"@type": "xsd:integer"},
                "kg:relation":           {"@type": "xsd:string"},
                "kg:weight":             {"@type": "xsd:decimal"},
                "kg:confidence":         {"@type": "xsd:decimal"},
                "kg:source":             {"@type": "@id"},
                "kg:target":             {"@type": "@id"},
                "kg:pagerank":           {"@type": "xsd:decimal"},
                "kg:degree_centrality":  {"@type": "xsd:decimal"},
                "xsd":                   "http://www.w3.org/2001/XMLSchema#",
            },
            "@graph": graph,
        }

    # ── CSV edge list ──────────────────────────────────────────────────────────

    def to_csv_edgelist(self) -> str:
        """Return CSV string with two sections.

        Section 1 - Nodes: id, label, entity_type, source_key, table,
                           pk_col, pk_val, node_kind, data_type, is_pk
        Section 2 - Edges: src_id, src_label, tgt_id, tgt_label,
                           relation, label, weight, confidence

        Isolated nodes (no edges) are still exported in the nodes section.
        An empty line and a "## edges" comment separate the two sections so
        the file is both human-readable and machine-parseable.
        """
        buf = io.StringIO()
        writer = csv.writer(buf)

        # ── helper: get attrs dict regardless of backend ──────────────────────
        def _node_attrs(nid):
            if self._G is not None:
                return self._G.nodes[nid] if nid in self._G.nodes else {}
            return self._nodes.get(nid, {})

        # ── Section 1: nodes ──────────────────────────────────────────────────
        writer.writerow(["## nodes"])
        writer.writerow([
            "id", "label", "entity_type", "source_key", "table",
            "pk_col", "pk_val", "node_kind", "data_type", "is_pk",
        ])
        if self._G is not None:
            nodes_iter = self._G.nodes(data=True)
        else:
            nodes_iter = self._nodes.items()

        for nid, attrs in nodes_iter:
            rd = attrs.get("row_dict") or {}
            writer.writerow([
                nid,
                attrs.get("label", nid),
                attrs.get("entity_type", ""),
                attrs.get("source_key", ""),
                attrs.get("table", ""),
                attrs.get("pk_col", ""),
                attrs.get("pk_val", ""),
                rd.get("node_kind", ""),
                rd.get("data_type", ""),
                rd.get("is_pk", ""),
            ])

        # ── Section 2: edges ──────────────────────────────────────────────────
        buf.write("\n")
        writer.writerow(["## edges"])
        writer.writerow([
            "src_id", "src_label", "tgt_id", "tgt_label",
            "relation", "label", "weight", "confidence",
        ])
        if self._G is not None:
            edges_iter = self._G.edges(data=True)
        else:
            edges_iter = self._edges

        for src, tgt, attrs in edges_iter:
            src_label = _node_attrs(src).get("label", src)
            tgt_label = _node_attrs(tgt).get("label", tgt)
            writer.writerow([
                src, src_label,
                tgt, tgt_label,
                attrs.get("relation", ""),
                attrs.get("label", ""),
                attrs.get("weight", 1.0),
                attrs.get("confidence", 1.0),
            ])

        return buf.getvalue()

    # ── CSR/CSC adjacency index (P2-C) ────────────────────────────────────────

    def _build_index(self) -> None:
        """Build forward/reverse adjacency lists from the NetworkX graph.
        # MARKER:kg-graph-csr-adjacency
        """
        self._fwd: Dict[str, List[str]] = {}
        self._bwd: Dict[str, List[str]] = {}
        if self._G is None:
            return
        for src, tgt in self._G.edges():
            self._fwd.setdefault(src, []).append(tgt)
            self._bwd.setdefault(tgt, []).append(src)

    # ── Subgraph ───────────────────────────────────────────────────────────────

    def subgraph(self, root_id: str, depth: int = 2) -> "EntityGraph":
        """Return a new EntityGraph containing only the depth-limited neighbourhood."""
        if self._G is None:
            return self  # can't do BFS without networkx
        if not hasattr(self, '_fwd'):
            self._build_index()
        visited = set()
        queue = [(root_id, 0)]
        while queue:
            nid, d = queue.pop(0)
            if nid in visited:
                continue
            visited.add(nid)
            if d < depth:
                for nbr in self._fwd.get(nid, []):
                    if nbr not in visited:
                        queue.append((nbr, d + 1))
                for nbr in self._bwd.get(nid, []):
                    if nbr not in visited:
                        queue.append((nbr, d + 1))
        sub_nx = self._G.subgraph(visited).copy()
        sub = EntityGraph.__new__(EntityGraph)
        sub._G = sub_nx
        sub._build_index()
        return sub

    # ── Surgical node excision ────────────────────────────────────────────────

    def remove_nodes_by_source(self, source_key: str) -> List[str]:
        """Remove all nodes whose ``source_key`` attribute equals *source_key*.

        # MARKER:kg-surgical-excision
        # MARKER:kg-source-confidence

        **Source-confidence preservation rule (P2-B):**
        A node matching *source_key* is *preserved* (not removed) when ALL of
        the following hold:
          1. Its ``source_confidence`` attribute is < 1.0 (i.e. it was proposed
             by an LLM / external agent rather than deterministically extracted).
          2. At least one neighbour (predecessor or successor) belongs to a
             *different* source key (external reference).

        Any node matching *source_key* that is NOT preserved is removed.

        The operation is atomic: if any step raises the graph is restored to
        its pre-call state before the exception propagates.

        Returns
        -------
        list[str]
            IDs of the nodes that were removed (excludes preserved nodes).
        """
        if self._G is not None:
            # Partition candidate nodes into to_remove and to_preserve.
            to_remove: List[str] = []
            for nid, attrs in self._G.nodes(data=True):
                if attrs.get("source_key") != source_key:
                    continue
                confidence = attrs.get("source_confidence", 1.0)
                if confidence < 1.0:
                    # Keep uncertain nodes that are externally referenced
                    neighbours = (
                        list(self._G.predecessors(nid))
                        + list(self._G.successors(nid))
                    )
                    referenced_externally = any(
                        self._G.nodes[nbr].get("source_key") != source_key
                        for nbr in neighbours
                        if self._G.has_node(nbr)
                    )
                    if referenced_externally:
                        continue  # preserve
                to_remove.append(nid)

            if not to_remove:
                return []
            # Snapshot for rollback
            saved_nodes = [(nid, dict(self._G.nodes[nid])) for nid in to_remove]
            saved_edges: List[tuple] = []
            for nid in to_remove:
                for u, v, attrs in list(self._G.in_edges(nid, data=True)):
                    saved_edges.append((u, v, dict(attrs)))
                for u, v, attrs in list(self._G.out_edges(nid, data=True)):
                    saved_edges.append((u, v, dict(attrs)))
            try:
                self._G.remove_nodes_from(to_remove)
                # Invalidate adjacency index
                if hasattr(self, '_fwd'):
                    del self._fwd
                if hasattr(self, '_bwd'):
                    del self._bwd
            except Exception:
                # Rollback
                for nid, attrs in saved_nodes:
                    self._G.add_node(nid, **attrs)
                for u, v, attrs in saved_edges:
                    if not self._G.has_edge(u, v):
                        self._G.add_edge(u, v, **attrs)
                raise
            return to_remove
        else:
            # Fallback dict-based graph
            to_remove_dict: List[str] = []
            for nid, attrs in self._nodes.items():
                if attrs.get("source_key") != source_key:
                    continue
                confidence = attrs.get("source_confidence", 1.0)
                if confidence < 1.0:
                    neighbours = [
                        nbr for u, v, _ in self._edges
                        for nbr in ([v] if u == nid else ([u] if v == nid else []))
                    ]
                    referenced_externally = any(
                        self._nodes.get(nbr, {}).get("source_key") != source_key
                        for nbr in neighbours
                    )
                    if referenced_externally:
                        continue  # preserve
                to_remove_dict.append(nid)

            if not to_remove_dict:
                return []
            target_set = set(to_remove_dict)
            saved_nodes_dict = {nid: self._nodes[nid] for nid in to_remove_dict}
            saved_edges = [(u, v, a) for u, v, a in self._edges
                           if u in target_set or v in target_set]
            try:
                for nid in to_remove_dict:
                    del self._nodes[nid]
                self._edges = [(u, v, a) for u, v, a in self._edges
                               if u not in target_set and v not in target_set]
            except Exception:
                self._nodes.update(saved_nodes_dict)
                self._edges.extend(saved_edges)
                raise
            return to_remove_dict

    def remove_edges_for_nodes(self, node_ids: List[str]) -> int:
        """Remove all edges where either endpoint is in *node_ids*.

        # MARKER:kg-surgical-excision

        The operation is atomic: if any step raises the graph is restored to
        its pre-call state before the exception propagates.

        Returns
        -------
        int
            Number of edges removed.
        """
        if not node_ids:
            return 0
        target_set = set(node_ids)

        if self._G is not None:
            edges_to_remove = [
                (u, v)
                for u, v in list(self._G.edges())
                if u in target_set or v in target_set
            ]
            if not edges_to_remove:
                return 0
            saved_edges = [(u, v, dict(self._G[u][v])) for u, v in edges_to_remove]
            try:
                self._G.remove_edges_from(edges_to_remove)
                if hasattr(self, '_fwd'):
                    del self._fwd
                if hasattr(self, '_bwd'):
                    del self._bwd
            except Exception:
                for u, v, attrs in saved_edges:
                    self._G.add_edge(u, v, **attrs)
                raise
            return len(edges_to_remove)
        else:
            before = len(self._edges)
            saved = list(self._edges)
            try:
                self._edges = [(u, v, a) for u, v, a in self._edges
                               if u not in target_set and v not in target_set]
            except Exception:
                self._edges = saved
                raise
            return before - len(self._edges)

    # ── Extended centrality metrics (P2-C extended) ────────────────────────────

    def betweenness_centrality(self, *, normalized: bool = True, k: Optional[int] = None) -> Dict[str, float]:
        """Return betweenness centrality for each node.

        # MARKER:kg-betweenness-centrality

        Uses an approximation when *k* is specified (sample k source nodes for
        speed on large graphs).  Falls back to exact when k is None.

        Parameters
        ----------
        normalized : bool
            Divide raw counts by the normalization factor (default True).
        k : int or None
            If given, approximate via k-sample Brandes algorithm.

        Returns
        -------
        dict[str, float]
            {node_id: betweenness_score}
        """
        if self._G is None or self._G.number_of_nodes() == 0:
            return {}
        import networkx as nx
        if k is not None:
            return nx.betweenness_centrality(self._G, k=k, normalized=normalized)
        return nx.betweenness_centrality(self._G, normalized=normalized)

    def closeness_centrality(self) -> Dict[str, float]:
        """Return closeness centrality for each node.

        # MARKER:kg-closeness-centrality

        Closeness = (n-1) / sum-of-shortest-path-lengths from node to all reachable nodes.
        Implemented via NetworkX ``closeness_centrality`` on the directed graph.
        Isolated nodes (no reachable others) receive score 0.0.

        Returns
        -------
        dict[str, float]
            {node_id: closeness_score}
        """
        if self._G is None or self._G.number_of_nodes() == 0:
            return {}
        import networkx as nx
        return nx.closeness_centrality(self._G)

    # ── Community detection ────────────────────────────────────────────────────

    def community_detect(self) -> Dict[str, int]:
        """Return {node_id: community_id} mapping.

        # MARKER:kg-community-detect-fallback

        Tries three backends in order, falling back gracefully:
          1. python-louvain (``community`` package) - best quality
          2. networkx greedy modularity communities (NetworkX >= 2.4)
          3. Pure-Python label propagation - zero extra dependencies

        The public API (return type, empty-graph handling) is unchanged.
        """
        if self._G is None or self._G.number_of_nodes() == 0:
            return {}

        import networkx as nx
        undirected = self._G.to_undirected()

        # ── Tier 1: python-louvain ────────────────────────────────────────────
        try:
            import community as community_louvain
            return community_louvain.best_partition(undirected)
        except (ImportError, Exception):
            pass

        # ── Tier 2: networkx greedy_modularity_communities ────────────────────
        try:
            from networkx.algorithms.community import greedy_modularity_communities
            parts = greedy_modularity_communities(undirected)
            result: Dict[str, int] = {}
            for cid, component in enumerate(parts):
                for nid in component:
                    result[nid] = cid
            return result
        except (ImportError, Exception):
            pass

        # ── Tier 3: pure-Python label propagation ─────────────────────────────
        return _community_label_propagation(undirected)


# ── Pure-Python community detection fallback ─────────────────────────────────

def _community_label_propagation(G) -> Dict[str, int]:
    """Greedy label-propagation community detection - pure Python, no dependencies.

    Each node starts in its own community.  In each iteration every node adopts
    the most common label among its neighbours (ties broken by lowest label int).
    Convergence is detected when no label changes.  A maximum of 100 iterations
    prevents infinite loops on degenerate graphs.

    Returns {node_id: community_id}.  Empty graph returns {}.
    """
    import random as _random

    nodes = list(G.nodes())
    if not nodes:
        return {}

    # Assign each node a unique integer label
    labels: Dict = {nid: i for i, nid in enumerate(nodes)}

    for _ in range(100):
        changed = False
        # Random order to avoid systematic bias
        shuffled = nodes[:]
        _random.shuffle(shuffled)
        for node in shuffled:
            nbrs = list(G.neighbors(node))
            if not nbrs:
                continue
            # Count label frequencies among neighbours
            freq: Dict[int, int] = {}
            for nbr in nbrs:
                lbl = labels[nbr]
                freq[lbl] = freq.get(lbl, 0) + 1
            # Pick most frequent; break ties by smallest label value
            best_label = min(freq, key=lambda lbl: (-freq[lbl], lbl))
            if labels[node] != best_label:
                labels[node] = best_label
                changed = True
        if not changed:
            break

    # Remap labels to contiguous community IDs
    label_to_cid: Dict[int, int] = {}
    result: Dict = {}
    next_cid = 0
    for nid in nodes:
        lbl = labels[nid]
        if lbl not in label_to_cid:
            label_to_cid[lbl] = next_cid
            next_cid += 1
        result[nid] = label_to_cid[lbl]
    return result


# ── Factory function ──────────────────────────────────────────────────────────

def build_graph(nodes, edges) -> EntityGraph:
    """Build an EntityGraph from lists of EntityNode and EntityEdge objects.

    Only edges whose src_id and tgt_id both exist in the node set are added.
    This prevents NetworkX from auto-creating unlabelled ghost nodes for
    dangling edge references, which would appear as uncoloured, unlabelled
    nodes in Cytoscape.
    """
    g = EntityGraph()
    node_ids: set = set()
    for node in nodes:
        g.add_node(node)
        node_ids.add(node.id)
    for edge in edges:
        if edge.src_id in node_ids and edge.tgt_id in node_ids:
            g.add_edge(edge)
    g._build_index()
    return g


def to_cytoscape_json(g: EntityGraph) -> dict:
    """Convenience wrapper."""
    return g.to_cytoscape_json()


def build_graph_from_schema(schema, fk_candidates, db_path=None, max_rows=5000):
    """Build a NetworkX MultiDiGraph from a core.Schema using the shared builder.

    This is the server-side adapter for nybble.core.kg_builder.build_kg().
    It wires SQLite-backed row-fetch and distinct-count callbacks so that the
    shared builder has no direct knowledge of the storage layer.

    Parameters
    ----------
    schema : nybble.core.schema.Schema
    fk_candidates : list[FKCandidate]
    db_path : str | Path | None
        Path to the queryable SQLite file.  When None, returns an empty graph.
    max_rows : int
        Maximum rows fetched per table.

    Returns
    -------
    nx.MultiDiGraph
    """
    import networkx as nx
    from nybble.core.kg_builder import build_kg as _core_build_kg

    if db_path is None:
        return nx.MultiDiGraph()

    import sqlite3
    from pathlib import Path as _Path

    db_str = str(db_path)

    def _sqlite_fetch_rows(table_name: str, limit: int):
        try:
            con = sqlite3.connect(f"file:{db_str}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            rows = cur.execute(
                f'SELECT * FROM "{table_name}" LIMIT ?', (limit,)
            ).fetchall()
            con.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def _sqlite_distinct_count(table_name: str, col_name: str) -> int:
        try:
            con = sqlite3.connect(f"file:{db_str}?mode=ro", uri=True)
            cur = con.cursor()
            row = cur.execute(
                f'SELECT COUNT(DISTINCT "{col_name}") FROM "{table_name}"'
            ).fetchone()
            con.close()
            return row[0] if row else 0
        except Exception:
            return 0

    return _core_build_kg(
        schema=schema,
        fk_candidates=fk_candidates,
        fetch_rows=_sqlite_fetch_rows,
        max_rows=max_rows,
        fetch_distinct_count=_sqlite_distinct_count,
    )


# ── Scaling stubs ─────────────────────────────────────────────────────────────

class KuzuGraph:
    """KùzuDB-backed graph store (Tier 2 upgrade).
    # MARKER:kg-kuzu-backend

    Install: pip install "datajoins-nybble[kuzu]"
    Config:  GRAPH_BACKEND=kuzu  KUZU_DB_PATH=/output/kg/graph.kuzu
    See SCALING.md for full migration steps.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        try:
            import kuzu  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "KùzuDB backend requires `pip install 'datajoins-nybble[kuzu]'`. "
                "See docs/SCALING.md § Tier 2."
            ) from exc
        import os as _os
        path = db_path or _os.environ.get("KUZU_DB_PATH", "/output/kg/graph.kuzu")
        self._db = kuzu.Database(path)
        self._conn = kuzu.Connection(self._db)
        self._nodes: Dict[str, Any] = {}
        self._edges: List[tuple] = []

    def add_node(self, node) -> None:
        self._nodes[node.id] = {
            "label": node.label,
            "entity_type": node.entity_type,
            "source_key": node.source_key,
        }

    def add_edge(self, edge) -> None:
        self._edges.append((edge.src_id, edge.tgt_id, {
            "relation": edge.relation,
            "label": edge.label,
        }))

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    def to_cytoscape_json(self) -> dict:
        cy_nodes = [{"data": {"id": nid, **attrs}} for nid, attrs in self._nodes.items()]
        cy_edges = [
            {"data": {"id": f"e{i}", "source": s, "target": t, **a}}
            for i, (s, t, a) in enumerate(self._edges)
        ]
        return {"elements": {"nodes": cy_nodes, "edges": cy_edges}}

    def subgraph(self, root_id: str, depth: int = 2) -> "KuzuGraph":
        return self

    def community_detect(self) -> Dict[str, int]:
        return {}
