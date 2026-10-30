"""Build the HR fixture and write database-truth answers for human/eval comparison."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import PROJECT_ROOT
from app.enterprise import EnterpriseKnowledgeBase, ensure_enterprise_hr_database


def main() -> None:
    database = ensure_enterprise_hr_database(PROJECT_ROOT)
    cases_path = PROJECT_ROOT / "evals/hr/questions.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    knowledge = EnterpriseKnowledgeBase(mode="keyword")
    rows = []
    with closing(sqlite3.connect(database)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        counts = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("departments", "employees", "attendance", "performance_reviews")
        }
        for case in cases:
            cursor = connection.execute(case["truth_sql"])
            metric_ids = [item["id"] for item in knowledge.retrieve(
                case["question"], limit=3, database_id="enterprise_hr")]
            rows.append({
                "id": case["id"],
                "columns": [item[0] for item in cursor.description],
                "rows": [list(row) for row in cursor.fetchall()],
                "expected_metric_id": case["expected_metric_id"],
                "retrieved_metric_ids": metric_ids,
                "metric_hit": case["expected_metric_id"] in metric_ids,
            })
    report = {
        "evaluation": "enterprise-hr-database-truth-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database_id": "enterprise_hr",
        "as_of_date": "2026-10-28",
        "integrity": integrity,
        "foreign_key_error_count": len(foreign_key_errors),
        "table_counts": counts,
        "record_count": sum(counts.values()),
        "metric_hit_count": sum(item["metric_hit"] for item in rows),
        "case_count": len(rows),
        "cases": rows,
    }
    output = PROJECT_ROOT / "evals/hr/truth.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
