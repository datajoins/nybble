"""
config.py - Declarative Workspace Configuration Loader

Reads a nybble.yaml (or any YAML file) and loads all declared sources into the
shared DuckDB session via resolver.load(). This is the P1-3 implementation:
a single config file replaces repeated --source flags on every CLI command.

Schema (nybble.yaml)
--------------------
version: "1.0"

workspace:
  name: "my-workspace"          # optional display name
  environment: "local"          # informational only

databases:
  - id: "my-db"                 # becomes the DuckDB table name
    type: "postgresql"          # postgresql | mysql | sqlite | duckdb
    connection_string: "..."    # supports ${ENV_VAR} interpolation
    tables: ["users", "orders"] # optional: load only these tables (future)
    cache: true                 # informational only (all loads are in-memory)

files:
  - id: "q4-report"             # becomes the DuckDB table name
    type: "xlsx"                # csv | tsv | parquet | json | jsonl | xlsx | xls | avro
    path: "./data/report.xlsx"  # relative to cwd, absolute, or S3/GCS/R2 URI
    sheet: "Sheet1"             # xlsx/xls only, passed to polars (future)

  # Object-storage examples (credentials from env or ~/.aws/credentials):
  # - id: "s3-events"
  #   type: "parquet"
  #   path: "s3://my-bucket/events/year=2024/*.parquet"  # glob supported
  #
  # - id: "gcs-logs"
  #   type: "csv"
  #   path: "gs://my-bucket/logs/2024-01-01.csv"
  #   # GCS Application Default Credentials resolved by DuckDB httpfs extension
  #
  # - id: "kafka-export"
  #   type: "avro"
  #   path: "s3://my-bucket/kafka-exports/topic.avro"
  #
  # S3 credentials can be injected via environment variables:
  #   AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}
  #   AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}
  # These are resolved by config.py _interpolate() from the shell environment.
  # Nybble never logs or stores credential values.

streams:
  - id: "github-api"            # becomes the DuckDB table name
    type: "api"                 # only "api" is supported
    url: "https://..."          # HTTP/HTTPS URL
    method: "GET"               # GET only (resolver._load_url uses GET)
    headers:                    # injected as request headers (✅ supported)
      Authorization: "Bearer ${GITHUB_TOKEN}"
    format: "json"              # only "json" is supported

graph_engine:
  auto_infer_erd: true          # informational; erd() always infers FKs
  export_format: "jsonld"       # graphml | jsonld | html (default: all three)
                                # "triples" is mapped to "jsonld"
  semantic_layer:
    enabled: false              # sentence-transformers not yet shipped (P2-3)
    embedding_model: "..."      # parsed but ignored until P2-3 ships

Public API
----------
  load_config(path) -> WorkspaceConfig
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class DatabaseSource:
    id: str
    type: str
    connection_string: str
    tables: list[str] = field(default_factory=list)
    cache: bool = True


@dataclass
class FileSource:
    id: str
    type: str
    path: str
    sheet: Optional[str] = None


@dataclass
class StreamSource:
    id: str
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    format: str = "json"


@dataclass
class GraphEngineConfig:
    auto_infer_erd: bool = True
    export_format: str = "all"          # graphml | jsonld | html | all
    semantic_layer_enabled: bool = False
    embedding_model: Optional[str] = None


@dataclass
class WorkspaceConfig:
    name: str
    environment: str
    databases: list[DatabaseSource]
    files: list[FileSource]
    streams: list[StreamSource]
    graph_engine: GraphEngineConfig


# ── Environment variable interpolation ───────────────────────────────────────

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _interpolate(value: str) -> str:
    """Replace ${VAR_NAME} tokens with their environment variable values.

    Raises KeyError if a referenced variable is not set.
    """
    def replacer(match: re.Match) -> str:
        var = match.group(1)
        if var not in os.environ:
            raise KeyError(
                f"nybble.yaml references environment variable ${{{var}}} "
                f"but it is not set. Export it before running nybble."
            )
        return os.environ[var]

    return _ENV_PATTERN.sub(replacer, value)


def _interpolate_deep(obj: Any) -> Any:
    """Recursively interpolate ${VAR} tokens in any string-valued leaf."""
    if isinstance(obj, str):
        return _interpolate(obj)
    if isinstance(obj, dict):
        return {k: _interpolate_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_deep(item) for item in obj]
    return obj


# ── Parser ────────────────────────────────────────────────────────────────────

def _parse_export_format(raw: str) -> str:
    """Normalize export_format value.

    "triples" is a common alias for JSON-LD (closest semantic web format).
    """
    mapping = {
        "triples": "jsonld",
        "json-ld": "jsonld",
        "json_ld": "jsonld",
        "cypher": "graphml",   # no Cypher exporter yet; fall back to graphml
        "pgq":    "graphml",   # no PGQ DDL exporter yet (P2-6); fall back
    }
    normalized = raw.strip().lower()
    if normalized in mapping:
        import warnings
        warnings.warn(
            f"nybble.yaml export_format '{raw}' is not a native Nybble format. "
            f"Mapped to '{mapping[normalized]}'. "
            f"Supported values: graphml, jsonld, html, all.",
            stacklevel=3,
        )
        return mapping[normalized]
    if normalized not in ("graphml", "jsonld", "html", "all"):
        import warnings
        warnings.warn(
            f"nybble.yaml export_format '{raw}' is not recognised. "
            f"Falling back to 'all'. "
            f"Supported values: graphml, jsonld, html, all.",
            stacklevel=3,
        )
        return "all"
    return normalized


def _parse_config(data: dict) -> WorkspaceConfig:
    """Parse a raw YAML dict into a WorkspaceConfig."""
    workspace = data.get("workspace", {})

    # databases
    databases: list[DatabaseSource] = []
    for entry in data.get("databases", []):
        if entry.get("tables"):
            raise NotImplementedError(
                f"'tables:' filtering under databases.{entry.get('id', '<unknown>')} "
                "is not yet implemented (roadmap P2). "
                "Remove the 'tables:' key from your nybble.yaml to proceed."
            )
        databases.append(DatabaseSource(
            id=entry["id"],
            type=entry.get("type", "postgresql"),
            connection_string=entry["connection_string"],
            tables=entry.get("tables", []),
            cache=entry.get("cache", True),
        ))

    # files
    files: list[FileSource] = []
    for entry in data.get("files", []):
        files.append(FileSource(
            id=entry["id"],
            type=entry.get("type", "csv"),
            path=entry["path"],
            sheet=entry.get("sheet"),
        ))

    # streams
    streams: list[StreamSource] = []
    for entry in data.get("streams", []):
        streams.append(StreamSource(
            id=entry["id"],
            url=entry["url"],
            method=entry.get("method", "GET").upper(),
            headers=entry.get("headers", {}),
            format=entry.get("format", "json"),
        ))

    # graph_engine
    ge_raw = data.get("graph_engine", {})
    sem = ge_raw.get("semantic_layer", {})
    if sem.get("enabled", False):
        import warnings
        warnings.warn(
            "semantic_layer.enabled is set to true but the semantic layer is not yet "
            "active in this version. The flag will be ignored.",
            UserWarning,
            stacklevel=3,
        )
    graph_engine = GraphEngineConfig(
        auto_infer_erd=ge_raw.get("auto_infer_erd", True),
        export_format=_parse_export_format(ge_raw.get("export_format", "all")),
        semantic_layer_enabled=False,   # not shipped
        embedding_model=sem.get("embedding_model"),
    )

    return WorkspaceConfig(
        name=workspace.get("name", "nybble-workspace"),
        environment=workspace.get("environment", "local"),
        databases=databases,
        files=files,
        streams=streams,
        graph_engine=graph_engine,
    )


# ── Public loader ─────────────────────────────────────────────────────────────

def load_config(path: str | Path) -> WorkspaceConfig:
    """Parse a nybble.yaml file and return a WorkspaceConfig.

    Parameters
    ----------
    path:
        Path to the YAML config file.

    Returns
    -------
    WorkspaceConfig
        Parsed and validated configuration.

    Raises
    ------
    FileNotFoundError
        If the config file does not exist.
    KeyError
        If a required field is missing or an ${ENV_VAR} is not set.
    """
    try:
        import yaml  # pyyaml
    except ImportError as exc:
        raise ImportError(
            "pyyaml is required to load nybble.yaml config files. "
            "Install it with: pip install pyyaml"
        ) from exc

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw = _interpolate_deep(raw)
    return _parse_config(raw)


def apply_config(config: WorkspaceConfig) -> list:
    """Load all sources declared in a WorkspaceConfig into the DuckDB session.

    Parameters
    ----------
    config:
        A parsed WorkspaceConfig (from load_config()).

    Returns
    -------
    list[Relation]
        All successfully loaded Relation objects.
    """
    from nybble.resolver import load as _load, _safe_name

    loaded = []
    errors = []

    for db in config.databases:
        try:
            safe_id = _safe_name(db.id)
            rel = _load(db.connection_string, name=safe_id)
            loaded.append(rel)
        except Exception as exc:
            errors.append(f"[database:{db.id}] {exc}")

    for f in config.files:
        try:
            safe_id = _safe_name(f.id)
            rel = _load(f.path, name=safe_id)
            loaded.append(rel)
        except Exception as exc:
            errors.append(f"[file:{f.id}] {exc}")

    for s in config.streams:
        try:
            safe_id = _safe_name(s.id)
            rel = _load(s.url, name=safe_id, headers=s.headers or None)
            loaded.append(rel)
        except Exception as exc:
            errors.append(f"[stream:{s.id}] {exc}")

    if errors:
        import warnings
        for msg in errors:
            warnings.warn(f"nybble config load error - {msg}", stacklevel=2)

    return loaded


# ── Config file discovery ─────────────────────────────────────────────────────

_DEFAULT_CONFIG_NAMES = ("nybble.yaml", "nybble.yml", ".nybble.yaml", ".nybble.yml")


def find_config(start_dir: str | Path | None = None) -> Optional[Path]:
    """Search for a nybble.yaml file starting from start_dir (default: cwd).

    Looks in the start directory only (does not walk upward).
    Returns the Path if found, None otherwise.
    """
    search_dir = Path(start_dir) if start_dir else Path.cwd()
    for name in _DEFAULT_CONFIG_NAMES:
        candidate = search_dir / name
        if candidate.exists():
            return candidate
    return None
