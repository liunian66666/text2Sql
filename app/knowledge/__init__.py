"""Structured enterprise knowledge layer (Phase 1).

This package turns the metric rules that used to be concatenated into a prompt
(``app/enterprise/knowledge.py`` -> ``SQLSkillRequest.evidence``) into
retrievable, linkable, verifiable and versioned knowledge objects.

Phase 1 scope: models, metric registry, SQLite schema extraction, explicit
metric/schema dependencies and a reproducible ``build-knowledge`` CLI.
No runtime Agent, SQL generation or SSE behaviour is changed here.
"""
from __future__ import annotations

from .models import (
    UNKNOWN,
    METRIC_VERSION,
    SCHEMA_VERSION,
    AggregationRisk,
    Cardinality,
    DatabaseSchema,
    EdgeType,
    EvidenceStatus,
    KnowledgeEdge,
    KnowledgeSnapshot,
    MetricDefinition,
    RelationOrigin,
    SchemaColumn,
    SchemaRelation,
    SchemaTable,
    UniqueConstraint,
    ValidationIssue,
    canonical_json,
    content_hash,
    stable_column_id,
    stable_relation_id,
    stable_table_id,
)

__all__ = [
    "UNKNOWN",
    "METRIC_VERSION",
    "SCHEMA_VERSION",
    "AggregationRisk",
    "Cardinality",
    "DatabaseSchema",
    "EdgeType",
    "EvidenceStatus",
    "KnowledgeEdge",
    "KnowledgeSnapshot",
    "MetricDefinition",
    "RelationOrigin",
    "SchemaColumn",
    "SchemaRelation",
    "SchemaTable",
    "UniqueConstraint",
    "ValidationIssue",
    "canonical_json",
    "content_hash",
    "stable_column_id",
    "stable_relation_id",
    "stable_table_id",
]
