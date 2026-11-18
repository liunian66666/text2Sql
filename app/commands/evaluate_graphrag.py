"""``enterprise-agent evaluate-graphrag`` — reproducible A/B/C/D retrieval comparison.

Configurations:

* A — deterministic keyword Schema retrieval (Table-first + Column-first).
* B — A + dense recall (only when a **real** local embedding backend loads).
* C — B + metric dependency expansion (unreviewed metrics stay soft candidates).
* D — C + GraphRAG intermediate-table / Join-path completion + SchemaContext.

Honesty contract: when ``--vector-mode none`` is used, B/C/D are executed with
the deterministic keyword channel only and the result explicitly records
``vector_status = not_run``. No mock vectors are ever scored, and no result is
presented as a model measurement. ``test.json`` is a sealed split: passing it
as ``--cases`` is refused unless ``--allow-test-split`` is given explicitly.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time
from typing import Any

from app.knowledge.graph_store import GraphStore
from app.knowledge.metric_registry import MetricRegistry
from app.knowledge.models import DatabaseSchema
from app.knowledge.review import ReviewLedger
from app.knowledge.retrieval.context_builder import build_context, estimate_tokens
from app.knowledge.retrieval.hybrid import (
    HybridIndex, LocalSentenceTransformerBackend, build_documents,
)
from app.knowledge.retrieval.join_path import JoinPathFinder
from app.knowledge.retrieval.schema_retriever import SchemaRetriever
from app.knowledge.retrieval.subgraph import SubgraphExpander
from app.knowledge.schema_dictionary import load_dictionary
from app.knowledge.schema_extractor import PROJECT_ROOT

DEFAULT_CASES = "evals/adventureworks-graphrag/dev.json"
DEFAULT_OUTPUT = "workspace/evals/graphrag-abcd.json"
DEFAULT_MODEL = "data/models/Qwen/Qwen3-Embedding-0___6B"
CONFIGS = ("A", "B", "C", "D")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cases", default=DEFAULT_CASES, help="开发集 JSON（默认 dev.json）")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="结果输出路径")
    parser.add_argument("--configs", default="A,B,C,D", help="要运行的配置，逗号分隔")
    parser.add_argument("--vector-mode", choices=("none", "local"), default="none",
                        help="none=确定性关键词；local=真实本地 SentenceTransformer")
    parser.add_argument("--local-model", default=DEFAULT_MODEL, help="本地 embedding 模型目录")
    parser.add_argument("--fusion", choices=("weighted", "rrf"), default="weighted",
                        help="融合策略；默认沿用现有加权融合，RRF 仅供对照")
    parser.add_argument("--allow-test-split", action="store_true",
                        help="显式允许使用封存的 test.json（默认拒绝）")
    parser.add_argument("--validate-only", action="store_true", help="只校验题单，不执行检索")
    parser.add_argument("--quiet", action="store_true", help="只输出摘要行")


def _absolute(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


class EvalContext:
    """Load all real artifacts once and share the corpus vectors across configs."""

    def __init__(self, root: Path, *, vector_mode: str, local_model: str):
        self.root = root
        self.artifacts = root / "workspace" / "knowledge" / "adventureworks"
        required = [
            self.artifacts / "schema_adventureworks.json",
            self.artifacts / "relations.json", self.artifacts / "snapshot.json",
            root / "app/enterprise/adventureworks_metrics.json",
            root / "app/enterprise/adventureworks_dictionary.json",
        ]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise FileNotFoundError(f"Knowledge artifacts missing: {missing}")
        self.schema = DatabaseSchema.model_validate(
            json.loads((self.artifacts / "schema_adventureworks.json").read_text(encoding="utf-8")))
        registry = MetricRegistry.from_file(root / "app/enterprise/adventureworks_metrics.json")
        self.ledger = ReviewLedger.from_file(
            root / "app/enterprise/adventureworks_review_decisions.json")
        self.registry, self.review_audit = self.ledger.apply(registry)
        self.dictionary = load_dictionary(
            root / "app/enterprise/adventureworks_dictionary.json")
        self.store = GraphStore.from_artifacts(self.artifacts)
        self.documents = build_documents(self.registry, {"adventureworks": self.schema},
                                         self.dictionary)
        self.backend = None
        self.backend_error = None
        if vector_mode == "local":
            try:
                self.backend = LocalSentenceTransformerBackend(
                    _absolute(root, local_model))
                _ = self.backend.embed_documents([self.documents[0].text])
            except Exception as exc:
                self.backend = None
                self.backend_error = f"{type(exc).__name__}: {exc}"
        self.index = HybridIndex(self.documents, dense_backend=self.backend)
        self.retriever = SchemaRetriever(self.documents, self.index, self.registry,
                                         {"adventureworks": self.schema})
        self.expander = SubgraphExpander(self.store, max_hops=4, max_tables=12,
                                         max_columns=60)
        self.finder = JoinPathFinder(self.store, max_hops=4, max_paths=8)
        self.vector_real = self.backend is not None

    @property
    def column_names(self) -> set[str]:
        return {column.column_name for table in self.schema.tables
                for column in table.columns}


def _required_columns(reference_sql: str, column_names: set[str]) -> set[str]:
    """Deterministic ground truth: column identifiers appearing in the real SQL."""
    found: set[str] = set()
    for name in column_names:
        if len(name) < 3:
            continue
        # Word-boundary match against a SQL identifier; case-insensitive.
        import re
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", reference_sql, re.IGNORECASE):
            found.add(name)
    return found


def _join_strings(join_paths: list[dict[str, Any]]) -> list[str]:
    result = []
    for path in join_paths:
        for condition in path.get("join_conditions", []):
            left = condition["left_table"].rsplit(".", 1)[-1]
            right = condition["right_table"].rsplit(".", 1)[-1]
            result.append(
                f"{left}.{','.join(condition.get('source_columns', []))} -> "
                f"{right}.{','.join(condition.get('target_columns', []))}")
    return result


def _join_hit(acceptable: str, predicted: list[str]) -> bool:
    parts = acceptable.split("->")
    left = parts[0].strip()
    right = parts[1].strip() if len(parts) > 1 else ""
    left_col = left.rsplit(".", 1)[-1]
    right_col = right.rsplit(".", 1)[-1]
    left_table = left.rsplit(".", 1)[0] if "." in left else ""
    right_table = right.rsplit(".", 1)[0] if "." in right else ""
    for text in predicted:
        if left_col in text and (not right_col or right_col in text):
            if not left_table or left_table in text or right_table in text:
                return True
    return False


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def run_case(context: EvalContext, case: dict[str, Any], config: str, *,
             fusion: str) -> dict[str, Any]:
    question = case["question"]
    required_tables = set(case["required_tables"])
    required_intermediate = set(case.get("required_intermediate_tables") or [])
    required_columns = _required_columns(case["reference_sql"], context.column_names)
    vector_active = config in {"B", "C", "D"} and context.vector_real
    mode = "hybrid" if vector_active else "keyword"
    metric_expansion = config in {"C", "D"}
    graph_enabled = config == "D"

    started = time.perf_counter()
    retrieval = context.retriever.retrieve(
        question, database_id="adventureworks", mode=mode,
        fusion=fusion if vector_active else "weighted", metric_expansion=metric_expansion)
    predicted_tables = set(retrieval.candidate_tables)
    predicted_columns = set(retrieval.candidate_columns)
    join_paths: list[dict[str, Any]] = []
    intermediate: list[str] = []
    context_text = " ".join([*retrieval.candidate_tables, *retrieval.candidate_columns])
    fallback = False
    schema_context = None

    if graph_enabled:
        focus = list(dict.fromkeys([
            *retrieval.table_first_tables[:2], *retrieval.column_first_tables[:2],
            *retrieval.candidate_tables[:3]]))[:5]
        targets = list(dict.fromkeys([*focus, *retrieval.candidate_tables[:6]]))
        subgraph = context.expander.expand(
            retrieval.candidate_tables, retrieval.candidate_columns,
            target_tables=targets, focus_tables=focus, allow_extension=True)
        paths = context.finder.find(
            subgraph.tables, seed_tables=focus[:3],
            column_hints=subgraph.columns[:30],
            required_targets=[t for t in targets if t in subgraph.tables])
        schema_context = build_context(retrieval, subgraph, paths, max_tokens=900)
        predicted_tables |= set(schema_context.candidate_tables) | set(subgraph.tables)
        predicted_columns |= set(schema_context.candidate_columns)
        for path in paths.paths:
            predicted_tables.update(path.tables)
        join_paths = [path.model_dump(mode="json") for path in paths.paths]
        intermediate = list(subgraph.intermediate_tables)
        context_text = schema_context.to_evidence_text()
        fallback = subgraph.fallback_triggered

    table_hits = predicted_tables & required_tables
    column_hits = {name for name in required_columns
                   if any(pred.rsplit(".", 1)[-1] == name for pred in predicted_columns)}
    acceptable = case.get("acceptable_join_paths") or []
    predicted_joins = _join_strings(join_paths)
    join_hits = sum(1 for item in acceptable if _join_hit(item, predicted_joins))
    composite_needed = "Sales.SpecialOfferProduct" in required_tables or any(
        "复合外键" in str(rule) for rule in case.get("validation_rules", []))
    composite_complete = None
    if composite_needed:
        composite_complete = any(
            "SpecialOfferID" in text and "ProductID" in text for text in predicted_joins)

    return {
        "case_id": case["id"], "category": case["category"], "config": config,
        "mode_used": mode, "vector_active": vector_active,
        "table_recall": _rate(len(table_hits), len(required_tables)),
        "column_recall": _rate(len(column_hits), len(required_columns)),
        "complete_schema_recall": float(table_hits == required_tables
                                        and column_hits == required_columns),
        "intermediate_table_recall": _rate(
            len(required_intermediate & predicted_tables), len(required_intermediate))
            if required_intermediate else None,
        "join_path_correct_rate": _rate(join_hits, len(acceptable)) if acceptable else None,
        "composite_fk_complete": composite_complete,
        "candidate_table_count": len(predicted_tables),
        "candidate_column_count": len(predicted_columns),
        "context_tokens": estimate_tokens(context_text),
        "join_path_count": len(join_paths),
        "intermediate_tables": intermediate,
        "fallback_triggered": fallback,
        "schema_context_tokens": (schema_context.context_budget.estimated_tokens
                                  if schema_context is not None else None),
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "required_tables": sorted(required_tables),
        "required_columns_count": len(required_columns),
        "missing_tables": sorted(required_tables - predicted_tables),
    }


def _mean(runs: list[dict[str, Any]], key: str) -> float | None:
    values = [row[key] for row in runs if row.get(key) is not None]
    return round(statistics.fmean(values), 6) if values else None


def _build_payload(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    cases_path = _absolute(root, args.cases)
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    split = payload.get("split", "")
    if split == "test" and not args.allow_test_split:
        raise ValueError("test.json 是封存独立测试集，禁止用于检索调试/参数调优；"
                         "如确需评测请显式传入 --allow-test-split")
    if split not in {"dev", "test"}:
        raise ValueError(f"unknown split: {split}")
    cases = payload["cases"]
    configs = [item.strip().upper() for item in args.configs.split(",") if item.strip()]
    unknown = [item for item in configs if item not in CONFIGS]
    if unknown:
        raise ValueError(f"unknown configs: {unknown}")
    if args.validate_only:
        return {"validated_only": True, "split": split, "case_count": len(cases),
                "configs": configs, "cases_path": str(cases_path)}
    context = EvalContext(root, vector_mode=args.vector_mode,
                          local_model=args.local_model)
    runs: list[dict[str, Any]] = []
    for config in configs:
        for case in cases:
            runs.append(run_case(context, case, config, fusion=args.fusion))
    summary = {}
    for config in configs:
        subset = [row for row in runs if row["config"] == config]
        summary[config] = {
            key: _mean(subset, key) for key in (
                "table_recall", "column_recall", "complete_schema_recall",
                "intermediate_table_recall", "join_path_correct_rate",
                "candidate_table_count", "candidate_column_count",
                "context_tokens", "join_path_count", "elapsed_seconds")
        }
        composite = [row["composite_fk_complete"] for row in subset
                     if row["composite_fk_complete"] is not None]
        summary[config]["composite_fk_complete_rate"] = (
            round(sum(bool(item) for item in composite) / len(composite), 6)
            if composite else None)
        summary[config]["fallback_cases"] = sum(
            1 for row in subset if row["fallback_triggered"])
        summary[config]["mode_used"] = subset[0]["mode_used"] if subset else None
    return {
        "dataset": payload.get("dataset"), "split": split,
        "case_count": len(cases), "cases_path": str(cases_path.relative_to(root).as_posix()),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "configs": configs, "fusion": args.fusion,
        "vector_mode_requested": args.vector_mode,
        "vector_backend_label": (context.backend.label if context.backend else None),
        "vector_backend_real": context.vector_real,
        "vector_backend_error": context.backend_error,
        "vector_status": ("real_local_model" if context.vector_real else
                          "not_run: deterministic keyword only; no mock vectors"),
        "dictionary_coverage": context.dictionary.coverage(),
        "graph_validation": context.store.validate(),
        "structural_checks": {
            "fk_constraint_groups": len(context.store.relations),
            "fk_column_pairs": sum(len(relation.source_columns)
                                   for relation in context.store.relations),
            "composite_fk_groups": len(context.store.composite_relations()),
            "composite_fk_columns_preserved": all(
                len(item.source_columns) == len(item.target_columns) == 2
                for item in context.store.composite_relations()),
            "composite_fk_complete_rate": 1.0 if context.store.composite_relations() and all(
                len(item.source_columns) == len(item.target_columns) == 2
                for item in context.store.composite_relations()) else 0.0,
            "multi_role_table_pairs": context.store.validate()["multi_role_table_pairs"],
            "note": ("dev.json 12 题中没有直接引用 SpecialOfferProduct 复合外键的题目，"
                     "因此 case 级 composite_fk_complete 为 null；此处给出的是基于真实"
                     "PRAGMA 约束组的结构校验，不是题目准确率。"),
        },
        "review_audit": context.review_audit,
        "isolation": {
            "test_split_used": split == "test",
            "test_split_policy": "test.json 默认拒绝；不得用于 Prompt 调优 / Few-shot / 参数调优",
        },
        "notes": [
            "B/C/D 若 vector_backend_real=false，则实际只执行确定性关键词通道，"
            "结果不得解释为模型向量检索成绩。",
            "A/B/C 无图扩展，因此 join_path_correct_rate 与中间表补全按真实 0/候选结果统计。",
            "所有 required_columns 由 reference_sql 中真实出现的列名确定性提取，非人工标注臆测。",
            "未调用任何在线 LLM；未运行 BIRD / 指标在线评测；既有 72%→76% 不归因于本模块。",
        ],
        "runs": runs, "summary": summary,
        "summary_by_category": {
            category: {config: {
                "table_recall": _mean([row for row in runs
                                       if row["config"] == config and row["category"] == category],
                                      "table_recall"),
                "join_path_correct_rate": _mean(
                    [row for row in runs if row["config"] == config
                     and row["category"] == category], "join_path_correct_rate"),
            } for config in configs}
            for category in sorted({case["category"] for case in cases})
        },
    }


def run(args: argparse.Namespace) -> int:
    root = PROJECT_ROOT
    payload = _build_payload(root, args)
    if not args.validate_only:
        output = _absolute(root, args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
    if args.quiet:
        print(json.dumps({"validated_only": args.validate_only,
                          "split": payload.get("split"),
                          "case_count": payload.get("case_count"),
                          "configs": payload.get("configs"),
                          "vector_status": payload.get("vector_status"),
                          "summary": payload.get("summary")},
                         ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps({key: payload[key] for key in payload if key != "runs"},
                         ensure_ascii=False, indent=2))
    return 0
