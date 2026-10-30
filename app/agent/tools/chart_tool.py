"""Deterministic visualization-selection tool."""
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState


def create_chart_tool(registry):
    @tool("build_visualization")
    def build_visualization(tool_call_id: Annotated[str, InjectedToolCallId],
                            chart_type: str | None = None,
                            state: Annotated[dict[str, Any], InjectedState] = None,
                            config: RunnableConfig = None):
        """根据最近一次 SQL 结果生成指标卡、柱状图、折线图、饼图或表格配置。"""
        arguments = {}
        if chart_type:
            arguments["chart_type"] = chart_type
        return registry.invoke("build_visualization", arguments, state, config, tool_call_id)

    return build_visualization
