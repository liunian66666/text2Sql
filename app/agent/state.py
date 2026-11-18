"""Serializable state shared by the LangGraph Agent and its tools."""
from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    request: dict[str, Any]
    turn_id: str
    question: str
    database_id: str
    requested_database_id: str
    available_databases: list[str]
    user_hint: str
    trusted_evidence: str
    business_context: list[dict[str, Any]]
    business_context_turn_id: str
    schema_context: dict[str, Any]
    schema_context_database_id: str
    sql_result: dict[str, Any]
    sql_result_turn_id: str
    sql_result_database_id: str
    # 本轮所有业务查询的逐条记录（追加语义）。sql_result 只保留最后一次以兼容
    # 既有 sql/columns/rows 装配与画图；复合任务的多条子结果必须在这里可追溯，
    # 否则前序 SQL 会被覆盖，用户只看到最后一条。
    # 只存预览行（preview_rows 上限 50）+ 真实 row_count：全量入 state 会让
    # checkpoint 与最终响应膨胀，而聚合本来就该在 SQL 内完成。
    sql_results: Annotated[list[dict[str, Any]], operator.add]
    sql_results_turn_id: str
    # 业务 SQL 预算（与探查、工具轮数分离）：记录本轮已生成几次 SQL 及各自的子问题，
    # 用于允许有限次数的复杂分析、同时拦截重复提问。
    sql_generations: int
    sql_history: Annotated[list[dict[str, Any]], operator.add]
    # 轻量探查结果：只承载确定性事实（日期覆盖/行数/取值），永不写入 sql_result，
    # 因此不会被当成业务查询结果展示给用户。
    probe_results: Annotated[list[dict[str, Any]], operator.add]
    probe_cache: dict[str, Any]
    schema_cache: dict[str, Any]
    metrics_cache: dict[str, Any]
    # 通用调用指纹：`工具名 + 归一化参数` → {turn_id, count, content}。
    # 这是 Infra 层的循环防线，独立于各工具自己的语义缓存：
    # 第 2 次完全相同调用直接复用结果、第 3 次判定 loop_detected 并阻断。
    tool_calls_seen: dict[str, Any]
    # 反思节点：每题只跑一次，避免为省轮次反而多付模型调用。
    reflection_done: bool
    reflection_decision: str
    reflection_notes: Annotated[list[dict[str, Any]], operator.add]
    # 图状态：只存「引用 + 结论」，不存 networkx 对象本身（那玩意每轮都会
    # 过 checkpointer 序列化，既膨胀又无法 JSON 化）。图资源放进程级单例。
    join_path_context: dict[str, Any]
    join_path_cache: dict[str, Any]
    analysis_plan: dict[str, Any] | None
    analysis_plan_turn_id: str
    capability_snapshot: dict[str, Any]
    capability_gap: dict[str, str]
    termination_reason: str | None
    visualization: dict[str, Any] | None
    visualization_turn_id: str
    analysis_intent: Literal["query", "ranking", "trend", "proportion"]
    tool_trace: Annotated[list[dict[str, Any]], operator.add]
    tool_rounds: int
    model_calls: int
    agent_usage: dict[str, int]
    agent_error: str | None


__all__ = ["AgentState"]
