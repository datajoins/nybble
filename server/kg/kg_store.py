"""Server-side Knowledge Graph persistence layer for the Nybble Flask application.
The pip-installable equivalent is ``nybble.kg`` (marketed as Nybble GraphAI).
Do not import this file from the ``nybble`` package - use ``nybble.kg`` instead.

kg_store.py - Knowledge Graph Index Store (Sub-Task 1)

Wraps a SQLite database at /output/kg/search.db that backs all KG search
functionality.  Two FTS5 virtual tables index schema metadata and sampled row
data; a plain table stores serialised embedding vectors as BLOBs; a state
table tracks the last-indexed timestamp per source.

Backend env var
---------------
  BACKEND=sqlite    (default) - uses this module's KGStore
  BACKEND=lancedb   - stub; raises NotImplementedError (see SCALING.md)
  BACKEND=qdrant    - stub; raises NotImplementedError (see SCALING.md)

Usage
-----
  from kg_store import open_store, KGStore
  with open_store() as store:
      store.schema_doc_upsert("mykey", "users", "email", "TEXT", False, False, None)
      hits = store.fts_schema("email address")
"""
from __future__ import annotations

import os
import sqlite3
import struct
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Generator, List, Optional

# ── Storage location ──────────────────────────────────────────────────────────
KG_DIR = Path(os.environ.get("KG_DIR", "/output/kg"))

# ── Backend selection ─────────────────────────────────────────────────────────
BACKEND = os.environ.get("BACKEND", "sqlite").lower()

# ── Embedding model selection ─────────────────────────────────────────────────
# "none"   - keyword-only search
# "minilm" - sentence-transformers all-MiniLM-L6-v2 (~90 MB)
# "bge-small" - BAAI/bge-small-en-v1.5 (~130 MB, slightly better)
EMBED_MODEL = os.environ.get("EMBED_MODEL", "none").lower()


# ── Staleness status ───────────────────────────────────────────────────────────
# MARKER:kg-batch-staleness-summary
#
# Ported from UA staleness.ts getGraphFreshnessBatch().
# GraphStalenessStatus mirrors the UA four-value freshness enum; UNKNOWN is
# used when no index_state row exists for a source key.

class GraphStalenessStatus(str, Enum):
    """Staleness status for a single indexed source.

    Mirrors UA ``staleness.ts`` freshness values.
    """
    FRESH   = "fresh"    # last indexed within the TTL window
    STALE   = "stale"    # last indexed outside the TTL window
    UNKNOWN = "unknown"  # never indexed or no state record found


@dataclass
class StalenessSummary:
    """Per-source staleness summary returned by ``KGStore.batch_staleness_summary()``.

    Attributes
    ----------
    source_key      : str    Source key identifying the data source.
    status          : GraphStalenessStatus
    last_indexed_at : float | None  Unix timestamp of the most recent indexing run,
                                    or None if the source has never been indexed.
    schema_doc_count: int    Number of schema documents in the index.
    data_doc_count  : int    Number of data documents in the index.
    reason          : str    Human-readable staleness explanation.
    """
    source_key:       str
    status:           GraphStalenessStatus
    last_indexed_at:  Optional[float]
    schema_doc_count: int = 0
    data_doc_count:   int = 0
    reason:           str = ""


# ── Capability negotiation ─────────────────────────────────────────────────────

@dataclass
class SearchCapabilities:
    """Runtime capability flags checked by kg_search.py."""
    supports_dense_vectors: bool = False
    supports_sparse_vectors: bool = False
    supports_server_side_text: bool = False
    embed_model_name: str = "none"


def get_capabilities() -> SearchCapabilities:
    """Return capabilities based on installed optional packages."""
    caps = SearchCapabilities(embed_model_name=EMBED_MODEL)
    if EMBED_MODEL != "none":
        try:
            import sentence_transformers  # noqa: F401
            caps.supports_dense_vectors = True
        except ImportError:
            pass
        try:
            import fastembed  # noqa: F401
            caps.supports_sparse_vectors = True
        except ImportError:
            pass
    return caps


# ── Schema ─────────────────────────────────────────────────────────────────────

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- FTS5 table for schema-level documents (one doc per column)
CREATE VIRTUAL TABLE IF NOT EXISTS schema_docs USING fts5(
    source_key   UNINDEXED,
    table_name   UNINDEXED,
    column_name  UNINDEXED,
    data_type    UNINDEXED,
    is_pk        UNINDEXED,
    is_fk        UNINDEXED,
    fk_target    UNINDEXED,
    full_text,
    tokenize     = 'porter unicode61'
);

-- FTS5 table for data-level documents (one doc per cell value)
CREATE VIRTUAL TABLE IF NOT EXISTS data_docs USING fts5(
    source_key  UNINDEXED,
    table_name  UNINDEXED,
    pk_col      UNINDEXED,
    pk_val      UNINDEXED,
    col_name    UNINDEXED,
    cell_value,
    full_text,
    tokenize    = 'porter unicode61'
);

-- Dense embedding vectors stored as BLOBs
CREATE TABLE IF NOT EXISTS embeddings (
    id     TEXT    NOT NULL,
    layer  TEXT    NOT NULL,
    vector BLOB    NOT NULL,
    PRIMARY KEY (id, layer)
);

-- Per-source indexing state
CREATE TABLE IF NOT EXISTS index_state (
    source_key        TEXT    PRIMARY KEY,
    schema_indexed_at REAL,
    data_indexed_at   REAL,
    schema_doc_count  INTEGER DEFAULT 0,
    data_doc_count    INTEGER DEFAULT 0
);

-- Per-document incremental index state (content-hash gating, P2-D)
CREATE TABLE IF NOT EXISTS kg_index_state (
    source_key   TEXT NOT NULL,
    content_hash TEXT DEFAULT NULL,
    PRIMARY KEY (source_key)
);
"""


class KGStore:
    """Thin wrapper around a sqlite3 connection providing upsert helpers."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-65536")
        # MARKER:kg-store-wal-enabled
        self._conn.row_factory = sqlite3.Row
        # Apply schema (idempotent)
        for stmt in _DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                self._conn.execute(stmt)
        self._conn.commit()
        # Migrate: add content_hash column if not present (idempotent)
        try:
            self._conn.execute("ALTER TABLE kg_index_state ADD COLUMN content_hash TEXT DEFAULT NULL")
            self._conn.commit()
        except Exception:
            pass  # column already exists
        # Migrate: add indexed_by column if not present (idempotent, P3-E)
        try:
            self._conn.execute("ALTER TABLE kg_index_state ADD COLUMN indexed_by TEXT DEFAULT NULL")
            self._conn.commit()
        except Exception:
            pass  # column already exists
        # Migrate: add structural_hash column if not present (idempotent, P2-A)
        try:
            self._conn.execute("ALTER TABLE kg_index_state ADD COLUMN structural_hash TEXT DEFAULT NULL")
            self._conn.commit()
        except Exception:
            pass  # column already exists

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "KGStore":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ── Schema document upsert ────────────────────────────────────────────────

    def schema_doc_upsert(
        self,
        source_key: str,
        table_name: str,
        column_name: str,
        data_type: str,
        is_pk: bool,
        is_fk: bool,
        fk_target: Optional[str],
    ) -> None:
        """Insert or replace one column-level schema document."""
        full_text = " ".join(filter(None, [
            table_name, column_name, data_type,
            "primary key" if is_pk else "",
            "foreign key" if is_fk else "",
            fk_target or "",
        ]))
        # FTS5 content tables don't have rowid-based upsert - delete + insert
        self._conn.execute(
            "DELETE FROM schema_docs WHERE source_key=? AND table_name=? AND column_name=?",
            (source_key, table_name, column_name),
        )
        self._conn.execute(
            "INSERT INTO schema_docs"
            " (source_key, table_name, column_name, data_type, is_pk, is_fk, fk_target, full_text)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (source_key, table_name, column_name,
             data_type, int(is_pk), int(is_fk), fk_target or "", full_text),
        )
        self._conn.commit()

    def schema_docs_delete_source(self, source_key: str) -> None:
        """Remove all schema documents for a source key (used before re-indexing)."""
        self._conn.execute("DELETE FROM schema_docs WHERE source_key=?", (source_key,))
        self._conn.commit()

    # ── Data document upsert ──────────────────────────────────────────────────

    def data_doc_upsert(
        self,
        source_key: str,
        table_name: str,
        pk_col: str,
        pk_val: str,
        col_name: str,
        cell_value: str,
    ) -> None:
        """Insert or replace one cell-level data document."""
        full_text = f"{table_name} {col_name} {cell_value}"
        self._conn.execute(
            "DELETE FROM data_docs"
            " WHERE source_key=? AND table_name=? AND pk_col=? AND pk_val=? AND col_name=?",
            (source_key, table_name, pk_col, pk_val, col_name),
        )
        self._conn.execute(
            "INSERT INTO data_docs"
            " (source_key, table_name, pk_col, pk_val, col_name, cell_value, full_text)"
            " VALUES (?,?,?,?,?,?,?)",
            (source_key, table_name, pk_col, pk_val, col_name, cell_value, full_text),
        )
        self._conn.commit()

    def data_docs_delete_source(self, source_key: str) -> None:
        """Remove all data documents for a source key."""
        self._conn.execute("DELETE FROM data_docs WHERE source_key=?", (source_key,))
        self._conn.commit()

    def data_docs_batch_upsert(self, rows: list) -> None:
        """Insert many data documents in a single transaction.

        Each element of *rows* must be a tuple:
          (source_key, table_name, pk_col, pk_val, col_name, cell_value)

        Using a single transaction instead of one commit per cell is orders of
        magnitude faster for large sources (50 rows × 20 cols = 1 000 inserts
        committed atomically instead of 1 000 separate fsync calls).
        """
        inserts = []
        for source_key, table_name, pk_col, pk_val, col_name, cell_value in rows:
            full_text = f"{table_name} {col_name} {cell_value}"
            inserts.append((source_key, table_name, pk_col, pk_val, col_name, cell_value, full_text))

        with self._conn:
            # Bulk-delete matching keys first (FTS5 content tables don't support UPSERT)
            self._conn.executemany(
                "DELETE FROM data_docs"
                " WHERE source_key=? AND table_name=? AND pk_col=? AND pk_val=? AND col_name=?",
                [(r[0], r[1], r[2], r[3], r[4]) for r in inserts],
            )
            self._conn.executemany(
                "INSERT INTO data_docs"
                " (source_key, table_name, pk_col, pk_val, col_name, cell_value, full_text)"
                " VALUES (?,?,?,?,?,?,?)",
                inserts,
            )

    # ── Embedding upsert ──────────────────────────────────────────────────────

    def embedding_upsert(self, doc_id: str, layer: str, vector: list) -> None:
        """Store a dense embedding vector (as packed floats) for a document id."""
        blob = struct.pack(f"{len(vector)}f", *vector)
        self._conn.execute(
            "INSERT OR REPLACE INTO embeddings (id, layer, vector) VALUES (?,?,?)",
            (doc_id, layer, blob),
        )
        self._conn.commit()

    def embedding_get(self, doc_id: str, layer: str) -> Optional[list]:
        """Retrieve a stored embedding vector, or None if not found."""
        row = self._conn.execute(
            "SELECT vector FROM embeddings WHERE id=? AND layer=?", (doc_id, layer)
        ).fetchone()
        if row is None:
            return None
        blob = row[0]
        n = len(blob) // 4  # 4 bytes per float32
        return list(struct.unpack(f"{n}f", blob))

    def embeddings_for_layer(self, layer: str) -> List[tuple]:
        """Return all (id, vector_list) pairs for a given layer."""
        rows = self._conn.execute(
            "SELECT id, vector FROM embeddings WHERE layer=?", (layer,)
        ).fetchall()
        result = []
        for row in rows:
            n = len(row[1]) // 4
            vec = list(struct.unpack(f"{n}f", row[1]))
            result.append((row[0], vec))
        return result

    # ── FTS5 queries ──────────────────────────────────────────────────────────

    def fts_schema(self, query: str, limit: int = 20) -> List[sqlite3.Row]:
        """BM25-ranked FTS5 search over schema_docs."""
        return self._conn.execute(
            "SELECT *, rank FROM schema_docs WHERE schema_docs MATCH ?"
            " ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()

    def fts_data(self, query: str, limit: int = 20) -> List[sqlite3.Row]:
        """BM25-ranked FTS5 search over data_docs."""
        return self._conn.execute(
            "SELECT *, rank FROM data_docs WHERE data_docs MATCH ?"
            " ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()

    # ── Index state ───────────────────────────────────────────────────────────

    def last_indexed(self, source_key: str) -> Optional[dict]:
        """Return the index_state row for a source, or None."""
        row = self._conn.execute(
            "SELECT * FROM index_state WHERE source_key=?", (source_key,)
        ).fetchone()
        return dict(row) if row else None

    def mark_indexed(
        self,
        source_key: str,
        schema_doc_count: int,
        data_doc_count: int,
        *,
        schema: bool = True,
        data: bool = True,
        actor_id: Optional[str] = None,
    ) -> None:
        """Update index_state after a successful index run."""
        now = time.time()
        existing = self.last_indexed(source_key)
        if existing is None:
            self._conn.execute(
                "INSERT INTO index_state"
                " (source_key, schema_indexed_at, data_indexed_at,"
                "  schema_doc_count, data_doc_count)"
                " VALUES (?,?,?,?,?)",
                (source_key,
                 now if schema else None,
                 now if data else None,
                 schema_doc_count, data_doc_count),
            )
        else:
            updates = []
            params: list = []
            if schema:
                updates += ["schema_indexed_at=?", "schema_doc_count=?"]
                params += [now, schema_doc_count]
            if data:
                updates += ["data_indexed_at=?", "data_doc_count=?"]
                params += [now, data_doc_count]
            if updates:
                params.append(source_key)
                self._conn.execute(
                    f"UPDATE index_state SET {', '.join(updates)} WHERE source_key=?",
                    params,
                )
        # Store actor provenance in kg_index_state (P3-E)
        if actor_id is not None:
            self._conn.execute(
                "INSERT INTO kg_index_state (source_key, indexed_by)"
                " VALUES (?, ?)"
                " ON CONFLICT(source_key) DO UPDATE SET indexed_by=excluded.indexed_by",
                (source_key, actor_id),
            )
        self._conn.commit()

    def all_index_states(self) -> List[dict]:
        """Return all index_state rows as dicts."""
        rows = self._conn.execute("SELECT * FROM index_state").fetchall()
        return [dict(r) for r in rows]

    def get_staleness_summary(
        self,
        source_key: str,
        staleness_ttl_s: float = 3600.0,
    ) -> StalenessSummary:
        """Return a :class:`StalenessSummary` for a single source key.

        Convenience wrapper around :meth:`batch_staleness_summary` for
        single-source callers.
        """
        summaries = self.batch_staleness_summary(
            source_keys=[source_key],
            staleness_ttl_s=staleness_ttl_s,
        )
        return summaries[source_key]

    def batch_staleness_summary(
        self,
        source_keys: Optional[List[str]] = None,
        staleness_ttl_s: float = 3600.0,
    ) -> Dict[str, StalenessSummary]:
        """Return :class:`StalenessSummary` for all (or specified) source keys.

        # MARKER:kg-batch-staleness-summary

        Calls ``all_index_states()`` exactly once and evaluates staleness for
        all entries in a single pass.  Mirrors UA ``staleness.ts``
        ``getGraphFreshnessBatch()``.

        Parameters
        ----------
        source_keys
            Optional list of keys to check.  If *None*, all keys present in
            ``index_state`` are returned.  Keys not found in ``index_state``
            are returned with status ``UNKNOWN``.
        staleness_ttl_s
            Maximum age (seconds) for a source to be considered FRESH.
            Default: 3600 (1 hour).

        Returns
        -------
        Dict[str, StalenessSummary]
            One entry per key in *source_keys* (or all indexed keys when
            *source_keys* is None).
        """
        all_states = self.all_index_states()
        state_by_key: Dict[str, dict] = {row["source_key"]: row for row in all_states}

        keys_to_check: List[str] = (
            source_keys if source_keys is not None else list(state_by_key.keys())
        )

        result: Dict[str, StalenessSummary] = {}
        now = time.time()
        for key in keys_to_check:
            row = state_by_key.get(key)
            if row is None:
                result[key] = StalenessSummary(
                    source_key=key,
                    status=GraphStalenessStatus.UNKNOWN,
                    last_indexed_at=None,
                    schema_doc_count=0,
                    data_doc_count=0,
                    reason="No index state found",
                )
                continue
            indexed_at = row.get("schema_indexed_at") or row.get("data_indexed_at") or 0.0
            age_s = now - indexed_at
            status = (
                GraphStalenessStatus.FRESH
                if age_s < staleness_ttl_s
                else GraphStalenessStatus.STALE
            )
            result[key] = StalenessSummary(
                source_key=key,
                status=status,
                last_indexed_at=indexed_at,
                schema_doc_count=row.get("schema_doc_count", 0),
                data_doc_count=row.get("data_doc_count", 0),
                reason=f"Age {age_s:.0f}s (TTL {staleness_ttl_s:.0f}s)",
            )
        return result

    def has_doc_changed(self, doc_id: str, content_hash: str) -> bool:
        """Return True if doc_id is new or its stored hash differs from content_hash.
        # MARKER:kg-incremental-indexing
        """
        row = self._conn.execute(
            "SELECT content_hash FROM kg_index_state WHERE source_key = ? LIMIT 1",
            (doc_id,)
        ).fetchone()
        if row is None:
            return True
        return row[0] != content_hash

    def set_doc_hash(self, doc_id: str, content_hash: str) -> None:
        """Persist the content hash for a document (used after successful embedding)."""
        self._conn.execute(
            "INSERT OR REPLACE INTO kg_index_state (source_key, content_hash) VALUES (?, ?)",
            (doc_id, content_hash),
        )
        self._conn.commit()

    def set_doc_fingerprint(self, doc_id: str, content_hash: str, structural_hash: str) -> None:
        """Persist both content and structural hashes for a document.

        # MARKER:kg-structural-fingerprint
        Stores the full fingerprint so future runs can distinguish COSMETIC
        from STRUCTURAL changes without re-parsing unchanged files.
        """
        self._conn.execute(
            "INSERT INTO kg_index_state (source_key, content_hash, structural_hash)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(source_key) DO UPDATE SET"
            "   content_hash=excluded.content_hash,"
            "   structural_hash=excluded.structural_hash",
            (doc_id, content_hash, structural_hash),
        )
        self._conn.commit()

    def has_structure_changed(self, doc_id: str, content_hash: str, structural_hash: str) -> bool:
        """Return True only when the structural signature has changed.

        Returns False (skip re-index) when:
          - content_hash is identical (NONE change level), OR
          - content_hash differs but structural_hash is unchanged (COSMETIC).

        Returns True (re-index needed) when:
          - doc_id is new (never indexed), OR
          - structural_hash differs (STRUCTURAL change).

        When structural_hash is stored as NULL (legacy row written before P2-A)
        the comparison is conservative: returns True so the document is
        re-indexed and the new structural_hash gets stored.
        """
        row = self._conn.execute(
            "SELECT content_hash, structural_hash FROM kg_index_state WHERE source_key = ? LIMIT 1",
            (doc_id,),
        ).fetchone()
        if row is None:
            return True  # new document
        stored_content, stored_struct = row[0], row[1]
        if stored_content == content_hash:
            return False  # NONE - identical content, skip
        if stored_struct is None:
            return True   # legacy row - conservative: treat as STRUCTURAL
        return stored_struct != structural_hash  # STRUCTURAL if sig changed

    def schema_doc_count(self, source_key: str) -> int:
        """Count schema docs for a source key."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM schema_docs WHERE source_key=?", (source_key,)
        ).fetchone()
        return row[0] if row else 0

    def data_doc_count(self, source_key: str) -> int:
        """Count data docs for a source key."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM data_docs WHERE source_key=?", (source_key,)
        ).fetchone()
        return row[0] if row else 0


# ── Factory / context manager ─────────────────────────────────────────────────

@contextmanager
def open_store(db_path: Optional[Path] = None) -> Generator[KGStore, None, None]:
    """Context manager that opens and closes a KGStore."""
    if db_path is None:
        db_path = KG_DIR / "search.db"
    store = _open_store_raw(db_path)
    try:
        yield store
    finally:
        store.close()


def _open_store_raw(db_path: Optional[Path] = None) -> KGStore:
    """Open a KGStore without context-manager semantics (caller must close)."""
    if db_path is None:
        db_path = KG_DIR / "search.db"
    if BACKEND == "sqlite":
        return KGStore(db_path)
    elif BACKEND == "lancedb":
        raise NotImplementedError(
            "LanceDB backend is not yet implemented. "
            "See SCALING.md § Tier 1 for upgrade instructions."
        )
    elif BACKEND in ("qdrant", "opensearch"):
        raise NotImplementedError(
            f"{BACKEND} backend is not yet implemented. "
            "See SCALING.md § Tier 2 for upgrade instructions."
        )
    else:
        raise ValueError(f"Unknown BACKEND={BACKEND!r}. Supported: sqlite, lancedb, qdrant")


# ── Scaling stubs (documented in SCALING.md) ──────────────────────────────────

class LanceDBStore:
    """Stub for LanceDB-backed vector store (Tier 1 upgrade).

    Install: pip install lancedb>=0.6 fastembed>=0.5.1
    Config:  BACKEND=lancedb
    See SCALING.md for full migration steps.
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "LanceDB backend requires `pip install lancedb>=0.6`. "
            "See SCALING.md § Tier 1."
        )
