"""server/kg/kg_llm_cache.py - Disk-backed LLM response cache for kg_inference.

Modelled on ai-knowledge-graph src/knowledge_graph/llm.py LLMClient cache_dir pattern.
Responses are keyed by SHA-256 of (model, system_prompt, user_prompt) and stored as
individual JSON files in a cache directory.

Opt-in: enabled only when ``KG_LLM_CACHE_DIR`` environment variable is set, or an
explicit ``cache_dir`` is passed to :class:`KGInferenceCache`.

# MARKER:kg-inference-cache
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default cache directory from environment variable (empty string = disabled).
_DEFAULT_CACHE_DIR: str = os.environ.get("KG_LLM_CACHE_DIR", "")


class KGInferenceCache:
    """Disk-backed SHA-256-keyed cache for LLM inference responses.

    Usage::

        cache = KGInferenceCache()          # opt-in via KG_LLM_CACHE_DIR env var
        cache = KGInferenceCache(".kg-llm-cache")  # explicit path

        result = cache.get(model, system, user)
        if result is None:
            result = call_llm(...)
            cache.put(model, system, user, result)

    Truncated or error responses should NOT be cached - only pass a ``content``
    value to :meth:`put` when the LLM returned a complete, successful response.
    """

    def __init__(self, cache_dir: Optional[str] = None) -> None:
        raw = cache_dir if cache_dir is not None else _DEFAULT_CACHE_DIR
        self._dir: Optional[Path] = Path(raw) if raw else None

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, model: str, system: str, user: str) -> Optional[str]:
        """Return the cached content string, or ``None`` on miss / disabled."""
        path = self._key_path(model, system, user)
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            content = data.get("content")
            return content if isinstance(content, str) and content.strip() else None
        except Exception as exc:
            logger.debug("Cache read failed for %s: %s", path, exc)
            return None

    def put(self, model: str, system: str, user: str, content: str) -> None:
        """Write ``content`` to the cache.  Silently ignores I/O errors."""
        path = self._key_path(model, system, user)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "model":   model,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "content": content,
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write inference cache entry %s: %s", path, exc)

    @property
    def enabled(self) -> bool:
        """True when a cache directory is configured."""
        return self._dir is not None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _key_path(self, model: str, system: str, user: str) -> Optional[Path]:
        """Return the cache file Path for these request parameters, or None."""
        if self._dir is None:
            return None
        # Key material matches ai-knowledge-graph LLMClient._cache_path logic:
        # deterministic JSON serialisation → SHA-256 hex digest → .json file.
        key_material = json.dumps(
            {"model": model, "system": system, "user": user},
            sort_keys=True,
            ensure_ascii=False,
        )
        digest = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        return self._dir / f"{digest}.json"
