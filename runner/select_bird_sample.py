"""Create a deterministic database- and difficulty-stratified BIRD sample."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random


DEFAULT_DATABASES = (
    "california_schools",
    "superhero",
    "student_club",
    "toxicology",
    "thrombosis_prediction",
)
DEFAULT_QUOTAS = {"simple": 4, "moderate": 3, "challenging": 3}


def select_sample(source: Path, databases, quotas, seed: int):
    records = json.loads(source.read_text(encoding="utf-8"))
    selected = []
    for database_id in databases:
        database_records = [record for record in records if record.get("db_id") == database_id]
        if not database_records:
            raise ValueError(f"Unknown or empty database: {database_id}")
        for difficulty, quota in quotas.items():
            candidates = sorted(
                (record for record in database_records if record.get("difficulty") == difficulty),
                key=lambda record: record["question_id"],
            )
            if len(candidates) < quota:
                raise ValueError(
                    f"{database_id}/{difficulty} has {len(candidates)} items, needs {quota}"
                )
            picker = random.Random(f"{seed}:{database_id}:{difficulty}")
            selected.extend(sorted(picker.sample(candidates, quota), key=lambda record: record["question_id"]))
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="data/bird/dev/dev.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--databases", nargs="+", default=list(DEFAULT_DATABASES))
    parser.add_argument("--simple", type=int, default=4)
    parser.add_argument("--moderate", type=int, default=3)
    parser.add_argument("--challenging", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20261025)
    args = parser.parse_args()
    source = Path(args.source)
    quotas = {"simple": args.simple, "moderate": args.moderate, "challenging": args.challenging}
    selected = select_sample(source, args.databases, quotas, args.seed)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "seed": args.seed,
        "databases": args.databases,
        "difficulty_quota_per_database": quotas,
        "question_ids": [record["question_id"] for record in selected],
        "database_counts": dict(Counter(record["db_id"] for record in selected)),
        "difficulty_counts": dict(Counter(record["difficulty"] for record in selected)),
        "items": [
            {
                "question_id": record["question_id"],
                "database_id": record["db_id"],
                "difficulty": record["difficulty"],
                "question": record["question"],
            }
            for record in selected
        ],
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("database_counts", "difficulty_counts")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
