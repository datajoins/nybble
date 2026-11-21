"""
highlight_store.py - Per-relation highlight rule persistence

Highlight rules let browse users color rows that match a column value/pattern.
Rules are stored in `.nybble/highlights.json` as a dict keyed by relation name.

Each rule is a dict:
    {"column": str, "pattern": str, "color": str}

Where:
  - `column` is the column name (must exist in the schema)
  - `pattern` is a substring match (case-insensitive)
  - `color`   is a Rich style string ("red", "bold green", "yellow", …)

Public API
----------
  load_rules(relation_name)  -> list[dict]
  save_rules(relation_name, rules)
  add_rule(relation_name, column, pattern, color)
  clear_rules(relation_name)
  apply_rules(rules, row, schema) -> str | None
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_STORE_FILE = Path(".nybble") / "highlights.json"

# ── Valid Rich color names (subset) used for input validation ─────────────────
_VALID_COLORS = {
    "red", "green", "blue", "yellow", "cyan", "magenta", "white",
    "bold red", "bold green", "bold blue", "bold yellow", "bold cyan",
    "bold magenta", "bold white", "dim", "italic", "orange3", "purple",
    "bright_red", "bright_green", "bright_blue", "bright_yellow",
    "bright_cyan", "bright_magenta", "bright_white",
}


def _load_all() -> dict:
    """Return the full highlights dict from disk (empty dict if absent)."""
    if not _STORE_FILE.exists():
        return {}
    try:
        return json.loads(_STORE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_all(data: dict) -> None:
    """Write the full highlights dict to disk."""
    _STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STORE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_rules(relation_name: str) -> list[dict]:
    """Return highlight rules for *relation_name* (may be empty list)."""
    return _load_all().get(relation_name, [])


def save_rules(relation_name: str, rules: list[dict]) -> None:
    """Persist *rules* for *relation_name* to disk."""
    data = _load_all()
    data[relation_name] = rules
    _save_all(data)


def add_rule(relation_name: str, column: str, pattern: str, color: str) -> list[dict]:
    """Append a new rule and persist. Returns the updated rules list."""
    rules = load_rules(relation_name)
    rules.append({"column": column, "pattern": pattern, "color": color})
    save_rules(relation_name, rules)
    return rules


def clear_rules(relation_name: str) -> None:
    """Remove all highlight rules for *relation_name*."""
    save_rules(relation_name, [])


def apply_rules(
    rules: list[dict],
    row: list,
    schema: list[dict],
) -> Optional[str]:
    """Return a Rich style string if *row* matches any rule, else None.

    Rules are evaluated in order; the first match wins.

    Parameters
    ----------
    rules:
        List of rule dicts with keys ``column``, ``pattern``, ``color``.
    row:
        One row of values aligned with *schema*.
    schema:
        List of ``{"name": str, "type": str}`` dicts.
    """
    if not rules:
        return None
    col_idx = {col["name"]: i for i, col in enumerate(schema)}
    for rule in rules:
        col = rule.get("column", "")
        pattern = rule.get("pattern", "")
        color = rule.get("color", "")
        if col not in col_idx or not pattern:
            continue
        cell_val = str(row[col_idx[col]] if row[col_idx[col]] is not None else "")
        if pattern.lower() in cell_val.lower():
            return color or "bold yellow"
    return None
