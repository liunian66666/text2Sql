"""运行时索引签名校验（Phase 4B §2.2）。

测的是**真的会拦住错误向量检索那道门**，不是 preflight 的展示字段：

* 缺 manifest  → ``missing``，SQLSkill 会 fail-fast；
* 换模型/换配置 → ``incompatible``，必须拒绝，否则拿 A 模型的向量去打分 B 模型的库；
* 数据库被改   → ``stale``，必须拒绝；
* 只建了部分列 → ``partial``，允许通过，未覆盖列降级为空结果；
* 只有版本 1 签名的老索引 → 仍然兼容（不能因为加了 v2 就让 Sales/HR 索引失效）。
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from app.vector_db.local_index import (
    INDEX_INCOMPATIBLE,
    INDEX_MISSING,
    INDEX_PARTIAL,
    INDEX_STALE,
    canonical_embedding_signature,
    classify_index_status,
    embedding_signature,
    verify_runtime_index,
    write_local_index_manifest,
)


def _config(**overrides):
    base = {
        "api_type": "local",
        "embedding_model_name_or_path": "data/models/Qwen/Qwen3-Embedding-0___6B",
        "use_qwen3_embedding": True,
        "normalize_embeddings": False,
        "max_value_length": 100,
        "lower_meta_data": True,
    }
    base.update(overrides)
    return base


def _column(table: str, column: str, dim: int = 8) -> dict:
    return {
        "column_key": f"{table}\t{column}",
        "table_name": table,
        "column_name": column,
        "value_count": 3,
        "embedding_dim": dim,
        "embeddings_file": f"columns/{table}.{column}.npy",
        "documents_file": f"columns/{table}.{column}.json",
    }


class RuntimeSignatureTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.index = Path(self._tmp.name) / "local_index"

    def _build(self, *, columns=None, signature="v1", **manifest_extra) -> None:
        columns = columns if columns is not None else [_column("Sales.SalesTerritory", "Name")]
        entries = []
        for entry in columns:
            entries.append(entry)
        write_local_index_manifest(self.index, entries)
        manifest = json.loads((self.index / "manifest.json").read_text(encoding="utf-8"))
        manifest.update({
            "index_kind": manifest_extra.pop("index_kind", "partial"),
            "embedding_model": "data/models/Qwen/Qwen3-Embedding-0___6B",
            "embedding_dim": 1024,
            "policy_version": "1.0",
            "covered_tables": sorted({entry["table_name"] for entry in columns}),
            "covered_column_count": len(columns),
        })
        if signature == "v1":
            manifest["embedding_signature"] = embedding_signature(_config())
        elif signature == "v2":
            manifest["embedding_signature_v2"] = canonical_embedding_signature(_config())
        elif signature == "bogus":
            manifest["embedding_signature"] = "0" * 64
        manifest.update(manifest_extra)
        (self.index / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    def test_missing_manifest(self) -> None:
        status = verify_runtime_index(self.index, embedding_config=_config())
        self.assertEqual(status["status"], INDEX_MISSING)
        self.assertIn("manifest_not_found", status["reasons"])

    def test_partial_index_is_allowed(self) -> None:
        self._build()
        status = verify_runtime_index(self.index, embedding_config=_config())
        self.assertEqual(status["status"], INDEX_PARTIAL)
        self.assertEqual(status["reasons"], [])
        self.assertEqual(status["covered_columns"], 1)

    def test_complete_index_is_allowed(self) -> None:
        self._build(columns=[_column("Sales.SalesTerritory", "Name"),
                             _column("Production.Product", "Name")], index_kind="complete")
        self.assertEqual(verify_runtime_index(self.index, embedding_config=_config())["status"],
                         "complete")

    def test_other_embedding_model_is_incompatible(self) -> None:
        self._build()
        other = _config(embedding_model_name_or_path="text-embedding-v4", use_qwen3_embedding=False)
        status = verify_runtime_index(self.index, embedding_config=other)
        self.assertEqual(status["status"], INDEX_INCOMPATIBLE)
        self.assertTrue(status["reasons"])

    def test_changed_value_length_is_incompatible(self) -> None:
        self._build()
        status = verify_runtime_index(self.index, embedding_config=_config(max_value_length=50))
        self.assertEqual(status["status"], INDEX_INCOMPATIBLE)

    def test_database_fingerprint_change_is_stale(self) -> None:
        self._build(database_fingerprint="a" * 64)
        status = verify_runtime_index(self.index, embedding_config=_config(),
                                      database_fingerprint="b" * 64)
        self.assertEqual(status["status"], INDEX_STALE)
        self.assertIn("database_fingerprint_mismatch", status["reasons"])

    def test_matching_fingerprint_is_not_stale(self) -> None:
        self._build(database_fingerprint="a" * 64)
        status = verify_runtime_index(self.index, embedding_config=_config(),
                                      database_fingerprint="a" * 64)
        self.assertEqual(status["status"], INDEX_PARTIAL)

    def test_v2_signature_matches_when_relative_and_absolute_paths_differ(self) -> None:
        # 构建时用相对路径，运行时被展开成绝对路径；v2 把路径归一成目录名。
        self._build(signature="v2")
        runtime = _config(
            embedding_model_name_or_path=str(
                Path("E:/x/data/models/Qwen/Qwen3-Embedding-0___6B")))
        status = verify_runtime_index(self.index, embedding_config=runtime)
        self.assertEqual(status["status"], INDEX_PARTIAL)
        self.assertEqual(status["signature_source"], "v2")

    def test_legacy_manifest_without_phase4a_fields_still_works(self) -> None:
        # Sales/HR 的历史索引没有 index_kind / embedding_dim：必须继续可用。
        write_local_index_manifest(self.index, [_column("enterprise_sales", "orders")])
        status = classify_index_status(self.index)
        self.assertIn(status["status"], {"complete", "partial"})
        self.assertEqual(status["embedding_dim"], None)


if __name__ == "__main__":
    unittest.main()
