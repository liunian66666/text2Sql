from __future__ import annotations

import json
from pathlib import Path


class EnterpriseKnowledgeBase:
    def __init__(self, path: Path | None = None):
        path = path or Path(__file__).with_name("business_metrics.json")
        self.metrics = json.loads(path.read_text(encoding="utf-8"))["metrics"]

    def retrieve(self, question: str, limit: int = 3) -> list[dict]:
        normalized = question.lower()
        scored = []
        for metric in self.metrics:
            score = sum(2 if keyword.lower() in normalized else 0 for keyword in metric["keywords"])
            if metric["name"].lower() in normalized:
                score += 3
            if score:
                scored.append((score, metric))
        return [metric for _, metric in sorted(scored, key=lambda pair: -pair[0])[:limit]]

    @staticmethod
    def as_evidence(metrics: list[dict]) -> str:
        return "\n".join(
            f"业务口径[{metric['name']}]: {metric['definition']} SQL规则: {metric['sql_rule']}"
            for metric in metrics
        )
