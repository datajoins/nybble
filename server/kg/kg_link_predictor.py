"""server/kg/kg_link_predictor.py - Link prediction algorithms for EntityGraph.

Five classic graph-structural link prediction methods, adapted from the
semantica LinkPredictor design.  All algorithms operate directly on a
NetworkX DiGraph or any object exposing .neighbors() / .degree().

No external dependencies - pure stdlib: math, typing, logging.
numpy is NOT required; adamic_adar uses math.log instead.

# MARKER:kg-link-predictor

Public API
----------
  preferential_attachment(G, u, v) → float
  common_neighbors(G, u, v)        → float
  jaccard_coefficient(G, u, v)     → float
  adamic_adar(G, u, v)             → float   (uses math.log)
  resource_allocation(G, u, v)     → float

  predict_top_links(graph, top_k=20, method="preferential_attachment",
                   exclude_existing=True) → List[Tuple[str, str, float]]
      Convenience wrapper that iterates all non-existing node pairs and
      returns the top-k scored candidates sorted descending by score.

Algorithms
----------
preferential_attachment:
    score(u, v) = deg(u) × deg(v)
    Cheap, effective for scale-free networks.

common_neighbors:
    score(u, v) = |N(u) ∩ N(v)|

jaccard_coefficient:
    score(u, v) = |N(u) ∩ N(v)| / |N(u) ∪ N(v)|

adamic_adar:
    score(u, v) = Σ_{w ∈ N(u)∩N(v)}  1 / log(deg(w) + ε)
    Uses math.log (no numpy).

resource_allocation:
    score(u, v) = Σ_{w ∈ N(u)∩N(v)}  1 / deg(w)
"""
from __future__ import annotations

import logging
import math
from typing import List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Small epsilon to prevent log(0) in adamic_adar
_LOG_EPS = 1e-10

SUPPORTED_METHODS = frozenset({
    "preferential_attachment",
    "common_neighbors",
    "jaccard_coefficient",
    "adamic_adar",
    "resource_allocation",
})


# ── Neighbour helpers ─────────────────────────────────────────────────────────

def _neighbors(G, node) -> Set:
    """Return the neighbour set of *node* in *G* (ignores self-loops)."""
    try:
        return {n for n in G.neighbors(node) if n != node}
    except Exception:
        return set()


def _degree(G, node) -> int:
    """Return the degree of *node* in *G*."""
    try:
        d = G.degree(node)
        if isinstance(d, int):
            return d
        # NetworkX DiGraph.degree() returns DegreeView; handle both
        return int(d)
    except Exception:
        return 0


# ── Five algorithms ───────────────────────────────────────────────────────────

def preferential_attachment(G, u: str, v: str) -> float:
    """Score = deg(u) × deg(v).

    >>> # returns 0.0 for a self-pair
    """
    if u == v:
        return 0.0
    return float(_degree(G, u) * _degree(G, v))


def common_neighbors(G, u: str, v: str) -> float:
    """Score = number of shared neighbours between u and v."""
    if u == v:
        return 0.0
    nu = _neighbors(G, u)
    nv = _neighbors(G, v)
    return float(len(nu & nv))


def jaccard_coefficient(G, u: str, v: str) -> float:
    """Score = |N(u) ∩ N(v)| / |N(u) ∪ N(v)|.  Returns 0 if union is empty."""
    if u == v:
        return 0.0
    nu = _neighbors(G, u)
    nv = _neighbors(G, v)
    union = nu | nv
    if not union:
        return 0.0
    return float(len(nu & nv) / len(union))


def adamic_adar(G, u: str, v: str) -> float:
    """Score = Σ_{w ∈ N(u)∩N(v)}  1 / log(deg(w) + ε).

    Uses math.log - no numpy required.  Nodes with degree ≤ 1 contribute
    negligibly (log(1+ε) ≈ 1e-10, so 1/log ≈ 1e10 - dominated by degree 1
    common neighbours).  The standard formulation uses log(deg(w)); we add
    ε to avoid division by zero when deg(w) == 0.
    """
    if u == v:
        return 0.0
    nu = _neighbors(G, u)
    nv = _neighbors(G, v)
    common = nu & nv
    score = 0.0
    for w in common:
        deg_w = _degree(G, w)
        score += 1.0 / math.log(deg_w + _LOG_EPS)
    return score


def resource_allocation(G, u: str, v: str) -> float:
    """Score = Σ_{w ∈ N(u)∩N(v)}  1 / deg(w).  Skips deg-0 intermediaries."""
    if u == v:
        return 0.0
    nu = _neighbors(G, u)
    nv = _neighbors(G, v)
    common = nu & nv
    score = 0.0
    for w in common:
        deg_w = _degree(G, w)
        if deg_w > 0:
            score += 1.0 / deg_w
    return score


# ── score_link dispatcher ─────────────────────────────────────────────────────

def score_link(G, u: str, v: str, method: str = "preferential_attachment") -> float:
    """Dispatch to the named link-prediction algorithm.

    Parameters
    ----------
    G      : NetworkX graph (or any object exposing .neighbors() / .degree())
    u, v   : node IDs to score
    method : one of SUPPORTED_METHODS

    Raises ValueError for unknown methods.
    """
    if method not in SUPPORTED_METHODS:
        raise ValueError(
            f"Unknown link prediction method: {method!r}. "
            f"Supported: {sorted(SUPPORTED_METHODS)}"
        )
    if method == "preferential_attachment":
        return preferential_attachment(G, u, v)
    if method == "common_neighbors":
        return common_neighbors(G, u, v)
    if method == "jaccard_coefficient":
        return jaccard_coefficient(G, u, v)
    if method == "adamic_adar":
        return adamic_adar(G, u, v)
    # resource_allocation
    return resource_allocation(G, u, v)


# ── Batch top-k prediction ────────────────────────────────────────────────────

def predict_top_links(
    graph,
    top_k: int = 20,
    method: str = "preferential_attachment",
    exclude_existing: bool = True,
) -> List[Tuple[str, str, float]]:
    """Return up to *top_k* predicted links sorted by score (descending).

    Parameters
    ----------
    graph          : EntityGraph instance (uses graph._G)
    top_k          : maximum number of results
    method         : link prediction method name
    exclude_existing : if True, skip pairs already connected in the graph

    Returns
    -------
    List of (node1, node2, score) tuples.
    """
    G = getattr(graph, "_G", None)
    if G is None or G.number_of_nodes() < 2:
        return []

    nodes = list(G.nodes())
    existing: Set[Tuple] = set()
    if exclude_existing:
        existing = set(G.edges())

    scores: List[Tuple[str, str, float]] = []
    for i, u in enumerate(nodes):
        for v in nodes[i + 1:]:
            if exclude_existing and ((u, v) in existing or (v, u) in existing):
                continue
            s = score_link(G, u, v, method=method)
            if s > 0:
                scores.append((u, v, s))

    scores.sort(key=lambda x: x[2], reverse=True)
    return scores[:top_k]
