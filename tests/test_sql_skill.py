import json
import os
from collections import Counter
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.skills.sql_skill import SQLSkill, SQLSkillRequest, execute_readonly_table, _usage_environment
from app.llm.usage import ThreadPoolExecutor, meter_item, record_usage
from app.config import AppConfig, PROJECT_ROOT
from app.config.config import LLMConfig
from app.llm import LLM
from runner.report_baseline import compare_execution, aggregate_usage
from runner.report_ablation import separate_recovery_usage
from runner.select_bird_sample import select_sample


class SQLSkillSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "data.sqlite"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE numbers (n INTEGER)")
            conn.executemany("INSERT INTO numbers VALUES (?)", [(i,) for i in range(10)])
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_select_and_empty_results_and_row_limit(self):
        cols, rows, truncated = execute_readonly_table(self.db, "SELECT n FROM numbers ORDER BY n", 3, 2)
        self.assertEqual((cols, rows, truncated), (["n"], [[0], [1], [2]], True))
        self.assertEqual(execute_readonly_table(self.db, "SELECT n FROM numbers WHERE n < 0", 3, 2), (["n"], [], False))
        self.assertEqual(execute_readonly_table(self.db, "SELECT X'ff00' AS blob_value", 3, 2)[1], [[{"hex": "ff00"}]])

    def test_blocks_writes_attach_and_multiple_statements(self):
        for sql in ("DELETE FROM numbers", "CREATE TABLE x(n)", "ATTACH DATABASE ':memory:' AS x",
                    "PRAGMA writable_schema=ON", "SELECT 1; DELETE FROM numbers"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.Error):
                execute_readonly_table(self.db, sql, 3, 2)
        self.assertEqual(execute_readonly_table(self.db, "SELECT COUNT(*) FROM numbers", 3, 2)[1], [[10]])

    def test_interrupts_expensive_recursive_query(self):
        with self.assertRaises(sqlite3.OperationalError):
            execute_readonly_table(self.db, "WITH RECURSIVE t(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM t) SELECT SUM(x) FROM t", 1, 1)

    def test_pipeline_executor_allows_schema_inspection_but_blocks_attach(self):
        from app.db_utils.execution import execute_sql_without_cache, _clear_cached_connection
        try:
            self.assertEqual(execute_sql_without_cache(str(self.db), "PRAGMA table_info(numbers)").result_type, "success")
            self.assertEqual(execute_sql_without_cache(str(self.db), "ATTACH DATABASE ':memory:' AS x").result_type, "execution_error")
        finally:
            _clear_cached_connection(str(self.db.resolve()))

    def test_request_paths_are_isolated_and_missing_index_fails_before_api(self):
        llm = {"model": "test", "base_url": "http://localhost:1", "api_key": "test"}
        config = AppConfig.model_validate({
            "dataset": {"type": "bird", "split": "dev", "root_path": "."},
            "vector_database": {"store_root_path": str(Path(self.tmp.name) / "indexes")},
            "value_retrieval": {"llm": llm, "backend": "local_index"},
            **{stage: {"llm": llm} for stage in ("schema_linking", "sql_generation", "sql_revision", "sql_selection")},
        })
        skill = SQLSkill(config, {"data": self.db}, workspace=Path(self.tmp.name) / "requests")
        request = SQLSkillRequest(question="Count rows", database_id="data")
        original = config.model_dump()
        a = skill._runtime_config(Path(self.tmp.name) / "a", request)
        b = skill._runtime_config(Path(self.tmp.name) / "b", request)
        self.assertNotEqual(a.sql_selection_config.save_path, b.sql_selection_config.save_path)
        self.assertEqual(original, config.model_dump())
        with patch.object(skill, "_runner_for_stage", side_effect=AssertionError("must not call model")):
            result = skill.run(request)
        self.assertEqual((result.status, result.failed_stage), ("error", "prepare"))
        self.assertTrue((Path(result.artifacts_dir) / "result.json").exists())
        with self.assertRaises(ValueError):
            skill.run({"question": "Count rows", "database_id": "unknown"})

    def test_blank_request_and_invalid_limits(self):
        for values in ({"question": " "}, {"max_rows": 0}, {"max_rows": 1001}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                SQLSkillRequest.model_validate({"question": "Count rows", "database_id": "data", **values})


class UsageTest(unittest.TestCase):
    def test_recovery_usage_excludes_only_retried_items(self):
        runs = [
            {"stage": "prepare", "status": "failed", "started_at": "2026-01-01T00:00:00+00:00",
             "finished_at": "2026-01-01T00:01:00+00:00", "wall_seconds": 60},
            {"stage": "prepare", "status": "completed", "started_at": "2026-01-01T00:02:00+00:00",
             "finished_at": "2026-01-01T00:03:00+00:00", "wall_seconds": 60},
        ]
        usage = [
            {"stage": "prepare", "item_id": "kept", "timestamp": "2026-01-01T00:00:10+00:00", "total_tokens": 10},
            {"stage": "prepare", "item_id": "retried", "timestamp": "2026-01-01T00:00:20+00:00", "total_tokens": 20},
            {"stage": "prepare", "item_id": "retried", "timestamp": "2026-01-01T00:02:20+00:00", "total_tokens": 30},
        ]
        kept, recovery = separate_recovery_usage(usage, runs)
        self.assertEqual([record["total_tokens"] for record in kept], [10, 30])
        self.assertEqual(recovery["wasted_response_tokens"], 20)
        self.assertEqual(recovery["runs"][0]["retried_item_ids"], ["retried"])

    def test_nested_threads_attribute_usage_and_restore_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "usage.jsonl"
            response = SimpleNamespace(id="fake", usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3, total_tokens=8))
            item = SimpleNamespace(get_item_id=lambda: "42")

            @meter_item
            def call(item):
                with ThreadPoolExecutor(1) as pool:
                    pool.submit(record_usage, response, model="test", elapsed_seconds=1).result()

            with patch.dict(os.environ, {"SQL_RUN_STAGE": "outer"}):
                with _usage_environment(ledger):
                    os.environ["SQL_RUN_STAGE"] = "generation"
                    with ThreadPoolExecutor(1) as pool:
                        pool.submit(call, item).result()
                    record_usage(response, model="test", elapsed_seconds=1)
                self.assertEqual(os.environ["SQL_RUN_STAGE"], "outer")
            records = [json.loads(line) for line in ledger.read_text().splitlines()]
            self.assertEqual([r["item_id"] for r in records], ["42", None])
            self.assertEqual(aggregate_usage(records)["total_tokens"], 16)
            self.assertNotIn("api_key", ledger.read_text())

    def test_partial_multi_sample_response_keeps_valid_choices(self):
        def response(contents, prompt, completion):
            return SimpleNamespace(
                id="fake",
                choices=[SimpleNamespace(message=SimpleNamespace(content=content)) for content in contents],
                usage=SimpleNamespace(
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    total_tokens=prompt + completion,
                ),
            )

        responses = iter([
            response(["A", ""], 10, 4),
            response(["B"], 10, 2),
        ])
        completions = SimpleNamespace(create=lambda **kwargs: next(responses))
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        llm = LLM(LLMConfig(model="test", base_url="http://localhost/v1", api_key="test", max_request_n=2))
        llm._client = client

        messages, usage = llm.ask([{"role": "user", "content": "test"}], n=2)

        self.assertEqual([message.content for message in messages], ["A", "B"])
        self.assertEqual(usage, {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26})


class FailureLedgerTest(unittest.TestCase):
    """Attempts that produced no response must still be accountable."""

    def test_provider_failure_leaves_a_ledger_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "usage.jsonl"

            def boom(**kwargs):
                raise RuntimeError("upstream exploded")

            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
            llm = LLM(LLMConfig(model="test", base_url="http://localhost/v1", api_key="secret-key"))
            llm._client = client

            with _usage_environment(ledger):
                with self.assertRaises(RuntimeError):
                    llm.ask([{"role": "user", "content": "test"}])

            records = [json.loads(line) for line in ledger.read_text().splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "failed")
            self.assertEqual(records[0]["error_type"], "RuntimeError")
            self.assertIsNone(records[0]["completion_tokens"])
            self.assertGreaterEqual(records[0]["elapsed_seconds"], 0)
            # The ledger is safe to ship: no key, no message body.
            self.assertNotIn("secret-key", ledger.read_text())
            self.assertNotIn("upstream exploded", ledger.read_text())

    def test_summary_keeps_failed_attempts_out_of_billed_responses(self):
        from app.skills.sql_skill import _summarize_usage

        records = [
            {"kind": "chat", "status": "success", "usage_available": True, "elapsed_seconds": 1.0,
             "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            {"kind": "chat", "status": "failed", "usage_available": False, "elapsed_seconds": 45.0,
             "prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
            {"kind": "chat", "status": "failed", "usage_available": False, "elapsed_seconds": 45.0,
             "prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
            {"kind": "chat", "usage_available": True, "elapsed_seconds": 2.0,  # legacy line, no status
             "prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "usage.jsonl"
            ledger.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
            summary = _summarize_usage(ledger)

        self.assertEqual(summary["chat"]["responses"], 2)
        self.assertEqual(summary["chat"]["failed_responses"], 2)
        self.assertEqual(summary["chat"]["failed_seconds"], 90.0)
        self.assertEqual(summary["chat"]["total_tokens"], 21)
        self.assertEqual(summary["embedding"]["responses"], 0)


class EvaluationTest(unittest.TestCase):
    def test_empty_invalid_and_duplicate_results_are_explicit(self):
        result = lambda rows: SimpleNamespace(result_rows=rows)
        self.assertEqual(compare_execution(result([]), result([])), (True, True))
        self.assertEqual(compare_execution(result(None), result([])), (False, False))
        self.assertEqual(compare_execution(result([]), result(None)), (None, None))
        self.assertEqual(compare_execution(result([(1,), (1,)]), result([(1,)])), (True, False))

    # The BIRD dev release is gitignored, so this is the only test that cannot
    # run on a clean checkout. Skip it there rather than turn CI red: sampling
    # determinism only matters when the data is actually present. Resolved
    # against PROJECT_ROOT so it also survives being run from another CWD.
    @unittest.skipUnless((PROJECT_ROOT / "data/bird/dev/dev.json").is_file(),
                         "BIRD dev release not downloaded (gitignored)")
    def test_stratified_sample_is_deterministic_and_balanced(self):
        source = PROJECT_ROOT / "data/bird/dev/dev.json"
        databases = ["california_schools", "superhero", "student_club", "toxicology", "thrombosis_prediction"]
        quotas = {"simple": 4, "moderate": 3, "challenging": 3}
        first = select_sample(source, databases, quotas, 20261025)
        second = select_sample(source, databases, quotas, 20261025)
        self.assertEqual([item["question_id"] for item in first], [item["question_id"] for item in second])
        self.assertEqual(Counter(item["db_id"] for item in first), Counter({database: 10 for database in databases}))
        self.assertEqual(Counter(item["difficulty"] for item in first), Counter({"simple": 20, "moderate": 15, "challenging": 15}))


if __name__ == "__main__":
    unittest.main()
