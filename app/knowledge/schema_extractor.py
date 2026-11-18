"""Deterministic SQLite schema knowledge extraction (Phase 1).

Sources of truth, in order:

1. ``sqlite_master`` / PRAGMA metadata (tables, columns, PK, FK, UNIQUE).
2. Explicit, human-supplied business relations passed via ``manual_relations``.

Nothing is inferred from column *names*. When a relation's cardinality cannot
be proven from declared keys it stays ``UNKNOWN``. Only declared FKs are
``verified=True``. No table row / personal data is ever read or serialized.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any

from .models import (
    UNKNOWN,
    Cardinality,
    DatabaseSchema,
    RelationOrigin,
    SchemaColumn,
    SchemaRelation,
    SchemaTable,
    UniqueConstraint,
    stable_column_id,
    stable_relation_id,
    stable_table_id,
)
from .sqlite_metadata import RawTable, list_tables, read_table


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Default build scope. Kept unchanged so the Phase 1 artifacts, the 15-question
# metric-RAG benchmark and all existing tests stay byte-for-byte stable.
ENTERPRISE_DATABASE_IDS = ("enterprise_sales", "enterprise_hr")
# Optional curated sample database (Microsoft AdventureWorks OLTP, MIT).
# It is only ever built when explicitly requested via --database.
ADVENTUREWORKS_DATABASE_ID = "adventureworks"
KNOWN_DATABASE_IDS = (*ENTERPRISE_DATABASE_IDS, ADVENTUREWORKS_DATABASE_ID)
DEFAULT_RELATION_SOURCE = "sqlite:PRAGMA foreign_key_list"


def resolve_database_path(database_id: str, project_root: Path | None = None) -> Path:
    """Locate a curated enterprise database without importing app config."""
    root = (project_root or PROJECT_ROOT).resolve()
    return root / "data" / database_id / f"{database_id}.sqlite"


def connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)


def _primary_keys(raw: RawTable) -> list[str]:
    ordered = sorted(
        (column for column in raw.columns if column.primary_key_position > 0),
        key=lambda column: column.primary_key_position,
    )
    return [column.name for column in ordered]


def _unique_combinations(raw: RawTable) -> set[tuple[str, ...]]:
    combinations: set[tuple[str, ...]] = set()
    primary = tuple(_primary_keys(raw))
    if primary:
        combinations.add(primary)
    for constraint in raw.unique_constraints:
        if constraint.columns:
            combinations.add(tuple(constraint.columns))
    return combinations


def _is_unique(raw: RawTable | None, columns: list[str]) -> bool:
    if raw is None or not columns:
        return False
    return tuple(columns) in _unique_combinations(raw)


def _group_foreign_keys(raw: RawTable, target_tables: dict[str, RawTable],
                        database_id: str) -> list[SchemaRelation]:
    """Group PRAGMA rows by constraint id, preserving (id, seq) column order."""
    grouped: dict[int, list[Any]] = {}
    for key in raw.foreign_keys:
        grouped.setdefault(key.constraint_id, []).append(key)
    results: list[SchemaRelation] = []
    for constraint_id in sorted(grouped):
        entries = sorted(grouped[constraint_id], key=lambda item: item.seq)
        first = entries[0]
        source_columns = [item.source_column for item in entries]
        # SQLite permits ``REFERENCES parent`` with no target column list; in
        # that case the target is the parent PRIMARY KEY by definition.
        declared_targets = [item.target_column for item in entries]
        if all(item is None for item in declared_targets):
            parent = target_tables.get(first.target_table)
            target_columns = _primary_keys(parent) if parent is not None else []
        elif None in declared_targets:
            target_columns = []
        else:
            target_columns = [str(item) for item in declared_targets]

        source_raw = target_tables.get(raw.name, raw)
        target_raw = target_tables.get(first.target_table)
        cardinality = Cardinality.UNKNOWN
        duplicate_risk = False
        if target_columns and _is_unique(target_raw, target_columns):
            if _is_unique(source_raw, source_columns):
                cardinality = Cardinality.ONE_TO_ONE
            else:
                cardinality = Cardinality.MANY_TO_ONE
                # Joining from the unique parent back to the non-unique child
                # multiplies parent-side measures (e.g. refunds per order).
                duplicate_risk = True

        relation = SchemaRelation(
            relation_id=stable_relation_id(
                database_id, raw.name, source_columns,
                first.target_table, target_columns, RelationOrigin.DECLARED.value,
            ),
            database_id=database_id,
            source_table=stable_table_id(database_id, raw.name),
            source_columns=source_columns,
            source_column_ids=[
                stable_column_id(database_id, raw.name, column) for column in source_columns
            ],
            target_table=stable_table_id(database_id, first.target_table),
            target_columns=target_columns,
            target_column_ids=[
                stable_column_id(database_id, first.target_table, column)
                for column in target_columns
            ],
            cardinality=cardinality,
            source_ref=f"{DEFAULT_RELATION_SOURCE}:{raw.name}#{constraint_id}",
            verified=True,
            origin=RelationOrigin.DECLARED,
            duplicate_aggregation_risk=duplicate_risk,
        )
        results.append(relation)
    return results


def _manual_relations(database_id: str, manual: list[dict[str, Any]]) -> list[SchemaRelation]:
    """Human-supplied business relations are kept separate and never verified."""
    results = []
    for index, item in enumerate(manual):
        source_columns = list(item.get("source_columns") or [])
        target_columns = list(item.get("target_columns") or [])
        source_table = str(item["source_table"])
        target_table = str(item["target_table"])
        results.append(SchemaRelation(
            relation_id=stable_relation_id(
                database_id, source_table, source_columns, target_table,
                target_columns, f"{RelationOrigin.MANUAL.value}#{index}",
            ),
            database_id=database_id,
            source_table=stable_table_id(database_id, source_table),
            source_columns=source_columns,
            source_column_ids=[
                stable_column_id(database_id, source_table, column) for column in source_columns
            ],
            target_table=stable_table_id(database_id, target_table),
            target_columns=target_columns,
            target_column_ids=[
                stable_column_id(database_id, target_table, column) for column in target_columns
            ],
            cardinality=Cardinality(item.get("cardinality", Cardinality.UNKNOWN.value)),
            source_ref=str(item.get("source_ref", "manual:unspecified")),
            verified=False,
            origin=RelationOrigin.MANUAL,
            duplicate_aggregation_risk=bool(item.get("duplicate_aggregation_risk", False)),
        ))
    return results


def extract_schema(connection: sqlite3.Connection, database_id: str, *,
                   manual_relations: list[dict[str, Any]] | None = None) -> DatabaseSchema:
    """Extract one database. ``connection`` may be readonly or in-memory."""
    table_names = list_tables(connection)
    raw_tables: dict[str, RawTable] = {
        name: read_table(connection, name) for name in table_names
    }
    tables: list[SchemaTable] = []
    for name in table_names:
        raw = raw_tables[name]
        table_id = stable_table_id(database_id, name)
        primary_keys = _primary_keys(raw)
        columns = [
            SchemaColumn(
                column_id=stable_column_id(database_id, name, column.name),
                table_id=table_id,
                database_id=database_id,
                column_name=column.name,
                data_type=column.data_type or UNKNOWN,
                nullable=not column.not_null,
                description=UNKNOWN,  # no business description in the DB itself
                primary_key=column.primary_key_position > 0,
                primary_key_position=column.primary_key_position or None,
            )
            for column in raw.columns
        ]
        tables.append(SchemaTable(
            table_id=table_id,
            database_id=database_id,
            table_name=name,
            description=UNKNOWN,
            primary_keys=primary_keys,
            unique_constraints=[
                UniqueConstraint(name=item.name, columns=list(item.columns), origin=item.origin)
                for item in raw.unique_constraints
            ],
            columns=columns,
        ))

    relations: list[SchemaRelation] = []
    for name in table_names:
        relations.extend(_group_foreign_keys(raw_tables[name], raw_tables, database_id))
    relations.extend(_manual_relations(database_id, manual_relations or []))
    relations.sort(key=lambda relation: relation.relation_id)

    schema = DatabaseSchema(
        database_id=database_id,
        tables=tables,
        relations=relations,
        source_ref="sqlite:PRAGMA",
    )
    return schema.with_fingerprint()


def extract_database_schema(database_id: str, path: Path, *,
                            manual_relations: list[dict[str, Any]] | None = None
                            ) -> DatabaseSchema:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Database file not found: {path}")
    with closing(connect_readonly(path)) as connection:
        return extract_schema(connection, database_id, manual_relations=manual_relations)


def extract_all(project_root: Path | None = None,
                database_ids: list[str] | None = None) -> dict[str, DatabaseSchema]:
    root = (project_root or PROJECT_ROOT).resolve()
    selected = list(database_ids or ENTERPRISE_DATABASE_IDS)
    return {
        database_id: extract_database_schema(database_id, resolve_database_path(database_id, root))
        for database_id in selected
    }


def schema_to_json(schema: DatabaseSchema) -> str:
    return json.dumps(schema.model_dump(mode="json"), ensure_ascii=False,
                      indent=2, sort_keys=True)


def schema_from_json(text: str) -> DatabaseSchema:
    return DatabaseSchema.model_validate(json.loads(text))
