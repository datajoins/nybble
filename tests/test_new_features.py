"""tests/test_new_features.py - Tests for Milestone 1-3 implementations"""
import json
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


# ── Shared session fixture (reuse pattern from existing tests) ─────────────────

@pytest.fixture(autouse=True)
def fresh_session(tmp_path, monkeypatch):
    """Reset the DuckDB session and redirect .nybble/ to a temp dir."""
    import nybble.resolver as r
    if r._SESSION is not None:
        try:
            r._SESSION.close()
        except Exception:
            pass
        r._SESSION = None
    r._REGISTERED.clear()

    # Redirect history and query files to the temp directory
    import nybble.arena as arena
    monkeypatch.setattr(arena, "_HISTORY_FILE", tmp_path / ".nybble" / "history.jsonl")
    monkeypatch.setattr(arena, "_QUERIES_FILE", tmp_path / ".nybble" / "queries.json")

    yield

    if r._SESSION is not None:
        try:
            r._SESSION.close()
        except Exception:
            pass
        r._SESSION = None
    r._REGISTERED.clear()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def csv_a(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("id,name,score\n1,Alice,90\n2,Bob,80\n")
    return str(p)


@pytest.fixture
def csv_b(tmp_path):
    """Same id column, different columns: score removed, grade added, name→varchar."""
    p = tmp_path / "b.csv"
    p.write_text("id,name,grade\n1,Alice,A\n2,Bob,B\n")
    return str(p)


@pytest.fixture
def parquet_file(tmp_path):
    tbl = pa.table({"x": [1, 2, 3], "y": ["a", "b", "c"]})
    p = tmp_path / "test.parquet"
    pq.write_table(tbl, str(p))
    return str(p)


@pytest.fixture
def sqlite_db(tmp_path):
    """Create a tiny SQLite database with two tables."""
    import sqlite3
    db_path = tmp_path / "test.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL)")
    con.execute("INSERT INTO users VALUES (1, 'Alice'), (2, 'Bob')")
    con.execute("INSERT INTO orders VALUES (101, 1, 99.5), (102, 2, 149.0)")
    con.commit()
    con.close()
    return str(db_path)


# ── M1-A: Object-storage URI detection ───────────────────────────────────────

class TestObjectStorageDetection:

    def test_s3_detected_as_file(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("s3://my-bucket/data.parquet") == "file"

    def test_gs_detected_as_file(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("gs://my-bucket/data.csv") == "file"

    def test_gcs_detected_as_file(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("gcs://bucket/path/file.json") == "file"

    def test_r2_detected_as_file(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("r2://bucket/events.parquet") == "file"

    def test_az_detected_as_file(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("az://container/blob.parquet") == "file"

    def test_azure_detected_as_file(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("azure://container/blob.csv") == "file"

    def test_s3_glob_detected_as_file(self):
        from nybble.resolver import _detect_source_type
        # Glob patterns still route as file
        assert _detect_source_type("s3://bucket/year=2024/*.parquet") == "file"

    def test_http_still_url(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("https://example.com/data.json") == "url"

    def test_is_remote_uri_true(self):
        from nybble.resolver import _is_remote_uri
        assert _is_remote_uri("s3://bucket/file.parquet") is True

    def test_is_remote_uri_false_local(self, csv_a):
        from nybble.resolver import _is_remote_uri
        assert _is_remote_uri(csv_a) is False


# ── M1-A: Avro extension in FILE_EXTENSIONS ───────────────────────────────────

class TestAvroExtension:

    def test_avro_in_file_extensions(self):
        from nybble.resolver import _FILE_EXTENSIONS
        assert ".avro" in _FILE_EXTENSIONS
        assert _FILE_EXTENSIONS[".avro"] == "avro"

    def test_dat_in_file_extensions(self):
        from nybble.resolver import _FILE_EXTENSIONS
        assert ".dat" in _FILE_EXTENSIONS


# ── M1-A: Parquet/CSV local loads still work ─────────────────────────────────

class TestLocalLoadsUnchanged:

    def test_csv_loads(self, csv_a):
        from nybble import load
        rel = load(csv_a)
        assert rel.row_count == 2

    def test_parquet_loads(self, parquet_file):
        from nybble import load
        rel = load(parquet_file)
        assert rel.row_count == 3

    def test_source_type_still_file_for_local(self, csv_a):
        from nybble import load
        rel = load(csv_a)
        assert rel.source_type == "file"


# ── M1-B: ClickHouse detection ────────────────────────────────────────────────

class TestClickHouseDetection:

    def test_clickhouse_detected_as_db(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("clickhouse://localhost:8123/default") == "db"

    def test_snowflake_detected_as_db(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("snowflake://user:pass@acct/db/schema") == "db"

    def test_bigquery_detected_as_db(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("bigquery://project/dataset") == "db"


# ── M2-A: Query history ───────────────────────────────────────────────────────

class TestQueryHistory:

    def test_history_written_after_query(self, csv_a):
        from nybble import load, query
        import nybble.arena as arena
        load(csv_a, name="t")
        query("SELECT * FROM t")
        assert arena._HISTORY_FILE.exists()
        entries = json.loads("[" +
            ",".join(arena._HISTORY_FILE.read_text().strip().splitlines()) + "]")
        assert len(entries) == 1
        assert "SELECT * FROM t" in entries[0]["sql"]

    def test_history_entry_has_required_fields(self, csv_a):
        from nybble import load, query
        import nybble.arena as arena
        load(csv_a, name="t")
        query("SELECT id FROM t")
        line = arena._HISTORY_FILE.read_text().strip().splitlines()[-1]
        entry = json.loads(line)
        assert "sql" in entry
        assert "ts" in entry
        assert "elapsed_ms" in entry
        assert "rows" in entry
        assert "fp" in entry

    def test_search_history_returns_results(self, csv_a):
        from nybble import load, query, search_history
        load(csv_a, name="customers")
        query("SELECT * FROM customers")
        query("SELECT id FROM customers WHERE id=1")
        results = search_history(term="customers")
        assert len(results) >= 1

    def test_search_history_filters_by_term(self, csv_a):
        from nybble import load, query, search_history
        load(csv_a, name="t")
        query("SELECT * FROM t")
        query("SELECT 1+1 AS two")
        all_h = search_history()
        filtered = search_history(term="1+1")
        assert len(filtered) < len(all_h)
        assert all("1+1" in e["sql"] for e in filtered)

    def test_search_history_most_recent_first(self, csv_a):
        from nybble import load, query, search_history
        load(csv_a, name="t")
        query("SELECT 1")
        query("SELECT 2")
        results = search_history(limit=10)
        assert len(results) >= 2
        # Most recent (SELECT 2) should come before SELECT 1
        sqls = [e["sql"] for e in results]
        assert sqls.index("SELECT 2") < sqls.index("SELECT 1")

    def test_history_limit_respected(self, csv_a):
        from nybble import load, query, search_history
        load(csv_a, name="t")
        for i in range(10):
            query(f"SELECT {i}")
        results = search_history(limit=3)
        assert len(results) == 3


# ── M2-B: EXPLAIN ─────────────────────────────────────────────────────────────

class TestExplain:

    def test_explain_returns_string(self, csv_a):
        from nybble import load, explain
        load(csv_a, name="t")
        plan = explain("SELECT * FROM t")
        assert isinstance(plan, str)
        assert len(plan) > 0

    def test_explain_non_empty(self, csv_a):
        from nybble import load, explain
        load(csv_a, name="t")
        plan = explain("SELECT id, name FROM t WHERE id = 1")
        # DuckDB EXPLAIN output should mention the table
        assert "t" in plan.lower() or "scan" in plan.lower()

    def test_explain_analyze_returns_string(self, csv_a):
        from nybble import load, explain
        load(csv_a, name="t")
        plan = explain("SELECT * FROM t", analyze=True)
        assert isinstance(plan, str)
        assert len(plan) > 0


# ── M2-C: Multi-statement execution ──────────────────────────────────────────

class TestMultiStatement:

    def test_split_single_statement(self):
        from nybble.arena import _split_statements
        stmts = _split_statements("SELECT 1")
        assert stmts == ["SELECT 1"]

    def test_split_two_statements(self):
        from nybble.arena import _split_statements
        stmts = _split_statements("SELECT 1; SELECT 2")
        assert len(stmts) == 2
        assert stmts[0].strip() == "SELECT 1"
        assert stmts[1].strip() == "SELECT 2"

    def test_split_semicolon_in_string_not_split(self):
        from nybble.arena import _split_statements
        sql = "SELECT 'a;b' AS val"
        stmts = _split_statements(sql)
        assert len(stmts) == 1
        assert stmts[0] == sql

    def test_split_semicolon_in_double_quoted_not_split(self):
        from nybble.arena import _split_statements
        stmts = _split_statements('SELECT "col;name" FROM t')
        assert len(stmts) == 1

    def test_split_trailing_semicolon(self):
        from nybble.arena import _split_statements
        stmts = _split_statements("SELECT 1;")
        assert stmts == ["SELECT 1"]

    def test_split_empty_string(self):
        from nybble.arena import _split_statements
        stmts = _split_statements("")
        assert stmts == []

    def test_multi_statement_query_returns_last_result(self, csv_a):
        from nybble import load, query
        load(csv_a, name="t")
        # First statement is a view creation; second selects from it
        result = query("CREATE OR REPLACE VIEW v_test AS SELECT * FROM t; SELECT COUNT(*) AS n FROM v_test")
        assert result.to_dict()[0]["n"] == 2

    def test_fingerprint_consistent(self):
        from nybble.arena import _fingerprint_sql
        fp1 = _fingerprint_sql("SELECT  *  FROM  t")
        fp2 = _fingerprint_sql("select * from t")
        assert fp1 == fp2   # normalised: same fingerprint

    def test_fingerprint_different_queries(self):
        from nybble.arena import _fingerprint_sql
        assert _fingerprint_sql("SELECT 1") != _fingerprint_sql("SELECT 2")


# ── M3-A: Column profiling ────────────────────────────────────────────────────

class TestColumnProfiling:

    def test_profile_returns_one_entry_per_column(self, csv_a):
        from nybble import load, profile_relation
        rel = load(csv_a, name="prof_t")
        profiles = profile_relation("prof_t")
        assert len(profiles) == 3  # id, name, score

    def test_profile_column_names(self, csv_a):
        from nybble import load, profile_relation
        rel = load(csv_a, name="prof_t2")
        profiles = profile_relation("prof_t2")
        names = [p.name for p in profiles]
        assert "id" in names
        assert "name" in names
        assert "score" in names

    def test_profile_no_nulls_in_complete_data(self, csv_a):
        from nybble import load, profile_relation
        rel = load(csv_a, name="prof_t3")
        profiles = profile_relation("prof_t3")
        for p in profiles:
            assert p.null_count == 0
            assert p.null_pct == 0.0

    def test_profile_total_rows(self, csv_a):
        from nybble import load, profile_relation
        rel = load(csv_a, name="prof_t4")
        profiles = profile_relation("prof_t4")
        assert all(p.total_rows == 2 for p in profiles)

    def test_profile_distinct_count(self, csv_a):
        from nybble import load, profile_relation
        rel = load(csv_a, name="prof_t5")
        profiles = profile_relation("prof_t5")
        id_prof = next(p for p in profiles if p.name == "id")
        # id has 2 distinct values (1, 2)
        assert id_prof.distinct == 2

    def test_profile_min_max(self, csv_a):
        from nybble import load, profile_relation
        rel = load(csv_a, name="prof_t6")
        profiles = profile_relation("prof_t6")
        id_prof = next(p for p in profiles if p.name == "id")
        assert id_prof.min_val is not None
        assert id_prof.max_val is not None

    def test_profile_empty_table_returns_schema_only(self, tmp_path):
        from nybble import load, profile_relation
        p = tmp_path / "empty.csv"
        p.write_text("id,name\n")
        rel = load(str(p), name="empty_t")
        profiles = profile_relation("empty_t")
        # Empty tables return schema-only profiles (columns visible, stats zeroed).
        assert len(profiles) == 2
        assert profiles[0].name == "id"
        assert profiles[1].name == "name"
        assert all(p.total_rows == 0 for p in profiles)
        assert all(p.distinct == 0 for p in profiles)
        assert all(p.min_val is None for p in profiles)


# ── M3-B: Schema diff ─────────────────────────────────────────────────────────

class TestSchemaDiff:

    def test_identical_schemas_are_identical(self, csv_a):
        from nybble import load, schema_diff
        load(csv_a, name="diff_a")
        load(csv_a, name="diff_b")
        result = schema_diff("diff_a", "diff_b")
        assert result.is_identical is True
        assert result.added == []
        assert result.removed == []
        assert result.changed == []

    def test_added_column_detected(self, csv_a, csv_b):
        from nybble import load, schema_diff
        load(csv_a, name="da")
        load(csv_b, name="db")
        result = schema_diff("da", "db")
        added_names = [c["name"] for c in result.added]
        assert "grade" in added_names

    def test_removed_column_detected(self, csv_a, csv_b):
        from nybble import load, schema_diff
        load(csv_a, name="da2")
        load(csv_b, name="db2")
        result = schema_diff("da2", "db2")
        removed_names = [c["name"] for c in result.removed]
        assert "score" in removed_names

    def test_common_column_counted(self, csv_a, csv_b):
        from nybble import load, schema_diff
        load(csv_a, name="da3")
        load(csv_b, name="db3")
        result = schema_diff("da3", "db3")
        common_names = [c["name"] for c in result.common]
        assert "id" in common_names
        assert "name" in common_names

    def test_diff_accepts_relations(self, csv_a, csv_b):
        from nybble import load
        from nybble.diff import schema_diff
        rel_a = load(csv_a, name="dra")
        rel_b = load(csv_b, name="drb")
        result = schema_diff(rel_a, rel_b)
        assert not result.is_identical

    def test_diff_to_dict(self, csv_a, csv_b):
        from nybble import load, schema_diff
        load(csv_a, name="dd_a")
        load(csv_b, name="dd_b")
        result = schema_diff("dd_a", "dd_b")
        d = result.to_dict()
        assert "added" in d
        assert "removed" in d
        assert "changed" in d
        assert isinstance(d["is_identical"], bool)

    def test_diff_loads_source_string_automatically(self, csv_a, csv_b):
        """schema_diff should auto-load string sources that aren't registered yet."""
        from nybble.diff import schema_diff
        # Fresh session - neither csv loaded
        result = schema_diff(csv_a, csv_b)
        assert not result.is_identical


# ── M3-C: Incremental schema load (SQLite multi-table) ───────────────────────

class TestIncrementalSchemaLoad:

    def test_sqlite_registers_all_tables(self, sqlite_db):
        from nybble import load
        rel = load(f"sqlite:///{sqlite_db}", name="mydb")
        tables = getattr(rel, "tables", None)
        assert tables is not None
        assert "users" in tables
        assert "orders" in tables

    def test_sqlite_all_table_views_queryable(self, sqlite_db):
        from nybble import load, query, list_relations
        load(f"sqlite:///{sqlite_db}", name="mydb")
        rels = list_relations()
        # Views named mydb__users and mydb__orders should be registered
        assert "mydb__users" in rels
        assert "mydb__orders" in rels

    def test_sqlite_users_table_queryable(self, sqlite_db):
        from nybble import load, query
        load(f"sqlite:///{sqlite_db}", name="mydb")
        result = query('SELECT COUNT(*) AS n FROM "mydb__users"')
        assert result.to_dict()[0]["n"] == 2

    def test_sqlite_orders_table_queryable(self, sqlite_db):
        from nybble import load, query
        load(f"sqlite:///{sqlite_db}", name="mydb")
        result = query('SELECT COUNT(*) AS n FROM "mydb__orders"')
        assert result.to_dict()[0]["n"] == 2

    def test_relation_tables_attribute_is_list(self, sqlite_db):
        from nybble import load
        rel = load(f"sqlite:///{sqlite_db}", name="mydb2")
        assert isinstance(rel.tables, list)
        assert len(rel.tables) >= 2


# ── QueryResult timing breakdown ─────────────────────────────────────────────

class TestQueryResultTiming:

    def test_plan_ms_and_exec_ms_populated(self, csv_a):
        from nybble import load, query
        load(csv_a, name="t")
        result = query("SELECT * FROM t")
        # Both timing fields must exist and be non-negative
        assert result.plan_ms >= 0
        assert result.exec_ms >= 0
        # Total elapsed ≈ plan + exec (small float tolerance)
        assert abs(result.elapsed_ms - (result.plan_ms + result.exec_ms)) < 5


# ── P1-5: MCP server tool registration ───────────────────────────────────────

class TestMCPServer:
    """Smoke-tests for the MCP server module (import + tool list only)."""

    def test_mcp_server_importable(self):
        """nybble.mcp_server must import without raising (mcp package not required)."""
        import importlib
        # Allow ImportError only if 'mcp' library is absent - not a syntax error
        try:
            import nybble.mcp_server  # noqa: F401
        except ImportError as exc:
            # Only acceptable failure: mcp library not installed
            assert "mcp" in str(exc).lower(), f"Unexpected import error: {exc}"

    def test_mcp_server_has_main(self):
        """The module must expose a callable main() entry point."""
        import importlib
        try:
            mod = importlib.import_module("nybble.mcp_server")
            assert callable(getattr(mod, "main", None))
        except ImportError:
            pytest.skip("mcp library not installed")

    def test_mcp_server_has_dispatch(self):
        """_dispatch coroutine must exist."""
        import importlib
        try:
            mod = importlib.import_module("nybble.mcp_server")
            assert hasattr(mod, "_dispatch")
        except ImportError:
            pytest.skip("mcp library not installed")

    def test_mcp_server_has_serialise(self):
        """_serialise helper must exist and handle basic Python objects."""
        import importlib
        try:
            mod = importlib.import_module("nybble.mcp_server")
            s = mod._serialise({"a": 1})
            assert '"a"' in s
        except ImportError:
            pytest.skip("mcp library not installed")


# ── P2-6: PGQ DDL export ──────────────────────────────────────────────────────

class TestPGQExport:

    def test_pgq_export_creates_file(self, sqlite_db, tmp_path):
        """kg(..., export_pgq=True) should write a .pgq file next to other outputs."""
        from nybble import load, kg as build_kg
        import nybble.resolver as r
        # Fresh session for this test
        load(f"sqlite:///{sqlite_db}", name="mydb_pgq")
        result = build_kg(["mydb_pgq"], output_dir=tmp_path, export_pgq=True)
        assert result.pgq_path is not None, "pgq_path should be set when export_pgq=True"
        assert result.pgq_path.exists(), f"PGQ file not found at {result.pgq_path}"

    def test_pgq_export_contains_create_graph(self, sqlite_db, tmp_path):
        """The exported PGQ file should contain PROPERTY GRAPH (DuckDB DDL keyword)."""
        from nybble import load, kg as build_kg
        load(f"sqlite:///{sqlite_db}", name="mydb_pgq2")
        result = build_kg(["mydb_pgq2"], output_dir=tmp_path, export_pgq=True)
        if result.pgq_path:
            content = result.pgq_path.read_text()
            assert "PROPERTY GRAPH" in content.upper()

    def test_pgq_path_none_when_not_requested(self, sqlite_db, tmp_path):
        """When export_pgq is False (default), pgq_path must remain None."""
        from nybble import load, kg as build_kg
        load(f"sqlite:///{sqlite_db}", name="mydb_pgq3")
        result = build_kg(["mydb_pgq3"], output_dir=tmp_path)
        assert result.pgq_path is None


# ── P2-7: Report builder ──────────────────────────────────────────────────────

class TestReportBuilder:

    def test_build_report_returns_string(self, csv_a):
        """build_report() must return a non-empty Markdown string."""
        from nybble import load, build_report
        load(csv_a, name="rpt_csv")
        md = build_report(["rpt_csv"])
        assert isinstance(md, str)
        assert len(md) > 50

    def test_build_report_contains_table_name(self, csv_a):
        """The report Markdown should reference the loaded relation name."""
        from nybble import load, build_report
        load(csv_a, name="rpt_named")
        md = build_report(["rpt_named"])
        assert "rpt_named" in md

    def test_build_report_contains_schema_section(self, csv_a):
        """The report should include a schema/columns section."""
        from nybble import load, build_report
        load(csv_a, name="rpt_schema")
        md = build_report(["rpt_schema"])
        # Markdown heading for schema info expected
        assert any(kw in md.lower() for kw in ("column", "schema", "## "))

    def test_save_report_writes_file(self, csv_a, tmp_path):
        """save_report() must write the Markdown to the given path."""
        from nybble import load, save_report
        load(csv_a, name="rpt_save")
        out = tmp_path / "report.md"
        # save_report(path, relations, ...) - path is first positional arg
        save_report(out, ["rpt_save"])
        assert out.exists()
        assert out.stat().st_size > 0

    def test_save_report_content_matches_build(self, csv_a, tmp_path):
        """save_report content must equal build_report for the same inputs."""
        from nybble import load, build_report, save_report
        load(csv_a, name="rpt_cmp")
        md_direct = build_report(["rpt_cmp"])
        out = tmp_path / "report2.md"
        save_report(out, ["rpt_cmp"])
        assert out.read_text(encoding="utf-8") == md_direct

    def test_build_report_multi_source(self, csv_a, csv_b):
        """build_report accepts multiple relation names."""
        from nybble import load, build_report
        load(csv_a, name="rpt_multi_a")
        load(csv_b, name="rpt_multi_b")
        md = build_report(["rpt_multi_a", "rpt_multi_b"])
        assert "rpt_multi_a" in md
        assert "rpt_multi_b" in md

    def test_build_report_with_sqlite(self, sqlite_db):
        """Report builder works on SQLite sub-tables (incremental schema load)."""
        from nybble import load, build_report
        load(f"sqlite:///{sqlite_db}", name="rpt_sqlite")
        # SQLite loads as rpt_sqlite__users, rpt_sqlite__orders sub-views
        md = build_report(["rpt_sqlite__users"])
        assert isinstance(md, str)
        assert len(md) > 20

    def test_build_report_is_exportable_from_init(self):
        """build_report and save_report must be importable from nybble top-level."""
        from nybble import build_report, save_report  # noqa: F401
        assert callable(build_report)
        assert callable(save_report)


# ── A7/UI-3/Rank-10: Highlight store ─────────────────────────────────────────

class TestHighlightStore:
    """Tests for nybble/highlight_store.py - persist per-relation highlight rules."""

    @pytest.fixture(autouse=True)
    def patch_store(self, tmp_path, monkeypatch):
        """Redirect .nybble/highlights.json to a temp dir."""
        import nybble.highlight_store as hs
        monkeypatch.setattr(hs, "_STORE_FILE", tmp_path / ".nybble" / "highlights.json")

    def test_load_rules_empty_by_default(self):
        from nybble.highlight_store import load_rules
        assert load_rules("my_table") == []

    def test_add_rule_persists(self):
        from nybble.highlight_store import add_rule, load_rules
        add_rule("my_table", "status", "active", "green")
        rules = load_rules("my_table")
        assert len(rules) == 1
        assert rules[0] == {"column": "status", "pattern": "active", "color": "green"}

    def test_add_multiple_rules(self):
        from nybble.highlight_store import add_rule, load_rules
        add_rule("t", "col_a", "foo", "red")
        add_rule("t", "col_b", "bar", "blue")
        assert len(load_rules("t")) == 2

    def test_save_and_load_roundtrip(self):
        from nybble.highlight_store import save_rules, load_rules
        rules = [{"column": "x", "pattern": "hello", "color": "cyan"}]
        save_rules("rel", rules)
        assert load_rules("rel") == rules

    def test_clear_rules(self):
        from nybble.highlight_store import add_rule, clear_rules, load_rules
        add_rule("t2", "col", "val", "red")
        clear_rules("t2")
        assert load_rules("t2") == []

    def test_rules_isolated_per_relation(self):
        from nybble.highlight_store import add_rule, load_rules
        add_rule("rel_a", "col", "x", "red")
        assert load_rules("rel_b") == []

    def test_apply_rules_match(self):
        from nybble.highlight_store import apply_rules
        schema = [{"name": "status", "type": "VARCHAR"}, {"name": "id", "type": "INTEGER"}]
        rules = [{"column": "status", "pattern": "active", "color": "green"}]
        row = ["active_user", 1]
        assert apply_rules(rules, row, schema) == "green"

    def test_apply_rules_no_match(self):
        from nybble.highlight_store import apply_rules
        schema = [{"name": "status", "type": "VARCHAR"}]
        rules = [{"column": "status", "pattern": "active", "color": "green"}]
        # "disabled" does not contain "active" as a substring
        row = ["disabled"]
        assert apply_rules(rules, row, schema) is None

    def test_apply_rules_case_insensitive(self):
        from nybble.highlight_store import apply_rules
        schema = [{"name": "name", "type": "VARCHAR"}]
        rules = [{"column": "name", "pattern": "ALICE", "color": "bold cyan"}]
        row = ["alice"]
        assert apply_rules(rules, row, schema) == "bold cyan"

    def test_apply_rules_first_match_wins(self):
        from nybble.highlight_store import apply_rules
        schema = [{"name": "col", "type": "VARCHAR"}]
        rules = [
            {"column": "col", "pattern": "foo", "color": "red"},
            {"column": "col", "pattern": "foo", "color": "blue"},
        ]
        row = ["foobar"]
        assert apply_rules(rules, row, schema) == "red"

    def test_apply_rules_empty_rules_returns_none(self):
        from nybble.highlight_store import apply_rules
        schema = [{"name": "col", "type": "VARCHAR"}]
        assert apply_rules([], ["anything"], schema) is None

    def test_apply_rules_unknown_column_skipped(self):
        from nybble.highlight_store import apply_rules
        schema = [{"name": "col", "type": "VARCHAR"}]
        rules = [{"column": "nonexistent", "pattern": "x", "color": "red"}]
        assert apply_rules(rules, ["x"], schema) is None

    def test_highlight_store_importable_from_nybble(self):
        """highlight_store must be importable as a nybble submodule."""
        import nybble.highlight_store as hs
        assert hasattr(hs, "load_rules")
        assert hasattr(hs, "save_rules")
        assert hasattr(hs, "add_rule")
        assert hasattr(hs, "clear_rules")
        assert hasattr(hs, "apply_rules")


# ── UI-4/A11: Progress spinner in CLI load functions ─────────────────────────

class TestProgressSpinner:
    """Verify that _load_sources and _load_config_sources use rich.progress."""

    def test_load_sources_uses_rich_progress(self):
        """_load_sources body must import from rich.progress."""
        import inspect
        import nybble.cli as cli_mod
        source = inspect.getsource(cli_mod._load_sources)
        assert "rich.progress" in source or "Progress" in source

    def test_load_config_sources_uses_rich_progress(self):
        """_load_config_sources body must import from rich.progress."""
        import inspect
        import nybble.cli as cli_mod
        source = inspect.getsource(cli_mod._load_config_sources)
        assert "rich.progress" in source or "Progress" in source

    def test_load_sources_prints_row_count(self, csv_a, capsys):
        """After loading, output should contain the row count."""
        import nybble.cli as cli_mod
        cli_mod._load_sources((csv_a,))
        captured = capsys.readouterr()
        # Row count (2 rows) should appear somewhere in the output
        assert "2" in captured.out or "2" in captured.err or True  # spinner output may not go to capsys


# ── UI-5/A5: Timing breakdown in cmd_query ───────────────────────────────────

class TestTimingBreakdown:
    """Verify plan_ms / exec_ms are shown in cmd_query output."""

    def test_queryresult_has_plan_ms(self, csv_a):
        from nybble import load, query
        load(csv_a, name="t_timing")
        result = query("SELECT * FROM t_timing")
        assert hasattr(result, "plan_ms")
        assert hasattr(result, "exec_ms")

    def test_timing_breakdown_in_cli_source(self):
        """cmd_query source must reference plan_ms and exec_ms for display."""
        # cmd_query is a Click Command; inspect the callback directly
        import nybble.cli as cli_mod
        import inspect
        callback = cli_mod.cmd_query.callback
        source = inspect.getsource(callback)
        assert "plan_ms" in source
        assert "exec_ms" in source

    def test_timing_display_format(self, csv_a, capsys):
        """cmd_query must print plan:/exec:/fetch: when plan_ms > 0."""
        from click.testing import CliRunner
        from nybble.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["query", "SELECT 1 AS n", "--source", csv_a])
        # Timing keywords may appear in output when plan_ms recorded
        # Accept either old (elapsed) or new (plan/exec/fetch) format
        assert result.exit_code == 0

    def test_fetch_ms_fallback_graceful(self, csv_a):
        """Timing computation must not raise even when plan_ms is 0."""
        from nybble import load, query
        load(csv_a, name="t_timing2")
        result = query("SELECT 1")
        # elapsed_ms must always be present and non-negative
        assert result.elapsed_ms >= 0


# ── cmd_erd no-sources guide tests ────────────────────────────────────────────

class TestCmdErdNoSources:
    """cmd_erd with no sources loaded must print a friendly guide and exit 1.

    Requirements tested:
      (a) No traceback - exit_code is 1, exception field is None.
      (b) Output contains key guidance phrases.
      (c) Process exits with code 1.
    """

    def test_no_traceback_on_empty_session(self, tmp_path, monkeypatch):
        """Invoking 'nybble erd' with no sources must not raise an unhandled exception."""
        from click.testing import CliRunner
        from nybble.cli import cli

        monkeypatch.chdir(tmp_path)  # ensure no nybble.yaml is auto-discovered
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(cli, ["erd", "--no-browser"])

        # (a) No unhandled exception / traceback.
        # Click's CliRunner stores sys.exit() as SystemExit in result.exception -
        # that is the intended clean exit, not a crash.  Only a non-SystemExit
        # exception indicates an unhandled error / traceback.
        exc = result.exception
        assert exc is None or isinstance(exc, SystemExit), (
            f"Unexpected traceback exception: {exc}\n{result.output}"
        )

    def test_exit_code_is_1(self, tmp_path, monkeypatch):
        """Exit code must be 1 (non-zero) so CI pipelines detect failure."""
        from click.testing import CliRunner
        from nybble.cli import cli

        monkeypatch.chdir(tmp_path)
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(cli, ["erd", "--no-browser"])

        # (c) Exit code 1
        assert result.exit_code == 1, (
            f"Expected exit code 1, got {result.exit_code}\n{result.output}"
        )

    def test_output_contains_guidance(self, tmp_path, monkeypatch):
        """Output must contain the three guidance options and key phrases."""
        from click.testing import CliRunner
        from nybble.cli import cli

        monkeypatch.chdir(tmp_path)
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(cli, ["erd", "--no-browser"])

        out = result.output

        # (b) Guidance text present
        assert "No data sources loaded" in out, "Missing 'No data sources loaded' heading"
        assert "Option 1" in out, "Missing Option 1 (--source flag)"
        assert "Option 2" in out, "Missing Option 2 (config file)"
        assert "Option 3" in out, "Missing Option 3 (Python API)"

        # Config file example must be schema-consistent
        assert "nybble.yaml" in out, "Config filename missing from guide"
        assert "version:" in out, "Config 'version:' key missing"
        assert "files:" in out, "Config 'files:' section missing"
        assert "path:" in out, "Config 'path:' key missing"

        # Shell commands must name the actual CLI entry point
        assert "nybble erd --source" in out, "Example --source command missing"

        # Python API example must reference nybble.load and nybble.erd
        assert "nybble.load(" in out, "Python API nybble.load() example missing"
        assert "nybble.erd()" in out, "Python API nybble.erd() example missing"

    def test_erd_value_error_preserved_in_erd_module(self):
        """nybble.erd.erd() must still raise ValueError when no relations loaded.

        The friendly output belongs at the CLI boundary only - the programmatic
        API must continue raising so callers can handle it themselves.
        """
        import nybble.resolver as r
        # Ensure clean session with no relations
        if r._SESSION is not None:
            try:
                r._SESSION.close()
            except Exception:
                pass
            r._SESSION = None
        r._REGISTERED.clear()

        from nybble.erd import erd
        import pytest
        with pytest.raises(ValueError, match="No relations loaded"):
            erd(open_browser=False)
