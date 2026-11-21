"""
conftest.py - Shared pytest configuration for the Nybble test suite.

Auto-skip behaviour
-------------------
Tests that import from the ``server.*`` namespace are only valid when the
private ``server/`` directory is present (it is excluded from the public
GitHub repo).  We wrap fixture setup and test execution so that any
``ModuleNotFoundError`` / ``ImportError`` for a ``server.*`` module is
converted to a pytest skip rather than a failure.
"""
import pathlib
import pytest


# True when the private server/ directory is present (private workspace).
_SERVER_AVAILABLE = (pathlib.Path(__file__).parent.parent / "server").is_dir()


def _is_server_error(exc: BaseException) -> bool:
    """Return True if *exc* is a missing server.* module error."""
    if not isinstance(exc, (ModuleNotFoundError, ImportError)):
        return False
    name = getattr(exc, "name", "") or ""
    msg = str(exc)
    return name.startswith("server") or "server." in msg or "'server'" in msg


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    """Skip tests whose fixtures fail with a server.* import error."""
    outcome = yield
    if outcome.excinfo is not None:
        exc = outcome.excinfo[1]
        if _is_server_error(exc):
            pytest.skip(f"server package not available: {exc}")


@pytest.hookimpl(hookwrapper=True)
def pytest_pyfunc_call(pyfuncitem):
    """Skip tests that fail with a server.* import error during execution."""
    outcome = yield
    if outcome.excinfo is not None:
        exc = outcome.excinfo[1]
        if _is_server_error(exc):
            pytest.skip(f"server package not available: {exc}")
