"""Tool dependency registry and transport-neutral implementations."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.types import Command

from app.enterprise import EnterpriseKnowledgeBase
from app.agent.visualization import MAX_SLICES, build_visualization


@dataclass
class ToolOutcome:
    content: str
    updates: dict[str, Any] = field(default_factory=dict)
    status: Literal["completed", "failed"] = "completed"
    error: str | None = None


class ToolRegistry:
    """Own runtime dependencies while exposing only safe business arguments."""

    def __init__(self, sql_skill, databases: dict[str, Path],
                 knowledge: EnterpriseKnowledgeBase | None = None):
        self.sql_skill = sql_skill
        self.databases = {key: Path(value).resolve() for key, value in databases.items()}
        self.knowledge = knowledge or EnterpriseKnowledgeBase()

    def as_langchain_tools(self) -> list[BaseTool]:
        from .capability_tool import create_capability_tool
        from .chart_tool import create_chart_tool
        from .metric_tool import create_metric_tool
        from .source_tool import create_source_tool
        from .schema_tool import create_schema_tool
        from .text2sql_tool import create_text2sql_tool

        return [
            create_source_tool(self),
            create_metric_tool(self),
            create_schema_tool(self),
            create_text2sql_tool(self),
            create_chart_tool(self),
            create_capability_tool(self),
        ]

    def invoke(self, name: str, arguments: dict[str, Any], state: dict[str, Any],
               config: RunnableConfig | None, tool_call_id: str) -> Command:
        runtime_config = config or {}
        callback = runtime_config.get("configurable", {}).get("on_stage")
        if callback:
            callback({"stage": "agent_tool", "status": "started", "tool": name})
        try:
            outcome = self.execute(name, arguments, state, callback)
        except Exception as exc:
            error = f"{type(exc).__name__}: tool failed"
            if callback:
                callback({"stage": "agent_tool", "status": "failed", "tool": name})
            return Command(update={
                "messages": [ToolMessage(content=json.dumps({"error": error}, ensure_ascii=False),
                                         tool_call_id=tool_call_id, name=name, status="error")],
                "tool_trace": [{"turn_id": state["turn_id"], "tool": name,
                                "arguments": arguments, "status": "failed", "error": error}],
            })
        if callback:
            callback({"stage": "agent_tool", "status": outcome.status, "tool": name})
        message_status = "success" if outcome.status == "completed" else "error"
        trace = {"turn_id": state["turn_id"], "tool": name, "arguments": arguments,
                 "status": outcome.status}
        if outcome.error:
            trace["error"] = outcome.error
        return Command(update={
            **outcome.updates,
            "messages": [ToolMessage(content=outcome.content, tool_call_id=tool_call_id,
                                     name=name, status=message_status)],
            "tool_trace": [trace],
        })

    def execute(self, name: str, arguments: dict[str, Any], state: dict[str, Any], on_stage=None) -> ToolOutcome:
        handlers = {
            "select_data_source": self._select_data_source,
            "search_business_metrics": self._search_metrics,
            "inspect_database_schema": self._inspect_schema,
            "run_text_to_sql": self._run_text_to_sql,
            "build_visualization": self._build_visualization,
            "get_capabilities": self._get_capabilities,
        }
        if name not in handlers:
            raise ValueError(f"Unknown tool: {name}")
        return handlers[name](arguments, state, on_stage)

    def retrieve_business_metrics(self, query: str, *, limit: int = 4,
                                  database_id: str | None = None) -> list[dict[str, Any]]:
        return self.knowledge.retrieve(query, limit=max(1, min(limit, 8)), database_id=database_id)

    def search_business_metrics(self, query: str, *, limit: int = 4,
                                database_id: str | None = None) -> dict[str, Any]:
        return self.knowledge.search(query, limit=max(1, min(limit, 8)), database_id=database_id)

    @staticmethod
    def explicit_question_domain(question: str) -> str | None:
        """Return only strong domain signals; ambiguous wording stays LLM-routed."""
        normalized = question.casefold()
        hr_terms = ("员工", "部门", "人力", "hr", "入职", "离职", "绩效", "出勤", "迟到", "请假", "职级")
        sales_terms = ("销售", "订单", "商品", "客户", "退款", "营收", "gmv", "客单价", "销量", "渠道")
        hr_hit = any(term in normalized for term in hr_terms)
        sales_hit = any(term in normalized for term in sales_terms)
        if hr_hit == sales_hit:
            return None
        return "enterprise_hr" if hr_hit else "enterprise_sales"

    @staticmethod
    def normalize_intent(question: str, intent: str) -> str:
        """Enforce the documented boundary: ordering alone is not ranking."""
        if intent != "ranking":
            return intent
        normalized = question.casefold()
        ranking_cues = ("排名", "最高", "最低", "top ", "topn", "top n", "前几", "前10", "前十", "第几")
        return "ranking" if any(cue in normalized for cue in ranking_cues) else "query"

    @staticmethod
    def is_visualization_followup(question: str) -> bool:
        """Recognize a request that only restyles the previous query result."""
        normalized = question.casefold().strip()
        chart_terms = ("柱状图", "折线图", "饼图", "指标卡", "表格", "bar chart", "line chart", "pie chart")
        followup_terms = ("做成", "改成", "改为", "换成", "显示成", "展示成", "刚才", "上一", "上面", "结果")
        return any(term in normalized for term in chart_terms) and any(
            term in normalized for term in followup_terms)

    def _select_data_source(self, arguments, state, _on_stage) -> ToolOutcome:
        selected = arguments.get("database_id", "").strip()
        requested = state.get("requested_database_id", state.get("database_id", "auto"))
        if selected not in self.databases:
            error = f"未知数据源：{selected}"
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               status="failed", error=error)
        if requested != "auto" and selected != requested:
            error = f"用户已手动指定数据源 {requested}，不能自动切换为 {selected}"
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               status="failed", error=error)
        explicit = self.explicit_question_domain(state.get("question", ""))
        if requested == "auto" and explicit and selected != explicit:
            error = f"所选数据源与问题中的明确业务域不一致，应选择 {explicit}"
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               status="failed", error=error)
        runtime_request = dict(state.get("request") or {})
        runtime_request["database_id"] = selected
        reuse_previous = (
            requested == "auto"
            and selected == state.get("sql_result_database_id")
            and (state.get("sql_result") or {}).get("status") in {"success", "empty_result"}
            and self.is_visualization_followup(state.get("question", ""))
        )
        updates = {
            "database_id": selected,
            "request": runtime_request,
        }
        if not reuse_previous:
            updates.update({
                "schema_context": {},
                "schema_context_database_id": "",
                "business_context": [],
                "business_context_turn_id": "",
                "trusted_evidence": "",
                "sql_result": {},
                "sql_result_turn_id": "",
                "sql_result_database_id": "",
            })
        return ToolOutcome(json.dumps({
            "database_id": selected,
            "selection_mode": "automatic" if requested == "auto" else "manual",
            "reused_previous_result": reuse_previous,
        }, ensure_ascii=False), updates)

    def _search_metrics(self, arguments, state, _on_stage) -> ToolOutcome:
        database_id = state.get("database_id")
        if database_id not in self.databases:
            error = "检索业务指标前必须先选择明确的数据源"
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               status="failed", error=error)
        search = self.search_business_metrics(
            arguments.get("query", state["question"]), database_id=database_id)
        metrics = search["metrics"]
        evidence = self.knowledge.as_evidence(metrics)
        original = state.get("trusted_evidence", "").strip()
        combined = "\n".join(part for part in (original, evidence) if part)
        payload = [{"name": item["name"], "database_id": item.get("database_id"),
                    "definition": item["definition"], "formula": item.get("formula"),
                    "time_grain": item.get("time_grain"), "sql_rule": item["sql_rule"],
                    "retrieval": item.get("retrieval", {})}
                   for item in metrics]
        return ToolOutcome(json.dumps({"metrics": payload, "retrieval": search["retrieval"]}, ensure_ascii=False), {
            "business_context": metrics,
            "business_context_turn_id": state["turn_id"],
            "trusted_evidence": combined,
        })

    def inspect_database_schema(self, database_id: str,
                                table_names: list[str] | None = None) -> dict[str, Any]:
        if database_id not in self.databases:
            raise ValueError(f"Unknown database ID: {database_id}")
        with closing(sqlite3.connect(
                f"file:{self.databases[database_id].as_posix()}?mode=ro", uri=True)) as connection:
            available = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            selected = [name for name in (table_names or available) if name in available][:12]
            tables = []
            for table in selected:
                escaped = table.replace('"', '""')
                columns = [{"name": row[1], "type": row[2], "nullable": not bool(row[3]),
                            "primary_key": bool(row[5])}
                           for row in connection.execute(f'PRAGMA table_info("{escaped}")')]
                foreign_keys = [{"from": row[3], "to_table": row[2], "to_column": row[4]}
                                for row in connection.execute(f'PRAGMA foreign_key_list("{escaped}")')]
                tables.append({"name": table, "columns": columns, "foreign_keys": foreign_keys})
        return {"database_id": database_id, "available_tables": available, "tables": tables}

    def _inspect_schema(self, arguments, state, _on_stage) -> ToolOutcome:
        schema = self.inspect_database_schema(state["database_id"], arguments.get("table_names") or [])
        return ToolOutcome(json.dumps(schema, ensure_ascii=False), {
            "schema_context": schema,
            "schema_context_database_id": state["database_id"],
        })

    def run_text_to_sql_request(self, request: dict[str, Any], *, on_stage=None) -> dict[str, Any]:
        return self.sql_skill.run(request, on_stage=on_stage).model_dump()

    def _run_text_to_sql(self, arguments, state, on_stage) -> ToolOutcome:
        previous = state.get("sql_result") or {}
        if (self.is_visualization_followup(state.get("question", ""))
                and previous.get("status") in {"success", "empty_result"}
                and state.get("sql_result_database_id") == state.get("database_id")):
            return ToolOutcome(json.dumps({
                "status": previous.get("status"),
                "note": "这是图表样式追问，已保留上一轮查询结果；禁止重新生成SQL，请调用build_visualization",
            }, ensure_ascii=False))
        if state.get("schema_context_database_id") != state["database_id"]:
            error = "生成 SQL 前必须先调用 inspect_database_schema"
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               status="failed", error=error)
        if state.get("sql_result_turn_id") == state["turn_id"]:
            return ToolOutcome(json.dumps({"status": state.get("sql_result", {}).get("status"),
                                           "note": "本轮SQL已成功生成，禁止再次调用run_text_to_sql；下一步必须调用build_visualization"}, ensure_ascii=False))
        request = dict(state["request"])
        request["question"] = arguments.get("question") or state["question"]
        intent = self.normalize_intent(state["question"], arguments["intent"])
        evidence_parts = []
        trusted = state.get("trusted_evidence", "").strip()
        user_hint = state.get("user_hint", "").strip()
        if trusted:
            evidence_parts.append(f"[系统业务口径]\n{trusted}")
        if user_hint:
            evidence_parts.append(f"[用户补充说明，未经系统验证]\n{user_hint}")
        request["evidence"] = "\n\n".join(evidence_parts)
        result = self.run_text_to_sql_request(request, on_stage=on_stage)
        compact = {key: result.get(key) for key in (
            "status", "sql", "columns", "rows", "truncated", "elapsed_seconds", "failed_stage", "error")}
        if intent != arguments["intent"]:
            compact["intent_correction"] = {
                "requested": arguments["intent"], "applied": intent,
                "reason": "仅要求升降序不构成排名场景",
            }
        if len(compact.get("rows") or []) > 20:
            compact["rows"] = compact["rows"][:20]
            compact["preview_note"] = "仅向 Agent 返回前20行，完整结果保留在应用响应中"
        return ToolOutcome(json.dumps(compact, ensure_ascii=False, default=str), {
            "sql_result": result,
            "sql_result_turn_id": state["turn_id"],
            "sql_result_database_id": state["database_id"],
            "analysis_intent": intent,
        })

    def _build_visualization(self, arguments, state, _on_stage) -> ToolOutcome:
        result = state.get("sql_result") or {}
        result_database_id = state.get("sql_result_database_id")
        current_database_id = state.get("database_id")
        requested_database_id = state.get("requested_database_id", current_database_id)
        # In auto mode every new turn starts with database_id="auto" while the
        # checkpointer still holds the last executed SQL and its actual source.
        # A chart-only follow-up ("做成柱状图") must be able to reuse that result
        # without rerunning SQL.  Manual source selection remains strict so a
        # result can never leak across an explicit database switch.
        reuse_auto_result = (
            requested_database_id == "auto"
            and current_database_id == "auto"
            and result_database_id in self.databases
        )
        if result_database_id != current_database_id and not reuse_auto_result:
            result = {}
        if result.get("status") not in {"success", "empty_result"}:
            error = "当前会话没有可复用的查询结果，请先成功调用 run_text_to_sql"
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               status="failed", error=error)
        intent = state.get("analysis_intent", "query")
        requested = arguments.get("chart_type")
        chart = build_visualization(intent, result.get("columns", []), result.get("rows", []), requested)
        payload: dict[str, Any] = {"visualization": chart}
        if requested and chart:
            payload["requested_chart_type"] = requested
            payload["chart_type_applied"] = chart["type"]
            if chart["type"] != str(requested).lower():
                payload["note"] = (f"数据不适合 {requested}（饼图最多 {MAX_SLICES} 个分类、"
                                   f"指标卡只支持单行），已改用 {chart['type']}")
        updates = {
            "visualization": chart,
            "visualization_turn_id": state["turn_id"],
        }
        if reuse_auto_result:
            updates["database_id"] = result_database_id
        return ToolOutcome(json.dumps(payload, ensure_ascii=False, default=str), updates)

    @staticmethod
    def capabilities() -> dict[str, Any]:
        return {
            "analysis": ["查数", "排名", "趋势", "占比"],
            "data_sources": ["enterprise_sales", "enterprise_hr"],
            "metrics": ["销售额", "净销售额", "退款率", "新客户数", "在职人数", "离职率", "平均绩效", "出勤率"],
            "dimensions": ["商品", "分类", "地区", "渠道", "部门", "月份"],
            "safety": "只读 SQL、执行超时、返回行数限制",
        }

    def _get_capabilities(self, _arguments, _state, _on_stage) -> ToolOutcome:
        return ToolOutcome(json.dumps(self.capabilities(), ensure_ascii=False))
