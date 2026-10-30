import json
import unittest

from app.enterprise import EnterpriseKnowledgeBase


class SemanticEmbedding:
    """Deterministic five-topic embedding used without network access."""

    topics = [
        ("净销售额", [0, 1, 0, 0, 0]),
        ("平均客单价", [0, 0, 1, 0, 0]),
        ("退款率", [0, 0, 0, 1, 0]),
        ("新增客户", [0, 0, 0, 0, 1]),
        ("销售额", [1, 0, 0, 0, 0]),
    ]

    def __init__(self):
        self.calls = 0

    def __call__(self, texts):
        self.calls += 1
        vectors = []
        for text in texts:
            if "指标名称：" in text:
                vectors.append(next((vector for name, vector in self.topics
                                     if f"指标名称：{name}" in text), [0, 0, 0, 0, 0]))
            elif "第一次完成购买" in text:
                vectors.append([0, 0, 0, 0, 1])
            elif "扣掉退款" in text:
                vectors.append([0, 1, 0, 0, 0])
            else:
                vectors.append([1, 0, 0, 0, 0])
        return vectors


class FailedEmbedding:
    def __call__(self, _texts):
        raise TimeoutError("provider unavailable")


class MetricRAGTest(unittest.TestCase):
    def test_keyword_mode_is_a_reproducible_baseline(self):
        knowledge = EnterpriseKnowledgeBase(mode="keyword")
        self.assertEqual(knowledge.retrieve("查询销售额", limit=1)[0]["id"], "revenue")
        self.assertEqual(knowledge.retrieve("第一次完成购买的客户", limit=3), [])

    def test_hybrid_mode_recalls_semantic_paraphrase_and_explains_score(self):
        embedding = SemanticEmbedding()
        knowledge = EnterpriseKnowledgeBase(mode="hybrid", embedding_function=embedding)
        result = knowledge.search("第一次完成购买的客户", limit=2)
        self.assertEqual(result["metrics"][0]["id"], "new_customer")
        self.assertEqual(result["retrieval"]["mode_used"], "hybrid")
        self.assertGreater(result["metrics"][0]["retrieval"]["semantic_score"], 0.9)
        self.assertEqual(result["metrics"][0]["retrieval"]["matched_keywords"], [])

        # Corpus vectors and identical query vectors are both cached.
        knowledge.search("第一次完成购买的客户", limit=2)
        self.assertEqual(embedding.calls, 2)

    def test_embedding_failure_falls_back_without_breaking_metric_tool(self):
        knowledge = EnterpriseKnowledgeBase(mode="hybrid", embedding_function=FailedEmbedding())
        result = knowledge.search("查询退款率", limit=2)
        self.assertEqual(result["metrics"][0]["id"], "refund_rate")
        self.assertEqual(result["retrieval"]["mode_used"], "keyword")
        self.assertEqual(result["retrieval"]["fallback_reason"], "TimeoutError")

    def test_tool_payload_contains_retrieval_metadata(self):
        from app.agent.tools import ToolRegistry

        registry = ToolRegistry(object(), {"enterprise_sales": "demo.sqlite"},
                                EnterpriseKnowledgeBase(mode="keyword"))
        outcome = registry.execute(
            "search_business_metrics", {"query": "销售额"},
            {"question": "销售额", "turn_id": "turn-1", "trusted_evidence": "",
             "database_id": "enterprise_sales"},
        )
        payload = json.loads(outcome.content)
        self.assertEqual(payload["retrieval"]["mode_used"], "keyword")
        self.assertIn("score", payload["metrics"][0]["retrieval"])

    def test_database_filter_prevents_metric_cross_domain_leakage(self):
        knowledge = EnterpriseKnowledgeBase(mode="keyword")
        hr = knowledge.retrieve("员工人数和销售额", limit=8, database_id="enterprise_hr")
        sales = knowledge.retrieve("员工人数和销售额", limit=8, database_id="enterprise_sales")
        self.assertTrue(hr)
        self.assertTrue(sales)
        self.assertTrue(all(item["database_id"] == "enterprise_hr" for item in hr))
        self.assertTrue(all(item["database_id"] == "enterprise_sales" for item in sales))


if __name__ == "__main__":
    unittest.main()
