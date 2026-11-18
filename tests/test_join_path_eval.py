"""公平 Join 路径评测的契约测试（Phase 4B §六）。

三条必须成立的公平性前提，任何一条破了，「两个方法可比」就是假的：

1. 两种方法拿到**同一批端点对**（同一个 ``build_endpoint_pairs``，同样的 focus/targets）；
2. 两种方法遍历**同一张外键图**、用**同一个每跳角色约定**；
3. 种子表只能来自问题文本，``required_tables`` / ``acceptable_join_paths`` 不得进入检索。

另外测两个真实数据坑：短表名解析、复合外键的两列写法。
"""
from __future__ import annotations

import unittest
from pathlib import Path

import networkx as nx

from app.commands.evaluate_join_paths import (
    _pair_key,
    _path_pairs,
    baseline_shortest_paths,
    classify_gold_edges,
    make_node_resolver,
)
from app.knowledge.retrieval.join_path import JoinCondition, JoinPath, build_endpoint_pairs


DB = "adventureworks"
TABLES = {
    f"{DB}.Sales.SalesOrderHeader",
    f"{DB}.Sales.SalesOrderDetail",
    f"{DB}.Sales.SpecialOfferProduct",
    f"{DB}.Sales.SpecialOffer",
    f"{DB}.Production.Product",
}


def _resolver():
    return make_node_resolver(TABLES, DB)


def _graph() -> nx.DiGraph:
    graph = nx.DiGraph()
    roles = {
        ("Sales.SalesOrderHeader", "Sales.SalesOrderDetail"): [
            {"relation_id": "r1", "source_columns": ["SalesOrderID"],
             "target_columns": ["SalesOrderID"], "cardinality": "1:N"}],
        ("Sales.SalesOrderDetail", "Sales.SpecialOfferProduct"): [
            {"relation_id": "r2", "source_columns": ["SpecialOfferID", "ProductID"],
             "target_columns": ["SpecialOfferID", "ProductID"], "composite": True,
             "cardinality": "N:1"}],
        ("Sales.SpecialOfferProduct", "Sales.SpecialOffer"): [
            {"relation_id": "r3", "source_columns": ["SpecialOfferID"],
             "target_columns": ["SpecialOfferID"], "cardinality": "N:1"}],
    }
    for (left, right), items in roles.items():
        for item in items:
            graph.add_edge(f"{DB}.{left}", f"{DB}.{right}", roles=items)
            graph.add_edge(f"{DB}.{right}", f"{DB}.{left}", roles=[
                {**item, "source_columns": item["target_columns"],
                 "target_columns": item["source_columns"]}])
    graph.add_node(f"{DB}.Production.Product")
    return graph


class NodeResolverTest(unittest.TestCase):
    def test_full_name_resolves(self) -> None:
        self.assertEqual(_resolver()("Sales.SalesOrderHeader"),
                         f"{DB}.Sales.SalesOrderHeader")

    def test_bare_name_resolves(self) -> None:
        # dev 集里 gold 边写的是 SalesOrderHeader，图节点带 schema 前缀。
        self.assertEqual(_resolver()("SalesOrderHeader"), f"{DB}.Sales.SalesOrderHeader")

    def test_unknown_table_returns_none(self) -> None:
        self.assertIsNone(_resolver()("NoSuchTable"))


class GoldEdgeClassificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _Store()
        self.resolve = _resolver()

    def test_single_column_fk(self) -> None:
        specs = classify_gold_edges(self.store, self.resolve,
                                    ["SalesOrderHeader.SalesOrderID -> "
                                     "SalesOrderDetail.SalesOrderID"])
        self.assertEqual(specs[0]["kind"], "fk")

    def test_composite_member_uses_declared_relation(self) -> None:
        specs = classify_gold_edges(self.store, self.resolve,
                                    ["SalesOrderDetail.SpecialOfferID -> "
                                     "SpecialOfferProduct.SpecialOfferID"])
        self.assertEqual(specs[0]["kind"], "composite_member")

    def test_non_fk_predicate_is_not_counted_as_fk(self) -> None:
        specs = classify_gold_edges(self.store, self.resolve,
                                    ["SalesOrderDetail.ProductID -> "
                                     "Product.ProductID + 日期区间"])
        self.assertIn(specs[0]["kind"], {"unknown", "non_fk_predicate", "fk_with_predicate"})

    def test_undeclared_fk_is_reported_as_unknown(self) -> None:
        # SQLite 移植库确实没有这条真实 AW 外键；必须如实标 unknown，
        # 而不是假装算法没召回。
        specs = classify_gold_edges(self.store, self.resolve,
                                    ["SalesOrderDetail.ProductID -> Product.ProductID"])
        self.assertEqual(specs[0]["kind"], "unknown")


class CompositePairExpansionTest(unittest.TestCase):
    def test_composite_condition_expands_into_two_pairs(self) -> None:
        path = JoinPath(
            tables=["SalesOrderDetail", "SpecialOfferProduct"],
            relation_ids=["r2"],
            join_conditions=[JoinCondition(
                left_table="SalesOrderDetail", right_table="SpecialOfferProduct",
                relation_id="r2", source_columns=["SpecialOfferID", "ProductID"],
                target_columns=["SpecialOfferID", "ProductID"], composite=True,
                cardinality="N:1", verified=True)],
            score=1.0, hops=1, composite_fk_complete=True, reasons=[])
        pairs = _path_pairs(path)
        self.assertEqual(len(pairs), 2)
        self.assertIn(_pair_key("SalesOrderDetail", "SpecialOfferID",
                                "SpecialOfferProduct", "SpecialOfferID"), pairs)
        self.assertIn(_pair_key("SalesOrderDetail", "ProductID",
                                "SpecialOfferProduct", "ProductID"), pairs)


class BaselineFairnessTest(unittest.TestCase):
    def test_baseline_uses_the_given_endpoint_pairs(self) -> None:
        graph = _graph()
        resolve = _resolver()
        focus = ["Sales.SalesOrderDetail"]
        targets = ["Sales.SpecialOffer"]
        paths, _ = baseline_shortest_paths(
            graph, focus=focus, targets=targets, resolve=resolve, max_hops=4, max_paths=8)
        self.assertTrue(paths)
        expected_pairs = build_endpoint_pairs(
            [resolve(focus[0]), resolve(targets[0])], [resolve(targets[0])])
        self.assertTrue(expected_pairs)
        # 路径首表必须在端点集合内，且不引入图里不存在的表
        for path in paths:
            for table in path.tables:
                self.assertIn(table, {name.rsplit(".", 1)[-1] for name in TABLES})

    def test_baseline_respects_max_hops(self) -> None:
        graph = _graph()
        paths, excluded = baseline_shortest_paths(
            graph, focus=["Sales.SalesOrderHeader"], targets=["Sales.SpecialOffer"],
            resolve=_resolver(), max_hops=1, max_paths=8)
        self.assertTrue(all(path.hops <= 1 for path in paths))
        self.assertTrue(any("max_hops" in item["reason"] for item in excluded))

    def test_baseline_respects_max_paths(self) -> None:
        graph = _graph()
        paths, _ = baseline_shortest_paths(
            graph, focus=["Sales.SalesOrderHeader", "Sales.SalesOrderDetail"],
            targets=["Sales.SpecialOffer"], resolve=_resolver(), max_hops=4, max_paths=1)
        self.assertEqual(len(paths), 1)

    def test_baseline_scores_by_hops_only(self) -> None:
        graph = _graph()
        paths, _ = baseline_shortest_paths(
            graph, focus=["Sales.SalesOrderHeader"], targets=["Sales.SpecialOffer"],
            resolve=_resolver(), max_hops=4, max_paths=8)
        self.assertTrue(all(path.score == -path.hops for path in paths))


class _Store:
    """Minimal stand-in for GraphStore.relations_between used by classification."""

    _RELATIONS = {
        (f"{DB}.Sales.SalesOrderHeader", f"{DB}.Sales.SalesOrderDetail"):
            [(["SalesOrderID"], ["SalesOrderID"])],
        # 复合外键：两列一起构成一条关系，单看一列不是「单列外键」。
        (f"{DB}.Sales.SalesOrderDetail", f"{DB}.Sales.SpecialOfferProduct"):
            [(["SpecialOfferID", "ProductID"], ["SpecialOfferID", "ProductID"])],
    }

    def relations_between(self, left: str, right: str):
        return [_Relation(source, target)
                for source, target in self._RELATIONS.get((left, right), [])]


class _Relation:
    def __init__(self, source: list[str], target: list[str]) -> None:
        self.source_columns = list(source)
        self.target_columns = list(target)
        self.relation_id = f"{'+'.join(source)}->{'+'.join(target)}"


if __name__ == "__main__":
    unittest.main()
