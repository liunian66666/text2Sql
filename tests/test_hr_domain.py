from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from langchain_core.messages import AIMessage

from app.agent import DataAnalysisAgent
from app.agent.tools import ToolRegistry
from app.enterprise import EnterpriseKnowledgeBase, ensure_enterprise_hr_database
from tests.test_agent import FakeSQLSkill, ScriptedChatModel


class HRDatabaseTest(unittest.TestCase):
    def test_hr_database_is_reproducible_relational_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = ensure_enterprise_hr_database(root, force=True)
            with closing(sqlite3.connect(first)) as connection:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                counts1 = {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                           for table in tables}
                employee_rows1 = connection.execute(
                    "SELECT employee_id, department_id, hire_date, termination_date FROM employees ORDER BY employee_id").fetchall()
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM employees WHERE termination_date IS NOT NULL "
                    "AND julianday(termination_date) < julianday(hire_date)").fetchone()[0], 0)
            ensure_enterprise_hr_database(root, force=True)
            with closing(sqlite3.connect(first)) as connection:
                employee_rows2 = connection.execute(
                    "SELECT employee_id, department_id, hire_date, termination_date FROM employees ORDER BY employee_id").fetchall()
            self.assertEqual(tables, {"departments", "employees", "attendance", "performance_reviews"})
            self.assertEqual(counts1["employees"], 180)
            self.assertGreaterEqual(sum(counts1.values()), 500)
            self.assertLessEqual(sum(counts1.values()), 2000)
            self.assertEqual(employee_rows1, employee_rows2)


class HRRoutingTest(unittest.TestCase):
    def test_explicit_sales_and_hr_questions_are_guarded(self):
        self.assertEqual(ToolRegistry.explicit_question_domain("各部门在职员工人数"), "enterprise_hr")
        self.assertEqual(ToolRegistry.explicit_question_domain("各渠道销售额"), "enterprise_sales")
        registry = ToolRegistry(FakeSQLSkill(), {
            "enterprise_sales": Path("sales.sqlite"), "enterprise_hr": Path("hr.sqlite")})
        state = {"turn_id": "t1", "question": "各部门在职员工人数", "database_id": "auto",
                 "requested_database_id": "auto", "request": {"database_id": "auto"}}
        wrong = registry.execute("select_data_source", {"database_id": "enterprise_sales"}, state)
        right = registry.execute("select_data_source", {"database_id": "enterprise_hr"}, state)
        self.assertEqual(wrong.status, "failed")
        self.assertEqual(right.updates["database_id"], "enterprise_hr")
        self.assertEqual(right.updates["request"]["database_id"], "enterprise_hr")

    def test_sort_order_alone_is_not_ranking(self):
        self.assertEqual(ToolRegistry.normalize_intent("各部门人数按降序", "ranking"), "query")
        self.assertEqual(ToolRegistry.normalize_intent("哪些部门绩效最高", "ranking"), "ranking")

    def test_manual_selection_has_priority_and_cannot_be_switched(self):
        registry = ToolRegistry(FakeSQLSkill(), {
            "enterprise_sales": Path("sales.sqlite"), "enterprise_hr": Path("hr.sqlite")})
        state = {"turn_id": "t1", "question": "员工人数", "database_id": "enterprise_sales",
                 "requested_database_id": "enterprise_sales", "request": {"database_id": "enterprise_sales"}}
        outcome = registry.execute("select_data_source", {"database_id": "enterprise_hr"}, state)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("手动指定", outcome.error)

    def test_auto_selected_hr_uses_the_existing_agent_tool_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hr = ensure_enterprise_hr_database(root)
            sales = root / "sales.sqlite"
            with closing(sqlite3.connect(sales)) as connection:
                connection.execute("CREATE TABLE orders(order_id INTEGER)")
            model = ScriptedChatModel([
                AIMessage(content="", tool_calls=[{"id": "1", "name": "select_data_source",
                                                    "args": {"database_id": "enterprise_hr"}}]),
                AIMessage(content="", tool_calls=[{"id": "2", "name": "search_business_metrics",
                                                    "args": {"query": "各部门在职员工人数"}}]),
                AIMessage(content="", tool_calls=[{"id": "3", "name": "inspect_database_schema",
                                                    "args": {"table_names": ["departments", "employees"]}}]),
                AIMessage(content="", tool_calls=[{"id": "4", "name": "run_text_to_sql",
                                                    "args": {"question": "各部门在职员工人数", "intent": "query"}}]),
                AIMessage(content="", tool_calls=[{"id": "5", "name": "build_visualization", "args": {}}]),
                AIMessage(content="已完成HR分析。"),
            ])
            agent = DataAnalysisAgent(FakeSQLSkill(), workspace=root / "artifacts",
                                      databases={"enterprise_sales": sales, "enterprise_hr": hr},
                                      chat_model=model, knowledge=EnterpriseKnowledgeBase(mode="keyword"))
            result = agent.run({"question": "各部门在职员工人数", "database_id": "auto"})
            self.assertEqual(result.database_id, "enterprise_hr")
            self.assertEqual(result.business_context[0]["database_id"], "enterprise_hr")
            self.assertEqual([row["tool"] for row in result.tool_trace], [
                "select_data_source", "search_business_metrics", "inspect_database_schema",
                "run_text_to_sql", "build_visualization"])


if __name__ == "__main__":
    unittest.main()
