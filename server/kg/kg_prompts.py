"""server/kg/kg_prompts.py - Centralised prompt registry for KG inference and MCP.

Modelled on ai-knowledge-graph src/knowledge_graph/prompts/__init__.py PromptFactory.
Stores prompts as either static strings or callable generators and retrieves them by
name via ``kg_prompt_registry.get_prompt(name, *args, **kwargs)``.

# MARKER:kg-prompt-registry
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Union

PromptEntry = Union[str, Callable[..., str]]


class KGPromptRegistry:
    """Register and retrieve LLM prompt strings or generator callables by name.

    Usage::

        # Static string:
        registry.register("my_system", "You are an expert.")

        # Callable generator:
        registry.register("my_user", lambda topic: f"Tell me about {topic}.")

        # Retrieve:
        registry.get_prompt("my_system")           # → "You are an expert."
        registry.get_prompt("my_user", "graphs")   # → "Tell me about graphs."
    """

    def __init__(self) -> None:
        self._prompts: Dict[str, PromptEntry] = {}

    def register(self, name: str, entry: PromptEntry) -> None:
        """Register a prompt under ``name``.  Overwrites any previous entry."""
        self._prompts[name] = entry

    def get_prompt(self, name: str, *args: Any, **kwargs: Any) -> str:
        """Return the prompt string for ``name``, passing args to callables.

        Raises:
            KeyError: when ``name`` is not registered.
        """
        if name not in self._prompts:
            raise KeyError(f"Prompt {name!r} not registered in KGPromptRegistry")
        entry = self._prompts[name]
        return entry(*args, **kwargs) if callable(entry) else entry


# ── Singleton ─────────────────────────────────────────────────────────────────

kg_prompt_registry = KGPromptRegistry()


# ── Bridge inference ───────────────────────────────────────────────────────────

kg_prompt_registry.register(
    "bridge_system",
    "You are an expert knowledge-graph engineer. "
    "Connect isolated node groups to the main graph using only well-established "
    "factual relationships. "
    "Return ONLY a JSON array of "
    '{"src_id": ..., "relation": ..., "tgt_id": ...} objects. '
    "Predicates must be 1-4 words. "
    "Subject and object must differ. Do not invent new entity names.",
)


def _bridge_user(main_sample: str, groups_text: str) -> str:
    return (
        f"Main component entities (sample):\n{main_sample}\n\n"
        f"Isolated groups (connect FROM these):\n{groups_text}\n\n"
        "For EACH group, propose 1-3 edges that link one of its entities to one of "
        "the main-component entities above. Use entity IDs exactly as written. "
        "Return ONLY a JSON array of "
        '{"src_id": ..., "relation": ..., "tgt_id": ...} objects.'
    )


kg_prompt_registry.register("bridge_user", _bridge_user)


# ── Hub inference ──────────────────────────────────────────────────────────────

kg_prompt_registry.register(
    "hub_system",
    "You are an expert knowledge-graph engineer. "
    "Add well-known relationships between the most connected entities in a graph. "
    "Use only widely accepted facts. "
    "Return ONLY a JSON array of "
    '{"src_id": ..., "relation": ..., "tgt_id": ...} objects. '
    "Prefer specific predicates (enabled, powered, preceded, depends_on) over vague ones (related_to). "
    "Subject and object must differ.",
)


def _hub_user(entities_text: str, existing_text: str, max_new: int) -> str:
    return (
        f"Hub entities:\n{entities_text}\n\n"
        f"Existing relationships between them:\n{existing_text}\n\n"
        f"Propose up to {max_new} additional well-known relationships between pairs of "
        "these entities. Do not repeat or reverse existing ones. "
        "Return ONLY a JSON array of "
        '{"src_id": ..., "relation": ..., "tgt_id": ...} objects.'
    )


kg_prompt_registry.register("hub_user", _hub_user)


# ── MCP propose-link ───────────────────────────────────────────────────────────

kg_prompt_registry.register(
    "propose_link_system",
    "You are a knowledge-graph expert. Given two entity IDs and their context, "
    "propose a concise relationship predicate (1-4 words) connecting them. "
    "Return ONLY a JSON object: "
    '{"relation": "<predicate>", "confidence": <0.0-1.0>}.',
)


def _propose_link_user(src_id: str, tgt_id: str, context: str) -> str:
    return (
        f"Source entity: {src_id}\n"
        f"Target entity: {tgt_id}\n"
        f"Context:\n{context}\n\n"
        "Propose a relationship predicate. Return ONLY valid JSON."
    )


kg_prompt_registry.register("propose_link_user", _propose_link_user)
