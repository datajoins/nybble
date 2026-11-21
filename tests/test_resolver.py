"""tests/test_resolver.py - Tests for nybble.resolver"""
import json
import os
import tempfile
from pathlib import Path

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_session():
    """Reset the module-level DuckDB session before each test."""
    import nybble.resolver as r
    # Close existing session and clear registrations
    if r._SESSION is not None:
        try:
            r._SESSION.close()
        except Exception:
            pass
        r._SESSION = None
    r._REGISTERED.clear()
    yield
    # Cleanup
    if r._SESSION is not None:
        try:
            r._SESSION.close()
        except Exception:
            pass
        r._SESSION = None
    r._REGISTERED.clear()


@pytest.fixture
def csv_file(tmp_path):
    p = tmp_path / "orders.csv"
    p.write_text("id,name,amount\n1,Alice,100\n2,Bob,200\n3,Carol,300\n")
    return str(p)


@pytest.fixture
def json_file(tmp_path):
    data = [{"id": i, "value": i * 10} for i in range(5)]
    p = tmp_path / "data.json"
    p.write_text(json.dumps(data))
    return str(p)


@pytest.fixture
def parquet_file(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    tbl = pa.table({"x": [1, 2, 3], "y": ["a", "b", "c"]})
    p = tmp_path / "test.parquet"
    pq.write_table(tbl, str(p))
    return str(p)


# ── Source type detection tests ────────────────────────────────────────────────

class TestSourceTypeDetection:

    def test_detect_file_csv(self, csv_file):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type(csv_file) == "file"

    def test_detect_file_parquet(self, parquet_file):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type(parquet_file) == "file"

    def test_detect_json_inline_list(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type([{"a": 1}]) == "json_inline"

    def test_detect_json_inline_string(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type('[{"a": 1}]') == "json_inline"

    def test_detect_url_https(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("https://api.example.com/data") == "url"

    def test_detect_db_sqlite(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("sqlite:///path/to/db.sqlite") == "db"

    def test_detect_db_postgresql(self):
        from nybble.resolver import _detect_source_type
        assert _detect_source_type("postgresql://user:pass@localhost/db") == "db"

    def test_unsupported_type_raises(self):
        from nybble.resolver import _detect_source_type
        with pytest.raises(TypeError):
            _detect_source_type(12345)


# ── Loading tests ──────────────────────────────────────────────────────────────

class TestLoad:

    def test_load_csv_returns_relation(self, csv_file):
        from nybble import load
        rel = load(csv_file, name="orders")
        assert rel.name == "orders"
        assert rel.source_type == "file"
        assert rel.row_count == 3

    def test_load_csv_queryable(self, csv_file):
        from nybble import load, query
        load(csv_file, name="t")
        result = query("SELECT COUNT(*) AS n FROM t")
        assert result.to_dict()[0]["n"] == 3

    def test_load_json_inline_list(self):
        from nybble import load
        data = [{"id": i, "val": i * 2} for i in range(10)]
        rel = load(data, name="inline_test")
        assert rel.row_count == 10
        assert rel.source_type == "json_inline"

    def test_load_json_string(self):
        from nybble import load
        data_str = '[{"city": "London", "pop": 9000000}, {"city": "Paris", "pop": 2100000}]'
        rel = load(data_str, name="cities")
        df = rel.to_polars()
        assert "city" in df.columns
        assert len(df) == 2

    def test_load_parquet(self, parquet_file):
        from nybble import load
        rel = load(parquet_file, name="pq_test")
        assert rel.row_count == 3
        df = rel.to_polars()
        assert set(df.columns) == {"x", "y"}

    def test_load_auto_name(self, csv_file):
        from nybble import load
        rel = load(csv_file)
        assert rel.name  # auto-derived from filename stem
        assert len(rel.name) > 0

    def test_load_registers_in_session(self, csv_file):
        from nybble import list_relations, load
        load(csv_file, name="reg_test")
        assert "reg_test" in list_relations()

    def test_load_same_file_twice_deduplicates_name(self, csv_file):
        """Loading the same file with different names registers both."""
        from nybble import load
        rel1 = load(csv_file, name="first")
        rel2 = load(csv_file, name="second")
        assert rel1.name == "first"
        assert rel2.name == "second"

    def test_drop_removes_registration(self, csv_file):
        from nybble import drop, list_relations, load
        load(csv_file, name="to_drop")
        drop("to_drop")
        assert "to_drop" not in list_relations()

    def test_relation_schema_returns_columns(self, csv_file):
        from nybble import load
        rel = load(csv_file, name="schema_test")
        schema = rel.schema()
        names = [c["name"] for c in schema]
        assert "id" in names
        assert "name" in names
        assert "amount" in names

    def test_relation_sample(self, csv_file):
        from nybble import load
        rel = load(csv_file, name="samp")
        sample = rel.sample(2)
        assert len(sample) == 2

    def test_json_inline_single_dict(self):
        from nybble import load
        rel = load({"key": "value", "num": 42}, name="single")
        assert rel.row_count == 1

    def test_file_not_found_raises(self):
        from nybble.resolver import load
        with pytest.raises(FileNotFoundError):
            load("/nonexistent/path/file.csv", name="nope")


# ── browse multi-table picker tests ───────────────────────────────────────────

class TestBrowseMultiTablePicker:
    """Regression tests for the browse() crash on multi-table sources.

    When a SQL dump or SQLite DB is loaded, the resolver creates per-table views
    named ``<source>__<table>`` and returns a parent Relation that has NO
    corresponding DuckDB view.  browse() must detect this parent and show a
    picker instead of calling .schema() on it.
    """

    def _make_sqlite_db(self, tmp_path) -> str:
        """Create a minimal SQLite DB with two tables."""
        import sqlite3
        db = tmp_path / "multi.db"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)")
        con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total REAL)")
        con.execute("INSERT INTO customers VALUES (1,'Alice'),(2,'Bob')")
        con.execute("INSERT INTO orders VALUES (10,1,99.9),(20,2,49.5)")
        con.commit()
        con.close()
        return str(db)

    def test_multitable_parent_has_tables_attr(self, tmp_path):
        """Loading a SQLite DB returns a parent Relation with a .tables list."""
        from nybble.resolver import load
        db_path = self._make_sqlite_db(tmp_path)
        rel = load(f"sqlite:///{db_path}", name="testdb")
        assert hasattr(rel, "tables"), "Parent Relation must have .tables attribute"
        assert set(rel.tables) == {"customers", "orders"}

    def test_multitable_child_views_registered(self, tmp_path):
        """Child views testdb__customers and testdb__orders must be in _REGISTERED."""
        from nybble.resolver import load, _REGISTERED
        db_path = self._make_sqlite_db(tmp_path)
        load(f"sqlite:///{db_path}", name="testdb")
        assert "testdb__customers" in _REGISTERED
        assert "testdb__orders" in _REGISTERED

    def test_pick_table_returns_child_relation(self, tmp_path):
        """_pick_table() with user input '1' must return the first child Relation."""
        from nybble.resolver import load
        from nybble.browse import _pick_table
        import io
        db_path = self._make_sqlite_db(tmp_path)
        parent = load(f"sqlite:///{db_path}", name="testdb2")
        assert hasattr(parent, "tables")
        # Simulate user typing "1\n"
        import builtins
        original_input = builtins.input
        builtins.input = lambda *a, **kw: "1"
        try:
            child = _pick_table(parent)
        finally:
            builtins.input = original_input
        # The child must be a real queryable relation
        assert child.schema(), "Child relation must return non-empty schema"

    def test_browse_multitable_does_not_crash(self, tmp_path, monkeypatch):
        """browse() on a multi-table source must not raise CatalogException.

        Flow:
          1. browse() detects multi-table parent, enters outer picker loop.
          2. _pick_table (patched) returns first child on call 1.
          3. _browse_one runs; input() returns "q" → returns _BACK.
          4. Outer loop iterates; _pick_table called again (call 2) → SystemExit(0)
             which browse() lets propagate → test catches it as a clean exit.
        """
        import sys
        import importlib
        from nybble.resolver import load, _REGISTERED

        browse_mod = importlib.import_module("nybble.browse")

        db_path = self._make_sqlite_db(tmp_path)
        parent = load(f"sqlite:///{db_path}", name="testdb3")

        first_child = _REGISTERED.get("testdb3__customers") or _REGISTERED.get("testdb3__orders")
        assert first_child is not None

        # _pick_table: return child on first call, simulate user pressing 'q'
        # on second call (which raises SystemExit(0) per the real implementation).
        pick_calls = {"n": 0}
        def _fake_pick(rel):
            pick_calls["n"] += 1
            if pick_calls["n"] == 1:
                return first_child
            raise SystemExit(0)
        monkeypatch.setattr(browse_mod, "_pick_table", _fake_pick)

        monkeypatch.setattr(sys, "platform", "win32")  # non-raw input path
        # "q" in _browse_one (multi_table=True) → returns _BACK → outer loop repeats
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "q")

        # SystemExit(0) is the expected clean-exit signal from the picker
        with pytest.raises(SystemExit) as exc_info:
            browse_mod.browse(parent, page_size=5)
        assert exc_info.value.code == 0


# ── SQLite bare-path extension tests ─────────────────────────────────────────

class TestSqliteBareExtension:
    """load('path/to/file.sqlite') and load('path/to/file.db') must work."""

    @staticmethod
    def _make_sqlite(tmp_path, filename="mydb.sqlite"):
        import sqlite3
        p = tmp_path / filename
        conn = sqlite3.connect(str(p))
        conn.execute("CREATE TABLE items (item_id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany("INSERT INTO items VALUES (?,?)", [(1, "alpha"), (2, "beta")])
        conn.commit()
        conn.close()
        return p

    def test_dot_sqlite_extension_loads(self, tmp_path):
        """load('mydb.sqlite') must succeed and register child table views."""
        from nybble.resolver import load, _REGISTERED
        db_file = self._make_sqlite(tmp_path, "mydb.sqlite")
        rel = load(str(db_file))
        assert rel.source_type == "db"
        assert hasattr(rel, "tables") and "items" in rel.tables
        assert f"{rel.name}__items" in _REGISTERED

    def test_dot_db_extension_loads(self, tmp_path):
        """load('mydb.db') must succeed and register child table views."""
        from nybble.resolver import load, _REGISTERED
        db_file = self._make_sqlite(tmp_path, "mydb.db")
        rel = load(str(db_file))
        assert rel.source_type == "db"
        assert hasattr(rel, "tables") and "items" in rel.tables

    def test_dot_sqlite3_extension_loads(self, tmp_path):
        """load('mydb.sqlite3') must succeed."""
        from nybble.resolver import load, _REGISTERED
        db_file = self._make_sqlite(tmp_path, "mydb.sqlite3")
        rel = load(str(db_file))
        assert rel.source_type == "db"
        assert hasattr(rel, "tables")

    def test_relative_sqlite_uri_resolves(self, tmp_path, monkeypatch):
        """sqlite:///relative/path must resolve correctly (not break on Windows)."""
        import os
        from nybble.resolver import load, _REGISTERED
        # Create DB in tmp_path; chdir there so the relative path works
        db_file = self._make_sqlite(tmp_path, "shop.sqlite")
        monkeypatch.chdir(tmp_path)
        rel = load("sqlite:///shop.sqlite")
        assert rel.source_type == "db"
        assert hasattr(rel, "tables") and "items" in rel.tables


# ── Cross-platform path and extension tests ───────────────────────────────────

class TestCrossPlatformPaths:
    """Ensure resolver handles Windows-style paths and edge cases on all OSes."""

    def test_windows_drive_letter_path_detected_as_file(self):
        """C:\\path\\file.csv must be classified as 'file', not 'db'."""
        from nybble.resolver import _detect_source_type
        # On non-Windows this path won't exist, but detection must not mis-classify it.
        result = _detect_source_type("C:\\data\\orders.csv")
        assert result == "file"

    def test_windows_drive_letter_duckdb_path_detected_as_file(self):
        """C:\\path\\file.duckdb must be classified as 'file'."""
        from nybble.resolver import _detect_source_type
        result = _detect_source_type("C:\\data\\analytics.duckdb")
        assert result == "file"

    def test_duckdb_file_extension_in_extensions_map(self):
        """.duckdb must appear in _FILE_EXTENSIONS."""
        from nybble.resolver import _FILE_EXTENSIONS
        assert ".duckdb" in _FILE_EXTENSIONS
        assert _FILE_EXTENSIONS[".duckdb"] == "duckdb_file"

    def test_dot_duckdb_bare_path_loads(self, tmp_path):
        """load('mydb.duckdb') via bare .duckdb extension must succeed."""
        import duckdb
        db_path = tmp_path / "mydb.duckdb"
        # Create a DuckDB file with one table
        con = duckdb.connect(str(db_path))
        con.execute("CREATE TABLE products (id INTEGER, name VARCHAR)")
        con.execute("INSERT INTO products VALUES (1, 'Widget'), (2, 'Gadget')")
        con.close()

        from nybble.resolver import load, _REGISTERED
        rel = load(str(db_path))
        assert rel.source_type == "db"
        assert hasattr(rel, "tables") and "products" in rel.tables
        assert f"{rel.name}__products" in _REGISTERED

    def test_relative_duckdb_uri_resolves(self, tmp_path, monkeypatch):
        """duckdb:///relative/file.duckdb must resolve on Windows (leading-slash fix)."""
        import duckdb
        db_path = tmp_path / "rel.duckdb"
        con = duckdb.connect(str(db_path))
        con.execute("CREATE TABLE sales (id INTEGER, amount REAL)")
        con.execute("INSERT INTO sales VALUES (1, 9.99)")
        con.close()

        monkeypatch.chdir(tmp_path)
        from nybble.resolver import load
        rel = load("duckdb:///rel.duckdb")
        assert rel.source_type == "db"
        assert hasattr(rel, "tables") and "sales" in rel.tables

    def test_single_letter_scheme_not_treated_as_db(self):
        """Single-letter scheme (Windows drive) must never match _DB_SCHEMES."""
        from nybble.resolver import _DB_SCHEMES
        # No single-letter schemes should be in the DB scheme set
        single_letter = {s for s in _DB_SCHEMES if len(s) == 1}
        assert single_letter == set(), f"Single-letter schemes in _DB_SCHEMES: {single_letter}"

    def test_explicit_utf8_on_all_file_writes(self, tmp_path):
        """Verify that arena history and saved queries use UTF-8 encoding."""
        # We do this indirectly: write a query with non-ASCII text, reload it.
        import nybble.arena as arena
        import json

        orig_queries = arena._QUERIES_FILE
        orig_history = arena._HISTORY_FILE
        # Redirect to tmp_path
        arena._QUERIES_FILE = tmp_path / "queries.json"
        arena._HISTORY_FILE = tmp_path / "history.jsonl"
        try:
            arena.save_query("café_query", "SELECT '☕' AS coffee")
            loaded = arena._load_saved()
            assert loaded.get("café_query") == "SELECT '☕' AS coffee"
        finally:
            arena._QUERIES_FILE = orig_queries
            arena._HISTORY_FILE = orig_history
