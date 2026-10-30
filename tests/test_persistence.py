from __future__ import annotations

from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.persistence import SQLitePersistence, SQLiteStateStore
from app.agent import DataAnalysisAgent
from app.web.task_manager import TaskManager


class MessageState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def compile_graph(checkpointer):
    builder = StateGraph(MessageState)
    builder.add_node("remember", lambda state: {})
    builder.add_edge(START, "remember")
    builder.add_edge("remember", END)
    return builder.compile(checkpointer=checkpointer)


def request(conversation_id: str, database_id: str = "auto", question: str = "问题") -> dict:
    return {
        "message": question,
        "database_id": database_id,
        "conversation_id": conversation_id,
        "user_hint": "",
        "max_rows": 100,
        "sql_timeout_seconds": 30,
    }


def task(task_id: str, conversation_id: str, status: str = "queued",
         question: str = "问题") -> dict:
    return {
        "id": task_id,
        "status": status,
        "created_at": "2026-10-30T00:00:00+00:00",
        "request": request(conversation_id, question=question),
    }


class SQLiteCheckpointTest(unittest.TestCase):
    def test_messages_survive_process_style_reopen_and_threads_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            first = SQLitePersistence(path)
            graph = compile_graph(first.checkpointer)
            sales = {"configurable": {"thread_id": "conversation-1:enterprise_sales"}}
            hr = {"configurable": {"thread_id": "conversation-1:enterprise_hr"}}
            graph.invoke({"messages": [HumanMessage(content="销售问题")]}, config=sales)
            graph.invoke({"messages": [HumanMessage(content="HR 问题")]}, config=hr)
            first.close()

            reopened = SQLitePersistence(path)
            restored = compile_graph(reopened.checkpointer)
            sales_messages = restored.get_state(sales).values["messages"]
            hr_messages = restored.get_state(hr).values["messages"]
            self.assertEqual([message.content for message in sales_messages], ["销售问题"])
            self.assertEqual([message.content for message in hr_messages], ["HR 问题"])
            restored.invoke({"messages": [HumanMessage(content="继续追问")]}, config=sales)
            self.assertEqual(
                [message.content for message in restored.get_state(sales).values["messages"]],
                ["销售问题", "继续追问"],
            )
            reopened.close()

    def test_data_analysis_agent_recovers_dialogue_after_restart(self):
        class Model:
            def __init__(self, response):
                self.response = response
                self.seen_messages = []

            def bind_tools(self, _tools):
                return self

            def invoke(self, messages, config=None):
                self.seen_messages.append(messages)
                return AIMessage(content=self.response)

        class Skill:
            def run(self, *_args, **_kwargs):
                raise AssertionError("chat-only turn must not call SQL")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "sales.sqlite"
            database.touch()
            persistence = SQLitePersistence(root / "state.sqlite")
            first_model = Model("第一轮回答")
            first_agent = DataAnalysisAgent(
                Skill(), workspace=root / "agent-1", databases={"enterprise_sales": database},
                chat_model=first_model, checkpointer=persistence.checkpointer,
            )
            first_agent.run({"question": "第一轮问题", "database_id": "enterprise_sales",
                             "conversation_id": "conversation-1"})
            persistence.close()

            reopened = SQLitePersistence(root / "state.sqlite")
            second_model = Model("第二轮回答")
            second_agent = DataAnalysisAgent(
                Skill(), workspace=root / "agent-2", databases={"enterprise_sales": database},
                chat_model=second_model, checkpointer=reopened.checkpointer,
            )
            second_agent.run({"question": "继续追问", "database_id": "enterprise_sales",
                              "conversation_id": "conversation-1"})
            contents = [message.content for message in second_model.seen_messages[0][1:]]
            self.assertIn("第一轮问题", contents)
            self.assertIn("第一轮回答", contents)
            self.assertIn("继续追问", contents)
            reopened.close()


class SQLiteStateStoreTest(unittest.TestCase):
    def test_legacy_json_tasks_are_imported_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "demo_tasks"
            legacy.mkdir()
            (legacy / "old-task.json").write_text(json.dumps({
                "id": "old-task", "status": "success",
                "created_at": "2026-10-29T00:00:00+00:00",
                "finished_at": "2026-10-29T00:00:01+00:00",
                "request": {"message": "旧问题", "database_id": "auto",
                            "conversation_id": "old-conversation"},
                "result": {"status": "success", "answer": "旧回答"},
            }, ensure_ascii=False), encoding="utf-8")
            store = SQLiteStateStore(root / "state.sqlite")
            self.assertEqual(store.import_legacy_tasks(legacy), 1)
            self.assertEqual(store.import_legacy_tasks(legacy), 0)
            self.assertEqual(store.get_task("old-task")["result"]["answer"], "旧回答")
            self.assertEqual(store.get_conversation("old-conversation")["task_count"], 1)

    def test_tasks_events_and_conversations_survive_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            store = SQLiteStateStore(path)
            store.create_task(task("task-1", "conversation-1"))
            store.append_event("task-1", {"stage": "queue", "status": "queued"})
            store.update_task("task-1", status="running", started_at="2026-10-30T00:00:01+00:00")
            store.append_event("task-1", {"stage": "queue", "status": "running"})
            store.finish_task(
                "task-1", status="success", finished_at="2026-10-30T00:00:02+00:00",
                result={"status": "success", "database_id": "enterprise_sales", "rows": [[1]]},
            )

            reopened = SQLiteStateStore(path)
            saved = reopened.get_task("task-1")
            self.assertEqual(saved["status"], "success")
            self.assertEqual(saved["result"]["rows"], [[1]])
            events = reopened.events_after("task-1", 1)
            self.assertEqual([event["sequence"] for event in events], [2, 3])
            conversation = reopened.get_conversation("conversation-1")
            self.assertEqual(conversation["actual_database_id"], "enterprise_sales")
            self.assertEqual(conversation["task_count"], 1)

    def test_restart_interrupts_active_tasks_without_requeueing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStateStore(Path(tmp) / "state.sqlite")
            store.create_task(task("queued-1", "conversation-1"))
            store.append_event("queued-1", {"stage": "queue", "status": "queued"})
            store.create_task(task("running-1", "conversation-2"))
            store.update_task("running-1", status="running", started_at="2026-10-30T00:00:01+00:00")
            interrupted = store.interrupt_incomplete_tasks()
            self.assertEqual(set(interrupted), {"queued-1", "running-1"})
            for task_id in interrupted:
                saved = store.get_task(task_id)
                self.assertEqual(saved["status"], "interrupted")
                self.assertIn("重启", saved["result"]["error"])
                self.assertEqual(store.events_after(task_id, 0)[-1]["status"], "interrupted")

    def test_delete_conversation_cascades_tasks_events_and_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = SQLitePersistence(Path(tmp) / "state.sqlite")
            graph = compile_graph(persistence.checkpointer)
            config = {"configurable": {"thread_id": "conversation-1:auto"}}
            graph.invoke({"messages": [HumanMessage(content="记住我")]}, config=config)
            persistence.state.create_task(task("task-1", "conversation-1"))
            persistence.state.append_event("task-1", {"stage": "queue", "status": "queued"})

            class Agent:
                def run(self, *_args, **_kwargs):
                    raise AssertionError("not used")

                def delete_conversation(self, conversation_id):
                    persistence.delete_threads([f"{conversation_id}:auto"])

            manager = TaskManager(Agent(), persistence.state)
            manager.delete_conversation("conversation-1")
            with self.assertRaises(KeyError):
                persistence.state.get_task("task-1")
            self.assertIsNone(persistence.checkpointer.get(config))
            persistence.close()

    def test_state_database_does_not_capture_runtime_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            store = SQLiteStateStore(path)
            store.create_task(task("task-1", "conversation-1", question="安全问题"))
            with closing(sqlite3.connect(path)) as connection:
                values = connection.execute(
                    "SELECT request_json || COALESCE(result_json, '') || COALESCE(error, '') FROM tasks"
                ).fetchall()
                values += connection.execute(
                    "SELECT title || COALESCE(last_question, '') FROM conversations"
                ).fetchall()
            text = json.dumps(values, ensure_ascii=False)
            self.assertNotIn("OPENAI_API_KEY", text)
            self.assertNotIn("sk-", text)
            self.assertNotIn(str(Path.cwd()), text)


if __name__ == "__main__":
    unittest.main()
