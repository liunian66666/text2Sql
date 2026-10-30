"""Expose DeepEye SQL tools as an MCP server (stdio by default)."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import PROJECT_ROOT, get_config
from app.demo.registry import DatabaseRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/local/demo-fast/config-bird-demo.toml")
    parser.add_argument("--transport", choices=("stdio", "sse", "streamable-http"), default="stdio")
    args = parser.parse_args()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    os.environ["CONFIG_PATH"] = str(config_path.resolve())

    from app.mcp_server import create_mcp_server
    from app.enterprise import create_enterprise_knowledge
    from app.skills import SQLSkill
    registry = DatabaseRegistry(PROJECT_ROOT)
    skill = SQLSkill(get_config(), registry.database_paths, workspace=PROJECT_ROOT / "workspace/mcp_requests")
    server = create_mcp_server(
        sql_skill=skill, databases=registry.database_paths,
        knowledge=create_enterprise_knowledge(get_config().app_config),
    )
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
