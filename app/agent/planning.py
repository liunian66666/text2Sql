"""Minimal structured AnalysisPlan for complex questions.

This is deliberately *not* a multi-agent planner: one extra model call produces a
short JSON plan that is injected back into the system prompt. It gives the ReAct
loop an explicit decomposition (which step, which SQL purpose) so it stops
re-asking the same sub-question and stops spending its single SQL budget on
metadata probes.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.llm.budget import BudgetExceededError

PLAN_FIELDS = ("database_id", "goal", "metrics", "dimensions", "time_range",
               "knowledge", "steps", "expected_output")

# 复杂度信号：多要求、多指标、跨期对比。命中 2 个以上或长问句才触发规划，
# 避免每个简单查数都多付一次模型调用。
_COMPLEX_CUES = ("同比", "环比", "对比", "毛利", "毛利率", "排名", "前三", "top",
                 "并", "同时", "以及", "增长", "下降", "占比", "趋势", "分地区")

PLAN_PROMPT = """你是数据分析任务的规划器。只输出一个 JSON 对象，不要代码块、不要解释。
字段必须完整：
{
  "database_id": "要使用的数据源",
  "goal": "一句话分析目标",
  "metrics": ["需要计算的指标及口径"],
  "dimensions": ["分组维度"],
  "time_range": "分析时间范围（只写年份/区间，若不确定写待探查）",
  "knowledge": ["必须确认的维度关系或口径风险，如历史成本按生效区间匹配"],
  "steps": ["按顺序的步骤，每步说明目的；业务SQL步骤最多3步"],
  "expected_output": "最终交付形态"
}
硬约束：
- 确定性事实（日期覆盖、行数、字段是否存在）必须规划为 probe_database 步骤，不能规划成业务 SQL。
- 业务 SQL 步骤最多 3 个，每个只解决一个子问题，禁止把同一子问题拆成两步。
- 数据源必须来自给定清单；若问题要求的能力当前数据源不支持，在 knowledge 中写明缺失。
"""


def is_complex_question(question: str) -> bool:
    normalized = question.casefold()
    hits = sum(1 for cue in _COMPLEX_CUES if cue in normalized)
    return len(normalized) >= 60 or hits >= 2


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        candidate = text[start:end + 1] if 0 <= start < end else None
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {field: parsed.get(field) for field in PLAN_FIELDS if parsed.get(field) is not None}


def normalize_plan(plan: dict[str, Any], *, database_id: str) -> dict[str, Any]:
    """Keep the plan faithful but bounded: unknown sources fall back to the real one."""
    steps = plan.get("steps") or []
    if not isinstance(steps, list):
        steps = [str(steps)]
    normalized = dict(plan)
    normalized["steps"] = [str(step) for step in steps][:8]
    normalized["database_id"] = database_id
    for key in ("metrics", "dimensions", "knowledge"):
        value = normalized.get(key)
        if isinstance(value, str):
            normalized[key] = [value]
    return normalized


def render_plan(plan: dict[str, Any] | None) -> str:
    if not plan:
        return ""
    lines = [f"分析计划（已生成，按步骤执行）：目标={plan.get('goal', '')}"]
    for label, key in (("数据源", "database_id"), ("时间范围", "time_range"),
                       ("指标", "metrics"), ("维度", "dimensions"),
                       ("知识要点", "knowledge"), ("预期产出", "expected_output")):
        value = plan.get(key)
        if not value:
            continue
        if isinstance(value, list):
            value = "；".join(str(item) for item in value)
        lines.append(f"- {label}：{value}")
    steps = plan.get("steps") or []
    if steps:
        lines.append("- 步骤：")
        lines.extend(f"  {index + 1}. {step}" for index, step in enumerate(steps))
    return "\n".join(lines)


def generate_plan(chat_model, *, question: str, database_id: str,
                  capabilities: dict[str, Any] | None = None,
                  probe_summary: str = "") -> tuple[dict[str, Any] | None, dict[str, int]]:
    """One model call. Returns (plan, usage); any failure degrades to no plan."""
    from langchain_core.messages import HumanMessage, SystemMessage

    context = [f"用户问题：{question}", f"当前数据源：{database_id}"]
    if capabilities:
        context.append("数据源清单：" + json.dumps(capabilities, ensure_ascii=False))
    if probe_summary:
        context.append("已探查事实：" + probe_summary)
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    try:
        response = chat_model.invoke([
            SystemMessage(content=PLAN_PROMPT),
            HumanMessage(content="\n".join(context)),
        ])
    except BudgetExceededError:
        # 预算耗尽必须上抛：计划可以没有，但「没预算了」不能被降级成
        # 「计划生成失败」然后继续循环。
        raise
    except Exception:
        return None, usage
    from .graph import _usage_from_message  # 复用同一套用量解析，避免两套口径

    usage = _usage_from_message(response)
    plan = _extract_json(getattr(response, "content", "") or "")
    if not plan:
        return None, usage
    return normalize_plan(plan, database_id=database_id), usage
