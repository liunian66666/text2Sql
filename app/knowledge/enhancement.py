"""Optional, fail-safe knowledge enhancement for the Text-to-SQL request.

Integration contract (minimal invasion):

* OFF by default (``ENABLE_KNOWLEDGE_ENHANCEMENT=1`` to opt in).
* Only active for ``database_id == "adventureworks"``; the original Sales/HR
  path is byte-for-byte unchanged even when the flag is on.
* Output is delivered through the *existing* ``request["evidence"]`` string,
  which reaches every upstream prompt as ``HINT``. No SQL generation algorithm,
  no ``DataItem.database_schema`` pruning, no new LangGraph tool, no new
  dependency and no change to the SSE wire format.
* ``hint`` mode only. ``hard_filter`` is a separate future mode that would need
  explicit verification of Value Retrieval / Few-shot / Schema Linking / SQL
  Revision completeness; it is intentionally not enabled here.
* Any failure degrades to an empty hint and is reported, never raised into the
  Text-to-SQL call path.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from .graph_store import GraphStore
from .metric_registry import MetricRegistry
from .models import DatabaseSchema
from .review import ReviewLedger
from .retrieval.context_builder import build_context, estimate_tokens
from .retrieval.hybrid import build_documents
from .retrieval.join_path import JoinPathFinder
from .retrieval.schema_retriever import SchemaRetriever
from .retrieval.subgraph import SubgraphExpander
from .schema_dictionary import load_dictionary

ENABLE_ENV = "ENABLE_KNOWLEDGE_ENHANCEMENT"
MODE_ENV = "KNOWLEDGE_ENHANCEMENT_MODE"
# Phase 4B: 显式打开检索通道的向量后端。默认不设置 —— 默认仍是纯关键词检索，
# 原因是本地 Qwen3 首次加载约 50 秒，不能让 Web 首请求悄悄背上这个成本。
# 取值：``local``（用仓库内 Qwen3-Embedding-0.6B，零在线配额）或显式模型目录。
DENSE_ENV = "KNOWLEDGE_ENHANCEMENT_DENSE"
DENSE_LOCAL_ALIASES = {"1", "true", "yes", "on", "local"}
SUPPORTED_MODE = "hint"
SUPPORTED_DATABASE = "adventureworks"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACTS = PROJECT_ROOT / "workspace" / "knowledge" / SUPPORTED_DATABASE
DEFAULT_METRICS = PROJECT_ROOT / "app" / "enterprise" / "adventureworks_metrics.json"
DEFAULT_DICTIONARY = PROJECT_ROOT / "app" / "enterprise" / "adventureworks_dictionary.json"
DEFAULT_LOCAL_EMBEDDING = PROJECT_ROOT / "data/models/Qwen/Qwen3-Embedding-0___6B"

_dense_backend_cache: dict[str, Any] = {}
# 稠密索引（把 595 篇知识文档编码成向量）必须缓存：不缓存时实测每次增强请求
# 要 348 秒，全部耗在重复编码同一批文档上。键 = 后端标签 + 文档内容指纹，
# 所以知识产物一变，旧缓存自动失效，不会喂过期向量。
_dense_index_cache: dict[str, Any] = {}
DENSE_INDEX_CACHE_LIMIT = 4


def is_enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def dense_model_path_from_env(project_root: Path | None = None) -> str | None:
    """Resolve ``KNOWLEDGE_ENHANCEMENT_DENSE`` into a concrete model directory.

    Pure function on purpose: the decision is testable without loading torch.
    """
    raw = os.environ.get(DENSE_ENV, "").strip()
    if not raw:
        return None
    if raw.lower() in DENSE_LOCAL_ALIASES:
        root = (project_root or PROJECT_ROOT).resolve()
        return str(root / "data/models/Qwen/Qwen3-Embedding-0___6B")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (project_root or PROJECT_ROOT) / candidate
    return str(candidate)


def dense_backend_from_env(project_root: Path | None = None) -> Any | None:
    """Cached local embedding backend for the dense retrieval channel.

    Returns ``None`` when the env var is unset, so the default stays keyword-only.
    """
    model_path = dense_model_path_from_env(project_root)
    if model_path is None:
        return None
    if model_path not in _dense_backend_cache:
        from .retrieval.hybrid import LocalSentenceTransformerBackend

        _dense_backend_cache[model_path] = LocalSentenceTransformerBackend(model_path)
    return _dense_backend_cache[model_path]


def documents_fingerprint(documents: list[Any]) -> str:
    """Stable fingerprint of the knowledge corpus used to build a dense index."""
    import hashlib

    digest = hashlib.sha256()
    for document in documents:
        digest.update(str(getattr(document, "doc_id", "")).encode())
        digest.update(b"\x00")
        digest.update(str(getattr(document, "text", "")).encode())
        digest.update(b"\x00")
    return digest.hexdigest()


def build_hybrid_index(documents: list[Any], dense_backend: Any | None):
    """Build (or reuse) the hybrid index; dense encoding is cached corpus-wide."""
    from .retrieval.hybrid import HybridIndex

    if dense_backend is None:
        return HybridIndex(documents)
    key = f"{getattr(dense_backend, 'label', type(dense_backend).__name__)}:" \
          f"{documents_fingerprint(documents)}"
    cached = _dense_index_cache.get(key)
    if cached is not None:
        return cached
    index = HybridIndex(documents, dense_backend=dense_backend)
    if len(_dense_index_cache) >= DENSE_INDEX_CACHE_LIMIT:
        _dense_index_cache.clear()
    _dense_index_cache[key] = index
    return index


class EnhancementResult(BaseModel):
    enabled: bool = False
    mode: str = SUPPORTED_MODE
    database_id: str = ""
    reason: str = ""
    evidence_text: str = ""
    schema_context: dict[str, Any] = Field(default_factory=dict)
    observability: dict[str, Any] = Field(default_factory=dict)
    elapsed_seconds: float = 0.0
    error: str | None = None


def _disabled(database_id: str, reason: str) -> EnhancementResult:
    return EnhancementResult(enabled=False, database_id=database_id, reason=reason)


def _emit(on_stage: Callable[[dict], None] | None, event: dict[str, Any]) -> None:
    if on_stage is None:
        return
    try:
        on_stage(event)
    except Exception:
        pass  # observability must never break the query path


def build_enhancement(question: str, database_id: str, *,
                      project_root: Path | None = None,
                      mode: str = "keyword", fusion: str = "weighted",
                      dense_backend: Any | None = None,
                      max_tokens: int = 900,
                      on_stage: Callable[[dict], None] | None = None) -> EnhancementResult:
    """Run recall -> subgraph -> join paths -> SchemaContext -> evidence hint."""
    started = time.perf_counter()
    if not is_enabled():
        return _disabled(database_id, f"{ENABLE_ENV} not set")
    if database_id != SUPPORTED_DATABASE:
        return _disabled(database_id, "knowledge enhancement only enabled for adventureworks")
    requested_mode = os.environ.get(MODE_ENV, SUPPORTED_MODE).strip().lower()
    if requested_mode != SUPPORTED_MODE:
        return _disabled(database_id, f"mode={requested_mode} not supported; only hint is enabled")

    root = (project_root or PROJECT_ROOT).resolve()
    artifacts = root / "workspace" / "knowledge" / SUPPORTED_DATABASE
    schema_path = artifacts / f"schema_{SUPPORTED_DATABASE}.json"
    metrics_path = root / "app" / "enterprise" / "adventureworks_metrics.json"
    dictionary_path = root / "app" / "enterprise" / "adventureworks_dictionary.json"

    _emit(on_stage, {"stage": "knowledge_retrieval", "status": "started",
                     "database_id": database_id, "mode": requested_mode})
    try:
        for path in (schema_path, metrics_path, dictionary_path, artifacts / "relations.json",
                     artifacts / "snapshot.json"):
            if not path.is_file():
                raise FileNotFoundError(f"Knowledge artifact missing: {path}")
        schema = DatabaseSchema.model_validate(json.loads(schema_path.read_text(encoding="utf-8")))
        registry = MetricRegistry.from_file(metrics_path)
        ledger = ReviewLedger.from_file(
            root / "app" / "enterprise" / "adventureworks_review_decisions.json")
        registry, review_audit = ledger.apply(registry, database_id=SUPPORTED_DATABASE)
        dictionary = load_dictionary(dictionary_path)
        store = GraphStore.from_artifacts(artifacts, database_id=SUPPORTED_DATABASE)

        documents = build_documents(registry, {SUPPORTED_DATABASE: schema}, dictionary)
        index = build_hybrid_index(documents, dense_backend)
        retriever = SchemaRetriever(documents, index, registry,
                                    {SUPPORTED_DATABASE: schema})
        retrieval = retriever.retrieve(
            question, database_id=SUPPORTED_DATABASE, mode=mode, fusion=fusion,
            metric_expansion=True)

        # Focus tables are the strongest *original* retrieval hits. They anchor
        # both target-aware path completion and Join path enumeration.
        focus = list(dict.fromkeys([
            *retrieval.table_first_tables[:2],
            *retrieval.column_first_tables[:2],
            *retrieval.candidate_tables[:3],
        ]))[:5]
        target_tables = list(dict.fromkeys([
            *focus,
            *retrieval.candidate_tables[:6],
        ]))
        expander = SubgraphExpander(store, max_hops=4, max_tables=12, max_columns=60)
        subgraph = expander.expand(
            retrieval.candidate_tables, retrieval.candidate_columns,
            target_tables=target_tables, focus_tables=focus, allow_extension=True)

        finder = JoinPathFinder(store, max_hops=4, max_paths=8)
        paths = finder.find(
            subgraph.tables, seed_tables=focus[:3],
            column_hints=subgraph.columns[:30],
            required_targets=[t for t in target_tables if t in subgraph.tables])

        context = build_context(retrieval, subgraph, paths, max_tokens=max_tokens,
                                mode=SUPPORTED_MODE)
        evidence_text = context.to_evidence_text()
        observability = {
            # 检索通道必须可审计：否则「GraphRAG 生效」无法区分是关键词还是真向量。
            "retrieval_mode": mode,
            "dense_backend": (type(dense_backend).__name__ if dense_backend is not None
                              else None),
            "original_seeds": {"tables": subgraph.seed_tables,
                               "columns": subgraph.seed_columns},
            "graph_added_nodes": {"tables": subgraph.added_tables,
                                  "columns": subgraph.added_columns},
            "necessary_intermediate_tables": subgraph.intermediate_tables,
            "candidate_join_paths": [p.tables for p in paths.paths],
            "excluded_paths": paths.excluded_paths[:10],
            "composite_fk_handling": {
                "groups": [item.relation_id for item in store.composite_relations()],
                "all_paths_complete": all(p.composite_fk_complete for p in paths.paths),
            },
            "grain_risks": [risk for risk in paths.risks
                            if risk["kind"] in {"mixed_grain", "duplicate_aggregation",
                                                "historical_interval_missing"}],
            "ambiguity_count": len(paths.ambiguity),
            "final_schema_context": context.model_dump(mode="json"),
            "context_tokens": estimate_tokens(evidence_text),
            "dictionary_coverage": dictionary.coverage(),
            "review_audit": review_audit,
            "retrieval_elapsed_seconds": retrieval.retrieval_trace.get(
                "metric_channel", {}).get("elapsed_seconds"),
            "total_elapsed_seconds": round(time.perf_counter() - started, 6),
        }
        result = EnhancementResult(
            enabled=True, mode=SUPPORTED_MODE, database_id=database_id,
            evidence_text=evidence_text, schema_context=context.model_dump(mode="json"),
            observability=observability,
            elapsed_seconds=round(time.perf_counter() - started, 6),
        )
        _emit(on_stage, {"stage": "knowledge_retrieval", "status": "completed",
                         "database_id": database_id,
                         "candidate_tables": len(context.candidate_tables),
                         "candidate_columns": len(context.candidate_columns),
                         "join_paths": len(context.join_paths),
                         "intermediate_tables": subgraph.intermediate_tables,
                         "risks": len(context.aggregation_risks),
                         "ambiguity": len(context.ambiguity),
                         "context_tokens": observability["context_tokens"],
                         "elapsed_seconds": result.elapsed_seconds})
        return result
    except Exception as exc:
        reason = f"{type(exc).__name__}"
        _emit(on_stage, {"stage": "knowledge_retrieval", "status": "failed",
                         "database_id": database_id, "error": reason})
        return EnhancementResult(
            enabled=True, database_id=database_id, reason="degraded: knowledge enhancement failed",
            error=reason, elapsed_seconds=round(time.perf_counter() - started, 6))
