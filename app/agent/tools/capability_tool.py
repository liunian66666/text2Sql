"""Agent capability-discovery tool."""
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState


def create_capability_tool(registry):
    @tool("get_capabilities")
    def get_capabilities(tool_call_id: Annotated[str, InjectedToolCallId],
                         state: Annotated[dict[str, Any], InjectedState] = None,
                         config: RunnableConfig = None):
        """查询当前数据分析 Agent 支持的能力，不访问数据库。"""
        return registry.invoke("get_capabilities", {}, state, config, tool_call_id)

    return get_capabilities
