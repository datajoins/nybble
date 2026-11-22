"""
share.py - Shareable Static Export Backend

Closes the collaborative-workspace gap (P3-1) by providing shareable static
exports without requiring real-time collaboration infrastructure.

Supported backends
------------------
  gist     : Creates or updates an anonymous/authenticated GitHub Gist and
             returns the raw Gist URL.  Requires internet access.
             Optionally authenticates via GITHUB_TOKEN env var or --token.
  datajoins: POSTs the HTML payload to a configurable DataJoins-hosted share
             endpoint and returns the resulting share URL.

Public API
----------
  share(source, *, backend, output, gist_token, endpoint, description,
        expires_hours) → NybbleShareResult
"""
from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Union


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class NybbleShareResult:
    """Result of a successful :func:`share` call.

    Attributes
    ----------
    url        : str - the public URL where the shared content is accessible
    backend    : str - which backend was used (``"gist"`` or ``"datajoins"``)
    expires_at : str | None - ISO 8601 UTC expiry timestamp, or None if permanent
    """

    url: str
    backend: str
    expires_at: Optional[str]


# ── Token resolution (for deep-link support) ──────────────────────────────────

# Module-level token registry: token → file path or remote URL
_TOKEN_REGISTRY: dict[str, str] = {}


def register_token(token: str, target: str) -> None:
    """Register a deep-link token mapping to a local file path or remote URL.

    Parameters
    ----------
    token  : str - the token value used in ``?token=<value>``
    target : str - local file path (``/path/to/erd.html``) or remote URL
    """
    _TOKEN_REGISTRY[token] = target


def resolve_token(token: str) -> Optional[str]:
    """Resolve a deep-link token to its target path or URL.

    Parameters
    ----------
    token : str - the token to look up

    Returns
    -------
    str or None
        The registered target, or None if the token is not known.
    """
    return _TOKEN_REGISTRY.get(token)


def get_token_registry() -> dict[str, str]:
    """Return a copy of the current token → target registry."""
    return dict(_TOKEN_REGISTRY)


# ── GitHub Gist backend ───────────────────────────────────────────────────────

_GIST_API = "https://api.github.com/gists"


def _share_gist(
    html_content: str,
    filename: str,
    description: str,
    token: Optional[str],
) -> NybbleShareResult:
    """Upload *html_content* as a GitHub Gist and return a :class:`NybbleShareResult`.

    Parameters
    ----------
    html_content : str - full self-contained HTML string to share
    filename     : str - filename shown in the Gist (should end in ``.html``)
    description  : str - Gist description text
    token        : str | None - GitHub personal access token for authenticated
                   Gists (higher rate limits, private Gists possible).
                   When None an anonymous public Gist is created.

    Returns
    -------
    NybbleShareResult
        ``url`` is the raw Gist content URL for the HTML file
        (suitable for embedding or direct browser preview).
        ``expires_at`` is None (Gists are permanent until manually deleted).

    Raises
    ------
    RuntimeError
        If the Gist API returns an unexpected response.
    ImportError
        If ``requests`` is not installed.
    """
    import requests

    payload = {
        "description": description,
        "public": True,
        "files": {
            filename: {"content": html_content},
        },
    }

    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.post(_GIST_API, json=payload, headers=headers, timeout=30)
    if not resp.ok:
        if resp.status_code == 401:
            raise RuntimeError(
                "GitHub Gist API returned 401 Unauthorized.\n"
                "Your token may be expired, revoked, or missing the 'gist' scope.\n"
                "\n"
                "  • Check your token at: https://github.com/settings/tokens\n"
                "  • Required scope: gist\n"
                "  • Pass a new token:  nybble share --source ... --gist-token ghp_xxxx\n"
                "  • Or set env var:    export GITHUB_TOKEN=ghp_xxxx"
            )
        raise RuntimeError(
            f"GitHub Gist API returned {resp.status_code}: {resp.text[:200]}"
        )

    data = resp.json()
    gist_id = data.get("id", "")
    files = data.get("files", {})
    raw_url: str = ""

    # Prefer the raw URL for the specific file
    file_info = files.get(filename, {})
    raw_url = file_info.get("raw_url", "")

    if not raw_url:
        # Fall back to the Gist HTML URL
        raw_url = data.get("html_url", f"https://gist.github.com/{gist_id}")

    return NybbleShareResult(
        url=raw_url,
        backend="gist",
        expires_at=None,
    )


# ── DataJoins-hosted share backend ────────────────────────────────────────────

_DEFAULT_DATAJOINS_ENDPOINT = "https://share.datajoins.com/v1/share"


def _share_datajoins(
    html_content: str,
    endpoint: str,
    description: str,
    expires_hours: Optional[int],
) -> NybbleShareResult:
    """POST *html_content* to the DataJoins share endpoint.

    Parameters
    ----------
    html_content  : str - full self-contained HTML string to share
    endpoint      : str - full URL of the share API endpoint
    description   : str - human-readable description / title
    expires_hours : int | None - requested TTL in hours; None = no expiry

    Returns
    -------
    NybbleShareResult
        ``url`` is the resulting share URL.
        ``expires_at`` is an ISO 8601 UTC timestamp if ``expires_hours`` was set.

    Raises
    ------
    RuntimeError
        If the share endpoint returns an unexpected response.
    ImportError
        If ``requests`` is not installed.
    """
    import requests

    payload: dict = {
        "html": html_content,
        "description": description,
    }
    if expires_hours is not None:
        payload["expires_hours"] = expires_hours

    resp = requests.post(
        endpoint,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(
            f"DataJoins share API returned {resp.status_code}: {resp.text[:200]}"
        )

    data = resp.json()
    share_url: str = data.get("url", "")
    if not share_url:
        raise RuntimeError(
            f"DataJoins share API response missing 'url' field: {resp.text[:200]}"
        )

    expires_at: Optional[str] = None
    if expires_hours is not None:
        expiry_dt = datetime.now(timezone.utc) + timedelta(hours=expires_hours)
        expires_at = expiry_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    return NybbleShareResult(
        url=share_url,
        backend="datajoins",
        expires_at=expires_at,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def share(
    source: Union[str, Path],
    *,
    backend: str = "gist",
    output: str = "erd",
    gist_token: Optional[str] = None,
    endpoint: str = _DEFAULT_DATAJOINS_ENDPOINT,
    description: str = "Nybble shared export",
    expires_hours: Optional[int] = None,
    confirm: bool = False,
    _html_override: Optional[str] = None,
) -> NybbleShareResult:
    """Share a Nybble ERD or Knowledge Graph HTML output at a public URL.

    Parameters
    ----------
    source : str | Path
        Path to an existing self-contained HTML file **or** a data source
        path/connection string that is passed to :func:`nybble.erd` /
        :func:`nybble.kg` to produce the HTML first.
    backend : ``"gist"`` | ``"datajoins"``
        Which sharing backend to use.  Default is ``"gist"``.
        The backend can also be set via the ``NYBBLE_SHARE_BACKEND`` env var.
    output : ``"erd"`` | ``"kg"``
        When *source* is a data source (not a pre-rendered HTML file), which
        visualisation to generate before sharing.  Default is ``"erd"``.
    gist_token : str | None
        GitHub personal access token for authenticated Gist creation.
        Falls back to the ``GITHUB_TOKEN`` environment variable.
    endpoint : str
        Full URL for the DataJoins share endpoint.
        Falls back to the ``NYBBLE_SHARE_ENDPOINT`` environment variable.
    description : str
        Human-readable description attached to the shared resource.
    expires_hours : int | None
        Requested TTL in hours (DataJoins backend only).  None = permanent.
    confirm : bool
        Set to ``True`` to acknowledge that data will be uploaded to a public
        URL and suppress the upload warning.  Default is ``False``, which
        emits a :class:`UserWarning` before uploading.
    _html_override : str | None
        Internal: supply raw HTML directly (used in tests / programmatic use).

    Returns
    -------
    NybbleShareResult
        A dataclass with ``url``, ``backend``, and ``expires_at`` fields.

    Raises
    ------
    ValueError
        If *backend* is not ``"gist"`` or ``"datajoins"``.
    RuntimeError
        If the upstream API call fails.
    """
    # Warn before anything leaves the machine — matches the CLI confirmation
    # prompt.  Pass confirm=True to suppress (e.g. in scripts that have
    # already obtained consent or are sharing pre-anonymised exports).
    if not confirm:
        _dest = (
            "a public GitHub Gist"
            if (backend or "gist").lower().strip() == "gist"
            else "share.datajoins.com"
        )
        warnings.warn(
            f"nybble.share() will upload your data to {_dest}. "
            "Pass confirm=True to suppress this warning.",
            UserWarning,
            stacklevel=2,
        )

    # Backend resolution: explicit arg → env var → default
    resolved_backend = (
        backend
        or os.environ.get("NYBBLE_SHARE_BACKEND", "gist")
    ).lower().strip()

    if resolved_backend not in ("gist", "datajoins"):
        raise ValueError(
            f"Unknown share backend: {resolved_backend!r}. "
            "Choose 'gist' or 'datajoins'."
        )

    # ── Obtain HTML content ──────────────────────────────────────────────────
    if _html_override is not None:
        html_content = _html_override
        filename = "nybble_share.html"
    else:
        source_path = Path(source)
        if source_path.exists() and source_path.suffix.lower() == ".html":
            # Caller supplied a pre-rendered HTML file
            html_content = source_path.read_text(encoding="utf-8")
            filename = source_path.name
        else:
            # Caller supplied a data source - render it first
            html_content, filename = _render_html(str(source), output=output)

    # ── Dispatch to backend ──────────────────────────────────────────────────
    if resolved_backend == "gist":
        resolved_token = gist_token or os.environ.get("GITHUB_TOKEN")
        if not resolved_token:
            raise ValueError(
                "A GitHub personal access token is required to create a Gist.\n"
                "\n"
                "GitHub removed anonymous Gist creation in 2024. Provide a token\n"
                "in one of these ways:\n"
                "\n"
                "  1. Pass it directly:\n"
                "       nybble share --source .nybble/erd.html --gist-token ghp_xxxx\n"
                "\n"
                "  2. Set it in your shell (persists for the session):\n"
                "       export GITHUB_TOKEN=ghp_xxxx\n"
                "       nybble share --source .nybble/erd.html\n"
                "\n"
                "Create a token at: https://github.com/settings/tokens\n"
                "Required scope: gist (no repository or org access needed)."
            )
        return _share_gist(
            html_content=html_content,
            filename=filename,
            description=description,
            token=resolved_token,
        )
    else:  # datajoins
        resolved_endpoint = (
            endpoint
            or os.environ.get("NYBBLE_SHARE_ENDPOINT", _DEFAULT_DATAJOINS_ENDPOINT)
        )
        return _share_datajoins(
            html_content=html_content,
            endpoint=resolved_endpoint,
            description=description,
            expires_hours=expires_hours,
        )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _render_html(source: str, output: str = "erd") -> tuple[str, str]:
    """Load *source* and render it as a self-contained HTML string.

    Parameters
    ----------
    source : str - data source path or connection string
    output : ``"erd"`` | ``"kg"`` - which visualisation to produce

    Returns
    -------
    (html_content, filename) : tuple[str, str]
    """
    import tempfile

    from nybble.resolver import load as _load

    rel = _load(source)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        if output.lower() == "kg":
            from nybble.kg import kg as _kg
            result = _kg(
                [rel.name],
                output_dir=tmp_path,
                open_browser=False,
            )
            html_path = result.html_path
        else:
            from nybble.erd import erd as _erd
            html_path = _erd(
                relations=[rel.name],
                output=tmp_path / "erd.html",
                open_browser=False,
            )

        html_content = html_path.read_text(encoding="utf-8")

    return html_content, html_path.name
