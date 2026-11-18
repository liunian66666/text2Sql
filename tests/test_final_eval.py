"""``final-eval`` 离线单测：封存集守卫、题目选择、预算分配、门禁顺序。

全部零在线请求；在线门禁只验证「参数不合法时立刻拒绝」。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import unittest

from app.commands import final_eval
from app.commands.phase4b_smoke import DEFAULT_MAX_CALLS_PER_RUN


def _write_test_split(path: Path, *, split: str = "test", ids=("AW-TEST-001", "AW-TEST-002")):
    payload = {
        "dataset": "adventureworks-graphrag",
        "split": split,
        "cases": [
            {"id": case_id, "question": f"q-{case_id}", "split": split,
             "reference_sql": "SELECT 1", "reference_sql_verified": True,
             "database_id": "adventureworks"}
            for case_id in ids
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


class LoadTestCasesTests(unittest.TestCase):
    def test_accepts_test_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.json"
            _write_test_split(path)
            cases = final_eval.load_test_cases(path, [])
            self.assertEqual([c["id"] for c in cases],
                             ["AW-TEST-001", "AW-TEST-002"])

    def test_refuses_dev_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dev.json"
            _write_test_split(path, split="dev")
            with self.assertRaisesRegex(ValueError, "sealed test split"):
                final_eval.load_test_cases(path, [])

    def test_case_ids_subset_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.json"
            _write_test_split(path)
            subset = final_eval.load_test_cases(path, ["AW-TEST-002"])
            self.assertEqual([c["id"] for c in subset], ["AW-TEST-002"])
            with self.assertRaisesRegex(ValueError, "AW-TEST-999"):
                final_eval.load_test_cases(path, ["AW-TEST-999"])


class AllocateCallCapsTests(unittest.TestCase):
    def test_caps_are_min_of_per_run_and_remaining(self):
        caps = final_eval.allocate_call_caps(6, 2, total=600, per_run=50)
        self.assertEqual(len(caps), 12)
        self.assertTrue(all(cap == 50 for cap in caps))

    def test_global_budget_exhaustion_flattens_to_zero(self):
        caps = final_eval.allocate_call_caps(3, 2, total=7, per_run=50)
        self.assertEqual(caps, [7, 0, 0, 0, 0, 0])

    def test_zero_total_yields_all_zero(self):
        self.assertEqual(final_eval.allocate_call_caps(2, 2, total=0, per_run=50),
                         [0, 0, 0, 0])

    def test_default_per_run_matches_spec(self):
        self.assertEqual(DEFAULT_MAX_CALLS_PER_RUN, 50)


class OnlineGateTests(unittest.TestCase):
    def _args(self, **overrides):
        base = {"execute": True, "approve_online": False, "max_tokens": 0,
                "max_seconds": 0.0, "max_llm_calls": 0, "max_calls_per_run": 50,
                "config": "config/local/adventureworks-smoke.toml",
                "cases": "evals/adventureworks-graphrag/test.json",
                "bands": "baseline,enhanced", "case_ids": "",
                "output_dir": "workspace/evals/phase4b-final",
                "dense_backend": "local", "rel_tol": 1e-6, "abs_tol": 1e-2,
                "quiet": True}
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_execute_without_approve_fails_before_config_load(self):
        with self.assertRaisesRegex(ValueError, "--approve-online"):
            final_eval.run(self._args())

    def test_execute_without_caps_fails(self):
        with self.assertRaisesRegex(ValueError, "--max-tokens and --max-seconds"):
            final_eval.run(self._args(approve_online=True))


if __name__ == "__main__":
    unittest.main()
