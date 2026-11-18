"""Phase 2 hybrid retrieval + SchemaContext unit tests (offline, deterministic)."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from app.knowledge.graph_store import GraphStore
from app.knowledge.metric_registry import MetricRegistry
from app.knowledge.models import DatabaseSchema
from app.knowledge.retrieval.context_builder import SchemaContext, build_context, estimate_tokens
from app.knowledge.retrieval.hybrid import (
    HybridIndex, RetrievalFilters, build_documents, keyword_score, split_identifier,
)
from app.knowledge.retrieval.join_path import JoinPathFinder
from app.knowledge.retrieval.schema_retriever import SchemaRetriever
from app.knowledge.retrieval.subgraph import SubgraphExpander
from app.knowledge.schema_dictionary import load_dictionary
from app.knowledge.schema_extractor import PROJECT_ROOT

ROOT = PROJECT_ROOT
ARTIFACTS = ROOT / "workspace" / "knowledge" / "adventureworks"


class RetrievalFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = [ARTIFACTS / "schema_adventureworks.json", ARTIFACTS / "snapshot.json",
                    ARTIFACTS / "relations.json",
                    ROOT / "app/enterprise/adventureworks_metrics.json",
                    ROOT / "app/enterprise/adventureworks_dictionary.json"]
        if not all(path.is_file() for path in required):
            raise unittest.SkipTest("run build-knowledge + dictionary builders first")
        cls.schema = DatabaseSchema.model_validate(
            json.loads((ARTIFACTS / "schema_adventureworks.json").read_text(encoding="utf-8")))
        cls.registry = MetricRegistry.from_file(
            ROOT / "app/enterprise/adventureworks_metrics.json")
        cls.dictionary = load_dictionary(
            ROOT / "app/enterprise/adventureworks_dictionary.json")
        cls.store = GraphStore.from_artifacts(ARTIFACTS)
        cls.documents = build_documents(cls.registry, {"adventureworks": cls.schema},
                                        cls.dictionary)
        cls.index = HybridIndex(cls.documents)
        cls.retriever = SchemaRetriever(cls.documents, cls.index, cls.registry,
                                        {"adventureworks": cls.schema})


class DocumentAndKeywordTest(RetrievalFixture):
    def test_three_level_index_covers_real_knowledge(self):
        from collections import Counter
        counts = Counter(doc.knowledge_type for doc in self.documents)
        self.assertEqual(counts["metric"], 38)
        self.assertEqual(counts["table"], 71)
        self.assertEqual(counts["column"], 486)
        self.assertEqual(len(self.documents), 595)

    def test_camelcase_and_dotted_prefix_are_split(self):
        fragments = split_identifier("Sales.SalesOrderHeader.LineTotal")
        for expected in ("sales", "salesorderheader", "linetotal", "order", "header"):
            self.assertIn(expected, fragments)

    def test_exact_column_name_outweighs_description_overlap(self):
        exact = next(doc for doc in self.documents
                     if doc.doc_id == "column:adventureworks.Sales.SalesOrderDetail.LineTotal")
        other = next(doc for doc in self.documents
                     if doc.doc_id == "column:adventureworks.Sales.SalesOrderHeader.SubTotal")
        self.assertGreater(keyword_score("LineTotal 明细销售额", exact)[0],
                           keyword_score("LineTotal 明细销售额", other)[0])

    def test_chinese_business_alias_recalls_category_table(self):
        filters = RetrievalFilters(database_id="adventureworks",
                                   knowledge_types=["table"])
        hits = self.index.keyword("统计各商品类别的明细净销售额", filters)
        self.assertIn("table:adventureworks.Production.ProductCategory",
                      [hit.doc_id for hit in hits[:12]])

    def test_database_and_domain_and_version_filters_isolate_context(self):
        foreign = RetrievalFilters(database_id="enterprise_sales")
        self.assertEqual(self.index.keyword("销售额", foreign), [])
        domain = RetrievalFilters(database_id="adventureworks", domains=["purchasing"],
                                  knowledge_types=["table"])
        hits = self.index.keyword("采购订单", domain)
        self.assertTrue(hits)
        self.assertTrue(all(self.index.by_id[hit.doc_id].domain == "purchasing" for hit in hits))
        version = RetrievalFilters(database_id="adventureworks", version="999")
        self.assertEqual(self.index.keyword("销售额", version), [])

    def test_no_dense_backend_reports_keyword_fallback(self):
        result = self.index.search("销售额", RetrievalFilters(database_id="adventureworks"),
                                   mode="hybrid")
        self.assertEqual(result.mode_used, "keyword")
        self.assertEqual(result.fallback_reason, "no_dense_backend")
        self.assertIsNone(result.backend_label)

    def test_rrf_fusion_is_available_but_not_silently_substituted(self):
        filters = RetrievalFilters(database_id="adventureworks", knowledge_types=["metric"])
        weighted = self.index.search("销售额", filters, mode="keyword", fusion="weighted")
        rrf = self.index.search("销售额", filters, mode="keyword", fusion="rrf")
        self.assertEqual(weighted.trace["fusion_used"], "weighted")
        self.assertEqual(rrf.trace["fusion_used"], "weighted")  # keyword-only: no dense path
        self.assertTrue(rrf.hits)


class SchemaRetrieverTest(RetrievalFixture):
    def test_bidirectional_paths_both_contribute(self):
        result = self.retriever.retrieve(
            "统计各商品类别的明细净销售额", database_id="adventureworks",
            mode="keyword", metric_expansion=True)
        self.assertTrue(result.table_first_tables)
        self.assertTrue(result.column_first_tables)
        self.assertTrue(set(result.candidate_tables) & set(result.table_first_tables))
        self.assertTrue(set(result.candidate_tables) & set(result.column_first_tables))
        self.assertIn("table_first", result.retrieval_trace)
        self.assertIn("column_first", result.retrieval_trace)

    def test_unreviewed_metrics_are_recalled_but_never_forced(self):
        result = self.retriever.retrieve(
            "订单销售额", database_id="adventureworks", metric_expansion=True)
        self.assertTrue(result.selected_metrics)
        self.assertTrue(all(metric["trusted"] is False for metric in result.selected_metrics))
        self.assertEqual(result.required_columns, [])
        self.assertTrue(result.metric_expansion["expanded_columns"])
        self.assertEqual(result.metric_expansion["trusted_metric_count"], 0)


class SchemaContextTest(RetrievalFixture):
    def _context(self, question: str, max_tokens: int = 900) -> SchemaContext:
        retrieval = self.retriever.retrieve(
            question, database_id="adventureworks", metric_expansion=True)
        focus = list(dict.fromkeys([
            *retrieval.table_first_tables[:2], *retrieval.column_first_tables[:2],
            *retrieval.candidate_tables[:3]]))[:5]
        targets = list(dict.fromkeys([*focus, *retrieval.candidate_tables[:6]]))
        subgraph = SubgraphExpander(self.store).expand(
            retrieval.candidate_tables, retrieval.candidate_columns,
            target_tables=targets, focus_tables=focus)
        paths = JoinPathFinder(self.store).find(
            subgraph.tables, seed_tables=focus[:3], column_hints=subgraph.columns[:30],
            required_targets=[t for t in targets if t in subgraph.tables])
        return build_context(retrieval, subgraph, paths, max_tokens=max_tokens)

    def test_all_required_fields_are_present_and_serializable(self):
        context = self._context("统计各商品类别的明细净销售额")
        for field in ("database_id", "schema_version", "selected_metrics", "candidate_tables",
                      "candidate_columns", "required_columns", "join_paths",
                      "aggregation_risks", "ambiguity", "evidence_refs",
                      "retrieval_trace", "context_budget"):
            self.assertIn(field, context.model_dump())
        restored = SchemaContext.model_validate(json.loads(context.to_json()))
        self.assertEqual(restored.database_id, "adventureworks")
        self.assertEqual(restored.mode, "hint")

    def test_token_budget_is_enforced_and_traced(self):
        context = self._context("统计各商品类别的明细净销售额", max_tokens=200)
        budget = context.context_budget
        self.assertLessEqual(budget.estimated_tokens, 200)
        self.assertTrue(budget.within_budget)
        self.assertTrue(budget.truncated_fields)
        self.assertEqual(budget.estimated_tokens,
                         estimate_tokens(context.to_evidence_text()))

    def test_evidence_text_keeps_dialect_and_trust_labels(self):
        text = self._context("订单销售额").to_evidence_text()
        self.assertIn("hint", text)
        self.assertIn("不裁剪原始 DataItem.database_schema", text)
        self.assertIn("双引号", text)
        self.assertIn("unreviewed-candidate", text)
        self.assertIn("schema_version", self._context("订单销售额").to_json())
        self.assertNotIn("硬裁剪", text)


if __name__ == "__main__":
    unittest.main()
