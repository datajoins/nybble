"""tests/test_arena.py - Tests for nybble.arena (SQL Arena)"""
import json
from pathlib import Path

import polars as pl
import pytest


@pytest.fixture(autouse=True)
def fresh_session():
    import nybble.resolver as r
    if r._SESSION is not None:
        try:
            r._SESSION.close()
        except Exception:
            pass
        r._SESSION = None
    r._REGISTERED.clear()
    yield
    if r._SESSION is not None:
        try:
            r._SESSION.close()
        except Exception:
            pass
        r._SESSION = None
    r._REGISTERED.clear()


@pytest.fixture
def two_tables():
    """Load orders and customers into the session."""
    from nybble import load
    customers = [
        {"customer_id": 1, "name": "Alice"},
        {"customer_id": 2, "name": "Bob"},
    ]
    orders = [
        {"order_id": 101, "customer_id": 1, "amount": 99.5},
        {"order_id": 102, "customer_id": 2, "amount": 149.0},
        {"order_id": 103, "customer_id": 1, "amount": 49.95},
    ]
    load(customers, name="customers")
    load(orders, name="orders")


@pytest.fixture
def saved_query_file(tmp_path, monkeypatch):
    """Redirect .nybble/queries.json to a temp directory."""
    import nybble.arena as arena
    queries_path = tmp_path / ".nybble" / "queries.json"
    monkeypatch.setattr(arena, "_QUERIES_FILE", queries_path)
    return queries_path


# ── Query execution tests ──────────────────────────────────────────────────────

class TestQuery:

    def test_basic_select(self, two_tables):
        from nybble import query
        result = query("SELECT * FROM orders")
        assert result.row_count == 3
        assert "order_id" in result.columns

    def test_aggregation(self, two_tables):
        from nybble import query
        result = query("SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id ORDER BY customer_id")
        assert result.row_count == 2
        rows = result.to_dict()
        assert rows[0]["n"] == 2  # Alice has 2 orders

    def test_cross_source_join(self, two_tables):
        from nybble import query
        result = query(
            "SELECT o.order_id, c.name, o.amount "
            "FROM orders o JOIN customers c ON o.customer_id = c.customer_id"
        )
        assert result.row_count == 3
        assert "name" in result.columns
        names = [r["name"] for r in result.to_dict()]
        assert "Alice" in names

    def test_with_cte(self, two_tables):
        from nybble import query
        result = query(
            "WITH high_value AS (SELECT * FROM orders WHERE amount > 100) "
            "SELECT COUNT(*) AS cnt FROM high_value"
        )
        assert result.to_dict()[0]["cnt"] == 1

    def test_limit_applied(self, two_tables):
        from nybble import query
        result = query("SELECT * FROM orders", limit=2)
        assert result.row_count == 2
        assert result.truncated is True

    def test_limit_no_truncation_when_within(self, two_tables):
        from nybble import query
        result = query("SELECT * FROM orders", limit=10)
        assert result.row_count == 3
        assert result.truncated is False

    def test_non_select_raises(self):
        from nybble import query
        # DuckDB will raise for DROP on an unmaterialized view
        with pytest.raises(Exception):
            query("DROP TABLE nonexistent_table_xyz")

    def test_elapsed_ms_populated(self, two_tables):
        from nybble import query
        result = query("SELECT 1+1 AS two")
        assert result.elapsed_ms >= 0

    def test_to_polars(self, two_tables):
        from nybble import query
        result = query("SELECT * FROM customers")
        df = result.to_polars()
        assert isinstance(df, pl.DataFrame)
        assert len(df) == 2

    def test_to_arrow(self, two_tables):
        from nybble import query
        import pyarrow as pa
        result = query("SELECT * FROM customers")
        arrow = result.to_arrow()
        assert isinstance(arrow, pa.Table)

    def test_to_dict(self, two_tables):
        from nybble import query
        result = query("SELECT * FROM customers ORDER BY customer_id")
        d = result.to_dict()
        assert isinstance(d, list)
        assert d[0]["name"] == "Alice"

    def test_rows(self, two_tables):
        from nybble import query
        result = query("SELECT amount FROM orders ORDER BY amount")
        rows = result.rows()
        assert rows[0][0] < rows[-1][0]


# ── Saved queries tests ────────────────────────────────────────────────────────

class TestSavedQueries:

    def test_save_and_run(self, two_tables, saved_query_file):
        from nybble import save_query, run_query
        save_query("my_query", "SELECT COUNT(*) AS n FROM orders")
        result = run_query("my_query")
        assert result.to_dict()[0]["n"] == 3

    def test_list_saved_queries(self, saved_query_file):
        from nybble import save_query, list_saved_queries
        save_query("q1", "SELECT 1")
        save_query("q2", "SELECT 2")
        listing = list_saved_queries()
        names = [q["name"] for q in listing]
        assert "q1" in names
        assert "q2" in names

    def test_delete_saved_query(self, saved_query_file):
        from nybble import save_query, delete_saved_query, list_saved_queries
        save_query("del_me", "SELECT 1")
        delete_saved_query("del_me")
        listing = list_saved_queries()
        assert "del_me" not in [q["name"] for q in listing]

    def test_run_nonexistent_query_raises(self, saved_query_file):
        from nybble import run_query
        with pytest.raises(KeyError):
            run_query("does_not_exist")

    def test_delete_nonexistent_raises(self, saved_query_file):
        from nybble import delete_saved_query
        with pytest.raises(KeyError):
            delete_saved_query("ghost_query")

    def test_saved_query_persists_to_file(self, saved_query_file):
        from nybble import save_query
        save_query("persist_test", "SELECT 42")
        assert saved_query_file.exists()
        content = json.loads(saved_query_file.read_text())
        assert "persist_test" in content
        assert content["persist_test"] == "SELECT 42"

    def test_save_overwrites_existing(self, two_tables, saved_query_file):
        from nybble import save_query, run_query
        save_query("overwrite_me", "SELECT COUNT(*) AS n FROM orders")
        save_query("overwrite_me", "SELECT COUNT(*) AS n FROM customers")
        result = run_query("overwrite_me")
        # After overwrite, should count customers (2), not orders (3)
        assert result.to_dict()[0]["n"] == 2
