"""
nybble.core - Shared internal library.

Contains the canonical implementations of:
  schema   - Schema/Table/Column/ForeignKey/Index dataclasses
  infer_fk - Unified 4-tier FK inference engine (schema-model-based)
  erd_data - ERD data bundle builder (schema-model-driven)
  kg_builder - KG graph builder (pure networkx, no DuckDB/SQLite)

Both the CLI (nybble/) and the Flask server (server/) import from here.
No Flask, DuckDB, SQLite, click, or argparse imports are permitted in this package.
"""
