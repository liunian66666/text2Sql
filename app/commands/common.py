"""Shared helpers for the CLI subcommands."""
from __future__ import annotations

import os
from pathlib import Path

from app.config import PROJECT_ROOT


DEFAULT_HOST = "127.0.0.1"  # 仅本机；局域网投屏时显式 --host 0.0.0.0
DEFAULT_PORT = 8000

# 迁移期：新路径优先，旧路径回退。config/local/ 被 git 忽略，改名搬不动
# 每个人机器上已有的配置，所以这里必须同时认两个位置。
CONFIG_CANDIDATES = (
    Path("config/local/app-fast/config.toml"),
    Path("config/local/demo-fast/config-bird-demo.toml"),
)


def absolute(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def default_config_path() -> Path | None:
    for candidate in CONFIG_CANDIDATES:
        path = PROJECT_ROOT / candidate
        if path.is_file():
            return path
    return None


def resolve_config(value: str | os.PathLike[str] | None) -> Path:
    """``--config`` > ``CONFIG_PATH`` > 候选默认路径；解析结果写回环境变量。

    必须在 ``get_config()`` 首次调用之前执行，配置是惰性单例。
    """
    if value:
        path = absolute(value)
    elif os.environ.get("CONFIG_PATH"):
        path = absolute(os.environ["CONFIG_PATH"])
    else:
        path = default_config_path()
        if path is None:
            raise FileNotFoundError(
                "找不到配置文件：请用 --config 指定，或先 `cp .env.example .env` 并设置 CONFIG_PATH。"
                f"已尝试：{', '.join(str(PROJECT_ROOT / item) for item in CONFIG_CANDIDATES)}"
            )
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"配置文件不存在：{path}")
    os.environ["CONFIG_PATH"] = str(path)
    return path
