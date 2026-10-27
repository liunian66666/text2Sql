"""LLM → tools → LLM ReAct loop for enterprise data analysis."""
from __future__ import annotations

import operator
from typing import Annotated, Any, Callable, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from .brain import AgentBrain
from .prompts import build_system_prompt
from .tools import AgentToolbox


MAX_AGENT_ROUNDS = 5


class AgentState(TypedDict, total=False):
    messages: Annotated[list[dict[str, Any]], operator.add]
    request: dict[str, Any]
    question: str
    database_id: str
    evidence: str
    pending_tool_calls: list[dict[str, Any]]
    tool_trace: list[dict[str, Any]]
    model_calls: int
    agent_usage: dict[str, int]
    skill_result: dict[str, Any] | None
    business_context: list[dict[str, Any]]
    schema_context: dict[str, Any]
    visualization: dict[str, Any] | None
    intent: str
    answer: str
    agent_error: str | None


def build_agent_graph(brain: AgentBrain, toolbox: AgentToolbox, *, checkpointer=None):
    """Compile a stateful tool-calling graph.

    Runtime callbacks are passed in ``configurable.on_stage`` so persistent
    conversation state contains only serializable data.
    """

    def call_model(state: AgentState, config):
        callback = config.get("configurable", {}).get("on_stage")
        if callback:
            callback({"stage": "agent_planning", "status": "started"})
        try:
            decision = brain.decide(
                messages=state.get("messages", []), tools=toolbox.schemas,
                system_prompt=build_system_prompt(state["database_id"], MAX_AGENT_ROUNDS),
            )
        except Exception as exc:
            if callback:
                callback({"stage": "agent_planning", "status": "failed"})
            return {"pending_tool_calls": [], "agent_error": f"Agent 模型调用失败：{type(exc).__name__}",
                    "answer": "负责规划工具调用的模型暂时不可用，请稍后重试。"}
        calls = [{"id": call.id, "name": call.name, "arguments": call.arguments}
                 for call in decision.tool_calls]
        message: dict[str, Any] = {"role": "assistant", "content": decision.content or ""}
        if calls:
            message["tool_calls"] = [{"id": call["id"], "type": "function",
                                      "function": {"name": call["name"],
                                                   "arguments": __import__("json").dumps(call["arguments"], ensure_ascii=False)}}
                                     for call in calls]
        if callback:
            callback({"stage": "agent_planning", "status": "completed",
                      "tool_calls": [call["name"] for call in calls]})
        previous_usage = state.get("agent_usage", {})
        usage = {key: int(previous_usage.get(key, 0)) + int(decision.usage.get(key, 0) or 0)
                 for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        return {"messages": [message], "pending_tool_calls": calls,
                "answer": decision.content or state.get("answer", ""),
                "model_calls": state.get("model_calls", 0) + 1, "agent_usage": usage, "agent_error": None}

    def execute_tools(state: AgentState, config):
        callback: Callable[[dict], None] | None = config.get("configurable", {}).get("on_stage")
        updates: dict[str, Any] = {}
        tool_messages, trace = [], list(state.get("tool_trace", []))
        for call in state.get("pending_tool_calls", []):
            if callback:
                callback({"stage": "agent_tool", "status": "started", "tool": call["name"]})
            try:
                merged_state = {**state, **updates}
                outcome = toolbox.execute(call["name"], call.get("arguments", {}), merged_state, callback)
                updates.update(outcome.updates)
                status, content = "completed", outcome.content
            except Exception as exc:
                status = "failed"
                content = __import__("json").dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
            trace.append({"tool": call["name"], "arguments": call.get("arguments", {}), "status": status})
            tool_messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": content})
            if callback:
                callback({"stage": "agent_tool", "status": status, "tool": call["name"]})
        return {**updates, "messages": tool_messages, "tool_trace": trace, "pending_tool_calls": []}

    def route_after_model(state: AgentState):
        if state.get("agent_error"):
            return "end"
        if state.get("pending_tool_calls") and state.get("model_calls", 0) < MAX_AGENT_ROUNDS:
            return "tools"
        return "end"

    graph = StateGraph(AgentState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", execute_tools)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_model, {"tools": "tools", "end": END})
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer or InMemorySaver(), name="DeepEye Tool-Calling Agent")
