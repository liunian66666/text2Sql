"""Typed tools shared by the LangGraph agent and the MCP server."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable

from app.enterprise import EnterpriseKnowledgeBase
from .visualization import build_visualization


@dataclass
class ToolOutcome:
    content: str
    updates: dict[str, Any] = field(default_factory=dict)


class AgentToolbox:
    """Application tools with one implementation and two transports.

    LangGraph calls :meth:`execute`; ``runner/run_mcp_server.py`` exposes the
    same public methods over MCP. Database paths never enter model arguments.
    """

    def __init__(self, sql_skill, databases: dict[str, Path],
                 knowledge: EnterpriseKnowledgeBase | None = None):
        self.sql_skill = sql_skill
        self.databases = {key: Path(value).resolve() for key, value in databases.items()}
        self.knowledge = knowledge or EnterpriseKnowledgeBase()

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [
            self._schema("search_business_metrics", "检索销售额、客单价、退款率等企业指标的定义和 SQL 口径。",
                         {"query": {"type": "string", "description": "用户问题或需要解释的业务术语"}}, ["query"]),
            self._schema("inspect_database_schema", "查看当前数据库的真实表、字段、主外键；生成 SQL 前必须调用。",
                         {"table_names": {"type": "array", "items": {"type": "string"}, "description": "可选的候选表名；空数组表示返回所有表的精简结构"}}, []),
            self._schema("run_text_to_sql", "调用 DeepEye RAG Text-to-SQL Skill：值检索、Few-shot、Schema Linking、生成、修订、选择并只读执行。",
                         {"question": {"type": "string", "description": "需要查询数据库的完整自然语言问题"},
                          "evidence": {"type": "string", "description": "已经确认的业务口径，可为空"}}, ["question"]),
            self._schema("build_visualization", "根据 SQL 查询结果和分析意图生成指标卡、柱状图、折线图、饼图或表格配置。",
                         {"intent": {"type": "string", "enum": ["query", "ranking", "trend", "proportion"], "description": "结果展示意图"}}, ["intent"]),
            self._schema("get_capabilities", "查询当前 Agent 支持的分析能力，不访问数据库。", {}, []),
        ]

    @staticmethod
    def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict[str, Any]:
        return {"type": "function", "function": {"name": name, "description": description,
                "parameters": {"type": "object", "properties": properties, "required": required,
                               "additionalProperties": False}}}

    def execute(self, name: str, arguments: dict[str, Any], state: dict[str, Any],
                on_stage: Callable[[dict], None] | None = None) -> ToolOutcome:
        handlers = {
            "search_business_metrics": self._search_metrics,
            "inspect_database_schema": self._inspect_schema,
            "run_text_to_sql": self._run_text_to_sql,
            "build_visualization": self._build_visualization,
            "get_capabilities": self._get_capabilities,
        }
        if name not in handlers:
            return ToolOutcome(json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False))
        return handlers[name](arguments, state, on_stage)

    def _search_metrics(self, arguments, state, _on_stage) -> ToolOutcome:
        metrics = self.knowledge.retrieve(arguments.get("query", state["question"]), limit=4)
        evidence = self.knowledge.as_evidence(metrics)
        original = state.get("evidence", "").strip()
        combined = "\n".join(part for part in (original, evidence) if part)
        payload = [{"name": item["name"], "definition": item["definition"], "sql_rule": item["sql_rule"]}
                   for item in metrics]
        return ToolOutcome(json.dumps({"metrics": payload}, ensure_ascii=False),
                           {"business_context": metrics, "evidence": combined})

    def _inspect_schema(self, arguments, state, _on_stage) -> ToolOutcome:
        database_id = state["database_id"]
        requested = arguments.get("table_names") or []
        schema = self.inspect_database_schema(database_id, requested)
        return ToolOutcome(json.dumps(schema, ensure_ascii=False), {"schema_context": schema})

    def inspect_database_schema(self, database_id: str, table_names: list[str] | None = None) -> dict[str, Any]:
        if database_id not in self.databases:
            raise ValueError(f"Unknown database ID: {database_id}")
        with sqlite3.connect(f"file:{self.databases[database_id].as_posix()}?mode=ro", uri=True) as connection:
            available = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            selected = [name for name in (table_names or available) if name in available][:12]
            tables = []
            for table in selected:
                escaped = table.replace('"', '""')
                columns = [{"name": row[1], "type": row[2], "nullable": not bool(row[3]), "primary_key": bool(row[5])}
                           for row in connection.execute(f'PRAGMA table_info("{escaped}")')]
                foreign_keys = [{"from": row[3], "to_table": row[2], "to_column": row[4]}
                                for row in connection.execute(f'PRAGMA foreign_key_list("{escaped}")')]
                tables.append({"name": table, "columns": columns, "foreign_keys": foreign_keys})
        return {"database_id": database_id, "available_tables": available, "tables": tables}

    def _run_text_to_sql(self, arguments, state, on_stage) -> ToolOutcome:
        existing = state.get("skill_result")
        if existing:
            return ToolOutcome(json.dumps({"status": existing.get("status"), "note": "本轮已调用过该工具"}, ensure_ascii=False))
        request = dict(state["request"])
        request["question"] = arguments.get("question") or state["question"]
        supplied = arguments.get("evidence", "").strip()
        request["evidence"] = "\n".join(part for part in (state.get("evidence", "").strip(), supplied) if part)
        result = self.sql_skill.run(request, on_stage=on_stage).model_dump()
        compact = {key: result.get(key) for key in ("status", "sql", "columns", "rows", "truncated", "elapsed_seconds", "failed_stage", "error")}
        if len(compact.get("rows") or []) > 20:
            compact["rows"] = compact["rows"][:20]
            compact["preview_note"] = "仅向 Agent 返回前20行，完整结果保留在应用响应中"
        return ToolOutcome(json.dumps(compact, ensure_ascii=False, default=str), {"skill_result": result})

    def _build_visualization(self, arguments, state, _on_stage) -> ToolOutcome:
        result = state.get("skill_result") or {}
        if result.get("status") not in {"success", "empty_result"}:
            return ToolOutcome(json.dumps({"error": "必须先成功调用 run_text_to_sql"}, ensure_ascii=False))
        intent = arguments.get("intent", "query")
        chart = build_visualization(intent, result.get("columns", []), result.get("rows", []))
        return ToolOutcome(json.dumps({"visualization": chart}, ensure_ascii=False, default=str),
                           {"visualization": chart, "intent": intent})

    @staticmethod
    def _get_capabilities(_arguments, _state, _on_stage) -> ToolOutcome:
        capabilities = {
            "analysis": ["查数", "排名", "趋势", "占比"],
            "metrics": ["销售额", "净销售额", "订单量", "平均客单价", "销量", "退款率", "新客户数"],
            "dimensions": ["商品", "分类", "地区", "渠道", "月份"],
            "safety": "只读 SQL、执行超时、返回行数限制",
        }
        return ToolOutcome(json.dumps(capabilities, ensure_ascii=False))
