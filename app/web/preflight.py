"""Local, credential-safe readiness checks for the web application."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT, ENV_FILE, get_config

from .registry import DatabaseRegistry
from .server import resolve_config_path


def _absolute(root: Path, value: str | os.PathLike) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _value_index_facts(manifest_path: Path) -> dict[str, Any]:
    """Read the manifest directly (json only) to keep preflight lightweight.

    Reports the real coverage so a *partial* index is visible instead of being
    flattened to a single ready/not-ready bit.
    """
    if not manifest_path.is_file():
        return {"ready": False, "kind": "missing", "covered_tables": 0, "covered_columns": 0}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ready": False, "kind": "unreadable", "covered_tables": 0, "covered_columns": 0, "error": str(exc)}
    columns = manifest.get("columns") or []
    covered_tables = {entry.get("table_name") for entry in columns if entry.get("table_name")}
    kind = manifest.get("index_kind") or ("complete" if columns else "partial")
    return {
        "ready": True,
        "kind": kind,
        "covered_tables": len(covered_tables),
        "covered_columns": len(columns),
    }


def collect_preflight(
    app_config: Any,
    database_paths: dict[str, Path],
    *,
    project_root: Path,
    evaluations_root: Path,
) -> dict[str, Any]:
    """Return public readiness facts without exposing endpoints or credentials."""
    vector_root = _absolute(project_root, app_config.vector_database.store_root_path)
    few_shot_root = _absolute(project_root, app_config.few_shot_index.save_path)
    databases = []
    for database_id, database_path in sorted(database_paths.items()):
        manifest = vector_root / database_id / "local_index" / "manifest.json"
        index_facts = _value_index_facts(manifest)
        databases.append({
            "id": database_id,
            "database_ready": Path(database_path).is_file(),
            "value_index_ready": index_facts["ready"],
            "value_index_kind": index_facts["kind"],
            "value_index_covered_tables": index_facts["covered_tables"],
            "value_index_covered_columns": index_facts["covered_columns"],
        })
    reports = sorted(
        path.parent.relative_to(evaluations_root).as_posix()
        for path in evaluations_root.rglob("summary.json")
    ) if evaluations_root.exists() else []
    required = [bool(databases), (few_shot_root / "manifest.json").is_file()]
    required.extend(item["database_ready"] and item["value_index_ready"] for item in databases)
    return {
        "ready": all(required),
        "database_count": len(databases),
        "databases": databases,
        "few_shot_index_ready": (few_shot_root / "manifest.json").is_file(),
        # Names and values stay out of the report; only presence is public.
        "env_file": {
            "present": bool(ENV_FILE.get("present")),
            "variable_count": len(ENV_FILE.get("loaded") or []),
            "error": ENV_FILE.get("error"),
        },
        "evaluation_reports": reports,
        "evaluation_report_count": len(reports),
    }


def preflight_report(config_path: str | os.PathLike | None = None) -> dict[str, Any]:
    resolved = resolve_config_path(config_path)
    os.environ["CONFIG_PATH"] = str(resolved)
    loaded = get_config()
    app_config = loaded.app_config if hasattr(loaded, "app_config") else loaded
    registry = DatabaseRegistry(PROJECT_ROOT)
    report = collect_preflight(
        app_config,
        registry.database_paths,
        project_root=PROJECT_ROOT,
        evaluations_root=PROJECT_ROOT / "evals",
    )
    report["config"] = resolved.relative_to(PROJECT_ROOT).as_posix()
    return report
