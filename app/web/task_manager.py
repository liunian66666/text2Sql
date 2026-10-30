"""Serial Web task queue backed by the shared SQLite state database."""
from __future__ import annotations

from queue import Queue
import threading
from typing import Any
from uuid import uuid4

from app.agent import ChatRequest
from app.persistence import SQLiteStateStore
from app.persistence.sqlite import TERMINAL_STATUSES, utc_now

from .serializers import serialize_agent_result


class TaskManager:
    """Run one Agent turn at a time and persist tasks plus replayable SSE events."""

    def __init__(self, agent, store: SQLiteStateStore):
        self.agent = agent
        self.store = store
        # A process cannot safely know whether an external LLM request completed
        # before a crash. Keep the durable conversation, but never auto-repeat it.
        self.store.interrupt_incomplete_tasks()
        self._queue: Queue[str | None] = Queue()
        self._condition = threading.Condition()
        self._worker = threading.Thread(target=self._run_worker, name="web-agent-worker", daemon=True)
        self._worker.start()

    def submit(self, request: ChatRequest | dict[str, Any]) -> dict[str, Any]:
        validated = ChatRequest.model_validate(request)
        if not validated.conversation_id:
            validated = validated.model_copy(update={"conversation_id": str(uuid4())})
        task_id = uuid4().hex[:12]
        task = {
            "id": task_id,
            "status": "queued",
            "created_at": utc_now(),
            "started_at": None,
            "finished_at": None,
            "request": validated.model_dump(),
            "result": None,
        }
        with self._condition:
            self.store.create_task(task)
            self.store.append_event(task_id, {"stage": "queue", "status": "queued"})
            self._condition.notify_all()
        self._queue.put(task_id)
        return self.get(task_id)

    def _append_event(self, task_id: str, event: dict[str, Any]) -> dict[str, Any]:
        safe_event = {key: value for key, value in event.items() if key != "request_id"}
        with self._condition:
            persisted = self.store.append_event(task_id, safe_event)
            self._condition.notify_all()
        return persisted

    def _run_worker(self) -> None:
        while True:
            task_id = self._queue.get()
            if task_id is None:
                return
            started_at = utc_now()
            with self._condition:
                self.store.update_task(task_id, status="running", started_at=started_at)
                self.store.append_event(task_id, {"stage": "queue", "status": "running"})
                self._condition.notify_all()
            task = self.store.get_task(task_id)
            try:
                result = self.agent.run(
                    task["request"], on_stage=lambda event: self._append_event(task_id, event)
                )
                serialized = serialize_agent_result(result)
                status = result.status
            except Exception as exc:
                status = "error"
                serialized = {
                    "status": "error",
                    "failed_stage": "prepare",
                    "error": f"{type(exc).__name__}: request could not be started",
                }
            finished_at = utc_now()
            with self._condition:
                self.store.finish_task(
                    task_id, status=status, finished_at=finished_at, result=serialized,
                )
                self._condition.notify_all()
            self._queue.task_done()

    def get(self, task_id: str) -> dict[str, Any]:
        return self.store.get_task(task_id)

    def active(self) -> dict[str, Any] | None:
        return self.store.active_task()

    def wait_for_events(self, task_id: str, after: int,
                        timeout: float = 15.0) -> tuple[list[dict[str, Any]], bool]:
        with self._condition:
            self.store.get_task(task_id)
            self._condition.wait_for(
                lambda: bool(self.store.events_after(task_id, after))
                or self.store.get_task(task_id)["status"] in TERMINAL_STATUSES,
                timeout=timeout,
            )
            events = self.store.events_after(task_id, after)
            terminal = self.store.get_task(task_id)["status"] in TERMINAL_STATUSES
        return events, terminal

    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.list_conversations(limit=limit)

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        return self.store.get_conversation(conversation_id)

    def delete_conversation(self, conversation_id: str) -> None:
        self.store.get_conversation(conversation_id)
        self.agent.delete_conversation(conversation_id)
        self.store.delete_conversation(conversation_id)
