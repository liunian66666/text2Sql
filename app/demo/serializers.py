from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.dataset import load_dataset


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def serialize_skill_result(result) -> dict[str, Any]:
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

    def _summary(self, evaluation_id: str, path: Path) -> dict[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8"))
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
