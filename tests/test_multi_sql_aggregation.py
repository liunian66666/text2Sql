"""C1：复合任务的多条 SQL 子结果必须逐条可追溯，不能被后一条覆盖。

背景：`sql_result` 是单值槽，每次 `run_text_to_sql` 直接覆盖。复合任务被拆成
多条 SQL 后，前序结果在结构化输出里消失，最终只剩最后一条 —— 用户与审计都
无法还原分析过程。这里用离线脚本化模型验证新增的 `sql_results` / `analyses`
通道，同时锁住「单值槽语义不变」这条兼容约束。
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from app.agent.service import DataAnalysisAgent


def _make_db(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders(order_id INTEGER PRIMARY KEY, region TEXT, amount REAL)")
        connection.execute("INSERT INTO orders VALUES (1, 'East', 10.0), (2, 'West', 20.0)")
    return path


class _RowCountSkill:
    """第 n 次调用返回 n 行，SQL 文本带上序号，便于断言"哪一条"被保留。"""

    def __init__(self, rows_per_call: list[int] | None = None, statuses: list[str] | None = None):
        self.rows_per_call = rows_per_call or [1, 2, 3]
        self.statuses = statuses or []
        self.requests: list[dict] = []

    def run(self, request, on_stage=None):
        self.requests.append(dict(request))
        index = len(self.requests) - 1
        count = self.rows_per_call[min(index, len(self.rows_per_call) - 1)]
        status = self.statuses[index] if index < len(self.statuses) else "success"
        payload = {
            "request_id": f"req-{index + 1}",
            "status": status,
            "sql": f"SELECT {index + 1}",
            "columns": ["region", "amount"],
            "rows": [[f"R{row}", float(row)] for row in range(count)],
            "truncated": False,
            "elapsed_seconds": 0.01,
            "stage_seconds": {},
            "usage": {},
            "failed_stage": None if status == "success" else "sql_execution",
            "error": None if status == "success" else "数据库拒绝执行",
            "artifacts_dir": "tmp",
        }
        return SimpleNamespace(model_dump=lambda: payload)


def _tool_call(call_id: str, question: str):
    from langchain_core.messages import AIMessage

    return AIMessage(content="", tool_calls=[{
        "id": call_id,
        "name": "run_text_to_sql",
        "args": {"question": question, "intent": "query"},
    }])


def _schema_call(call_id: str = "schema"):
    """生成 SQL 前必须先看真实 Schema（registry 里的硬前置守卫）。"""
    from langchain_core.messages import AIMessage

    return AIMessage(content="", tool_calls=[{
        "id": call_id,
        "name": "inspect_database_schema",
        "args": {"table_names": []},
    }])


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


def _run(script, skill, question="各州销售额"):
    from langchain_core.messages import AIMessage

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = _make_db(Path(tmp) / "sales.sqlite")
        model = _ScriptedModel([*script, AIMessage(content="已汇总")])
        agent = DataAnalysisAgent(skill, workspace=Path(tmp) / "agent",
                                 databases={"enterprise_sales": db}, chat_model=model)
        return agent.run({"question": question, "database_id": "enterprise_sales"})


class MultiSqlAggregationTest(unittest.TestCase):

    def test_three_sql_calls_are_all_traceable(self):
        result = _run([
            _schema_call(),
            _tool_call("0", "各州销售额"),
            _tool_call("1", "各商品大类销售额"),
            _tool_call("2", "各州订单数"),
        ], _RowCountSkill())

        self.assertEqual(result.status, "success", result.error)
        self.assertEqual(len(result.analyses), 3)
        self.assertEqual([item["sql"] for item in result.analyses],
                         ["SELECT 1", "SELECT 2", "SELECT 3"])
        self.assertEqual([item["row_count"] for item in result.analyses], [1, 2, 3])
        self.assertEqual([len(item["columns"]) for item in result.analyses], [2, 2, 2])
        # 每条记录都保留自己的子问题，便于把结果映射回分析步骤
        self.assertEqual([item["question"] for item in result.analyses],
                         ["各州销售额", "各商品大类销售额", "各州订单数"])

    def test_last_single_value_slot_still_wins(self):
        """兼容约束：sql/columns/rows 仍代表最后一次查询，画图与前端依赖它。"""
        result = _run([
            _schema_call(),
            _tool_call("0", "各州销售额"),
            _tool_call("1", "各商品大类销售额"),
            _tool_call("2", "各州订单数"),
        ], _RowCountSkill())

        self.assertEqual(result.sql, "SELECT 3")
        self.assertEqual(len(result.rows), 3)
        self.assertEqual(result.sql_generations, 3)

    def test_preview_is_capped_but_row_count_is_truthful(self):
        """预览行上限 50：既不撑爆响应，也不谎报行数。"""
        result = _run([_schema_call(), _tool_call("0", "各州销售额")], _RowCountSkill([120]))

        record = result.analyses[0]
        self.assertEqual(record["row_count"], 120)
        self.assertEqual(len(record["preview_rows"]), 50)
        self.assertTrue(record["preview_truncated"])

    def test_failed_sql_is_recorded_with_error(self):
        """失败的子查询也要留痕：否则"少了一个维度"永远查不出来。"""
        result = _run([_schema_call(), _tool_call("0", "各州销售额")],
                      _RowCountSkill([0], statuses=["error"]))

        record = result.analyses[0]
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["failed_stage"], "sql_execution")
        self.assertIn("拒绝执行", record["error"])

    def test_analyses_exclude_previous_turns(self):
        """多轮对话里 analyses 只含本轮；上一轮的子结果不得混入。"""
        from langchain_core.messages import AIMessage

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = _make_db(Path(tmp) / "sales.sqlite")
            skill = _RowCountSkill([1, 1, 1])
            model = _ScriptedModel([
                _schema_call("s1"),
                _tool_call("0", "各州销售额"),
                AIMessage(content="第一轮完成"),
                _schema_call("s2"),
                _tool_call("1", "各州订单数"),
                AIMessage(content="第二轮完成"),
            ])
            agent = DataAnalysisAgent(skill, workspace=Path(tmp) / "agent",
                                     databases={"enterprise_sales": db}, chat_model=model)
            first = agent.run({"question": "各州销售额", "database_id": "enterprise_sales",
                               "conversation_id": "c1"})
            second = agent.run({"question": "各州订单数", "database_id": "enterprise_sales",
                                "conversation_id": "c1"})

        self.assertEqual(len(first.analyses), 1)
        self.assertEqual(len(second.analyses), 1)
        self.assertEqual(second.analyses[0]["sql"], "SELECT 2")


if __name__ == "__main__":
    unittest.main()
