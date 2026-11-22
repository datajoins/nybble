"""server/kg/kg_temporal.py - Bi-temporal data model + temporal query engine.

Implements valid-time / transaction-time separation for relationship facts,
aligned with the semantica BiTemporalFact design.

No external dependencies - uses only stdlib: datetime, re, dataclasses, enum.

# MARKER:kg-bitemporal-model

Key concepts
------------
valid_from / valid_until  - when the fact was true in the modelled world
recorded_at / superseded_at - when the fact was recorded / replaced in this system

TemporalBound.OPEN represents an open-ended interval (no upper bound).

Public API
----------
  TemporalBound            - Enum sentinel for open-ended intervals
  BiTemporalFact           - Dataclass wrapping a relationship dict
  KGTemporalError          - Raised on unparseable temporal values
  parse_temporal_value()   - str / int / float / datetime → UTC datetime
  parse_temporal_bound()   - any → datetime | TemporalBound.OPEN | None
  serialize_temporal_value() - datetime → ISO 8601 "Z" string
  serialize_temporal_bound() - datetime|OPEN → str | None
  _coerce_iso_date()       - "2024-1-5" → "2024-01-05" (single-digit padding)
  from_relationship()      - dict → BiTemporalFact
  to_relationship_fields() - BiTemporalFact → dict of temporal keys
  relationship_active_at() - bool: is a relationship dict valid at a given datetime?
  relationship_overlaps()  - bool: does a relationship overlap a time range?
"""
from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


# ── Sentinel ──────────────────────────────────────────────────────────────────

class TemporalBound(Enum):
    """Sentinel for open-ended temporal intervals (no upper bound)."""
    OPEN = "OPEN"


# ── Exceptions ────────────────────────────────────────────────────────────────

class KGTemporalError(ValueError):
    """Raised when a temporal value cannot be parsed or is inconsistent."""

    def __init__(self, message: str, *, temporal_context: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.temporal_context: Dict[str, Any] = temporal_context or {}


# ── ISO date coercion ─────────────────────────────────────────────────────────

_ISO_LOOSE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?P<rest>.*)$"
)


def _coerce_iso_date(value: str) -> str:
    """Pad single-digit month/day so datetime.fromisoformat() accepts the string.

    Examples
    --------
    >>> _coerce_iso_date("2024-1-5")
    '2024-01-05'
    >>> _coerce_iso_date("2024-01-05T10:00:00")
    '2024-01-05T10:00:00'
    >>> _coerce_iso_date("not-a-date")
    'not-a-date'
    """
    m = _ISO_LOOSE.match(value.strip())
    if not m:
        return value.strip()
    return (
        f"{m.group('year')}-{int(m.group('month')):02d}"
        f"-{int(m.group('day')):02d}{m.group('rest')}"
    )


# ── Parsing helpers ───────────────────────────────────────────────────────────

def parse_temporal_value(value: Any) -> Optional[datetime]:
    """Parse any temporal representation into a UTC-aware datetime.

    Accepts:
      - None                → None
      - datetime (aware)   → returned as-is, converted to UTC
      - datetime (naive)   → assumed UTC
      - int / float        → Unix timestamp (UTC)
      - str                → ISO 8601, padded via _coerce_iso_date; trailing 'Z' accepted

    Raises KGTemporalError on unrecognised input.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        normalised = _coerce_iso_date(value.strip())
        if normalised.endswith("Z"):
            normalised = normalised[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(normalised)
        except ValueError as exc:
            raise KGTemporalError(
                f"Invalid temporal value: {value!r}",
                temporal_context={"value": value},
            ) from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    raise KGTemporalError(
        f"Unsupported temporal value type: {type(value).__name__}",
        temporal_context={"value": value, "type": type(value).__name__},
    )


def parse_temporal_bound(
    value: Any,
    *,
    default: Optional[Union[datetime, TemporalBound]] = None,
) -> Optional[Union[datetime, TemporalBound]]:
    """Parse a temporal bound that may be OPEN, None, or a concrete datetime."""
    if value is None:
        return default
    if value is TemporalBound.OPEN or value == TemporalBound.OPEN.value:
        return TemporalBound.OPEN
    return parse_temporal_value(value)


def serialize_temporal_value(value: Optional[datetime]) -> Optional[str]:
    """Serialize a UTC datetime to ISO 8601 with 'Z' suffix, or None."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def serialize_temporal_bound(
    value: Optional[Union[datetime, TemporalBound]],
) -> Optional[str]:
    """Serialize a temporal bound (datetime or OPEN) to string or None."""
    if value is None or value is TemporalBound.OPEN:
        return None
    return serialize_temporal_value(value)  # type: ignore[arg-type]


# ── BiTemporalFact ────────────────────────────────────────────────────────────

def _default_recorded_at() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BiTemporalFact:
    """Bi-temporal wrapper around a relationship dict.

    Facts continue to live as plain dicts in the graph. This wrapper is used
    for normalisation, querying, and consistency checking without mutating the
    stored representation.

    Attributes
    ----------
    valid_from      : when the fact became true in the modelled world
    valid_until     : when the fact ceased to be true (OPEN = still true)
    recorded_at     : when this system first recorded the fact
    superseded_at   : when this record was superseded (OPEN = still current)
    """
    valid_from:     Optional[datetime]
    valid_until:    Union[Optional[datetime], TemporalBound]
    recorded_at:    datetime = field(default_factory=_default_recorded_at)
    superseded_at:  Union[datetime, TemporalBound] = TemporalBound.OPEN

    @classmethod
    def from_relationship(cls, relationship: Dict[str, Any]) -> "BiTemporalFact":
        """Construct a BiTemporalFact from a relationship dict.

        Missing fields default to None (valid_from) or OPEN (valid_until,
        superseded_at). Existing dicts without temporal keys are valid inputs.
        """
        valid_from_raw = relationship.get("valid_from")
        valid_until_raw = relationship.get("valid_until", TemporalBound.OPEN)
        if valid_until_raw is None:
            valid_until_raw = TemporalBound.OPEN

        recorded_at_raw = relationship.get("recorded_at")
        superseded_at_raw = relationship.get("superseded_at", TemporalBound.OPEN)

        valid_from = parse_temporal_value(valid_from_raw)
        recorded_at = (
            parse_temporal_value(recorded_at_raw)
            if recorded_at_raw is not None
            else (valid_from or _default_recorded_at())
        )
        return cls(
            valid_from=valid_from,
            valid_until=parse_temporal_bound(valid_until_raw, default=TemporalBound.OPEN),
            recorded_at=recorded_at,
            superseded_at=parse_temporal_bound(superseded_at_raw, default=TemporalBound.OPEN),
        )

    def to_relationship_fields(self) -> Dict[str, Any]:
        """Return a dict of temporal keys suitable for merging into a relationship dict."""
        return {
            "valid_from":    serialize_temporal_value(self.valid_from),
            "valid_until":   serialize_temporal_bound(self.valid_until),
            "recorded_at":   serialize_temporal_value(self.recorded_at),
            "superseded_at": serialize_temporal_bound(self.superseded_at),
        }

    def is_valid_at(self, point: datetime) -> bool:
        """Return True if *point* falls within [valid_from, valid_until)."""
        if point.tzinfo is None:
            point = point.replace(tzinfo=timezone.utc)
        point = point.astimezone(timezone.utc)
        if self.valid_from is not None and point < self.valid_from:
            return False
        if isinstance(self.valid_until, datetime) and point >= self.valid_until:
            return False
        return True

    def overlaps(
        self,
        start: Optional[datetime],
        end: Optional[Union[datetime, TemporalBound]],
    ) -> bool:
        """Return True if this fact's valid interval overlaps [start, end]."""
        # Resolve bounds to concrete datetimes for comparison
        fact_start = self.valid_from
        fact_end   = self.valid_until

        # OPEN end = infinity; treat as far future
        if fact_end is TemporalBound.OPEN:
            fact_end = None  # unbounded
        if end is TemporalBound.OPEN:
            end = None  # unbounded

        # [A, B] overlaps [C, D] iff A <= D and C <= B
        if fact_end is not None and start is not None and fact_end <= start:
            return False
        if end is not None and fact_start is not None and end <= fact_start:
            return False
        return True


# ── Relationship-level helpers ────────────────────────────────────────────────

def relationship_active_at(
    relationship: Dict[str, Any],
    point: datetime,
) -> bool:
    """Return True if a relationship dict is valid at the given UTC datetime."""
    fact = BiTemporalFact.from_relationship(relationship)
    return fact.is_valid_at(point)


def relationship_overlaps(
    relationship: Dict[str, Any],
    start: Optional[datetime],
    end: Optional[Union[datetime, TemporalBound]],
) -> bool:
    """Return True if a relationship dict's valid interval overlaps [start, end]."""
    fact = BiTemporalFact.from_relationship(relationship)
    return fact.overlaps(start, end)


# ── Temporal query rewriter ───────────────────────────────────────────────────

def rewrite_temporal_query(
    query: Dict[str, Any],
    *,
    as_of: Optional[datetime] = None,
    between: Optional[Tuple[Optional[datetime], Optional[Union[datetime, TemporalBound]]]] = None,
    current_only: bool = False,
) -> Dict[str, Any]:
    """Rewrite a graph query dict with temporal filter predicates.

    # MARKER:kg-temporal-query-rewriter

    Given an existing query specification (any dict with optional ``filters`` key),
    inject temporal boundary conditions so downstream search/filter steps honour
    bi-temporal constraints without every call site manually computing the bounds.

    Three modes (only one should be specified; ``as_of`` takes precedence):

    ``as_of``
        Inject a point-in-time filter: only facts whose ``[valid_from, valid_until)``
        interval includes *as_of* are returned.

    ``between``
        Inject a range filter: only facts whose valid interval overlaps ``[start, end]``.
        ``between`` must be a 2-tuple ``(start, end)`` where either element may be None
        (unbounded on that side) or ``TemporalBound.OPEN``.

    ``current_only``
        Convenience flag: add a ``superseded_at_open: true`` predicate so only
        facts whose ``superseded_at`` is ``OPEN`` (i.e. still the current version)
        are returned.  May be combined with ``as_of`` or ``between``.

    Parameters
    ----------
    query : dict
        Input query specification.  A copy is returned - the original is not mutated.
    as_of : datetime or None
        Point-in-time anchor.  If timezone-naive, treated as UTC.
    between : (start, end) tuple or None
        Valid-time overlap window.
    current_only : bool
        When True, add ``superseded_at_open: true`` to the rewritten query.

    Returns
    -------
    dict
        A shallow copy of *query* with a ``temporal_filters`` key injected (or
        merged into an existing one).

    Examples
    --------
    >>> from datetime import datetime, timezone
    >>> q = {"entity_type": "TABLE", "source_key": "main"}
    >>> rewrite_temporal_query(q, as_of=datetime(2024, 6, 1, tzinfo=timezone.utc))
    {'entity_type': 'TABLE', 'source_key': 'main', 'temporal_filters': {'as_of': '2024-06-01T00:00:00Z'}}
    """
    result = dict(query)
    tf: Dict[str, Any] = dict(result.get("temporal_filters") or {})

    if as_of is not None:
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        tf["as_of"] = serialize_temporal_value(as_of)

    elif between is not None:
        start, end = between
        tf["valid_from_gte"] = serialize_temporal_value(start) if start is not None else None
        tf["valid_until_lte"] = serialize_temporal_bound(end) if end is not None else None

    if current_only:
        tf["superseded_at_open"] = True

    result["temporal_filters"] = tf
    return result


# ── TemporalVersionManager ────────────────────────────────────────────────────

class TemporalVersionManager:
    """SQLite-backed snapshot store for graph states.

    # MARKER:kg-temporal-version-manager

    Stores serialised graph snapshots keyed by (source_key, version_tag, recorded_at).
    Each snapshot is a JSON blob of the Cytoscape-format graph returned by
    ``EntityGraph.to_cytoscape_json()``.

    Thread-safe via a module-level lock.  All writes are transactional.

    Parameters
    ----------
    db_path : str or Path or None
        Path to the SQLite file.  Defaults to a temporary file (useful for tests).
        Pass ``":memory:"`` for an in-memory store.
    """

    _lock: threading.Lock = threading.Lock()

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        if db_path is None:
            # Persist in a discoverable temp location rather than truly ephemeral
            db_path = Path(tempfile.gettempdir()) / "nybble_kg_versions.db"
        self._db_path = str(db_path)
        # For :memory: databases we must keep a single persistent connection
        # because each new sqlite3.connect(":memory:") opens a different, empty DB.
        self._memory_conn: Optional[sqlite3.Connection] = None
        if self._db_path == ":memory:":
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._memory_conn is not None:
            return self._memory_conn   # return the shared in-memory connection
        conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _close(self, conn: sqlite3.Connection) -> None:
        """Close *conn* unless it is the persistent in-memory connection."""
        if self._memory_conn is None:
            conn.close()

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS kg_snapshots (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_key   TEXT    NOT NULL,
                        version_tag  TEXT    NOT NULL DEFAULT '',
                        recorded_at  TEXT    NOT NULL,
                        snapshot     TEXT    NOT NULL,
                        node_count   INTEGER NOT NULL DEFAULT 0,
                        edge_count   INTEGER NOT NULL DEFAULT 0
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_kg_snap_source
                        ON kg_snapshots (source_key, recorded_at)
                """)
                conn.commit()
            finally:
                if self._memory_conn is None:
                    conn.close()

    def save_snapshot(
        self,
        source_key: str,
        graph_data: Dict[str, Any],
        *,
        version_tag: str = "",
        recorded_at: Optional[datetime] = None,
    ) -> int:
        """Persist *graph_data* (Cytoscape JSON dict) as a versioned snapshot.

        Parameters
        ----------
        source_key : str
            The KG source key this snapshot belongs to.
        graph_data : dict
            Cytoscape-format graph dict (``{"elements": {"nodes": [...], "edges": [...]}}``)
        version_tag : str
            Optional human-readable version label (e.g. ``"after-reindex"``).
        recorded_at : datetime or None
            When the snapshot was taken.  Defaults to now (UTC).

        Returns
        -------
        int
            The auto-incremented row ``id`` of the new snapshot.
        """
        if recorded_at is None:
            recorded_at = datetime.now(timezone.utc)

        nodes = graph_data.get("elements", {}).get("nodes", [])
        edges = graph_data.get("elements", {}).get("edges", [])

        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """INSERT INTO kg_snapshots
                       (source_key, version_tag, recorded_at, snapshot, node_count, edge_count)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        source_key,
                        version_tag,
                        serialize_temporal_value(recorded_at),
                        json.dumps(graph_data, separators=(",", ":")),
                        len(nodes),
                        len(edges),
                    ),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                self._close(conn)

    def get_snapshot(self, source_key: str, *, as_of: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """Return the most recent snapshot for *source_key* recorded at or before *as_of*.

        If *as_of* is None, returns the most recent snapshot regardless of time.

        Returns None if no matching snapshot exists.
        """
        conn = self._connect()
        try:
            if as_of is not None:
                ts = serialize_temporal_value(as_of)
                row = conn.execute(
                    """SELECT snapshot FROM kg_snapshots
                       WHERE source_key = ? AND recorded_at <= ?
                       ORDER BY recorded_at DESC LIMIT 1""",
                    (source_key, ts),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT snapshot FROM kg_snapshots
                       WHERE source_key = ?
                       ORDER BY recorded_at DESC LIMIT 1""",
                    (source_key,),
                ).fetchone()
            if row is None:
                return None
            return json.loads(row[0])
        finally:
            self._close(conn)

    def list_versions(self, source_key: str) -> List[Dict[str, Any]]:
        """Return all snapshot metadata (without the full blob) for *source_key*.

        Returns a list of dicts: ``{id, version_tag, recorded_at, node_count, edge_count}``.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT id, version_tag, recorded_at, node_count, edge_count
                   FROM kg_snapshots WHERE source_key = ?
                   ORDER BY recorded_at DESC""",
                (source_key,),
            ).fetchall()
            return [
                {
                    "id": r[0],
                    "version_tag": r[1],
                    "recorded_at": r[2],
                    "node_count": r[3],
                    "edge_count": r[4],
                }
                for r in rows
            ]
        finally:
            self._close(conn)

    def diff_snapshots(
        self,
        source_key: str,
        *,
        before: Optional[datetime] = None,
        after: Optional[datetime] = None,
        before_id: Optional[int] = None,
        after_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Compute a structural diff between two snapshots.

        # MARKER:kg-snapshot-diff

        Resolves the "before" snapshot as the most recent one recorded at or
        before *before* (or by explicit *before_id*), and similarly for "after".
        When only one is available, the diff treats the other as empty.

        Returns
        -------
        dict with keys:
          ``added_nodes``   - node ids present in after but not before
          ``removed_nodes`` - node ids present in before but not after
          ``added_edges``   - edge (src, tgt) tuples added
          ``removed_edges`` - edge (src, tgt) tuples removed
          ``node_delta``    - int: positive = net growth, negative = net shrink
          ``edge_delta``    - int
        """
        def _snap(id_: Optional[int], as_of_: Optional[datetime]) -> Dict[str, Any]:
            if id_ is not None:
                conn = self._connect()
                try:
                    row = conn.execute(
                        "SELECT snapshot FROM kg_snapshots WHERE id = ? AND source_key = ?",
                        (id_, source_key),
                    ).fetchone()
                    return json.loads(row[0]) if row else {}
                finally:
                    self._close(conn)
            return self.get_snapshot(source_key, as_of=as_of_) or {}

        snap_before = _snap(before_id, before)
        snap_after  = _snap(after_id, after)

        def _node_ids(snap: Dict[str, Any]) -> set:
            return {n["data"]["id"] for n in snap.get("elements", {}).get("nodes", [])}

        def _edge_pairs(snap: Dict[str, Any]) -> set:
            return {
                (e["data"]["source"], e["data"]["target"])
                for e in snap.get("elements", {}).get("edges", [])
            }

        before_nodes = _node_ids(snap_before)
        after_nodes  = _node_ids(snap_after)
        before_edges = _edge_pairs(snap_before)
        after_edges  = _edge_pairs(snap_after)

        added_nodes   = sorted(after_nodes - before_nodes)
        removed_nodes = sorted(before_nodes - after_nodes)
        added_edges   = sorted(after_edges - before_edges)
        removed_edges = sorted(before_edges - after_edges)

        return {
            "added_nodes":   added_nodes,
            "removed_nodes": removed_nodes,
            "added_edges":   added_edges,
            "removed_edges": removed_edges,
            "node_delta":    len(added_nodes) - len(removed_nodes),
            "edge_delta":    len(added_edges) - len(removed_edges),
        }


# ── TemporalGraphQuery ────────────────────────────────────────────────────────

class TemporalGraphQuery:
    """Point-in-time and range-based query engine over a list of relationship dicts.

    # MARKER:kg-temporal-graph-query

    Wraps a collection of relationship dicts (each may carry ``valid_from``,
    ``valid_until``, ``recorded_at``, ``superseded_at`` fields) and exposes
    fluent filter methods that return a new query object rather than mutating
    the original.

    Parameters
    ----------
    relationships : list[dict]
        The relationship dicts to query over.

    Examples
    --------
    >>> from datetime import datetime, timezone
    >>> rels = [{"src": "A", "tgt": "B", "valid_from": "2023-01-01Z", "valid_until": None}]
    >>> q = TemporalGraphQuery(rels)
    >>> q.as_of(datetime(2024, 1, 1, tzinfo=timezone.utc)).all()
    [{'src': 'A', 'tgt': 'B', 'valid_from': '2023-01-01Z', 'valid_until': None}]
    """

    def __init__(self, relationships: List[Dict[str, Any]]) -> None:
        self._rels: List[Dict[str, Any]] = list(relationships)

    def as_of(self, point: datetime) -> "TemporalGraphQuery":
        """Filter to relationships valid at *point* in time."""
        if point.tzinfo is None:
            point = point.replace(tzinfo=timezone.utc)
        filtered = [r for r in self._rels if relationship_active_at(r, point)]
        return TemporalGraphQuery(filtered)

    def between(
        self,
        start: Optional[datetime],
        end: Optional[Union[datetime, TemporalBound]],
    ) -> "TemporalGraphQuery":
        """Filter to relationships whose valid interval overlaps [start, end]."""
        filtered = [r for r in self._rels if relationship_overlaps(r, start, end)]
        return TemporalGraphQuery(filtered)

    def current_only(self) -> "TemporalGraphQuery":
        """Filter to relationships that are still current (superseded_at is OPEN)."""
        def _is_current(r: Dict[str, Any]) -> bool:
            raw = r.get("superseded_at")
            if raw is None:
                return True  # no superseded_at → assumed open
            if raw is TemporalBound.OPEN or raw == TemporalBound.OPEN.value:
                return True
            return False

        filtered = [r for r in self._rels if _is_current(r)]
        return TemporalGraphQuery(filtered)

    def source(self, source_key: str) -> "TemporalGraphQuery":
        """Filter to relationships from a specific source key."""
        filtered = [r for r in self._rels if r.get("source_key") == source_key]
        return TemporalGraphQuery(filtered)

    def relation(self, relation_type: str) -> "TemporalGraphQuery":
        """Filter to relationships with the given relation type (prefix match)."""
        filtered = [r for r in self._rels if str(r.get("relation", "")).startswith(relation_type)]
        return TemporalGraphQuery(filtered)

    def all(self) -> List[Dict[str, Any]]:
        """Return the current filtered relationship list."""
        return list(self._rels)

    def count(self) -> int:
        """Return the number of relationships in the current result set."""
        return len(self._rels)

    def __len__(self) -> int:
        return self.count()
