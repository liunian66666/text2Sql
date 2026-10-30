import unittest

from runner.evaluate_agent import score_agent_case, validate_agent_cases


class AgentEvaluationTest(unittest.TestCase):
    def test_case_validation_rejects_duplicate_ids(self):
        cases = [{"id": "same", "question": "q", "database_id": "db", "expected_intent": "chat"}] * 2
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            validate_agent_cases(cases)

    def test_scores_tool_policy_intent_chart_and_sql(self):
        case = {
            "expected_intent": "ranking",
            "expected_chart_type": "bar",
            "required_tools": ["inspect_database_schema", "run_text_to_sql", "build_visualization"],
            "forbidden_tools": ["get_capabilities"],
        }
        result = {
            "status": "success", "intent": "ranking", "sql": "SELECT 1",
            "visualization": {"type": "bar"},
            "tool_trace": [
                {"tool": "inspect_database_schema", "status": "completed"},
                {"tool": "run_text_to_sql", "status": "completed"},
                {"tool": "build_visualization", "status": "completed"},
            ],
        }
        scored = score_agent_case(case, result)
        self.assertTrue(scored["passed"])

    def test_scores_required_sql_semantics_case_insensitively(self):
        case = {
            "expected_intent": "query", "required_tools": ["run_text_to_sql"],
            "forbidden_tools": [], "sql_must_contain": ["网页商城", "IN ("],
        }
        result = {
            "intent": "query", "status": "success",
            "sql": "SELECT * FROM orders WHERE channel IN ('网页商城', '移动端')",
            "tool_trace": [{"tool": "run_text_to_sql", "status": "completed"}],
        }
        self.assertTrue(score_agent_case(case, result)["passed"])
        result["sql"] = "SELECT * FROM orders"
        self.assertFalse(score_agent_case(case, result)["passed"])

    def test_failed_or_forbidden_tools_fail_policy(self):
        case = {"expected_intent": "chat", "required_tools": [],
                "forbidden_tools": ["run_text_to_sql"]}
        result = {"status": "success", "intent": "chat", "sql": None,
                  "tool_trace": [{"tool": "run_text_to_sql", "status": "completed"}]}
        scored = score_agent_case(case, result)
        self.assertFalse(scored["passed"])
        self.assertFalse(scored["checks"]["forbidden_tools"])

    def test_database_and_truth_rows_are_part_of_end_to_end_score(self):
        case = {"expected_intent": "query", "required_tools": ["run_text_to_sql"],
                "expected_database_id": "enterprise_hr", "expected_rows": [["研发部", 12], ["销售部", 9.0]]}
        result = {"status": "success", "intent": "query", "database_id": "enterprise_hr",
                  "sql": "SELECT 1", "rows": [["销售部", 9], ["研发部", 12]],
                  "tool_trace": [{"tool": "run_text_to_sql", "status": "completed"}]}
        self.assertTrue(score_agent_case(case, result)["passed"])
        result["database_id"] = "enterprise_sales"
        self.assertFalse(score_agent_case(case, result)["passed"])


if __name__ == "__main__":
    unittest.main()
