from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Literal
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import AliasChoices, BaseModel, Field, field_validator

from .graph import build_agent_graph
from .llm import MeteredChatModel, agent_budget_limits, create_chat_model
from .tools import ToolRegistry
from .visualization import build_visualization, summarize_result
from app.llm.budget import BudgetExceededError, get_meter, reset_meter, start_meter


class ChatRequest(BaseModel):
    """One conversational turn; ``question`` remains a compatible input alias."""

    message: str = Field(min_length=1, validation_alias=AliasChoices("message", "question"))
    database_id: str = Field(default="auto", min_length=1)
    conversation_id: str | None = Field(default=None, max_length=128)
    user_hint: str = Field(default="", validation_alias=AliasChoices("user_hint", "evidence"))
    max_rows: int = Field(default=100, ge=1, le=1000)
    sql_timeout_seconds: int = Field(default=30, ge=1, le=600)

    @field_validator("message", "database_id")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    def as_runtime_request(self) -> dict[str, Any]:
        return {
            "question": self.message,
            "database_id": self.database_id,
            "conversation_id": self.conversation_id,
            "max_rows": self.max_rows,
            "sql_timeout_seconds": self.sql_timeout_seconds,
        }


class AgentResult(BaseModel):
    request_id: str
    # partial 表示「拿到了部分真实结果但没跑完全部要求」，与 success 严格区分：
    # 旧实现里达到工具轮数上限仍返回 success，前端于是显示「分析完成」。
    status: Literal["success", "empty_result", "partial", "error"]
    intent: str
    conversation_id: str
    database_id: str
    answer: str = ""
    visualization: dict[str, Any] | None = None
    business_context: list[dict[str, Any]] = Field(default_factory=list)
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)
    model_calls: int = 0
    agent_usage: dict[str, int] = Field(default_factory=dict)
    sql: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    truncated: bool = False
    elapsed_seconds: float = 0
    # Wall time of the whole Agent request, which is longer than elapsed_seconds:
    # that one is the SQL Skill only, so Planner turns were previously free.
    agent_elapsed_seconds: float = 0
    stage_seconds: dict[str, float] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    failed_stage: str | None = None
    error: str | None = None
    artifacts_dir: str
    # 可观测性：探查事实、结构化计划、业务 SQL 次数、终止原因。
    # probe_results 永远不会并入 sql/rows —— 中间探查不得当作业务结论展示。
    probe_results: list[dict[str, Any]] = Field(default_factory=list)
    analysis_plan: dict[str, Any] | None = None
    sql_generations: int = 0
    # 本轮每条业务 SQL 的逐条记录（问题/SQL/列/行数/预览）。sql/columns/rows
    # 仍只代表最后一次查询，保持既有前端与画图契约；复合任务的全部子结果在
    # analyses 里逐条呈现，不会再被后一条覆盖。
    analyses: list[dict[str, Any]] = Field(default_factory=list)
    # 任务级反思记录：触发原因、决策与轮次。属于可观测性，不参与结论。
    reflection_notes: list[dict[str, Any]] = Field(default_factory=list)
    termination_reason: str | None = None
    capability_gap: dict[str, str] = Field(default_factory=dict)


class DataAnalysisAgent:
    def __init__(self, sql_skill, *, workspace: Path, databases: dict[str, Path],
                 llm_config=None, chat_model=None, knowledge=None, checkpointer=None,
                 llm_budget: dict[str, Any] | None = None):
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.tool_registry = ToolRegistry(sql_skill, databases, knowledge)
        # 所有模型调用（含 planner 与超限总结）都过同一个计量包装：
        # 裸 ChatOpenAI 会让 BudgetMeter 形同虚设，Web 路径没有硬上限。
        self.chat_model = MeteredChatModel(chat_model or create_chat_model(llm_config))
        self.llm_budget = agent_budget_limits(llm_budget)
        self.tools = self.tool_registry.as_langchain_tools()
        self.checkpointer = checkpointer
        self.graph = build_agent_graph(self.chat_model, self.tools, checkpointer=checkpointer)

    @staticmethod
    def thread_id(conversation_id: str, database_id: str) -> str:
        return f"{conversation_id}:{database_id}"

    def delete_conversation(self, conversation_id: str) -> None:
        """Delete every source-scoped checkpoint belonging to one conversation."""
        if self.checkpointer is None or not hasattr(self.checkpointer, "delete_thread"):
            return
        for database_id in {"auto", *self.tool_registry.databases}:
            self.checkpointer.delete_thread(self.thread_id(conversation_id, database_id))

    @staticmethod
    def _current_turn(messages):
        start = 0
        for index in range(len(messages) - 1, -1, -1):
            if isinstance(messages[index], HumanMessage):
                start = index
                break
        return messages[start:]

    @staticmethod
    def _report_orchestration(on_stage, seconds: float, failed: bool) -> None:
        """关闭「规划与编排」这一格；它没有独立的计时器，用残差收尾。"""
        if not on_stage:
            return
        on_stage({"stage": "agent_orchestration", "status": "failed" if failed else "completed",
                  "seconds": round(float(seconds), 3)})

    def _budget_exhausted_result(self, *, conversation_id: str, database_id: str,
                                 started: float, exc: Exception, snapshot: dict,
                                 on_stage) -> AgentResult:
        """预算耗尽也是一种「真实的终止」：明确报错，不伪装成模型故障。"""
        request_id = uuid4().hex
        artifacts = self.workspace / request_id
        artifacts.mkdir(parents=True, exist_ok=False)
        elapsed = round(time.perf_counter() - started, 3)
        self._report_orchestration(on_stage, elapsed, True)
        message = ("本次请求的模型预算已用尽，已安全停止；已完成的部分结果见上方执行记录。"
                   "请缩小问题范围或提高预算后重试。")
        return AgentResult(
            request_id=request_id, status="error", intent="chat",
            conversation_id=conversation_id, database_id=database_id,
            answer=message, artifacts_dir=str(artifacts),
            failed_stage="agent", error=f"模型预算耗尽：{exc}",
            agent_elapsed_seconds=elapsed,
            stage_seconds={"agent_orchestration": elapsed},
            model_calls=int(snapshot.get("calls", 0)),
            usage=dict(snapshot),
            termination_reason="llm_budget_exhausted",
        )

    def run(self, request: ChatRequest | dict, on_stage=None) -> AgentResult:
        validated = ChatRequest.model_validate(request)
        if validated.database_id != "auto" and validated.database_id not in self.tool_registry.databases:
            raise ValueError(f"Unknown database ID: {validated.database_id}")
        conversation_id = validated.conversation_id or uuid4().hex
        turn_id = uuid4().hex
        started = time.perf_counter()
        # 「规划与编排」是包住整个请求的外层循环（Planner 轮次 + 工具调度），
        # 它的耗时是最后用总墙钟减去 Skill 自己的秒表算出来的残差。以前只有
        # 结果里的数字、没有进度事件，前端那一格永远是灰的；这里补一对
        # started/completed，让它在进度条上和别的阶段一样能变绿。
        if on_stage:
            on_stage({"stage": "agent_orchestration", "status": "started"})
        runtime_request = validated.as_runtime_request()
        # Agent 层预算硬闸：与 SQL pipeline 共用同一 BudgetMeter 机制，
        # 每次真实模型调用前检查、后记账。没有它，Web 路径的 token 成本
        # 只靠评测脚本外层兜，产品侧等于无上限。
        meter_token = start_meter(self.llm_budget["max_total_tokens"],
                                  self.llm_budget["max_seconds"],
                                  self.llm_budget["max_calls"])
        try:
            state = self.graph.invoke({
                "messages": [HumanMessage(content=validated.message)],
                "request": runtime_request,
                "turn_id": turn_id,
                "question": validated.message,
                "database_id": validated.database_id,
                "requested_database_id": validated.database_id,
                "available_databases": list(self.tool_registry.databases),
                "user_hint": validated.user_hint,
                "trusted_evidence": "",
                "business_context": [],
                "business_context_turn_id": "",
                "tool_rounds": 0,
                "model_calls": 0,
                "agent_usage": {},
                "agent_error": None,
                "sql_generations": 0,
                "sql_history": [],
                "sql_results": [],
                "sql_results_turn_id": "",
                "probe_results": [],
                "probe_cache": {},
                "schema_cache": {},
                "metrics_cache": {},
                "tool_calls_seen": {},
                "reflection_done": False,
                "reflection_notes": [],
                "join_path_context": {},
                "join_path_cache": {},
                "analysis_plan": None,
                "analysis_plan_turn_id": "",
                "capability_snapshot": self.tool_registry.capabilities().get("database_capabilities", {}),
                "capability_gap": {},
                "termination_reason": None,
            }, config={"configurable": {
                "thread_id": self.thread_id(conversation_id, validated.database_id),
                "on_stage": on_stage,
            }})
        except BudgetExceededError as exc:
            meter = get_meter()
            snapshot = meter.snapshot() if meter is not None else {}
            return self._budget_exhausted_result(
                conversation_id=conversation_id, database_id=validated.database_id,
                started=started, exc=exc, snapshot=snapshot, on_stage=on_stage)
        finally:
            reset_meter(meter_token)
        selected_database_id = state.get("database_id", validated.database_id)
        messages = state.get("messages", [])
        current_messages = self._current_turn(messages)
        current_trace = [
            {key: value for key, value in item.items() if key != "turn_id"}
            for item in state.get("tool_trace", []) if item.get("turn_id") == turn_id
        ]
        visualization = (state.get("visualization")
                         if state.get("visualization_turn_id") == turn_id else None)
        skill = state.get("sql_result") if state.get("sql_result_turn_id") == turn_id else None
        if visualization is not None and skill is None and state.get("sql_result_database_id") == selected_database_id:
            skill = state.get("sql_result")
        last_answer = next((message for message in reversed(current_messages)
                            if isinstance(message, AIMessage) and not message.tool_calls), None)
        answer = last_answer.text if last_answer is not None else ""
        # 终止原因与可观测性字段：partial 状态必须真实反映「没跑完」，
        # 不能被 skill 自带的状态覆盖成 success。
        termination_reason = state.get("termination_reason")
        probe_results = [item for item in (state.get("probe_results") or [])]
        analyses = [
            {key: value for key, value in item.items() if key != "turn_id"}
            for item in (state.get("sql_results") or []) if item.get("turn_id") == turn_id
        ]
        reflection_notes = [
            {key: value for key, value in item.items() if key != "turn_id"}
            for item in (state.get("reflection_notes") or []) if item.get("turn_id") == turn_id
        ]
        analysis_plan = (state.get("analysis_plan")
                         if state.get("analysis_plan_turn_id") == turn_id else None)
        sql_generations = int(state.get("sql_generations", 0))
        capability_gap = state.get("capability_gap") or {}
        if not skill:
            request_id = uuid4().hex
            artifacts = self.workspace / request_id
            artifacts.mkdir(parents=True, exist_ok=False)
            failed_trace = next((item for item in reversed(current_trace) if item.get("status") == "failed"), None)
            failed = bool(state.get("agent_error") or failed_trace)
            error = state.get("agent_error") or (failed_trace or {}).get("error")
            intent = state.get("analysis_intent", "query") if visualization is not None else "chat"
            agent_elapsed = round(time.perf_counter() - started, 3)
            # 没有 SQL Skill 的回合里，总耗时全部是编排本身
            self._report_orchestration(on_stage, agent_elapsed, failed)
            status = "error" if failed else "success"
            if termination_reason:
                # 有任何真实产出 → partial；纯空转 → error。
                # 这条判断以前只覆盖 probe/visualization，导致「反思判定循环、
                # 但本轮没有任何产物」时仍报 success —— 又一个假成功入口。
                if probe_results or visualization or analyses or termination_reason == "capability_gap":
                    status = "partial"
                elif not failed:
                    status = "error"
            return AgentResult(request_id=request_id, status=status, intent=intent,
                               conversation_id=conversation_id, database_id=selected_database_id, answer=answer,
                               visualization=visualization, tool_trace=current_trace,
                               model_calls=state.get("model_calls", 0), agent_usage=state.get("agent_usage", {}),
                               agent_elapsed_seconds=agent_elapsed,
                               stage_seconds={"agent_orchestration": agent_elapsed},
                               failed_stage="agent" if failed else None,
                               error=error, artifacts_dir=str(artifacts),
                               probe_results=probe_results, analysis_plan=analysis_plan,
                               sql_generations=sql_generations,
                               analyses=analyses, reflection_notes=reflection_notes,
                               termination_reason=termination_reason,
                               capability_gap=capability_gap)
        intent = state.get("analysis_intent", "query")
        if visualization is None and skill.get("status") in {"success", "empty_result"}:
            visualization = build_visualization(intent, skill.get("columns", []), skill.get("rows", []))
        answer = answer or summarize_result(intent, skill.get("columns", []), skill.get("rows", []))
        agent_elapsed = round(time.perf_counter() - started, 3)
        stage_seconds = dict(skill.get("stage_seconds") or {})
        # Planner turns and tool orchestration sat outside the Skill's own
        # stopwatch, so a request reported the SQL stages as if they were the
        # whole answer. The residual is measured, not estimated: both endpoints
        # are timed here and the Skill times itself.
        stage_seconds["agent_orchestration"] = round(
            max(agent_elapsed - float(skill.get("elapsed_seconds") or 0.0), 0.0), 3)
        self._report_orchestration(on_stage, stage_seconds["agent_orchestration"],
                                   skill.get("status") not in {"success", "empty_result"})
        # 先合成 payload 再覆盖状态：原先用两个 ** 并列展开，skill 自带 status 时
        # 与 partial 的 status 撞成 "got multiple values for keyword argument"，
        # 也就是「已有成功 SQL + 后续触发终止原因」这条真实路径会直接 500。
        payload = {**skill, "stage_seconds": stage_seconds}
        if termination_reason:
            payload.update({"status": "partial", "failed_stage": "agent",
                            "error": state.get("agent_error") or "本轮未完成全部分析要求"})
        return AgentResult(
            **payload,
            conversation_id=conversation_id, database_id=selected_database_id, intent=intent,
            answer=answer, visualization=visualization,
            business_context=(state.get("business_context", [])
                              if state.get("business_context_turn_id") == turn_id else []),
            tool_trace=current_trace,
            model_calls=state.get("model_calls", 0), agent_usage=state.get("agent_usage", {}),
            agent_elapsed_seconds=agent_elapsed,
            probe_results=probe_results, analysis_plan=analysis_plan,
            sql_generations=sql_generations, analyses=analyses,
            reflection_notes=reflection_notes,
            termination_reason=termination_reason,
            capability_gap=capability_gap,
        )
