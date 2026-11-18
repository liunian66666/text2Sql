"""Phase 1: structured metric registry, trust boundary and retrieval compatibility."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from app.enterprise.knowledge import EnterpriseKnowledgeBase
from app.knowledge.dependencies import KnowledgeIndex, build_edges
from app.knowledge.metric_registry import (
    SALES_SUPPLEMENTS,
    SOURCE_PATH,
    MetricRegistry,
)
from app.knowledge.schema_extractor import extract_all


class LoadAndShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = MetricRegistry.from_file()
        cls.records = json.loads(Path(SOURCE_PATH).read_text(encoding="utf-8"))["metrics"]
        cls.schemas = extract_all()

    def test_loads_exactly_13_metrics(self):
        self.assertEqual(len(self.registry.metrics), 13)
        self.assertEqual(len(self.registry.by_database("enterprise_sales")), 5)
        self.assertEqual(len(self.registry.by_database("enterprise_hr")), 8)
        self.assertEqual(len(self.registry.by_domain("sales")), 5)

    def test_original_names_keywords_definitions_and_sql_rules_are_preserved(self):
        by_id = {record["id"]: record for record in self.records}
        for metric in self.registry.metrics:
            source = by_id[metric.metric_id]
            self.assertEqual(metric.name, source["name"])
            self.assertEqual(metric.keywords, source["keywords"])
            self.assertEqual(metric.aliases, source["keywords"])
            self.assertEqual(metric.raw_definition, source["definition"])
            self.assertEqual(metric.raw_sql_rule, source.get("sql_rule", ""))
            self.assertEqual(metric.raw_formula, source.get("formula"))
            self.assertEqual(metric.raw_tables, list(source.get("tables") or []))
            self.assertEqual(metric.raw_fields, list(source.get("fields") or []))
            self.assertEqual(metric.raw_time_grain, source.get("time_grain"))

    def test_sales_five_now_have_structured_tables_and_columns(self):
        for metric_id in SALES_SUPPLEMENTS:
            metric = self.registry.get(metric_id)
            self.assertIsNotNone(metric)
            self.assertTrue(metric.source_tables, metric_id)
            self.assertTrue(metric.source_columns, metric_id)
            self.assertEqual(metric.evidence_status.value, "partial")
            self.assertEqual(metric.expression_kind, "verbatim")

    def test_hr_eight_keep_complete_source_evidence(self):
        for metric in self.registry.by_database("enterprise_hr"):
            self.assertEqual(metric.evidence_status.value, "complete")
            self.assertEqual(metric.expression, metric.raw_formula)

    def test_every_expression_is_a_verbatim_source_quote(self):
        by_id = {record["id"]: record for record in self.records}
        for metric in self.registry.metrics:
            source = by_id[metric.metric_id]
            evidence = " ".join(str(source.get(key) or "") for key in
                                ("definition", "formula", "sql_rule"))
            self.assertIn(metric.expression, evidence,
                          f"{metric.metric_id} expression was not verbatim")

    def test_lookup_by_id_database_domain_and_alias(self):
        self.assertEqual(self.registry.get("revenue").name, "销售额")
        self.assertIsNone(self.registry.get("does_not_exist"))
        self.assertEqual([m.metric_id for m in self.registry.resolve("GMV")], ["revenue"])
        self.assertEqual([m.metric_id for m in self.registry.resolve("net_revenue")],
                         ["net_revenue"])
        self.assertEqual([m.metric_id for m in self.registry.resolve("平均绩效")],
                         ["average_performance"])


class TrustBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = MetricRegistry.from_file()
        cls.schemas = extract_all()

    def test_all_metrics_are_unreviewed_and_untrusted(self):
        self.assertTrue(all(not metric.reviewed for metric in self.registry.metrics))
        self.assertTrue(all(not metric.trusted for metric in self.registry.metrics))
        self.assertEqual(self.registry.trusted_metrics(), [])
        self.assertEqual(len(self.registry.pending_review_metrics()), 13)

    def test_unknown_business_fields_are_explicit_not_invented(self):
        for metric in self.registry.metrics:
            for field in metric.unknown_fields:
                value = getattr(metric, field)
                self.assertTrue(value == "UNKNOWN" or value == [],
                                f"{metric.metric_id}.{field} unexpectedly filled with {value!r}")

    def test_reviewed_metric_without_unknown_fields_becomes_trusted(self):
        metric = self.registry.get("revenue")
        candidate = metric.model_copy(update={"reviewed": True, "unknown_fields": []})
        self.assertTrue(candidate.trusted)
        still_unknown = metric.model_copy(update={"reviewed": True})
        self.assertFalse(still_unknown.trusted)

    def test_real_build_has_no_validation_errors(self):
        issues = self.registry.validate(self.schemas)
        self.assertEqual([issue for issue in issues if issue.severity == "error"], [])
        self.assertEqual(sum(1 for issue in issues if issue.code == "unreviewed_metric"), 13)

    def test_missing_table_and_column_references_are_detected(self):
        bad = {
            "id": "fake_metric", "domain": "sales", "database_id": "enterprise_sales",
            "name": "测试指标", "keywords": ["测试"], "definition": "测试",
            "sql_rule": "", "formula": "SUM(x)", "time_grain": "",
            "tables": ["not_a_table"],
            "fields": ["not_a_table.nope", "orders.missing_column"],
        }
        issues = MetricRegistry.from_records([bad]).validate(self.schemas)
        codes = {issue.code for issue in issues if issue.severity == "error"}
        self.assertIn("missing_table", codes)
        self.assertIn("missing_column", codes)

    def test_missing_database_reference_is_detected(self):
        bad = {
            "id": "fake_db_metric", "domain": "sales", "database_id": "unknown_db",
            "name": "测试", "keywords": [], "definition": "测试",
            "sql_rule": "", "formula": "SUM(x)", "tables": [], "fields": [],
        }
        issues = MetricRegistry.from_records([bad]).validate(self.schemas)
        self.assertIn("missing_database",
                      {issue.code for issue in issues if issue.severity == "error"})


class NetRevenueRiskTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = MetricRegistry.from_file()
        cls.schemas = extract_all()

    def test_refund_aggregation_constraint_is_recorded_verbatim(self):
        metric = self.registry.get("net_revenue")
        self.assertEqual(
            metric.join_constraints,
            ["退款按 order_id 聚合后再与订单连接，避免订单明细导致退款金额重复。"],
        )
        self.assertEqual(metric.raw_sql_rule, metric.join_constraints[0])
        self.assertIn("revenue", metric.depends_on_metrics)

    def test_duplicate_aggregation_and_mixed_grain_risks_are_structured(self):
        metric = self.registry.get("net_revenue")
        kinds = {risk.kind for risk in metric.aggregation_risks}
        self.assertIn("duplicate_aggregation", kinds)
        self.assertIn("mixed_grain", kinds)
        risk = next(item for item in metric.aggregation_risks
                    if item.kind == "duplicate_aggregation")
        self.assertEqual(set(risk.affected_tables), {"orders", "order_items", "refunds"})
        self.assertEqual(risk.required_mitigation, metric.raw_sql_rule)
        self.assertIn("business_metrics.json", risk.source_ref)
        self.assertFalse(risk.reviewed)

    def test_recorded_risk_matches_real_declared_fk_cardinality(self):
        sales = self.schemas["enterprise_sales"]
        risky = {
            relation.source_table.rsplit(".", 1)[-1]
            for relation in sales.relations
            if relation.target_table.endswith(".orders")
            and relation.cardinality.value == "MANY_TO_ONE"
            and relation.duplicate_aggregation_risk
        }
        self.assertIn("order_items", risky)
        self.assertIn("refunds", risky)


class RetrievalCompatibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = MetricRegistry.from_file()
        cls.records = json.loads(Path(SOURCE_PATH).read_text(encoding="utf-8"))["metrics"]

    def test_keyword_retrieval_still_finds_every_original_metric(self):
        knowledge = EnterpriseKnowledgeBase(path=Path(SOURCE_PATH))
        known = {metric.metric_id for metric in self.registry.metrics}
        for record in self.records:
            result = knowledge.search(record["name"], limit=3,
                                      database_id=record["database_id"])
            self.assertTrue(result["metrics"])
            self.assertIn(result["metrics"][0]["id"], known)
        self.assertEqual(knowledge.mode, "keyword")

    def test_hybrid_retrieval_with_stub_embedding_still_works(self):
        def stub(texts):
            return [[1.0, 0.0] for _ in texts]

        knowledge = EnterpriseKnowledgeBase(
            path=Path(SOURCE_PATH), mode="hybrid", embedding_function=stub)
        result = knowledge.search("上个月销售额", limit=3, database_id="enterprise_sales")
        self.assertEqual(result["retrieval"]["mode_used"], "hybrid")
        self.assertTrue(result["metrics"])

    def test_structured_view_does_not_alter_original_json_file(self):
        before = Path(SOURCE_PATH).read_bytes()
        MetricRegistry.from_file()
        self.assertEqual(Path(SOURCE_PATH).read_bytes(), before)


class ColumnImpactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = MetricRegistry.from_file()
        cls.schemas = extract_all()

    def test_column_impact_analysis(self):
        index = KnowledgeIndex(self.registry, self.schemas)
        self.assertEqual(
            set(index.metrics_affected_by_column("enterprise_sales.order_items.unit_price")),
            {"revenue", "net_revenue", "average_order_value", "refund_rate"},
        )
        self.assertEqual(
            set(index.metrics_affected_by_column("enterprise_sales.refunds.refund_amount")),
            {"net_revenue", "refund_rate"},
        )
        self.assertEqual(
            index.grain_for_metric("revenue"), "UNKNOWN"
        )  # no invented statistical grain
        self.assertEqual(index.dimensions_for_metric("revenue"), [])

    def test_removing_a_column_flags_every_dependent_metric(self):
        sales = self.schemas["enterprise_sales"]
        pruned = sales.model_copy(update={"tables": [
            table.model_copy(update={"columns": [
                column for column in table.columns if column.column_name != "unit_price"
            ]}) if table.table_name == "order_items" else table
            for table in sales.tables
        ]})
        issues = self.registry.validate({**self.schemas, "enterprise_sales": pruned})
        affected = {
            issue.metric_id for issue in issues
            if issue.code == "missing_column" and "unit_price" in (issue.reference or "")
        }
        self.assertEqual(affected,
                         {"revenue", "net_revenue", "average_order_value", "refund_rate"})
        edges = build_edges(self.registry, {**self.schemas, "enterprise_sales": pruned})
        self.assertFalse(any(edge.target_id.endswith("order_items.unit_price") for edge in edges))

    def test_verified_relations_for_net_revenue(self):
        index = KnowledgeIndex(self.registry, self.schemas)
        relations = index.verified_relations_for_metric("net_revenue")
        self.assertTrue(relations)
        self.assertTrue(all(relation.verified for relation in relations))
        self.assertIn("enterprise_sales.order_items",
                      {relation.source_table for relation in relations})
        self.assertIn("enterprise_sales.refunds",
                      {relation.source_table for relation in relations})


if __name__ == "__main__":
    unittest.main()
