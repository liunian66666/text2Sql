"""Complex-question flow: capability routing, probes, plan, SQL budget, partial status."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from app.agent.capabilities import describe_database, required_capabilities
from app.agent.planning import is_complex_question, normalize_plan, render_plan
from app.agent.service import AgentResult, DataAnalysisAgent
from app.agent.tools.probe import last_complete_year
from app.agent.tools.registry import (
    MAX_SQL_GENERATIONS_PER_TURN, ToolRegistry, _question_key)

COMPLEX_QUESTION = (
    "请分析数据库中最近一个完整自然年的销售经营情况。按照订单的收货地址所在州和商品大类进行统计，"
    "计算各地区、各商品类别的明细销售额、订单数量、历史销售成本、毛利额及毛利率，并与上一年度进行同比比较。")


def _make_sales_like(path: Path) -> Path:
    """最小电商库：只有当前成本，没有任何历史成本表。"""
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders(order_id INTEGER PRIMARY KEY, order_date TEXT, region TEXT)")
        connection.execute("CREATE TABLE products(product_id INTEGER PRIMARY KEY, unit_cost REAL)")
        connection.execute("INSERT INTO orders VALUES (1, '2025-01-02', 'East'), (2, '2026-01-05', 'West')")
        connection.execute("INSERT INTO products VALUES (1, 9.9)")
    return path


def _make_history_like(path: Path) -> Path:
    """带成本历史与收货地址的最小库：能力识别应命中三项。"""
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders(order_id INTEGER PRIMARY KEY, order_date TEXT, ShipToAddressID INTEGER)")
        connection.execute("CREATE TABLE ProductCostHistory(ProductID INTEGER, StandardCost REAL, "
                           "StartDate TEXT, EndDate TEXT)")
        connection.execute("CREATE TABLE ProductCategory(ProductCategoryID INTEGER, Name TEXT)")
        connection.execute("CREATE TABLE ProductSubcategory(ProductSubcategoryID INTEGER, Name TEXT)")
        connection.execute("INSERT INTO orders VALUES (1, '2025-03-01', 10)")
    return path


class _FakeSkill:
    """Records each SQL generation; returns a stable payload."""

    def __init__(self):
        self.requests: list[dict] = []

    def run(self, request, on_stage=None):
        self.requests.append(dict(request))
        payload = {
            "status": "success", "sql": f"SELECT {len(self.requests)}", "columns": ["c"], "rows": [[1]],
            "truncated": False, "elapsed_seconds": 0.01, "stage_seconds": {}, "usage": {},
            "failed_stage": None, "error": None, "artifacts_dir": "tmp",
        }
        return SimpleNamespace(model_dump=lambda: payload)


def _state(**overrides):
    base = {
        "turn_id": "t1", "question": COMPLEX_QUESTION, "database_id": "enterprise_sales",
        "requested_database_id": "auto", "request": {"question": COMPLEX_QUESTION},
        "available_databases": ["enterprise_sales", "adventureworks"],
        "schema_context_database_id": "enterprise_sales",
        "sql_result": {}, "sql_result_turn_id": "", "sql_result_database_id": "",
        "sql_generations": 0, "sql_history": [], "probe_results": [], "probe_cache": {},
        "trusted_evidence": "", "user_hint": "", "analysis_intent": "query",
    }
    base.update(overrides)
    return base


class CapabilityRoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.sales = _make_sales_like(root / "sales.sqlite")
        self.history = _make_history_like(root / "history.sqlite")
        self.registry = ToolRegistry(sql_skill=_FakeSkill(),
                                     databases={"enterprise_sales": self.sales,
                                                "adventureworks": self.history})

    def tearDown(self):
        self.tmp.cleanup()

    def test_required_capabilities_reads_business_wording(self):
        # 这道题同时要求历史成本、收货地址州省、商品大类层级——三项都必须识别
        self.assertEqual(required_capabilities(COMPLEX_QUESTION),
                         {"historical_cost", "ship_address", "product_hierarchy"})
        self.assertEqual(required_capabilities("统计各商品类别销售额"), set())

    def test_schema_introspection_detects_history_and_ship_address(self):
        caps = describe_database("adventureworks", self.history)
        self.assertTrue(caps.supports("historical_cost"))
        self.assertTrue(caps.supports("ship_address"))
        self.assertTrue(caps.supports("product_hierarchy"))
        plain = describe_database("enterprise_sales", self.sales)
        self.assertFalse(plain.supports("historical_cost"))

    def test_auto_mode_switches_to_source_that_really_has_cost_history(self):
        outcome = self.registry._select_data_source({"database_id": "enterprise_sales"},
                                                    _state(), None)
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.updates["database_id"], "adventureworks")
        payload = json.loads(outcome.content)
        self.assertIn("能力", payload["capability_note"])
        self.assertEqual(payload["required_capabilities"],
                         ["historical_cost", "product_hierarchy", "ship_address"])

    def test_manual_choice_is_never_overridden_and_gap_is_reported(self):
        state = _state(requested_database_id="enterprise_sales")
        outcome = self.registry._select_data_source({"database_id": "enterprise_sales"}, state, None)
        self.assertEqual(outcome.updates["database_id"], "enterprise_sales")
        payload = json.loads(outcome.content)
        self.assertIn("capability_gap", payload)
        self.assertIn("ProductCostHistory", payload["capability_gap"]["historical_cost"])
        self.assertEqual(outcome.updates["capability_gap"], payload["capability_gap"])

    def test_no_source_supports_capability_reports_gap_instead_of_faking_tables(self):
        registry = ToolRegistry(sql_skill=_FakeSkill(),
                                databases={"enterprise_sales": self.sales})
        outcome = registry._select_data_source({"database_id": "enterprise_sales"}, _state(), None)
        payload = json.loads(outcome.content)
        self.assertEqual(outcome.updates["database_id"], "enterprise_sales")
        self.assertTrue(payload["capability_gap"])
        self.assertIn("禁止编造", payload["capability_note"])


class ProbeToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.db = root / "sales.sqlite"
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE orders(order_id INTEGER PRIMARY KEY, order_date TEXT, "
                               "email TEXT, region TEXT)")
            connection.executemany("INSERT INTO orders VALUES (?,?,?,?)", [
                (1, "2025-01-02", "a@example.com", "East"),
                (2, "2026-12-31", "b@example.com", "West"),
                (3, "2025-06-01", "c@example.com", "East"),
            ])
        self.registry = ToolRegistry(sql_skill=_FakeSkill(),
                                     databases={"enterprise_sales": self.db})

    def tearDown(self):
        self.tmp.cleanup()

    def test_date_range_returns_reality_aware_complete_year(self):
        state = _state(database_id="enterprise_sales", question="最近一个完整自然年")
        outcome = self.registry._probe_database(
            {"probe": "date_range", "table": "orders", "column": "order_date"}, state, None)
        payload = json.loads(outcome.content)
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(payload["coverage"], "2025-01-02 → 2026-12-31")
        # 现实时间 2026 年内，MAX(order_date) 落在 2026-12-31 也不代表 2026 完整
        self.assertEqual(payload["last_complete_year"], date.today().year - 1)
        self.assertIn("不完整", payload["last_complete_year_reason"])

    def test_complete_year_helper_never_uses_current_year(self):
        year, reason = last_complete_year("2023-01-01", "2024-12-31", today=date(2024, 6, 1))
        self.assertEqual(year, 2023)
        self.assertIn("2023", reason)
        self.assertIsNone(last_complete_year("2026-02-01", "2026-12-31", today=date(2026, 11, 13))[0])

    def test_unknown_table_and_sensitive_column_are_rejected(self):
        state = _state(database_id="enterprise_sales")
        unknown = self.registry._probe_database(
            {"probe": "date_range", "table": "cost_history", "column": "start_date"}, state, None)
        self.assertEqual(unknown.status, "failed")
        self.assertIn("不存在表", unknown.error)
        sensitive = self.registry._probe_database(
            {"probe": "distinct_values", "table": "orders", "column": "email"}, state, None)
        self.assertEqual(sensitive.status, "failed")
        self.assertIn("敏感字段", sensitive.error)

    def test_repeated_probe_is_served_from_cache(self):
        state = _state(database_id="enterprise_sales")
        first = self.registry._probe_database(
            {"probe": "row_count", "table": "orders"}, state, None)
        cache = first.updates["probe_cache"]
        second = self.registry._probe_database(
            {"probe": "row_count", "table": "orders"}, _state(database_id="enterprise_sales", probe_cache=cache), None)
        self.assertTrue(json.loads(second.content)["cached"])


class SqlBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = _make_sales_like(Path(self.tmp.name) / "sales.sqlite")
        self.skill = _FakeSkill()
        self.registry = ToolRegistry(sql_skill=self.skill, databases={"enterprise_sales": self.db})

    def tearDown(self):
        self.tmp.cleanup()

    def test_metadata_probe_question_is_redirected_to_probe_tool(self):
        state = _state(question="查询订单表中order_date的最小值和最大值，确定数据覆盖的年份范围")
        outcome = self.registry._run_text_to_sql(
            {"question": "查询订单表中order_date的最小值和最大值，确定数据覆盖的年份范围", "intent": "query"},
            state, None)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("probe_database", outcome.error)
        self.assertEqual(self.skill.requests, [])

    def test_multiple_distinct_sql_allowed_up_to_limit(self):
        state = _state()
        for index in range(MAX_SQL_GENERATIONS_PER_TURN):
            outcome = self.registry._run_text_to_sql(
                {"question": f"第{index}个子问题：统计销售额", "intent": "query"}, state, None)
            self.assertEqual(outcome.status, "completed")
            state = {**state, **outcome.updates,
                     "sql_history": state["sql_history"] + outcome.updates["sql_history"]}
        blocked = self.registry._run_text_to_sql(
            {"question": "还有一个子问题：统计毛利", "intent": "query"}, state, None)
        self.assertEqual(blocked.status, "failed")
        self.assertIn("上限", blocked.error)
        self.assertEqual(len(self.skill.requests), MAX_SQL_GENERATIONS_PER_TURN)

    def test_repeated_sub_question_is_not_regenerated(self):
        state = _state()
        first = self.registry._run_text_to_sql({"question": "统计2025年销售额", "intent": "query"}, state, None)
        state = {**state, **first.updates, "sql_history": state["sql_history"] + first.updates["sql_history"]}
        again = self.registry._run_text_to_sql({"question": "统计 2025 年销售额！", "intent": "query"}, state, None)
        payload = json.loads(again.content)
        self.assertTrue(payload["repeated_sql"])
        self.assertEqual(len(self.skill.requests), 1)

    def test_question_key_ignores_spacing_and_punctuation(self):
        self.assertEqual(_question_key("统计 2025 年销售额！"), _question_key("统计2025年销售额"))


class PlanTest(unittest.TestCase):
    def test_complex_detection(self):
        self.assertTrue(is_complex_question(COMPLEX_QUESTION))
        self.assertFalse(is_complex_question("统计各商品类别销售额"))

    def test_plan_normalization_and_rendering(self):
        plan = normalize_plan({"goal": "经营分析", "metrics": "销售额", "steps": ["a", "b"]},
                              database_id="adventureworks")
        self.assertEqual(plan["database_id"], "adventureworks")
        self.assertEqual(plan["metrics"], ["销售额"])
        text = render_plan(plan)
        self.assertIn("经营分析", text)
        self.assertIn("1. a", text)


if __name__ == "__main__":
    unittest.main()


class _ScriptedModel:
    """LangChain-compatible double: planner and agent share one script."""

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


class EndToEndGraphTest(unittest.TestCase):
    """复杂问题：探查 → 计划 → 最终回答，全程离线。"""

    def test_probe_then_plan_then_answer(self):
        from langchain_core.messages import AIMessage

        from app.agent.graph import MAX_TOOL_ROUNDS

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = _make_sales_like(Path(tmp) / "sales.sqlite")
            model = _ScriptedModel([
                AIMessage(content="", tool_calls=[{
                    "id": "0", "name": "select_data_source",
                    "args": {"database_id": "enterprise_sales"}}]),
                AIMessage(content=json.dumps({
                    "goal": "经营分析", "metrics": ["销售额"], "dimensions": ["地区"],
                    "time_range": "待探查", "steps": ["探查", "生成SQL"], "expected_output": "表格"},
                    ensure_ascii=False)),
                AIMessage(content="", tool_calls=[{
                    "id": "1", "name": "probe_database",
                    "args": {"probe": "date_range", "table": "orders", "column": "order_date"}}]),
                AIMessage(content="已完成探查，但尚未生成业务 SQL。"),
            ])
            agent = DataAnalysisAgent(_FakeSkill(), workspace=Path(tmp) / "agent",
                                      databases={"enterprise_sales": db}, chat_model=model)
            result = agent.run({"question": COMPLEX_QUESTION, "database_id": "auto"})
            # 行为变化（C2）：该库只有当前成本、没有成本历史表，而问题明确要求
            # 历史销售成本。以前这种「能力缺失但仍给出说明」被标成 success，
            # 现在如实标 partial + capability_gap —— 这正是要修的假成功。
            self.assertEqual(result.status, "partial", result.error)
            self.assertEqual(result.termination_reason, "capability_gap")
            self.assertTrue(result.capability_gap)
            self.assertEqual([item["tool"] for item in result.tool_trace],
                             ["select_data_source", "probe_database"])
            self.assertEqual(result.analysis_plan["goal"], "经营分析")
            self.assertEqual(result.probe_results[0]["last_complete_year"], date.today().year - 1)
            # 探查结果不得冒充业务查询结果
            self.assertIsNone(result.sql)
            self.assertEqual(result.rows, [])
            # 3 次助手决策（选库/探查/回答）+ 1 次计划生成
            self.assertEqual(result.model_calls, 4)
            self.assertLessEqual(len(result.tool_trace), MAX_TOOL_ROUNDS)


class CompleteYearRegressionTest(unittest.TestCase):
    """真实回归里踩到的两个判定缺陷，防止回退。"""

    def test_truncated_year_is_not_complete(self):
        # AdventureWorks 真实覆盖 2022-05-30 → 2025-06-29：2025 只到 6 月，不算完整年
        year, reason = last_complete_year("2022-05-30 00:00:00.000", "2025-06-29 00:00:00.000",
                                          today=date(2026, 11, 13))
        self.assertEqual(year, 2024)
        self.assertIn("不完整", reason)

    def test_capability_requirement_overrides_business_domain_keyword(self):
        # 问题含「销售」「订单」等销售域词，但同时要求历史成本：
        # 模型选择 adventureworks 是正确的，不能被关键词启发式判成错误。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            sales = _make_sales_like(root / "sales.sqlite")
            history = _make_history_like(root / "history.sqlite")
            registry = ToolRegistry(sql_skill=_FakeSkill(),
                                    databases={"enterprise_sales": sales,
                                               "adventureworks": history})
            outcome = registry._select_data_source({"database_id": "adventureworks"}, _state(), None)
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(outcome.updates["database_id"], "adventureworks")
