"""
nybble.core.schema - Canonical normalized intermediate schema representation.

This is the single shared data model produced by every extractor (server/ingestion/)
and consumed by the ERD renderer, FK inference engine, and KG builder.

Rules
-----
* Zero imports from either nybble/ or server/ - this file is the root dependency.
* No DuckDB, SQLite, Flask, click, or argparse imports.
* Adding a new format requires only writing a new extractor that returns a Schema;
  no other file in nybble/core/ needs to change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ForeignKey:
    """One foreign-key constraint (potentially multi-column)."""
    constrained_columns: List[str]
    referred_table: str
    referred_columns: List[str]


@dataclass
class Index:
    """A named index on one or more columns."""
    name: str
    columns: List[str]
    unique: bool


@dataclass
class Column:
    """A single table column."""
    name: str
    data_type: str          # preserved original type string, e.g. "BIGINT", "TEXT"
    nullable: bool = True
    primary_key: bool = False
    default: Optional[str] = None
    unique: bool = False


@dataclass
class Table:
    """A database table with its columns and constraints."""
    name: str
    columns: List[Column] = field(default_factory=list)
    primary_keys: List[str] = field(default_factory=list)        # column names
    foreign_keys: List[ForeignKey] = field(default_factory=list)
    indexes: List[Index] = field(default_factory=list)


@dataclass
class Schema:
    """The root schema object: a named collection of tables."""
    name: str                                    # e.g. database filename or "parsed_dump"
    tables: List[Table] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience helpers used by the ERD renderer and FK engine
    # ------------------------------------------------------------------

    def table_names(self) -> List[str]:
        return [t.name for t in self.tables]

    def get_table(self, name: str) -> Optional[Table]:
        for t in self.tables:
            if t.name == name:
                return t
        return None

    def summary(self) -> str:
        lines = [f"Schema '{self.name}': {len(self.tables)} table(s)"]
        for t in self.tables:
            fk_count = len(t.foreign_keys)
            idx_count = len(t.indexes)
            lines.append(
                f"  {t.name}: {len(t.columns)} col(s), "
                f"{fk_count} FK(s), {idx_count} index(es)"
            )
        return "\n".join(lines)
