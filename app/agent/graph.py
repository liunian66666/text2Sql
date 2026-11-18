"""Standard ChatOpenAI → ToolNode → ChatOpenAI LangGraph workflow."""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from .prompts import build_system_prompt
from .state import AgentState
from .planning import generate_plan, is_complex_question, render_plan
from .reflection import reflect, should_reflect
from app.llm.budget import BudgetExceededError


# 自动数据源模式比手动模式多一次 select_data_source 调用；保留一轮容错，
# 避免模型一次重复调用就挤掉最终的 build_visualization。
# 复合问题（查询 + 追问解释 + 画图）实际需要 6-7 轮：选库、口径、Schema、
# run_text_to_sql、追问复用、build_visualization；8 轮再留两次容错。
MAX_TOOL_ROUNDS = 8
MAX_HISTORY_MESSAGES = 40

# 复杂问题超出工具轮数时，用一次「只总结、不调工具」的模型调用收尾。
# 这样用户至少拿到已完成的部分，而不是一句「请缩小范围」。
TOOL_LIMIT_SUMMARY = """工具调用轮数已用尽。请基于已经获得的工具结果，直接给出最终回答：
1) 已完成了哪些分析、得到什么结论（引用真实数字）；
2) 哪些部分未完成、为什么（轮数/能力/数据缺失）；
3) 建议用户下一步怎么做。
禁止调用任何工具，禁止编造未执行的数据。"""

# 反思节点的两种收尾指令。反思只做「判断」，不产生答案，所以判定结束之后
# 仍需要一次无工具模型调用把结果落成自然语言。
FINISH_INSTRUCTION = """复盘判定：已有信息足以回答用户问题。
请直接给出最终回答，引用已获得的真实数字与字段，明确说明口径与时间范围。
禁止调用任何工具，禁止补写没有执行过的查询结果。"""

STOP_INSTRUCTION = """复盘判定：本轮在原地打转或无法获得新信息，应当终止。
请如实说明：1) 已完成哪些查询与得到什么结论；2) 还缺什么、为什么拿不到；
3) 建议用户下一步怎么做。禁止调用任何工具，禁止编造数据。"""


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
        rounds_left = max(MAX_TOOL_ROUNDS - int(state.get("tool_rounds", 0)), 0)
        try:
            response = model_with_tools.invoke([
                SystemMessage(content=build_system_prompt(
                    state["database_id"], MAX_TOOL_ROUNDS, state.get("available_databases", []),
                    analysis_plan=render_plan(state.get("analysis_plan")),
                    rounds_left=rounds_left)),
                *_prompt_history(state.get("messages", [])),
            ], config=config)
        except BudgetExceededError as exc:
            # 预算耗尽是「可识别的终止」，不能退化成「模型暂时不可用」——
            # 后者会让路由继续循环，每一轮都再撞一次上限。
            if callback:
                callback({"stage": "agent_planning", "status": "failed",
                          "reason": "llm_budget_exhausted"})
            return {
                "messages": [AIMessage(
                    content="本次请求的模型预算已用尽，已安全停止。已完成的部分结果见上方执行记录。")],
                "agent_error": f"模型预算耗尽：{exc}",
                "termination_reason": "llm_budget_exhausted",
            }
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
        # 收尾总结：不再直接抛一句「请缩小范围」，而是把已完成的结果交代清楚。
        # 这是一次无工具的模型调用，失败也不影响主流程。
        answer_text = "本轮分析步骤已达上限，已安全停止；未能生成最终总结，已完成的结果见上方工具记录。"
        model_calls = int(state.get("model_calls", 0))
        agent_usage = dict(state.get("agent_usage", {}) or {})
        try:
            response = chat_model.invoke([
                SystemMessage(content=build_system_prompt(
                    state["database_id"], MAX_TOOL_ROUNDS, state.get("available_databases", []),
                    analysis_plan=render_plan(state.get("analysis_plan")), rounds_left=0)),
                *_prompt_history(state.get("messages", [])),
                HumanMessage(content=TOOL_LIMIT_SUMMARY),
            ], config=config)
            answer_text = (getattr(response, "content", "") or "").strip() or answer_text
            model_calls += 1
            for key, value in _usage_from_message(response).items():
                agent_usage[key] = int(agent_usage.get(key, 0)) + value
        except Exception:
            pass
        return {
            "messages": [*tool_messages, AIMessage(content=answer_text)],
            "agent_error": error,
            "termination_reason": "tool_limit",
            "model_calls": model_calls,
            "agent_usage": agent_usage,
        }

    def plan_analysis(state: AgentState, config):
        """复杂问题在第 1 轮工具后补一份轻量计划（1 次模型调用）。"""
        callback = config.get("configurable", {}).get("on_stage")
        if state.get("analysis_plan") or not is_complex_question(state.get("question", "")):
            return {}
        if callback:
            callback({"stage": "analysis_plan", "status": "started"})
        capabilities = state.get("capability_snapshot") or {}
        probe_summary = "; ".join(
            str(item.get("coverage") or item.get("row_count") or item.get("table") or "")
            for item in (state.get("probe_results") or [])[:3])
        plan, usage = generate_plan(
            chat_model, question=state.get("question", ""),
            database_id=state.get("database_id", ""), capabilities=capabilities,
            probe_summary=probe_summary)
        if callback:
            callback({"stage": "analysis_plan", "status": "completed" if plan else "failed"})
        if not plan:
            return {}
        agent_usage = dict(state.get("agent_usage", {}) or {})
        for key, value in usage.items():
            agent_usage[key] = int(agent_usage.get(key, 0)) + value
        return {"analysis_plan": plan, "analysis_plan_turn_id": state["turn_id"],
                "model_calls": int(state.get("model_calls", 0)) + 1,
                "agent_usage": agent_usage}

    def reflect_on_progress(state: AgentState, config):
        """任务级复盘：信息够不够、下一轮是否有新查询、是否在原地打转。

        与 ``sql_revision`` 的分界：那个修「一条 SQL 写得对不对」，这个判断
        「任务该不该继续」。判定 finish/stop 后由一次无工具调用直接收尾，
        不再回到 assistant 循环 —— 这是「停止条件明确」的落地位置。
        """
        callback = config.get("configurable", {}).get("on_stage")
        if callback:
            callback({"stage": "agent_reflection", "status": "started"})
        decision, reason, usage = reflect(chat_model, state)
        agent_usage = dict(state.get("agent_usage", {}) or {})
        for key, value in usage.items():
            agent_usage[key] = int(agent_usage.get(key, 0)) + value
        model_calls = int(state.get("model_calls", 0)) + 1
        updates: dict = {
            "reflection_done": True,
            "reflection_decision": decision,
            "reflection_notes": [{"turn_id": state["turn_id"],
                                  "round": int(state.get("tool_rounds", 0)),
                                  "decision": decision, "reason": reason}],
            "model_calls": model_calls,
            "agent_usage": agent_usage,
        }
        if callback:
            callback({"stage": "agent_reflection", "status": "completed",
                      "decision": decision, "reason": reason})
        if decision == "continue":
            return updates
        instruction = STOP_INSTRUCTION if decision == "stop" else FINISH_INSTRUCTION
        answer_text = ("复盘判定本轮应终止，但收尾总结未能生成；已完成的结果见上方工具记录。"
                       if decision == "stop" else "复盘判定信息已足够，但收尾总结未能生成。")
        try:
            response = chat_model.invoke([
                SystemMessage(content=build_system_prompt(
                    state["database_id"], MAX_TOOL_ROUNDS, state.get("available_databases", []),
                    analysis_plan=render_plan(state.get("analysis_plan")), rounds_left=0)),
                *_prompt_history(state.get("messages", [])),
                HumanMessage(content=instruction),
            ], config=config)
            answer_text = (getattr(response, "content", "") or "").strip() or answer_text
            model_calls += 1
            for key, value in _usage_from_message(response).items():
                agent_usage[key] = int(agent_usage.get(key, 0)) + value
        except BudgetExceededError:
            raise
        except Exception:
            pass
        updates.update({
            "messages": [AIMessage(content=answer_text)],
            "model_calls": model_calls,
            "agent_usage": agent_usage,
        })
        if decision == "stop":
            updates["termination_reason"] = "loop_detected"
        return updates

    def route_after_model(state: AgentState):
        if state.get("agent_error"):
            return "end"
        last = state.get("messages", [])[-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools" if state.get("tool_rounds", 0) < MAX_TOOL_ROUNDS else "limit"
        return "end"

    def route_after_tools(state: AgentState):
        """第一轮工具后补规划；之后按需插入一次反思。"""
        if (not state.get("analysis_plan")
                and int(state.get("tool_rounds", 0)) == 1
                and is_complex_question(state.get("question", ""))):
            return "planner"
        if should_reflect(state):
            return "reflection"
        return "assistant"

    def route_after_reflection(state: AgentState):
        if state.get("reflection_decision") in {"finish", "stop"}:
            return "end"
        return "assistant"

    graph = StateGraph(AgentState)
    graph.add_node("assistant", call_model)
    graph.add_node("tools", tool_node)
    graph.add_node("increment_tool_round", increment_tool_round)
    graph.add_node("planner", plan_analysis)
    graph.add_node("reflection", reflect_on_progress)
    graph.add_node("tool_limit", stop_at_tool_limit)
    graph.add_edge(START, "assistant")
    graph.add_conditional_edges("assistant", route_after_model,
                                {"tools": "tools", "limit": "tool_limit", "end": END})
    graph.add_edge("tools", "increment_tool_round")
    graph.add_conditional_edges("increment_tool_round", route_after_tools,
                                {"planner": "planner", "reflection": "reflection",
                                 "assistant": "assistant"})
    graph.add_conditional_edges("reflection", route_after_reflection,
                                {"assistant": "assistant", "end": END})
    graph.add_edge("planner", "assistant")
    graph.add_edge("tool_limit", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver(), name="Enterprise Data Analyst Agent")
