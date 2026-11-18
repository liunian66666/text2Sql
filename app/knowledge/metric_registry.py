"""Structured metric registry built on top of ``business_metrics.json``.

The source JSON is **read only** — its 13 records keep every original field
verbatim so ``EnterpriseKnowledgeBase`` keyword/vector retrieval is untouched.

What Phase 1 adds is a structured, auditable view:

* ``expression`` is always a *verbatim quote* from ``definition`` / ``sql_rule``
  / ``formula``. It is never an invented SQL expression.
* Fields that can be proven from the source text plus the real SQLite schema
  (e.g. which table ``quantity`` lives in) are marked ``derived + verified``.
* Fields with no source evidence stay ``UNKNOWN`` and are listed in
  ``unknown_fields``.
* Every metric is ``reviewed=False`` because no human has signed off the
  business semantics yet. Consequently ``trusted`` is False for all 13 and
  ``trusted_metrics()`` is empty: Phase 1 knowledge must **not** be used as a
  hard SQL constraint. See ``validation.json`` for the pending-review list.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .models import (
    UNKNOWN,
    METRIC_VERSION,
    AggregationRisk,
    DatabaseSchema,
    EvidenceStatus,
    MetricDefinition,
    ValidationIssue,
)


SOURCE_PATH = Path(__file__).resolve().parents[1] / "enterprise" / "business_metrics.json"
SOURCE_REF = "app/enterprise/business_metrics.json"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _relative_source_ref(source: Path) -> str:
    """Keep source references project-relative and reproducible across machines."""
    try:
        return source.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return source.as_posix()

# Fields whose absence must be reported instead of guessed.
SCALAR_UNKNOWN_FIELDS = ("expression", "aggregation", "grain", "time_field", "unit")
LIST_UNKNOWN_FIELDS = ("allowed_dimensions", "join_constraints", "required_filters")

# --------------------------------------------------------------------------- #
# Phase 1 supplements for the 5 sales metrics.
#
# Every entry is either a verbatim quote from business_metrics.json or a value
# derived from those quotes and VERIFIED against the real SQLite schema.
# Entries deliberately left out are UNKNOWN (see unknown_fields), not invented.
# --------------------------------------------------------------------------- #
SALES_SUPPLEMENTS: dict[str, dict[str, Any]] = {
    "revenue": {
        "expression": "quantity * unit_price * (1 - discount_rate)",
        "expression_source": "definition",
        "aggregation": "SUM",
        "aggregation_note": "derived: definition 中「商品成交金额之和」",
        "source_tables": ["orders", "order_items"],
        "source_columns": [
            "order_items.quantity", "order_items.unit_price",
            "order_items.discount_rate", "orders.status",
        ],
        "columns_note": "derived: definition/sql_rule 出现的列名在真实库中定位并验证",
        "required_filters": ["orders.status IN ('paid', 'shipped', 'completed')"],
        "filters_note": "verbatim: sql_rule",
        "unknown": ["grain", "time_field", "unit", "allowed_dimensions", "join_constraints"],
    },
    "net_revenue": {
        "expression": "销售额减去退款金额；没有退款的订单退款金额按0处理。",
        "expression_source": "definition",
        "aggregation": UNKNOWN,
        "source_tables": ["orders", "order_items", "refunds"],
        "source_columns": [
            "order_items.quantity", "order_items.unit_price", "order_items.discount_rate",
            "refunds.order_id", "refunds.refund_amount", "orders.order_id",
        ],
        "columns_note": "derived: definition/sql_rule 提及「订单明细」「退款按 order_id 聚合」，"
                        "字段在真实库中定位并验证",
        "join_constraints": ["退款按 order_id 聚合后再与订单连接，避免订单明细导致退款金额重复。"],
        "joins_note": "verbatim: sql_rule",
        "depends_on_metrics": ["revenue"],
        "risks": [
            {
                "kind": "duplicate_aggregation",
                "description": "orders→order_items 与 orders→refunds 均为一对多（PRAGMA 声明外键，"
                               "order_items.order_id / refunds.order_id 均非唯一）。若把退款金额直接与"
                               "订单明细行连接，同一订单的多条明细会重复计入退款金额，导致净销售额被低估。",
                "affected_tables": ["orders", "order_items", "refunds"],
                "required_mitigation": "退款按 order_id 聚合后再与订单连接，避免订单明细导致退款金额重复。",
                "source_ref": f"{SOURCE_REF}#net_revenue.sql_rule + sqlite:PRAGMA foreign_key_list",
            },
            {
                "kind": "mixed_grain",
                "description": "订单明细为行级粒度、退款记录为订单级/退款单级粒度，两者汇总前必须对齐到同一粒度。",
                "affected_tables": ["order_items", "refunds"],
                "required_mitigation": "先按 order_id 聚合退款，再与订单粒度结果连接。",
                "source_ref": f"{SOURCE_REF}#net_revenue.sql_rule",
            },
        ],
        "unknown": ["grain", "time_field", "unit", "allowed_dimensions", "aggregation",
                    "required_filters"],
    },
    "average_order_value": {
        "expression": "有效订单销售额除以有效订单去重数量。",
        "expression_source": "definition",
        "aggregation": "SUM / COUNT(DISTINCT orders.order_id)",
        "aggregation_note": "derived: definition 分子 + sql_rule 分母逐字",
        "source_tables": ["orders", "order_items"],
        "source_columns": [
            "orders.order_id", "orders.status", "order_items.quantity",
            "order_items.unit_price", "order_items.discount_rate",
        ],
        "columns_note": "derived: sql_rule 明确 orders.order_id；分子字段继承 revenue 并在真实库验证",
        "required_filters": ["有效订单（definition 原文；有效状态集合见 revenue.sql_rule）"],
        "filters_note": "verbatim: definition«有效订单» + 关联 revenue.sql_rule",
        "depends_on_metrics": ["revenue"],
        "unknown": ["grain", "time_field", "unit", "allowed_dimensions", "join_constraints"],
    },
    "refund_rate": {
        "expression": "退款金额除以销售额，结果通常乘100表示百分比。",
        "expression_source": "definition",
        "aggregation": "RATIO",
        "aggregation_note": "derived: definition「除以」",
        "unit": "%",
        "unit_note": "derived: definition「乘100表示百分比」",
        "source_tables": ["refunds", "orders", "order_items"],
        "source_columns": [
            "refunds.refund_amount", "refunds.order_id", "orders.order_id",
            "order_items.quantity", "order_items.unit_price", "order_items.discount_rate",
        ],
        "columns_note": "derived: definition/sql_rule 提及退款金额与销售额，字段继承 net_revenue/revenue 并验证",
        "depends_on_metrics": ["revenue", "net_revenue"],
        "risks": [
            {
                "kind": "division_by_zero",
                "description": "销售额为 0 时退款率分母为零。",
                "affected_tables": ["order_items", "refunds"],
                "required_mitigation": "使用 NULLIF(销售额, 0) 防止除零。",
                "source_ref": f"{SOURCE_REF}#refund_rate.sql_rule",
            },
            {
                "kind": "duplicate_aggregation",
                "description": "分子退款金额与分母销售额来自不同粒度表，继承 net_revenue 的重复聚合风险。",
                "affected_tables": ["orders", "order_items", "refunds"],
                "required_mitigation": "退款先按 order_id 聚合；分子分母使用同一有效订单口径。",
                "source_ref": f"{SOURCE_REF}#net_revenue.sql_rule",
            },
        ],
        "unknown": ["grain", "time_field", "allowed_dimensions", "join_constraints",
                    "required_filters"],
    },
    "new_customer": {
        "expression": "先按 customer_id 计算 MIN(order_date)，且订单状态必须有效。",
        "expression_source": "sql_rule",
        "aggregation": UNKNOWN,
        "time_field": "orders.order_date",
        "time_field_note": "derived: sql_rule 明确 MIN(order_date)，orders.order_date 在真实库验证存在",
        "source_tables": ["orders"],
        "source_columns": ["orders.customer_id", "orders.order_date", "orders.status"],
        "columns_note": "derived: sql_rule 明确 customer_id / order_date / 订单状态，字段在真实库验证",
        "required_filters": ["订单状态必须有效（sql_rule 原文；状态集合见 revenue.sql_rule）"],
        "filters_note": "verbatim: sql_rule + 关联 revenue.sql_rule",
        "depends_on_metrics": ["revenue"],
        "unknown": ["grain", "unit", "allowed_dimensions", "join_constraints", "aggregation"],
    },
}


# --------------------------------------------------------------------------- #
# Deterministic derivation helpers (used for the 8 HR metrics)
# --------------------------------------------------------------------------- #
def _derive_aggregation(formula: str) -> str:
    text = formula.upper()
    if "COUNT(DISTINCT" in text:
        return "COUNT_DISTINCT"
    if "AVG(" in text:
        return "AVG"
    if "SUM(" in text and "/" in text:
        return "SUM_RATIO"
    if "/" in text:
        return "RATIO"
    if "COUNT(" in text:
        return "COUNT"
    if "SUM(" in text:
        return "SUM"
    return UNKNOWN


def _derive_time_field(record: dict[str, Any], columns: list[str]) -> str:
    """Only accept a time field explicitly named in the source text."""
    evidence = " ".join(str(record.get(key) or "")
                        for key in ("formula", "time_grain", "sql_rule", "definition"))
    candidates = []
    for reference in columns:
        if "." not in reference:
            continue
        bare = reference.rsplit(".", 1)[1]
        # Only real time columns qualify. Measure columns such as
        # ``attended_days`` / ``late_days`` must never be mistaken for one.
        if not re.search(r"(^|_)(date|month|period|quarter|year|time)($|_)",
                         bare, re.IGNORECASE):
            continue
        if bare in evidence:
            candidates.append(reference)
    return candidates[0] if len(candidates) == 1 else UNKNOWN


def _derive_unit(record: dict[str, Any]) -> str:
    text = f"{record.get('formula', '')} {record.get('definition', '')}"
    if "* 100%" in text or "占比" in text or "比例" in text or "率" in text:
        return "%"
    return UNKNOWN


def _compute_unknown_fields(metric: MetricDefinition) -> list[str]:
    unknown = [field for field in SCALAR_UNKNOWN_FIELDS
               if getattr(metric, field) == UNKNOWN]
    unknown.extend(field for field in LIST_UNKNOWN_FIELDS
                   if not getattr(metric, field))
    # Keep a stable order for byte-identical builds.
    order = [*SCALAR_UNKNOWN_FIELDS, *LIST_UNKNOWN_FIELDS]
    return sorted(unknown, key=order.index)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _apply_structured(metric: MetricDefinition, structured: dict[str, Any]) -> None:
    """Apply explicitly documented structured fields from a curated metric file.

    Only used by sources that opt in via a ``structured`` object. The original
    13 records do not have one, so their behaviour is unchanged. ``reviewed``
    is deliberately NOT overridable: an unreviewed definition must never be
    turned into a trusted SQL constraint by editing a JSON file.
    """
    for key in ("expression", "expression_kind", "aggregation", "grain",
                "time_field", "unit", "description", "version", "source_ref"):
        if key in structured:
            setattr(metric, key, str(structured[key]))
    for key in ("source_tables", "source_columns", "allowed_dimensions",
                "join_constraints", "required_filters", "depends_on_metrics"):
        if key in structured:
            setattr(metric, key, list(structured[key]))
    if "evidence_status" in structured:
        metric.evidence_status = EvidenceStatus(str(structured["evidence_status"]))
    for key, note in (structured.get("provenance") or {}).items():
        metric.provenance[key] = str(note)
    for index, item in enumerate(structured.get("aggregation_risks") or []):
        metric.aggregation_risks.append(AggregationRisk(
            risk_id=f"{metric.metric_id}:structured:{index}:{item['kind']}",
            metric_id=metric.metric_id,
            kind=item["kind"],
            description=str(item["description"]),
            affected_tables=list(item.get("affected_tables") or []),
            required_mitigation=str(item["required_mitigation"]),
            source_ref=str(item.get("source_ref") or metric.source_ref),
            reviewed=False,
        ))


def _build_metric(record: dict[str, Any],
                  source_ref_base: str = SOURCE_REF) -> MetricDefinition:
    metric_id = str(record["id"])
    source_ref = f"{source_ref_base}#{metric_id}"
    raw_fields = list(record.get("fields") or [])
    raw_tables = list(record.get("tables") or [])
    supplement = SALES_SUPPLEMENTS.get(metric_id)
    provenance: dict[str, str] = {}

    if supplement is not None:
        expression = supplement["expression"]
        expression_kind = "verbatim"
        provenance["expression"] = f"verbatim: {source_ref}.{supplement['expression_source']}"
        aggregation = supplement.get("aggregation", UNKNOWN)
        provenance["aggregation"] = supplement.get(
            "aggregation_note", "unknown: 原文未给出聚合方式")
        source_tables = list(supplement["source_tables"])
        source_columns = list(supplement["source_columns"])
        provenance["source_tables"] = "derived: 由原文列名在真实库定位（build-knowledge 校验）"
        provenance["source_columns"] = supplement["columns_note"]
        time_field = supplement.get("time_field", UNKNOWN)
        provenance["time_field"] = supplement.get(
            "time_field_note", "unknown: 原文未指定统计时间字段")
        unit = supplement.get("unit", UNKNOWN)
        provenance["unit"] = supplement.get("unit_note", "unknown: 原文未说明单位")
        required_filters = list(supplement.get("required_filters") or [])
        provenance["required_filters"] = supplement.get(
            "filters_note", "unknown: 原文未明确必需过滤条件")
        join_constraints = list(supplement.get("join_constraints") or [])
        provenance["join_constraints"] = supplement.get(
            "joins_note", "unknown: 原文未明确 Join 约束")
        depends_on_metrics = list(supplement.get("depends_on_metrics") or [])
        evidence_status = EvidenceStatus.PARTIAL
        risks = [
            AggregationRisk(
                risk_id=f"{metric_id}:{index}:{item['kind']}",
                metric_id=metric_id,
                kind=item["kind"],
                description=item["description"],
                affected_tables=list(item["affected_tables"]),
                required_mitigation=item["required_mitigation"],
                source_ref=item["source_ref"],
                reviewed=False,
            )
            for index, item in enumerate(supplement.get("risks") or [])
        ]
    else:
        expression = str(record.get("formula") or UNKNOWN)
        expression_kind = "verbatim" if record.get("formula") else "unknown"
        provenance["expression"] = f"verbatim: {source_ref}.formula"
        aggregation = _derive_aggregation(expression)
        provenance["aggregation"] = "derived: 从 formula 的聚合函数字面提取"
        source_tables = raw_tables
        source_columns = raw_fields
        provenance["source_tables"] = f"verbatim: {source_ref}.tables"
        provenance["source_columns"] = f"verbatim: {source_ref}.fields"
        time_field = _derive_time_field(record, raw_fields)
        provenance["time_field"] = (
            "derived: 原文 time_grain/sql_rule 明确提及的日期类字段" if time_field != UNKNOWN
            else "unknown: 原文未指定统计时间字段")
        unit = _derive_unit(record)
        provenance["unit"] = (
            "derived: 原文含百分比/占比/率" if unit != UNKNOWN else "unknown: 原文未说明单位")
        required_filters = [str(record["sql_rule"])] if record.get("sql_rule") else []
        provenance["required_filters"] = f"verbatim: {source_ref}.sql_rule"
        join_constraints = []
        provenance["join_constraints"] = "unknown: 原文未给出可校验的 Join 约束"
        depends_on_metrics = []
        evidence_status = EvidenceStatus.COMPLETE
        risks = []

    metric = MetricDefinition(
        metric_id=metric_id,
        name=str(record["name"]),
        aliases=list(record.get("keywords") or []),
        domain=str(record.get("domain", UNKNOWN)),
        database_id=str(record["database_id"]),
        description=str(record["definition"]),
        expression=expression,
        expression_kind=expression_kind,
        aggregation=aggregation,
        grain=UNKNOWN,
        source_tables=source_tables,
        source_columns=source_columns,
        time_field=time_field,
        allowed_dimensions=[],
        required_filters=required_filters,
        join_constraints=join_constraints,
        unit=unit,
        depends_on_metrics=depends_on_metrics,
        aggregation_risks=risks,
        version=METRIC_VERSION,
        source_ref=source_ref,
        reviewed=False,
        evidence_status=evidence_status,
        provenance=provenance,
        keywords=list(record.get("keywords") or []),
        raw_definition=str(record["definition"]),
        raw_sql_rule=str(record.get("sql_rule", "")),
        raw_formula=record.get("formula"),
        raw_tables=raw_tables,
        raw_fields=raw_fields,
        raw_time_grain=record.get("time_grain"),
    )
    structured = record.get("structured") or {}
    if structured:
        _apply_structured(metric, structured)
    metric.unknown_fields = _compute_unknown_fields(metric)
    return metric


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class MetricRegistry:
    """Query API + schema validation for the structured metric knowledge."""

    def __init__(self, metrics: list[MetricDefinition]):
        seen: set[str] = set()
        for metric in metrics:
            if metric.metric_id in seen:
                raise ValueError(f"Duplicate metric_id: {metric.metric_id}")
            seen.add(metric.metric_id)
        self._metrics = list(metrics)

    @classmethod
    def from_records(cls, records: list[dict[str, Any]],
                     source_ref_base: str = SOURCE_REF) -> MetricRegistry:
        return cls([_build_metric(record, source_ref_base) for record in records])

    @classmethod
    def from_file(cls, path: Path | None = None) -> MetricRegistry:
        source = Path(path) if path is not None else SOURCE_PATH
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        # For the default source this resolves to exactly SOURCE_REF, so the
        # original 13 metrics keep their byte-identical provenance strings.
        return cls.from_records(payload["metrics"],
                                _relative_source_ref(source))

    @property
    def metrics(self) -> list[MetricDefinition]:
        return list(self._metrics)

    def get(self, metric_id: str) -> MetricDefinition | None:
        return next((m for m in self._metrics if m.metric_id == metric_id), None)

    def by_database(self, database_id: str) -> list[MetricDefinition]:
        return [m for m in self._metrics if m.database_id == database_id]

    def by_domain(self, domain: str) -> list[MetricDefinition]:
        return [m for m in self._metrics if m.domain == domain]

    def resolve(self, text: str) -> list[MetricDefinition]:
        """Exact metric_id / name / alias lookup (case-insensitive)."""
        needle = text.strip().casefold()
        return [m for m in self._metrics
                if needle in {m.metric_id.casefold(), m.name.casefold(),
                              *(alias.casefold() for alias in m.aliases)}]

    def trusted_metrics(self) -> list[MetricDefinition]:
        """Only reviewed, fully specified metrics may become SQL constraints."""
        return [m for m in self._metrics if m.trusted]

    def pending_review_metrics(self) -> list[MetricDefinition]:
        return [m for m in self._metrics if not m.trusted]

    def validate(self, schemas: dict[str, DatabaseSchema]) -> list[ValidationIssue]:
        """Check every referenced table/column/metric against real metadata."""
        issues: list[ValidationIssue] = []
        known_ids = {m.metric_id for m in self._metrics}
        for metric in self._metrics:
            # Guard against silently "filling in" a business rule that has no
            # source evidence: the computed unknown set must match the audited
            # expectation recorded in SALES_SUPPLEMENTS.
            expected = SALES_SUPPLEMENTS.get(metric.metric_id, {}).get("unknown")
            if expected is not None and set(metric.unknown_fields) != set(expected):
                issues.append(ValidationIssue(
                    severity="error", code="supplement_mismatch",
                    message=(f"指标 {metric.metric_id} 的未知字段集与审核记录不一致："
                             f"实际 {sorted(metric.unknown_fields)} / 期望 {sorted(expected)}"),
                    metric_id=metric.metric_id, database_id=metric.database_id,
                ))
            schema = schemas.get(metric.database_id)
            if schema is None:
                issues.append(ValidationIssue(
                    severity="error", code="missing_database",
                    message=f"指标 {metric.metric_id} 引用的数据库未构建 Schema：{metric.database_id}",
                    metric_id=metric.metric_id, database_id=metric.database_id,
                ))
                continue
            for table_name in metric.source_tables:
                if schema.table(table_name) is None:
                    issues.append(ValidationIssue(
                        severity="error", code="missing_table",
                        message=f"指标 {metric.metric_id} 引用的表不存在：{table_name}",
                        metric_id=metric.metric_id, database_id=metric.database_id,
                        reference=table_name,
                    ))
            for reference in metric.source_columns:
                if "." not in reference:
                    issues.append(ValidationIssue(
                        severity="error", code="bad_column_reference",
                        message=f"指标 {metric.metric_id} 的字段引用缺少表名：{reference}",
                        metric_id=metric.metric_id, database_id=metric.database_id,
                        reference=reference,
                    ))
                    continue
                # rsplit: AdventureWorks keeps SQL Server "Schema.Table" names,
                # so a column reference may contain more than one dot.
                table_name, column_name = reference.rsplit(".", 1)
                if schema.column(table_name, column_name) is None:
                    issues.append(ValidationIssue(
                        severity="error", code="missing_column",
                        message=f"指标 {metric.metric_id} 引用的字段不存在：{reference}",
                        metric_id=metric.metric_id, database_id=metric.database_id,
                        reference=reference,
                    ))
            if metric.time_field != UNKNOWN and metric.time_field not in metric.source_columns:
                issues.append(ValidationIssue(
                    severity="error", code="missing_column",
                    message=f"指标 {metric.metric_id} 的时间字段未被依赖字段覆盖：{metric.time_field}",
                    metric_id=metric.metric_id, database_id=metric.database_id,
                    reference=metric.time_field,
                ))
            for dependency in metric.depends_on_metrics:
                if dependency not in known_ids:
                    issues.append(ValidationIssue(
                        severity="error", code="missing_metric_dependency",
                        message=f"指标 {metric.metric_id} 依赖的指标不存在：{dependency}",
                        metric_id=metric.metric_id, database_id=metric.database_id,
                        reference=dependency,
                    ))
            issues.append(ValidationIssue(
                severity="warning", code="unreviewed_metric",
                message=(f"指标 {metric.metric_id} 未人工审核，且存在 {len(metric.unknown_fields)} "
                         f"项未知业务定义，不可作为可信 SQL 约束"),
                metric_id=metric.metric_id, database_id=metric.database_id,
                reference=",".join(metric.unknown_fields),
            ))
        return issues

    def to_json(self) -> str:
        return json.dumps({"knowledge_version": "1",
                           "metrics": [m.model_dump(mode="json") for m in self._metrics]},
                          ensure_ascii=False, indent=2, sort_keys=True)
