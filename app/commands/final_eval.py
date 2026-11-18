"""``enterprise-agent final-eval`` — Phase 4B 收尾：封存 test 集的最终在线对照。

与 ``phase4b-smoke`` 的分工（刻意不合并）：

* smoke 只接受 ``split="dev"`` 的题单；本命令**只接受 ``split="test"``**，
  两个入口互斥，避免封存测试集被当成调优开发集反复跑。
* 运行逻辑、结果比较协议、预算 meter 完全复用 ``phase4b_smoke`` 的
  ``run_band`` / ``compare_results`` / ``reference_rows`` / ``preconditions``，
  这里只做循环、累计预算核算与汇总落盘。

硬约束（对应收尾规格第三节）：

* ``--execute`` 必须与 ``--approve-online``、``--max-tokens``、``--max-seconds``
  同时出现，缺失直接拒绝，且在任何配置加载/客户端创建之前。
* 每次运行是独立 Agent 会话；参考 SQL 只用于评分，从不进入模型输入。
* 失败样本（超时、模型错误、SQL 错误）全部保留在汇总里，不做静默剔除。
* 单 run 调用数上限默认 50（规格授权值），累计调用数由 ``--max-llm-calls``
  在 meter 层强制；band 间剩余额度递减分配，防止单题吃光全局预算。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

from .common import resolve_config
from .phase4b_smoke import (
    BANDS,
    DEFAULT_CASES_TEST,
    DEFAULT_MAX_CALLS_PER_RUN,
    _absolute,
    budget_plan,
    compare_results,
    preconditions,
    reference_rows,
    run_band,
)

DEFAULT_OUTPUT_DIR = "workspace/evals/phase4b-final"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="config/local/adventureworks-smoke.toml")
    parser.add_argument("--cases", default=DEFAULT_CASES_TEST,
                        help="必须是 split=test 的题单（封存集正式解封）")
    parser.add_argument("--bands", default="baseline,enhanced",
                        help="baseline,enhanced 的任意子集")
    parser.add_argument("--case-ids", default="",
                        help="逗号分隔的题目 ID 子集；留空 = 全部 6 题")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--execute", action="store_true",
                        help="真的调用在线模型；必须与 --approve-online 同时使用")
    parser.add_argument("--approve-online", action="store_true",
                        help="确认本轮在线预算已被批准")
    parser.add_argument("--max-tokens", type=int, default=0,
                        help="单次 Agent 运行的 Token 硬上限")
    parser.add_argument("--max-seconds", type=float, default=0.0,
                        help="单次 Agent 运行的秒级硬上限")
    parser.add_argument("--max-llm-calls", type=int, default=0,
                        help="本轮所有运行的模型调用总数上限；meter 层强制")
    parser.add_argument("--max-calls-per-run", type=int, default=DEFAULT_MAX_CALLS_PER_RUN,
                        help=f"单次运行调用数上限（默认 {DEFAULT_MAX_CALLS_PER_RUN}，规格授权值）")
    parser.add_argument("--dense-backend", default="local")
    parser.add_argument("--rel-tol", type=float, default=1e-6)
    parser.add_argument("--abs-tol", type=float, default=1e-2)
    parser.add_argument("--quiet", action="store_true")


def load_test_cases(cases_path: Path, case_ids: list[str]) -> list[dict[str, Any]]:
    """Load the sealed test split. Refuses anything that is not split='test'."""
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    split = payload.get("split", "")
    if split != "test":
        raise ValueError(f"final-eval only runs the sealed test split; got split={split!r} "
                         f"from {cases_path} (use phase4b-smoke for dev)")
    cases = payload["cases"]
    if case_ids:
        wanted = set(case_ids)
        missing = wanted - {case["id"] for case in cases}
        if missing:
            raise ValueError(f"case_ids not found in {cases_path}: {sorted(missing)}")
        cases = [case for case in cases if case["id"] in wanted]
    return cases


def allocate_call_caps(case_count: int, band_count: int, *, total: int,
                       per_run: int) -> list[int]:
    """Per-run cap = min(per_run, remaining global budget), computed greedily.

    Pure arithmetic helper so the allocation policy is unit-testable offline.
    """
    caps: list[int] = []
    remaining = total
    for _ in range(case_count * band_count):
        caps.append(max(0, min(per_run, remaining)))
        remaining -= caps[-1]
    return caps


def run(args: argparse.Namespace) -> int:
    # 预算门槛最先判：不加载配置、不创建任何客户端。
    if args.execute:
        if not args.approve_online:
            raise ValueError("--execute requires --approve-online "
                             "(online budget must be approved first)")
        if args.max_tokens <= 0 or args.max_seconds <= 0:
            raise ValueError("--execute requires --max-tokens and --max-seconds as hard caps")

    resolve_config(args.config)

    from app.config import PROJECT_ROOT, get_config
    root = PROJECT_ROOT
    cfg = get_config()

    bands = [band.strip() for band in args.bands.split(",") if band.strip()]
    unknown = [band for band in bands if band not in BANDS]
    if unknown:
        raise ValueError(f"Unknown band(s): {unknown}; expected a subset of {BANDS}")

    cases_path = _absolute(root, args.cases)
    case_ids = [item.strip() for item in args.case_ids.split(",") if item.strip()]
    cases = load_test_cases(cases_path, case_ids)
    if not cases:
        raise ValueError("No test cases selected")

    plan = budget_plan(cfg)
    worst_total = plan["per_run_worst_case_total"]
    if args.execute:
        max_calls_total = args.max_llm_calls or worst_total * len(cases) * len(bands)
    else:
        max_calls_total = args.max_llm_calls or worst_total * len(cases) * len(bands)

    checks = preconditions(root, cfg, cases[0], cases[0].get("database_id", "adventureworks"))
    # preconditions 是与 smoke 共享的：其中的 case_is_dev 对本命令不适用——
    # 本命令的封存集守卫在 load_test_cases（只接受 split=test）里完成。
    for check in checks:
        if check["check"] == "case_is_dev":
            check.update({"check": "case_is_test",
                          "ok": cases[0].get("split") == "test",
                          "case_id": cases[0].get("id")})
    ready = all(check["ok"] for check in checks)

    output_dir = _absolute(root, args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    header = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "online_eval" if args.execute else "offline_plan",
        "online_requests_made": 0,
        "config": args.config,
        "cases_file": str(cases_path),
        "case_ids": [case["id"] for case in cases],
        "bands": bands,
        "worst_case_calls_per_run": worst_total,
        "worst_case_calls_total": worst_total * len(cases) * len(bands),
        "caps": {"max_tokens_per_run": args.max_tokens,
                 "max_seconds_per_run": args.max_seconds,
                 "max_llm_calls_total": max_calls_total,
                 "max_calls_per_run": args.max_calls_per_run},
        "preconditions": checks,
        "ready_for_online": ready,
    }
    (output_dir / "plan.json").write_text(
        json.dumps(header, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    if not args.execute:
        if not args.quiet:
            print(json.dumps(header, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if ready else 1

    if not ready:
        raise ValueError("Preconditions failed; refusing to burn online budget "
                         f"(see {output_dir / 'plan.json'})")

    caps = allocate_call_caps(len(cases), len(bands),
                              total=max_calls_total, per_run=args.max_calls_per_run)
    expected_rows_cache: dict[str, list[list[Any]]] = {}

    runs: list[dict[str, Any]] = []
    total_calls = 0
    total_tokens = 0
    for case in cases:
        if case["id"] not in expected_rows_cache:
            expected_rows_cache[case["id"]] = reference_rows(root, case)
        expected_rows = expected_rows_cache[case["id"]]
        for band in bands:
            cap = caps[len(runs)]
            if cap <= 0:
                runs.append({"case_id": case["id"], "band": band, "status": "skipped",
                             "error": f"global --max-llm-calls={max_calls_total} exhausted"})
                continue
            record = run_band(root=root, cfg=cfg, case=case, band=band,
                              database_id=case.get("database_id", "adventureworks"),
                              max_tokens=args.max_tokens, max_seconds=args.max_seconds,
                              dense_backend=args.dense_backend, max_llm_calls=cap)
            model_calls = int(record.get("model_calls") or 0)
            total_calls += model_calls
            usage = record.get("usage") or {}
            total_tokens += int(usage.get("total_tokens") or 0)
            comparison = compare_results(record.get("rows") or [], expected_rows,
                                         rel=args.rel_tol, absolute=args.abs_tol)
            record.update({
                "run_id": f"{case['id']}-{band}-{int(time.time())}",
                "case_id": case["id"],
                "question": case["question"],
                "category": case.get("category"),
                "difficulty": case.get("difficulty"),
                "database_id": case.get("database_id", "adventureworks"),
                "llm_call_cap_for_run": cap,
                "reference_sql": case["reference_sql"],
                "reference_rows": expected_rows,
                "sql_executed": record.get("status") in {"success", "empty_result"},
                "result_correct": comparison["ordered_match"],
                "comparison": comparison,
            })
            runs.append(record)
            (output_dir / f"run_{case['id']}_{band}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8")

    def _num(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cases_file": str(cases_path),
        "case_ids": [case["id"] for case in cases],
        "bands": bands,
        "caps": header["caps"],
        "totals": {"model_calls": total_calls, "total_tokens": total_tokens,
                   "runs": len([r for r in runs if r.get("status") != "skipped"])},
        "per_band": {band: {
            "runs": len([r for r in runs if r.get("band") == band
                         and r.get("status") != "skipped"]),
            "sql_executed": len([r for r in runs if r.get("band") == band
                                 and r.get("sql_executed")]),
            "result_correct": len([r for r in runs if r.get("band") == band
                                   and r.get("result_correct")]),
            "execution_accuracy": round(
                len([r for r in runs if r.get("band") == band and r.get("result_correct")])
                / max(1, len([r for r in runs if r.get("band") == band
                              and r.get("status") != "skipped"])), 4),
            "avg_tokens": _num([float((r.get("usage") or {}).get("total_tokens") or 0)
                                for r in runs if r.get("band") == band]),
            "avg_elapsed_seconds": _num([float(r.get("elapsed_seconds") or 0)
                                         for r in runs if r.get("band") == band]),
            "errors": [{"case_id": r.get("case_id"), "stage": r.get("failed_stage"),
                        "error": r.get("error")}
                       for r in runs if r.get("band") == band and r.get("error")],
        } for band in bands},
        "per_case": [{
            "case_id": case["id"],
            "question": case["question"],
            "category": case.get("category"),
            "bands": {band: {key: next(
                (r.get(key) for r in runs if r.get("case_id") == case["id"]
                 and r.get("band") == band), None)
                for key in ("status", "sql", "result_correct", "comparison",
                            "model_calls", "elapsed_seconds", "usage", "failed_stage",
                            "error", "evidence_in_prompt")}
                for band in bands},
        } for case in cases],
    }
    (output_dir / "summary_final.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    if not args.quiet:
        print(json.dumps(summary["totals"] | summary["per_band"],
                         ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(run.get("result_correct") for run in runs if run.get("status") != "skipped") else 1
