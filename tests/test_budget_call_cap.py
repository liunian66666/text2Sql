"""Phase 4B 收尾：BudgetMeter 调用数硬闸的离线单测（零在线请求）。

规格要求 ``--max-llm-calls`` 在**每次真实模型请求前**强制检查，而不是
band 之间事后统计。这里直接测 BudgetMeter 的 check()/add_call() 契约，
以及 start_meter 的 max_calls 透传。
"""
from __future__ import annotations

import unittest

from app.llm.budget import (
    BudgetExceededError,
    BudgetMeter,
    get_meter,
    reset_meter,
    start_meter,
)


class BudgetMeterCallCapTests(unittest.TestCase):
    def test_check_passes_below_cap(self):
        meter = BudgetMeter(max_calls=3)
        meter.add_call(10, 5)
        meter.add_call(10, 5)  # 2 completed calls < 3 → still allowed
        meter.check()

    def test_check_raises_at_cap(self):
        meter = BudgetMeter(max_calls=2)
        meter.add_call(1, 1)
        meter.add_call(1, 1)
        with self.assertRaises(BudgetExceededError) as ctx:
            meter.check()
        self.assertIn("2/2", str(ctx.exception))

    def test_cap_independent_of_tokens(self):
        # Tokens alone would pass; only the call count trips the guard.
        meter = BudgetMeter(max_calls=1)
        meter.add_call(0, 0)
        with self.assertRaises(BudgetExceededError):
            meter.check()

    def test_token_cap_still_enforced(self):
        meter = BudgetMeter(max_total_tokens=10)
        meter.add_call(8, 8)
        with self.assertRaises(BudgetExceededError):
            meter.check()

    def test_no_caps_never_raises(self):
        meter = BudgetMeter()
        meter.add_call(1000, 1000)
        meter.check()

    def test_snapshot_reports_call_budget(self):
        meter = BudgetMeter(max_calls=7)
        snap = meter.snapshot()
        self.assertEqual(snap["budget_calls"], 7)
        self.assertEqual(snap["calls"], 0)

    def test_start_meter_propagates_max_calls(self):
        token = start_meter(None, None, 5)
        try:
            meter = get_meter()
            self.assertIsNotNone(meter)
            self.assertEqual(meter.max_calls, 5)
            for _ in range(5):
                meter.add_call(1, 1)
            with self.assertRaises(BudgetExceededError):
                meter.check()
        finally:
            reset_meter(token)

    def test_default_start_meter_has_no_call_cap(self):
        token = start_meter(None, None)
        try:
            self.assertIsNone(get_meter().max_calls)
        finally:
            reset_meter(token)


if __name__ == "__main__":
    unittest.main()
