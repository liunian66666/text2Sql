"""``enterprise-agent serve`` — 启动 Web 应用（默认命令）。"""
from __future__ import annotations

import argparse

from .common import DEFAULT_HOST, DEFAULT_PORT, resolve_config


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None,
                        help="Pipeline TOML；默认读 CONFIG_PATH，再回退到 config/local/app-fast/config.toml")
    parser.add_argument("--host", default=DEFAULT_HOST, help="默认 127.0.0.1，仅本机访问")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--reload", action="store_true",
                        help="开发模式：代码变更自动重启。演示与正式运行不要开，重启会把运行中的任务转成 interrupted")


def run(args: argparse.Namespace) -> int:
    resolve_config(args.config)
    import uvicorn

    # 用导入字符串而不是 App 对象：这样 --reload 与 `uvicorn app.main:app` 行为一致，
    # 两条入口共用同一个应用工厂（app/web/server.py:build_app）。
    uvicorn.run("app.main:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")
    return 0
