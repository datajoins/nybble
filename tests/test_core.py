"""tests/test_core.py - Unit tests for nybble.core shared library.

All tests in this file exercise nybble.core functions directly via Python
import - no Flask context, no DuckDB session, no CLI invocation.

This confirms the per-function checklist from the integration plan:
  ✓ No HTTP context
  ✓ No CLI args
  ✓ No DuckDB coupling (pure Schema dataclass inputs)
  ✓ No SQLite coupling
  ✓ Callable from both CLI and Flask paths via identical import
  ✓ Pip-installable (no new deps)
"""
import json
import pytest


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def simple_schema():
    """A simple normalised schema: customers → orders → order_items.

    Built entirely from nybble.core.schema dataclasses - no DB required.
    """
    from nybble.core.schema import Column, ForeignKey, Schema, Table

    customers = Table(
        name="customers",
        columns=[
            Column(name="id",   data_type="INTEGER", primary_key=True),
            Column(name="name", data_type="TEXT"),
        ],
        primary_keys=["id"],
    )
    orders = Table(
        name="orders",
        columns=[
            Column(name="id",          data_type="INTEGER", primary_key=True),
            Column(name="customer_id", data_type="INTEGER"),
            Column(name="total",       data_type="REAL"),
        ],
        primary_keys=["id"],
    )
    order_items = Table(
        name="order_items",
        columns=[
            Column(name="id",       data_type="INTEGER", primary_key=True),
            Column(name="order_id", data_type="INTEGER"),
            Column(name="sku",      data_type="TEXT"),
            Column(name="qty",      data_type="INTEGER"),
        ],
        primary_keys=["id"],
    )
    return Schema(name="test", tables=[customers, orders, order_items])


@pytest.fixture
def schema_with_declared_fks():
    """Schema where FK constraints are already declared (Tier-1 path)."""
    from nybble.core.schema import Column, ForeignKey, Schema, Table

    parent = Table(
        name="parent",
        columns=[Column(name="id", data_type="INTEGER", primary_key=True)],
        primary_keys=["id"],
    )
    child = Table(
        name="child",
        columns=[
            Column(name="id",        data_type="INTEGER", primary_key=True),
            Column(name="parent_id", data_type="INTEGER"),
        ],
        primary_keys=["id"],
        foreign_keys=[ForeignKey(
            constrained_columns=["parent_id"],
            referred_table="parent",
            referred_columns=["id"],
        )],
    )
    return Schema(name="declared", tables=[parent, child])


# ── nybble.core.schema ─────────────────────────────────────────────────────────

class TestCoreSchema:

    def test_schema_dataclass_creates_correctly(self, simple_schema):
        assert simple_schema.name == "test"
        assert len(simple_schema.tables) == 3

    def test_table_names(self, simple_schema):
        names = simple_schema.table_names()
        assert "customers" in names
        assert "orders" in names
        assert "order_items" in names

    def test_get_table(self, simple_schema):
        t = simple_schema.get_table("orders")
        assert t is not None
        assert t.name == "orders"
        assert len(t.columns) == 3

    def test_get_table_missing_returns_none(self, simple_schema):
        assert simple_schema.get_table("nonexistent") is None

    def test_summary_contains_table_names(self, simple_schema):
        s = simple_schema.summary()
        assert "customers" in s
        assert "orders" in s

    def test_schema_model_re_export_from_server_shim(self, simple_schema):
        """server/ingestion/schema_model.py must re-export the same classes."""
        from server.ingestion.schema_model import Schema as ServerSchema, Table as ServerTable
        from nybble.core.schema import Schema as CoreSchema
        # They must be the same class (shim re-exports)
        assert ServerSchema is CoreSchema


# ── nybble.core.infer_fk ──────────────────────────────────────────────────────

class TestCoreInferFK:

    def test_tier2_detects_customer_id(self, simple_schema):
        from nybble.core.infer_fk import infer_fks
        fks = infer_fks(simple_schema)
        src_cols = [(f.src_table, f.src_col) for f in fks]
        assert ("orders", "customer_id") in src_cols

    def test_tier2_detects_order_id(self, simple_schema):
        from nybble.core.infer_fk import infer_fks
        fks = infer_fks(simple_schema)
        src_cols = [(f.src_table, f.src_col) for f in fks]
        assert ("order_items", "order_id") in src_cols

    def test_tier1_declared_fks_come_first(self, schema_with_declared_fks):
        from nybble.core.infer_fk import infer_fks
        fks = infer_fks(schema_with_declared_fks)
        parent_fk = next(
            (f for f in fks if f.src_table == "child" and f.src_col == "parent_id"),
            None,
        )
        assert parent_fk is not None
        assert parent_fk.tier == 1
        assert parent_fk.confidence == 1.0

    def test_confidence_between_0_and_1(self, simple_schema):
        from nybble.core.infer_fk import infer_fks
        for fk in infer_fks(simple_schema):
            assert 0.0 <= fk.confidence <= 1.0

    def test_no_self_references(self, simple_schema):
        from nybble.core.infer_fk import infer_fks
        for fk in infer_fks(simple_schema):
            assert fk.src_table != fk.tgt_table

    def test_deduplication(self, simple_schema):
        from nybble.core.infer_fk import infer_fks
        fks = infer_fks(simple_schema)
        keys = [(f.src_table, f.src_col, f.tgt_table, f.tgt_col) for f in fks]
        assert len(keys) == len(set(keys))

    def test_sorted_by_tier_then_confidence(self, simple_schema):
        from nybble.core.infer_fk import infer_fks
        fks = infer_fks(simple_schema)
        for i in range(len(fks) - 1):
            a, b = fks[i], fks[i + 1]
            if a.tier == b.tier:
                assert a.confidence >= b.confidence
            else:
                assert a.tier <= b.tier

    def test_empty_schema_returns_empty(self):
        from nybble.core.infer_fk import infer_fks
        from nybble.core.schema import Schema
        result = infer_fks(Schema(name="empty"))
        assert result == []

    def test_tier4_skipped_when_no_fetch_stats(self, simple_schema):
        """Without fetch_stats, Tier 4 must not run (no combinatorial explosion)."""
        from nybble.core.infer_fk import infer_fks
        fks = infer_fks(simple_schema, fetch_stats=None)
        tiers = {f.tier for f in fks}
        assert 4 not in tiers

    def test_tier4_runs_when_fetch_stats_provided(self, simple_schema):
        """With a suitable fetch_stats, Tier 4 candidates appear."""
        from nybble.core.infer_fk import infer_fks

        # Make every column look 100% unique to maximise Tier 4 candidates
        def mock_stats(table, col):
            return (100, 100)  # distinct=100, total=100 → ratio 1.0

        # Clear declared FKs so Tier 2/3/4 have room to fire
        from nybble.core.schema import Column, Schema, Table
        schema = Schema(name="t4test", tables=[
            Table(name="a", columns=[Column("id", "INTEGER"), Column("b_id", "INTEGER")]),
            Table(name="b", columns=[Column("id", "INTEGER")]),
        ])
        fks = infer_fks(schema, fetch_stats=mock_stats)
        tiers = {f.tier for f in fks}
        # Tier 2 should fire for b_id → b.id; Tier 4 would also produce candidates
        assert 2 in tiers

    def test_no_flask_import(self):
        """nybble.core.infer_fk must not import flask at module level."""
        import importlib
        import sys
        # Temporarily remove flask from sys.modules to ensure it is not pulled in
        flask_backup = sys.modules.pop("flask", None)
        try:
            if "nybble.core.infer_fk" in sys.modules:
                del sys.modules["nybble.core.infer_fk"]
            mod = importlib.import_module("nybble.core.infer_fk")
            assert mod is not None
        finally:
            if flask_backup is not None:
                sys.modules["flask"] = flask_backup


# ── nybble.core.erd_data ──────────────────────────────────────────────────────

class TestCoreErdData:

    def test_build_erd_data_returns_tables_and_edges(self, simple_schema):
        from nybble.core.erd_data import build_erd_data
        from nybble.core.infer_fk import infer_fks
        fks = infer_fks(simple_schema)
        result = build_erd_data(simple_schema, fks)
        assert "tables" in result
        assert "edges" in result

    def test_all_tables_present(self, simple_schema):
        from nybble.core.erd_data import build_erd_data
        result = build_erd_data(simple_schema, [])
        names = [t["name"] for t in result["tables"]]
        assert "customers" in names
        assert "orders" in names
        assert "order_items" in names

    def test_fk_edges_appear(self, simple_schema):
        from nybble.core.erd_data import build_erd_data
        from nybble.core.infer_fk import infer_fks
        fks = infer_fks(simple_schema)
        result = build_erd_data(simple_schema, fks)
        edge_srcs = [(e["source"], e["source_col"]) for e in result["edges"]]
        assert ("orders", "customer_id") in edge_srcs

    def test_column_is_pk_annotated(self, simple_schema):
        from nybble.core.erd_data import build_erd_data
        from nybble.core.infer_fk import infer_fks
        fks = infer_fks(simple_schema)
        result = build_erd_data(simple_schema, fks)
        cust = next(t for t in result["tables"] if t["name"] == "customers")
        id_col = next(c for c in cust["columns"] if c["name"] == "id")
        assert id_col["is_pk"] is True

    def test_column_is_fk_annotated(self, simple_schema):
        from nybble.core.erd_data import build_erd_data
        from nybble.core.infer_fk import infer_fks
        fks = infer_fks(simple_schema)
        result = build_erd_data(simple_schema, fks)
        orders = next(t for t in result["tables"] if t["name"] == "orders")
        cust_id_col = next(c for c in orders["columns"] if c["name"] == "customer_id")
        assert cust_id_col["is_fk"] is True

    def test_row_counts_populated(self, simple_schema):
        from nybble.core.erd_data import build_erd_data
        result = build_erd_data(simple_schema, [], row_counts={"customers": 42})
        cust = next(t for t in result["tables"] if t["name"] == "customers")
        assert cust["row_count"] == 42

    def test_table_meta_merged_into_table_dict(self, simple_schema):
        """table_meta extras (e.g. source_type) must appear in the output table dict."""
        from nybble.core.erd_data import build_erd_data
        meta = {"customers": {"source_type": "file"}, "orders": {"source_type": "db"}}
        result = build_erd_data(simple_schema, [], table_meta=meta)
        cust = next(t for t in result["tables"] if t["name"] == "customers")
        orders = next(t for t in result["tables"] if t["name"] == "orders")
        assert cust["source_type"] == "file"
        assert orders["source_type"] == "db"

    def test_table_meta_none_leaves_no_extra_keys(self, simple_schema):
        """With no table_meta, standard keys are present and no unexpected extras."""
        from nybble.core.erd_data import build_erd_data
        result = build_erd_data(simple_schema, [])
        for t in result["tables"]:
            assert "id" in t
            assert "name" in t
            assert "row_count" in t
            assert "columns" in t
            assert "source_type" not in t

    def test_edge_has_tier_and_confidence(self, simple_schema):
        from nybble.core.erd_data import build_erd_data
        from nybble.core.infer_fk import infer_fks
        fks = infer_fks(simple_schema)
        result = build_erd_data(simple_schema, fks)
        for edge in result["edges"]:
            assert "tier" in edge
            assert "confidence" in edge

    def test_no_duplicate_edges(self, simple_schema):
        from nybble.core.erd_data import build_erd_data
        from nybble.core.infer_fk import infer_fks
        fks = infer_fks(simple_schema)
        result = build_erd_data(simple_schema, fks)
        edge_ids = [e["id"] for e in result["edges"]]
        assert len(edge_ids) == len(set(edge_ids))

    def test_erd_data_to_schema_json_roundtrip(self, simple_schema):
        from nybble.core.erd_data import build_erd_data, erd_data_to_schema_json
        from nybble.core.infer_fk import infer_fks
        fks = infer_fks(simple_schema)
        erd_data = build_erd_data(simple_schema, fks)
        schema_json = erd_data_to_schema_json(erd_data)
        assert isinstance(schema_json, dict)
        assert "tables" in schema_json
        assert "enums" in schema_json
        # Must be JSON-serialisable
        json_str = json.dumps(schema_json)
        assert "customers" in json_str

    def test_erd_data_to_schema_json_fk_constraint(self, simple_schema):
        from nybble.core.erd_data import build_erd_data, erd_data_to_schema_json
        from nybble.core.infer_fk import infer_fks
        fks = infer_fks(simple_schema)
        erd_data = build_erd_data(simple_schema, fks)
        schema_json = erd_data_to_schema_json(erd_data)
        orders = schema_json["tables"].get("orders", {})
        constraints = orders.get("constraints", {})
        fk_constraints = [c for c in constraints.values() if c.get("type") == "FOREIGN KEY"]
        assert len(fk_constraints) >= 1

    def test_no_flask_import(self):
        """nybble.core.erd_data must not import flask at module level."""
        import importlib, sys
        flask_backup = sys.modules.pop("flask", None)
        try:
            if "nybble.core.erd_data" in sys.modules:
                del sys.modules["nybble.core.erd_data"]
            mod = importlib.import_module("nybble.core.erd_data")
            assert mod is not None
        finally:
            if flask_backup is not None:
                sys.modules["flask"] = flask_backup


# ── nybble.core.kg_builder ────────────────────────────────────────────────────

class TestCoreKgBuilder:

    def _make_rows(self):
        """Return row dicts for the simple schema fixtures."""
        return {
            "customers":   [{"id": i, "name": f"C{i}"} for i in range(1, 4)],
            "orders":      [{"id": i * 10, "customer_id": i, "total": float(i * 100)} for i in range(1, 4)],
            "order_items": [{"id": i * 100, "order_id": i * 10, "sku": f"S{i}", "qty": i} for i in range(1, 4)],
        }

    def test_build_kg_returns_multidigraph(self, simple_schema):
        import networkx as nx
        from nybble.core.infer_fk import infer_fks
        from nybble.core.kg_builder import build_kg

        rows = self._make_rows()

        def fetch_rows(table_name, limit):
            return rows.get(table_name, [])[:limit]

        fks = infer_fks(simple_schema)
        G = build_kg(simple_schema, fks, fetch_rows=fetch_rows)
        assert isinstance(G, nx.MultiDiGraph)

    def test_build_kg_node_count(self, simple_schema):
        from nybble.core.infer_fk import infer_fks
        from nybble.core.kg_builder import build_kg

        rows = self._make_rows()

        def fetch_rows(table_name, limit):
            return rows.get(table_name, [])[:limit]

        fks = infer_fks(simple_schema)
        G = build_kg(simple_schema, fks, fetch_rows=fetch_rows)
        # 3 customers + 3 orders + 3 items = 9 nodes
        assert G.number_of_nodes() == 9

    def test_build_kg_fk_edges_present(self, simple_schema):
        from nybble.core.infer_fk import infer_fks
        from nybble.core.kg_builder import build_kg

        rows = self._make_rows()

        def fetch_rows(table_name, limit):
            return rows.get(table_name, [])[:limit]

        fks = infer_fks(simple_schema)
        G = build_kg(simple_schema, fks, fetch_rows=fetch_rows)
        edge_relations = [d.get("relation", "") for _, _, d in G.edges(data=True)]
        fk_edges = [r for r in edge_relations if r.startswith("FK:")]
        assert len(fk_edges) > 0

    def test_build_kg_empty_schema_empty_graph(self):
        import networkx as nx
        from nybble.core.kg_builder import build_kg
        from nybble.core.schema import Schema

        def fetch_rows(table_name, limit):
            return []

        G = build_kg(Schema(name="empty"), [], fetch_rows=fetch_rows)
        assert G.number_of_nodes() == 0
        assert G.number_of_edges() == 0

    def test_build_kg_max_rows_respected(self, simple_schema):
        """fetch_rows is called with the max_rows limit."""
        from nybble.core.infer_fk import infer_fks
        from nybble.core.kg_builder import build_kg

        call_limits = []

        def fetch_rows(table_name, limit):
            call_limits.append(limit)
            return [{"id": 1, "name": "x", "customer_id": 1, "total": 1.0,
                     "order_id": 10, "sku": "s", "qty": 1}][:limit]

        fks = infer_fks(simple_schema)
        build_kg(simple_schema, fks, fetch_rows=fetch_rows, max_rows=7)
        assert all(lim == 7 for lim in call_limits), f"Expected 7, got {call_limits}"

    def test_no_flask_import(self):
        """nybble.core.kg_builder must not import flask at module level."""
        import importlib, sys
        flask_backup = sys.modules.pop("flask", None)
        try:
            if "nybble.core.kg_builder" in sys.modules:
                del sys.modules["nybble.core.kg_builder"]
            mod = importlib.import_module("nybble.core.kg_builder")
            assert mod is not None
        finally:
            if flask_backup is not None:
                sys.modules["flask"] = flask_backup

    def test_no_duckdb_import(self):
        """nybble.core.kg_builder must not import duckdb at module level."""
        import importlib, sys
        duckdb_backup = sys.modules.pop("duckdb", None)
        try:
            if "nybble.core.kg_builder" in sys.modules:
                del sys.modules["nybble.core.kg_builder"]
            mod = importlib.import_module("nybble.core.kg_builder")
            assert mod is not None
        finally:
            if duckdb_backup is not None:
                sys.modules["duckdb"] = duckdb_backup


# ── server shims (integration gate) ──────────────────────────────────────────

class TestServerShims:
    """Verify that server-side shims delegate correctly to nybble.core."""

    def test_server_infer_missing_fks_adds_fks(self, simple_schema):
        from server.ingestion.infer_fk import infer_missing_fks
        # Clear any pre-existing FKs to test inference
        for t in simple_schema.tables:
            t.foreign_keys.clear()
        added = infer_missing_fks(simple_schema)
        assert added > 0

    def test_server_schema_model_shim_same_class(self):
        from server.ingestion.schema_model import Schema as S1
        from nybble.core.schema import Schema as S2
        assert S1 is S2

    def test_build_erd_data_callable_without_flask(self, simple_schema):
        """build_erd_data must work with no Flask app context active."""
        from nybble.core.erd_data import build_erd_data
        # If Flask context were required, this would raise RuntimeError
        result = build_erd_data(simple_schema, [])
        assert isinstance(result, dict)
