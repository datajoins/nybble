"""
nybble - Ultra-lightweight data engine.

Public API
----------
  load(source, name=None)              → Relation
  browse(source, page_size)
  query(sql, limit=None)               → QueryResult
  explain(sql, analyze=False)          → str
  search_history(term, limit)          → list[dict]
  save_query(name, sql)
  run_query(name, limit=None)          → QueryResult
  list_saved_queries()                 → list[dict]
  erd(relations, output, open_browser) → Path
  kg(relations, output_dir, open_browser, export_pgq) → KGResult
  kg_summary(relations)                → dict
  search_nodes(query, graph, top_k)    → list[NodeSearchResult]
  generate_pgq_ddl(G, fk_candidates)  → str
  schema_diff(source_a, source_b)      → SchemaDiff
  profile_relation(name)               → list[ColumnProfile]
  build_report(source, *, ...) → NybbleReport (or str for legacy list arg)
  save_report(path, relations, ...)    → Path  (legacy)
  share(source, *, backend, output, ...) → NybbleShareResult
  build_lineage_dag(queries_path, dialect) → dict
  dag_to_html(dag, title)              → str
  save_lineage_html(output_path, ...)  → Path
  load_config(path)                    → WorkspaceConfig
  apply_config(config)                 → list[Relation]
  CONNECTOR_REGISTRY                   → dict  (tier/install/scheme metadata)
"""
from __future__ import annotations

from nybble.resolver import (
    load,
    list_relations,
    drop,
    session,
    Relation,
    CONNECTOR_REGISTRY,
)
from nybble.browse import browse
from nybble.arena import (
    query,
    explain,
    search_history,
    save_query,
    run_query,
    list_saved_queries,
    delete_saved_query,
    repl,
    QueryResult,
)
from nybble.erd import erd
from nybble.kg import (
    kg, kg_summary, KGResult, search_nodes, NodeSearchResult, generate_pgq_ddl,
    frd_scores, frd_expand, node_detail, kg_graph, kg_validate,
)
from nybble.infer_fk import infer_fks, FKCandidate
from nybble.config import load_config, apply_config, WorkspaceConfig
from nybble.diff import schema_diff, SchemaDiff
from nybble.profiler import profile_relation, ColumnProfile
from nybble.report import build_report, save_report, NybbleReport, ReportSection
from nybble.share import share, NybbleShareResult, register_token, resolve_token, get_token_registry
from nybble.lineage import build_lineage_dag, dag_to_html, save_lineage_html

__version__ = "1.0.2"
__author__ = "DataJoins"
__license__ = "Apache-2.0"

__all__ = [
    # Core ingestion
    "load",
    "list_relations",
    "drop",
    "session",
    "Relation",
    # Browse
    "browse",
    # SQL Arena
    "query",
    "explain",
    "search_history",
    "save_query",
    "run_query",
    "list_saved_queries",
    "delete_saved_query",
    "repl",
    "QueryResult",
    # ERD
    "erd",
    # Knowledge Graph
    "kg",
    "kg_summary",
    "KGResult",
    "search_nodes",
    "NodeSearchResult",
    "generate_pgq_ddl",
    "frd_scores",
    "frd_expand",
    "node_detail",
    "kg_graph",
    "kg_validate",
    # FK inference (exposed for advanced use)
    "infer_fks",
    "FKCandidate",
    # Schema diff
    "schema_diff",
    "SchemaDiff",
    # Column profiling
    "profile_relation",
    "ColumnProfile",
    # Report builder
    "build_report",
    "save_report",
    "NybbleReport",
    "ReportSection",
    # Workspace config
    "load_config",
    "apply_config",
    "WorkspaceConfig",
    # Share / collaborative exports
    "share",
    "NybbleShareResult",
    "register_token",
    "resolve_token",
    "get_token_registry",
    # Lineage DAG
    "build_lineage_dag",
    "dag_to_html",
    "save_lineage_html",
    # Connector tier registry
    "CONNECTOR_REGISTRY",
]
