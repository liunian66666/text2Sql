"""Phase 4A: selective value-index policy, dry-run budgeting and partial index."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import numpy as np

from app.commands.build_index import _build_policy, _candidate_text_columns, _dry_run_report, _plan
from app.vector_db.local_index import (
    INDEX_INCOMPATIBLE,
    INDEX_STALE,
    LocalValueIndex,
    classify_index_status,
    write_local_index_column,
    write_local_index_manifest,
)
from app.vector_db.value_index_policy import ValueIndexPolicy


class SensitiveFieldTest(unittest.TestCase):
    def test_known_pii_is_rejected(self) -> None:
        policy = ValueIndexPolicy(database_id="aw")
        for table, column in [
            ("Person.EmailAddress", "EmailAddress"),
            ("Person.Password", "PasswordHash"),
            ("Person.Password", "PasswordSalt"),
            ("HumanResources.Employee", "NationalIDNumber"),
            ("Sales.CreditCard", "CardNumber"),
            ("Production.Product", "rowguid"),
        ]:
            include, reason = policy.evaluate(table, column)
            self.assertFalse(include, f"{table}.{column} should be excluded")
            self.assertEqual(reason, "sensitive_field", f"{table}.{column}")

    def test_ordinary_name_is_not_sensitive(self) -> None:
        policy = ValueIndexPolicy(database_id="aw", allowed_tables=["Production.Product"])
        include, reason = policy.evaluate("Production.Product", "Name")
        self.assertTrue(include, reason)


class WhitelistTest(unittest.TestCase):
    def test_table_whitelist_blocks_others(self) -> None:
        policy = ValueIndexPolicy(database_id="aw", allowed_tables=["Production.Product"])
        self.assertTrue(policy.evaluate("Production.Product", "Name")[0])
        include, reason = policy.evaluate("Sales.SalesTerritory", "Name")
        self.assertFalse(include)
        self.assertEqual(reason, "table_not_in_whitelist")

    def test_column_whitelist_and_exclusion(self) -> None:
        policy = ValueIndexPolicy(
            database_id="aw",
            allowed_tables=["Production.Product"],
            allowed_columns=["Production.Product.Name", "Production.Product.Color"],
            excluded_columns=["Production.Product.Color"],
        )
        self.assertTrue(policy.evaluate("Production.Product", "Name")[0])
        include, reason = policy.evaluate("Production.Product", "Color")
        self.assertFalse(include)
        self.assertEqual(reason, "excluded_column:Production.Product.Color")

    def test_column_not_in_whitelist(self) -> None:
        policy = ValueIndexPolicy(
            database_id="aw",
            allowed_tables=["Production.Product"],
            allowed_columns=["Name"],
        )
        include, reason = policy.evaluate("Production.Product", "ProductNumber")
        self.assertFalse(include)
        self.assertEqual(reason, "column_not_in_whitelist")


class DryRunTest(unittest.TestCase):
    def _make_db(self, directory: Path) -> Path:
        db_path = directory / "tiny.sqlite"
        con = sqlite3.connect(db_path)
        con.execute('CREATE TABLE "Products" (Name TEXT, Code TEXT, PasswordHash TEXT)')
        con.executemany(
            'INSERT INTO "Products" (Name, Code, PasswordHash) VALUES (?, ?, ?)',
            [("Alpha", "A1", "secret"), ("Beta", "B2", "secret"), ("Gamma", "A1", "secret")],
        )
        con.commit()
        con.close()
        return db_path

    def test_dry_run_reports_plan_and_budget(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = self._make_db(Path(tmp))
            args = Namespace(
                tables="Products", columns="", exclude_columns="Code",
                max_values=None, max_total_values=None, max_requests=None,
            )
            policy = _build_policy(args, "tiny")
            candidates = _candidate_text_columns(db_path)
            included, excluded = _plan(db_path, policy, candidates)
            # Code is excluded by --exclude-columns, PasswordHash by sensitive policy.
            self.assertEqual([item["column"] for item in included], ["Name"])
            reasons = {item["column"]: item["reason"] for item in excluded}
            self.assertEqual(reasons["Code"], "excluded_column:Code")
            self.assertEqual(reasons["PasswordHash"], "sensitive_field")

            report = _dry_run_report(db_path, policy, included, excluded, batch_size=2)
            self.assertEqual(report["included_column_count"], 1)
            self.assertEqual(report["total_values_after_cap"], 3)  # Alpha/Beta/Gamma
            self.assertEqual(report["estimated_embedding_requests"], 2)  # ceil(3 / 2)
            self.assertTrue(report["within_max_total_values"])
            self.assertTrue(report["within_max_requests"])

    def test_dry_run_flags_exceeded_budget(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = self._make_db(Path(tmp))
            policy = ValueIndexPolicy(database_id="tiny", allowed_tables=["Products"],
                                      allowed_columns=["Name"], max_total_values=1)
            candidates = _candidate_text_columns(db_path)
            included, excluded = _plan(db_path, policy, candidates)
            report = _dry_run_report(db_path, policy, included, excluded, batch_size=2)
            self.assertFalse(report["within_max_total_values"])

    def test_request_estimate(self) -> None:
        policy = ValueIndexPolicy(database_id="aw")
        self.assertEqual(policy.estimated_requests(0, 128), 0)
        self.assertEqual(policy.estimated_requests(1, 128), 1)
        self.assertEqual(policy.estimated_requests(129, 128), 2)


class PartialManifestTest(unittest.TestCase):
    def test_partial_index_degrades_uncovered_columns(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            index = Path(tmp) / "local_index"
            rng = np.random.default_rng(0)
            entries = [
                write_local_index_column(index, "products", "name", ["Alpha", "Beta"],
                                         rng.random((2, 8)).tolist()),
            ]
            write_local_index_manifest(index, entries, meta={
                "index_kind": "partial",
                "covered_tables": ["products"],
                "database_fingerprint": "db-1",
                "embedding_model": "fake",
                "embedding_signature": "sig-1",
            })
            manifest = json.loads((index / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["index_kind"], "partial")

            value_index = LocalValueIndex(index, device="cpu")
            covered = value_index.retrieve_values_for_column(
                query_embeddings=rng.random((1, 8)).tolist(),
                table_name="products", column_name="name",
                max_values_per_column=5, lower_meta_data=True,
            )
            self.assertEqual(len(covered["values"]), 2)

            uncovered = value_index.retrieve_values_for_column(
                query_embeddings=rng.random((1, 8)).tolist(),
                table_name="products", column_name="color",
                max_values_per_column=5, lower_meta_data=True,
            )
            self.assertEqual(uncovered["values"], [])

            status = classify_index_status(index, expected_signature="sig-1",
                                           database_fingerprint="db-1")
            self.assertEqual(status["status"], "partial")
            self.assertEqual(status["covered_tables"], ["products"])
            self.assertEqual(status["covered_columns"], 1)

    def test_signature_and_fingerprint_detection(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            index = Path(tmp) / "local_index"
            entries = [{"table_name": "t", "column_name": "c", "column_key": "t\tc",
                        "embeddings_file": "columns/x.npy", "documents_file": "columns/x.json",
                        "count": 1, "embedding_dim": 8}]
            write_local_index_manifest(index, entries, meta={
                "index_kind": "partial", "embedding_signature": "sig-A",
                "database_fingerprint": "db-A",
            })
            self.assertEqual(
                classify_index_status(index, expected_signature="sig-B")["status"],
                INDEX_INCOMPATIBLE,
            )
            self.assertEqual(
                classify_index_status(index, expected_signature="sig-A",
                                      database_fingerprint="db-B")["status"],
                INDEX_STALE,
            )

    def test_missing_index_is_reported(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            status = classify_index_status(Path(tmp) / "local_index")
            self.assertEqual(status["status"], "missing")


if __name__ == "__main__":
    unittest.main()
