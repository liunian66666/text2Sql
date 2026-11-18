"""Explicit human-review ledger for metric definitions.

Design rule (delegated requirement): **no batch approval**. A metric may only
become ``reviewed=True`` when a *separate* decision file contains an explicit
``decision="approved"`` record naming a human reviewer and the exact metric id.
Candidate review worksheets and the decision ledger are different files: the
generator writes the worksheet only, never the ledger.

Anything else (missing reviewer, "system"/"auto" reviewer, deferred, rejected,
unknown metric, wrong database) is ignored and reported. Registration of a
candidate metric as recallable knowledge is unconditional; promotion to a
*trusted SQL constraint* is not.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .metric_registry import MetricRegistry

DEFAULT_DECISIONS = (
    Path(__file__).resolve().parents[1]
    / "enterprise" / "adventureworks_review_decisions.json"
)

NON_HUMAN_REVIEWERS = {"", "system", "auto", "model", "llm", "agent", "script", "unknown"}


class MetricReviewDecision(BaseModel):
    metric_id: str
    decision: Literal["approved", "rejected", "deferred"]
    reviewer: str
    reviewed_at: str | None = None
    evidence_ref: str = ""
    notes: str = ""


class ReviewLedger(BaseModel):
    database_id: str = "adventureworks"
    decisions: list[MetricReviewDecision] = Field(default_factory=list)
    notice: str = ""

    @classmethod
    def from_file(cls, path: Path | str | None = None) -> "ReviewLedger":
        target = Path(path) if path is not None else DEFAULT_DECISIONS
        if not target.is_file():
            # Absence of a decision file means *no* approvals, never a default grant.
            return cls()
        return cls.model_validate(json.loads(target.read_text(encoding="utf-8")))

    def apply(self, registry: MetricRegistry, *, database_id: str = "adventureworks"
              ) -> tuple[MetricRegistry, dict[str, Any]]:
        """Return a new registry plus a full audit trail of the decision."""
        known = {metric.metric_id for metric in registry.metrics
                 if metric.database_id == database_id}
        approved: list[str] = []
        ignored: list[dict[str, str]] = []
        for decision in self.decisions:
            reason = self._reject_reason(decision, known)
            if reason is not None:
                ignored.append({"metric_id": decision.metric_id, "reason": reason})
                continue
            if decision.metric_id not in approved:
                approved.append(decision.metric_id)

        promoted: list[str] = []
        metrics = []
        for metric in registry.metrics:
            if metric.metric_id in approved:
                # clone() keeps all other fields; only reviewed flips.
                updated = metric.model_copy(update={"reviewed": True})
                metrics.append(updated)
                promoted.append(metric.metric_id)
            else:
                metrics.append(metric)
        result = MetricRegistry(metrics)
        return result, {
            "decisions_seen": len(self.decisions),
            "approved": sorted(approved),
            "promoted": sorted(promoted),
            "ignored": ignored,
            "trusted_after": len(result.trusted_metrics()),
            "pending_after": len(result.pending_review_metrics()),
            "note": ("trusted = reviewed AND no unknown_fields; even an approved metric "
                     "with UNKNOWN fields does not become a hard SQL constraint."),
        }

    @staticmethod
    def _reject_reason(decision: MetricReviewDecision, known: set[str]) -> str | None:
        if decision.decision != "approved":
            return f"decision={decision.decision} (not approved)"
        if decision.metric_id not in known:
            return "metric_id not in this database's registry"
        if decision.reviewer.strip().casefold() in NON_HUMAN_REVIEWERS:
            return "reviewer is missing or not a named human"
        if not decision.reviewer.strip():
            return "reviewer is empty"
        return None
