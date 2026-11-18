"""``enterprise-agent build-knowledge`` — reproducible offline knowledge build.

No model endpoint, no database writes and no new dependencies. The command
reads ``business_metrics.json`` plus SQLite PRAGMA metadata and writes a set of
deterministic JSON artifacts under ``workspace/knowledge/``.

Determinism contract:
* Stable IDs are natural keys (see ``app.knowledge.models``).
* ``snapshot.content_fingerprint`` excludes ``built_at`` entirely.
* ``built_at`` defaults to None, so a rebuild with unchanged inputs produces
  byte-identical artifacts. Pass ``--built-at`` only when an audit timestamp is
  actually required; the content fingerprint still stays unchanged.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from app.knowledge.dependencies import KnowledgeIndex, build_edges, build_snapshot
from app.knowledge.metric_registry import MetricRegistry
from app.knowledge.models import EdgeType, ValidationIssue, canonical_json
from app.knowledge.schema_extractor import (
    ENTERPRISE_DATABASE_IDS,
    KNOWN_DATABASE_IDS,
    PROJECT_ROOT,
    extract_database_schema,
    resolve_database_path,
    schema_to_json,
)


DEFAULT_OUTPUT = "workspace/knowledge"
DEFAULT_METRICS = "app/enterprise/business_metrics.json"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", action="append", default=None,
                        help="数据源 ID，可重复指定；缺省构建全部企业库")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help=f"产物目录，默认 {DEFAULT_OUTPUT}")
    parser.add_argument("--metrics", default=DEFAULT_METRICS,
                        help="指标来源 JSON（只读）")
    parser.add_argument("--project-root", default=None,
                        help="项目根目录；默认取 app 包的上两级")
    parser.add_argument("--validate-only", action="store_true",
                        help="只校验并打印摘要，不写任何文件")
    parser.add_argument("--strict", action="store_true",
                        help="存在 warning 时也返回非 0（用于 CI 审核门禁）")
    parser.add_argument("--built-at", default=None,
                        help="可选的 ISO 审计时间戳；缺省为空以保证产物可复现")
    parser.add_argument("--quiet", action="store_true", help="只输出一行摘要")


def _absolute(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _group_issues(issues: list[ValidationIssue]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {"error": [], "warning": [], "info": []}
    for issue in issues:
        grouped.setdefault(issue.severity, []).append(issue.model_dump(mode="json"))
    return grouped


def _build_payload(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    output = _absolute(root, args.output)
    metrics_path = _absolute(root, args.metrics)
    database_ids = list(dict.fromkeys(args.database or list(ENTERPRISE_DATABASE_IDS)))
    unknown = [db for db in database_ids if db not in KNOWN_DATABASE_IDS]
    if unknown:
        raise ValueError(f"未知数据源：{unknown}；可用 {list(KNOWN_DATABASE_IDS)}")

    full_registry = MetricRegistry.from_file(metrics_path)
    schemas = {
        database_id: extract_database_schema(
            database_id, resolve_database_path(database_id, root))
        for database_id in database_ids
    }
    # Only metrics belonging to the selected databases participate in this build.
    active = MetricRegistry([m for m in full_registry.metrics
                             if m.database_id in schemas])
    issues = active.validate(schemas)
    edges = build_edges(active, schemas)
    index = KnowledgeIndex(active, schemas, edges)
    snapshot = build_snapshot(active, schemas, issues, built_at=args.built_at)

    grouped = _group_issues(issues)
    pending = [
        {
            "metric_id": metric.metric_id,
            "name": metric.name,
            "database_id": metric.database_id,
            "evidence_status": metric.evidence_status.value,
            "unknown_fields": list(metric.unknown_fields),
            "reviewed": metric.reviewed,
            "trusted": metric.trusted,
            "source_ref": metric.source_ref,
        }
        for metric in active.pending_review_metrics()
    ]
    risks = [
        risk.model_dump(mode="json")
        for metric in active.metrics
        for risk in metric.aggregation_risks
    ]

    artifacts: dict[str, str] = {
        "metrics.json": full_registry.to_json(),
        **{f"schema_{db}.json": schema_to_json(schema) for db, schema in sorted(schemas.items())},
        "relations.json": json.dumps(
            {"knowledge_version": "1",
             "relations": [relation.model_dump(mode="json")
                           for schema in sorted(schemas.values(),
                                                key=lambda item: item.database_id)
                           for relation in schema.relations]},
            ensure_ascii=False, indent=2, sort_keys=True),
        "snapshot.json": json.dumps(
            {**snapshot.model_dump(mode="json"),
             "content_fingerprint": snapshot.content_fingerprint()},
            ensure_ascii=False, indent=2, sort_keys=True),
        "validation.json": json.dumps(
            {
                "knowledge_version": "1",
                "valid": not grouped["error"],
                "error_count": len(grouped["error"]),
                "warning_count": len(grouped["warning"]),
                "issues": grouped,
                "pending_review": pending,
                "aggregation_risks": risks,
            },
            ensure_ascii=False, indent=2, sort_keys=True),
    }

    artifact_hashes = {
        name: _hash_text(text) for name, text in sorted(artifacts.items())
        if name != "snapshot.json"  # keep the hash set free of self-reference
    }
    summary = {
        "knowledge_version": "1",
        "selected_databases": database_ids,
        "metric_registry_count": len(full_registry.metrics),
        "metrics_in_snapshot": len(active.metrics),
        "structured_metrics": [
            {"metric_id": metric.metric_id,
             "evidence_status": metric.evidence_status.value,
             "unknown_fields": list(metric.unknown_fields),
             "trusted": metric.trusted}
            for metric in active.metrics
        ],
        "trusted_metric_count": len(active.trusted_metrics()),
        "pending_review_metric_count": len(active.pending_review_metrics()),
        "table_count": snapshot.table_count,
        "column_count": snapshot.column_count,
        "schema_relation_count": snapshot.relation_count,
        "edge_count": snapshot.edge_count,
        "edge_type_counts": {
            edge_type.value: sum(1 for edge in edges if edge.edge_type == edge_type)
            for edge_type in EdgeType
        },
        "schema_fingerprints": snapshot.schema_fingerprints,
        "content_fingerprint": snapshot.content_fingerprint(),
        "artifact_hashes": artifact_hashes,
        "validation_error_count": len(grouped["error"]),
        "validation_warning_count": len(grouped["warning"]),
        "column_impact_example": {
            column_id: index.metrics_affected_by_column(column_id)
            for column_id in (
                "enterprise_sales.order_items.unit_price",
                "enterprise_sales.refunds.refund_amount",
                "enterprise_hr.employees.hire_date",
            ) if column_id in {column.column_id
                               for schema in schemas.values()
                               for table in schema.tables
                               for column in table.columns}
        },
    }
    artifacts["summary.json"] = json.dumps(summary, ensure_ascii=False,
                                           indent=2, sort_keys=True)
    return {
        "output": output,
        "artifacts": artifacts,
        "summary": summary,
        "error_count": len(grouped["error"]),
        "warning_count": len(grouped["warning"]),
    }


def run(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve() if args.project_root else PROJECT_ROOT
    payload = _build_payload(root, args)
    summary = payload["summary"]

    if not args.validate_only:
        output: Path = payload["output"]
        output.mkdir(parents=True, exist_ok=True)
        for name, text in sorted(payload["artifacts"].items()):
            (output / name).write_text(text + "\n", encoding="utf-8")

    report = {
        "validated_only": bool(args.validate_only),
        "output": str(payload["output"]),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "selected_databases": summary["selected_databases"],
        "metrics": summary["metrics_in_snapshot"],
        "tables": summary["table_count"],
        "columns": summary["column_count"],
        "schema_relations": summary["schema_relation_count"],
        "edges": summary["edge_count"],
        "trusted_metrics": summary["trusted_metric_count"],
        "pending_review": summary["pending_review_metric_count"],
        "validation_errors": summary["validation_error_count"],
        "validation_warnings": summary["validation_warning_count"],
        "content_fingerprint": summary["content_fingerprint"],
    }
    if args.quiet:
        print(canonical_json(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    if payload["error_count"]:
        return 1
    if args.strict and payload["warning_count"]:
        return 1
    return 0
