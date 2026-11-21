"""
tests/test_serve.py - Unit tests for nybble/serve.py (P2-4)

All tests are unit-level - no actual servers are started, no ports are bound.
Flask app import is mocked where needed so the tests pass without the server/
directory being importable in all environments.
"""
from __future__ import annotations

import importlib
import sys
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _import_serve():
    """Re-import nybble.serve fresh (avoids cached state between tests)."""
    import nybble.serve as m
    return m


# ─────────────────────────────────────────────────────────────────────────────
# Port validation
# ─────────────────────────────────────────────────────────────────────────────

class TestValidatePort:
    def test_valid_port_returned(self):
        from nybble.serve import validate_port
        assert validate_port(7700) == 7700

    def test_valid_port_1(self):
        from nybble.serve import validate_port
        assert validate_port(1) == 1

    def test_valid_port_65535(self):
        from nybble.serve import validate_port
        assert validate_port(65535) == 65535

    def test_port_zero_raises(self):
        from nybble.serve import validate_port
        with pytest.raises(ValueError, match="1 and 65535"):
            validate_port(0)

    def test_port_negative_raises(self):
        from nybble.serve import validate_port
        with pytest.raises(ValueError, match="1 and 65535"):
            validate_port(-1)

    def test_port_too_high_raises(self):
        from nybble.serve import validate_port
        with pytest.raises(ValueError, match="1 and 65535"):
            validate_port(65536)

    def test_non_integer_string_raises(self):
        from nybble.serve import validate_port
        with pytest.raises(ValueError, match="integer"):
            validate_port("abc")

    def test_none_raises(self):
        from nybble.serve import validate_port
        with pytest.raises(ValueError, match="integer"):
            validate_port(None)

    def test_string_integer_coerced(self):
        """String representations of valid integers are accepted."""
        from nybble.serve import validate_port
        assert validate_port("8080") == 8080

    def test_float_string_raises(self):
        from nybble.serve import validate_port
        with pytest.raises(ValueError, match="integer"):
            validate_port("7.5")


# ─────────────────────────────────────────────────────────────────────────────
# find_server_dir
# ─────────────────────────────────────────────────────────────────────────────

class TestFindServerDir:
    def test_finds_server_dir_in_repo(self):
        """In the dev/editable install the server/ dir sits next to nybble/."""
        from nybble.serve import find_server_dir
        d = find_server_dir()
        assert d.is_dir()
        assert (d / "app" / "api_server.py").exists()

    def test_raises_when_server_dir_missing(self, tmp_path, monkeypatch):
        """RuntimeError raised with a helpful message when server/ is absent."""
        import nybble.serve as serve_mod

        # Patch __file__ so the sibling-dir search points at tmp_path (no server/ there)
        monkeypatch.setattr(serve_mod, "__file__", str(tmp_path / "serve.py"))
        # Also clear sys.path of any entries that contain server/app/api_server.py
        clean_path = [
            p for p in sys.path
            if not (Path(p) / "server" / "app" / "api_server.py").exists()
        ]
        monkeypatch.setattr(sys, "path", clean_path)

        with pytest.raises(RuntimeError, match="server directory could not be found"):
            serve_mod.find_server_dir()


# ─────────────────────────────────────────────────────────────────────────────
# _has_waitress
# ─────────────────────────────────────────────────────────────────────────────

class TestHasWaitress:
    def test_returns_true_when_waitress_importable(self):
        from nybble.serve import _has_waitress

        fake_waitress = types.ModuleType("waitress")
        with patch.dict(sys.modules, {"waitress": fake_waitress}):
            assert _has_waitress() is True

    def test_returns_false_when_waitress_absent(self):
        from nybble.serve import _has_waitress

        with patch.dict(sys.modules, {"waitress": None}):
            assert _has_waitress() is False

    def test_returns_false_when_waitress_import_error(self):
        """Simulate waitress not installed at all (not even in sys.modules)."""
        from nybble.serve import _has_waitress

        original = sys.modules.pop("waitress", _SENTINEL := object())
        try:
            # Force an ImportError by removing waitress and hiding it
            with patch("builtins.__import__", side_effect=_make_import_raiser("waitress")):
                result = _has_waitress()
            assert result is False
        finally:
            if original is not _SENTINEL:
                sys.modules["waitress"] = original  # type: ignore[assignment]


def _make_import_raiser(blocked: str):
    """Return a side_effect for __import__ that raises ImportError for *blocked*."""
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def _patched(name, *args, **kwargs):
        if name == blocked:
            raise ImportError(f"No module named '{blocked}'")
        return real_import(name, *args, **kwargs)

    return _patched


# ─────────────────────────────────────────────────────────────────────────────
# get_flask_app
# ─────────────────────────────────────────────────────────────────────────────

class TestGetFlaskApp:
    def test_raises_import_error_when_flask_missing(self, monkeypatch):
        with patch.dict(sys.modules, {"flask": None}):
            import nybble.serve as serve_mod
            with pytest.raises(ImportError, match="Flask is required"):
                serve_mod.get_flask_app()

    def test_returns_flask_app_object(self):
        """When server/ is on the path, get_flask_app() returns the app object."""
        from nybble.serve import get_flask_app
        app = get_flask_app()
        # The Flask app object has a 'run' callable
        assert callable(getattr(app, "run", None))

    def test_server_dir_added_to_sys_path(self):
        """get_flask_app() ensures the repo root is on sys.path."""
        from nybble.serve import get_flask_app, find_server_dir
        get_flask_app()
        repo_root = str(find_server_dir().parent)
        assert repo_root in sys.path


# ─────────────────────────────────────────────────────────────────────────────
# _load_sources_for_serve - config fallback
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadSourcesForServe:
    @pytest.fixture(autouse=True)
    def _reset_session(self, tmp_path, monkeypatch):
        """Reset DuckDB session between tests."""
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

    def test_no_sources_no_config_returns_empty(self, tmp_path, monkeypatch):
        """When no sources and no config exist, returns empty list silently."""
        from nybble.serve import _load_sources_for_serve
        # Point find_config to an empty directory
        monkeypatch.chdir(tmp_path)
        result = _load_sources_for_serve((), config_path=None)
        assert result == []

    def test_explicit_source_loaded(self, tmp_path, monkeypatch):
        """A --source flag loads the relation and returns a label."""
        from nybble.serve import _load_sources_for_serve
        monkeypatch.chdir(tmp_path)

        csv = tmp_path / "data.csv"
        csv.write_text("id,name\n1,Alice\n2,Bob\n")

        result = _load_sources_for_serve((f"mydata={csv}",), config_path=None)
        assert len(result) == 1
        assert "mydata" in result[0]
        assert "rows" in result[0]

    def test_named_source_uses_custom_name(self, tmp_path, monkeypatch):
        """name=path format assigns the custom table name."""
        from nybble.serve import _load_sources_for_serve
        monkeypatch.chdir(tmp_path)

        csv = tmp_path / "t.csv"
        csv.write_text("x,y\n1,2\n")

        result = _load_sources_for_serve((f"mytable={csv}",), config_path=None)
        assert "mytable" in result[0]

    def test_bare_source_path_loaded(self, tmp_path, monkeypatch):
        """A bare path (no name=) loads using the inferred table name."""
        from nybble.serve import _load_sources_for_serve
        monkeypatch.chdir(tmp_path)

        csv = tmp_path / "sales.csv"
        csv.write_text("a,b\n1,2\n")

        result = _load_sources_for_serve((str(csv),), config_path=None)
        assert len(result) == 1

    def test_config_file_not_found_raises(self, tmp_path, monkeypatch):
        """Explicit config path that doesn't exist raises FileNotFoundError."""
        from nybble.serve import _load_sources_for_serve
        monkeypatch.chdir(tmp_path)

        with pytest.raises(FileNotFoundError, match="Config file not found"):
            _load_sources_for_serve((), config_path=str(tmp_path / "missing.yaml"))

    def test_nybble_yaml_auto_discovered(self, tmp_path, monkeypatch):
        """When nybble.yaml is present in cwd, sources are loaded automatically."""
        from nybble.serve import _load_sources_for_serve
        monkeypatch.chdir(tmp_path)

        csv = tmp_path / "customers.csv"
        csv.write_text("id,name\n1,Alice\n")

        config = tmp_path / "nybble.yaml"
        config.write_text(
            f"version: '1.0'\nfiles:\n  - id: customers\n    type: csv\n    path: {csv}\n"
        )

        result = _load_sources_for_serve((), config_path=None)
        assert len(result) == 1
        assert "customers" in result[0]

    def test_multiple_sources_all_loaded(self, tmp_path, monkeypatch):
        """Multiple --source flags are all loaded and labelled."""
        from nybble.serve import _load_sources_for_serve
        monkeypatch.chdir(tmp_path)

        a = tmp_path / "a.csv"
        a.write_text("id,val\n1,x\n")
        b = tmp_path / "b.csv"
        b.write_text("id,val\n2,y\n")

        result = _load_sources_for_serve(
            (f"ta={a}", f"tb={b}"),
            config_path=None,
        )
        assert len(result) == 2
        names = [r.split()[0] for r in result]
        assert "ta" in names
        assert "tb" in names


# ─────────────────────────────────────────────────────────────────────────────
# serve_app - Waitress / Flask-dev path selection (no actual server start)
# ─────────────────────────────────────────────────────────────────────────────

class TestServeAppBackendSelection:
    """Verify which WSGI backend is chosen based on Waitress availability.

    serve_app() is blocking so we patch the actual serve calls and assert
    which one was invoked.
    """

    @pytest.fixture(autouse=True)
    def _reset_session(self, tmp_path, monkeypatch):
        import nybble.resolver as r
        if r._SESSION is not None:
            try:
                r._SESSION.close()
            except Exception:
                pass
            r._SESSION = None
        r._REGISTERED.clear()
        monkeypatch.chdir(tmp_path)
        yield
        if r._SESSION is not None:
            try:
                r._SESSION.close()
            except Exception:
                pass
            r._SESSION = None
        r._REGISTERED.clear()

    def test_waitress_path_when_waitress_importable(self, monkeypatch):
        """When Waitress is importable, waitress.serve() is called."""
        import nybble.serve as serve_mod

        fake_flask_app = MagicMock()
        monkeypatch.setattr(serve_mod, "get_flask_app", lambda: fake_flask_app)
        monkeypatch.setattr(serve_mod, "_has_waitress", lambda: True)

        fake_waitress_mod = MagicMock()
        serve_called = []

        def fake_waitress_serve(app, host, port):
            serve_called.append((host, port))

        fake_waitress_mod.serve = fake_waitress_serve

        with patch.dict(sys.modules, {"waitress": fake_waitress_mod}):
            serve_mod.serve_app(
                host="127.0.0.1",
                port=7700,
                sources=(),
                config_path=None,
                open_browser=False,
            )

        assert len(serve_called) == 1
        assert serve_called[0] == ("127.0.0.1", 7700)
        fake_flask_app.run.assert_not_called()

    def test_flask_dev_path_when_waitress_absent(self, monkeypatch):
        """When Waitress is absent, flask_app.run() is called."""
        import nybble.serve as serve_mod

        fake_flask_app = MagicMock()
        monkeypatch.setattr(serve_mod, "get_flask_app", lambda: fake_flask_app)
        monkeypatch.setattr(serve_mod, "_has_waitress", lambda: False)

        serve_mod.serve_app(
            host="127.0.0.1",
            port=7700,
            sources=(),
            config_path=None,
            open_browser=False,
        )

        fake_flask_app.run.assert_called_once_with(
            host="127.0.0.1",
            port=7700,
            threaded=True,
            use_reloader=False,
        )

    def test_keyboard_interrupt_exits_cleanly(self, monkeypatch):
        """KeyboardInterrupt during serve is caught - no exception propagates."""
        import nybble.serve as serve_mod

        fake_flask_app = MagicMock()
        fake_flask_app.run.side_effect = KeyboardInterrupt()
        monkeypatch.setattr(serve_mod, "get_flask_app", lambda: fake_flask_app)
        monkeypatch.setattr(serve_mod, "_has_waitress", lambda: False)

        # Should not raise
        serve_mod.serve_app(
            host="127.0.0.1",
            port=7700,
            sources=(),
            config_path=None,
            open_browser=False,
        )

    def test_keyboard_interrupt_with_waitress_exits_cleanly(self, monkeypatch):
        """KeyboardInterrupt from Waitress is also caught cleanly."""
        import nybble.serve as serve_mod

        fake_flask_app = MagicMock()
        monkeypatch.setattr(serve_mod, "get_flask_app", lambda: fake_flask_app)
        monkeypatch.setattr(serve_mod, "_has_waitress", lambda: True)

        fake_waitress_mod = MagicMock()
        fake_waitress_mod.serve.side_effect = KeyboardInterrupt()

        with patch.dict(sys.modules, {"waitress": fake_waitress_mod}):
            # Should not raise
            serve_mod.serve_app(
                host="127.0.0.1",
                port=7700,
                sources=(),
                config_path=None,
                open_browser=False,
            )

    def test_open_browser_false_no_thread(self, monkeypatch):
        """When open_browser=False, no Timer thread is started."""
        import nybble.serve as serve_mod

        fake_flask_app = MagicMock()
        fake_flask_app.run.side_effect = KeyboardInterrupt()
        monkeypatch.setattr(serve_mod, "get_flask_app", lambda: fake_flask_app)
        monkeypatch.setattr(serve_mod, "_has_waitress", lambda: False)

        timer_calls = []
        original_timer = threading.Timer

        def mock_timer(interval, func, *args, **kwargs):
            timer_calls.append(interval)
            return original_timer(interval, func, *args, **kwargs)

        with patch("nybble.serve.threading.Timer", side_effect=mock_timer):
            serve_mod.serve_app(
                host="127.0.0.1",
                port=7700,
                sources=(),
                config_path=None,
                open_browser=False,
            )

        assert timer_calls == []

    def test_open_browser_true_schedules_timer(self, monkeypatch):
        """When open_browser=True, a threading.Timer is started."""
        import nybble.serve as serve_mod

        fake_flask_app = MagicMock()
        fake_flask_app.run.side_effect = KeyboardInterrupt()
        monkeypatch.setattr(serve_mod, "get_flask_app", lambda: fake_flask_app)
        monkeypatch.setattr(serve_mod, "_has_waitress", lambda: False)

        timer_calls = []

        class _FakeTimer:
            def __init__(self, interval, func, *a, **kw):
                timer_calls.append(interval)
            def start(self):
                pass

        with patch("nybble.serve.threading.Timer", _FakeTimer):
            serve_mod.serve_app(
                host="127.0.0.1",
                port=7700,
                sources=(),
                config_path=None,
                open_browser=True,
            )

        assert len(timer_calls) == 1
        assert timer_calls[0] == 1.5


# ─────────────────────────────────────────────────────────────────────────────
# CLI integration - argument parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestCLIServeCommand:
    """Test that the CLI 'serve' command parses arguments correctly and
    delegates to serve_app() with the right parameters."""

    def test_serve_help_exits_zero(self):
        """nybble serve --help exits with code 0."""
        from click.testing import CliRunner
        from nybble.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--port" in result.output
        assert "--source" in result.output
        assert "--no-browser" in result.output

    def test_serve_help_mentions_waitress(self):
        """Help text references Waitress."""
        from click.testing import CliRunner
        from nybble.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["serve", "--help"])
        assert "Waitress" in result.output or "waitress" in result.output

    def test_invalid_port_string_exits_nonzero(self):
        """Non-integer port string causes immediate exit with error message."""
        from click.testing import CliRunner
        from nybble.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["serve", "--port", "abc"])
        # Click itself rejects non-integer values for integer options
        assert result.exit_code != 0

    def test_serve_delegates_to_serve_app(self, monkeypatch, tmp_path):
        """cmd_serve calls serve_app() with correct arguments."""
        from click.testing import CliRunner
        from nybble.cli import cli

        calls = []

        def fake_serve_app(**kwargs):
            calls.append(kwargs)

        monkeypatch.chdir(tmp_path)

        import nybble.serve as serve_mod
        monkeypatch.setattr(serve_mod, "serve_app", fake_serve_app)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "serve",
            "--port", "8080",
            "--host", "0.0.0.0",
            "--no-browser",
        ])

        assert len(calls) == 1
        assert calls[0]["port"] == 8080
        assert calls[0]["host"] == "0.0.0.0"
        assert calls[0]["open_browser"] is False

    def test_serve_passes_source_flags(self, monkeypatch, tmp_path):
        """--source flags are forwarded to serve_app as a tuple."""
        from click.testing import CliRunner
        from nybble.cli import cli

        monkeypatch.chdir(tmp_path)
        calls = []

        def fake_serve_app(**kwargs):
            calls.append(kwargs)

        import nybble.serve as serve_mod
        monkeypatch.setattr(serve_mod, "serve_app", fake_serve_app)

        csv = tmp_path / "a.csv"
        csv.write_text("x\n1\n")

        runner = CliRunner()
        result = runner.invoke(cli, [
            "serve",
            "--source", f"mydata={csv}",
            "--no-browser",
        ])

        assert len(calls) == 1
        assert len(calls[0]["sources"]) == 1
        assert f"mydata={csv}" in calls[0]["sources"]

    def test_serve_passes_config_flag(self, monkeypatch, tmp_path):
        """--config flag is forwarded to serve_app as config_path."""
        from click.testing import CliRunner
        from nybble.cli import cli

        monkeypatch.chdir(tmp_path)
        calls = []

        def fake_serve_app(**kwargs):
            calls.append(kwargs)

        import nybble.serve as serve_mod
        monkeypatch.setattr(serve_mod, "serve_app", fake_serve_app)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "serve",
            "--config", "my_workspace.yaml",
            "--no-browser",
        ])

        assert len(calls) == 1
        assert calls[0]["config_path"] == "my_workspace.yaml"


# ─────────────────────────────────────────────────────────────────────────────
# Importability
# ─────────────────────────────────────────────────────────────────────────────

class TestServeModuleImportable:
    def test_serve_importable_from_nybble(self):
        """nybble.serve is importable as a module."""
        import nybble.serve  # noqa: F401

    def test_serve_app_exported(self):
        """serve_app is a callable in nybble.serve."""
        from nybble.serve import serve_app
        assert callable(serve_app)

    def test_validate_port_exported(self):
        """validate_port is a callable in nybble.serve."""
        from nybble.serve import validate_port
        assert callable(validate_port)

    def test_find_server_dir_exported(self):
        """find_server_dir is a callable in nybble.serve."""
        from nybble.serve import find_server_dir
        assert callable(find_server_dir)

    def test_get_flask_app_exported(self):
        """get_flask_app is a callable in nybble.serve."""
        from nybble.serve import get_flask_app
        assert callable(get_flask_app)
