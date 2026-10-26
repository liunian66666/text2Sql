"""CLI for one question. No benchmark labels or dataset preprocessing required."""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--database-id", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--evidence", default="")
    parser.add_argument("--max-rows", type=int, default=100)
    args = parser.parse_args()
    os.environ["CONFIG_PATH"] = args.config
    from app.config import get_config
    from app.skills import DEFAULT_REQUEST_SECONDS_CAP, DEFAULT_REQUEST_TOKEN_CAP, SQLSkill, SQLSkillRequest
    # Same per-request hard caps as the demo site, so a runaway question cannot burn
    # tokens through the nested retry layers forever from the command line either.
    skill = SQLSkill(get_config(), {args.database_id: args.database},
                     max_total_tokens=DEFAULT_REQUEST_TOKEN_CAP,
                     max_seconds=DEFAULT_REQUEST_SECONDS_CAP)
    result = skill.run(SQLSkillRequest(question=args.question, database_id=args.database_id,
                                      evidence=args.evidence, max_rows=args.max_rows))
    print(result.model_dump_json(indent=2))
    if result.status == "error":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
