"""C5：知识图从「证据文本生成器」变成「Agent 可查询、可校验的状态」。

三件事一起验证：
1. 图资源进程级复用（同一份产物只建一次，产物变了自动失效）；
2. 模型可以主动调用 `inspect_join_path` 查真实外键路径与风险；
3. 最终 SQL 的 FROM/JOIN 与图对账，不一致时写进结果元数据。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from app.agent.join_guard import extract_tables, validate_sql_joins
from app.agent.service import DataAnalysisAgent
from app.agent.tools.registry import ToolRegistry
from app.knowledge.graph_store import DEFAULT_ARTIFACTS, GraphStore, clear_store_cache


class GraphStoreCacheTest(unittest.TestCase):

    def setUp(self):
        clear_store_cache()

    def tearDown(self):
        clear_store_cache()

    def test_same_artifacts_return_the_same_object(self):
        first = GraphStore.from_artifacts(DEFAULT_ARTIFACTS)
        second = GraphStore.from_artifacts(DEFAULT_ARTIFACTS)
        self.assertIs(first, second)

    def test_cache_can_be_bypassed(self):
        first = GraphStore.from_artifacts(DEFAULT_ARTIFACTS)
        uncached = GraphStore.from_artifacts(DEFAULT_ARTIFACTS, use_cache=False)
        self.assertIsNot(first, uncached)

    def test_rebuilt_artifacts_invalidate_the_cache(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "knowledge"
            shutil.copytree(DEFAULT_ARTIFACTS, target)
            first = GraphStore.from_artifacts(target)
            again = GraphStore.from_artifacts(target)
            self.assertIs(first, again)
            # 模拟重跑 build-knowledge：改写产物并推进 mtime
            snapshot = target / "snapshot.json"
            payload = json.loads(snapshot.read_text(encoding="utf-8"))
            payload["rebuilt_marker"] = "v2"
            snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            future = time.time() + 5
            os.utime(snapshot, (future, future))
            rebuilt = GraphStore.from_artifacts(target)

        self.assertIsNot(first, rebuilt)


class JoinGuardTest(unittest.TestCase):

    def test_extract_tables_ignores_cte_names(self):
        sql = ('WITH per_state AS (SELECT StateProvinceID FROM "Sales.SalesOrderHeader") '
               'SELECT * FROM per_state JOIN "Sales.SalesTerritory" t ON t.TerritoryID = 1')
        tables = extract_tables(sql)
        self.assertIn("Sales.SalesOrderHeader", tables)
        self.assertIn("Sales.SalesTerritory", tables)
        self.assertNotIn("per_state", tables)

    def test_unexpected_table_is_reported(self):
        context = {"paths": [{"tables": ["SalesOrderHeader", "SalesOrderDetail"],
                              "join_conditions": []}]}
        sql = ('SELECT * FROM "Sales.SalesOrderHeader" h '
               'JOIN "Sales.SalesOrderDetail" d ON d.SalesOrderID = h.SalesOrderID '
               'JOIN "Person.Address" a ON a.AddressID = 1')
        warnings = validate_sql_joins(sql, context)
        self.assertEqual([item["kind"] for item in warnings], ["unexpected_table"])
        self.assertEqual(warnings[0]["tables"], ["Address"])

    def test_composite_fk_missing_column_is_reported(self):
        context = {"paths": [{
            "tables": ["SalesOrderDetail", "SpecialOfferProduct"],
            "join_conditions": [{
                "relation_id": "fk_special_offer_product", "composite": True,
                "source_columns": ["SpecialOfferID", "ProductID"],
                "target_columns": ["SpecialOfferID", "ProductID"]}],
        }]}
        sql = ('SELECT * FROM "Sales.SalesOrderDetail" d '
               'JOIN "Sales.SpecialOfferProduct" s ON s.ProductID = d.ProductID')
        warnings = validate_sql_joins(sql, context)
        self.assertEqual([item["kind"] for item in warnings], ["composite_fk_incomplete"])
        self.assertIn("SpecialOfferID", warnings[0]["missing_columns"])

    def test_no_context_means_no_warnings(self):
        self.assertEqual(validate_sql_joins('SELECT 1 FROM "Sales.SalesOrderHeader"', {}), [])


def _make_db(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders(order_id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO orders VALUES (1)")
    return path


class JoinPathToolTest(unittest.TestCase):

    def _registry(self, tmp: str) -> ToolRegistry:
        return ToolRegistry(sql_skill=SimpleNamespace(run=lambda *a, **k: None),
                            databases={"adventureworks": _make_db(Path(tmp) / "aw.sqlite")})

    def test_tool_returns_real_paths_and_risks(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            registry = self._registry(tmp)
            state = {"turn_id": "t1", "database_id": "adventureworks"}
            outcome = registry.execute(
                "inspect_join_path",
                {"tables": ["SalesOrderHeader", "SalesOrderDetail"]}, state)

        self.assertEqual(outcome.status, "completed")
        payload = json.loads(outcome.content)
        self.assertTrue(payload["paths"])
        self.assertEqual(payload["paths"][0]["tables"][0], "SalesOrderHeader")
        kinds = {item["kind"] for item in payload["risks"]}
        self.assertIn("duplicate_aggregation", kinds)
        # 状态里只存引用与结论，不存图对象
        context = outcome.updates["join_path_context"]
        self.assertEqual(context["turn_id"], "t1")
        self.assertNotIn("graph", context)

    def test_same_table_set_is_served_from_cache(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            registry = self._registry(tmp)
            state = {"turn_id": "t1", "database_id": "adventureworks"}
            first = registry.execute("inspect_join_path",
                                     {"tables": ["SalesOrderHeader", "SalesOrderDetail"]}, state)
            second_state = {"turn_id": "t1", "database_id": "adventureworks",
                            "join_path_cache": first.updates["join_path_cache"]}
            second = registry.execute("inspect_join_path",
                                      {"tables": ["SalesOrderDetail", "SalesOrderHeader"]},
                                      second_state)

        self.assertTrue(json.loads(second.content)["cached"])

    def test_other_databases_are_rejected_honestly(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            registry = ToolRegistry(sql_skill=SimpleNamespace(run=lambda *a, **k: None),
                                    databases={"enterprise_sales": _make_db(Path(tmp) / "s.sqlite")})
            outcome = registry.execute("inspect_join_path",
                                       {"tables": ["orders", "products"]},
                                       {"turn_id": "t1", "database_id": "enterprise_sales"})

        self.assertEqual(outcome.status, "failed")
        self.assertIn("adventureworks", outcome.error)


class _SkillWithSql:
    def __init__(self, sql: str):
        self.sql = sql
        self.calls = 0

    def run(self, request, on_stage=None):
        self.calls += 1
        return SimpleNamespace(model_dump=lambda: {
            "request_id": "req-1", "status": "success", "sql": self.sql,
            "columns": ["c"], "rows": [[1]], "truncated": False, "elapsed_seconds": 0.01,
            "stage_seconds": {}, "usage": {}, "failed_stage": None, "error": None,
            "artifacts_dir": "tmp"})


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


class GraphDrivenSqlCheckTest(unittest.TestCase):
    """模型先查图、再写 SQL；SQL 绕开图里声明的表时必须留下记录。"""

    def test_sql_outside_the_checked_path_is_flagged(self):
        sql = ('SELECT * FROM "Sales.SalesOrderHeader" h '
               'JOIN "Sales.SalesOrderDetail" d ON d.SalesOrderID = h.SalesOrderID '
               'JOIN "Person.Address" a ON a.AddressID = 1')
        model = _ScriptedModel([
            AIMessage(content="", tool_calls=[{
                "id": "s", "name": "inspect_database_schema", "args": {"table_names": []}}]),
            AIMessage(content="", tool_calls=[{
                "id": "0", "name": "inspect_join_path",
                "args": {"tables": ["SalesOrderHeader", "SalesOrderDetail"]}}]),
            AIMessage(content="", tool_calls=[{
                "id": "1", "name": "run_text_to_sql",
                "args": {"question": "各州销售额", "intent": "query"}}]),
            AIMessage(content="完成"),
        ])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            agent = DataAnalysisAgent(_SkillWithSql(sql), workspace=Path(tmp) / "agent",
                                     databases={"adventureworks": _make_db(Path(tmp) / "aw.sqlite")},
                                     chat_model=model)
            result = agent.run({"question": "各州销售额", "database_id": "adventureworks"})

        self.assertEqual(result.status, "success", result.error)
        self.assertEqual(result.analyses[0]["join_path_warnings"][0]["kind"], "unexpected_table")
        self.assertEqual([item["tool"] for item in result.tool_trace],
                         ["inspect_database_schema", "inspect_join_path", "run_text_to_sql"])

    def test_consistent_sql_has_no_warnings(self):
        sql = ('SELECT * FROM "Sales.SalesOrderHeader" h '
               'JOIN "Sales.SalesOrderDetail" d ON d.SalesOrderID = h.SalesOrderID')
        model = _ScriptedModel([
            AIMessage(content="", tool_calls=[{
                "id": "s", "name": "inspect_database_schema", "args": {"table_names": []}}]),
            AIMessage(content="", tool_calls=[{
                "id": "0", "name": "inspect_join_path",
                "args": {"tables": ["SalesOrderHeader", "SalesOrderDetail"]}}]),
            AIMessage(content="", tool_calls=[{
                "id": "1", "name": "run_text_to_sql",
                "args": {"question": "订单明细", "intent": "query"}}]),
            AIMessage(content="完成"),
        ])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            agent = DataAnalysisAgent(_SkillWithSql(sql), workspace=Path(tmp) / "agent",
                                     databases={"adventureworks": _make_db(Path(tmp) / "aw.sqlite")},
                                     chat_model=model)
            result = agent.run({"question": "订单明细", "database_id": "adventureworks"})

        self.assertEqual(result.analyses[0]["join_path_warnings"], [])


if __name__ == "__main__":
    unittest.main()
