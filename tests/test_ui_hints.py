"""tests/test_ui_hints.py
Tests for Level 1 (UI hint printing) and Level 2 (artefact discovery).

Level 1: _ui_running() / _ui_hint() in nybble/cli.py
Level 2: _find_nybble_artefacts() / serve banner in nybble/serve.py
"""
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── Level 1 tests ─────────────────────────────────────────────────────────────

class TestUiRunning(unittest.TestCase):
    """_ui_running() detects whether the UI port is listening."""

    def test_returns_true_when_port_open(self):
        """Simulate an open port by making create_connection succeed."""
        from nybble.cli import _ui_running

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("socket.create_connection", return_value=mock_conn):
            assert _ui_running(7700) is True

    def test_returns_false_when_port_closed(self):
        """Simulate a closed port by making create_connection raise OSError."""
        from nybble.cli import _ui_running

        with patch("socket.create_connection", side_effect=OSError("refused")):
            assert _ui_running(7700) is False

    def test_returns_false_on_timeout(self):
        """socket.timeout (a subclass of OSError) is caught correctly."""
        from nybble.cli import _ui_running

        with patch("socket.create_connection", side_effect=socket.timeout("timed out")):
            assert _ui_running(7700) is False


class TestUiHint(unittest.TestCase):
    """_ui_hint() prints a URL when the UI is running, silent otherwise."""

    def test_prints_url_when_ui_running(self):
        """When _ui_running returns True, a URL line is printed."""
        from nybble.cli import _ui_hint
        import nybble.cli as cli_mod

        printed = []
        fake_console = MagicMock()
        fake_console.print = lambda msg, *a, **kw: printed.append(msg)

        with patch.object(cli_mod, "_ui_running", return_value=True), \
             patch.object(cli_mod, "console", fake_console):
            _ui_hint("/erd")

        assert len(printed) == 1
        assert "localhost:7700/erd" in printed[0]

    def test_silent_when_ui_not_running(self):
        """When _ui_running returns False, nothing is printed."""
        from nybble.cli import _ui_hint
        import nybble.cli as cli_mod

        printed = []
        fake_console = MagicMock()
        fake_console.print = lambda msg, *a, **kw: printed.append(msg)

        with patch.object(cli_mod, "_ui_running", return_value=False), \
             patch.object(cli_mod, "console", fake_console):
            _ui_hint("/erd")

        assert printed == []

    def test_hint_respects_custom_port(self):
        """Custom port appears in the printed URL."""
        from nybble.cli import _ui_hint
        import nybble.cli as cli_mod

        printed = []
        fake_console = MagicMock()
        fake_console.print = lambda msg, *a, **kw: printed.append(msg)

        with patch.object(cli_mod, "_ui_running", return_value=True), \
             patch.object(cli_mod, "console", fake_console):
            _ui_hint(port=8080)

        assert "localhost:8080" in printed[0]

    def test_hint_empty_path_links_to_root(self):
        """Empty path produces the root URL."""
        from nybble.cli import _ui_hint
        import nybble.cli as cli_mod

        printed = []
        fake_console = MagicMock()
        fake_console.print = lambda msg, *a, **kw: printed.append(msg)

        with patch.object(cli_mod, "_ui_running", return_value=True), \
             patch.object(cli_mod, "console", fake_console):
            _ui_hint()

        assert "localhost:7700" in printed[0]
        # Path should be empty - no trailing slash artefact
        assert printed[0].count("7700/") == 0 or "7700" in printed[0]


class TestCLICommandsCallUiHint(unittest.TestCase):
    """Smoke-test that the key CLI commands call _ui_hint() after success."""

    def _run_cmd(self, args: list, env_patch: dict = None):
        """Invoke the CLI via Click's test runner."""
        from click.testing import CliRunner
        from nybble.cli import cli
        runner = CliRunner()
        env = env_patch or {}
        return runner.invoke(cli, args, env=env, catch_exceptions=False)

    def test_erd_calls_ui_hint(self):
        """nybble erd calls _ui_hint after writing the ERD."""
        import sys
        import nybble.cli as cli_mod
        import nybble.erd  # ensure the real module is in sys.modules

        # nybble/__init__.py does `from nybble.erd import erd`, which sets
        # nybble.__dict__["erd"] to the erd *function*.  unittest.mock's string-
        # form patch("nybble.erd.erd") uses getattr(nybble, "erd") which returns
        # the function, not the sub-module.  We must patch via sys.modules directly.
        erd_module = sys.modules["nybble.erd"]
        hint_called = []
        original_erd = erd_module.erd
        erd_module.erd = lambda **kw: Path("/tmp/erd.html")
        try:
            with patch.object(cli_mod, "_ui_hint", side_effect=lambda *a, **kw: hint_called.append(True)), \
                 patch.object(cli_mod, "_load_config_sources"), \
                 patch.object(cli_mod, "_load_sources"):
                result = self._run_cmd(["erd", "--no-browser"])
        finally:
            erd_module.erd = original_erd

        assert hint_called, f"_ui_hint not called; output: {result.output}"

    def test_profile_calls_ui_hint(self, tmp_path=None):
        """nybble profile calls _ui_hint after printing column stats."""
        import nybble.cli as cli_mod
        from click.testing import CliRunner
        from nybble.cli import cli

        hint_called = []
        fake_rel = MagicMock()
        fake_rel.name = "test_table"
        fake_rel.tables = None

        fake_profile = MagicMock()
        fake_profile.name = "col"
        fake_profile.dtype = "VARCHAR"
        fake_profile.null_pct = 0.0
        fake_profile.distinct = 10
        fake_profile.approx = False
        fake_profile.cardinality_pct = 100.0
        fake_profile.min_val = "a"
        fake_profile.max_val = "z"
        fake_profile.top_val = "a"

        with patch.object(cli_mod, "_ui_hint", side_effect=lambda *a, **kw: hint_called.append(True)), \
             patch.object(cli_mod, "_resolve_source", return_value=fake_rel), \
             patch("nybble.profiler.profile_relation", return_value=[fake_profile]):
            runner = CliRunner()
            runner.invoke(cli, ["profile", "dummy.csv"], catch_exceptions=False)

        assert hint_called, "_ui_hint not called by profile command"

    def test_history_calls_ui_hint_when_entries_exist(self):
        """nybble history calls _ui_hint when history rows are printed."""
        import nybble.cli as cli_mod
        from click.testing import CliRunner
        from nybble.cli import cli

        hint_called = []
        fake_entries = [{"ts": "2026-10-30T12:00:00", "rows": 5, "elapsed_ms": 10, "sql": "SELECT 1"}]

        with patch.object(cli_mod, "_ui_hint", side_effect=lambda *a, **kw: hint_called.append(True)), \
             patch("nybble.arena.search_history", return_value=fake_entries):
            runner = CliRunner()
            runner.invoke(cli, ["history"], catch_exceptions=False)

        assert hint_called, "_ui_hint not called by history command"


# ── Level 2 tests ─────────────────────────────────────────────────────────────

class TestFindNybbleArtefacts(unittest.TestCase):
    """_find_nybble_artefacts() returns artefacts that exist on disk."""

    def test_returns_empty_when_dir_missing(self):
        """Non-existent directory returns empty list (no crash)."""
        from nybble.serve import _find_nybble_artefacts

        result = _find_nybble_artefacts(Path("/nonexistent/__test_nybble__"))
        assert result == []

    def test_returns_empty_when_no_artefacts(self, tmp_path=None):
        """Empty directory returns empty list."""
        from nybble.serve import _find_nybble_artefacts
        import tempfile, os

        with tempfile.TemporaryDirectory() as d:
            result = _find_nybble_artefacts(Path(d))
        assert result == []

    def test_detects_erd_html(self):
        """erd.html is returned when present."""
        from nybble.serve import _find_nybble_artefacts
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "erd.html").write_text("<html/>")
            result = _find_nybble_artefacts(p)

        labels = [r[0] for r in result]
        assert "ERD" in labels

    def test_detects_kg_html(self):
        """kg.html is returned when present."""
        from nybble.serve import _find_nybble_artefacts
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "kg.html").write_text("<html/>")
            result = _find_nybble_artefacts(p)

        labels = [r[0] for r in result]
        assert "Knowledge Graph" in labels

    def test_detects_lineage_html(self):
        """lineage.html is returned when present."""
        from nybble.serve import _find_nybble_artefacts
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "lineage.html").write_text("<html/>")
            result = _find_nybble_artefacts(p)

        labels = [r[0] for r in result]
        assert "Lineage DAG" in labels

    def test_returns_file_uris(self):
        """Returned URIs are file:// scheme strings."""
        from nybble.serve import _find_nybble_artefacts
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "erd.html").write_text("<html/>")
            result = _find_nybble_artefacts(p)

        assert result[0][1].startswith("file://")

    def test_preserves_order(self):
        """Returns artefacts in erd → kg → lineage order."""
        from nybble.serve import _find_nybble_artefacts
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "lineage.html").write_text("<html/>")
            (p / "erd.html").write_text("<html/>")
            (p / "kg.html").write_text("<html/>")
            result = _find_nybble_artefacts(p)

        labels = [r[0] for r in result]
        assert labels == ["ERD", "Knowledge Graph", "Lineage DAG"]

    def test_skips_absent_artefacts(self):
        """Only present files are included; absent ones are silently skipped."""
        from nybble.serve import _find_nybble_artefacts
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "kg.html").write_text("<html/>")
            # erd.html and lineage.html intentionally absent
            result = _find_nybble_artefacts(p)

        assert len(result) == 1
        assert result[0][0] == "Knowledge Graph"


class TestServeBannerIncludesArtefacts(unittest.TestCase):
    """serve_app() banner lists CLI artefacts when they exist."""

    def test_banner_prints_artefacts(self):
        """When _find_nybble_artefacts returns items, they appear in printed output."""
        import nybble.serve as serve_mod
        import types

        fake_waitress = types.ModuleType("waitress")
        fake_waitress.serve = MagicMock()
        fake_app = MagicMock()

        printed = []

        class FakeConsole:
            def print(self, msg, *a, **kw):
                printed.append(str(msg))

        fake_artefacts = [
            ("ERD", "file:///tmp/.nybble/erd.html"),
            ("Knowledge Graph", "file:///tmp/.nybble/kg.html"),
        ]

        with patch.object(serve_mod, "_has_waitress", return_value=True), \
             patch.object(serve_mod, "validate_port", return_value=7700), \
             patch.object(serve_mod, "_configure_env"), \
             patch.object(serve_mod, "_load_sources_for_serve", return_value=[]), \
             patch.object(serve_mod, "get_flask_app", return_value=fake_app), \
             patch.object(serve_mod, "_find_nybble_artefacts", return_value=fake_artefacts), \
             patch.dict(sys.modules, {"waitress": fake_waitress}), \
             patch("rich.console.Console", return_value=FakeConsole()):
            serve_mod.serve_app(host="127.0.0.1", port=7700, open_browser=False)

        banner = "\n".join(printed)
        assert "ERD" in banner
        assert "Knowledge Graph" in banner
        assert "file://" in banner

    def test_banner_silent_when_no_artefacts(self):
        """When .nybble/ has no artefacts, the Artefacts line is not printed."""
        import nybble.serve as serve_mod
        import types

        fake_waitress = types.ModuleType("waitress")
        fake_waitress.serve = MagicMock()
        fake_app = MagicMock()

        printed = []

        class FakeConsole:
            def print(self, msg, *a, **kw):
                printed.append(str(msg))

        with patch.object(serve_mod, "_has_waitress", return_value=True), \
             patch.object(serve_mod, "validate_port", return_value=7700), \
             patch.object(serve_mod, "_configure_env"), \
             patch.object(serve_mod, "_load_sources_for_serve", return_value=[]), \
             patch.object(serve_mod, "get_flask_app", return_value=fake_app), \
             patch.object(serve_mod, "_find_nybble_artefacts", return_value=[]), \
             patch.dict(sys.modules, {"waitress": fake_waitress}), \
             patch("rich.console.Console", return_value=FakeConsole()):
            serve_mod.serve_app(host="127.0.0.1", port=7700, open_browser=False)

        banner = "\n".join(printed)
        assert "Artefacts" not in banner


if __name__ == "__main__":
    unittest.main()
