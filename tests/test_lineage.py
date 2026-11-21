"""
tests/test_lineage.py - P3-3: SQL Query Lineage DAG tests

All tests are hermetic: use tmp_path fixtures, in-memory query lists, or
monkeypatching. No reliance on real .nybble/ directories or live DB connections.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _write_queries(tmp_path: Path, queries: list[dict]) -> Path:
    """Write a queries JSON file and return its path."""
    p = tmp_path / "queries.json"
    p.write_text(json.dumps(queries), encoding="utf-8")
    return p


# ─────────────────────────────────────────────────────────────────────────────
# _load_queries
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadQueries:

    def test_missing_file_returns_empty(self, tmp_path):
        from nybble.lineage import _load_queries
        result = _load_queries(tmp_path / "nonexistent.json")
        assert result == []

    def test_empty_array_file(self, tmp_path):
        p = _write_queries(tmp_path, [])
        from nybble.lineage import _load_queries
        assert _load_queries(p) == []

    def test_loads_queries(self, tmp_path):
        queries = [{"name": "q1", "sql": "SELECT * FROM orders"}]
        p = _write_queries(tmp_path, queries)
        from nybble.lineage import _load_queries
        result = _load_queries(p)
        assert len(result) == 1
        assert result[0]["name"] == "q1"

    def test_invalid_json_returns_empty(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("NOT JSON", encoding="utf-8")
        from nybble.lineage import _load_queries
        assert _load_queries(p) == []

    def test_non_array_json_returns_empty(self, tmp_path):
        p = tmp_path / "obj.json"
        p.write_text('{"name": "q1", "sql": "SELECT 1"}', encoding="utf-8")
        from nybble.lineage import _load_queries
        assert _load_queries(p) == []


# ─────────────────────────────────────────────────────────────────────────────
# _extract_tables
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractTables:

    def test_simple_select(self):
        from nybble.lineage import _extract_tables
        tables = _extract_tables("SELECT * FROM orders")
        assert "orders" in tables

    def test_join_extracts_both_tables(self):
        from nybble.lineage import _extract_tables
        tables = _extract_tables(
            "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id"
        )
        assert "orders" in tables
        assert "customers" in tables

    def test_empty_sql_returns_empty(self):
        from nybble.lineage import _extract_tables
        assert _extract_tables("") == []

    def test_invalid_sql_returns_empty(self):
        from nybble.lineage import _extract_tables
        # Should not raise - returns empty on parse error
        result = _extract_tables("SELECT FROM WHERE INVALID !!!!")
        assert isinstance(result, list)

    def test_deduplication(self):
        from nybble.lineage import _extract_tables
        tables = _extract_tables(
            "SELECT * FROM t UNION ALL SELECT * FROM t"
        )
        assert tables.count("t") == 1

    def test_schema_qualified_name(self):
        from nybble.lineage import _extract_tables
        tables = _extract_tables("SELECT * FROM myschema.orders")
        # The fully qualified name should be returned
        assert any("orders" in t for t in tables)

    def test_subquery_not_included_as_table(self):
        from nybble.lineage import _extract_tables
        tables = _extract_tables(
            "SELECT * FROM (SELECT id FROM customers) sub"
        )
        # 'sub' is an alias, not a real table - customers should be there
        assert "customers" in tables
        assert "sub" not in tables

    def test_dialect_parameter_accepted(self):
        from nybble.lineage import _extract_tables
        # Just ensure it doesn't crash with a valid dialect
        tables = _extract_tables("SELECT * FROM orders", dialect="bigquery")
        assert isinstance(tables, list)

    def test_cte_source_tables_extracted(self):
        from nybble.lineage import _extract_tables
        sql = """
        WITH recent AS (
            SELECT * FROM orders WHERE amount > 100
        )
        SELECT * FROM recent JOIN customers ON recent.cid = customers.id
        """
        tables = _extract_tables(sql)
        assert "orders" in tables
        assert "customers" in tables


# ─────────────────────────────────────────────────────────────────────────────
# build_lineage_dag
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildLineageDag:

    def test_empty_queries_list_returns_empty_dag(self, tmp_path):
        from nybble.lineage import build_lineage_dag
        p = _write_queries(tmp_path, [])
        dag = build_lineage_dag(queries_path=p)
        assert dag == {"nodes": [], "edges": []}

    def test_missing_file_returns_empty_dag_without_raising(self, tmp_path):
        from nybble.lineage import build_lineage_dag
        dag = build_lineage_dag(queries_path=tmp_path / "missing.json")
        assert dag == {"nodes": [], "edges": []}

    def test_single_query_two_tables(self, tmp_path):
        """1 query + 2 tables → 3 nodes, 2 edges."""
        queries = [{"name": "join_q", "sql": "SELECT * FROM orders JOIN customers ON orders.cid = customers.id"}]
        p = _write_queries(tmp_path, queries)
        from nybble.lineage import build_lineage_dag
        dag = build_lineage_dag(queries_path=p)

        assert len(dag["nodes"]) == 3
        assert len(dag["edges"]) == 2

    def test_node_types_correct(self, tmp_path):
        queries = [{"name": "q1", "sql": "SELECT * FROM orders"}]
        p = _write_queries(tmp_path, queries)
        from nybble.lineage import build_lineage_dag
        dag = build_lineage_dag(queries_path=p)

        types = {n["type"] for n in dag["nodes"]}
        assert "query" in types
        assert "table" in types

    def test_query_node_id_format(self, tmp_path):
        queries = [{"name": "my_query", "sql": "SELECT * FROM t"}]
        p = _write_queries(tmp_path, queries)
        from nybble.lineage import build_lineage_dag
        dag = build_lineage_dag(queries_path=p)

        query_nodes = [n for n in dag["nodes"] if n["type"] == "query"]
        assert len(query_nodes) == 1
        assert query_nodes[0]["id"] == "query:my_query"
        assert query_nodes[0]["label"] == "my_query"

    def test_table_node_id_format(self, tmp_path):
        queries = [{"name": "q", "sql": "SELECT * FROM orders"}]
        p = _write_queries(tmp_path, queries)
        from nybble.lineage import build_lineage_dag
        dag = build_lineage_dag(queries_path=p)

        table_nodes = [n for n in dag["nodes"] if n["type"] == "table"]
        assert len(table_nodes) == 1
        assert table_nodes[0]["id"] == "table:orders"

    def test_shared_table_appears_once(self, tmp_path):
        """Two queries sharing a common table → table node deduplicated."""
        queries = [
            {"name": "q1", "sql": "SELECT * FROM orders"},
            {"name": "q2", "sql": "SELECT * FROM orders JOIN customers ON orders.cid = customers.id"},
        ]
        p = _write_queries(tmp_path, queries)
        from nybble.lineage import build_lineage_dag
        dag = build_lineage_dag(queries_path=p)

        # Count table nodes for 'orders'
        order_nodes = [n for n in dag["nodes"] if n["id"] == "table:orders"]
        assert len(order_nodes) == 1

    def test_two_queries_shared_table_edge_count(self, tmp_path):
        """q1→orders(1 edge), q2→orders+customers(2 edges) = 3 total edges."""
        queries = [
            {"name": "q1", "sql": "SELECT * FROM orders"},
            {"name": "q2", "sql": "SELECT * FROM orders JOIN customers ON orders.cid = customers.id"},
        ]
        p = _write_queries(tmp_path, queries)
        from nybble.lineage import build_lineage_dag
        dag = build_lineage_dag(queries_path=p)

        # Edges: orders→q1, orders→q2, customers→q2 = 3
        assert len(dag["edges"]) == 3

    def test_two_queries_node_count(self, tmp_path):
        """2 queries + 2 unique tables (orders shared) = 4 nodes."""
        queries = [
            {"name": "q1", "sql": "SELECT * FROM orders"},
            {"name": "q2", "sql": "SELECT * FROM orders JOIN customers ON 1=1"},
        ]
        p = _write_queries(tmp_path, queries)
        from nybble.lineage import build_lineage_dag
        dag = build_lineage_dag(queries_path=p)

        # nodes: query:q1, query:q2, table:orders, table:customers = 4
        assert len(dag["nodes"]) == 4

    def test_edges_are_source_table_to_query(self, tmp_path):
        queries = [{"name": "q1", "sql": "SELECT * FROM orders"}]
        p = _write_queries(tmp_path, queries)
        from nybble.lineage import build_lineage_dag
        dag = build_lineage_dag(queries_path=p)

        edges = dag["edges"]
        assert len(edges) == 1
        assert edges[0]["source"] == "table:orders"
        assert edges[0]["target"] == "query:q1"

    def test_queries_without_name_skipped(self, tmp_path):
        queries = [
            {"sql": "SELECT * FROM orders"},  # no name
            {"name": "q_valid", "sql": "SELECT * FROM customers"},
        ]
        p = _write_queries(tmp_path, queries)
        from nybble.lineage import build_lineage_dag
        dag = build_lineage_dag(queries_path=p)

        query_nodes = [n for n in dag["nodes"] if n["type"] == "query"]
        assert len(query_nodes) == 1
        assert query_nodes[0]["label"] == "q_valid"

    def test_fully_qualified_table_name(self, tmp_path):
        queries = [{"name": "q", "sql": "SELECT * FROM myschema.orders"}]
        p = _write_queries(tmp_path, queries)
        from nybble.lineage import build_lineage_dag
        dag = build_lineage_dag(queries_path=p)

        table_nodes = [n for n in dag["nodes"] if n["type"] == "table"]
        # The qualified name should include 'orders'
        assert any("orders" in n["id"] for n in table_nodes)

    def test_dialect_parameter_passed_through(self, tmp_path):
        queries = [{"name": "bq_q", "sql": "SELECT * FROM `project.dataset.table`"}]
        p = _write_queries(tmp_path, queries)
        from nybble.lineage import build_lineage_dag
        # Should not raise with bigquery dialect
        dag = build_lineage_dag(queries_path=p, dialect="bigquery")
        assert "nodes" in dag
        assert "edges" in dag


# ─────────────────────────────────────────────────────────────────────────────
# dag_to_html
# ─────────────────────────────────────────────────────────────────────────────

class TestDagToHtml:

    def _make_dag(self, tmp_path: Path) -> dict:
        queries = [{"name": "my_query", "sql": "SELECT * FROM orders JOIN customers ON 1=1"}]
        p = _write_queries(tmp_path, queries)
        from nybble.lineage import build_lineage_dag
        return build_lineage_dag(queries_path=p)

    def test_returns_string(self, tmp_path):
        from nybble.lineage import dag_to_html
        dag = self._make_dag(tmp_path)
        html = dag_to_html(dag)
        assert isinstance(html, str)

    def test_non_empty_output(self, tmp_path):
        from nybble.lineage import dag_to_html
        dag = self._make_dag(tmp_path)
        html = dag_to_html(dag)
        assert len(html) > 200

    def test_contains_html_tag(self, tmp_path):
        from nybble.lineage import dag_to_html
        dag = self._make_dag(tmp_path)
        html = dag_to_html(dag)
        assert "<html" in html

    def test_contains_body_tag(self, tmp_path):
        from nybble.lineage import dag_to_html
        dag = self._make_dag(tmp_path)
        html = dag_to_html(dag)
        assert "<body>" in html

    def test_contains_query_name(self, tmp_path):
        from nybble.lineage import dag_to_html
        dag = self._make_dag(tmp_path)
        html = dag_to_html(dag)
        assert "my_query" in html

    def test_contains_table_name(self, tmp_path):
        from nybble.lineage import dag_to_html
        dag = self._make_dag(tmp_path)
        html = dag_to_html(dag)
        assert "orders" in html or "customers" in html

    def test_custom_title_in_output(self, tmp_path):
        from nybble.lineage import dag_to_html
        dag = self._make_dag(tmp_path)
        html = dag_to_html(dag, title="My Custom Lineage")
        assert "My Custom Lineage" in html

    def test_empty_dag_returns_valid_html(self):
        from nybble.lineage import dag_to_html
        html = dag_to_html({"nodes": [], "edges": []})
        assert "<html" in html
        assert isinstance(html, str)

    def test_empty_dag_does_not_raise(self):
        from nybble.lineage import dag_to_html
        # Must not raise
        html = dag_to_html({"nodes": [], "edges": []})
        assert len(html) > 50

    def test_has_style_block(self, tmp_path):
        from nybble.lineage import dag_to_html
        html = dag_to_html({"nodes": [], "edges": []})
        assert "<style>" in html

    def test_nodes_json_embedded(self, tmp_path):
        from nybble.lineage import dag_to_html
        dag = self._make_dag(tmp_path)
        html = dag_to_html(dag)
        # The nodes JSON should be embedded in the script
        assert "DAG_NODES" in html

    def test_edges_json_embedded(self, tmp_path):
        from nybble.lineage import dag_to_html
        dag = self._make_dag(tmp_path)
        html = dag_to_html(dag)
        assert "DAG_EDGES" in html

    def test_table_and_query_colors_distinct(self):
        from nybble.lineage import dag_to_html
        html = dag_to_html({"nodes": [], "edges": []})
        # Both color constants should appear in the HTML
        assert "TABLE_COLOR" in html
        assert "QUERY_COLOR" in html


# ─────────────────────────────────────────────────────────────────────────────
# save_lineage_html
# ─────────────────────────────────────────────────────────────────────────────

class TestSaveLineageHtml:

    def test_writes_file(self, tmp_path):
        queries = [{"name": "q", "sql": "SELECT * FROM t"}]
        qp = _write_queries(tmp_path, queries)
        out = tmp_path / "lineage.html"
        from nybble.lineage import save_lineage_html
        result = save_lineage_html(output_path=out, queries_path=qp)
        assert out.exists()
        assert result == out.resolve()

    def test_returns_path(self, tmp_path):
        queries = [{"name": "q", "sql": "SELECT * FROM t"}]
        qp = _write_queries(tmp_path, queries)
        out = tmp_path / "out.html"
        from nybble.lineage import save_lineage_html
        result = save_lineage_html(output_path=out, queries_path=qp)
        assert isinstance(result, Path)

    def test_creates_parent_directories(self, tmp_path):
        queries = [{"name": "q", "sql": "SELECT * FROM t"}]
        qp = _write_queries(tmp_path, queries)
        out = tmp_path / "a" / "b" / "lineage.html"
        from nybble.lineage import save_lineage_html
        save_lineage_html(output_path=out, queries_path=qp)
        assert out.exists()

    def test_output_contains_html(self, tmp_path):
        queries = [{"name": "q", "sql": "SELECT * FROM t"}]
        qp = _write_queries(tmp_path, queries)
        out = tmp_path / "lineage.html"
        from nybble.lineage import save_lineage_html
        save_lineage_html(output_path=out, queries_path=qp)
        content = out.read_text(encoding="utf-8")
        assert "<html" in content

    def test_missing_queries_file_writes_empty_dag(self, tmp_path):
        """Missing queries path → empty DAG HTML written without raising."""
        out = tmp_path / "lineage.html"
        from nybble.lineage import save_lineage_html
        save_lineage_html(output_path=out, queries_path=tmp_path / "missing.json")
        assert out.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Init exports
# ─────────────────────────────────────────────────────────────────────────────

class TestInitExports:

    def test_build_lineage_dag_exported(self):
        from nybble import build_lineage_dag
        assert callable(build_lineage_dag)

    def test_dag_to_html_exported(self):
        from nybble import dag_to_html
        assert callable(dag_to_html)

    def test_save_lineage_html_exported(self):
        from nybble import save_lineage_html
        assert callable(save_lineage_html)


# ─────────────────────────────────────────────────────────────────────────────
# CLI subcommand
# ─────────────────────────────────────────────────────────────────────────────

class TestCLILineageCommand:

    def test_cli_exits_0_with_valid_queries(self, tmp_path):
        queries = [{"name": "q1", "sql": "SELECT * FROM orders"}]
        qp = _write_queries(tmp_path, queries)
        out = tmp_path / "lineage.html"
        from click.testing import CliRunner
        from nybble.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, [
            "lineage",
            "--queries", str(qp),
            "--output", str(out),
        ])
        assert result.exit_code == 0, f"exit={result.exit_code}\n{result.output}"

    def test_cli_writes_output_file(self, tmp_path):
        queries = [{"name": "q1", "sql": "SELECT * FROM orders"}]
        qp = _write_queries(tmp_path, queries)
        out = tmp_path / "lineage.html"
        from click.testing import CliRunner
        from nybble.cli import cli

        runner = CliRunner()
        runner.invoke(cli, [
            "lineage",
            "--queries", str(qp),
            "--output", str(out),
        ])
        assert out.exists()

    def test_cli_prints_output_path(self, tmp_path):
        queries = [{"name": "q1", "sql": "SELECT * FROM orders"}]
        qp = _write_queries(tmp_path, queries)
        out = tmp_path / "lineage.html"
        from click.testing import CliRunner
        from nybble.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, [
            "lineage",
            "--queries", str(qp),
            "--output", str(out),
        ])
        assert "lineage" in result.output.lower() or str(out) in result.output

    def test_cli_prints_node_and_edge_counts(self, tmp_path):
        queries = [{"name": "q1", "sql": "SELECT * FROM orders JOIN customers ON 1=1"}]
        qp = _write_queries(tmp_path, queries)
        out = tmp_path / "lineage.html"
        from click.testing import CliRunner
        from nybble.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, [
            "lineage",
            "--queries", str(qp),
            "--output", str(out),
        ])
        # Output should mention node/edge counts
        assert "node" in result.output.lower() or "edge" in result.output.lower()

    def test_cli_exits_1_missing_queries_file(self, tmp_path):
        out = tmp_path / "lineage.html"
        from click.testing import CliRunner
        from nybble.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, [
            "lineage",
            "--queries", str(tmp_path / "nonexistent.json"),
            "--output", str(out),
        ])
        assert result.exit_code != 0

    def test_cli_error_message_on_missing_file(self, tmp_path):
        out = tmp_path / "lineage.html"
        from click.testing import CliRunner
        from nybble.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, [
            "lineage",
            "--queries", str(tmp_path / "nonexistent.json"),
            "--output", str(out),
        ])
        # Check stdout + stderr for error text
        try:
            stderr_text = result.stderr or ""
        except ValueError:
            stderr_text = ""
        combined = (result.output or "") + stderr_text
        assert "error" in combined.lower() or "not found" in combined.lower() or result.exit_code != 0

    def test_cli_help_text(self):
        from click.testing import CliRunner
        from nybble.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["lineage", "--help"])
        assert result.exit_code == 0
        assert "lineage" in result.output.lower() or "queries" in result.output.lower()

    def test_cli_dialect_option_accepted(self, tmp_path):
        queries = [{"name": "q1", "sql": "SELECT * FROM orders"}]
        qp = _write_queries(tmp_path, queries)
        out = tmp_path / "lineage.html"
        from click.testing import CliRunner
        from nybble.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, [
            "lineage",
            "--queries", str(qp),
            "--output", str(out),
            "--dialect", "ansi",
        ])
        assert result.exit_code == 0, f"exit={result.exit_code}\n{result.output}"
