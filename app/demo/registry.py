from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Any


DISPLAY_NAMES = {
    "enterprise_sales": "企业电商销售分析",
    "california_schools": "California Schools",
    "superhero": "Superhero",
    "student_club": "Student Club",
    "toxicology": "Toxicology",
    "thrombosis_prediction": "Thrombosis Prediction",
}

# Databases whose value index has been rebuilt for the embedding model that the
# active config selects. Vectors from text-embedding-v4 are not comparable with
# qwen3.7-text-embedding-flash, and an unindexed database fails inside
# value_retrieval with "Prepare the database value index" -- showing it in the
# picker only turns a known gap into a live demo error. Rebuild with
# `runner/complete_value_index.py --database <path>`, then add the ID back here.
DEMO_READY_DATABASES = ("enterprise_sales",)


class DatabaseRegistry:
    """Expose only explicitly discovered BIRD databases and curated examples."""

    def __init__(self, project_root: Path, sample_path: Path | None = None):
        self.project_root = project_root.resolve()
        self.sample_path = (sample_path or project_root / "evals/bird-dev-50/sample.json").resolve()
        self._databases = self._discover_databases()
        self._examples = self._load_examples()

    def _discover_databases(self) -> dict[str, Path]:
        database_root = self.project_root / "data/bird/dev/dev_databases"
        allowed = set(DEMO_READY_DATABASES)
        databases: dict[str, Path] = {}
        from app.enterprise import ensure_enterprise_demo_database
        enterprise = ensure_enterprise_demo_database(self.project_root)
        databases["enterprise_sales"] = enterprise
        for database_id in sorted(allowed):
            candidate = database_root / database_id / f"{database_id}.sqlite"
            if candidate.is_file():
                databases[database_id] = candidate.resolve()
        return databases

    def _load_examples(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        enterprise_examples = Path(__file__).parents[1] / "enterprise" / "examples.json"
        if enterprise_examples.is_file():
            grouped["enterprise_sales"] = json.loads(enterprise_examples.read_text(encoding="utf-8"))
        if not self.sample_path.exists():
            return grouped
        sample = json.loads(self.sample_path.read_text(encoding="utf-8"))
        evidence_by_id: dict[int, str] = {}
        source = Path(sample.get("source", ""))
        if source and not source.is_absolute():
            source = self.project_root / source
        if source.is_file():
            for item in json.loads(source.read_text(encoding="utf-8")):
                evidence_by_id[item.get("question_id")] = item.get("evidence", "")
        for item in sample.get("items", []):
            database_id = item.get("database_id")
            if database_id not in self._databases or len(grouped[database_id]) >= 4:
                continue
            grouped[database_id].append({
                "question_id": item.get("question_id"),
                "difficulty": item.get("difficulty", ""),
                "question": item.get("question", ""),
                "evidence": evidence_by_id.get(item.get("question_id"), ""),
            })
        return grouped

    @property
    def database_paths(self) -> dict[str, Path]:
        return dict(self._databases)

    def public_entries(self) -> list[dict[str, Any]]:
        order = {"enterprise_sales": 0}
        return [
            {
                "id": database_id,
                "name": DISPLAY_NAMES.get(database_id, database_id.replace("_", " ").title()),
                "examples": self._examples.get(database_id, []),
            }
            for database_id in sorted(self._databases, key=lambda item: (order.get(item, 1), item))
        ]
