"""server/kg/kg_inference.py - Lightweight graph inference engine.

Implements:
  - Transitive closure over declared transitive edge types
  - Inverse-edge materialisation for declared symmetric relations
  - Simple forward-chaining rules as Python predicates

No external dependencies. All operations are read-only (returns new edge lists).

# MARKER:kg-inference-engine
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, List, Optional, Set, Tuple, TypeVar

# Prompt registry - import so callers can do:
#   from server.kg.kg_inference import kg_prompt_registry
# MARKER:kg-prompt-registry
from .kg_prompts import kg_prompt_registry  # noqa: F401

logger = logging.getLogger(__name__)


# ── LLM JSON repair ────────────────────────────────────────────────────────────
#
# MARKER:kg-llm-json-repair
#
# Ported from ai-knowledge-graph src/knowledge_graph/llm.py extract_json_from_text().
# Applied wherever kg_inference parses a raw LLM response string.
#
_THINK_BLOCK  = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_TAG    = re.compile(r"</?think>", re.IGNORECASE)
_CODE_FENCE   = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\r?\n?(.*?)```", re.DOTALL)
_TRAIL_COMMA  = re.compile(r",(\s*[\]}])")
_UNQUOTED_KEY = re.compile(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*):')


def _find_matching_bracket(text: str, start: int, opener: str, closer: str) -> int:
    """Return index of the bracket matching ``text[start]``, or -1.

    Ignores brackets inside JSON string literals.
    Mirrors ai-knowledge-graph llm.py _find_matching().
    """
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            escaped = (ch == "\\" and not escaped)
            if ch == '"' and not escaped:
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _repair_llm_json(text: Optional[str]) -> Optional[list]:
    """Extract and repair a JSON array from an LLM response string.

    Six-step repair pipeline (mirrors ai-knowledge-graph llm.py extract_json_from_text):

    1. Strip ``<think>...</think>`` blocks (reasoning models).
    2. Unwrap `` ```json ... ``` `` code fences.
    3. Direct ``json.loads`` parse.
    4. Trailing-comma removal + unquoted-key repair, then re-parse.
    5. Find first ``[`` and bracket-match to extract array substring.
    6. Salvage every complete ``{...}`` object from a truncated array.

    Returns a list (possibly empty) or ``None`` when nothing parseable is found.
    # MARKER:kg-llm-json-repair
    """
    if not text or not text.strip():
        return None

    # Step 1: strip reasoning blocks
    text = _THINK_BLOCK.sub("", text)
    text = _THINK_TAG.sub("", text)

    # Build candidate strings: code fence contents first, then full text
    candidates = [m.group(1).strip() for m in _CODE_FENCE.finditer(text)]
    candidates.append(text.strip())

    for candidate in candidates:
        if not candidate:
            continue

        # Step 3: direct parse
        try:
            parsed = json.loads(candidate)
            result = _coerce_to_list(parsed)
            if result is not None:
                return result
        except json.JSONDecodeError:
            pass

        # Step 4: repair trailing commas + unquoted keys, then re-parse
        repaired = _TRAIL_COMMA.sub(r"\1", candidate)
        repaired = _UNQUOTED_KEY.sub(r'\1"\2"\3:', repaired)
        try:
            parsed = json.loads(repaired)
            result = _coerce_to_list(parsed)
            if result is not None:
                return result
        except json.JSONDecodeError:
            pass

        # Step 5: find array boundaries and extract substring
        start = candidate.find("[")
        if start != -1:
            end = _find_matching_bracket(candidate, start, "[", "]")
            if end != -1:
                try:
                    parsed = json.loads(candidate[start : end + 1])
                    result = _coerce_to_list(parsed)
                    if result is not None:
                        return result
                except json.JSONDecodeError:
                    pass

            # Step 6: salvage complete objects from truncated array
            objects: list = []
            i = start + 1
            while True:
                obj_start = candidate.find("{", i)
                if obj_start == -1:
                    break
                obj_end = _find_matching_bracket(candidate, obj_start, "{", "}")
                if obj_end == -1:
                    break
                try:
                    obj = json.loads(candidate[obj_start : obj_end + 1])
                    if isinstance(obj, dict):
                        objects.append(obj)
                except json.JSONDecodeError:
                    pass
                i = obj_end + 1
            if objects:
                logger.warning(
                    "Salvaged %d objects from truncated LLM JSON array", len(objects)
                )
                return objects

    return None


def _coerce_to_list(parsed: object) -> Optional[list]:
    """Return a list from ``parsed``, or None.  Unwraps single-key dict wrappers."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        # Unwrap JSON-mode wrappers like {"triples": [...]} or {"edges": [...]}
        lists = [v for v in parsed.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
        # Single bare object → wrap in list
        return [parsed]
    return None


# ── Parallel inference executor ───────────────────────────────────────────────
#
# MARKER:kg-parallel-inference-executor
#
# Modelled on ai-knowledge-graph main.py ThreadPoolExecutor dispatch in
# process_documents().  Runs a list of zero-argument callables concurrently,
# returns results in submission order, and isolates individual failures.
#
_T = TypeVar("_T")


def _run_inference_batch(
    callables: List[Callable[[], _T]],
    max_workers: int = 4,
) -> List[Optional[_T]]:
    """Execute inference callables concurrently; return results in submission order.

    Failed callables are logged as warnings and produce ``None`` in the result list,
    so a single LLM error never aborts the whole batch.

    Mirrors ai-knowledge-graph main.py ThreadPoolExecutor chunk dispatch.

    Args:
        callables:   Zero-argument callables to execute.
        max_workers: Thread-pool size.  Capped at ``len(callables)``.

    Returns:
        List of results in the same order as ``callables``.  ``None`` for failures.
    """
    if not callables:
        return []

    # Serial fast-path when only one item or max_workers==1
    if max_workers == 1 or len(callables) == 1:
        results: List[Optional[_T]] = []
        for fn in callables:
            try:
                results.append(fn())
            except Exception as exc:
                logger.warning("Inference batch callable failed (serial): %s", exc)
                results.append(None)
        return results

    ordered: List[Optional[_T]] = [None] * len(callables)
    with ThreadPoolExecutor(max_workers=min(max_workers, len(callables))) as pool:
        futures = {pool.submit(fn): i for i, fn in enumerate(callables)}
        for future in as_completed(futures):
            idx = futures[future]
            try:
                ordered[idx] = future.result()
            except Exception as exc:
                logger.warning(
                    "Inference batch callable %d failed: %s", idx, exc
                )
    return ordered


@dataclass
class InferenceRule:
    """A forward-chaining inference rule.

    A rule fires when its predicate returns True for a (src_node, tgt_node, edge_attrs)
    triple and produces a new edge relation string.
    """
    name: str
    predicate: Callable[[str, str, dict, dict, dict], bool]
    """predicate(src_id, tgt_id, edge_attrs, src_node_attrs, tgt_node_attrs) -> bool"""
    derived_relation: str
    derived_label: str = ""
    confidence: float = 0.7


@dataclass
class InferenceConfig:
    """Configuration for the inference engine."""
    transitive_relations: FrozenSet[str] = field(default_factory=frozenset)
    """Edge relation prefixes that should have transitive closure materialised."""
    symmetric_relations: FrozenSet[str] = field(default_factory=frozenset)
    """Edge relation prefixes that should have inverse edges materialised."""
    rules: List[InferenceRule] = field(default_factory=list)
    max_depth: int = 3
    """Maximum depth for transitive closure materialisation."""


# Default inference config applied when none is provided
DEFAULT_INFERENCE_CONFIG = InferenceConfig(
    transitive_relations=frozenset({"is_part_of", "is_child_of", "belongs_to", "FK"}),
    symmetric_relations=frozenset({"co_occurs", "cross_db", "SIMILAR_TO"}),
)


def materialise_transitive_closure(
    G,
    relations: FrozenSet[str],
    max_depth: int = 3,
) -> List[Tuple[str, str, dict]]:
    """Materialise transitive edges for given relation prefixes.

    Returns a list of (src_id, tgt_id, attrs) tuples for new inferred edges.
    Does NOT modify the graph in-place.

    Example: A -[FK]-> B -[FK]-> C produces A -[FK:transitive]-> C
    """
    if G is None:
        return []

    new_edges: List[Tuple[str, str, dict]] = []
    seen: Set[Tuple[str, str, str]] = set()

    # Index existing edges by (src, tgt, kind) to avoid duplicates
    for src, tgt, attrs in G.edges(data=True):
        relation = attrs.get("relation", "")
        kind = relation.split(":")[0] if ":" in relation else relation
        seen.add((src, tgt, kind))

    for rel_prefix in relations:
        # Collect all edges matching this relation prefix
        rel_edges: Dict[str, List[str]] = {}  # src → [tgt, ...]
        for src, tgt, attrs in G.edges(data=True):
            relation = attrs.get("relation", "")
            kind = relation.split(":")[0] if ":" in relation else relation
            if kind == rel_prefix:
                rel_edges.setdefault(src, []).append(tgt)

        if not rel_edges:
            continue

        # BFS to depth max_depth, materialise transitive edges
        for start_node in list(rel_edges.keys()):
            visited: Set[str] = {start_node}
            frontier: List[str] = list(rel_edges.get(start_node, []))
            depth = 1

            while frontier and depth <= max_depth:
                next_frontier: List[str] = []
                for node in frontier:
                    if node in visited:
                        continue
                    visited.add(node)
                    key = (start_node, node, rel_prefix)
                    if key not in seen:
                        seen.add(key)
                        new_edges.append((start_node, node, {
                            "relation": f"{rel_prefix}:transitive",
                            "label":    f"transitive({rel_prefix})",
                            "weight":   max(0.1, 1.0 / (depth + 1)),
                            "confidence": max(0.1, 0.9 ** depth),
                        }))
                    next_frontier.extend(rel_edges.get(node, []))
                frontier = next_frontier
                depth += 1

    logger.debug(
        "Transitive closure materialised",
        extra={"new_edges": len(new_edges), "relations": list(relations)},
    )
    return new_edges


def materialise_symmetric_edges(
    G,
    relations: FrozenSet[str],
) -> List[Tuple[str, str, dict]]:
    """Materialise inverse edges for symmetric relation types.

    Returns (src_id, tgt_id, attrs) for each missing reverse edge.
    Does NOT modify the graph in-place.
    """
    if G is None:
        return []

    new_edges: List[Tuple[str, str, dict]] = []
    existing: Set[Tuple[str, str]] = set(G.edges())

    for src, tgt, attrs in G.edges(data=True):
        relation = attrs.get("relation", "")
        kind = relation.split(":")[0] if ":" in relation else relation
        if kind in relations and (tgt, src) not in existing:
            existing.add((tgt, src))
            new_edges.append((tgt, src, {
                "relation":   f"{relation}:inverse",
                "label":      f"inverse({attrs.get('label', '')})",
                "weight":     attrs.get("weight", 1.0),
                "confidence": attrs.get("confidence", 1.0) * 0.9,
            }))

    logger.debug(
        "Symmetric edges materialised",
        extra={"new_edges": len(new_edges), "relations": list(relations)},
    )
    return new_edges


def apply_rules(
    G,
    rules: List[InferenceRule],
) -> List[Tuple[str, str, dict]]:
    """Apply forward-chaining inference rules over the graph.

    Returns (src_id, tgt_id, attrs) for each new inferred edge.
    """
    if G is None or not rules:
        return []

    new_edges: List[Tuple[str, str, dict]] = []
    existing: Set[Tuple[str, str]] = set(G.edges())
    node_data = dict(G.nodes(data=True))

    for src, tgt, edge_attrs in G.edges(data=True):
        src_attrs = node_data.get(src, {})
        tgt_attrs = node_data.get(tgt, {})
        for rule in rules:
            try:
                if rule.predicate(src, tgt, edge_attrs, src_attrs, tgt_attrs):
                    if (src, tgt) not in existing or edge_attrs.get("relation") != rule.derived_relation:
                        new_edges.append((src, tgt, {
                            "relation":   rule.derived_relation,
                            "label":      rule.derived_label or rule.name,
                            "weight":     1.0,
                            "confidence": rule.confidence,
                        }))
            except Exception as _exc:
                logger.debug(
                    "Inference rule predicate raised",
                    extra={"rule": rule.name, "error": str(_exc)},
                )

    logger.debug(
        "Forward-chaining rules applied",
        extra={"rules": len(rules), "new_edges": len(new_edges)},
    )
    return new_edges


def run_inference(
    graph,
    config: Optional[InferenceConfig] = None,
) -> List[Tuple[str, str, dict]]:
    """Run the full inference pipeline over an EntityGraph.

    Returns all new (src_id, tgt_id, attrs) edges that were inferred.
    Does NOT mutate the graph - the caller decides whether to add them.

    Usage:
        from server.kg.kg_inference import run_inference
        new_edges = run_inference(my_graph)
        for src, tgt, attrs in new_edges:
            # add to graph if desired
    """
    if config is None:
        config = DEFAULT_INFERENCE_CONFIG

    G = graph._G if hasattr(graph, "_G") else None

    new_edges: List[Tuple[str, str, dict]] = []

    if config.transitive_relations:
        new_edges.extend(
            materialise_transitive_closure(G, config.transitive_relations, config.max_depth)
        )
    if config.symmetric_relations:
        new_edges.extend(
            materialise_symmetric_edges(G, config.symmetric_relations)
        )
    if config.rules:
        new_edges.extend(apply_rules(G, config.rules))

    return new_edges
