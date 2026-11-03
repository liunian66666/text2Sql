"""装配 Web 应用 ASGI 应用。

装配顺序（唯一约束：get_config() 是惰性单例，首次调用前必须设好 CONFIG_PATH）：

    解析配置路径 → 写入 CONFIG_PATH → 组装三大依赖 → create_app(...)

三大依赖：
    DatabaseRegistry  扫描白名单 sqlite 库 + 读取示例题单（只读）
    DataAnalysisAgent ChatOpenAI + LangGraph 编排器，内部把 SQLSkill 作为工具
    TaskManager       串行任务队列 + 事件回放，状态落盘 workspace/state/agent_state.sqlite

两种启动方式：
    enterprise-agent serve                      # 带 --config/--host/--port 参数
    uvicorn app.main:app                        # 标准 ASGI 入口，配置走 CONFIG_PATH
"""
from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

from app.config import PROJECT_ROOT, get_config
from app.persistence import SQLitePersistence

from app.api import create_app
from .registry import DatabaseRegistry
from .serializers import EvaluationRepository
from .task_manager import TaskManager

DEFAULT_CONFIG_PATH = Path("config/local/app-fast/config.toml")
# 迁移期兼容：旧目录名还认得，避免升级后每个人都要手动搬一次本地配置
LEGACY_CONFIG_PATH = Path("config/local/demo-fast/config-bird-demo.toml")
FALLBACK_CONFIG_PATH = Path("config/local/baseline-dashscope/config-bird-dev-50-ablation.toml")

# 单次请求的产物根目录：每个请求一个子目录，浏览器刷新后仍可按请求 ID 查询。
# 旧版写在 workspace/demo_requests/，那个目录保留为只读历史，不再产生新文件。
SQL_REQUESTS_DIR = PROJECT_ROOT / "workspace/sql_requests"
AGENT_REQUESTS_DIR = PROJECT_ROOT / "workspace/agent_requests"
STATE_DATABASE = PROJECT_ROOT / "workspace/state/agent_state.sqlite"


def _cap_from_env(name: str, legacy: str, default: int) -> int:
    """读取单请求熔断阈值：新变量优先，旧变量在过渡期内仍然生效。"""
    for key in (name, legacy):
        value = os.environ.get(key)
        if value:
            if key == legacy:
                logger.warning(f"{legacy} 已更名为 {name}，旧变量将在下一版移除")
            return int(value)
    return default


def resolve_config_path(config_path: str | os.PathLike | None) -> Path:
    """把 --config 值解析为绝对路径；未指定时按 新路径 → 旧路径 → 兜底 依次尝试。"""
    if config_path:
        path = Path(config_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.is_file():
            raise FileNotFoundError(f"Config file not found: {path}")
        return path
    for candidate in (DEFAULT_CONFIG_PATH, LEGACY_CONFIG_PATH, FALLBACK_CONFIG_PATH):
        path = PROJECT_ROOT / candidate
        if path.is_file():
            return path
    raise FileNotFoundError(
        "No config file found. 请把配置放到 "
        f"{PROJECT_ROOT / DEFAULT_CONFIG_PATH}，或用 --config / CONFIG_PATH 指定。"
    )


def build_app(config_path: str | os.PathLike | None = None):
    """装配完整 Web 应用。CLI 的 serve 与 app.main:app 共用此函数。"""
    resolved = resolve_config_path(config_path)
    os.environ["CONFIG_PATH"] = str(resolved)  # get_config() 首次调用前设置即可（惰性单例）

    registry = DatabaseRegistry(PROJECT_ROOT)
    if not registry.database_paths:
        raise RuntimeError("No registered application databases were found")

    persistence = SQLitePersistence(STATE_DATABASE)
    persistence.state.import_legacy_tasks(PROJECT_ROOT / "workspace/demo_tasks")

    from app.skills import DEFAULT_REQUEST_SECONDS_CAP, DEFAULT_REQUEST_TOKEN_CAP, SQLSkill  # 延迟导入：SQLSkill 构造链较重，装配期才需要

    skill = SQLSkill(
        get_config(),
        registry.database_paths,
        workspace=SQL_REQUESTS_DIR,
        # 单请求硬上限：超限即熔断报错，防止超纲问题在重试层里无限烧 token。
        # 默认值与 CLI 入口共用（见 app/skills/sql_skill.py），环境变量可临时放宽。
        max_total_tokens=_cap_from_env("AGENT_MAX_TOTAL_TOKENS", "DEMO_MAX_TOTAL_TOKENS",
                                      DEFAULT_REQUEST_TOKEN_CAP),
        max_seconds=_cap_from_env("AGENT_MAX_SECONDS", "DEMO_MAX_SECONDS",
                                  DEFAULT_REQUEST_SECONDS_CAP),
    )
    from app.agent import DataAnalysisAgent
    from app.enterprise import create_enterprise_knowledge
    app_config = get_config().app_config
    agent = DataAnalysisAgent(
        skill,
        workspace=AGENT_REQUESTS_DIR,
        databases=registry.database_paths,
        llm_config=get_config().sql_generation_config.llm,
        knowledge=create_enterprise_knowledge(app_config),
        checkpointer=persistence.checkpointer,
    )
    manager = TaskManager(agent, persistence.state)
    web_app = create_app(
        manager=manager,
        registry=registry,
        evaluations=EvaluationRepository(PROJECT_ROOT / "evals"),
    )
    web_app.state.persistence = persistence

    @web_app.on_event("shutdown")
    def close_persistence() -> None:
        # 先停 worker 再关库：store 现在持有一条长连接，worker 还在写的时候关掉
        # 会变成 "Cannot operate on a closed database"。
        manager.close()
        persistence.close()

    return web_app


def create_app_from_env():
    """uvicorn --factory 入口：CONFIG_PATH 由外部环境变量决定，未设置时用默认配置。"""
    return build_app(os.environ.get("CONFIG_PATH"))
