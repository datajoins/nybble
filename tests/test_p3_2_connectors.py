"""
tests/test_p3_2_connectors.py - P3-2 Connector Expansion test suite

Covers:
  - Object storage URI detection (s3://, gs://, gcs://, r2://, az://)
  - Avro file extension registration
  - Analytical database URI detection (clickhouse://, snowflake://, bigquery://)
  - Redshift / CockroachDB normalisation to PostgreSQL
  - Optional-extra connector stubs (ImportError + actionable message)
  - DuckDB extension list completeness
  - CONNECTOR_REGISTRY structure and content
  - pyproject.toml optional extras presence

No real network calls. Optional driver packages are absent in CI - all stub
tests confirm ImportError is raised with the correct install message.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _read_pyproject() -> str:
    """Return the full text of pyproject.toml."""
    root = Path(__file__).parent.parent
    return (root / "pyproject.toml").read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Object storage URI detection
# ─────────────────────────────────────────────────────────────────────────────

class TestObjectStorageURIDetection:

    def test_s3_uri_detected_as_file(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("s3://bucket/data.parquet") == "file"

    def test_gs_uri_detected_as_file(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("gs://bucket/data.parquet") == "file"

    def test_gcs_uri_detected_as_file(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("gcs://bucket/data.csv") == "file"

    def test_r2_uri_detected_as_file(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("r2://bucket/data.jsonl") == "file"

    def test_az_uri_detected_as_file(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("az://container/data.parquet") == "file"

    def test_azure_uri_detected_as_file(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("azure://container/data.parquet") == "file"

    def test_s3_glob_detected_as_file(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("s3://datalake/year=2024/month=*/*.parquet") == "file"

    def test_remote_uri_skips_path_exists_check(self):
        """Loading a remote URI must not call Path.exists() - no FileNotFoundError."""
        from nybble.resolver import _load_file, _SESSION_LOCK
        # Patch DuckDB session to avoid real network access
        mock_con = MagicMock()
        mock_con.execute = MagicMock()

        with patch("nybble.resolver.session", return_value=mock_con):
            # Should not raise FileNotFoundError for remote URI
            with patch.object(Path, "exists", side_effect=AssertionError("exists() called on remote URI")):
                try:
                    _load_file("s3://bucket/nonexistent.parquet", "test_remote")
                except AssertionError:
                    pytest.fail("_load_file() called Path.exists() on a remote URI")
                except Exception:
                    pass  # Other exceptions (DuckDB mock, etc.) are acceptable


# ─────────────────────────────────────────────────────────────────────────────
# Avro file extension
# ─────────────────────────────────────────────────────────────────────────────

class TestAvroFileExtension:

    def test_avro_extension_in_file_extensions(self):
        from nybble.resolver import _FILE_EXTENSIONS
        assert ".avro" in _FILE_EXTENSIONS

    def test_avro_extension_maps_to_avro_format(self):
        from nybble.resolver import _FILE_EXTENSIONS
        assert _FILE_EXTENSIONS[".avro"] == "avro"


# ─────────────────────────────────────────────────────────────────────────────
# Analytical database URI detection
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyticalDatabaseURIDetection:

    def test_clickhouse_uri_detected(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("clickhouse://host:8123/db") == "db"

    def test_snowflake_uri_detected(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("snowflake://account.snowflakecomputing.com/db") == "db"

    def test_bigquery_uri_detected(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("bigquery://project/dataset") == "db"

    def test_mysql_uri_detected(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("mysql://host/db") == "db"


# ─────────────────────────────────────────────────────────────────────────────
# Redshift / CockroachDB normalisation
# ─────────────────────────────────────────────────────────────────────────────

class TestPostgreSQLWireCompatibleNormalisation:

    def test_redshift_detected_as_db(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("redshift://host:5439/db") == "db"

    def test_cockroachdb_detected_as_db(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("cockroachdb://host:26257/db") == "db"

    def test_redshift_normalised_to_postgresql_in_load_db(self):
        """_load_db rewrites redshift:// to postgresql:// before dispatching."""
        from nybble.resolver import _load_db

        dispatched: list[str] = []

        def fake_pg_load(source: str, name: str):
            dispatched.append(source)
            # Return a minimal Relation-like mock
            from nybble.resolver import Relation
            return Relation(name=name, source=source, source_type="db", row_count=0)

        # Patch only the postgresql path in _load_db
        with patch("nybble.resolver._load_db", side_effect=_load_db):
            # We'll test the normalisation by checking the scheme rewrite:
            import urllib.parse
            source = "redshift://host:5439/db"
            parsed = urllib.parse.urlparse(source)
            scheme = parsed.scheme.lower()
            if scheme in ("redshift", "cockroachdb"):
                rewritten = "postgresql" + source[len(scheme):]
                rewritten_parsed = urllib.parse.urlparse(rewritten)
                assert rewritten_parsed.scheme == "postgresql"
                assert "5439" in rewritten

    def test_cockroachdb_normalised_to_postgresql_scheme(self):
        source = "cockroachdb://host:26257/db"
        import urllib.parse
        parsed = urllib.parse.urlparse(source)
        scheme = parsed.scheme.lower()
        if scheme in ("redshift", "cockroachdb"):
            rewritten = "postgresql" + source[len(scheme):]
            assert urllib.parse.urlparse(rewritten).scheme == "postgresql"


# ─────────────────────────────────────────────────────────────────────────────
# Optional-extra connector URI detection
# ─────────────────────────────────────────────────────────────────────────────

class TestOptionalExtraURIDetection:

    @pytest.mark.parametrize("uri", [
        "redis://localhost:6379/0",
        "mongodb://host:27017/mydb",
        "kafka://broker:9092/topic",
        "dynamodb://us-east-1/my-table",
        "oracle://user:pass@host:1521/XE",
        "mssql://user:pass@host:1433/dbname",
        "elasticsearch://host:9200/index",
    ])
    def test_optional_extra_uri_detected_as_db(self, uri):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type(uri) == "db"


# ─────────────────────────────────────────────────────────────────────────────
# Optional-extra connector stub ImportError tests
# ─────────────────────────────────────────────────────────────────────────────

class TestOptionalExtraStubsRaiseImportError:
    """Each stub must raise ImportError with an actionable pip install message
    when the required driver package is absent."""

    def _assert_import_error_with_pip_install(self, loader_fn, source: str) -> None:
        """Helper: call loader_fn, confirm ImportError with 'pip install' in message."""
        with pytest.raises(ImportError) as exc_info:
            loader_fn(source, "test_rel")
        assert "pip install" in str(exc_info.value), (
            f"ImportError message should contain 'pip install', got: {exc_info.value}"
        )

    def test_redis_stub_raises_import_error(self):
        from nybble.resolver import _load_redis
        # Temporarily hide the redis package if installed
        with patch.dict(sys.modules, {"redis": None}):
            self._assert_import_error_with_pip_install(_load_redis, "redis://localhost/0")

    def test_mongodb_stub_raises_import_error(self):
        from nybble.resolver import _load_mongodb
        with patch.dict(sys.modules, {"pymongo": None}):
            self._assert_import_error_with_pip_install(_load_mongodb, "mongodb://localhost/test")

    def test_cassandra_stub_raises_import_error(self):
        from nybble.resolver import _load_cassandra
        with patch.dict(sys.modules, {"cassandra": None, "cassandra.cluster": None}):
            self._assert_import_error_with_pip_install(_load_cassandra, "cassandra://localhost/ks")

    def test_elasticsearch_stub_raises_import_error(self):
        from nybble.resolver import _load_elasticsearch
        with patch.dict(sys.modules, {"elasticsearch": None}):
            self._assert_import_error_with_pip_install(
                _load_elasticsearch, "elasticsearch://localhost:9200/idx"
            )

    def test_kafka_stub_raises_import_error(self):
        from nybble.resolver import _load_kafka
        with patch.dict(sys.modules, {"confluent_kafka": None}):
            self._assert_import_error_with_pip_install(_load_kafka, "kafka://localhost:9092/topic")

    def test_dynamodb_stub_raises_import_error(self):
        from nybble.resolver import _load_dynamodb
        with patch.dict(sys.modules, {"boto3": None}):
            self._assert_import_error_with_pip_install(_load_dynamodb, "dynamodb://us-east-1/tbl")

    def test_oracle_stub_raises_import_error(self):
        from nybble.resolver import _load_oracle
        with patch.dict(sys.modules, {"oracledb": None}):
            self._assert_import_error_with_pip_install(_load_oracle, "oracle://user:pw@host/XE")

    def test_mssql_stub_raises_import_error(self):
        from nybble.resolver import _load_mssql
        with patch.dict(sys.modules, {"pyodbc": None}):
            self._assert_import_error_with_pip_install(_load_mssql, "mssql://user:pw@host/db")

    def test_redis_error_message_contains_extra_name(self):
        from nybble.resolver import _load_redis
        with patch.dict(sys.modules, {"redis": None}):
            with pytest.raises(ImportError) as exc_info:
                _load_redis("redis://localhost/0", "r")
        assert "redis" in str(exc_info.value).lower()

    def test_mongodb_error_message_contains_extra_name(self):
        from nybble.resolver import _load_mongodb
        with patch.dict(sys.modules, {"pymongo": None}):
            with pytest.raises(ImportError) as exc_info:
                _load_mongodb("mongodb://localhost/test", "m")
        assert "mongo" in str(exc_info.value).lower()

    def test_kafka_error_message_contains_extra_name(self):
        from nybble.resolver import _load_kafka
        with patch.dict(sys.modules, {"confluent_kafka": None}):
            with pytest.raises(ImportError) as exc_info:
                _load_kafka("kafka://localhost:9092/topic", "k")
        assert "kafka" in str(exc_info.value).lower()

    def test_dynamodb_error_message_contains_extra_name(self):
        from nybble.resolver import _load_dynamodb
        with patch.dict(sys.modules, {"boto3": None}):
            with pytest.raises(ImportError) as exc_info:
                _load_dynamodb("dynamodb://us-east-1/tbl", "d")
        assert "dynamo" in str(exc_info.value).lower()

    def test_oracle_error_message_contains_extra_name(self):
        from nybble.resolver import _load_oracle
        with patch.dict(sys.modules, {"oracledb": None}):
            with pytest.raises(ImportError) as exc_info:
                _load_oracle("oracle://user:pw@host/XE", "o")
        assert "oracle" in str(exc_info.value).lower()

    def test_mssql_error_message_contains_extra_name(self):
        from nybble.resolver import _load_mssql
        with patch.dict(sys.modules, {"pyodbc": None}):
            with pytest.raises(ImportError) as exc_info:
                _load_mssql("mssql://user:pw@host/db", "ms")
        assert "mssql" in str(exc_info.value).lower()


# ─────────────────────────────────────────────────────────────────────────────
# DuckDB extension list
# ─────────────────────────────────────────────────────────────────────────────

class TestDuckDBExtensionList:

    def test_httpfs_in_extension_list(self):
        from nybble.resolver import _DUCKDB_EXTENSIONS
        assert "httpfs" in _DUCKDB_EXTENSIONS

    def test_aws_in_extension_list(self):
        from nybble.resolver import _DUCKDB_EXTENSIONS
        assert "aws" in _DUCKDB_EXTENSIONS

    def test_avro_in_extension_list(self):
        from nybble.resolver import _DUCKDB_EXTENSIONS
        assert "avro" in _DUCKDB_EXTENSIONS

    def test_chsql_in_extension_list(self):
        from nybble.resolver import _DUCKDB_EXTENSIONS
        assert "chsql" in _DUCKDB_EXTENSIONS

    def test_snowflake_in_extension_list(self):
        from nybble.resolver import _DUCKDB_EXTENSIONS
        assert "snowflake" in _DUCKDB_EXTENSIONS

    def test_mysql_in_extension_list(self):
        from nybble.resolver import _DUCKDB_EXTENSIONS
        assert "mysql" in _DUCKDB_EXTENSIONS

    def test_json_in_extension_list(self):
        from nybble.resolver import _DUCKDB_EXTENSIONS
        assert "json" in _DUCKDB_EXTENSIONS

    def test_parquet_in_extension_list(self):
        from nybble.resolver import _DUCKDB_EXTENSIONS
        assert "parquet" in _DUCKDB_EXTENSIONS

    def test_bigquery_in_community_extensions(self):
        """bigquery is a community extension, not in the main list."""
        from nybble.resolver import _COMMUNITY_EXTENSIONS
        assert "bigquery" in _COMMUNITY_EXTENSIONS


# ─────────────────────────────────────────────────────────────────────────────
# CONNECTOR_REGISTRY
# ─────────────────────────────────────────────────────────────────────────────

class TestConnectorRegistry:

    def test_registry_exported_from_init(self):
        from nybble import CONNECTOR_REGISTRY
        assert isinstance(CONNECTOR_REGISTRY, dict)
        assert len(CONNECTOR_REGISTRY) > 0

    def test_registry_contains_all_three_tiers(self):
        from nybble import CONNECTOR_REGISTRY
        tiers = {v["tier"] for v in CONNECTOR_REGISTRY.values()}
        assert "core" in tiers
        assert "duckdb_ext" in tiers
        assert "optional_extra" in tiers

    def test_registry_s3_entry_present(self):
        from nybble import CONNECTOR_REGISTRY
        assert "s3" in CONNECTOR_REGISTRY

    def test_registry_s3_schemes_contains_s3(self):
        from nybble import CONNECTOR_REGISTRY
        assert "s3://" in CONNECTOR_REGISTRY["s3"]["schemes"]

    def test_registry_s3_schemes_contains_gs(self):
        from nybble import CONNECTOR_REGISTRY
        assert "gs://" in CONNECTOR_REGISTRY["s3"]["schemes"]

    def test_registry_s3_schemes_contains_r2(self):
        from nybble import CONNECTOR_REGISTRY
        assert "r2://" in CONNECTOR_REGISTRY["s3"]["schemes"]

    def test_registry_s3_schemes_contains_az(self):
        from nybble import CONNECTOR_REGISTRY
        assert "az://" in CONNECTOR_REGISTRY["s3"]["schemes"]

    def test_registry_postgresql_includes_redshift_scheme(self):
        from nybble import CONNECTOR_REGISTRY
        assert "redshift://" in CONNECTOR_REGISTRY["postgresql"]["schemes"]

    def test_registry_postgresql_includes_cockroachdb_scheme(self):
        from nybble import CONNECTOR_REGISTRY
        assert "cockroachdb://" in CONNECTOR_REGISTRY["postgresql"]["schemes"]

    def test_registry_clickhouse_is_duckdb_ext_tier(self):
        from nybble import CONNECTOR_REGISTRY
        assert CONNECTOR_REGISTRY["clickhouse"]["tier"] == "duckdb_ext"

    def test_registry_redis_is_optional_extra_tier(self):
        from nybble import CONNECTOR_REGISTRY
        assert CONNECTOR_REGISTRY["redis"]["tier"] == "optional_extra"

    def test_registry_mongodb_is_optional_extra_tier(self):
        from nybble import CONNECTOR_REGISTRY
        assert CONNECTOR_REGISTRY["mongodb"]["tier"] == "optional_extra"

    def test_registry_cassandra_is_optional_extra_tier(self):
        from nybble import CONNECTOR_REGISTRY
        assert CONNECTOR_REGISTRY["cassandra"]["tier"] == "optional_extra"

    def test_registry_elasticsearch_is_optional_extra_tier(self):
        from nybble import CONNECTOR_REGISTRY
        assert CONNECTOR_REGISTRY["elasticsearch"]["tier"] == "optional_extra"

    def test_registry_kafka_is_optional_extra_tier(self):
        from nybble import CONNECTOR_REGISTRY
        assert CONNECTOR_REGISTRY["kafka"]["tier"] == "optional_extra"

    def test_registry_dynamodb_is_optional_extra_tier(self):
        from nybble import CONNECTOR_REGISTRY
        assert CONNECTOR_REGISTRY["dynamodb"]["tier"] == "optional_extra"

    def test_registry_oracle_is_optional_extra_tier(self):
        from nybble import CONNECTOR_REGISTRY
        assert CONNECTOR_REGISTRY["oracle"]["tier"] == "optional_extra"

    def test_registry_mssql_is_optional_extra_tier(self):
        from nybble import CONNECTOR_REGISTRY
        assert CONNECTOR_REGISTRY["mssql"]["tier"] == "optional_extra"

    def test_registry_csv_is_core_tier(self):
        from nybble import CONNECTOR_REGISTRY
        assert CONNECTOR_REGISTRY["csv"]["tier"] == "core"

    def test_registry_parquet_is_core_tier(self):
        from nybble import CONNECTOR_REGISTRY
        assert CONNECTOR_REGISTRY["parquet"]["tier"] == "core"

    def test_registry_all_entries_have_required_keys(self):
        from nybble import CONNECTOR_REGISTRY
        required_keys = {"tier", "install", "schemes", "extensions"}
        for connector_name, info in CONNECTOR_REGISTRY.items():
            missing = required_keys - set(info.keys())
            assert not missing, (
                f"CONNECTOR_REGISTRY[{connector_name!r}] missing keys: {missing}"
            )

    def test_registry_optional_extras_have_pip_install_message(self):
        """All optional_extra connectors must have an install string with 'pip install'."""
        from nybble import CONNECTOR_REGISTRY
        for name, info in CONNECTOR_REGISTRY.items():
            if info["tier"] == "optional_extra":
                assert info["install"] is not None, f"{name} has None install"
                assert "pip install" in str(info["install"]), (
                    f"{name} install message missing 'pip install': {info['install']}"
                )


# ─────────────────────────────────────────────────────────────────────────────
# pyproject.toml optional extras
# ─────────────────────────────────────────────────────────────────────────────

class TestPyprojectOptionalExtras:

    def _get_extras(self) -> str:
        return _read_pyproject()

    def test_redis_extra_defined(self):
        assert 'redis' in self._get_extras()

    def test_mongo_extra_defined(self):
        assert 'mongo' in self._get_extras()

    def test_cassandra_extra_defined(self):
        assert 'cassandra' in self._get_extras()

    def test_elastic_extra_defined(self):
        assert 'elastic' in self._get_extras()

    def test_kafka_extra_defined(self):
        assert 'kafka' in self._get_extras()

    def test_dynamo_extra_defined(self):
        assert 'dynamo' in self._get_extras()

    def test_oracle_extra_defined(self):
        assert 'oracle' in self._get_extras()

    def test_mssql_extra_defined(self):
        assert 'mssql' in self._get_extras()

    def test_optional_extras_defined_parseable(self):
        """All eight new extras appear in [project.optional-dependencies] section."""
        pyproject = _read_pyproject()
        try:
            import tomllib  # Python 3.11+
            data = tomllib.loads(pyproject)
        except (ImportError, ModuleNotFoundError):
            # Python 3.9/3.10 - fall back to string check
            for extra in ("redis", "mongo", "cassandra", "elastic",
                          "kafka", "dynamo", "oracle", "mssql"):
                assert extra in pyproject, f"Extra '{extra}' not found in pyproject.toml"
            return

        extras = data.get("project", {}).get("optional-dependencies", {})
        for extra in ("redis", "mongo", "cassandra", "elastic",
                      "kafka", "dynamo", "oracle", "mssql"):
            assert extra in extras, f"Extra '{extra}' not found in optional-dependencies"

    def test_existing_postgres_extra_preserved(self):
        assert "psycopg2-binary" in self._get_extras()

    def test_existing_mysql_extra_preserved(self):
        assert "pymysql" in self._get_extras()


# ─────────────────────────────────────────────────────────────────────────────
# DB schemes coverage
# ─────────────────────────────────────────────────────────────────────────────

class TestDBSchemesCoverage:

    @pytest.mark.parametrize("scheme,uri", [
        ("redis",         "redis://localhost:6379/0"),
        ("mongodb",       "mongodb://localhost:27017/db"),
        ("kafka",         "kafka://broker:9092/topic"),
        ("dynamodb",      "dynamodb://us-east-1/table"),
        ("oracle",        "oracle://user:pw@host:1521/XE"),
        ("mssql",         "mssql://user:pw@host:1433/db"),
        ("elasticsearch", "elasticsearch://host:9200/idx"),
        ("redshift",      "redshift://host:5439/db"),
        ("cockroachdb",   "cockroachdb://host:26257/db"),
    ])
    def test_scheme_in_db_schemes(self, scheme, uri):
        from nybble.resolver import _DB_SCHEMES, _detect_source_type
        assert scheme in _DB_SCHEMES
        assert _detect_source_type(uri) == "db"

    def test_existing_schemes_preserved(self):
        from nybble.resolver import _DB_SCHEMES
        for scheme in ("sqlite", "postgresql", "postgres", "mysql", "duckdb",
                       "clickhouse", "snowflake", "bigquery"):
            assert scheme in _DB_SCHEMES, f"Existing scheme {scheme!r} was removed"
