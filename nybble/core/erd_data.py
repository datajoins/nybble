"""
nybble.core.erd_data - Shared ERD data bundle builder.

Produces the canonical ERD data dict consumed by both the CLI Jinja2 template
(nybble/templates/erd.html.jinja) and the Flask ERD route
(server/app/api_server.py:_schema_to_erd_data).

Public API
----------
  build_erd_data(schema, fk_candidates, row_counts=None) -> dict
      Returns {"tables": [...], "edges": [...]}.

  erd_data_to_schema_json(erd_data) -> dict
      Converts the ERD data bundle to the Liam keyed-object format used by
      schema.json files on disk, for backward compatibility.

Rules (enforced by the integration plan)
-----------------------------------------
* No Flask, DuckDB, SQLite, click, or argparse imports.
* No HTTP context (request, g, current_app, etc.).
* Callable identically from the CLI and from a Flask route.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from nybble.core.schema import ForeignKey, Schema


# ---------------------------------------------------------------------------
# Type alias - FK candidates are imported lazily to avoid circular imports
# when nybble.core.infer_fk imports nybble.core.schema.
# Callers pass list[FKCandidate]; we only use .src_table/.src_col/.tgt_table/
# .tgt_col/.tier/.confidence, so duck-typing is fine here.
# ---------------------------------------------------------------------------


def build_erd_data(
    schema: Schema,
    fk_candidates: list,
    row_counts: Optional[Dict[str, int]] = None,
    table_meta: Optional[Dict[str, dict]] = None,
) -> dict:
    """Build the JSON structure consumed by both ERD templates.

    Parameters
    ----------
    schema:
        Canonical Schema dataclass (from nybble.core.schema).
    fk_candidates:
        List of FKCandidate objects (tier, confidence, src_table, src_col,
        tgt_table, tgt_col). May be empty.
    row_counts:
        Optional mapping of {table_name: row_count}. When None all row counts
        are reported as 0 rather than making a DB call.
    table_meta:
        Optional mapping of {table_name: {extra_key: value, ...}} merged into
        each table's output dict.  Use this to pass adapter-specific fields
        (e.g. ``source_type`` from the CLI) without polluting the shared schema.

    Returns
    -------
    dict with shape:
        {
          "tables": [
            {
              "id": str, "name": str,
              "row_count": int,
              "columns": [
                {"name": str, "type": str,
                 "is_pk": bool, "is_fk": bool,
                 "fk_ref": {...} | absent}
              ]
              # plus any extra keys from table_meta
            }, ...
          ],
          "edges": [
            {
              "id": str, "source": str, "source_col": str,
              "target": str, "target_col": str,
              "tier": int, "confidence": float, "label": str
            }, ...
          ]
        }
    """
    if row_counts is None:
        row_counts = {}
    if table_meta is None:
        table_meta = {}

    table_names = {t.name for t in schema.tables}

    # Build PK and FK column index from candidates
    # pk_cols[table] = set of column names that are targets of FKs (i.e. PKs)
    pk_cols: Dict[str, set] = {}
    # fk_cols[table][col] = {ref_table, ref_col, tier, confidence}
    fk_col_map: Dict[str, Dict[str, dict]] = {}

    for fk in fk_candidates:
        pk_cols.setdefault(fk.tgt_table, set()).add(fk.tgt_col)
        fk_col_map.setdefault(fk.src_table, {})[fk.src_col] = {
            "ref_table": fk.tgt_table,
            "ref_col": fk.tgt_col,
            "tier": fk.tier,
            "confidence": fk.confidence,
        }

    # Also mark columns declared as primary_key=True in the schema itself
    for table in schema.tables:
        declared_pks = set(table.primary_keys or [])
        declared_pks.update(c.name for c in table.columns if c.primary_key)
        if declared_pks:
            pk_cols.setdefault(table.name, set()).update(declared_pks)
        # Declared FK constraints in schema
        for fk_constraint in table.foreign_keys:
            for src_col, tgt_col in zip(
                fk_constraint.constrained_columns,
                fk_constraint.referred_columns,
            ):
                if src_col not in fk_col_map.get(table.name, {}):
                    fk_col_map.setdefault(table.name, {})[src_col] = {
                        "ref_table": fk_constraint.referred_table,
                        "ref_col": tgt_col,
                        "tier": 1,
                        "confidence": 1.0,
                    }

    tables_out = []
    for table in schema.tables:
        t_pk_cols = pk_cols.get(table.name, set())
        t_fk_cols = fk_col_map.get(table.name, {})

        columns = []
        for col in table.columns:
            col_info: dict = {
                "name": col.name,
                "type": col.data_type,
                "is_pk": col.name in t_pk_cols,
                "is_fk": col.name in t_fk_cols,
            }
            if col.name in t_fk_cols:
                col_info["fk_ref"] = t_fk_cols[col.name]
            columns.append(col_info)

        tbl_dict: dict = {
            "id": table.name,
            "name": table.name,
            "row_count": row_counts.get(table.name, 0),
            "columns": columns,
        }
        # Merge any extra metadata supplied by the adapter layer (e.g. source_type)
        extra = table_meta.get(table.name)
        if extra:
            tbl_dict.update(extra)
        tables_out.append(tbl_dict)

    edges_out = []
    seen_edges: set = set()
    for fk in fk_candidates:
        if fk.src_table not in table_names or fk.tgt_table not in table_names:
            continue
        edge_key = (fk.src_table, fk.src_col, fk.tgt_table, fk.tgt_col)
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)
        edges_out.append({
            "id": f"{fk.src_table}__{fk.src_col}__{fk.tgt_table}__{fk.tgt_col}",
            "source": fk.src_table,
            "source_col": fk.src_col,
            "target": fk.tgt_table,
            "target_col": fk.tgt_col,
            "tier": fk.tier,
            "confidence": fk.confidence,
            "label": f"{fk.src_col} → {fk.tgt_col}",
        })

    return {"tables": tables_out, "edges": edges_out}


def erd_data_to_schema_json(erd_data: dict) -> dict:
    """Convert an ERD data bundle to the Liam keyed-object format.

    The Liam format is:
        {
          "tables": {
            "tableName": {
              "name": str,
              "columns": {"colName": {"name": str, "type": str, ...}},
              "constraints": {
                "pk_tableName": {"type": "PRIMARY KEY", "columnNames": [...]},
                "fk_tableName_col_0": {"type": "FOREIGN KEY", ...}
              }
            }
          },
          "enums": {}
        }

    This format is what schema.json files on disk use and what the Flask
    pipeline writes. It is less rich than the ERD bundle (no row counts, no
    confidence scores) but is required for backward compatibility.
    """
    tables_out: dict = {}

    for table in erd_data.get("tables", []):
        tname = table["name"]
        cols_out: dict = {}
        for col in table.get("columns", []):
            cols_out[col["name"]] = {
                "name": col["name"],
                "type": col["type"],
                "notNull": False,
                "default": None,
            }

        constraints: dict = {}

        # PK constraint from columns marked is_pk
        pk_cols = [c["name"] for c in table.get("columns", []) if c.get("is_pk")]
        if pk_cols:
            constraints[f"pk_{tname}"] = {
                "type": "PRIMARY KEY",
                "columnNames": pk_cols,
            }

        # FK constraints from edges whose source is this table
        fk_idx = 0
        for edge in erd_data.get("edges", []):
            if edge["source"] != tname:
                continue
            key = f"fk_{tname}_{edge['source_col']}_{fk_idx}"
            constraints[key] = {
                "type": "FOREIGN KEY",
                "columnNames": [edge["source_col"]],
                "targetTableName": edge["target"],
                "targetColumnNames": [edge["target_col"]],
            }
            fk_idx += 1

        tables_out[tname] = {
            "name": tname,
            "columns": cols_out,
            "constraints": constraints,
        }

    return {"tables": tables_out, "enums": {}}
