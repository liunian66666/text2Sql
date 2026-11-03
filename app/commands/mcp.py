"""``enterprise-agent mcp`` — 把同一套能力暴露为 MCP Server。"""
from __future__ import annotations

import argparse

from .common import resolve_config


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None)
    parser.add_argument("--transport", choices=("stdio", "sse", "streamable-http"),
                        default="stdio")


def run(args: argparse.Namespace) -> int:
    resolve_config(args.config)

    from app.config import PROJECT_ROOT, get_config
    from app.enterprise import create_enterprise_knowledge
    from app.mcp_server import create_mcp_server
    from app.skills import SQLSkill
    from app.web.registry import DatabaseRegistry

    registry = DatabaseRegistry(PROJECT_ROOT)
    skill = SQLSkill(get_config(), registry.database_paths,
                     workspace=PROJECT_ROOT / "workspace/mcp_requests")
    server = create_mcp_server(
        sql_skill=skill,
        databases=registry.database_paths,
        knowledge=create_enterprise_knowledge(get_config().app_config),
    )
    server.run(transport=args.transport)
    return 0
