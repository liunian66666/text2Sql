"""Compare generation-only, generation+revision, and the full pipeline."""
import argparse
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path
import statistics
import sys
import tomllib
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dataset import load_dataset
from app.db_utils import execute_sql_without_cache
from runner.report_baseline import aggregate_usage, compare_execution, read_jsonl


VARIANTS = {
    "generation_only": ("few_shot_preparation", "value_retrieval", "schema_linking", "sql_generation"),
    "generation_revision": ("few_shot_preparation", "value_retrieval", "schema_linking", "sql_generation", "sql_revision"),
    "full_pipeline": ("few_shot_preparation", "value_retrieval", "schema_linking", "sql_generation", "sql_revision", "sql_selection"),
}
LABELS = {
    "generation_only": "仅生成",
    "generation_revision": "生成＋修订",
    "full_pipeline": "完整流程",
}


def _timestamp(value):
    return datetime.fromisoformat(value) if value else None


def separate_recovery_usage(usage_records, stage_runs):
    """Remove only calls that were repeated after a failed checkpointed stage.

    A failed stage can still leave valid per-item artifacts. On retry those items
    are skipped, so their first-attempt calls remain part of the final result.
    Only item IDs that occur again in a later completed run are recovery waste.
    """
    wasted_indexes = set()
    recovery_runs = []
    for failed in (run for run in stage_runs if run.get("status") == "failed"):
        stage = failed.get("stage")
        failed_start = _timestamp(failed.get("started_at"))
        failed_end = _timestamp(failed.get("finished_at"))
        completed = next((
            run for run in stage_runs
            if run.get("stage") == stage
            and run.get("status") == "completed"
            and _timestamp(run.get("started_at")) > failed_end
        ), None)
        if not completed:
            continue
        completed_start = _timestamp(completed.get("started_at"))
        completed_end = _timestamp(completed.get("finished_at"))
        retried_items = {
            str(record.get("item_id"))
            for record in usage_records
            if record.get("stage") == stage
            and record.get("item_id") is not None
            and completed_start <= _timestamp(record.get("timestamp")) <= completed_end
        }
        for index, record in enumerate(usage_records):
            record_time = _timestamp(record.get("timestamp"))
            if (
                record.get("stage") == stage
                and str(record.get("item_id")) in retried_items
                and failed_start <= record_time <= failed_end
            ):
                wasted_indexes.add(index)
        recovery_runs.append({
            "stage": stage,
            "retried_item_ids": sorted(retried_items),
            "failed_wall_seconds": failed.get("wall_seconds", 0),
        })
    wasted = [record for index, record in enumerate(usage_records) if index in wasted_indexes]
    kept = [record for index, record in enumerate(usage_records) if index not in wasted_indexes]
    recovery = None
    if recovery_runs:
        recovery = {
            "interrupted_attempts": len(recovery_runs),
            "wasted_response_tokens": sum(record.get("total_tokens") or 0 for record in wasted),
            "wasted_api_response_seconds": sum(record.get("elapsed_seconds") or 0 for record in wasted),
            "runs": recovery_runs,
        }
    return kept, recovery


def candidate_for(item, variant):
    if variant == "generation_only":
        candidates = item.sql_candidates or []
        return candidates[0] if candidates else "Error"
    if variant == "generation_revision":
        candidates = item.sql_candidates_after_revision or []
        return candidates[0] if candidates else "Error"
    return item.final_selected_sql or "Error"


def item_seconds(item, stages):
    seconds = 0.0
    if "few_shot_preparation" in stages:
        seconds += (item.few_shot_preparation_metadata or {}).get("wall_seconds", 0) or 0
    for stage in stages:
        if stage == "few_shot_preparation":
            continue
        seconds += getattr(item, f"{stage}_time", 0) or 0
    return seconds


def nearest_rank(values, percentile):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)] if ordered else None


def summarize_variant(rows, usage_records, stage_runs, stages):
    valid = [row for row in rows if row["execution_correct"] is not None]
    latencies = [row["seconds"] for row in rows]
    relevant_usage = [record for record in usage_records if record.get("stage") in stages]
    by_database = {}
    for database_id in sorted({row["database_id"] for row in rows}):
        database_rows = [row for row in rows if row["database_id"] == database_id]
        database_valid = [row for row in database_rows if row["execution_correct"] is not None]
        by_database[database_id] = {
            "count": len(database_rows),
            "execution_accuracy": sum(row["execution_correct"] for row in database_valid) / len(database_valid) if database_valid else None,
            "strict_multiset_accuracy": sum(row["multiset_match"] is True for row in database_valid) / len(database_valid) if database_valid else None,
        }
    return {
        "sample_count": len(rows),
        "valid_gold_count": len(valid),
        "correct_count": sum(row["execution_correct"] for row in valid),
        "execution_accuracy": sum(row["execution_correct"] for row in valid) / len(valid) if valid else None,
        "strict_correct_count": sum(row["multiset_match"] is True for row in valid),
        "strict_multiset_accuracy": sum(row["multiset_match"] is True for row in valid) / len(valid) if valid else None,
        "executable_count": sum(row["prediction_status"] in {"success", "empty_result", "all_null_result"} for row in rows),
        "mean_item_seconds": statistics.mean(latencies) if latencies else None,
        "p95_item_seconds": nearest_rank(latencies, .95),
        "batch_wall_seconds": sum(
            record.get("wall_seconds", 0) for record in stage_runs
            if record.get("status") == "completed" and record.get("stage") in stages
        ),
        "chat_usage": aggregate_usage([record for record in relevant_usage if record.get("kind", "chat") == "chat"]),
        "embedding_usage": aggregate_usage([record for record in relevant_usage if record.get("kind") == "embedding"]),
        "by_database": by_database,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    run = Path(args.run)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    dataset = load_dataset(str(run / "sql_selection.snapshot"))
    usage_records = read_jsonl(run / "measurement/llm_usage.jsonl")
    stage_runs = read_jsonl(run / "measurement/stage_runs.jsonl")
    usage_records, detected_recovery = separate_recovery_usage(usage_records, stage_runs)
    by_item = defaultdict(list)
    for record in usage_records:
        if record.get("item_id") is not None:
            by_item[str(record["item_id"])].append(record)

    question_rows = []
    variant_rows = {variant: [] for variant in VARIANTS}
    for item in dataset:
        gold = execute_sql_without_cache(item.database_path, item.gold_sql, timeout=60)
        question_record = {
            "question_id": item.question_id,
            "database_id": item.database_id,
            "difficulty": item.difficulty,
            "question": item.question,
            "gold_sql": item.gold_sql,
            "variants": {},
        }
        for variant, stages in VARIANTS.items():
            sql = candidate_for(item, variant)
            prediction = execute_sql_without_cache(item.database_path, sql, timeout=60)
            correct, multiset = compare_execution(prediction, gold)
            item_usage = [record for record in by_item[item.get_item_id()] if record.get("stage") in stages]
            row = {
                "question_id": item.question_id,
                "database_id": item.database_id,
                "difficulty": item.difficulty,
                "sql": sql,
                "execution_correct": correct,
                "multiset_match": multiset,
                "prediction_status": prediction.result_type,
                "prediction_error": prediction.error_message if prediction.result_rows is None else None,
                "seconds": item_seconds(item, stages),
                "chat_usage": aggregate_usage([record for record in item_usage if record.get("kind", "chat") == "chat"]),
                "embedding_usage": aggregate_usage([record for record in item_usage if record.get("kind") == "embedding"]),
            }
            variant_rows[variant].append(row)
            question_record["variants"][variant] = row
        question_rows.append(question_record)

    variants = {
        variant: summarize_variant(variant_rows[variant], usage_records, stage_runs, stages)
        for variant, stages in VARIANTS.items()
    }
    generation = {row["question_id"]: row for row in variant_rows["generation_only"]}
    revision = {row["question_id"]: row for row in variant_rows["generation_revision"]}
    full = {row["question_id"]: row for row in variant_rows["full_pipeline"]}
    transitions = {
        "revision_fixes": [question_id for question_id, row in revision.items() if row["execution_correct"] and not generation[question_id]["execution_correct"]],
        "revision_regressions": [question_id for question_id, row in revision.items() if not row["execution_correct"] and generation[question_id]["execution_correct"]],
        "selection_fixes": [question_id for question_id, row in full.items() if row["execution_correct"] and not revision[question_id]["execution_correct"]],
        "selection_regressions": [question_id for question_id, row in full.items() if not row["execution_correct"] and revision[question_id]["execution_correct"]],
    }
    config = tomllib.loads((run / "config.toml").read_text(encoding="utf-8"))
    sample_path = Path(config.get("dataset", {}).get("question_ids_path", "evals/bird-dev-50/sample.json"))
    sample = json.loads(sample_path.read_text(encoding="utf-8")) if sample_path.exists() else None
    recovery_path = run / "measurement/recovery.json"
    recovery = json.loads(recovery_path.read_text(encoding="utf-8")) if recovery_path.exists() else detected_recovery
    if detected_recovery and not recovery_path.exists():
        recovery_path.write_text(json.dumps(detected_recovery, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"variants": variants, "transitions": transitions, "sample": sample, "stage_runs": stage_runs, "recovery": recovery}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "questions.json").write_text(json.dumps(question_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    with (output / "questions.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        fields = ["question_id", "database_id", "difficulty", "variant", "execution_correct", "multiset_match", "prediction_status", "seconds", "total_tokens", "sql"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for variant, rows in variant_rows.items():
            for row in rows:
                writer.writerow({
                    **{key: row[key] for key in fields if key in row},
                    "variant": variant,
                    "total_tokens": row["chat_usage"]["total_tokens"] + row["embedding_usage"]["total_tokens"],
                })

    report = [
        "# BIRD-dev 50题消融实验", "",
        "固定样本覆盖5个数据库，每库10题；三个方案共享相同的检索、Schema Linking和候选生成结果。", "",
        "| 方案 | 集合式EX | 严格EX | 可执行 | 批次墙钟时间 | 在线token |", "|---|---:|---:|---:|---:|---:|",
    ]
    for variant in VARIANTS:
        metrics = variants[variant]
        total_tokens = metrics["chat_usage"]["total_tokens"] + metrics["embedding_usage"]["total_tokens"]
        report.append(
            f"| {LABELS[variant]} | {metrics['correct_count']}/{metrics['valid_gold_count']} ({metrics['execution_accuracy']:.1%}) | "
            f"{metrics['strict_correct_count']}/{metrics['valid_gold_count']} ({metrics['strict_multiset_accuracy']:.1%}) | "
            f"{metrics['executable_count']}/{metrics['sample_count']} | {metrics['batch_wall_seconds']:.2f}秒 | {total_tokens:,} |"
        )
    report += ["", "## 分数据库集合式EX", "", "| 数据库 | 仅生成 | 生成＋修订 | 完整流程 |", "|---|---:|---:|---:|"]
    database_ids = sorted(variants["full_pipeline"]["by_database"])
    for database_id in database_ids:
        values = [variants[variant]["by_database"][database_id]["execution_accuracy"] for variant in VARIANTS]
        report.append(f"| {database_id} | {values[0]:.1%} | {values[1]:.1%} | {values[2]:.1%} |")
    report += [
        "", "## 阶段影响", "",
        f"- 修订修复题号：{transitions['revision_fixes'] or '无'}",
        f"- 修订导致退化题号：{transitions['revision_regressions'] or '无'}",
        f"- 最终选择修复题号：{transitions['selection_fixes'] or '无'}",
        f"- 最终选择导致退化题号：{transitions['selection_regressions'] or '无'}",
    ]
    if recovery:
        report += [
            "", "## 恢复记录", "",
            f"- 正式实验前中断尝试：{recovery['interrupted_attempts']}次；已返回 {int(recovery['wasted_response_tokens']):,} token。",
            "- 这部分用量单独归档，没有混入三组可比在线成本。",
        ]
    report += [
        "", "## 口径", "",
        "- 仅生成取 `sql_candidates[0]`；生成＋修订取 `sql_candidates_after_revision[0]`；完整流程取 `final_selected_sql`。",
        "- 集合式EX忽略行顺序与重复次数；严格EX使用多重集合比较。",
        "- 在线token按方案实际需要的阶段累计。离线值索引和few-shot索引不计入三组在线成本。",
        "- 这是固定50题分层样本，不等同于完整BIRD-dev官方成绩。", "",
    ]
    (output / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps({variant: {key: variants[variant][key] for key in ("correct_count", "execution_accuracy", "strict_multiset_accuracy")} for variant in VARIANTS}, ensure_ascii=False))


if __name__ == "__main__":
    main()
