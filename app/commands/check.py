"""``enterprise-agent check`` — 启动前自检，不启动服务、不调用模型。"""
from __future__ import annotations

import argparse
import json

from .common import resolve_config


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None)
    parser.add_argument("--quiet", action="store_true", help="只输出 ready 布尔值")


def run(args: argparse.Namespace) -> int:
    from app.web.preflight import preflight_report

    path = resolve_config(args.config)
    report = preflight_report(str(path))
    if args.quiet:
        print("ready" if report["ready"] else "not ready")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 1
