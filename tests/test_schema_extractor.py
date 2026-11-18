"""Phase 1: real SQLite metadata extraction, composite keys and tool compatibility."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import unittest

from app.knowledge.models import Cardinality, RelationOrigin
from app.knowledge.schema_extractor import (
    connect_readonly,
    extract_all,
    extract_database_schema,
    extract_schema,
    resolve_database_path,
    schema_to_json,
)
from app.knowledge.sqlite_metadata import compact_view, read_table


SALES_TABLES = {
    "categories", "customers", "order_items", "orders",
    "payments", "products", "refunds",
}
HR_TABLES = {"attendance", "departments", "employees", "performance_reviews"}


class RealDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schemas = extract_all()

    def test_all_real_business_tables_are_extracted(self):
        self.assertEqual(set(self.schemas), {"enterprise_sales", "enterprise_hr"})
        self.assertEqual({t.table_name for t in self.schemas["enterprise_sales"].tables},
                         SALES_TABLES)
        self.assertEqual({t.table_name for t in self.schemas["enterprise_hr"].tables},
                         HR_TABLES)

    def test_repeated_extraction_has_identical_ids_and_fingerprint(self):
        first = extract_all()
        second = extract_all()
        for database_id in first:
            self.assertEqual(first[database_id].schema_fingerprint,
                             second[database_id].schema_fingerprint)
            self.assertEqual(first[database_id].table_ids, second[database_id].table_ids)
            self.assertEqual(first[database_id].column_ids, second[database_id].column_ids)
            self.assertEqual(first[database_id].relations, second[database_id].relations)

    def test_declared_foreign_keys_are_verified_with_real_cardinality(self):
        sales = self.schemas["enterprise_sales"]
        hr = self.schemas["enterprise_hr"]
        self.assertEqual(len(sales.relations), 6)
        self.assertEqual(len(hr.relations), 3)
        self.assertTrue(all(relation.verified for relation in
                            [*sales.relations, *hr.relations]))
        self.assertTrue(all(relation.origin == RelationOrigin.DECLARED for relation in
                            [*sales.relations, *hr.relations]))

        payments = next(r for r in sales.relations
                        if r.source_table.endswith(".payments"))
        self.assertEqual(payments.cardinality, Cardinality.ONE_TO_ONE)
        self.assertFalse(payments.duplicate_aggregation_risk)

        items = next(r for r in sales.relations
                     if r.source_table.endswith(".order_items")
                     and r.target_table.endswith(".orders"))
        self.assertEqual(items.cardinality, Cardinality.MANY_TO_ONE)
        self.assertTrue(items.duplicate_aggregation_risk)

    def test_unique_constraints_and_composite_keys_are_extracted(self):
        sales = self.schemas["enterprise_sales"]
        hr = self.schemas["enterprise_hr"]
        payments_unique = {tuple(item.columns)
                           for item in sales.table("payments").unique_constraints}
        self.assertIn(("order_id",), payments_unique)
        attendance_unique = {tuple(item.columns)
                             for item in hr.table("attendance").unique_constraints}
        self.assertIn(("employee_id", "attendance_month"), attendance_unique)
        self.assertEqual(hr.table("departments").primary_keys, ["department_id"])

    def test_missing_business_description_is_marked_unknown(self):
        column = self.schemas["enterprise_sales"].column("orders", "order_id")
        self.assertEqual(column.description, "UNKNOWN")
        self.assertEqual(self.schemas["enterprise_sales"].table("orders").description,
                         "UNKNOWN")

    def test_no_raw_personal_data_is_serialized(self):
        path = resolve_database_path("enterprise_sales")
        with closing(sqlite3.connect(path)) as connection:
            customer_name = connection.execute(
                "SELECT customer_name FROM customers LIMIT 1").fetchone()[0]
        serialized = schema_to_json(self.schemas["enterprise_sales"])
        self.assertNotIn(customer_name, serialized)
        self.assertNotIn('"rows"', serialized)
        self.assertNotIn('"values"', serialized)

    def test_manual_relation_is_kept_separate_and_not_verified(self):
        path = resolve_database_path("enterprise_sales")
        manual = [{
            "source_table": "orders", "source_columns": ["customer_id"],
            "target_table": "customers", "target_columns": ["customer_id"],
            "source_ref": "manual:hand-written-business-rule.md",
            "cardinality": "UNKNOWN",
        }]
        schema = extract_database_schema("enterprise_sales", path,
                                         manual_relations=manual)
        added = [relation for relation in schema.relations
                 if relation.origin == RelationOrigin.MANUAL]
        self.assertEqual(len(added), 1)
        self.assertFalse(added[0].verified)
        self.assertEqual(added[0].cardinality, Cardinality.UNKNOWN)
        self.assertEqual(added[0].source_ref, "manual:hand-written-business-rule.md")
        self.assertTrue(all(relation.verified for relation in schema.relations
                            if relation.origin == RelationOrigin.DECLARED))


def _memory_schema(script: str, database_id: str = "fixture"):
    """Build a fixture in memory — no temp files, no Windows file-lock issues."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(script)
        return extract_schema(connection, database_id)
    finally:
        connection.close()


class CompositeKeyTest(unittest.TestCase):
    def test_composite_primary_key_and_composite_foreign_key_keep_order(self):
        schema = _memory_schema("""
            CREATE TABLE parent(a INTEGER, b INTEGER, label TEXT, PRIMARY KEY(a, b));
            CREATE TABLE child(x INTEGER, y INTEGER, value REAL,
                FOREIGN KEY (x, y) REFERENCES parent(a, b));
        """)
        self.assertEqual(schema.table("parent").primary_keys, ["a", "b"])
        relation = next(item for item in schema.relations
                        if item.source_table.endswith(".child"))
        self.assertEqual(relation.source_columns, ["x", "y"])
        self.assertEqual(relation.target_columns, ["a", "b"])
        self.assertEqual(relation.source_column_ids,
                         ["fixture.child.x", "fixture.child.y"])
        self.assertEqual(relation.target_column_ids,
                         ["fixture.parent.a", "fixture.parent.b"])
        self.assertEqual(relation.cardinality, Cardinality.MANY_TO_ONE)
        self.assertTrue(relation.duplicate_aggregation_risk)

    def test_foreign_key_without_target_columns_uses_declared_primary_key(self):
        schema = _memory_schema("""
            CREATE TABLE parent(a INTEGER, b INTEGER, PRIMARY KEY(a, b));
            CREATE TABLE child(x INTEGER, y INTEGER,
                FOREIGN KEY (x, y) REFERENCES parent);
        """)
        relation = next(item for item in schema.relations
                        if item.source_table.endswith(".child"))
        self.assertEqual(relation.target_columns, ["a", "b"])

    def test_unique_source_gives_one_to_one(self):
        schema = _memory_schema("""
            CREATE TABLE parent(id INTEGER PRIMARY KEY);
            CREATE TABLE child(id INTEGER PRIMARY KEY REFERENCES parent(id));
        """)
        relation = next(iter(schema.relations))
        self.assertEqual(relation.cardinality, Cardinality.ONE_TO_ONE)
        self.assertFalse(relation.duplicate_aggregation_risk)

    def test_unprovable_cardinality_stays_unknown(self):
        schema = _memory_schema("""
            CREATE TABLE loose(a INTEGER);
            CREATE TABLE loose_child(x INTEGER REFERENCES loose(a));
        """)
        relation = next(iter(schema.relations))
        self.assertEqual(relation.cardinality, Cardinality.UNKNOWN)
        self.assertFalse(relation.duplicate_aggregation_risk)

    def test_column_ids_never_collide_across_databases(self):
        path = resolve_database_path("enterprise_sales")
        first = extract_database_schema("enterprise_sales", path)
        second = extract_database_schema("enterprise_hr_copy", path)
        self.assertNotEqual(first.column_ids, second.column_ids)
        self.assertTrue(all(item.startswith("enterprise_hr_copy.") for item in second.column_ids))


class ToolRegistryCompatibilityTest(unittest.TestCase):
    def test_existing_llm_schema_view_shape_is_unchanged(self):
        from app.agent.tools.registry import ToolRegistry

        registry = ToolRegistry(None, {
            "enterprise_sales": resolve_database_path("enterprise_sales"),
        })
        view = registry.inspect_database_schema("enterprise_sales", ["orders"])
        self.assertEqual(set(view), {"database_id", "available_tables", "tables"})
        self.assertIn("orders", view["available_tables"])
        orders = view["tables"][0]
        self.assertEqual(set(orders), {"name", "columns", "foreign_keys"})
        self.assertEqual(set(orders["columns"][0]),
                         {"name", "type", "nullable", "primary_key"})
        self.assertEqual(len(orders["foreign_keys"]), 1)
        self.assertEqual(set(orders["foreign_keys"][0]), {"from", "to_table", "to_column"})

        with closing(connect_readonly(resolve_database_path("enterprise_sales"))) as connection:
            self.assertEqual(orders, compact_view(read_table(connection, "orders")))


if __name__ == "__main__":
    unittest.main()
