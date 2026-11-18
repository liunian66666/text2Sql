"""Bidirectional (Table-first / Column-first) schema recall.

This module produces an *initial candidate context* for the upstream
DeepEye-SQL Schema Linking stage; it does NOT re-implement the upstream
three-way schema linking, value retrieval or SQL generation.

Trust rule: only a ``trusted`` metric may force its dependent fields into
``required_columns``. Unreviewed metrics remain recallable candidates and can
soft-boost their tables/columns, but never determine a business formula or a
hard SQL constraint.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..metric_registry import MetricRegistry
from ..models import DatabaseSchema
from .hybrid import HybridIndex, HybridResult, KnowledgeDocument, RetrievalFilters, keyword_score


class RetrievalResult(BaseModel):
    question: str
    database_id: str
    selected_metrics: list[dict[str, Any]] = Field(default_factory=list)
    candidate_tables: list[str] = Field(default_factory=list)
    candidate_columns: list[str] = Field(default_factory=list)
    required_columns: list[str] = Field(default_factory=list)
    table_first_tables: list[str] = Field(default_factory=list)
    column_first_tables: list[str] = Field(default_factory=list)
    metric_expansion: dict[str, Any] = Field(default_factory=dict)
    retrieval_trace: dict[str, Any] = Field(default_factory=dict)
    hybrid: HybridResult | None = None


def _short_column(database_id: str, node_id: str) -> str:
    prefix = f"{database_id}."
    return node_id[len(prefix):] if node_id.startswith(prefix) else node_id


class SchemaRetriever:
    def __init__(self, documents: list[KnowledgeDocument], index: HybridIndex,
                 registry: MetricRegistry, schemas: dict[str, DatabaseSchema]):
        self.documents = documents
        self.index = index
        self.registry = registry
        self.schemas = schemas
        self.by_id = {doc.doc_id: doc for doc in documents}

    def table_first(self, question: str, filters: RetrievalFilters, *, top_tables: int = 8,
                    mode: str = "keyword", fusion: str = "weighted") -> tuple[list[str], list[str], HybridResult]:
        table_filters = filters.model_copy(update={"knowledge_types": ["table"]})
        hybrid = self.index.search(question, table_filters, mode=mode, fusion=fusion,
                                   limit=max(top_tables * 3, 24))
        tables: list[str] = []
        for hit in hybrid.hits:
            doc = self.by_id.get(hit.doc_id)
            if doc and doc.table_name and doc.table_name not in tables:
                tables.append(doc.table_name)
            if len(tables) >= top_tables:
                break
        # Re-rank columns *inside* the recalled tables only.
        column_filters = filters.model_copy(update={"knowledge_types": ["column"]})
        columns: list[tuple[float, str]] = []
        for doc in self.documents:
            if not column_filters.accepts(doc) or doc.table_name not in tables:
                continue
            score, _ = keyword_score(question, doc)
            if score > 0:
                columns.append((score, f"{doc.table_name}.{doc.column_name}"))
        columns.sort(key=lambda row: (-row[0], row[1]))
        return tables, [name for _, name in columns[:40]], hybrid

    def column_first(self, question: str, filters: RetrievalFilters, *, top_columns: int = 30,
                     mode: str = "keyword", fusion: str = "weighted"
                     ) -> tuple[list[str], list[str], HybridResult]:
        column_filters = filters.model_copy(update={"knowledge_types": ["column"]})
        hybrid = self.index.search(question, column_filters, mode=mode, fusion=fusion,
                                   limit=max(top_columns * 2, 60))
        columns: list[str] = []
        tables: list[str] = []
        for hit in hybrid.hits:
            doc = self.by_id.get(hit.doc_id)
            if not doc or not doc.column_name:
                continue
            name = f"{doc.table_name}.{doc.column_name}"
            if name not in columns:
                columns.append(name)
            if doc.table_name and doc.table_name not in tables:
                tables.append(doc.table_name)
            if len(columns) >= top_columns:
                break
        return tables, columns, hybrid

    def retrieve(self, question: str, *, database_id: str, mode: str = "keyword",
                 fusion: str = "weighted", top_k: int = 12, metric_expansion: bool = False,
                 domains: list[str] | None = None, version: str | None = None,
                 max_tables: int = 10, max_columns: int = 50) -> RetrievalResult:
        filters = RetrievalFilters(database_id=database_id,
                                   domains=list(domains or []), version=version)
        tf_tables, tf_columns, tf_hybrid = self.table_first(
            question, filters, mode=mode, fusion=fusion, top_tables=max_tables)
        cf_tables, cf_columns, cf_hybrid = self.column_first(
            question, filters, mode=mode, fusion=fusion, top_columns=max_columns)

        # Metric channel, isolated from the schema channels.
        metric_filters = filters.model_copy(update={"knowledge_types": ["metric"]})
        metric_hybrid = self.index.search(question, metric_filters, mode=mode,
                                          fusion=fusion, limit=top_k)
        selected_metrics: list[dict[str, Any]] = []
        for hit in metric_hybrid.hits[:top_k]:
            doc = self.by_id.get(hit.doc_id)
            if not doc:
                continue
            metric = self.registry.get(doc.node_id)
            if metric is None or metric.database_id != database_id:
                continue
            selected_metrics.append({
                "metric_id": metric.metric_id, "name": metric.name,
                "trusted": metric.trusted, "review_status": doc.review_status,
                "grain": metric.grain, "time_field": metric.time_field,
                "source_tables": list(metric.source_tables),
                "source_columns": list(metric.source_columns),
                "expression_kind": metric.expression_kind,
                "score": hit.score,
                "source_ref": doc.source_ref,
            })

        # Fusion of the two schema paths. Table-first has a small prior because
        # it respects the business entity, while column-first recovers fields
        # whose table name never appears in the question.
        table_scores: dict[str, float] = {}
        for rank, name in enumerate(tf_tables, start=1):
            table_scores[name] = table_scores.get(name, 0.0) + 1.2 / (1 + rank)
        for rank, name in enumerate(cf_tables, start=1):
            table_scores[name] = table_scores.get(name, 0.0) + 1.0 / (1 + rank)

        # Metrics soft-boost their own tables; trusted metrics also force fields.
        required_columns: list[str] = []
        expansion_columns: list[str] = []
        trusted_count = 0
        # Only the strongest metric hits soft-boost tables. Boosting all of them
        # floods the context with unrelated business domains.
        for metric in selected_metrics[:3]:
            for table in metric["source_tables"]:
                table_scores[table] = table_scores.get(table, 0.0) + 0.6
        for metric in selected_metrics:
            if metric_expansion:
                for reference in metric["source_columns"]:
                    if reference not in expansion_columns:
                        expansion_columns.append(reference)
                if metric["trusted"]:
                    trusted_count += 1
                    for reference in metric["source_columns"]:
                        if reference not in required_columns:
                            required_columns.append(reference)

        ordered_tables = [name for name, _ in sorted(
            table_scores.items(), key=lambda row: (-row[1], row[0]))][:max_tables]
        # Metric-dependency columns are the reason configuration C exists:
        # they must survive the column budget, so they are placed first.
        ordered_columns: list[str] = []
        for name in [*required_columns, *expansion_columns, *tf_columns, *cf_columns]:
            if name not in ordered_columns:
                ordered_columns.append(name)
        ordered_columns = ordered_columns[:max_columns]

        return RetrievalResult(
            question=question, database_id=database_id,
            selected_metrics=selected_metrics,
            candidate_tables=ordered_tables, candidate_columns=ordered_columns,
            required_columns=required_columns,
            table_first_tables=tf_tables, column_first_tables=cf_tables,
            metric_expansion={
                "enabled": metric_expansion,
                "candidate_metric_count": len(selected_metrics),
                "trusted_metric_count": trusted_count,
                "expanded_columns": expansion_columns if metric_expansion else [],
                "forced_columns": required_columns,
                "note": ("未审核指标只作为召回候选，不强制确定业务公式；"
                         "trusted = reviewed AND no unknown_fields。"),
            },
            retrieval_trace={
                "mode_requested": mode, "mode_used": metric_hybrid.mode_used,
                "fusion": fusion,
                "backend_label": metric_hybrid.backend_label,
                "fallback_reason": metric_hybrid.fallback_reason,
                "table_first": {"tables": tf_tables, "columns": tf_columns[:20],
                                "hits": tf_hybrid.keyword_hit_count,
                                "elapsed_seconds": tf_hybrid.elapsed_seconds},
                "column_first": {"tables": cf_tables, "columns": cf_columns[:20],
                                 "hits": cf_hybrid.keyword_hit_count,
                                 "elapsed_seconds": cf_hybrid.elapsed_seconds},
                "metric_channel": {"hits": metric_hybrid.keyword_hit_count,
                                   "elapsed_seconds": metric_hybrid.elapsed_seconds},
                "filters": {"database_id": database_id, "domains": list(domains or []),
                            "version": version},
            },
            hybrid=metric_hybrid,
        )
