"""Server-side Knowledge Graph search engine for the Nybble Flask application.
The pip-installable equivalent is ``nybble.kg`` (marketed as Nybble GraphAI).
Do not import this file from the ``nybble`` package - use ``nybble.kg`` instead.

kg_search.py - Knowledge Graph Search Engine (Sub-Task 3)

Provides keyword, semantic, and hybrid search over the KGStore.
All functions are pure Python, stateless, and callable from Flask routes or CLI.

Search modes:
  keyword_search(q, layer, limit)  - FTS5 BM25 ranking
  semantic_search(q, layer, limit) - cosine similarity over stored embeddings
  hybrid_search(q, layer, limit)   - Reciprocal Rank Fusion of keyword + semantic
  data_search(q, source_keys, limit) - live LIKE queries across SQLite source DBs

Graceful degradation:
  If sentence-transformers is not installed, semantic_search returns [] and
  hybrid_search falls back to keyword_search only.

References:
  RRF: Cormack, Clarke & Buettcher, SIGIR 2009 - k=60 default constant.
"""
from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Hit dataclasses ───────────────────────────────────────────────────────────

@dataclass
class SearchHit:
    """A single schema-level search result."""
    source_key:  str
    table_name:  str
    column_name: str
    data_type:   str
    is_pk:       bool
    is_fk:       bool
    fk_target:   Optional[str]
    score:       float
    snippet:     str
    layer:       str = "schema"


@dataclass
class DataHit:
    """A single data-level search result (a matching cell or row)."""
    source_key: str
    table_name: str
    col_name:   str
    pk_col:     str
    pk_val:     str
    cell_value: str
    score:      float
    row_dict:   Dict = field(default_factory=dict)
    layer:      str  = "data"


# ── RRF constants ─────────────────────────────────────────────────────────────
RRF_K = 60  # Cormack et al. recommended constant


# ── Keyword search ─────────────────────────────────────────────────────────────

def keyword_search(q: str, layer: str = "schema", limit: int = 20, store=None) -> List:
    """BM25-ranked FTS5 search.  Returns list[SearchHit] or list[DataHit]."""
    from .kg_store import _open_store_raw

    close = store is None
    if store is None:
        store = _open_store_raw()

    results = []
    try:
        if layer == "schema":
            rows = store.fts_schema(_fts_escape(q), limit=limit)
            for i, row in enumerate(rows):
                results.append(SearchHit(
                    source_key  = row["source_key"],
                    table_name  = row["table_name"],
                    column_name = row["column_name"],
                    data_type   = row["data_type"],
                    is_pk       = bool(row["is_pk"]),
                    is_fk       = bool(row["is_fk"]),
                    fk_target   = row["fk_target"] or None,
                    score       = _rrf_score(i),
                    snippet     = row["full_text"][:120],
                    layer       = "schema",
                ))
        elif layer == "data":
            rows = store.fts_data(_fts_escape(q), limit=limit)
            for i, row in enumerate(rows):
                results.append(DataHit(
                    source_key = row["source_key"],
                    table_name = row["table_name"],
                    col_name   = row["col_name"],
                    pk_col     = row["pk_col"],
                    pk_val     = row["pk_val"],
                    cell_value = row["cell_value"],
                    score      = _rrf_score(i),
                    layer      = "data",
                ))
    except Exception as _exc:
        logger.debug(
            "keyword_search degraded",
            extra={"layer": layer, "error": str(_exc), "outcome": "skip"},
        )
    finally:
        if close:
            store.close()

    return results


def _fts_escape(q: str) -> str:
    """Escape user query for FTS5 MATCH expression."""
    # Wrap each token in quotes to avoid FTS5 syntax errors from special chars
    tokens = q.split()
    return " ".join(f'"{t.replace(chr(34), "")}"' for t in tokens if t)


def _rrf_score(rank: int, k: int = RRF_K) -> float:
    """Classic rank-based RRF score (Cormack et al. 2009)."""
    return 1.0 / (k + rank + 1)


def _rrf_score_weighted(rank: int, raw_score: float = 1.0, k: int = RRF_K) -> float:
    """Blend rank-based RRF with raw similarity score.
    # MARKER:kg-rrf-weighted-fusion
    """
    rank_score = 1.0 / (k + rank + 1)
    return rank_score * (0.7 + 0.3 * min(max(raw_score, 0.0), 1.0))


# ── Semantic search ────────────────────────────────────────────────────────────

def semantic_search(q: str, layer: str = "schema", limit: int = 20, store=None) -> List:
    """Cosine-similarity search over stored embedding vectors.

    Returns [] gracefully if sentence-transformers is not installed.
    """
    from .kg_store import _open_store_raw

    close = store is None
    if store is None:
        store = _open_store_raw()

    results = []
    try:
        # Embed the query
        q_vec = _embed_query(q)
        if q_vec is None:
            return results  # embeddings not available

        import numpy as np
        q_arr = np.array(q_vec, dtype=np.float32)
        norm_q = np.linalg.norm(q_arr)
        if norm_q == 0:
            return results

        # Load all stored vectors for the layer
        pairs = store.embeddings_for_layer(layer)
        if not pairs:
            return results

        ids, vecs = zip(*pairs)
        mat = np.array(vecs, dtype=np.float32)  # shape (N, D)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        mat_norm = mat / norms

        scores = mat_norm @ (q_arr / norm_q)  # shape (N,)
        top_indices = scores.argsort()[::-1][:limit]

        for rank, idx in enumerate(top_indices):
            doc_id = ids[idx]
            score  = float(scores[idx])
            if score <= 0:
                break
            # doc_id format: "<source_key>:<table>:<column>"
            parts = doc_id.split(":", 2)
            if layer == "schema" and len(parts) == 3:
                src_key, tbl, col = parts
                results.append(SearchHit(
                    source_key  = src_key,
                    table_name  = tbl,
                    column_name = col,
                    data_type   = "",
                    is_pk       = False,
                    is_fk       = False,
                    fk_target   = None,
                    score       = score,
                    snippet     = f"semantic match (score={score:.3f})",
                    layer       = "schema",
                ))
    except ImportError as _exc:
        logger.debug(
            "semantic_search unavailable (numpy/sentence-transformers missing)",
            extra={"layer": layer, "error": str(_exc), "outcome": "skip"},
        )
    except Exception as _exc:
        logger.debug(
            "semantic_search failed",
            extra={"layer": layer, "error": str(_exc), "outcome": "skip"},
        )
    finally:
        if close:
            store.close()

    return results


def _embed_query(text: str) -> Optional[List[float]]:
    """Embed a query string.  Returns list[float] or None.

    Delegates to kg_indexer._get_embedder() so that the model is loaded at most
    once per process and shared with the indexing path - avoids reloading a
    ~200 MB model on every search call.
    """
    try:
        from .kg_indexer import _get_embedder
        model = _get_embedder()
        if model is None:
            return None
        return model.encode(text, normalize_embeddings=True).tolist()
    except Exception as _exc:
        logger.debug(
            "_embed_query failed",
            extra={"error": str(_exc), "outcome": "skip"},
        )
        return None


# ── Hybrid search (RRF fusion) ─────────────────────────────────────────────────

def hybrid_search(q: str, layer: str = "schema", limit: int = 20, store=None) -> List:
    """Fuse keyword + semantic results using Reciprocal Rank Fusion.

    Falls back to keyword_search if embeddings are unavailable.
    """
    from .kg_store import _open_store_raw

    close = store is None
    if store is None:
        store = _open_store_raw()

    try:
        kw_hits   = keyword_search(q, layer=layer, limit=limit, store=store)
        sem_hits  = semantic_search(q, layer=layer, limit=limit, store=store)

        if not sem_hits:
            return kw_hits

        # Assign RRF scores keyed by a stable identity string
        rrf: Dict[str, float]           = {}
        hit_map: Dict[str, object]      = {}

        def _hit_id(h) -> str:
            if isinstance(h, SearchHit):
                return f"{h.source_key}:{h.table_name}:{h.column_name}"
            return f"{h.source_key}:{h.table_name}:{h.pk_col}:{h.pk_val}:{h.col_name}"

        for rank, h in enumerate(kw_hits):
            hid = _hit_id(h)
            rrf[hid] = rrf.get(hid, 0.0) + _rrf_score_weighted(rank, getattr(h, 'score', 1.0))
            hit_map[hid] = h

        for rank, h in enumerate(sem_hits):
            hid = _hit_id(h)
            rrf[hid] = rrf.get(hid, 0.0) + _rrf_score_weighted(rank, getattr(h, 'score', 1.0))
            if hid not in hit_map:
                hit_map[hid] = h

        # Sort by fused score
        sorted_ids = sorted(rrf, key=lambda x: rrf[x], reverse=True)[:limit]
        fused = []
        for hid in sorted_ids:
            h = hit_map[hid]
            h.score = rrf[hid]  # overwrite with fused score
            fused.append(h)

        return fused

    finally:
        if close:
            store.close()


# ── Data (live SQL) search ─────────────────────────────────────────────────────

def data_search(
    q: str,
    source_keys: Optional[List[str]] = None,
    limit: int = 50,
) -> List[DataHit]:
    """Run live LIKE queries against source SQLite DBs.

    Falls back gracefully when a source has no materialised SQLite file.
    Uses a thread pool for parallel table scanning.
    """
    import os as _os
    from pathlib import Path as _P

    output_dir = _P(_os.environ.get("OUTPUT_DIR", "/output/erd"))
    upload_dir = _P(_os.environ.get("UPLOAD_DIR", "/uploads"))

    def _find_db(key: str) -> Optional[_P]:
        for base in (upload_dir / key, output_dir / key):
            if base.exists():
                for f in sorted(base.iterdir()):
                    if f.suffix.lower() in (".db", ".sqlite"):
                        return f
        return None

    def _search_db(key: str) -> List[DataHit]:
        db_path = _find_db(key)
        if db_path is None or not db_path.exists():
            return []
        hits: List[DataHit] = []
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            conn.row_factory = sqlite3.Row
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()

            for (table_name,) in tables:
                if len(hits) >= limit:
                    break
                try:
                    sample = conn.execute(f'SELECT * FROM "{table_name}" LIMIT 1').fetchone()
                    if sample is None:
                        continue
                    col_names = sample.keys()
                    pk_col = col_names[0] if col_names else "rowid"

                    for col_name in col_names:
                        try:
                            rows = conn.execute(
                                f'SELECT "{pk_col}", "{col_name}" FROM "{table_name}"'
                                f' WHERE CAST("{col_name}" AS TEXT) LIKE ? LIMIT 10',
                                (f"%{q}%",),
                            ).fetchall()
                            for row in rows:
                                if len(hits) >= limit:
                                    break
                                hits.append(DataHit(
                                    source_key = key,
                                    table_name = table_name,
                                    col_name   = col_name,
                                    pk_col     = pk_col,
                                    pk_val     = str(row[0]),
                                    cell_value = str(row[1]),
                                    score      = 1.0,
                                    layer      = "data",
                                ))
                        except Exception as _exc:
                            logger.debug(
                                "data_search: column LIKE query failed",
                                extra={"table": table_name, "col": col_name, "error": str(_exc), "outcome": "skip"},
                            )
                            continue
                except Exception as _exc:
                    logger.debug(
                        "data_search: table scan failed",
                        extra={"table": table_name, "error": str(_exc), "outcome": "skip"},
                    )
                    continue
            conn.close()
        except Exception as _exc:
            logger.debug(
                "data_search: cannot open db",
                extra={"source_key": key, "error": str(_exc), "outcome": "skip"},
            )
        return hits

    if source_keys is None:
        # Default: discover from registry
        import json as _json
        registry_path = _P(_os.environ.get("REGISTRY", "/output/registry.json"))
        if registry_path.exists():
            try:
                reg = _json.loads(registry_path.read_text(encoding="utf-8"))
                source_keys = [k for k, v in reg.items() if v.get("status") == "ready"]
            except Exception as _exc:
                logger.debug(
                    "data_search: cannot read registry",
                    extra={"error": str(_exc), "outcome": "skip"},
                )
                source_keys = []
        else:
            source_keys = []

    all_hits: List[DataHit] = []
    max_workers = min(8, len(source_keys) if source_keys else 1)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_search_db, k): k for k in source_keys}
        for fut in as_completed(futures):
            try:
                all_hits.extend(fut.result())
            except Exception as _exc:
                logger.debug(
                    "data_search worker failed",
                    extra={"source_key": futures[fut], "error": str(_exc), "outcome": "skip"},
                )

    # Sort by hit density (just order by source, deterministic)
    return all_hits[:limit]
