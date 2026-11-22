"""server/kg/kg_schema.py - Property Graph Schema declarations (P2-A, P5-A, P5-D).

Provides KGSchema, NodeTypeDecl, EdgeTypeDecl, and PropDecl dataclasses
for typed property-graph schema validation, aligned with the OmniGraph
.pg schema language.

P5-A additions:
  derive_schema_from_graph(graph) -> KGSchema
  export_graph_to_pg(graph, output_path) -> str

P5-D addition:
  KGSchema.to_owl() - OWL/SKOS JSON-LD export (requires [rdf] extra)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PropDecl:
    name: str
    scalar_type: str      # "String" | "Int" | "Float" | "Bool" | "Date" | "Vector(n)"
    nullable: bool = True
    is_key: bool = False  # equivalent to @key


@dataclass
class NodeTypeDecl:
    name: str             # e.g. "TABLE", "PERSON", "ORG"
    properties: List[PropDecl] = field(default_factory=list)
    color: str = "#586069"


@dataclass
class EdgeTypeDecl:
    name: str             # e.g. "FK", "cross_db", "SUPPORTS"
    from_type: str
    to_type: str
    properties: List[PropDecl] = field(default_factory=list)
    directed: bool = True


@dataclass
class KGSchema:
    """Property graph schema registry for node and edge type declarations.

    # MARKER:kg-schema-typed-nodes
    """
    node_types: Dict[str, NodeTypeDecl] = field(default_factory=dict)
    edge_types: Dict[str, EdgeTypeDecl] = field(default_factory=dict)

    def validate_node(self, entity_type: str, props: dict) -> List[str]:
        """Return list of validation errors, empty = valid."""
        errors = []
        if entity_type not in self.node_types:
            errors.append(
                f"Unknown node type: {entity_type!r}. Known types: {list(self.node_types)}"
            )
            return errors
        decl = self.node_types[entity_type]
        for p in decl.properties:
            if not p.nullable and p.name not in props:
                errors.append(
                    f"Required property {p.name!r} missing for node type {entity_type!r}"
                )
        return errors

    def to_pg_schema(self) -> str:
        """Serialise declared node/edge types to OmniGraph .pg schema format.
        # MARKER:kg-pg-schema-export

        Example output:
            node TABLE {
              name: String @key
              source_key: String?
            }

            edge FK: TABLE -> TABLE
        """
        lines = []
        for nt in self.node_types.values():
            if nt.properties:
                prop_lines = []
                for p in nt.properties:
                    type_str = p.scalar_type
                    if p.nullable:
                        type_str += "?"
                    key_str = " @key" if p.is_key else ""
                    prop_lines.append(f"  {p.name}: {type_str}{key_str}")
                props_block = "\n".join(prop_lines)
                lines.append(f"node {nt.name} {{\n{props_block}\n}}")
            else:
                lines.append(f"node {nt.name} {{}}")
        for et in self.edge_types.values():
            lines.append(f"edge {et.name}: {et.from_type} -> {et.to_type}")
        return "\n\n".join(lines)

    def validate_edge(self, relation: str, src_type: str, tgt_type: str) -> List[str]:
        """Return list of validation errors, empty = valid."""
        # relation may be "FK:table→ref" - check prefix
        edge_kind = relation.split(":")[0] if ":" in relation else relation
        errors = []
        if edge_kind not in self.edge_types:
            # Unknown edge kinds are warnings, not errors (open schema)
            return errors
        decl = self.edge_types[edge_kind]
        if decl.from_type and decl.from_type != src_type and src_type != "UNKNOWN":
            errors.append(
                f"Edge {edge_kind!r} expects from_type={decl.from_type!r}, got {src_type!r}"
            )
        if decl.to_type and decl.to_type != tgt_type and tgt_type != "UNKNOWN":
            errors.append(
                f"Edge {edge_kind!r} expects to_type={decl.to_type!r}, got {tgt_type!r}"
            )
        return errors

    def to_owl(self, base_uri: str = "https://github.com/datajoins/nybble/kg#") -> dict:
        """Serialise the schema as an OWL/SKOS vocabulary (JSON-LD format).

        Requires the [rdf] optional extra: pip install "datajoins-nybble[rdf]"
        # MARKER:kg-owl-skos-export

        Returns a JSON-LD dict representing:
        - Node types as owl:Class
        - Edge types as owl:ObjectProperty
        - Key properties as owl:FunctionalProperty
        """
        try:
            import rdflib  # type: ignore[import]
            from rdflib import Graph as RDFGraph, URIRef, Literal, Namespace
            from rdflib.namespace import OWL, RDF, RDFS, SKOS
        except ImportError as exc:
            raise ImportError(
                "OWL/SKOS export requires rdflib: "
                'pip install "datajoins-nybble[rdf]"'
            ) from exc

        g = RDFGraph()
        KG = Namespace(base_uri)
        g.bind("kg", KG)
        g.bind("owl", OWL)
        g.bind("skos", SKOS)

        # Declare ontology
        ontology_uri = URIRef(base_uri.rstrip("#/"))
        g.add((ontology_uri, RDF.type, OWL.Ontology))
        g.add((ontology_uri, RDFS.label, Literal("Nybble Knowledge Graph Ontology")))

        # Node types → owl:Class
        for nt in self.node_types.values():
            cls_uri = KG[nt.name]
            g.add((cls_uri, RDF.type, OWL.Class))
            g.add((cls_uri, RDFS.label, Literal(nt.name)))
            g.add((cls_uri, SKOS.prefLabel, Literal(nt.name)))

            for p in nt.properties:
                prop_uri = KG[f"{nt.name}_{p.name}"]
                if p.is_key:
                    g.add((prop_uri, RDF.type, OWL.FunctionalProperty))
                    g.add((prop_uri, RDF.type, OWL.DatatypeProperty))
                else:
                    g.add((prop_uri, RDF.type, OWL.DatatypeProperty))
                g.add((prop_uri, RDFS.domain, cls_uri))
                g.add((prop_uri, RDFS.label, Literal(p.name)))

        # Edge types → owl:ObjectProperty
        for et in self.edge_types.values():
            prop_uri = KG[et.name]
            g.add((prop_uri, RDF.type, OWL.ObjectProperty))
            g.add((prop_uri, RDFS.label, Literal(et.name)))
            if et.from_type in self.node_types:
                g.add((prop_uri, RDFS.domain, KG[et.from_type]))
            if et.to_type in self.node_types:
                g.add((prop_uri, RDFS.range, KG[et.to_type]))
            if not et.directed:
                g.add((prop_uri, RDF.type, OWL.SymmetricProperty))

        return {
            "@context": {"kg": base_uri, "owl": str(OWL), "skos": str(SKOS)},
            "@graph": g.serialize(format="json-ld"),
        }


# ── P5-A: Schema derivation and .pg export helpers ────────────────────────────

def derive_schema_from_graph(graph: "object") -> KGSchema:
    """Derive a KGSchema by introspecting an EntityGraph's node/edge types.
    # MARKER:kg-pg-schema-derive

    This enables exporting a Nybble-extracted graph to OmniGraph .pg format
    without manually declaring a schema.
    """
    from collections import defaultdict  # noqa: F401 - kept for clarity

    schema = KGSchema()

    if getattr(graph, "_G", None) is not None:
        G = graph._G

        # Collect node types
        for _nid, attrs in G.nodes(data=True):
            etype = attrs.get("entity_type", "UNKNOWN")
            if etype not in schema.node_types:
                color = attrs.get("color", "#586069")
                schema.node_types[etype] = NodeTypeDecl(
                    name=etype,
                    properties=[
                        PropDecl("label", "String", nullable=True),
                        PropDecl("source_key", "String", nullable=True),
                    ],
                    color=color,
                )

        # Collect edge types
        for src, tgt, attrs in G.edges(data=True):
            relation = attrs.get("relation", "related")
            edge_kind = relation.split(":")[0] if ":" in relation else relation
            if edge_kind not in schema.edge_types:
                src_type = G.nodes[src].get("entity_type", "UNKNOWN")
                tgt_type = G.nodes[tgt].get("entity_type", "UNKNOWN")
                schema.edge_types[edge_kind] = EdgeTypeDecl(
                    name=edge_kind,
                    from_type=src_type,
                    to_type=tgt_type,
                )

    return schema


def export_graph_to_pg(graph: "object", output_path: Optional[str] = None) -> str:
    """Export an EntityGraph to OmniGraph .pg schema format.
    # MARKER:kg-export-to-pg

    Args:
        graph: An EntityGraph instance
        output_path: If provided, write the .pg schema to this file path

    Returns:
        The .pg schema string
    """
    schema = derive_schema_from_graph(graph)
    pg_str = schema.to_pg_schema()
    if output_path is not None:
        from pathlib import Path
        Path(output_path).write_text(pg_str, encoding="utf-8")
    return pg_str
