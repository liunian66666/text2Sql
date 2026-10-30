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
    visualization: dict[str, Any] | None
    visualization_turn_id: str
    analysis_intent: Literal["query", "ranking", "trend", "proportion"]
    tool_trace: Annotated[list[dict[str, Any]], operator.add]
    tool_rounds: int
    model_calls: int
    agent_usage: dict[str, int]
    agent_error: str | None


__all__ = ["AgentState"]
