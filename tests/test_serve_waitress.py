"""tests/test_serve_waitress.py
Verify that nybble.serve selects Waitress when it is importable, and falls
back to Flask's dev server when it is not.

These are pure unit tests - no network, no Flask app start.
"""
import importlib
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


class TestHasWaitress(unittest.TestCase):
    """_has_waitress() returns True iff waitress is importable."""

    def test_returns_true_when_waitress_importable(self):
        """Patch sys.modules so waitress appears installed."""
        fake_waitress = types.ModuleType("waitress")
        fake_waitress.serve = MagicMock()
        with patch.dict(sys.modules, {"waitress": fake_waitress}):
            from nybble.serve import _has_waitress
            assert _has_waitress() is True

    def test_returns_false_when_waitress_not_installed(self):
        """Remove waitress from sys.modules to simulate it being absent."""
        saved = sys.modules.pop("waitress", None)
        try:
            # Also remove from the nybble.serve module's cached import
            import nybble.serve as serve_mod
            importlib.reload(serve_mod)
            # Force ImportError path by temporarily hiding waitress
            with patch.dict(sys.modules, {"waitress": None}):
                assert serve_mod._has_waitress() is False
        finally:
            if saved is not None:
                sys.modules["waitress"] = saved

    def test_returns_false_when_import_raises(self):
        """ImportError from waitress is caught and returns False."""
        import nybble.serve as serve_mod

        # builtins.__import__ receives (name, globals, locals, fromlist, level)
        _real_import = __import__

        def _raise(name, *args, **kwargs):
            if name == "waitress":
                raise ImportError("waitress not found")
            return _real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_raise):
            result = serve_mod._has_waitress()
        assert result is False


class TestServeAppSelectsWaitress(unittest.TestCase):
    """serve_app() calls waitress.serve() when Waitress is available."""

    def _make_fake_flask_app(self):
        app = MagicMock()
        app.run = MagicMock()
        return app

    def test_waitress_serve_called_when_available(self):
        """When _has_waitress() is True, waitress.serve() is invoked."""
        import nybble.serve as serve_mod

        fake_waitress = types.ModuleType("waitress")
        fake_waitress.serve = MagicMock()

        fake_app = self._make_fake_flask_app()

        with patch.object(serve_mod, "_has_waitress", return_value=True), \
             patch.object(serve_mod, "validate_port", return_value=7700), \
             patch.object(serve_mod, "_configure_env"), \
             patch.object(serve_mod, "_load_sources_for_serve", return_value=[]), \
             patch.object(serve_mod, "get_flask_app", return_value=fake_app), \
             patch.dict(sys.modules, {"waitress": fake_waitress}):
            serve_mod.serve_app(
                host="127.0.0.1",
                port=7700,
                sources=(),
                config_path=None,
                open_browser=False,
            )

        fake_waitress.serve.assert_called_once_with(
            fake_app, host="127.0.0.1", port=7700
        )
        fake_app.run.assert_not_called()

    def test_flask_dev_server_used_when_waitress_absent(self):
        """When _has_waitress() is False, app.run() is invoked instead."""
        import nybble.serve as serve_mod

        fake_app = self._make_fake_flask_app()

        with patch.object(serve_mod, "_has_waitress", return_value=False), \
             patch.object(serve_mod, "validate_port", return_value=7700), \
             patch.object(serve_mod, "_configure_env"), \
             patch.object(serve_mod, "_load_sources_for_serve", return_value=[]), \
             patch.object(serve_mod, "get_flask_app", return_value=fake_app):
            serve_mod.serve_app(
                host="127.0.0.1",
                port=7700,
                sources=(),
                config_path=None,
                open_browser=False,
            )

        fake_app.run.assert_called_once_with(
            host="127.0.0.1", port=7700, threaded=True, use_reloader=False
        )

    def test_banner_reports_waitress_backend_name(self, capsys=None):
        """The startup banner string says 'Waitress' when Waitress is selected."""
        import nybble.serve as serve_mod

        fake_waitress = types.ModuleType("waitress")
        fake_waitress.serve = MagicMock()
        fake_app = self._make_fake_flask_app()

        printed = []

        class FakeConsole:
            def print(self, msg, *a, **kw):
                printed.append(msg)

        # Console is imported *inside* serve_app from rich.console - patch it there.
        with patch.object(serve_mod, "_has_waitress", return_value=True), \
             patch.object(serve_mod, "validate_port", return_value=7700), \
             patch.object(serve_mod, "_configure_env"), \
             patch.object(serve_mod, "_load_sources_for_serve", return_value=[]), \
             patch.object(serve_mod, "get_flask_app", return_value=fake_app), \
             patch.dict(sys.modules, {"waitress": fake_waitress}), \
             patch("rich.console.Console", return_value=FakeConsole()):
            serve_mod.serve_app(
                host="127.0.0.1",
                port=7700,
                open_browser=False,
            )

        banner = "\n".join(printed)
        assert "Waitress" in banner, f"Expected 'Waitress' in banner, got: {banner!r}"


if __name__ == "__main__":
    unittest.main()
