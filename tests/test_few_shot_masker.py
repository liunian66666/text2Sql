"""Target-mask error layering: soft per-call truncation vs hard budget abort.

A masked question only feeds few-shot retrieval, so one truncated call degrades
to raw text. A real token/time cap must not degrade: that is what made a 213s
stage look inexplicable, because three timed-out attempts left no trace.
"""
import unittest
from types import SimpleNamespace

from app.few_shot.masker import mask_target_question_sql
from app.llm.budget import BudgetExceededError, TruncatedResponseError


def _llm_raising(exc):
    def ask(*args, **kwargs):
        raise exc
    return SimpleNamespace(ask=ask)


class TargetMaskBudgetTest(unittest.TestCase):
    def test_truncated_call_degrades_to_raw_text(self):
        result = mask_target_question_sql(
            question="统计2026年销售额",
            evidence="有效订单指状态为 paid",
            sql="SELECT 1",
            llm=_llm_raising(TruncatedResponseError("all choices hit max_tokens")),
        )
        self.assertEqual(result.source, "fallback")
        self.assertEqual(result.masked_sql, "SELECT 1")

    def test_hard_budget_abort_is_not_swallowed(self):
        with self.assertRaises(BudgetExceededError):
            mask_target_question_sql(
                question="统计2026年销售额",
                evidence="",
                sql="SELECT 1",
                llm=_llm_raising(BudgetExceededError("LLM time budget exceeded (250s/240s)")),
            )

    def test_provider_error_still_degrades(self):
        result = mask_target_question_sql(
            question="统计2026年销售额",
            evidence="",
            sql=None,
            llm=_llm_raising(RuntimeError("timeout after retries")),
        )
        self.assertEqual(result.source, "fallback")


if __name__ == "__main__":
    unittest.main()
