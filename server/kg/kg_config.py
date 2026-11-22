"""server/kg/kg_config.py - Centralised configuration manager for the KG subsystem.

Replaces scattered os.environ.get() calls across kg_store.py, kg_indexer.py,
kg_graph.py, kg_resolver.py, and kg_search.py with a single typed interface.

Aligned with the semantica KGConfig design (3-source fallback chain).

No external dependencies - pure stdlib: os, pathlib, typing, logging.

# MARKER:kg-config-manager

Configuration resolution order (highest priority first):
  1. Explicit ``set()`` / constructor kwargs
  2. Environment variables (exact name match)
  3. Hardcoded defaults declared in _CONFIG_SCHEMA

Public API
----------
  KGConfig          - Configuration manager class
  kg_config         - Module-level singleton; import and use directly
  get_kg_config()   - Return the module-level singleton (testable alternative)

Usage
-----
  from server.kg.kg_config import kg_config

  # Read a value
  embed_model = kg_config.get("embed_model")           # str
  sample_rows = kg_config.get_int("sample_rows")       # int
  deadline    = kg_config.get_float("embed_deadline_s")# float

  # Override programmatically (e.g. in tests)
  kg_config.set("embed_model", "minilm")

  # Reset to env/defaults (e.g. between tests)
  kg_config.reset()
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)


# ── Config schema ─────────────────────────────────────────────────────────────
# Each entry: (env_var_name, default_value)
# The key is the canonical config key used with .get() / .set().

_CONFIG_SCHEMA: Dict[str, tuple] = {
    # ── Storage paths ──────────────────────────────────────────────────────────
    "kg_dir":       ("KG_DIR",          "/output/kg"),
    "output_dir":   ("OUTPUT_DIR",       "/output/erd"),
    "upload_dir":   ("UPLOAD_DIR",       "/uploads"),
    "registry":     ("REGISTRY",         "/output/registry.json"),

    # ── Storage backend ────────────────────────────────────────────────────────
    "backend":      ("BACKEND",          "sqlite"),

    # ── Graph backend ──────────────────────────────────────────────────────────
    "graph_backend":("GRAPH_BACKEND",    "networkx"),
    "kuzu_db_path": ("KUZU_DB_PATH",     "/output/kg/graph.kuzu"),

    # ── Embedding model (local sentence-transformers) ──────────────────────────
    "embed_model":  ("EMBED_MODEL",      "none"),

    # ── HTTP embedding provider ────────────────────────────────────────────────
    "embed_provider": ("EMBED_PROVIDER", "none"),
    "embed_base_url": ("EMBED_BASE_URL", "https://api.openai.com/v1"),
    "embed_api_key":  ("OPENAI_API_KEY", ""),          # see also OPENROUTER_API_KEY
    "embed_dim":      ("EMBED_DIM",      "256"),        # stored as str, cast on read
    "embed_deadline_s": ("EMBED_DEADLINE_S", "30"),     # stored as str, cast on read
    "embed_retries":  ("EMBED_RETRIES",  "3"),          # stored as str, cast on read

    # ── Indexer ────────────────────────────────────────────────────────────────
    "sample_rows":  ("KG_SAMPLE_ROWS",   "50"),         # stored as str, cast on read
}


class KGConfig:
    """Typed configuration manager for the Knowledge Graph subsystem.

    Three-source fallback chain (highest priority first):
      1. Programmatic overrides via ``set()``
      2. Environment variables
      3. Hardcoded defaults in ``_CONFIG_SCHEMA``

    The manager is independently unit-testable: construct a fresh
    ``KGConfig(overrides={...})`` per test without touching ``os.environ``.
    """

    def __init__(self, overrides: Optional[Dict[str, Any]] = None) -> None:
        self._overrides: Dict[str, Any] = dict(overrides or {})

    # ── Core accessors ────────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for *key*, or *default* if not configured.

        Resolution order: explicit overrides → env var → schema default → *default*.
        """
        # 1. Explicit overrides
        if key in self._overrides:
            return self._overrides[key]

        # 2. Schema lookup (env var + hardcoded default)
        if key in _CONFIG_SCHEMA:
            env_name, schema_default = _CONFIG_SCHEMA[key]
            # Special case: embed_api_key can come from two env vars
            if key == "embed_api_key":
                val = (
                    os.environ.get("OPENAI_API_KEY", "")
                    or os.environ.get("OPENROUTER_API_KEY", "")
                )
                return val if val else schema_default
            val = os.environ.get(env_name)
            return val if val is not None else schema_default

        # 3. Caller-supplied default
        return default

    def get_str(self, key: str, default: str = "") -> str:
        """Return value as ``str`` (lowercased where env-var convention demands)."""
        raw = self.get(key, default)
        return str(raw).lower() if isinstance(raw, str) else str(raw)

    def get_int(self, key: str, default: int = 0) -> int:
        """Return value coerced to ``int``."""
        raw = self.get(key, str(default))
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "KGConfig.get_int: cannot convert %r for key %r; using %d",
                raw, key, default,
            )
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        """Return value coerced to ``float``."""
        raw = self.get(key, str(default))
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "KGConfig.get_float: cannot convert %r for key %r; using %f",
                raw, key, default,
            )
            return default

    def get_path(self, key: str, default: Optional[str] = None) -> Path:
        """Return value as a ``pathlib.Path``."""
        raw = self.get(key, default or "")
        return Path(str(raw))

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Return value coerced to ``bool``.

        Truthy strings: "true", "1", "yes", "on" (case-insensitive).
        """
        raw = self.get(key, str(default))
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("true", "1", "yes", "on")

    # ── Mutation ──────────────────────────────────────────────────────────────

    def set(self, key: str, value: Any) -> None:
        """Programmatically override a configuration value."""
        self._overrides[key] = value

    def reset(self, key: Optional[str] = None) -> None:
        """Remove an override (or all overrides) so env/defaults take effect again."""
        if key is not None:
            self._overrides.pop(key, None)
        else:
            self._overrides.clear()

    # ── Inspection ────────────────────────────────────────────────────────────

    def all_keys(self):
        """Return all declared config keys."""
        return list(_CONFIG_SCHEMA.keys())

    def snapshot(self) -> Dict[str, Any]:
        """Return a snapshot of all resolved values (useful for logging / debugging)."""
        return {key: self.get(key) for key in _CONFIG_SCHEMA}

    def __repr__(self) -> str:  # pragma: no cover
        overrides_summary = list(self._overrides.keys())
        return f"KGConfig(overrides={overrides_summary})"


# ── Module-level singleton ────────────────────────────────────────────────────

kg_config = KGConfig()


def get_kg_config() -> KGConfig:
    """Return the module-level singleton.  Useful when dependency injection
    is preferred over direct module import."""
    return kg_config
