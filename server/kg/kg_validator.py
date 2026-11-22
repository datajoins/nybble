"""server/kg/kg_validator.py - Structured graph validation with severity levels.

Aligned with the semantica GraphValidator design.  All validation is non-destructive:
the graph is never modified.  Callers receive a ValidationResult they can inspect
without catching exceptions.

No external dependencies - pure stdlib: enum, dataclasses, typing, logging.

# MARKER:kg-graph-validator

Public API
----------
  ValidationSeverity          - Enum: INFO, WARNING, ERROR, CRITICAL
  ValidationIssue             - Dataclass: code, message, severity, element_id, details
  ValidationResult            - Container: is_valid, issues, stats
  validate_graph(graph, strict=False) → ValidationResult
    Checks performed:
      - Duplicate node IDs              (DUPLICATE_NODE,   CRITICAL)
      - Dangling edge endpoints         (DANGLING_EDGE,    ERROR)
      - Self-loops                      (SELF_LOOP,        INFO)
      - Orphan nodes (no edges)         (ORPHAN_NODE,      WARNING)
      - Type / ID-prefix consistency    (TYPE_ID_MISMATCH, WARNING)
      - Cycle detection                 (CYCLE_DETECTED,   INFO)
    In strict mode warnings are treated as errors for is_valid.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Node type / ID-prefix consistency ─────────────────────────────────────────
# MARKER:kg-type-id-consistency
#
# Ported from UA graph-reviewer.md Check 9.
# Maps canonical entity_type values to their expected node-ID prefix patterns.
# Only entity_types listed here are checked; unknown/custom types are exempt
# (open-schema tolerance - entities from external systems may use any prefix).

TYPE_PREFIX_MAP: Dict[str, str] = {
    "TABLE":    "table:",
    "COLUMN":   "col:",
    "FUNCTION": "func:",
    "CLASS":    "class:",
    "MODULE":   "module:",
    "SCHEMA":   "schema:",
    "SERVICE":  "service:",
    "PIPELINE": "pipeline:",
    "ENDPOINT": "endpoint:",
    "RESOURCE": "resource:",
    "CONFIG":   "config:",
    "DOCUMENT": "document:",
}


# ── Severity ──────────────────────────────────────────────────────────────────

class ValidationSeverity(Enum):
    """Severity levels for validation issues, ordered low → high."""
    INFO     = "info"
    WARNING  = "warning"
    ERROR    = "error"
    CRITICAL = "critical"


# ── Issue ─────────────────────────────────────────────────────────────────────

@dataclass
class ValidationIssue:
    """A single validation finding."""
    code:       str                           # e.g. "DANGLING_EDGE"
    message:    str
    severity:   ValidationSeverity
    element_id: Optional[str] = None         # node or edge identifier
    details:    Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code":       self.code,
            "message":    self.message,
            "severity":   self.severity.value,
            "element_id": self.element_id,
            "details":    self.details,
        }


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    """Container for all validation findings."""
    is_valid: bool
    issues:   List[ValidationIssue] = field(default_factory=list)
    stats:    Dict[str, int]        = field(default_factory=dict)

    def errors(self) -> List[ValidationIssue]:
        """Return only ERROR and CRITICAL issues."""
        return [
            i for i in self.issues
            if i.severity in (ValidationSeverity.ERROR, ValidationSeverity.CRITICAL)
        ]

    def warnings(self) -> List[ValidationIssue]:
        """Return only WARNING issues."""
        return [i for i in self.issues if i.severity == ValidationSeverity.WARNING]

    def by_severity(self, severity: ValidationSeverity) -> List[ValidationIssue]:
        """Return issues matching *severity* exactly."""
        return [i for i in self.issues if i.severity == severity]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "issues":   [i.to_dict() for i in self.issues],
            "stats":    self.stats,
        }


# ── Validator ─────────────────────────────────────────────────────────────────

def validate_graph(graph, strict: bool = False) -> ValidationResult:
    """Validate an EntityGraph and return a structured ValidationResult.

    Parameters
    ----------
    graph  : EntityGraph instance (server.kg.kg_graph.EntityGraph)
    strict : If True, WARNING issues also count as errors for ``is_valid``.

    Returns
    -------
    ValidationResult with all findings.  Never raises.
    """
    issues: List[ValidationIssue] = []

    # Extract the underlying NetworkX DiGraph, or work with a dict-based fallback
    G = None
    if hasattr(graph, "_G"):
        G = graph._G

    if G is None:
        # No graph object - nothing to validate
        return ValidationResult(
            is_valid=True,
            issues=[],
            stats={"nodes": 0, "edges": 0, "errors": 0, "warnings": 0},
        )

    # ── 1. Duplicate node IDs ────────────────────────────────────────────────
    seen_ids: set = set()
    for nid in G.nodes():
        if nid in seen_ids:
            issues.append(ValidationIssue(
                code="DUPLICATE_NODE",
                message=f"Duplicate node ID: {nid!r}",
                severity=ValidationSeverity.CRITICAL,
                element_id=str(nid),
            ))
        else:
            seen_ids.add(nid)

    # ── 2. Dangling edge endpoints ───────────────────────────────────────────
    for src, tgt in G.edges():
        if src not in seen_ids:
            issues.append(ValidationIssue(
                code="DANGLING_EDGE",
                message=f"Edge source {src!r} not in node set",
                severity=ValidationSeverity.ERROR,
                element_id=f"{src}->{tgt}",
                details={"source": str(src), "target": str(tgt)},
            ))
        if tgt not in seen_ids:
            issues.append(ValidationIssue(
                code="DANGLING_EDGE",
                message=f"Edge target {tgt!r} not in node set",
                severity=ValidationSeverity.ERROR,
                element_id=f"{src}->{tgt}",
                details={"source": str(src), "target": str(tgt)},
            ))

    # ── 3. Self-loops ────────────────────────────────────────────────────────
    for nid in seen_ids:
        if G.has_edge(nid, nid):
            issues.append(ValidationIssue(
                code="SELF_LOOP",
                message=f"Self-loop on node {nid!r}",
                severity=ValidationSeverity.INFO,
                element_id=str(nid),
            ))

    # ── 4. Orphan nodes ───────────────────────────────────────────────────────
    for nid in seen_ids:
        if G.degree(nid) == 0:
            issues.append(ValidationIssue(
                code="ORPHAN_NODE",
                message=f"Node {nid!r} has no edges",
                severity=ValidationSeverity.WARNING,
                element_id=str(nid),
            ))

    # ── 5. Node type / ID-prefix consistency (WARNING) ───────────────────────
    for nid in seen_ids:
        etype = G.nodes[nid].get("entity_type", "")
        expected_prefix = TYPE_PREFIX_MAP.get(etype.upper() if etype else "")
        if expected_prefix and not str(nid).startswith(expected_prefix):
            issues.append(ValidationIssue(
                code="TYPE_ID_MISMATCH",
                message=(
                    f"Node {nid!r} has entity_type={etype!r} but ID does not start "
                    f"with expected prefix {expected_prefix!r}"
                ),
                severity=ValidationSeverity.WARNING,
                element_id=str(nid),
                details={"entity_type": etype, "expected_prefix": expected_prefix},
            ))

    # ── 6. Cycle detection (structural info only) ─────────────────────────────
    try:
        import networkx as nx
        cycles = list(nx.simple_cycles(G))
        if cycles:
            issues.append(ValidationIssue(
                code="CYCLE_DETECTED",
                message=f"Graph contains {len(cycles)} cycle(s)",
                severity=ValidationSeverity.INFO,
                details={"count": len(cycles)},
            ))
    except Exception as exc:
        logger.debug(
            "validate_graph: cycle detection skipped",
            extra={"error": str(exc), "outcome": "skip"},
        )

    # ── Result assembly ───────────────────────────────────────────────────────
    error_count = len([i for i in issues if i.severity in (
        ValidationSeverity.ERROR, ValidationSeverity.CRITICAL)])
    warn_count  = len([i for i in issues if i.severity == ValidationSeverity.WARNING])

    failing = error_count
    if strict:
        failing += warn_count

    return ValidationResult(
        is_valid=failing == 0,
        issues=issues,
        stats={
            "nodes":    G.number_of_nodes(),
            "edges":    G.number_of_edges(),
            "errors":   error_count,
            "warnings": warn_count,
        },
    )
