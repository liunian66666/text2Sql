"""刷新恢复的契约测试：接口字段、静态资源与前端状态机。

前端判定逻辑放在 app/web/static/recovery.js，用 Node 跑纯函数测试；
后端这一侧只保证「刷新后能查到恢复所需的一切」，不新增接口。
"""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import unittest

import httpx

from app.api import create_app
from app.config import PROJECT_ROOT
from app.web.registry import DatabaseRegistry
from app.web.serializers import EvaluationRepository


def _task(status: str, *, result=None) -> dict:
    return {
        "id": "req00001",
        "status": status,
        "created_at": "2026-10-29T07:50:00+00:00",
        "started_at": "2026-10-29T07:50:01+00:00" if status != "queued" else None,
        "finished_at": "2026-10-29T07:52:00+00:00" if status not in {"queued", "running"} else None,
        "request": {
            "message": "统计2026年销售额最高的10个商品",
            "question": "统计2026年销售额最高的10个商品",
            "database_id": "auto",
            "conversation_id": "conv-1",
        },
        "result": result,
    }


class RecoveryContractTest(unittest.TestCase):
    """GET /api/query/{id} 必须够浏览器把整页恢复出来。"""

    def _client(self, tasks: dict[str, dict]):
        class Manager:
            def get(self, task_id):
                if task_id not in tasks:
                    raise KeyError(task_id)
                return tasks[task_id]

            def active(self):
                # 服务端串行队列：同一时刻只会有一个 queued/running 任务
                for task in tasks.values():
                    if task["status"] in {"queued", "running"}:
                        return task
                return None

            def submit(self, request):  # 恢复流程不允许走到这里
                raise AssertionError("恢复流程不应提交新任务")

        app = create_app(
            manager=Manager(),
            registry=DatabaseRegistry(PROJECT_ROOT),
            evaluations=EvaluationRepository(PROJECT_ROOT / "evals"),
        )
        return app

    def _get(self, tasks, task_id):
        app = self._client(tasks)

        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(f"/api/query/{task_id}")

        return asyncio.run(exercise())

    def test_active_task_reports_status_and_request_identity(self):
        for status in ("queued", "running"):
            with self.subTest(status=status):
                response = self._get({f"req00001": _task(status)}, "req00001")
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["status"], status)
                self.assertIsNone(payload["result"])
                self.assertEqual(payload["request"]["conversation_id"], "conv-1")
                self.assertEqual(payload["request"]["database_id"], "auto")
                # 刷新后要能还原问题气泡，所以问题文本必须留在请求里
                self.assertTrue(payload["request"]["message"])

    def test_finished_task_restores_table_chart_sql_and_usage(self):
        result = {
            "status": "success",
            "answer": "共 10 个商品。",
            "sql": "SELECT 1",
            "columns": ["商品名称", "销量", "销售额", "排名"],
            "rows": [["坚果礼盒专业版", 70, 105910.79, 1]],
            "visualization": {"type": "bar", "title": "TOP10", "labels": ["坚果礼盒专业版"],
                              "series": [{"name": "销售额", "values": [105910.79]}]},
            "agent_elapsed_seconds": 101.0,
            "usage": {"chat": {"total_tokens": 29342}},
            "artifacts": {"final_sql": "SELECT 1"},
        }
        payload = self._get({"req00001": _task("success", result=result)}, "req00001").json()
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["result"]["columns"][0], "商品名称")
        self.assertEqual(payload["result"]["rows"][0][3], 1)
        self.assertEqual(payload["result"]["visualization"]["type"], "bar")
        self.assertEqual(payload["result"]["artifacts"]["final_sql"], "SELECT 1")
        self.assertIn("total_tokens", payload["result"]["usage"]["chat"])
        self.assertIsNotNone(payload["finished_at"])

    def test_interrupted_task_explains_the_restart(self):
        result = {"status": "interrupted", "failed_stage": "unknown",
                  "error": "演示服务在该请求运行期间被重启，请求未完成；请重新提交这个问题。"}
        payload = self._get({"req00001": _task("interrupted", result=result)}, "req00001").json()
        self.assertEqual(payload["status"], "interrupted")
        self.assertEqual(payload["result"]["status"], "interrupted")
        self.assertIn("重启", payload["result"]["error"])

    def test_unknown_request_is_404_so_the_page_can_clean_up(self):
        response = self._get({}, "gone")
        self.assertEqual(response.status_code, 404)

    def test_active_endpoint_lets_a_new_tab_adopt_the_running_task(self):
        """新标签页的 sessionStorage 是空的，只能问服务端“现在有任务在跑吗”。"""
        response = self._get({"req00001": _task("running")}, "active")
        self.assertEqual(response.status_code, 200)
        task = response.json()["task"]
        self.assertEqual(task["id"], "req00001")
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["request"]["conversation_id"], "conv-1")
        self.assertTrue(task["request"]["message"])

    def test_active_endpoint_is_empty_when_nothing_is_running(self):
        payload = self._get({"req00001": _task("success", result={"status": "success"})}, "active").json()
        self.assertIsNone(payload["task"])

    def test_active_route_is_not_shadowed_by_the_request_id_route(self):
        """/query/active 必须声明在 /query/{request_id} 之前，否则会被当成请求 ID。"""
        response = self._get({}, "active")
        self.assertEqual(response.status_code, 200, "active 被 /query/{request_id} 抢走了")

    def test_response_never_leaks_paths_or_credentials(self):
        result = {
            "status": "success", "sql": "SELECT 1", "columns": ["a"], "rows": [[1]],
            "artifacts": {"final_sql": "SELECT 1"},
        }
        body = self._get({"req00001": _task("success", result=result)}, "req00001").text
        self.assertNotIn("artifacts_dir", body)
        self.assertNotIn("sk-", body)
        self.assertNotIn("workspace/", body)
        self.assertNotIn("E:\\\\", body)
        self.assertNotIn("Traceback", body)


class RecoveryStaticAssetTest(unittest.TestCase):
    """恢复脚本必须真的被页面加载，否则前端状态机等于没上。"""

    def test_page_loads_recovery_before_chat(self):
        html = (PROJECT_ROOT / "app/web/static/chat.html").read_text(encoding="utf-8")
        recovery_at = html.find("/static/recovery.js")
        chat_at = html.find("/static/chat.js")
        self.assertGreaterEqual(recovery_at, 0, "chat.html 必须引入 recovery.js")
        self.assertGreaterEqual(chat_at, 0)
        self.assertLess(recovery_at, chat_at, "recovery.js 必须先于 chat.js 执行")

    def test_static_assets_are_served_without_cache(self):
        app = create_app(
            manager=object(),
            registry=DatabaseRegistry(PROJECT_ROOT),
            evaluations=EvaluationRepository(PROJECT_ROOT / "evals"),
        )

        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get("/static/recovery.js")

        response = asyncio.run(exercise())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertIn("deepeye.active_task", response.text)

    def test_restore_path_never_resubmits_the_question(self):
        """恢复只能用 GET 查状态；一旦在恢复分支里调 POST /api/chat 就会重复烧 Token。"""
        source = (PROJECT_ROOT / "app/web/static/chat.js").read_text(encoding="utf-8")
        start = source.index("async function restoreActiveTask")
        end = source.index("\nfunction renderAssistantResult", start)
        restore = source[start:end]
        self.assertNotIn("/api/chat", restore)
        self.assertIn("/api/query/", restore)
        # 新标签页没有本地记录，必须能问服务端要当前任务，而不是重新提交
        self.assertIn("/api/query/active", restore)
        # 恢复分支只能重连事件流或直接渲染，不允许再走一次提交
        self.assertNotIn("submitQuery", restore)

    def test_duplicate_submit_is_blocked_while_a_run_is_active(self):
        """双击发送、恢复期间点推荐问题都必须被同一个守卫挡住。"""
        source = (PROJECT_ROOT / "app/web/static/chat.js").read_text(encoding="utf-8")
        start = source.index("async function submitQuery")
        end = source.index("function persistRun", start)
        submit = source[start:end]
        self.assertIn("if (activeRun || recovering) return;", submit)


class FrontendRecoveryLogicTest(unittest.TestCase):
    """把 Node 侧的纯逻辑测试接进同一条 unittest 命令。"""

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_recovery_state_machine(self):
        script = PROJECT_ROOT / "tests/js/recovery.test.mjs"
        completed = subprocess.run(
            ["node", str(script)], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("assertions", completed.stdout)


if __name__ == "__main__":
    unittest.main()
