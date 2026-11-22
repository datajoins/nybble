"""server/ingestion/infer_fk.py - DDL-only shim for the unified FK engine.

The canonical implementation has moved to nybble.core.infer_fk.
This file is a thin shim that calls core.infer_fk.infer_fks() in DDL-only
mode (fetch_stats=None, Tier 4 disabled) because the server pipeline runs at
upload/processing time when no row data is available.

Do not add logic here - edit nybble/core/infer_fk.py instead.

Original public API preserved:
  infer_missing_fks(schema: Schema) -> int
"""
from __future__ import annotations

from nybble.core.infer_fk import infer_fks as _core_infer_fks
from nybble.core.schema import Schema


def infer_missing_fks(schema: Schema) -> int:
    """Add inferred ForeignKey entries where none are declared.

    Delegates to nybble.core.infer_fk.infer_fks() in DDL-only mode
    (fetch_stats=None → Tier 4 statistical sampling disabled).

    Returns the count of FK relationships added.
    """
    # Collect (table, col) pairs already covered by declared FKs
    already_declared: set = set()
    for table in schema.tables:
        for fk in table.foreign_keys:
            for col in fk.constrained_columns:
                already_declared.add((table.name, col))

    # Run the unified engine - DDL-only mode (no row stats)
    candidates = _core_infer_fks(schema, fetch_stats=None)

    added = 0
    for candidate in candidates:
        # Skip if already declared
        if (candidate.src_table, candidate.src_col) in already_declared:
            continue

        tbl = schema.get_table(candidate.src_table)
        if tbl is None:
            continue

        from nybble.core.schema import ForeignKey
        tbl.foreign_keys.append(ForeignKey(
            constrained_columns=[candidate.src_col],
            referred_table=candidate.tgt_table,
            referred_columns=[candidate.tgt_col],
        ))
        already_declared.add((candidate.src_table, candidate.src_col))
        added += 1

    return added
