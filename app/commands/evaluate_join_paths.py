"""``enterprise-agent evaluate-join-paths`` — 公平的 Join 路径方法对照。

为什么需要这个命令：Phase 2/3 的 A/B/C 三个配置根本不输出 Join 路径，只有 D
输出，所以 0 → 0.9286 不能解释成「路径评分优于其他路径算法」，只能解释成
「不输出路径的方法没有路径可评」。这里把两个**都输出路径**的方法放在同一套
约束下比较：

* ``baseline_shortest`` —— 真实外键图上的无权最短路径，按跳数排序，不看完整性 /
  基数 / 关系角色 / 业务粒度。
* ``ours`` —— 现有 ``JoinPathFinder``，评分含完整性、基数、来源与粒度，歧义显式返回。

公平性由代码保证，而不是靠说明文字：

1. 相同的端点对：两者都调用 ``build_endpoint_pairs`` 生成，且 focus/targets 来自
   同一份**只由问题文本**驱动的检索结果；
2. 相同的图：都读 ``workspace/knowledge/adventureworks/relations.json`` 的同一
   ``table_projection``；
3. 相同预算：``--max-hops`` / ``--max-paths`` 同时作用于两者；
4. 相同的角色约定：两者对每跳都取 ``roles[0]``，所以「选错角色」是方法共性，
   不会被伪装成差距；
5. 相同的标准答案：``acceptable_join_paths`` 只在评分阶段读取，绝不进入检索器。

所有指标的分子分母都写进输出，N/A 记为 ``None`` 而不是 0。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
from typing import Any

import networkx as nx

from app.knowledge.retrieval.join_path import (
    JoinCondition,
    JoinPath,
    JoinPathFinder,
    build_endpoint_pairs,
)
from app.knowledge.schema_extractor import PROJECT_ROOT

DEFAULT_CASES = ("evals/adventureworks-graphrag/dev.json",
                 "evals/adventureworks-graphrag/dev-join.json")
DEFAULT_OUTPUT = "workspace/evals/phase4b/join_paths.json"
METHODS = ("baseline_shortest", "ours")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cases", nargs="+", default=list(DEFAULT_CASES),
                        help="一个或多个开发集 JSON；封存 test.json 一律拒绝")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--max-hops", type=int, default=4)
    parser.add_argument("--max-paths", type=int, default=8)
    parser.add_argument("--validate-only", action="store_true",
                        help="只校验题单：外键边是否真实存在、表名是否存在")
    parser.add_argument("--quiet", action="store_true")


def _absolute(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _short(name: str) -> str:
    return name.rsplit(".", 1)[-1] if "." in name else name


def make_node_resolver(known_tables: set[str], database_id: str):
    """Map ``Sales.SalesOrderDetail`` *and* ``SalesOrderDetail`` to graph nodes.

    The dev set writes join edges with bare table names while the graph keeps the
    full ``database.schema.table`` id (AW table names themselves contain a dot).
    Resolving both forms is a correctness requirement, not a convenience: without
    it every gold edge looks "not in schema" and the gold labels become noise.
    """
    by_tail: dict[str, str] = {}
    for node in known_tables:
        body = node.split(".", 1)[1] if node.startswith(f"{database_id}.") else node
        by_tail.setdefault(body.rsplit(".", 1)[-1], node)

    def resolve(name: str) -> str | None:
        if name in known_tables:
            return name
        prefixed = f"{database_id}.{name}"
        if prefixed in known_tables:
            return prefixed
        return by_tail.get(name.rsplit(".", 1)[-1])

    return resolve


def _pair_key(left_table: str, left_column: str,
              right_table: str, right_column: str) -> frozenset:
    """Orientation-free identity of one *column pair* of a join edge."""
    left = (_short(left_table), left_column)
    right = (_short(right_table), right_column)
    return frozenset([left, right])


def _path_pairs(path: JoinPath) -> set[frozenset]:
    """Expand a path into per-column pairs so composite FKs match gold labels.

    Gold labels in the dev set list each column of a composite FK separately
    (``Detail.SpecialOfferID -> SpecialOfferProduct.SpecialOfferID`` and
    ``Detail.ProductID -> ...ProductID``). Expanding the path the same way makes
    "composite key written as two lines" comparable to "one composite condition",
    instead of silently scoring it as a miss.
    """
    pairs: set[frozenset] = set()
    for condition in path.join_conditions:
        sources = condition.source_columns or []
        targets = condition.target_columns or []
        for left_column, right_column in zip(sources, targets):
            pairs.add(_pair_key(condition.left_table, left_column,
                                condition.right_table, right_column))
    return pairs


def _gold_key(text: str) -> frozenset:
    base = text.split(" + ")[0].strip()
    left, right = [part.strip() for part in base.split("->")]
    left_table, left_column = left.rsplit(".", 1)
    right_table, right_column = right.rsplit(".", 1)
    return _pair_key(left_table, left_column, right_table, right_column)


def classify_gold_edges(store, resolve, texts: list[str]) -> list[dict[str, Any]]:
    """Label each gold edge by how it can be represented in this database.

    ``fk``                —— 真实声明的单列外键
    ``composite_member``  —— 复合外键中的一列
    ``non_fk_predicate``  —— 不是外键的谓词（例如历史成本区间），不进入外键召回分母
    ``unknown``           —— 该库根本没有这条外键（移植库缺失声明），也不进入分母，
                             但要单独计数，否则会把「库缺外键」伪装成「算法召回低」
    """
    specs: list[dict[str, Any]] = []
    for text in texts:
        base = text.split(" + ")[0].strip()
        left, right = [part.strip() for part in base.split("->")]
        left_table, left_column = left.rsplit(".", 1)
        right_table, right_column = right.rsplit(".", 1)
        left_node, right_node = resolve(left_table), resolve(right_table)
        kind = "unknown"
        if left_node and right_node:
            relations = store.relations_between(left_node, right_node)
            single = any(
                (set(relation.source_columns) == {left_column}
                 and set(relation.target_columns) == {right_column})
                or (set(relation.source_columns) == {right_column}
                    and set(relation.target_columns) == {left_column})
                for relation in relations)
            if single:
                kind = "fk"
            elif any(left_column in (relation.source_columns + relation.target_columns)
                     and right_column in (relation.source_columns + relation.target_columns)
                     for relation in relations):
                kind = "composite_member"
        if " + " in text and kind != "unknown":
            kind = f"{kind}_with_predicate"
        elif " + " in text:
            kind = "non_fk_predicate"
        specs.append({"text": text, "key": _gold_key(text), "kind": kind})
    return specs


def _condition(left: str, right: str, roles: list[dict[str, Any]]) -> JoinCondition:
    """Same role convention as JoinPathFinder (``roles[0]``).

    Keeping it identical is what makes the comparison about *ranking*, not about
    an accidentally better role picker inside the baseline.
    """
    chosen = roles[0] if roles else {}
    return JoinCondition(
        left_table=_short(left), right_table=_short(right),
        relation_id=str(chosen.get("relation_id", "")),
        source_columns=list(chosen.get("source_columns", [])),
        target_columns=list(chosen.get("target_columns", [])),
        composite=bool(chosen.get("composite", False)),
        cardinality=str(chosen.get("cardinality", "UNKNOWN")),
        verified=bool(chosen.get("verified", False)),
    )


def baseline_shortest_paths(projection: nx.DiGraph, *, focus: list[str], targets: list[str],
                            resolve, max_hops: int, max_paths: int):
    """Unweighted shortest path over the real FK graph, hops only."""
    focus_nodes = [node for node in (resolve(name) for name in focus) if node in projection]
    target_nodes = [node for node in (resolve(name) for name in targets) if node in projection]
    internal_focus = list(dict.fromkeys(focus_nodes[:4] + target_nodes[:4]))
    pairs = build_endpoint_pairs(internal_focus, target_nodes)

    raw: list[list[str]] = []
    excluded: list[dict[str, Any]] = []
    for src, dst in pairs:
        if not nx.has_path(projection, src, dst):
            excluded.append({"from": _short(src), "to": _short(dst),
                             "reason": "no declared FK path"})
            continue
        try:
            path = nx.shortest_path(projection, src, dst)
        except nx.NetworkXNoPath:
            continue
        if len(path) - 1 > max_hops:
            excluded.append({"from": _short(src), "to": _short(dst),
                             "reason": f"path longer than max_hops={max_hops}"})
            continue
        raw.append(path)

    # Deterministic: shortest first, then lexicographic. No semantic scoring.
    raw = sorted({tuple(path) for path in raw}, key=lambda p: (len(p), "|".join(p)))
    paths: list[JoinPath] = []
    for path in raw[:max_paths]:
        conditions = [
            _condition(left, right, projection[left][right].get("roles", []))
            for left, right in zip(path, path[1:])
        ]
        paths.append(JoinPath(
            tables=[_short(node) for node in path],
            relation_ids=[c.relation_id for c in conditions],
            join_conditions=conditions,
            score=float(-(len(path) - 1)),
            hops=len(path) - 1,
            composite_fk_complete=all(
                len(c.source_columns) >= 2 for c in conditions if c.composite),
            reasons=["无权最短路径：仅按跳数排序"],
        ))
    if len(raw) > max_paths:
        excluded.extend({"tables": [_short(n) for n in path], "reason": "max_paths budget"}
                        for path in raw[max_paths:])
    return paths, excluded


def _score_case(entry: dict[str, Any], paths: list[JoinPath], *,
                ambiguity: list[dict[str, Any]],
                gold_specs: list[dict[str, Any]]) -> dict[str, Any]:
    representable = [spec for spec in gold_specs
                     if spec["kind"] in {"fk", "composite_member",
                                         "fk_with_predicate", "composite_member_with_predicate"}]
    unreachable = [spec["text"] for spec in gold_specs
                   if spec["kind"] in {"unknown", "non_fk_predicate"}]
    gold_keys = [spec["key"] for spec in representable]
    gold_set = set(gold_keys)

    per_path = [_path_pairs(path) for path in paths]
    union: set[frozenset] = set()
    for pairs in per_path:
        union |= pairs
    hits = [key for key in gold_keys if key in union]
    top1 = per_path[0] if per_path else set()
    top1_hits = [key for key in gold_keys if key in top1]
    irrelevant = [pairs for pairs in per_path if gold_set and not (pairs & gold_set)]

    composite_pair = entry.get("composite_pair")
    composite_complete = None
    if composite_pair:
        left_name, right_name = composite_pair
        composite_complete = any(
            condition.composite and len(condition.source_columns) >= 2
            for path in paths for condition in path.join_conditions
            if {_short(condition.left_table), _short(condition.right_table)}
            == {left_name, right_name})

    wrong_role_paths = None
    if entry.get("role_sensitive"):
        gold_table_pairs = {frozenset(pair) for key in gold_keys for pair in [tuple(key)]}
        gold_tables = {frozenset({item[0] for item in key}) for key in gold_keys}
        wrong_role_paths = 0
        for pairs, path in zip(per_path, paths):
            path_tables = {frozenset({_short(c.left_table), _short(c.right_table)})
                           for c in path.join_conditions}
            if (path_tables & gold_tables) and not (pairs & gold_set):
                wrong_role_paths += 1
        del gold_table_pairs

    return {
        "case_id": entry["id"],
        "category": entry.get("category"),
        "source": entry.get("_source"),
        "gold_edge_count": len(gold_specs),
        "gold_edge_count_representable": len(representable),
        "gold_edges_unreachable_in_graph": unreachable,
        "path_count": len(paths),
        "path_found": bool(paths),
        "gold_edge_hits": len(hits),
        "gold_edge_recall": (round(len(hits) / len(gold_keys), 6) if gold_keys else None),
        "top1_complete": (1.0 if gold_keys and len(top1_hits) == len(gold_keys) else 0.0)
                         if gold_keys else None,
        "top1_edge_hits": len(top1_hits) if gold_keys else None,
        "composite_fk_complete": composite_complete,
        "wrong_role_paths": wrong_role_paths,
        "ambiguous_flagged": bool(ambiguity),
        "expect_ambiguity": bool(entry.get("expect_ambiguity")),
        "irrelevant_paths": len(irrelevant),
        "hop_counts": [path.hops for path in paths],
        "top_paths": [path.tables for path in paths[:3]],
        "missing_gold_edges": [
            f"{sorted(key)[0][0]}.{sorted(key)[0][1]} -> "
            f"{sorted(key)[1][0]}.{sorted(key)[1][1]}"
            for key in gold_keys if key not in union],
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [row for row in rows if row["gold_edge_count_representable"]]
    gold_total = sum(row["gold_edge_count_representable"] for row in scored)
    hit_total = sum(row["gold_edge_hits"] for row in scored)
    composite = [row for row in rows if row["composite_fk_complete"] is not None]
    role_rows = [row for row in rows if row["wrong_role_paths"] is not None]
    ambiguity_rows = [row for row in rows if row["expect_ambiguity"]]
    unreachable = [row for row in rows if row["gold_edges_unreachable_in_graph"]]
    return {
        "cases_total": len(rows),
        "cases_with_gold_edges": len(scored),
        "cases_with_gold_edges_denominator": len(scored),
        "gold_edges_total": gold_total,
        "gold_edges_unreachable_total": sum(
            len(row["gold_edges_unreachable_in_graph"]) for row in rows),
        "cases_with_unreachable_gold": len(unreachable),
        "path_found_cases": sum(1 for row in scored if row["path_found"]),
        "path_found_rate": (round(sum(1 for row in scored if row["path_found"]) / len(scored), 6)
                            if scored else None),
        "gold_edge_recall_micro": (round(hit_total / gold_total, 6) if gold_total else None),
        "gold_edge_hits": hit_total,
        "top1_complete_cases": sum(1 for row in scored if row["top1_complete"] == 1.0),
        "top1_complete_rate": (round(sum(1 for row in scored if row["top1_complete"] == 1.0)
                                     / len(scored), 6) if scored else None),
        "top1_edge_recall_micro": (
            round(sum(row["top1_edge_hits"] or 0 for row in scored) / gold_total, 6)
            if gold_total else None),
        "composite_fk_complete_cases": len(composite),
        "composite_fk_complete_rate": (
            round(sum(1 for row in composite if row["composite_fk_complete"]) / len(composite), 6)
            if composite else None),
        "role_sensitive_cases": len(role_rows),
        "wrong_role_paths_total": sum(row["wrong_role_paths"] for row in role_rows),
        "role_correct_cases": sum(1 for row in role_rows if row["wrong_role_paths"] == 0),
        "role_correct_rate": (round(sum(1 for row in role_rows if row["wrong_role_paths"] == 0)
                                    / len(role_rows), 6) if role_rows else None),
        "ambiguity_expected_cases": len(ambiguity_rows),
        "ambiguity_flagged_cases": sum(1 for row in ambiguity_rows if row["ambiguous_flagged"]),
        "ambiguity_flag_rate": (
            round(sum(1 for row in ambiguity_rows if row["ambiguous_flagged"])
                  / len(ambiguity_rows), 6) if ambiguity_rows else None),
        "irrelevant_paths_total": sum(row["irrelevant_paths"] for row in rows),
        "mean_paths_per_case": (round(statistics.fmean([row["path_count"] for row in rows]), 6)
                                if rows else None),
    }


def run(args: argparse.Namespace) -> int:
    from app.commands.evaluate_graphrag import EvalContext

    root = PROJECT_ROOT
    cases: list[dict[str, Any]] = []
    for value in args.cases:
        path = _absolute(root, value)
        payload = json.loads(path.read_text(encoding="utf-8"))
        split = payload.get("split", "")
        if split != "dev":
            raise ValueError(f"Only the dev split may be used here; got split={split!r} from {path}")
        for case in payload["cases"]:
            cases.append({**case, "_source": path.name})

    context = EvalContext(root, vector_mode="none", local_model="")
    projection = context.store.table_projection()
    known_tables = set(projection.nodes)
    resolve = make_node_resolver(known_tables, context.store.database_id)

    validation: list[dict[str, Any]] = []
    for case in cases:
        problems: list[str] = []
        warnings: list[str] = []
        for table in case.get("required_tables") or []:
            if resolve(table) is None:
                problems.append(f"missing_table:{table}")
        for text in case.get("acceptable_join_paths") or []:
            left, right = [part.strip() for part in text.split(" + ")[0].split("->")]
            left_table, right_table = left.rsplit(".", 1)[0], right.rsplit(".", 1)[0]
            if resolve(left_table) is None or resolve(right_table) is None:
                problems.append(f"gold_edge_unknown_table:{text}")
        # 「该库没有声明这条外键」不是脚本 bug，而是环境事实（SQLite 移植版丢了
        # SalesOrderDetail.ProductID → Product.ProductID 这条真实 AW 外键）。
        # 它被降级为 warning，并在指标里以 gold_edges_unreachable_in_graph 单列，
        # 避免把它偷偷算成算法召回失败。
        for spec in classify_gold_edges(context.store, resolve,
                                        case.get("acceptable_join_paths") or []):
            if spec["kind"] == "unknown":
                warnings.append(f"gold_edge_not_declared_in_this_db:{spec['text']}")
            elif spec["kind"] in {"non_fk_predicate"}:
                warnings.append(f"gold_edge_is_non_fk_predicate:{spec['text']}")
        validation.append({"case_id": case["id"], "problems": problems,
                           "warnings": warnings, "ok": not problems})

    if args.validate_only:
        payload = {
            "validated_only": True,
            "cases": len(cases),
            "files": [str(_absolute(root, value).relative_to(root).as_posix()) for value in args.cases],
            "validation": validation,
            "all_valid": all(item["ok"] for item in validation),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["all_valid"] else 1

    finder = JoinPathFinder(context.store, max_hops=args.max_hops, max_paths=args.max_paths)
    runs: list[dict[str, Any]] = []
    for case in cases:
        retrieval = context.retriever.retrieve(
            case["question"], database_id="adventureworks", mode="keyword",
            fusion="weighted", metric_expansion=True)
        # Seeds come from the question only. required_tables / acceptable_join_paths
        # are never read here — they are used at scoring time below.
        focus = list(dict.fromkeys([
            *retrieval.table_first_tables[:2],
            *retrieval.column_first_tables[:2],
            *retrieval.candidate_tables[:3]]))[:5]
        targets = list(dict.fromkeys([*focus, *retrieval.candidate_tables[:6]]))
        focus_nodes = [node for node in (resolve(name) for name in focus) if node]
        target_nodes = [node for node in (resolve(name) for name in targets) if node]
        internal_focus = list(dict.fromkeys(focus_nodes[:4] + target_nodes[:4]))

        expected_required = {node for node in (resolve(name) for name in
                                               (case.get("required_tables") or [])) if node}
        seed_recall = (round(len(expected_required & set(target_nodes)) / len(expected_required), 6)
                       if expected_required else None)

        base_paths, base_excluded = baseline_shortest_paths(
            projection, focus=focus, targets=targets, resolve=resolve,
            max_hops=args.max_hops, max_paths=args.max_paths)
        ours = finder.find(
            [*internal_focus, *target_nodes],
            seed_tables=focus[:3], column_hints=retrieval.candidate_columns[:30],
            required_targets=[name for name in targets if resolve(name)])

        for method, paths, ambiguity in (
                ("baseline_shortest", base_paths, []),
                ("ours", ours.paths, ours.ambiguity)):
            gold_specs = classify_gold_edges(context.store, resolve,
                                             case.get("acceptable_join_paths") or [])
            row = _score_case(case, paths, ambiguity=ambiguity, gold_specs=gold_specs)
            row.update({
                "method": method,
                "seeds": [_short(node) for node in focus_nodes],
                "targets": [_short(node) for node in target_nodes],
                "endpoint_pairs": len(build_endpoint_pairs(internal_focus, target_nodes)),
                "required_table_recall_from_question_only": seed_recall,
            })
            runs.append(row)

    summary = {method: _aggregate([row for row in runs if row["method"] == method])
               for method in METHODS}
    summary_by_source = {}
    for source in sorted({row["source"] for row in runs}):
        summary_by_source[source] = {
            method: _aggregate([row for row in runs
                                if row["method"] == method and row["source"] == source])
            for method in METHODS}

    output = {
        "dataset": "adventureworks-graphrag-join",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cases": len(cases),
        "files": [str(_absolute(root, value).relative_to(root).as_posix()) for value in args.cases],
        "methods": list(METHODS),
        "budget": {"max_hops": args.max_hops, "max_paths": args.max_paths,
                   "same_endpoint_pairs": True,
                   "role_convention": "roles[0] for both methods",
                   "graph": "workspace/knowledge/adventureworks/relations.json (real PRAGMA FKs)"},
        "fairness": [
            "两种方法都使用 build_endpoint_pairs 生成的同一批端点对，跳数预算与输出条数预算相同。",
            "两种方法都遍历同一个 table_projection（真实外键图），都取每跳的 roles[0]。",
            "种子表只由问题文本的确定性关键词检索产生，required_tables / acceptable_join_paths 不进入检索。",
            "含 gold 边的题目才算分母；无 gold 边的题目记 N/A（None），不当作 0 分。",
        ],
        "validation": validation,
        "summary": summary,
        "summary_by_source": summary_by_source,
        "runs": runs,
        "notes": [
            "baseline_shortest 按跳数排序，不看 verified / 基数 / 完整性 / 粒度；ours 是现有 JoinPathFinder 的真实输出。",
            "上一轮报告的 0 → 0.9286 是「不输出路径的配置」与「输出路径的配置」之间的差，不能当作路径算法准确率；本文件才是同类方法的对照。",
            "role_correct_rate 对两种方法共用 roles[0] 约定，因此它衡量的是「当前实现能否按问题选择角色」，而不是两者差异。",
        ],
    }
    destination = _absolute(root, args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    if args.quiet:
        print(json.dumps({"cases": len(cases), "summary": summary}, ensure_ascii=False,
                         sort_keys=True))
    else:
        print(json.dumps({key: output[key] for key in output if key != "runs"},
                         ensure_ascii=False, indent=2))
    return 0 if all(item["ok"] for item in validation) else 1
