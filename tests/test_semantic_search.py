"""
tests/test_semantic_search.py - Tests for P2-3: semantic / substring node search

All tests run without sentence-transformers or torch installed (substring mode).
Semantic mode is tested via mocking - no model download, no torch required in CI.
"""
from __future__ import annotations

import json as _json
import sys
import types
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
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

class _FakeTensor(list):
    """Minimal tensor-like mock: list-backed, supports indexing and .tolist()."""
    def __init__(self, data):
        super().__init__(data)
        self._data = data

    def __getitem__(self, idx):
        row = self._data[idx]
        return _FakeTensor(row) if isinstance(row, list) else row

    def tolist(self):
        return list(self._data)

    @property
    def shape(self):
        if self._data and isinstance(self._data[0], list):
            return (len(self._data), len(self._data[0]))
        return (len(self._data),)


def _make_fake_torch():
    """Return a minimal torch module mock (no actual torch required)."""
    fake_torch = types.ModuleType("torch")
    fake_torch.ones = lambda n, m=None: _FakeTensor(
        [[1.0] * m for _ in range(n)] if m else [1.0] * n
    )
    fake_torch.tensor = lambda data: _FakeTensor(data)
    return fake_torch


def _make_mock_st(scores: list[float]):
    """Build fake sentence_transformers + torch modules with preset cosine scores."""
    fake_torch = _make_fake_torch()
    fake_st = types.ModuleType("sentence_transformers")
    fake_util = types.ModuleType("sentence_transformers.util")

    class FakeModel:
        def __init__(self, model_name=None):
            pass
        def encode(self, texts, convert_to_tensor=False, show_progress_bar=False):
            return _FakeTensor([[1.0] * 4 for _ in texts])

    def fake_cos_sim(q_emb, corpus_emb):
        return _FakeTensor([scores])

    fake_st.SentenceTransformer = FakeModel
    fake_util.cos_sim = fake_cos_sim
    fake_st.util = fake_util
    return fake_st, fake_util, fake_torch


def _extract_json(output: str) -> str:
    """Strip any Rich/progress preamble and extract the JSON portion."""
    # Find first '[' or '{' and return from there
    for i, ch in enumerate(output):
        if ch in ("[", "{"):
            return output[i:]
    return output


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def csv_customers(tmp_path):
    p = tmp_path / "customers.csv"
    p.write_text(
        "id,name,country\n"
        "1,Alice Martin,US\n"
        "2,Bob Chen,CA\n"
        "3,Alice Dupont,FR\n"
        "4,Charlie Brown,US\n"
    )
    return str(p)


@pytest.fixture
def tiny_graph() -> nx.MultiDiGraph:
    """A manually constructed graph with known labels for deterministic testing."""
    G = nx.MultiDiGraph()
    G.add_node("customers:1", label="Alice Martin",  entity_type="customers", pk_val="1", row={"id": 1, "name": "Alice Martin",  "country": "US"})
    G.add_node("customers:2", label="Bob Chen",      entity_type="customers", pk_val="2", row={"id": 2, "name": "Bob Chen",      "country": "CA"})
    G.add_node("customers:3", label="Alice Dupont",  entity_type="customers", pk_val="3", row={"id": 3, "name": "Alice Dupont",  "country": "FR"})
    G.add_node("customers:4", label="Charlie Brown", entity_type="customers", pk_val="4", row={"id": 4, "name": "Charlie Brown", "country": "US"})
    G.add_node("orders:101",  label="Order-101",     entity_type="orders",    pk_val="101", row={})
    return G


# ─────────────────────────────────────────────────────────────────────────────
# NodeSearchResult dataclass
# ─────────────────────────────────────────────────────────────────────────────

class TestNodeSearchResult:
    def test_fields_accessible(self):
        from nybble.kg import NodeSearchResult
        r = NodeSearchResult(
            node_id="t:1", label="foo", entity_type="t",
            score=0.9, match_mode="substring"
        )
        assert r.node_id == "t:1"
        assert r.label == "foo"
        assert r.entity_type == "t"
        assert r.score == 0.9
        assert r.match_mode == "substring"

    def test_row_defaults_to_empty_dict(self):
        from nybble.kg import NodeSearchResult
        r = NodeSearchResult(node_id="t:1", label="x", entity_type="t",
                             score=1.0, match_mode="substring")
        assert r.row == {}

    def test_importable_from_nybble(self):
        from nybble import NodeSearchResult  # noqa: F401

    def test_search_nodes_importable_from_nybble(self):
        from nybble import search_nodes  # noqa: F401


# ─────────────────────────────────────────────────────────────────────────────
# _has_sentence_transformers
# ─────────────────────────────────────────────────────────────────────────────

class TestHasSentenceTransformers:
    def test_returns_false_when_absent(self):
        from nybble.kg import _has_sentence_transformers
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            assert _has_sentence_transformers() is False

    def test_returns_true_when_present(self):
        from nybble.kg import _has_sentence_transformers
        fake = types.ModuleType("sentence_transformers")
        with patch.dict(sys.modules, {"sentence_transformers": fake}):
            assert _has_sentence_transformers() is True


# ─────────────────────────────────────────────────────────────────────────────
# _substring_search
# ─────────────────────────────────────────────────────────────────────────────

class TestSubstringSearch:
    def test_exact_match_score_1(self, tiny_graph):
        from nybble.kg import _substring_search
        results = _substring_search("Alice Martin", tiny_graph, top_k=10)
        assert len(results) == 1
        assert results[0].node_id == "customers:1"
        assert results[0].score == 1.0
        assert results[0].match_mode == "substring"

    def test_case_insensitive_exact_score_08(self, tiny_graph):
        from nybble.kg import _substring_search
        results = _substring_search("alice martin", tiny_graph, top_k=10)
        assert len(results) == 1
        assert results[0].score == 0.8

    def test_substring_match_score_05(self, tiny_graph):
        from nybble.kg import _substring_search
        results = _substring_search("alice", tiny_graph, top_k=10)
        assert len(results) == 2
        for r in results:
            assert r.score == 0.5
            assert "alice" in r.label.lower()

    def test_no_match_returns_empty(self, tiny_graph):
        from nybble.kg import _substring_search
        results = _substring_search("XYZNOTFOUND", tiny_graph, top_k=10)
        assert results == []

    def test_top_k_limits_results(self, tiny_graph):
        from nybble.kg import _substring_search
        results = _substring_search("a", tiny_graph, top_k=1)
        assert len(results) == 1

    def test_results_sorted_by_score_desc(self, tiny_graph):
        from nybble.kg import _substring_search
        results = _substring_search("Alice Martin", tiny_graph, top_k=10)
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_empty_graph_returns_empty(self):
        from nybble.kg import _substring_search
        G = nx.MultiDiGraph()
        results = _substring_search("anything", G, top_k=10)
        assert results == []

    def test_row_is_preserved(self, tiny_graph):
        from nybble.kg import _substring_search
        results = _substring_search("Alice Martin", tiny_graph, top_k=10)
        assert results[0].row["country"] == "US"

    def test_entity_type_is_preserved(self, tiny_graph):
        from nybble.kg import _substring_search
        results = _substring_search("Alice Martin", tiny_graph, top_k=10)
        assert results[0].entity_type == "customers"


# ─────────────────────────────────────────────────────────────────────────────
# search_nodes - public API (substring mode, no sentence-transformers)
# ─────────────────────────────────────────────────────────────────────────────

class TestSearchNodesSubstringMode:
    def test_empty_query_returns_empty(self, tiny_graph):
        from nybble.kg import search_nodes
        assert search_nodes("", graph=tiny_graph) == []

    def test_whitespace_only_query_returns_empty(self, tiny_graph):
        from nybble.kg import search_nodes
        assert search_nodes("   ", graph=tiny_graph) == []

    def test_finds_nodes_by_label(self, tiny_graph):
        from nybble.kg import search_nodes
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            results = search_nodes("Alice", graph=tiny_graph, top_k=10)
        assert len(results) == 2
        labels = {r.label for r in results}
        assert "Alice Martin" in labels
        assert "Alice Dupont" in labels

    def test_match_mode_is_substring(self, tiny_graph):
        from nybble.kg import search_nodes
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            results = search_nodes("Bob", graph=tiny_graph, top_k=10)
        assert all(r.match_mode == "substring" for r in results)

    def test_top_k_respected(self, tiny_graph):
        from nybble.kg import search_nodes
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            results = search_nodes("a", graph=tiny_graph, top_k=1)
        assert len(results) <= 1

    def test_no_match_returns_empty(self, tiny_graph):
        from nybble.kg import search_nodes
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            results = search_nodes("ZZZNOMATCH", graph=tiny_graph)
        assert results == []

    def test_builds_graph_from_registered_when_graph_none(self, csv_customers, tmp_path, monkeypatch):
        """When graph=None, search_nodes builds a graph from loaded relations."""
        from nybble.resolver import load as _load
        from nybble.kg import search_nodes
        monkeypatch.chdir(tmp_path)
        _load(csv_customers, name="customers")

        with patch.dict(sys.modules, {"sentence_transformers": None}):
            results = search_nodes("Alice", top_k=10)

        assert len(results) >= 1
        assert all("alice" in r.label.lower() for r in results)

    def test_raises_when_no_relations_loaded(self, tmp_path, monkeypatch):
        from nybble.kg import search_nodes
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError, match="No relations loaded"):
            search_nodes("anything")

    def test_empty_graph_returns_empty(self):
        from nybble.kg import search_nodes
        G = nx.MultiDiGraph()
        results = search_nodes("anything", graph=G)
        assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# search_nodes - semantic mode (all mocked, no torch/sentence-transformers needed)
# ─────────────────────────────────────────────────────────────────────────────

class TestSearchNodesSemanticMode:
    """Verify semantic path is taken and cosine similarity scores are used.

    Both sentence-transformers and torch are fully mocked - no packages needed.
    """

    def _kg_module(self):
        """Return the nybble.kg module object (not the kg() function)."""
        import sys as _sys
        return _sys.modules["nybble.kg"]

    def test_semantic_mode_returns_results(self, tiny_graph):
        from nybble.kg import search_nodes
        kg_mod = self._kg_module()

        scores = [0.3, 0.9, 0.2, 0.1, 0.4]
        fake_st, fake_util, fake_torch = _make_mock_st(scores)
        with patch.dict(sys.modules, {
            "sentence_transformers": fake_st,
            "sentence_transformers.util": fake_util,
            "torch": fake_torch,
        }), patch.object(kg_mod, "_has_sentence_transformers", return_value=True):
            results = search_nodes("bob", graph=tiny_graph, top_k=3)

        assert len(results) == 3
        assert all(r.match_mode == "semantic" for r in results)

    def test_semantic_scores_are_sorted_desc(self, tiny_graph):
        from nybble.kg import search_nodes
        kg_mod = self._kg_module()

        scores = [0.5, 0.9, 0.1, 0.3, 0.7]
        fake_st, fake_util, fake_torch = _make_mock_st(scores)
        with patch.dict(sys.modules, {
            "sentence_transformers": fake_st,
            "sentence_transformers.util": fake_util,
            "torch": fake_torch,
        }), patch.object(kg_mod, "_has_sentence_transformers", return_value=True):
            results = search_nodes("test", graph=tiny_graph, top_k=5)

        result_scores = [r.score for r in results]
        assert result_scores == sorted(result_scores, reverse=True)

    def test_semantic_top_k_respected(self, tiny_graph):
        from nybble.kg import search_nodes
        kg_mod = self._kg_module()

        scores = [0.1, 0.2, 0.3, 0.4, 0.5]
        fake_st, fake_util, fake_torch = _make_mock_st(scores)
        with patch.dict(sys.modules, {
            "sentence_transformers": fake_st,
            "sentence_transformers.util": fake_util,
            "torch": fake_torch,
        }), patch.object(kg_mod, "_has_sentence_transformers", return_value=True):
            results = search_nodes("test", graph=tiny_graph, top_k=2)

        assert len(results) == 2

    def test_semantic_failure_falls_back_to_substring(self, tiny_graph):
        """If _semantic_search raises, search_nodes falls back to substring."""
        kg_mod = self._kg_module()
        from nybble.kg import search_nodes

        with patch.object(kg_mod, "_has_sentence_transformers", return_value=True), \
             patch.object(kg_mod, "_semantic_search", side_effect=RuntimeError("fail")):
            results = search_nodes("Alice", graph=tiny_graph, top_k=10)

        assert len(results) >= 1
        assert all(r.match_mode == "substring" for r in results)

    def test_model_name_override_via_env(self, tiny_graph, monkeypatch):
        """NYBBLE_EMBEDDING_MODEL env var is passed to SentenceTransformer."""
        kg_mod = self._kg_module()
        from nybble.kg import search_nodes

        monkeypatch.setenv("NYBBLE_EMBEDDING_MODEL", "custom-model-name")
        loaded_models: list[str] = []
        fake_torch = _make_fake_torch()

        fake_st = types.ModuleType("sentence_transformers")
        fake_util = types.ModuleType("sentence_transformers.util")

        # cos_sim must exist on fake_util or the import inside _semantic_search fails
        # before the model is even instantiated.
        def fake_cos_sim(q, c):
            raise RuntimeError("tracking only - fallback expected")
        fake_util.cos_sim = fake_cos_sim

        class TrackingModel:
            def __init__(self, model_name):
                loaded_models.append(model_name)
            def encode(self, texts, **kw):
                return _FakeTensor([[1.0] * 4 for _ in texts])

        fake_st.SentenceTransformer = TrackingModel
        fake_st.util = fake_util

        with patch.dict(sys.modules, {
            "sentence_transformers": fake_st,
            "sentence_transformers.util": fake_util,
            "torch": fake_torch,
        }), patch.object(kg_mod, "_has_sentence_transformers", return_value=True):
            # cos_sim raises → _semantic_search fails → falls back to substring
            # but TrackingModel.__init__ was called first with the env-var model name.
            search_nodes("test", graph=tiny_graph)

        assert "custom-model-name" in loaded_models


# ─────────────────────────────────────────────────────────────────────────────
# CLI integration: nybble kg search
# ─────────────────────────────────────────────────────────────────────────────

class TestCLIKgSearch:
    def test_kg_search_help_exits_zero(self):
        from click.testing import CliRunner
        from nybble.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["kg", "search", "--help"])
        assert result.exit_code == 0
        assert "QUERY" in result.output or "query" in result.output.lower()

    def test_kg_help_lists_search_subcommand(self):
        from click.testing import CliRunner
        from nybble.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["kg", "--help"])
        assert result.exit_code == 0
        assert "search" in result.output

    def test_kg_build_still_works_without_subcommand(self, csv_customers, tmp_path, monkeypatch):
        """nybble kg (no subcommand) still builds the graph as before."""
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "kg",
            "--source", f"customers={csv_customers}",
            "--no-browser",
        ])
        assert result.exit_code == 0
        assert "Knowledge Graph" in result.output or "nodes" in result.output

    def test_kg_summary_only_still_works(self, csv_customers, tmp_path, monkeypatch):
        """nybble kg --summary-only still works as a standalone invocation."""
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "kg",
            "--source", f"customers={csv_customers}",
            "--summary-only",
        ])
        assert result.exit_code == 0
        assert "node_count" in result.output or "source_count" in result.output

    def test_kg_search_finds_matching_nodes(self, csv_customers, tmp_path, monkeypatch):
        """nybble kg search returns matching node labels."""
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        with patch.dict(sys.modules, {"sentence_transformers": None}):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "kg", "search", "Alice",
                "--source", f"customers={csv_customers}",
            ])

        assert result.exit_code == 0
        assert "Alice" in result.output

    def test_kg_search_json_format(self, csv_customers, tmp_path, monkeypatch):
        """--format json emits valid JSON (after stripping any progress output)."""
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        with patch.dict(sys.modules, {"sentence_transformers": None}):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "kg", "search", "Alice",
                "--source", f"customers={csv_customers}",
                "--format", "json",
            ])

        assert result.exit_code == 0
        parsed = _json.loads(_extract_json(result.output))
        assert isinstance(parsed, list)
        assert len(parsed) >= 1
        first = parsed[0]
        assert "node_id" in first
        assert "label" in first
        assert "score" in first
        assert "match_mode" in first

    def test_kg_search_no_results_message(self, csv_customers, tmp_path, monkeypatch):
        """When no nodes match, prints a 'no matching nodes' message."""
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        with patch.dict(sys.modules, {"sentence_transformers": None}):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "kg", "search", "ZZZNOMATCH999",
                "--source", f"customers={csv_customers}",
            ])

        assert result.exit_code == 0
        output_lower = result.output.lower()
        assert "no matching" in output_lower

    def test_kg_search_top_k_option(self, csv_customers, tmp_path, monkeypatch):
        """--top-k limits the number of results."""
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        with patch.dict(sys.modules, {"sentence_transformers": None}):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "kg", "search", "a",
                "--source", f"customers={csv_customers}",
                "--top-k", "1",
                "--format", "json",
            ])

        assert result.exit_code == 0
        parsed = _json.loads(_extract_json(result.output))
        assert len(parsed) <= 1

    def test_kg_search_match_mode_substring_when_no_st(self, csv_customers, tmp_path, monkeypatch):
        """Without sentence-transformers, match_mode is 'substring'."""
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        with patch.dict(sys.modules, {"sentence_transformers": None}):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "kg", "search", "Alice",
                "--source", f"customers={csv_customers}",
                "--format", "json",
            ])

        assert result.exit_code == 0
        parsed = _json.loads(_extract_json(result.output))
        assert all(r["match_mode"] == "substring" for r in parsed)

    def test_kg_search_tip_shown_without_st(self, csv_customers, tmp_path, monkeypatch):
        """Without sentence-transformers, a tip to install it is shown."""
        from click.testing import CliRunner
        from nybble.cli import cli
        monkeypatch.chdir(tmp_path)

        with patch.dict(sys.modules, {"sentence_transformers": None}):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "kg", "search", "Alice",
                "--source", f"customers={csv_customers}",
            ])

        assert "sentence-transformers" in result.output or "semantic" in result.output


# ─────────────────────────────────────────────────────────────────────────────
# MCP server: nybble_kg_search tool (skipped when mcp package absent)
# ─────────────────────────────────────────────────────────────────────────────

import importlib as _importlib
_mcp_available = pytest.mark.skipif(
    _importlib.util.find_spec("mcp") is None,
    reason="mcp package not installed; skipping MCP tests",
)


@_mcp_available
class TestMCPKgSearch:
    def test_nybble_kg_search_in_tool_list(self):
        """nybble_kg_search is registered in the MCP server tool list."""
        import asyncio
        from nybble.mcp_server import list_tools
        tools = asyncio.get_event_loop().run_until_complete(list_tools())
        names = [t.name for t in tools]
        assert "nybble_kg_search" in names

    def test_nybble_kg_search_tool_has_query_param(self):
        """nybble_kg_search tool schema requires 'query'."""
        import asyncio
        from nybble.mcp_server import list_tools
        tools = asyncio.get_event_loop().run_until_complete(list_tools())
        tool = next(t for t in tools if t.name == "nybble_kg_search")
        assert "query" in tool.inputSchema["properties"]
        assert "query" in tool.inputSchema["required"]

    def test_mcp_dispatch_kg_search(self, csv_customers, tmp_path, monkeypatch):
        """Dispatching nybble_kg_search returns a list of dicts."""
        import asyncio
        from nybble.resolver import load as _load
        from nybble.mcp_server import _dispatch
        monkeypatch.chdir(tmp_path)

        _load(csv_customers, name="customers")

        with patch.dict(sys.modules, {"sentence_transformers": None}):
            result = asyncio.get_event_loop().run_until_complete(
                _dispatch("nybble_kg_search", {"query": "Alice", "top_k": 5})
            )

        assert isinstance(result, list)
        if result:
            first = result[0]
            assert "node_id" in first
            assert "label" in first
            assert "score" in first
            assert "match_mode" in first
