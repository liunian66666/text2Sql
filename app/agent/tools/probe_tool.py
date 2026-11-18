"""Agent tool for deterministic, read-only schema/data probes."""
from typing import Annotated, Any, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState


def create_probe_tool(registry):
    @tool("probe_database")
    def probe_database(probe: Literal["date_range", "row_count", "distinct_values", "exists"],
                       table: str,
                       tool_call_id: Annotated[str, InjectedToolCallId],
                       column: str = "",
                       limit: int = 10,
                       state: Annotated[dict[str, Any], InjectedState] = None,
                       config: RunnableConfig = None):
        """轻量只读探查确定事实（日期覆盖、行数、字段是否存在、低基数字段取值）。
        不要用 run_text_to_sql 查询这类元数据事实。date_range 会同时返回现实时间与
        最近一个完整自然年（last_complete_year），确定分析时间范围时应优先使用它。"""
        return registry.invoke("probe_database", {
            "probe": probe, "table": table, "column": column, "limit": limit,
        }, state, config, tool_call_id)

    return probe_database
