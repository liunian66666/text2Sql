"""Read-only knowledge-asset and system-status repositories for the web UI.

Everything here only *reads* already-built artifacts under ``workspace/knowledge``
and the curated metric JSON files; nothing is rebuilt, edited or written.
Responses intentionally omit filesystem paths and any credential-bearing fields.

Honesty contract (mirrors docs/adventureworks/PROVENANCE.md):
* ``reviewed``/``trusted`` flags are reported verbatim; unreviewed metrics are
  surfaced as 待审核, never as confirmed business rules.
* AdventureWorks is reported with its real registration state: enabled/registered
  vs not enabled (``ENABLE_ADVENTUREWORKS``) vs file missing. We never fake a
  "ready" state.
* The simulated policy documents are listed as SIMULATED assets only; no
  document-RAG capability is claimed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class KnowledgeOverviewRepository:
    """Aggregate the knowledge layer's real artifacts for the read-only page."""

    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self.knowledge_root = self.project_root / "workspace" / "knowledge"
        self.enterprise_root = self.project_root / "app" / "enterprise"

    # ------------------------------------------------------------------ #
    # loaders
    # ------------------------------------------------------------------ #
    @staticmethod
    def _load_json(path: Path) -> Any | None:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _database_ids(self) -> list[str]:
        ids: list[str] = []
        paths = [*self.knowledge_root.glob("schema_*.json"),
                 *self.knowledge_root.glob("*/schema_*.json")]
        for path in sorted(paths):
            data = self._load_json(path)
            if isinstance(data, dict) and data.get("database_id"):
                ids.append(data["database_id"])
        return ids

    def _metrics_for(self, database_id: str) -> list[dict[str, Any]]:
        metrics: list[dict[str, Any]] = []
        if database_id == "adventureworks":
            data = self._load_json(self.enterprise_root / "adventureworks_metrics.json")
        else:
            data = self._load_json(self.enterprise_root / "business_metrics.json")
        for item in (data or {}).get("metrics", []):
            if item.get("database_id") == database_id:
                metrics.append(item)
        return metrics

    def _schema_for(self, database_id: str) -> dict[str, Any] | None:
        for candidate in (self.knowledge_root / f"schema_{database_id}.json",
                          self.knowledge_root / database_id / f"schema_{database_id}.json"):
            data = self._load_json(candidate)
            if isinstance(data, dict):
                return data
        return None

    def _summary_for(self, database_id: str) -> dict[str, Any]:
        for candidate in (self.knowledge_root / database_id / "summary.json",
                          self.knowledge_root / "summary.json"):
            data = self._load_json(candidate)
            if isinstance(data, dict):
                return data
        return {}

    # ------------------------------------------------------------------ #
    # public payload
    # ------------------------------------------------------------------ #
    def overview(self) -> dict[str, Any]:
        databases: list[dict[str, Any]] = []
        total_metrics = reviewed_metrics = 0
        total_tables = total_columns = total_relations = 0
        total_edges = 0
        for database_id in self._database_ids():
            schema = self._schema_for(database_id) or {}
            summary = self._summary_for(database_id)
            metrics = self._metrics_for(database_id)
            tables = schema.get("tables", []) or []
            relations = schema.get("relations", []) or []
            column_count = sum(len(table.get("columns", []) or []) for table in tables)
            reviewed = sum(1 for item in metrics if item.get("reviewed") is True)
            pending = len(metrics) - reviewed
            total_metrics += len(metrics)
            reviewed_metrics += reviewed
            total_tables += len(tables)
            total_columns += column_count
            total_relations += len(relations)
            total_edges += int(summary.get("edge_count") or 0)
            databases.append({
                "database_id": database_id,
                "table_count": len(tables),
                "column_count": column_count,
                "relation_count": len(relations),
                "metric_count": len(metrics),
                "reviewed_metric_count": reviewed,
                "pending_metric_count": pending,
                "edge_count": int(summary.get("edge_count") or 0),
                # Unreviewed metrics must never act as hard SQL constraints.
                "review_note": "指标口径未经人工审核，仅作为检索提示" if pending else "指标已人工审核",
                "content_fingerprint": summary.get("content_fingerprint"),
            })
        return {
            "databases": databases,
            "totals": {
                "database_count": len(databases),
                "table_count": total_tables,
                "column_count": total_columns,
                "relation_count": total_relations,
                "metric_count": total_metrics,
                "reviewed_metric_count": reviewed_metrics,
                "pending_metric_count": total_metrics - reviewed_metrics,
                "edge_count": total_edges,
            },
            "simulated_assets": self._simulated_assets(),
        }

    def _simulated_assets(self) -> dict[str, Any]:
        """Simulated policy documents: presence only, explicitly not document RAG."""
        docs_root = self.project_root / "docs" / "simulated-policies"
        documents = sorted(path.name for path in docs_root.glob("*.md")) if docs_root.is_dir() else []
        return {
            "document_count": len(documents),
            "documents": documents,
            "statement": "以上为 SIMULATED / UNREVIEWED 模拟制度资产，仅用于示例展示；"
                         "文档 RAG 联合查询尚未实现。",
        }

    def metrics(self) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for database_id in self._database_ids():
            for item in self._metrics_for(database_id):
                payload.append({
                    "database_id": database_id,
                    "metric_id": item.get("metric_id"),
                    "name": item.get("name"),
                    "aliases": item.get("aliases") or [],
                    "description": item.get("description"),
                    "expression": item.get("expression"),
                    "expression_kind": item.get("expression_kind"),
                    "aggregation": item.get("aggregation"),
                    "grain": item.get("grain") or "UNKNOWN",
                    "source_tables": item.get("source_tables") or [],
                    "source_columns": item.get("source_columns") or [],
                    "time_field": item.get("time_field"),
                    "reviewed": item.get("reviewed") is True,
                    "evidence_status": item.get("evidence_status"),
                    "unknown_fields": item.get("unknown_fields") or [],
                })
        return payload

    def schema_dictionary(self) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for database_id in self._database_ids():
            schema = self._schema_for(database_id) or {}
            tables = []
            for table in schema.get("tables", []) or []:
                columns = [{
                    "name": column.get("column_name"),
                    "data_type": column.get("data_type"),
                    "nullable": bool(column.get("nullable")),
                    "primary_key": bool(column.get("primary_key")),
                } for column in (table.get("columns") or [])]
                tables.append({
                    "table_id": table.get("table_id"),
                    "table_name": table.get("table_name") or table.get("table_id"),
                    "column_count": len(columns),
                    "columns": columns,
                })
            payload.append({"database_id": database_id, "tables": tables})
        return payload

    def relations(self) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for database_id in self._database_ids():
            # 根目录 relations.json 只含 Sales/HR；AdventureWorks 的关系产物
            # 在其同名子目录里，两处都要找。
            edges: list[dict[str, Any]] = []
            for candidate in (self.knowledge_root / "relations.json",
                              self.knowledge_root / database_id / "relations.json"):
                data = self._load_json(candidate)
                for relation in (data or {}).get("relations", []):
                    if relation.get("database_id") != database_id:
                        continue
                    edges.append({
                        "relation_id": relation.get("relation_id"),
                        "source_table": relation.get("source_table") or "",
                        "target_table": relation.get("target_table") or "",
                        "source_columns": relation.get("source_columns") or [],
                        "target_columns": relation.get("target_columns") or [],
                        "cardinality": relation.get("cardinality"),
                        "origin": relation.get("origin"),
                        "verified": relation.get("verified") is True,
                        "duplicate_aggregation_risk": bool(relation.get("duplicate_aggregation_risk")),
                        "composite": len(relation.get("source_columns") or []) > 1,
                    })
            payload.append({"database_id": database_id, "relations": edges})
        return payload


class SystemStatusRepository:
    """Registration and index-readiness facts, without paths or credentials."""

    def __init__(self, project_root: Path, registry):
        self.project_root = project_root.resolve()
        self.registry = registry

    def status(self) -> dict[str, Any]:
        from app.knowledge.enhancement import is_enabled as knowledge_enhancement_enabled

        databases = []
        for entry in self.registry.public_entries():
            path = self.registry.database_paths.get(entry["id"])
            # Sales/HR 的 summary.json 在 knowledge 根目录，AdventureWorks 在其
            # 同名子目录；两处任一存在即视为知识产物已构建。
            knowledge_ready = any(
                (self.project_root / "workspace" / "knowledge" / candidate / "summary.json").is_file()
                for candidate in (entry["id"], ".")
            )
            databases.append({
                "id": entry["id"],
                "name": entry["name"],
                "domain": entry.get("domain"),
                "domain_name": entry.get("domain_name"),
                "table_count": entry.get("table_count"),
                "registered": path is not None,
                "database_ready": bool(path and Path(path).is_file()),
                "knowledge_artifacts": knowledge_ready,
            })
        return {
            "databases": databases,
            "knowledge_enhancement_enabled": knowledge_enhancement_enabled(),
            "optional_databases": self._optional_databases(),
        }

    def _optional_databases(self) -> list[dict[str, Any]]:
        """AdventureWorks facts, reported honestly whatever the switch state."""
        try:
            from app.enterprise import adventureworks as aw
        except Exception:
            return []
        path = aw.database_path(self.project_root)
        return [{
            "id": aw.DATABASE_ID,
            "display_name": aw.DISPLAY_NAME,
            "table_count": aw.TABLE_COUNT,
            "column_count": aw.COLUMN_COUNT,
            "row_count": aw.ROW_COUNT,
            "enabled": aw.is_enabled(),
            "file_present": path.is_file(),
            # The registry only lists AW when enabled AND the file exists; the
            # picker therefore already reflects reality without extra wiring.
            "registered": aw.registered_path(self.project_root) is not None,
            "enable_hint": f"设置 {aw.ENABLE_ENV_VAR}=1 并重启服务后启用",
            "sample_data": True,
        }]
