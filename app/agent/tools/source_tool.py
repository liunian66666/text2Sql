"""Agent tool for choosing one whitelisted business data source."""
from typing import Annotated, Any, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState


def create_source_tool(registry):
    # 数据源清单必须来自运行时注册表：此前硬编码为两个企业库，导致
    # AdventureWorks 即使注册成功也无法被 Agent 选中。
    available = tuple(registry.databases)
    database_literal = Literal[available]  # type: ignore[valid-type]

    @tool("select_data_source")
    def select_data_source(database_id: database_literal,  # type: ignore[valid-type]
                           tool_call_id: Annotated[str, InjectedToolCallId],
                           state: Annotated[dict[str, Any], InjectedState] = None,
                           config: RunnableConfig = None):
        """自动模式下先选择唯一数据源。销售经营问题优先 enterprise_sales；需要历史成本等
        企业级维度时按能力选择 adventureworks；人事组织选 enterprise_hr。"""
        return registry.invoke("select_data_source", {"database_id": database_id},
                               state, config, tool_call_id)

    return select_data_source
