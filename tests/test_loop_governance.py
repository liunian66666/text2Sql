"""C4：循环治理 —— 通用调用指纹去重 + 任务级反思。

两层防线分工：
* **指纹层（Infra）**：`工具名 + 完全相同的参数`。第 2 次直接复用上次结果、
  不再执行；第 3 次判定 `loop_detected` 并写终止原因。
* **反思层（认知）**：只在出现「缓存复用 / 连续失败」等循环证据时触发一次，
  由模型判断 `continue / finish / stop`。不做定时反思 —— 那等于用成本换焦虑。

`sql_revision` 是另一回事：它修的是「一条 SQL 写得对不对」，不是「任务要不要继续」。
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from app.agent.reflection import parse_decision, should_reflect
from app.agent.service import DataAnalysisAgent
from app.agent.tools.registry import (
    MAX_IDENTICAL_TOOL_CALLS, ToolRegistry, tool_call_signature)


class SignatureTest(unittest.TestCase):

    def test_signature_is_key_order_independent(self):
        self.assertEqual(tool_call_signature("t", {"a": 1, "b": 2}),
                         tool_call_signature("t", {"b": 2, "a": 1}))

    def test_signature_distinguishes_arguments_and_tools(self):
        base = tool_call_signature("t", {"a": 1})
        self.assertNotEqual(base, tool_call_signature("t", {"a": 2}))
        self.assertNotEqual(base, tool_call_signature("u", {"a": 1}))


def _make_db(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders(order_id INTEGER PRIMARY KEY, region TEXT)")
        connection.execute("INSERT INTO orders VALUES (1, 'East')")
    return path


class _CountingSkill:
    def __init__(self):
        self.calls = 0

    def run(self, request, on_stage=None):
        self.calls += 1
        return SimpleNamespace(model_dump=lambda: {
            "request_id": f"req-{self.calls}", "status": "success", "sql": "SELECT 1",
            "columns": ["c"], "rows": [[1]], "truncated": False, "elapsed_seconds": 0.01,
            "stage_seconds": {}, "usage": {}, "failed_stage": None, "error": None,
            "artifacts_dir": "tmp"})


def _state(**overrides):
    base = {"turn_id": "t1", "database_id": "enterprise_sales", "tool_calls_seen": {},
            "tool_trace": [], "sql_results": [], "probe_results": [],
            "sql_generations": 0, "sql_history": [], "request": {"question": "q"}}
    base.update(overrides)
    return base


class _ToolSpy(ToolRegistry):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.executed: list[str] = []

    def execute(self, name, arguments, state, on_stage=None):
        self.executed.append(name)
        return super().execute(name, arguments, state, on_stage)


class FingerprintDedupTest(unittest.TestCase):

    def _registry(self, tmp: str) -> _ToolSpy:
        return _ToolSpy(sql_skill=_CountingSkill(), databases={
            "enterprise_sales": _make_db(Path(tmp) / "s.sqlite")})

    def test_second_identical_call_is_served_from_cache(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            registry = self._registry(tmp)
            state = _state()
            first = registry.invoke("get_capabilities", {}, state, None, "c1")
            seen = first.update["tool_calls_seen"]
            second_state = _state(tool_calls_seen=seen)
            second = registry.invoke("get_capabilities", {}, second_state, None, "c2")

        self.assertEqual(registry.executed, ["get_capabilities"])  # 只真正执行一次
        payload = json.loads(second.update["messages"][0].content)
        self.assertTrue(payload["cached_tool_result"])
        self.assertIn("没有重新执行", payload["note"])
        self.assertTrue(second.update["tool_trace"][0]["cached"])

    def test_third_identical_call_is_blocked_as_loop(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            registry = self._registry(tmp)
            seen: dict = {}
            for call_id in ("c1", "c2"):
                result = registry.invoke("get_capabilities", {}, _state(tool_calls_seen=seen),
                                         None, call_id)
                seen = {**seen, **result.update["tool_calls_seen"]}
            third = registry.invoke("get_capabilities", {}, _state(tool_calls_seen=seen), None, "c3")

        self.assertEqual(registry.executed, ["get_capabilities"])
        self.assertEqual(third.update["termination_reason"], "loop_detected")
        self.assertEqual(third.update["tool_trace"][0]["status"], "failed")
        self.assertTrue(third.update["tool_trace"][0]["loop_detected"])
        self.assertIn(str(MAX_IDENTICAL_TOOL_CALLS - 1), third.update["messages"][0].content)

    def test_different_arguments_are_never_blocked(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            registry = self._registry(tmp)
            seen: dict = {}
            for index in range(3):
                result = registry.invoke("get_capabilities",
                                         {"database_id": f"db{index}"} if index else {},
                                         _state(tool_calls_seen=seen), None, f"c{index}")
                seen = {**seen, **result.update["tool_calls_seen"]}
        self.assertNotIn("termination_reason", result.update)

    def test_counter_resets_across_turns(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            registry = self._registry(tmp)
            first = registry.invoke("get_capabilities", {}, _state(turn_id="t1"), None, "c1")
            # 上一轮的记录带的是 t1；新轮次不得继承计数
            second_state = _state(turn_id="t2", tool_calls_seen=first.update["tool_calls_seen"])
            second = registry.invoke("get_capabilities", {}, second_state, None, "c2")

        self.assertEqual(registry.executed, ["get_capabilities", "get_capabilities"])
        self.assertNotIn("cached", second.update["tool_trace"][0])


class ReflectionGateTest(unittest.TestCase):

    def test_no_reflection_without_loop_evidence(self):
        state = {"turn_id": "t1", "tool_trace": [
            {"turn_id": "t1", "tool": "probe_database", "status": "completed"},
            {"turn_id": "t1", "tool": "run_text_to_sql", "status": "completed"}]}
        self.assertFalse(should_reflect(state))

    def test_reflection_triggers_on_cached_call(self):
        state = {"turn_id": "t1", "tool_trace": [
            {"turn_id": "t1", "tool": "get_capabilities", "status": "completed", "cached": True}]}
        self.assertTrue(should_reflect(state))

    def test_reflection_triggers_on_repeated_failures(self):
        state = {"turn_id": "t1", "tool_trace": [
            {"turn_id": "t1", "tool": "a", "status": "failed"},
            {"turn_id": "t1", "tool": "b", "status": "failed"}]}
        self.assertTrue(should_reflect(state))

    def test_reflection_runs_once_per_turn(self):
        state = {"turn_id": "t1", "reflection_done": True, "tool_trace": [
            {"turn_id": "t1", "tool": "a", "status": "completed", "cached": True}]}
        self.assertFalse(should_reflect(state))

    def test_parse_decision_tolerates_noise(self):
        self.assertEqual(parse_decision('{"decision": "stop", "reason": "原地打转"}')[0], "stop")
        self.assertEqual(parse_decision('好的：{"decision":"finish"}')[0], "finish")
        self.assertEqual(parse_decision("完全不是 JSON")[0], "continue")


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


def _call(call_id: str, name: str = "get_capabilities", args: dict | None = None):
    return AIMessage(content="", tool_calls=[{
        "id": call_id, "name": name, "args": args if args is not None else {}}])


class ReflectionEndToEndTest(unittest.TestCase):

    def test_stop_decision_terminates_with_loop_detected(self):
        model = _ScriptedModel([
            _call("0"),
            _call("1"),  # 完全相同 → 缓存复用 → 触发反思
            AIMessage(content='{"decision": "stop", "reason": "同一调用重复"}'),
            AIMessage(content="已终止：本轮在重复同一查询。"),
        ])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            agent = DataAnalysisAgent(_CountingSkill(), workspace=Path(tmp) / "agent",
                                     databases={"enterprise_sales": _make_db(Path(tmp) / "s.sqlite")},
                                     chat_model=model)
            result = agent.run({"question": "看看能力", "database_id": "enterprise_sales"})

        self.assertEqual(result.termination_reason, "loop_detected")
        self.assertEqual(result.status, "error")
        self.assertIn("已终止", result.answer)
        # 反思 + 收尾各一次模型调用，没有回到无限循环
        self.assertEqual(model.calls, 4)

    def test_continue_decision_keeps_the_loop_alive(self):
        model = _ScriptedModel([
            _call("0"),
            _call("1"),  # 缓存复用 → 反思
            AIMessage(content='{"decision": "continue", "reason": "还需要真实查询"}'),
            AIMessage(content="继续处理。"),
        ])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            agent = DataAnalysisAgent(_CountingSkill(), workspace=Path(tmp) / "agent",
                                     databases={"enterprise_sales": _make_db(Path(tmp) / "s.sqlite")},
                                     chat_model=model)
            result = agent.run({"question": "看看能力", "database_id": "enterprise_sales"})

        self.assertIsNone(result.termination_reason)
        self.assertEqual(result.status, "success", result.error)
        self.assertEqual(result.reflection_notes[0]["decision"], "continue")


if __name__ == "__main__":
    unittest.main()
