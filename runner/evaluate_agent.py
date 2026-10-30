"""Run and score the dedicated Text-to-SQL Agent evaluation set."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sqlite3
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ALLOWED_INTENTS = {"chat", "query", "ranking", "trend", "proportion"}


def validate_agent_cases(cases: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for index, case in enumerate(cases, start=1):
        missing = {key for key in ("id", "question", "database_id", "expected_intent") if not case.get(key)}
        if missing:
            raise ValueError(f"Case {index} is missing fields: {sorted(missing)}")
        if case["id"] in seen:
            raise ValueError(f"Duplicate case id: {case['id']}")
        seen.add(case["id"])
        if case["expected_intent"] not in ALLOWED_INTENTS:
            raise ValueError(f"Invalid expected_intent in {case['id']}: {case['expected_intent']}")


def score_agent_case(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    tools = [item.get("tool") for item in result.get("tool_trace", [])
             if item.get("status") == "completed"]
    required = set(case.get("required_tools", []))
    forbidden = set(case.get("forbidden_tools", []))
    checks = {
        "required_tools": required.issubset(tools),
        "forbidden_tools": not forbidden.intersection(tools),
        "intent": result.get("intent") == case.get("expected_intent"),
    }
    expected_chart = case.get("expected_chart_type")
    if expected_chart:
        checks["chart"] = (result.get("visualization") or {}).get("type") == expected_chart
    if case.get("expected_database_id"):
        checks["database"] = result.get("database_id") == case["expected_database_id"]
    if case.get("expected_rows") is not None:
        def normalized(rows):
            return sorted(
                tuple(round(value, 2) if isinstance(value, float) else value for value in row)
                for row in rows
            )
        checks["result_matches_database_truth"] = normalized(result.get("rows") or []) == normalized(case["expected_rows"])
    if "run_text_to_sql" in required:
        checks["sql_status"] = result.get("status") in {"success", "empty_result"}
        checks["sql_present"] = bool(result.get("sql"))
        sql = (result.get("sql") or "").casefold()
        if case.get("sql_must_contain"):
            checks["sql_semantics"] = all(
                fragment.casefold() in sql for fragment in case["sql_must_contain"])
    else:
        checks["request_status"] = result.get("status") == "success"
    return {"passed": all(checks.values()), "checks": checks, "tools": tools}


def _total_tokens(result: dict[str, Any]) -> int:
    total = int((result.get("agent_usage") or {}).get("total_tokens", 0) or 0)
    for usage in (result.get("usage") or {}).values():
        if isinstance(usage, dict):
            total += int(usage.get("total_tokens", 0) or 0)
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/local/demo-fast/config-bird-demo.toml")
    parser.add_argument("--cases", default="evals/agent/questions.json")
    parser.add_argument("--output", default="evals/agent/latest/summary.json")
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--validate-only", action="store_true",
                        help="Validate the case file without loading models or calling providers")
    parser.add_argument("--rerun-failures", action="store_true",
                        help="Reuse passed rows in the output report and rerun only failed case IDs")
    args = parser.parse_args()

    from app.config import PROJECT_ROOT, get_config

    cases_path = Path(args.cases)
    if not cases_path.is_absolute():
        cases_path = PROJECT_ROOT / cases_path
    cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    validate_agent_cases(cases)
    if args.validate_only:
        print(json.dumps({"valid": True, "case_count": len(cases), "path": str(cases_path)},
                         ensure_ascii=False, indent=2))
        return
    output = Path(args.output)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    previous = None
    if args.rerun_failures and output.is_file():
        previous = json.loads(output.read_text(encoding="utf-8"))
        failed_ids = {row["id"] for row in previous.get("cases", []) if not row.get("passed")}
        cases = [case for case in cases if case["id"] in failed_ids]
    if args.max_cases is not None:
        cases = cases[:max(0, args.max_cases)]

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    os.environ["CONFIG_PATH"] = str(config_path)

    from app.agent import DataAnalysisAgent
    from app.enterprise import create_enterprise_knowledge
    from app.web.registry import DatabaseRegistry
    from app.skills import SQLSkill

    registry = DatabaseRegistry(PROJECT_ROOT)
    runtime_config = get_config()
    model_name = runtime_config.sql_generation_config.llm.model
    skill = SQLSkill(runtime_config, registry.database_paths,
                     workspace=PROJECT_ROOT / "workspace/agent_eval_skill",
                     max_total_tokens=80_000, max_seconds=240)
    agent = DataAnalysisAgent(skill, workspace=PROJECT_ROOT / "workspace/agent_eval",
                              databases=registry.database_paths,
                              llm_config=runtime_config.sql_generation_config.llm,
                              knowledge=create_enterprise_knowledge(runtime_config.app_config))

    rows = []
    for case in cases:
        scored_case = dict(case)
        if case.get("truth_sql"):
            database_path = registry.database_paths[case.get("expected_database_id") or case["database_id"]]
            with sqlite3.connect(database_path) as connection:
                cursor = connection.execute(case["truth_sql"])
                scored_case["expected_rows"] = [list(row) for row in cursor.fetchall()]
        conversation = case.get("conversation_group") or case["id"]
        request = {
            "question": case["question"],
            "database_id": case["database_id"],
            "conversation_id": f"agent-eval:{conversation}",
            "user_hint": case.get("user_hint", ""),
        }
        try:
            result = agent.run(request).model_dump(exclude={"artifacts_dir"})
            score = score_agent_case(scored_case, result)
            rows.append({"id": case["id"], "question": case["question"], **score,
                         "intent": result.get("intent"), "status": result.get("status"),
                         "database_id": result.get("database_id"),
                         "chart_type": (result.get("visualization") or {}).get("type"),
                         "sql": result.get("sql"), "elapsed_seconds": result.get("agent_elapsed_seconds", 0),
                         "expected_rows": scored_case.get("expected_rows"),
                         "actual_rows": result.get("rows"),
                         "tokens": _total_tokens(result), "error": result.get("error")})
        except Exception as exc:
            rows.append({"id": case["id"], "question": case["question"], "passed": False,
                         "checks": {}, "tools": [], "status": "error",
                         "elapsed_seconds": 0, "tokens": 0,
                         "error": f"{type(exc).__name__}: evaluation case failed"})

    if previous is not None:
        replacements = {row["id"]: row for row in rows}
        rows = [replacements.get(row["id"], row) for row in previous.get("cases", [])]
    elapsed = [float(row.get("elapsed_seconds") or 0) for row in rows]
    summary = {
        "evaluation": "enterprise-agent-evaluation-v1",
        "evaluation_mode": "incremental_failure_rerun" if previous is not None else "full",
        "model": (f"mixed incremental report; latest rerun={model_name}"
                  if previous is not None else model_name),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(rows),
        "passed_count": sum(bool(row["passed"]) for row in rows),
        "pass_rate": (sum(bool(row["passed"]) for row in rows) / len(rows)) if rows else 0,
        "mean_seconds": statistics.mean(elapsed) if elapsed else 0,
        "total_tokens": sum(int(row.get("tokens") or 0) for row in rows),
        "cases": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "cases"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
