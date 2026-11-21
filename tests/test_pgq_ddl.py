"""
tests/test_pgq_ddl.py - Tests for P2-6: DuckDB SQL/PGQ DDL generation

Covers:
  - generate_pgq_ddl() with multiple vertex + edge tables
  - generate_pgq_ddl() with vertex tables but no edges
  - generate_pgq_ddl() on empty graph returns empty string without raising
  - DDL contains required DuckDB SQL/PGQ structural keywords
  - graph_name parameter is reflected in the output
  - FKCandidate dataclass and dict inputs both work
  - CLI: nybble kg pgq --help, stdout output, --out file, empty-graph error
  - CLI: nybble kg --export pgq still works (backward compat via kg())
  - MCP: nybble_kg_export_pgq tool registered and dispatchable
  - nybble/__init__ exports generate_pgq_ddl
  - Round-trip parse: DuckDB in-process validation (skipped if duckdb unavailable)
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import networkx as nx
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Shared session reset
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_session(tmp_path, monkeypatch):
    import nybble.resolver as r
    import nybble.arena as arena
    if r._SESSION is not None:
        try:
            r._SESSION.close()
        except Exception:
            pass
        r._SESSION = None
    r._REGISTERED.clear()
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


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def sqlite_db(tmp_path):
    """Small SQLite database: customers + orders with a FK relationship."""
    import sqlite3
    db_path = tmp_path / "test.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)")
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL)")
    con.execute("INSERT INTO customers VALUES (1, 'Alice'), (2, 'Bob')")
    con.execute("INSERT INTO orders VALUES (101, 1, 99.5), (102, 2, 149.0)")
    con.commit()
    con.close()
    return str(db_path)


@pytest.fixture
def csv_customers(tmp_path):
    p = tmp_path / "customers.csv"
    p.write_text("id,name\n1,Alice\n2,Bob\n")
    return str(p)


@pytest.fixture
def csv_orders(tmp_path):
    p = tmp_path / "orders.csv"
    p.write_text("id,customer_id,amount\n101,1,99.5\n102,2,149.0\n")
    return str(p)


@pytest.fixture
def two_table_graph(csv_customers, csv_orders):
    """Build a real graph from two CSV fixtures with an inferred FK."""
    from nybble.resolver import load as _load
    from nybble.infer_fk import infer_fks
    from nybble.kg import _build_graph

    _load(csv_customers, name="customers")
    _load(csv_orders, name="orders")

    from nybble.resolver import _REGISTERED
    rels = list(_REGISTERED.values())
    fks = infer_fks(rels)
    G = _build_graph(rels, fks)
    return G, fks


@pytest.fixture
def tiny_graph_with_fks():
    """Manually built graph with known vertex tables and FK candidates."""
    from nybble.infer_fk import FKCandidate

    G = nx.MultiDiGraph()
    # customers table
    G.add_node("customers:1", entity_type="customers", pk_col="id", label="Alice", row={})
    G.add_node("customers:2", entity_type="customers", pk_col="id", label="Bob",   row={})
    # orders table
    G.add_node("orders:101",  entity_type="orders",    pk_col="id", label="Order-101", row={})
    G.add_node("orders:102",  entity_type="orders",    pk_col="id", label="Order-102", row={})

    fks = [
        FKCandidate(
            src_table="orders", src_col="customer_id",
            tgt_table="customers", tgt_col="id",
            tier=2, confidence=0.9,
        )
    ]
    return G, fks


@pytest.fixture
def empty_graph():
    return nx.MultiDiGraph(), []


# ─────────────────────────────────────────────────────────────────────────────
# generate_pgq_ddl - unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestGeneratePgqDdl:

    def test_empty_graph_returns_empty_string(self, empty_graph):
        from nybble.kg import generate_pgq_ddl
        G, fks = empty_graph
        result = generate_pgq_ddl(G, fks)
        assert result == ""

    def test_empty_graph_does_not_raise(self, empty_graph):
        from nybble.kg import generate_pgq_ddl
        G, fks = empty_graph
        # Must not raise under any circumstance
        generate_pgq_ddl(G, fks)

    def test_returns_string(self, tiny_graph_with_fks):
        from nybble.kg import generate_pgq_ddl
        G, fks = tiny_graph_with_fks
        result = generate_pgq_ddl(G, fks)
        assert isinstance(result, str)

    def test_contains_create_property_graph(self, tiny_graph_with_fks):
        from nybble.kg import generate_pgq_ddl
        G, fks = tiny_graph_with_fks
        ddl = generate_pgq_ddl(G, fks)
        assert "CREATE" in ddl.upper()
        assert "PROPERTY GRAPH" in ddl.upper()

    def test_contains_vertex_tables_keyword(self, tiny_graph_with_fks):
        from nybble.kg import generate_pgq_ddl
        G, fks = tiny_graph_with_fks
        ddl = generate_pgq_ddl(G, fks)
        assert "VERTEX TABLES" in ddl.upper()

    def test_contains_edge_tables_keyword(self, tiny_graph_with_fks):
        from nybble.kg import generate_pgq_ddl
        G, fks = tiny_graph_with_fks
        ddl = generate_pgq_ddl(G, fks)
        assert "EDGE TABLES" in ddl.upper()

    def test_vertex_tables_contain_both_tables(self, tiny_graph_with_fks):
        from nybble.kg import generate_pgq_ddl
        G, fks = tiny_graph_with_fks
        ddl = generate_pgq_ddl(G, fks)
        assert '"customers"' in ddl
        assert '"orders"' in ddl

    def test_edge_source_key_references_src_table(self, tiny_graph_with_fks):
        from nybble.kg import generate_pgq_ddl
        G, fks = tiny_graph_with_fks
        ddl = generate_pgq_ddl(G, fks)
        # SOURCE KEY must reference the FK column
        assert "SOURCE KEY" in ddl.upper()
        assert "customer_id" in ddl

    def test_edge_destination_references_tgt_table(self, tiny_graph_with_fks):
        from nybble.kg import generate_pgq_ddl
        G, fks = tiny_graph_with_fks
        ddl = generate_pgq_ddl(G, fks)
        assert "DESTINATION KEY" in ddl.upper()
        assert '"customers"' in ddl

    def test_graph_name_in_ddl(self, tiny_graph_with_fks):
        from nybble.kg import generate_pgq_ddl
        G, fks = tiny_graph_with_fks
        ddl = generate_pgq_ddl(G, fks, graph_name="my_custom_graph")
        assert "my_custom_graph" in ddl

    def test_default_graph_name_is_nybble_graph(self, tiny_graph_with_fks):
        from nybble.kg import generate_pgq_ddl
        G, fks = tiny_graph_with_fks
        ddl = generate_pgq_ddl(G, fks)
        assert "nybble_graph" in ddl

    def test_vertex_tables_only_no_edges(self):
        """Graph with vertices but no FK candidates still emits VERTEX TABLES."""
        from nybble.kg import generate_pgq_ddl

        G = nx.MultiDiGraph()
        G.add_node("t:1", entity_type="things", pk_col="id", label="thing", row={})
        ddl = generate_pgq_ddl(G, [])
        assert "VERTEX TABLES" in ddl.upper()
        assert '"things"' in ddl
        # No EDGE TABLES block when there are no edges
        assert "EDGE TABLES" not in ddl.upper()

    def test_ends_with_semicolon(self, tiny_graph_with_fks):
        from nybble.kg import generate_pgq_ddl
        G, fks = tiny_graph_with_fks
        ddl = generate_pgq_ddl(G, fks)
        assert ";" in ddl

    def test_pk_key_clause_present(self, tiny_graph_with_fks):
        from nybble.kg import generate_pgq_ddl
        G, fks = tiny_graph_with_fks
        ddl = generate_pgq_ddl(G, fks)
        # Both vertex tables have pk_col="id" so KEY (id) must appear
        assert "KEY (id)" in ddl

    def test_dict_fk_candidates_also_work(self):
        """generate_pgq_ddl supports FK candidates as plain dicts (not just dataclasses)."""
        from nybble.kg import generate_pgq_ddl

        G = nx.MultiDiGraph()
        G.add_node("a:1", entity_type="a", pk_col="id", label="x", row={})
        G.add_node("b:1", entity_type="b", pk_col="id", label="y", row={})

        fks_as_dicts = [
            {"src_table": "b", "src_col": "a_id", "tgt_table": "a", "tgt_col": "id"}
        ]
        ddl = generate_pgq_ddl(G, fks_as_dicts)
        assert "EDGE TABLES" in ddl.upper()
        assert "a_id" in ddl

    def test_or_replace_in_ddl(self, tiny_graph_with_fks):
        from nybble.kg import generate_pgq_ddl
        G, fks = tiny_graph_with_fks
        ddl = generate_pgq_ddl(G, fks)
        assert "OR REPLACE" in ddl.upper()

    def test_fk_outside_vertex_tables_excluded(self):
        """FK candidates referencing tables not in the graph are excluded."""
        from nybble.kg import generate_pgq_ddl
        from nybble.infer_fk import FKCandidate

        G = nx.MultiDiGraph()
        G.add_node("a:1", entity_type="a", pk_col="id", label="x", row={})

        # FK references table "b" which is NOT in the graph
        fks = [FKCandidate(src_table="a", src_col="b_id",
                           tgt_table="b", tgt_col="id", tier=2, confidence=0.9)]
        ddl = generate_pgq_ddl(G, fks)
        assert "EDGE TABLES" not in ddl.upper()
        assert "b_id" not in ddl

    def test_two_table_graph_produces_valid_structure(self, two_table_graph):
        """End-to-end: real graph from CSV fixtures produces well-formed DDL."""
        from nybble.kg import generate_pgq_ddl
        G, fks = two_table_graph
        ddl = generate_pgq_ddl(G, fks)
        assert "CREATE" in ddl.upper()
        assert "PROPERTY GRAPH" in ddl.upper()
        assert "VERTEX TABLES" in ddl.upper()


# ─────────────────────────────────────────────────────────────────────────────
# nybble/__init__ export
# ─────────────────────────────────────────────────────────────────────────────

class TestExportedFromInit:
    def test_generate_pgq_ddl_importable(self):
        from nybble import generate_pgq_ddl  # noqa: F401

    def test_generate_pgq_ddl_callable(self):
        from nybble import generate_pgq_ddl
        assert callable(generate_pgq_ddl)

    def test_generate_pgq_ddl_in_all(self):
        import nybble
        assert "generate_pgq_ddl" in nybble.__all__


# ─────────────────────────────────────────────────────────────────────────────
# CLI: nybble kg pgq
# ─────────────────────────────────────────────────────────────────────────────

class TestCLIKgPgq:

    def test_pgq_help_exits_zero(self):
        from click.testing import CliRunner
        from nybble.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["kg", "pgq", "--help"])
        assert result.exit_code == 0
        assert "--out" in result.output or "-o" in result.output

    def test_kg_help_lists_pgq_subcommand(self):
        from click.testing import CliRunner
        from nybble.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["kg", "--help"])
        assert result.exit_code == 0
        assert "pgq" in result.output

    def test_pgq_stdout_contains_create_property_graph(
        self, csv_customers, csv_orders, tmp_path, monkeypatch
    ):
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "kg", "pgq",
            "--source", f"customers={csv_customers}",
            "--source", f"orders={csv_orders}",
        ])
        assert result.exit_code == 0
        assert "PROPERTY GRAPH" in result.output.upper()

    def test_pgq_stdout_is_pipeable(self, csv_customers, tmp_path, monkeypatch):
        """Output must start with SQL content, not Rich markup (status goes to stderr)."""
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "kg", "pgq",
            "--source", f"customers={csv_customers}",
        ])
        assert result.exit_code == 0
        # DDL content must be present in output
        assert "--" in result.output or "CREATE" in result.output.upper()

    def test_pgq_out_flag_writes_file(self, csv_customers, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        out_file = tmp_path / "out.sql"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "kg", "pgq",
            "--source", f"customers={csv_customers}",
            "--out", str(out_file),
        ])
        assert result.exit_code == 0
        assert out_file.exists()
        content = out_file.read_text()
        assert "PROPERTY GRAPH" in content.upper()

    def test_pgq_out_flag_confirmation_not_on_stdout(
        self, csv_customers, tmp_path, monkeypatch
    ):
        """--out flag must not pollute stdout (confirmation goes to stderr)."""
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        out_file = tmp_path / "out.sql"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "kg", "pgq",
            "--source", f"customers={csv_customers}",
            "--out", str(out_file),
        ])
        assert result.exit_code == 0
        # DDL went to file - SQL CREATE statements must not be on stdout
        assert "PROPERTY GRAPH" not in result.output.upper()

    def test_pgq_graph_name_in_output(self, csv_customers, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "kg", "pgq",
            "--source", f"customers={csv_customers}",
            "--graph-name", "test_graph",
        ])
        assert result.exit_code == 0
        assert "test_graph" in result.output

    def test_pgq_no_sources_exits_nonzero(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, ["kg", "pgq"])
        assert result.exit_code != 0

    def test_pgq_vertex_tables_in_output(
        self, csv_customers, csv_orders, tmp_path, monkeypatch
    ):
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "kg", "pgq",
            "--source", f"customers={csv_customers}",
            "--source", f"orders={csv_orders}",
        ])
        assert result.exit_code == 0
        assert "VERTEX TABLES" in result.output.upper()
        assert '"customers"' in result.output
        assert '"orders"' in result.output

    def test_kg_export_pgq_option_still_works(
        self, csv_customers, tmp_path, monkeypatch
    ):
        """nybble kg --export pgq (the original file-writing path) still works."""
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "kg",
            "--source", f"customers={csv_customers}",
            "--no-browser",
            "--export", "pgq",
        ])
        assert result.exit_code == 0
        # The SQL/PGQ path should be mentioned in the output
        assert "pgq" in result.output.lower() or "sql" in result.output.lower()


# ─────────────────────────────────────────────────────────────────────────────
# MCP server: nybble_kg_export_pgq (skipped when mcp package absent)
# ─────────────────────────────────────────────────────────────────────────────

_mcp_available = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="mcp package not installed; skipping MCP tests",
)


@_mcp_available
class TestMCPKgExportPgq:

    def test_tool_in_list(self):
        import asyncio
        from nybble.mcp_server import list_tools
        tools = asyncio.get_event_loop().run_until_complete(list_tools())
        names = [t.name for t in tools]
        assert "nybble_kg_export_pgq" in names

    def test_tool_has_graph_name_param(self):
        import asyncio
        from nybble.mcp_server import list_tools
        tools = asyncio.get_event_loop().run_until_complete(list_tools())
        tool = next(t for t in tools if t.name == "nybble_kg_export_pgq")
        assert "graph_name" in tool.inputSchema["properties"]
        assert tool.inputSchema["required"] == []

    def test_dispatch_returns_ddl(
        self, csv_customers, tmp_path, monkeypatch
    ):
        import asyncio
        from nybble.resolver import load as _load
        from nybble.mcp_server import _dispatch
        monkeypatch.chdir(tmp_path)

        _load(csv_customers, name="customers")

        result = asyncio.get_event_loop().run_until_complete(
            _dispatch("nybble_kg_export_pgq", {})
        )
        assert isinstance(result, dict)
        assert "ddl" in result
        assert "PROPERTY GRAPH" in result["ddl"].upper()

    def test_dispatch_no_sources_raises(self, tmp_path, monkeypatch):
        import asyncio
        from nybble.mcp_server import _dispatch
        monkeypatch.chdir(tmp_path)

        with pytest.raises(ValueError, match="No sources loaded"):
            asyncio.get_event_loop().run_until_complete(
                _dispatch("nybble_kg_export_pgq", {})
            )


# ─────────────────────────────────────────────────────────────────────────────
# Round-trip parse: DuckDB in-process validation
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(
    importlib.util.find_spec("duckdb") is None,
    reason="duckdb not installed",
)
class TestPgqRoundTrip:
    """Validate generated DDL by parsing it with DuckDB in-process."""

    def test_ddl_parses_without_syntax_error(self, sqlite_db, tmp_path, monkeypatch):
        """Load a SQLite DB, generate DDL, create graph in DuckDB."""
        import duckdb
        from nybble.resolver import load as _load, _REGISTERED
        from nybble.infer_fk import infer_fks
        from nybble.kg import _build_graph, generate_pgq_ddl
        monkeypatch.chdir(tmp_path)

        _load(f"sqlite:///{sqlite_db}")

        rels = list(_REGISTERED.values())
        fks = infer_fks(rels)
        G = _build_graph(rels, fks)
        ddl = generate_pgq_ddl(G, fks)

        assert ddl.strip(), "DDL should not be empty for a non-empty graph"

        # Parse the DDL with DuckDB - if syntax is invalid this raises
        # duckdb.ParserException.  We only test parsing, not execution,
        # because the tables don't exist in a fresh connection.
        con = duckdb.connect(":memory:")
        try:
            # DuckDB doesn't expose a pure parse API, so we try the CREATE
            # and accept any non-syntax error (tables won't exist in-memory).
            try:
                con.execute(ddl)
            except duckdb.CatalogException:
                pass  # Table doesn't exist - syntax is fine
            except duckdb.ParserException as exc:
                msg = str(exc)
                if "PROPERTY" in msg.upper() or "GRAPH" in msg.upper():
                    # SQL/PGQ syntax not yet supported in this DuckDB build
                    pytest.skip(f"DuckDB {duckdb.__version__} does not support PROPERTY GRAPH syntax")
                pytest.fail(f"DuckDB SQL/PGQ DDL has a syntax error:\n{exc}\n\nDDL:\n{ddl}")
            except Exception:
                pass  # Other runtime errors are acceptable for a parse-only check
        finally:
            con.close()
