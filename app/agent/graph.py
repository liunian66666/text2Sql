"""Standard ChatOpenAI → ToolNode → ChatOpenAI LangGraph workflow."""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from .prompts import build_system_prompt
from .state import AgentState


# 自动数据源模式比手动模式多一次 select_data_source 调用；保留一轮容错，
# 避免模型一次重复调用就挤掉最终的 build_visualization。
MAX_TOOL_ROUNDS = 6
MAX_HISTORY_MESSAGES = 40


def _prompt_history(messages):
    """Bound prompt growth while never starting with an orphaned tool result."""
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    start = max(0, len(messages) - MAX_HISTORY_MESSAGES)
    for index in range(start, len(messages)):
        if isinstance(messages[index], HumanMessage):
            return messages[index:]
    return messages[-MAX_HISTORY_MESSAGES:]


def _usage_from_message(message: AIMessage) -> dict[str, int]:
    usage = message.usage_metadata or {}
    if not usage:
        usage = (message.response_metadata or {}).get("token_usage", {})
    return {
        "prompt_tokens": int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0),
        "completion_tokens": int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0),
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
    }


def build_agent_graph(chat_model, tools, *, checkpointer=None):
    """Compile a stateful Agent using native LangChain messages and tools."""

    model_with_tools = chat_model.bind_tools(tools)

    def call_model(state: AgentState, config):
        callback = config.get("configurable", {}).get("on_stage")
        if callback:
            callback({"stage": "agent_planning", "status": "started"})
        try:
            response = model_with_tools.invoke([
                SystemMessage(content=build_system_prompt(
                    state["database_id"], MAX_TOOL_ROUNDS, state.get("available_databases", []))),
                *_prompt_history(state.get("messages", [])),
            ], config=config)
        except Exception as exc:
            if callback:
                callback({"stage": "agent_planning", "status": "failed"})
            return {
                "messages": [AIMessage(content="负责规划工具调用的模型暂时不可用，请稍后重试。")],
                "agent_error": f"Agent 模型调用失败：{type(exc).__name__}",
            }
        if callback:
            callback({"stage": "agent_planning", "status": "completed",
                      "tool_calls": [call.get("name") for call in response.tool_calls]})
        previous = state.get("agent_usage", {})
        current = _usage_from_message(response)
        usage = {key: int(previous.get(key, 0)) + current[key] for key in current}
        return {"messages": [response], "model_calls": state.get("model_calls", 0) + 1,
                "agent_usage": usage, "agent_error": None}

    tool_node = ToolNode(tools, handle_tool_errors=True)

    def increment_tool_round(state: AgentState):
        return {"tool_rounds": state.get("tool_rounds", 0) + 1}

    def stop_at_tool_limit(state: AgentState, config):
        callback = config.get("configurable", {}).get("on_stage")
        if callback:
            callback({"stage": "agent_planning", "status": "failed", "reason": "tool_round_limit"})
        last = state.get("messages", [])[-1]
        tool_messages = [ToolMessage(
            content="工具未执行：已达到本轮最大工具调用轮数。",
            tool_call_id=call["id"],
            name=call["name"],
            status="error",
        ) for call in (last.tool_calls if isinstance(last, AIMessage) else [])]
        error = f"Agent 已达到 {MAX_TOOL_ROUNDS} 轮工具调用上限"
        return {
            "messages": [*tool_messages, AIMessage(content="本轮分析步骤过多，已安全停止。请缩小问题范围后重试。")],
            "agent_error": error,
        }

    def route_after_model(state: AgentState):
        if state.get("agent_error"):
            return "end"
        last = state.get("messages", [])[-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools" if state.get("tool_rounds", 0) < MAX_TOOL_ROUNDS else "limit"
        return "end"

    graph = StateGraph(AgentState)
    graph.add_node("assistant", call_model)
    graph.add_node("tools", tool_node)
    graph.add_node("increment_tool_round", increment_tool_round)
    graph.add_node("tool_limit", stop_at_tool_limit)
    graph.add_edge(START, "assistant")
    graph.add_conditional_edges("assistant", route_after_model,
                                {"tools": "tools", "limit": "tool_limit", "end": END})
    graph.add_edge("tools", "increment_tool_round")
    graph.add_edge("increment_tool_round", "assistant")
    graph.add_edge("tool_limit", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver(), name="Enterprise Data Analyst Agent")
