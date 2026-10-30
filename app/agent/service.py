from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Literal
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import AliasChoices, BaseModel, Field, field_validator

from .graph import build_agent_graph
from .llm import create_chat_model
from .tools import ToolRegistry
from .visualization import build_visualization, summarize_result


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
    status: Literal["success", "empty_result", "error"]
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


class DataAnalysisAgent:
    def __init__(self, sql_skill, *, workspace: Path, databases: dict[str, Path],
                 llm_config=None, chat_model=None, knowledge=None, checkpointer=None):
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.tool_registry = ToolRegistry(sql_skill, databases, knowledge)
        self.chat_model = chat_model or create_chat_model(llm_config)
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
        }, config={"configurable": {
            "thread_id": self.thread_id(conversation_id, validated.database_id),
            "on_stage": on_stage,
        }})
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
            return AgentResult(request_id=request_id, status="error" if failed else "success", intent=intent,
                               conversation_id=conversation_id, database_id=selected_database_id, answer=answer,
                               visualization=visualization, tool_trace=current_trace,
                               model_calls=state.get("model_calls", 0), agent_usage=state.get("agent_usage", {}),
                               agent_elapsed_seconds=agent_elapsed,
                               stage_seconds={"agent_orchestration": agent_elapsed},
                               failed_stage="agent" if failed else None,
                               error=error, artifacts_dir=str(artifacts))
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
        return AgentResult(
            **{**skill, "stage_seconds": stage_seconds},
            conversation_id=conversation_id, database_id=selected_database_id, intent=intent,
            answer=answer, visualization=visualization,
            business_context=(state.get("business_context", [])
                              if state.get("business_context_turn_id") == turn_id else []),
            tool_trace=current_trace,
            model_calls=state.get("model_calls", 0), agent_usage=state.get("agent_usage", {}),
            agent_elapsed_seconds=agent_elapsed,
        )
