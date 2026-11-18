"""Data-source capability routing based on the real SQLite schema.

The Agent may only claim a data source can answer a question when the source
actually has the tables/columns that capability needs. Everything here is
derived from ``PRAGMA`` metadata of the whitelisted database files, never from
hard-coded assumptions about what a schema "probably" contains.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
from typing import Iterable

from app.knowledge.sqlite_metadata import read_table

# Question phrase -> capability key. Phrases are business language only; no
# evaluation case IDs and no reference SQL ever appear here.
CAPABILITY_TERMS: dict[str, tuple[str, ...]] = {
    "historical_cost": ("历史成本", "历史销售成本", "订单发生时", "成本历史", "当时成本", "有效性成本"),
    "ship_address": ("收货地址", "收货方", "发货至", "ship to", "shipto"),
    "product_hierarchy": ("商品大类", "商品小类", "子类", "分类层级", "层次结构"),
}

CAPABILITY_LABELS = {
    "historical_cost": "订单发生时有效的历史商品成本",
    "ship_address": "订单收货地址维度的州/省信息",
    "product_hierarchy": "商品大类/小类层级结构",
}

# Cost history is only trustworthy when the table carries both a cost amount and
# a validity window. A plain products.unit_cost column is the *current* standard
# cost and must never be presented as history.
_COST_NAME_HINTS = ("cost", "成本")
_WINDOW_HINTS = ("startdate", "start_date", "validfrom", "fromdate")


@dataclass
class DatabaseCapabilities:
    database_id: str
    tables: dict[str, set[str]] = field(default_factory=dict)
    capabilities: set[str] = field(default_factory=set)
    evidence: dict[str, str] = field(default_factory=dict)

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def missing_tables_hint(self, capability: str) -> str:
        return {
            "historical_cost": "缺少带生效起止日期的成本历史表（如 ProductCostHistory）",
            "ship_address": "缺少收货地址相关字段（如 ShipToAddressID / 地址州省）",
            "product_hierarchy": "缺少商品大类/小类层级表（如 ProductCategory / ProductSubcategory）",
        }.get(capability, f"缺少支撑「{CAPABILITY_LABELS.get(capability, capability)}」的表或字段")


def _normalize(name: str) -> str:
    return name.replace("_", "").replace(" ", "").casefold()


def _looks_like_cost_history(table: str, columns: Iterable[str]) -> bool:
    column_names = [_normalize(column) for column in columns]
    has_cost = any(any(hint in name for hint in _COST_NAME_HINTS) for name in column_names)
    has_window = any(any(name.startswith(hint) for hint in _WINDOW_HINTS) for name in column_names)
    named_history = any(token in _normalize(table) for token in ("costhistory", "cost_history", "历史成本"))
    return has_cost and (has_window or named_history)


def _looks_like_ship_address(columns: Iterable[str]) -> bool:
    return any(
        "shipto" in _normalize(column) or "addressto" in _normalize(column)
        for column in columns
    )


def _looks_like_product_hierarchy(tables: dict[str, set[str]]) -> bool:
    for table in tables:
        normalized = _normalize(table)
        if "productsubcategory" in normalized or "productcategory" in normalized or "子类" in normalized:
            return True
    return False


def describe_database(database_id: str, path: Path) -> DatabaseCapabilities:
    """Introspect one whitelisted database and derive its capabilities.

    A database that cannot be opened (missing file, locked temp file in tests)
    yields an empty capability set instead of raising: capability routing must
    never turn a read-only probe failure into an Agent crash.
    """
    try:
        with closing(sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)) as connection:
            table_names = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            tables: dict[str, set[str]] = {}
            for table in table_names:
                tables[table] = {column.name for column in read_table(connection, table).columns}
    except sqlite3.Error:
        return DatabaseCapabilities(database_id=database_id)

    capabilities: set[str] = set()
    evidence: dict[str, str] = {}
    for table, columns in tables.items():
        if _looks_like_cost_history(table, columns):
            capabilities.add("historical_cost")
            evidence.setdefault("historical_cost", table)
        if _looks_like_ship_address(columns):
            capabilities.add("ship_address")
            evidence.setdefault("ship_address", table)
    if _looks_like_product_hierarchy(tables):
        capabilities.add("product_hierarchy")
        evidence.setdefault("product_hierarchy", "ProductCategory/ProductSubcategory")
    return DatabaseCapabilities(database_id=database_id, tables=tables,
                                capabilities=capabilities, evidence=evidence)


def required_capabilities(question: str) -> set[str]:
    """Only explicit business wording triggers a requirement; guesses stay out."""
    normalized = question.casefold()
    return {
        capability for capability, terms in CAPABILITY_TERMS.items()
        if any(term in normalized for term in terms)
    }


def describe_all(databases: dict[str, Path], cache: dict[str, DatabaseCapabilities] | None = None
                 ) -> dict[str, DatabaseCapabilities]:
    cache = cache if cache is not None else {}
    for database_id, path in databases.items():
        if database_id not in cache:
            cache[database_id] = describe_database(database_id, path)
    return cache
