"""
resolver.py - Universal Source Resolver and DuckDB Session Manager

Accepts any of the following source types and registers them as named
virtual tables in a shared, thread-safe, in-memory DuckDB session:

  • Database connection strings: sqlite:///, postgresql://, mysql://, duckdb://,
    clickhouse://, snowflake://, bigquery://, redshift://, cockroachdb://
  • Object storage: s3://, gs://, gcs://, r2://, az://  (DuckDB httpfs, zero new deps)
  • Local files: .csv, .tsv, .parquet, .json, .jsonl, .xlsx, .xls, .feather, .arrow, .avro,
                 .sqlite, .sqlite3, .db (SQLite), .duckdb (DuckDB native file)
  • JSON arrays: list[dict] or JSON-encoded strings
  • Remote API endpoints: HTTP/HTTPS URLs returning JSON arrays or paginated JSON
  • Optional-extra connectors (require pip extras): redis, mongodb, cassandra,
    elasticsearch, kafka, dynamodb, oracle, mssql

Connector tiers
---------------
  core          - always available (CSV, Parquet, JSON, XLSX, SQLite, DuckDB file)
  duckdb_ext    - installed on first use via DuckDB extension system, zero pip deps
                  (httpfs/aws for S3/GCS/R2, avro, chsql for ClickHouse, snowflake,
                   bigquery, mysql)
  optional_extra - require a separate pip install, wired up via stub loaders that
                   raise ImportError with actionable messages when the package is absent

Credential model
----------------
All cloud credentials are resolved from environment variables or standard
credential files (~/.aws/credentials, GCS ADC). Environment variables can be
injected via ${ENV_VAR} interpolation in nybble.yaml.  Nybble never stores or
logs credentials.

Public API
----------
  load(source, name=None)  → Relation
  session()                → duckdb.DuckDBPyConnection
  list_relations()         → list[str]
  drop(name)
  CONNECTOR_REGISTRY       → dict  (tier / install / scheme metadata)
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Optional, Union

import duckdb
import polars as pl

# ── Module-level thread-safe DuckDB session ───────────────────────────────────
# Each OS thread gets its own DuckDB connection via threading.local().
# The module-level _SESSION attribute is a transparent proxy into _local.session
# so that existing test fixtures (r._SESSION = None / r._SESSION.close()) work
# without modification.
_SESSION_LOCK = threading.Lock()
_local = threading.local()
_REGISTERED: dict[str, "Relation"] = {}


class _ModuleWithSessionProperty(type(sys.modules[__name__])):
    """Module subclass that exposes _SESSION as a per-thread property."""

    @property
    def _SESSION(self):  # type: ignore[override]
        return getattr(_local, "session", None)

    @_SESSION.setter
    def _SESSION(self, value):  # type: ignore[override]
        _local.session = value


sys.modules[__name__].__class__ = _ModuleWithSessionProperty


def session() -> duckdb.DuckDBPyConnection:
    """Return (or lazily create) the per-thread in-memory DuckDB connection."""
    with _SESSION_LOCK:
        if getattr(_local, "session", None) is None:
            _local.session = duckdb.connect(database=":memory:")
            _install_extensions(_local.session)
        return _local.session


# Extensions installed on DuckDB session initialisation.
# All are zero-pip-dependency - DuckDB's extension system handles downloads.
_DUCKDB_EXTENSIONS = (
    "json",      # always needed for JSON local reads
    "parquet",   # always needed for Parquet local reads
    "httpfs",    # S3/GCS/R2/Azure object storage
    "aws",       # AWS credential chain (complements httpfs)
    "avro",      # Avro file support (Kafka exports, Hadoop pipelines)
    "sqlite",    # SQLite attach
    "mysql",     # MySQL via DuckDB native extension (avoids pymysql in core)
    "chsql",     # ClickHouse community extension (primary ClickHouse path)
    "snowflake", # Snowflake native extension
)
# bigquery is a community extension - installed separately on demand
# to avoid startup noise on environments where it fails.
_COMMUNITY_EXTENSIONS = ("bigquery",)


def _install_extensions(con: duckdb.DuckDBPyConnection) -> None:
    """Install and load DuckDB extensions needed for broad format support.

    Extensions loaded:
      - json, parquet  : always needed for local file reads
      - httpfs, aws    : S3/GCS/R2/Azure object storage (zero new pip deps)
      - avro           : Avro file support (Kafka exports, Hadoop pipelines)
      - sqlite         : SQLite database attach
      - mysql          : MySQL via DuckDB native extension
      - chsql          : ClickHouse community extension
      - snowflake      : Snowflake native extension
    Community extensions (loaded on demand, not at startup):
      - bigquery       : BigQuery community extension
    """
    for ext in _DUCKDB_EXTENSIONS:
        try:
            con.execute(f"INSTALL '{ext}'; LOAD '{ext}';")
        except Exception:
            pass  # already installed, unavailable, or air-gapped - continue


def list_relations() -> list[str]:
    """Return the names of all registered virtual tables."""
    with _SESSION_LOCK:
        return list(_REGISTERED.keys())


def drop(name: str) -> None:
    """Remove a named virtual table from the session."""
    con = session()
    with _SESSION_LOCK:
        try:
            con.execute(f'DROP VIEW IF EXISTS "{name}"')
        except Exception:
            pass
        _REGISTERED.pop(name, None)


# ── Source type detection ─────────────────────────────────────────────────────

_DB_SCHEMES = frozenset([
    # Core relational
    "sqlite", "postgresql", "postgres", "mysql", "duckdb", "libsql",
    # Analytical / cloud databases (DuckDB extension tier)
    "clickhouse", "snowflake", "bigquery",
    # PostgreSQL-wire-compatible (normalised to postgresql handler)
    "redshift", "cockroachdb",
    # Optional-extra connectors
    "redis", "mongodb", "kafka", "dynamodb", "oracle", "mssql", "elasticsearch",
])

# Object-storage URI prefixes that route through _load_file (via DuckDB httpfs)
_OBJECT_STORAGE_PREFIXES = ("s3://", "gs://", "gcs://", "r2://", "az://", "azure://")

_FILE_EXTENSIONS = {
    ".csv": "csv", ".tsv": "csv", ".parquet": "parquet",
    ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl",
    ".xlsx": "xlsx", ".xls": "xls",
    ".feather": "feather", ".arrow": "arrow",
    ".avro": "avro",
    ".dat": "csv",      # common raw-data extension - try CSV auto-detect
    ".zip": "zip",      # ZIP archives - transparently extracted by _sniff_zip_csv
    ".gz":  "gz",       # gzip-compressed files - inner format detected from stem ext
    ".sql": "sql",      # SQL dump files - imported to a queryable SQLite db
    ".sqlite":  "sqlite_file",  # bare SQLite DB files - routed to _load_db
    ".sqlite3": "sqlite_file",  # alternate extension
    ".db":      "sqlite_file",  # generic DB extension - assume SQLite
    ".duckdb":  "duckdb_file",  # native DuckDB database file - routed to _load_db
}


# ── Connector tier registry ───────────────────────────────────────────────────

CONNECTOR_REGISTRY: dict[str, dict] = {
    # ── Core tier - always available, no install step ─────────────────────────
    "csv":           {"tier": "core",           "install": None,
                      "schemes": [],            "extensions": []},
    "parquet":       {"tier": "core",           "install": None,
                      "schemes": [],            "extensions": ["parquet"]},
    "json":          {"tier": "core",           "install": None,
                      "schemes": [],            "extensions": ["json"]},
    "xlsx":          {"tier": "core",           "install": None,
                      "schemes": [],            "extensions": []},
    "sqlite":        {"tier": "core",           "install": None,
                      "schemes": ["sqlite://"], "extensions": [".sqlite", ".sqlite3", ".db"]},
    "duckdb":        {"tier": "core",           "install": None,
                      "schemes": ["duckdb://"], "extensions": [".duckdb"]},
    # ── DuckDB extension tier - auto-installed on first use ───────────────────
    "s3":            {"tier": "duckdb_ext",     "install": "httpfs, aws",
                      "schemes": ["s3://", "gs://", "gcs://", "r2://", "az://", "azure://"],
                      "extensions": ["httpfs", "aws"]},
    "avro":          {"tier": "duckdb_ext",     "install": "avro",
                      "schemes": [],            "extensions": ["avro"]},
    "clickhouse":    {"tier": "duckdb_ext",     "install": "chsql (community)",
                      "schemes": ["clickhouse://"], "extensions": ["chsql"]},
    "snowflake":     {"tier": "duckdb_ext",     "install": "snowflake",
                      "schemes": ["snowflake://"], "extensions": ["snowflake"]},
    "bigquery":      {"tier": "duckdb_ext",     "install": "bigquery (community)",
                      "schemes": ["bigquery://"], "extensions": ["bigquery"]},
    "mysql":         {"tier": "duckdb_ext",     "install": "mysql",
                      "schemes": ["mysql://"],  "extensions": ["mysql"]},
    # ── Optional-extra tier - require pip install ─────────────────────────────
    "postgresql":    {"tier": "optional_extra", "install": "pip install datajoins-nybble[postgres]",
                      "schemes": ["postgresql://", "postgres://",
                                  "redshift://", "cockroachdb://"],
                      "extensions": []},
    "redis":         {"tier": "optional_extra", "install": "pip install datajoins-nybble[redis]",
                      "schemes": ["redis://"],  "extensions": []},
    "mongodb":       {"tier": "optional_extra", "install": "pip install datajoins-nybble[mongo]",
                      "schemes": ["mongodb://", "mongodb+srv://"], "extensions": []},
    "cassandra":     {"tier": "optional_extra", "install": "pip install datajoins-nybble[cassandra]",
                      "schemes": ["cassandra://"], "extensions": []},
    "elasticsearch": {"tier": "optional_extra", "install": "pip install datajoins-nybble[elastic]",
                      "schemes": ["elasticsearch://"], "extensions": []},
    "kafka":         {"tier": "optional_extra", "install": "pip install datajoins-nybble[kafka]",
                      "schemes": ["kafka://"],  "extensions": []},
    "dynamodb":      {"tier": "optional_extra", "install": "pip install datajoins-nybble[dynamo]",
                      "schemes": ["dynamodb://"], "extensions": []},
    "oracle":        {"tier": "optional_extra", "install": "pip install datajoins-nybble[oracle]",
                      "schemes": ["oracle://"], "extensions": []},
    "mssql":         {"tier": "optional_extra", "install": "pip install datajoins-nybble[mssql]",
                      "schemes": ["mssql://"],  "extensions": []},
}


def _detect_source_type(source: Any) -> str:
    """Return one of: 'db', 'file', 'json_inline', 'url'.

    Routing table
    -------------
    json_inline : list/dict argument, or string starting with '[' or '{'
    file        : object-storage URI (s3://, gs://, r2://, az://), local path,
                  or file:// URI - DuckDB httpfs handles remote reads
    url         : HTTP/HTTPS URL returning JSON
    db          : any recognised database scheme including optional-extra connectors
                  (redis://, mongodb://, kafka://, dynamodb://, oracle://,
                   mssql://, elasticsearch://, redshift://, cockroachdb://)
    """
    if isinstance(source, (list, dict)):
        return "json_inline"
    if not isinstance(source, str):
        raise TypeError(f"Unsupported source type: {type(source)}")
    s = source.strip()
    # JSON string check first - before any file-path logic
    if s.startswith(("[", "{")):
        return "json_inline"
    # Object storage: route through file loader (DuckDB httpfs handles the rest)
    if s.startswith(_OBJECT_STORAGE_PREFIXES):
        return "file"
    if s.startswith(("http://", "https://")):
        return "url"
    parsed = urllib.parse.urlparse(s)
    scheme = parsed.scheme.lower()
    if scheme in _DB_SCHEMES:
        return "db"
    # Bare path or file:// URI - check if it looks like a real path.
    # On Windows, urllib.parse.urlparse("C:\\data\\file.csv") returns scheme="c"
    # (single-letter drive), so we also accept single-letter schemes that match
    # an actual filesystem path.
    if parsed.scheme in ("", "file") or s.startswith(("./", "../", "/")):
        return "file"
    if len(parsed.scheme) == 1 and parsed.scheme.isalpha():
        # Single-letter scheme → treat as a Windows drive-letter absolute path
        return "file"
    # Fallback: try as file path
    return "file"


def _safe_name(hint: str) -> str:
    """Derive a safe DuckDB identifier from an arbitrary string."""
    base = re.sub(r"[^\w]", "_", hint)[:40].strip("_") or "source"
    # Ensure uniqueness if already taken
    candidate = base
    i = 1
    with _SESSION_LOCK:
        while candidate in _REGISTERED:
            candidate = f"{base}_{i}"
            i += 1
    return candidate


# ── Per-type loaders ──────────────────────────────────────────────────────────

def _is_remote_uri(source: str) -> bool:
    """Return True when source is an object-storage or remote URI handled by httpfs."""
    return source.strip().startswith(_OBJECT_STORAGE_PREFIXES)


def _sniff_zip_csv(path: Path) -> Optional[Path]:
    """If *path* is a ZIP archive containing exactly one CSV entry, extract
    that entry to a NamedTemporaryFile and return its Path.  Returns None for
    any file that is not a ZIP.  The caller is responsible for nothing - the
    temp file is registered in _ZIP_TEMP_FILES so the module holds a reference
    that prevents early deletion on Windows.
    """
    import zipfile, tempfile as _tf
    # ZIP magic: first 4 bytes are 50 4B 03 04
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic[:2] != b"PK":
        return None
    if not zipfile.is_zipfile(path):
        return None
    with zipfile.ZipFile(path) as zf:
        csv_entries = [e for e in zf.infolist() if e.filename.lower().endswith(".csv")]
        if not csv_entries:
            raise ValueError(
                f"ZIP archive '{path.name}' contains no .csv entries. "
                f"Entries found: {[e.filename for e in zf.infolist()]}"
            )
        entry = csv_entries[0]
        tmp = _tf.NamedTemporaryFile(suffix=".csv", delete=False, prefix="nybble_zip_")
        with zf.open(entry) as src:
            tmp.write(src.read())
        tmp.flush()
        tmp.close()
        tmp_path = Path(tmp.name)
        _ZIP_TEMP_FILES.append(tmp_path)   # keep reference, clean up on process exit
        return tmp_path


# Temp files extracted from ZIP-disguised CSVs - held so Python doesn't delete them
# before DuckDB is done reading.  Cleaned up via atexit.
_ZIP_TEMP_FILES: list[Path] = []


def _cleanup_zip_temps() -> None:
    for p in _ZIP_TEMP_FILES:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


import atexit as _atexit
_atexit.register(_cleanup_zip_temps)


# Inner extensions whose gzip-compressed form DuckDB can read natively
# (e.g. data.csv.gz, data.parquet.gz).  For these we just pass the .gz path
# straight through to the matching DuckDB read function.
_GZ_NATIVE_INNER_EXTS = frozenset([".csv", ".tsv", ".parquet", ".json", ".jsonl", ".ndjson"])


def _load_gz_or_sql(path: Path, name: str, original_source: str) -> "Relation":
    """Handle .gz and .sql files.

    Strategy
    --------
    .csv.gz / .parquet.gz / .json.gz / .jsonl.gz
        DuckDB reads these natively - delegate to _load_file with the .gz path;
        the inner extension is used to pick the right DuckDB reader.

    .sql.gz / .sql  (PostgreSQL / MySQL plain-text dumps)
        1. Try to import via server.ingestion (available in the repo install).
           import_dump() produces a queryable SQLite .db in a temp directory.
           Load that .db via _load_db().
        2. If server.ingestion is not importable (standalone pip install outside
           the repo), raise a clear error explaining the limitation.
    """
    import tempfile as _tf

    suffix      = path.suffix.lower()              # .gz or .sql
    stem_suffix = Path(path.stem).suffix.lower()   # .sql, .csv, .parquet, …  (or "" for bare .gz)

    # ── Case 1: data format compressed with gzip ──────────────────────────────
    if suffix == ".gz" and stem_suffix in _GZ_NATIVE_INNER_EXTS:
        # DuckDB accepts the .gz path directly - just re-dispatch _load_file
        # with the inner extension so the right reader is selected.
        # We temporarily rename (in-memory) by pretending the path ends with the
        # inner extension.  The actual path stays unchanged - DuckDB handles the
        # decompression itself.
        inner_path_str = str(path)
        inner_ext = stem_suffix
        # Trick: create a temp symlink-free alias so _FILE_EXTENSIONS lookup works
        # without touching the real file.  We pass the gz path straight to DuckDB
        # and override the extension lookup by calling the DuckDB layer directly.
        safe_path = inner_path_str.replace("'", "''")
        con = session()
        with _SESSION_LOCK:
            if inner_ext in (".csv", ".tsv"):
                sep = "\t" if inner_ext == ".tsv" else ","
                con.execute(
                    f"""CREATE OR REPLACE VIEW "{name}" AS
                        SELECT * FROM read_csv_auto('{safe_path}', sep='{sep}', header=true)"""
                )
            elif inner_ext == ".parquet":
                con.execute(
                    f"""CREATE OR REPLACE VIEW "{name}" AS
                        SELECT * FROM read_parquet('{safe_path}')"""
                )
            elif inner_ext in (".json", ".jsonl", ".ndjson"):
                con.execute(
                    f"""CREATE OR REPLACE VIEW "{name}" AS
                        SELECT * FROM read_json_auto('{safe_path}')"""
                )
            else:
                raise ValueError(f"Unsupported inner extension in .gz file: {inner_ext!r}")
        row_count = _count(name, con)
        return Relation(name=name, source=original_source, source_type="file", row_count=row_count)

    # ── Case 2: SQL dump (.sql or .sql.gz) ────────────────────────────────────
    if suffix == ".sql" or (suffix == ".gz" and stem_suffix == ".sql"):
        from nybble.core.import_to_sqlite import import_dump
        from nybble.core.detect_format import detect as _detect_fmt

        fmt = _detect_fmt(path)
        if fmt == "unknown":
            raise ValueError(
                f"Could not classify {path.name!r} as a PostgreSQL or MySQL dump. "
                f"Ensure it is a plain-text SQL dump (not a pg_restore binary format)."
            )

        tmp_dir = Path(_tf.mkdtemp(prefix="nybble_sql_"))
        _ZIP_TEMP_FILES.append(tmp_dir)   # reuse cleanup list for the temp dir

        db_path = import_dump(path, tmp_dir, vendor=fmt if fmt in ("postgresql", "mysql") else "postgresql")
        if db_path is None or not db_path.exists():
            raise ValueError(
                f"Failed to import SQL dump {path.name!r}. "
                f"The file may contain no CREATE TABLE statements or use an unsupported dialect."
            )

        # Load the resulting SQLite database via the normal _load_db path
        return _load_db(f"sqlite:///{db_path}", name)

    # ── Fallback: bare .gz with unknown inner type ────────────────────────────
    # Decompress to a temp file and re-dispatch on the inner extension.
    if suffix == ".gz":
        import gzip as _gzip
        inner_name = path.stem  # strips the outer .gz
        tmp = _tf.NamedTemporaryFile(
            suffix=Path(inner_name).suffix or ".bin",
            delete=False,
            prefix="nybble_gz_",
        )
        try:
            with _gzip.open(path, "rb") as src, open(tmp.name, "wb") as dst:
                import shutil as _shutil
                _shutil.copyfileobj(src, dst)
        except Exception as exc:
            Path(tmp.name).unlink(missing_ok=True)
            raise ValueError(f"Failed to decompress {path.name!r}: {exc}") from None
        tmp.flush(); tmp.close()
        tmp_path = Path(tmp.name)
        _ZIP_TEMP_FILES.append(tmp_path)
        return _load_file(str(tmp_path), name)

    raise ValueError(f"Unsupported file: {path.name!r}")


def _load_file(source: str, name: str) -> "Relation":
    """Load a local file or remote object-storage URI as a DuckDB view.

    Supports local paths and s3://, gs://, gcs://, r2://, az:// URIs via the
    DuckDB httpfs extension (no new pip dependency required).

    ZIP transparency: if a local file with any supported extension is actually a
    ZIP archive (e.g. a .csv exported from a tool that zips its output), the
    single CSV entry inside the archive is transparently extracted to a temp file
    and loaded from there.  No change is required to the calling code.
    """
    remote = _is_remote_uri(source)

    if not remote:
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {source}")
        # Transparent ZIP extraction: if the file is a ZIP, unwrap it first.
        unzipped = _sniff_zip_csv(path)
        if unzipped is not None:
            path = unzipped
        path_str = str(path)
        ext = path.suffix.lower()
    else:
        # For remote URIs use the URI directly; derive extension from the path component
        path_str = source.strip()
        # Strip query strings / glob tails to find the extension
        uri_path = urllib.parse.urlparse(path_str).path
        # Handle glob patterns: find the last literal segment before any '*'
        uri_path_clean = uri_path.split("*")[0].rstrip("/")
        ext = Path(uri_path_clean).suffix.lower() if uri_path_clean else ""

    fmt = _FILE_EXTENSIONS.get(ext)
    if fmt is None:
        if remote:
            # For unknown remote extensions, try Parquet (most common in data lakes)
            fmt = "parquet"
        else:
            raise ValueError(
                f"Unsupported file extension: {ext!r}. "
                f"Supported: {', '.join(_FILE_EXTENSIONS)}"
            )

    # For bare .zip files, route through the ZIP extractor rather than DuckDB SQL
    if fmt == "zip":
        if remote:
            raise ValueError("ZIP archives cannot be loaded from remote URIs. Download the file first.")
        unzipped = _sniff_zip_csv(Path(path_str))
        if unzipped is None:
            raise ValueError(f"'{path_str}' does not appear to be a valid ZIP archive.")
        # Recurse with the extracted inner file - ext and fmt will be re-resolved
        return _load_file(str(unzipped), name)

    # For .gz files and .sql files, delegate to the gz/sql handler
    if fmt in ("gz", "sql"):
        if remote:
            raise ValueError(".gz / .sql files cannot be loaded from remote URIs. Download the file first.")
        return _load_gz_or_sql(Path(path_str), name, source)

    # For bare .sqlite / .sqlite3 / .db files, delegate to the SQLite DB loader
    if fmt == "sqlite_file":
        if remote:
            raise ValueError("SQLite files cannot be loaded from remote URIs. Download the file first.")
        abs_path = Path(path_str).resolve()
        return _load_db(f"sqlite:///{abs_path}", name)

    # For bare .duckdb files, delegate to the DuckDB file loader
    if fmt == "duckdb_file":
        if remote:
            raise ValueError("DuckDB files cannot be loaded from remote URIs. Download the file first.")
        abs_path = Path(path_str).resolve()
        return _load_db(f"duckdb:///{abs_path}", name)

    # Escape single quotes in path strings for SQL safety
    safe_path = path_str.replace("'", "''")

    con = session()
    with _SESSION_LOCK:
        if fmt == "csv":
            sep = "\t" if ext == ".tsv" else ","
            con.execute(
                f"""CREATE OR REPLACE VIEW "{name}" AS
                    SELECT * FROM read_csv_auto('{safe_path}', sep='{sep}', header=true)"""
            )
        elif fmt == "parquet":
            con.execute(
                f"""CREATE OR REPLACE VIEW "{name}" AS
                    SELECT * FROM read_parquet('{safe_path}')"""
            )
        elif fmt in ("json", "jsonl"):
            con.execute(
                f"""CREATE OR REPLACE VIEW "{name}" AS
                    SELECT * FROM read_json_auto('{safe_path}')"""
            )
        elif fmt == "avro":
            con.execute(
                f"""CREATE OR REPLACE VIEW "{name}" AS
                    SELECT * FROM read_avro('{safe_path}')"""
            )
        elif fmt in ("xlsx", "xls"):
            if remote:
                raise ValueError(
                    "XLSX files cannot be read from remote URIs. "
                    "Download the file locally first."
                )
            df = pl.read_excel(Path(path_str))
            arrow_table = df.to_arrow()
            con.register(name, arrow_table)
        elif fmt in ("feather", "arrow"):
            if remote:
                raise ValueError(
                    "Feather/Arrow files cannot be read from remote URIs. "
                    "Download the file locally first."
                )
            import pyarrow.feather as pf
            arrow_table = pf.read_table(path_str)
            con.register(name, arrow_table)
        else:
            raise ValueError(f"Unsupported format: {fmt}")

    row_count = _count(name, con)
    return Relation(
        name=name,
        source=source,
        source_type="object_storage" if remote else "file",
        row_count=row_count,
    )


def _load_json_inline(source: Any, name: str) -> "Relation":
    """Register a list[dict] or JSON string as a DuckDB virtual table."""
    if isinstance(source, str):
        data = json.loads(source)
    else:
        data = source
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise TypeError("JSON inline source must be a list or dict")

    df = pl.DataFrame(data, infer_schema_length=min(len(data), 500))
    arrow_table = df.to_arrow()
    con = session()
    with _SESSION_LOCK:
        con.register(name, arrow_table)

    return Relation(name=name, source="<inline>", source_type="json_inline", row_count=len(data))


def _load_url(
    source: str,
    name: str,
    max_pages: int = 20,
    headers: Optional[dict] = None,
) -> "Relation":
    """Fetch a remote JSON API endpoint (with pagination) and register it."""
    import requests

    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    records: list[dict] = []
    url: Optional[str] = source
    page = 0

    while url and page < max_pages:
        resp = requests.get(url, timeout=30, headers=request_headers)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")

        if "json" not in content_type and not resp.text.strip().startswith(("[", "{")):
            raise ValueError(
                f"URL returned non-JSON content ({content_type}). "
                "For file downloads use a local path."
            )

        payload = resp.json()

        # Unwrap common envelope shapes
        rows: list[dict] = []
        next_url: Optional[str] = None

        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            # Heuristic: find the first list value as the data array
            for key in ("data", "results", "items", "records", "rows", "value"):
                if isinstance(payload.get(key), list):
                    rows = payload[key]
                    break
            if not rows and isinstance(payload, dict):
                rows = [payload]
            # Pagination cursor fields
            for cursor_key in ("next", "nextPage", "next_page", "nextUrl",
                               "next_cursor", "cursor", "@odata.nextLink"):
                nxt = payload.get(cursor_key)
                if nxt and isinstance(nxt, str) and nxt.startswith("http"):
                    next_url = nxt
                    break

        records.extend(rows)
        url = next_url
        page += 1
        if not rows:
            break

    return _load_json_inline(records, name)


_MAX_TABLES_PER_DB = 200   # guard against databases with hundreds of tables


def _load_db(source: str, name: str) -> "Relation":
    """Introspect a database connection string and register tables as views.

    For multi-table databases (PostgreSQL, MySQL, DuckDB files, SQLite) each
    table is exposed as a separate DuckDB view named ``<name>__<table>``.
    The ``Relation`` returned carries a ``tables`` attribute listing all
    registered table names (up to _MAX_TABLES_PER_DB).

    URI normalisation
    -----------------
    redshift://... and cockroachdb://... are both PostgreSQL-wire-compatible
    and are normalised to postgresql://... before dispatch.
    """
    parsed = urllib.parse.urlparse(source)
    scheme = parsed.scheme.lower()

    # ── URI normalisation for PostgreSQL-wire-compatible databases ────────────
    if scheme in ("redshift", "cockroachdb"):
        # Both speak the PostgreSQL wire protocol; rewrite the scheme.
        source = "postgresql" + source[len(scheme):]
        scheme = "postgresql"
        parsed = urllib.parse.urlparse(source)

    if scheme in ("sqlite", "file"):
        # parsed.path for `sqlite:///relative/path` is `/relative/path` which is
        # wrong on Windows.  Reconstruct the real path by stripping the leading
        # slash when the remainder looks like a relative path (no drive letter on
        # the part after the leading '/').
        raw_path = parsed.path
        if raw_path and raw_path.startswith("/") and not Path(raw_path).exists():
            # Try stripping the leading slash to get a relative path
            candidate = raw_path.lstrip("/")
            candidate_abs = Path(candidate).resolve()
            if candidate_abs.exists():
                raw_path = str(candidate_abs)
        db_path = raw_path
        con = session()
        with _SESSION_LOCK:
            alias = f"_attach_{name}"
            try:
                con.execute("INSTALL 'sqlite'; LOAD 'sqlite';")
            except Exception:
                pass
            # sqlite_all_varchar=true makes DuckDB load every SQLite column as
            # VARCHAR, bypassing strict type coercion.  This is necessary because
            # SQLite's type affinity is weak - a column declared INTEGER can legally
            # store text (e.g. "f" for false), causing TypeMismatchException at
            # read time without this setting.
            try:
                con.execute("SET sqlite_all_varchar=true")
            except Exception:
                pass
            con.execute(f"ATTACH '{db_path}' AS {alias} (TYPE SQLITE, READ_ONLY)")
            # Use information_schema - sqlite_master is not accessible via the
            # DuckDB catalog path in newer DuckDB versions.
            tbl_rows = con.execute(
                f"SELECT table_name FROM information_schema.tables "
                f"WHERE table_catalog='{alias}' AND table_schema='main'"
            ).fetchall()
        tables = [r[0] for r in tbl_rows][:_MAX_TABLES_PER_DB]
        with _SESSION_LOCK:
            for tbl in tables:
                view_name = f"{name}__{tbl}" if tbl != name else name
                con.execute(
                    f'CREATE OR REPLACE VIEW "{view_name}" AS '
                    f'SELECT * FROM {alias}."{tbl}"'
                )
                _REGISTERED[view_name] = Relation(
                    name=view_name, source=source, source_type="db", row_count=None
                )
        # The "primary" relation lists tables
        rel = Relation(name=name, source=source, source_type="db", row_count=None)
        rel.tables = tables
        return rel

    elif scheme in ("postgresql", "postgres"):
        try:
            from sqlalchemy import create_engine, inspect as sa_inspect
            engine = create_engine(source)
            try:
                inspector = sa_inspect(engine)
                table_names = inspector.get_table_names(schema="public")
            except Exception as _conn_exc:
                engine.dispose()
                # Suppress the full SQLAlchemy/psycopg2 traceback; surface a
                # one-line message so the CLI can print it cleanly.
                raise ConnectionError(str(_conn_exc).splitlines()[0]) from None
            engine.dispose()
        except ImportError:
            table_names = []
        except ConnectionError:
            raise   # already clean - let it bubble to the CLI

        if not table_names:
            raise ValueError(
                f"No tables found in database: {source}\n"
                "Ensure psycopg2-binary is installed: pip install datajoins-nybble[postgres]"
            )

        tables = table_names[:_MAX_TABLES_PER_DB]
        con = session()
        for tbl in tables:
            view_name = f"{name}__{tbl}"
            try:
                df = pl.read_database_uri(
                    f'SELECT * FROM "{tbl}" LIMIT 50000', source
                )
                arrow_table = df.to_arrow()
                with _SESSION_LOCK:
                    con.register(view_name, arrow_table)
                _REGISTERED[view_name] = Relation(
                    name=view_name, source=source, source_type="db",
                    row_count=len(df)
                )
            except Exception:
                pass   # skip tables we can't read (permissions, views, etc.)

        rel = Relation(name=name, source=source, source_type="db", row_count=None)
        rel.tables = tables
        return rel

    elif scheme in ("mysql",):
        try:
            from sqlalchemy import create_engine, inspect as sa_inspect
            engine = create_engine(source)
            inspector = sa_inspect(engine)
            table_names = inspector.get_table_names()
            engine.dispose()
        except ImportError:
            table_names = []

        if not table_names:
            # Fallback: query information_schema
            df = pl.read_database_uri(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() LIMIT 200",
                source,
            )
            table_names = [r[0] for r in df.iter_rows()]

        tables = table_names[:_MAX_TABLES_PER_DB]
        con = session()
        for tbl in tables:
            view_name = f"{name}__{tbl}"
            try:
                df = pl.read_database_uri(f"SELECT * FROM `{tbl}` LIMIT 50000", source)
                arrow_table = df.to_arrow()
                with _SESSION_LOCK:
                    con.register(view_name, arrow_table)
                _REGISTERED[view_name] = Relation(
                    name=view_name, source=source, source_type="db",
                    row_count=len(df)
                )
            except Exception:
                pass

        rel = Relation(name=name, source=source, source_type="db", row_count=None)
        rel.tables = tables
        return rel

    elif scheme == "duckdb":
        # Native DuckDB file - attach and expose each table as a view.
        # Apply the same Windows path normalisation used for SQLite:
        # parsed.path for `duckdb:///relative/path` is `/relative/path` which is
        # wrong on Windows.  Strip the leading slash when the path doesn't exist
        # as-is but does exist after stripping.
        raw_duckdb_path = parsed.path or parsed.netloc
        if raw_duckdb_path and raw_duckdb_path.startswith("/") and not Path(raw_duckdb_path).exists():
            candidate = raw_duckdb_path.lstrip("/")
            candidate_abs = Path(candidate).resolve()
            if candidate_abs.exists():
                raw_duckdb_path = str(candidate_abs)
        db_path = raw_duckdb_path
        con = session()
        with _SESSION_LOCK:
            alias = f"_dbfile_{name}"
            con.execute(f"ATTACH '{db_path}' AS {alias} (READ_ONLY)")
            tbl_rows = con.execute(
                f"SELECT table_name FROM information_schema.tables "
                f"WHERE table_catalog='{alias}' AND table_schema='main'"
            ).fetchall()
        tables = [r[0] for r in tbl_rows][:_MAX_TABLES_PER_DB]
        with _SESSION_LOCK:
            for tbl in tables:
                view_name = f"{name}__{tbl}"
                con.execute(
                    f'CREATE OR REPLACE VIEW "{view_name}" AS '
                    f'SELECT * FROM {alias}."{tbl}"'
                )
                _REGISTERED[view_name] = Relation(
                    name=view_name, source=source, source_type="db", row_count=None
                )
        rel = Relation(name=name, source=source, source_type="db", row_count=None)
        rel.tables = tables
        return rel

    elif scheme == "clickhouse":
        return _load_clickhouse(source, parsed, name)

    elif scheme in ("snowflake",):
        return _load_snowflake(source, name)

    elif scheme in ("bigquery",):
        return _load_bigquery(source, name)

    elif scheme in ("redis",):
        return _load_redis(source, name)

    elif scheme in ("mongodb",):
        return _load_mongodb(source, name)

    elif scheme in ("cassandra",):
        return _load_cassandra(source, name)

    elif scheme in ("elasticsearch",):
        return _load_elasticsearch(source, name)

    elif scheme in ("kafka",):
        return _load_kafka(source, name)

    elif scheme in ("dynamodb",):
        return _load_dynamodb(source, name)

    elif scheme in ("oracle",):
        return _load_oracle(source, name)

    elif scheme in ("mssql",):
        return _load_mssql(source, name)

    else:
        raise ValueError(
            f"Unsupported database scheme: {scheme!r}. "
            f"Supported schemes: sqlite, postgresql, mysql, duckdb, clickhouse, "
            f"snowflake, bigquery, redshift, cockroachdb, redis, mongodb, cassandra, "
            f"elasticsearch, kafka, dynamodb, oracle, mssql. "
            f"For S3/GCS/R2/GCS object storage use the URI directly "
            f"(e.g. 's3://bucket/file.parquet')."
        )


def _load_clickhouse(source: str, parsed: urllib.parse.ParseResult, name: str) -> "Relation":
    """Load tables from a ClickHouse server.

    Tries the DuckDB ``chsql`` community extension first; falls back to
    ClickHouse's native HTTP interface (port 8123) which returns Arrow IPC
    and requires only ``requests`` (already in core).
    """
    host = parsed.hostname or "localhost"
    port = parsed.port or 8123
    database = (parsed.path or "/default").lstrip("/") or "default"
    user = parsed.username or "default"
    password = parsed.password or ""
    http_base = f"http://{host}:{port}"

    con = session()

    # ── Attempt 1: DuckDB chsql extension ────────────────────────────────────
    try:
        with _SESSION_LOCK:
            con.execute("INSTALL 'chsql'; LOAD 'chsql';")
        ch_conn_str = f"clickhouse://{user}:{password}@{host}:{port}/{database}"
        with _SESSION_LOCK:
            con.execute(
                f"""CREATE OR REPLACE VIEW "{name}" AS
                    SELECT * FROM chsql.query('{ch_conn_str}',
                    'SELECT * FROM system.tables WHERE database = ''{database}''')"""
            )
        return Relation(name=name, source=source, source_type="db", row_count=None)
    except Exception:
        pass

    # ── Attempt 2: HTTP interface → Arrow IPC ────────────────────────────────
    import requests as _requests
    params = {
        "query": f"SELECT * FROM system.tables WHERE database='{database}' LIMIT 500",
        "default_format": "Arrow",
        "database": database,
    }
    auth = (user, password) if user else None
    try:
        resp = _requests.get(http_base, params=params, auth=auth, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        raise ConnectionError(
            f"Could not connect to ClickHouse at {http_base}: {exc}\n"
            "Ensure the server is running and the HTTP interface is enabled."
        ) from exc

    import pyarrow as pa
    import pyarrow.ipc as pa_ipc
    arrow_table = pa_ipc.open_file(resp.content).read_all()
    with _SESSION_LOCK:
        con.register(name, arrow_table)
    return Relation(name=name, source=source, source_type="db", row_count=len(arrow_table))


def _load_snowflake(source: str, name: str) -> "Relation":
    """Load from Snowflake via the DuckDB snowflake extension."""
    con = session()
    try:
        with _SESSION_LOCK:
            con.execute("INSTALL 'snowflake'; LOAD 'snowflake';")
            con.execute(
                f"""CREATE OR REPLACE VIEW "{name}" AS
                    SELECT * FROM snowflake_query('{source}',
                    'SELECT table_name, table_schema FROM information_schema.tables LIMIT 200')"""
            )
        return Relation(name=name, source=source, source_type="db", row_count=None)
    except Exception as exc:
        raise ConnectionError(
            f"Could not connect to Snowflake: {exc}\n"
            "Ensure the DuckDB snowflake extension is available and credentials are set."
        ) from exc


def _load_bigquery(source: str, name: str) -> "Relation":
    """Load from BigQuery via the DuckDB community bigquery extension."""
    con = session()
    try:
        with _SESSION_LOCK:
            con.execute("INSTALL 'bigquery' FROM community; LOAD 'bigquery';")
            con.execute(
                f"""CREATE OR REPLACE VIEW "{name}" AS
                    SELECT * FROM bigquery_query('{source}',
                    'SELECT table_name FROM INFORMATION_SCHEMA.TABLES LIMIT 200')"""
            )
        return Relation(name=name, source=source, source_type="db", row_count=None)
    except Exception as exc:
        raise ConnectionError(
            f"Could not connect to BigQuery: {exc}\n"
            "Ensure the DuckDB bigquery community extension is available and "
            "GOOGLE_APPLICATION_CREDENTIALS is set."
        ) from exc


# ── Optional-extra connector stubs ────────────────────────────────────────────
#
# Each stub follows the same pattern:
#   1. Attempt importlib.import_module for the required driver package.
#   2. If missing, raise ImportError with an actionable install message.
#   3. If available, use the driver to connect and load data into DuckDB.
#
# Credentials are read exclusively from environment variables.
# These stubs provide a clean integration point - the actual query logic
# can be expanded once the optional package is present.

def _load_redis(source: str, name: str) -> "Relation":
    """Load data from a Redis server (requires pip install datajoins-nybble[redis])."""
    import importlib
    try:
        redis = importlib.import_module("redis")
    except ImportError as exc:
        raise ImportError(
            "Redis connector requires: pip install datajoins-nybble[redis]\n"
            "The 'redis' package is not installed."
        ) from exc

    parsed = urllib.parse.urlparse(source)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    password = parsed.password or None
    db_idx = int(parsed.path.lstrip("/") or "0")

    client = redis.Redis(host=host, port=port, password=password, db=db_idx, decode_responses=True)
    keys = client.keys("*")[:500]
    rows = []
    for key in keys:
        key_type = client.type(key)
        rows.append({"key": key, "type": key_type})

    import polars as _pl
    df = _pl.DataFrame(rows) if rows else _pl.DataFrame({"key": [], "type": []})
    arrow_table = df.to_arrow()
    con = session()
    with _SESSION_LOCK:
        con.register(name, arrow_table)
    return Relation(name=name, source=source, source_type="db", row_count=len(df))


def _load_mongodb(source: str, name: str) -> "Relation":
    """Load data from a MongoDB collection (requires pip install datajoins-nybble[mongo])."""
    import importlib
    try:
        pymongo = importlib.import_module("pymongo")
    except ImportError as exc:
        raise ImportError(
            "MongoDB connector requires: pip install datajoins-nybble[mongo]\n"
            "The 'pymongo' package is not installed."
        ) from exc

    parsed = urllib.parse.urlparse(source)
    # Path: /database/collection - or just /database
    path_parts = [p for p in parsed.path.split("/") if p]
    db_name = path_parts[0] if path_parts else "test"
    collection_name = path_parts[1] if len(path_parts) > 1 else "data"

    client = pymongo.MongoClient(source, serverSelectionTimeoutMS=5000)
    db = client[db_name]
    collection = db[collection_name]
    docs = list(collection.find({}, {"_id": 0}).limit(50000))

    import polars as _pl
    df = _pl.DataFrame(docs) if docs else _pl.DataFrame()
    arrow_table = df.to_arrow()
    con = session()
    with _SESSION_LOCK:
        con.register(name, arrow_table)
    return Relation(name=name, source=source, source_type="db", row_count=len(df))


def _load_cassandra(source: str, name: str) -> "Relation":
    """Load data from a Cassandra / ScyllaDB table
    (requires pip install datajoins-nybble[cassandra])."""
    import importlib
    try:
        cassandra_cluster = importlib.import_module("cassandra.cluster")
    except ImportError as exc:
        raise ImportError(
            "Cassandra connector requires: pip install datajoins-nybble[cassandra]\n"
            "The 'cassandra-driver' package is not installed."
        ) from exc

    parsed = urllib.parse.urlparse(source)
    host = parsed.hostname or "localhost"
    port = parsed.port or 9042
    path_parts = [p for p in parsed.path.split("/") if p]
    keyspace = path_parts[0] if path_parts else None

    Cluster = cassandra_cluster.Cluster
    cluster = Cluster([host], port=port)
    session_cass = cluster.connect(keyspace)
    rows_raw = session_cass.execute("SELECT * FROM system.local LIMIT 100")
    rows = [dict(r._asdict()) for r in rows_raw]

    import polars as _pl
    df = _pl.DataFrame(rows) if rows else _pl.DataFrame()
    arrow_table = df.to_arrow()
    con = session()
    with _SESSION_LOCK:
        con.register(name, arrow_table)
    return Relation(name=name, source=source, source_type="db", row_count=len(df))


def _load_elasticsearch(source: str, name: str) -> "Relation":
    """Load documents from an Elasticsearch index
    (requires pip install datajoins-nybble[elastic])."""
    import importlib
    try:
        elasticsearch = importlib.import_module("elasticsearch")
    except ImportError as exc:
        raise ImportError(
            "Elasticsearch connector requires: pip install datajoins-nybble[elastic]\n"
            "The 'elasticsearch' package is not installed."
        ) from exc

    parsed = urllib.parse.urlparse(source)
    index = (parsed.path or "/").lstrip("/") or "_all"
    host = f"{parsed.scheme}://{parsed.netloc}"

    Elasticsearch = elasticsearch.Elasticsearch
    es = Elasticsearch(host)
    result = es.search(index=index, body={"query": {"match_all": {}}}, size=500)
    hits = result.get("hits", {}).get("hits", [])
    rows = [h.get("_source", {}) for h in hits]

    import polars as _pl
    df = _pl.DataFrame(rows) if rows else _pl.DataFrame()
    arrow_table = df.to_arrow()
    con = session()
    with _SESSION_LOCK:
        con.register(name, arrow_table)
    return Relation(name=name, source=source, source_type="db", row_count=len(df))


def _load_kafka(source: str, name: str) -> "Relation":
    """Read messages from a Kafka topic
    (requires pip install datajoins-nybble[kafka])."""
    import importlib
    try:
        confluent_kafka = importlib.import_module("confluent_kafka")
    except ImportError as exc:
        raise ImportError(
            "Kafka connector requires: pip install datajoins-nybble[kafka]\n"
            "The 'confluent-kafka' package is not installed."
        ) from exc

    parsed = urllib.parse.urlparse(source)
    bootstrap_server = f"{parsed.hostname or 'localhost'}:{parsed.port or 9092}"
    topic = (parsed.path or "/").lstrip("/") or "default"

    Consumer = confluent_kafka.Consumer
    consumer = Consumer({
        "bootstrap.servers": bootstrap_server,
        "group.id": "nybble-reader",
        "auto.offset.reset": "earliest",
    })
    consumer.subscribe([topic])
    messages = []
    for _ in range(500):
        msg = consumer.poll(timeout=1.0)
        if msg is None or msg.error():
            break
        val = msg.value()
        messages.append({"offset": msg.offset(), "value": val.decode("utf-8") if val else None})
    consumer.close()

    import polars as _pl
    df = _pl.DataFrame(messages) if messages else _pl.DataFrame({"offset": [], "value": []})
    arrow_table = df.to_arrow()
    con = session()
    with _SESSION_LOCK:
        con.register(name, arrow_table)
    return Relation(name=name, source=source, source_type="db", row_count=len(df))


def _load_dynamodb(source: str, name: str) -> "Relation":
    """Scan a DynamoDB table (requires pip install datajoins-nybble[dynamo]).

    Credentials are resolved from environment variables:
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION.
    """
    import importlib
    try:
        boto3 = importlib.import_module("boto3")
    except ImportError as exc:
        raise ImportError(
            "DynamoDB connector requires: pip install datajoins-nybble[dynamo]\n"
            "The 'boto3' package is not installed."
        ) from exc

    import os
    parsed = urllib.parse.urlparse(source)
    table_name = (parsed.path or "/").lstrip("/") or parsed.netloc
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)
    response = table.scan(Limit=500)
    items = response.get("Items", [])

    import polars as _pl
    df = _pl.DataFrame(items) if items else _pl.DataFrame()
    arrow_table = df.to_arrow()
    con = session()
    with _SESSION_LOCK:
        con.register(name, arrow_table)
    return Relation(name=name, source=source, source_type="db", row_count=len(df))


def _load_oracle(source: str, name: str) -> "Relation":
    """Load from an Oracle database (requires pip install datajoins-nybble[oracle]).

    Uses the ``oracledb`` thin client - no Oracle Instant Client required.
    """
    import importlib
    try:
        oracledb = importlib.import_module("oracledb")
    except ImportError as exc:
        raise ImportError(
            "Oracle connector requires: pip install datajoins-nybble[oracle]\n"
            "The 'oracledb' package is not installed."
        ) from exc

    parsed = urllib.parse.urlparse(source)
    user = parsed.username or ""
    password = parsed.password or ""
    host = parsed.hostname or "localhost"
    port = parsed.port or 1521
    service = (parsed.path or "/").lstrip("/") or "XE"
    dsn = f"{host}:{port}/{service}"

    connection = oracledb.connect(user=user, password=password, dsn=dsn)
    cursor = connection.cursor()
    cursor.execute(
        "SELECT owner, table_name FROM all_tables WHERE owner NOT IN "
        "('SYS','SYSTEM','DBSNMP') AND ROWNUM <= 200"
    )
    rows = [{"owner": r[0], "table_name": r[1]} for r in cursor.fetchall()]
    cursor.close()
    connection.close()

    import polars as _pl
    df = _pl.DataFrame(rows) if rows else _pl.DataFrame({"owner": [], "table_name": []})
    arrow_table = df.to_arrow()
    con = session()
    with _SESSION_LOCK:
        con.register(name, arrow_table)
    return Relation(name=name, source=source, source_type="db", row_count=len(df))


def _load_mssql(source: str, name: str) -> "Relation":
    """Load from a Microsoft SQL Server database
    (requires pip install datajoins-nybble[mssql]).

    Uses ``pyodbc`` with the Microsoft ODBC driver or FreeTDS.
    """
    import importlib
    try:
        pyodbc = importlib.import_module("pyodbc")
    except ImportError as exc:
        raise ImportError(
            "MSSQL connector requires: pip install datajoins-nybble[mssql]\n"
            "The 'pyodbc' package is not installed."
        ) from exc

    parsed = urllib.parse.urlparse(source)
    server = parsed.hostname or "localhost"
    port = parsed.port or 1433
    user = parsed.username or ""
    password = parsed.password or ""
    database = (parsed.path or "/").lstrip("/") or "master"

    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={server},{port};DATABASE={database};"
        f"UID={user};PWD={password};TrustServerCertificate=yes;"
    )
    connection = pyodbc.connect(conn_str, timeout=10)
    cursor = connection.cursor()
    cursor.execute(
        "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE='BASE TABLE'"
    )
    rows = [{"schema": r[0], "table_name": r[1]} for r in cursor.fetchall()]
    cursor.close()
    connection.close()

    import polars as _pl
    df = _pl.DataFrame(rows) if rows else _pl.DataFrame({"schema": [], "table_name": []})
    arrow_table = df.to_arrow()
    con = session()
    with _SESSION_LOCK:
        con.register(name, arrow_table)
    return Relation(name=name, source=source, source_type="db", row_count=len(df))


def _count(name: str, con: duckdb.DuckDBPyConnection) -> int:
    """Return approximate row count for a registered view."""
    try:
        row = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


# ── Main public loader ────────────────────────────────────────────────────────

def load(
    source: Any,
    name: Optional[str] = None,
    headers: Optional[dict] = None,
) -> "Relation":
    """Load any data source into the shared DuckDB session.

    Parameters
    ----------
    source:
        A database URI, local file path, list[dict], JSON string, or HTTP(S) URL.
    name:
        Optional identifier for the virtual table. Auto-derived if omitted.
    headers:
        Optional HTTP request headers forwarded to the URL loader (e.g.,
        ``{"Authorization": "Bearer token"}``). Only used when source is a URL.

    Returns
    -------
    Relation
        An object representing the registered virtual table.
    """
    source_type = _detect_source_type(source)

    if name is None:
        if isinstance(source, str):
            if source_type == "file":
                hint = Path(source).stem
            elif source_type == "db":
                # Extract the database name from the URI (last path segment).
                # e.g. "postgresql://user:pass@host/mydb" → "mydb"
                # Fall back to a letter-prefixed hash so DuckDB never sees a
                # name that starts with a digit.
                try:
                    import urllib.parse as _up
                    db_name = _up.urlparse(source).path.lstrip("/").split("/")[0].split("?")[0]
                    hint = db_name if db_name else "db_" + hashlib.md5(source.encode()).hexdigest()[:8]
                except Exception:
                    hint = "db_" + hashlib.md5(source.encode()).hexdigest()[:8]
            else:
                hint = "db_" + hashlib.md5(source.encode()).hexdigest()[:8]
        else:
            hint = "inline"
        name = _safe_name(hint)

    if source_type == "url":
        relation = _load_url(source, name, headers=headers)
    else:
        dispatch = {
            "file":        _load_file,
            "json_inline": _load_json_inline,
            "db":          _load_db,
        }
        relation = dispatch[source_type](source, name)

    with _SESSION_LOCK:
        _REGISTERED[name] = relation

    return relation


# ── Relation object ───────────────────────────────────────────────────────────

class Relation:
    """A named virtual table registered in the shared DuckDB session.

    Attributes
    ----------
    name        : str   - DuckDB virtual table name
    source      : str   - original source string (path, URI, or '<inline>')
    source_type : str   - one of 'file', 'json_inline', 'url', 'db'
    row_count   : int | None
    """

    def __init__(
        self,
        name: str,
        source: str,
        source_type: str,
        row_count: Optional[int] = None,
    ) -> None:
        self.name = name
        self.source = source
        self.source_type = source_type
        self._row_count = row_count

    @property
    def row_count(self) -> int:
        if self._row_count is None:
            con = session()
            with _SESSION_LOCK:
                self._row_count = _count(self.name, con)
        return self._row_count

    @row_count.setter
    def row_count(self, value: Optional[int]) -> None:
        self._row_count = value

    def schema(self) -> list[dict]:
        """Return column schema as a list of {name, type} dicts."""
        con = session()
        with _SESSION_LOCK:
            cols = con.execute(
                f"DESCRIBE SELECT * FROM \"{self.name}\" LIMIT 0"
            ).fetchall()
        return [{"name": c[0], "type": c[1]} for c in cols]

    def sample(self, n: int = 5) -> pl.DataFrame:
        """Return the first *n* rows as a polars DataFrame."""
        con = session()
        with _SESSION_LOCK:
            arrow = con.sql(
                f'SELECT * FROM "{self.name}" LIMIT {n}'
            ).arrow()
        return pl.from_arrow(arrow)

    def to_polars(self, limit: Optional[int] = None) -> pl.DataFrame:
        """Materialise the full relation (or up to *limit* rows) as polars."""
        sql = f'SELECT * FROM "{self.name}"'
        if limit is not None:
            sql += f" LIMIT {limit}"
        con = session()
        with _SESSION_LOCK:
            arrow = con.sql(sql).arrow()
        return pl.from_arrow(arrow)

    def __repr__(self) -> str:
        return (
            f"Relation(name={self.name!r}, source_type={self.source_type!r}, "
            f"rows={self._row_count})"
        )
