"""Structured SchemaContext: serializable, budgeted, fully traceable.

The default delivery mode is ``hint``: GraphRAG output is rendered as a
high-priority *prompt hint* via the existing ``request["evidence"]`` channel.
``DataItem.database_schema`` is never permanently pruned, so upstream Value
Retrieval, Few-shot, Schema Linking and SQL Revision keep the complete schema.
A ``hard_filter`` mode is deliberately NOT enabled in this phase (see report).

Unreviewed metrics are always labelled as candidates in the rendered hint.
"""
from __future__ import annotations

import json
import math
from typing import Any

from pydantic import BaseModel, Field

from .join_path import JoinPathResult
from .schema_retriever import RetrievalResult
from .subgraph import SubgraphResult


def estimate_tokens(text: str) -> int:
    """Conservative, dependency-free estimate: CJK ~1 token/char, ASCII ~4 chars/token."""
    if not text:
        return 0
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return int(cjk + math.ceil((len(text) - cjk) / 4))


class ContextBudget(BaseModel):
    mode: str = "hint"
    max_tokens: int = 900
    estimated_tokens: int = 0
    within_budget: bool = True
    compact: bool = False
    truncated_fields: list[str] = Field(default_factory=list)


class SchemaContext(BaseModel):
    database_id: str
    schema_version: str = "1"
    mode: str = "hint"
    selected_metrics: list[dict[str, Any]] = Field(default_factory=list)
    candidate_tables: list[str] = Field(default_factory=list)
    candidate_columns: list[str] = Field(default_factory=list)
    required_columns: list[str] = Field(default_factory=list)
    join_paths: list[dict[str, Any]] = Field(default_factory=list)
    aggregation_risks: list[dict[str, Any]] = Field(default_factory=list)
    ambiguity: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    retrieval_trace: dict[str, Any] = Field(default_factory=dict)
    context_budget: ContextBudget = Field(default_factory=ContextBudget)
    graph_trace: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False,
                          indent=2, sort_keys=True)

    def _compact_evidence_text(self) -> str:
        """Hard-budget rendering used only when trimming alone cannot fit.

        Lines are appended one at a time and the result is guaranteed not to
        exceed ``max_tokens``; nothing is cut in the middle of an identifier.
        """
        limit = self.context_budget.max_tokens
        lines = [
            f"[企业知识增强 Schema 上下文｜hint｜{self.database_id}]",
            f"数据源: {self.database_id}；版本 {self.schema_version}；不裁剪原始 database_schema。",
        ]
        if self.candidate_tables:
            lines.append("候选表(须双引号): " + ", ".join(
                f'"{name}"' for name in self.candidate_tables[:6]))
        if self.required_columns:
            lines.append("必要字段: " + ", ".join(self.required_columns[:8]))
        elif self.candidate_columns:
            lines.append("候选字段: " + ", ".join(self.candidate_columns[:8]))
        if self.aggregation_risks:
            lines.append("风险: " + ", ".join(
                sorted({risk["kind"] for risk in self.aggregation_risks})))
        if self.ambiguity:
            lines.append(f"关联歧义: {len(self.ambiguity)} 处")
        if self.join_paths:
            lines.append("Join 路径: " + " -> ".join(self.join_paths[0].get("tables", [])[:5]))
        kept: list[str] = []
        for line in lines:
            candidate = "\n".join([*kept, line])
            if estimate_tokens(candidate) > limit:
                break
            kept.append(line)
        return "\n".join(kept) or lines[0]

    def to_evidence_text(self) -> str:
        """Render the hint injected into DeepEye-SQL's existing HINT slot."""
        if self.context_budget.compact:
            return self._compact_evidence_text()
        trusted = [m for m in self.selected_metrics if m.get("trusted")]
        lines = [
            "[企业知识增强 Schema 上下文｜模式=hint｜不裁剪原始 DataItem.database_schema]",
            f"数据源: {self.database_id}；Schema 版本: {self.schema_version}。",
            "以下为候选提示，上游 Schema Linking / Value Retrieval / Few-shot / SQL Revision 仍使用完整原始 Schema。",
        ]
        if self.candidate_tables:
            quoted = ", ".join(f'"{name}"' for name in self.candidate_tables[:10])
            lines.append(f"候选表（含 SQL Server Schema 点号前缀，SQL 中必须双引号）: {quoted}")
        if self.required_columns:
            lines.append("已审核指标强制保留字段: " + ", ".join(self.required_columns[:20]))
        elif self.candidate_columns:
            lines.append("候选字段: " + ", ".join(self.candidate_columns[:16]))
        if self.selected_metrics:
            names = ", ".join(
                f"{m['name']}[{'trusted' if m.get('trusted') else 'unreviewed-candidate'}]"
                for m in self.selected_metrics[:6])
            lines.append(f"相关指标: {names}")
            if not trusted:
                lines.append("注意: 当前无已审核可信指标，以上指标仅作召回候选，不得强制确定业务公式。")
        if self.join_paths:
            lines.append("候选 Join 路径（来自 PRAGMA 真实外键约束组）:")
            for path in self.join_paths[:3]:
                parts = []
                for condition in path.get("join_conditions", []):
                    left = ",".join(condition.get("source_columns", []))
                    right = ",".join(condition.get("target_columns", []))
                    parts.append(f"{condition['left_table']}({left}) -> {condition['right_table']}({right})")
                lines.append("  - " + " | ".join(parts) if parts else "  - " + " -> ".join(path["tables"]))
        for risk in self.aggregation_risks[:4]:
            lines.append(f"业务风险[{risk['kind']}]: {risk['description']} 缓解: {risk['required_mitigation']}")
        if self.ambiguity:
            lines.append(f"关联歧义: {len(self.ambiguity)} 处，需结合问题语义判断，不静默选择路径。")
        if self.evidence_refs:
            lines.append("知识来源: " + ", ".join(self.evidence_refs[:5]))
        return "\n".join(lines)


def build_context(retrieval: RetrievalResult, subgraph: SubgraphResult,
                  paths: JoinPathResult, *, schema_version: str = "1",
                  max_tokens: int = 700, mode: str = "hint") -> SchemaContext:
    """Merge recall + graph expansion into one deterministic SchemaContext."""
    tables: list[str] = []
    for name in [*retrieval.candidate_tables, *subgraph.tables]:
        if name not in tables:
            tables.append(name)
    columns: list[str] = []
    for name in [*retrieval.required_columns, *retrieval.candidate_columns,
                 *subgraph.columns, *subgraph.seed_columns]:
        if name not in columns:
            columns.append(name)

    evidence_refs = sorted({
        *[metric.get("source_ref", "") for metric in retrieval.selected_metrics],
        "sqlite:PRAGMA foreign_key_list (90 组真实外键约束)",
        "workspace/knowledge/adventureworks/snapshot.json",
        "app/enterprise/adventureworks_dictionary.json",
    } - {""})

    context = SchemaContext(
        database_id=retrieval.database_id, schema_version=schema_version, mode=mode,
        selected_metrics=retrieval.selected_metrics,
        candidate_tables=tables, candidate_columns=columns,
        required_columns=retrieval.required_columns,
        join_paths=[path.model_dump(mode="json") for path in paths.paths],
        aggregation_risks=list(paths.risks),
        ambiguity=list(paths.ambiguity), evidence_refs=evidence_refs,
        retrieval_trace={
            **retrieval.retrieval_trace,
            "metric_expansion": retrieval.metric_expansion,
            "table_first_tables": retrieval.table_first_tables,
            "column_first_tables": retrieval.column_first_tables,
        },
        graph_trace={
            "seed_tables": subgraph.seed_tables,
            "seed_columns": subgraph.seed_columns,
            "added_tables": subgraph.added_tables,
            "intermediate_tables": subgraph.intermediate_tables,
            "hops_used": subgraph.hops_used,
            "extended_hops": subgraph.extended_hops,
            "fallback_triggered": subgraph.fallback_triggered,
            "unreachable_targets": subgraph.unreachable_targets,
            "excluded": subgraph.excluded,
            "relations_used": subgraph.relations_used,
            "subgraph_trace": subgraph.trace,
            "join_path_trace": paths.trace,
            "excluded_paths": paths.excluded_paths[:10],
            "composite_fk_complete": all(p.composite_fk_complete for p in paths.paths),
        },
        context_budget=ContextBudget(mode=mode, max_tokens=max_tokens),
    )

    # Token budget: the evidence text is the real payload sent to the model.
    truncated: list[str] = []
    text = context.to_evidence_text()
    if estimate_tokens(text) > max_tokens:
        # Trim low-priority material first; keep required tables/columns intact.
        while estimate_tokens(context.to_evidence_text()) > max_tokens and len(context.selected_metrics) > 3:
            context.selected_metrics.pop()
            if "selected_metrics" not in truncated:
                truncated.append("selected_metrics")
        while estimate_tokens(context.to_evidence_text()) > max_tokens and len(context.join_paths) > 2:
            context.join_paths.pop()
            if "join_paths" not in truncated:
                truncated.append("join_paths")
        while estimate_tokens(context.to_evidence_text()) > max_tokens and len(context.candidate_columns) > 12:
            removed = context.candidate_columns.pop()
            if removed not in truncated:
                truncated.append(removed)
        while estimate_tokens(context.to_evidence_text()) > max_tokens and len(context.candidate_tables) > 6:
            removed = context.candidate_tables.pop()
            if removed not in truncated:
                truncated.append(removed)
        text = context.to_evidence_text()
    tokens = estimate_tokens(text)
    compact = False
    if tokens > max_tokens:
        compact = True
        truncated.append("compact_rendering")
        context.context_budget.compact = True
        text = context.to_evidence_text()
        tokens = estimate_tokens(text)
    context.context_budget = ContextBudget(
        mode=mode, max_tokens=max_tokens, estimated_tokens=tokens,
        within_budget=tokens <= max_tokens, compact=compact,
        truncated_fields=truncated,
    )
    return context
