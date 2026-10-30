"""Explainable hybrid retrieval for enterprise metric definitions."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import threading
import time
from typing import Any, Literal


RetrievalMode = Literal["keyword", "hybrid"]


class EnterpriseKnowledgeBase:
    """Retrieve trusted metric rules with lexical and semantic recall.

    The embedding dependency is injected so this module stays provider-neutral
    and testable. Corpus vectors are lazy, query vectors are cached, and provider
    failures fall back to the deterministic keyword baseline.
    """

    def __init__(self, path: Path | None = None, *, mode: RetrievalMode = "keyword",
                 embedding_function: Any | None = None, lexical_weight: float = 0.6,
                 semantic_weight: float = 0.4, semantic_min_score: float = 0.25):
        path = path or Path(__file__).with_name("business_metrics.json")
        self.metrics = json.loads(path.read_text(encoding="utf-8"))["metrics"]
        if mode == "hybrid" and embedding_function is None:
            raise ValueError("hybrid metric retrieval requires an embedding function")
        if lexical_weight < 0 or semantic_weight < 0 or lexical_weight + semantic_weight <= 0:
            raise ValueError("retrieval weights must be non-negative and not both zero")
        self.mode = mode
        self.embedding_function = embedding_function
        total = lexical_weight + semantic_weight
        self.lexical_weight = lexical_weight / total
        self.semantic_weight = semantic_weight / total
        self.semantic_min_score = semantic_min_score
        self._corpus_vectors: list[list[float]] | None = None
        self._query_cache: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _document(metric: dict[str, Any]) -> str:
        aliases = "、".join(metric["keywords"])
        return (f"业务域：{metric.get('domain', 'enterprise')}。指标名称：{metric['name']}。别名：{aliases}。"
                f"业务定义：{metric['definition']} 计算公式：{metric.get('formula', '')} "
                f"时间口径：{metric.get('time_grain', '')} SQL口径：{metric['sql_rule']}")

    @staticmethod
    def _vector(value: Any) -> list[float]:
        if hasattr(value, "tolist"):
            value = value.tolist()
        return [float(item) for item in value]

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        numerator = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return max(0.0, min(1.0, numerator / (left_norm * right_norm)))

    @staticmethod
    def _lexical_score(question: str, metric: dict[str, Any]) -> tuple[float, list[str]]:
        normalized = question.casefold().replace(" ", "")
        name = metric["name"].casefold().replace(" ", "")
        matched = [keyword for keyword in metric["keywords"]
                   if keyword.casefold().replace(" ", "") in normalized]
        raw = (3.0 if name in normalized else 0.0) + 2.0 * len(matched)
        return min(raw / 3.0, 1.0), matched

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(vector) for vector in self.embedding_function(texts)]

    def _semantic_scores(self, question: str) -> tuple[list[float], bool]:
        with self._lock:
            if self._corpus_vectors is None:
                self._corpus_vectors = self._embed([self._document(metric) for metric in self.metrics])
            query_vector = self._query_cache.get(question)
            cache_hit = query_vector is not None
            if query_vector is None:
                query_vector = self._embed([question])[0]
                self._query_cache[question] = query_vector
            corpus = list(self._corpus_vectors)
        return [self._cosine(query_vector, vector) for vector in corpus], cache_hit

    def search(self, question: str, limit: int = 3, database_id: str | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        lexical = [self._lexical_score(question, metric) for metric in self.metrics]
        semantic = [0.0] * len(self.metrics)
        mode_used: RetrievalMode = self.mode
        fallback_reason = None
        cache_hit = False
        if self.mode == "hybrid":
            try:
                semantic, cache_hit = self._semantic_scores(question)
            except Exception as exc:  # retrieval must never take down Text-to-SQL
                mode_used = "keyword"
                fallback_reason = type(exc).__name__

        scored: list[tuple[float, int, dict[str, Any]]] = []
        candidate_indices = [
            index for index, metric in enumerate(self.metrics)
            if database_id is None or metric.get("database_id") == database_id
        ]
        for index in candidate_indices:
            metric = self.metrics[index]
            lexical_score, matched = lexical[index]
            semantic_score = semantic[index]
            if mode_used == "keyword":
                score, eligible = lexical_score, lexical_score > 0
            else:
                score = self.lexical_weight * lexical_score + self.semantic_weight * semantic_score
                eligible = lexical_score > 0 or semantic_score >= self.semantic_min_score
            if not eligible:
                continue
            item = deepcopy(metric)
            item["retrieval"] = {
                "score": round(score, 4), "lexical_score": round(lexical_score, 4),
                "semantic_score": round(semantic_score, 4), "matched_keywords": matched,
            }
            scored.append((score, index, item))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return {
            "metrics": [item for _, _, item in scored[:max(1, limit)]],
            "retrieval": {
                "requested_mode": self.mode, "mode_used": mode_used,
                "fallback_reason": fallback_reason, "query_cache_hit": cache_hit,
                "database_id": database_id,
                "candidate_count": len(candidate_indices),
                "elapsed_seconds": round(time.perf_counter() - started, 4),
            },
        }

    def retrieve(self, question: str, limit: int = 3,
                 database_id: str | None = None) -> list[dict[str, Any]]:
        """Compatibility method used by MCP and older callers."""
        return self.search(question, limit, database_id=database_id)["metrics"]

    @staticmethod
    def as_evidence(metrics: list[dict[str, Any]]) -> str:
        return "\n".join(
            f"业务口径[{metric['name']}|{metric.get('database_id', '')}]: {metric['definition']} "
            f"计算公式: {metric.get('formula', '')} 时间口径: {metric.get('time_grain', '')} "
            f"相关表字段: {', '.join(metric.get('tables', []) + metric.get('fields', []))} "
            f"SQL规则: {metric['sql_rule']}"
            for metric in metrics
        )


def create_enterprise_knowledge(app_config, *, mode: RetrievalMode | None = None) -> EnterpriseKnowledgeBase:
    """Build metric retrieval from the same embedding profile as Text-to-SQL."""
    from app.vector_db import get_embedding_function

    retrieval = app_config.business_metric_retrieval
    selected_mode = mode or retrieval.mode
    embedding_function = None
    if selected_mode == "hybrid":
        embedding = app_config.vector_database
        embedding_function = get_embedding_function(
            embedding.embedding_model_name_or_path, api_type=embedding.api_type,
            use_qwen3_embedding=embedding.use_qwen3_embedding,
            local_files_only=embedding.local_files_only,
            normalize_embeddings=embedding.normalize_embeddings,
            base_url=embedding.base_url, api_key=embedding.api_key,
            embedding_device=embedding.embedding_device,
        )
    return EnterpriseKnowledgeBase(
        mode=selected_mode, embedding_function=embedding_function,
        lexical_weight=retrieval.lexical_weight, semantic_weight=retrieval.semantic_weight,
        semantic_min_score=retrieval.semantic_min_score,
    )
