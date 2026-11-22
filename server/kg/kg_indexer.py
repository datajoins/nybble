"""Server-side Knowledge Graph index builder for the Nybble Flask application.
The pip-installable equivalent is ``nybble.kg`` (marketed as Nybble GraphAI).
Do not import this file from the ``nybble`` package - use ``nybble.kg`` instead.

kg_indexer.py - Knowledge Graph Index Builder (Sub-Task 2)

Reads schema.json files and (optionally) sampled rows from SQLite source DBs,
then populates the KGStore FTS5 tables so that kg_search.py can query them.

Can be run directly:
    python kg_indexer.py              # index every registered source
    python kg_indexer.py --key <key>  # index one source

Or called from Flask:
    from kg_indexer import index_source
    threading.Thread(target=index_source, args=(key,), daemon=True).start()
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .kg_fingerprint import (
    ChangeLevel,
    UpdateDecision,
    ChangeClassifier,
    _extract_python_fingerprint,
    classify_change_level,
)

# ── Paths (mirrors api_server.py) ────────────────────────────────────────────
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/output/erd"))
REGISTRY   = Path(os.environ.get("REGISTRY",  "/output/registry.json"))

# Max rows sampled per table for data indexing
SAMPLE_ROWS = int(os.environ.get("KG_SAMPLE_ROWS", "50"))

# ── HTTP embedding provider configuration (P2-B) ──────────────────────────────
EMBED_PROVIDER = os.environ.get("EMBED_PROVIDER", "none").lower()
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "https://api.openai.com/v1")
EMBED_API_KEY  = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("OPENROUTER_API_KEY", "")
EMBED_DIM      = int(os.environ.get("EMBED_DIM", "256"))
EMBED_DEADLINE = float(os.environ.get("EMBED_DEADLINE_S", "30"))
EMBED_RETRIES  = int(os.environ.get("EMBED_RETRIES", "3"))

logger = logging.getLogger(__name__)


# ── Source-key path sanitisation ─────────────────────────────────────────────
# MARKER:kg-source-key-sanitise
#
# Ported from UA persistence/index.ts sanitiseFilePaths().
# Converts container-absolute paths (e.g. /output/erd/mydb/schema.json) to
# project-relative form (mydb/schema.json) so that source_key values in
# cross-source edges and API responses never expose internal directory layouts.

def _sanitise_source_key(raw_key: str, base_dir: str) -> str:
    """Convert an absolute *raw_key* path to a *base_dir*-relative path.

    Mirrors UA ``persistence/index.ts sanitiseFilePaths()``.

    Rules (matching UA behaviour):
      - If *raw_key* is absolute and starts with *base_dir*: return the
        relative sub-path (``Path(raw_key).relative_to(base_dir)``).
      - If *raw_key* is absolute but NOT under *base_dir*: return the
        path stem only (last component without suffix - mirrors UA
        ``basename`` fallback for paths outside projectRoot).
      - If *raw_key* is already relative or otherwise not an absolute path:
        return *raw_key* unchanged.
      - Empty string: return unchanged.

    Parameters
    ----------
    raw_key  : str   Raw source key, possibly an absolute path.
    base_dir : str   Base directory to relativise against (e.g. OUTPUT_DIR).

    Returns
    -------
    str  Relative or basename source key.
    """
    if not raw_key:
        return raw_key
    try:
        p = Path(raw_key)
        if p.is_absolute():
            base = Path(base_dir)
            try:
                return str(p.relative_to(base))
            except ValueError:
                # Outside base_dir: return stem (last component, no suffix)
                return p.stem
    except Exception as exc:
        logger.debug(
            "_sanitise_source_key: cannot parse path",
            extra={"raw_key": raw_key, "error": str(exc), "outcome": "passthrough"},
        )
    return raw_key

# ── Serialisation lock (prevents concurrent writes to the same store) ────────
_index_lock = threading.Lock()


# ── Registry helpers ──────────────────────────────────────────────────────────

def _load_registry() -> dict:
    if REGISTRY.exists():
        try:
            return json.loads(REGISTRY.read_text(encoding="utf-8"))
        except Exception as _exc:
            logger.debug(
                "_load_registry: cannot parse registry.json",
                extra={"error": str(_exc), "outcome": "skip"},
            )
    return {}


def _find_sqlite_db(key: str) -> Optional[Path]:
    """Locate the materialised SQLite file for a source key.

    Mirrors the _find_sqlite_db logic from api_server.py.
    """
    # 1. UPLOAD_DIR/<key>/*.db / *.sqlite
    upload_dir = Path(os.environ.get("UPLOAD_DIR", "/uploads")) / key
    if upload_dir.exists():
        for f in sorted(upload_dir.iterdir()):
            if f.suffix.lower() in (".db", ".sqlite"):
                return f
    # 2. OUTPUT_DIR/<key>/*.db
    output_dir = OUTPUT_DIR / key
    if output_dir.exists():
        for f in sorted(output_dir.iterdir()):
            if f.suffix.lower() in (".db", ".sqlite"):
                return f
    return None


# ── Embedding helper (optional, graceful degradation) ────────────────────────

_embed_model = None   # lazy loaded
_embed_lock  = threading.Lock()


def _get_embedder() -> Optional[object]:
    """Return a callable(text) → list[float], or None if not available."""
    global _embed_model
    with _embed_lock:
        if _embed_model is not None:
            return _embed_model
        embed_model_name = os.environ.get("EMBED_MODEL", "none").lower()
        if embed_model_name == "none":
            return None
        try:
            from sentence_transformers import SentenceTransformer
            name_map = {
                "minilm": "all-MiniLM-L6-v2",
                "bge-small": "BAAI/bge-small-en-v1.5",
            }
            model_name = name_map.get(embed_model_name, embed_model_name)
            _embed_model = SentenceTransformer(model_name)
            return _embed_model
        except ImportError:
            logger.debug("sentence-transformers not installed - semantic search disabled")
            return None
        except Exception as exc:
            logger.warning("Failed to load embedding model: %s", exc)
            return None


def _normalize(vec: list) -> Optional[List[float]]:
    """L2-normalize a float vector. Returns None if zero-norm."""
    norm = math.sqrt(sum(v * v for v in vec))
    if norm < 1e-9:
        return None
    return [v / norm for v in vec]


def _http_embed(text: str) -> Optional[list]:
    """POST to OpenAI-compatible /embeddings endpoint with retry + deadline.
    # MARKER:kg-embedding-http-provider
    Uses only stdlib (urllib.request). Returns None when EMBED_PROVIDER=none.
    """
    import json as _json
    import time as _time
    import urllib.request
    import urllib.error

    if EMBED_PROVIDER == "none" or not EMBED_API_KEY:
        return None
    payload = _json.dumps({
        "model": os.environ.get("EMBED_MODEL", "text-embedding-3-small"),
        "input": [text[:8000]],
        "dimensions": EMBED_DIM,
    }).encode()
    deadline = _time.monotonic() + EMBED_DEADLINE
    for attempt in range(max(1, EMBED_RETRIES)):
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            break
        try:
            req = urllib.request.Request(
                f"{EMBED_BASE_URL.rstrip('/')}/embeddings",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {EMBED_API_KEY}",
                },
            )
            with urllib.request.urlopen(req, timeout=min(remaining, 20)) as resp:
                data = _json.loads(resp.read())
            vec = data["data"][0]["embedding"]
            return _normalize(vec)
        except Exception:
            if attempt < EMBED_RETRIES - 1:
                _time.sleep(0.2 * (2 ** attempt))
    return None


def _embed(text: str) -> Optional[list]:
    """Return a list of floats embedding, or None if embeddings unavailable.

    Resolution order:
      1. EMBED_PROVIDER=<http provider> → _http_embed (stdlib urllib.request)
      2. EMBED_PROVIDER=local (or unset) + EMBED_MODEL != none → sentence-transformers
      3. Anything else → None (keyword-only mode)
    """
    if EMBED_PROVIDER not in ("none", "local"):
        # HTTP provider path (P2-B)
        return _http_embed(text)
    # Local sentence-transformers path
    model = _get_embedder()
    if model is None:
        return None
    try:
        return model.encode(text, normalize_embeddings=True).tolist()
    except Exception as exc:
        logger.debug("Embedding failed for %r: %s", text[:80], exc)
        return None


# ── Core indexing functions ───────────────────────────────────────────────────

def _find_xlsx_upload(key: str) -> Optional[Path]:
    """Return the first .xlsx file in the upload dir for *key*, or None."""
    upload_dir = Path(os.environ.get("UPLOAD_DIR", "/uploads")) / key
    if upload_dir.exists():
        for f in sorted(upload_dir.iterdir()):
            if f.suffix.lower() in (".xlsx", ".xls"):
                return f
    return None


def _xlsx_to_schema_dict(xlsx_path: Path) -> dict:
    """Convert an Excel workbook to the internal Liam schema.json dict format.

    Each sheet becomes a table entry under ``{"tables": {…}}``.
    Uses :mod:`extract_xlsx` which is already a project dependency.
    """
    try:
        from extract_xlsx import extract as _extract_xlsx
    except ImportError as exc:
        raise ImportError(
            "extract_xlsx module not found - cannot index xlsx schema"
        ) from exc

    schema = _extract_xlsx(xlsx_path)

    tables_out: dict = {}
    for table in schema.tables:
        cols_out = {
            col.name: {
                "name":    col.name,
                "type":    col.data_type,
                "notNull": not col.nullable,
                "default": col.default,
            }
            for col in table.columns
        }

        constraints_out: dict = {}
        pk_cols = table.primary_keys or [c.name for c in table.columns if c.primary_key]
        if pk_cols:
            constraints_out[f"pk_{table.name}"] = {
                "type":        "PRIMARY KEY",
                "columnNames": pk_cols,
            }
        for i, fk in enumerate(table.foreign_keys):
            key_name = f"fk_{table.name}_{'_'.join(fk.constrained_columns)}_{i}"
            constraints_out[key_name] = {
                "type":              "FOREIGN KEY",
                "columnNames":       fk.constrained_columns,
                "targetTableName":   fk.referred_table,
                "targetColumnNames": fk.referred_columns,
            }

        tables_out[table.name] = {
            "name":        table.name,
            "columns":     cols_out,
            "constraints": constraints_out,
        }

    return {"tables": tables_out, "enums": {}}


def index_schema(key: str, schema_json_path: Path, store) -> int:
    """Index schema metadata for one source key.  Returns doc count.

    The *schema_json_path* must point to a ``schema.json`` file in the
    Liam keyed-object format.  If the file does not exist but an ``.xlsx``
    upload is present for this key, the schema is extracted from the
    workbook on-the-fly and indexed directly (without writing a new file).
    """
    import hashlib

    # ── Determine the raw schema dict ────────────────────────────────────────
    if schema_json_path.exists():
        try:
            raw = json.loads(schema_json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "Cannot read schema.json",
                extra={"source_key": key, "error": str(exc), "outcome": "skip"},
            )
            return 0
    else:
        # No schema.json - try to derive it from an uploaded .xlsx file
        xlsx_path = _find_xlsx_upload(key)
        if xlsx_path is None:
            logger.info(
                "kg_indexer: no schema.json or xlsx - skipping",
                extra={"source_key": key, "outcome": "skip"},
            )
            return 0
        try:
            raw = _xlsx_to_schema_dict(xlsx_path)
            logger.info(
                "kg_indexer: derived schema from xlsx",
                extra={"source_key": key, "xlsx": xlsx_path.name, "outcome": "ok"},
            )
        except Exception as exc:
            logger.warning(
                "Cannot extract xlsx schema",
                extra={"source_key": key, "error": str(exc), "outcome": "skip"},
            )
            return 0

    tables = raw.get("tables", {})
    doc_count = 0

    # Delete existing schema docs for this source first
    store.schema_docs_delete_source(key)

    for table_name, table_data in tables.items():
        constraints = table_data.get("constraints", {})
        # Build set of FK columns  (schema.json uses "columnNames" / "targetTableName")
        fk_cols: dict[str, str] = {}
        pk_cols: set = set()
        for cname, cdata in constraints.items():
            ctype = cdata.get("type", "")
            if ctype == "FOREIGN KEY":
                for col in cdata.get("columnNames", cdata.get("columns", [])):
                    ref = cdata.get("targetTableName", cdata.get("ref_table", ""))
                    fk_cols[col] = ref
            elif ctype == "PRIMARY KEY":
                for col in cdata.get("columnNames", cdata.get("columns", [])):
                    pk_cols.add(col)

        columns = table_data.get("columns", {})
        for col_name, col_data in columns.items():
            data_type = col_data.get("type", "TEXT")
            is_pk = col_name in pk_cols
            is_fk = col_name in fk_cols
            fk_target = fk_cols.get(col_name)

            store.schema_doc_upsert(
                key, table_name, col_name, data_type, is_pk, is_fk, fk_target
            )

            # Optional: store embedding for this column doc (with fingerprint gating)
            full_text = " ".join(filter(None, [
                table_name, col_name, data_type,
                "primary key" if is_pk else "",
                "foreign key" if is_fk else "",
                fk_target or "",
            ]))
            doc_id = f"{key}:{table_name}:{col_name}"
            fp = _extract_python_fingerprint(full_text)
            content_hash   = fp["content_hash"]
            structural_hash = fp["structural_hash"]
            # Only re-embed when structure has changed (skips NONE and COSMETIC)
            if not store.has_structure_changed(doc_id, content_hash, structural_hash):
                doc_count += 1
                continue  # skip - content unchanged or only cosmetic diff
            vec = _embed(full_text)
            if vec is not None:
                store.embedding_upsert(doc_id, "schema", vec)
            store.set_doc_fingerprint(doc_id, content_hash, structural_hash)

            doc_count += 1

    return doc_count


def index_data(key: str, db_path: Path, store, sample_rows: int = SAMPLE_ROWS) -> int:
    """Index sampled row data for one source key.  Returns doc count.

    All inserts are batched into a single SQLite transaction to avoid the
    performance penalty of one commit per cell (N_rows × N_cols commits).
    """
    import hashlib

    doc_count = 0

    t0 = time.monotonic()
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        logger.warning(
            "Cannot open SQLite for data indexing",
            extra={"source_key": key, "error": str(exc), "outcome": "skip"},
        )
        return 0

    # Delete existing data docs for this source first
    store.data_docs_delete_source(key)

    # Collect all (source_key, table, pk_col, pk_val, col_name, cell_value) tuples
    # then write them in a single batch transaction.
    batch: list = []
    # Track doc_ids with their hashes for embedding after batch insert
    embed_candidates: list = []

    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()

        for (table_name,) in tables:
            try:
                rows = conn.execute(f'SELECT * FROM "{table_name}" LIMIT ?', (sample_rows,)).fetchall()
            except Exception as _exc:
                logger.debug(
                    "index_data: table scan failed",
                    extra={"source_key": key, "table_name": table_name, "error": str(_exc), "outcome": "skip"},
                )
                continue

            if not rows:
                continue

            col_names = list(rows[0].keys())
            # Identify first column as a best-guess PK
            pk_col = col_names[0] if col_names else "rowid"

            for row in rows:
                pk_val = str(row[pk_col]) if pk_col in col_names else str(row[0])
                for col_name in col_names:
                    cell = row[col_name]
                    if cell is None:
                        continue
                    cell_str = str(cell)
                    if not cell_str.strip():
                        continue
                    batch.append((key, table_name, pk_col, pk_val, col_name, cell_str))
                    # Build doc_id and hash for incremental embedding gating
                    doc_id = f"{key}:{table_name}:{pk_val}:{col_name}"
                    full_text = f"{table_name} {col_name} {cell_str}"
                    fp = _extract_python_fingerprint(full_text)
                    embed_candidates.append((doc_id, full_text, fp["content_hash"], fp["structural_hash"]))

    except Exception as exc:
        logger.warning(
            "Data indexing error",
            extra={"source_key": key, "error": str(exc), "outcome": "error"},
        )
    finally:
        conn.close()

    # Write all docs in one transaction
    if batch:
        store.data_docs_batch_upsert(batch)
        doc_count = len(batch)

    # Embed only structurally changed documents
    for doc_id, full_text, content_hash, structural_hash in embed_candidates:
        if not store.has_structure_changed(doc_id, content_hash, structural_hash):
            continue  # skip - content unchanged or only cosmetic diff
        vec = _embed(full_text)
        if vec is not None:
            store.embedding_upsert(doc_id, "data", vec)
        store.set_doc_fingerprint(doc_id, content_hash, structural_hash)

    return doc_count


def index_source(key: str, store=None, actor_id: Optional[str] = None) -> dict:
    """Index schema + data for one source key.  Thread-safe.

    Returns:
        {"key": key, "schema_docs": n, "data_docs": m, "ok": True/False}
    """
    from .kg_store import _open_store_raw

    close_store = store is None
    if store is None:
        store = _open_store_raw()

    result = {"key": key, "schema_docs": 0, "data_docs": 0, "ok": False}
    try:
        schema_json_path = OUTPUT_DIR / key / "schema.json"
        # index_schema handles the case where schema.json is absent by falling
        # back to an uploaded .xlsx file - only skip if neither is available.
        if not schema_json_path.exists() and _find_xlsx_upload(key) is None:
            logger.info(
                "kg_indexer: no schema.json or xlsx - skipping",
                extra={"source_key": key, "outcome": "skip"},
            )
            return result

        with _index_lock:
            # Schema indexing (always available)
            n_schema = index_schema(key, schema_json_path, store)
            result["schema_docs"] = n_schema

            # Data indexing (only for file-backed SQLite sources)
            db_path = _find_sqlite_db(key)
            n_data = 0
            if db_path is not None and db_path.exists():
                n_data = index_data(key, db_path, store)
                result["data_docs"] = n_data

            store.mark_indexed(
                key,
                schema_doc_count=n_schema,
                data_doc_count=n_data,
                schema=True,
                data=db_path is not None,
                actor_id=actor_id,
            )

        result["ok"] = True
        logger.info(
            "kg_indexer: indexed source",
            extra={
                "source_key":  key,
                "schema_docs": n_schema,
                "data_docs":   n_data,
                "outcome":     "ok",
            },
        )
    except Exception as exc:
        logger.exception(
            "kg_indexer: failed for key",
            extra={"source_key": key, "error": str(exc), "outcome": "error"},
        )
    finally:
        if close_store:
            store.close()

    return result


def index_all(store=None, staleness_ttl_s: float = 3600.0) -> list:
    """Index every registered source.  Returns list of per-source result dicts.

    Uses ``KGStore.batch_staleness_summary()`` to skip sources that were
    indexed within *staleness_ttl_s* seconds.  Mirrors UA
    ``getGraphFreshnessBatch()`` single-snapshot optimisation.

    Parameters
    ----------
    store
        Optional open :class:`~server.kg.kg_store.KGStore` instance.
        If *None*, a new store is opened and closed before returning.
    staleness_ttl_s
        Sources indexed within this many seconds are skipped (status FRESH).
        Pass ``0.0`` to force re-index of all sources regardless of age.
    """
    from .kg_store import _open_store_raw, GraphStalenessStatus

    close_store = store is None
    if store is None:
        store = _open_store_raw()

    results = []
    try:
        reg = _load_registry()
        ready_keys = [k for k, v in reg.items() if v.get("status") == "ready"]

        # Compute staleness for all ready sources in a single SQL round-trip.
        summaries = store.batch_staleness_summary(
            source_keys=ready_keys,
            staleness_ttl_s=staleness_ttl_s,
        )

        for key in ready_keys:
            summary = summaries.get(key)
            if summary is not None and summary.status == GraphStalenessStatus.FRESH:
                logger.info(
                    "kg_indexer: skipping fresh source",
                    extra={
                        "source_key": key,
                        "reason":     summary.reason,
                        "outcome":    "skip_fresh",
                    },
                )
                results.append({
                    "key":     key,
                    "ok":      True,
                    "skipped": True,
                    "reason":  "fresh",
                })
                continue
            results.append(index_source(key, store=store))
    finally:
        if close_store:
            store.close()

    return results


# ── Async wrappers (P3-D) ─────────────────────────────────────────────────────

async def index_source_async(key: str, store=None) -> dict:
    """Async wrapper for index_source using asyncio.to_thread (Python ≥ 3.9).

    Allows calling from async Flask/Quart contexts without blocking the event loop.
    Falls back to synchronous execution in environments that don't support to_thread.
    """
    import asyncio
    try:
        return await asyncio.to_thread(index_source, key, store)
    except AttributeError:
        # Python < 3.9 fallback
        return index_source(key, store)


async def index_all_async(store=None) -> list:
    """Async wrapper for index_all using asyncio.to_thread (Python ≥ 3.9)."""
    import asyncio
    try:
        return await asyncio.to_thread(index_all, store)
    except AttributeError:
        return index_all(store)


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Knowledge-graph index builder")
    parser.add_argument("--key", help="Index a single source key instead of all")
    parser.add_argument(
        "--backend",
        default=os.environ.get("BACKEND", "sqlite"),
        help="Storage backend (sqlite | lancedb). Default: sqlite",
    )
    args = parser.parse_args()

    if args.backend != "sqlite":
        os.environ["BACKEND"] = args.backend

    if args.key:
        res = index_source(args.key)
        print(f"Done: {res}")
    else:
        results = index_all()
        for r in results:
            status = "OK" if r["ok"] else "FAIL"
            print(f"[{status}] {r['key']}: {r['schema_docs']} schema docs, {r['data_docs']} data docs")
        print(f"\n{len(results)} source(s) processed")
