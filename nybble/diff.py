"""
diff.py - Schema Diff Between Two Data Sources

Compares the column schemas of two loaded Nybble relations (or any two
sources accepted by ``nybble.load()``) and reports added, removed, and
type-changed columns.

Public API
----------
  schema_diff(source_a, source_b, *, name_a=None, name_b=None) → SchemaDiff
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union

from nybble.resolver import Relation, _REGISTERED, load as _load


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class ColumnChange:
    """A column whose type changed between source A and source B."""
    name: str
    type_a: str
    type_b: str


@dataclass
class SchemaDiff:
    """Result of comparing two relation schemas.

    Attributes
    ----------
    name_a, name_b  : display names for the two sources
    added           : columns present in B but not in A  (list[{name, type}])
    removed         : columns present in A but not in B  (list[{name, type}])
    changed         : columns present in both but with different types
    common          : columns identical in both sources
    is_identical    : True when added, removed, and changed are all empty
    """
    name_a: str
    name_b: str
    added: list[dict]     # {name, type} - in B, not in A
    removed: list[dict]   # {name, type} - in A, not in B
    changed: list[ColumnChange]
    common: list[dict]    # {name, type} - identical in both
    is_identical: bool

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict representation."""
        return {
            "source_a": self.name_a,
            "source_b": self.name_b,
            "added": self.added,
            "removed": self.removed,
            "changed": [
                {"name": c.name, "type_a": c.type_a, "type_b": c.type_b}
                for c in self.changed
            ],
            "common_count": len(self.common),
            "is_identical": self.is_identical,
        }


# ── Public entry point ────────────────────────────────────────────────────────

def schema_diff(
    source_a: Union[str, Relation],
    source_b: Union[str, Relation],
    *,
    name_a: Optional[str] = None,
    name_b: Optional[str] = None,
) -> SchemaDiff:
    """Compare the schemas of two sources and return a structured diff.

    Parameters
    ----------
    source_a, source_b:
        Either a ``Relation`` object or any source string accepted by
        ``nybble.load()`` (file path, URI, connection string, etc.).
    name_a, name_b:
        Optional display names used in the diff output.  Default to the
        relation names derived from loading.

    Returns
    -------
    SchemaDiff
    """
    rel_a = _resolve(source_a)
    rel_b = _resolve(source_b)

    label_a = name_a or rel_a.name
    label_b = name_b or rel_b.name

    schema_a = {c["name"]: c["type"] for c in rel_a.schema()}
    schema_b = {c["name"]: c["type"] for c in rel_b.schema()}

    cols_a = set(schema_a)
    cols_b = set(schema_b)

    added = [
        {"name": c, "type": schema_b[c]}
        for c in sorted(cols_b - cols_a)
    ]
    removed = [
        {"name": c, "type": schema_a[c]}
        for c in sorted(cols_a - cols_b)
    ]
    changed: list[ColumnChange] = []
    common: list[dict] = []
    for col in sorted(cols_a & cols_b):
        ta, tb = schema_a[col], schema_b[col]
        if ta != tb:
            changed.append(ColumnChange(name=col, type_a=ta, type_b=tb))
        else:
            common.append({"name": col, "type": ta})

    return SchemaDiff(
        name_a=label_a,
        name_b=label_b,
        added=added,
        removed=removed,
        changed=changed,
        common=common,
        is_identical=not (added or removed or changed),
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _resolve(source: Union[str, Relation]) -> Relation:
    """Return a Relation for *source*, loading it if necessary."""
    if isinstance(source, Relation):
        return source
    if source in _REGISTERED:
        return _REGISTERED[source]
    return _load(source)
