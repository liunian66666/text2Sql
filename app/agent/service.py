from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.skills import SQLSkillRequest
from .brain import OpenAIToolCallingBrain
from .graph import build_agent_graph
from .tools import AgentToolbox
from .visualization import build_visualization, summarize_result


class AgentResult(BaseModel):
    request_id: str
    status: Literal["success", "empty_result", "error"]
    intent: str
    intent_reason: str = ""
    conversation_id: str
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
                 llm_config=None, brain=None, knowledge=None):
        self.sql_skill = sql_skill
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.toolbox = AgentToolbox(sql_skill, databases, knowledge)
        self.brain = brain or OpenAIToolCallingBrain(llm_config)
        self.graph = build_agent_graph(self.brain, self.toolbox)

    def run(self, request: SQLSkillRequest | dict, on_stage=None) -> AgentResult:
        validated = SQLSkillRequest.model_validate(request)
        conversation_id = validated.conversation_id or uuid4().hex
        started = time.perf_counter()
        state = self.graph.invoke({
            "messages": [{"role": "user", "content": validated.question}],
            "request": validated.model_dump(),
            "question": validated.question,
            "database_id": validated.database_id,
            "evidence": validated.evidence,
            "pending_tool_calls": [], "tool_trace": [], "model_calls": 0, "agent_usage": {},
            "skill_result": None, "business_context": [], "schema_context": {},
            "visualization": None, "intent": "query", "answer": "", "agent_error": None,
        }, config={"configurable": {"thread_id": conversation_id, "on_stage": on_stage}})
        skill = state.get("skill_result")
        if not skill:
            request_id = uuid4().hex
            artifacts = self.workspace / request_id
            artifacts.mkdir(parents=True, exist_ok=False)
            failed = bool(state.get("agent_error"))
            return AgentResult(request_id=request_id, status="error" if failed else "success", intent="chat",
                               conversation_id=conversation_id, answer=state.get("answer", ""),
                               visualization=None, tool_trace=state.get("tool_trace", []),
                               model_calls=state.get("model_calls", 0), agent_usage=state.get("agent_usage", {}),
                               agent_elapsed_seconds=round(time.perf_counter() - started, 3),
                               failed_stage="agent_planning" if failed else None,
                               error=state.get("agent_error"), artifacts_dir=str(artifacts))
        intent = state.get("intent", "query")
        visualization = state.get("visualization")
        if visualization is None and skill.get("status") in {"success", "empty_result"}:
            visualization = build_visualization(intent, skill.get("columns", []), skill.get("rows", []))
        answer = state.get("answer") or summarize_result(intent, skill.get("columns", []), skill.get("rows", []))
        agent_elapsed = round(time.perf_counter() - started, 3)
        stage_seconds = dict(skill.get("stage_seconds") or {})
        # Planner turns and tool orchestration sat outside the Skill's own
        # stopwatch, so a request reported the SQL stages as if they were the
        # whole answer. The residual is measured, not estimated: both endpoints
        # are timed here and the Skill times itself.
        stage_seconds["agent_orchestration"] = round(
            max(agent_elapsed - float(skill.get("elapsed_seconds") or 0.0), 0.0), 3)
        return AgentResult(
            **{**skill, "stage_seconds": stage_seconds},
            conversation_id=conversation_id, intent=intent,
            answer=answer, visualization=visualization,
            business_context=state.get("business_context", []), tool_trace=state.get("tool_trace", []),
            model_calls=state.get("model_calls", 0), agent_usage=state.get("agent_usage", {}),
            agent_elapsed_seconds=agent_elapsed,
        )
