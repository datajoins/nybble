"""Path A - SQLite schema extractor.

Uses only Python's built-in sqlite3 module and SQLite PRAGMA statements.
Contains zero references to any specific table or column names.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .schema_model import Column, ForeignKey, Index, Schema, Table


def extract(db_path: str | Path) -> Schema:
    """Introspect a SQLite database file and return a normalized Schema."""
    db_path = Path(db_path)
    schema = Schema(name=db_path.stem)

    # Open with a plain path rather than the URI ?mode=ro form.
    # The URI read-only mode fails when the database was created with WAL
    # journaling and the -shm/-wal sidecar files are absent (e.g. inside a
    # container where only the main .db file was copied).  A plain connect()
    # is safe for introspection-only use: we never write to the file.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        table_names = _get_table_names(conn)
        for table_name in table_names:
            table = _extract_table(conn, table_name)
            schema.tables.append(table)
    finally:
        conn.close()

    return schema


# ---------------------------------------------------------------------------
# Internal helpers - all generic, zero schema-specific knowledge
# ---------------------------------------------------------------------------

def _get_table_names(conn: sqlite3.Connection) -> list[str]:
    """Return all user-visible table names (excluding SQLite internals)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def _extract_table(conn: sqlite3.Connection, table_name: str) -> Table:
    table = Table(name=table_name)

    # ---- Columns via PRAGMA table_info ----
    # Returns: cid, name, type, notnull, dflt_value, pk
    col_rows = conn.execute(f"PRAGMA table_info({_q(table_name)})").fetchall()
    pk_col_names: list[str] = []

    # Collect multi-column PK members first (pk field is 1-based position)
    pk_positions: dict[int, str] = {}
    for row in col_rows:
        if row["pk"] > 0:
            pk_positions[row["pk"]] = row["name"]
    table.primary_keys = [pk_positions[i] for i in sorted(pk_positions)]

    for row in col_rows:
        is_pk = row["pk"] > 0
        col = Column(
            name=row["name"],
            data_type=row["type"] or "TEXT",
            nullable=not bool(row["notnull"]),
            primary_key=is_pk,
            default=row["dflt_value"],
            unique=False,  # filled in from index scan below
        )
        table.columns.append(col)

    # ---- Indexes via PRAGMA index_list + PRAGMA index_info ----
    idx_rows = conn.execute(f"PRAGMA index_list({_q(table_name)})").fetchall()
    unique_col_names: set[str] = set()

    for idx_row in idx_rows:
        idx_name: str = idx_row["name"]
        is_unique: bool = bool(idx_row["unique"])
        is_partial: bool = bool(idx_row["partial"]) if "partial" in idx_row.keys() else False

        info_rows = conn.execute(f"PRAGMA index_info({_q(idx_name)})").fetchall()
        idx_columns = [r["name"] for r in info_rows]

        table.indexes.append(
            Index(name=idx_name, columns=idx_columns, unique=is_unique)
        )

        # Single-column unique index → mark the column itself
        if is_unique and len(idx_columns) == 1:
            unique_col_names.add(idx_columns[0])

    # Apply unique flag back to Column objects
    for col in table.columns:
        if col.name in unique_col_names:
            col.unique = True

    # ---- Foreign keys via PRAGMA foreign_key_list ----
    fk_rows = conn.execute(f"PRAGMA foreign_key_list({_q(table_name)})").fetchall()

    # Group multi-column FKs by their id field
    fk_groups: dict[int, list] = {}
    for row in fk_rows:
        fk_groups.setdefault(row["id"], []).append(row)

    for fk_id, rows in fk_groups.items():
        # Sort by seq to preserve column order
        rows_sorted = sorted(rows, key=lambda r: r["seq"])
        table.foreign_keys.append(
            ForeignKey(
                constrained_columns=[r["from"] for r in rows_sorted],
                referred_table=rows_sorted[0]["table"],
                referred_columns=[r["to"] for r in rows_sorted],
            )
        )

    return table


def _q(name: str) -> str:
    """Double-quote a SQLite identifier, escaping embedded double-quotes."""
    return '"' + name.replace('"', '""') + '"'
