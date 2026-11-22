"""Live database schema extractor.

Connects to a running database via a connection string or structured params
and extracts schema using SQLAlchemy introspection (inspect / MetaData.reflect).

Supports: PostgreSQL, MySQL/MariaDB, SQLite (file path), and any other
SQLAlchemy-compatible engine.

Usage from run_pipeline_live.py:
    from extract_live_db import extract_from_url
    schema = extract_from_url("postgresql://user:pass@host:5432/mydb")
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .schema_model import Column, ForeignKey, Index, Schema, Table


def _resolve_conn_url(conn_url: str) -> str:
    """Rewrite localhost/127.0.0.1 → host.docker.internal when inside Docker.

    Inside a Docker container ``localhost`` resolves to the container itself,
    not the Docker host.  This rewrite lets users supply the natural
    ``localhost`` form in their connection strings while still reaching a
    database running on the host machine.
    """
    if not Path("/.dockerenv").exists():
        return conn_url
    return re.sub(
        r"(@)(localhost|127\.0\.0\.1)(:\d+/|/)",
        r"\1host.docker.internal\3",
        conn_url,
    )


def extract_from_url(conn_url: str, schema_name: Optional[str] = None) -> Schema:
    """Connect to a live database and introspect its schema.

    Args:
        conn_url:    SQLAlchemy-compatible connection URL.
        schema_name: Optional schema/database name override (useful for PG schemas).

    Returns:
        A normalized Schema object.
    """
    try:
        from sqlalchemy import create_engine, inspect, text
        from sqlalchemy.exc import SQLAlchemyError
    except ImportError:
        raise RuntimeError(
            "SQLAlchemy is required for live database connections. "
            "Install it with: pip install sqlalchemy"
        )

    conn_url = _resolve_conn_url(conn_url)

    # Derive a friendly name from the URL (strip credentials)
    name = schema_name or _url_to_name(conn_url)

    engine = create_engine(conn_url, connect_args={"connect_timeout": 15})
    schema = Schema(name=name)

    try:
        inspector = inspect(engine)
        db_schema = schema_name  # None = default schema

        table_names = inspector.get_table_names(schema=db_schema)
        if not table_names:
            # Try views as well
            table_names = inspector.get_view_names(schema=db_schema)

        for tbl_name in table_names:
            table = Table(name=tbl_name)

            # ── Columns ──────────────────────────────────────────────────
            cols = inspector.get_columns(tbl_name, schema=db_schema)
            for col_info in cols:
                col = Column(
                    name=col_info["name"],
                    data_type=_type_str(col_info.get("type")),
                    nullable=bool(col_info.get("nullable", True)),
                    primary_key=False,
                    default=str(col_info["default"]) if col_info.get("default") is not None else None,
                    unique=False,
                )
                table.columns.append(col)

            # ── Primary key ───────────────────────────────────────────────
            try:
                pk_info = inspector.get_pk_constraint(tbl_name, schema=db_schema)
                pk_cols = pk_info.get("constrained_columns", [])
                table.primary_keys = pk_cols
                for col in table.columns:
                    if col.name in pk_cols:
                        col.primary_key = True
                        col.nullable = False
            except Exception:
                pass

            # ── Foreign keys ──────────────────────────────────────────────
            try:
                fks = inspector.get_foreign_keys(tbl_name, schema=db_schema)
                for fk_info in fks:
                    table.foreign_keys.append(
                        ForeignKey(
                            constrained_columns=fk_info.get("constrained_columns", []),
                            referred_table=fk_info.get("referred_table", ""),
                            referred_columns=fk_info.get("referred_columns", []),
                        )
                    )
            except Exception:
                pass

            # ── Indexes ───────────────────────────────────────────────────
            try:
                idxs = inspector.get_indexes(tbl_name, schema=db_schema)
                for idx_info in idxs:
                    is_unique = bool(idx_info.get("unique", False))
                    idx_cols  = idx_info.get("column_names", [])
                    idx_name  = idx_info.get("name", f"idx_{tbl_name}_{'_'.join(idx_cols or [])}")
                    table.indexes.append(
                        Index(name=str(idx_name), columns=idx_cols, unique=is_unique)
                    )
                    if is_unique and len(idx_cols) == 1:
                        for col in table.columns:
                            if col.name == idx_cols[0]:
                                col.unique = True
            except Exception:
                pass

            schema.tables.append(table)

    finally:
        engine.dispose()

    return schema


def _type_str(sa_type) -> str:
    """Convert a SQLAlchemy type object to a clean string."""
    if sa_type is None:
        return "TEXT"
    try:
        return str(sa_type).upper()
    except Exception:
        return "TEXT"


def _url_to_name(url: str) -> str:
    """Extract a friendly schema name from a connection URL."""
    # Strip credentials: postgres://user:pass@host:port/dbname → dbname
    m = re.search(r"/([^/?#]+)(?:[?#].*)?$", url)
    if m:
        return m.group(1).strip("/") or "live_db"
    return "live_db"
