"""SQLite persistence for LangGraph checkpoints and Web task state.

The state database is deliberately separate from the read-only sales and HR
databases.  LangGraph owns its checkpoint tables; this module owns only the
human-readable conversation, task and SSE event tables.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Iterable

from langgraph.checkpoint.sqlite import SqliteSaver


TERMINAL_STATUSES = {"success", "empty_result", "error", "interrupted"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class SQLiteStateStore:
    """Application-owned tables stored alongside LangGraph checkpoints."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self.setup()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def setup(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    requested_database_id TEXT NOT NULL DEFAULT 'auto',
                    actual_database_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_question TEXT,
                    last_status TEXT NOT NULL DEFAULT 'active',
                    archived INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_conversations_updated
                ON conversations(archived, updated_at DESC);

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    database_id TEXT NOT NULL DEFAULT 'auto',
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error TEXT,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_conversation
                ON tasks(conversation_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_tasks_status
                ON tasks(status, created_at);

                CREATE TABLE IF NOT EXISTS task_events (
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (task_id, sequence),
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_task_events_task_sequence
                ON task_events(task_id, sequence);
                """
            )
            connection.commit()

    @staticmethod
    def _conversation_from_row(row: sqlite3.Row, *, task_count: int | None = None) -> dict[str, Any]:
        item = {
            "id": row["id"],
            "title": row["title"],
            "requested_database_id": row["requested_database_id"],
            "actual_database_id": row["actual_database_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_question": row["last_question"],
            "last_status": row["last_status"],
            "archived": bool(row["archived"]),
        }
        if task_count is not None:
            item["task_count"] = task_count
        return item

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "request": _json_load(row["request_json"], {}),
            "result": _json_load(row["result_json"], None),
        }

    def create_task(self, task: dict[str, Any]) -> None:
        request = task["request"]
        conversation_id = request["conversation_id"]
        question = request.get("message") or request.get("question") or ""
        title = question.strip().replace("\n", " ")[:48] or "新对话"
        now = task["created_at"]
        database_id = request.get("database_id") or "auto"
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO conversations
                   (id, title, requested_database_id, created_at, updated_at,
                    last_question, last_status, archived)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                   ON CONFLICT(id) DO UPDATE SET
                     updated_at=excluded.updated_at,
                     last_question=excluded.last_question,
                     last_status=excluded.last_status,
                     archived=0""",
                (conversation_id, title, database_id, now, now, question, task["status"]),
            )
            connection.execute(
                """INSERT INTO tasks
                   (id, conversation_id, database_id, status, request_json,
                    result_json, created_at, started_at, finished_at, error)
                   VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, NULL, NULL)""",
                (task["id"], conversation_id, database_id, task["status"],
                 _json_dump(request), now),
            )
            connection.commit()

    def import_legacy_tasks(self, directory: Path) -> int:
        """One-time, idempotent import of the former JSON task records."""
        if not directory.is_dir():
            return 0
        imported = 0
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("id") != path.stem:
                continue
            try:
                self.get_task(path.stem)
                continue
            except KeyError:
                pass
            request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
            request = dict(request)
            request["message"] = request.get("message") or request.get("question") or "（历史问题）"
            request["database_id"] = request.get("database_id") or "auto"
            request["conversation_id"] = request.get("conversation_id") or f"legacy-{path.stem}"
            created_at = payload.get("created_at") or utc_now()
            status = str(payload.get("status") or "interrupted")
            self.create_task({
                "id": path.stem,
                "status": "queued",
                "created_at": created_at,
                "request": request,
            })
            result = payload.get("result") if isinstance(payload.get("result"), dict) else None
            if status in TERMINAL_STATUSES:
                self.finish_task(
                    path.stem,
                    status=status,
                    finished_at=payload.get("finished_at") or created_at,
                    result=result or {"status": status},
                )
            else:
                self.update_task(
                    path.stem,
                    status=status,
                    started_at=payload.get("started_at"),
                    result=result,
                )
            imported += 1
        return imported

    def update_task(self, task_id: str, *, status: str, started_at: str | None = None,
                    finished_at: str | None = None, result: dict[str, Any] | None = None) -> None:
        result_json = _json_dump(result) if result is not None else None
        error = (result or {}).get("error") if result else None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT conversation_id FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            connection.execute(
                """UPDATE tasks SET status=?,
                   started_at=COALESCE(?, started_at),
                   finished_at=COALESCE(?, finished_at),
                   result_json=COALESCE(?, result_json), error=COALESCE(?, error)
                   WHERE id=?""",
                (status, started_at, finished_at, result_json, error, task_id),
            )
            actual_database_id = (result or {}).get("database_id")
            connection.execute(
                """UPDATE conversations SET updated_at=?, last_status=?,
                   actual_database_id=COALESCE(?, actual_database_id)
                   WHERE id=?""",
                (finished_at or started_at or utc_now(), status, actual_database_id,
                 row["conversation_id"]),
            )
            connection.commit()

    def append_event(self, task_id: str, event: dict[str, Any]) -> dict[str, Any]:
        payload = {key: value for key, value in event.items() if key not in {"sequence", "at"}}
        at = event.get("at") or utc_now()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone()
            if exists is None:
                raise KeyError(task_id)
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_events WHERE task_id=?",
                (task_id,),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO task_events
                   (task_id, sequence, created_at, stage, status, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (task_id, sequence, at, str(payload.get("stage", "unknown")),
                 str(payload.get("status", "unknown")), _json_dump(payload)),
            )
            connection.commit()
        return {"sequence": sequence, "at": at, **payload}

    def finish_task(self, task_id: str, *, status: str, finished_at: str,
                    result: dict[str, Any]) -> dict[str, Any]:
        """Atomically persist the result and terminal SSE event.

        Keeping these in one transaction prevents readers from observing a
        terminal task before its final event has been committed.
        """
        payload = {"stage": "request", "status": status}
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT conversation_id FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            connection.execute(
                """UPDATE tasks SET status=?, finished_at=?, result_json=?, error=?
                   WHERE id=?""",
                (status, finished_at, _json_dump(result), result.get("error"), task_id),
            )
            connection.execute(
                """UPDATE conversations SET updated_at=?, last_status=?,
                   actual_database_id=COALESCE(?, actual_database_id)
                   WHERE id=?""",
                (finished_at, status, result.get("database_id"), row["conversation_id"]),
            )
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_events WHERE task_id=?",
                (task_id,),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO task_events
                   (task_id, sequence, created_at, stage, status, payload_json)
                   VALUES (?, ?, ?, 'request', ?, ?)""",
                (task_id, sequence, finished_at, status, _json_dump(payload)),
            )
            connection.commit()
        return {"sequence": sequence, "at": finished_at, **payload}

    def get_task(self, task_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task_from_row(row)

    def active_task(self) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT * FROM tasks WHERE status NOT IN ({placeholders}) ORDER BY created_at, id LIMIT 1",
                tuple(sorted(TERMINAL_STATUSES)),
            ).fetchone()
        return self._task_from_row(row) if row else None

    def events_after(self, task_id: str, after: int) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            exists = connection.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone()
            if exists is None:
                raise KeyError(task_id)
            rows = connection.execute(
                """SELECT sequence, created_at, payload_json FROM task_events
                   WHERE task_id=? AND sequence>? ORDER BY sequence""",
                (task_id, after),
            ).fetchall()
        return [
            {"sequence": row["sequence"], "at": row["created_at"],
             **_json_load(row["payload_json"], {})}
            for row in rows
        ]

    def interrupt_incomplete_tasks(self) -> list[str]:
        interrupted: list[str] = []
        now = utc_now()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, conversation_id FROM tasks WHERE status IN ('queued', 'running')"
            ).fetchall()
            for row in rows:
                result = {
                    "status": "interrupted",
                    "failed_stage": "unknown",
                    "error": "Web 服务在该请求运行期间被重启，请求未完成；请重新提交这个问题。",
                }
                connection.execute(
                    """UPDATE tasks SET status='interrupted', finished_at=?, result_json=?, error=?
                       WHERE id=?""",
                    (now, _json_dump(result), result["error"], row["id"]),
                )
                sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_events WHERE task_id=?",
                    (row["id"],),
                ).fetchone()[0]
                payload = {"stage": "request", "status": "interrupted"}
                connection.execute(
                    """INSERT INTO task_events
                       (task_id, sequence, created_at, stage, status, payload_json)
                       VALUES (?, ?, ?, 'request', 'interrupted', ?)""",
                    (row["id"], sequence, now, _json_dump(payload)),
                )
                connection.execute(
                    """UPDATE conversations SET updated_at=?, last_status='interrupted'
                       WHERE id=?""",
                    (now, row["conversation_id"]),
                )
                interrupted.append(row["id"])
            connection.commit()
        return interrupted

    def list_conversations(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT c.*, COUNT(t.id) AS task_count
                   FROM conversations c LEFT JOIN tasks t ON t.conversation_id=c.id
                   WHERE c.archived=0
                   GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?""",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [self._conversation_from_row(row, task_count=row["task_count"]) for row in rows]

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            conversation = connection.execute(
                "SELECT * FROM conversations WHERE id=? AND archived=0", (conversation_id,)
            ).fetchone()
            if conversation is None:
                raise KeyError(conversation_id)
            task_rows = connection.execute(
                "SELECT * FROM tasks WHERE conversation_id=? ORDER BY created_at, id",
                (conversation_id,),
            ).fetchall()
        return {
            **self._conversation_from_row(conversation, task_count=len(task_rows)),
            "tasks": [self._task_from_row(row) for row in task_rows],
        }

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            connection.commit()
            return cursor.rowcount > 0

    def table_names(self) -> list[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        return [row[0] for row in rows]


class SQLitePersistence:
    """Own the SQLite checkpoint connection and application state repository."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Harden deserialization of checkpoints if the local database is tampered with.
        os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
        self._checkpoint_connection = sqlite3.connect(
            self.path, timeout=5.0, check_same_thread=False,
        )
        self._checkpoint_connection.execute("PRAGMA journal_mode=WAL")
        self._checkpoint_connection.execute("PRAGMA busy_timeout=5000")
        self.checkpointer = SqliteSaver(self._checkpoint_connection)
        self.checkpointer.setup()
        self.state = SQLiteStateStore(self.path)

    def delete_threads(self, thread_ids: Iterable[str]) -> None:
        for thread_id in set(thread_ids):
            self.checkpointer.delete_thread(thread_id)

    def close(self) -> None:
        self._checkpoint_connection.close()
