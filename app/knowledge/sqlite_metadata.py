"""Single SQLite PRAGMA reader shared by the Agent tool and the knowledge layer.

``ToolRegistry.inspect_database_schema()`` needs a compact, LLM-facing view;
``app.knowledge.schema_extractor`` needs the full structural view (composite
keys, UNIQUE constraints, cardinality). Both now call :func:`read_table` so the
PRAGMA parsing exists in exactly one place.

No raw table data is ever read here — only ``sqlite_master``/PRAGMA metadata.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any


@dataclass(frozen=True)
class RawColumn:
    name: str
    data_type: str | None  # PRAGMA may legitimately return None
    not_null: bool
    primary_key_position: int  # 0 when the column is not part of the PK


@dataclass(frozen=True)
class RawForeignKey:
    constraint_id: int
    seq: int
    target_table: str
    source_column: str
    target_column: str | None  # SQLite allows omitting the target column list
    on_update: str
    on_delete: str
    match: str


@dataclass(frozen=True)
class RawUniqueConstraint:
    name: str
    columns: tuple[str, ...]  # ordered by seqno; composite keys keep their order
    origin: str              # 'u' UNIQUE constraint, 'pk' PK, 'c' CREATE INDEX


@dataclass(frozen=True)
class RawTable:
    name: str
    columns: tuple[RawColumn, ...]
    foreign_keys: tuple[RawForeignKey, ...]
    unique_constraints: tuple[RawUniqueConstraint, ...]


def _quote(identifier: str) -> str:
    return identifier.replace('"', '""')


def list_tables(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]


def read_table(connection: sqlite3.Connection, table_name: str) -> RawTable:
    """Read all declared metadata for one table. Never reads column values."""
    escaped = _quote(table_name)
    columns = tuple(
        RawColumn(name=row[1], data_type=row[2], not_null=bool(row[3]),
                  primary_key_position=int(row[5] or 0))
        for row in connection.execute(f'PRAGMA table_info("{escaped}")')
    )
    foreign_keys = tuple(
        RawForeignKey(constraint_id=int(row[0]), seq=int(row[1]),
                      target_table=row[2], source_column=row[3],
                      target_column=row[4], on_update=row[5],
                      on_delete=row[6], match=row[7])
        for row in connection.execute(f'PRAGMA foreign_key_list("{escaped}")')
    )
    constraints = []
    for row in connection.execute(f'PRAGMA index_list("{escaped}")'):
        index_name, is_unique, origin = row[1], bool(row[2]), row[3]
        if not is_unique:
            continue
        index_columns = tuple(
            item[2] for item in sorted(
                connection.execute(f'PRAGMA index_info("{_quote(index_name)}")'),
                key=lambda entry: entry[0],
            ) if item[2] is not None
        )
        constraints.append(RawUniqueConstraint(
            name=index_name, columns=index_columns, origin=origin,
        ))
    return RawTable(name=table_name, columns=columns, foreign_keys=foreign_keys,
                    unique_constraints=tuple(constraints))


def compact_view(table: RawTable) -> dict[str, Any]:
    """Projection used by ``ToolRegistry.inspect_database_schema``.

    Output shape is intentionally identical to the pre-Phase-1 implementation
    so every existing caller and test keeps working unchanged.
    """
    return {
        "name": table.name,
        "columns": [
            {"name": column.name, "type": column.data_type,
             "nullable": not column.not_null,
             "primary_key": column.primary_key_position > 0}
            for column in table.columns
        ],
        "foreign_keys": [
            {"from": key.source_column, "to_table": key.target_table,
             "to_column": key.target_column}
            for key in table.foreign_keys
        ],
    }
