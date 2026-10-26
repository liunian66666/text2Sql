"""装配演示站 ASGI 应用。

装配顺序（唯一约束：get_config() 是惰性单例，首次调用前必须设好 CONFIG_PATH）：

    解析配置路径 → 写入 CONFIG_PATH → 组装三大依赖 → create_app(...)

三大依赖：
    DatabaseRegistry  扫描白名单 sqlite 库 + 读取示例题单（只读）
    SQLSkill          流水线本体，进程级单例（上游服务为进程全局，故整站单进程）
    TaskManager       串行任务队列 + 事件回放，状态落盘 workspace/demo_tasks/

两种启动方式：
    python runner/run_demo.py                                  # 带 --config/--host/--port 参数
    uvicorn app.demo.server:create_app_from_env --factory      # 零参工厂，用默认配置
"""
from __future__ import annotations

import os
from pathlib import Path

from app.config import PROJECT_ROOT, get_config

from .api import create_app
from .registry import DatabaseRegistry
from .serializers import EvaluationRepository
from .task_manager import TaskManager

DEFAULT_CONFIG_PATH = Path("config/local/demo-fast/config-bird-demo.toml")
FALLBACK_CONFIG_PATH = Path("config/local/baseline-dashscope/config-bird-dev-50-ablation.toml")

# 演示产物根目录：每个请求一个子目录，浏览器刷新后仍可按请求 ID 查询。
DEMO_REQUESTS_DIR = PROJECT_ROOT / "workspace/demo_requests"
DEMO_TASKS_DIR = PROJECT_ROOT / "workspace/demo_tasks"


def resolve_config_path(config_path: str | os.PathLike | None) -> Path:
    """把 --config 值解析为绝对路径；未指定时用默认配置，缺失则明确报错。"""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if config_path is None and not path.is_file():
        path = PROJECT_ROOT / FALLBACK_CONFIG_PATH
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    return path


def build_app(config_path: str | os.PathLike | None = None):
    """装配完整演示应用。runner/run_demo.py 与 create_app() 工厂共用此函数。"""
    resolved = resolve_config_path(config_path)
    os.environ["CONFIG_PATH"] = str(resolved)  # get_config() 首次调用前设置即可（惰性单例）

    registry = DatabaseRegistry(PROJECT_ROOT)
    if not registry.database_paths:
        raise RuntimeError("No registered demo databases were found")

    from app.skills import DEFAULT_REQUEST_SECONDS_CAP, DEFAULT_REQUEST_TOKEN_CAP, SQLSkill  # 延迟导入：SQLSkill 构造链较重，装配期才需要

    skill = SQLSkill(
        get_config(),
        registry.database_paths,
        workspace=DEMO_REQUESTS_DIR,
        # 单请求硬上限：超限即熔断报错，防止超纲问题在重试层里无限烧 token。
        # 默认值与 CLI 入口共用（见 app/skills/sql_skill.py），环境变量可临时放宽。
        max_total_tokens=int(os.environ.get("DEMO_MAX_TOTAL_TOKENS", str(DEFAULT_REQUEST_TOKEN_CAP))),
        max_seconds=int(os.environ.get("DEMO_MAX_SECONDS", str(DEFAULT_REQUEST_SECONDS_CAP))),
    )
    manager = TaskManager(skill, DEMO_TASKS_DIR)
    return create_app(
        manager=manager,
        registry=registry,
        evaluations=EvaluationRepository(PROJECT_ROOT / "evals"),
    )


def create_app_from_env():
    """uvicorn --factory 入口：CONFIG_PATH 由外部环境变量决定，未设置时用默认配置。"""
    return build_app(os.environ.get("CONFIG_PATH"))
