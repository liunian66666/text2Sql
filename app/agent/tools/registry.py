"""Tool dependency registry and transport-neutral implementations."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import hashlib
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
from app.agent.capabilities import (
    CAPABILITY_LABELS, describe_database, required_capabilities)
from app.agent.tools.probe import ProbeRunner, probe_cache_key

# 一轮分析允许的业务 SQL 次数。旧实现只允许 1 次，导致一次廉价的日期探查
# 就把整轮唯一的 SQL 预算耗尽，真正的经营分析 SQL 根本没有机会执行。
MAX_SQL_GENERATIONS_PER_TURN = 3
MAX_PROBES_PER_TURN = 6
# 子结果只保留预览行：全量 rows 进 state 会撑大 checkpoint 与最终响应，
# 而真正的聚合应当发生在 SQL 里；这里保留「结构 + 行数 + 样本」用于审计与追问。
SUB_RESULT_PREVIEW_ROWS = 50

# 「确定性元数据事实」不应走完整 Text-to-SQL 流水线。
_METADATA_PROBE_HINTS = (
    "最小值和最大值", "最小日期", "最大日期", "日期范围", "时间范围", "数据覆盖",
    "覆盖的年份", "有哪些表", "是否存在表", "字段是否存在", "表是否存在", "总共有多少行",
)



@dataclass
class ToolOutcome:
    content: str
    updates: dict[str, Any] = field(default_factory=dict)
    status: Literal["completed", "failed"] = "completed"
    error: str | None = None


def _question_key(question: str) -> str:
    """Normalize a sub-question so trivial rewording is still detected as repeat."""
    return "".join(ch for ch in question.casefold() if ch.isalnum())[:200]


def tool_call_signature(name: str, arguments: dict[str, Any]) -> str:
    """Stable fingerprint of one tool call: name + canonicalised arguments.

    Deliberately *not* semantic: this layer only catches byte-identical repeats.
    Reworded-but-identical questions are handled by each tool's own key, so the
    two layers do not fight each other.
    """
    payload = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{name}:{digest}"


# 同一个完全相同的调用允许出现两次（偶发的重试），第三次判定为循环。
MAX_IDENTICAL_TOOL_CALLS = 3


def _is_json_object(raw: str) -> bool:
    try:
        return isinstance(json.loads(raw), dict)
    except (TypeError, ValueError):
        return False


def _short_table(name: str) -> str:
    """`adventureworks.Sales.SalesOrderHeader` → `SalesOrderHeader`."""
    return str(name).rsplit(".", 1)[-1]


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
        from .join_path_tool import create_join_path_tool
        from .metric_tool import create_metric_tool
        from .source_tool import create_source_tool
        from .schema_tool import create_schema_tool
        from .text2sql_tool import create_text2sql_tool
        from .probe_tool import create_probe_tool

        return [
            create_source_tool(self),
            create_metric_tool(self),
            create_schema_tool(self),
            create_probe_tool(self),
            create_join_path_tool(self),
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
        turn_id = state["turn_id"]
        # 通用循环防线（Infra 层）：同一工具 + 完全相同的参数。
        # 第 2 次复用上次结果、不再执行；第 3 次判定循环并写终止原因。
        signature = tool_call_signature(name, arguments)
        seen = dict(state.get("tool_calls_seen") or {})
        entry = seen.get(signature)
        if not isinstance(entry, dict) or entry.get("turn_id") != turn_id:
            entry = None
        count = int((entry or {}).get("count", 0)) + 1
        if entry is not None and count >= MAX_IDENTICAL_TOOL_CALLS:
            error = (f"同一工具与完全相同的参数已连续调用 {count - 1} 次，结果不会变化。"
                     "禁止继续重试，请换参数、换工具，或用已有结果直接回答")
            if callback:
                callback({"stage": "agent_tool", "status": "failed", "tool": name,
                          "reason": "loop_detected"})
            return Command(update={
                "messages": [ToolMessage(
                    content=json.dumps({"error": error, "loop_detected": True}, ensure_ascii=False),
                    tool_call_id=tool_call_id, name=name, status="error")],
                "tool_trace": [{"turn_id": turn_id, "tool": name, "arguments": arguments,
                                "status": "failed", "error": error, "loop_detected": True}],
                "tool_calls_seen": {signature: {**entry, "count": count}},
                "termination_reason": "loop_detected",
            })
        if (entry is not None and count >= 2
                and entry.get("status") == "completed" and entry.get("content")):
            if callback:
                callback({"stage": "agent_tool", "status": "cached", "tool": name})
            cached = json.loads(entry["content"]) if _is_json_object(entry["content"]) else {}
            cached["cached_tool_result"] = True
            cached["note"] = ("这次调用与上一次完全相同，已直接复用上次结果，没有重新执行；"
                              "请基于该结果继续，不要再次调用同一工具同一参数")
            return Command(update={
                "messages": [ToolMessage(
                    content=json.dumps(cached, ensure_ascii=False, default=str),
                    tool_call_id=tool_call_id, name=name, status="success")],
                "tool_trace": [{"turn_id": turn_id, "tool": name, "arguments": arguments,
                                "status": "completed", "cached": True}],
                "tool_calls_seen": {signature: {**entry, "count": count}},
            })
        try:
            outcome = self.execute(name, arguments, state, callback)
        except Exception as exc:
            error = f"{type(exc).__name__}: tool failed"
            if callback:
                callback({"stage": "agent_tool", "status": "failed", "tool": name})
            return Command(update={
                "messages": [ToolMessage(content=json.dumps({"error": error}, ensure_ascii=False),
                                         tool_call_id=tool_call_id, name=name, status="error")],
                "tool_trace": [{"turn_id": turn_id, "tool": name,
                                "arguments": arguments, "status": "failed", "error": error}],
                "tool_calls_seen": {signature: {"turn_id": turn_id, "tool": name, "count": count,
                                                "status": "failed"}},
            })
        if callback:
            callback({"stage": "agent_tool", "status": outcome.status, "tool": name})
        message_status = "success" if outcome.status == "completed" else "error"
        trace = {"turn_id": turn_id, "tool": name, "arguments": arguments,
                 "status": outcome.status}
        if outcome.error:
            trace["error"] = outcome.error
        return Command(update={
            **outcome.updates,
            "messages": [ToolMessage(content=outcome.content, tool_call_id=tool_call_id,
                                     name=name, status=message_status)],
            "tool_trace": [trace],
            "tool_calls_seen": {signature: {"turn_id": turn_id, "tool": name, "count": count,
                                            "status": outcome.status,
                                            "content": outcome.content}},
        })

    def execute(self, name: str, arguments: dict[str, Any], state: dict[str, Any], on_stage=None) -> ToolOutcome:
        handlers = {
            "select_data_source": self._select_data_source,
            "search_business_metrics": self._search_metrics,
            "inspect_database_schema": self._inspect_schema,
            "probe_database": self._probe_database,
            "inspect_join_path": self._inspect_join_path,
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
        """Recognize a request that only restyles the previous query result.

        长度守卫：真正的改图追问都是短句（“做成柱状图”“换成表格”）。
        长分析题经常同时命中关键词（如“以表格和图表展示分析结果”同时含
        图表词“表格”与追问词“结果”），若不设长度上限会被误判为追问，
        导致 run_text_to_sql 走复用快速路径、Agent 在旧结果上乒乓循环。
        """
        normalized = question.casefold().strip()
        if len(normalized) > 40:
            return False
        chart_terms = ("柱状图", "折线图", "饼图", "指标卡", "表格", "bar chart", "line chart", "pie chart")
        followup_terms = ("做成", "改成", "改为", "换成", "显示成", "展示成", "刚才", "上一", "上面", "结果")
        return any(term in normalized for term in chart_terms) and any(
            term in normalized for term in followup_terms)

    def database_capabilities(self) -> dict:
        """Lazily introspect every whitelisted database once per registry."""
        cache = getattr(self, "_capability_cache", None)
        if cache is None:
            cache = {}
            self._capability_cache = cache
        for database_id, path in self.databases.items():
            if database_id not in cache:
                cache[database_id] = describe_database(database_id, path)
        return cache

    def _select_data_source(self, arguments, state, _on_stage) -> ToolOutcome:
        selected = arguments.get("database_id", "").strip()
        requested = state.get("requested_database_id", state.get("database_id", "auto"))
        if selected not in self.databases:
            error = f"未知数据源：{selected}"
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               status="failed", error=error)
        if requested != "auto" and selected != requested:
            # 不记失败：失败会白耗一轮工具预算，模型也会误以为数据源不可用。
            return ToolOutcome(json.dumps({
                "database_id": requested,
                "selection_mode": "manual",
                "note": f"用户已手动指定数据源 {requested}，数据源固定不变；请直接基于它继续分析，无需再选库",
            }, ensure_ascii=False))
        # 能力优先于「业务域关键词」：问题要求历史成本等能力时，能支撑它的数据源
        # 才是对的。此前 销售→enterprise_sales 的关键词启发式会把模型选对的
        # adventureworks 判成错误，白耗一轮（案例 C 的真实回归暴露）。
        required = required_capabilities(state.get("question", ""))
        explicit = self.explicit_question_domain(state.get("question", ""))
        if required and explicit:
            capabilities = self.database_capabilities()
            if required - capabilities[explicit].capabilities:
                explicit = None
        if requested == "auto" and explicit and selected != explicit:
            error = f"所选数据源与问题中的明确业务域不一致，应选择 {explicit}"
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               status="failed", error=error)

        # 能力路由：问题明确要求历史成本等能力时，必须按真实 Schema 判断数据源
        # 是否支持。自动模式下可切换到能支撑的数据源；手动指定则如实报告缺失，
        # 绝不擅自改库，也绝不在旧库里编造不存在的表。
        capability_note: str | None = None
        capability_gap: dict[str, str] = {}
        if required:
            capabilities = self.database_capabilities()
            missing = sorted(capability for capability in required
                             if not capabilities[selected].supports(capability))
            if missing:
                candidates = [database_id for database_id in self.databases
                              if required <= capabilities[database_id].capabilities]
                if requested == "auto" and candidates:
                    target = explicit if explicit in candidates else candidates[0]
                    capability_note = (
                        f"数据源 {selected} 缺少能力：{'、'.join(CAPABILITY_LABELS.get(m, m) for m in missing)}；"
                        f"已按真实 Schema 能力切换为 {target}，请以该数据源继续分析。")
                    selected = target
                else:
                    capability_gap = {
                        capability: capabilities[selected].missing_tables_hint(capability)
                        for capability in missing}
                    capability_note = (
                        f"数据源 {selected} 不支持：{'、'.join(CAPABILITY_LABELS.get(m, m) for m in missing)}。"
                        + " ".join(capability_gap.values())
                        + " 请如实向用户说明该能力缺失，禁止编造不存在的表或字段，"
                          "也不要把当前标准成本当作历史成本。")

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
                "probe_results": [],
                "probe_cache": {},
            })
        payload = {
            "database_id": selected,
            "selection_mode": "automatic" if requested == "auto" else "manual",
            "reused_previous_result": reuse_previous,
            "required_capabilities": sorted(required),
        }
        if capability_note:
            payload["capability_note"] = capability_note
        if capability_gap:
            payload["capability_gap"] = capability_gap
            updates["capability_gap"] = capability_gap
            # 真缺口（无法自动切换到具备该能力的数据源）必须登记终止原因，
            # 否则模型在缺表的情况下直接总结回答，结果仍会被标成 success。
            updates["termination_reason"] = "capability_gap"
        return ToolOutcome(json.dumps(payload, ensure_ascii=False), updates)

    def _search_metrics(self, arguments, state, _on_stage) -> ToolOutcome:
        database_id = state.get("database_id")
        if database_id not in self.databases:
            error = "检索业务指标前必须先选择明确的数据源"
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               status="failed", error=error)
        query = arguments.get("query", state["question"])
        cache_key = _question_key(query)
        cached = (state.get("metrics_cache") or {}).get(cache_key)
        if cached is not None:
            return ToolOutcome(json.dumps({
                "cached": True, "metrics": cached,
                "note": "同一口径检索本轮已执行过，结果持续有效，直接复用",
            }, ensure_ascii=False))
        search = self.search_business_metrics(query, database_id=database_id)
        metrics = search["metrics"]
        evidence = self.knowledge.as_evidence(metrics)
        original = state.get("trusted_evidence", "").strip()
        combined = "\n".join(part for part in (original, evidence) if part)
        payload = [{"name": item["name"], "database_id": item.get("database_id"),
                    "definition": item["definition"], "formula": item.get("formula"),
                    "time_grain": item.get("time_grain"), "sql_rule": item["sql_rule"],
                    "retrieval": item.get("retrieval", {})}
                   for item in metrics]
        metrics_cache = {**(state.get("metrics_cache") or {}), cache_key: payload}
        return ToolOutcome(json.dumps({"metrics": payload, "retrieval": search["retrieval"]}, ensure_ascii=False), {
            "business_context": metrics,
            "business_context_turn_id": state["turn_id"],
            "trusted_evidence": combined,
            "metrics_cache": metrics_cache,
        })

    def inspect_database_schema(self, database_id: str,
                                table_names: list[str] | None = None) -> dict[str, Any]:
        if database_id not in self.databases:
            raise ValueError(f"Unknown database ID: {database_id}")
        # Phase 1: PRAGMA parsing lives in exactly one place. The compact view
        # below is byte-for-byte the same shape the LLM tool has always returned;
        # app.knowledge.schema_extractor consumes the richer RawTable view.
        from app.knowledge.sqlite_metadata import compact_view, list_tables, read_table

        with closing(sqlite3.connect(
                f"file:{self.databases[database_id].as_posix()}?mode=ro", uri=True)) as connection:
            available = list_tables(connection)
            selected = [name for name in (table_names or available) if name in available][:12]
            tables = [compact_view(read_table(connection, table)) for table in selected]
        return {"database_id": database_id, "available_tables": available, "tables": tables}

    def _inspect_schema(self, arguments, state, _on_stage) -> ToolOutcome:
        tables = arguments.get("table_names") or []
        key = ",".join(sorted(tables)) or "*"
        cache = state.get("schema_cache") or {}
        if key in cache:
            return ToolOutcome(json.dumps({
                "cached": True,
                "tables": [table.get("name") for table in cache[key].get("tables", [])],
                "note": "同一范围的 Schema 本轮已获取过，直接复用，不要重复调用 inspect_database_schema",
            }, ensure_ascii=False))
        schema = self.inspect_database_schema(state["database_id"], tables)
        return ToolOutcome(json.dumps(schema, ensure_ascii=False), {
            "schema_context": schema,
            "schema_context_database_id": state["database_id"],
            "schema_cache": {**cache, key: schema},
        })

    def run_text_to_sql_request(self, request: dict[str, Any], *, on_stage=None) -> dict[str, Any]:
        return self.sql_skill.run(request, on_stage=on_stage).model_dump()

    def _probe_database(self, arguments, state, _on_stage) -> ToolOutcome:
        """受控只读探查：表/字段必须来自真实 Schema，SQL 由模板生成。"""
        database_id = state.get("database_id")
        if database_id not in self.databases:
            error = "探查数据库事实前必须先选择明确的数据源"
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               status="failed", error=error)
        probes = state.get("probe_results") or []
        if len(probes) >= MAX_PROBES_PER_TURN:
            error = f"本轮探查次数已达上限（{MAX_PROBES_PER_TURN} 次），请直接使用已有探查结果"
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               {"termination_reason": "probe_limit"},
                               status="failed", error=error)
        probe = str(arguments.get("probe") or "").strip()
        table = str(arguments.get("table") or "").strip()
        column = str(arguments.get("column") or "").strip()
        limit = int(arguments.get("limit") or 10)
        cache = state.get("probe_cache") or {}
        key = probe_cache_key(probe, table, column, limit)
        if key in cache:
            return ToolOutcome(json.dumps({
                "probe": probe, "cached": True, "result": cache[key],
                "note": "相同探查本轮已执行过，直接复用缓存结果，不要重复探查",
            }, ensure_ascii=False, default=str))
        capabilities = self.database_capabilities()
        runner = ProbeRunner(database_id, self.databases[database_id],
                            capabilities[database_id].tables)
        try:
            payload = runner.run(probe, table, column, limit)
        except ValueError as exc:
            error = str(exc)
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               status="failed", error=error)
        return ToolOutcome(json.dumps(payload, ensure_ascii=False, default=str), {
            "probe_results": [payload],
            "probe_cache": {**cache, key: payload},
        })

    @staticmethod
    def looks_like_metadata_probe(question: str) -> bool:
        """短问句且只问元数据事实时，应走 probe_database 而不是完整流水线。"""
        normalized = question.casefold()
        if len(normalized) > 60:
            return False
        business_terms = ("统计", "汇总", "分组", "排名", "同比", "毛利", "占比", "趋势", "增长率")
        if any(term in normalized for term in business_terms):
            return False
        return any(hint in normalized for hint in _METADATA_PROBE_HINTS)

    def _inspect_join_path(self, arguments, state, _on_stage) -> ToolOutcome:
        """把知识图暴露成可查询能力：给一组表，返回候选 FK 路径与风险。

        这是「图状态」而非「图提示」的关键一步 —— 模型可以主动问图，而不是
        被动接收一段拼好的文字。状态里只存引用与结论，networkx 对象留在
        进程级单例里。
        """
        database_id = state.get("database_id")
        if database_id != "adventureworks":
            error = ("连接路径图目前只覆盖 adventureworks；当前数据源没有图产物。"
                     "请改用 inspect_database_schema 返回的真实外键信息")
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               status="failed", error=error)
        tables = [str(item).strip() for item in (arguments.get("tables") or [])
                  if str(item).strip()]
        if len(tables) < 2:
            error = "至少提供两张表才能计算连接路径"
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               status="failed", error=error)
        cache_key = "|".join(sorted(item.casefold() for item in tables))
        cache = state.get("join_path_cache") or {}
        if cache_key in cache:
            payload = dict(cache[cache_key])
            payload["cached"] = True
            payload["note"] = "同一表组合本轮已查询过，结果持续有效，直接复用"
            return ToolOutcome(json.dumps(payload, ensure_ascii=False, default=str))
        try:
            from app.knowledge.graph_store import GraphStore
            from app.knowledge.retrieval.join_path import JoinPathFinder

            store = GraphStore.from_artifacts(database_id="adventureworks")
            # 模型给的是短名（SalesOrderHeader）或 Schema.Table；图里存的是
            # 完整节点 id。按「末段表名」做唯一映射，避免模型必须背全路径。
            known: dict[str, str] = {}
            for node in store.table_projection().nodes:
                known.setdefault(_short_table(node).casefold(), node)
            resolved = [known.get(_short_table(item).casefold(), item) for item in tables]
            unknown = [item for item in tables
                       if _short_table(item).casefold() not in known]
            if unknown:
                error = (f"图中不存在这些表：{'、'.join(unknown)}。"
                         "请先用 inspect_database_schema 确认真实表名")
                return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                                   status="failed", error=error)
            result = JoinPathFinder(store, max_hops=4, max_paths=6).find(resolved)
        except FileNotFoundError as exc:
            error = f"知识图产物缺失，无法查询连接路径：{exc}"
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               status="failed", error=error)
        paths = []
        for path in result.paths[:6]:
            paths.append({
                "tables": [_short_table(item) for item in path.tables],
                "hops": path.hops,
                "score": round(float(path.score), 2),
                "relation_ids": list(path.relation_ids),
                "composite_fk_complete": path.composite_fk_complete,
                "join_conditions": [condition.model_dump() for condition in path.join_conditions],
                "risks": list(path.risks),
            })
        payload = {
            "database_id": "adventureworks",
            "requested_tables": tables,
            "paths": paths,
            "ambiguity": list(result.ambiguity[:5]),
            "risks": list(result.risks[:8]),
            "excluded": list(result.excluded_paths[:5]),
            "guidance": ("优先选择复合外键完整、跳数最少的路径；"
                         "风险项必须按 required_mitigation 规避，不得只按单列关联"),
        }
        return ToolOutcome(json.dumps(payload, ensure_ascii=False, default=str), {
            "join_path_context": {"turn_id": state["turn_id"], "tables": tables,
                                  "paths": paths, "risks": payload["risks"]},
            "join_path_cache": {cache_key: payload},
        })

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
        question = str(arguments.get("question") or state["question"]).strip()
        if self.looks_like_metadata_probe(question):
            error = ("这类确定性元数据事实（日期覆盖/行数/字段是否存在）请改用 probe_database 工具，"
                     "不要占用业务 SQL 预算，也不要走完整 Text-to-SQL 流水线")
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               status="failed", error=error)
        history = state.get("sql_history") or []
        key = _question_key(question)
        repeated = next((index + 1 for index, item in enumerate(history) if item.get("key") == key), None)
        if repeated:
            return ToolOutcome(json.dumps({
                "status": previous.get("status"),
                "repeated_sql": True,
                "note": f"该子问题本轮已执行过（第 {repeated} 次），结果已在上下文里，禁止重复生成 SQL；"
                        "请推进下一个分析步骤，或直接汇总回答",
            }, ensure_ascii=False))
        generations = int(state.get("sql_generations", 0))
        if generations >= MAX_SQL_GENERATIONS_PER_TURN:
            error = (f"本轮业务 SQL 已达上限（{MAX_SQL_GENERATIONS_PER_TURN} 次）。"
                     "请用已执行结果汇总回答，若确实还缺关键数据，请说明缺少什么")
            return ToolOutcome(json.dumps({"error": error}, ensure_ascii=False),
                               {"termination_reason": "sql_budget_limit"},
                               status="failed", error=error)
        request = dict(state["request"])
        request["question"] = question
        intent = self.normalize_intent(state["question"], arguments["intent"])
        evidence_parts = []
        trusted = state.get("trusted_evidence", "").strip()
        user_hint = state.get("user_hint", "").strip()
        if trusted:
            evidence_parts.append(f"[系统业务口径]\n{trusted}")
        if user_hint:
            evidence_parts.append(f"[用户补充说明，未经系统验证]\n{user_hint}")
        # Knowledge-layer input constraint only. DeepEye-SQL's schema linking and
        # SQL generation are untouched; the hint just tells the generator that
        # this port keeps SQL Server "Schema.Table" names and TEXT decimal values.
        from app.enterprise.adventureworks import sql_dialect_hint
        dialect = sql_dialect_hint(state.get("database_id"))
        if dialect:
            evidence_parts.append(dialect)
        # Phase 2/3 optional knowledge enhancement. OFF by default; when ON it
        # only appends a hint to the existing evidence string. The original
        # database_schema, trusted_evidence, SQL safety constraints and every
        # upstream pipeline stage remain untouched.
        #
        # Phase 4B: 检索通道默认仍是关键词；设 KNOWLEDGE_ENHANCEMENT_DENSE=local
        # 才接入本地 Qwen3 向量（本地推理，不消耗在线配额）。之所以做成显式开关，
        # 是因为首次加载本地模型约 50 秒，不能让 Web 首请求隐式承担。
        from app.knowledge.enhancement import (
            build_enhancement, dense_backend_from_env, is_enabled as knowledge_enabled)
        if knowledge_enabled() and state.get("database_id") == "adventureworks":
            dense_backend = dense_backend_from_env()
            enhancement = build_enhancement(
                request["question"], state["database_id"], on_stage=on_stage,
                mode="hybrid" if dense_backend is not None else "keyword",
                dense_backend=dense_backend)
            if enhancement.evidence_text:
                evidence_parts.append(enhancement.evidence_text)
            if on_stage:
                on_stage({"stage": "knowledge_enhancement", "status": "completed",
                          "database_id": state["database_id"],
                          "enabled": enhancement.enabled, "error": enhancement.error,
                          "observability": enhancement.observability})
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
        # 每条业务 SQL 都留一条可追溯记录。复合任务会被拆成多条 SQL，
        # 若只保留最后一次，前序结果就彻底消失，用户与审计都无法还原分析过程。
        # 图校验：若本轮查过连接路径，就把 SQL 实际用到的表和 JOIN 与图对账。
        # 结论只写进结果元数据与模型可见的提示，不直接拒绝执行 ——
        # 「与图不一致」不等于「业务口径错」，需要模型或人判断。
        join_context = state.get("join_path_context") or {}
        join_warnings: list[dict[str, Any]] = []
        if join_context.get("turn_id") == state.get("turn_id") and result.get("sql"):
            from app.agent.join_guard import validate_sql_joins

            join_warnings = validate_sql_joins(result["sql"], join_context)
            if join_warnings:
                compact["join_path_warnings"] = join_warnings
                compact["join_path_hint"] = (
                    "上面这些项与知识图里声明的连接路径不一致。"
                    "如果是普通多表关联，请按图里的 join_conditions 重写；"
                    "如果是有意的额外关联，请在回答里说明理由。")
        all_rows = list(result.get("rows") or [])
        sub_result = {
            "turn_id": state["turn_id"],
            "question": question,
            "sql": result.get("sql"),
            "status": result.get("status"),
            "database_id": state.get("database_id"),
            "columns": list(result.get("columns") or []),
            "row_count": len(all_rows),
            "preview_rows": all_rows[:SUB_RESULT_PREVIEW_ROWS],
            "preview_truncated": len(all_rows) > SUB_RESULT_PREVIEW_ROWS,
            "truncated": bool(result.get("truncated")),
            "failed_stage": result.get("failed_stage"),
            "error": result.get("error"),
            "join_path_warnings": join_warnings,
        }
        return ToolOutcome(json.dumps(compact, ensure_ascii=False, default=str), {
            "sql_result": result,
            "sql_result_turn_id": state["turn_id"],
            "sql_result_database_id": state["database_id"],
            "sql_results": [sub_result],
            "sql_results_turn_id": state["turn_id"],
            "analysis_intent": intent,
            "sql_generations": generations + 1,
            "sql_history": [{"key": key, "question": question,
                             "status": result.get("status"),
                             "sql": result.get("sql")}],
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

    def capabilities(self) -> dict[str, Any]:
        """Report only what the whitelisted databases really contain."""
        described = self.database_capabilities()
        return {
            "analysis": ["查数", "排名", "趋势", "占比"],
            "data_sources": list(self.databases),
            "database_capabilities": {
                database_id: {
                    "capabilities": sorted(item.capabilities),
                    "evidence": item.evidence,
                    "table_count": len(item.tables),
                } for database_id, item in described.items()
            },
            "metrics": ["销售额", "净销售额", "退款率", "新客户数", "在职人数", "离职率", "平均绩效", "出勤率"],
            "dimensions": ["商品", "分类", "地区", "渠道", "部门", "月份"],
            "safety": "只读 SQL、执行超时、返回行数限制、探查白名单",
        }

    def _get_capabilities(self, _arguments, _state, _on_stage) -> ToolOutcome:
        return ToolOutcome(json.dumps(self.capabilities(), ensure_ascii=False))
