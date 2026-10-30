from pathlib import Path
import json
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from langchain_core.messages import AIMessage

from app.agent import ChatRequest, DataAnalysisAgent
from app.agent.visualization import build_visualization


class FakeSQLSkill:
    def __init__(self):
        self.requests = []

    def run(self, request, on_stage=None):
        self.requests.append(dict(request))
        if on_stage:
            on_stage({"stage": "sql_generation", "status": "completed", "seconds": 0.01})
        payload = {
            "request_id": "sql001", "status": "success", "sql": "SELECT category, revenue FROM sales",
            "columns": ["category", "revenue"], "rows": [["智能设备", 120], ["办公用品", 80]],
            "truncated": False, "elapsed_seconds": 0.001, "stage_seconds": {}, "usage": {},
            "failed_stage": None, "error": None, "artifacts_dir": str(Path(tempfile.gettempdir()) / "sql001"),
        }
        return SimpleNamespace(model_dump=lambda: payload)


class ScriptedChatModel:
    """Small LangChain-compatible model double; no provider calls in tests."""

    def __init__(self, messages):
        self.responses = iter(messages)
        self.seen_messages = []
        self.bound_tools = []

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self

    def invoke(self, messages, config=None):
        self.seen_messages.append(messages)
        return next(self.responses)


class AgentTest(unittest.TestCase):
    @staticmethod
    def _database(path: Path) -> Path:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS sales(category TEXT, revenue REAL)")
        return path

    def test_chat_request_keeps_question_as_a_compatible_alias(self):
        request = ChatRequest.model_validate({"question": "查销售额", "database_id": "demo"})
        self.assertEqual(request.message, "查销售额")
        self.assertEqual(request.as_runtime_request()["question"], "查销售额")
        hinted = ChatRequest.model_validate({"question": "查销售额", "database_id": "demo",
                                             "evidence": "用户说明"})
        self.assertEqual(hinted.user_hint, "用户说明")
        self.assertEqual(ChatRequest.model_validate({"question": "自动识别"}).database_id, "auto")

    def test_chart_rules_cover_metric_bar_line_and_pie(self):
        self.assertEqual(build_visualization("query", ["count"], [[3]])["type"], "metric")
        self.assertEqual(build_visualization("ranking", ["name", "amount"], [["A", 3], ["B", 2]])["type"], "bar")
        self.assertEqual(build_visualization("trend", ["month", "amount"], [["01", 3], ["02", 4]])["type"], "line")
        self.assertEqual(build_visualization("proportion", ["name", "amount"], [["A", 3], ["B", 2]])["type"], "pie")

    def test_table_visualization_is_self_contained(self):
        columns = ["department", "status"]
        rows = [["研发部", "active"], ["销售部", "active"]]
        table = build_visualization("query", columns, rows)
        self.assertEqual(table, {
            "type": "table", "title": "查询结果",
            "columns": columns, "rows": rows, "row_count": 2,
        })

        explicit = build_visualization("query", ["department", "headcount"],
                                       [["研发部", 20], ["销售部", 18]], "table")
        self.assertEqual((explicit["columns"], explicit["rows"]),
                         (["department", "headcount"], [["研发部", 20], ["销售部", 18]]))

    def test_explicit_chart_type_overrides_and_falls_back(self):
        columns = ["分类", "销售额"]
        three = [["智能设备", 120], ["办公用品", 80], ["家居生活", 60]]
        twelve = [[f"分类{i}", i] for i in range(12)]
        self.assertEqual(build_visualization("proportion", columns, three)["type"], "pie")
        # 用户点名要柱状图，就按点名的画。
        self.assertEqual(build_visualization("proportion", columns, three, "bar")["type"], "bar")
        # 未知类型、以及数据撑不起的形状，一律退回按意图选定的图，不画残图。
        self.assertEqual(build_visualization("proportion", columns, three, "donut")["type"], "pie")
        self.assertEqual(build_visualization("proportion", columns, three, "metric")["type"], "pie")
        self.assertEqual(build_visualization("ranking", columns, twelve, "pie")["type"], "bar")
        self.assertEqual(build_visualization("query", columns, three, "table")["type"], "table")

    def test_visualization_tool_tells_the_model_when_it_fell_back(self):
        # 回退必须可见，否则模型会以为用户拿到了饼图。
        from app.agent.tools import ToolRegistry

        with tempfile.TemporaryDirectory() as tmp:
            toolbox = ToolRegistry(FakeSQLSkill(), {"enterprise_sales": Path(tmp) / "demo.sqlite"})
            state = {"turn_id": "turn-1", "database_id": "enterprise_sales",
                     "sql_result_database_id": "enterprise_sales",
                     "analysis_intent": "ranking",
                     "sql_result": {"status": "success", "columns": ["分类", "销售额"],
                                    "rows": [[f"分类{i}", i] for i in range(12)]}}
            outcome = toolbox.execute("build_visualization", {"intent": "ranking", "chart_type": "pie"}, state)
            payload = json.loads(outcome.content)
            self.assertEqual(payload["visualization"]["type"], "bar")
            self.assertEqual(payload["chart_type_applied"], "bar")
            self.assertIn("note", payload)
            schema = next(item for item in toolbox.as_langchain_tools()
                          if item.name == "build_visualization").tool_call_schema.model_json_schema()
            self.assertIn("chart_type", schema["properties"])
            self.assertNotIn("intent", schema["properties"])
            self.assertNotIn("required", schema)

    def test_prompt_keeps_prose_out_of_the_results_table(self):
        from app.agent.prompts import build_system_prompt

        prompt = build_system_prompt("demo", 5)
        self.assertIn("不要再输出 Markdown 表格", prompt)
        self.assertIn("chart_type", prompt)
        self.assertIn("复用上一轮SQL结果", prompt)
        self.assertIn("每个新的数据分析问题都先调用 search_business_metrics", prompt)
        self.assertIn("退款率、转化率、增长率", prompt)
        self.assertIn("要求结果升降序不属于排名", prompt)

    def test_standard_tools_hide_runtime_dependencies_from_the_model(self):
        from app.agent.tools import ToolRegistry

        toolbox = ToolRegistry(FakeSQLSkill(), {"enterprise_sales": Path("demo.sqlite")})
        schemas = {item.name: set(item.args) for item in toolbox.as_langchain_tools()}
        self.assertEqual(schemas["run_text_to_sql"], {"question", "intent"})
        self.assertEqual(schemas["inspect_database_schema"], {"table_names"})
        self.assertEqual(schemas["select_data_source"], {"database_id"})
        for fields in schemas.values():
            self.assertNotIn("state", fields)
            self.assertNotIn("config", fields)
            self.assertNotIn("database_path", fields)

    def test_llm_driven_agent_calls_sql_and_visualization_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = self._database(Path(tmp) / "demo.sqlite")
            model = ScriptedChatModel([
                AIMessage(content="", tool_calls=[{"id": "1", "name": "search_business_metrics",
                                                    "args": {"query": "销售额占比"}}]),
                AIMessage(content="", tool_calls=[{"id": "2", "name": "inspect_database_schema",
                                                    "args": {}}]),
                AIMessage(content="", tool_calls=[{"id": "3", "name": "run_text_to_sql",
                                                    "args": {"question": "各分类销售额占比",
                                                             "intent": "proportion"}}]),
                AIMessage(content="", tool_calls=[{"id": "4", "name": "build_visualization",
                                                    "args": {}}]),
                AIMessage(content="智能设备贡献最高。"),
            ])
            skill = FakeSQLSkill()
            agent = DataAnalysisAgent(skill, workspace=Path(tmp),
                                      databases={"enterprise_sales": database}, chat_model=model)
            data = agent.run({"question": "各分类销售额占比", "database_id": "enterprise_sales",
                              "conversation_id": "thread-data", "evidence": "财务确认口径"})
            self.assertEqual((data.intent, data.visualization["type"]), ("proportion", "pie"))
            self.assertEqual(data.business_context[0]["name"], "销售额")
            self.assertEqual([item["tool"] for item in data.tool_trace],
                             ["search_business_metrics", "inspect_database_schema",
                              "run_text_to_sql", "build_visualization"])
            self.assertTrue(all("turn_id" not in item for item in data.tool_trace))
            self.assertEqual(data.model_calls, 5)
            self.assertEqual(data.answer, "智能设备贡献最高。")
            self.assertIn("[系统业务口径]", skill.requests[0]["evidence"])
            self.assertIn("[用户补充说明，未经系统验证]", skill.requests[0]["evidence"])

    def test_agent_reports_its_own_wall_clock_not_only_the_skills(self):
        # elapsed_seconds is the SQL Skill stopwatch; Planner turns and tool
        # orchestration ran unreported, so a 65s answer really took longer.
        with tempfile.TemporaryDirectory() as tmp:
            database = self._database(Path(tmp) / "demo.sqlite")
            model = ScriptedChatModel([
                AIMessage(content="", tool_calls=[{"id": "1", "name": "inspect_database_schema",
                                                    "args": {}}]),
                AIMessage(content="", tool_calls=[{"id": "2", "name": "run_text_to_sql",
                                                    "args": {"question": "各分类销售额占比",
                                                             "intent": "proportion"}}]),
                AIMessage(content="完成"),
            ])
            agent = DataAnalysisAgent(FakeSQLSkill(), workspace=Path(tmp),
                                      databases={"enterprise_sales": database}, chat_model=model)
            data = agent.run({"question": "各分类销售额占比", "database_id": "enterprise_sales"})
            self.assertGreater(data.agent_elapsed_seconds, 0)
            self.assertGreaterEqual(data.agent_elapsed_seconds, data.elapsed_seconds)
            self.assertIn("agent_orchestration", data.stage_seconds)
            self.assertAlmostEqual(
                data.stage_seconds["agent_orchestration"],
                round(data.agent_elapsed_seconds - data.elapsed_seconds, 3), places=2)

    def test_orchestration_stage_emits_started_and_completed_events(self):
        """「规划与编排」以前只有结果里的秒数、没有进度事件，前端那格永远是灰的。"""
        with tempfile.TemporaryDirectory() as tmp:
            database = self._database(Path(tmp) / "demo.sqlite")
            model = ScriptedChatModel([
                AIMessage(content="", tool_calls=[{"id": "1", "name": "inspect_database_schema",
                                                    "args": {}}]),
                AIMessage(content="", tool_calls=[{"id": "2", "name": "run_text_to_sql",
                                                    "args": {"question": "各分类销售额占比",
                                                             "intent": "proportion"}}]),
                AIMessage(content="完成"),
            ])
            agent = DataAnalysisAgent(FakeSQLSkill(), workspace=Path(tmp),
                                      databases={"enterprise_sales": database}, chat_model=model)
            events = []
            agent.run({"question": "各分类销售额占比", "database_id": "enterprise_sales"},
                      on_stage=events.append)
            orchestration = [event for event in events if event["stage"] == "agent_orchestration"]
            self.assertEqual([event["status"] for event in orchestration], ["started", "completed"])
            self.assertGreater(orchestration[-1]["seconds"], 0)
            # 收尾事件必须是最后一条，否则进度条会停在半路
            self.assertEqual(events[-1]["stage"], "agent_orchestration")
            self.assertEqual(events[-1]["status"], "completed")

    def test_chat_turn_still_closes_the_orchestration_stage(self):
        """没有 SQL 的闲聊回合也要收尾，否则那一格永远不亮。"""
        with tempfile.TemporaryDirectory() as tmp:
            agent = DataAnalysisAgent(FakeSQLSkill(), workspace=Path(tmp),
                                      databases={"enterprise_sales": Path(tmp) / "demo.sqlite"},
                                      chat_model=ScriptedChatModel([AIMessage(content="你好")]))
            events = []
            data = agent.run({"question": "你好", "database_id": "enterprise_sales"},
                             on_stage=events.append)
            self.assertEqual(data.intent, "chat")
            self.assertEqual(events[-1]["stage"], "agent_orchestration")
            self.assertEqual(events[-1]["status"], "completed")
            self.assertIn("agent_orchestration", data.stage_seconds)

    def test_conversation_memory_is_preserved_by_thread_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = ScriptedChatModel([AIMessage(content="第一轮"), AIMessage(content="第二轮")])
            agent = DataAnalysisAgent(FakeSQLSkill(), workspace=Path(tmp),
                                      databases={"enterprise_sales": Path(tmp) / "demo.sqlite"}, chat_model=model)
            first = agent.run({"question": "你好", "database_id": "enterprise_sales", "conversation_id": "same"})
            second = agent.run({"question": "继续", "database_id": "enterprise_sales", "conversation_id": "same"})
            self.assertEqual((first.answer, second.answer), ("第一轮", "第二轮"))
            self.assertGreater(len(model.seen_messages[1]), len(model.seen_messages[0]))

    def test_auto_mode_chart_followup_reuses_previous_query_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = self._database(Path(tmp) / "demo.sqlite")
            model = ScriptedChatModel([
                AIMessage(content="", tool_calls=[{"id": "1", "name": "select_data_source",
                                                    "args": {"database_id": "enterprise_hr"}}]),
                AIMessage(content="", tool_calls=[{"id": "2", "name": "inspect_database_schema",
                                                    "args": {}}]),
                AIMessage(content="", tool_calls=[{"id": "3", "name": "run_text_to_sql",
                                                    "args": {"question": "各部门在职人数",
                                                             "intent": "query"}}]),
                AIMessage(content="", tool_calls=[{"id": "4", "name": "build_visualization",
                                                    "args": {}}]),
                AIMessage(content="已完成查询。"),
                # Even if the Planner redundantly selects the same source in
                # auto mode, a chart-only follow-up must retain the prior rows.
                AIMessage(content="", tool_calls=[{"id": "5", "name": "select_data_source",
                                                    "args": {"database_id": "enterprise_hr"}}]),
                AIMessage(content="", tool_calls=[{"id": "6", "name": "build_visualization",
                                                    "args": {"chart_type": "bar"}}]),
                AIMessage(content="已改成柱状图。"),
            ])
            skill = FakeSQLSkill()
            agent = DataAnalysisAgent(skill, workspace=Path(tmp), databases={
                "enterprise_sales": database,
                "enterprise_hr": database,
            }, chat_model=model)
            first = agent.run({"question": "各部门在职人数是多少？", "database_id": "auto",
                               "conversation_id": "auto-chart-followup"})
            second = agent.run({"question": "做成柱状图", "database_id": "auto",
                                "conversation_id": "auto-chart-followup"})
            self.assertEqual((first.database_id, second.database_id),
                             ("enterprise_hr", "enterprise_hr"))
            self.assertEqual(second.visualization["type"], "bar")
            self.assertEqual(second.rows, [["智能设备", 120], ["办公用品", 80]])
            self.assertEqual([item["tool"] for item in second.tool_trace],
                             ["select_data_source", "build_visualization"])
            self.assertEqual(len(skill.requests), 1)

    def test_same_conversation_is_isolated_by_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = ScriptedChatModel([AIMessage(content="A"), AIMessage(content="B")])
            agent = DataAnalysisAgent(FakeSQLSkill(), workspace=Path(tmp), databases={
                "db_a": self._database(Path(tmp) / "a.sqlite"),
                "db_b": self._database(Path(tmp) / "b.sqlite"),
            }, chat_model=model)
            agent.run({"question": "第一问", "database_id": "db_a", "conversation_id": "same"})
            agent.run({"question": "第二问", "database_id": "db_b", "conversation_id": "same"})
            self.assertEqual(len(model.seen_messages[0]), len(model.seen_messages[1]))

    def test_tool_failure_is_not_reported_as_successful_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = ScriptedChatModel([
                AIMessage(content="", tool_calls=[{"id": "1", "name": "build_visualization", "args": {}}]),
                AIMessage(content="当前没有可画图的数据。"),
            ])
            agent = DataAnalysisAgent(FakeSQLSkill(), workspace=Path(tmp),
                                      databases={"enterprise_sales": self._database(Path(tmp) / "demo.sqlite")},
                                      chat_model=model)
            result = agent.run({"question": "画图", "database_id": "enterprise_sales"})
            self.assertEqual(result.status, "error")
            self.assertEqual(result.tool_trace[0]["status"], "failed")

    def test_tool_round_limit_closes_pending_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            responses = [AIMessage(content="", tool_calls=[{
                "id": str(index), "name": "get_capabilities", "args": {}}]) for index in range(7)]
            agent = DataAnalysisAgent(FakeSQLSkill(), workspace=Path(tmp),
                                      databases={"enterprise_sales": self._database(Path(tmp) / "demo.sqlite")},
                                      chat_model=ScriptedChatModel(responses))
            result = agent.run({"question": "反复调用工具", "database_id": "enterprise_sales"})
            self.assertEqual(result.status, "error")
            self.assertIn("工具调用上限", result.error)
            self.assertEqual(len(result.tool_trace), 6)
            self.assertIn("安全停止", result.answer)


if __name__ == "__main__":
    unittest.main()
