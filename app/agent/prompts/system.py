"""System-prompt assembly kept separate from graph execution."""
from __future__ import annotations

from .analyst import ANALYST_POLICY
from .planner import PLANNER_POLICY


SYSTEM_IDENTITY = """你是一名谨慎、可解释的企业数据分析 Agent。
DeepEye-SQL 只是你调用的底层 Text-to-SQL 引擎，不是你的身份。

你的目标是通过工具回答用户问题，而不是凭空编造数据库事实。你可以直接处理一般对话；凡是涉及具体数据、排名、趋势、占比、业务指标或数据库结构的问题，都必须调用工具。

安全边界：只允许只读分析；拒绝修改、删除、插入或建表请求。
当前数据源：{database_id}
可用数据源：{available_databases}
当前可用工具由运行时提供。一次分析最多进行 {max_rounds} 轮工具规划。
"""


def build_system_prompt(database_id: str, max_rounds: int,
                        available_databases: list[str] | None = None) -> str:
    return "\n\n".join([
        SYSTEM_IDENTITY.format(
            database_id=database_id,
            available_databases=", ".join(available_databases or [database_id]),
            max_rounds=max_rounds,
        ).strip(),
        PLANNER_POLICY.strip(),
        ANALYST_POLICY.strip(),
    ])
