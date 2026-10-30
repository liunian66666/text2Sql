"""Agent tool for choosing one whitelisted business data source."""
from typing import Annotated, Any, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState


def create_source_tool(registry):
    @tool("select_data_source")
    def select_data_source(database_id: Literal["enterprise_sales", "enterprise_hr"],
                           tool_call_id: Annotated[str, InjectedToolCallId],
                           state: Annotated[dict[str, Any], InjectedState] = None,
                           config: RunnableConfig = None):
        """自动模式下先选择唯一数据源；销售经营选enterprise_sales，人事组织选enterprise_hr。"""
        return registry.invoke("select_data_source", {"database_id": database_id},
                               state, config, tool_call_id)

    return select_data_source
