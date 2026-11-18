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
当前可用工具由运行时提供。一次分析最多进行 {max_rounds} 轮工具规划，剩余 {rounds_left} 轮。
"""


def build_system_prompt(database_id: str, max_rounds: int,
                        available_databases: list[str] | None = None,
                        *, analysis_plan: str = "", rounds_left: int | None = None) -> str:
    remaining = max_rounds if rounds_left is None else max(rounds_left, 0)
    parts = [
        SYSTEM_IDENTITY.format(
            database_id=database_id,
            available_databases=", ".join(available_databases or [database_id]),
            max_rounds=max_rounds,
            rounds_left=remaining,
        ).strip(),
        PLANNER_POLICY.strip(),
        ANALYST_POLICY.strip(),
    ]
    if analysis_plan:
        parts.append("本轮已生成的结构化分析计划（严格按步骤推进，不要重复已完成步骤）：\n"
                     + analysis_plan)
    if remaining <= 1:
        parts.append("剩余工具轮数已不足：本轮必须直接输出最终回答，不得再调用任何工具。"
                     "回答中要说明已完成部分、缺失部分与建议。")
    return "\n\n".join(parts)
