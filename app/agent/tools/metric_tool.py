"""Business-metric retrieval tool."""
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState


def create_metric_tool(registry):
    @tool("search_business_metrics")
    def search_business_metrics(query: str, state: Annotated[dict[str, Any], InjectedState],
                                tool_call_id: Annotated[str, InjectedToolCallId],
                                config: RunnableConfig):
        """检索企业指标定义和 SQL 口径；每个新的数据分析问题在生成 SQL 前都必须调用。"""
        return registry.invoke("search_business_metrics", {"query": query}, state, config, tool_call_id)

    return search_business_metrics
