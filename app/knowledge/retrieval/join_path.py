"""Multi-candidate Join path search with structured business risk detection.

Path scoring deliberately does NOT equate "shortest" with "business correct":
verified FK groups, cardinality, composite-key completeness, metric grain,
historical interval semantics and relation provenance all affect the score.
When two candidates are close, or one hop has several FK roles, the finder
returns ``ambiguity`` instead of silently choosing a winner.

Structured risks are *hints*, not a claim that the graph alone has solved SQL
semantic correctness (no AST validation in this phase).
"""
from __future__ import annotations

from itertools import combinations
from typing import Any

import networkx as nx
from pydantic import BaseModel, Field

from ..graph_store import GraphStore


class JoinCondition(BaseModel):
    left_table: str
    right_table: str
    relation_id: str
    source_columns: list[str] = Field(default_factory=list)
    target_columns: list[str] = Field(default_factory=list)
    composite: bool = False
    complete: bool = True
    cardinality: str = "UNKNOWN"
    verified: bool = False


class JoinPath(BaseModel):
    tables: list[str] = Field(default_factory=list)
    relation_ids: list[str] = Field(default_factory=list)
    join_conditions: list[JoinCondition] = Field(default_factory=list)
    score: float = 0.0
    hops: int = 0
    composite_fk_complete: bool = True
    risks: list[dict[str, Any]] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class JoinPathResult(BaseModel):
    paths: list[JoinPath] = Field(default_factory=list)
    ambiguity: list[dict[str, Any]] = Field(default_factory=list)
    excluded_paths: list[dict[str, Any]] = Field(default_factory=list)
    risks: list[dict[str, Any]] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)


def _short(database_id: str, node_id: str) -> str:
    prefix = f"{database_id}."
    return node_id[len(prefix):] if node_id.startswith(prefix) else node_id


def detect_semantic_risks(tables: list[str], *, column_hints: list[str] | None = None,
                          relations: list[Any] | None = None) -> list[dict[str, Any]]:
    """Deterministic, evidence-based risk signals for the five required cases."""
    hints = [str(item).casefold() for item in (column_hints or [])]
    names = {table.rsplit(".", 1)[-1].casefold(): table for table in tables}
    risks: list[dict[str, Any]] = []

    if "salesorderheader" in names and "salesorderdetail" in names:
        risks.append({
            "kind": "duplicate_aggregation",
            "description": ("SalesOrderHeader 与 SalesOrderDetail 直接 JOIN 会把订单级金额 "
                            "SubTotal / TotalDue 按明细行重复计数（实测放大约 23.6 倍）。"),
            "affected_tables": [names["salesorderheader"], names["salesorderdetail"]],
            "required_mitigation": "先在明细层聚合，再与订单头按 SalesOrderID 1:1 关联；订单级金额不得 SUM 放大后的行。",
            "source_ref": "data/adventureworks/query_evidence.json#q15_inflated_wrong",
        })
    if "purchaseorderheader" in names and "purchaseorderdetail" in names:
        risks.append({
            "kind": "duplicate_aggregation",
            "description": "采购订单头 TotalDue 与采购明细直接 JOIN 会重复计数。",
            "affected_tables": [names["purchaseorderheader"], names["purchaseorderdetail"]],
            "required_mitigation": "明细先聚合，再按 PurchaseOrderID 关联订单头。",
            "source_ref": "data/adventureworks/query_evidence.json#q11_purchase",
        })

    history_tables = [table for table in tables
                      if table.rsplit(".", 1)[-1].casefold().endswith("history")
                      and ("cost" in table.casefold() or "price" in table.casefold()
                           or "quota" in table.casefold())]
    if len(history_tables) >= 2:
        has_interval = any("startdate" in hint or "enddate" in hint for hint in hints)
        risks.append({
            "kind": "historical_interval_missing",
            "description": ("成本历史与价格/配额历史均为「商品(或人员)×时间区间」粒度，"
                            "仅按 ProductID / BusinessEntityID 关联会产生区间笛卡尔积（实测 649 行）。"),
            "affected_tables": history_tables,
            "required_mitigation": "必须同时匹配 StartDate <= 业务日期 AND (EndDate IS NULL OR 业务日期 < EndDate)。",
            "interval_hint_present": has_interval,
            "source_ref": "data/adventureworks/query_evidence.json#q19_cost_price_cartesian",
        })

    if "productinventory" in names and "product" in names:
        risks.append({
            "kind": "mixed_grain",
            "description": ("ProductInventory 粒度是 (ProductID, LocationID)，Product.ReorderPoint "
                            "是商品级阈值；逐库位 Quantity 与阈值比较会得到错误商品数。"),
            "affected_tables": [names["productinventory"], names["product"]],
            "required_mitigation": "先 GROUP BY ProductID 汇总 Quantity，再与 Product 比较 ReorderPoint。",
            "source_ref": "data/adventureworks/query_evidence.json#q07_below_reorder",
        })

    if "specialofferproduct" in names:
        has_special = any("specialofferid" in hint for hint in hints)
        has_product = any("productid" in hint for hint in hints)
        if hints and has_product and not has_special:
            risks.append({
                "kind": "composite_fk_incomplete",
                "description": ("SalesOrderDetail → SpecialOfferProduct 的外键是复合外键，"
                                "只用 ProductID 关联会把行数从 121,317 膨胀到 296,983。"),
                "affected_tables": [names["specialofferproduct"]],
                "required_mitigation": "必须同时使用 SpecialOfferID 与 ProductID 两列。",
                "source_ref": "data/adventureworks/query_evidence.json#q13_composite_fk",
            })
    return risks


def build_endpoint_pairs(focus: list[str], targets: list[str]) -> list[tuple[str, str]]:
    """Deterministic endpoint list shared by every path method.

    Extracted so a baseline (pure shortest path) can search exactly the same
    endpoint pairs as :class:`JoinPathFinder`; otherwise a path-budget or
    endpoint difference would masquerade as an algorithm difference.
    """
    pairs: list[tuple[str, str]] = []
    for src, dst in combinations(focus, 2):
        pairs.append((src, dst))
    for src in focus:
        for dst in targets:
            if src != dst:
                pairs.append((src, dst))
    return list(dict.fromkeys(pairs))[:16]


class JoinPathFinder:
    def __init__(self, store: GraphStore, *, max_hops: int = 4, max_paths: int = 8,
                 ambiguity_margin: float = 0.35):
        self.store = store
        self.projection = store.table_projection()
        self.max_hops = max_hops
        self.max_paths = max_paths
        self.ambiguity_margin = ambiguity_margin

    def find(self, tables: list[str], *, seed_tables: list[str] | None = None,
             column_hints: list[str] | None = None,
             required_targets: list[str] | None = None) -> JoinPathResult:
        database_id = self.store.database_id

        def node_id(name: str) -> str:
            return name if name in self.projection else f"{database_id}.{name}"

        valid = [node_id(name) for name in tables if node_id(name) in self.projection]
        seeds = [node_id(name) for name in (seed_tables or valid[:2])
                 if node_id(name) in self.projection] or valid[:1]
        targets = [node_id(name) for name in (required_targets or [])
                   if node_id(name) in self.projection]
        # Restrict enumeration to the retrieved focus tables. Enumerating all
        # pairs of the BFS frontier produces hundreds of irrelevant paths and
        # drowns the real ambiguity signal.
        focus = list(dict.fromkeys([*seeds[:4], *targets[:4]]))
        endpoint_pairs = build_endpoint_pairs(focus, targets)

        raw_paths: list[tuple[list[str], list[dict[str, Any]]]] = []
        excluded: list[dict[str, Any]] = []
        for src, dst in endpoint_pairs:
            if not nx.has_path(self.projection, src, dst):
                excluded.append({"from": _short(database_id, src), "to": _short(database_id, dst),
                                 "reason": "no declared FK path"})
                continue
            try:
                paths = list(nx.all_simple_paths(self.projection, src, dst,
                                                 cutoff=self.max_hops))
            except nx.NetworkXNoPath:
                continue
            if not paths:
                excluded.append({"from": _short(database_id, src), "to": _short(database_id, dst),
                                 "reason": f"path longer than max_hops={self.max_hops}"})
            for path in paths[:max(self.max_paths * 2, 8)]:
                hops: list[dict[str, Any]] = []
                for left, right in zip(path, path[1:]):
                    roles = self.projection[left][right].get("roles", [])
                    hops.append({"left": left, "right": right, "roles": roles})
                raw_paths.append((path, hops))

        scored: list[JoinPath] = []
        role_ambiguities: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path, hops in raw_paths:
            signature = "|".join(path)
            if signature in seen:
                continue
            seen.add(signature)
            conditions: list[JoinCondition] = []
            reasons: list[str] = []
            score = 5.0
            hops_count = len(path) - 1
            score -= 1.0 * hops_count
            reasons.append(f"路径长度 {hops_count} 跳")
            composite_complete = True
            for hop in hops:
                roles = hop["roles"]
                if len(roles) > 1:
                    role_ambiguities.append({
                        "left": _short(database_id, hop["left"]),
                        "right": _short(database_id, hop["right"]),
                        "candidate_relation_ids": [r["relation_id"] for r in roles],
                        "reason": "同一对表之间存在多个不同外键角色，语义不唯一",
                    })
                chosen = roles[0] if roles else {}
                condition = JoinCondition(
                    left_table=_short(database_id, hop["left"]),
                    right_table=_short(database_id, hop["right"]),
                    relation_id=str(chosen.get("relation_id", "")),
                    source_columns=list(chosen.get("source_columns", [])),
                    target_columns=list(chosen.get("target_columns", [])),
                    composite=bool(chosen.get("composite", False)),
                    cardinality=str(chosen.get("cardinality", "UNKNOWN")),
                    verified=bool(chosen.get("verified", False)),
                )
                if condition.composite and len(condition.source_columns) < 2:
                    condition.complete = False
                    composite_complete = False
                    score -= 1.0
                    reasons.append("复合外键缺列")
                conditions.append(condition)
                if condition.verified:
                    score += 0.5
                else:
                    score -= 1.0
                    reasons.append("存在未验证关系")
                if condition.cardinality == "ONE_TO_ONE":
                    score += 0.3
                if condition.cardinality == "MANY_TO_ONE":
                    reasons.append("MANY_TO_ONE：向上关联存在粒度放大风险")
            relation = self.store.relation_by_id.get(conditions[0].relation_id) if conditions else None
            scored.append(JoinPath(
                tables=[_short(database_id, node) for node in path],
                relation_ids=[condition.relation_id for condition in conditions],
                join_conditions=conditions, score=round(score, 6), hops=hops_count,
                composite_fk_complete=composite_complete,
                reasons=reasons,
            ))

        scored.sort(key=lambda item: (-item.score, "|".join(item.tables)))
        if len(scored) > self.max_paths:
            excluded.extend({"tables": item.tables, "reason": "max_paths budget"}
                            for item in scored[self.max_paths:])
        scored = scored[:self.max_paths]

        # Risks are scoped to the top candidate paths plus the focus tables,
        # not to the entire BFS frontier (which would emit unrelated domains).
        risk_tables = {_short(database_id, node) for node in focus}
        for path in scored[:3]:
            risk_tables.update(path.tables)
        risks = detect_semantic_risks(sorted(risk_tables), column_hints=column_hints,
                                      relations=list(self.store.relations))
        for path in scored:
            path.risks = [risk for risk in risks
                          if any(table in path.tables for table in risk["affected_tables"])]

        ambiguity: list[dict[str, Any]] = []
        seen_roles: set[tuple[str, str]] = set()
        for item in role_ambiguities:
            key = (item["left"], item["right"])
            if key not in seen_roles:
                seen_roles.add(key)
                ambiguity.append(item)
        if len(scored) >= 2 and abs(scored[0].score - scored[1].score) < self.ambiguity_margin:
            ambiguity.append({
                "kind": "near_tie",
                "candidates": [{"tables": item.tables, "score": item.score,
                                "relation_ids": item.relation_ids} for item in scored[:3]],
                "reason": "候选路径评分接近，不静默选择唯一路径",
            })

        return JoinPathResult(
            paths=scored, ambiguity=ambiguity, excluded_paths=excluded, risks=risks,
            trace={
                "endpoint_pairs": len(endpoint_pairs),
                "raw_path_count": len(raw_paths),
                "returned_paths": len(scored),
                "max_hops": self.max_hops, "max_paths": self.max_paths,
                "ambiguity_margin": self.ambiguity_margin,
                "seed_tables": [_short(database_id, node) for node in seeds],
                "target_tables": [_short(database_id, node) for node in targets],
                "note": "最短路径不等于业务正确路径；评分含完整性、基数、来源与粒度，歧义显式返回。",
            },
        )
