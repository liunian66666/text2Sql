"""Database-schema inspection tool."""
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState


def create_schema_tool(registry):
    @tool("inspect_database_schema")
    def inspect_database_schema(tool_call_id: Annotated[str, InjectedToolCallId],
                                table_names: list[str] | None = None,
                                state: Annotated[dict[str, Any], InjectedState] = None,
                                config: RunnableConfig = None):
        """查看当前白名单数据库的真实表、字段、主外键；生成 SQL 前必须调用。"""
        return registry.invoke("inspect_database_schema", {"table_names": table_names or []},
                               state, config, tool_call_id)

    return inspect_database_schema
