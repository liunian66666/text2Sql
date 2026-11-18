"""Selective value-index policy.

Phase 4A replaces the "walk every text column and embed everything" default of
``build-index`` with an explicit, auditable policy:

* a whitelist of ``allowed_tables`` / ``allowed_columns`` (empty = all, but the
  CLI refuses a full-database build unless ``--include-all`` is passed),
* a blacklist of ``excluded_tables`` / ``excluded_columns``,
* hard budget caps (per-column distinct values, total values, embedding calls),
* an independent **sensitive-field registry** that must never be indexed even
  if a column is accidentally whitelisted.

Exclusion is intentionally *not* a single regex over column names: a column
called ``Name`` in ``Production.Product`` is fine, while ``PasswordHash`` or
``NationalIDNumber`` anywhere must be dropped. The registry therefore combines
explicit ``(table, column)`` pairs, known sensitive tables and known sensitive
column names, and it is cross-checked against the real table name.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from fnmatch import fnmatch
from typing import Any


# Bumped whenever the *meaning* of coverage changes (not when vectors change).
POLICY_VERSION = "1.0"


# --- Sensitive-field registry (PII / secrets / low analytic value) -----------
# Explicit (table, column) pairs. Lower-case, table may carry a schema prefix
# such as ``person.emailaddress``.
SENSITIVE_TABLE_COLUMN: frozenset[tuple[str, str]] = frozenset({
    ("person", "emailaddress"),
    ("person", "passwordhash"),
    ("person", "passwordsalt"),
    ("humanresources", "nationalidnumber"),
    ("sales", "cardnumber"),
    ("sales", "creditcard"),
})

# Whole tables whose every column is sensitive and must never be indexed.
SENSITIVE_TABLES: frozenset[str] = frozenset({
    "person.emailaddress",
    "person.password",
    "sales.creditcard",
    "person.creditcard",
})

# Column names that are sensitive regardless of table (exact, lower-case).
SENSITIVE_COLUMN_NAMES: frozenset[str] = frozenset({
    "passwordhash",
    "passwordsalt",
    "nationalidnumber",
    "cardnumber",
    "emailaddress",
    "loginid",
    "additionalcontactinfo",
    "demographics",
    "bankaccount",
    "ssn",
    "taxid",
    "creditcard",
    "phonenumber",
    "rowguid",  # SQL Server replication id: no analytic value, huge cardinality
})


@dataclass
class ValueIndexPolicy:
    """Declarative, serialisable policy for one value-index build."""

    database_id: str
    allowed_tables: list[str] = field(default_factory=list)
    allowed_columns: list[str] = field(default_factory=list)
    excluded_tables: list[str] = field(default_factory=list)
    excluded_columns: list[str] = field(default_factory=list)
    max_distinct_values_per_column: int = 2000
    max_value_length: int = 100
    max_total_values: int = 200_000
    max_embedding_requests: int = 60_000
    embedding_backend: str = "local"
    schema_fingerprint: str | None = None
    policy_version: str = POLICY_VERSION

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def _norm(value: str) -> str:
        return value.lower()

    def is_sensitive(self, table: str, column: str) -> bool:
        """Return True when (table, column) is a protected PII/secret field."""
        table_key = self._norm(table)
        column_key = self._norm(column)
        if (table_key, column_key) in SENSITIVE_TABLE_COLUMN:
            return True
        if column_key in SENSITIVE_COLUMN_NAMES:
            return True
        base = table_key.split(".")[-1]
        if table_key in SENSITIVE_TABLES:
            return True
        if base in {name.split(".")[-1] for name in SENSITIVE_TABLES}:
            return True
        return False

    def _table_allowed(self, table: str) -> bool:
        table_key = self._norm(table)
        if any(fnmatch(table_key, self._norm(item)) for item in self.excluded_tables):
            return False
        if self.allowed_tables:
            return any(fnmatch(table_key, self._norm(item)) for item in self.allowed_tables)
        return True

    def _column_allowed(self, table: str, column: str) -> bool:
        if not self.allowed_columns:
            return True
        table_key = self._norm(table)
        column_key = self._norm(column)
        for item in self.allowed_columns:
            item_key = self._norm(item)
            if "." in item_key:
                if fnmatch(f"{table_key}.{column_key}", item_key):
                    return True
            elif fnmatch(column_key, item_key):
                return True
        return False

    def evaluate(self, table: str, column: str) -> tuple[bool, str]:
        """Return ``(include, reason)``; ``reason`` is empty when included."""
        if self.is_sensitive(table, column):
            return False, "sensitive_field"
        table_key = self._norm(table)
        column_key = self._norm(column)
        for item in self.excluded_columns:
            item_key = self._norm(item)
            if "." in item_key:
                if fnmatch(f"{table_key}.{column_key}", item_key):
                    return False, f"excluded_column:{item}"
            elif fnmatch(column_key, item_key):
                return False, f"excluded_column:{item}"
        if not self._table_allowed(table):
            return False, "table_not_in_whitelist"
        if not self._column_allowed(table, column):
            return False, "column_not_in_whitelist"
        return True, ""

    def estimated_requests(self, total_values: int, batch_size: int) -> int:
        """Number of embedding calls needed for ``total_values`` documents."""
        batch_size = max(1, int(batch_size))
        return (int(total_values) + batch_size - 1) // batch_size

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def split_csv(value: str | None) -> list[str]:
    """Split a comma-separated CLI value, dropping blank entries."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]
