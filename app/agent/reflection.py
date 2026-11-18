"""任务级反思：判断「信息够不够、该不该继续」，而不只是看轮数。

这不是 SQL 层的 ``sql_revision``（那个修的是「一条 SQL 写得对不对」）。
这里回答的是编排层的问题：已经拿到的结果是否足以回答用户、下一轮是否还有
明确的新查询、是否在原地打转。

成本控制：每题只跑一次，且只在「复杂问题跑到第 2 轮」或「任何问题跑到第 4 轮」
时触发 —— 反思本身也是一次模型调用，滥用等于用成本换焦虑。
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.llm.budget import BudgetExceededError

DECISIONS = ("continue", "finish", "stop")

REFLECT_PROMPT = """你是数据分析 Agent 的复盘员。基于「用户问题 + 已执行的工具调用 + 最新结果」，
判断下一步该做什么。只输出一个 JSON 对象，不要解释：

{"decision": "continue|finish|stop", "reason": "一句话"}

判定标准：
- finish：已有结果足以回答用户问题（哪怕不够完美），应停止调用工具、直接总结。
- stop：下面任一情况成立就选它 ——
  * 最近几轮在重复同一类查询，参数微变但信息量相同；
  * 工具连续报错/无数据，再查也不会有新信息；
  * 剩余轮数不足以完成剩余步骤。
- continue：确实还缺关键数据，且下一轮有明确、不同的一次查询。

禁止为了「确认/校验」而选择 continue。信息已足够时选 finish。"""


def should_reflect(state: dict[str, Any]) -> bool:
    """Reflection is driven by *evidence of a loop*, not by a round timer.

    定时反思等于用成本换焦虑：正常的多步分析（选库 → 探查 → 取数 → 画图）
    本来就要 3-4 轮，每轮多问一次模型既贵又会过早打断正确路径。真正值得
    介入的信号只有三类：
      * 刚才有一次「完全相同调用」被缓存复用（参数微变、信息量相同）；
      * 本轮已经失败 2 次以上（再试也不会有新信息）；
      * 工具层已判定 loop_detected。
    每题最多反思一次。
    """
    if state.get("reflection_done"):
        return False
    turn_id = state.get("turn_id")
    trace = [item for item in (state.get("tool_trace") or [])
             if item.get("turn_id") == turn_id]
    if not trace:
        return False
    if any(item.get("cached") or item.get("loop_detected") for item in trace[-3:]):
        return True
    return sum(1 for item in trace if item.get("status") == "failed") >= 2


def build_reflection_context(state: dict[str, Any]) -> str:
    """Compact, auditable context — never the full message history."""
    turn_id = state.get("turn_id")
    lines = [f"用户问题：{state.get('question', '')}",
             f"当前数据源：{state.get('database_id', '')}"]
    trace = [item for item in (state.get("tool_trace") or [])
             if item.get("turn_id") == turn_id][-6:]
    if trace:
        lines.append("最近工具调用：")
        for item in trace:
            arguments = json.dumps(item.get("arguments") or {}, ensure_ascii=False, default=str)
            row = f"- {item.get('tool')}({arguments[:140]}) → {item.get('status')}"
            if item.get("cached"):
                row += "（缓存复用）"
            if item.get("error"):
                row += f"；错误：{str(item['error'])[:80]}"
            lines.append(row)
    probes = state.get("probe_results") or []
    if probes:
        lines.append(f"已有确定性探查事实 {len(probes)} 条")
    analyses = [item for item in (state.get("sql_results") or [])
                if item.get("turn_id") == turn_id]
    if analyses:
        lines.append("已生成的业务查询：")
        for item in analyses:
            lines.append(f"- {str(item.get('question'))[:60]} → {item.get('status')}，"
                         f"{item.get('row_count')} 行")
    else:
        lines.append("尚未产生任何业务查询结果")
    lines.append(f"已用工具轮数：{state.get('tool_rounds')}（上限 8）")
    plan = state.get("analysis_plan") or {}
    if plan.get("steps"):
        lines.append("分析计划步骤：" + "；".join(str(step)[:40] for step in plan["steps"][:6]))
    return "\n".join(lines)


def parse_decision(raw: str) -> tuple[str, str]:
    """Never let a malformed reflection break the run — default to continue."""
    text = (raw or "").strip()
    fenced = re.search(r"\{[\s\S]*\}", text)
    if fenced:
        try:
            parsed = json.loads(fenced.group(0))
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            decision = str(parsed.get("decision") or "").strip().lower()
            reason = str(parsed.get("reason") or "").strip()
            if decision in DECISIONS:
                return decision, reason or decision
    lowered = text.lower()
    for decision in DECISIONS:
        if decision in lowered:
            return decision, text[:120]
    return "continue", "反思输出无法解析，按继续处理"


def reflect(chat_model, state: dict[str, Any]) -> tuple[str, str, dict[str, int]]:
    """One tool-free model call. Returns (decision, reason, usage)."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from .graph import _usage_from_message

    try:
        response = chat_model.invoke([
            SystemMessage(content=REFLECT_PROMPT),
            HumanMessage(content=build_reflection_context(state)),
        ])
    except BudgetExceededError:
        # 预算耗尽是硬终止，不能被降级成「反思失败，继续」。
        raise
    except Exception:
        return "continue", "反思调用失败，按继续处理", {}
    decision, reason = parse_decision(getattr(response, "content", "") or "")
    return decision, reason, _usage_from_message(response)


__all__ = ["should_reflect", "reflect", "parse_decision", "build_reflection_context"]
