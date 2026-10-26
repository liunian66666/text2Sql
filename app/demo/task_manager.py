from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from queue import Queue
import threading
from typing import Any
from uuid import uuid4

from app.skills import SQLSkillRequest

from .serializers import serialize_skill_result


INTERRUPTED_STATUS = "interrupted"
TERMINAL_STATUSES = {"success", "empty_result", "error", INTERRUPTED_STATUS}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskManager:
    """Run one SQLSkill request at a time and retain replayable progress events."""

    def __init__(self, skill, state_dir: Path):
        self.skill = skill
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, dict[str, Any]] = {}
        self._restore_records()
        self._queue: Queue[str | None] = Queue()
        self._condition = threading.Condition()
        self._worker = threading.Thread(target=self._run_worker, name="demo-sql-worker", daemon=True)
        self._worker.start()

    def _restore_records(self) -> None:
        """Re-adopt task records written by an earlier process.

        Every transition is persisted, but the worker queue lives only in memory,
        so a restart leaves `queued`/`running` records that nothing will ever
        finish. Answering those IDs with 404 makes a browser tab look like it is
        still waiting on the model; instead we mark them interrupted and replay a
        single terminal event so the existing SSE path reports what happened.
        Assumes a single demo process owns this directory.
        """
        for record in sorted(self.state_dir.glob("*.json")):
            try:
                task = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(task, dict) or task.get("id") != record.stem:
                continue
            changed = task.get("status") not in TERMINAL_STATUSES
            if changed:
                task["status"] = INTERRUPTED_STATUS
                task["finished_at"] = task.get("finished_at") or _now()
                task["result"] = {
                    "status": INTERRUPTED_STATUS,
                    "failed_stage": "unknown",
                    "error": "演示服务在该请求运行期间被重启，请求未完成；请重新提交这个问题。",
                }
            events = task.get("events") or []
            if not events or events[-1].get("stage") != "request":
                events.append({"sequence": len(events) + 1, "at": _now(),
                               "stage": "request", "status": task["status"]})
            task["events"] = events
            self._tasks[task["id"]] = task
            if changed:
                self._persist_locked(task)

    def submit(self, request: SQLSkillRequest | dict[str, Any]) -> dict[str, Any]:
        validated = SQLSkillRequest.model_validate(request)
        task_id = uuid4().hex[:12]
        task = {
            "id": task_id,
            "status": "queued",
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "request": validated.model_dump(),
            "result": None,
            "events": [],
        }
        with self._condition:
            self._tasks[task_id] = task
            self._append_event_locked(task, {"stage": "queue", "status": "queued"})
            self._persist_locked(task)
            self._condition.notify_all()
        self._queue.put(task_id)
        return self.get(task_id)

    def _append_event_locked(self, task: dict[str, Any], event: dict[str, Any]) -> None:
        task["events"].append({"sequence": len(task["events"]) + 1, "at": _now(), **event})

    def _persist_locked(self, task: dict[str, Any]) -> None:
        public = {key: value for key, value in task.items() if key != "events"}
        destination = self.state_dir / f"{task['id']}.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)

    def _on_stage(self, task_id: str, event: dict[str, Any]) -> None:
        safe_event = {key: value for key, value in event.items() if key != "request_id"}
        with self._condition:
            task = self._tasks[task_id]
            self._append_event_locked(task, safe_event)
            self._persist_locked(task)
            self._condition.notify_all()

    def _run_worker(self) -> None:
        while True:
            task_id = self._queue.get()
            if task_id is None:
                return
            with self._condition:
                task = self._tasks[task_id]
                task["status"] = "running"
                task["started_at"] = _now()
                self._append_event_locked(task, {"stage": "queue", "status": "running"})
                self._persist_locked(task)
                self._condition.notify_all()
            try:
                result = self.skill.run(task["request"], on_stage=lambda event: self._on_stage(task_id, event))
                serialized = serialize_skill_result(result)
                status = result.status
            except Exception as exc:
                status = "error"
                serialized = {
                    "status": "error",
                    "failed_stage": "prepare",
                    "error": f"{type(exc).__name__}: request could not be started",
                }
            with self._condition:
                task = self._tasks[task_id]
                task["status"] = status
                task["result"] = serialized
                task["finished_at"] = _now()
                self._append_event_locked(task, {"stage": "request", "status": status})
                self._persist_locked(task)
                self._condition.notify_all()
            self._queue.task_done()

    def get(self, task_id: str) -> dict[str, Any]:
        with self._condition:
            if task_id not in self._tasks:
                raise KeyError(task_id)
            task = deepcopy(self._tasks[task_id])
        task.pop("events", None)
        return task

    def wait_for_events(self, task_id: str, after: int, timeout: float = 15.0) -> tuple[list[dict[str, Any]], bool]:
        with self._condition:
            if task_id not in self._tasks:
                raise KeyError(task_id)
            self._condition.wait_for(
                lambda: len(self._tasks[task_id]["events"]) > after
                or self._tasks[task_id]["status"] in TERMINAL_STATUSES,
                timeout=timeout,
            )
            task = self._tasks[task_id]
            events = deepcopy(task["events"][after:])
            terminal = task["status"] in TERMINAL_STATUSES
        return events, terminal
