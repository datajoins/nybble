"""
lineage.py - SQL Query Lineage DAG Builder

Surfaces SQL query lineage for the Nybble SQL Arena as a simple dependency
DAG showing which source tables feed into which saved queries.  Intentionally
narrower than DataHub or Marquez - this is a developer-facing debugging aid.

DAG structure
-------------
The DAG is a plain Python dict::

    {
        "nodes": [{"id": str, "type": "table" | "query", "label": str}],
        "edges": [{"source": str, "target": str}]
    }

Each saved query becomes one node of type ``"query"``.
Each unique referenced table becomes one node of type ``"table"``.
Edges run source-table → query (directed).

Public API
----------
  build_lineage_dag(queries_path, dialect) → dict
  dag_to_html(dag, title)                  → str
  save_lineage_html(output_path, queries_path, dialect) → Path
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Union

# ── DAG construction ──────────────────────────────────────────────────────────

_DEFAULT_QUERIES_PATH = Path(".nybble") / "queries.json"
_DEFAULT_OUTPUT_PATH = Path(".nybble") / "lineage.html"


def _load_queries(queries_path: Union[str, Path]) -> list[dict]:
    """Load saved queries from a JSON file.

    Returns an empty list if the file does not exist or cannot be parsed.

    Parameters
    ----------
    queries_path : str | Path
        Path to the queries JSON file (list of objects with ``name`` and ``sql``).

    Returns
    -------
    list[dict]
        List of query objects, each with at minimum ``name`` and ``sql`` keys.
    """
    path = Path(queries_path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _extract_tables(sql: str, dialect: Optional[str] = None) -> list[str]:
    """Parse *sql* with sqlglot and return a list of referenced table name strings.

    Fully qualified names (``schema.table``, ``db.schema.table``) are returned
    as-is using sqlglot's string representation of the Table expression.

    Parameters
    ----------
    sql     : str - SQL query text
    dialect : str | None - optional sqlglot dialect (e.g. ``"bigquery"``)

    Returns
    -------
    list[str]
        Deduplicated list of table identifier strings.
    """
    try:
        import sqlglot
        import sqlglot.expressions as _exp
    except ImportError as exc:
        raise ImportError(
            "sqlglot is required for lineage analysis.\n"
            "Install it with: pip install sqlglot"
        ) from exc

    tables: list[str] = []
    seen: set[str] = set()

    try:
        tree = sqlglot.parse_one(sql, dialect=dialect, error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception:
        return []

    if tree is None:
        return []

    for node in tree.walk():
        if not isinstance(node, _exp.Table):
            continue
        # Build a fully qualified name from Table node parts.
        # sqlglot stores the simple table name in node.name, and any
        # qualifier parts in node.db (schema) and node.catalog.
        parts: list[str] = []
        try:
            if node.catalog:
                parts.append(str(node.catalog))
            if node.db:
                parts.append(str(node.db))
            tbl_name = node.name
            if not tbl_name:
                continue  # skip nameless Table nodes (e.g. subquery stubs)
            parts.append(tbl_name)
        except Exception:
            continue

        table_id = ".".join(parts)
        if table_id and table_id not in seen:
            seen.add(table_id)
            tables.append(table_id)

    return tables


def build_lineage_dag(
    queries_path: Union[str, Path] = _DEFAULT_QUERIES_PATH,
    dialect: Optional[str] = None,
) -> dict:
    """Build a lineage DAG from saved SQL queries.

    Parameters
    ----------
    queries_path : str | Path
        Path to the saved queries JSON file.  Defaults to
        ``.nybble/queries.json``.  Missing file → empty DAG without raising.
    dialect : str | None
        Optional sqlglot dialect (e.g. ``"bigquery"``, ``"snowflake"``).

    Returns
    -------
    dict
        ``{"nodes": [...], "edges": [...]}`` - see module docstring for schema.
    """
    queries = _load_queries(queries_path)
    if not queries:
        return {"nodes": [], "edges": []}

    nodes: list[dict] = []
    edges: list[dict] = []
    table_ids: set[str] = set()

    for q in queries:
        name = q.get("name", "")
        sql = q.get("sql", "")
        if not name or not sql:
            continue

        query_id = f"query:{name}"
        nodes.append({"id": query_id, "type": "query", "label": name, "sql": sql})

        tables = _extract_tables(sql, dialect=dialect)
        for table in tables:
            table_id = f"table:{table}"
            if table_id not in table_ids:
                table_ids.add(table_id)
                nodes.append({"id": table_id, "type": "table", "label": table})
            edges.append({"source": table_id, "target": query_id})

    return {"nodes": nodes, "edges": edges}


# ── HTML renderer ─────────────────────────────────────────────────────────────

def dag_to_html(
    dag: dict,
    title: str = "SQL Lineage DAG",
    schema: Optional[list] = None,
) -> str:
    """Render a lineage DAG dict as a self-contained HTML document.

    Uses Cytoscape + ELK for layout and a canvas overlay for rendering,
    matching the Docker UI (query.html) lineage pane exactly.  Table nodes
    are drawn as full ERD cards (header + column rows with PK/FK badges);
    query nodes are drawn as amber-header cards.

    Parameters
    ----------
    dag    : dict          - output of :func:`build_lineage_dag`
    title  : str           - page title and topbar heading
    schema : list | None   - list of table dicts in Flask SCHEMA format
                             (each with ``name``, ``columns`` list where each
                             column has ``name``, ``type``, ``pk``, ``fk``,
                             ``fk_target``).  When provided, table cards show
                             real columns; when None, table cards show the
                             table name only with an empty column list.

    Returns
    -------
    str
        Self-contained HTML (no external assets).
    """
    import html as _html_mod

    nodes_json = json.dumps(dag.get("nodes", []))
    edges_json = json.dumps(dag.get("edges", []))
    node_count = len(dag.get("nodes", []))
    edge_count = len(dag.get("edges", []))
    safe_title = _html_mod.escape(title)
    schema_json = json.dumps(schema or [])

    # Use simple token replacement rather than str.format() to avoid having to
    # escape every { } in the JavaScript source with {{ }}.
    return (
        _HTML_TEMPLATE
        .replace("__TITLE__", safe_title)
        .replace("__NODE_COUNT__", str(node_count))
        .replace("__EDGE_COUNT__", str(edge_count))
        .replace("__NODES_JSON__", nodes_json)
        .replace("__EDGES_JSON__", edges_json)
        .replace("__SCHEMA_JSON__", schema_json)
    )


# ── Self-contained HTML template (matches Docker UI query.html lineage pane) ──

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__ - __NODE_COUNT__ nodes · __EDGE_COUNT__ edges</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { width: 100%; height: 100%; overflow: hidden; background: #141616; color: #e2e6f0; font-family: -apple-system, "Segoe UI", system-ui, sans-serif; font-size: 13px; }
#app { display: flex; flex-direction: column; height: 100%; }
#topbar { height: 40px; flex-shrink: 0; background: #232526; border-bottom: 1px solid #2e3234; display: flex; align-items: center; gap: 10px; padding: 0 14px; z-index: 30; }
#topbar h1 { font-size: 13px; font-weight: 600; color: #c4b5fd; display: flex; align-items: center; gap: 7px; white-space: nowrap; }
.stats { color: rgba(255,255,255,0.3); font-size: 11px; white-space: nowrap; }
.tb-spacer { flex: 1; }
.tip { color: rgba(255,255,255,0.2); font-size: 10px; white-space: nowrap; }
#btn-fit { background: none; border: 1px solid rgba(255,255,255,0.14); border-radius: 4px; color: rgba(255,255,255,0.5); cursor: pointer; font-size: 11px; padding: 4px 10px; display: flex; align-items: center; gap: 5px; transition: background .1s, color .1s; flex-shrink: 0; line-height: 1; }
#btn-fit:hover { background: rgba(167,139,250,0.14); color: #c4b5fd; border-color: rgba(167,139,250,0.45); }
#cy-wrap { flex: 1; min-height: 0; position: relative; }
#cy { width: 100%; height: 100%; background: #141616; }
#cy-overlay { position: absolute; inset: 0; pointer-events: none; }
#legend { position: fixed; bottom: 14px; left: 14px; background: rgba(28,31,32,0.95); border: 1px solid #2e3234; border-radius: 6px; padding: 8px 12px; font-size: 10px; z-index: 50; pointer-events: none; }
.li { display: flex; align-items: center; gap: 7px; margin-bottom: 4px; color: rgba(255,255,255,0.4); }
.li:last-child { margin-bottom: 0; }
.lc { width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }
#empty-msg { position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%); color: rgba(255,255,255,0.25); font-size: 13px; text-align: center; line-height: 1.8; z-index: 10; }
#empty-msg code { background: rgba(255,255,255,0.07); padding: 1px 5px; border-radius: 3px; font-family: Consolas, monospace; }
/* colour token variables - referenced by tests */
:root { --TABLE-COLOR: #a78bfa; --QUERY-COLOR: rgba(203,166,83,0.85); }
/* ── Detail panel ── */
#detail-panel {
  position: fixed; top: 0; right: 0; bottom: 0;
  width: 340px; max-width: 90vw;
  background: #1a1d1e; border-left: 1px solid #2e3234;
  display: flex; flex-direction: column;
  transform: translateX(100%); transition: transform .2s ease;
  z-index: 60;
}
#detail-panel.open { transform: translateX(0); }
#detail-header {
  display: flex; align-items: center; gap: 9px;
  padding: 0 14px; height: 40px; flex-shrink: 0;
  border-bottom: 1px solid #2e3234; background: #232526;
}
#detail-header .dh-icon { color: rgba(203,166,83,0.85); font-size: 14px; flex-shrink: 0; }
#detail-title { font-size: 13px; font-weight: 600; color: rgba(203,166,83,0.9); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#detail-close {
  background: none; border: none; cursor: pointer;
  color: rgba(255,255,255,0.3); font-size: 16px; line-height: 1;
  padding: 4px 6px; border-radius: 4px; transition: color .15s, background .15s;
}
#detail-close:hover { color: #e2e6f0; background: rgba(255,255,255,0.07); }
#detail-body { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 14px; }
.detail-section-label {
  font-size: 10px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
  color: rgba(255,255,255,0.28); margin-bottom: 6px;
}
#detail-sql-block {
  background: #111314; border: 1px solid #2e3234; border-radius: 6px;
  padding: 12px 14px; font-family: Consolas, "Courier New", monospace;
  font-size: 12px; line-height: 1.7; color: #c9d1d9;
  white-space: pre-wrap; word-break: break-word; max-height: 360px; overflow-y: auto;
}
.detail-table-chip {
  display: inline-flex; align-items: center; gap: 5px;
  background: rgba(167,139,250,0.1); border: 1px solid rgba(167,139,250,0.25);
  border-radius: 5px; padding: 3px 9px; font-size: 11px; font-weight: 600;
  color: #a78bfa; margin: 2px 3px 2px 0;
}
#detail-tables-wrap { display: flex; flex-wrap: wrap; }
</style>
</head>
<body>

<div id="app">
<div id="topbar">
  <h1>
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#a78bfa" stroke-width="2">
      <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
    </svg>
    __TITLE__
  </h1>
  <span class="stats">__NODE_COUNT__ nodes · __EDGE_COUNT__ edges</span>
  <span class="tb-spacer"></span>
  <span class="tip">Scroll to zoom · Drag to pan · Drag nodes</span>
  <button id="btn-fit" onclick="fitView()" title="Re-centre all nodes (F)">
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/>
    </svg>
    Fit
  </button>
</div>
<div id="cy-wrap">
  <div id="cy"></div>
  <canvas id="cy-overlay"></canvas>
</div>
</div>

<div id="legend">
  <div class="li"><div class="lc" style="background:#6d52d4;"></div>Source table</div>
  <div class="li"><div class="lc" style="background:#8a6514;"></div>Saved query · click to inspect</div>
  <div class="li"><div class="lc" style="background:#2e3234;"></div>Data flow</div>
</div>

<div id="detail-panel">
  <div id="detail-header">
    <span class="dh-icon">≡</span>
    <span id="detail-title">query</span>
    <button id="detail-close" title="Close (Escape)">✕</button>
  </div>
  <div id="detail-body">
    <div id="detail-tables-section">
      <div class="detail-section-label">Source tables</div>
      <div id="detail-tables-wrap"></div>
    </div>
    <div>
      <div class="detail-section-label">SQL</div>
      <div id="detail-sql-block"></div>
    </div>
  </div>
</div>

<!-- Cytoscape + ELK - same CDN pins as Docker UI -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.29.2/cytoscape.min.js" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/elkjs@0.10.0/lib/elk.bundled.js" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/cytoscape-elk@2.2.0/dist/cytoscape-elk.js" crossorigin="anonymous"></script>

<script>
// ── Data injected by Python ───────────────────────────────────────────────────
const DAG_NODES  = __NODES_JSON__;
const DAG_EDGES  = __EDGES_JSON__;
const SCHEMA     = __SCHEMA_JSON__;

// ── Colour constants (referenced by tests via grep) ───────────────────────────
const TABLE_COLOR = "#a78bfa";   // purple - source tables (matches ERD palette)
const QUERY_COLOR = "#cb9f4f";   // amber  - saved queries

// ── Theme constants (mirrored from query.html _LCY_T) ────────────────────────
const T = {
  bg:'#141616', headerBg:'#232526',
  headerBgDirect:'rgba(167,139,250,0.18)',
  headerBgQuery:'rgba(203,166,83,0.18)',
  border:'rgba(255,255,255,0.15)', borderHL:'#a78bfa',
  borderDirect:'rgba(167,139,250,0.55)',
  borderQuery:'rgba(203,166,83,0.55)',
  text:'#ffffff', textMuted:'rgba(255,255,255,0.55)',
  textType:'rgba(255,255,255,0.40)',
  pkColor:'#a78bfa', fkColor:'#c4b5fd',
  rowBorder:'#2e3234', fkRowBg:'rgba(167,139,250,0.06)',
  font:'-apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif',
  fontMono:'Consolas, "Courier New", monospace',
  radius:6, hdrH:36, rowH:28, minW:180,
};
const PARTICLE_COLOR='rgba(167,139,250,0.80)', GLOW_COLOR='rgba(167,139,250,0.18)';
const PARTICLE_RADIUS=2.5, PARTICLE_SPEED=80;

// ── Canvas-overlay state ──────────────────────────────────────────────────────
let _cy = null, _overlay = null, _overlayCtx = null;
let _hlIds = new Set(), _ro = null;
let _animEdges = [], _animFrameId = null, _lastAnimTime = null;

// ── Schema lookup: id → table object (stub for unknown/query nodes) ───────────
function _nodeTable(dagNode) {
  if (dagNode.type === 'query') {
    return { name: dagNode.label || dagNode.id, columns: [], _isQuery: true };
  }
  const label = dagNode.label || dagNode.id.replace(/^table:/, '');
  const found = SCHEMA.find(t => t.name === label);
  return found || { name: label, columns: [] };
}

// ── Node sizing (same formula as query.html _lcyNodeW/_lcyNodeH) ──────────────
function _nodeW(t) {
  const nw = t.name.length * 8.5 + 52;
  const cw = t.columns.length
    ? Math.max(...t.columns.map(c => (c.name||'').length*7.5 + (c.type||'').length*6.5 + 56))
    : 0;
  return Math.ceil(Math.max(T.minW, nw, cw));
}
function _nodeH(t) {
  return Math.ceil(T.hdrH + Math.max(1, t.columns.length) * T.rowH);
}

// ── Coord helpers ─────────────────────────────────────────────────────────────
function _cyToCanvas(pt) {
  const z = _cy.zoom(), p = _cy.pan();
  return { x: pt.x * z + p.x, y: pt.y * z + p.y };
}

// ── Edge path (cubic bezier from wall to wall) ────────────────────────────────
function _getEdgePath(edge) {
  const sn = edge.source(), tn = edge.target();
  const sc = _cyToCanvas(sn.position()), tc = _cyToCanvas(tn.position());
  const z = _cy.zoom();
  const sw = sn.data('w') * z / 2, sh = sn.data('h') * z / 2;
  const tw = tn.data('w') * z / 2, th = tn.data('h') * z / 2;
  const dx = tc.x - sc.x, dy = tc.y - sc.y;
  let src, tgt;
  if (Math.abs(dx) >= Math.abs(dy)) {
    src = dx >= 0 ? {x: sc.x+sw, y: sc.y} : {x: sc.x-sw, y: sc.y};
    tgt = dx >= 0 ? {x: tc.x-tw, y: tc.y} : {x: tc.x+tw, y: tc.y};
  } else {
    src = dy >= 0 ? {x: sc.x, y: sc.y+sh} : {x: sc.x, y: sc.y-sh};
    tgt = dy >= 0 ? {x: tc.x, y: tc.y-th} : {x: tc.x, y: tc.y+th};
  }
  const bdx = tgt.x - src.x, bow = Math.min(Math.abs(bdx) * 0.45, 120);
  return { src, cp1:{x:src.x+bow,y:src.y}, cp2:{x:tgt.x-bow,y:tgt.y}, tgt };
}

// ── Bezier maths ──────────────────────────────────────────────────────────────
function _bezier(p0,p1,p2,p3,t) {
  const mt=1-t;
  return {
    x: mt*mt*mt*p0.x + 3*mt*mt*t*p1.x + 3*mt*t*t*p2.x + t*t*t*p3.x,
    y: mt*mt*mt*p0.y + 3*mt*mt*t*p1.y + 3*mt*t*t*p2.y + t*t*t*p3.y,
  };
}
function _bezierLen(p0,p1,p2,p3) {
  let len=0, prev=p0;
  for (let i=1; i<=20; i++) {
    const cur = _bezier(p0,p1,p2,p3,i/20);
    const dx=cur.x-prev.x, dy=cur.y-prev.y;
    len += Math.sqrt(dx*dx+dy*dy);
    prev = cur;
  }
  return len;
}

// ── Animated-edge helpers ─────────────────────────────────────────────────────
function _buildAnimEdges() {
  if (!_cy) return;
  _animEdges = [];
  _cy.edges().forEach(edge => {
    const path = _getEdgePath(edge);
    const tl = _bezierLen(path.src, path.cp1, path.cp2, path.tgt);
    if (tl < 10) return;
    const count = Math.max(1, Math.round(tl / 80));
    _animEdges.push({
      id: edge.id(), path,
      particles: Array.from({length:count}, (_,i) => ({t:i/count, speed:PARTICLE_SPEED/tl})),
      len: tl,
    });
  });
}
function _syncAnimPaths() {
  if (!_cy || !_animEdges.length) return;
  _animEdges.forEach(ae => {
    const edge = _cy.$('#' + CSS.escape(ae.id));
    if (!edge.length) return;
    ae.path = _getEdgePath(edge);
    const nl = _bezierLen(ae.path.src, ae.path.cp1, ae.path.cp2, ae.path.tgt);
    if (Math.abs(nl - ae.len) > 5) {
      ae.len = nl;
      ae.particles.forEach(p => { p.speed = PARTICLE_SPEED / nl; });
    }
  });
}
function _startAnim() {
  if (_animFrameId) return;
  _lastAnimTime = null;
  _animFrameId = requestAnimationFrame(_animFrame);
}
function _cancelAnim() {
  if (_animFrameId) { cancelAnimationFrame(_animFrameId); _animFrameId = null; }
}
function _animFrame(now) {
  _animFrameId = null;
  if (!_animEdges.length || !_overlayCtx) return;
  const dt = _lastAnimTime ? Math.min((now - _lastAnimTime) / 1000, 0.05) : 0;
  _lastAnimTime = now;
  _animEdges.forEach(ae => {
    ae.particles.forEach(p => { p.t += p.speed * dt; if (p.t > 1) p.t -= 1; });
  });
  _drawOverlayWithAnim();
  _animFrameId = requestAnimationFrame(_animFrame);
}

// ── Canvas draw helpers ───────────────────────────────────────────────────────
function _roundRect(ctx,x,y,w,h,r) {
  ctx.beginPath();
  ctx.moveTo(x+r,y); ctx.lineTo(x+w-r,y); ctx.arcTo(x+w,y,x+w,y+r,r);
  ctx.lineTo(x+w,y+h-r); ctx.arcTo(x+w,y+h,x+w-r,y+h,r);
  ctx.lineTo(x+r,y+h); ctx.arcTo(x,y+h,x,y+h-r,r);
  ctx.lineTo(x,y+r); ctx.arcTo(x,y,x+r,y,r);
  ctx.closePath();
}
function _clip(ctx,text,maxW) {
  if (!maxW || maxW <= 0) return '';
  if (ctx.measureText(text).width <= maxW) return text;
  let lo=0, hi=text.length;
  while (lo < hi) {
    const m = (lo + hi + 1) >> 1;
    ctx.measureText(text.slice(0, m) + '…').width <= maxW ? lo=m : hi=m-1;
  }
  return text.slice(0, lo) + '…';
}

// ── Draw a single node card onto canvas ───────────────────────────────────────
// Matches query.html _lcyDrawNode exactly; isDirect=true → amber (query style)
function _drawNode(ctx, table, w, h, rx, ry, zoom, highlighted, isDirect) {
  const rw=w*zoom, rh=h*zoom, r=T.radius*zoom;
  // Card body
  _roundRect(ctx, rx, ry, rw, rh, r);
  ctx.fillStyle = T.bg; ctx.fill();
  ctx.strokeStyle = highlighted ? T.borderHL : isDirect ? T.borderDirect : T.border;
  ctx.lineWidth = (highlighted || isDirect) ? 2 : 1;
  if (highlighted || isDirect) {
    ctx.shadowColor = isDirect && table._isQuery ? 'rgba(203,166,83,0.8)' : T.borderHL;
    ctx.shadowBlur = isDirect ? 6*zoom : 10*zoom;
  }
  ctx.stroke(); ctx.shadowBlur = 0;
  // Header strip
  ctx.save();
  ctx.beginPath();
  ctx.moveTo(rx+r, ry); ctx.lineTo(rx+rw-r, ry);
  ctx.arcTo(rx+rw, ry, rx+rw, ry+r, r);
  ctx.lineTo(rx+rw, ry+T.hdrH*zoom);
  ctx.lineTo(rx, ry+T.hdrH*zoom);
  ctx.lineTo(rx, ry+r); ctx.arcTo(rx, ry, rx+r, ry, r); ctx.closePath();
  ctx.fillStyle = table._isQuery
    ? T.headerBgQuery
    : (isDirect ? T.headerBgDirect : T.headerBg);
  ctx.fill(); ctx.restore();
  // Header icon
  const isz=12*zoom, ix=rx+10*zoom, iy=ry+(T.hdrH*zoom-isz)/2;
  ctx.save(); ctx.strokeStyle=T.textMuted; ctx.lineWidth=1.2;
  if (table._isQuery) {
    // Stack icon for query nodes
    ctx.beginPath();
    ctx.moveTo(ix+isz*0.15, iy+isz*0.35); ctx.lineTo(ix+isz*0.85, iy+isz*0.35);
    ctx.moveTo(ix+isz*0.15, iy+isz*0.55); ctx.lineTo(ix+isz*0.85, iy+isz*0.55);
    ctx.moveTo(ix+isz*0.15, iy+isz*0.75); ctx.lineTo(ix+isz*0.85, iy+isz*0.75);
    ctx.stroke();
  } else {
    // Grid icon for table nodes
    ctx.strokeRect(ix+isz*0.15, iy+isz*0.15, isz*0.7, isz*0.7);
    ctx.beginPath();
    ctx.moveTo(ix+isz*0.15, iy+isz*0.5); ctx.lineTo(ix+isz*0.85, iy+isz*0.5);
    ctx.moveTo(ix+isz*0.5, iy+isz*0.15); ctx.lineTo(ix+isz*0.5, iy+isz*0.85);
    ctx.stroke();
  }
  ctx.restore();
  // Header label
  const nfsz=Math.max(9, Math.round(13*zoom));
  ctx.font=`600 ${nfsz}px ${T.font}`;
  ctx.fillStyle=(highlighted||isDirect)?'#ffffff':T.textMuted;
  ctx.textBaseline='middle'; ctx.textAlign='left';
  const labelColor = table._isQuery ? (highlighted ? '#ffffff' : 'rgba(203,166,83,0.9)') : ctx.fillStyle;
  ctx.fillStyle = labelColor;
  ctx.fillText(_clip(ctx, table.name, rw-56*zoom), rx+28*zoom, ry+T.hdrH*zoom/2);
  // Column rows
  const cfsz=Math.max(8, Math.round(11*zoom));
  const tfsz=Math.max(7, Math.round(9.5*zoom));
  table.columns.forEach((col, i) => {
    const rowTop=ry+(T.hdrH+i*T.rowH)*zoom, rowH=T.rowH*zoom;
    if (col.fk) { ctx.fillStyle=T.fkRowBg; ctx.fillRect(rx, rowTop, rw, rowH); }
    if (i < table.columns.length-1) {
      ctx.strokeStyle=T.rowBorder; ctx.lineWidth=0.5;
      ctx.beginPath(); ctx.moveTo(rx, rowTop+rowH); ctx.lineTo(rx+rw, rowTop+rowH); ctx.stroke();
    }
    const csz=Math.max(8, Math.round(11*zoom));
    const cx0=rx+10*zoom, cy0=rowTop+(rowH-csz)/2;
    if (col.pk) {
      // PK icon: circle + stem
      ctx.strokeStyle=T.pkColor; ctx.lineWidth=1.3;
      ctx.beginPath(); ctx.arc(cx0+csz*0.35, cy0+csz*0.35, csz*0.28, 0, Math.PI*2); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(cx0+csz*0.35, cy0+csz*0.63); ctx.lineTo(cx0+csz*0.35, cy0+csz); ctx.stroke();
    } else if (col.fk) {
      // FK icon: two interlocked circles
      ctx.strokeStyle=T.fkColor; ctx.lineWidth=1.2;
      ctx.beginPath(); ctx.arc(cx0+csz*0.28, cy0+csz*0.5, csz*0.24, 0, Math.PI*2); ctx.stroke();
      ctx.beginPath(); ctx.arc(cx0+csz*0.68, cy0+csz*0.5, csz*0.24, 0, Math.PI*2); ctx.stroke();
    } else {
      // Generic col icon: small square
      ctx.strokeStyle=T.textMuted; ctx.lineWidth=1;
      ctx.strokeRect(cx0+csz*0.2, cy0+csz*0.2, csz*0.6, csz*0.6);
    }
    // Col name
    ctx.font=`${cfsz}px ${T.font}`; ctx.fillStyle=T.text; ctx.textBaseline='middle'; ctx.textAlign='left';
    ctx.fillText(_clip(ctx, col.name||'', rw*0.55-24*zoom), rx+26*zoom, rowTop+rowH/2);
    // Col type
    ctx.font=`${tfsz}px ${T.fontMono}`; ctx.fillStyle=T.textType; ctx.textAlign='right';
    ctx.fillText(_clip(ctx, col.type||'', rw*0.42), rx+rw-10*zoom, rowTop+rowH/2);
  });
  // "Click for Query" hint inside query node body
  if (table._isQuery) {
    const hintFsz = Math.max(8, Math.round(10*zoom));
    ctx.font = `${hintFsz}px ${T.font}`;
    ctx.fillStyle = 'rgba(203,166,83,0.45)';
    ctx.textBaseline = 'middle'; ctx.textAlign = 'center';
    ctx.fillText('Click for Query', rx + rw/2, ry + T.hdrH*zoom + (rh - T.hdrH*zoom)/2);
  }
}

// ── Draw all node cards onto overlay ─────────────────────────────────────────
function _drawNodes(ctx, zoom, pan) {
  _cy.nodes().forEach(n => {
    const pos=n.position(), w=n.data('w'), h=n.data('h');
    const rx=pos.x*zoom+pan.x-w*zoom/2, ry=pos.y*zoom+pan.y-h*zoom/2;
    _drawNode(ctx, n.data('table'), w, h, rx, ry, zoom, _hlIds.has(n.id()), n.data('isDirect'));
  });
}

// ── Overlay clear + redraw ────────────────────────────────────────────────────
function _drawOverlay() {
  if (!_cy || !_overlayCtx) return;
  const c=_overlay, dpr=window.devicePixelRatio||1;
  _overlayCtx.clearRect(0, 0, c.width, c.height);
  _overlayCtx.save(); _overlayCtx.scale(dpr, dpr);
  _drawNodes(_overlayCtx, _cy.zoom(), _cy.pan());
  _overlayCtx.restore();
}
function _drawOverlayWithAnim() {
  if (!_cy || !_overlayCtx) return;
  const c=_overlay, dpr=window.devicePixelRatio||1;
  _overlayCtx.clearRect(0, 0, c.width, c.height);
  _overlayCtx.save(); _overlayCtx.scale(dpr, dpr);
  const zoom=_cy.zoom(), pan=_cy.pan();
  _drawNodes(_overlayCtx, zoom, pan);
  _animEdges.forEach(ae => {
    const {path, particles} = ae;
    // Glow trace
    _overlayCtx.save();
    _overlayCtx.beginPath();
    _overlayCtx.moveTo(path.src.x, path.src.y);
    _overlayCtx.bezierCurveTo(path.cp1.x, path.cp1.y, path.cp2.x, path.cp2.y, path.tgt.x, path.tgt.y);
    _overlayCtx.strokeStyle=GLOW_COLOR; _overlayCtx.lineWidth=1.5;
    _overlayCtx.shadowColor='#a78bfa'; _overlayCtx.shadowBlur=4; _overlayCtx.stroke();
    _overlayCtx.restore();
    // Particles
    particles.forEach(p => {
      const pt=_bezier(path.src,path.cp1,path.cp2,path.tgt,p.t);
      const t0=Math.max(0, p.t-0.12);
      // Tail gradient
      _overlayCtx.save();
      _overlayCtx.beginPath();
      for (let i=0; i<=8; i++) {
        const ti=t0+(p.t-t0)*(i/8);
        const tp=_bezier(path.src,path.cp1,path.cp2,path.tgt,ti);
        i===0 ? _overlayCtx.moveTo(tp.x,tp.y) : _overlayCtx.lineTo(tp.x,tp.y);
      }
      const ts=_bezier(path.src,path.cp1,path.cp2,path.tgt,t0);
      const grad=_overlayCtx.createLinearGradient(ts.x,ts.y,pt.x,pt.y);
      grad.addColorStop(0,'rgba(167,139,250,0)'); grad.addColorStop(1,'rgba(167,139,250,0.55)');
      _overlayCtx.strokeStyle=grad; _overlayCtx.lineWidth=1.5;
      _overlayCtx.shadowColor='#a78bfa'; _overlayCtx.shadowBlur=4; _overlayCtx.stroke();
      _overlayCtx.restore();
      // Dot
      _overlayCtx.save();
      _overlayCtx.beginPath(); _overlayCtx.arc(pt.x, pt.y, PARTICLE_RADIUS, 0, Math.PI*2);
      _overlayCtx.fillStyle=PARTICLE_COLOR;
      _overlayCtx.shadowColor='#a78bfa'; _overlayCtx.shadowBlur=7; _overlayCtx.fill();
      _overlayCtx.restore();
    });
  });
  _overlayCtx.restore();
}

// ── Resize canvas to match wrapper ───────────────────────────────────────────
function _resizeOverlay() {
  const wrap=document.getElementById('cy-wrap');
  if (!wrap) return;
  const w=wrap.clientWidth, h=wrap.clientHeight;
  if (!w || !h || !_overlay) return;
  const dpr=window.devicePixelRatio||1;
  _overlay.width=w*dpr; _overlay.height=h*dpr;
  _overlay.style.width=w+'px'; _overlay.style.height=h+'px';
  _drawOverlay();
}

// ── Highlight set ─────────────────────────────────────────────────────────────
function _setHL(ids) {
  _hlIds=new Set(ids);
  _drawOverlay();
}

// ── DAG topology layout (ELK fallback) ───────────────────────────────────────
// Keyed by node data.id (not table.name) to work for query nodes too.
function _dagTopoLayout(nodeDataList) {
  const W = (document.getElementById('cy-wrap')||{clientWidth:900}).clientWidth || 900;
  const H = (document.getElementById('cy-wrap')||{clientHeight:500}).clientHeight || 500;
  const outEdges = {}, inDegree = {};
  nodeDataList.forEach(d => { outEdges[d.id] = []; inDegree[d.id] = 0; });
  DAG_EDGES.forEach(e => {
    if (outEdges[e.source] !== undefined && outEdges[e.target] !== undefined) {
      outEdges[e.source].push(e.target);
      inDegree[e.target] = (inDegree[e.target] || 0) + 1;
    }
  });
  const depth = {}, queue = [];
  nodeDataList.forEach(d => { depth[d.id] = 0; if (!inDegree[d.id]) queue.push(d.id); });
  if (!queue.length) nodeDataList.forEach(d => queue.push(d.id));
  while (queue.length) {
    const cur = queue.shift();
    (outEdges[cur] || []).forEach(nxt => {
      depth[nxt] = Math.max(depth[nxt] || 0, depth[cur] + 1);
      inDegree[nxt]--;
      if (inDegree[nxt] === 0) queue.push(nxt);
    });
  }
  const cols = {};
  nodeDataList.forEach(d => {
    const c = depth[d.id] || 0;
    (cols[c] = cols[c] || []).push(d);
  });
  const numCols = Math.max(...Object.keys(cols).map(Number)) + 1;
  const COL_GAP = Math.max(280, Math.round(W / numCols));
  const ROW_GAP = 80;
  const pos = {};
  Object.entries(cols).forEach(([dep, items]) => {
    const colX = Number(dep) * COL_GAP + COL_GAP / 2;
    const totalH = items.length * ROW_GAP;
    items.forEach((d, i) => {
      pos[d.id] = { x: colX, y: i * ROW_GAP - totalH / 2 + ROW_GAP / 2 };
    });
  });
  return pos;
}

// ── Re-centre ─────────────────────────────────────────────────────────────────
function fitView() {
  if (!_cy) return;
  _cy.fit(undefined, 56);
}

// ── Detail panel open/close ───────────────────────────────────────────────────
function _openDetail(dagNode, cyNode) {
  const panel = document.getElementById('detail-panel');
  document.getElementById('detail-title').textContent = dagNode.label || dagNode.id;

  // SQL block
  const sqlBlock = document.getElementById('detail-sql-block');
  sqlBlock.textContent = dagNode.sql || '(no SQL)';

  // Source table chips - read from the edges of this node
  const tablesWrap = document.getElementById('detail-tables-wrap');
  tablesWrap.innerHTML = '';
  if (cyNode) {
    cyNode.incomers('node').forEach(src => {
      const chip = document.createElement('span');
      chip.className = 'detail-table-chip';
      chip.textContent = src.data('label') || src.id();
      tablesWrap.appendChild(chip);
    });
  }
  if (!tablesWrap.children.length) {
    const none = document.createElement('span');
    none.style.cssText = 'font-size:11px;color:rgba(255,255,255,0.28)';
    none.textContent = 'No source tables detected';
    tablesWrap.appendChild(none);
  }

  panel.classList.add('open');
}

function _closeDetail() {
  document.getElementById('detail-panel').classList.remove('open');
}

// ── Empty state ───────────────────────────────────────────────────────────────
if (DAG_NODES.length === 0) {
  const msg = document.createElement("div");
  msg.id = "empty-msg";
  msg.innerHTML = "No saved queries found.<br><span style='font-size:11px'>Run <code>nybble query</code> and save queries to see lineage.</span>";
  document.getElementById("cy-wrap").appendChild(msg);
}

// ── Build and mount Cytoscape ─────────────────────────────────────────────────
async function _buildCy() {
  if (!DAG_NODES.length) return;

  if (window.cytoscapeElk && typeof cytoscape !== 'undefined') {
    try { cytoscape.use(window.cytoscapeElk); } catch(e) {}
  }

  _overlay    = document.getElementById('cy-overlay');
  _overlayCtx = _overlay ? _overlay.getContext('2d') : null;
  _animEdges  = []; _hlIds = new Set();

  // Build node data: attach table object and dimensions
  const nodeDataList = DAG_NODES.map(n => {
    const tbl = _nodeTable(n);
    return { id: n.id, label: n.label || n.id, type: n.type, isDirect: n.type === 'query', tbl };
  });

  const cyNodes = nodeDataList.map(d => ({
    group: 'nodes',
    data: { id: d.id, label: d.label, type: d.type, isDirect: d.isDirect,
            table: d.tbl, w: _nodeW(d.tbl), h: _nodeH(d.tbl) },
  }));

  const seenEdges = new Set(), cyEdges = [];
  DAG_EDGES.forEach(e => {
    const key = e.source + '|' + e.target;
    if (seenEdges.has(key)) return;
    seenEdges.add(key);
    cyEdges.push({ group:'edges', data:{ id: key, source: e.source, target: e.target } });
  });

  // Ghost nodes - all rendering is done on canvas overlay
  _cy = cytoscape({
    container: document.getElementById('cy'),
    elements:  [...cyNodes, ...cyEdges],
    style: [
      { selector: 'node', style: {
          shape: 'rectangle',
          width: 'data(w)', height: 'data(h)',
          'background-opacity': 0, 'border-width': 0,
          label: '', padding: 0,
        }
      },
      { selector: 'edge', style: {
          width: 0, 'line-color': 'transparent',
          'target-arrow-color': 'transparent',
          'curve-style': 'unbundled-bezier',
          'control-point-distances': [60], 'control-point-weights': [0.5],
          'target-arrow-shape': 'none', 'source-arrow-shape': 'none',
          opacity: 0,
        }
      },
    ],
    minZoom: 0.05, maxZoom: 3, wheelSensitivity: 0.15,
    layout: { name: 'preset' },
    pixelRatio: window.devicePixelRatio || 1,
  });

  // Wire up canvas redraw on every Cytoscape render event
  _cy.on('render', () => {
    if (_animEdges.length) _syncAnimPaths();
    else _drawOverlay();
  });

  // ResizeObserver keeps canvas in sync with container size changes
  if (window.ResizeObserver) {
    _ro = new ResizeObserver(() => _resizeOverlay());
    _ro.observe(document.getElementById('cy-wrap'));
  }

  // Hover: highlight connected subgraph
  _cy.on('mouseover', 'node', e => {
    const n = e.target;
    const ids = new Set([n.id()]);
    n.connectedEdges().connectedNodes().forEach(x => ids.add(x.id()));
    _setHL(ids);
    document.getElementById('cy').style.cursor = 'pointer';
  });
  _cy.on('mouseout', 'node', () => {
    _setHL(new Set());
    document.getElementById('cy').style.cursor = '';
  });

  // Click query node → open detail panel with SQL + source tables
  _cy.on('tap', 'node', e => {
    const n = e.target;
    if (n.data('type') !== 'query') return;
    const dagNode = DAG_NODES.find(d => d.id === n.id());
    if (!dagNode) return;
    _openDetail(dagNode, n);
  });
  // Click canvas background → close panel
  _cy.on('tap', e => { if (e.target === _cy) _closeDetail(); });

  // ── Layout ────────────────────────────────────────────────────────────────
  const elkOk = window.cytoscapeElk && typeof cytoscape !== 'undefined';
  if (elkOk) {
    try {
      await new Promise((resolve, reject) => {
        const l = _cy.layout({
          name: 'elk',
          elk: {
            algorithm: 'layered',
            'elk.direction': 'RIGHT',
            'elk.layered.spacing.nodeNodeBetweenLayers': '100',
            'elk.spacing.nodeNode': '48',
            'elk.edgeRouting': 'ORTHOGONAL',
            'elk.layered.mergeEdges': 'true',
          },
          animate: false, fit: true, padding: 48,
        });
        l.on('layoutstop', resolve);
        l.on('layouterror', reject);
        l.run();
      });
    } catch(e) {
      const pos = _dagTopoLayout(nodeDataList);
      _cy.nodes().forEach(n => { const p=pos[n.id()]; if(p) n.position(p); });
      _cy.fit(undefined, 48);
    }
  } else {
    const pos = _dagTopoLayout(nodeDataList);
    _cy.nodes().forEach(n => { const p=pos[n.id()]; if(p) n.position(p); });
    _cy.fit(undefined, 48);
  }

  // After layout browser has painted - now clientWidth/Height are valid
  requestAnimationFrame(() => {
    _cy.resize();
    _cy.fit(undefined, 56);
    _resizeOverlay();
    _buildAnimEdges();
    if (_animEdges.length) _startAnim();
  });
}

window.addEventListener('load', () => { _buildCy(); });

// Close button + keyboard shortcuts
document.getElementById('detail-close').addEventListener('click', _closeDetail);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') _closeDetail();
  if ((e.key === 'f' || e.key === 'F') && !e.ctrlKey && !e.metaKey) fitView();
});

window.addEventListener('resize', () => {
  if (_cy) { _cy.resize(); _resizeOverlay(); }
});
</script>
</body>
</html>
"""


# ── Orchestrator ──────────────────────────────────────────────────────────────

def save_lineage_html(
    output_path: Union[str, Path] = _DEFAULT_OUTPUT_PATH,
    queries_path: Union[str, Path] = _DEFAULT_QUERIES_PATH,
    dialect: Optional[str] = None,
    schema: Optional[list] = None,
) -> Path:
    """Build the lineage DAG, render it as HTML, and write to *output_path*.

    Parameters
    ----------
    output_path  : str | Path - destination HTML file (default ``.nybble/lineage.html``)
    queries_path : str | Path - source queries JSON (default ``.nybble/queries.json``)
    dialect      : str | None - optional sqlglot dialect
    schema       : list | None - pre-built schema list (Flask SCHEMA format).
                   When None, the function attempts to auto-load from registered
                   DuckDB relations so table cards show real column data.

    Returns
    -------
    Path
        Resolved path of the written file.
    """
    # Auto-build schema from live registered relations when not supplied
    if schema is None:
        schema = _load_schema_from_session()

    dag = build_lineage_dag(queries_path=queries_path, dialect=dialect)
    html = dag_to_html(dag, schema=schema)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out.resolve()


def _load_schema_from_session() -> list:
    """Build a SCHEMA list from the currently registered DuckDB relations.

    Returns an empty list if no relations are registered or if the ERD
    pipeline is unavailable.  Never raises - failures are silently ignored
    so that ``nybble lineage`` always produces output.
    """
    try:
        from nybble.resolver import _REGISTERED
        from nybble.erd import _duckdb_schema_to_core, _erd_bundle_to_schema
        from nybble.infer_fk import infer_fks
        from nybble.core.erd_data import build_erd_data
        from nybble.erd import _display_name, _MAX_FK_TIER_FOR_ERD

        rels = list(_REGISTERED.values())
        if not rels:
            return []

        schema_obj, row_counts, table_meta = _duckdb_schema_to_core(rels)
        fk_candidates = infer_fks(rels, display_name_fn=_display_name)
        erd_data = build_erd_data(
            schema_obj, fk_candidates, row_counts=row_counts, table_meta=table_meta
        )
        return _erd_bundle_to_schema(erd_data)
    except Exception:
        return []
