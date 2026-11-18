"""知识增强的检索通道开关（Phase 4B §2.3）。

要求是「核实 GraphRAG 运行时到底走关键词还是真向量」。默认必须是关键词：
本地 Qwen3 首次加载约 50 秒，不能让 Web 首请求隐式承担。打开后必须真的换成
本地模型，并且这个事实要写进 observability，否则「GraphRAG 生效」无从审计。
"""
from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest import mock

from app.knowledge import enhancement


class DenseSwitchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get(enhancement.DENSE_ENV)
        os.environ.pop(enhancement.DENSE_ENV, None)
        enhancement._dense_backend_cache.clear()

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop(enhancement.DENSE_ENV, None)
        else:
            os.environ[enhancement.DENSE_ENV] = self._saved
        enhancement._dense_backend_cache.clear()

    def test_default_is_keyword_only(self) -> None:
        self.assertIsNone(enhancement.dense_model_path_from_env())
        self.assertIsNone(enhancement.dense_backend_from_env())

    def test_local_alias_resolves_to_repo_model(self) -> None:
        os.environ[enhancement.DENSE_ENV] = "local"
        path = enhancement.dense_model_path_from_env()
        self.assertTrue(path.endswith(str(Path("data/models/Qwen/Qwen3-Embedding-0___6B"))))

    def test_relative_path_is_project_relative(self) -> None:
        os.environ[enhancement.DENSE_ENV] = "data/models/custom"
        expected = str(enhancement.PROJECT_ROOT / "data/models/custom")
        self.assertEqual(enhancement.dense_model_path_from_env(), expected)

    def test_absolute_path_is_kept(self) -> None:
        os.environ[enhancement.DENSE_ENV] = "E:/models/other"
        # Windows 上 str(Path(...)) 会把分隔符归一成反斜杠，比较用 Path 而不是字符串。
        self.assertEqual(Path(enhancement.dense_model_path_from_env()),
                         Path("E:/models/other"))

    def test_backend_is_cached_per_model(self) -> None:
        os.environ[enhancement.DENSE_ENV] = "local"
        fake = mock.Mock(name="backend")
        with mock.patch("app.knowledge.retrieval.hybrid.LocalSentenceTransformerBackend",
                        return_value=fake) as factory:
            first = enhancement.dense_backend_from_env()
            second = enhancement.dense_backend_from_env()
        self.assertIs(first, fake)
        self.assertIs(second, fake)
        self.assertEqual(factory.call_count, 1)


class ObservabilityTest(unittest.TestCase):
    def test_result_records_retrieval_channel(self) -> None:
        """增强结果必须自报用的是哪条检索通道，不能只报 hint 模式。"""
        with mock.patch.dict(os.environ, {enhancement.ENABLE_ENV: "0"}, clear=False):
            result = enhancement.build_enhancement("统计销售额", "adventureworks")
        self.assertFalse(result.enabled)
        self.assertEqual(result.reason, f"{enhancement.ENABLE_ENV} not set")

    def test_non_adventureworks_is_disabled(self) -> None:
        with mock.patch.dict(os.environ, {enhancement.ENABLE_ENV: "1"}, clear=False):
            result = enhancement.build_enhancement("在职人数", "enterprise_sales")
        self.assertFalse(result.enabled)
        self.assertIn("adventureworks", result.reason)

    def test_unsupported_mode_is_disabled(self) -> None:
        with mock.patch.dict(os.environ, {enhancement.ENABLE_ENV: "1",
                                          enhancement.MODE_ENV: "hard_filter"}, clear=False):
            result = enhancement.build_enhancement("统计销售额", "adventureworks")
        self.assertFalse(result.enabled)
        self.assertIn("hard_filter", result.reason)


class DenseIndexCacheTest(unittest.TestCase):
    """稠密索引必须缓存，否则每次请求都重编码全部知识文档（实测 348 秒）。"""

    def setUp(self) -> None:
        enhancement._dense_index_cache.clear()
        self.addCleanup(enhancement._dense_index_cache.clear)

    class _Backend:
        label = "fake:test"

        def __init__(self) -> None:
            self.calls = 0

        def embed_documents(self, texts):
            self.calls += 1
            return [[0.0, 1.0] for _ in texts]

        def embed_query(self, text):
            return [0.0, 1.0]

    class _Doc:
        """Duck-typed KnowledgeDocument: DenseIndex 只用 knowledge_type/name/text。"""

        knowledge_type = "metric"

        def __init__(self, doc_id: str, text: str) -> None:
            self.doc_id = doc_id
            self.text = text
            self.name = doc_id

    def test_same_corpus_is_encoded_once(self) -> None:
        backend = self._Backend()
        documents = [self._Doc("a", "销售额"), self._Doc("b", "订单数")]
        first = enhancement.build_hybrid_index(documents, backend)
        # DenseIndex 是懒编码：成本发生在首次 search，所以这里必须真的查一次。
        first.dense.search("销售额", limit=2)
        second = enhancement.build_hybrid_index([self._Doc("a", "销售额"),
                                                 self._Doc("b", "订单数")], backend)
        second.dense.search("订单数", limit=2)
        self.assertIs(first, second)
        self.assertEqual(backend.calls, 1)

    def test_changed_corpus_rebuilds(self) -> None:
        backend = self._Backend()
        enhancement.build_hybrid_index([self._Doc("a", "销售额")], backend).dense.search(
            "销售额")
        enhancement.build_hybrid_index([self._Doc("a", "销售额"), self._Doc("b", "成本")],
                                       backend).dense.search("成本")
        self.assertEqual(backend.calls, 2)

    def test_keyword_mode_never_encodes(self) -> None:
        backend = self._Backend()
        index = enhancement.build_hybrid_index([self._Doc("a", "销售额")], None)
        self.assertIsNone(index.dense)
        self.assertEqual(backend.calls, 0)

    def test_fingerprint_is_content_sensitive(self) -> None:
        left = enhancement.documents_fingerprint([self._Doc("a", "销售额")])
        right = enhancement.documents_fingerprint([self._Doc("a", "销售额 ")])
        self.assertNotEqual(left, right)


if __name__ == "__main__":
    unittest.main()
