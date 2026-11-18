"""Phase 2 knowledge-quality tests: dictionary provenance + review ledger."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from app.knowledge.metric_registry import MetricRegistry
from app.knowledge.review import MetricReviewDecision, ReviewLedger
from app.knowledge.schema_dictionary import load_dictionary
from app.knowledge.schema_extractor import PROJECT_ROOT

ROOT = PROJECT_ROOT
DICTIONARY = ROOT / "app/enterprise/adventureworks_dictionary.json"
WORKSHEET = ROOT / "app/enterprise/adventureworks_metric_review.json"
DECISIONS = ROOT / "app/enterprise/adventureworks_review_decisions.json"
METRICS = ROOT / "app/enterprise/adventureworks_metrics.json"
PROVIDER = ROOT / "data/adventureworks/provider-schema.json"


class DictionaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DICTIONARY.is_file():
            raise unittest.SkipTest("run scripts/build_aw_dictionary.py first")
        cls.dictionary = load_dictionary(DICTIONARY)

    def test_real_coverage_is_reported_not_inflated(self):
        coverage = self.dictionary.coverage()
        self.assertEqual(coverage["tables_total"], 71)
        self.assertEqual(coverage["columns_total"], 486)
        self.assertEqual(coverage["upstream_en_table_count"], 71)
        self.assertEqual(coverage["upstream_en_column_count"], 452)
        self.assertEqual(coverage["unknown_en_column_count"], 34)
        self.assertEqual(coverage["official_reviewed_count"], 0)
        self.assertGreater(coverage["zh_candidate_table_count"], 0)
        self.assertLess(coverage["zh_candidate_table_count"], 71)

    def test_english_descriptions_are_verbatim_upstream_text(self):
        provider = json.loads(PROVIDER.read_text(encoding="utf-8"))
        upstream = {t["name"]: t.get("description", "") for t in provider["tables"]
                    if t.get("kind") == "table"}
        table = self.dictionary.table("Sales.SalesOrderHeader")
        self.assertIsNotNone(table)
        self.assertEqual(table.description_en, upstream["Sales.SalesOrderHeader"])
        self.assertEqual(table.origin["description_en"], "upstream_description")
        self.assertIn("instawdb.sql", self.dictionary.source["upstream_installer"])

    def test_chinese_and_aliases_are_model_candidates_not_official(self):
        table = self.dictionary.table("Production.ProductSubcategory")
        self.assertIsNotNone(table)
        self.assertEqual(table.origin["description_zh"], "model_candidate")
        self.assertEqual(table.origin["aliases"], "model_candidate")
        self.assertEqual(table.review_status, "unreviewed")
        column = table.column("ProductCategoryID")
        self.assertIsNotNone(column)
        self.assertEqual(column.origin["description_zh"], "model_candidate")
        self.assertEqual(self.dictionary.validate(), [])

    def test_unknown_descriptions_are_not_invented(self):
        unknown = [f"{t.table_name}.{c.column_name}"
                   for t in self.dictionary.tables for c in t.columns
                   if c.description_en == "UNKNOWN"]
        self.assertEqual(len(unknown), 34)
        for reference in unknown[:3]:
            table_name, column_name = reference.rsplit(".", 1)
            column = self.dictionary.table(table_name).column(column_name)
            self.assertEqual(column.description_en, "UNKNOWN")
            self.assertEqual(column.origin["description_en"], "unknown")

    def test_every_entry_matches_real_sqlite_metadata(self):
        from app.knowledge.schema_extractor import connect_readonly, extract_schema
        from contextlib import closing
        with closing(connect_readonly(ROOT / "data/adventureworks/adventureworks.sqlite")) as connection:
            schema = extract_schema(connection, "adventureworks")
        self.assertEqual({t.table_name for t in self.dictionary.tables},
                         {t.table_name for t in schema.tables})
        for table in self.dictionary.tables:
            real = schema.table(table.table_name)
            self.assertIsNotNone(real)
            self.assertEqual({c.column_name for c in table.columns},
                             {c.column_name for c in real.columns})


class ReviewLedgerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not METRICS.is_file():
            raise unittest.SkipTest("AdventureWorks metrics file missing")
        cls.registry = MetricRegistry.from_file(METRICS)

    def test_default_ledger_approves_nothing(self):
        ledger = ReviewLedger.from_file(DECISIONS) if DECISIONS.is_file() else ReviewLedger()
        result, audit = ledger.apply(self.registry)
        self.assertEqual(len(audit["promoted"]), len(ledger.decisions))
        self.assertEqual(audit["trusted_after"], 0)
        self.assertEqual(len(result.pending_review_metrics()), 38)

    def test_no_batch_approval_and_worksheet_stays_unreviewed(self):
        worksheet = json.loads(WORKSHEET.read_text(encoding="utf-8"))
        self.assertEqual(worksheet["selection"]["selected"], 14)
        self.assertTrue(all(c["reviewed"] is False and c["trusted"] is False
                            for c in worksheet["metric_candidates"]))
        self.assertTrue(all(c["reference_result"]["columns"] for c in worksheet["metric_candidates"]))
        self.assertEqual(worksheet["decision_ledger"],
                         "app/enterprise/adventureworks_review_decisions.json")

    def test_only_explicit_named_human_approval_promotes(self):
        ledger = ReviewLedger(decisions=[
            MetricReviewDecision(metric_id="aw_order_count", decision="approved",
                                 reviewer="张工", evidence_ref="人工审核记录 A"),
            MetricReviewDecision(metric_id="aw_sales_amount", decision="approved",
                                 reviewer="auto", evidence_ref="自动脚本"),
            MetricReviewDecision(metric_id="aw_line_net_sales", decision="deferred",
                                 reviewer="李工"),
        ])
        result, audit = ledger.apply(self.registry)
        self.assertEqual(audit["promoted"], ["aw_order_count"])
        self.assertEqual(len(audit["ignored"]), 2)
        self.assertEqual(result.get("aw_order_count").reviewed, True)
        self.assertEqual(result.get("aw_sales_amount").reviewed, False)
        # reviewed alone is not enough: unknown_fields keep trusted=False.
        self.assertGreater(len(result.get("aw_order_count").unknown_fields), 0)
        self.assertEqual(result.get("aw_order_count").trusted, False)
        self.assertEqual(audit["trusted_after"], 0)

    def test_unknown_metric_and_non_human_reviewers_are_ignored(self):
        ledger = ReviewLedger(decisions=[
            MetricReviewDecision(metric_id="does_not_exist", decision="approved",
                                 reviewer="王工"),
            MetricReviewDecision(metric_id="aw_order_count", decision="approved",
                                 reviewer="system"),
        ])
        _, audit = ledger.apply(self.registry)
        self.assertEqual(audit["promoted"], [])
        self.assertEqual(len(audit["ignored"]), 2)


if __name__ == "__main__":
    unittest.main()
