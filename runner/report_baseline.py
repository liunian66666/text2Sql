"""Export reproducible execution accuracy, latency and response-level usage."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import tomllib
import re

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dataset import load_dataset
from app.db_utils import execute_sql_without_cache

PIPELINE_STAGES = ("value_retrieval", "schema_linking", "sql_generation", "sql_revision", "sql_selection")


def read_jsonl(path):
    path = Path(path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def aggregate_usage(records):
    return {
        "responses": len(records),
        "missing_usage_responses": sum(not r.get("usage_available", True) for r in records),
        **{key: sum(r.get(key) or 0 for r in records) for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
    }


def compare_execution(pred, gold):
    if gold.result_rows is None:
        return None, None
    if pred.result_rows is None:
        return False, False
    # Match the repository's BIRD/Spider evaluator. Also expose duplicate-row
    # sensitivity separately; do not silently replace its set-based metric.
    return set(pred.result_rows) == set(gold.result_rows), Counter(pred.result_rows) == Counter(gold.result_rows)


def redact_config(value):
    if isinstance(value, dict):
        return {k: "<redacted>" if k in {"api_key", "password", "secret"} else redact_config(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_config(v) for v in value]
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--reused-setup-ledger", action="append", default=[])
    args = parser.parse_args()
    run = Path(args.run)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    dataset = load_dataset(str(run / "sql_selection.snapshot"))
    if not dataset or not all(item.is_stage_complete("sql_selection") for item in dataset):
        raise ValueError("Final snapshot is incomplete")
    ledger = read_jsonl(run / "measurement/llm_usage.jsonl")
    reused = [r for p in args.reused_setup_ledger for r in read_jsonl(p) if r["stage"] == "few_shot_index"]
    stage_runs = read_jsonl(run / "measurement/stage_runs.jsonl")
    recovery_path = run / "measurement/recovery.json"
    recovery = json.loads(recovery_path.read_text(encoding="utf-8")) if recovery_path.exists() else None
    config = redact_config(tomllib.loads((run / "config.toml").read_text(encoding="utf-8")))
    by_item = defaultdict(list)
    for record in ledger:
        if record.get("item_id") is not None:
            by_item[str(record["item_id"])].append(record)
    rows = []
    for item in dataset:
        pred = execute_sql_without_cache(item.database_path, item.final_selected_sql, timeout=60)
        gold = execute_sql_without_cache(item.database_path, item.gold_sql, timeout=60)
        correct, multiset = compare_execution(pred, gold)
        usage = by_item[item.get_item_id()]
        chat = aggregate_usage([r for r in usage if r.get("kind", "chat") == "chat"])
        embedding = aggregate_usage([r for r in usage if r.get("kind") == "embedding"])
        prep = (item.few_shot_preparation_metadata or {}).get("wall_seconds", 0)
        seconds = {s: getattr(item, s + "_time") or 0 for s in PIPELINE_STAGES}
        rows.append({
            "question_id": item.question_id, "database_id": item.database_id,
            "difficulty": item.difficulty, "question": item.question, "evidence": item.evidence,
            "predicted_sql": item.final_selected_sql, "gold_sql": item.gold_sql,
            "execution_correct": correct, "multiset_match": multiset,
            "prediction_status": pred.result_type, "gold_status": gold.result_type,
            "prediction_error": pred.error_message if pred.result_rows is None else None,
            "gold_error": gold.error_message if gold.result_rows is None else None,
            "returned_rows": len(pred.result_rows) if pred.result_rows is not None else None,
            "prediction_preview": pred.result_rows[:5] if pred.result_rows is not None else None,
            "gold_preview": gold.result_rows[:5] if gold.result_rows is not None else None,
            "pipeline_seconds": sum(seconds.values()), "few_shot_seconds": prep,
            "query_stage_seconds": sum(seconds.values()) + prep,
            "stage_seconds": seconds, "chat_usage": chat, "embedding_usage": embedding,
        })
    valid = [r for r in rows if r["execution_correct"] is not None]
    correct = sum(r["execution_correct"] for r in valid)
    strict_correct = sum(r["multiset_match"] is True for r in valid)
    revision_log = (run / "sql_revision.log").read_text(encoding="utf-8", errors="replace") if (run / "sql_revision.log").exists() else ""
    selection_log = (run / "sql_selection.log").read_text(encoding="utf-8", errors="replace") if (run / "sql_selection.log").exists() else ""
    quota_failures = {
        "sql_revision": revision_log.count("Free quota exhausted"),
        "sql_selection": selection_log.count("Free quota exhausted"),
    }
    zero_result_groups = {
        "sql_revision": len(re.findall(r"Only got 0/", revision_log)),
        "sql_selection": len(re.findall(r"Only got 0/", selection_log)),
    }
    failed_stage_attempts = [r for r in stage_runs if r.get("status") == "failed"]
    query_records = [r for r in ledger if r["stage"] not in {"value_index", "few_shot_index"}]
    setup_records = [r for r in ledger if r["stage"] in {"value_index", "few_shot_index"}] + reused
    grouped = defaultdict(list)
    for record in ledger + reused:
        grouped[(record["stage"], record.get("kind", "chat"))].append(record)
    latencies = sorted(r["query_stage_seconds"] for r in rows)
    def usage_for(records, kind):
        return aggregate_usage([r for r in records if r.get("kind", "chat") == kind])
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sample_count": len(rows), "valid_gold_count": len(valid), "correct_count": correct,
        "question_ids": [row["question_id"] for row in rows],
        "execution_accuracy": correct / len(valid) if valid else None,
        "strict_multiset_accuracy": strict_correct / len(valid) if valid else None,
        "fixed_denominator_accuracy": correct / len(rows),
        "successful_executions": sum(r["prediction_status"] in {"success", "empty_result", "all_null_result"} for r in rows),
        "mean_query_stage_seconds": statistics.mean(latencies),
        "p95_query_stage_seconds_nearest_rank": latencies[max(0, math.ceil(.95 * len(latencies)) - 1)],
        "online_chat_usage": usage_for(query_records, "chat"),
        "online_embedding_usage": usage_for(query_records, "embedding"),
        "offline_chat_usage": usage_for(setup_records, "chat"),
        "offline_embedding_usage": usage_for(setup_records, "embedding"),
        "unattributed_online_responses": sum(r.get("item_id") is None for r in query_records),
        "measured_query_wall_seconds": sum(r["wall_seconds"] for r in stage_runs if r["stage"] not in {"value_index", "few_shot_index"}),
        "measured_setup_wall_seconds": sum(r["wall_seconds"] for r in stage_runs if r["stage"] in {"value_index", "few_shot_index"}),
        "stage_runs": stage_runs,
        "degraded_by_provider_quota": any(quota_failures.values()),
        "failed_stage_attempts": failed_stage_attempts,
        "recovery": recovery,
        "quota_failure_attempts": quota_failures,
        "zero_result_llm_groups": zero_result_groups,
        "stage_usage": [{"stage": s, "kind": k, **aggregate_usage(v)} for (s,k),v in sorted(grouped.items())],
        "database_sha256": {item.database_id: hashlib.sha256(Path(item.database_path).read_bytes()).hexdigest() for item in dataset},
        "config": config,
    }
    train_source = Path(config["dataset"]["root_path"]) / "train/source.json"
    if train_source.exists():
        summary["training_labels_source"] = json.loads(train_source.read_text(encoding="utf-8"))
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "questions.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output / "questions.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        fields = ["question_id", "database_id", "execution_correct", "query_stage_seconds", "prompt_tokens", "completion_tokens", "total_tokens", "embedding_tokens", "predicted_sql", "gold_sql"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**{k: row[k] for k in fields if k in row},
                             **{k: row["chat_usage"][k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
                             "embedding_tokens": row["embedding_usage"]["total_tokens"]})
    default_profile = config.get("run", {}).get("default_llm_profile")
    model_name = (config.get("llm_profiles", {}).get(default_profile, {}) or config.get("llm", {})).get("model", "unknown")
    embedding_name = config.get("embedding", {}).get("embedding_model_name_or_path", "unknown")
    report = ["# BIRD-dev 10 题基线", "",
        f"- 项目集合式 EX：**{correct}/{len(valid)}（{correct / len(valid):.1%}）**；金标执行失败 {len(rows)-len(valid)} 题。" if valid else "- 无可评测金标。",
        f"- 严格多重集合准确率：**{strict_correct}/{len(valid)}（{strict_correct / len(valid):.1%}）**。",
        f"- SQL 可执行：{summary['successful_executions']}/{len(rows)}。",
        f"- 平均每题阶段耗时：{summary['mean_query_stage_seconds']:.2f} 秒；P95：{summary['p95_query_stage_seconds_nearest_rank']:.2f} 秒。",
        f"- 整批查询实测墙钟时间：{summary['measured_query_wall_seconds']:.2f} 秒。",
        f"- 在线生成 token：{summary['online_chat_usage']['total_tokens']:,}（输入 {summary['online_chat_usage']['prompt_tokens']:,}，输出 {summary['online_chat_usage']['completion_tokens']:,}）。",
        f"- 在线向量 token：{summary['online_embedding_usage']['total_tokens']:,}。",
        f"- 离线建索引：生成 token {summary['offline_chat_usage']['total_tokens']:,}；向量 token {summary['offline_embedding_usage']['total_tokens']:,}。", "",
        "## 有效性说明", ""]
    if any(quota_failures.values()) or any(zero_result_groups.values()):
        report += [
            f"- **本次评测存在不完整的模型阶段。** SQL 修复阶段出现 {quota_failures['sql_revision']} 次配额错误，最终选择阶段出现 {quota_failures['sql_selection']} 次配额错误。",
            f"- 修复阶段有 {zero_result_groups['sql_revision']} 组检查没有得到 LLM 结果；选择阶段有 {zero_result_groups['sql_selection']} 组比较没有得到有效投票。",
            "- 该成绩适合工程链路检查，不能标记为完整 DeepEye-SQL 基线。", ""]
    else:
        report += [
            "- SQL 修订与最终选择均完成，未发现配额错误或零有效投票组。",
            (
                f"- 选择阶段有 {recovery['interrupted_attempts']} 次人工中断并从增量检查点恢复；"
                f"中断请求已返回 {recovery['wasted_response_tokens']:,} token，API 响应耗时合计 "
                f"{recovery['wasted_api_elapsed_seconds']:.2f} 秒，均保留在消耗记录中。"
                if recovery else
                (f"- 运行中有 {len(failed_stage_attempts)} 次可恢复的阶段尝试中断；最终结果从增量检查点恢复，相关已返回 token 仍计入总消耗。" if failed_stage_attempts else "- 所有阶段均在首次尝试中完成。")
            ),
            "- 这是前 10 题的小样本工程基线，完整性通过不代表可外推到完整 BIRD-dev。", ""]
    report += [
        "## 逐题结果", "", "| 题号 | EX | 阶段耗时（秒） | 生成 token | 向量 token |", "|---|---|---:|---:|---:|"]
    for row in rows:
        report.append(f"| {row['question_id']} | {row['execution_correct']} | {row['query_stage_seconds']:.2f} | {row['chat_usage']['total_tokens']:,} | {row['embedding_usage']['total_tokens']:,} |")
    report += ["", "## 统计口径与限制", "",
        "- 样本是现有快照中的 BIRD-dev 前 10 题，均来自 california_schools；这是工程冒烟基线，不能外推为完整 BIRD-dev 分数。",
        f"- 当前修订/选择模型为 {model_name}，前置生成沿用已有 qwen3.8-max 快照，向量模型为 {embedding_name}；使用 100 条训练样本构建 few-shot 索引，每题检索 7 条。采样预算保留原配置。",
        "- EX 沿用项目评测器：预测 SQL 与金标执行结果的行集合相等；不检查行顺序和重复次数。questions.json 另列多重集合匹配结果。空结果也正常比较。",
        "- 每题耗时为各阶段处理时间之和，包含 few-shot 准备及阶段内等待；不包含进程启动、离线建库和排队进入首个 worker 的时间。它不是 HTTP 服务端到端延迟。并行运行下，逐题耗时之和不等于整批墙钟时间。",
        "- 整批查询墙钟时间只汇总成功落盘的阶段记录；若存在 recovery 记录，中断请求的 API 耗时单列，不混入该墙钟值。",
        "- token 来自逐次 API 响应 usage，覆盖掩码、初步 SQL、解析重试及主流程；生成与向量分别计数。没有返回 usage 的失败请求无法计量，不能等同于账单。连接探测请求不计入基线。",
        "- 403 配额失败没有成功响应，因此不在响应数和 token 中；相关失败次数来自阶段日志。",
        "- 离线生成用量包含本次先完成、后复用的 100 条训练掩码；准备时间仅统计当前云端流程，未包含此前本地 CPU 尝试和数据下载。",
        "- 训练索引只使用 train 标签；dev 金标只用于执行评测及原有 schema recall 诊断，不进入生成提示词。",
        "- 详细配置（密钥已移除）、数据文件哈希和各阶段用量见 summary.json；逐题 SQL、结果预览见 questions.json / questions.csv。", ""]
    (output / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("sample_count", "correct_count", "execution_accuracy", "online_chat_usage", "online_embedding_usage")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
