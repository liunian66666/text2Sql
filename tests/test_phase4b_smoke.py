"""Phase 4B 在线 Smoke Test 的离线契约测试。

这里一条在线请求都不发。测的是「能不能在花钱之前拦住」：

* 预算上界必须从真实配置字段推出来，不能写死；
* ``--execute`` 缺 ``--approve-online`` / 缺上限 → 必须在创建 LLM 客户端之前报错；
* 结果比较协议必须是显式规则（数值容差、顺序、行数），而不是「跑通即正确」。
"""
from __future__ import annotations

import argparse
import unittest
from unittest import mock

from app.commands import phase4b_smoke


class _Budget:
    def __init__(self, **overrides):
        self.checkers = overrides.get("checkers")
        for key, value in overrides.items():
            if key == "checkers":
                continue
            setattr(self, key, value)


class _LLM:
    def __init__(self, model="qwen3.8-max", max_tokens=8192):
        self.model = model
        self.max_tokens = max_tokens


class _Config:
    def __init__(self, budgets, *, filter_top_k=2, preliminary=True):
        self.schema_linking_config = _Budget(
            direct_linking_sampling_budget=budgets["direct"],
            reversed_linking_sampling_budget=budgets["reversed"])
        self.sql_generation_config = _Budget(
            dc_sampling_budget=budgets["dc"], skeleton_sampling_budget=budgets["skeleton"],
            icl_sampling_budget=budgets["icl"], llm=_LLM())
        self.sql_revision_config = _Budget(
            checker_sampling_budget=budgets["checker"], checkers=None)
        self.sql_selection_config = _Budget(
            filter_top_k_sql=filter_top_k, evaluator_sampling_budget=budgets["evaluator"])
        self.few_shot_index_config = _Budget(preliminary_sql=_Budget(enabled=preliminary))
        self.run_config = _Budget(parallelism=2, llm_timeout=300)


class BudgetPlanTest(unittest.TestCase):
    def test_derivation_uses_real_budgets(self) -> None:
        cfg = _Config({"direct": 1, "reversed": 1, "dc": 1, "skeleton": 1, "icl": 1,
                       "checker": 1, "evaluator": 2})
        plan = phase4b_smoke.budget_plan(cfg)
        per_run = plan["per_run_worst_case_llm_requests"]
        self.assertEqual(per_run["schema_linking"], 2)
        self.assertEqual(per_run["sql_generation"], 3)
        # 3 个候选 × 8 个 checker × 1
        self.assertEqual(per_run["sql_revision_worst_case"], 24)
        # C(2,2)=1 对 × 2 票
        self.assertEqual(per_run["sql_selection"], 2)
        self.assertEqual(per_run["planner"], phase4b_smoke.PLANNER_ROUNDS + 1)
        self.assertEqual(per_run["few_shot_preparation"], 2)
        self.assertEqual(plan["per_run_worst_case_total"], sum(per_run.values()))

    def test_high_sampling_budget_scales_worst_case(self) -> None:
        cfg = _Config({"direct": 8, "reversed": 8, "dc": 8, "skeleton": 8, "icl": 8,
                       "checker": 8, "evaluator": 8})
        plan = phase4b_smoke.budget_plan(cfg)
        per_run = plan["per_run_worst_case_llm_requests"]
        self.assertEqual(per_run["sql_generation"], 24)
        self.assertEqual(per_run["sql_revision_worst_case"], 24 * 8 * 8)
        self.assertGreater(plan["per_run_worst_case_total"], 1500)

    def test_single_top_k_has_no_pairwise_selection(self) -> None:
        cfg = _Config({"direct": 1, "reversed": 1, "dc": 1, "skeleton": 1, "icl": 1,
                       "checker": 1, "evaluator": 2}, filter_top_k=1)
        self.assertEqual(
            phase4b_smoke.budget_plan(cfg)["per_run_worst_case_llm_requests"]["sql_selection"], 0)

    def test_preliminary_sql_disabled_reduces_few_shot_calls(self) -> None:
        cfg = _Config({"direct": 1, "reversed": 1, "dc": 1, "skeleton": 1, "icl": 1,
                       "checker": 1, "evaluator": 1}, preliminary=False)
        self.assertEqual(
            phase4b_smoke.budget_plan(cfg)["per_run_worst_case_llm_requests"]
            ["few_shot_preparation"], 1)


class GateTest(unittest.TestCase):
    """在线执行的两道门必须在任何网络调用之前生效。"""

    def _args(self, **overrides) -> argparse.Namespace:
        base = dict(config="config/local/adventureworks-smoke.toml",
                    cases="evals/adventureworks-graphrag/dev.json",
                    case_id="AW-DEV-001", bands="baseline,enhanced",
                    output_dir="workspace/evals/phase4b", execute=True,
                    approve_online=True, max_tokens=80_000, max_seconds=240.0,
                    max_llm_calls=0, rel_tol=1e-6, abs_tol=1e-2, quiet=True)
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_execute_without_approval_is_refused(self) -> None:
        with mock.patch.object(phase4b_smoke, "resolve_config"), \
                mock.patch.object(phase4b_smoke, "load_case",
                                  return_value={"id": "X", "question": "q", "split": "dev"}):
            with self.assertRaises(ValueError) as raised:
                phase4b_smoke.run(self._args(approve_online=False))
        self.assertIn("approve-online", str(raised.exception))

    def test_execute_without_hard_caps_is_refused(self) -> None:
        with mock.patch.object(phase4b_smoke, "resolve_config"), \
                mock.patch.object(phase4b_smoke, "load_case",
                                  return_value={"id": "X", "question": "q", "split": "dev"}):
            with self.assertRaises(ValueError) as raised:
                phase4b_smoke.run(self._args(max_tokens=0))
        self.assertIn("max-tokens", str(raised.exception))

    def test_sealed_test_split_is_refused(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.json"
            path.write_text(json.dumps({"split": "test", "cases": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                phase4b_smoke.load_case(path, "AW-DEV-001")


class ComparisonProtocolTest(unittest.TestCase):
    """比较协议写死在这里，避免「SQL 执行成功 = 业务正确」。"""

    def test_exact_match(self) -> None:
        rows = [["Bikes", 94651172.7], ["Components", 11802593.29]]
        result = phase4b_smoke.compare_results(rows, rows, rel=1e-6, absolute=1e-2)
        self.assertTrue(result["ordered_match"])
        self.assertTrue(result["unordered_match"])

    def test_rounding_difference_within_absolute_tolerance(self) -> None:
        actual = [["Bikes", 94651172.6999]]
        expected = [["Bikes", 94651172.70]]
        self.assertTrue(phase4b_smoke.compare_results(
            actual, expected, rel=1e-9, absolute=1e-2)["ordered_match"])

    def test_numeric_text_is_compared_as_number(self) -> None:
        self.assertTrue(phase4b_smoke.compare_results(
            [["Bikes", "94651172.7"]], [["Bikes", 94651172.7]],
            rel=1e-6, absolute=1e-2)["ordered_match"])

    def test_order_matters_for_ordered_match_but_not_unordered(self) -> None:
        actual = [["A", 1], ["B", 2]]
        expected = [["B", 2], ["A", 1]]
        result = phase4b_smoke.compare_results(actual, expected, rel=1e-6, absolute=1e-2)
        self.assertFalse(result["ordered_match"])
        self.assertTrue(result["unordered_match"])

    def test_row_count_mismatch_is_not_correct(self) -> None:
        result = phase4b_smoke.compare_results([["A", 1]], [["A", 1], ["B", 2]],
                                               rel=1e-6, absolute=1e-2)
        self.assertFalse(result["row_count_match"])
        self.assertFalse(result["ordered_match"])

    def test_empty_actual_against_non_empty_expected(self) -> None:
        result = phase4b_smoke.compare_results([], [["A", 1]], rel=1e-6, absolute=1e-2)
        self.assertFalse(result["ordered_match"])

    def test_null_matches_null_only(self) -> None:
        self.assertTrue(phase4b_smoke.compare_results(
            [[None]], [[None]], rel=1e-6, absolute=1e-2)["ordered_match"])
        self.assertFalse(phase4b_smoke.compare_results(
            [[None]], [[0]], rel=1e-6, absolute=1e-2)["ordered_match"])


if __name__ == "__main__":
    unittest.main()
