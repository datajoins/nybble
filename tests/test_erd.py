"""tests/test_erd.py - Tests for nybble.erd and nybble.infer_fk"""
import json
from pathlib import Path

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
def schema_relations():
    """Load a simple normalised schema: customers → orders → order_items."""
    from nybble import load
    customers = [{"id": i, "name": f"Customer {i}"} for i in range(1, 6)]
    orders = [{"id": i*10, "customer_id": i, "total": i*100.0} for i in range(1, 6)]
    items  = [{"id": i*100, "order_id": i*10, "sku": f"SKU-{i}", "qty": i} for i in range(1, 6)]
    c = load(customers, name="customers")
    o = load(orders, name="orders")
    it = load(items, name="order_items")
    return [c, o, it]


# ── FK inference tests ─────────────────────────────────────────────────────────

class TestInferFK:

    def test_tier2_detects_customer_id(self, schema_relations):
        from nybble.infer_fk import infer_fks
        fks = infer_fks(schema_relations)
        src_cols = [(f.src_table, f.src_col) for f in fks]
        # orders.customer_id should reference customers.id
        assert ("orders", "customer_id") in src_cols

    def test_tier2_detects_order_id(self, schema_relations):
        from nybble.infer_fk import infer_fks
        fks = infer_fks(schema_relations)
        src_cols = [(f.src_table, f.src_col) for f in fks]
        assert ("order_items", "order_id") in src_cols

    def test_fk_tier_is_2_for_suffix_match(self, schema_relations):
        from nybble.infer_fk import infer_fks
        fks = infer_fks(schema_relations)
        customer_fk = next(
            (f for f in fks if f.src_table == "orders" and f.src_col == "customer_id"),
            None,
        )
        assert customer_fk is not None
        assert customer_fk.tier == 2

    def test_confidence_between_0_and_1(self, schema_relations):
        from nybble.infer_fk import infer_fks
        fks = infer_fks(schema_relations)
        for fk in fks:
            assert 0.0 <= fk.confidence <= 1.0

    def test_no_self_references(self, schema_relations):
        from nybble.infer_fk import infer_fks
        fks = infer_fks(schema_relations)
        for fk in fks:
            assert fk.src_table != fk.tgt_table

    def test_empty_relations_returns_empty(self):
        from nybble.infer_fk import infer_fks
        result = infer_fks([])
        assert result == []

    def test_deduplication(self, schema_relations):
        """No duplicate (src_table, src_col, tgt_table, tgt_col) entries."""
        from nybble.infer_fk import infer_fks
        fks = infer_fks(schema_relations)
        keys = [(f.src_table, f.src_col, f.tgt_table, f.tgt_col) for f in fks]
        assert len(keys) == len(set(keys))

    def test_sorted_by_tier_then_confidence(self, schema_relations):
        from nybble.infer_fk import infer_fks
        fks = infer_fks(schema_relations)
        for i in range(len(fks) - 1):
            a, b = fks[i], fks[i+1]
            if a.tier == b.tier:
                assert a.confidence >= b.confidence
            else:
                assert a.tier <= b.tier


# ── ERD generation tests ───────────────────────────────────────────────────────

class TestERD:

    def test_erd_generates_html_file(self, schema_relations, tmp_path):
        from nybble.erd import erd
        out_path = tmp_path / "test.html"
        result_path = erd(relations=schema_relations, output=out_path, open_browser=False)
        assert result_path.exists()
        assert result_path.suffix == ".html"

    def test_erd_html_is_valid(self, schema_relations, tmp_path):
        from nybble.erd import erd
        out_path = tmp_path / "erd.html"
        erd(relations=schema_relations, output=out_path, open_browser=False)
        content = out_path.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in content
        assert "customers" in content
        assert "orders" in content

    def test_erd_html_contains_schema_json(self, schema_relations, tmp_path):
        """Output HTML must embed SCHEMA with table and column data."""
        from nybble.erd import erd
        out_path = tmp_path / "erd.html"
        erd(relations=schema_relations, output=out_path, open_browser=False)
        content = out_path.read_text(encoding="utf-8")
        # New Cytoscape template uses SCHEMA (pk/fk/fk_target format)
        assert "SCHEMA" in content
        assert '"name"' in content
        assert '"columns"' in content

    def test_erd_html_uses_cytoscape_elk(self, schema_relations, tmp_path):
        """Output HTML must load Cytoscape + ELK from CDN (same as Docker UI).

        The CLI ERD now uses the same Cytoscape+ELK renderer as the Docker UI
        for layout quality and visual parity.  The CDN scripts are the same
        three URLs used by the Docker erd.html template.  They are intentional
        and expected.  Only CSS/font CDN refs (which would block offline use)
        are disallowed.
        """
        import re
        from nybble.erd import erd
        out_path = tmp_path / "erd.html"
        erd(relations=schema_relations, output=out_path, open_browser=False)
        content = out_path.read_text(encoding="utf-8")
        # Cytoscape + ELK CDN scripts must be present
        assert "cytoscape" in content
        assert "elkjs" in content
        # No external CSS or @font-face CDN (those would break offline use)
        css_cdn = re.findall(r'href=["\']https?://', content)
        assert len(css_cdn) == 0, f"Found CDN stylesheet refs: {css_cdn}"

    def test_erd_highlights_param_support(self, schema_relations, tmp_path):
        """HTML must contain code to handle ?highlight= URL param."""
        from nybble.erd import erd
        out_path = tmp_path / "erd.html"
        erd(relations=schema_relations, output=out_path, open_browser=False)
        content = out_path.read_text(encoding="utf-8")
        assert "highlight" in content

    def test_erd_embed_mode_support(self, schema_relations, tmp_path):
        """HTML must contain code to handle ?embed= URL param."""
        from nybble.erd import erd
        out_path = tmp_path / "erd.html"
        erd(relations=schema_relations, output=out_path, open_browser=False)
        content = out_path.read_text(encoding="utf-8")
        assert "embed" in content

    def test_erd_no_relations_raises(self):
        from nybble.erd import erd
        with pytest.raises(ValueError, match="No relations"):
            erd(relations=[], open_browser=False)

    def test_erd_default_output_is_nybble_dir(self, schema_relations, tmp_path):
        """Default output path is .nybble/erd.html when no output specified."""
        import sys
        import importlib
        import nybble.erd as _erd_fn  # noqa - may be shadowed
        erd_mod = sys.modules["nybble.erd"]
        default = tmp_path / ".nybble" / "erd.html"
        original = erd_mod._DEFAULT_OUTPUT
        try:
            erd_mod._DEFAULT_OUTPUT = default
            result_path = erd_mod.erd(relations=schema_relations, open_browser=False)
            assert result_path == default
            assert default.exists()
        finally:
            erd_mod._DEFAULT_OUTPUT = original


# ── Tier-filter and bbox tests ─────────────────────────────────────────────────

class TestErdTierFilter:
    """Verify that tier-3/4 FK noise is stripped from the rendered SCHEMA."""

    def test_tier3_fk_columns_absent_from_schema_json(self, schema_relations, tmp_path):
        """Columns inferred only at tier 3+ must NOT appear as fk:true in the HTML."""
        from nybble.erd import erd
        out_path = tmp_path / "erd.html"
        erd(relations=schema_relations, output=out_path, open_browser=False)
        content = out_path.read_text(encoding="utf-8")
        # Extract the SCHEMA JSON from the HTML
        import re, json as _json
        m = re.search(r'const SCHEMA\s*=\s*(\[.*?\]);', content, re.DOTALL)
        assert m, "SCHEMA not found in rendered HTML"
        schema = _json.loads(m.group(1))
        # Collect all columns marked fk:true
        fk_cols = [
            (t["name"], c["name"])
            for t in schema
            for c in t.get("columns", [])
            if c.get("fk")
        ]
        # The only legitimate FK columns for customers/orders/order_items are:
        #   orders.customer_id → customers.id   (tier 2)
        #   order_items.order_id → orders.id    (tier 2)
        # Any other column marked fk:true would be tier-3/4 noise.
        legitimate = {("orders", "customer_id"), ("order_items", "order_id")}
        noise = [(t, c) for t, c in fk_cols if (t, c) not in legitimate]
        assert noise == [], f"Tier-3/4 noise FK columns in rendered SCHEMA: {noise}"

    def test_erd_bundle_to_schema_strips_tier3(self):
        """_erd_bundle_to_schema must suppress fk=True for tier > 2."""
        from nybble.erd import _erd_bundle_to_schema
        fake_bundle = {
            "tables": [{
                "name": "t1",
                "row_count": 0,
                "columns": [
                    {"name": "id",   "type": "INTEGER", "is_pk": True,  "is_fk": False},
                    {"name": "ref2", "type": "INTEGER", "is_pk": False, "is_fk": True,
                     "fk_ref": {"ref_table": "t2", "ref_col": "id", "tier": 2, "confidence": 0.9}},
                    {"name": "ref3", "type": "INTEGER", "is_pk": False, "is_fk": True,
                     "fk_ref": {"ref_table": "t2", "ref_col": "id", "tier": 3, "confidence": 0.7}},
                    {"name": "ref4", "type": "INTEGER", "is_pk": False, "is_fk": True,
                     "fk_ref": {"ref_table": "t2", "ref_col": "id", "tier": 4, "confidence": 0.5}},
                ],
            }],
            "edges": [],
        }
        result = _erd_bundle_to_schema(fake_bundle)
        assert len(result) == 1
        cols = {c["name"]: c for c in result[0]["columns"]}
        assert cols["ref2"]["fk"] is True,  "tier-2 FK should be surfaced"
        assert cols["ref3"]["fk"] is False, "tier-3 FK must be suppressed"
        assert cols["ref4"]["fk"] is False, "tier-4 FK must be suppressed"

    def test_bbox_svg_element_present_in_html(self, schema_relations, tmp_path):
        """The rendered HTML must include the #bbox-svg element for group overlays."""
        from nybble.erd import erd
        out_path = tmp_path / "erd.html"
        erd(relations=schema_relations, output=out_path, open_browser=False)
        content = out_path.read_text(encoding="utf-8")
        assert 'id="bbox-svg"' in content, "#bbox-svg element missing from rendered HTML"
        assert "detectBBoxGroups" in content, "detectBBoxGroups() JS function missing"
        assert "drawBBoxGroups" in content, "drawBBoxGroups() JS function missing"
        assert "initBBoxGroups" in content, "initBBoxGroups() JS function missing"


class TestErdDisplayNameMismatch:
    """Regression tests for the SQL-dump name-mismatch bug.

    When a SQL dump is loaded, DuckDB registers views as
    ``<source>__<table>`` (e.g. ``dump__orders``).  The display name is
    ``orders``.  FK inference must use display names so that FK candidates
    match the Schema node IDs used by build_erd_data().
    """

    @pytest.fixture(autouse=True)
    def _reset(self):
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

    def test_infer_fks_display_name_fn(self):
        """infer_fks with display_name_fn must return FK candidates with display names."""
        from nybble import load
        from nybble.infer_fk import infer_fks
        from nybble.erd import _display_name

        # Simulate SQL-dump loading: register with __ prefix names
        customers = [{"id": i, "name": f"C{i}"} for i in range(1, 6)]
        orders    = [{"id": i*10, "customer_id": i} for i in range(1, 6)]
        # Load and manually rename to simulate the __ prefix convention
        c = load(customers, name="dump__customers")
        o = load(orders,    name="dump__orders")

        fks = infer_fks([c, o], display_name_fn=_display_name)
        # FK candidates must use display names (stripped of "dump__")
        src_tables = {f.src_table for f in fks}
        tgt_tables = {f.tgt_table for f in fks}
        assert "dump__orders"    not in src_tables, "Raw DuckDB name leaked into FK src_table"
        assert "dump__customers" not in tgt_tables, "Raw DuckDB name leaked into FK tgt_table"
        # The tier-2 FK orders.customer_id → customers.id must be found
        match = next(
            (f for f in fks if f.src_table == "orders" and f.src_col == "customer_id"),
            None,
        )
        assert match is not None, "orders.customer_id → customers.id not found with display names"
        assert match.tgt_table == "customers"

    def test_erd_prefixed_relations_produce_edges(self):
        """ERD generated from __ prefixed relations must contain FK edges."""
        import re, json as _json
        from nybble import load
        from nybble.erd import erd

        customers = [{"id": i, "name": f"C{i}"} for i in range(1, 6)]
        orders    = [{"id": i*10, "customer_id": i} for i in range(1, 6)]
        c = load(customers, name="dump__customers")
        o = load(orders,    name="dump__orders")

        out = erd(relations=[c, o], output="/tmp/erd_prefix_test.html", open_browser=False)
        content = out.read_text(encoding="utf-8")
        m = re.search(r'const SCHEMA\s*=\s*(\[.*?\]);', content, re.DOTALL)
        assert m, "SCHEMA not found in rendered HTML"
        schema = _json.loads(m.group(1))
        fk_cols = [
            (t["name"], c["name"])
            for t in schema
            for c in t.get("columns", [])
            if c.get("fk")
        ]
        assert ("orders", "customer_id") in fk_cols, (
            "orders.customer_id must be marked fk:true in SCHEMA; "
            f"got fk columns: {fk_cols}"
        )
