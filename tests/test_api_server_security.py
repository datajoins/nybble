"""tests/test_api_server_security.py

Unit tests for the security-hardening additions made in Items 4 and 5:
  - _validate_conn_field (Item 4 - connection-string injection prevention)
  - _safe_sort           (Item 5 - sort column whitelist consistency)
"""
import importlib
import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Module import helper
# We need to import the Flask app module without starting Flask itself.
# The module-level code registers routes but doesn't call app.run(), so a
# plain import is safe.  We cache the module to avoid duplicate imports.
# ---------------------------------------------------------------------------

def _get_api_module():
    """Return the server.app.api_server module, importing it once."""
    import os
    import tempfile

    mod_name = "server.app.api_server"
    if mod_name in sys.modules:
        return sys.modules[mod_name]

    # Redirect Docker-only default paths to a writable temp directory so the
    # module-level mkdir() calls don't raise PermissionError in CI.
    _tmp = tempfile.mkdtemp(prefix="nybble_ci_")
    os.environ.setdefault("UPLOAD_DIR", _tmp + "/uploads")
    os.environ.setdefault("OUTPUT_DIR", _tmp + "/output/erd")
    os.environ.setdefault("REGISTRY",   _tmp + "/output/registry.json")

    # Ensure the package path is importable.
    import importlib.util
    spec = importlib.util.find_spec(mod_name)
    if spec is None:
        pytest.skip("server.app.api_server not importable in this environment")
    try:
        return importlib.import_module(mod_name)
    except ImportError as exc:
        pytest.skip(f"server.app.api_server dependencies not installed: {exc}")
    except OSError as exc:
        pytest.skip(f"server.app.api_server could not initialise directories: {exc}")


# ===========================================================================
# Item 4 - _validate_conn_field
# ===========================================================================

class TestValidateConnField:
    """_validate_conn_field rejects dangerous characters in DB connection params."""

    @pytest.fixture(autouse=True)
    def _load(self):
        m = _get_api_module()
        self.validate = m._validate_conn_field
        self.SAFE_HOST = m._SAFE_HOST_RE
        self.SAFE_USER = m._SAFE_USERNAME_RE
        self.SAFE_DBNAME = m._SAFE_DBNAME_RE

    # ── Host validation ──────────────────────────────────────────────────────

    def test_valid_hostname(self):
        """Normal DNS hostnames must pass without raising."""
        for h in [
            "localhost",
            "db.example.com",
            "my-db-server.internal",
            "prod.rds.amazonaws.com",
            "myserver.database.windows.net",
        ]:
            self.validate(h, self.SAFE_HOST, "host")  # must not raise

    def test_valid_ipv4(self):
        self.validate("192.168.1.100", self.SAFE_HOST, "host")

    def test_valid_ipv6_bracket(self):
        self.validate("[::1]", self.SAFE_HOST, "host")
        self.validate("[2001:db8::1]", self.SAFE_HOST, "host")

    def test_empty_host_passes(self):
        """Empty strings are allowed - required-field checks are the caller's job."""
        self.validate("", self.SAFE_HOST, "host")

    def test_host_with_at_sign_rejected(self):
        """@ can redirect a PDO/SQLAlchemy DSN to a different server."""
        with pytest.raises(ValueError, match="host"):
            self.validate("evil@attacker.com", self.SAFE_HOST, "host")

    def test_host_with_semicolon_rejected(self):
        """Semicolons terminate DSN option groups (e.g., ODBC)."""
        with pytest.raises(ValueError):
            self.validate("localhost;password=x", self.SAFE_HOST, "host")

    def test_host_with_space_rejected(self):
        with pytest.raises(ValueError):
            self.validate("local host", self.SAFE_HOST, "host")

    def test_host_with_null_byte_rejected(self):
        with pytest.raises(ValueError):
            self.validate("host\x00injected", self.SAFE_HOST, "host")

    def test_host_with_newline_rejected(self):
        with pytest.raises(ValueError):
            self.validate("host\nX-Injected: header", self.SAFE_HOST, "host")

    # ── Username validation ──────────────────────────────────────────────────

    def test_valid_username(self):
        for u in ["admin", "db_user", "user.name", "user@domain", "my-user"]:
            self.validate(u, self.SAFE_USER, "user")

    def test_username_with_quote_rejected(self):
        with pytest.raises(ValueError):
            self.validate("user' OR '1'='1", self.SAFE_USER, "user")

    def test_username_with_slash_rejected(self):
        with pytest.raises(ValueError):
            self.validate("../etc/passwd", self.SAFE_USER, "user")

    # ── Database name validation ──────────────────────────────────────────────

    def test_valid_dbname(self):
        for d in ["mydb", "my_database", "prod-db", "app/schema", "db.v2"]:
            self.validate(d, self.SAFE_DBNAME, "database")

    def test_dbname_with_at_sign_rejected(self):
        with pytest.raises(ValueError):
            self.validate("mydb@host", self.SAFE_DBNAME, "database")

    def test_dbname_with_semicolon_rejected(self):
        with pytest.raises(ValueError):
            self.validate("mydb;DROP TABLE users", self.SAFE_DBNAME, "database")


# ===========================================================================
# Item 5 - _safe_sort
# ===========================================================================

class TestSafeSort:
    """_safe_sort returns a validated (column, direction) tuple."""

    @pytest.fixture(autouse=True)
    def _load(self):
        m = _get_api_module()
        self.safe_sort = m._safe_sort
        self.allowed = ["name", "created_at", "file_size", "id"]

    def test_valid_asc(self):
        col, direction = self.safe_sort("name", self.allowed, "id")
        assert col == "name"
        assert direction == "asc"

    def test_valid_desc_prefix(self):
        col, direction = self.safe_sort("-created_at", self.allowed, "id")
        assert col == "created_at"
        assert direction == "desc"

    def test_none_returns_default(self):
        col, direction = self.safe_sort(None, self.allowed, "id")
        assert col == "id"
        assert direction == "asc"

    def test_empty_string_returns_default(self):
        col, direction = self.safe_sort("", self.allowed, "id")
        assert col == "id"
        assert direction == "asc"

    def test_invalid_column_falls_back_to_default(self):
        col, direction = self.safe_sort("'; DROP TABLE--", self.allowed, "name")
        assert col == "name"
        assert direction == "asc"

    def test_sql_keyword_as_column_falls_back(self):
        col, direction = self.safe_sort("SELECT", self.allowed, "id")
        assert col == "id"

    def test_nonexistent_column_falls_back(self):
        col, direction = self.safe_sort("nonexistent_col", self.allowed, "created_at")
        assert col == "created_at"

    def test_desc_on_invalid_column_falls_back(self):
        col, direction = self.safe_sort("-EVIL", self.allowed, "id")
        assert col == "id"
        # direction is whatever the leading - implies - we care the column was rejected
        assert direction in ("asc", "desc")

    def test_whitespace_stripped(self):
        col, direction = self.safe_sort("  name  ", self.allowed, "id")
        # name with surrounding spaces - after strip it matches
        assert col == "name"

    def test_desc_with_whitespace(self):
        col, direction = self.safe_sort(" -file_size ", self.allowed, "id")
        assert col == "file_size"
        assert direction == "desc"
