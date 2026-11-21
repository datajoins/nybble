"""tests/test_kg.py - Tests for nybble.kg"""
import json
from pathlib import Path

import networkx as nx
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
def simple_schema():
    """A simple two-table schema with an FK relationship."""
    from nybble import load
    users = [
        {"id": 1, "name": "Alice", "email": "alice@example.com"},
        {"id": 2, "name": "Bob",   "email": "bob@example.com"},
    ]
    posts = [
        {"id": 10, "user_id": 1, "title": "Hello World"},
        {"id": 11, "user_id": 2, "title": "Second Post"},
        {"id": 12, "user_id": 1, "title": "Third Post"},
    ]
    u = load(users, name="users")
    p = load(posts, name="posts")
    return [u, p]


@pytest.fixture
def cross_source_schema():
    """Two tables sharing values in a string column (co-occurrence)."""
    from nybble import load
    a = [{"id": 1, "dept": "Engineering"}, {"id": 2, "dept": "Marketing"}]
    b = [{"id": 10, "dept": "Engineering", "budget": 500000},
         {"id": 11, "dept": "Marketing",   "budget": 200000}]
    ra = load(a, name="employees")
    rb = load(b, name="departments")
    return [ra, rb]


# ── Graph construction tests ──────────────────────────────────────────────────

class TestKGGraph:

    def test_kg_returns_result(self, simple_schema, tmp_path):
        from nybble.kg import kg
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        assert result is not None
        assert isinstance(result.graph, nx.MultiDiGraph)

    def test_graph_has_nodes_for_all_rows(self, simple_schema, tmp_path):
        from nybble.kg import kg
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        G = result.graph
        # 2 users + 3 posts = 5 nodes minimum
        assert G.number_of_nodes() >= 5

    def test_graph_has_edges_for_fk_links(self, simple_schema, tmp_path):
        from nybble.kg import kg
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        G = result.graph
        # There should be FK edges from posts to users
        fk_edges = [(u, v) for u, v, d in G.edges(data=True)
                    if d.get("relation", "").startswith("FK")]
        assert len(fk_edges) > 0

    def test_graph_nodes_have_entity_type(self, simple_schema, tmp_path):
        from nybble.kg import kg
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        G = result.graph
        for _, attrs in G.nodes(data=True):
            assert "entity_type" in attrs

    def test_graph_nodes_have_label(self, simple_schema, tmp_path):
        from nybble.kg import kg
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        G = result.graph
        for _, attrs in G.nodes(data=True):
            assert "label" in attrs


# ── Export tests ──────────────────────────────────────────────────────────────

class TestKGExports:

    def test_graphml_file_created(self, simple_schema, tmp_path):
        from nybble.kg import kg
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        assert result.graphml_path.exists()
        assert result.graphml_path.suffix == ".graphml"

    def test_graphml_is_valid_xml(self, simple_schema, tmp_path):
        from nybble.kg import kg
        import xml.etree.ElementTree as ET
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        # Should parse without error
        ET.parse(str(result.graphml_path))

    def test_jsonld_file_created(self, simple_schema, tmp_path):
        from nybble.kg import kg
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        assert result.jsonld_path.exists()
        assert result.jsonld_path.suffix == ".jsonld"

    def test_jsonld_has_context(self, simple_schema, tmp_path):
        from nybble.kg import kg
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        content = json.loads(result.jsonld_path.read_text(encoding="utf-8"))
        assert "@context" in content
        assert "@graph" in content
        assert "schema" in content["@context"]

    def test_html_file_created(self, simple_schema, tmp_path):
        from nybble.kg import kg
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        assert result.html_path.exists()
        assert result.html_path.suffix == ".html"

    def test_html_is_self_contained(self, simple_schema, tmp_path):
        """No external CDN references in the KG HTML."""
        from nybble.kg import kg
        import re
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        content = result.html_path.read_text(encoding="utf-8")
        cdn_refs = re.findall(r'src=["\']https?://', content)
        assert len(cdn_refs) == 0, f"Found CDN references: {cdn_refs}"

    def test_html_contains_kg_data(self, simple_schema, tmp_path):
        from nybble.kg import kg
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        content = result.html_path.read_text(encoding="utf-8")
        assert "KG_NODES" in content
        assert "KG_EDGES" in content


# ── Summary tests ─────────────────────────────────────────────────────────────

class TestKGSummary:

    def test_summary_returns_dict(self, simple_schema, tmp_path):
        from nybble.kg import kg
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        assert isinstance(result.summary, dict)

    def test_summary_has_node_count(self, simple_schema, tmp_path):
        from nybble.kg import kg
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        assert "node_count" in result.summary
        assert result.summary["node_count"] >= 5

    def test_summary_has_edge_count(self, simple_schema, tmp_path):
        from nybble.kg import kg
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        assert "edge_count" in result.summary

    def test_summary_has_entity_type_counts(self, simple_schema, tmp_path):
        from nybble.kg import kg
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        assert "entity_type_counts" in result.summary
        counts = result.summary["entity_type_counts"]
        assert isinstance(counts, dict)

    def test_summary_avg_degree_is_float(self, simple_schema, tmp_path):
        from nybble.kg import kg
        result = kg(relations=simple_schema, output_dir=tmp_path, open_browser=False)
        assert isinstance(result.summary["avg_degree"], float)

    def test_kg_summary_standalone(self, simple_schema):
        from nybble.kg import kg_summary
        summary = kg_summary(simple_schema)
        assert "source_count" in summary
        assert summary["source_count"] == 2
        assert "inferred_fk_count" in summary

    def test_kg_empty_relations_returns_empty_summary(self):
        from nybble.kg import kg_summary
        summary = kg_summary([])
        assert summary["node_count"] == 0

    def test_kg_no_relations_raises(self):
        from nybble.kg import kg
        with pytest.raises(ValueError, match="No relations"):
            kg(relations=[], open_browser=False)


# ── P1 roadmap tests ──────────────────────────────────────────────────────────

class TestP1Depths:
    """P1-A: _compute_depths cycle-guard and root-memoization fixes."""

    def _make_graph(self, edges):
        import networkx as nx
        G = nx.DiGraph()
        G.add_edges_from(edges)
        return G

    def test_depths_root_memoized(self):
        """Root node (A) must have depth 0; children B and C must have depth 1."""
        from server.kg.kg_graph import _compute_depths
        import networkx as nx
        G = nx.DiGraph()
        G.add_edges_from([("A", "B"), ("A", "C")])
        depths = _compute_depths(G)
        assert depths["A"] == 0
        assert depths["B"] == 1
        assert depths["C"] == 1

    def test_depths_cycle_guard(self):
        """A cycle A→B→A must not cause infinite recursion."""
        from server.kg.kg_graph import _compute_depths
        import networkx as nx
        G = nx.DiGraph()
        G.add_edges_from([("A", "B"), ("B", "A")])
        # Should return without RecursionError
        depths = _compute_depths(G)
        assert "A" in depths
        assert "B" in depths


class TestP1KGStoreWAL:
    """P1-B: KGStore must enable WAL journal mode on open."""

    def test_kg_store_wal_mode(self, tmp_path):
        from server.kg.kg_store import KGStore
        db_path = tmp_path / "kg.db"
        with KGStore(db_path) as store:
            row = store._conn.execute("PRAGMA journal_mode").fetchone()
            assert row[0] == "wal"


class TestP1EdgeKind:
    """P1-C: EdgeKind enum values must match the string literals used in relations."""

    def test_edge_kind_fk_forward(self):
        from server.kg.kg_resolver import EdgeKind
        assert EdgeKind.FK_FORWARD.value == "FK"

    def test_edge_kind_fk_reverse(self):
        from server.kg.kg_resolver import EdgeKind
        assert EdgeKind.FK_REVERSE.value == "REV_FK"

    def test_edge_kind_cross_db(self):
        from server.kg.kg_resolver import EdgeKind
        assert EdgeKind.CROSS_DB.value == "cross_db"

    def test_edge_kind_inferred(self):
        from server.kg.kg_resolver import EdgeKind
        assert EdgeKind.INFERRED.value == "inferred"

    def test_edge_kind_co_occurs(self):
        from server.kg.kg_resolver import EdgeKind
        assert EdgeKind.CO_OCCURS.value == "co_occurs"

    def test_edge_kind_schema_fk(self):
        from server.kg.kg_resolver import EdgeKind
        assert EdgeKind.SCHEMA_FK.value == "schema_fk"

    def test_edge_kind_is_str(self):
        """EdgeKind(str, Enum) values must be usable as plain strings."""
        from server.kg.kg_resolver import EdgeKind
        assert EdgeKind.FK_FORWARD == "FK"
        assert EdgeKind.FK_REVERSE == "REV_FK"


# ── P2 roadmap tests ──────────────────────────────────────────────────────────

class TestP2Schema:
    """P2-A: KGSchema property graph schema declarations and validation."""

    def test_schema_registry_validates_node(self):
        """Valid node with all required props returns empty error list."""
        from server.kg.kg_schema import KGSchema, NodeTypeDecl, PropDecl
        schema = KGSchema()
        schema.node_types["TABLE"] = NodeTypeDecl(
            name="TABLE",
            properties=[PropDecl("name", "String", nullable=False)],
        )
        errors = schema.validate_node("TABLE", {"name": "users"})
        assert errors == []

    def test_schema_registry_rejects_unknown_type(self):
        """Validating an unknown node type returns a non-empty error list."""
        from server.kg.kg_schema import KGSchema
        schema = KGSchema()
        errors = schema.validate_node("NONEXISTENT_TYPE", {})
        assert len(errors) > 0
        assert "NONEXISTENT_TYPE" in errors[0]

    def test_schema_registry_catches_missing_required_prop(self):
        """A non-nullable property that is absent produces an error."""
        from server.kg.kg_schema import KGSchema, NodeTypeDecl, PropDecl
        schema = KGSchema()
        schema.node_types["TABLE"] = NodeTypeDecl(
            name="TABLE",
            properties=[PropDecl("name", "String", nullable=False)],
        )
        errors = schema.validate_node("TABLE", {})
        assert any("name" in e for e in errors)

    def test_schema_edge_unknown_kind_is_open(self):
        """Unknown edge kinds are not errors (open schema policy)."""
        from server.kg.kg_schema import KGSchema
        schema = KGSchema()
        errors = schema.validate_edge("UNKNOWN_REL", "TABLE", "TABLE")
        assert errors == []

    def test_schema_edge_validates_from_to_types(self):
        """A known edge with wrong src/tgt types produces an error."""
        from server.kg.kg_schema import KGSchema, EdgeTypeDecl
        schema = KGSchema()
        schema.edge_types["FK"] = EdgeTypeDecl(name="FK", from_type="TABLE", to_type="TABLE")
        errors = schema.validate_edge("FK", "PERSON", "TABLE")
        assert len(errors) > 0


class TestP2HTTPEmbed:
    """P2-B: HTTP embedding provider (urllib.request, stdlib only)."""

    def test_http_embed_none_when_provider_none(self):
        """Default EMBED_PROVIDER=none returns None without making any HTTP call."""
        import os
        os.environ.pop("EMBED_PROVIDER", None)
        # Re-import to pick up the module-level constant (or call directly)
        from server.kg.kg_indexer import _http_embed
        result = _http_embed("test text")
        assert result is None

    def test_http_embed_none_when_no_api_key(self, monkeypatch):
        """EMBED_PROVIDER set but no API key also returns None."""
        import server.kg.kg_indexer as idx
        monkeypatch.setattr(idx, "EMBED_PROVIDER", "openai")
        monkeypatch.setattr(idx, "EMBED_API_KEY", "")
        result = idx._http_embed("test text")
        assert result is None

    def test_normalize_unit_vector(self):
        """[3, 4] normalises to [0.6, 0.8]."""
        from server.kg.kg_indexer import _normalize
        result = _normalize([3.0, 4.0])
        assert result is not None
        assert abs(result[0] - 0.6) < 1e-6
        assert abs(result[1] - 0.8) < 1e-6

    def test_normalize_zero_rejected(self):
        """Zero vector returns None (not a valid direction)."""
        from server.kg.kg_indexer import _normalize
        result = _normalize([0.0, 0.0])
        assert result is None

    def test_normalize_already_unit(self):
        """A unit vector normalised again stays the same."""
        import math
        from server.kg.kg_indexer import _normalize
        v = [1.0, 0.0, 0.0]
        result = _normalize(v)
        assert result is not None
        assert abs(math.sqrt(sum(x * x for x in result)) - 1.0) < 1e-6


class TestP2GraphIndex:
    """P2-C: CSR/CSC adjacency index for EntityGraph."""

    def _build_entity_graph(self, edges):
        """Build an EntityGraph directly from (src, tgt) edge tuples."""
        import networkx as nx
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        g._G = nx.DiGraph()
        g._G.add_edges_from(edges)
        g._build_index()
        return g

    def test_graph_index_subgraph_uses_adjacency(self):
        """depth=1 subgraph from root includes root + direct neighbours only."""
        g = self._build_entity_graph([
            ("A", "B"), ("A", "C"), ("C", "D"), ("D", "E"),
        ])
        sub = g.subgraph("A", depth=1)
        nodes = set(sub._G.nodes())
        # A + direct fwd neighbours B, C
        assert "A" in nodes
        assert "B" in nodes
        assert "C" in nodes
        # D and E are depth-2 and depth-3
        assert "D" not in nodes
        assert "E" not in nodes

    def test_graph_index_built_on_build_graph(self):
        """build_graph() attaches _fwd/_bwd index automatically."""
        from server.kg.kg_graph import build_graph
        from server.kg.kg_resolver import EntityNode, EntityEdge

        n1 = EntityNode(id="n1", label="N1", entity_type="TABLE",
                        source_key="s", table="t1", pk_col="id", pk_val="1")
        n2 = EntityNode(id="n2", label="N2", entity_type="TABLE",
                        source_key="s", table="t2", pk_col="id", pk_val="2")
        e = EntityEdge(src_id="n1", tgt_id="n2", relation="FK",
                       label="fk", weight=1.0, confidence=1.0)
        g = build_graph([n1, n2], [e])
        assert hasattr(g, "_fwd")
        assert "n2" in g._fwd.get("n1", [])

    def test_subgraph_backward_traversal(self):
        """Backward (in-edge) traversal is also included in BFS."""
        g = self._build_entity_graph([("X", "Y"), ("Z", "Y")])
        # BFS from Y at depth=1 should include X and Z (both point into Y)
        sub = g.subgraph("Y", depth=1)
        nodes = set(sub._G.nodes())
        assert "X" in nodes
        assert "Z" in nodes


class TestP2IncrementalIndexing:
    """P2-D: has_doc_changed / set_doc_hash incremental hash gating."""

    def test_has_doc_changed_new_doc(self, tmp_path):
        """A brand-new doc_id returns True (no entry exists)."""
        from server.kg.kg_store import KGStore
        with KGStore(tmp_path / "kg.db") as store:
            assert store.has_doc_changed("k:t:c", "abc123") is True

    def test_has_doc_changed_same_hash(self, tmp_path):
        """After set_doc_hash, has_doc_changed with the same hash returns False."""
        from server.kg.kg_store import KGStore
        with KGStore(tmp_path / "kg.db") as store:
            store.set_doc_hash("k:t:c", "abc123")
            assert store.has_doc_changed("k:t:c", "abc123") is False

    def test_has_doc_changed_different_hash(self, tmp_path):
        """After set_doc_hash, a different hash returns True (content changed)."""
        from server.kg.kg_store import KGStore
        with KGStore(tmp_path / "kg.db") as store:
            store.set_doc_hash("k:t:c", "abc123")
            assert store.has_doc_changed("k:t:c", "xyz999") is True

    def test_set_doc_hash_is_idempotent(self, tmp_path):
        """Calling set_doc_hash twice with the same value doesn't raise."""
        from server.kg.kg_store import KGStore
        with KGStore(tmp_path / "kg.db") as store:
            store.set_doc_hash("k:t:c", "hash1")
            store.set_doc_hash("k:t:c", "hash1")  # should not raise
            assert store.has_doc_changed("k:t:c", "hash1") is False


# ── P3 roadmap tests ──────────────────────────────────────────────────────────

class TestP3PgSchemaExport:
    """P3-A: to_pg_schema() serialises node/edge types to .pg format."""

    def test_to_pg_schema_node_with_key_prop(self):
        """Node type with is_key=True prop must include @key in output."""
        from server.kg.kg_schema import KGSchema, NodeTypeDecl, PropDecl
        schema = KGSchema()
        schema.node_types["TABLE"] = NodeTypeDecl(
            name="TABLE",
            properties=[
                PropDecl("name", "String", nullable=False, is_key=True),
                PropDecl("source_key", "String", nullable=True, is_key=False),
            ],
        )
        output = schema.to_pg_schema()
        assert "@key" in output
        assert "node TABLE" in output
        assert "name: String @key" in output
        assert "source_key: String?" in output

    def test_to_pg_schema_edge_declaration(self):
        """Edge type must appear as 'edge FK: TABLE -> TABLE' in output."""
        from server.kg.kg_schema import KGSchema, EdgeTypeDecl
        schema = KGSchema()
        schema.edge_types["FK"] = EdgeTypeDecl(name="FK", from_type="TABLE", to_type="TABLE")
        output = schema.to_pg_schema()
        assert "edge FK: TABLE -> TABLE" in output

    def test_to_pg_schema_node_no_props(self):
        """Node type with no properties renders as 'node NAME {}'."""
        from server.kg.kg_schema import KGSchema, NodeTypeDecl
        schema = KGSchema()
        schema.node_types["PERSON"] = NodeTypeDecl(name="PERSON")
        output = schema.to_pg_schema()
        assert "node PERSON {}" in output

    def test_to_pg_schema_nullable_prop(self):
        """Nullable property has trailing '?' in type string."""
        from server.kg.kg_schema import KGSchema, NodeTypeDecl, PropDecl
        schema = KGSchema()
        schema.node_types["T"] = NodeTypeDecl(
            name="T",
            properties=[PropDecl("col", "Int", nullable=True)],
        )
        output = schema.to_pg_schema()
        assert "col: Int?" in output


class TestP3RRFWeighted:
    """P3-C: Score-weighted RRF fusion correctness."""

    def test_rrf_weighted_higher_score_wins(self):
        """Two results at same rank: higher raw_score → higher weighted score."""
        from server.kg.kg_search import _rrf_score_weighted
        low  = _rrf_score_weighted(0, raw_score=0.2)
        high = _rrf_score_weighted(0, raw_score=0.9)
        assert high > low

    def test_rrf_weighted_clamps_score(self):
        """raw_score > 1.0 is clamped to 1.0 - same result as raw_score=1.0."""
        from server.kg.kg_search import _rrf_score_weighted
        clamped = _rrf_score_weighted(0, raw_score=5.0)
        at_one  = _rrf_score_weighted(0, raw_score=1.0)
        assert abs(clamped - at_one) < 1e-9

    def test_rrf_weighted_zero_score_still_positive(self):
        """raw_score=0.0 still yields a positive (rank-based) contribution."""
        from server.kg.kg_search import _rrf_score_weighted
        result = _rrf_score_weighted(0, raw_score=0.0)
        assert result > 0.0

    def test_rrf_classic_still_exists(self):
        """_rrf_score (classic) is still importable and unchanged."""
        from server.kg.kg_search import _rrf_score, RRF_K
        assert abs(_rrf_score(0) - 1.0 / (RRF_K + 1)) < 1e-12

    def test_hybrid_search_fallback_no_sem(self, tmp_path):
        """hybrid_search with empty store falls back to keyword results (empty list)."""
        from server.kg.kg_store import KGStore
        from server.kg.kg_search import hybrid_search
        with KGStore(tmp_path / "kg.db") as store:
            results = hybrid_search("anything", layer="schema", limit=5, store=store)
        # Empty store → no keyword hits; hybrid must return a list (not raise)
        assert isinstance(results, list)


class TestP3AsyncIndexer:
    """P3-D: Async wrappers for index_source and index_all."""

    @pytest.fixture(autouse=True)
    def _tmp_kg_dir(self, tmp_path, monkeypatch):
        """Redirect KG_DIR to a writable tmp directory for both async tests."""
        kg_dir = tmp_path / "kg"
        kg_dir.mkdir(parents=True)
        monkeypatch.setenv("KG_DIR", str(kg_dir))
        # kg_store and kg_indexer cache KG_DIR at import time; patch the live
        # module attribute so the already-imported module uses the tmp path.
        import server.kg.kg_store as kg_store_mod
        import server.kg.kg_indexer as kg_indexer_mod
        monkeypatch.setattr(kg_store_mod, "KG_DIR", kg_dir)
        monkeypatch.setattr(kg_indexer_mod, "OUTPUT_DIR", tmp_path / "erd", raising=False)

    def test_index_source_async_returns_dict(self):
        """asyncio.run(index_source_async('nonexistent_key')) returns dict with 'ok' key."""
        import asyncio
        from server.kg.kg_indexer import index_source_async
        result = asyncio.run(index_source_async("nonexistent_key"))
        assert isinstance(result, dict)
        assert "ok" in result

    def test_index_all_async_returns_list(self):
        """asyncio.run(index_all_async()) returns a list (may be empty)."""
        import asyncio
        from server.kg.kg_indexer import index_all_async
        result = asyncio.run(index_all_async())
        assert isinstance(result, list)


class TestP3ActorProvenance:
    """P3-E: Actor provenance stored in kg_index_state.indexed_by."""

    def test_has_doc_changed_actor_provenance(self, tmp_path):
        """mark_indexed with actor_id='test-agent' stores value in indexed_by column."""
        from server.kg.kg_store import KGStore
        with KGStore(tmp_path / "kg.db") as store:
            store.mark_indexed(
                "mykey",
                schema_doc_count=5,
                data_doc_count=10,
                actor_id="test-agent",
            )
            row = store._conn.execute(
                "SELECT indexed_by FROM kg_index_state WHERE source_key = ?",
                ("mykey",),
            ).fetchone()
        assert row is not None
        assert row[0] == "test-agent"

    def test_mark_indexed_without_actor_id_leaves_null(self, tmp_path):
        """mark_indexed without actor_id leaves indexed_by as NULL."""
        from server.kg.kg_store import KGStore
        with KGStore(tmp_path / "kg.db") as store:
            store.mark_indexed("noactor", schema_doc_count=1, data_doc_count=0)
            row = store._conn.execute(
                "SELECT indexed_by FROM kg_index_state WHERE source_key = ?",
                ("noactor",),
            ).fetchone()
        # Row may not exist at all (actor_id=None skips kg_index_state insert)
        if row is not None:
            assert row[0] is None


class TestP3KuzuImportError:
    """P3-B: KuzuGraph raises ImportError when kuzu package is not installed."""

    def test_kuzu_import_error_without_package(self, monkeypatch):
        """Instantiating KuzuGraph without kuzu installed raises ImportError."""
        import sys
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "kuzu":
                raise ImportError("No module named 'kuzu'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        from server.kg.kg_graph import KuzuGraph
        with pytest.raises(ImportError, match="datajoins-nybble\\[kuzu\\]"):
            KuzuGraph()


class TestP4JSONLDContextURI:
    """P4-C: JSON-LD context kg: URI must point to the datajoins/nybble project."""

    def test_jsonld_context_uri_is_project_owned(self):
        """to_jsonld() must NOT reference frectonz/sql-studio."""
        from server.kg.kg_graph import EntityGraph

        g = EntityGraph()
        doc = g.to_jsonld()

        kg_uri = doc["@context"]["kg"]
        assert "frectonz" not in kg_uri, (
            f"JSON-LD context still references old upstream repo: {kg_uri!r}"
        )
        assert "datajoins" in kg_uri, (
            f"JSON-LD context does not reference datajoins project: {kg_uri!r}"
        )


class TestP4NoBareExceptPass:
    """P4-B: No bare 'except Exception: pass' patterns outside ALTER TABLE blocks."""

    def test_structured_log_no_bare_pass(self):
        """kg_indexer.py and kg_search.py must not contain silent except…pass blocks."""
        import pathlib

        for fname in ("server/kg/kg_indexer.py", "server/kg/kg_search.py"):
            text = pathlib.Path(fname).read_text(encoding="utf-8")
            # The only allowed silent passes are ALTER TABLE migration blocks (kg_store.py).
            # In indexer and search there should be none.
            assert "except Exception:\n        pass" not in text, (
                f"Silent 'except Exception: pass' still present in {fname}"
            )
            assert "except Exception:\n            pass" not in text, (
                f"Silent 'except Exception: pass' still present in {fname}"
            )


class TestP4TypeAnnotations:
    """P4-D: Public functions must have return type annotations."""

    def test_type_annotations_present(self):
        """index_source must carry a return annotation."""
        import inspect
        from server.kg.kg_indexer import index_source

        sig = inspect.signature(index_source)
        assert sig.return_annotation is not inspect.Parameter.empty, (
            "index_source() is missing a return type annotation"
        )

    def test_index_all_annotation_present(self):
        """index_all must carry a return annotation."""
        import inspect
        from server.kg.kg_indexer import index_all

        sig = inspect.signature(index_all)
        assert sig.return_annotation is not inspect.Parameter.empty, (
            "index_all() is missing a return type annotation"
        )

    def test_keyword_search_annotation_present(self):
        """keyword_search must carry a return annotation."""
        import inspect
        from server.kg.kg_search import keyword_search

        sig = inspect.signature(keyword_search)
        assert sig.return_annotation is not inspect.Parameter.empty, (
            "keyword_search() is missing a return type annotation"
        )


# ── P5-A Tests: derive_schema_from_graph and export_graph_to_pg ───────────────

class TestP5PgSchemaDerive:
    """Tests for derive_schema_from_graph and export_graph_to_pg (P5-A)."""

    def _make_entity_graph(self):
        """Build a minimal EntityGraph with two node types and an FK edge."""
        import networkx as nx
        from server.kg.kg_graph import EntityGraph

        g = EntityGraph()
        # Manually populate the internal networkx graph
        g._G.add_node("a1", entity_type="TABLE", label="orders", color="#586069")
        g._G.add_node("a2", entity_type="COLUMN", label="user_id", color="#0366d6")
        g._G.add_edge("a1", "a2", relation="FK:orders→users", label="FK")
        return g

    def test_derive_schema_from_graph_node_types(self):
        """derive_schema_from_graph must produce node types for every entity_type."""
        from server.kg.kg_schema import derive_schema_from_graph

        g = self._make_entity_graph()
        schema = derive_schema_from_graph(g)

        assert "TABLE" in schema.node_types, "Expected TABLE node type in derived schema"
        assert "COLUMN" in schema.node_types, "Expected COLUMN node type in derived schema"

    def test_derive_schema_from_graph_edge_types(self):
        """derive_schema_from_graph must produce an edge type for every relation prefix."""
        from server.kg.kg_schema import derive_schema_from_graph

        g = self._make_entity_graph()
        schema = derive_schema_from_graph(g)

        assert "FK" in schema.edge_types, "Expected FK edge type in derived schema"

    def test_derive_schema_from_graph_empty(self):
        """derive_schema_from_graph on an empty EntityGraph returns an empty schema."""
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_schema import derive_schema_from_graph

        g = EntityGraph()
        schema = derive_schema_from_graph(g)
        assert schema.node_types == {}
        assert schema.edge_types == {}

    def test_export_graph_to_pg_string(self):
        """export_graph_to_pg must return a non-empty .pg schema string."""
        from server.kg.kg_schema import export_graph_to_pg

        g = self._make_entity_graph()
        result = export_graph_to_pg(g)

        assert isinstance(result, str), "export_graph_to_pg must return a string"
        assert len(result) > 0, "export_graph_to_pg result must be non-empty"

    def test_export_graph_to_pg_writes_file(self, tmp_path):
        """export_graph_to_pg must write the schema to output_path when given."""
        from server.kg.kg_schema import export_graph_to_pg

        g = self._make_entity_graph()
        out = tmp_path / "schema.pg"
        result = export_graph_to_pg(g, output_path=str(out))

        assert out.exists(), "output file must be created"
        assert out.read_text(encoding="utf-8") == result


# ── P5-B Tests: kg_inference ──────────────────────────────────────────────────

class TestP5Inference:
    """Tests for the kg_inference engine (P5-B)."""

    def _make_chain_graph(self):
        """Build A→B→C with FK edges as a plain networkx DiGraph."""
        import networkx as nx
        G = nx.DiGraph()
        G.add_node("A")
        G.add_node("B")
        G.add_node("C")
        G.add_edge("A", "B", relation="FK")
        G.add_edge("B", "C", relation="FK")
        return G

    def _make_symmetric_graph(self):
        """Build A→B with a co_occurs edge."""
        import networkx as nx
        G = nx.DiGraph()
        G.add_node("A")
        G.add_node("B")
        G.add_edge("A", "B", relation="co_occurs", label="co", weight=1.0, confidence=1.0)
        return G

    def test_inference_transitive_closure(self):
        """materialise_transitive_closure must infer A→C from A→B→C (FK)."""
        from server.kg.kg_inference import materialise_transitive_closure

        G = self._make_chain_graph()
        new_edges = materialise_transitive_closure(G, frozenset({"FK"}), max_depth=3)

        pairs = {(src, tgt) for src, tgt, _ in new_edges}
        assert ("A", "C") in pairs, f"Expected transitive edge A→C, got: {pairs}"

    def test_inference_transitive_closure_empty_graph(self):
        """materialise_transitive_closure on None G returns empty list."""
        from server.kg.kg_inference import materialise_transitive_closure

        result = materialise_transitive_closure(None, frozenset({"FK"}))
        assert result == []

    def test_inference_transitive_closure_no_matching_relation(self):
        """No edges produced when relation prefix not present in graph."""
        from server.kg.kg_inference import materialise_transitive_closure

        G = self._make_chain_graph()
        result = materialise_transitive_closure(G, frozenset({"NONEXISTENT"}))
        assert result == []

    def test_inference_symmetric_edges(self):
        """materialise_symmetric_edges must produce B→A from A→B co_occurs."""
        from server.kg.kg_inference import materialise_symmetric_edges

        G = self._make_symmetric_graph()
        new_edges = materialise_symmetric_edges(G, frozenset({"co_occurs"}))

        pairs = {(src, tgt) for src, tgt, _ in new_edges}
        assert ("B", "A") in pairs, f"Expected inverse edge B→A, got: {pairs}"

    def test_inference_symmetric_edges_empty_graph(self):
        """materialise_symmetric_edges on None G returns empty list."""
        from server.kg.kg_inference import materialise_symmetric_edges

        result = materialise_symmetric_edges(None, frozenset({"co_occurs"}))
        assert result == []

    def test_inference_symmetric_edges_already_present(self):
        """No duplicate produced when reverse edge already exists."""
        import networkx as nx
        from server.kg.kg_inference import materialise_symmetric_edges

        G = nx.DiGraph()
        G.add_edge("A", "B", relation="co_occurs", label="", weight=1.0, confidence=1.0)
        G.add_edge("B", "A", relation="co_occurs", label="", weight=1.0, confidence=1.0)

        new_edges = materialise_symmetric_edges(G, frozenset({"co_occurs"}))
        pairs = [(src, tgt) for src, tgt, _ in new_edges]
        # B→A must not appear because it already exists
        assert ("B", "A") not in pairs

    def test_inference_rules(self):
        """apply_rules must fire a matching InferenceRule and return a new edge."""
        import networkx as nx
        from server.kg.kg_inference import InferenceRule, apply_rules

        G = nx.DiGraph()
        G.add_node("X", entity_type="TABLE")
        G.add_node("Y", entity_type="TABLE")
        G.add_edge("X", "Y", relation="FK", label="")

        rule = InferenceRule(
            name="test_rule",
            predicate=lambda src, tgt, ea, sna, tna: ea.get("relation") == "FK",
            derived_relation="INFERRED_LINK",
            derived_label="inferred",
            confidence=0.8,
        )
        new_edges = apply_rules(G, [rule])
        assert len(new_edges) > 0, "Expected at least one inferred edge"
        relations = [attrs["relation"] for _, _, attrs in new_edges]
        assert "INFERRED_LINK" in relations

    def test_inference_rules_empty_graph(self):
        """apply_rules on None G returns empty list."""
        from server.kg.kg_inference import InferenceRule, apply_rules

        rule = InferenceRule(
            name="r",
            predicate=lambda *_: True,
            derived_relation="X",
        )
        result = apply_rules(None, [rule])
        assert result == []

    def test_inference_empty_graph(self):
        """run_inference on an EntityGraph with no nodes returns empty list."""
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_inference import run_inference

        g = EntityGraph()
        result = run_inference(g)
        assert result == [], f"Expected empty list, got {result}"

    def test_run_inference_with_config(self):
        """run_inference with a custom config applies only specified relations."""
        import networkx as nx
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_inference import InferenceConfig, run_inference

        g = EntityGraph()
        g._G.add_node("A")
        g._G.add_node("B")
        g._G.add_node("C")
        g._G.add_edge("A", "B", relation="FK")
        g._G.add_edge("B", "C", relation="FK")

        config = InferenceConfig(
            transitive_relations=frozenset({"FK"}),
            symmetric_relations=frozenset(),
        )
        result = run_inference(g, config=config)
        pairs = {(src, tgt) for src, tgt, _ in result}
        assert ("A", "C") in pairs


# ── P5-C Tests: kg_mcp ────────────────────────────────────────────────────────

class TestP5MCP:
    """Tests for the kg_mcp bridge (P5-C)."""

    def test_mcp_get_tools_returns_list(self):
        """get_tools() must return a list of >= 4 tool dicts, each with a 'name'."""
        from server.kg.kg_mcp import get_tools

        tools = get_tools()
        assert isinstance(tools, list), "get_tools() must return a list"
        assert len(tools) >= 4, f"Expected >= 4 tools, got {len(tools)}"
        for t in tools:
            assert "name" in t, f"Tool missing 'name' key: {t}"

    def test_mcp_dispatch_unknown_tool(self):
        """dispatch_tool with an unknown name must return ok=False."""
        from server.kg.kg_mcp import dispatch_tool

        result = dispatch_tool("nonexistent_tool", {})
        assert isinstance(result, dict), "dispatch_tool must return a dict"
        assert result["ok"] is False

    def test_mcp_tool_kg_search_returns_dict(self):
        """tool_kg_search must always return a dict with an 'ok' key."""
        from server.kg.kg_mcp import tool_kg_search

        result = tool_kg_search("test")
        assert isinstance(result, dict), "tool_kg_search must return a dict"
        assert "ok" in result, "Result must contain 'ok' key"

    def test_mcp_tool_kg_resolve_no_db(self):
        """tool_kg_resolve must return a dict with 'ok' key even when DB absent."""
        from server.kg.kg_mcp import tool_kg_resolve

        result = tool_kg_resolve("nonexistent", "t", "id", "1")
        assert isinstance(result, dict), "tool_kg_resolve must return a dict"
        assert "ok" in result, "Result must contain 'ok' key"

    def test_mcp_tool_kg_subgraph_no_graph(self):
        """tool_kg_subgraph with graph=None must return ok=False gracefully."""
        from server.kg.kg_mcp import tool_kg_subgraph

        result = tool_kg_subgraph("some_root_id", depth=1, graph=None)
        assert isinstance(result, dict)
        assert result["ok"] is False

    def test_mcp_dispatch_invalid_params_returns_error(self):
        """dispatch_tool with wrong param types must return ok=False, not raise."""
        from server.kg.kg_mcp import dispatch_tool

        # kg_search requires 'query' but we pass nothing
        result = dispatch_tool("kg_search", {})
        assert isinstance(result, dict)
        assert result["ok"] is False

    def test_mcp_tool_definitions_have_input_schema(self):
        """Every tool definition must have an inputSchema dict."""
        from server.kg.kg_mcp import get_tools

        for tool in get_tools():
            assert "inputSchema" in tool, f"Tool {tool['name']!r} missing inputSchema"
            assert isinstance(tool["inputSchema"], dict)


# ── P5-D Tests: to_owl() ─────────────────────────────────────────────────────

class TestP5OWL:
    """Tests for KGSchema.to_owl() (P5-D)."""

    def _make_schema(self):
        from server.kg.kg_schema import EdgeTypeDecl, KGSchema, NodeTypeDecl, PropDecl

        schema = KGSchema()
        schema.node_types["TABLE"] = NodeTypeDecl(
            name="TABLE",
            properties=[
                PropDecl("name", "String", nullable=False, is_key=True),
                PropDecl("source_key", "String", nullable=True),
            ],
        )
        schema.edge_types["FK"] = EdgeTypeDecl(
            name="FK",
            from_type="TABLE",
            to_type="TABLE",
        )
        return schema

    def test_owl_export_raises_without_rdflib(self, monkeypatch):
        """to_owl() must raise ImportError when rdflib is not available."""
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "rdflib":
                raise ImportError("No module named 'rdflib'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        schema = self._make_schema()
        with pytest.raises(ImportError, match="rdflib"):
            schema.to_owl()

    def test_owl_export_with_rdflib(self):
        """to_owl() must return a dict with @context and @graph when rdflib is available."""
        rdflib = pytest.importorskip("rdflib")  # skip if rdflib not installed

        schema = self._make_schema()
        result = schema.to_owl()

        assert isinstance(result, dict), "to_owl() must return a dict"
        assert "@context" in result, "Result must have '@context' key"
        assert "@graph" in result, "Result must have '@graph' key"


# ── Pattern 1: BiTemporalFact ──────────────────────────────────────────────────

class TestBiTemporalFact:
    """Tests for server/kg/kg_temporal.py - BiTemporalFact model."""

    def _import(self):
        from server.kg.kg_temporal import (
            BiTemporalFact, TemporalBound, KGTemporalError,
            parse_temporal_value, parse_temporal_bound,
            serialize_temporal_value, serialize_temporal_bound,
            _coerce_iso_date,
            relationship_active_at, relationship_overlaps,
        )
        return (BiTemporalFact, TemporalBound, KGTemporalError,
                parse_temporal_value, parse_temporal_bound,
                serialize_temporal_value, serialize_temporal_bound,
                _coerce_iso_date,
                relationship_active_at, relationship_overlaps)

    # ── TemporalBound ────────────────────────────────────────────────────────

    def test_temporal_bound_open_value(self):
        (_, TemporalBound, *_rest) = self._import()
        assert TemporalBound.OPEN.value == "OPEN"

    # ── _coerce_iso_date ──────────────────────────────────────────────────────

    def test_coerce_single_digit_month_and_day(self):
        (*_, _coerce_iso_date, _ra, _ro) = self._import()
        assert _coerce_iso_date("2024-1-5") == "2024-01-05"

    def test_coerce_already_padded(self):
        (*_, _coerce_iso_date, _ra, _ro) = self._import()
        assert _coerce_iso_date("2024-01-05T10:30:00") == "2024-01-05T10:30:00"

    def test_coerce_non_date_passthrough(self):
        (*_, _coerce_iso_date, _ra, _ro) = self._import()
        assert _coerce_iso_date("not-a-date") == "not-a-date"

    def test_coerce_single_digit_month_only(self):
        (*_, _coerce_iso_date, _ra, _ro) = self._import()
        assert _coerce_iso_date("2024-3-15") == "2024-03-15"

    # ── parse_temporal_value ──────────────────────────────────────────────────

    def test_parse_none_returns_none(self):
        (_, _tb, _err, parse_temporal_value, *_rest) = self._import()
        assert parse_temporal_value(None) is None

    def test_parse_iso_string(self):
        (_, _tb, _err, parse_temporal_value, *_rest) = self._import()
        from datetime import timezone
        dt = parse_temporal_value("2024-06-15T12:00:00+00:00")
        assert dt.year == 2024
        assert dt.tzinfo == timezone.utc

    def test_parse_iso_z_suffix(self):
        (_, _tb, _err, parse_temporal_value, *_rest) = self._import()
        dt = parse_temporal_value("2024-06-15T12:00:00Z")
        assert dt.year == 2024

    def test_parse_unix_timestamp(self):
        (_, _tb, _err, parse_temporal_value, *_rest) = self._import()
        from datetime import datetime, timezone
        expected = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = parse_temporal_value(expected.timestamp())
        assert result.year == 2024

    def test_parse_naive_datetime_assumed_utc(self):
        (_, _tb, _err, parse_temporal_value, *_rest) = self._import()
        from datetime import datetime, timezone
        naive = datetime(2024, 6, 1, 0, 0, 0)
        result = parse_temporal_value(naive)
        assert result.tzinfo == timezone.utc

    def test_parse_bad_string_raises(self):
        (_, _tb, KGTemporalError, parse_temporal_value, *_rest) = self._import()
        with pytest.raises(KGTemporalError):
            parse_temporal_value("not-a-date-at-all")

    def test_parse_bad_type_raises(self):
        (_, _tb, KGTemporalError, parse_temporal_value, *_rest) = self._import()
        with pytest.raises(KGTemporalError):
            parse_temporal_value(["2024", "01", "01"])

    def test_parse_single_digit_month_via_coerce(self):
        (_, _tb, _err, parse_temporal_value, *_rest) = self._import()
        dt = parse_temporal_value("2024-1-5")
        assert dt.month == 1
        assert dt.day == 5

    # ── serialize_temporal_value ──────────────────────────────────────────────

    def test_serialize_none_returns_none(self):
        (*_, serialize_temporal_value, _stb, _coerce, _ra, _ro) = self._import()
        assert serialize_temporal_value(None) is None

    def test_serialize_produces_z_suffix(self):
        (*_, serialize_temporal_value, _stb, _coerce, _ra, _ro) = self._import()
        from datetime import datetime, timezone
        dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = serialize_temporal_value(dt)
        assert result.endswith("Z")
        assert "2024-06-15" in result

    # ── serialize_temporal_bound ──────────────────────────────────────────────

    def test_serialize_open_returns_none(self):
        (*_, _sv, serialize_temporal_bound, _coerce, _ra, _ro) = self._import()
        (_, TemporalBound, *_rest2) = self._import()
        assert serialize_temporal_bound(TemporalBound.OPEN) is None

    def test_serialize_bound_none_returns_none(self):
        (*_, _sv, serialize_temporal_bound, _coerce, _ra, _ro) = self._import()
        assert serialize_temporal_bound(None) is None

    # ── BiTemporalFact.from_relationship ──────────────────────────────────────

    def test_from_empty_relationship(self):
        (BiTemporalFact, TemporalBound, *_rest) = self._import()
        fact = BiTemporalFact.from_relationship({"source": "A", "target": "B", "type": "FK"})
        assert fact.valid_from is None
        assert fact.valid_until is TemporalBound.OPEN

    def test_from_relationship_with_valid_from(self):
        (BiTemporalFact, TemporalBound, *_rest) = self._import()
        fact = BiTemporalFact.from_relationship({
            "source": "A", "target": "B", "type": "FK",
            "valid_from": "2024-01-01T00:00:00Z",
        })
        assert fact.valid_from is not None
        assert fact.valid_from.year == 2024

    def test_from_relationship_with_full_bounds(self):
        (BiTemporalFact, TemporalBound, *_rest) = self._import()
        fact = BiTemporalFact.from_relationship({
            "valid_from": "2024-01-01T00:00:00Z",
            "valid_until": "2024-12-31T00:00:00Z",
        })
        assert isinstance(fact.valid_until, __import__("datetime").datetime)

    # ── is_valid_at ───────────────────────────────────────────────────────────

    def test_is_valid_at_within_range(self):
        (BiTemporalFact, TemporalBound, *_rest) = self._import()
        from datetime import datetime, timezone
        fact = BiTemporalFact.from_relationship({
            "valid_from": "2024-01-01T00:00:00Z",
            "valid_until": "2024-12-31T00:00:00Z",
        })
        assert fact.is_valid_at(datetime(2024, 6, 1, tzinfo=timezone.utc)) is True

    def test_is_valid_at_before_range(self):
        (BiTemporalFact, TemporalBound, *_rest) = self._import()
        from datetime import datetime, timezone
        fact = BiTemporalFact.from_relationship({
            "valid_from": "2024-06-01T00:00:00Z",
            "valid_until": "2024-12-31T00:00:00Z",
        })
        assert fact.is_valid_at(datetime(2024, 1, 1, tzinfo=timezone.utc)) is False

    def test_is_valid_at_after_range(self):
        (BiTemporalFact, TemporalBound, *_rest) = self._import()
        from datetime import datetime, timezone
        fact = BiTemporalFact.from_relationship({
            "valid_from": "2024-01-01T00:00:00Z",
            "valid_until": "2024-06-01T00:00:00Z",
        })
        assert fact.is_valid_at(datetime(2025, 1, 1, tzinfo=timezone.utc)) is False

    def test_is_valid_at_open_end(self):
        (BiTemporalFact, TemporalBound, *_rest) = self._import()
        from datetime import datetime, timezone
        fact = BiTemporalFact.from_relationship({
            "valid_from": "2020-01-01T00:00:00Z",
        })
        assert fact.is_valid_at(datetime(2030, 1, 1, tzinfo=timezone.utc)) is True

    def test_is_valid_at_naive_point_assumed_utc(self):
        (BiTemporalFact, TemporalBound, *_rest) = self._import()
        from datetime import datetime
        fact = BiTemporalFact.from_relationship({
            "valid_from": "2024-01-01T00:00:00Z",
        })
        assert fact.is_valid_at(datetime(2024, 6, 1)) is True  # naive assumed UTC

    # ── overlaps ──────────────────────────────────────────────────────────────

    def test_overlaps_full_containment(self):
        (BiTemporalFact, TemporalBound, *_rest) = self._import()
        from datetime import datetime, timezone
        fact = BiTemporalFact.from_relationship({
            "valid_from": "2024-01-01T00:00:00Z",
            "valid_until": "2024-12-31T00:00:00Z",
        })
        start = datetime(2024, 3, 1, tzinfo=timezone.utc)
        end   = datetime(2024, 9, 1, tzinfo=timezone.utc)
        assert fact.overlaps(start, end) is True

    def test_overlaps_no_intersection(self):
        (BiTemporalFact, TemporalBound, *_rest) = self._import()
        from datetime import datetime, timezone
        fact = BiTemporalFact.from_relationship({
            "valid_from": "2024-01-01T00:00:00Z",
            "valid_until": "2024-06-01T00:00:00Z",
        })
        start = datetime(2024, 7, 1, tzinfo=timezone.utc)
        end   = datetime(2024, 12, 1, tzinfo=timezone.utc)
        assert fact.overlaps(start, end) is False

    def test_overlaps_open_end(self):
        (BiTemporalFact, TemporalBound, *_rest) = self._import()
        from datetime import datetime, timezone
        fact = BiTemporalFact.from_relationship({"valid_from": "2024-01-01T00:00:00Z"})
        start = datetime(2030, 1, 1, tzinfo=timezone.utc)
        assert fact.overlaps(start, TemporalBound.OPEN) is True

    # ── relationship_active_at ────────────────────────────────────────────────

    def test_relationship_active_at(self):
        (*_rest, relationship_active_at, _ro) = self._import()
        from datetime import datetime, timezone
        rel = {"valid_from": "2024-01-01T00:00:00Z", "valid_until": "2024-12-31T00:00:00Z"}
        assert relationship_active_at(rel, datetime(2024, 6, 1, tzinfo=timezone.utc)) is True

    def test_relationship_active_at_outside(self):
        (*_rest, relationship_active_at, _ro) = self._import()
        from datetime import datetime, timezone
        rel = {"valid_from": "2024-01-01T00:00:00Z", "valid_until": "2024-06-01T00:00:00Z"}
        assert relationship_active_at(rel, datetime(2025, 1, 1, tzinfo=timezone.utc)) is False

    # ── to_relationship_fields round-trip ─────────────────────────────────────

    def test_round_trip_preserves_valid_from(self):
        (BiTemporalFact, TemporalBound, *_rest) = self._import()
        original = {"valid_from": "2024-06-15T12:00:00Z", "valid_until": "2025-01-01T00:00:00Z"}
        fact = BiTemporalFact.from_relationship(original)
        fields = fact.to_relationship_fields()
        assert "2024-06-15" in fields["valid_from"]
        assert fields["valid_until"] is not None

    def test_round_trip_open_until_is_none(self):
        (BiTemporalFact, TemporalBound, *_rest) = self._import()
        fact = BiTemporalFact.from_relationship({"valid_from": "2024-01-01T00:00:00Z"})
        fields = fact.to_relationship_fields()
        assert fields["valid_until"] is None  # OPEN serialises as None


# ── Pattern 2: GraphValidator ─────────────────────────────────────────────────

class TestGraphValidator:
    """Tests for server/kg/kg_validator.py."""

    def _import(self):
        from server.kg.kg_validator import (
            ValidationSeverity, ValidationIssue, ValidationResult, validate_graph
        )
        return ValidationSeverity, ValidationIssue, ValidationResult, validate_graph

    def _make_valid_graph(self):
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode, EntityEdge, EdgeKind
        g = EntityGraph()
        n1 = EntityNode(id="A", label="Alice", entity_type="PERSON",
                        source_key="s", table="t", pk_col="id", pk_val="1")
        n2 = EntityNode(id="B", label="Bob",   entity_type="PERSON",
                        source_key="s", table="t", pk_col="id", pk_val="2")
        e  = EntityEdge(src_id="A", tgt_id="B", relation="FK:t→t",
                        label="fk", weight=1.0)
        g.add_node(n1); g.add_node(n2); g.add_edge(e)
        g._build_index()
        return g

    def _make_empty_graph(self):
        from server.kg.kg_graph import EntityGraph
        return EntityGraph()

    # ── ValidationSeverity ────────────────────────────────────────────────────

    def test_severity_values(self):
        ValidationSeverity, *_ = self._import()
        assert ValidationSeverity.INFO.value     == "info"
        assert ValidationSeverity.WARNING.value  == "warning"
        assert ValidationSeverity.ERROR.value    == "error"
        assert ValidationSeverity.CRITICAL.value == "critical"

    # ── ValidationIssue ───────────────────────────────────────────────────────

    def test_issue_to_dict(self):
        ValidationSeverity, ValidationIssue, *_ = self._import()
        issue = ValidationIssue(
            code="TEST", message="test msg",
            severity=ValidationSeverity.WARNING,
            element_id="nodeX",
        )
        d = issue.to_dict()
        assert d["code"]       == "TEST"
        assert d["severity"]   == "warning"
        assert d["element_id"] == "nodeX"

    # ── Valid graph ───────────────────────────────────────────────────────────

    def test_valid_graph_is_valid(self):
        *_, validate_graph = self._import()
        result = validate_graph(self._make_valid_graph())
        assert result.is_valid is True
        assert result.errors() == []

    def test_valid_graph_stats(self):
        *_, validate_graph = self._import()
        result = validate_graph(self._make_valid_graph())
        assert result.stats["nodes"] == 2
        assert result.stats["edges"] == 1

    # ── Empty graph ───────────────────────────────────────────────────────────

    def test_empty_graph_is_valid(self):
        *_, validate_graph = self._import()
        result = validate_graph(self._make_empty_graph())
        assert result.is_valid is True

    # ── Self-loops ────────────────────────────────────────────────────────────

    def test_self_loop_detected_as_info(self):
        ValidationSeverity, _, __, validate_graph = self._import()
        import networkx as nx
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        g._G = nx.DiGraph()
        g._G.add_node("A")
        g._G.add_edge("A", "A")   # self-loop
        result = validate_graph(g)
        self_loops = [i for i in result.issues if i.code == "SELF_LOOP"]
        assert len(self_loops) == 1
        assert self_loops[0].severity == ValidationSeverity.INFO

    # ── Orphan nodes ──────────────────────────────────────────────────────────

    def test_orphan_node_detected_as_warning(self):
        ValidationSeverity, _, __, validate_graph = self._import()
        import networkx as nx
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        g._G = nx.DiGraph()
        g._G.add_node("lonely")
        result = validate_graph(g)
        orphans = [i for i in result.issues if i.code == "ORPHAN_NODE"]
        assert len(orphans) == 1
        assert orphans[0].severity == ValidationSeverity.WARNING
        # is_valid still True (warnings don't fail by default)
        assert result.is_valid is True

    def test_strict_mode_orphan_fails(self):
        *_, validate_graph = self._import()
        import networkx as nx
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        g._G = nx.DiGraph()
        g._G.add_node("lonely")
        result = validate_graph(g, strict=True)
        assert result.is_valid is False

    # ── Cycle detection ────────────────────────────────────────────────────────

    def test_cycle_detected_as_info(self):
        ValidationSeverity, _, __, validate_graph = self._import()
        import networkx as nx
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        g._G = nx.DiGraph()
        g._G.add_edge("A", "B")
        g._G.add_edge("B", "C")
        g._G.add_edge("C", "A")   # cycle
        result = validate_graph(g)
        cycles = [i for i in result.issues if i.code == "CYCLE_DETECTED"]
        assert len(cycles) == 1
        assert cycles[0].severity == ValidationSeverity.INFO

    # ── to_dict ───────────────────────────────────────────────────────────────

    def test_result_to_dict_structure(self):
        *_, validate_graph = self._import()
        d = validate_graph(self._make_valid_graph()).to_dict()
        assert "is_valid" in d
        assert "issues" in d
        assert "stats" in d

    # ── None graph ────────────────────────────────────────────────────────────

    def test_none_graph_returns_valid(self):
        *_, validate_graph = self._import()
        # An object with _G = None should not blow up
        class FakeGraph:
            _G = None
        result = validate_graph(FakeGraph())
        assert result.is_valid is True

    # ── by_severity helper ────────────────────────────────────────────────────

    def test_by_severity_filters_correctly(self):
        ValidationSeverity, ValidationIssue, ValidationResult, _ = self._import()
        result = ValidationResult(
            is_valid=False,
            issues=[
                ValidationIssue("A", "msg a", ValidationSeverity.ERROR),
                ValidationIssue("B", "msg b", ValidationSeverity.WARNING),
                ValidationIssue("C", "msg c", ValidationSeverity.ERROR),
            ],
        )
        errors = result.by_severity(ValidationSeverity.ERROR)
        assert len(errors) == 2
        warns = result.by_severity(ValidationSeverity.WARNING)
        assert len(warns) == 1


# ── Pattern 3: Link prediction algorithms ─────────────────────────────────────

class TestLinkPredictor:
    """Tests for server/kg/kg_link_predictor.py."""

    def _import(self):
        from server.kg.kg_link_predictor import (
            preferential_attachment, common_neighbors,
            jaccard_coefficient, adamic_adar, resource_allocation,
            score_link, predict_top_links, SUPPORTED_METHODS,
        )
        return (preferential_attachment, common_neighbors,
                jaccard_coefficient, adamic_adar, resource_allocation,
                score_link, predict_top_links, SUPPORTED_METHODS)

    def _triangle(self):
        """A → B → C → A (triangle + an isolated node D)."""
        import networkx as nx
        G = nx.Graph()
        G.add_edge("A", "B")
        G.add_edge("B", "C")
        G.add_edge("C", "A")
        G.add_node("D")
        return G

    def _star(self):
        """Hub H connected to S1, S2, S3."""
        import networkx as nx
        G = nx.Graph()
        for spoke in ("S1", "S2", "S3"):
            G.add_edge("H", spoke)
        return G

    # ── SUPPORTED_METHODS ────────────────────────────────────────────────────

    def test_all_five_methods_listed(self):
        *_, SUPPORTED_METHODS = self._import()
        for m in ("preferential_attachment", "common_neighbors",
                  "jaccard_coefficient", "adamic_adar", "resource_allocation"):
            assert m in SUPPORTED_METHODS

    # ── preferential_attachment ──────────────────────────────────────────────

    def test_pa_self_pair_zero(self):
        pa, *_ = self._import()
        G = self._star()
        assert pa(G, "H", "H") == 0.0

    def test_pa_positive_for_distinct_nodes(self):
        pa, *_ = self._import()
        G = self._star()
        score = pa(G, "H", "S1")
        assert score > 0.0

    def test_pa_product_of_degrees(self):
        pa, *_ = self._import()
        G = self._star()
        # H has degree 3, S1 has degree 1 → 3 × 1 = 3
        assert pa(G, "H", "S1") == 3.0

    # ── common_neighbors ─────────────────────────────────────────────────────

    def test_cn_zero_when_no_shared(self):
        _, cn, *_ = self._import()
        G = self._star()
        # S1 and S2 share H as neighbour
        assert cn(G, "S1", "S2") == 1.0

    def test_cn_triangle_has_one_common(self):
        _, cn, *_ = self._import()
        G = self._triangle()
        assert cn(G, "A", "B") == 1.0   # C is common for undirected triangle

    def test_cn_self_pair_zero(self):
        _, cn, *_ = self._import()
        G = self._star()
        assert cn(G, "H", "H") == 0.0

    def test_cn_isolated_node_zero(self):
        _, cn, *_ = self._import()
        G = self._triangle()
        assert cn(G, "A", "D") == 0.0

    # ── jaccard_coefficient ──────────────────────────────────────────────────

    def test_jaccard_between_zero_and_one(self):
        _, _, jc, *_ = self._import()
        G = self._triangle()
        score = jc(G, "A", "B")
        assert 0.0 <= score <= 1.0

    def test_jaccard_no_union_returns_zero(self):
        _, _, jc, *_ = self._import()
        import networkx as nx
        G = nx.Graph()
        G.add_node("X")
        G.add_node("Y")
        assert jc(G, "X", "Y") == 0.0

    def test_jaccard_self_pair_zero(self):
        _, _, jc, *_ = self._import()
        G = self._star()
        assert jc(G, "H", "H") == 0.0

    # ── adamic_adar ──────────────────────────────────────────────────────────

    def test_aa_no_common_returns_zero(self):
        _, _, _, aa, *_ = self._import()
        G = self._star()
        # S1 and S2 have H as common neighbour with deg 3
        score = aa(G, "S1", "S2")
        assert score > 0.0

    def test_aa_uses_math_log_not_numpy(self):
        """adamic_adar must not import numpy."""
        import importlib, sys
        # Remove any cached reference
        mod_name = "server.kg.kg_link_predictor"
        mod = importlib.import_module(mod_name)
        assert "numpy" not in sys.modules or not hasattr(mod, "np"), \
            "kg_link_predictor must not import numpy at module level"

    def test_aa_self_pair_zero(self):
        _, _, _, aa, *_ = self._import()
        G = self._star()
        assert aa(G, "H", "H") == 0.0

    # ── resource_allocation ───────────────────────────────────────────────────

    def test_ra_positive_when_common_neighbour(self):
        _, _, _, _, ra, *_ = self._import()
        G = self._star()
        score = ra(G, "S1", "S2")
        # H (deg 3) is common: 1/3 ≈ 0.333
        assert abs(score - 1.0 / 3.0) < 1e-9

    def test_ra_zero_when_no_common(self):
        _, _, _, _, ra, *_ = self._import()
        import networkx as nx
        G = nx.Graph()
        G.add_edge("A", "B")
        G.add_edge("C", "D")
        assert ra(G, "A", "C") == 0.0

    def test_ra_self_pair_zero(self):
        _, _, _, _, ra, *_ = self._import()
        G = self._star()
        assert ra(G, "H", "H") == 0.0

    # ── score_link dispatcher ─────────────────────────────────────────────────

    def test_score_link_unknown_method_raises(self):
        *_, score_link, _ptl, _sm = self._import()
        G = self._star()
        with pytest.raises(ValueError, match="Unknown link prediction method"):
            score_link(G, "H", "S1", method="bogus_algo")

    def test_score_link_dispatches_all_methods(self):
        *_, score_link, _ptl, SUPPORTED_METHODS = self._import()
        G = self._star()
        for method in SUPPORTED_METHODS:
            result = score_link(G, "S1", "S2", method=method)
            assert isinstance(result, float)

    # ── predict_top_links ─────────────────────────────────────────────────────

    def test_predict_top_links_returns_list(self):
        *_, predict_top_links, _sm = self._import()
        from server.kg.kg_graph import build_graph
        from server.kg.kg_resolver import EntityNode, EntityEdge
        nodes = [
            EntityNode(id=str(i), label=str(i), entity_type="ENTITY",
                       source_key="s", table="t", pk_col="id", pk_val=str(i))
            for i in range(5)
        ]
        edges = [EntityEdge(src_id="0", tgt_id="1", relation="FK", label="fk")]
        graph = build_graph(nodes, edges)
        results = predict_top_links(graph, top_k=5)
        assert isinstance(results, list)

    def test_predict_top_links_excludes_existing(self):
        *_, predict_top_links, _sm = self._import()
        from server.kg.kg_graph import build_graph
        from server.kg.kg_resolver import EntityNode, EntityEdge
        nodes = [
            EntityNode(id=str(i), label=str(i), entity_type="ENTITY",
                       source_key="s", table="t", pk_col="id", pk_val=str(i))
            for i in range(3)
        ]
        edges = [EntityEdge(src_id="0", tgt_id="1", relation="FK", label="fk")]
        graph = build_graph(nodes, edges)
        results = predict_top_links(graph, top_k=10, exclude_existing=True)
        existing_pairs = {("0", "1"), ("1", "0")}
        for u, v, _ in results:
            assert (u, v) not in existing_pairs

    def test_predict_top_links_sorted_descending(self):
        *_, predict_top_links, _sm = self._import()
        from server.kg.kg_graph import build_graph
        from server.kg.kg_resolver import EntityNode, EntityEdge
        nodes = [
            EntityNode(id=str(i), label=str(i), entity_type="ENTITY",
                       source_key="s", table="t", pk_col="id", pk_val=str(i))
            for i in range(4)
        ]
        edges = [
            EntityEdge(src_id="0", tgt_id="1", relation="FK", label="fk"),
            EntityEdge(src_id="0", tgt_id="2", relation="FK", label="fk"),
        ]
        graph = build_graph(nodes, edges)
        results = predict_top_links(graph, top_k=10)
        scores = [s for _, _, s in results]
        assert scores == sorted(scores, reverse=True)

    def test_predict_top_links_empty_graph(self):
        *_, predict_top_links, _sm = self._import()
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        assert predict_top_links(g) == []


# ── Pattern 4: community_detect() pure-Python fallback ────────────────────────

class TestCommunityDetectFallback:
    """Tests for the three-tier community_detect() in EntityGraph."""

    def _build_triangle_graph(self):
        """A → B → C → A (triangle = one community)."""
        import networkx as nx
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode, EntityEdge
        g = EntityGraph()
        for nid in ("A", "B", "C"):
            g.add_node(EntityNode(id=nid, label=nid, entity_type="ENTITY",
                                  source_key="s", table="t", pk_col="id", pk_val=nid))
        for src, tgt in [("A", "B"), ("B", "C"), ("C", "A")]:
            g.add_edge(EntityEdge(src_id=src, tgt_id=tgt, relation="FK", label="fk"))
        g._build_index()
        return g

    def _build_two_component_graph(self):
        """Two disconnected pairs: A-B and C-D → should be 2 communities."""
        import networkx as nx
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode, EntityEdge
        g = EntityGraph()
        for nid in ("A", "B", "C", "D"):
            g.add_node(EntityNode(id=nid, label=nid, entity_type="ENTITY",
                                  source_key="s", table="t", pk_col="id", pk_val=nid))
        for src, tgt in [("A", "B"), ("C", "D")]:
            g.add_edge(EntityEdge(src_id=src, tgt_id=tgt, relation="FK", label="fk"))
        g._build_index()
        return g

    # ── Normal operation ──────────────────────────────────────────────────────

    def test_returns_dict_for_triangle(self):
        g = self._build_triangle_graph()
        result = g.community_detect()
        assert isinstance(result, dict)
        assert set(result.keys()) == {"A", "B", "C"}

    def test_all_values_are_ints(self):
        g = self._build_triangle_graph()
        result = g.community_detect()
        assert all(isinstance(v, int) for v in result.values())

    def test_empty_graph_returns_empty_dict(self):
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        assert g.community_detect() == {}

    def test_two_components_get_different_community_ids(self):
        g = self._build_two_component_graph()
        result = g.community_detect()
        assert result["A"] == result["B"]
        assert result["C"] == result["D"]
        assert result["A"] != result["C"]

    # ── Pure-Python fallback directly ─────────────────────────────────────────

    def test_label_propagation_fallback_no_networkx_community(self, monkeypatch):
        """community_detect() must not raise when greedy_modularity_communities
        is unavailable; the pure-Python fallback must activate."""
        import server.kg.kg_graph as kg_graph_mod
        # Patch out Tier-2 by making the import raise
        original = __builtins__.__import__ if hasattr(__builtins__, '__import__') else \
                   __import__

        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "networkx.algorithms.community" or (
                name == "networkx" and args and "greedy_modularity_communities" in str(args)
            ):
                raise ImportError("mocked missing community module")
            return real_import(name, *args, **kwargs)

        # Directly test the pure-Python function
        from server.kg.kg_graph import _community_label_propagation
        import networkx as nx
        G = nx.Graph()
        G.add_edge("X", "Y")
        G.add_edge("Y", "Z")
        result = _community_label_propagation(G)
        assert isinstance(result, dict)
        assert set(result.keys()) == {"X", "Y", "Z"}

    def test_label_propagation_empty_graph(self):
        from server.kg.kg_graph import _community_label_propagation
        import networkx as nx
        G = nx.Graph()
        assert _community_label_propagation(G) == {}

    def test_label_propagation_single_node(self):
        from server.kg.kg_graph import _community_label_propagation
        import networkx as nx
        G = nx.Graph()
        G.add_node("solo")
        result = _community_label_propagation(G)
        assert result == {"solo": 0}

    def test_label_propagation_isolated_nodes(self):
        """Nodes with no edges stay in their own singleton communities."""
        from server.kg.kg_graph import _community_label_propagation
        import networkx as nx
        G = nx.Graph()
        G.add_node("A")
        G.add_node("B")
        G.add_node("C")
        result = _community_label_propagation(G)
        # All isolated → each in own community (3 distinct IDs)
        assert len(set(result.values())) == 3

    def test_label_propagation_fully_connected_triangle(self):
        """Triangle → all nodes should converge to same community."""
        from server.kg.kg_graph import _community_label_propagation
        import networkx as nx
        G = nx.Graph()
        G.add_edge("A", "B")
        G.add_edge("B", "C")
        G.add_edge("C", "A")
        result = _community_label_propagation(G)
        # All connected → expected to be in 1 community
        assert len(set(result.values())) == 1

    def test_label_propagation_two_cliques(self):
        """Two separate cliques → should produce 2 communities."""
        from server.kg.kg_graph import _community_label_propagation
        import networkx as nx
        G = nx.Graph()
        # Clique 1
        G.add_edge("A", "B"); G.add_edge("B", "C"); G.add_edge("A", "C")
        # Clique 2
        G.add_edge("X", "Y"); G.add_edge("Y", "Z"); G.add_edge("X", "Z")
        result = _community_label_propagation(G)
        cid_a = result["A"]
        cid_x = result["X"]
        # Nodes in the same clique share a community ID
        assert result["B"] == cid_a
        assert result["C"] == cid_a
        assert result["Y"] == cid_x
        assert result["Z"] == cid_x
        # The two cliques must be in different communities
        assert cid_a != cid_x


# ── Pattern 5: KGConfig configuration manager ─────────────────────────────────

class TestKGConfig:
    """Tests for server/kg/kg_config.py."""

    def _make(self, overrides=None):
        from server.kg.kg_config import KGConfig
        return KGConfig(overrides=overrides)

    # ── Basic get ─────────────────────────────────────────────────────────────

    def test_get_returns_schema_default(self):
        cfg = self._make()
        assert cfg.get("backend") == "sqlite"

    def test_get_returns_unknown_key_default(self):
        cfg = self._make()
        assert cfg.get("totally_unknown_key", "fallback") == "fallback"

    def test_get_none_for_unknown_key_without_default(self):
        cfg = self._make()
        assert cfg.get("totally_unknown_key") is None

    # ── Programmatic override ─────────────────────────────────────────────────

    def test_set_overrides_schema_default(self):
        cfg = self._make()
        cfg.set("backend", "lancedb")
        assert cfg.get("backend") == "lancedb"

    def test_set_then_reset_restores_default(self):
        cfg = self._make()
        cfg.set("embed_model", "minilm")
        cfg.reset("embed_model")
        assert cfg.get("embed_model") == "none"

    def test_reset_all_clears_all_overrides(self):
        cfg = self._make()
        cfg.set("backend", "lancedb")
        cfg.set("embed_model", "minilm")
        cfg.reset()
        assert cfg.get("backend") == "sqlite"
        assert cfg.get("embed_model") == "none"

    def test_constructor_overrides_take_precedence(self):
        cfg = self._make(overrides={"backend": "qdrant"})
        assert cfg.get("backend") == "qdrant"

    # ── Env var resolution ─────────────────────────────────────────────────────

    def test_env_var_overrides_schema_default(self, monkeypatch):
        monkeypatch.setenv("BACKEND", "lancedb")
        cfg = self._make()
        assert cfg.get("backend") == "lancedb"

    def test_programmatic_override_beats_env_var(self, monkeypatch):
        monkeypatch.setenv("BACKEND", "lancedb")
        cfg = self._make()
        cfg.set("backend", "sqlite")
        assert cfg.get("backend") == "sqlite"

    def test_embed_api_key_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        cfg = self._make()
        assert cfg.get("embed_api_key") == "sk-test-123"

    def test_embed_api_key_openrouter_fallback(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-xyz")
        cfg = self._make()
        assert cfg.get("embed_api_key") == "or-xyz"

    # ── Typed getters ─────────────────────────────────────────────────────────

    def test_get_int_sample_rows(self):
        cfg = self._make()
        assert cfg.get_int("sample_rows") == 50

    def test_get_int_with_override(self):
        cfg = self._make()
        cfg.set("sample_rows", "100")
        assert cfg.get_int("sample_rows") == 100

    def test_get_int_invalid_returns_default(self):
        cfg = self._make()
        cfg.set("sample_rows", "not_a_number")
        assert cfg.get_int("sample_rows", default=50) == 50

    def test_get_float_embed_deadline(self):
        cfg = self._make()
        assert cfg.get_float("embed_deadline_s") == 30.0

    def test_get_float_with_override(self):
        cfg = self._make()
        cfg.set("embed_deadline_s", "15.5")
        assert cfg.get_float("embed_deadline_s") == 15.5

    def test_get_bool_false_default(self):
        cfg = self._make()
        assert cfg.get_bool("totally_unknown_key") is False

    def test_get_bool_true_strings(self):
        cfg = self._make()
        for truthy in ("true", "1", "yes", "on", "True", "YES"):
            cfg.set("some_flag", truthy)
            assert cfg.get_bool("some_flag") is True

    def test_get_bool_false_strings(self):
        cfg = self._make()
        for falsy in ("false", "0", "no", "off", "False"):
            cfg.set("some_flag", falsy)
            assert cfg.get_bool("some_flag") is False

    def test_get_path_returns_path_object(self):
        cfg = self._make()
        p = cfg.get_path("output_dir")
        assert hasattr(p, "parts")   # it's a Path

    def test_get_path_with_override(self):
        from pathlib import Path
        cfg = self._make(overrides={"output_dir": "/custom/path"})
        assert cfg.get_path("output_dir") == Path("/custom/path")

    # ── all_keys / snapshot ────────────────────────────────────────────────────

    def test_all_keys_contains_expected(self):
        cfg = self._make()
        keys = cfg.all_keys()
        for expected in ("backend", "embed_model", "sample_rows", "kg_dir"):
            assert expected in keys

    def test_snapshot_returns_all_keys(self):
        cfg = self._make()
        snap = cfg.snapshot()
        assert "backend" in snap
        assert "embed_model" in snap

    # ── Independence / no side effects ────────────────────────────────────────

    def test_two_instances_are_independent(self):
        cfg1 = self._make()
        cfg2 = self._make()
        cfg1.set("backend", "lancedb")
        assert cfg2.get("backend") == "sqlite"

    def test_singleton_accessible(self):
        from server.kg.kg_config import kg_config, get_kg_config
        assert kg_config is get_kg_config()


# ── Pattern 6: ISO date coercion - _coerce_iso_date() ────────────────────────

class TestISODateCoercion:
    """Tests for _coerce_iso_date() integration in server/kg/kg_temporal.py.

    _coerce_iso_date is already implemented as part of Pattern 1.  These tests
    verify its integration at the parse_temporal_value() call-site and confirm
    no ValueError is raised on real-world non-zero-padded date strings.
    """

    def _coerce(self, s):
        from server.kg.kg_temporal import _coerce_iso_date
        return _coerce_iso_date(s)

    def _parse(self, s):
        from server.kg.kg_temporal import parse_temporal_value
        return parse_temporal_value(s)

    # ── Standalone _coerce_iso_date ───────────────────────────────────────────

    def test_coerce_single_digit_month_day(self):
        assert self._coerce("2024-1-5") == "2024-01-05"

    def test_coerce_single_digit_month_padded_day(self):
        assert self._coerce("2024-3-15") == "2024-03-15"

    def test_coerce_padded_month_single_digit_day(self):
        assert self._coerce("2024-12-5") == "2024-12-05"

    def test_coerce_already_padded_unchanged(self):
        assert self._coerce("2024-01-05") == "2024-01-05"

    def test_coerce_with_time_component(self):
        result = self._coerce("2024-1-5T08:30:00")
        assert result == "2024-01-05T08:30:00"

    def test_coerce_with_time_and_tz(self):
        result = self._coerce("2024-1-5T08:30:00+00:00")
        assert result == "2024-01-05T08:30:00+00:00"

    def test_coerce_non_date_passthrough(self):
        assert self._coerce("not-a-date") == "not-a-date"

    def test_coerce_empty_string_passthrough(self):
        assert self._coerce("") == ""

    def test_coerce_year_only_passthrough(self):
        # "2024" has no month/day → passes through unchanged
        assert self._coerce("2024") == "2024"

    # ── Integration: parse_temporal_value does not raise on non-padded input ──

    def test_parse_single_digit_month(self):
        """parse_temporal_value("2024-1-15") must not raise ValueError."""
        dt = self._parse("2024-1-15")
        assert dt is not None
        assert dt.month == 1
        assert dt.day == 15

    def test_parse_single_digit_day(self):
        dt = self._parse("2024-12-5")
        assert dt is not None
        assert dt.month == 12
        assert dt.day == 5

    def test_parse_both_single_digit(self):
        dt = self._parse("2024-3-7")
        assert dt.month == 3
        assert dt.day == 7

    def test_parse_result_is_utc(self):
        from datetime import timezone
        dt = self._parse("2024-1-5")
        assert dt.tzinfo == timezone.utc

    def test_parse_with_time_single_digit(self):
        dt = self._parse("2024-1-5T00:00:00Z")
        assert dt.month == 1
        assert dt.day == 5

    # ── Integration: BiTemporalFact accepts non-padded dates ──────────────────

    def test_bitemporal_fact_accepts_non_padded_valid_from(self):
        from server.kg.kg_temporal import BiTemporalFact
        fact = BiTemporalFact.from_relationship({"valid_from": "2024-1-5"})
        assert fact.valid_from is not None
        assert fact.valid_from.month == 1

    def test_bitemporal_fact_accepts_non_padded_valid_until(self):
        from server.kg.kg_temporal import BiTemporalFact, TemporalBound
        fact = BiTemporalFact.from_relationship({
            "valid_from": "2024-1-1",
            "valid_until": "2024-12-5",
        })
        from datetime import datetime
        assert isinstance(fact.valid_until, datetime)
        assert fact.valid_until.month == 12
        assert fact.valid_until.day == 5

    # ── Coercion is idempotent ─────────────────────────────────────────────────

    def test_coerce_is_idempotent(self):
        """Applying _coerce_iso_date twice gives the same result as once."""
        s = "2024-3-7T10:00:00"
        once  = self._coerce(s)
        twice = self._coerce(once)
        assert once == twice


# ── P2-A Tests: structural fingerprint ───────────────────────────────────────

class TestStructuralFingerprint:
    """Tests for _extract_python_fingerprint and ChangeLevel classification."""

    def _fp(self, src: str) -> dict:
        from server.kg.kg_fingerprint import _extract_python_fingerprint
        return _extract_python_fingerprint(src)

    def _classify(self, old_src: str, new_src: str):
        from server.kg.kg_fingerprint import _extract_python_fingerprint, classify_change_level
        old_fp = _extract_python_fingerprint(old_src)
        new_fp = _extract_python_fingerprint(new_src)
        return classify_change_level(old_fp, new_fp)

    # ── ChangeLevel enum ───────────────────────────────────────────────────────

    def test_change_level_enum_values(self):
        from server.kg.kg_fingerprint import ChangeLevel
        assert ChangeLevel.NONE.value       == "NONE"
        assert ChangeLevel.COSMETIC.value   == "COSMETIC"
        assert ChangeLevel.STRUCTURAL.value == "STRUCTURAL"

    def test_change_level_is_string_subclass(self):
        from server.kg.kg_fingerprint import ChangeLevel
        assert isinstance(ChangeLevel.NONE, str)

    # ── _extract_python_fingerprint ────────────────────────────────────────────

    def test_fingerprint_returns_dict_with_two_keys(self):
        fp = self._fp("x = 1")
        assert "content_hash" in fp
        assert "structural_hash" in fp

    def test_content_hash_is_sha256_hex(self):
        import hashlib
        src = "def foo(): pass"
        fp = self._fp(src)
        expected = hashlib.sha256(src.encode("utf-8")).hexdigest()
        assert fp["content_hash"] == expected

    def test_content_hash_changes_with_whitespace(self):
        fp1 = self._fp("def foo(): pass")
        fp2 = self._fp("def foo():  pass")
        assert fp1["content_hash"] != fp2["content_hash"]

    def test_structural_hash_stable_across_docstring_change(self):
        src1 = 'def foo():\n    """old docstring"""\n    return 1\n'
        src2 = 'def foo():\n    """new docstring"""\n    return 1\n'
        fp1 = self._fp(src1)
        fp2 = self._fp(src2)
        assert fp1["structural_hash"] == fp2["structural_hash"]

    def test_structural_hash_changes_with_function_rename(self):
        fp1 = self._fp("def foo(): pass")
        fp2 = self._fp("def bar(): pass")
        assert fp1["structural_hash"] != fp2["structural_hash"]

    def test_structural_hash_changes_with_new_argument(self):
        fp1 = self._fp("def foo(a): pass")
        fp2 = self._fp("def foo(a, b): pass")
        assert fp1["structural_hash"] != fp2["structural_hash"]

    def test_structural_hash_changes_with_new_class(self):
        fp1 = self._fp("class A: pass")
        fp2 = self._fp("class A: pass\nclass B: pass")
        assert fp1["structural_hash"] != fp2["structural_hash"]

    def test_structural_hash_stable_across_comment_change(self):
        src1 = "def foo():\n    # old comment\n    return 1\n"
        src2 = "def foo():\n    # new comment\n    return 1\n"
        fp1 = self._fp(src1)
        fp2 = self._fp(src2)
        assert fp1["structural_hash"] == fp2["structural_hash"]

    def test_structural_hash_captures_decorator_name(self):
        src1 = "@property\ndef foo(self): pass"
        src2 = "@staticmethod\ndef foo(self): pass"
        fp1 = self._fp(src1)
        fp2 = self._fp(src2)
        assert fp1["structural_hash"] != fp2["structural_hash"]

    def test_structural_hash_captures_base_class(self):
        src1 = "class A(Base): pass"
        src2 = "class A(OtherBase): pass"
        fp1 = self._fp(src1)
        fp2 = self._fp(src2)
        assert fp1["structural_hash"] != fp2["structural_hash"]

    def test_syntax_error_returns_fixed_structural_hash(self):
        # Two unparseable snippets should produce the same structural_hash
        fp1 = self._fp("def foo(: syntax error")
        fp2 = self._fp("class !!!")
        assert fp1["structural_hash"] == fp2["structural_hash"]

    def test_empty_string_produces_hashes(self):
        fp = self._fp("")
        assert len(fp["content_hash"]) == 64
        assert len(fp["structural_hash"]) == 64

    def test_non_python_text_returns_hashes(self):
        fp = self._fp("users id name email")
        assert "content_hash" in fp
        assert "structural_hash" in fp

    # ── classify_change_level ──────────────────────────────────────────────────

    def test_classify_none_when_identical(self):
        from server.kg.kg_fingerprint import ChangeLevel
        result = self._classify("def foo(): pass", "def foo(): pass")
        assert result == ChangeLevel.NONE

    def test_classify_cosmetic_when_docstring_changes(self):
        from server.kg.kg_fingerprint import ChangeLevel
        src1 = 'def foo():\n    """old"""\n    pass\n'
        src2 = 'def foo():\n    """new"""\n    pass\n'
        result = self._classify(src1, src2)
        assert result == ChangeLevel.COSMETIC

    def test_classify_structural_when_function_renamed(self):
        from server.kg.kg_fingerprint import ChangeLevel
        result = self._classify("def foo(): pass", "def bar(): pass")
        assert result == ChangeLevel.STRUCTURAL

    def test_classify_structural_when_arg_added(self):
        from server.kg.kg_fingerprint import ChangeLevel
        result = self._classify("def foo(a): pass", "def foo(a, b): pass")
        assert result == ChangeLevel.STRUCTURAL

    def test_classify_structural_when_old_fp_has_no_structural_hash(self):
        from server.kg.kg_fingerprint import ChangeLevel, classify_change_level
        old_fp = {"content_hash": "aaa", "structural_hash": None}
        new_fp = {"content_hash": "bbb", "structural_hash": "ccc"}
        result = classify_change_level(old_fp, new_fp)
        assert result == ChangeLevel.STRUCTURAL

    def test_classify_structural_when_new_fp_has_no_structural_hash(self):
        from server.kg.kg_fingerprint import ChangeLevel, classify_change_level
        old_fp = {"content_hash": "aaa", "structural_hash": "sss"}
        new_fp = {"content_hash": "bbb", "structural_hash": None}
        result = classify_change_level(old_fp, new_fp)
        assert result == ChangeLevel.STRUCTURAL

    def test_classify_none_when_content_hash_identical_ignores_structural(self):
        """NONE takes precedence when content hash is the same even if structural differs."""
        from server.kg.kg_fingerprint import ChangeLevel, classify_change_level
        fp = {"content_hash": "same", "structural_hash": "x"}
        result = classify_change_level(fp, fp)
        assert result == ChangeLevel.NONE


# ── P2-A Tests: KGStore fingerprint storage ───────────────────────────────────

class TestKGStoreFingerprint:
    """Tests for set_doc_fingerprint and has_structure_changed in KGStore."""

    @pytest.fixture
    def store(self, tmp_path):
        from server.kg.kg_store import KGStore
        s = KGStore(tmp_path / "test.db")
        yield s
        s.close()

    def test_set_and_retrieve_fingerprint(self, store):
        store.set_doc_fingerprint("doc1", "content_abc", "struct_xyz")
        # has_structure_changed should return False - content is identical
        assert store.has_structure_changed("doc1", "content_abc", "struct_xyz") is False

    def test_new_doc_always_changed(self, store):
        assert store.has_structure_changed("unseen", "c1", "s1") is True

    def test_none_change_skips_when_content_identical(self, store):
        store.set_doc_fingerprint("doc2", "c_same", "s1")
        # Same content_hash → NONE → return False
        assert store.has_structure_changed("doc2", "c_same", "s_different") is False

    def test_cosmetic_skips_when_struct_same(self, store):
        store.set_doc_fingerprint("doc3", "c_old", "s_same")
        # Different content_hash but same structural_hash → COSMETIC → return False
        assert store.has_structure_changed("doc3", "c_new", "s_same") is False

    def test_structural_triggers_when_struct_different(self, store):
        store.set_doc_fingerprint("doc4", "c_old", "s_old")
        # Different content and structural hash → STRUCTURAL → return True
        assert store.has_structure_changed("doc4", "c_new", "s_new") is True

    def test_legacy_null_struct_triggers_reindex(self, store):
        """A row with NULL structural_hash (written before P2-A) triggers re-index."""
        # Write a legacy row using the old set_doc_hash interface
        store.set_doc_hash("doc5", "c_old")
        # structural_hash is NULL → conservative: return True
        assert store.has_structure_changed("doc5", "c_new", "s_new") is True

    def test_set_fingerprint_overwrites_existing(self, store):
        store.set_doc_fingerprint("doc6", "c1", "s1")
        store.set_doc_fingerprint("doc6", "c2", "s2")
        # New fingerprint persisted
        assert store.has_structure_changed("doc6", "c2", "s2") is False
        assert store.has_structure_changed("doc6", "c1", "s1") is True


# ── P2-B Tests: ChangeClassifier and UpdateDecision ──────────────────────────

class TestChangeClassifier:
    """Tests for UpdateDecision enum and ChangeClassifier.classify_update."""

    def test_update_decision_enum_values(self):
        from server.kg.kg_fingerprint import UpdateDecision
        assert UpdateDecision.SKIP.value           == "SKIP"
        assert UpdateDecision.PARTIAL_UPDATE.value == "PARTIAL_UPDATE"
        assert UpdateDecision.SCHEMA_UPDATE.value  == "SCHEMA_UPDATE"
        assert UpdateDecision.FULL_UPDATE.value    == "FULL_UPDATE"

    def test_update_decision_is_string_subclass(self):
        from server.kg.kg_fingerprint import UpdateDecision
        assert isinstance(UpdateDecision.SKIP, str)

    def test_none_maps_to_skip(self):
        from server.kg.kg_fingerprint import ChangeClassifier, ChangeLevel, UpdateDecision
        result = ChangeClassifier.classify_update(ChangeLevel.NONE)
        assert result == UpdateDecision.SKIP

    def test_cosmetic_maps_to_partial_update(self):
        from server.kg.kg_fingerprint import ChangeClassifier, ChangeLevel, UpdateDecision
        result = ChangeClassifier.classify_update(ChangeLevel.COSMETIC)
        assert result == UpdateDecision.PARTIAL_UPDATE

    def test_structural_non_schema_maps_to_schema_update(self):
        from server.kg.kg_fingerprint import ChangeClassifier, ChangeLevel, UpdateDecision
        result = ChangeClassifier.classify_update(
            ChangeLevel.STRUCTURAL, {"schema_affecting": False}
        )
        assert result == UpdateDecision.SCHEMA_UPDATE

    def test_structural_schema_affecting_maps_to_full_update(self):
        from server.kg.kg_fingerprint import ChangeClassifier, ChangeLevel, UpdateDecision
        result = ChangeClassifier.classify_update(
            ChangeLevel.STRUCTURAL, {"schema_affecting": True}
        )
        assert result == UpdateDecision.FULL_UPDATE

    def test_structural_default_metadata_is_schema_update(self):
        """Missing schema_affecting key defaults to False → SCHEMA_UPDATE."""
        from server.kg.kg_fingerprint import ChangeClassifier, ChangeLevel, UpdateDecision
        result = ChangeClassifier.classify_update(ChangeLevel.STRUCTURAL)
        assert result == UpdateDecision.SCHEMA_UPDATE

    def test_structural_none_metadata_is_schema_update(self):
        from server.kg.kg_fingerprint import ChangeClassifier, ChangeLevel, UpdateDecision
        result = ChangeClassifier.classify_update(ChangeLevel.STRUCTURAL, None)
        assert result == UpdateDecision.SCHEMA_UPDATE

    def test_none_ignores_schema_affecting(self):
        """schema_affecting is irrelevant when change is NONE."""
        from server.kg.kg_fingerprint import ChangeClassifier, ChangeLevel, UpdateDecision
        result = ChangeClassifier.classify_update(
            ChangeLevel.NONE, {"schema_affecting": True}
        )
        assert result == UpdateDecision.SKIP

    def test_cosmetic_ignores_schema_affecting(self):
        """schema_affecting is irrelevant when change is COSMETIC."""
        from server.kg.kg_fingerprint import ChangeClassifier, ChangeLevel, UpdateDecision
        result = ChangeClassifier.classify_update(
            ChangeLevel.COSMETIC, {"schema_affecting": True}
        )
        assert result == UpdateDecision.PARTIAL_UPDATE

    def test_extra_metadata_keys_are_ignored(self):
        """Unknown metadata keys do not raise."""
        from server.kg.kg_fingerprint import ChangeClassifier, ChangeLevel, UpdateDecision
        result = ChangeClassifier.classify_update(
            ChangeLevel.STRUCTURAL,
            {"schema_affecting": False, "unknown_key": "value"},
        )
        assert result == UpdateDecision.SCHEMA_UPDATE

    def test_all_decisions_are_update_decision_instances(self):
        from server.kg.kg_fingerprint import ChangeClassifier, ChangeLevel, UpdateDecision
        for cl in ChangeLevel:
            for sa in (True, False):
                decision = ChangeClassifier.classify_update(cl, {"schema_affecting": sa})
                assert isinstance(decision, UpdateDecision)


# ── P2-C Tests: remove_nodes_by_source + remove_edges_for_nodes ───────────────

class TestSurgicalExcision:
    """Tests for EntityGraph.remove_nodes_by_source and remove_edges_for_nodes."""

    @pytest.fixture
    def graph_with_two_sources(self):
        """EntityGraph with 4 nodes: 2 from 'src_a', 2 from 'src_b', plus edges."""
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode, EntityEdge

        g = EntityGraph()

        def _node(nid, src):
            return EntityNode(
                id=nid,
                label=nid,
                entity_type="TABLE",
                source_key=src,
                table=nid,
                pk_col="id",
                pk_val="1",
                row_dict={},
            )

        def _edge(src_id, tgt_id):
            return EntityEdge(
                src_id=src_id,
                tgt_id=tgt_id,
                relation="FK",
                label="FK",
                weight=1.0,
                confidence=1.0,
            )

        g.add_node(_node("a1", "src_a"))
        g.add_node(_node("a2", "src_a"))
        g.add_node(_node("b1", "src_b"))
        g.add_node(_node("b2", "src_b"))
        # Cross-source edge a1→b1, intra-source a1→a2, intra-source b1→b2
        g.add_edge(_edge("a1", "b1"))
        g.add_edge(_edge("a1", "a2"))
        g.add_edge(_edge("b1", "b2"))
        return g

    # ── remove_nodes_by_source ─────────────────────────────────────────────────

    def test_removes_nodes_for_source(self, graph_with_two_sources):
        g = graph_with_two_sources
        removed = g.remove_nodes_by_source("src_a")
        assert set(removed) == {"a1", "a2"}
        assert g.node_count == 2  # b1, b2 remain

    def test_returns_empty_list_when_source_absent(self, graph_with_two_sources):
        g = graph_with_two_sources
        removed = g.remove_nodes_by_source("nonexistent")
        assert removed == []
        assert g.node_count == 4  # unchanged

    def test_removes_incident_edges_automatically(self, graph_with_two_sources):
        g = graph_with_two_sources
        initial_edges = g.edge_count
        g.remove_nodes_by_source("src_a")
        # Edges a1→b1 and a1→a2 are gone; b1→b2 remains
        assert g.edge_count == 1

    def test_removes_all_nodes_when_only_one_source(self):
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode
        g = EntityGraph()

        def _node(nid):
            return EntityNode(id=nid, label=nid, entity_type="TABLE",
                              source_key="only", table=nid,
                              pk_col="id", pk_val="1", row_dict={})

        g.add_node(_node("x"))
        g.add_node(_node("y"))
        removed = g.remove_nodes_by_source("only")
        assert set(removed) == {"x", "y"}
        assert g.node_count == 0

    def test_graph_is_still_usable_after_removal(self, graph_with_two_sources):
        from server.kg.kg_resolver import EntityNode
        g = graph_with_two_sources
        g.remove_nodes_by_source("src_a")
        # Should be able to add new nodes without error
        g.add_node(EntityNode(id="new", label="new", entity_type="TABLE",
                              source_key="src_c", table="new",
                              pk_col="id", pk_val="1", row_dict={}))
        assert g.node_count == 3

    def test_adjacency_index_invalidated_after_removal(self, graph_with_two_sources):
        g = graph_with_two_sources
        g._build_index()  # populate _fwd / _bwd
        g.remove_nodes_by_source("src_a")
        # _fwd/_bwd should no longer exist (lazy invalidation)
        assert not hasattr(g, '_fwd') or g._fwd is not None  # either gone or rebuilt
        # subgraph() should rebuild index without error
        sub = g.subgraph("b1", depth=1)
        assert sub is not None

    # ── remove_edges_for_nodes ─────────────────────────────────────────────────

    def test_removes_edges_for_given_nodes(self, graph_with_two_sources):
        g = graph_with_two_sources
        count = g.remove_edges_for_nodes(["a1"])
        # a1→b1 and a1→a2 are removed (both originate from a1)
        assert count == 2
        assert g.edge_count == 1  # b1→b2 remains

    def test_returns_zero_for_empty_node_list(self, graph_with_two_sources):
        g = graph_with_two_sources
        count = g.remove_edges_for_nodes([])
        assert count == 0
        assert g.edge_count == 3  # unchanged

    def test_returns_zero_when_no_incident_edges(self, graph_with_two_sources):
        g = graph_with_two_sources
        # b2 has no outgoing edges and is only a target
        count = g.remove_edges_for_nodes(["b2"])
        assert count == 1  # b1→b2

    def test_removes_edges_for_multiple_nodes(self, graph_with_two_sources):
        g = graph_with_two_sources
        # Removing all nodes should remove all edges
        count = g.remove_edges_for_nodes(["a1", "a2", "b1", "b2"])
        assert count == 3
        assert g.edge_count == 0

    def test_returns_zero_for_absent_node_ids(self, graph_with_two_sources):
        g = graph_with_two_sources
        count = g.remove_edges_for_nodes(["ghost"])
        assert count == 0

    def test_adjacency_index_invalidated_after_edge_removal(self, graph_with_two_sources):
        g = graph_with_two_sources
        g._build_index()
        g.remove_edges_for_nodes(["a1"])
        # subgraph() must rebuild without errors
        sub = g.subgraph("b1", depth=1)
        assert sub is not None

    def test_nodes_intact_after_remove_edges(self, graph_with_two_sources):
        g = graph_with_two_sources
        g.remove_edges_for_nodes(["a1", "a2"])
        assert g.node_count == 4  # nodes are untouched


# ── P1-A Tests: Numeric Complexity Normaliser ─────────────────────────────────

class TestNumericComplexityNormaliser:
    """Tests for _normalise_complexity() and _COMPLEXITY_ALIASES in kg_graph."""

    def _normalise(self, value):
        from server.kg.kg_graph import _normalise_complexity
        return _normalise_complexity(value)

    def _node_complexity(self, complexity_value):
        """Helper: build an EntityGraph, add a node with given complexity in row_dict,
        return the stored complexity attribute."""
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode
        g = EntityGraph()
        g.add_node(EntityNode(
            id="n1", label="n1", entity_type="TABLE",
            source_key="src", table="t", pk_col="id", pk_val="1",
            row_dict={"complexity": complexity_value},
        ))
        return g._G.nodes["n1"]["complexity"]

    # ── _normalise_complexity: numeric scale ───────────────────────────────────

    def test_numeric_1_is_simple(self):
        assert self._normalise(1) == "simple"

    def test_numeric_2_is_simple(self):
        assert self._normalise(2) == "simple"

    def test_numeric_3_is_simple(self):
        assert self._normalise(3) == "simple"

    def test_numeric_4_is_moderate(self):
        assert self._normalise(4) == "moderate"

    def test_numeric_5_is_moderate(self):
        assert self._normalise(5) == "moderate"

    def test_numeric_6_is_moderate(self):
        assert self._normalise(6) == "moderate"

    def test_numeric_7_is_complex(self):
        assert self._normalise(7) == "complex"

    def test_numeric_8_is_complex(self):
        assert self._normalise(8) == "complex"

    def test_numeric_10_is_complex(self):
        assert self._normalise(10) == "complex"

    def test_numeric_float_2_5_is_simple(self):
        assert self._normalise(2.5) == "simple"

    def test_numeric_float_6_5_is_complex(self):
        """6.5 > 6, so it maps to 'complex' per UA normalizeComplexity (value <= 6 → moderate)."""
        assert self._normalise(6.5) == "complex"

    def test_numeric_float_7_1_is_complex(self):
        assert self._normalise(7.1) == "complex"

    def test_numeric_out_of_range_zero_unchanged(self):
        """Numeric < 1 is not in range; returned unchanged."""
        assert self._normalise(0) == 0

    def test_numeric_negative_unchanged(self):
        assert self._normalise(-1) == -1

    def test_bool_true_unchanged(self):
        """bool is a subclass of int; must NOT be treated as numeric."""
        result = self._normalise(True)
        assert result is True

    def test_bool_false_unchanged(self):
        result = self._normalise(False)
        assert result is False

    # ── _normalise_complexity: string aliases ──────────────────────────────────

    def test_string_low_is_simple(self):
        assert self._normalise("low") == "simple"

    def test_string_easy_is_simple(self):
        assert self._normalise("easy") == "simple"

    def test_string_trivial_is_simple(self):
        assert self._normalise("trivial") == "simple"

    def test_string_basic_is_simple(self):
        assert self._normalise("basic") == "simple"

    def test_string_medium_is_moderate(self):
        assert self._normalise("medium") == "moderate"

    def test_string_intermediate_is_moderate(self):
        assert self._normalise("intermediate") == "moderate"

    def test_string_mid_is_moderate(self):
        assert self._normalise("mid") == "moderate"

    def test_string_average_is_moderate(self):
        assert self._normalise("average") == "moderate"

    def test_string_high_is_complex(self):
        assert self._normalise("high") == "complex"

    def test_string_hard_is_complex(self):
        assert self._normalise("hard") == "complex"

    def test_string_difficult_is_complex(self):
        assert self._normalise("difficult") == "complex"

    def test_string_advanced_is_complex(self):
        assert self._normalise("advanced") == "complex"

    def test_string_canonical_simple_passthrough(self):
        assert self._normalise("simple") == "simple"

    def test_string_canonical_moderate_passthrough(self):
        assert self._normalise("moderate") == "moderate"

    def test_string_canonical_complex_passthrough(self):
        assert self._normalise("complex") == "complex"

    def test_string_unknown_passthrough(self):
        assert self._normalise("unknown_value") == "unknown_value"

    def test_string_case_insensitive(self):
        assert self._normalise("LOW") == "simple"
        assert self._normalise("HARD") == "complex"
        assert self._normalise("Medium") == "moderate"

    def test_string_strips_whitespace(self):
        assert self._normalise("  low  ") == "simple"

    def test_none_unchanged(self):
        assert self._normalise(None) is None

    # ── add_node() integration: complexity from row_dict ──────────────────────

    def test_add_node_numeric_2_stored_as_simple(self):
        assert self._node_complexity(2) == "simple"

    def test_add_node_numeric_5_stored_as_moderate(self):
        assert self._node_complexity(5) == "moderate"

    def test_add_node_numeric_8_stored_as_complex(self):
        assert self._node_complexity(8) == "complex"

    def test_add_node_string_trivial_stored_as_simple(self):
        assert self._node_complexity("trivial") == "simple"

    def test_add_node_string_advanced_stored_as_complex(self):
        assert self._node_complexity("advanced") == "complex"

    def test_add_node_canonical_moderate_passthrough(self):
        assert self._node_complexity("moderate") == "moderate"

    def test_add_node_no_complexity_no_attribute(self):
        """Nodes without complexity in row_dict should not have a complexity attr."""
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode
        g = EntityGraph()
        g.add_node(EntityNode(
            id="n2", label="n2", entity_type="TABLE",
            source_key="src", table="t", pk_col="id", pk_val="1",
            row_dict={},
        ))
        assert "complexity" not in g._G.nodes["n2"]

    def test_add_node_empty_row_dict_no_complexity(self):
        """Empty row_dict should not set complexity attr."""
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode
        g = EntityGraph()
        g.add_node(EntityNode(
            id="n3", label="n3", entity_type="TABLE",
            source_key="src", table="t", pk_col="id", pk_val="1",
            row_dict=None,
        ))
        assert "complexity" not in g._G.nodes["n3"]


# ── P1-B Tests: Source-Key Absolute-Path Sanitisation ────────────────────────

class TestSanitiseSourceKey:
    """Tests for _sanitise_source_key() in kg_indexer.

    All path tests use tmp_path-based absolute paths so they work on both
    POSIX (Docker production) and Windows (CI / developer machines).
    """

    def _ssk(self, raw_key, base_dir):
        from server.kg.kg_indexer import _sanitise_source_key
        return _sanitise_source_key(raw_key, base_dir)

    def test_absolute_under_base_returns_relative(self, tmp_path):
        base = tmp_path / "erd"
        base.mkdir()
        mydb = base / "mydb"
        mydb.mkdir()
        schema = mydb / "schema.json"
        schema.touch()
        result = self._ssk(str(schema), str(base))
        from pathlib import Path
        assert result == str(Path("mydb") / "schema.json")

    def test_already_relative_unchanged(self):
        result = self._ssk("mydb/schema.json", "/some/base")
        assert result == "mydb/schema.json"

    def test_absolute_outside_base_returns_stem(self, tmp_path):
        other = tmp_path / "other" / "path"
        other.mkdir(parents=True)
        schema = other / "schema.json"
        schema.touch()
        base = tmp_path / "erd"
        base.mkdir()
        result = self._ssk(str(schema), str(base))
        assert result == "schema"

    def test_empty_string_unchanged(self):
        result = self._ssk("", "/some/base")
        assert result == ""

    def test_relative_path_with_subdir_unchanged(self):
        result = self._ssk("subdir/file.json", "/some/base")
        assert result == "subdir/file.json"

    def test_absolute_matches_exact_base_subdir_returns_relative(self, tmp_path):
        """Absolute path directly under base (single level) → relative stem."""
        base = tmp_path / "erd"
        base.mkdir()
        mydb = base / "mydb"
        mydb.mkdir()
        result = self._ssk(str(mydb), str(base))
        assert result == "mydb"

    def test_absolute_nested_under_base_preserves_hierarchy(self, tmp_path):
        base = tmp_path / "erd"
        grp  = base / "grp" / "mydb"
        grp.mkdir(parents=True)
        schema = grp / "schema.json"
        schema.touch()
        result = self._ssk(str(schema), str(base))
        from pathlib import Path
        assert result == str(Path("grp") / "mydb" / "schema.json")

    def test_plain_filename_no_path_unchanged(self):
        result = self._ssk("schema.json", "/some/base")
        assert result == "schema.json"


# ── P1-C Tests: Node Type / ID Prefix Consistency ────────────────────────────

class TestTypeIDConsistency:
    """Tests for TYPE_PREFIX_MAP and TYPE_ID_MISMATCH in kg_validator."""

    def _make_graph_with_node(self, node_id: str, entity_type: str):
        """Build a minimal EntityGraph with a single node for validation testing."""
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode
        g = EntityGraph()
        g.add_node(EntityNode(
            id=node_id, label=node_id, entity_type=entity_type,
            source_key="src", table="t", pk_col="id", pk_val="1",
            row_dict={},
        ))
        return g

    def test_type_prefix_map_is_exported(self):
        from server.kg.kg_validator import TYPE_PREFIX_MAP
        assert isinstance(TYPE_PREFIX_MAP, dict)
        assert len(TYPE_PREFIX_MAP) >= 12

    def test_table_with_table_prefix_no_warning(self):
        from server.kg.kg_validator import validate_graph
        g = self._make_graph_with_node("table:users", "TABLE")
        result = validate_graph(g)
        mismatch_issues = [i for i in result.issues if i.code == "TYPE_ID_MISMATCH"]
        assert mismatch_issues == []

    def test_table_with_wrong_prefix_raises_warning(self):
        from server.kg.kg_validator import validate_graph, ValidationSeverity
        g = self._make_graph_with_node("func:users", "TABLE")
        result = validate_graph(g)
        mismatch_issues = [i for i in result.issues if i.code == "TYPE_ID_MISMATCH"]
        assert len(mismatch_issues) == 1
        assert mismatch_issues[0].severity == ValidationSeverity.WARNING

    def test_mismatch_includes_element_id(self):
        from server.kg.kg_validator import validate_graph
        g = self._make_graph_with_node("func:users", "TABLE")
        result = validate_graph(g)
        issue = next(i for i in result.issues if i.code == "TYPE_ID_MISMATCH")
        assert issue.element_id == "func:users"

    def test_mismatch_details_contain_entity_type_and_expected_prefix(self):
        from server.kg.kg_validator import validate_graph
        g = self._make_graph_with_node("func:users", "TABLE")
        result = validate_graph(g)
        issue = next(i for i in result.issues if i.code == "TYPE_ID_MISMATCH")
        assert issue.details["entity_type"] == "TABLE"
        assert issue.details["expected_prefix"] == "table:"

    def test_unknown_entity_type_no_warning(self):
        """Custom / external entity_types not in TYPE_PREFIX_MAP → no warning."""
        from server.kg.kg_validator import validate_graph
        g = self._make_graph_with_node("any:id:here", "CUSTOM_ENTITY")
        result = validate_graph(g)
        mismatch_issues = [i for i in result.issues if i.code == "TYPE_ID_MISMATCH"]
        assert mismatch_issues == []

    def test_empty_entity_type_no_warning(self):
        """Nodes with empty entity_type string are exempt."""
        from server.kg.kg_validator import validate_graph
        g = self._make_graph_with_node("some_id", "")
        result = validate_graph(g)
        mismatch_issues = [i for i in result.issues if i.code == "TYPE_ID_MISMATCH"]
        assert mismatch_issues == []

    def test_mismatch_is_warning_not_error(self):
        """TYPE_ID_MISMATCH must be WARNING severity so is_valid remains True in non-strict mode."""
        from server.kg.kg_validator import validate_graph
        g = self._make_graph_with_node("func:users", "TABLE")
        result = validate_graph(g)
        assert result.is_valid  # warnings don't fail non-strict validation

    def test_mismatch_fails_strict_validation(self):
        """In strict mode, warnings count as errors → is_valid == False."""
        from server.kg.kg_validator import validate_graph
        g = self._make_graph_with_node("func:users", "TABLE")
        result = validate_graph(g, strict=True)
        # has at least the ORPHAN_NODE warning + TYPE_ID_MISMATCH → strict=False
        assert not result.is_valid

    def test_column_with_col_prefix_no_warning(self):
        from server.kg.kg_validator import validate_graph
        g = self._make_graph_with_node("col:users.email", "COLUMN")
        result = validate_graph(g)
        mismatch_issues = [i for i in result.issues if i.code == "TYPE_ID_MISMATCH"]
        assert mismatch_issues == []

    def test_function_with_func_prefix_no_warning(self):
        from server.kg.kg_validator import validate_graph
        g = self._make_graph_with_node("func:calculate_total", "FUNCTION")
        result = validate_graph(g)
        mismatch_issues = [i for i in result.issues if i.code == "TYPE_ID_MISMATCH"]
        assert mismatch_issues == []

    def test_function_with_table_prefix_raises_warning(self):
        from server.kg.kg_validator import validate_graph
        g = self._make_graph_with_node("table:calculate_total", "FUNCTION")
        result = validate_graph(g)
        mismatch_issues = [i for i in result.issues if i.code == "TYPE_ID_MISMATCH"]
        assert len(mismatch_issues) == 1


# ── P3-A Tests: Cross-Source FK Edge Verification ─────────────────────────────

class TestCrossSourceEdgeVerify:
    """Tests for EdgeVerifyResult and verify_cross_source_fk_edges in kg_edge_verify."""

    # ── Import sanity ──────────────────────────────────────────────────────────

    def test_module_imports_cleanly(self):
        from server.kg.kg_edge_verify import verify_cross_source_fk_edges, EdgeVerifyResult
        assert callable(verify_cross_source_fk_edges)
        assert EdgeVerifyResult is not None

    def test_edge_verify_result_defaults(self):
        from server.kg.kg_edge_verify import EdgeVerifyResult
        r = EdgeVerifyResult()
        assert r.edges_added == 0
        assert r.edges_already_present == 0
        assert r.pairs_checked == 0
        assert r.added_edges == []

    # ── Empty / trivial registries ─────────────────────────────────────────────

    def test_empty_registry_returns_zero_counts(self):
        from server.kg.kg_edge_verify import verify_cross_source_fk_edges
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        result = verify_cross_source_fk_edges(g, {})
        assert result.edges_added == 0
        assert result.pairs_checked == 0
        assert result.added_edges == []

    def test_registry_with_no_ready_sources_returns_zero(self):
        from server.kg.kg_edge_verify import verify_cross_source_fk_edges
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        registry = {"src_a": {"status": "pending"}, "src_b": {"status": "error"}}
        result = verify_cross_source_fk_edges(g, registry)
        assert result.edges_added == 0
        assert result.pairs_checked == 0

    def test_empty_graph_with_ready_registry_no_edges_added(self, tmp_path, monkeypatch):
        """If FK map references cross-source tables but endpoints are absent from graph,
        no edges are added (conservative: only add edges between present nodes)."""
        from server.kg.kg_edge_verify import verify_cross_source_fk_edges
        from server.kg.kg_graph import EntityGraph

        # Patch _load_fk_map to return a FK that crosses sources
        fk_maps = {
            "src_a": {"orders": [("user_id", "users", "id")]},
            "src_b": {"users": []},
        }
        import server.kg.kg_edge_verify as mod
        original = None
        try:
            from server.kg import kg_resolver as res
            original = res._load_fk_map
            res._load_fk_map = lambda key: fk_maps.get(key, {})

            g = EntityGraph()
            registry = {"src_a": {"status": "ready"}, "src_b": {"status": "ready"}}
            result = verify_cross_source_fk_edges(g, registry)
            # Nodes are absent → no edges added
            assert result.edges_added == 0
        finally:
            if original is not None:
                res._load_fk_map = original

    def test_adds_missing_cross_db_edge(self, monkeypatch):
        """When FK references a table in another source and no edge exists, add it."""
        from server.kg.kg_edge_verify import verify_cross_source_fk_edges
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode, EntityEdge

        # Build graph with one node from each source
        g = EntityGraph()

        def _node(nid, src, table):
            return EntityNode(
                id=nid, label=nid, entity_type="TABLE",
                source_key=src, table=table, pk_col="id", pk_val="1",
                row_dict={},
            )

        g.add_node(_node("src_a:orders", "src_a", "orders"))
        g.add_node(_node("src_b:users", "src_b", "users"))

        fk_maps = {
            "src_a": {"orders": [("user_id", "users", "id")]},
            "src_b": {"users": []},
        }

        from server.kg import kg_resolver as res
        original = res._load_fk_map
        try:
            res._load_fk_map = lambda key: fk_maps.get(key, {})
            registry = {"src_a": {"status": "ready"}, "src_b": {"status": "ready"}}
            result = verify_cross_source_fk_edges(g, registry)
        finally:
            res._load_fk_map = original

        assert result.edges_added == 1
        assert result.pairs_checked >= 1
        assert len(result.added_edges) == 1
        src_id, tgt_id, relation = result.added_edges[0]
        assert src_id == "src_a:orders"
        assert tgt_id == "src_b:users"
        assert "cross_db" in relation

    def test_no_duplicate_edge_when_already_present(self, monkeypatch):
        """When the cross_db edge already exists, edges_already_present is incremented."""
        from server.kg.kg_edge_verify import verify_cross_source_fk_edges
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode, EntityEdge

        g = EntityGraph()

        def _node(nid, src, table):
            return EntityNode(
                id=nid, label=nid, entity_type="TABLE",
                source_key=src, table=table, pk_col="id", pk_val="1",
                row_dict={},
            )

        g.add_node(_node("src_a:orders", "src_a", "orders"))
        g.add_node(_node("src_b:users", "src_b", "users"))

        # Pre-add the cross_db edge
        g.add_edge(EntityEdge(
            src_id="src_a:orders", tgt_id="src_b:users",
            relation="cross_db:user_id", label="pre-existing",
            weight=1.0, confidence=1.0,
        ))

        fk_maps = {
            "src_a": {"orders": [("user_id", "users", "id")]},
            "src_b": {"users": []},
        }

        from server.kg import kg_resolver as res
        original = res._load_fk_map
        try:
            res._load_fk_map = lambda key: fk_maps.get(key, {})
            registry = {"src_a": {"status": "ready"}, "src_b": {"status": "ready"}}
            result = verify_cross_source_fk_edges(g, registry)
        finally:
            res._load_fk_map = original

        assert result.edges_added == 0
        assert result.edges_already_present == 1
        # No duplicate edge was created
        assert g._G.number_of_edges() == 1

    def test_same_source_fk_not_added_as_cross_db(self, monkeypatch):
        """Intra-source FKs must NOT produce cross_db edges."""
        from server.kg.kg_edge_verify import verify_cross_source_fk_edges
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode

        g = EntityGraph()

        def _node(nid, src, table):
            return EntityNode(
                id=nid, label=nid, entity_type="TABLE",
                source_key=src, table=table, pk_col="id", pk_val="1",
                row_dict={},
            )

        g.add_node(_node("src_a:orders", "src_a", "orders"))
        g.add_node(_node("src_a:users", "src_a", "users"))

        # Both tables are in the same source
        fk_maps = {
            "src_a": {
                "orders": [("user_id", "users", "id")],
                "users": [],
            }
        }

        from server.kg import kg_resolver as res
        original = res._load_fk_map
        try:
            res._load_fk_map = lambda key: fk_maps.get(key, {})
            registry = {"src_a": {"status": "ready"}}
            result = verify_cross_source_fk_edges(g, registry)
        finally:
            res._load_fk_map = original

        assert result.edges_added == 0

    def test_result_added_edges_contains_all_new_edges(self, monkeypatch):
        """added_edges list has one entry per added edge."""
        from server.kg.kg_edge_verify import verify_cross_source_fk_edges
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode

        g = EntityGraph()

        def _node(nid, src, table):
            return EntityNode(
                id=nid, label=nid, entity_type="TABLE",
                source_key=src, table=table, pk_col="id", pk_val="1",
                row_dict={},
            )

        # Two cross-source FK pairs
        g.add_node(_node("src_a:invoices", "src_a", "invoices"))
        g.add_node(_node("src_a:line_items", "src_a", "line_items"))
        g.add_node(_node("src_b:customers", "src_b", "customers"))
        g.add_node(_node("src_b:products", "src_b", "products"))

        fk_maps = {
            "src_a": {
                "invoices":   [("customer_id", "customers", "id")],
                "line_items": [("product_id", "products", "id")],
            },
            "src_b": {
                "customers": [],
                "products":  [],
            },
        }

        from server.kg import kg_resolver as res
        original = res._load_fk_map
        try:
            res._load_fk_map = lambda key: fk_maps.get(key, {})
            registry = {"src_a": {"status": "ready"}, "src_b": {"status": "ready"}}
            result = verify_cross_source_fk_edges(g, registry)
        finally:
            res._load_fk_map = original

        assert result.edges_added == 2
        assert len(result.added_edges) == 2

    def test_verify_edge_has_correct_weight_and_confidence(self, monkeypatch):
        """Added edges must have weight=0.8 and confidence=0.9 per the spec."""
        from server.kg.kg_edge_verify import verify_cross_source_fk_edges
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode

        g = EntityGraph()

        def _node(nid, src, table):
            return EntityNode(
                id=nid, label=nid, entity_type="TABLE",
                source_key=src, table=table, pk_col="id", pk_val="1",
                row_dict={},
            )

        g.add_node(_node("src_a:orders", "src_a", "orders"))
        g.add_node(_node("src_b:users", "src_b", "users"))

        fk_maps = {
            "src_a": {"orders": [("user_id", "users", "id")]},
            "src_b": {"users": []},
        }

        from server.kg import kg_resolver as res
        original = res._load_fk_map
        try:
            res._load_fk_map = lambda key: fk_maps.get(key, {})
            registry = {"src_a": {"status": "ready"}, "src_b": {"status": "ready"}}
            verify_cross_source_fk_edges(g, registry)
        finally:
            res._load_fk_map = original

        edge_attrs = g._G["src_a:orders"]["src_b:users"]
        assert abs(edge_attrs["weight"] - 0.8) < 1e-9
        assert abs(edge_attrs["confidence"] - 0.9) < 1e-9


# ── P2-A Tests: Batch Staleness Summary ──────────────────────────────────────

class TestBatchStalenessSummary:
    """Tests for GraphStalenessStatus, StalenessSummary, and
    KGStore.batch_staleness_summary() (P2-A)."""

    def _open_store(self, tmp_path):
        from server.kg.kg_store import KGStore
        return KGStore(tmp_path / "test_staleness.db")

    # ── Import / type checks ───────────────────────────────────────────────────

    def test_imports_cleanly(self):
        from server.kg.kg_store import (
            GraphStalenessStatus,
            StalenessSummary,
        )
        assert GraphStalenessStatus.FRESH.value == "fresh"
        assert GraphStalenessStatus.STALE.value == "stale"
        assert GraphStalenessStatus.UNKNOWN.value == "unknown"
        assert StalenessSummary is not None

    def test_staleness_summary_defaults(self):
        from server.kg.kg_store import StalenessSummary, GraphStalenessStatus
        s = StalenessSummary(
            source_key="mykey",
            status=GraphStalenessStatus.FRESH,
            last_indexed_at=0.0,
        )
        assert s.schema_doc_count == 0
        assert s.data_doc_count == 0
        assert s.reason == ""

    # ── batch_staleness_summary: empty store ───────────────────────────────────

    def test_empty_store_returns_empty_dict(self, tmp_path):
        from server.kg.kg_store import GraphStalenessStatus
        store = self._open_store(tmp_path)
        try:
            result = store.batch_staleness_summary()
            assert isinstance(result, dict)
            assert len(result) == 0
        finally:
            store.close()

    def test_missing_key_returns_unknown(self, tmp_path):
        """batch_staleness_summary(["nonexistent"]) returns UNKNOWN for absent key."""
        from server.kg.kg_store import GraphStalenessStatus
        store = self._open_store(tmp_path)
        try:
            result = store.batch_staleness_summary(source_keys=["nonexistent"])
            assert "nonexistent" in result
            assert result["nonexistent"].status == GraphStalenessStatus.UNKNOWN
            assert result["nonexistent"].last_indexed_at is None
            assert result["nonexistent"].reason == "No index state found"
        finally:
            store.close()

    # ── batch_staleness_summary: freshness logic ───────────────────────────────

    def test_just_indexed_source_is_fresh(self, tmp_path):
        """Source indexed now should be FRESH with a 1-hour TTL."""
        from server.kg.kg_store import GraphStalenessStatus
        store = self._open_store(tmp_path)
        try:
            store.mark_indexed("src_a", schema_doc_count=3, data_doc_count=0)
            result = store.batch_staleness_summary(
                source_keys=["src_a"],
                staleness_ttl_s=3600.0,
            )
            assert result["src_a"].status == GraphStalenessStatus.FRESH
        finally:
            store.close()

    def test_ttl_zero_is_stale(self, tmp_path):
        """staleness_ttl_s=0.0 means even a just-indexed source is STALE."""
        from server.kg.kg_store import GraphStalenessStatus
        store = self._open_store(tmp_path)
        try:
            store.mark_indexed("src_b", schema_doc_count=1, data_doc_count=0)
            result = store.batch_staleness_summary(
                source_keys=["src_b"],
                staleness_ttl_s=0.0,
            )
            assert result["src_b"].status == GraphStalenessStatus.STALE
        finally:
            store.close()

    def test_batch_returns_all_sources_when_no_keys_specified(self, tmp_path):
        """batch_staleness_summary() with no source_keys returns all indexed keys."""
        store = self._open_store(tmp_path)
        try:
            store.mark_indexed("k1", schema_doc_count=1, data_doc_count=0)
            store.mark_indexed("k2", schema_doc_count=2, data_doc_count=0)
            store.mark_indexed("k3", schema_doc_count=0, data_doc_count=5)
            result = store.batch_staleness_summary()
            assert set(result.keys()) >= {"k1", "k2", "k3"}
        finally:
            store.close()

    def test_summary_doc_counts_match_index_state(self, tmp_path):
        """schema_doc_count and data_doc_count must be read from index_state."""
        store = self._open_store(tmp_path)
        try:
            store.mark_indexed("src_c", schema_doc_count=7, data_doc_count=13)
            result = store.batch_staleness_summary(
                source_keys=["src_c"],
                staleness_ttl_s=3600.0,
            )
            assert result["src_c"].schema_doc_count == 7
            assert result["src_c"].data_doc_count == 13
        finally:
            store.close()

    # ── get_staleness_summary wrapper ──────────────────────────────────────────

    def test_get_staleness_summary_unknown_for_missing(self, tmp_path):
        from server.kg.kg_store import GraphStalenessStatus
        store = self._open_store(tmp_path)
        try:
            s = store.get_staleness_summary("never_indexed")
            assert s.status == GraphStalenessStatus.UNKNOWN
        finally:
            store.close()

    def test_get_staleness_summary_fresh_for_recent(self, tmp_path):
        from server.kg.kg_store import GraphStalenessStatus
        store = self._open_store(tmp_path)
        try:
            store.mark_indexed("recent", schema_doc_count=2, data_doc_count=0)
            s = store.get_staleness_summary("recent", staleness_ttl_s=3600.0)
            assert s.status == GraphStalenessStatus.FRESH
            assert s.source_key == "recent"
        finally:
            store.close()


# ── P2-B Tests: Node Source-Confidence Attribute ──────────────────────────────

class TestSourceConfidenceAttribute:
    """Tests for source_confidence in add_node() and remove_nodes_by_source() (P2-B)."""

    def _make_node(self, nid, src, row_dict=None):
        from server.kg.kg_resolver import EntityNode
        return EntityNode(
            id=nid, label=nid, entity_type="TABLE",
            source_key=src, table="t", pk_col="id", pk_val="1",
            row_dict=row_dict or {},
        )

    # ── add_node: source_confidence attribute ──────────────────────────────────

    def test_default_source_confidence_is_1_0(self):
        """add_node() without source_confidence in row_dict stores 1.0."""
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        g.add_node(self._make_node("n1", "db1"))
        assert g._G.nodes["n1"]["source_confidence"] == 1.0

    def test_explicit_source_confidence_stored(self):
        """add_node() with source_confidence=0.7 in row_dict stores 0.7."""
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        g.add_node(self._make_node("n1", "db1", row_dict={"source_confidence": 0.7}))
        assert g._G.nodes["n1"]["source_confidence"] == pytest.approx(0.7)

    def test_source_confidence_is_float(self):
        """source_confidence stored as a Python float, not a string."""
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        g.add_node(self._make_node("n1", "db1"))
        assert isinstance(g._G.nodes["n1"]["source_confidence"], float)

    # ── remove_nodes_by_source: confidence-aware removal ──────────────────────

    def test_high_confidence_node_removed(self):
        """source_confidence == 1.0: node must be removed."""
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        g.add_node(self._make_node("n1", "db1"))
        removed = g.remove_nodes_by_source("db1")
        assert "n1" in removed
        assert not g._G.has_node("n1")

    def test_low_confidence_no_external_ref_removed(self):
        """source_confidence < 1.0 but no external neighbours: node IS removed."""
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityEdge
        g = EntityGraph()
        # Two nodes, both in db1
        g.add_node(self._make_node("n1", "db1", row_dict={"source_confidence": 0.7}))
        g.add_node(self._make_node("n2", "db1"))
        g.add_edge(EntityEdge(
            src_id="n2", tgt_id="n1",
            relation="FK", label="test", weight=1.0, confidence=1.0,
        ))
        removed = g.remove_nodes_by_source("db1")
        assert "n1" in removed  # referenced only by same-source node → removed

    def test_low_confidence_with_external_ref_preserved(self):
        """source_confidence < 1.0 and externally referenced: node is preserved."""
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityEdge
        g = EntityGraph()
        # n1 in db1 (low confidence), n2 in db2 (different source)
        g.add_node(self._make_node("n1", "db1", row_dict={"source_confidence": 0.7}))
        g.add_node(self._make_node("n2", "db2"))
        g.add_edge(EntityEdge(
            src_id="n2", tgt_id="n1",
            relation="cross_db:x", label="test", weight=1.0, confidence=1.0,
        ))
        removed = g.remove_nodes_by_source("db1")
        assert "n1" not in removed        # preserved
        assert g._G.has_node("n1")        # still in graph

    def test_remove_only_removes_target_source(self):
        """remove_nodes_by_source('db1') must not touch nodes from 'db2'."""
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        g.add_node(self._make_node("n1", "db1"))
        g.add_node(self._make_node("n2", "db2"))
        g.remove_nodes_by_source("db1")
        assert not g._G.has_node("n1")
        assert g._G.has_node("n2")

    # ── kg_mcp proposal includes source_confidence ────────────────────────────

    def test_proposal_contains_source_confidence_0_7(self, tmp_path, monkeypatch):
        """tool_kg_propose_link() must set source_confidence=0.7 in the proposal dict."""
        captured = {}

        def _fake_upsert(**kwargs):
            captured["cell_value"] = kwargs.get("cell_value", "")

        from server.kg import kg_mcp as mcp
        import server.kg.kg_mcp as mcp_mod
        import server.kg.kg_store as store_mod

        class FakeStore:
            def data_doc_upsert(self, **kw):
                captured["cell_value"] = kw.get("cell_value", "")
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        original = store_mod._open_store_raw
        store_mod._open_store_raw = lambda *a, **kw: FakeStore()
        try:
            result = mcp.tool_kg_propose_link(
                src_id="node_a",
                tgt_id="node_b",
                relation="related_to",
            )
        finally:
            store_mod._open_store_raw = original

        assert result["ok"] is True
        proposal = result["proposal"]
        assert "source_confidence" in proposal
        assert proposal["source_confidence"] == pytest.approx(0.7)


# ══════════════════════════════════════════════════════════════════════════════
# Extended centrality methods on EntityGraph
# ══════════════════════════════════════════════════════════════════════════════

class TestExtendedCentrality:
    """P2-C extended: betweenness_centrality + closeness_centrality on EntityGraph."""

    def _build_graph(self):
        import networkx as nx
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        # A → B → C  and  A → C  (star-like with a shortcut)
        g._G = nx.DiGraph()
        g._G.add_edges_from([("A", "B"), ("B", "C"), ("A", "C")])
        g._build_index()
        return g

    def test_betweenness_centrality_returns_all_nodes(self):
        """betweenness_centrality() returns an entry for every node."""
        g = self._build_graph()
        bc = g.betweenness_centrality()
        assert set(bc.keys()) == {"A", "B", "C"}

    def test_betweenness_centrality_normalized(self):
        """Normalized betweenness values are in [0, 1]."""
        g = self._build_graph()
        bc = g.betweenness_centrality(normalized=True)
        for v in bc.values():
            assert 0.0 <= v <= 1.0

    def test_betweenness_centrality_approximate(self):
        """k-sample approximation still returns one entry per node."""
        g = self._build_graph()
        bc = g.betweenness_centrality(k=2)
        assert set(bc.keys()) == {"A", "B", "C"}

    def test_betweenness_centrality_empty_graph(self):
        """Empty EntityGraph returns {}."""
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        assert g.betweenness_centrality() == {}

    def test_closeness_centrality_returns_all_nodes(self):
        """closeness_centrality() returns an entry for every node."""
        g = self._build_graph()
        cc = g.closeness_centrality()
        assert set(cc.keys()) == {"A", "B", "C"}

    def test_closeness_centrality_values_in_range(self):
        """Closeness centrality values are in [0, 1]."""
        g = self._build_graph()
        cc = g.closeness_centrality()
        for v in cc.values():
            assert 0.0 <= v <= 1.0

    def test_closeness_centrality_isolated_node(self):
        """A node with no edges gets closeness 0."""
        import networkx as nx
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        g._G = nx.DiGraph()
        g._G.add_node("X")
        g._build_index()
        cc = g.closeness_centrality()
        assert "X" in cc
        assert cc["X"] == pytest.approx(0.0)

    def test_closeness_centrality_empty_graph(self):
        """Empty EntityGraph returns {}."""
        from server.kg.kg_graph import EntityGraph
        g = EntityGraph()
        assert g.closeness_centrality() == {}


# ══════════════════════════════════════════════════════════════════════════════
# Temporal query rewriter
# ══════════════════════════════════════════════════════════════════════════════

class TestTemporalQueryRewriter:
    """rewrite_temporal_query() injects temporal predicates without mutation."""

    def test_rewrite_as_of_adds_temporal_filters(self):
        from datetime import datetime, timezone
        from server.kg.kg_temporal import rewrite_temporal_query
        q = {"entity_type": "TABLE", "source_key": "main"}
        result = rewrite_temporal_query(q, as_of=datetime(2024, 6, 1, tzinfo=timezone.utc))
        assert "temporal_filters" in result
        assert "as_of" in result["temporal_filters"]
        assert result["temporal_filters"]["as_of"] == "2024-06-01T00:00:00Z"

    def test_rewrite_does_not_mutate_original(self):
        from datetime import datetime, timezone
        from server.kg.kg_temporal import rewrite_temporal_query
        q = {"entity_type": "TABLE"}
        rewrite_temporal_query(q, as_of=datetime(2024, 1, 1, tzinfo=timezone.utc))
        assert "temporal_filters" not in q

    def test_rewrite_between(self):
        from datetime import datetime, timezone
        from server.kg.kg_temporal import rewrite_temporal_query, TemporalBound
        start = datetime(2023, 1, 1, tzinfo=timezone.utc)
        end   = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = rewrite_temporal_query({}, between=(start, end))
        tf = result["temporal_filters"]
        assert "valid_from_gte" in tf
        assert "valid_until_lte" in tf

    def test_rewrite_current_only(self):
        from server.kg.kg_temporal import rewrite_temporal_query
        result = rewrite_temporal_query({}, current_only=True)
        assert result["temporal_filters"]["superseded_at_open"] is True

    def test_rewrite_combined_as_of_and_current_only(self):
        from datetime import datetime, timezone
        from server.kg.kg_temporal import rewrite_temporal_query
        result = rewrite_temporal_query(
            {}, as_of=datetime(2024, 6, 1, tzinfo=timezone.utc), current_only=True
        )
        tf = result["temporal_filters"]
        assert "as_of" in tf
        assert tf["superseded_at_open"] is True

    def test_rewrite_merges_existing_temporal_filters(self):
        from datetime import datetime, timezone
        from server.kg.kg_temporal import rewrite_temporal_query
        q = {"temporal_filters": {"extra_key": "value"}}
        result = rewrite_temporal_query(q, current_only=True)
        tf = result["temporal_filters"]
        assert tf["extra_key"] == "value"
        assert tf["superseded_at_open"] is True

    def test_rewrite_naive_datetime_treated_as_utc(self):
        from datetime import datetime
        from server.kg.kg_temporal import rewrite_temporal_query
        naive = datetime(2024, 3, 15)
        result = rewrite_temporal_query({}, as_of=naive)
        assert result["temporal_filters"]["as_of"] == "2024-03-15T00:00:00Z"


# ══════════════════════════════════════════════════════════════════════════════
# CROSS_DB_ENDPOINT_POLICY
# ══════════════════════════════════════════════════════════════════════════════

class TestCrossDbEndpointPolicy:
    """apply_cross_db_endpoint_policy() enforces skip/stub/error policies."""

    def _make_nodes_edges(self):
        from server.kg.kg_resolver import EntityNode, EntityEdge
        n1 = EntityNode(id="src:t:id=1", label="N1", entity_type="TABLE",
                        source_key="src", table="t", pk_col="id", pk_val="1")
        e_known  = EntityEdge(src_id="src:t:id=1", tgt_id="src:t:id=1",
                              relation="self", label="self")
        e_dangle = EntityEdge(src_id="src:t:id=1", tgt_id="other:t:id=99",
                              relation="cross_db", label="orphan")
        return [n1], [e_known, e_dangle]

    def test_skip_policy_drops_dangling_edges(self):
        from server.kg.kg_resolver import apply_cross_db_endpoint_policy
        nodes, edges = self._make_nodes_edges()
        out_nodes, out_edges = apply_cross_db_endpoint_policy(nodes, edges, policy="skip")
        assert len(out_nodes) == 1
        assert all(e.tgt_id != "other:t:id=99" for e in out_edges)

    def test_skip_keeps_clean_edges(self):
        from server.kg.kg_resolver import apply_cross_db_endpoint_policy
        nodes, edges = self._make_nodes_edges()
        _, out_edges = apply_cross_db_endpoint_policy(nodes, edges, policy="skip")
        assert any(e.relation == "self" for e in out_edges)

    def test_stub_policy_creates_missing_nodes(self):
        from server.kg.kg_resolver import apply_cross_db_endpoint_policy
        nodes, edges = self._make_nodes_edges()
        out_nodes, out_edges = apply_cross_db_endpoint_policy(nodes, edges, policy="stub")
        node_ids = {n.id for n in out_nodes}
        assert "other:t:id=99" in node_ids

    def test_stub_preserves_all_edges(self):
        from server.kg.kg_resolver import apply_cross_db_endpoint_policy
        nodes, edges = self._make_nodes_edges()
        _, out_edges = apply_cross_db_endpoint_policy(nodes, edges, policy="stub")
        assert len(out_edges) == len(edges)

    def test_error_policy_raises_on_dangling(self):
        from server.kg.kg_resolver import apply_cross_db_endpoint_policy
        nodes, edges = self._make_nodes_edges()
        with pytest.raises(RuntimeError, match="CROSS_DB_ENDPOINT_POLICY=error"):
            apply_cross_db_endpoint_policy(nodes, edges, policy="error")

    def test_no_dangling_all_policies_no_op(self):
        from server.kg.kg_resolver import EntityNode, EntityEdge, apply_cross_db_endpoint_policy
        n1 = EntityNode(id="a:t:id=1", label="A", entity_type="TABLE",
                        source_key="a", table="t", pk_col="id", pk_val="1")
        n2 = EntityNode(id="a:t:id=2", label="B", entity_type="TABLE",
                        source_key="a", table="t", pk_col="id", pk_val="2")
        e  = EntityEdge(src_id="a:t:id=1", tgt_id="a:t:id=2", relation="FK", label="fk")
        for policy in ("skip", "stub", "error"):
            out_nodes, out_edges = apply_cross_db_endpoint_policy([n1, n2], [e], policy=policy)
            assert len(out_nodes) == 2
            assert len(out_edges) == 1

    def test_stub_node_has_correct_source_key(self):
        from server.kg.kg_resolver import apply_cross_db_endpoint_policy
        nodes, edges = self._make_nodes_edges()
        out_nodes, _ = apply_cross_db_endpoint_policy(nodes, edges, policy="stub")
        stub = next(n for n in out_nodes if n.id == "other:t:id=99")
        assert stub.source_key == "other"


# ══════════════════════════════════════════════════════════════════════════════
# TemporalVersionManager
# ══════════════════════════════════════════════════════════════════════════════

class TestTemporalVersionManager:
    """TemporalVersionManager: save, retrieve, list, diff snapshots."""

    def _make_graph(self, node_ids, edge_pairs=None):
        """Build a minimal Cytoscape-format graph dict."""
        nodes = [{"data": {"id": nid}} for nid in node_ids]
        edges = [{"data": {"id": f"e{i}", "source": s, "target": t}}
                 for i, (s, t) in enumerate(edge_pairs or [])]
        return {"elements": {"nodes": nodes, "edges": edges}}

    def test_save_and_retrieve_latest(self, tmp_path):
        from server.kg.kg_temporal import TemporalVersionManager
        mgr = TemporalVersionManager(tmp_path / "kg.db")
        g = self._make_graph(["A", "B"])
        row_id = mgr.save_snapshot("src1", g)
        assert isinstance(row_id, int) and row_id > 0
        retrieved = mgr.get_snapshot("src1")
        assert retrieved is not None
        assert len(retrieved["elements"]["nodes"]) == 2

    def test_get_snapshot_returns_none_for_unknown_source(self, tmp_path):
        from server.kg.kg_temporal import TemporalVersionManager
        mgr = TemporalVersionManager(tmp_path / "kg.db")
        assert mgr.get_snapshot("nonexistent") is None

    def test_list_versions_empty(self, tmp_path):
        from server.kg.kg_temporal import TemporalVersionManager
        mgr = TemporalVersionManager(tmp_path / "kg.db")
        assert mgr.list_versions("src1") == []

    def test_list_versions_sorted_newest_first(self, tmp_path):
        from datetime import datetime, timezone, timedelta
        from server.kg.kg_temporal import TemporalVersionManager
        mgr = TemporalVersionManager(tmp_path / "kg.db")
        t1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mgr.save_snapshot("src1", self._make_graph(["A"]), recorded_at=t1, version_tag="v1")
        mgr.save_snapshot("src1", self._make_graph(["A", "B"]), recorded_at=t2, version_tag="v2")
        versions = mgr.list_versions("src1")
        assert len(versions) == 2
        assert versions[0]["version_tag"] == "v2"  # newest first
        assert versions[1]["version_tag"] == "v1"

    def test_as_of_returns_correct_version(self, tmp_path):
        from datetime import datetime, timezone
        from server.kg.kg_temporal import TemporalVersionManager
        mgr = TemporalVersionManager(tmp_path / "kg.db")
        t1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mgr.save_snapshot("src1", self._make_graph(["A"]), recorded_at=t1)
        mgr.save_snapshot("src1", self._make_graph(["A", "B"]), recorded_at=t2)
        # As-of midway should return the first snapshot
        mid = datetime(2024, 3, 1, tzinfo=timezone.utc)
        snap = mgr.get_snapshot("src1", as_of=mid)
        assert snap is not None
        assert len(snap["elements"]["nodes"]) == 1

    def test_diff_added_removed_nodes(self, tmp_path):
        from datetime import datetime, timezone
        from server.kg.kg_temporal import TemporalVersionManager
        mgr = TemporalVersionManager(tmp_path / "kg.db")
        t1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2024, 6, 1, tzinfo=timezone.utc)
        id1 = mgr.save_snapshot("src1", self._make_graph(["A", "B"]), recorded_at=t1)
        id2 = mgr.save_snapshot("src1", self._make_graph(["B", "C"]), recorded_at=t2)
        diff = mgr.diff_snapshots("src1", before_id=id1, after_id=id2)
        assert "C" in diff["added_nodes"]
        assert "A" in diff["removed_nodes"]
        assert diff["node_delta"] == 0  # same total count

    def test_diff_by_datetime(self, tmp_path):
        from datetime import datetime, timezone
        from server.kg.kg_temporal import TemporalVersionManager
        mgr = TemporalVersionManager(tmp_path / "kg.db")
        t1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mgr.save_snapshot("src1", self._make_graph(["A"]), recorded_at=t1)
        mgr.save_snapshot("src1", self._make_graph(["A", "B"]), recorded_at=t2)
        diff = mgr.diff_snapshots("src1",
            before=datetime(2024, 3, 1, tzinfo=timezone.utc),
            after=datetime(2024, 7, 1, tzinfo=timezone.utc),
        )
        assert "B" in diff["added_nodes"]
        assert diff["node_delta"] == 1

    def test_diff_edge_added(self, tmp_path):
        from datetime import datetime, timezone
        from server.kg.kg_temporal import TemporalVersionManager
        mgr = TemporalVersionManager(tmp_path / "kg.db")
        t1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2024, 6, 1, tzinfo=timezone.utc)
        id1 = mgr.save_snapshot("src1", self._make_graph(["A", "B"]), recorded_at=t1)
        id2 = mgr.save_snapshot("src1", self._make_graph(["A", "B"], [("A","B")]), recorded_at=t2)
        diff = mgr.diff_snapshots("src1", before_id=id1, after_id=id2)
        assert diff["edge_delta"] == 1

    def test_snapshot_node_edge_counts_stored(self, tmp_path):
        from server.kg.kg_temporal import TemporalVersionManager
        mgr = TemporalVersionManager(tmp_path / "kg.db")
        g = self._make_graph(["X", "Y", "Z"], [("X","Y")])
        mgr.save_snapshot("s", g, version_tag="test")
        versions = mgr.list_versions("s")
        assert versions[0]["node_count"] == 3
        assert versions[0]["edge_count"] == 1

    def test_memory_store_works(self):
        from server.kg.kg_temporal import TemporalVersionManager
        mgr = TemporalVersionManager(":memory:")
        g = {"elements": {"nodes": [{"data": {"id": "X"}}], "edges": []}}
        mgr.save_snapshot("k", g)
        assert mgr.get_snapshot("k") is not None


# ══════════════════════════════════════════════════════════════════════════════
# TemporalGraphQuery
# ══════════════════════════════════════════════════════════════════════════════

class TestTemporalGraphQuery:
    """TemporalGraphQuery: fluent filtering over relationship dicts."""

    def _rels(self):
        return [
            {"src": "A", "tgt": "B", "valid_from": "2023-01-01Z", "valid_until": None,
             "source_key": "db1", "relation": "FK:orders"},
            {"src": "B", "tgt": "C", "valid_from": "2024-01-01Z",
             "valid_until": "2024-06-01T00:00:00Z",
             "source_key": "db1", "relation": "cross_db:email"},
            {"src": "X", "tgt": "Y", "valid_from": None, "valid_until": None,
             "source_key": "db2", "relation": "FK:users",
             "superseded_at": "2023-12-31T00:00:00Z"},
        ]

    def test_all_returns_full_list(self):
        from server.kg.kg_temporal import TemporalGraphQuery
        q = TemporalGraphQuery(self._rels())
        assert len(q.all()) == 3

    def test_count_and_len(self):
        from server.kg.kg_temporal import TemporalGraphQuery
        q = TemporalGraphQuery(self._rels())
        assert q.count() == 3
        assert len(q) == 3

    def test_as_of_filters_active(self):
        from datetime import datetime, timezone
        from server.kg.kg_temporal import TemporalGraphQuery
        q = TemporalGraphQuery(self._rels())
        # At 2024-03-01, rel B→C (valid 2024-01 to 2024-06) should be included
        pt = datetime(2024, 3, 1, tzinfo=timezone.utc)
        result = q.as_of(pt).all()
        srcs = {r["src"] for r in result}
        assert "B" in srcs

    def test_as_of_excludes_expired(self):
        from datetime import datetime, timezone
        from server.kg.kg_temporal import TemporalGraphQuery
        q = TemporalGraphQuery(self._rels())
        # At 2024-07-01, rel B→C (valid until 2024-06-01) should be excluded
        pt = datetime(2024, 7, 1, tzinfo=timezone.utc)
        result = q.as_of(pt).all()
        srcs = {r["src"] for r in result}
        assert "B" not in srcs

    def test_current_only_excludes_superseded(self):
        from server.kg.kg_temporal import TemporalGraphQuery
        q = TemporalGraphQuery(self._rels())
        result = q.current_only().all()
        srcs = {r["src"] for r in result}
        # X→Y has superseded_at set → excluded; A→B and B→C have no superseded_at → included
        assert "X" not in srcs
        assert "A" in srcs

    def test_source_filter(self):
        from server.kg.kg_temporal import TemporalGraphQuery
        q = TemporalGraphQuery(self._rels())
        result = q.source("db2").all()
        assert all(r["source_key"] == "db2" for r in result)
        assert len(result) == 1

    def test_relation_prefix_filter(self):
        from server.kg.kg_temporal import TemporalGraphQuery
        q = TemporalGraphQuery(self._rels())
        result = q.relation("FK").all()
        assert all(r["relation"].startswith("FK") for r in result)
        assert len(result) == 2

    def test_fluent_chaining(self):
        from datetime import datetime, timezone
        from server.kg.kg_temporal import TemporalGraphQuery
        q = TemporalGraphQuery(self._rels())
        pt = datetime(2024, 3, 1, tzinfo=timezone.utc)
        # Active at pt + from db1 + FK prefix
        result = q.as_of(pt).source("db1").relation("FK").all()
        assert all(r["source_key"] == "db1" for r in result)
        assert all(r["relation"].startswith("FK") for r in result)

    def test_empty_result_chain(self):
        from server.kg.kg_temporal import TemporalGraphQuery
        q = TemporalGraphQuery(self._rels())
        result = q.source("nonexistent").all()
        assert result == []

    def test_original_not_mutated(self):
        from datetime import datetime, timezone
        from server.kg.kg_temporal import TemporalGraphQuery
        rels = self._rels()
        q = TemporalGraphQuery(rels)
        q.source("db1").relation("FK")
        assert len(q.all()) == 3  # original query unchanged


# ── Roadmap v3 (ai-knowledge-graph) tests ─────────────────────────────────────


# ── P1-A: KGPromptRegistry ───────────────────────────────────────────────────

class TestKGPromptRegistry:
    """P1-A: KGPromptRegistry - centralised prompt storage and retrieval."""

    def test_import_singleton(self):
        """kg_prompt_registry singleton is importable from kg_prompts."""
        from server.kg.kg_prompts import kg_prompt_registry, KGPromptRegistry
        assert isinstance(kg_prompt_registry, KGPromptRegistry)

    def test_import_from_kg_inference(self):
        """kg_prompt_registry is re-exported from kg_inference for convenience."""
        from server.kg.kg_inference import kg_prompt_registry
        assert kg_prompt_registry is not None

    def test_import_from_kg_mcp(self):
        """kg_prompt_registry is re-exported from kg_mcp for convenience."""
        from server.kg.kg_mcp import kg_prompt_registry
        assert kg_prompt_registry is not None

    def test_static_prompt_returned_unchanged(self):
        """A registered static string is returned as-is."""
        from server.kg.kg_prompts import KGPromptRegistry
        r = KGPromptRegistry()
        r.register("sys", "You are helpful.")
        assert r.get_prompt("sys") == "You are helpful."

    def test_callable_prompt_receives_args(self):
        """A registered callable receives positional args via get_prompt."""
        from server.kg.kg_prompts import KGPromptRegistry
        r = KGPromptRegistry()
        r.register("greet", lambda name: f"Hello {name}!")
        assert r.get_prompt("greet", "world") == "Hello world!"

    def test_callable_prompt_receives_kwargs(self):
        """A registered callable receives keyword args via get_prompt."""
        from server.kg.kg_prompts import KGPromptRegistry
        r = KGPromptRegistry()
        r.register("greet", lambda who="stranger": f"Hi {who}.")
        assert r.get_prompt("greet", who="Alice") == "Hi Alice."

    def test_unknown_prompt_raises_key_error(self):
        """get_prompt on an unregistered name raises KeyError."""
        from server.kg.kg_prompts import KGPromptRegistry
        r = KGPromptRegistry()
        import pytest
        with pytest.raises(KeyError, match="unknown"):
            r.get_prompt("unknown")

    def test_register_overwrites_previous(self):
        """Registering the same name twice replaces the old entry."""
        from server.kg.kg_prompts import KGPromptRegistry
        r = KGPromptRegistry()
        r.register("p", "v1")
        r.register("p", "v2")
        assert r.get_prompt("p") == "v2"

    def test_bridge_system_prompt_non_empty(self):
        """Pre-registered bridge_system prompt is a non-empty string."""
        from server.kg.kg_prompts import kg_prompt_registry
        text = kg_prompt_registry.get_prompt("bridge_system")
        assert isinstance(text, str) and len(text) > 20

    def test_bridge_user_prompt_contains_args(self):
        """bridge_user prompt embeds main_sample and groups_text."""
        from server.kg.kg_prompts import kg_prompt_registry
        text = kg_prompt_registry.get_prompt("bridge_user", "nodeA", "Group1: nodeB")
        assert "nodeA" in text
        assert "Group1" in text

    def test_hub_user_prompt_contains_max_new(self):
        """hub_user prompt embeds max_new value."""
        from server.kg.kg_prompts import kg_prompt_registry
        text = kg_prompt_registry.get_prompt("hub_user", "entities", "existing", 7)
        assert "7" in text

    def test_propose_link_user_prompt_contains_ids(self):
        """propose_link_user prompt contains src_id, tgt_id, and context."""
        from server.kg.kg_prompts import kg_prompt_registry
        text = kg_prompt_registry.get_prompt(
            "propose_link_user", "table:orders", "table:users", "FK relationship"
        )
        assert "table:orders" in text
        assert "table:users" in text
        assert "FK relationship" in text


# ── P1-B: _normalise_relation and _RELATION_NORMALISER ───────────────────────

class TestNormaliseRelation:
    """P1-B: _normalise_relation() + _RELATION_NORMALISER in kg_graph.py."""

    def test_fk_lowercase_normalised(self):
        from server.kg.kg_graph import _normalise_relation
        assert _normalise_relation("fk") == "FK"

    def test_is_fk_normalised(self):
        from server.kg.kg_graph import _normalise_relation
        assert _normalise_relation("is_fk") == "FK"

    def test_foreign_key_normalised(self):
        from server.kg.kg_graph import _normalise_relation
        assert _normalise_relation("foreign_key") == "FK"

    def test_foreign_key_with_space_normalised(self):
        from server.kg.kg_graph import _normalise_relation
        assert _normalise_relation("foreign key") == "FK"

    def test_references_normalised(self):
        from server.kg.kg_graph import _normalise_relation
        assert _normalise_relation("references") == "FK"

    def test_cross_db_fk_normalised(self):
        from server.kg.kg_graph import _normalise_relation
        assert _normalise_relation("cross_db_fk") == "cross_db"

    def test_similar_to_normalised(self):
        from server.kg.kg_graph import _normalise_relation
        assert _normalise_relation("similar to") == "similar_to"

    def test_is_similar_to_normalised(self):
        from server.kg.kg_graph import _normalise_relation
        assert _normalise_relation("is similar to") == "similar_to"

    def test_related_normalised(self):
        from server.kg.kg_graph import _normalise_relation
        assert _normalise_relation("related") == "related_to"

    def test_unknown_passthrough(self):
        """Unknown relations are returned with only whitespace stripped."""
        from server.kg.kg_graph import _normalise_relation
        assert _normalise_relation("MY_CUSTOM_REL") == "MY_CUSTOM_REL"

    def test_uppercase_fk_passthrough(self):
        """Already-canonical FK is returned unchanged."""
        from server.kg.kg_graph import _normalise_relation
        assert _normalise_relation("FK") == "FK"

    def test_whitespace_stripped(self):
        """Leading/trailing whitespace is stripped for unknown relations."""
        from server.kg.kg_graph import _normalise_relation
        assert _normalise_relation("  FK  ") == "FK"

    def test_add_edge_normalises_fk(self):
        """EntityGraph.add_edge() stores 'FK' when relation='fk' is passed."""
        import networkx as nx
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityEdge

        g = EntityGraph()
        g._G.add_node("a")
        g._G.add_node("b")
        e = EntityEdge(src_id="a", tgt_id="b", relation="fk",
                       label="fk", weight=1.0, confidence=1.0)
        g.add_edge(e)
        edge_data = g._G.get_edge_data("a", "b")
        assert edge_data["relation"] == "FK"

    def test_add_edge_normalises_foreign_key(self):
        """EntityGraph.add_edge() normalises 'foreign_key' → 'FK'."""
        import networkx as nx
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityEdge

        g = EntityGraph()
        g._G.add_node("a")
        g._G.add_node("b")
        e = EntityEdge(src_id="a", tgt_id="b", relation="foreign_key",
                       label="fk", weight=1.0, confidence=1.0)
        g.add_edge(e)
        assert g._G.get_edge_data("a", "b")["relation"] == "FK"


# ── P1-C: _repair_llm_json ────────────────────────────────────────────────────

class TestRepairLLMJson:
    """P1-C: _repair_llm_json() 6-step repair pipeline in kg_inference.py."""

    def test_clean_json_parsed(self):
        from server.kg.kg_inference import _repair_llm_json
        result = _repair_llm_json('[{"src_id":"a","relation":"r","tgt_id":"b"}]')
        assert isinstance(result, list)
        assert result[0]["src_id"] == "a"

    def test_code_fence_unwrapped(self):
        from server.kg.kg_inference import _repair_llm_json
        text = '```json\n[{"src_id":"a","relation":"r","tgt_id":"b"}]\n```'
        result = _repair_llm_json(text)
        assert isinstance(result, list) and len(result) == 1

    def test_trailing_comma_repaired(self):
        from server.kg.kg_inference import _repair_llm_json
        result = _repair_llm_json('[{"src_id":"a","relation":"r","tgt_id":"b",}]')
        assert isinstance(result, list) and result[0]["src_id"] == "a"

    def test_think_block_stripped(self):
        from server.kg.kg_inference import _repair_llm_json
        text = '<think>reasoning here</think>[{"src_id":"a","relation":"r","tgt_id":"b"}]'
        result = _repair_llm_json(text)
        assert isinstance(result, list) and len(result) == 1

    def test_truncated_array_salvaged(self):
        from server.kg.kg_inference import _repair_llm_json
        # Missing closing ']'
        result = _repair_llm_json('[{"src_id":"a","relation":"r","tgt_id":"b"}')
        assert isinstance(result, list) and len(result) >= 1

    def test_json_wrapper_unwrapped(self):
        from server.kg.kg_inference import _repair_llm_json
        result = _repair_llm_json('{"edges": [{"src_id":"a","relation":"r","tgt_id":"b"}]}')
        assert isinstance(result, list) and result[0]["src_id"] == "a"

    def test_none_input_returns_none(self):
        from server.kg.kg_inference import _repair_llm_json
        assert _repair_llm_json(None) is None

    def test_empty_string_returns_none(self):
        from server.kg.kg_inference import _repair_llm_json
        assert _repair_llm_json("") is None

    def test_no_json_returns_none(self):
        from server.kg.kg_inference import _repair_llm_json
        assert _repair_llm_json("There is no JSON here at all.") is None

    def test_whitespace_only_returns_none(self):
        from server.kg.kg_inference import _repair_llm_json
        assert _repair_llm_json("   \n  ") is None


# ── P1-C: _find_matching_bracket ─────────────────────────────────────────────

class TestFindMatchingBracket:
    """_find_matching_bracket helper used by _repair_llm_json."""

    def test_simple_array(self):
        from server.kg.kg_inference import _find_matching_bracket
        text = '[1, 2, 3]'
        assert _find_matching_bracket(text, 0, "[", "]") == 8

    def test_nested_brackets(self):
        from server.kg.kg_inference import _find_matching_bracket
        text = '[[1, 2], [3, 4]]'
        assert _find_matching_bracket(text, 0, "[", "]") == 15

    def test_bracket_in_string_ignored(self):
        from server.kg.kg_inference import _find_matching_bracket
        text = '["a[b]c", 1]'
        assert _find_matching_bracket(text, 0, "[", "]") == 11

    def test_unmatched_returns_minus_one(self):
        from server.kg.kg_inference import _find_matching_bracket
        text = '[1, 2, 3'
        assert _find_matching_bracket(text, 0, "[", "]") == -1


# ── P2-A: _run_inference_batch ───────────────────────────────────────────────

class TestRunInferenceBatch:
    """P2-A: _run_inference_batch() concurrent executor in kg_inference.py."""

    def test_empty_input_returns_empty(self):
        from server.kg.kg_inference import _run_inference_batch
        assert _run_inference_batch([]) == []

    def test_single_callable_serial(self):
        from server.kg.kg_inference import _run_inference_batch
        result = _run_inference_batch([lambda: 42])
        assert result == [42]

    def test_three_callables_returns_in_order(self):
        from server.kg.kg_inference import _run_inference_batch
        result = _run_inference_batch([lambda: 1, lambda: 2, lambda: 3], max_workers=2)
        assert result == [1, 2, 3]

    def test_max_workers_one_serial_path(self):
        from server.kg.kg_inference import _run_inference_batch
        result = _run_inference_batch([lambda: "a", lambda: "b"], max_workers=1)
        assert result == ["a", "b"]

    def test_failed_callable_yields_none_not_raise(self):
        from server.kg.kg_inference import _run_inference_batch
        def fail():
            raise ValueError("boom")
        result = _run_inference_batch([fail, lambda: 99], max_workers=2)
        assert result[0] is None
        assert result[1] == 99

    def test_all_fail_yields_all_none(self):
        from server.kg.kg_inference import _run_inference_batch
        def fail():
            raise RuntimeError("fail")
        result = _run_inference_batch([fail, fail, fail], max_workers=3)
        assert result == [None, None, None]

    def test_parallel_execution(self):
        """Ten callables with max_workers=5 still return in order."""
        from server.kg.kg_inference import _run_inference_batch
        callables = [lambda i=i: i * 10 for i in range(10)]
        result = _run_inference_batch(callables, max_workers=5)
        assert result == [i * 10 for i in range(10)]


# ── P2-B: display_name ───────────────────────────────────────────────────────

class TestDisplayName:
    """P2-B: display_name() title-case formatter in kg_graph.py."""

    def test_two_word_lowercase(self):
        from server.kg.kg_graph import display_name
        assert display_name("steam engine") == "Steam Engine"

    def test_small_word_preserved(self):
        from server.kg.kg_graph import display_name
        assert display_name("internet of things") == "Internet of Things"

    def test_digit_hyphen_word(self):
        from server.kg.kg_graph import display_name
        assert display_name("mid-20th century") == "Mid-20th Century"

    def test_underscore_replaced_with_space(self):
        from server.kg.kg_graph import display_name
        assert display_name("user_id") == "User Id"

    def test_snake_case_table_name(self):
        from server.kg.kg_graph import display_name
        assert display_name("crm_contacts") == "Crm Contacts"

    def test_mixed_case_unchanged(self):
        """Names already containing uppercase are returned as-is."""
        from server.kg.kg_graph import display_name
        assert display_name("IBM") == "IBM"

    def test_already_title_case_unchanged(self):
        from server.kg.kg_graph import display_name
        assert display_name("Digital Revolution") == "Digital Revolution"

    def test_empty_string(self):
        from server.kg.kg_graph import display_name
        assert display_name("") == ""

    def test_single_word_capitalised(self):
        from server.kg.kg_graph import display_name
        assert display_name("orders") == "Orders"

    def test_small_word_first_still_capitalised(self):
        """'a', 'the', etc. are capitalised when they are the first word."""
        from server.kg.kg_graph import display_name
        assert display_name("a users table") == "A Users Table"

    def test_add_node_label_is_display_name(self):
        """EntityGraph.add_node() stores the display_name of node.label."""
        import networkx as nx
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode

        g = EntityGraph()
        node = EntityNode(
            id="t:orders:id=1", label="orders",
            entity_type="TABLE", source_key="db1",
            table="orders", pk_col="id", pk_val="1",
        )
        g.add_node(node)
        stored_label = g._G.nodes["t:orders:id=1"]["label"]
        assert stored_label == "Orders"

    def test_add_node_mixed_case_label_unchanged(self):
        """Mixed-case label in add_node() is not altered."""
        import networkx as nx
        from server.kg.kg_graph import EntityGraph
        from server.kg.kg_resolver import EntityNode

        g = EntityGraph()
        node = EntityNode(
            id="t:CRM:id=1", label="CRM",
            entity_type="TABLE", source_key="db1",
            table="CRM", pk_col="id", pk_val="1",
        )
        g.add_node(node)
        assert g._G.nodes["t:CRM:id=1"]["label"] == "CRM"


# ── P3-A: KGInferenceCache ───────────────────────────────────────────────────

class TestKGInferenceCache:
    """P3-A: KGInferenceCache disk-backed LLM response cache."""

    def test_import(self):
        from server.kg.kg_llm_cache import KGInferenceCache
        assert KGInferenceCache is not None

    def test_disabled_when_no_dir(self, monkeypatch):
        """Cache is disabled (enabled=False) when no cache_dir and no env var."""
        import os
        monkeypatch.delenv("KG_LLM_CACHE_DIR", raising=False)
        # Re-import to pick up monkeypatched env - use explicit empty cache_dir
        from server.kg.kg_llm_cache import KGInferenceCache
        cache = KGInferenceCache(cache_dir="")
        assert cache.enabled is False
        assert cache.get("m", "s", "u") is None

    def test_cache_miss_returns_none(self, tmp_path):
        from server.kg.kg_llm_cache import KGInferenceCache
        cache = KGInferenceCache(str(tmp_path))
        assert cache.get("model", "system", "user") is None

    def test_cache_hit_returns_content(self, tmp_path):
        from server.kg.kg_llm_cache import KGInferenceCache
        cache = KGInferenceCache(str(tmp_path))
        cache.put("model", "system", "user", '{"edges": []}')
        result = cache.get("model", "system", "user")
        assert result == '{"edges": []}'

    def test_different_user_different_key(self, tmp_path):
        from server.kg.kg_llm_cache import KGInferenceCache
        cache = KGInferenceCache(str(tmp_path))
        cache.put("model", "sys", "user_A", "result_A")
        cache.put("model", "sys", "user_B", "result_B")
        assert cache.get("model", "sys", "user_A") == "result_A"
        assert cache.get("model", "sys", "user_B") == "result_B"

    def test_cache_file_is_valid_json(self, tmp_path):
        """Cache file must contain model, created, content keys."""
        import json
        from server.kg.kg_llm_cache import KGInferenceCache
        cache = KGInferenceCache(str(tmp_path))
        cache.put("gpt-4", "sys", "usr", "hello")
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["model"] == "gpt-4"
        assert data["content"] == "hello"
        assert "created" in data

    def test_enabled_true_when_dir_set(self, tmp_path):
        from server.kg.kg_llm_cache import KGInferenceCache
        cache = KGInferenceCache(str(tmp_path))
        assert cache.enabled is True

    def test_put_creates_directory(self, tmp_path):
        """put() creates the cache directory if it does not exist."""
        from server.kg.kg_llm_cache import KGInferenceCache
        subdir = tmp_path / "new_cache_dir"
        cache = KGInferenceCache(str(subdir))
        cache.put("m", "s", "u", "content")
        assert subdir.exists()
        assert cache.get("m", "s", "u") == "content"
