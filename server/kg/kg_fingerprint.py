"""server/kg/kg_fingerprint.py - Structural fingerprint and change classification.

Distinguishes cosmetic-only changes (docstrings, comments, whitespace) from
structural changes (function/class signatures, argument lists, decorators,
base classes) so that the indexing pipeline can skip re-indexing on cosmetic
edits.

Ported from Understand Anything ``fingerprint.ts`` / ``change-classifier.ts``.

Public API
----------
  _extract_python_fingerprint(source_code: str) -> dict
  ChangeLevel (enum: NONE, COSMETIC, STRUCTURAL)
  classify_change_level(old_fp: dict, new_fp: dict) -> ChangeLevel
  UpdateDecision (enum: SKIP, PARTIAL_UPDATE, SCHEMA_UPDATE, FULL_UPDATE)
  ChangeClassifier.classify_update(change_level, metadata) -> UpdateDecision

Zero new [dependencies] - uses only Python stdlib (ast, hashlib).

# MARKER:kg-structural-fingerprint
# MARKER:kg-change-classifier
"""
from __future__ import annotations

import ast
import hashlib
import json
from enum import Enum
from typing import Any, Dict, List, Optional


# ── Change level enum ─────────────────────────────────────────────────────────

class ChangeLevel(str, Enum):
    """Degree of change between two versions of a document.

    NONE       - content is byte-for-byte identical.
    COSMETIC   - content differs (whitespace, comments, docstrings) but the
                 public structural signature (function/class names, arguments,
                 decorators, base classes) is unchanged.
    STRUCTURAL - the structural signature has changed; re-indexing is required.
    """
    NONE       = "NONE"
    COSMETIC   = "COSMETIC"
    STRUCTURAL = "STRUCTURAL"


# ── AST visitor for structural signature extraction ───────────────────────────

class _SignatureCollector(ast.NodeVisitor):
    """Collect structurally significant items from a Python AST.

    Specifically: function/method names + argument names, class names +
    base-class names, and decorator names.  Docstrings and implementation
    bodies are intentionally excluded.
    """

    def __init__(self) -> None:
        self._items: List[str] = []

    # ── Public helper ──────────────────────────────────────────────────────────

    def collect(self, tree: ast.AST) -> List[str]:
        self._items = []
        self.visit(tree)
        return self._items

    # ── Visitor methods ────────────────────────────────────────────────────────

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._record_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._record_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        # Class name
        self._items.append(f"class:{node.name}")
        # Base classes (names only, not expressions)
        for base in node.bases:
            name = self._expr_name(base)
            if name:
                self._items.append(f"base:{name}")
        # Decorator names
        for dec in node.decorator_list:
            name = self._expr_name(dec)
            if name:
                self._items.append(f"dec:{name}")
        # Recurse into class body (to catch nested defs)
        self.generic_visit(node)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _record_function(self, node: ast.FunctionDef) -> None:
        self._items.append(f"fn:{node.name}")
        # Positional + keyword arg names
        args = node.args
        for arg in args.args:
            self._items.append(f"arg:{arg.arg}")
        if args.vararg:
            self._items.append(f"vararg:{args.vararg.arg}")
        for arg in args.kwonlyargs:
            self._items.append(f"kwarg:{arg.arg}")
        if args.kwarg:
            self._items.append(f"kwarg:{args.kwarg.arg}")
        # Decorator names
        for dec in node.decorator_list:
            name = self._expr_name(dec)
            if name:
                self._items.append(f"dec:{name}")
        # Recurse into nested defs
        self.generic_visit(node)

    @staticmethod
    def _expr_name(node: ast.expr) -> Optional[str]:
        """Return a simple string name for a Name or Attribute AST node."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = _SignatureCollector._expr_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def _extract_python_fingerprint(source_code: str) -> dict:
    """Compute content and structural hashes for *source_code*.

    Returns a dict with keys:
        ``content_hash``    - SHA-256 hex digest of the raw source bytes.
        ``structural_hash`` - SHA-256 hex digest of the AST structural signature
                              (function/class names, arg lists, decorator names,
                              base classes; docstrings / comments excluded).

    On parse errors the ``structural_hash`` falls back to an empty-signature
    hash so that any two unparseable files compare as structurally equal (and
    thus trigger COSMETIC rather than STRUCTURAL unnecessarily).
    """
    raw_bytes = source_code.encode("utf-8", errors="replace")
    content_hash = hashlib.sha256(raw_bytes).hexdigest()

    try:
        tree = ast.parse(source_code)
        collector = _SignatureCollector()
        items = collector.collect(tree)
        # Stable serialisation - sorted so order-independent moves stay COSMETIC
        sig_bytes = json.dumps(sorted(items), separators=(",", ":")).encode()
    except SyntaxError:
        # Unparseable - fall back to a fixed empty-signature hash
        sig_bytes = b"[]"

    structural_hash = hashlib.sha256(sig_bytes).hexdigest()

    return {
        "content_hash":    content_hash,
        "structural_hash": structural_hash,
    }


def classify_change_level(old_fp: dict, new_fp: dict) -> ChangeLevel:
    """Compare two fingerprint dicts and return the appropriate ChangeLevel.

    Rules (mirrors UA ``compareFingerprints`` logic):
      NONE       - both hashes identical (content unchanged).
      COSMETIC   - content_hash differs but structural_hash is unchanged.
      STRUCTURAL - structural_hash differs.

    If either dict is missing a key the comparison is conservative:
    missing content_hash → treat as content-changed;
    missing structural_hash → STRUCTURAL (cannot verify structure unchanged).
    """
    old_c = old_fp.get("content_hash")
    new_c = new_fp.get("content_hash")
    old_s = old_fp.get("structural_hash")
    new_s = new_fp.get("structural_hash")

    # Fast path: identical content
    if old_c is not None and new_c is not None and old_c == new_c:
        return ChangeLevel.NONE

    # If either structural hash is absent we cannot certify structure unchanged
    if old_s is None or new_s is None:
        return ChangeLevel.STRUCTURAL

    if old_s != new_s:
        return ChangeLevel.STRUCTURAL

    return ChangeLevel.COSMETIC


# ── Update decision enum ──────────────────────────────────────────────────────

class UpdateDecision(str, Enum):
    """Action the indexing pipeline should take for a changed document.

    SKIP           - No change detected; perform no graph writes.
    PARTIAL_UPDATE - Cosmetic change; update metadata/labels only (no re-embed).
    SCHEMA_UPDATE  - Structural change that does NOT affect schema topology
                     (e.g. renamed column alias but same FK structure).
    FULL_UPDATE    - Structural AND schema-topology-affecting change; full
                     re-index including edge reconstruction.
    """
    SKIP           = "SKIP"
    PARTIAL_UPDATE = "PARTIAL_UPDATE"
    SCHEMA_UPDATE  = "SCHEMA_UPDATE"
    FULL_UPDATE    = "FULL_UPDATE"


# ── Change classifier ─────────────────────────────────────────────────────────

class ChangeClassifier:
    """Map a ChangeLevel + metadata dict to an UpdateDecision.

    Ported from Understand Anything ``ChangeClassifier`` / ``classifyUpdate``.

    Usage::

        decision = ChangeClassifier.classify_update(
            change_level=ChangeLevel.STRUCTURAL,
            metadata={"schema_affecting": True},
        )
        # → UpdateDecision.FULL_UPDATE

    ``metadata`` keys recognised:
        schema_affecting (bool, default False) - True when the change alters
            the graph topology (tables added/removed, FK columns changed, etc.).
            Only consulted when change_level is STRUCTURAL.
    """

    @staticmethod
    def classify_update(
        change_level: ChangeLevel,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> UpdateDecision:
        """Return the appropriate UpdateDecision for *change_level*.

        Mapping (mirrors UA ``classifyUpdate`` logic):
          NONE       → SKIP
          COSMETIC   → PARTIAL_UPDATE
          STRUCTURAL + schema_affecting=False → SCHEMA_UPDATE
          STRUCTURAL + schema_affecting=True  → FULL_UPDATE
        """
        if metadata is None:
            metadata = {}

        if change_level is ChangeLevel.NONE:
            return UpdateDecision.SKIP

        if change_level is ChangeLevel.COSMETIC:
            return UpdateDecision.PARTIAL_UPDATE

        # STRUCTURAL
        if metadata.get("schema_affecting", False):
            return UpdateDecision.FULL_UPDATE
        return UpdateDecision.SCHEMA_UPDATE
