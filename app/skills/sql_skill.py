"""Single-question Python adapter around the original staged SQL pipeline.

Upstream schema/execution services are process globals, so calls are serialized
within a process. Run separate worker processes when serving concurrent users.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import traceback
from types import SimpleNamespace
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator
from app.config import AppConfig, PROJECT_ROOT
from app.dataset import BaseDataset, DataItem, load_dataset, save_dataset
from app.llm import LLM
from app.llm.budget import BudgetExceededError, reset_hook, reset_meter, set_hook, start_meter
from app.llm.usage import meter_item
from app.services import SchemaService, reset_execution_service, reset_schema_service

_RUN_LOCK = threading.Lock()
STAGES = ("value_retrieval", "few_shot_preparation", "schema_linking", "sql_generation", "sql_revision", "sql_selection")

# Hard per-request caps for the whole pipeline (all stages, all retries, chat + embedding
# combined). The default demo profile disables thinking and uses small sampling budgets;
# these caps turn an overloaded provider or an out-of-scope question into a visible error
# instead of making an interviewer wait indefinitely.
# Passing None disables the guard entirely (batch/eval runs may not want a cap).
DEFAULT_REQUEST_TOKEN_CAP = 80_000
DEFAULT_REQUEST_SECONDS_CAP = 240


class SQLSkillRequest(BaseModel):
    question: str = Field(min_length=1)
    database_id: str = Field(min_length=1)
    evidence: str = ""
    max_rows: int = Field(default=100, ge=1, le=1000)
    sql_timeout_seconds: int = Field(default=30, ge=1, le=600)

    @field_validator("question", "database_id")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class SQLSkillResult(BaseModel):
    request_id: str
    status: Literal["success", "empty_result", "error"]
    sql: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    truncated: bool = False
    elapsed_seconds: float = 0
    stage_seconds: dict[str, float] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    failed_stage: str | None = None
    error: str | None = None
    artifacts_dir: str


class RequestDataset(BaseDataset):
    def __init__(self, config, item):
        self._config = config
        self._data = [item]
        self._database_schema_cache = {}

    def _load_data(self):
        return self._data

    def _get_database_path(self, database_id):
        return self._data[0].database_path


@contextmanager
def _usage_environment(destination):
    keys = ("SQL_USAGE_LEDGER", "SQL_RUN_STAGE")
    previous = {key: os.environ.get(key) for key in keys}
    os.environ["SQL_USAGE_LEDGER"] = str(destination)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def execute_readonly_table(database_path: Path, sql: str, max_rows: int, timeout: int):
    """Restrict final output to read operations, a deadline and bounded rows."""
    deadline = time.perf_counter() + timeout
    connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)
    try:
        connection.execute("PRAGMA query_only=ON")
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

        def authorize(action, arg1, arg2, database, source):
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

        connection.set_authorizer(authorize)
        connection.set_progress_handler(lambda: int(time.perf_counter() >= deadline), 1000)
        cursor = connection.execute(sql)
        if cursor.description is None:
            raise ValueError("SQL must return a result table")
        columns = [field[0] for field in cursor.description]
        rows = cursor.fetchmany(max_rows + 1)
        return columns, [[{"hex": value.hex()} if isinstance(value, bytes) else value for value in row]
                         for row in rows[:max_rows]], len(rows) > max_rows
    finally:
        connection.close()


def _summarize_usage(path):
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    result = {}
    for kind in ("chat", "embedding"):
        selected = [r for r in records if r.get("kind", "chat") == kind]
        result[kind] = {"responses": len(selected), "missing_usage_responses": sum(not r["usage_available"] for r in selected),
                        **{key: sum(r.get(key) or 0 for r in selected) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}}
    return result


class SQLSkill:
    def __init__(self, config, databases: dict[str, str | Path], *, workspace: str | Path | None = None,
                 max_total_tokens: int | None = None, max_seconds: int | None = None):
        self._config: AppConfig = (config if isinstance(config, AppConfig) else config.app_config).model_copy(deep=True)
        self._databases = {name: Path(path).resolve() for name, path in databases.items()}
        self._workspace = Path(workspace or PROJECT_ROOT / "workspace/skill_requests").resolve()
        # Per-request LLM hard caps; a runaway question fails fast instead of
        # burning tokens through the nested retry layers indefinitely.
        self._max_total_tokens = max_total_tokens
        self._max_seconds = max_seconds
        for name, path in self._databases.items():
            if not path.is_file():
                raise FileNotFoundError(f"Database {name!r} does not exist")
            if not name or Path(name).name != name or name in {".", ".."} or any(c in name for c in "/\\:"):
                raise ValueError("Database IDs must be simple directory names")

    def _runtime_config(self, destination: Path, request: SQLSkillRequest):
        cfg = self._config.model_copy(deep=True)
        cfg.run.save_dir = str(destination)
        cfg.run.checkpoint_interval = 1
        cfg.dataset.save_path = str(destination / "dataset.snapshot")
        cfg.dataset.sql_execution_timeout = request.sql_timeout_seconds
        cfg.dataset.max_samples = 1
        for stage in STAGES:
            if stage != "few_shot_preparation":
                getattr(cfg, stage).save_path = str(destination / f"{stage}.snapshot")
        cfg.few_shot_index.prepared_save_path = str(destination / "few_shot_preparation.snapshot")
        cfg.few_shot_index.target_mask_cache_path = str(destination / "target_masks.jsonl")
        for section, key in ((cfg.vector_database, "store_root_path"), (cfg.few_shot_index, "save_path"),
                             (cfg.sql_generation, "icl_few_shot_examples_path")):
            value = getattr(section, key)
            if value and not Path(value).is_absolute():
                setattr(section, key, str(PROJECT_ROOT / value))
        for embedding in (cfg.vector_database, cfg.few_shot_index.embedding):
            if embedding and embedding.api_type == "local" and embedding.local_files_only:
                value = embedding.embedding_model_name_or_path
                if value and not Path(value).is_absolute():
                    embedding.embedding_model_name_or_path = str(PROJECT_ROOT / value)
        return SimpleNamespace(**{name + "_config": getattr(cfg, name) for name in type(cfg).model_fields})

    @staticmethod
    def _runner_for_stage(stage):
        from app.pipeline import ValueRetrievalRunner, SchemaLinkingRunner, SQLGenerationRunner, SQLRevisionRunner, SQLSelectionRunner
        return dict(value_retrieval=ValueRetrievalRunner, schema_linking=SchemaLinkingRunner,
                    sql_generation=SQLGenerationRunner, sql_revision=SQLRevisionRunner,
                    sql_selection=SQLSelectionRunner)[stage]

    @staticmethod
    @meter_item
    def _prepare_few_shots(item, cfg):
        from app.few_shot.preliminary_sql import PreliminarySQLGenerator
        from app.few_shot.retriever import FewShotRetriever
        from app.few_shot.runtime import prepare_few_shot_examples_for_item
        few = cfg.few_shot_index_config
        if few.embedding is None or few.llm is None:
            raise ValueError("Few-shot embedding and LLM configuration are required")
        started = time.perf_counter()
        preliminary = None
        generator = None
        try:
            if few.preliminary_sql.enabled:
                generator = PreliminarySQLGenerator(few.preliminary_sql, cfg.dataset_config,
                    extractor_max_retry=cfg.llm_extractor_config.max_retry, parallelism=cfg.run_config.parallelism)
                preliminary = generator.generate(item)
        finally:
            if generator is not None:
                generator.close()
        retriever = FewShotRetriever.from_index_path(index_path=few.save_path, embedding_config=few.embedding,
            embedding_batch_size=cfg.run_config.embedding_batch_size, similarity_device=few.similarity_device)
        prepared = prepare_few_shot_examples_for_item(item, retriever, LLM(few.llm),
            top_k=few.num_examples, question_weight=few.question_weight, sql_weight=few.sql_weight,
            preliminary_sql=preliminary.sql if preliminary else None, llm_timeout=cfg.run_config.llm_timeout)
        item.few_shot_examples = prepared.examples
        item.few_shot_preliminary_sql = preliminary.sql if preliminary else None
        item.few_shot_preparation_metadata = {"wall_seconds": time.perf_counter() - started,
            "retrieved_example_count": len(prepared.examples), "target_mask_source": prepared.mask_source}

    def run(self, request: SQLSkillRequest | dict, *, on_stage: Callable[[dict], None] | None = None) -> SQLSkillResult:
        request = SQLSkillRequest.model_validate(request)
        if request.database_id not in self._databases:
            raise ValueError(f"Unknown database ID: {request.database_id}")
        started = time.perf_counter()
        request_id = uuid4().hex
        destination = self._workspace / request_id
        destination.mkdir(parents=True, exist_ok=False)
        result = SQLSkillResult(request_id=request_id, status="error", artifacts_dir=str(destination))
        ledger = destination / "usage.jsonl"
        current_stage = "prepare"
        meter_token = start_meter(self._max_total_tokens, self._max_seconds)
        hook_token = set_hook(lambda snapshot: on_stage({
            "stage": "meter", "status": "usage", "request_id": request_id, **snapshot}) if on_stage else None)
        with _RUN_LOCK, _usage_environment(ledger):
            try:
                cfg = self._runtime_config(destination, request)
                from app.db_utils.execution import _execute_sql_cached
                _execute_sql_cached.cache_clear()
                db = self._databases[request.database_id]
                index = Path(cfg.vector_database_config.store_root_path) / request.database_id
                if cfg.value_retrieval_config.backend == "local_index" and not (index / "local_index/manifest.json").exists():
                    raise FileNotFoundError("Prepare the database value index before running this skill")
                provenance = index / "local_index/embedding_provenance.json"
                if provenance.exists():
                    expected = json.loads(provenance.read_text(encoding="utf-8")).get("database_sha256")
                    if expected and expected != hashlib.sha256(db.read_bytes()).hexdigest():
                        raise ValueError("Database changed since value indexing; rebuild its index")
                if not (Path(cfg.few_shot_index_config.save_path) / "manifest.json").exists():
                    raise FileNotFoundError("Prepare the training few-shot index before running this skill")
                schema = SchemaService().load_sqlite_schema(str(db))
                item = DataItem(question_id=0, question=request.question, evidence=request.evidence,
                    gold_sql="", database_id=request.database_id, database_path=str(db), database_schema=schema)
                save_dataset(RequestDataset(cfg.dataset_config, item), cfg.dataset_config.save_path)
                for current_stage in STAGES:
                    os.environ["SQL_RUN_STAGE"] = current_stage
                    if on_stage:
                        on_stage({"stage": current_stage, "status": "started", "request_id": request_id})
                    stage_started = time.perf_counter()
                    if current_stage == "few_shot_preparation":
                        dataset = load_dataset(cfg.value_retrieval_config.save_path)
                        self._prepare_few_shots(dataset[0], cfg)
                        save_dataset(dataset, cfg.few_shot_index_config.prepared_save_path)
                    else:
                        runner = self._runner_for_stage(current_stage).from_config(cfg)
                        try:
                            runner.run()
                        finally:
                            runner._clean_up()
                        dataset = load_dataset(getattr(cfg, current_stage + "_config").save_path)
                        if len(dataset) != 1 or not dataset[0].is_stage_complete(current_stage):
                            raise RuntimeError(f"Stage did not complete: {current_stage}")
                    result.stage_seconds[current_stage] = time.perf_counter() - stage_started
                    if on_stage:
                        on_stage({"stage": current_stage, "status": "completed", "request_id": request_id,
                                  "seconds": result.stage_seconds[current_stage]})
                item = dataset[0]
                result.sql = item.final_selected_sql
                current_stage = "execution"
                if on_stage:
                    on_stage({"stage": current_stage, "status": "started", "request_id": request_id})
                stage_started = time.perf_counter()
                result.columns, result.rows, result.truncated = execute_readonly_table(db, result.sql, request.max_rows, request.sql_timeout_seconds)
                result.stage_seconds[current_stage] = time.perf_counter() - stage_started
                if on_stage:
                    on_stage({"stage": current_stage, "status": "completed", "request_id": request_id,
                              "seconds": result.stage_seconds[current_stage]})
                result.status = "success" if result.rows else "empty_result"
            except Exception as exc:
                result.failed_stage = current_stage
                if on_stage:
                    on_stage({"stage": current_stage, "status": "failed", "request_id": request_id,
                              "error": f"{type(exc).__name__}: stage failed"})
                if isinstance(exc, BudgetExceededError):
                    result.error = (f"预算熔断：{exc}。该问题可能超出数据库可回答范围，"
                                    "请换用库内数据可回答的问题（详见示例题单）")
                else:
                    # Keep detailed upstream exceptions out of public API responses:
                    # provider errors may contain prompt fragments or credentials.
                    result.error = f"{type(exc).__name__}: {current_stage} failed; inspect local stage artifacts"
                details = traceback.format_exc()
                def redact_secrets(value):
                    nonlocal details
                    if isinstance(value, dict):
                        for key, child in value.items():
                            if key == "api_key" and isinstance(child, str) and child:
                                details = details.replace(child, "<redacted>")
                            else:
                                redact_secrets(child)
                    elif isinstance(value, list):
                        for child in value:
                            redact_secrets(child)
                redact_secrets(self._config.model_dump())
                (destination / "error.txt").write_text(details, encoding="utf-8")
            finally:
                reset_execution_service()
                reset_schema_service()
                reset_hook(hook_token)
                reset_meter(meter_token)
                result.elapsed_seconds = time.perf_counter() - started
                result.usage = _summarize_usage(ledger)
                (destination / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result
