"""C3：Agent 层模型调用必须过 BudgetMeter 硬闸。

背景：编排层用的是裸 ``ChatOpenAI``，``BudgetMeter`` 只保护 SQL pipeline。
assistant 轮、planner 轮、超限总结全部免费 —— Web 路径上没有任何 token /
调用次数硬上限，成本只靠评测脚本外层兜。这里锁死三件事：
1) 每次真实模型调用前检查、后记账；
2) 超限抛可识别异常，不被伪装成「模型暂时不可用」；
3) 预算随请求开始/结束，不跨请求泄漏。
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from app.agent.llm import MeteredChatModel, agent_budget_limits
from app.agent.service import DataAnalysisAgent
from app.llm.budget import (
    BudgetExceededError, get_meter, reset_meter, start_meter)


class _CountingModel:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        self.bound = list(tools)
        return self

    def invoke(self, messages, config=None):
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return AIMessage(content="ok")


def _metered_call(*, prompt=10, completion=5):
    """Return a fake AIMessage carrying usage_metadata for accounting checks."""
    return AIMessage(content="x", usage_metadata={
        "input_tokens": prompt, "output_tokens": completion, "total_tokens": prompt + completion})


class MeteredModelTest(unittest.TestCase):

    def test_passthrough_without_meter(self):
        model = MeteredChatModel(_CountingModel())
        response = model.invoke([])
        self.assertEqual(response.content, "ok")
        self.assertIsNone(get_meter())

    def test_second_call_raises_when_max_calls_reached(self):
        inner = _CountingModel()
        model = MeteredChatModel(inner)
        token = start_meter(None, None, max_calls=1)
        try:
            model.invoke([])
            with self.assertRaises(BudgetExceededError) as raised:
                model.invoke([])
        finally:
            reset_meter(token)
        self.assertIn("call budget", str(raised.exception).lower())
        # 第 2 次在 HTTP 之前就被拦下，inner 只真正执行了 1 次
        self.assertEqual(inner.calls, 1)

    def test_usage_is_booked_and_token_cap_enforced(self):
        inner = _CountingModel([_metered_call(prompt=80, completion=40)])
        model = MeteredChatModel(inner)
        token = start_meter(max_total_tokens=100, max_seconds=None)
        try:
            model.invoke([])
            meter = get_meter()
            self.assertEqual(meter.total_tokens, 120)
            with self.assertRaises(BudgetExceededError):
                model.invoke([])
        finally:
            reset_meter(token)

    def test_bind_tools_keeps_metering(self):
        inner = _CountingModel()
        bound = MeteredChatModel(inner).bind_tools(["t"])
        token = start_meter(None, None, max_calls=1)
        try:
            bound.invoke([])
            with self.assertRaises(BudgetExceededError):
                bound.invoke([])
        finally:
            reset_meter(token)


def _make_db(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders(order_id INTEGER PRIMARY KEY, region TEXT)")
        connection.execute("INSERT INTO orders VALUES (1, 'East')")
    return path


class _FakeSkill:
    def run(self, request, on_stage=None):
        return SimpleNamespace(model_dump=lambda: {
            "request_id": "req-1", "status": "success", "sql": "SELECT 1",
            "columns": ["c"], "rows": [[1]], "truncated": False, "elapsed_seconds": 0.01,
            "stage_seconds": {}, "usage": {}, "failed_stage": None, "error": None,
            "artifacts_dir": "tmp"})


class AgentBudgetGateTest(unittest.TestCase):

    def test_model_call_budget_stops_the_loop_honestly(self):
        model = _CountingModel([
            AIMessage(content="", tool_calls=[{"id": "0", "name": "get_capabilities", "args": {}}]),
            AIMessage(content="不该被调用到"),
        ])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            agent = DataAnalysisAgent(_FakeSkill(), workspace=Path(tmp) / "agent",
                                     databases={"enterprise_sales": _make_db(Path(tmp) / "s.sqlite")},
                                     chat_model=model, llm_budget={"max_calls": 1})
            result = agent.run({"question": "看看有哪些数据源", "database_id": "enterprise_sales"})

        self.assertEqual(result.status, "error")
        self.assertEqual(result.termination_reason, "llm_budget_exhausted")
        self.assertIn("预算", result.error)
        self.assertEqual(model.calls, 1)
        self.assertEqual(result.model_calls, 1)

    def test_planner_budget_exhaustion_surfaces_as_error_result(self):
        """planner 阶段耗尽预算时，异常不能被降级成「计划生成失败」。"""
        complex_question = ("请分析最近一个完整自然年的销售经营情况，按州和商品大类统计销售额、"
                            "历史成本、毛利及毛利率，并与上一年度同比比较。")
        model = _CountingModel([
            AIMessage(content="", tool_calls=[{"id": "0", "name": "get_capabilities", "args": {}}]),
            AIMessage(content=json.dumps({"goal": "经营分析"})),
        ])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            agent = DataAnalysisAgent(_FakeSkill(), workspace=Path(tmp) / "agent",
                                     databases={"enterprise_sales": _make_db(Path(tmp) / "s.sqlite")},
                                     chat_model=model, llm_budget={"max_calls": 1})
            result = agent.run({"question": complex_question, "database_id": "enterprise_sales"})

        self.assertEqual(result.status, "error")
        self.assertEqual(result.termination_reason, "llm_budget_exhausted")

    def test_meter_does_not_leak_between_requests(self):
        """ContextVar 必须随请求复位，否则第二次请求会继承上次的计数。"""
        model = _CountingModel()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            agent = DataAnalysisAgent(_FakeSkill(), workspace=Path(tmp) / "agent",
                                     databases={"enterprise_sales": _make_db(Path(tmp) / "s.sqlite")},
                                     chat_model=model, llm_budget={"max_calls": 1})
            first = agent.run({"question": "第一问", "database_id": "enterprise_sales"})
            second = agent.run({"question": "第二问", "database_id": "enterprise_sales"})

        self.assertEqual(first.status, "success", first.error)
        self.assertEqual(second.status, "success", second.error)
        self.assertIsNone(get_meter())
        # 两次请求各调用 1 次模型（计数没有累加）
        self.assertEqual(model.calls, 2)

    def test_budget_limits_resolution_order(self):
        limits = agent_budget_limits({"max_calls": 3})
        self.assertEqual(limits["max_calls"], 3)
        self.assertGreater(limits["max_total_tokens"], 0)
        self.assertGreater(limits["max_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
