"""Phase 1: stable IDs, serialization, reproducible snapshot and CLI artifacts."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from app.commands import build_knowledge
from app.commands.build_knowledge import DEFAULT_METRICS
from app.knowledge.dependencies import build_snapshot
from app.knowledge.metric_registry import MetricRegistry
from app.knowledge.models import (
    KnowledgeSnapshot,
    MetricDefinition,
    canonical_json,
    content_hash,
    stable_column_id,
    stable_relation_id,
    stable_table_id,
)
from app.knowledge.schema_extractor import PROJECT_ROOT, extract_all


class StableIdTest(unittest.TestCase):
    def test_same_column_name_in_two_databases_has_different_id(self):
        sales = stable_column_id("enterprise_sales", "orders", "order_id")
        hr = stable_column_id("enterprise_hr", "orders", "order_id")
        self.assertNotEqual(sales, hr)
        self.assertEqual(sales, "enterprise_sales.orders.order_id")
        self.assertEqual(stable_table_id("enterprise_sales", "orders"),
                         "enterprise_sales.orders")

    def test_ids_are_deterministic_and_never_random(self):
        generated = [stable_column_id("enterprise_sales", "orders", "order_id")
                     for _ in range(20)]
        self.assertEqual(len(set(generated)), 1)

    def test_composite_relation_id_preserves_column_order(self):
        forward = stable_relation_id("db", "child", ["x", "y"], "parent", ["a", "b"], "declared")
        reversed_order = stable_relation_id("db", "child", ["y", "x"], "parent", ["b", "a"], "declared")
        self.assertNotEqual(forward, reversed_order)
        self.assertIn("child(x,y)->parent(a,b)", forward)


class SerializationTest(unittest.TestCase):
    def test_canonical_json_is_key_order_independent(self):
        self.assertEqual(canonical_json({"a": 1, "b": {"x": 2, "y": 3}}),
                         canonical_json({"b": {"y": 3, "x": 2}, "a": 1}))
        self.assertEqual(content_hash({"a": 1}), content_hash({"a": 1}))
        self.assertNotEqual(content_hash({"a": 1}), content_hash({"a": 2}))

    def test_snapshot_roundtrip_preserves_every_field(self):
        registry = MetricRegistry.from_file()
        schemas = extract_all()
        snapshot = build_snapshot(registry, schemas, registry.validate(schemas),
                                  built_at="2026-01-01T00:00:00Z")
        restored = KnowledgeSnapshot.from_json(snapshot.to_json())
        self.assertEqual(restored.metric_count, snapshot.metric_count)
        self.assertEqual(restored.edge_count, snapshot.edge_count)
        self.assertEqual(restored.nodes["column"], snapshot.nodes["column"])
        self.assertEqual(restored.schema_fingerprints, snapshot.schema_fingerprints)

    def test_content_fingerprint_ignores_build_timestamp(self):
        registry = MetricRegistry.from_file()
        schemas = extract_all()
        issues = registry.validate(schemas)
        first = build_snapshot(registry, schemas, issues)
        later = build_snapshot(registry, schemas, issues, built_at="2099-12-31T23:59:59Z")
        self.assertEqual(first.content_fingerprint(), later.content_fingerprint())
        self.assertIsNone(first.built_at)
        self.assertEqual(later.built_at, "2099-12-31T23:59:59Z")

    def test_content_fingerprint_changes_when_knowledge_changes(self):
        registry = MetricRegistry.from_file()
        schemas = extract_all()
        issues = registry.validate(schemas)
        original = build_snapshot(registry, schemas, issues)
        changed = original.model_copy(update={"edge_count": original.edge_count + 1})
        self.assertNotEqual(original.content_fingerprint(), changed.content_fingerprint())

    def test_metric_model_accepts_unknown_without_inventing_values(self):
        metric = MetricRegistry.from_file().get("revenue")
        self.assertIsInstance(metric, MetricDefinition)
        self.assertEqual(metric.grain, "UNKNOWN")
        self.assertEqual(metric.time_field, "UNKNOWN")
        self.assertIn("grain", metric.unknown_fields)


class RealBuildTest(unittest.TestCase):
    def test_real_build_counts_and_trust_boundary(self):
        registry = MetricRegistry.from_file()
        schemas = extract_all()
        issues = registry.validate(schemas)
        snapshot = build_snapshot(registry, schemas, issues)
        self.assertEqual(snapshot.metric_count, 13)
        self.assertEqual(snapshot.table_count, 11)
        self.assertEqual(snapshot.column_count, 57)
        self.assertEqual(snapshot.relation_count, 9)
        self.assertEqual(snapshot.trusted_metric_count, 0)
        self.assertEqual(snapshot.pending_review_metric_count, 13)
        self.assertEqual([i for i in issues if i.severity == "error"], [])
        self.assertEqual(sorted(snapshot.database_ids), ["enterprise_hr", "enterprise_sales"])


class BuildKnowledgeCommandTest(unittest.TestCase):
    @staticmethod
    def _args(output: Path, **overrides):
        values = dict(
            database=None,
            output=str(output),
            metrics=DEFAULT_METRICS,
            project_root=str(PROJECT_ROOT),
            validate_only=False,
            strict=False,
            built_at=None,
            quiet=True,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    @staticmethod
    def _run(args) -> int:
        with redirect_stdout(io.StringIO()):
            return build_knowledge.run(args)

    def test_validate_only_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "knowledge"
            self.assertEqual(self._run(self._args(output, validate_only=True)), 0)
            self.assertFalse(output.exists())

    def test_repeated_builds_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder) / "a", Path(folder) / "b"
            self.assertEqual(self._run(self._args(first)), 0)
            self.assertEqual(self._run(self._args(second)), 0)
            names = sorted(item.name for item in first.iterdir())
            self.assertEqual(names, sorted(item.name for item in second.iterdir()))
            self.assertIn("snapshot.json", names)
            self.assertIn("validation.json", names)
            for name in names:
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(),
                                 f"{name} differed between builds")

    def test_explicit_timestamp_does_not_change_content_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder:
            plain, stamped = Path(folder) / "a", Path(folder) / "b"
            self._run(self._args(plain))
            self._run(self._args(stamped, built_at="2026-11-13T09:00:00+08:00"))
            self.assertEqual(
                json.loads((plain / "summary.json").read_text(encoding="utf-8"))["content_fingerprint"],
                json.loads((stamped / "summary.json").read_text(encoding="utf-8"))["content_fingerprint"],
            )

    def test_single_database_build_is_scoped(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "hr_only"
            self.assertEqual(self._run(self._args(output, database=["enterprise_hr"])), 0)
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["selected_databases"], ["enterprise_hr"])
            self.assertEqual(summary["metric_registry_count"], 13)
            self.assertEqual(summary["metrics_in_snapshot"], 8)
            self.assertEqual(summary["table_count"], 4)
            self.assertFalse((output / "schema_enterprise_sales.json").exists())

    def test_strict_mode_flags_pending_review(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(self._run(self._args(Path(folder) / "s", strict=True)), 1)
            self.assertEqual(self._run(self._args(Path(folder) / "n")), 0)


if __name__ == "__main__":
    unittest.main()
