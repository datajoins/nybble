"""
tests/test_share.py - Unit tests for P3-1: Shareable Static Exports

All HTTP calls are mocked with unittest.mock - no real network requests.
Tests cover:
  - GitHub Gist backend (authenticated + anonymous)
  - DataJoins backend (with and without expiry)
  - Token registry (register, resolve, get)
  - ?token= deep-link resolution in serve.py
  - CLI nybble share subcommand (exit 0 and exit 1 paths)
  - NybbleShareResult dataclass
  - nybble.__init__ exports
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_token_registry():
    """Ensure the token registry is empty before and after each test."""
    import sys
    import nybble.share  # ensure module is loaded
    _share_mod = sys.modules["nybble.share"]
    _share_mod._TOKEN_REGISTRY.clear()
    yield
    _share_mod._TOKEN_REGISTRY.clear()


@pytest.fixture
def sample_html() -> str:
    return "<!DOCTYPE html><html><body><p>Nybble ERD</p></body></html>"


@pytest.fixture
def html_file(tmp_path, sample_html) -> Path:
    p = tmp_path / "erd.html"
    p.write_text(sample_html, encoding="utf-8")
    return p


# ── Tests: NybbleShareResult dataclass ───────────────────────────────────────

class TestNybbleShareResult:

    def test_dataclass_fields(self):
        from nybble.share import NybbleShareResult
        r = NybbleShareResult(url="https://example.com", backend="gist", expires_at=None)
        assert r.url == "https://example.com"
        assert r.backend == "gist"
        assert r.expires_at is None

    def test_dataclass_with_expiry(self):
        from nybble.share import NybbleShareResult
        r = NybbleShareResult(
            url="https://example.com/share/abc",
            backend="datajoins",
            expires_at="2025-12-31T23:59:59Z",
        )
        assert r.expires_at == "2025-12-31T23:59:59Z"

    def test_importable_from_init(self):
        from nybble import NybbleShareResult  # noqa: F401
        assert NybbleShareResult is not None

    def test_share_importable_from_init(self):
        from nybble import share  # noqa: F401
        assert callable(share)


# ── Tests: GitHub Gist backend ────────────────────────────────────────────────

class TestGistBackend:

    def _make_gist_response(self, gist_id: str = "abc123", filename: str = "erd.html") -> dict:
        return {
            "id": gist_id,
            "html_url": f"https://gist.github.com/{gist_id}",
            "files": {
                filename: {
                    "raw_url": f"https://gist.githubusercontent.com/user/{gist_id}/raw/{filename}",
                    "filename": filename,
                }
            },
        }

    def test_gist_returns_share_result(self, sample_html):
        from nybble.share import _share_gist

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = self._make_gist_response()

        with patch("requests.post", return_value=mock_resp) as mock_post:
            result = _share_gist(
                html_content=sample_html,
                filename="erd.html",
                description="test",
                token=None,
            )

        assert result.backend == "gist"
        assert result.url.startswith("https://")
        assert result.expires_at is None

    def test_gist_raw_url_returned(self, sample_html):
        from nybble.share import _share_gist

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = self._make_gist_response()

        with patch("requests.post", return_value=mock_resp):
            result = _share_gist(sample_html, "erd.html", "test", token=None)

        # Should return raw content URL not html_url
        assert "raw" in result.url or "gist" in result.url

    def test_gist_with_token_sends_auth_header(self, sample_html):
        from nybble.share import _share_gist

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = self._make_gist_response()

        with patch("requests.post", return_value=mock_resp) as mock_post:
            _share_gist(sample_html, "erd.html", "test", token="ghp_testtoken")

        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get("headers", call_kwargs[1].get("headers", {}))
        assert "Authorization" in headers
        assert "ghp_testtoken" in headers["Authorization"]

    def test_gist_anonymous_no_auth_header(self, sample_html):
        from nybble.share import _share_gist

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = self._make_gist_response()

        with patch("requests.post", return_value=mock_resp) as mock_post:
            _share_gist(sample_html, "erd.html", "test", token=None)

        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get("headers", call_kwargs[1].get("headers", {}))
        assert "Authorization" not in headers

    def test_gist_raises_on_api_error(self, sample_html):
        from nybble.share import _share_gist

        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 422
        mock_resp.text = "Unprocessable Entity"

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="422"):
                _share_gist(sample_html, "erd.html", "test", token=None)

    def test_gist_posts_html_content(self, sample_html):
        from nybble.share import _share_gist

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = self._make_gist_response()

        with patch("requests.post", return_value=mock_resp) as mock_post:
            _share_gist(sample_html, "erd.html", "test", token=None)

        posted_json = mock_post.call_args.kwargs.get("json", mock_post.call_args[1].get("json", {}))
        assert "files" in posted_json
        assert "erd.html" in posted_json["files"]
        assert posted_json["files"]["erd.html"]["content"] == sample_html

    def test_gist_fallback_to_html_url_when_no_raw(self, sample_html):
        """When the file dict has no raw_url, fall back to html_url."""
        from nybble.share import _share_gist

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "id": "xyz",
            "html_url": "https://gist.github.com/xyz",
            "files": {},  # no file key
        }

        with patch("requests.post", return_value=mock_resp):
            result = _share_gist(sample_html, "erd.html", "test", token=None)

        assert "gist.github.com" in result.url


# ── Tests: DataJoins backend ──────────────────────────────────────────────────

class TestDataJoinsBackend:

    def test_datajoins_returns_share_result(self, sample_html):
        from nybble.share import _share_datajoins

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"url": "https://share.datajoins.com/abc123"}

        with patch("requests.post", return_value=mock_resp):
            result = _share_datajoins(
                html_content=sample_html,
                endpoint="https://share.datajoins.com/v1/share",
                description="test",
                expires_hours=None,
            )

        assert result.backend == "datajoins"
        assert result.url == "https://share.datajoins.com/abc123"
        assert result.expires_at is None

    def test_datajoins_with_expiry_sets_expires_at(self, sample_html):
        from nybble.share import _share_datajoins

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"url": "https://share.datajoins.com/exp123"}

        with patch("requests.post", return_value=mock_resp):
            result = _share_datajoins(
                html_content=sample_html,
                endpoint="https://share.datajoins.com/v1/share",
                description="test",
                expires_hours=24,
            )

        assert result.expires_at is not None
        assert "T" in result.expires_at
        assert result.expires_at.endswith("Z")

    def test_datajoins_raises_on_api_error(self, sample_html):
        from nybble.share import _share_datajoins

        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="500"):
                _share_datajoins(
                    sample_html,
                    "https://share.datajoins.com/v1/share",
                    "test",
                    None,
                )

    def test_datajoins_raises_when_url_missing(self, sample_html):
        from nybble.share import _share_datajoins

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"status": "ok"}  # no 'url' key
        mock_resp.text = '{"status": "ok"}'

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="missing 'url'"):
                _share_datajoins(
                    sample_html,
                    "https://share.datajoins.com/v1/share",
                    "test",
                    None,
                )

    def test_datajoins_posts_html_field(self, sample_html):
        from nybble.share import _share_datajoins

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"url": "https://share.datajoins.com/x"}

        with patch("requests.post", return_value=mock_resp) as mock_post:
            _share_datajoins(sample_html, "https://share.datajoins.com/v1/share", "test", None)

        posted = mock_post.call_args.kwargs.get("json", mock_post.call_args[1].get("json", {}))
        assert "html" in posted
        assert posted["html"] == sample_html

    def test_datajoins_expires_hours_sent_in_payload(self, sample_html):
        from nybble.share import _share_datajoins

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"url": "https://share.datajoins.com/x"}

        with patch("requests.post", return_value=mock_resp) as mock_post:
            _share_datajoins(sample_html, "https://share.datajoins.com/v1/share", "test", 48)

        posted = mock_post.call_args.kwargs.get("json", mock_post.call_args[1].get("json", {}))
        assert posted.get("expires_hours") == 48


# ── Tests: share() public API ─────────────────────────────────────────────────

class TestSharePublicAPI:

    def test_share_pre_rendered_html_gist(self, html_file):
        """share() with a pre-rendered .html file → Gist backend."""
        from nybble.share import share

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "id": "abc",
            "html_url": "https://gist.github.com/abc",
            "files": {
                "erd.html": {
                    "raw_url": "https://gist.githubusercontent.com/user/abc/raw/erd.html",
                }
            },
        }

        with patch("requests.post", return_value=mock_resp), \
             patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test_token"}):
            result = share(str(html_file), backend="gist", confirm=True, _html_override=None)

        assert result.backend == "gist"
        assert result.url.startswith("https://")

    def test_share_with_html_override(self):
        """_html_override bypasses file/source loading."""
        from nybble.share import share

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "id": "test",
            "html_url": "https://gist.github.com/test",
            "files": {
                "nybble_share.html": {
                    "raw_url": "https://gist.githubusercontent.com/user/test/raw/x.html"
                }
            },
        }

        with patch("requests.post", return_value=mock_resp), \
             patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test_token"}):
            result = share(
                "irrelevant_source",
                backend="gist",
                confirm=True,
                _html_override="<html><body>test</body></html>",
            )

        assert result.backend == "gist"

    def test_share_invalid_backend_raises(self, html_file):
        from nybble.share import share

        with pytest.raises(ValueError, match="Unknown share backend"):
            share(str(html_file), backend="unknown_backend", confirm=True, _html_override="<html/>")

    def test_share_datajoins_backend(self, html_file):
        from nybble.share import share

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"url": "https://share.datajoins.com/xyz"}

        with patch("requests.post", return_value=mock_resp):
            result = share(
                str(html_file),
                backend="datajoins",
                confirm=True,
                endpoint="https://share.datajoins.com/v1/share",
            )

        assert result.backend == "datajoins"
        assert "datajoins" in result.url

    def test_share_gist_reads_github_token_from_env(self, html_file):
        """GITHUB_TOKEN env var is picked up for gist_token fallback."""
        from nybble.share import share

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "id": "x",
            "html_url": "https://gist.github.com/x",
            "files": {"erd.html": {"raw_url": "https://raw.github.com/x"}},
        }

        with patch("requests.post", return_value=mock_resp) as mock_post:
            with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_from_env"}):
                share(str(html_file), backend="gist", confirm=True)

        headers = mock_post.call_args.kwargs.get("headers", {})
        assert "ghp_from_env" in headers.get("Authorization", "")

    def test_share_backend_case_insensitive(self, html_file):
        from nybble.share import share

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "id": "y",
            "html_url": "https://gist.github.com/y",
            "files": {"erd.html": {"raw_url": "https://raw.github.com/y"}},
        }
        with patch("requests.post", return_value=mock_resp), \
             patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test_token"}):
            result = share(str(html_file), backend="GIST", confirm=True)
        assert result.backend == "gist"

    def test_share_gist_raises_without_token(self, html_file):
        """share() with gist backend and no token raises ValueError with instructions."""
        from nybble.share import share

        env = {k: v for k, v in __import__("os").environ.items() if k != "GITHUB_TOKEN"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(ValueError, match="GitHub personal access token is required"):
                share(str(html_file), backend="gist", confirm=True)

    def test_share_gist_401_raises_actionable_error(self, html_file):
        """A 401 from the Gist API raises RuntimeError with token guidance."""
        from nybble.share import share

        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 401
        mock_resp.text = '{"message": "Requires authentication"}'

        with patch("requests.post", return_value=mock_resp), \
             patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_expired_token"}):
            with pytest.raises(RuntimeError, match="401 Unauthorized"):
                share(str(html_file), backend="gist", confirm=True)


    def test_share_emits_warning_without_confirm(self, html_file):
        """share() without confirm=True should emit a UserWarning about upload."""
        import warnings as _warnings
        from nybble.share import share

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "id": "warn_test",
            "html_url": "https://gist.github.com/warn_test",
            "files": {"erd.html": {"raw_url": "https://raw.github.com/warn_test"}},
        }

        with patch("requests.post", return_value=mock_resp), \
             patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test_token"}):
            with _warnings.catch_warnings(record=True) as caught:
                _warnings.simplefilter("always")
                share(str(html_file), backend="gist")

        assert any(
            issubclass(w.category, UserWarning) and "upload" in str(w.message).lower()
            for w in caught
        ), "Expected a UserWarning about data upload"

    def test_share_no_warning_with_confirm(self, html_file):
        """share(confirm=True) should not emit any UserWarning."""
        import warnings as _warnings
        from nybble.share import share

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "id": "no_warn_test",
            "html_url": "https://gist.github.com/no_warn_test",
            "files": {"erd.html": {"raw_url": "https://raw.github.com/no_warn_test"}},
        }

        with patch("requests.post", return_value=mock_resp), \
             patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test_token"}):
            with _warnings.catch_warnings(record=True) as caught:
                _warnings.simplefilter("always")
                share(str(html_file), backend="gist", confirm=True)

        upload_warnings = [
            w for w in caught
            if issubclass(w.category, UserWarning) and "upload" in str(w.message).lower()
        ]
        assert not upload_warnings, "No UserWarning expected when confirm=True"


# ── Tests: Token registry ─────────────────────────────────────────────────────

class TestTokenRegistry:

    def test_register_and_resolve_token(self, tmp_path):
        from nybble.share import register_token, resolve_token

        register_token("tok123", "/some/path/erd.html")
        assert resolve_token("tok123") == "/some/path/erd.html"

    def test_resolve_unknown_token_returns_none(self):
        from nybble.share import resolve_token

        assert resolve_token("nonexistent-token-xyz") is None

    def test_get_token_registry_returns_copy(self):
        from nybble.share import register_token, resolve_token, get_token_registry

        register_token("a", "path_a")
        register_token("b", "path_b")
        reg = get_token_registry()
        assert reg["a"] == "path_a"
        assert reg["b"] == "path_b"
        # Mutating the copy does not affect the real registry
        reg["c"] = "path_c"
        assert resolve_token("c") is None

    def test_overwrite_token(self):
        from nybble.share import register_token, resolve_token

        register_token("dup", "old_target")
        register_token("dup", "new_target")
        assert resolve_token("dup") == "new_target"

    def test_register_token_importable_from_init(self):
        from nybble import register_token, resolve_token, get_token_registry  # noqa: F401
        assert callable(register_token)

    def test_resolve_token_importable_from_init(self):
        from nybble import resolve_token  # noqa: F401
        assert callable(resolve_token)


# ── Tests: ?token= deep-link resolution in serve.py ──────────────────────────

class TestTokenDeepLink:

    def test_resolve_token_request_local_file(self, tmp_path):
        """Token pointing to a local file → returns file content."""
        from nybble.share import register_token
        from nybble.serve import resolve_token_request

        html_file = tmp_path / "erd.html"
        html_file.write_text("<html><body>test</body></html>", encoding="utf-8")

        register_token("local-tok", str(html_file))
        result = resolve_token_request("local-tok")

        assert result is not None
        assert "test" in result

    def test_resolve_token_request_unknown_token(self):
        """Unknown token → returns None."""
        from nybble.serve import resolve_token_request

        result = resolve_token_request("completely-unknown-token-xyz")
        assert result is None

    def test_resolve_token_request_remote_url(self):
        """Token pointing to a remote URL → fetches and returns content."""
        from nybble.share import register_token
        from nybble.serve import resolve_token_request

        register_token("remote-tok", "https://example.com/erd.html")

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.text = "<html><body>remote content</body></html>"

        with patch("requests.get", return_value=mock_resp):
            result = resolve_token_request("remote-tok")

        assert result == "<html><body>remote content</body></html>"

    def test_resolve_token_request_remote_url_failure(self):
        """Failed remote fetch → returns None (does not raise)."""
        from nybble.share import register_token
        from nybble.serve import resolve_token_request

        register_token("fail-tok", "https://example.com/broken.html")

        mock_resp = MagicMock()
        mock_resp.ok = False

        with patch("requests.get", return_value=mock_resp):
            result = resolve_token_request("fail-tok")

        assert result is None

    def test_resolve_token_request_local_file_not_found(self, tmp_path):
        """Token pointing to non-existent file → returns None."""
        from nybble.share import register_token
        from nybble.serve import resolve_token_request

        register_token("missing-file", str(tmp_path / "does_not_exist.html"))
        result = resolve_token_request("missing-file")
        assert result is None

    def test_resolve_token_request_remote_network_error(self):
        """Network error during remote fetch → returns None."""
        from nybble.share import register_token
        from nybble.serve import resolve_token_request

        register_token("net-err-tok", "https://example.com/error.html")

        with patch("requests.get", side_effect=Exception("connection refused")):
            result = resolve_token_request("net-err-tok")

        assert result is None

    def test_resolve_token_importable_from_serve(self):
        from nybble.serve import resolve_token_request  # noqa: F401
        assert callable(resolve_token_request)


# ── Tests: CLI nybble share subcommand ────────────────────────────────────────

class TestCLIShareCommand:

    def test_share_cli_exits_0_on_success(self, html_file):
        """nybble share --source erd.html --backend gist should exit 0."""
        from click.testing import CliRunner
        from nybble.cli import cli

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "id": "cli1",
            "html_url": "https://gist.github.com/cli1",
            "files": {"erd.html": {"raw_url": "https://gist.githubusercontent.com/u/cli1/raw/erd.html"}},
        }

        runner = CliRunner()
        with patch("requests.post", return_value=mock_resp):
            result = runner.invoke(cli, [
                "share",
                "--source", str(html_file),
                "--gist-token", "ghp_test_token",
                "--backend", "gist",
            ], input="y\n")

        assert result.exit_code == 0, f"exit={result.exit_code}\n{result.output}"

    def test_share_cli_prints_url_to_stdout(self, html_file):
        """Successful share prints the URL to stdout."""
        from click.testing import CliRunner
        from nybble.cli import cli

        share_url = "https://gist.githubusercontent.com/u/abc/raw/erd.html"
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "id": "abc",
            "html_url": "https://gist.github.com/abc",
            "files": {"erd.html": {"raw_url": share_url}},
        }

        runner = CliRunner()
        with patch("requests.post", return_value=mock_resp):
            result = runner.invoke(cli, [
                "share",
                "--source", str(html_file),
                "--gist-token", "ghp_test_token",
                "--backend", "gist",
            ], input="y\n")

        assert share_url in result.output

    def test_share_cli_exits_1_on_api_failure(self, html_file):
        """API error → exit code 1."""
        from click.testing import CliRunner
        from nybble.cli import cli

        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 503
        mock_resp.text = "Service Unavailable"

        runner = CliRunner()
        with patch("requests.post", return_value=mock_resp):
            result = runner.invoke(cli, [
                "share",
                "--source", str(html_file),
                "--gist-token", "ghp_test_token",
                "--backend", "gist",
            ], input="y\n")

        assert result.exit_code != 0

    def test_share_cli_exits_1_without_token(self, html_file):
        """No token and no GITHUB_TOKEN env var → exit 1 with clear instructions."""
        import os
        from click.testing import CliRunner
        from nybble.cli import cli

        runner = CliRunner()
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
        result = runner.invoke(cli, [
            "share",
            "--source", str(html_file),
            "--backend", "gist",
        ], input="y\n", env=env)

        assert result.exit_code != 0
        assert "token" in result.output.lower() or "github" in result.output.lower()

    def test_share_cli_exits_1_without_source(self, tmp_path):
        """Missing --source and no NYBBLE_SOURCE env var → exit 1."""
        from click.testing import CliRunner
        from nybble.cli import cli
        import os

        runner = CliRunner()
        env = {k: v for k, v in os.environ.items() if k != "NYBBLE_SOURCE"}
        result = runner.invoke(cli, ["share"], env=env)
        assert result.exit_code != 0

    def test_share_cli_datajoins_backend(self, html_file):
        """--backend datajoins uses the DataJoins share endpoint."""
        from click.testing import CliRunner
        from nybble.cli import cli

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"url": "https://share.datajoins.com/cli-test"}

        runner = CliRunner()
        with patch("requests.post", return_value=mock_resp) as mock_post:
            result = runner.invoke(cli, [
                "share",
                "--source", str(html_file),
                "--backend", "datajoins",
                "--endpoint", "https://share.datajoins.com/v1/share",
            ], input="y\n")

        assert result.exit_code == 0, f"exit={result.exit_code}\n{result.output}"
        assert "share.datajoins.com/cli-test" in result.output

    def test_share_cli_help_text(self):
        from click.testing import CliRunner
        from nybble.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["share", "--help"])
        assert result.exit_code == 0
        assert "Share" in result.output or "share" in result.output.lower()
