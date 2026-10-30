"""Compare keyword and hybrid enterprise-metric retrieval."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def validate_cases(cases: list[dict]) -> None:
    seen: set[str] = set()
    for case in cases:
        if not case.get("id") or not case.get("question") or not case.get("expected_metric_ids"):
            raise ValueError("Each metric RAG case needs id, question, and expected_metric_ids")
        if case["id"] in seen:
            raise ValueError(f"Duplicate case id: {case['id']}")
        seen.add(case["id"])


def score(knowledge, cases: list[dict], *, top_k: int) -> dict:
    rows = []
    for case in cases:
        started = time.perf_counter()
        result = knowledge.search(case["question"], limit=top_k)
        predicted = [item["id"] for item in result["metrics"]]
        expected = set(case["expected_metric_ids"])
        hits = expected.intersection(predicted)
        recall = len(hits) / len(expected)
        reciprocal_rank = max((1 / (predicted.index(item) + 1) for item in expected if item in predicted), default=0)
        rows.append({
            "id": case["id"], "question": case["question"], "expected": sorted(expected),
            "predicted": predicted, "recall_at_k": recall, "reciprocal_rank": reciprocal_rank,
            "elapsed_seconds": round(time.perf_counter() - started, 4),
            "retrieval": result["retrieval"],
        })
    return {
        "case_count": len(rows),
        "recall_at_k": statistics.mean(row["recall_at_k"] for row in rows) if rows else 0,
        "mrr": statistics.mean(row["reciprocal_rank"] for row in rows) if rows else 0,
        "mean_seconds": statistics.mean(row["elapsed_seconds"] for row in rows) if rows else 0,
        "cases": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/local/demo-fast/config-bird-demo.toml")
    parser.add_argument("--cases", default="evals/metric-rag/questions.json")
    parser.add_argument("--output", default="evals/metric-rag/latest/summary.json")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    from app.config import PROJECT_ROOT

    cases_path = Path(args.cases)
    if not cases_path.is_absolute():
        cases_path = PROJECT_ROOT / cases_path
    cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    validate_cases(cases)
    if args.validate_only:
        print(json.dumps({"valid": True, "case_count": len(cases)}, ensure_ascii=False, indent=2))
        return

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    os.environ["CONFIG_PATH"] = str(config_path.resolve())
    output = Path(args.output)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    ledger = output.with_name("usage.jsonl")
    if ledger.exists():
        ledger.unlink()
    os.environ["SQL_USAGE_LEDGER"] = str(ledger)
    os.environ["SQL_RUN_STAGE"] = "business_metric_rag"

    from app.config import get_config
    from app.enterprise import create_enterprise_knowledge

    app_config = get_config().app_config
    variants = {
        "keyword": score(create_enterprise_knowledge(app_config, mode="keyword"), cases, top_k=args.top_k),
        "hybrid": score(create_enterprise_knowledge(app_config, mode="hybrid"), cases, top_k=args.top_k),
    }
    usage_rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()] if ledger.exists() else []
    embedding_usage = {
        "responses": len(usage_rows),
        "total_tokens": sum(int(row.get("total_tokens") or 0) for row in usage_rows),
        "elapsed_seconds": round(sum(float(row.get("elapsed_seconds") or 0) for row in usage_rows), 4),
    }
    summary = {
        "evaluation": "enterprise-metric-rag-v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "top_k": args.top_k, "variants": variants, "hybrid_embedding_usage": embedding_usage,
        "delta": {
            "recall_at_k": variants["hybrid"]["recall_at_k"] - variants["keyword"]["recall_at_k"],
            "mrr": variants["hybrid"]["mrr"] - variants["keyword"]["mrr"],
        },
    }
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**{key: value for key, value in summary.items() if key != "variants"},
                      "variants": {name: {k: v for k, v in data.items() if k != "cases"}
                                   for name, data in variants.items()}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
