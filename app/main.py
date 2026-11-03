"""ASGI 入口：``uvicorn app.main:app``（开发模式加 ``--reload``）。

这里只暴露一个 App，不做参数解析、不自检、不装配依赖——那些在
``app/cli.py`` 与 ``app/web/server.py`` 里。保持这个文件足够薄，是为了让
``uvicorn app.main:app`` 和 ``enterprise-agent serve`` 走完全相同的应用工厂。

配置通过环境变量传入（导入字符串没法带 Python 参数）：

    CONFIG_PATH=config/local/app-fast/config.toml uv run uvicorn app.main:app --reload
"""
from __future__ import annotations

from app.web.server import create_app_from_env

try:
    app = create_app_from_env()
except FileNotFoundError as exc:  # 配置缺失时给出可操作的信息，而不是一句 import 报错
    raise RuntimeError(
        "无法加载配置。请用 CONFIG_PATH 指向一个存在的 TOML，或先 `cp .env.example .env`。"
        f"原始错误：{exc}"
    ) from exc
