"""Load, validate and query the AdventureWorks schema dictionary.

Trust contract:

* ``description_en`` comes from the pinned provider metadata (upstream text).
* ``description_zh`` / ``aliases`` are ``model_candidate`` + ``unreviewed``;
  they are useful for *recall* but must never be presented as official or
  human-approved business rules.
* True provenance is per-field (``origin``), never inferred from a value.
* Anything unavailable stays ``UNKNOWN``.

The dictionary never writes into ``business_metrics.json`` or any historical
evaluation artifact.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .models import UNKNOWN

DEFAULT_DICTIONARY = (
    Path(__file__).resolve().parents[1]
    / "enterprise" / "adventureworks_dictionary.json"
)


class ColumnDescription(BaseModel):
    column_id: str
    column_name: str
    data_type_real: str = UNKNOWN
    primary_key: bool = False
    description_en: str = UNKNOWN
    description_zh: str = UNKNOWN
    aliases: list[str] = Field(default_factory=list)
    origin: dict[str, str] = Field(default_factory=dict)
    review_status: str = "unreviewed"
    source_ref: str = ""


class TableDescription(BaseModel):
    table_id: str
    table_name: str
    primary_keys_real: list[str] = Field(default_factory=list)
    description_en: str = UNKNOWN
    description_zh: str = UNKNOWN
    aliases: list[str] = Field(default_factory=list)
    origin: dict[str, str] = Field(default_factory=dict)
    review_status: str = "unreviewed"
    source_ref: str = ""
    columns: list[ColumnDescription] = Field(default_factory=list)

    def column(self, column_name: str) -> ColumnDescription | None:
        return next((item for item in self.columns if item.column_name == column_name), None)


class SchemaDictionary(BaseModel):
    database_id: str
    schema_version: str = "1"
    source: dict[str, Any] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    tables: list[TableDescription] = Field(default_factory=list)

    def table(self, table_name: str) -> TableDescription | None:
        return next((item for item in self.tables if item.table_name == table_name), None)

    def describe(self, table_name: str, column_name: str | None = None) -> tuple[str, str, list[str], str]:
        """Return (english, chinese, aliases, provenance_label) for retrieval."""
        table = self.table(table_name)
        if table is None:
            return UNKNOWN, UNKNOWN, [], "unknown"
        if column_name is None:
            return (table.description_en, table.description_zh, list(table.aliases),
                    table.origin.get("description_en", "unknown"))
        column = table.column(column_name)
        if column is None:
            return UNKNOWN, UNKNOWN, [], "unknown"
        return (column.description_en, column.description_zh, list(column.aliases),
                column.origin.get("description_en", "unknown"))

    def coverage(self) -> dict[str, Any]:
        tables = len(self.tables)
        columns = sum(len(table.columns) for table in self.tables)
        upstream_tables = sum(t.description_en != UNKNOWN for t in self.tables)
        upstream_columns = sum(c.description_en != UNKNOWN for t in self.tables for c in t.columns)
        zh_tables = sum(t.description_zh != UNKNOWN for t in self.tables)
        zh_columns = sum(c.description_zh != UNKNOWN for t in self.tables for c in t.columns)
        alias_tables = sum(bool(t.aliases) for t in self.tables)
        alias_columns = sum(bool(c.aliases) for t in self.tables for c in t.columns)

        def rate(numerator: int, denominator: int) -> float:
            return round(numerator / denominator, 6) if denominator else 0.0

        return {
            "tables_total": tables,
            "columns_total": columns,
            "upstream_en_table_count": upstream_tables,
            "upstream_en_table_rate": rate(upstream_tables, tables),
            "upstream_en_column_count": upstream_columns,
            "upstream_en_column_rate": rate(upstream_columns, columns),
            "zh_candidate_table_count": zh_tables,
            "zh_candidate_column_count": zh_columns,
            "alias_table_count": alias_tables,
            "alias_column_count": alias_columns,
            "unknown_en_column_count": columns - upstream_columns,
            "official_reviewed_count": 0,
            "note": "中文与别名均为 model_candidate / unreviewed，不计入官方或已审核覆盖率。",
        }

    def validate(self) -> list[str]:
        problems: list[str] = []
        for table in self.tables:
            for column in table.columns:
                if column.origin.get("description_zh") == "upstream_description":
                    problems.append(f"中文描述被错误标为官方来源: {column.column_id}")
                if column.review_status not in {"unreviewed", "approved", "rejected"}:
                    problems.append(f"未知审核状态: {column.column_id}")
                if table.origin.get("description_zh") == "upstream_description":
                    problems.append(f"中文表描述被错误标为官方来源: {table.table_id}")
        return problems


def load_dictionary(path: Path | str | None = None) -> SchemaDictionary:
    target = Path(path) if path is not None else DEFAULT_DICTIONARY
    if not target.is_file():
        raise FileNotFoundError(f"Schema dictionary not found: {target}")
    return SchemaDictionary.model_validate(json.loads(target.read_text(encoding="utf-8")))


def searchable_text(entry: TableDescription | ColumnDescription) -> str:
    """Concatenate all available descriptions/aliases for indexing."""
    parts = [entry.description_en, entry.description_zh, *entry.aliases]
    return " ".join(part for part in parts if part and part != UNKNOWN)
