"""Resume the full baseline using existing stage checkpoints and durable metrics."""
import argparse
from pathlib import Path
import subprocess
import sys

STAGES = [
    ("value_index", "runner/complete_value_index.py"),
    ("few_shot_index", "runner/build_few_shot_index.py"),
    ("value_retrieval", "runner/run_value_retrieval.py"),
    ("few_shot_preparation", "runner/run_few_shot_preparation.py"),
    ("schema_linking", "runner/run_schema_linking.py"),
    ("sql_generation", "runner/run_sql_generation.py"),
    ("sql_revision", "runner/run_sql_revision.py"),
    ("sql_selection", "runner/run_sql_selection.py"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--from-stage", choices=[stage for stage, _ in STAGES])
    parser.add_argument("--to-stage", choices=[stage for stage, _ in STAGES])
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    start = next((index for index, (stage, _) in enumerate(STAGES) if stage == args.from_stage), 0)
    end = next((index + 1 for index, (stage, _) in enumerate(STAGES) if stage == args.to_stage), len(STAGES))
    if start >= end:
        raise ValueError("--from-stage must not come after --to-stage")
    for stage, script in STAGES[start:end]:
        print(f"Starting {stage}", flush=True)
        command = [sys.executable, "-B", "runner/measured_stage.py", "--config", args.config,
                   "--stage", stage, "--output", str(output / "measurement"), script]
        with (output / f"{stage}.log").open("a", encoding="utf-8") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        print(f"Completed {stage}", flush=True)


if __name__ == "__main__":
    main()
