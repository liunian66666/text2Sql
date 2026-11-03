"""``enterprise-agent ask`` — 单问题跑一次 Agent，直接输出 JSON。"""
from __future__ import annotations

import argparse
import json

from .common import resolve_config


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None)
    parser.add_argument("--database", default="enterprise_sales",
                        help="数据源 ID；默认 enterprise_sales，跨域问题可用 auto")
    parser.add_argument("--question", required=True)
    parser.add_argument("--evidence", default="", help="用户补充口径，会带来源标签注入 SQL Skill")
    parser.add_argument("--conversation-id", default=None, help="复用会话以带上多轮上下文")


def run(args: argparse.Namespace) -> int:
    resolve_config(args.config)

    from app.agent import DataAnalysisAgent
    from app.config import PROJECT_ROOT, get_config
    from app.enterprise import create_enterprise_knowledge
    from app.skills import SQLSkill
    from app.web.registry import DatabaseRegistry

    registry = DatabaseRegistry(PROJECT_ROOT)
    skill = SQLSkill(get_config(), registry.database_paths,
                     workspace=PROJECT_ROOT / "workspace/skill_requests",
                     max_total_tokens=80_000, max_seconds=240)
    agent = DataAnalysisAgent(
        skill,
        workspace=PROJECT_ROOT / "workspace/agent_requests",
        databases=registry.database_paths,
        llm_config=get_config().sql_generation_config.llm,
        knowledge=create_enterprise_knowledge(get_config().app_config),
    )
    request = {"question": args.question, "database_id": args.database,
               "evidence": args.evidence}
    if args.conversation_id:
        request["conversation_id"] = args.conversation_id
    result = agent.run(request)
    print(json.dumps(result.model_dump(exclude={"artifacts_dir"}),
                     ensure_ascii=False, indent=2, default=str))
    return 0 if result.status in {"success", "empty_result"} else 1
