from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from app.agent import DataAnalysisAgent
from app.agent.brain import AgentDecision, OpenAIToolCallingBrain, ToolCall
from app.agent.visualization import build_visualization


class FakeSQLSkill:
    def run(self, request, on_stage=None):
        if on_stage:
            on_stage({"stage": "sql_generation", "status": "completed", "seconds": 0.01})
        payload = {
            "request_id": "sql001", "status": "success", "sql": "SELECT category, revenue FROM sales",
            "columns": ["category", "revenue"], "rows": [["智能设备", 120], ["办公用品", 80]],
            "truncated": False, "elapsed_seconds": 0.001, "stage_seconds": {}, "usage": {},
            "failed_stage": None, "error": None, "artifacts_dir": str(Path(tempfile.gettempdir()) / "sql001"),
        }
        return SimpleNamespace(model_dump=lambda: payload)


class ScriptedBrain:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.seen_messages = []

    def decide(self, *, messages, tools, system_prompt):
        self.seen_messages.append(messages)
        return next(self.decisions)


class AgentTest(unittest.TestCase):
    def test_json_react_protocol_parser(self):
        payload = OpenAIToolCallingBrain._parse_json_object(
            '```json\n{"tool_calls":[{"name":"get_capabilities","arguments":{}}],"answer":""}\n```')
        self.assertEqual(payload["tool_calls"][0]["name"], "get_capabilities")

    def test_chart_rules_cover_metric_bar_line_and_pie(self):
        self.assertEqual(build_visualization("query", ["count"], [[3]])["type"], "metric")
        self.assertEqual(build_visualization("ranking", ["name", "amount"], [["A", 3], ["B", 2]])["type"], "bar")
        self.assertEqual(build_visualization("trend", ["month", "amount"], [["01", 3], ["02", 4]])["type"], "line")
        self.assertEqual(build_visualization("proportion", ["name", "amount"], [["A", 3], ["B", 2]])["type"], "pie")

    def test_llm_driven_agent_calls_sql_and_visualization_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = ScriptedBrain([
                AgentDecision(tool_calls=[ToolCall("1", "search_business_metrics", {"query": "销售额占比"})]),
                AgentDecision(tool_calls=[ToolCall("2", "run_text_to_sql", {"question": "各分类销售额占比"})]),
                AgentDecision(tool_calls=[ToolCall("3", "build_visualization", {"intent": "proportion"})]),
                AgentDecision(content="智能设备贡献最高。"),
            ])
            agent = DataAnalysisAgent(FakeSQLSkill(), workspace=Path(tmp),
                                      databases={"enterprise_sales": Path(tmp) / "demo.sqlite"}, brain=brain)
            data = agent.run({"question": "各分类销售额占比", "database_id": "enterprise_sales",
                              "conversation_id": "thread-data"})
            self.assertEqual((data.intent, data.visualization["type"]), ("proportion", "pie"))
            self.assertEqual(data.business_context[0]["name"], "销售额")
            self.assertEqual([item["tool"] for item in data.tool_trace],
                             ["search_business_metrics", "run_text_to_sql", "build_visualization"])
            self.assertEqual(data.model_calls, 4)
            self.assertEqual(data.answer, "智能设备贡献最高。")

    def test_agent_reports_its_own_wall_clock_not_only_the_skills(self):
        # elapsed_seconds is the SQL Skill stopwatch; Planner turns and tool
        # orchestration ran unreported, so a 65s answer really took longer.
        with tempfile.TemporaryDirectory() as tmp:
            brain = ScriptedBrain([
                AgentDecision(tool_calls=[ToolCall("1", "run_text_to_sql", {"question": "各分类销售额占比"})]),
                AgentDecision(content="完成"),
            ])
            agent = DataAnalysisAgent(FakeSQLSkill(), workspace=Path(tmp),
                                      databases={"enterprise_sales": Path(tmp) / "demo.sqlite"}, brain=brain)
            data = agent.run({"question": "各分类销售额占比", "database_id": "enterprise_sales"})
            self.assertGreater(data.agent_elapsed_seconds, 0)
            self.assertGreaterEqual(data.agent_elapsed_seconds, data.elapsed_seconds)
            self.assertIn("agent_orchestration", data.stage_seconds)
            self.assertAlmostEqual(
                data.stage_seconds["agent_orchestration"],
                round(data.agent_elapsed_seconds - data.elapsed_seconds, 3), places=2)

    def test_conversation_memory_is_preserved_by_thread_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = ScriptedBrain([AgentDecision(content="第一轮"), AgentDecision(content="第二轮")])
            agent = DataAnalysisAgent(FakeSQLSkill(), workspace=Path(tmp),
                                      databases={"enterprise_sales": Path(tmp) / "demo.sqlite"}, brain=brain)
            first = agent.run({"question": "你好", "database_id": "enterprise_sales", "conversation_id": "same"})
            second = agent.run({"question": "继续", "database_id": "enterprise_sales", "conversation_id": "same"})
            self.assertEqual((first.answer, second.answer), ("第一轮", "第二轮"))
            self.assertGreater(len(brain.seen_messages[1]), len(brain.seen_messages[0]))


if __name__ == "__main__":
    unittest.main()
