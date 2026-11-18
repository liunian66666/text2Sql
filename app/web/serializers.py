from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.dataset import load_dataset


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def serialize_agent_result(result) -> dict[str, Any]:
    payload = result.model_dump()
    payload["artifacts"] = {}
    snapshot = Path(result.artifacts_dir) / "sql_selection.snapshot"
    if snapshot.exists():
        try:
            item = load_dataset(str(snapshot))[0]
            generated = item.sql_candidates or []
            revised = item.sql_candidates_after_revision or []
            payload["artifacts"] = {
                "generated_sql": generated[0] if generated else None,
                "revised_sql": revised[0] if revised else None,
                "final_sql": item.final_selected_sql,
                "generated_candidates": generated,
                "revised_candidates": revised,
                "generated_candidate_count": len(generated),
                "revised_candidate_count": len(revised),
                "linked_schema": item.final_linked_tables_and_columns or {},
                "retrieved_value_columns": sum(
                    len(columns) for columns in (item.retrieved_values or {}).values()
                ),
                "few_shot_example_count": len(item.few_shot_examples or []),
            }
        except Exception:
            # The final result is still useful if an optional visualization cannot
            # read a snapshot produced by a different repository version.
            payload["artifacts"] = {}
    # Local filesystem paths are intentionally omitted from the browser response.
    payload.pop("artifacts_dir", None)
    return _json_safe(payload)


class EvaluationRepository:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _entries(self) -> dict[str, Path]:
        entries: dict[str, Path] = {}
        if not self.root.exists():
            return entries
        for path in self.root.rglob("summary.json"):
            evaluation_id = path.parent.relative_to(self.root).as_posix()
            entries[evaluation_id] = path
        # phase4b 公平路径对照（GraphRAG vs 最短路径）不在 evals/ 目录下，
        # 但它是真实跑过的实验产物；只读纳入，缺文件时完全不出现该条目。
        join_paths = self.root.parent / "workspace" / "evals" / "phase4b" / "join_paths.json"
        if join_paths.is_file():
            entries["phase4b-join-paths"] = join_paths
        return entries

    @staticmethod
    def _variant_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
        chat = metrics.get("chat_usage", {})
        embedding = metrics.get("embedding_usage", {})
        return {
            "sample_count": metrics.get("sample_count"),
            "correct_count": metrics.get("correct_count"),
            "execution_accuracy": metrics.get("execution_accuracy"),
            "strict_multiset_accuracy": metrics.get("strict_multiset_accuracy"),
            "executable_count": metrics.get("executable_count"),
            "mean_item_seconds": metrics.get("mean_item_seconds"),
            "p95_item_seconds": metrics.get("p95_item_seconds"),
            "batch_wall_seconds": metrics.get("batch_wall_seconds"),
            "total_tokens": (chat.get("total_tokens") or 0) + (embedding.get("total_tokens") or 0),
            "by_database": metrics.get("by_database", {}),
        }

    @staticmethod
    def _join_paths_summary(evaluation_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Same-budget GraphRAG vs shortest-path comparison, reported as-is.

        The two methods tie on gold-edge recall here; we surface that fact and
        keep the authors' own caveat notes instead of spin.
        """
        summary = data.get("summary", {})
        ours = summary.get("ours", {})
        baseline = summary.get("baseline_shortest", {})
        return {
            "id": evaluation_id,
            "kind": "join_paths",
            "title": "GraphRAG Join 路径公平对照（vs 最短路径）",
            "sample_count": ours.get("cases_total"),
            "methods": {
                name: {
                    "gold_edge_recall_micro": row.get("gold_edge_recall_micro"),
                    "gold_edge_hits": row.get("gold_edge_hits"),
                    "gold_edges_total": row.get("gold_edges_total"),
                    "path_found_rate": row.get("path_found_rate"),
                    "ambiguity_flag_rate": row.get("ambiguity_flag_rate"),
                    "top1_edge_recall_micro": row.get("top1_edge_recall_micro"),
                    "top1_complete_rate": row.get("top1_complete_rate"),
                    "composite_fk_complete_rate": row.get("composite_fk_complete_rate"),
                    "irrelevant_paths_total": row.get("irrelevant_paths_total"),
                    "mean_paths_per_case": row.get("mean_paths_per_case"),
                }
                for name, row in summary.items()
            },
            "fairness": data.get("fairness", []),
            "notes": data.get("notes", []),
            "generated_at": data.get("generated_at"),
            "budget": data.get("budget"),
        }

    def _summary(self, evaluation_id: str, path: Path) -> dict[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("dataset") == "adventureworks-graphrag-join":
            return self._join_paths_summary(evaluation_id, data)
        if data.get("evaluation") == "enterprise-agent-evaluation-v1":
            return {
                "id": evaluation_id,
                "kind": "agent",
                "title": "企业分析 Agent 专项评测",
                "sample_count": data.get("case_count"),
                "passed_count": data.get("passed_count"),
                "pass_rate": data.get("pass_rate"),
                "mean_item_seconds": data.get("mean_seconds"),
                "total_tokens": data.get("total_tokens"),
                "evaluation_mode": data.get("evaluation_mode", "full"),
                "model": data.get("model"),
            }
        if data.get("evaluation") == "enterprise-metric-rag-v1":
            variants = {
                name: {
                    "case_count": row.get("case_count"),
                    "recall_at_k": row.get("recall_at_k"),
                    "mrr": row.get("mrr"),
                    "mean_seconds": row.get("mean_seconds"),
                }
                for name, row in data.get("variants", {}).items()
            }
            return {
                "id": evaluation_id,
                "kind": "retrieval",
                "title": "业务口径混合 RAG 对照",
                "sample_count": next((row.get("case_count") for row in variants.values()), 0),
                "variants": variants,
                "embedding_usage": data.get("hybrid_embedding_usage", {}),
                "delta": data.get("delta", {}),
            }
        if "variants" in data:
            variants = {name: self._variant_metrics(metrics) for name, metrics in data["variants"].items()}
            sample_count = next((v.get("sample_count") for v in variants.values()), None)
            return {
                "id": evaluation_id,
                "kind": "ablation",
                "title": "50题三组对照实验" if sample_count == 50 else f"{sample_count or ''}题三组对照实验",
                "sample_count": sample_count,
                "variants": variants,
                "transitions": data.get("transitions", {}),
                "recovery": data.get("recovery"),
            }
        chat = data.get("online_chat_usage", {})
        embedding = data.get("online_embedding_usage", {})
        return {
            "id": evaluation_id,
            "kind": "baseline",
            "title": f"{data.get('sample_count', '')}题基线评测",
            "sample_count": data.get("sample_count"),
            "correct_count": data.get("correct_count"),
            "execution_accuracy": data.get("execution_accuracy"),
            "strict_multiset_accuracy": data.get("strict_multiset_accuracy"),
            "successful_executions": data.get("successful_executions"),
            "mean_item_seconds": data.get("mean_query_stage_seconds"),
            "p95_item_seconds": data.get("p95_query_stage_seconds_nearest_rank"),
            "total_tokens": (chat.get("total_tokens") or 0) + (embedding.get("total_tokens") or 0),
        }

    def list(self) -> list[dict[str, Any]]:
        return [self._summary(key, path) for key, path in sorted(self._entries().items())]

    def get(self, evaluation_id: str) -> dict[str, Any]:
        entries = self._entries()
        if evaluation_id not in entries:
            raise KeyError(evaluation_id)
        summary = self._summary(evaluation_id, entries[evaluation_id])
        raw_summary = json.loads(entries[evaluation_id].read_text(encoding="utf-8"))
        if summary["kind"] == "agent":
            return {**summary, "cases": raw_summary.get("cases", [])}
        if summary["kind"] == "join_paths":
            runs = []
            for item in raw_summary.get("runs", []):
                runs.append({key: item.get(key) for key in (
                    "case_id", "category", "method", "gold_edge_count",
                    "gold_edge_hits", "gold_edge_recall", "path_count", "path_found",
                    "top1_edge_hits", "top1_complete", "ambiguous_flagged",
                    "expect_ambiguity", "composite_fk_complete", "missing_gold_edges",
                    "irrelevant_paths", "seeds", "targets")})
            return {**summary, "cases": runs}
        if summary["kind"] == "retrieval":
            return {
                **summary,
                "variant_cases": {
                    name: row.get("cases", [])
                    for name, row in raw_summary.get("variants", {}).items()
                },
            }
        questions_path = entries[evaluation_id].with_name("questions.json")
        questions = []
        if questions_path.exists():
            raw = json.loads(questions_path.read_text(encoding="utf-8"))
            for item in raw:
                if "variants" in item:
                    questions.append({
                        "question_id": item.get("question_id"),
                        "database_id": item.get("database_id"),
                        "difficulty": item.get("difficulty"),
                        "question": item.get("question"),
                        "variants": {
                            name: {
                                key: row.get(key)
                                for key in ("execution_correct", "multiset_match", "prediction_status", "seconds", "sql")
                            }
                            for name, row in item.get("variants", {}).items()
                        },
                    })
                else:
                    questions.append({
                        key: item.get(key)
                        for key in (
                            "question_id", "database_id", "difficulty", "question",
                            "predicted_sql", "execution_correct", "multiset_match",
                            "prediction_status", "returned_rows", "query_stage_seconds",
                        )
                    })
        return {**summary, "questions": questions}
