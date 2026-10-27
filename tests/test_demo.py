import json
from pathlib import Path
import tempfile
import time
import unittest

import asyncio
import httpx

from app.config import PROJECT_ROOT
from app.demo.api import create_app
from app.demo.preflight import collect_preflight
from app.demo.registry import DatabaseRegistry
from app.demo.serializers import EvaluationRepository
from app.demo.task_manager import TaskManager
from app.skills import SQLSkillResult


class FakeSkill:
    def __init__(self, artifacts_dir: Path):
        self.artifacts_dir = artifacts_dir

    def run(self, request, on_stage=None):
        on_stage({"stage": "value_retrieval", "status": "started", "request_id": "internal"})
        on_stage({"stage": "value_retrieval", "status": "completed", "request_id": "internal", "seconds": 0.01})
        return SQLSkillResult(
            request_id="internal",
            status="success",
            sql="SELECT 1",
            columns=["answer"],
            rows=[[1]],
            elapsed_seconds=0.02,
            stage_seconds={"value_retrieval": 0.01},
            artifacts_dir=str(self.artifacts_dir),
        )


class DemoTaskManagerTest(unittest.TestCase):
    def test_serial_task_replays_events_and_returns_safe_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TaskManager(FakeSkill(root), root / "states")
            submitted = manager.submit({"question": "Count rows", "database_id": "demo"})
            task_id = submitted["id"]
            deadline = time.time() + 3
            while time.time() < deadline:
                task = manager.get(task_id)
                if task["status"] == "success":
                    break
                time.sleep(0.01)
            self.assertEqual(task["status"], "success")
            self.assertEqual(task["result"]["rows"], [[1]])
            self.assertNotIn("artifacts_dir", task["result"])
            events, terminal = manager.wait_for_events(task_id, 0, 0)
            self.assertTrue(terminal)
            self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
            self.assertEqual(events[-1]["status"], "success")
            self.assertTrue((root / "states" / f"{task_id}.json").exists())


class TaskManagerRecoveryTest(unittest.TestCase):
    """A restart must not leave a browser tab waiting on a task nothing owns."""

    def _write(self, directory: Path, name: str, payload: dict | str) -> None:
        (directory / f"{name}.json").write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")

    def test_unfinished_records_become_interrupted_and_replayable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "states"
            root.mkdir()
            self._write(root, "run001", {"id": "run001", "status": "running", "result": None,
                                         "request": {"question": "q", "database_id": "demo"}})
            self._write(root, "que001", {"id": "que001", "status": "queued", "result": None})
            self._write(root, "ok0001", {"id": "ok0001", "status": "success",
                                         "result": {"status": "success", "sql": "SELECT 1"}})
            self._write(root, "broken", "{not json")
            self._write(root, "foreign", {"id": "some-other-id"})

            manager = TaskManager(FakeSkill(root.parent), root)

            self.assertEqual({task_id for task_id in manager._tasks}, {"run001", "que001", "ok0001"})
            recovered = manager.get("run001")
            self.assertEqual(recovered["status"], "interrupted")
            self.assertEqual(recovered["result"]["status"], "interrupted")
            self.assertIn("重启", recovered["result"]["error"])
            self.assertNotIn("events", recovered)
            self.assertEqual(manager.get("ok0001")["status"], "success")

            events, terminal = manager.wait_for_events("run001", 0, 0)
            self.assertTrue(terminal, "an interrupted task must close its event stream")
            self.assertEqual((events[-1]["stage"], events[-1]["status"]), ("request", "interrupted"))

            persisted = json.loads((root / "run001.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "interrupted")
            self.assertNotIn("events", persisted)
            untouched = json.loads((root / "ok0001.json").read_text(encoding="utf-8"))
            self.assertNotIn("error", untouched["result"])


class EvaluationRepositoryTest(unittest.TestCase):
    def test_normalizes_ablation_without_exposing_raw_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "bird-dev-50" / "ablation"
            report.mkdir(parents=True)
            metrics = {
                "sample_count": 50,
                "correct_count": 40,
                "execution_accuracy": 0.8,
                "strict_multiset_accuracy": 0.78,
                "executable_count": 48,
                "chat_usage": {"total_tokens": 100},
                "embedding_usage": {"total_tokens": 10},
                "by_database": {},
            }
            (report / "summary.json").write_text(json.dumps({
                "variants": {"generation_only": metrics, "generation_revision": metrics, "full_pipeline": metrics},
                "transitions": {"revision_fixes": [1]},
                "config": {"api_key": "must-not-leak"},
            }), encoding="utf-8")
            repository = EvaluationRepository(root)
            summaries = repository.list()
            self.assertEqual(summaries[0]["id"], "bird-dev-50/ablation")
            self.assertEqual(summaries[0]["variants"]["full_pipeline"]["total_tokens"], 110)
            self.assertNotIn("must-not-leak", json.dumps(summaries))


class DemoPreflightTest(unittest.TestCase):
    def test_reports_missing_and_ready_indexes_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "demo.sqlite"
            database.write_bytes(b"sqlite")
            vector = root / "vectors/demo/local_index"
            vector.mkdir(parents=True)
            (vector / "manifest.json").write_text("{}", encoding="utf-8")
            few_shot = root / "few-shot"
            few_shot.mkdir()
            (few_shot / "manifest.json").write_text("{}", encoding="utf-8")
            evaluations = root / "evals/run"
            evaluations.mkdir(parents=True)
            (evaluations / "summary.json").write_text("{}", encoding="utf-8")
            config = type("Config", (), {
                "vector_database": type("Vector", (), {"store_root_path": "vectors"})(),
                "few_shot_index": type("FewShot", (), {"save_path": "few-shot"})(),
            })()

            report = collect_preflight(
                config,
                {"demo": database},
                project_root=root,
                evaluations_root=root / "evals",
            )
            self.assertTrue(report["ready"])
            self.assertEqual(report["evaluation_reports"], ["run"])
            self.assertNotIn("api", json.dumps(report).lower())


class DemoAPITest(unittest.TestCase):
    def test_static_page_and_read_only_endpoints(self):
        class Manager:
            def get(self, task_id):
                raise KeyError(task_id)

        app = create_app(
            manager=Manager(),
            registry=DatabaseRegistry(PROJECT_ROOT),
            evaluations=EvaluationRepository(PROJECT_ROOT / "evals"),
        )

        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                health = await client.get("/api/health")
                databases = await client.get("/api/databases")
                evaluations = await client.get("/api/evaluations")
                page = await client.get("/")
                asset = await client.get("/static/chat.js")
                missing = await client.get("/api/query/missing")
            return health, databases, evaluations, page, asset, missing

        health, databases, evaluations, page, asset, missing = asyncio.run(exercise())
        self.assertEqual(health.json(), {"status": "ok"})
        # 改完前端不用强制刷新也不会拿到旧脚本，否则排练时的真假回归分不开。
        self.assertEqual(asset.headers.get("cache-control"), "no-store")
        # Only databases with a value index for the active embedding model are
        # listed; see DEMO_READY_DATABASES in app/demo/registry.py.
        entries = databases.json()["databases"]
        self.assertEqual([entry["id"] for entry in entries], ["enterprise_sales"])
        self.assertGreaterEqual(len(entries[0]["examples"]), 3)
        self.assertGreaterEqual(len(evaluations.json()["evaluations"]), 1)
        self.assertIn("DeepEye-SQL", page.text)
        self.assertIn("对话分析", page.text)
        self.assertIn("企业数据分析助手", page.text)
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
