"""The ten mandatory Phase 2/3 acceptance cases + runtime integration tests.

Every assertion runs against the real AdventureWorks knowledge artifacts and
the real read-only schema; no model endpoint and no mock retrieval scores.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from app.agent.tools.registry import ToolRegistry
from app.commands import evaluate_graphrag
from app.knowledge.enhancement import build_enhancement, is_enabled
from app.knowledge.graph_store import GraphStore
from app.knowledge.retrieval.join_path import JoinPathFinder, detect_semantic_risks
from app.knowledge.retrieval.subgraph import SubgraphExpander
from app.knowledge.schema_extractor import PROJECT_ROOT

ROOT = PROJECT_ROOT
ARTIFACTS = ROOT / "workspace" / "knowledge" / "adventureworks"


class GraphFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ARTIFACTS / "relations.json").is_file():
            raise unittest.SkipTest("AdventureWorks knowledge artifacts missing")
        cls.store = GraphStore.from_artifacts(ARTIFACTS)
        cls.expander = SubgraphExpander(cls.store)
        cls.finder = JoinPathFinder(cls.store)


class MandatoryCases(GraphFixture):
    def test_01_product_to_category_completes_intermediate_table(self):
        result = self.expander.expand(
            ["Production.Product", "Production.ProductCategory"], [],
            target_tables=["Production.Product", "Production.ProductCategory"],
            focus_tables=["Production.Product", "Production.ProductCategory"])
        self.assertIn("Production.ProductSubcategory", result.intermediate_tables)
        self.assertEqual(result.trace["completed_paths"][0]["hops"], 2)
        self.assertIn("Production.ProductSubcategory", result.tables)

    def test_02_composite_fk_keeps_both_columns(self):
        composite = self.store.composite_relations()
        self.assertEqual(len(composite), 1)
        relation = composite[0]
        self.assertEqual(relation.source_columns, ["SpecialOfferID", "ProductID"])
        self.assertEqual(relation.target_columns, ["SpecialOfferID", "ProductID"])
        validation = self.store.validate()
        self.assertEqual(validation["fk_constraint_groups"], 90)
        self.assertEqual(validation["fk_column_pairs"], 91)
        self.assertTrue(validation["groups_preserved_as_units"])
        risks = detect_semantic_risks(
            ["Sales.SalesOrderDetail", "Sales.SpecialOfferProduct"],
            column_hints=["ProductID"])
        self.assertIn("composite_fk_incomplete", {risk["kind"] for risk in risks})

    def test_03_order_header_detail_join_emits_duplicate_aggregation_risk(self):
        risks = detect_semantic_risks(
            ["Sales.SalesOrderHeader", "Sales.SalesOrderDetail"],
            column_hints=["SubTotal", "LineTotal"])
        risk = next(item for item in risks if item["kind"] == "duplicate_aggregation")
        self.assertIn("23.6", risk["description"])
        self.assertIn("聚合", risk["required_mitigation"])

    def test_04_cost_price_history_join_emits_interval_risk(self):
        tables = ["Production.ProductCostHistory", "Production.ProductListPriceHistory"]
        risks = detect_semantic_risks(tables)
        risk = next(item for item in risks if item["kind"] == "historical_interval_missing")
        self.assertFalse(risk["interval_hint_present"])
        self.assertIn("649", risk["description"])
        with_interval = detect_semantic_risks(tables, column_hints=["StartDate", "EndDate"])
        self.assertTrue(next(item for item in with_interval
                             if item["kind"] == "historical_interval_missing")["interval_hint_present"])

    def test_05_below_reorder_point_emits_product_level_grain_constraint(self):
        risks = detect_semantic_risks(
            ["Production.ProductInventory", "Production.Product"],
            column_hints=["Quantity", "ReorderPoint"])
        risk = next(item for item in risks if item["kind"] == "mixed_grain")
        self.assertIn("GROUP BY ProductID", risk["required_mitigation"])

    def test_06_multiple_role_paths_return_ambiguity(self):
        validation = self.store.validate()
        self.assertTrue(validation["multi_role_table_pairs"])
        result = self.finder.find(
            ["Sales.CurrencyRate", "Sales.Currency", "Sales.SalesOrderHeader"],
            seed_tables=["Sales.CurrencyRate", "Sales.SalesOrderHeader"],
            required_targets=["Sales.Currency"])
        self.assertTrue(result.ambiguity)
        self.assertTrue(
            any("candidate_relation_ids" in item for item in result.ambiguity)
            or any(item.get("kind") == "near_tie" for item in result.ambiguity))

    def test_07_missing_seed_can_expand_and_unreachable_target_falls_back(self):
        expanded = self.expander.expand(
            ["Production.Product"], [], target_tables=["Production.ProductCategory"],
            focus_tables=["Production.Product"])
        self.assertIn("Production.ProductSubcategory", expanded.tables)
        narrow = SubgraphExpander(self.store, max_hops=1)
        fallback = narrow.expand(
            ["Production.Product"], [], target_tables=["Production.ProductCategory"],
            focus_tables=["Production.Product"], allow_extension=False)
        self.assertTrue(fallback.fallback_triggered)
        self.assertIn("Production.ProductCategory", fallback.unreachable_targets)

    def test_08_feature_switch_on_off_and_original_databases_untouched(self):
        result = _run_tool("统计各商品类别的明细净销售额", "adventureworks", enabled=False)
        self.assertNotIn("[企业知识增强 Schema 上下文", result["evidence"])
        enhanced = _run_tool("统计各商品类别的明细净销售额", "adventureworks", enabled=True)
        self.assertIn("[企业知识增强 Schema 上下文", enhanced["evidence"])
        self.assertIn("[系统业务口径]", enhanced["evidence"])
        self.assertIn("Sales.SalesOrderHeader", enhanced["evidence"])  # dialect hint preserved
        sales = _run_tool("在职人数", "enterprise_sales", enabled=True)
        self.assertNotIn("[企业知识增强 Schema 上下文", sales["evidence"])

    def test_09_existing_contract_and_dev_split_stay_intact(self):
        from app import cli
        # Phase 4B 新增两个离线命令（evaluate-join-paths / phase4b-smoke），
        # 原有 10 个必须一个不少。
        self.assertGreaterEqual(len(cli.COMMANDS), 10)
        for name in ("serve", "check", "ask", "mcp", "evaluate", "evaluate-rag",
                     "generate-data", "build-index", "build-knowledge",
                     "evaluate-graphrag"):
            self.assertIn(name, cli.COMMANDS)
        self.assertIn("evaluate-join-paths", cli.COMMANDS)
        self.assertIn("phase4b-smoke", cli.COMMANDS)
        payload = evaluate_graphrag._build_payload(ROOT, argparse.Namespace(
            cases="evals/adventureworks-graphrag/dev.json", configs="A,B,C,D",
            vector_mode="none", local_model="", fusion="weighted",
            allow_test_split=False, validate_only=True, quiet=True,
            output="workspace/evals/graphrag-abcd.json"))
        self.assertEqual(payload["case_count"], 12)
        self.assertEqual(payload["split"], "dev")
        # The sealed split must be refused by default.
        with self.assertRaises(ValueError):
            evaluate_graphrag._build_payload(ROOT, argparse.Namespace(
                cases="evals/adventureworks-graphrag/test.json", configs="A",
                vector_mode="none", local_model="", fusion="weighted",
                allow_test_split=False, validate_only=True, quiet=True,
                output="workspace/evals/x.json"))

    def test_10_reproducible_real_comparison_result_exists(self):
        path = ROOT / "workspace" / "evals" / "graphrag-abcd.json"
        if not path.is_file():
            self.skipTest("run `evaluate-graphrag` first")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["split"], "dev")
        self.assertEqual(payload["case_count"], 12)
        self.assertEqual(payload["configs"], ["A", "B", "C", "D"])
        self.assertEqual(len(payload["runs"]), 48)
        self.assertTrue(all(key in payload["summary"] for key in "ABCD"))
        self.assertIn("graph_validation", payload)
        self.assertIn("vector_status", payload)
        self.assertFalse(payload["isolation"]["test_split_used"])
        # Deterministic replay of one case must be byte-identical in metrics.
        context = evaluate_graphrag.EvalContext(ROOT, vector_mode="none", local_model="")
        case = json.loads((ROOT / "evals/adventureworks-graphrag/dev.json").read_text(
            encoding="utf-8"))["cases"][0]
        first = evaluate_graphrag.run_case(context, case, "D", fusion="weighted")
        second = evaluate_graphrag.run_case(context, case, "D", fusion="weighted")
        for key in ("table_recall", "column_recall", "join_path_correct_rate",
                    "candidate_table_count", "context_tokens"):
            self.assertEqual(first[key], second[key])


def _run_tool(question: str, database_id: str, *, enabled: bool) -> dict:
    sql_skill = MagicMock()
    sql_skill.run.return_value.model_dump.return_value = {
        "status": "success", "sql": "SELECT 1", "columns": [], "rows": [],
        "truncated": False, "elapsed_seconds": 0.01,
    }
    registry = ToolRegistry(sql_skill, {
        "adventureworks": ROOT / "data/adventureworks/adventureworks.sqlite",
        "enterprise_sales": ROOT / "data/enterprise_sales/enterprise_sales.sqlite",
    })
    state = {
        "request": {"database_id": database_id, "question": question},
        "question": question, "database_id": database_id,
        "schema_context_database_id": database_id,
        "turn_id": "turn-1", "trusted_evidence": "[系统业务口径] 示例口径",
        "user_hint": "", "sql_result": {}, "sql_result_turn_id": "",
    }
    env = {"ENABLE_KNOWLEDGE_ENHANCEMENT": "1" if enabled else ""}
    with patch.dict(os.environ, env, clear=False):
        registry._run_text_to_sql({"question": question, "intent": "query"}, state, None)
    return {"evidence": sql_skill.run.call_args[0][0]["evidence"]}


class EnhancementRuntimeTest(unittest.TestCase):
    def test_disabled_by_default_and_never_reads_keys(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_enabled())
            result = build_enhancement("销售额", "adventureworks")
            self.assertFalse(result.enabled)
            self.assertEqual(result.evidence_text, "")

    def test_observability_and_sse_compatible_events(self):
        events: list[dict] = []
        with patch.dict(os.environ, {"ENABLE_KNOWLEDGE_ENHANCEMENT": "1"}):
            result = build_enhancement(
                "统计各商品类别的明细净销售额", "adventureworks",
                on_stage=lambda event: events.append(event))
        self.assertTrue(result.enabled)
        self.assertIsNone(result.error)
        stages = [event["stage"] for event in events]
        self.assertIn("knowledge_retrieval", stages)
        self.assertEqual(events[0]["status"], "started")
        self.assertEqual(events[-1]["status"], "completed")
        observability = result.observability
        for key in ("original_seeds", "graph_added_nodes", "necessary_intermediate_tables",
                    "candidate_join_paths", "excluded_paths", "composite_fk_handling",
                    "grain_risks", "final_schema_context", "context_tokens",
                    "total_elapsed_seconds"):
            self.assertIn(key, observability)
        self.assertIn("Production.ProductSubcategory",
                      observability["necessary_intermediate_tables"])
        self.assertTrue(observability["composite_fk_handling"]["all_paths_complete"])

    def test_failures_degrade_to_empty_hint_never_raise(self):
        with patch.dict(os.environ, {"ENABLE_KNOWLEDGE_ENHANCEMENT": "1"}):
            result = build_enhancement("销售额", "adventureworks",
                                       project_root=ROOT / "nonexistent-root")
        self.assertTrue(result.enabled)
        self.assertEqual(result.evidence_text, "")
        self.assertIsNotNone(result.error)


if __name__ == "__main__":
    unittest.main()
