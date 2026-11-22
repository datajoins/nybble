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

**Symptom:** `docker pull datajoins/nybble` fails because the image has not been
pushed to Docker Hub.  The Docker reference was removed from the README in v1.0.8,
but `nybble/serve.py:find_server_dir()` still suggests
`docker run -p 7700:80 datajoins/nybble` in its error message (fixed in v1.0.8 —
the Docker line was removed from that message too).

**Fix required:** Build and push the Docker image to Docker Hub, or leave the Docker
instructions removed until the image is available.

---

### #11 — CDN dependency claim and remaining CDN loads

**Symptom:** The README no longer claims "no CDN dependencies" (fixed), but several
pages still load libraries from public CDNs and will not render offline.

**What is already bundled** (`server/app/static/js/`): Cytoscape.js, dagre,
cytoscape-dagre.  Switching the HTML templates to use those local copies costs
nothing.

**What still needs bundling** (or the offline claim cannot be made):
ELK / cytoscape-elk, Chart.js, KaTeX, marked, sql-formatter.  The CLI ERD template
(`nybble/templates/erd.html.jinja`) also loads Cytoscape from a CDN.

**Fix required:** Point `kg.html`, `browse.html`, and `query.html` templates at the
already-bundled copies of Cytoscape/dagre/cytoscape-dagre.  Bundle or remove the
remaining CDN-loaded libraries, or document the internet requirement clearly.

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

### #15 — `nybble share --yes` / `-y` flag missing (CI/script use)

**Fixed in v1.0.8.**  Tracking entry kept for reference.

> `nybble share` always showed an interactive confirmation prompt with no way to
> skip it in scripts or CI.  Added `--yes` / `-y` flag in v1.0.8.

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

### #17 — Flask-missing error message garbled by Rich markup

**Fixed in v1.0.8.** `nybble/serve.py:get_flask_app()` now uses `\[serve]` so Rich
renders the literal bracket instead of treating it as a markup tag.

---

### #18 — KG indexing: `_open_store_raw()` failures silent; poll loop runs forever

**Fixed in v1.0.8.** The KG index store is SQLite (`search.db`), not KuZu.
`_run()` is now wrapped in `try/except`; store-open failures are captured under
`_index_errors["__store__"]`, which terminates the poll loop and shows a single
error toast.  Each error key is toasted exactly once per reindex run.

**Edge case fixed in v1.0.9:** When a targeted reindex (`keys` list provided) was
started after a previous `__store__` or `__all__` failure, those sentinel keys were
not cleared — only the targeted source keys were popped.  The stale sentinel caused
every subsequent reindex to surface the old error immediately on the first poll and
then terminate, even after the underlying problem (e.g. read-only permissions) was
resolved.  Fix: `__store__` and `__all__` are now always popped unconditionally at
the start of every reindex request, regardless of whether a key list was supplied.

---

### #19 — Index Sources button: `_syncIndexBtn()` never re-disables after first enable

**Fixed in v1.0.8.** Early-return guard removed; `_indexing` flag added.
`btn.disabled = _indexing || getSelectedKeys().length === 0` is evaluated on every
checkbox change, Deselect All, and poll-loop completion.

---

### #20 — `nybble.share()` Python API uploads silently (no confirmation)

**Fixed in v1.0.9.** `nybble.share()` previously uploaded data to a public URL with
no warning.  Only the CLI showed a confirmation prompt.

`share()` now emits a `UserWarning` by default before uploading:

```python
# Shows: UserWarning: nybble.share() will upload your data to a public GitHub Gist.
#        Pass confirm=True to suppress this warning.
result = share("erd.html", backend="gist", gist_token="ghp_...")

# Suppress the warning when you have already obtained consent:
result = share("erd.html", backend="gist", gist_token="ghp_...", confirm=True)
```

The CLI passes `confirm=True` automatically (its interactive prompt is the equivalent
consent gate), so CLI behaviour is unchanged.

---

## Deferred (not planned for near term)

| # | Issue | Reason deferred |
|---|---|---|
| #13a | CLI `bigquery://` loader stub | Low usage; needs GCP credentials in CI |

---

*To fix an issue: open a PR that addresses the root cause described above, adds a
test, and moves the entry to CHANGELOG.md under the relevant version.*
