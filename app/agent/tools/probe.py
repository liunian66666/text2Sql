"""Lightweight read-only probes for deterministic database facts.

Creating a full Text-to-SQL pipeline run just to learn ``MIN(order_date)`` is
both slow and wasteful — and in this Agent it also consumed the single SQL slot
that the real analysis needed. Deterministic facts (date coverage, row counts,
column existence, low-cardinality value lists) are answered here with
template-built SQL against the whitelisted schema.
"""
from __future__ import annotations

from datetime import date
import json
from typing import Any

PROBE_TYPES = ("date_range", "row_count", "distinct_values", "exists")
MAX_DISTINCT_LIMIT = 20

# Never expose high-risk columns through a value listing probe.
_SENSITIVE_HINTS = (
    "password", "passwd", "secret", "token", "ssn", "socialsecurity", "phone", "mobile",
    "email", "card", "iban", "account", "birth", "addressline", "postalcode", "salary",
    "身份证", "手机", "电话", "邮箱", "密码", "住址", "工资", "薪资",
)


def is_sensitive_column(column: str) -> bool:
    normalized = column.replace("_", "").casefold()
    return any(hint in normalized for hint in _SENSITIVE_HINTS)


def last_complete_year(min_value: Any, max_value: Any, today: date | None = None) -> tuple[int | None, str]:
    """Derive the latest *complete* calendar year without guessing.

    Reality and database coverage disagree in this project's demo data: the
    order table runs to 2026-12-31 while "today" is still inside 2026. Taking
    ``MAX(order_date)`` as "the newest complete year" would silently analyse a
    partial year, so the current calendar year is never treated as complete.
    """
    today = today or date.today()
    try:
        min_year = int(str(min_value)[:4])
        max_year = int(str(max_value)[:4])
    except (TypeError, ValueError):
        return None, "无法从日期值解析年份，未推断完整年度"
    candidate = min(today.year - 1, max_year)
    if candidate < min_year:
        return None, f"库内数据仅覆盖 {min_year}–{max_year}，不含任何完整自然年"
    # 完整年度要求库内覆盖该年年初（1 月内即视为覆盖整年；允许年初几天的自然缺失）
    covered_from_start = str(min_value)[:10] <= f"{candidate}-01-31"
    if not covered_from_start:
        return None, f"库内最早数据为 {str(min_value)[:10]}，未覆盖 {candidate} 年起点"
    # 同样要求覆盖到年末：数据只到 6 月时，该年不能算完整年（真实踩过的坑：
    # 2025-06-29 的最大日期曾被误判为「2025 年完整」）。
    covered_to_end = str(max_value)[:10] >= f"{candidate}-12-01"
    if not covered_to_end:
        fallback = candidate - 1
        if fallback < min_year or str(min_value)[:10] > f"{fallback}-01-31":
            return None, (f"库内数据 {str(min_value)[:10]} → {str(max_value)[:10]}，"
                          f"没有任何完整自然年")
        return fallback, (f"现实时间 {today.isoformat()}；库内 {candidate} 年数据仅到 "
                          f"{str(max_value)[:10]}，不完整；最近一个完整自然年为 {fallback} 年")
    reason = (f"现实时间 {today.isoformat()}，{candidate} 年之后的年度均不完整；"
              f"库内数据覆盖 {str(min_value)[:10]} → {str(max_value)[:10]}")
    if max_year > candidate:
        reason += f"（库内 {max_year} 年数据不代表完整年度）"
    return candidate, reason


class ProbeRunner:
    """Execute whitelisted probes against one read-only SQLite database."""

    def __init__(self, database_id: str, path, tables: dict[str, set[str]]):
        self.database_id = database_id
        self.path = path
        self.tables = tables

    def table_name(self, requested: str) -> str:
        for table in self.tables:
            if table == requested:
                return table
        lowered = requested.casefold()
        for table in self.tables:
            if table.casefold() == lowered:
                return table
        raise ValueError(f"数据源 {self.database_id} 中不存在表 {requested}")

    def column_name(self, table: str, requested: str) -> str:
        for column in self.tables[table]:
            if column == requested:
                return column
        lowered = requested.casefold()
        for column in self.tables[table]:
            if column.casefold() == lowered:
                return column
        raise ValueError(f"表 {table} 中不存在字段 {requested}")

    def run(self, probe: str, table: str, column: str = "", limit: int = 10) -> dict[str, Any]:
        import sqlite3
        from contextlib import closing

        probe = (probe or "").strip().lower()
        if probe not in PROBE_TYPES:
            raise ValueError(f"不支持的探查类型 {probe}，允许：{', '.join(PROBE_TYPES)}")
        resolved_table = self.table_name(table)
        resolved_column = ""
        if probe != "row_count":
            if not column:
                raise ValueError(f"{probe} 需要指定 column")
            resolved_column = self.column_name(resolved_table, column)
        quoted_table = '"' + resolved_table.replace('"', '""') + '"'
        quoted_column = ('"' + resolved_column.replace('"', '""') + '"') if resolved_column else ""
        payload: dict[str, Any] = {
            "probe": probe, "database_id": self.database_id,
            "table": resolved_table, "column": resolved_column or None,
        }
        if probe == "exists":
            payload.update({"exists": True, "table_columns": sorted(self.tables[resolved_table])[:60]})
            return payload
        if probe == "distinct_values" and is_sensitive_column(resolved_column):
            raise ValueError(f"字段 {resolved_column} 属于敏感字段，禁止值探查")
        bounded = max(1, min(int(limit or 10), MAX_DISTINCT_LIMIT))
        with closing(sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only = ON")
            if probe == "row_count":
                sql = f"SELECT COUNT(*) FROM {quoted_table}"
                value = connection.execute(sql).fetchone()[0]
                payload.update({"row_count": int(value), "sql": sql})
                return payload
            if probe == "date_range":
                sql = (f"SELECT MIN({quoted_column}), MAX({quoted_column}), "
                       f"COUNT(DISTINCT {quoted_column}) FROM {quoted_table}")
                low, high, distinct = connection.execute(sql).fetchone()
                year, reason = last_complete_year(low, high)
                payload.update({
                    "sql": sql, "min": low, "max": high, "distinct_count": int(distinct or 0),
                    "real_today": date.today().isoformat(),
                    "coverage": f"{low} → {high}",
                    "last_complete_year": year, "last_complete_year_reason": reason,
                })
                return payload
            sql = (f"SELECT {quoted_column}, COUNT(*) AS c FROM {quoted_table} "
                   f"GROUP BY {quoted_column} ORDER BY c DESC LIMIT {bounded}")
            rows = connection.execute(sql).fetchall()
            payload.update({
                "sql": sql, "values": [[row[0], int(row[1])] for row in rows], "limit": bounded,
            })
            return payload


def probe_cache_key(probe: str, table: str, column: str, limit: int) -> str:
    return json.dumps([probe, table, column, limit], ensure_ascii=False)
