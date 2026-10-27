from __future__ import annotations

from numbers import Number
from typing import Any


def _number(value: Any) -> bool:
    return isinstance(value, Number) and not isinstance(value, bool)


CHART_TYPES = ("metric", "bar", "line", "pie", "table")

# 用户显式指定图表类型时的可读性上限。自动档保持原有行为不变（它按意图选图，
# 演示题目本来就落在这些上限内），只有显式请求会被校验：要 10 个扇区的饼图
# 只会糊成一团，这时退回自动图并如实报告，而不是硬画。
MAX_SLICES = 8
MAX_BARS = 20
MAX_POINTS = 60


def _layout(columns: list[str], rows: list[list[Any]]) -> tuple[list[int], int, int | None]:
    numeric = _numeric_indexes(columns, rows)
    label_index = next((index for index in range(len(columns)) if index not in numeric), 0)
    value_index = None
    if numeric:
        value_index = next((index for index in numeric if any(key in columns[index].lower() for key in
                           ("销售", "金额", "revenue", "amount", "数量", "count"))), numeric[0])
    return numeric, label_index, value_index


def _numeric_indexes(columns: list[str], rows: list[list[Any]]) -> list[int]:
    return [index for index in range(len(columns)) if any(index < len(row) and _number(row[index]) for row in rows)]


def _auto_chart(intent: str, columns: list[str], rows: list[list[Any]]) -> dict[str, Any] | None:
    if not columns or not rows:
        return None
    numeric, label_index, value_index = _layout(columns, rows)
    if not numeric:
        return {"type": "table", "title": "查询结果"}
    if len(rows) == 1 and len(numeric) <= 4:
        return {
            "type": "metric",
            "title": "核心指标",
            "items": [{"name": columns[index], "value": rows[0][index]} for index in numeric],
        }
    labels = [str(row[label_index]) for row in rows]
    if intent == "trend":
        return {
            "type": "line", "title": "趋势分析", "labels": labels,
            "series": [{"name": columns[index], "values": [row[index] for row in rows]} for index in numeric[:3]],
        }
    if intent == "proportion":
        return {
            "type": "pie", "title": "占比分析",
            "items": [{"name": label, "value": row[value_index]} for label, row in zip(labels, rows)],
        }
    if intent == "ranking":
        return {
            "type": "bar", "title": "排名分析", "labels": labels,
            "series": [{"name": columns[value_index], "values": [row[value_index] for row in rows]}],
        }
    return {"type": "table", "title": "查询结果"}


def _explicit_chart(kind: str, columns: list[str], rows: list[list[Any]]) -> dict[str, Any] | None:
    """Build one requested chart kind, or None when the data cannot support it."""
    if not columns or not rows:
        return None
    numeric, label_index, value_index = _layout(columns, rows)
    if kind == "table":
        return {"type": "table", "title": "查询结果"}
    if kind == "metric":
        if len(rows) != 1 or not 1 <= len(numeric) <= 4:
            return None
        return {"type": "metric", "title": "核心指标",
                "items": [{"name": columns[index], "value": rows[0][index]} for index in numeric]}
    if not numeric or value_index is None:
        return None
    labels = [str(row[label_index]) for row in rows]
    if kind == "pie":
        if not 2 <= len(rows) <= MAX_SLICES:
            return None
        return {"type": "pie", "title": "占比分析",
                "items": [{"name": label, "value": row[value_index]} for label, row in zip(labels, rows)]}
    if kind == "bar":
        if len(rows) > MAX_BARS:
            return None
        return {"type": "bar", "title": f"{columns[value_index]}对比", "labels": labels,
                "series": [{"name": columns[value_index], "values": [row[value_index] for row in rows]}]}
    if kind == "line":
        if len(rows) > MAX_POINTS:
            return None
        return {"type": "line", "title": "趋势分析", "labels": labels,
                "series": [{"name": columns[index], "values": [row[index] for row in rows]} for index in numeric[:3]]}
    return None


def build_visualization(intent: str, columns: list[str], rows: list[list[Any]],
                        chart_type: str | None = None) -> dict[str, Any] | None:
    """Pick a chart by intent, optionally overridden by an explicit request.

    ``chart_type`` never degrades silently into a broken drawing: an unknown
    name or a shape the data cannot support (12 pie slices, a metric card for 3
    rows) falls back to the intent-based chart, and the caller reports which
    type actually won so the model can tell the user honestly.
    """
    auto = _auto_chart(intent, columns, rows)
    if auto is None or chart_type is None:
        return auto
    kind = str(chart_type).strip().lower()
    if kind not in CHART_TYPES or kind == auto["type"]:
        return auto
    return _explicit_chart(kind, columns, rows) or auto


def summarize_result(intent: str, columns: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "查询执行成功，但当前条件下没有匹配的数据。"
    if len(rows) == 1 and len(columns) == 1:
        return f"查询结果为 {rows[0][0]}。"
    if intent == "ranking":
        return f"共返回 {len(rows)} 条排名结果，排名第一的是 {rows[0][0]}。"
    if intent == "trend":
        return f"共返回 {len(rows)} 个时间点，可在趋势图中查看变化。"
    if intent == "proportion":
        return f"共返回 {len(rows)} 个分类，可在占比图中比较各分类贡献。"
    return f"查询成功，共返回 {len(rows)} 行、{len(columns)} 列数据。"
