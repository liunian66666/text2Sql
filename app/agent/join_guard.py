"""把「SQL 用到的连接」与「图里声明的连接」对账。

这不是把 SQL 交给解析器做完整语义分析（项目里没有 sqlglot，也不打算为此加
依赖），而是两条高价值的确定性检查：

1. **表外溢**：SQL 里出现了候选路径之外的表 —— 说明模型自己找了另一条连接
   路径，或者干脆跨了业务域。
2. **复合外键退化成单列**：图里明确的复合 FK，SQL 的 ON 只用了其中一列。
   这是实测会把行数从 121,317 放大到 296,983 的那类错误。

检查是「提示性」的：写进结果元数据与 ToolMessage，不直接拒绝执行 ——
拒绝需要可解释的业务规则，而这里只能证明「与图不一致」。
"""
from __future__ import annotations

import re
from typing import Any

_IDENT = r'(?:"[^"]+"|\[[^\]]+\]|`[^`]+`|[A-Za-z_][\w.]*)'
_TABLE_RE = re.compile(rf"\b(?:FROM|JOIN)\s+({_IDENT})", re.IGNORECASE)
_CTE_RE = re.compile(rf"(?:\bWITH\b|,)\s*({_IDENT})\s+AS\s*\(", re.IGNORECASE)


def _clean(identifier: str) -> str:
    token = identifier.strip()
    parts = re.findall(r'[^"\[\]`]+', token)
    return ".".join(part.strip() for part in parts if part.strip())


def _short(table: str) -> str:
    """`Sales.SalesOrderHeader` → `SalesOrderHeader`；已是短名则原样返回。"""
    return table.rsplit(".", 1)[-1]


def extract_cte_names(sql: str) -> set[str]:
    return {_short(_clean(match)) for match in _CTE_RE.findall(sql or "")}


def extract_tables(sql: str) -> list[str]:
    """Physical tables referenced by FROM/JOIN, CTEs and subqueries excluded."""
    ctes = extract_cte_names(sql)
    tables: list[str] = []
    for match in _TABLE_RE.findall(sql or ""):
        name = _clean(match)
        if not name or name.startswith("("):
            continue
        if _short(name) in ctes:
            continue
        if _short(name).lower() in {"select", "values"}:
            continue
        if name not in tables:
            tables.append(name)
    return tables


def _path_tables(context: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for path in context.get("paths") or []:
        for table in path.get("tables") or []:
            names.add(_short(str(table)))
    for table in context.get("tables") or []:
        names.add(_short(str(table)))
    return names


def validate_sql_joins(sql: str, context: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return deterministic warnings; empty list means "consistent with the graph"."""
    if not context or not context.get("paths"):
        return []
    warnings: list[dict[str, Any]] = []
    used = extract_tables(sql)
    used_short = {_short(table) for table in used}
    allowed = _path_tables(context)
    outside = sorted(used_short - allowed)
    if outside:
        warnings.append({
            "kind": "unexpected_table",
            "tables": outside,
            "detail": ("SQL 使用了候选连接路径之外的表：" + "、".join(outside)
                       + "。请确认这是有意的额外关联，而不是绕开了图里声明的路径。"),
            "checked_against": sorted(allowed),
        })
    lowered = (sql or "").lower()
    for path in context.get("paths") or []:
        for condition in path.get("join_conditions") or []:
            if not condition.get("composite"):
                continue
            columns = [str(item) for item in
                       (condition.get("source_columns") or []) + (condition.get("target_columns") or [])]
            if not columns:
                continue
            missing = [column for column in columns if column.lower() not in lowered]
            if missing:
                warnings.append({
                    "kind": "composite_fk_incomplete",
                    "relation_id": condition.get("relation_id"),
                    "missing_columns": missing,
                    "detail": ("复合外键只用了部分列：" + "、".join(missing)
                               + " 未出现在 SQL 中，可能导致行数放大。"),
                })
                break
    return warnings


__all__ = ["extract_tables", "extract_cte_names", "validate_sql_joins"]
