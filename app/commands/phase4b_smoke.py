"""``enterprise-agent phase4b-smoke`` — AdventureWorks 在线端到端 Smoke Test。

这个命令有**两种模式**，默认是零在线请求的那种：

* 默认（不加 ``--execute``）：只做离线准备 —— 校验题目、数据库、部分值索引、
  few-shot 索引、知识产物，并从**真实配置**里算出本轮最大 LLM 请求数/Token 上界，
  写进 ``workspace/evals/phase4b/budget_plan.json``。它不会创建 LLM 客户端、不会
  发任何网络请求。
* ``--execute --approve-online``：真的跑 Agent。必须同时给 ``--max-tokens`` 和
  ``--max-seconds``，这两个值直接变成 ``SQLSkill`` 的硬熔断上限，所以超预算时是
  流水线自己中止（``BudgetExceededError``），而不是「跑完再检查」。

必须成对出现的原因很直白：规格要求在线调用先获批具体预算，一个开关太容易被
误开，因此离线规划和在线执行是两道门。

对照组只切一个变量：``ENABLE_KNOWLEDGE_ENHANCEMENT``。模型、模型参数、数据库、
值索引、few-shot、题单、预算、执行规则全部相同。

证据采集的硬要求（对应规格第五节）：

* ``sql`` 只取 ``AgentResult.sql`` —— 即模型真实生成的 SQL；``reference_sql`` 只
  用来离线算参考结果，从不作为 Agent 的输入。
* ``evidence_in_prompt`` 从 SQL Skill 落盘的 ``dataset.snapshot/items.jsonl`` 里读，
  证明 GraphRAG 的 hint 真的进了流水线入参，而不是只在 enhancement 里自转一圈。
* ``value_index_status`` 从同一个 artifact 目录读 ``value_index_status.json``。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import statistics
import time
from typing import Any

from .common import resolve_config

DEFAULT_CASES = "evals/adventureworks-graphrag/dev.json"
DEFAULT_CASES_TEST = "evals/adventureworks-graphrag/test.json"
DEFAULT_CASE_ID = "AW-DEV-001"
DEFAULT_OUTPUT_DIR = "workspace/evals/phase4b"
BANDS = ("baseline", "enhanced")
# 收尾规格授权的单 run 调用数上限（final-eval 默认值）。
DEFAULT_MAX_CALLS_PER_RUN = 50

# Mirrors app/agent/graph.py MAX_TOOL_ROUNDS. Planner turns are pure chat calls.
PLANNER_ROUNDS = 6
# app/pipeline/sql_revision/sql_revision.py 的默认 checker 列表长度。
DEFAULT_CHECKERS = 8


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="config/local/adventureworks-value-index.toml")
    parser.add_argument("--cases", default=DEFAULT_CASES)
    parser.add_argument("--case-id", default=DEFAULT_CASE_ID,
                        help="dev 集里的题目 ID；不接受封存 test 集")
    parser.add_argument("--bands", default="baseline,enhanced",
                        help="baseline,enhanced 的任意子集")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--execute", action="store_true",
                        help="真的调用在线模型；必须与 --approve-online 同时使用")
    parser.add_argument("--approve-online", action="store_true",
                        help="确认本轮在线预算已被批准")
    parser.add_argument("--max-tokens", type=int, default=0,
                        help="单次 Agent 运行的 Token 硬上限（转成 SQLSkill 熔断值）")
    parser.add_argument("--max-seconds", type=float, default=0.0,
                        help="单次 Agent 运行的秒级硬上限（转成 SQLSkill 熔断值）")
    parser.add_argument("--max-llm-calls", type=int, default=0,
                        help="本轮所有运行的模型调用总数上限；超限立即停止后续运行")
    parser.add_argument("--dense-backend", default="local",
                        help="知识增强的向量通道：local（本地 Qwen3）/ 路径 / none（纯关键词）。"
                             "两个对照配置取值相同，保证唯一变量是知识增强开关本身")
    parser.add_argument("--dense-compare", action="store_true",
                        help="离线对比知识增强的关键词通道与本地向量通道，写 dense_channel.json；"
                             "只做检索，不调用任何 LLM")
    parser.add_argument("--rel-tol", type=float, default=1e-6,
                        help="数值相对容差")
    parser.add_argument("--abs-tol", type=float, default=1e-2,
                        help="数值绝对容差；默认 0.01 覆盖 ROUND(x,2) 与未取整显示差异")
    parser.add_argument("--quiet", action="store_true")


def _absolute(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


# --------------------------------------------------------------------------- #
# 离线准备：题单、预算、前置条件
# --------------------------------------------------------------------------- #
def load_case(cases_path: Path, case_id: str) -> dict[str, Any]:
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    split = payload.get("split", "")
    if split != "dev":
        raise ValueError(f"Only the dev split may be used; got split={split!r} from {cases_path}")
    for case in payload["cases"]:
        if case["id"] == case_id:
            return case
    raise ValueError(f"case_id={case_id} not found in {cases_path}")


def budget_plan(cfg) -> dict[str, Any]:
    """Derive the worst-case LLM request count from the *effective* config.

    Every number below comes from a real config field or a real code constant,
    and the mapping is written out so a reviewer can re-check it. Nothing here
    is a guess about the model's behaviour: it is the upper bound implied by
    the sampling budgets when every stage takes its maximum.
    """
    linking = cfg.schema_linking_config
    generation = cfg.sql_generation_config
    revision = cfg.sql_revision_config
    selection = cfg.sql_selection_config

    linking_calls = linking.direct_linking_sampling_budget + linking.reversed_linking_sampling_budget
    candidates = (generation.dc_sampling_budget + generation.skeleton_sampling_budget
                  + generation.icl_sampling_budget)
    checkers = len(revision.checkers) if getattr(revision, "checkers", None) else DEFAULT_CHECKERS
    checker_budget = revision.checker_sampling_budget
    # Every candidate SQL goes through every checker, sequentially.
    revision_calls = candidates * checkers * checker_budget
    pairs = math.comb(selection.filter_top_k_sql, 2) if selection.filter_top_k_sql >= 2 else 0
    selection_calls = pairs * selection.evaluator_sampling_budget
    few_shot_calls = 2 if cfg.few_shot_index_config.preliminary_sql.enabled else 1
    planner_calls = PLANNER_ROUNDS + 1
    # value_retrieval 阶段的 LLMExtractor 关键词抽取（n=1，最多 extractor max_retry 次
    # 重试都走同一 meter，这里按 1 次成功调用计入最坏情况）。
    value_retrieval_calls = 1

    per_run = {
        "planner": planner_calls,
        "few_shot_preparation": few_shot_calls,
        "value_retrieval_keyword_extraction": value_retrieval_calls,
        "schema_linking": linking_calls,
        "sql_generation": candidates,
        "sql_revision_worst_case": revision_calls,
        "sql_selection": selection_calls,
    }
    per_run_total = sum(per_run.values())
    return {
        "derivation": {
            "planner": f"app/agent/graph.py MAX_TOOL_ROUNDS={PLANNER_ROUNDS} + 1 final answer",
            "few_shot_preparation": "preliminary_sql generator + prepare_few_shot_examples_for_item",
            "value_retrieval_keyword_extraction": "LLMExtractor.extract_with_retry(n=1), app/pipeline/value_retrieval/utils.py",
            "schema_linking": "direct_linking_sampling_budget + reversed_linking_sampling_budget",
            "sql_generation": "dc + skeleton + icl sampling budgets = candidate SQL count",
            "sql_revision_worst_case": f"candidates × {checkers} checkers × checker_sampling_budget",
            "sql_selection": f"C(filter_top_k_sql={selection.filter_top_k_sql},2) × evaluator_sampling_budget",
        },
        "per_run_worst_case_llm_requests": per_run,
        "per_run_worst_case_total": per_run_total,
        "config_snapshot": {
            "model": cfg.sql_generation_config.llm.model if cfg.sql_generation_config.llm else None,
            "max_tokens": cfg.sql_generation_config.llm.max_tokens if cfg.sql_generation_config.llm else None,
            "parallelism": cfg.run_config.parallelism,
            "llm_timeout": cfg.run_config.llm_timeout,
            "sampling_budgets": {
                "direct_linking": linking.direct_linking_sampling_budget,
                "reversed_linking": linking.reversed_linking_sampling_budget,
                "dc": generation.dc_sampling_budget,
                "skeleton": generation.skeleton_sampling_budget,
                "icl": generation.icl_sampling_budget,
                "checker": checker_budget,
                "evaluator": selection.evaluator_sampling_budget,
                "filter_top_k_sql": selection.filter_top_k_sql,
            },
        },
    }


def preconditions(root: Path, cfg, case: dict[str, Any], database_id: str) -> list[dict[str, Any]]:
    import hashlib

    from app.vector_db.local_index import get_local_index_path, verify_runtime_index

    checks: list[dict[str, Any]] = []

    db_path = root / "data" / "adventureworks" / "adventureworks.sqlite"
    if not db_path.is_file():
        for candidate in (root / "data").rglob(f"{database_id}.sqlite"):
            db_path = candidate
            break
    checks.append({"check": "database_file", "ok": db_path.is_file(), "path": str(db_path)})

    index_dir = get_local_index_path(
        Path(cfg.vector_database_config.store_root_path) / database_id)
    # 这里刻意走**运行时**校验函数而不是只看 manifest 是否存在：它同时检查
    # embedding 签名、manifest 格式版本与数据库指纹，是 SQLSkill 真正会用的那道门。
    status = verify_runtime_index(
        index_dir,
        embedding_config=cfg.vector_database_config,
        database_fingerprint=(hashlib.sha256(db_path.read_bytes()).hexdigest()
                              if db_path.is_file() else None),
    )
    checks.append({
        "check": "value_index",
        "ok": status["status"] in {"complete", "partial"},
        "status": status["status"],
        "reasons": status.get("reasons"),
        "covered_tables": status.get("covered_tables"),
        "covered_columns": status.get("covered_columns"),
        "embedding_model": status.get("embedding_model"),
        "embedding_dim": status.get("embedding_dim"),
        "signature_source": status.get("signature_source"),
        "path": str(index_dir),
    })

    few_shot = Path(cfg.few_shot_index_config.save_path) / "manifest.json"
    checks.append({"check": "few_shot_index", "ok": few_shot.is_file(), "path": str(few_shot)})

    knowledge = root / "workspace" / "knowledge" / database_id
    required = ["relations.json", "snapshot.json", f"schema_{database_id}.json"]
    missing = [name for name in required if not (knowledge / name).is_file()]
    checks.append({"check": "knowledge_artifacts", "ok": not missing,
                   "missing": missing, "path": str(knowledge)})

    checks.append({"check": "case_is_dev", "ok": case.get("split") == "dev",
                   "case_id": case.get("id")})
    checks.append({"check": "reference_sql_verified",
                   "ok": bool(case.get("reference_sql_verified")),
                   "note": "参考 SQL 需先验证过，才能用它算参考结果"})
    return checks


def reference_rows(root: Path, case: dict[str, Any]) -> list[list[Any]]:
    """Execute the reference SQL read-only to obtain the gold result rows."""
    from app.skills.sql_skill import execute_readonly_table

    db_path = root / "data" / "adventureworks" / "adventureworks.sqlite"
    columns, rows, _ = execute_readonly_table(db_path, case["reference_sql"], 1000, 30)
    return [list(row) for row in rows]


# --------------------------------------------------------------------------- #
# 结果比较（显式协议，不把「SQL 执行成功」等同于「业务正确」）
# --------------------------------------------------------------------------- #
def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _cell_equal(actual: Any, expected: Any, *, rel: float, absolute: float) -> bool:
    if actual is None and expected is None:
        return True
    left, right = _as_number(actual), _as_number(expected)
    if left is not None and right is not None:
        return abs(left - right) <= max(absolute, rel * max(abs(left), abs(right)))
    return str(actual).strip().lower() == str(expected).strip().lower()


def _rows_equal(actual: list[Any], expected: list[Any], *, rel: float, absolute: float) -> bool:
    if len(actual) != len(expected):
        return False
    return all(_cell_equal(a, e, rel=rel, absolute=absolute) for a, e in zip(actual, expected))


def compare_results(actual_rows: list[list[Any]], expected_rows: list[list[Any]], *,
                    rel: float, absolute: float) -> dict[str, Any]:
    ordered = (len(actual_rows) == len(expected_rows)
               and all(_rows_equal(a, e, rel=rel, absolute=absolute)
                       for a, e in zip(actual_rows, expected_rows)))

    def key(row: list[Any]) -> tuple:
        return tuple(round(_as_number(cell), 6) if _as_number(cell) is not None
                     else str(cell).strip().lower() for cell in row)

    unordered = sorted(map(key, actual_rows)) == sorted(map(key, expected_rows))
    return {
        "row_count_match": len(actual_rows) == len(expected_rows),
        "ordered_match": ordered,
        "unordered_match": unordered,
        "actual_rows": len(actual_rows),
        "expected_rows": len(expected_rows),
    }


# --------------------------------------------------------------------------- #
# 在线执行
# --------------------------------------------------------------------------- #
def _skill_artifacts(workspace: Path, since: float) -> Path | None:
    """Newest SQL Skill request directory created after ``since``."""
    if not workspace.is_dir():
        return None
    fresh = [path for path in workspace.iterdir()
             if path.is_dir() and path.stat().st_mtime >= since]
    return max(fresh, key=lambda path: path.stat().st_mtime) if fresh else None


def _read_evidence(artifact: Path | None) -> dict[str, Any]:
    if artifact is None:
        return {"present": False}
    items = artifact / "dataset.snapshot.data" / "items.jsonl"
    if not items.is_file():
        # 旧版产物把 items.jsonl 直接放在 dataset.snapshot/ 下，兼容读取。
        items = artifact / "dataset.snapshot" / "items.jsonl"
    evidence = ""
    if items.is_file():
        first = items.read_text(encoding="utf-8").splitlines()[:1]
        if first:
            payload = json.loads(first[0])
            # items.jsonl 的真实结构是 {"input": {...DataItem 字段...}}；
            # evidence 在 input 层，不在顶层。为了同时兼容被拍平的历史产物，
            # 先查 input.evidence 再回落顶层。
            item = payload.get("input") if isinstance(payload.get("input"), dict) else payload
            evidence = str(item.get("evidence") or "")
    status_file = artifact / "value_index_status.json"
    status = json.loads(status_file.read_text(encoding="utf-8")) if status_file.is_file() else None
    # GraphRAG hint 的稳定锚点：知识增强 evidence 的固定标题行。方言 hint
    # （baseline 也有）不包含它，因此这个标记能区分「知识证据真的注入」。
    knowledge_marker = "[企业知识增强 Schema 上下文"
    knowledge_chars = 0
    if knowledge_marker in evidence:
        knowledge_chars = len(evidence) - evidence.index(knowledge_marker)
    return {
        "present": bool(evidence),
        "chars": len(evidence),
        "knowledge_marker_present": knowledge_marker in evidence,
        "knowledge_chars": knowledge_chars,
        "head": evidence[:600],
        "contains_graphrag_terms": any(
            token in evidence for token in
            ("关联歧义", "知识来源", "候选表", "必要中间表", "复合外键", "聚合粒度")),
        "value_index_status": status,
    }


def run_band(*, root: Path, cfg, case: dict[str, Any], band: str, database_id: str,
             max_tokens: int, max_seconds: float, dense_backend: str = "local",
             max_llm_calls: int | None = None) -> dict[str, Any]:
    """Run exactly one Agent request for one band. Online calls happen here only."""
    from app.agent import DataAnalysisAgent
    from app.enterprise import create_enterprise_knowledge
    from app.skills import SQLSkill
    from app.web.registry import DatabaseRegistry

    os.environ["ENABLE_ADVENTUREWORKS"] = "1"
    os.environ["ENABLE_KNOWLEDGE_ENHANCEMENT"] = "1" if band == "enhanced" else "0"
    # 两个 band 取值相同：唯一变量必须只是知识增强开关。
    if band == "enhanced":
        # 规格显式要求 hint 模式（当前唯一支持值，默认也是它，这里写死以留痕）。
        os.environ["KNOWLEDGE_ENHANCEMENT_MODE"] = "hint"
    if dense_backend and dense_backend.lower() != "none":
        os.environ["KNOWLEDGE_ENHANCEMENT_DENSE"] = dense_backend
    else:
        os.environ.pop("KNOWLEDGE_ENHANCEMENT_DENSE", None)

    registry = DatabaseRegistry(root)
    skill_workspace = root / "workspace" / "skill_requests"
    skill = SQLSkill(cfg, registry.database_paths, workspace=skill_workspace,
                     max_total_tokens=max_tokens, max_seconds=max_seconds,
                     max_llm_calls=max_llm_calls)
    agent = DataAnalysisAgent(
        skill, workspace=root / "workspace" / "agent_requests",
        databases=registry.database_paths,
        llm_config=cfg.sql_generation_config.llm,
        knowledge=create_enterprise_knowledge(cfg.app_config),
    )

    stages: list[dict[str, Any]] = []

    def on_stage(event: dict[str, Any]) -> None:
        if event.get("stage") in {"knowledge_enhancement", "meter"}:
            stages.append(event)

    started_wall = time.time()
    started = time.perf_counter()
    try:
        result = agent.run({"question": case["question"], "database_id": database_id},
                           on_stage=on_stage)
        payload = result.model_dump(exclude={"artifacts_dir"})
        sql, rows, answer = result.sql, result.rows, result.answer
        status, failed_stage, error = result.status, result.failed_stage, result.error
        model_calls, usage = result.model_calls, result.usage
        elapsed = result.agent_elapsed_seconds or result.elapsed_seconds
        tool_trace = result.tool_trace
    except Exception as exc:  # Agent 在 SQL Skill 之前就失败，也是完整链路失败
        payload = {}
        sql, rows, answer = None, [], ""
        status, failed_stage, error = "error", "agent_entry", f"{type(exc).__name__}: {exc}"
        model_calls, usage = 0, {}
        elapsed = time.perf_counter() - started
        tool_trace = []

    artifact = _skill_artifacts(skill_workspace, started_wall)
    evidence = _read_evidence(artifact)
    enhancement = next((item for item in stages if item.get("stage") == "knowledge_enhancement"), None)

    return {
        "band": band,
        "knowledge_enhancement_enabled": band == "enhanced",
        "status": status,
        "failed_stage": failed_stage,
        "error": error,
        "sql": sql,
        "rows": rows,
        "answer": answer,
        "model_calls": model_calls,
        "usage": usage,
        "elapsed_seconds": round(elapsed, 3),
        "tool_trace": tool_trace,
        "skill_artifacts_dir": str(artifact) if artifact else None,
        "evidence_in_prompt": evidence,
        "knowledge_enhancement_event": (
            {key: enhancement.get(key) for key in ("enabled", "error", "database_id")}
            | {"observability_keys": sorted((enhancement.get("observability") or {}).keys())}
            if enhancement else None),
        "result_payload_keys": sorted(payload.keys()),
    }


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def compare_retrieval_channels(case: dict[str, Any], *, dense_backend: str,
                               database_id: str) -> dict[str, Any]:
    """Keyword vs local-vector retrieval for one question. Retrieval only, no LLM.

    This exists because "GraphRAG 是否真的用上向量" must be a reproducible artifact,
    not a paragraph in a report: same question, same knowledge artifacts, one
    variable (``dense_backend``).
    """
    import time

    from app.knowledge.enhancement import build_enhancement, dense_backend_from_env

    os.environ["ENABLE_KNOWLEDGE_ENHANCEMENT"] = "1"
    os.environ["KNOWLEDGE_ENHANCEMENT_DENSE"] = dense_backend or "local"

    runs: dict[str, Any] = {}
    loaded = dense_backend_from_env()
    for label, mode, backend in (("keyword", "keyword", None),
                                 ("hybrid", "hybrid", loaded),
                                 ("hybrid_warm", "hybrid", loaded)):
        started = time.perf_counter()
        result = build_enhancement(case["question"], database_id, mode=mode,
                                   dense_backend=backend)
        elapsed = time.perf_counter() - started
        observability = result.observability
        context = observability.get("final_schema_context") or {}
        runs[label] = {
            "retrieval_mode": observability.get("retrieval_mode"),
            "dense_backend": observability.get("dense_backend"),
            "elapsed_seconds": round(elapsed, 3),
            "evidence_chars": len(result.evidence_text),
            "context_tokens": observability.get("context_tokens"),
            "candidate_tables": context.get("candidate_tables"),
            "candidate_columns": len(context.get("candidate_columns") or []),
            "necessary_intermediate_tables": observability.get(
                "necessary_intermediate_tables"),
            "join_paths": len(observability.get("candidate_join_paths") or []),
            "ambiguity_count": observability.get("ambiguity_count"),
            "enabled": result.enabled,
            "error": result.error,
        }
    return {
        "case_id": case["id"],
        "question": case["question"],
        "dense_backend": dense_backend,
        "note": ("hybrid_warm 是同一进程内第二次混合检索，用于区分「首次语料编码」"
                 "与「实际查询耗时」；keyword 不加载任何模型。"),
        "channels": runs,
    }


def run(args: argparse.Namespace) -> int:
    # 先做最便宜的判断：在线执行的两道门必须在加载配置、创建任何客户端之前生效。
    if args.execute:
        if not args.approve_online:
            raise ValueError("--execute requires --approve-online "
                             "(online budget must be approved first)")
        if args.max_tokens <= 0 or args.max_seconds <= 0:
            raise ValueError("--execute requires --max-tokens and --max-seconds as hard caps")

    resolve_config(args.config)  # 必须在 get_config() 之前，配置是惰性单例

    from app.config import PROJECT_ROOT, get_config
    root = PROJECT_ROOT
    cfg = get_config()

    bands = [band.strip() for band in args.bands.split(",") if band.strip()]
    unknown = [band for band in bands if band not in BANDS]
    if unknown:
        raise ValueError(f"Unknown band(s): {unknown}; expected a subset of {BANDS}")

    cases_path = _absolute(root, args.cases)
    case = load_case(cases_path, args.case_id)
    database_id = case.get("database_id", "adventureworks")

    plan = budget_plan(cfg)
    checks = preconditions(root, cfg, case, database_id)
    ready = all(check["ok"] for check in checks)

    output_dir = _absolute(root, args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    budget_file = output_dir / "budget_plan.json"
    budget_file.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": args.config,
        "case_id": case["id"],
        "question": case["question"],
        "bands": bands,
        "budget": plan,
        "preconditions": checks,
        "ready_for_online": ready,
    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.dense_compare:
        payload = compare_retrieval_channels(case, dense_backend=args.dense_backend,
                                             database_id=database_id)
        destination = output_dir / "dense_channel.json"
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        if not args.quiet:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(json.dumps({"case_id": payload["case_id"],
                              "channels": {name: {"elapsed_seconds": value["elapsed_seconds"],
                                                  "context_tokens": value["context_tokens"],
                                                  "ambiguity": value["ambiguity_count"]}
                                           for name, value in payload["channels"].items()}},
                             ensure_ascii=False, sort_keys=True))
        return 0

    if not args.execute:
        if not args.quiet:
            print(json.dumps({
                "mode": "offline_plan",
                "online_requests_made": 0,
                "case_id": case["id"],
                "bands": bands,
                "budget": plan,
                "preconditions": checks,
                "ready_for_online": ready,
                "next": ("Re-run with --execute --approve-online --max-tokens N --max-seconds S "
                         "after the online budget is approved."),
            }, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if ready else 1

    # 在线执行的门：两个开关 + 两个上限，缺一不可（已在函数入口先判过，这里
    # 再判一次是为了防止有人复用 run() 时绕过）。
    if not args.approve_online:
        raise ValueError("--execute requires --approve-online (online budget must be approved first)")
    if args.max_tokens <= 0 or args.max_seconds <= 0:
        raise ValueError("--execute requires --max-tokens and --max-seconds as hard caps")
    if not ready:
        raise ValueError("Preconditions failed; refusing to burn online budget "
                         f"(see {budget_file})")

    expected_rows = reference_rows(root, case)
    max_calls = args.max_llm_calls or plan["per_run_worst_case_total"] * len(bands)

    runs: list[dict[str, Any]] = []
    total_calls = 0
    for band in bands:
        if total_calls >= max_calls:
            runs.append({"band": band, "status": "skipped",
                         "error": f"global --max-llm-calls={max_calls} exhausted"})
            continue
        # Per-band hard cap: the meter enforces the *remaining* call budget
        # before every real HTTP request, so one runaway band cannot eat the
        # other band's allocation (the between-band check alone was not enough).
        remaining_calls = max_calls - total_calls
        record = run_band(root=root, cfg=cfg, case=case, band=band,
                          database_id=database_id,
                          max_tokens=args.max_tokens, max_seconds=args.max_seconds,
                          dense_backend=args.dense_backend,
                          max_llm_calls=remaining_calls)
        total_calls += int(record.get("model_calls") or 0)
        comparison = compare_results(record.get("rows") or [], expected_rows,
                                     rel=args.rel_tol, absolute=args.abs_tol)
        record.update({
            "run_id": f"{case['id']}-{band}-{int(time.time())}",
            "question_id": case["id"],
            "question": case["question"],
            "database_id": database_id,
            "llm_call_cap_for_band": remaining_calls,
            "reference_sql": case["reference_sql"],
            "reference_rows": expected_rows,
            "sql_executed": record.get("status") in {"success", "empty_result"},
            "result_correct": comparison["ordered_match"],
            "comparison": comparison,
        })
        runs.append(record)
        (output_dir / f"run_{band}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "case_id": case["id"],
        "question": case["question"],
        "config": args.config,
        "caps": {"max_tokens_per_run": args.max_tokens,
                 "max_seconds_per_run": args.max_seconds,
                 "max_llm_calls_total": max_calls},
        "controlled_variables": {
            "only_differs_by": "ENABLE_KNOWLEDGE_ENHANCEMENT",
            "dense_backend": args.dense_backend,
            "config": args.config,
        },
        "total_model_calls": total_calls,
        "errors": [{"band": run["band"], "stage": run.get("failed_stage"),
                    "error": run.get("error")} for run in runs if run.get("error")],
        "bands": {run["band"]: {
            "status": run.get("status"),
            "sql_executed": run.get("sql_executed"),
            "result_correct": run.get("result_correct"),
            "model_calls": run.get("model_calls"),
            "elapsed_seconds": run.get("elapsed_seconds"),
            "evidence_chars": (run.get("evidence_in_prompt") or {}).get("chars"),
            "value_index_status": ((run.get("evidence_in_prompt") or {})
                                   .get("value_index_status") or {}).get("status"),
            "usage": run.get("usage"),
        } for run in runs},
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(run.get("result_correct") for run in runs) else 1
