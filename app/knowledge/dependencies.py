"""Explicit metric <-> schema dependencies (Phase 1, graph-ready but no online graph).

Phase 1 deliberately stops at *recording* structure and risk metadata. It does
not run NetworkX, does not expand subgraphs and does not validate SQL. The
``KnowledgeEdge`` list produced here is the exact input the Phase 3 GraphRAG
retriever will consume.

Relation types reserved for Phase 3:
    Metric DEPENDS_ON Column / Metric USES_TABLE Table / Table HAS_COLUMN Column
    / Column FOREIGN_KEY_TO Column / Metric ALLOWS_DIMENSION Dimension
"""
from __future__ import annotations

from typing import Any

from .metric_registry import MetricRegistry
from .models import (
    UNKNOWN,
    DatabaseSchema,
    EdgeType,
    KnowledgeEdge,
    KnowledgeSnapshot,
    SchemaRelation,
    ValidationIssue,
    stable_column_id,
    stable_edge_id,
)


def _edge(edge_type: EdgeType, source_id: str, target_id: str, database_id: str,
          *, metric_id: str | None = None, verified: bool = False,
          source_ref: str, metadata: dict[str, Any] | None = None) -> KnowledgeEdge:
    return KnowledgeEdge(
        edge_id=stable_edge_id(edge_type.value, source_id, target_id),
        edge_type=edge_type,
        source_id=source_id,
        target_id=target_id,
        database_id=database_id,
        metric_id=metric_id,
        verified=verified,
        source_ref=source_ref,
        metadata=metadata or {},
    )


def build_edges(registry: MetricRegistry, schemas: dict[str, DatabaseSchema],
                issues: list[ValidationIssue] | None = None) -> list[KnowledgeEdge]:
    """Build every deterministic relation from real schema + metric references."""
    edges: dict[str, KnowledgeEdge] = {}
    columns_by_id = {
        column.column_id: column
        for schema in schemas.values()
        for table in schema.tables
        for column in table.columns
    }

    def add(edge: KnowledgeEdge) -> None:
        # Same (type, source, target) is one edge; keep the richest metadata.
        previous = edges.get(edge.edge_id)
        if previous is None:
            edges[edge.edge_id] = edge
            return
        previous.metadata = {**previous.metadata, **edge.metadata}
        previous.verified = previous.verified or edge.verified

    # Schema-internal structure.
    for database_id, schema in schemas.items():
        for table in schema.tables:
            for column in table.columns:
                add(_edge(EdgeType.TABLE_HAS_COLUMN, table.table_id, column.column_id,
                          database_id, verified=True,
                          source_ref=schema.source_ref))
        for relation in schema.relations:
            for source_column, target_column in zip(relation.source_column_ids,
                                                    relation.target_column_ids):
                add(_edge(EdgeType.COLUMN_FOREIGN_KEY_TO_COLUMN,
                          source_column, target_column, database_id,
                          verified=relation.verified,
                          source_ref=relation.source_ref,
                          metadata={
                              "relation_id": relation.relation_id,
                              "cardinality": relation.cardinality.value,
                              "origin": relation.origin.value,
                              "duplicate_aggregation_risk": relation.duplicate_aggregation_risk,
                          }))

    # Metric -> schema. References that do not exist in the real schema are
    # reported by the registry's validate() and are NOT turned into edges.
    for metric in registry.metrics:
        schema = schemas.get(metric.database_id)
        risks_by_table: dict[str, list[str]] = {}
        for risk in metric.aggregation_risks:
            for table_name in risk.affected_tables:
                risks_by_table.setdefault(table_name, []).append(risk.risk_id)

        for table_name in metric.source_tables:
            table_id = f"{metric.database_id}.{table_name}"
            if schema is None or schema.table(table_name) is None:
                continue
            add(_edge(EdgeType.METRIC_USES_TABLE, metric.metric_id, table_id,
                      metric.database_id, metric_id=metric.metric_id,
                      verified=False,  # metric definitions are not human-reviewed yet
                      source_ref=metric.source_ref,
                      metadata={
                          "evidence_status": metric.evidence_status.value,
                          "trusted": metric.trusted,
                          "aggregation_risks": risks_by_table.get(table_name, []),
                      }))
        for reference in metric.source_columns:
            if "." not in reference:
                continue
            # rsplit: "Schema.Table.Column" references contain multiple dots.
            table_name, column_name = reference.rsplit(".", 1)
            column_id = stable_column_id(metric.database_id, table_name, column_name)
            if column_id not in columns_by_id:
                continue
            add(_edge(EdgeType.METRIC_DEPENDS_ON_COLUMN, metric.metric_id, column_id,
                      metric.database_id, metric_id=metric.metric_id,
                      verified=False, source_ref=metric.source_ref))
        for dimension in metric.allowed_dimensions:
            add(_edge(EdgeType.METRIC_ALLOWS_DIMENSION, metric.metric_id, dimension,
                      metric.database_id, metric_id=metric.metric_id,
                      verified=False, source_ref=metric.source_ref))
        for dependency in metric.depends_on_metrics:
            add(_edge(EdgeType.METRIC_DEPENDS_ON_METRIC, metric.metric_id, dependency,
                      metric.database_id, metric_id=metric.metric_id,
                      verified=False, source_ref=metric.source_ref))

    return sorted(edges.values(), key=lambda edge: edge.edge_id)


def build_nodes(registry: MetricRegistry, schemas: dict[str, DatabaseSchema]
                ) -> dict[str, list[dict[str, Any]]]:
    """Metadata-only node catalogue. No table rows, no personal data."""
    metrics = [
        {
            "node_id": metric.metric_id,
            "name": metric.name,
            "database_id": metric.database_id,
            "domain": metric.domain,
            "version": metric.version,
            "reviewed": metric.reviewed,
            "trusted": metric.trusted,
            "evidence_status": metric.evidence_status.value,
            "unknown_fields": list(metric.unknown_fields),
            "expression_kind": metric.expression_kind,
        }
        for metric in registry.metrics
    ]
    tables = []
    columns = []
    for schema in schemas.values():
        for table in schema.tables:
            tables.append({
                "node_id": table.table_id,
                "database_id": table.database_id,
                "table_name": table.table_name,
                "version": table.version,
                "primary_keys": list(table.primary_keys),
            })
            for column in table.columns:
                columns.append({
                    "node_id": column.column_id,
                    "table_id": column.table_id,
                    "column_name": column.column_name,
                    "data_type": column.data_type,
                    "nullable": column.nullable,
                    "primary_key": column.primary_key,
                    "version": column.version,
                })
    dimensions = [
        {"node_id": dimension, "status": "pending_review"}
        for metric in registry.metrics
        for dimension in metric.allowed_dimensions
    ]
    return {
        "metric": sorted(metrics, key=lambda item: item["node_id"]),
        "table": sorted(tables, key=lambda item: item["node_id"]),
        "column": sorted(columns, key=lambda item: item["node_id"]),
        "dimension": sorted(dimensions, key=lambda item: item["node_id"]),
    }


class KnowledgeIndex:
    """Answers the six Phase 1 dependency questions without an online graph."""

    def __init__(self, registry: MetricRegistry, schemas: dict[str, DatabaseSchema],
                 edges: list[KnowledgeEdge] | None = None):
        self.registry = registry
        self.schemas = schemas
        self.edges = edges if edges is not None else build_edges(registry, schemas)

    def tables_for_metric(self, metric_id: str) -> list[str]:
        return sorted({edge.target_id for edge in self.edges
                       if edge.edge_type == EdgeType.METRIC_USES_TABLE
                       and edge.metric_id == metric_id})

    def columns_for_metric(self, metric_id: str) -> list[str]:
        return sorted({edge.target_id for edge in self.edges
                       if edge.edge_type == EdgeType.METRIC_DEPENDS_ON_COLUMN
                       and edge.metric_id == metric_id})

    def grain_for_metric(self, metric_id: str) -> str:
        metric = self.registry.get(metric_id)
        return metric.grain if metric is not None else UNKNOWN

    def dimensions_for_metric(self, metric_id: str) -> list[str]:
        return sorted({edge.target_id for edge in self.edges
                       if edge.edge_type == EdgeType.METRIC_ALLOWS_DIMENSION
                       and edge.metric_id == metric_id})

    def verified_relations_for_metric(self, metric_id: str) -> list[SchemaRelation]:
        """Declared FKs whose both endpoints are tables this metric uses."""
        table_ids = set(self.tables_for_metric(metric_id))
        result: list[SchemaRelation] = []
        for schema in self.schemas.values():
            for relation in schema.relations:
                if (relation.verified
                        and relation.source_table in table_ids
                        and relation.target_table in table_ids):
                    result.append(relation)
        return sorted(result, key=lambda relation: relation.relation_id)

    def metrics_affected_by_column(self, column_id: str) -> list[str]:
        """Impact analysis for renaming/removing a column."""
        return sorted({edge.metric_id for edge in self.edges
                       if edge.edge_type == EdgeType.METRIC_DEPENDS_ON_COLUMN
                       and edge.target_id == column_id
                       and edge.metric_id is not None})

    def aggregation_risks_for_metric(self, metric_id: str) -> list[dict[str, Any]]:
        metric = self.registry.get(metric_id)
        if metric is None:
            return []
        return [risk.model_dump(mode="json") for risk in metric.aggregation_risks]


def build_snapshot(registry: MetricRegistry, schemas: dict[str, DatabaseSchema],
                   issues: list[ValidationIssue], *, built_at: str | None = None
                   ) -> KnowledgeSnapshot:
    edges = build_edges(registry, schemas)
    nodes = build_nodes(registry, schemas)
    return KnowledgeSnapshot(
        database_ids=sorted(schemas),
        schema_fingerprints={db: schema.schema_fingerprint for db, schema in sorted(schemas.items())},
        metric_versions={metric.metric_id: metric.version for metric in registry.metrics},
        built_at=built_at,
        metric_count=len(nodes["metric"]),
        table_count=len(nodes["table"]),
        column_count=len(nodes["column"]),
        relation_count=sum(len(schema.relations) for schema in schemas.values()),
        edge_count=len(edges),
        trusted_metric_count=len(registry.trusted_metrics()),
        pending_review_metric_count=len(registry.pending_review_metrics()),
        nodes=nodes,
        relations=edges,
        validation=issues,
    )
