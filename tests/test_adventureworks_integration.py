"""AdventureWorks data-asset + knowledge-layer integration tests.

Offline tests always run (the sample SQLite file is gitignored). Database tests
are skipped automatically when ``data/adventureworks/adventureworks.sqlite`` is
absent, so CI on a clean checkout stays green.

Data provenance: Microsoft public SAMPLE database (MIT), not real production
data. All policies are SIMULATED / UNREVIEWED, not Microsoft official rules.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from app.agent.graph import build_agent_graph
from app.agent.tools.registry import ToolRegistry
from app.commands import build_knowledge
from app.enterprise.adventureworks import DATABASE_ID, SOURCE_SHA256, sql_dialect_hint
from app.knowledge.metric_registry import MetricRegistry
from app.knowledge.models import MetricDefinition
from app.knowledge.schema_extractor import (
    ENTERPRISE_DATABASE_IDS,
    KNOWN_DATABASE_IDS,
    extract_database_schema,
)
from app.services.schema_service import SchemaService
from app.skills.sql_skill import execute_readonly_table
from app.web.registry import DatabaseRegistry

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "adventureworks" / "adventureworks.sqlite"
METRICS = ROOT / "app" / "enterprise" / "adventureworks_metrics.json"
EVAL_DIR = ROOT / "evals" / "adventureworks-graphrag"
POLICY_DIR = ROOT / "docs" / "simulated-policies"
LICENSE = ROOT / "docs" / "adventureworks" / "LICENSE-microsoft-sql-server-samples.txt"
HAS_DB = DB.is_file()


def _load_metrics():
    return json.loads(METRICS.read_text(encoding="utf-8"))


def _read_sql(sql: str):
    columns, rows, _ = execute_readonly_table(DB, sql, max_rows=50, timeout=30)
    return columns, rows


class ProvenanceAndAssetTest(unittest.TestCase):
    """Runs everywhere, including CI without the 126 MB sample database."""

    def test_official_mit_license_is_vendored_with_known_hash(self):
        self.assertTrue(LICENSE.is_file())
        digest = hashlib.sha256(LICENSE.read_bytes()).hexdigest()
        self.assertEqual(digest, "201f3705979e932c92cd3c6ddbddf4dc3c71dcc5a82089a1e4018227cfe1cf09")
        self.assertIn("MIT License", LICENSE.read_text(encoding="utf-8"))
        self.assertIn("Microsoft Corporation", LICENSE.read_text(encoding="utf-8"))

    def test_provenance_doc_discloses_sample_data_and_simulated_policies(self):
        text = (ROOT / "docs" / "adventureworks" / "PROVENANCE.md").read_text(encoding="utf-8")
        for phrase in ("不是真实生产企业数据", "SIMULATED", "不是微软官方业务规则",
                       "MIT", SOURCE_SHA256, "71", "759,240"):
            self.assertIn(phrase, text, phrase)

    def test_all_policy_documents_are_explicitly_marked_simulated(self):
        files = sorted(POLICY_DIR.glob("*.md"))
        self.assertEqual(len(files), 13)
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertIn("SIMULATED", text, path.name)
            self.assertIn("不是微软官方业务规则", text, path.name)
            self.assertIn("不是真实生产企业数据", text, path.name)
            self.assertIn("UNREVIEWED", text, path.name)

    def test_metric_registry_has_38_structured_untrusted_metrics(self):
        payload = _load_metrics()
        metrics = payload["metrics"]
        self.assertEqual(len(metrics), 38)
        self.assertTrue(all(m["database_id"] == DATABASE_ID for m in metrics))
        self.assertTrue(all(m["structured"]["evidence_status"] == "partial" for m in metrics))
        # reviewed can never be turned on by the source file.
        self.assertTrue(all("reviewed" not in m["structured"] for m in metrics))
        self.assertTrue(all(m["structured"]["source_ref"].startswith("docs/simulated-policies/")
                            for m in metrics))
        ids = [m["id"] for m in metrics]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_metric_formula_is_a_verbatim_policy_quote(self):
        for metric in _load_metrics()["metrics"]:
            ref = metric["structured"]["source_ref"]
            relative = ref.split("#", 1)[0]
            document = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn(metric["formula"], document, metric["id"])

    def test_dotted_schema_table_names_resolve_with_rsplit(self):
        metric = MetricDefinition(
            metric_id="probe", name="probe", domain="sales",
            database_id="adventureworks", description="probe",
            source_tables=["Sales.SalesOrderHeader"],
            source_columns=["Sales.SalesOrderHeader.SubTotal"],
            source_ref="probe",
        )
        self.assertEqual(
            metric.column_ids,
            ["adventureworks.Sales.SalesOrderHeader.SubTotal"])

    def test_build_knowledge_scope_keeps_phase1_default_unchanged(self):
        self.assertEqual(ENTERPRISE_DATABASE_IDS, ("enterprise_sales", "enterprise_hr"))
        self.assertEqual(KNOWN_DATABASE_IDS,
                         ("enterprise_sales", "enterprise_hr", "adventureworks"))

    def test_dialect_hint_is_scoped_to_adventureworks_only(self):
        self.assertIn('"Sales.SalesOrderHeader"', sql_dialect_hint("adventureworks"))
        self.assertIn("CAST", sql_dialect_hint("adventureworks"))
        self.assertEqual(sql_dialect_hint("enterprise_sales"), "")
        self.assertEqual(sql_dialect_hint("enterprise_hr"), "")
        self.assertEqual(sql_dialect_hint(None), "")

    def test_registry_default_does_not_expose_sample_database(self):
        # 门控本身仍是显式 opt-in：这里显式关闭开关，验证默认不暴露示例库。
        # （本机 .env 可以打开它用于前端演示，但那属于运行配置，不是默认值。）
        with patch.dict(os.environ, {"ENABLE_ADVENTUREWORKS": ""}):
            registry = DatabaseRegistry(ROOT)
            self.assertEqual(set(registry.database_paths),
                             {"enterprise_sales", "enterprise_hr"})
            self.assertEqual([entry["id"] for entry in registry.public_entries()],
                             ["enterprise_sales", "enterprise_hr"])

    def test_eval_splits_are_isolated_and_all_reference_sql_verified(self):
        dev = json.loads((EVAL_DIR / "dev.json").read_text(encoding="utf-8"))
        test = json.loads((EVAL_DIR / "test.json").read_text(encoding="utf-8"))
        self.assertEqual(dev["case_count"], 12)
        self.assertEqual(test["case_count"], 6)
        dev_ids = {c["id"] for c in dev["cases"]}
        test_ids = {c["id"] for c in test["cases"]}
        self.assertFalse(dev_ids & test_ids)
        for payload in (dev, test):
            self.assertTrue(all(c["reference_sql_verified"] for c in payload["cases"]))
            self.assertTrue(all(c["reference_result"]["rows"] for c in payload["cases"]))
            self.assertTrue(all(c["database_id"] == DATABASE_ID for c in payload["cases"]))
            self.assertIn("不是真实生产企业数据", payload["source_notice"])
        categories = {c["category"] for c in dev["cases"]}
        self.assertEqual(categories, {"intermediate_table", "multi_join_path",
                                      "historical_cost", "grain_conflict"})
        self.assertIn("不得", test["isolation"])

    def test_examples_do_not_leak_independent_test_questions(self):
        examples = json.loads((ROOT / "app" / "enterprise" /
                               "adventureworks_examples.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(examples), 3)
        test = json.loads((EVAL_DIR / "test.json").read_text(encoding="utf-8"))
        test_questions = {c["question"] for c in test["cases"]}
        self.assertFalse({e["question"] for e in examples} & test_questions)

    @unittest.skipUnless(HAS_DB, "AdventureWorks SQLite not downloaded")
    def test_opt_in_registration_adds_database_without_overwriting_others(self):
        with patch.dict(os.environ, {"ENABLE_ADVENTUREWORKS": ""}):
            before = DatabaseRegistry(ROOT).database_paths
        with patch.dict(os.environ, {"ENABLE_ADVENTUREWORKS": "1"}):
            after = DatabaseRegistry(ROOT).database_paths
        self.assertEqual(set(after) - set(before), {"adventureworks"})
        self.assertEqual(after["enterprise_sales"], before["enterprise_sales"])
        self.assertEqual(after["enterprise_hr"], before["enterprise_hr"])
        self.assertEqual(after["adventureworks"].resolve(), DB.resolve())
        self.assertEqual(hashlib.sha256(DB.read_bytes()).hexdigest(), SOURCE_SHA256)


@unittest.skipUnless(HAS_DB, "AdventureWorks SQLite not downloaded")
class DatabaseIntegrationTest(unittest.TestCase):
    """Real metadata, read-only sandbox and knowledge-build integration."""

    @classmethod
    def setUpClass(cls):
        cls.connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
        cls.tables = [r[0] for r in cls.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def test_real_metadata_statistics(self):
        self.assertEqual(len(self.tables), 71)
        self.assertTrue(all("." in name for name in self.tables))
        columns = 0
        composite_pk = 0
        fk_groups = 0
        composite_fk = []
        for table in self.tables:
            escaped = table.replace('"', '""')
            info = list(self.connection.execute(f'PRAGMA table_info("{escaped}")'))
            columns += len(info)
            if sum(1 for row in info if row[5]) > 1:
                composite_pk += 1
            groups = {}
            for row in self.connection.execute(f'PRAGMA foreign_key_list("{escaped}")'):
                groups.setdefault((row[0], row[2]), []).append(row)
            fk_groups += len(groups)
            for rows in groups.values():
                if len(rows) > 1:
                    composite_fk.append((table, [r[3] for r in rows], rows[0][2]))
        self.assertEqual(columns, 486)
        self.assertEqual(composite_pk, 23)
        self.assertEqual(fk_groups, 90)
        self.assertEqual(len(composite_fk), 1)
        self.assertEqual(composite_fk[0][0], "Sales.SalesOrderDetail")
        self.assertEqual(composite_fk[0][1], ["SpecialOfferID", "ProductID"])
        self.assertEqual(composite_fk[0][2], "Sales.SpecialOfferProduct")

    def test_all_declared_foreign_keys_point_to_real_tables_and_columns(self):
        available = {t: {r[1] for r in self.connection.execute(
            f'PRAGMA table_info("{t.replace(chr(34), chr(34) * 2)}")')}
            for t in self.tables}
        for table in self.tables:
            escaped = table.replace('"', '""')
            for row in self.connection.execute(f'PRAGMA foreign_key_list("{escaped}")'):
                target = row[2]
                self.assertIn(target, available, f"{table} -> {target}")
                if row[4]:
                    self.assertIn(row[4], available[target])
                if row[3]:
                    self.assertIn(row[3], available[table])

    def test_row_count_matches_provider_contract(self):
        total = sum(self.connection.execute(
            f'SELECT COUNT(*) FROM "{t.replace(chr(34), chr(34) * 2)}"').fetchone()[0]
            for t in self.tables)
        self.assertEqual(total, 759240)

    def test_quoted_table_name_required(self):
        columns, rows = _read_sql(
            'SELECT COUNT(*) FROM "Sales.SalesOrderHeader"')
        self.assertEqual(rows, [[31465]])
        with self.assertRaises(sqlite3.DatabaseError):
            _read_sql("SELECT COUNT(*) FROM Sales.SalesOrderHeader")

    def test_decimal_columns_are_stored_as_text(self):
        columns, rows = _read_sql(
            'SELECT typeof("SubTotal"), "SubTotal" FROM '
            '"Sales.SalesOrderHeader" LIMIT 1')
        self.assertEqual(rows[0][0], "text")

    def test_readonly_sandbox_rejects_writes_and_attach(self):
        for sql in ('DELETE FROM "Sales.SalesOrderHeader"',
                    "ATTACH DATABASE ':memory:' AS extra"):
            with self.assertRaises(sqlite3.DatabaseError):
                _read_sql(sql)

    def test_end_to_end_queries_return_real_values(self):
        expectations = [
            ('SELECT COUNT(DISTINCT "SalesOrderID") AS orders, '
             'ROUND(SUM(CAST("SubTotal" AS REAL)),2) AS sales '
             'FROM "Sales.SalesOrderHeader"', [31465, 109846381.4]),
            ('SELECT COUNT(*) FROM "Production.Product" '
             'WHERE "FinishedGoodsFlag"=1 AND "SellEndDate" IS NULL', [197]),
            ('SELECT COUNT(*) FROM "HumanResources.Employee" '
             'WHERE "CurrentFlag"=1', [290]),
            ('SELECT SUM("Quantity") FROM "Production.ProductInventory"', [335974]),
            ('SELECT COUNT(*) FROM (SELECT "ProductID", SUM("Quantity") AS qty '
             'FROM "Production.ProductInventory" GROUP BY "ProductID") inv '
             'JOIN "Production.Product" p ON inv."ProductID"=p."ProductID" '
             'WHERE inv.qty < p."ReorderPoint"', [7]),
            ('SELECT COUNT(DISTINCT "PurchaseOrderID"), '
             'ROUND(SUM(CAST("TotalDue" AS REAL)),2) '
             'FROM "Purchasing.PurchaseOrderHeader"', [4012, 70479332.64]),
        ]
        for sql, expected in expectations:
            _, rows = _read_sql(sql)
            self.assertEqual(rows[0], expected, sql)

    def test_duplicate_aggregation_risk_is_reproducible(self):
        _, correct = _read_sql(
            'SELECT ROUND(SUM(CAST("SubTotal" AS REAL)),2) '
            'FROM "Sales.SalesOrderHeader"')
        _, inflated = _read_sql(
            'SELECT ROUND(SUM(CAST(h."SubTotal" AS REAL)),2) '
            'FROM "Sales.SalesOrderHeader" h JOIN "Sales.SalesOrderDetail" d '
            'ON h."SalesOrderID"=d."SalesOrderID"')
        self.assertEqual(correct[0][0], 109846381.4)
        self.assertEqual(inflated[0][0], 2596470456.68)
        self.assertGreater(inflated[0][0] / correct[0][0], 20)

    def test_composite_foreign_key_single_column_join_expands_rows(self):
        _, both = _read_sql(
            'SELECT COUNT(*) FROM "Sales.SalesOrderDetail" d '
            'JOIN "Sales.SpecialOfferProduct" s '
            'ON d."SpecialOfferID"=s."SpecialOfferID" '
            'AND d."ProductID"=s."ProductID"')
        _, one = _read_sql(
            'SELECT COUNT(*) FROM "Sales.SalesOrderDetail" d '
            'JOIN "Sales.SpecialOfferProduct" s ON d."ProductID"=s."ProductID"')
        self.assertEqual(both[0][0], 121317)
        self.assertEqual(one[0][0], 296983)

    def test_phase1_schema_extractor_handles_dotted_names(self):
        schema = extract_database_schema("adventureworks", DB)
        self.assertEqual(len(schema.tables), 71)
        self.assertEqual(len(schema.relations), 90)
        self.assertEqual(sum(len(t.columns) for t in schema.tables), 486)
        self.assertIsNotNone(schema.table("Sales.SalesOrderHeader"))
        self.assertIsNotNone(schema.column("Sales.SalesOrderHeader", "SubTotal"))
        again = extract_database_schema("adventureworks", DB)
        self.assertEqual(schema.schema_fingerprint, again.schema_fingerprint)
        self.assertEqual(schema.table_ids, again.table_ids)

    def test_metric_registry_validates_against_real_schema_with_zero_errors(self):
        registry = MetricRegistry.from_file(METRICS)
        self.assertEqual(len(registry.metrics), 38)
        self.assertEqual(len(registry.trusted_metrics()), 0)
        issues = registry.validate({"adventureworks": extract_database_schema("adventureworks", DB)})
        self.assertEqual([i for i in issues if i.severity == "error"], [])
        self.assertEqual(len([i for i in issues if i.code == "unreviewed_metric"]), 38)

    def test_build_knowledge_payload_is_reproducible(self):
        def args(output):
            return argparse.Namespace(
                database=["adventureworks"], output=str(output),
                metrics=str(METRICS), project_root=str(ROOT),
                validate_only=True, strict=False, built_at=None, quiet=True)

        with tempfile.TemporaryDirectory() as folder:
            with redirect_stdout(io.StringIO()):
                first = build_knowledge._build_payload(ROOT, args(Path(folder) / "a"))
                second = build_knowledge._build_payload(ROOT, args(Path(folder) / "b"))
        self.assertEqual(first["summary"]["metric_registry_count"], 38)
        self.assertEqual(first["summary"]["table_count"], 71)
        self.assertEqual(first["summary"]["column_count"], 486)
        self.assertEqual(first["summary"]["schema_relation_count"], 90)
        self.assertEqual(first["error_count"], 0)
        self.assertEqual(first["artifacts"], second["artifacts"])
        self.assertEqual(first["summary"]["content_fingerprint"],
                         second["summary"]["content_fingerprint"])

    def test_every_eval_reference_sql_still_matches_its_stored_result(self):
        for name in ("dev.json", "test.json"):
            payload = json.loads((EVAL_DIR / name).read_text(encoding="utf-8"))
            for case in payload["cases"]:
                columns, rows = _read_sql(case["reference_sql"])
                self.assertEqual(columns, case["reference_result"]["columns"], case["id"])
                self.assertEqual(rows, case["reference_result"]["rows"], case["id"])
                counter = case.get("counter_example")
                if counter:
                    columns, rows = _read_sql(counter["reference_sql"])
                    self.assertEqual(rows, counter["reference_result"]["rows"], case["id"])

    def test_deepeye_schema_service_serializes_all_71_tables(self):
        """DeepEye-SQL's own schema loader sees every dotted table name."""
        service = SchemaService()
        schema = service.load_sqlite_schema(str(DB))
        self.assertEqual(schema["db_id"], "adventureworks")
        self.assertEqual(len(schema["tables"]), 71)
        self.assertIn("Sales.SalesOrderHeader", schema["tables"])
        profile = service.build_schema_profile(
            schema, include_value_statistics=False, include_value_examples=False)
        self.assertIn("Sales.SalesOrderHeader", profile)
        self.assertIn("Production.ProductCostHistory", profile)

    def test_agent_schema_tool_reads_dotted_table_names(self):
        registry = ToolRegistry(sql_skill=None, databases={"adventureworks": DB})
        info = registry.inspect_database_schema(
            "adventureworks", ["Sales.SalesOrderHeader"])
        self.assertEqual(len(info["available_tables"]), 71)
        self.assertEqual(info["tables"][0]["name"], "Sales.SalesOrderHeader")
        self.assertEqual(len(info["tables"][0]["columns"]), 26)

    def test_langgraph_workflow_compiles_with_sample_database(self):
        """Assembly only: no model call, no LLM endpoint required."""
        registry = ToolRegistry(sql_skill=None, databases={"adventureworks": DB})
        tools = registry.as_langchain_tools()
        # 8 个：选库 / 指标 / Schema / 探查 / 连接路径 / SQL / 画图 / 能力。
        # 新增 inspect_join_path：图从「提示文本」升级为可主动查询的能力。
        self.assertEqual(len(tools), 8)
        chat_model = MagicMock()
        chat_model.bind_tools.return_value = MagicMock()
        graph = build_agent_graph(chat_model, tools)
        self.assertEqual(type(graph).__name__, "CompiledStateGraph")


if __name__ == "__main__":
    unittest.main()
