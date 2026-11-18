"""Agent tool: query the declared FK graph before writing a multi-table SQL.

这是「图状态」的第一半：图不再只是往 Prompt 里塞一段文字，而是变成模型可以
主动调用的能力。模型给出一组表，工具返回候选连接路径、跳数、复合外键完整性
与已知风险（重复计数、复合外键退化、历史区间笛卡尔积等）。
"""
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState


def create_join_path_tool(registry):
    @tool("inspect_join_path")
    def inspect_join_path(tables: list[str],
                          tool_call_id: Annotated[str, InjectedToolCallId],
                          state: Annotated[dict[str, Any], InjectedState] = None,
                          config: RunnableConfig = None):
        """查询数据库里「表与表之间真实声明的外键路径」，并返回连接风险。

        什么时候用：需要 2 张以上表关联、且不确定该走哪条 JOIN 路径时。
        传表的短名（如 SalesOrderHeader、SalesOrderDetail）即可。
        返回候选路径、每跳的关联列、复合外键是否完整，以及已知风险与规避要求。
        只读、无副作用，同一个表组合本轮只查一次。"""
        return registry.invoke("inspect_join_path", {"tables": list(tables or [])},
                               state, config, tool_call_id)

    return inspect_join_path
