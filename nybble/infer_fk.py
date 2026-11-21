"""
infer_fk.py - CLI adapter for the unified FK inference engine.

This module is a thin adapter that:
  1. Converts DuckDB-registered Relation objects into a core.Schema dataclass.
  2. Populates Tier-1 FK constraints from live DB introspection (PRAGMA / SQLAlchemy).
  3. Provides a DuckDB-backed fetch_stats callback for Tier-4 statistical sampling.
  4. Delegates all inference logic to nybble.core.infer_fk.infer_fks().

Public API (unchanged from pre-integration)
-------------------------------------------
  infer_fks(relations, con=None) → list[FKCandidate]
  FKCandidate                    - re-exported from nybble.core.infer_fk
"""
from __future__ import annotations

from typing import Optional

from nybble.core.infer_fk import FKCandidate, infer_fks as _core_infer_fks
from nybble.core.schema import Column, ForeignKey, Schema, Table
from nybble.resolver import session, _SESSION_LOCK, Relation


# Re-export FKCandidate so existing callers (nybble.infer_fk.FKCandidate) still work
__all__ = ["FKCandidate", "infer_fks"]


# ---------------------------------------------------------------------------
# DuckDB helpers (live DB schema + stats callbacks)
# ---------------------------------------------------------------------------

def _duckdb_get_schema_cols(relation_name: str) -> list:
    """Return column list as [{"name": str, "type": str}] from DuckDB."""
    con = session()
    with _SESSION_LOCK:
        try:
            rows = con.execute(
                f'DESCRIBE SELECT * FROM "{relation_name}" LIMIT 0'
            ).fetchall()
            return [{"name": r[0], "type": r[1]} for r in rows]
        except Exception:
            return []


def _make_duckdb_fetch_stats(con, lock, display_to_raw=None):
    """Return a fetch_stats callback backed by the DuckDB session.

    Parameters
    ----------
    con:
        Active DuckDB connection.
    lock:
        Threading lock protecting the connection.
    display_to_raw:
        Optional dict mapping display table names → raw DuckDB view names.
        Required when the Schema was built with display names (SQL dump / SQLite
        multi-table loaders) so that DuckDB queries use the correct view name.
    """
    def fetch_stats(table_name: str, col_name: str):
        # Translate display name back to raw DuckDB view name for the query
        raw_name = (display_to_raw or {}).get(table_name, table_name)
        with lock:
            try:
                row_d = con.execute(
                    f'SELECT COUNT(DISTINCT "{col_name}") FROM "{raw_name}"'
                ).fetchone()
                row_t = con.execute(
                    f'SELECT COUNT("{col_name}") FROM "{raw_name}"'
                ).fetchone()
                distinct = row_d[0] if row_d else 0
                total    = row_t[0] if row_t else 0
                return distinct, total
            except Exception:
                return 0, 0
    return fetch_stats


# ---------------------------------------------------------------------------
# Live DB Tier-1: explicit FK constraints from DB introspection
# ---------------------------------------------------------------------------

def _extract_tier1_sqlite(relation: Relation) -> list[ForeignKey]:
    """Read declared FK constraints from a SQLite-attached DB via PRAGMA."""
    fks: list[ForeignKey] = []
    con = session()
    schema_name = relation.name
    alias = f"_attach_{schema_name}"
    with _SESSION_LOCK:
        try:
            tables = con.execute(
                f"SELECT table_name FROM information_schema.tables "
                f"WHERE table_catalog='{alias}' AND table_schema='main'"
            ).fetchall()
            for (tbl,) in tables:
                fk_rows = con.execute(
                    f"PRAGMA {schema_name}.foreign_key_list('{tbl}')"
                ).fetchall()
                for row in fk_rows:
                    # PRAGMA cols: id, seq, table, from, to, on_update, on_delete, match
                    fks.append(ForeignKey(
                        constrained_columns=[row[3]],
                        referred_table=row[2],
                        referred_columns=[row[4]],
                    ))
        except Exception:
            pass
    return fks


def _extract_tier1_sqlalchemy(relation: Relation) -> list[ForeignKey]:
    """Read declared FK constraints via SQLAlchemy inspection."""
    fks: list[ForeignKey] = []
    try:
        from sqlalchemy import create_engine, inspect as sa_inspect
        engine = create_engine(relation.source)
        inspector = sa_inspect(engine)
        for tbl in inspector.get_table_names():
            for fk in inspector.get_foreign_keys(tbl):
                for src_col, tgt_col in zip(
                    fk["constrained_columns"], fk["referred_columns"]
                ):
                    fks.append(ForeignKey(
                        constrained_columns=[src_col],
                        referred_table=fk["referred_table"],
                        referred_columns=[tgt_col],
                    ))
        engine.dispose()
    except Exception:
        pass
    return fks


def _relations_to_schema(
    relations: list[Relation],
    display_name_fn=None,
) -> Schema:
    """Convert DuckDB-registered Relations into a core.Schema.

    Populates Tier-1 FK constraints from live DB introspection (SQLite PRAGMA
    or SQLAlchemy) so the core engine can treat them as Tier-1 declared FKs.

    Parameters
    ----------
    relations:
        List of Relation objects to analyse.
    display_name_fn:
        Optional callable ``(rel_name: str) -> str`` that maps a raw DuckDB
        relation name to its display name.  When provided the resulting Schema
        table names use display names so they match the schema built by
        ``_duckdb_schema_to_core()`` in ``erd.py``.  When None the raw DuckDB
        names are used (backward-compat for callers that don't need display names).
    """
    tables = []
    for rel in relations:
        duckdb_cols = _duckdb_get_schema_cols(rel.name)
        core_cols = [Column(name=c["name"], data_type=c["type"]) for c in duckdb_cols]

        # Pull live DB FK constraints for Tier 1
        live_fks: list[ForeignKey] = []
        if rel.source_type == "db":
            if rel.source.startswith("sqlite:///"):
                live_fks = _extract_tier1_sqlite(rel)
            else:
                live_fks = _extract_tier1_sqlalchemy(rel)

        table_name = display_name_fn(rel.name) if display_name_fn else rel.name
        tables.append(Table(
            name=table_name,
            columns=core_cols,
            foreign_keys=live_fks,
        ))

    return Schema(name="duckdb_session", tables=tables)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def infer_fks(
    relations: Optional[list[Relation]] = None,
    display_name_fn=None,
) -> list[FKCandidate]:
    """Run all four FK inference tiers and return deduplicated candidates.

    Parameters
    ----------
    relations:
        List of Relation objects to analyse. Defaults to all registered relations.
    display_name_fn:
        Optional callable ``(raw_name: str) -> str`` mapping raw DuckDB view names
        to the display names used in the ERD.  Must be the same function used by
        ``_duckdb_schema_to_core()`` so that FK candidate table names match the
        Schema table names consumed by ``build_erd_data()``.

        Example: passing ``nybble.erd._display_name`` ensures that a relation
        registered as ``umami_backup_20261018_020002_sql__website`` produces FK
        candidates with ``src_table="website"`` / ``tgt_table="website"`` -
        matching the Schema built by ``_duckdb_schema_to_core()``.

    Returns
    -------
    list[FKCandidate]
        Sorted by tier (ascending) then confidence (descending).
    """
    from nybble.resolver import _REGISTERED

    if relations is None:
        relations = list(_REGISTERED.values())

    if not relations:
        return []

    # Build core.Schema from DuckDB relations (includes live Tier-1 FKs).
    # Pass display_name_fn so Schema table names match the ERD node IDs.
    schema = _relations_to_schema(relations, display_name_fn=display_name_fn)

    # Build reverse map: display_name → raw DuckDB view name for fetch_stats
    display_to_raw: dict = {}
    if display_name_fn:
        for rel in relations:
            display_to_raw[display_name_fn(rel.name)] = rel.name

    # Provide DuckDB-backed fetch_stats for Tier 4
    con = session()
    fetch_stats = _make_duckdb_fetch_stats(con, _SESSION_LOCK, display_to_raw=display_to_raw)

    return _core_infer_fks(schema, fetch_stats=fetch_stats)
