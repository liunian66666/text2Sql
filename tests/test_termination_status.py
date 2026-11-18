"""C2：四类「没跑完」的终止条件必须如实登记，并映射为 partial。

背景：修复轮只给 `tool_limit` 一处写了 `termination_reason`，
而 SQL 上限、探查上限、能力缺口都只是返回 failed ToolOutcome。
模型若在这些情况下直接总结，结果仍被标成 success —— 用户看到「分析完成」，
实际上少了一个维度或整张表。这里逐条锁死。
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from app.agent.service import DataAnalysisAgent
from app.agent.tools.registry import MAX_PROBES_PER_TURN, MAX_SQL_GENERATIONS_PER_TURN, ToolRegistry


def _make_sales_like(path: Path) -> Path:
    """只有当前成本，没有任何历史成本表。"""
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders(order_id INTEGER PRIMARY KEY, order_date TEXT, region TEXT)")
        connection.execute("CREATE TABLE products(product_id INTEGER PRIMARY KEY, unit_cost REAL)")
        connection.execute("INSERT INTO orders VALUES (1, '2025-01-02', 'East')")
        connection.execute("INSERT INTO products VALUES (1, 9.9)")
    return path


def _make_history_like(path: Path) -> Path:
    """带成本历史与收货地址的库，能力识别应命中。"""
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders(order_id INTEGER PRIMARY KEY, order_date TEXT, "
                           "ShipToAddressID INTEGER)")
        connection.execute("CREATE TABLE ProductCostHistory(ProductID INTEGER, StandardCost REAL, "
                           "StartDate TEXT, EndDate TEXT)")
        connection.execute("CREATE TABLE ProductCategory(ProductCategoryID INTEGER, Name TEXT)")
        connection.execute("CREATE TABLE ProductSubcategory(ProductSubcategoryID INTEGER, Name TEXT)")
        connection.execute("INSERT INTO orders VALUES (1, '2025-03-01', 10)")
    return path


class _FakeSkill:
    def __init__(self):
        self.requests: list[dict] = []

    def run(self, request, on_stage=None):
        self.requests.append(dict(request))
        payload = {
            "request_id": f"req-{len(self.requests)}",
            "status": "success",
            "sql": f"SELECT {len(self.requests)}",
            "columns": ["c"],
            "rows": [[1]],
            "truncated": False,
            "elapsed_seconds": 0.01,
            "stage_seconds": {},
            "usage": {},
            "failed_stage": None,
            "error": None,
            "artifacts_dir": "tmp",
        }
        return SimpleNamespace(model_dump=lambda: payload)


def _state(**overrides):
    base = {
        "turn_id": "t1",
        "question": "各州历史成本与毛利",
        "database_id": "enterprise_sales",
        "requested_database_id": "auto",
        "request": {"question": "各州历史成本与毛利"},
        "available_databases": ["enterprise_sales", "adventureworks"],
        "schema_context_database_id": "enterprise_sales",
        "sql_result": {}, "sql_result_turn_id": "", "sql_result_database_id": "",
        "sql_generations": 0, "sql_history": [], "sql_results": [],
        "probe_results": [], "probe_cache": {},
        "trusted_evidence": "", "user_hint": "", "analysis_intent": "query",
    }
    base.update(overrides)
    return base


class CapabilityGapTerminationTest(unittest.TestCase):

    def test_manual_choice_with_missing_capability_records_reason(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            registry = ToolRegistry(sql_skill=_FakeSkill(), databases={
                "enterprise_sales": _make_sales_like(root / "sales.sqlite"),
                "adventureworks": _make_history_like(root / "history.sqlite"),
            })
            outcome = registry._select_data_source(
                {"database_id": "enterprise_sales"},
                _state(requested_database_id="enterprise_sales"), None)

        self.assertEqual(outcome.updates["termination_reason"], "capability_gap")
        self.assertTrue(outcome.updates["capability_gap"])

    def test_successful_auto_switch_is_not_a_gap(self):
        """自动切到真有能力的数据源属于「已解决」，不能误标 partial。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            registry = ToolRegistry(sql_skill=_FakeSkill(), databases={
                "enterprise_sales": _make_sales_like(root / "sales.sqlite"),
                "adventureworks": _make_history_like(root / "history.sqlite"),
            })
            outcome = registry._select_data_source(
                {"database_id": "enterprise_sales"}, _state(requested_database_id="auto"), None)

        self.assertEqual(outcome.updates["database_id"], "adventureworks")
        self.assertNotIn("termination_reason", outcome.updates)
        self.assertNotIn("capability_gap", outcome.updates)


class BudgetTerminationTest(unittest.TestCase):

    def test_probe_limit_records_reason(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            registry = ToolRegistry(sql_skill=_FakeSkill(), databases={
                "enterprise_sales": _make_sales_like(Path(tmp) / "sales.sqlite")})
            state = _state(probe_results=[{} for _ in range(MAX_PROBES_PER_TURN)])
            outcome = registry._probe_database(
                {"probe": "date_range", "table": "orders", "column": "order_date"}, state, None)

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.updates["termination_reason"], "probe_limit")

    def test_sql_budget_limit_records_reason(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            registry = ToolRegistry(sql_skill=_FakeSkill(), databases={
                "enterprise_sales": _make_sales_like(Path(tmp) / "sales.sqlite")})
            state = _state(sql_generations=MAX_SQL_GENERATIONS_PER_TURN)
            outcome = registry._run_text_to_sql(
                {"question": "各州历史成本", "intent": "query"}, state, None)

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.updates["termination_reason"], "sql_budget_limit")


class _ScriptedModel:
    def __init__(self, messages):
        self.responses = list(messages)
        self.calls = 0

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self

    def invoke(self, messages, config=None):
        self.calls += 1
        if not self.responses:
            raise RuntimeError("script exhausted")
        return self.responses.pop(0)


def _schema_call(call_id: str):
    from langchain_core.messages import AIMessage

    return AIMessage(content="", tool_calls=[{
        "id": call_id, "name": "inspect_database_schema", "args": {"table_names": []}}])


def _sql_call(call_id: str, question: str):
    from langchain_core.messages import AIMessage

    return AIMessage(content="", tool_calls=[{
        "id": call_id, "name": "run_text_to_sql",
        "args": {"question": question, "intent": "query"}}])


class PartialStatusEndToEndTest(unittest.TestCase):
    """状态映射：有 termination_reason 就必须是 partial，不能是 success。"""

    def test_sql_budget_exhaustion_yields_partial(self):
        from langchain_core.messages import AIMessage

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = _make_sales_like(Path(tmp) / "sales.sqlite")
            model = _ScriptedModel([
                _schema_call("s"),
                _sql_call("0", "各州销售额"),
                _sql_call("1", "各州订单数"),
                _sql_call("2", "各商品大类销售额"),
                _sql_call("3", "各州客户数"),  # 第 4 次会被上限拦下
                AIMessage(content="已用已有结果汇总"),
            ])
            agent = DataAnalysisAgent(_FakeSkill(), workspace=Path(tmp) / "agent",
                                     databases={"enterprise_sales": db}, chat_model=model)
            result = agent.run({"question": "各州销售额", "database_id": "enterprise_sales"})

        self.assertEqual(result.status, "partial", result.error)
        self.assertEqual(result.termination_reason, "sql_budget_limit")
        # 已达上限：只有前 3 次真正生成
        self.assertEqual(result.sql_generations, MAX_SQL_GENERATIONS_PER_TURN)
        self.assertEqual(len(result.analyses), MAX_SQL_GENERATIONS_PER_TURN)

    def test_capability_gap_without_sql_is_still_partial(self):
        """缺表且没有 SQL 时，也必须是「部分完成」而不是 error/success。"""
        from langchain_core.messages import AIMessage

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            model = _ScriptedModel([
                AIMessage(content="", tool_calls=[{
                    "id": "0", "name": "select_data_source",
                    "args": {"database_id": "enterprise_sales"}}]),
                AIMessage(content="该数据源缺少历史成本表，无法计算毛利。"),
            ])
            agent = DataAnalysisAgent(_FakeSkill(), workspace=root / "agent", databases={
                "enterprise_sales": _make_sales_like(root / "sales.sqlite"),
                "adventureworks": _make_history_like(root / "history.sqlite"),
            }, chat_model=model)
            result = agent.run({"question": "各州历史成本与毛利", "database_id": "enterprise_sales"})

        self.assertEqual(result.status, "partial", result.error)
        self.assertEqual(result.termination_reason, "capability_gap")
        self.assertTrue(result.capability_gap)
        self.assertTrue(result.answer)


if __name__ == "__main__":
    unittest.main()
