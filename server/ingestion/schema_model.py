"""server/ingestion/schema_model.py - re-export shim.

The canonical implementation has moved to nybble.core.schema.
This file exists solely for backward compatibility so that all existing
server/ imports (``from server.ingestion.schema_model import Schema``) continue
to work without modification.

Do not add logic here - edit nybble/core/schema.py instead.
"""
# Re-export everything from the canonical location.
from nybble.core.schema import (  # noqa: F401
    ForeignKey,
    Index,
    Column,
    Table,
    Schema,
)

__all__ = ["ForeignKey", "Index", "Column", "Table", "Schema"]
