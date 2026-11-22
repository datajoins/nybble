# Known Issues

This file tracks confirmed bugs and gaps that are not yet fixed.
Each entry includes a workaround where one exists.
Fixed issues are moved to [CHANGELOG.md](CHANGELOG.md).

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

### #9 — Smoke test fixture path wrong in README

**Symptom:** The README "30-Second Smoke Test" references `.nybble-test-fixtures/`
but the fixtures actually live in `tests/fixtures/` and are not included in the
PyPI wheel.

**Fix required:**
1. Update the README smoke test to use a URL-based source (e.g. a public CSV) that
   works without cloning the repo, or bundle a small fixture file inside `nybble/`.
2. Remove the stale `.nybble-test-fixtures/` reference.

---

### #10 — Docker Hub image `datajoins/nybble` returns 404

**Symptom:** `docker pull datajoins/nybble` and the README's Docker instructions
fail because the image has not been pushed to Docker Hub.

**Fix required:** Build and push the Docker image, or remove the Docker instructions
from the README until the image is available.

---

### #11 — README "no CDN dependencies" claim is incorrect

**Symptom:** README states the ERD export is "no CDN dependencies, no server
required to view it" but the ERD viewer, KG page, Browse, and Query pages load
Cytoscape.js, ELK, dagre, Chart.js, and CodeMirror from public CDNs.  They do not
render offline.

**Fix required:** Either:
- Bundle the JS assets inside the wheel (increases wheel size by ~2 MB), or
- Change the README wording to "no server required; internet connection needed for
  the interactive viewer".

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

### #16a — FK inference produces wrong links

**Symptom:** The ERD and KG infer relationships that don't exist (e.g. `story_id →
size`) and miss obvious ones (e.g. `s/t → story_id`).

**Root cause:** The 4-tier heuristic in `server/ingestion/infer_fk.py` matches on
column-name suffixes without enough context.  Short column names (single letters,
abbreviations) produce false positives.

**Fix required:** Add a minimum-name-length guard (e.g. skip columns whose name is
< 4 characters) and raise the statistics-tier confidence threshold.

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
| Column inference unification | #14 above | Large refactor; no breaking user impact in common cases |

---

*To fix an issue: open a PR that addresses the root cause described above, adds a
test, and moves the entry to CHANGELOG.md under the relevant version.*
