# Known Issues

This file tracks confirmed bugs and gaps that are not yet fixed.
Each entry includes a workaround where one exists.
Fixed issues are listed in the [Fixed](#fixed) section below and in [CHANGELOG.md](CHANGELOG.md).

---

## Open

### #4 — `nybble.yaml` sources don't appear in the serve UI

**Symptom:** `nybble serve` startup banner lists all configured sources as loaded,
but the UI shows "No data sources yet".

**Root cause:** The CLI (`nybble.resolver`) and the serve UI (`server/app/api_server.py`)
maintain separate source lists.  The CLI loads into an in-process DuckDB session;
the UI reads from `registry.json`.  `_load_sources_for_serve()` populates the DuckDB
session but never writes to `registry.json`.

**Fix required:** `_load_sources_for_serve()` in `nybble/serve.py` must write an entry
to the serve registry for each loaded source, or `api_server.py` must read from the
DuckDB session directly.

**Workaround:** Upload sources through the web UI after starting `nybble serve`.

---

### #6 — Browse page JS `Cannot access '_triggersOpen' before initialization`

**Symptom:** Opening the Browse view logs a JS error in the browser console.
The first table never auto-loads; you have to click it manually.

**Root cause:** A `let` variable `_triggersOpen` is referenced before its `let`
declaration in the same block in `server/app/templates/browse.html`.

**Fix required:** Move the `let _triggersOpen` declaration above its first use,
or convert to `var`.

**Workaround:** Click the first table name in the sidebar to load it.

---

### #7 — `server/` top-level package name can clash

**Symptom:** Any other installed package that registers a top-level `server` module
will conflict with nybble's `server/` package.

**Root cause:** `server/` is packaged and installed as a top-level Python package
named `server`.  This is an unusually generic name.

**Fix required:** Rename the entire `server/` tree to `nybble_server/` and update
all `from server.xxx import` statements (~20 occurrences in `api_server.py`).
`pyproject.toml` package-data keys must also be updated.

**Workaround:** None — avoid installing packages that also expose a top-level
`server` module in the same environment.

---

### #10 — Docker Hub image `datajoins/nybble` not yet published

**Symptom:** `docker pull datajoins/nybble` returns 404.  The README and
`find_server_dir()` error message no longer reference this image (fixed in v1.0.8),
but the Docker Compose deployment in `deploy/` still requires a manually built image.

**Fix required:** Build and push the Docker image to Docker Hub.

---

### #11 — Web UI pages require an internet connection (CDN loads)

**Symptom:** ERD Studio, Knowledge Graph, Browse, and Query pages load JavaScript
from public CDNs (ELK / cytoscape-elk, Chart.js, KaTeX, marked, sql-formatter).
They will not render correctly without an internet connection.

**What is already bundled** (`server/app/static/js/`): Cytoscape.js, dagre,
cytoscape-dagre.  Switching the HTML templates to those local copies costs nothing
but has not been done yet.

**What still needs bundling:** ELK / cytoscape-elk, Chart.js, KaTeX, marked,
sql-formatter.  The CLI ERD template (`nybble/templates/erd.html.jinja`) also loads
Cytoscape from a CDN.

**Fix required:** Point `kg.html`, `browse.html`, and `query.html` templates at the
already-bundled copies of Cytoscape/dagre/cytoscape-dagre.  Bundle or vendor the
remaining CDN libraries, or at minimum document the requirement clearly (done in
README v1.0.10).

---

### #13b — BigQuery via the web UI connector needs `sqlalchemy-bigquery`

**Symptom:** The BigQuery tab in the web UI Connect dialog fails because
`sqlalchemy-bigquery` is not installed by any nybble extra.

**Fix required:** Add a `bigquery` optional extra:
```toml
bigquery = ["sqlalchemy-bigquery"]
```
and document it.

*(The command-line `bigquery://` loader stub is a separate, lower-priority issue.)*

---

### #14 — Column types differ between CLI (`nybble profile`) and serve UI

**Symptom:** A numeric string column shows as `VARCHAR` in `nybble profile` but as
`REAL` or `INTEGER` in the serve UI's Browse/ERD view.

**Root cause:** The CLI uses DuckDB's native type inference; the UI pipeline uses a
custom SQLite-oriented extractor (`server/ingestion/`) with separate heuristics.

**Fix required:** Normalise column types through a shared function, or have the CLI
extractor and the UI pipeline call the same type-inference code.

---

### #16a — FK inference produces wrong links (Tier 4 / value-statistics tier)

**Symptom:** The ERD and KG infer relationships that don't exist (e.g.
`orders.product_id → categories.id` with `tier: 4, confidence: 0.5`) and miss
obvious ones (e.g. `orders.customer_id → customers.id`).

**Root cause (corrected):** The bad links come from **Tier 4** (value-statistics
matching), not from suffix matching.  The original description of "skip columns
< 4 chars" as a fix was incorrect — it would permanently hide short FK column names
(single letters, two-character abbreviations) that are perfectly valid FK targets.

The issue affects **both** FK inference paths:
- `nybble/infer_fk.py` — used by the CLI, MCP, and Python API
- `server/ingestion/infer_fk.py` — used by the serve UI upload pipeline

**Fix required:**
- Apply a name-length guard **only to the name-based tiers (2–3)**, not to Tier 4.
- For Tier 4, require *directional containment* (FK column values ⊆ values of a
  unique/PK column in the referenced table) and raise the confidence threshold
  (e.g. ≥ 0.85 instead of 0.5).
- Fix both `nybble/infer_fk.py` and `server/ingestion/infer_fk.py`.

---

### #16b — No way to declare relationships in `nybble.yaml`

**Symptom:** Users cannot override or supplement the auto-inferred FK relationships
via config.

**Fix required:** Add a `relationships:` block to `WorkspaceConfig` / `nybble.yaml`
schema, e.g.:

```yaml
relationships:
  - from: orders.customer_id
    to:   customers.id
    label: placed_by
```

This would feed directly into the ERD and KG builders, bypassing heuristic inference
for declared pairs.

---

## Deferred (not planned for near term)

| # | Issue | Reason deferred |
|---|---|---|
| #13a | CLI `bigquery://` loader stub | Low usage; needs GCP credentials in CI |

---

## Fixed

| # | Issue | Fixed in |
|---|---|---|
| #9 | Smoke test fixture path wrong in README | v1.0.9 (README rewrite removed stale path) |
| #10 (partial) | Docker reference in `find_server_dir()` error and README | v1.0.8 |
| #15 | `nybble share --yes` / `-y` missing for CI/scripts | v1.0.8 |
| #17 | Flask-missing error strips `[serve]` via Rich markup | v1.0.8 |
| #18 | KG store-open failures silent; poll loop runs forever | v1.0.8 (core fix), v1.0.9 (stale sentinel edge case) |
| #19 | Index Sources button never re-disables | v1.0.8 |
| #20 | `nybble.share()` Python API uploads without warning | v1.0.9 |
| #21 | `share()` warning named wrong destination when `NYBBLE_SHARE_BACKEND` env var used | v1.0.10 |

---

*To fix an issue: open a PR that addresses the root cause described above, adds a
test, and moves the entry to this Fixed table and to CHANGELOG.md under the relevant version.*
