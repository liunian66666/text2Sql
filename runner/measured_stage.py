"""Run an existing entry point with durable wall-time and LLM-usage records."""
import argparse
import json
import os
from pathlib import Path
import runpy
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("script")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    destination = Path(args.output).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    os.environ["CONFIG_PATH"] = str(Path(args.config).resolve())
    os.environ["SQL_USAGE_LEDGER"] = str(destination / "llm_usage.jsonl")
    os.environ["SQL_RUN_STAGE"] = args.stage
    # Avoid CPU oversubscription during local embedding (the configured pipeline
    # itself already uses worker threads).
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    os.environ.setdefault("MKL_NUM_THREADS", "4")
    start = time.perf_counter()
    record = {"stage": args.stage, "started_at": datetime.now(timezone.utc).isoformat()}
    sys.argv = [args.script, *args.args]
    try:
        runpy.run_path(args.script, run_name="__main__")
        from app.config import get_config
        from app.dataset import load_dataset
        config = get_config()
        pipeline_stages = {"value_retrieval", "schema_linking", "sql_generation", "sql_revision", "sql_selection"}
        if args.stage in pipeline_stages:
            dataset = load_dataset(getattr(config, args.stage + "_config").save_path)
            if not dataset or not all(item.is_stage_complete(args.stage) for item in dataset):
                raise RuntimeError(f"Incomplete stage: {args.stage}")
        elif args.stage == "few_shot_preparation":
            dataset = load_dataset(config.few_shot_index_config.prepared_save_path)
            if not dataset or not all(item.few_shot_examples for item in dataset):
                raise RuntimeError("Few-shot preparation left missing examples")
        record["status"] = "completed"
    except BaseException as exc:
        record["status"] = "completed" if isinstance(exc, SystemExit) and not exc.code else "failed"
        record["error_type"] = type(exc).__name__
        raise
    finally:
        record["wall_seconds"] = time.perf_counter() - start
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        with (destination / "stage_runs.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        print(json.dumps(record), flush=True)


if __name__ == "__main__":
    main()
