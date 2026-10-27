"""Run one LangGraph data-analysis request from the command line."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import PROJECT_ROOT, get_config
from app.demo.registry import DatabaseRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/local/demo-fast/config-bird-demo.toml")
    parser.add_argument("--database", default="enterprise_sales")
    parser.add_argument("--question", required=True)
    parser.add_argument("--evidence", default="")
    args = parser.parse_args()
    config = Path(args.config)
    if not config.is_absolute():
        config = PROJECT_ROOT / config
    os.environ["CONFIG_PATH"] = str(config)

    from app.agent import DataAnalysisAgent
    from app.skills import SQLSkill
    registry = DatabaseRegistry(PROJECT_ROOT)
    skill = SQLSkill(get_config(), registry.database_paths, workspace=PROJECT_ROOT / "workspace/skill_requests",
                     max_total_tokens=80_000, max_seconds=240)
    agent = DataAnalysisAgent(
        skill,
        workspace=PROJECT_ROOT / "workspace/agent_requests",
        databases=registry.database_paths,
        llm_config=get_config().sql_generation_config.llm,
    )
    result = agent.run({"question": args.question, "database_id": args.database, "evidence": args.evidence})
    payload = result.model_dump(exclude={"artifacts_dir"})
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
