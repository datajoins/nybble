"""
serve.py - Standalone Flask UI launcher for the Nybble pip package

Bridges the pip-install / Docker distribution gap: a developer who runs
``pip install datajoins-nybble`` can start the full browser UI with a single
command without Docker, nginx, or any mandatory new dependencies.

Public API
----------
  validate_port(port)             → int   (raises ValueError on bad input)
  find_server_dir()               → Path  (raises RuntimeError if not found)
  get_flask_app()                 → Flask app object
  serve_app(host, port, sources,  → None  (blocking; Ctrl-C exits cleanly)
            config_path,
            open_browser)
  resolve_token_request(token)    → str | None  (deep-link token → HTML content)

WSGI backend selection (zero hard deps)
----------------------------------------
  1. Waitress (``pip install "datajoins-nybble[serve]"``) - production-grade,
     multi-threaded, no debug output noise.
  2. Flask built-in dev server - always available when Flask is installed;
     used as automatic fallback when Waitress is absent.

Deep-link token support (P3-1)
---------------------------------
  Add ``?token=<value>`` to any Nybble web UI URL to load a specific ERD or KG.
  Tokens are registered via :func:`nybble.share.register_token` and resolved by
  :func:`resolve_token_request`.  Tokens map to local file paths or remote URLs.
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Optional


# ── Port validation ───────────────────────────────────────────────────────────

def validate_port(port: int) -> int:
    """Return *port* if valid, raise ValueError otherwise.

    Parameters
    ----------
    port:
        Integer port number to validate.

    Raises
    ------
    ValueError
        If *port* is not an integer or is outside 1-65535.
    """
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError(f"Port must be an integer, got: {port!r}")
    if not (1 <= port <= 65535):
        raise ValueError(f"Port must be between 1 and 65535, got: {port}")
    return port


# ── Server directory discovery ────────────────────────────────────────────────

def find_server_dir() -> Path:
    """Return the absolute path to the ``server/`` directory.

    Search order:
    1. Sibling of the ``nybble/`` package directory (editable / dev install).
    2. Any entry already on ``sys.path`` that contains ``server/app/api_server.py``.

    Raises
    ------
    RuntimeError
        If ``server/`` cannot be located (pip-installed without the server
        component, or running outside the repository).
    """
    # Case 1: editable / cloned-repo install - server/ is next to nybble/
    candidate = Path(__file__).parent.parent / "server"
    if (candidate / "app" / "api_server.py").exists():
        return candidate.resolve()

    # Case 2: someone added the server dir to sys.path manually
    for entry in sys.path:
        p = Path(entry) / "server"
        if (p / "app" / "api_server.py").exists():
            return p.resolve()

    raise RuntimeError(
        "The Nybble web UI server directory could not be found.\n"
        "\n"
        "Expected location: server/app/api_server.py next to the nybble/ package.\n"
        "\n"
        "If you installed from PyPI, re-install in a directory where the full\n"
        "repository is present:\n"
        "  git clone https://github.com/datajoins/nybble\n"
        "  cd nybble\n"
        "  pip install -e .\n"
        "  nybble serve\n"
        "\n"
        "Or run the Docker image (server/ is baked in):\n"
        "  docker run -p 7700:80 datajoins/nybble"
    )


# ── Flask app loader ──────────────────────────────────────────────────────────

def get_flask_app():  # type: ignore[return]
    """Import and return the Flask app object from server/app/api_server.py.

    Adds the server directory to sys.path if needed so the import succeeds.

    Raises
    ------
    RuntimeError
        If the server directory cannot be found (see find_server_dir()).
    ImportError
        If Flask is not installed.
    """
    try:
        import flask  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "Flask is required to run the Nybble web UI.\n"
            "Install it with:  pip install \"datajoins-nybble[dev]\""
        ) from exc

    server_dir = find_server_dir()
    repo_root = server_dir.parent

    # Ensure both the repo root (for ``server.app.api_server``) and the
    # server dir itself (for relative imports within server/) are on sys.path.
    for p in (str(repo_root), str(server_dir)):
        if p not in sys.path:
            sys.path.insert(0, p)

    from server.app.api_server import app  # type: ignore[import]
    return app


# ── Environment setup ─────────────────────────────────────────────────────────

def _configure_env(host: str, port: int) -> None:
    """Set environment variables consumed by api_server.py before it is imported."""
    nybble_dir = Path(".nybble")
    os.environ.setdefault("API_PORT",   str(port))
    os.environ.setdefault("API_HOST",   host)
    os.environ.setdefault("UPLOAD_DIR", str(nybble_dir / "uploads"))
    os.environ.setdefault("OUTPUT_DIR", str(nybble_dir / "output" / "erd"))
    os.environ.setdefault("REGISTRY",   str(nybble_dir / "registry.json"))

    # Ensure the directories exist before the Flask app touches them
    for d in (nybble_dir / "uploads", nybble_dir / "output" / "erd"):
        d.mkdir(parents=True, exist_ok=True)


# ── Source loading ────────────────────────────────────────────────────────────

def _load_sources_for_serve(
    sources: tuple[str, ...],
    config_path: Optional[str],
) -> list[str]:
    """Load sources from --source flags and/or nybble.yaml config.

    Returns a list of human-readable "<name> (<source_type>, N rows)" strings
    suitable for the startup banner.
    """
    from nybble.config import find_config, load_config, apply_config
    from nybble.resolver import load as _load

    loaded_labels: list[str] = []

    # Config file (auto-discover when config_path is None)
    cfg_file: Optional[Path] = None
    if config_path is not None:
        cfg_file = Path(config_path)
        if not cfg_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
    else:
        cfg_file = find_config()

    if cfg_file is not None:
        cfg = load_config(cfg_file)
        rels = apply_config(cfg)
        for rel in rels:
            loaded_labels.append(
                f"{rel.name} ({rel.source_type}, {rel.row_count:,} rows)"
            )

    # Explicit --source flags (override / supplement config sources)
    for s in sources:
        if "=" in s:
            name, path = s.split("=", 1)
            rel = _load(path, name=name.strip())
        else:
            rel = _load(s)
        loaded_labels.append(
            f"{rel.name} ({rel.source_type}, {rel.row_count:,} rows)"
        )

    return loaded_labels


# ── CLI artefact discovery ────────────────────────────────────────────────────

# Artefacts written by CLI commands to .nybble/ that are worth surfacing.
# Tuples of (filename, human label, optional serve-route).
# When a serve-route is provided the startup banner links to the live UI
# route rather than the static file URI (which opens the simpler jinja export).
_NYBBLE_ARTEFACTS = [
    ("erd.html",     "ERD",             None),
    ("kg.html",      "Knowledge Graph", "/kg"),
    ("lineage.html", "Lineage DAG",     None),
]


def _find_nybble_artefacts(
    nybble_dir: Optional[Path] = None,
    port: int = 7700,
) -> list[tuple[str, str]]:
    """Scan ``.nybble/`` for known CLI-generated HTML artefacts.

    Returns a list of ``(label, url)`` tuples for artefacts that exist.
    When a serve-route is defined for an artefact the URL points to the live
    interactive UI (``http://localhost:<port><route>``) rather than the static
    file URI, so the user always gets the full Cytoscape experience from the
    startup banner.

    Parameters
    ----------
    nybble_dir:
        Directory to scan.  Defaults to ``.nybble`` in the current working
        directory.  Pass an explicit path in tests.
    port:
        Port the server is about to listen on (used to build the route URL).
    """
    base = nybble_dir or Path(".nybble")
    found: list[tuple[str, str]] = []
    for filename, label, route in _NYBBLE_ARTEFACTS:
        p = base / filename
        if p.exists():
            if route:
                # Link to the live interactive route, not the static file
                url = f"http://localhost:{port}{route}"
            else:
                url = p.resolve().as_uri()
            found.append((label, url))
    return found


# ── WSGI backend selection ────────────────────────────────────────────────────

def _has_waitress() -> bool:
    """Return True if Waitress is importable."""
    try:
        import waitress  # noqa: F401
        return True
    except ImportError:
        return False


# ── Public entry point ────────────────────────────────────────────────────────

def serve_app(
    host: str = "127.0.0.1",
    port: int = 7700,
    sources: tuple[str, ...] = (),
    config_path: Optional[str] = None,
    open_browser: bool = True,
) -> None:
    """Start the Nybble web UI, blocking until Ctrl-C.

    Parameters
    ----------
    host:
        Bind address.  Use ``"0.0.0.0"`` to expose on the network.
    port:
        TCP port.  Must be 1-65535.
    sources:
        Tuple of ``name=path`` (or bare path) source strings pre-loaded
        before the server starts.
    config_path:
        Explicit path to a ``nybble.yaml`` config file.  When ``None``,
        auto-discovers ``nybble.yaml`` in the current directory.
    open_browser:
        When ``True``, open the UI in the default browser ~1.5 s after start.
    """
    from rich.console import Console
    console = Console()

    # ── 1. Validate port ──────────────────────────────────────────────────────
    port = validate_port(port)

    # ── 2. Configure environment (must happen before importing api_server) ─────
    _configure_env(host, port)

    # ── 3. Load sources ───────────────────────────────────────────────────────
    loaded_labels: list[str] = []
    try:
        loaded_labels = _load_sources_for_serve(sources, config_path)
    except FileNotFoundError as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        raise SystemExit(1)
    except Exception as exc:
        console.print(f"[red]Source load error:[/red] {exc}")
        raise SystemExit(1)

    # ── 4. Import Flask app ───────────────────────────────────────────────────
    try:
        flask_app = get_flask_app()
    except (RuntimeError, ImportError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)

    # ── 5. Select WSGI backend ────────────────────────────────────────────────
    use_waitress = _has_waitress()
    backend_name = "Waitress" if use_waitress else "Flask dev server"

    # ── 6. Print startup banner ───────────────────────────────────────────────
    url = f"http://{host}:{port}"
    console.print(f"\n[green]Nybble web UI[/green]  ->  [bold]{url}[/bold]")
    console.print(f"[dim]Backend:[/dim]   {backend_name}")
    if loaded_labels:
        console.print(f"[dim]Sources:[/dim]   {', '.join(loaded_labels)}")
    else:
        console.print("[dim]Sources:[/dim]   none pre-loaded (use the upload UI)")

    # ── Level 2: surface CLI artefacts in .nybble/ ────────────────────────────
    # Pass the port so KG artefacts link to /kg (full Cytoscape UI) not static file
    artefacts = _find_nybble_artefacts(port=port)
    if artefacts:
        console.print("[dim]Artefacts:[/dim]")
        for label, artefact_url in artefacts:
            console.print(f"  [dim]{label}[/dim]  [link={artefact_url}]{artefact_url}[/link]")

    console.print("[dim]Press Ctrl+C to stop.[/dim]\n")

    # ── 7. Schedule browser open ──────────────────────────────────────────────
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    # ── 8. Start server ───────────────────────────────────────────────────────
    try:
        if use_waitress:
            import waitress  # type: ignore[import]
            waitress.serve(flask_app, host=host, port=port)
        else:
            flask_app.run(host=host, port=port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        console.print("\n[dim]Nybble web UI stopped.[/dim]")


# ── Deep-link token support (P3-1) ────────────────────────────────────────────

def resolve_token_request(token: str) -> Optional[str]:
    """Resolve a ``?token=<value>`` deep-link to the HTML content it represents.

    This is the route-handler helper for the Docker web UI.  Call it from any
    Flask route that accepts a ``token`` query-string parameter::

        from nybble.serve import resolve_token_request

        @app.route("/view")
        def view():
            token = request.args.get("token", "")
            if token:
                html = resolve_token_request(token)
                if html is not None:
                    return html
            # ... normal rendering path ...

    Token resolution order
    ----------------------
    1. Look up *token* in :data:`nybble.share._TOKEN_REGISTRY`.
    2. If the target is a local file path that exists, read and return its content.
    3. If the target looks like an HTTP/HTTPS URL, fetch it with ``requests``
       and return the response text.
    4. If the token is not registered or cannot be resolved, return ``None``.

    Parameters
    ----------
    token : str - the raw token value from the ``?token=`` query parameter

    Returns
    -------
    str or None
        The resolved HTML content, or ``None`` if the token cannot be resolved.
    """
    from nybble.share import resolve_token as _resolve_token

    target = _resolve_token(token)
    if target is None:
        return None

    # ── Local file path ───────────────────────────────────────────────────────
    local = Path(target)
    if local.exists():
        try:
            return local.read_text(encoding="utf-8")
        except OSError:
            return None

    # ── Remote URL ────────────────────────────────────────────────────────────
    if target.startswith(("http://", "https://")):
        try:
            import requests
            resp = requests.get(target, timeout=15)
            if resp.ok:
                return resp.text
        except Exception:
            pass
        return None

    return None
