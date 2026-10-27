from __future__ import annotations

from numbers import Number
from typing import Any


def _number(value: Any) -> bool:
    return isinstance(value, Number) and not isinstance(value, bool)


def _numeric_indexes(columns: list[str], rows: list[list[Any]]) -> list[int]:
    return [index for index in range(len(columns)) if any(index < len(row) and _number(row[index]) for row in rows)]


def build_visualization(intent: str, columns: list[str], rows: list[list[Any]]) -> dict[str, Any] | None:
    if not columns or not rows:
        return None
    numeric = _numeric_indexes(columns, rows)
    if not numeric:
        return {"type": "table", "title": "查询结果"}
    label_index = next((index for index in range(len(columns)) if index not in numeric), 0)
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
    value_index = next((index for index in numeric if any(key in columns[index].lower() for key in
                        ("销售", "金额", "revenue", "amount", "数量", "count"))), numeric[0])
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
