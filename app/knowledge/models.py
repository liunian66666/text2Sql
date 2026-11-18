"""Pydantic models for the enterprise knowledge layer.

Hard rules encoded here:

* **Stable IDs.** Every ID is a deterministic function of its natural key and
  always contains ``database_id``. Nothing is generated from UUIDs, randomness,
  wall-clock time or filesystem paths, so repeated builds are byte-identical.
* **No invented business knowledge.** A field whose business meaning cannot be
  derived from ``business_metrics.json`` plus real SQLite metadata is stored as
  the explicit sentinel ``UNKNOWN`` and listed in ``unknown_fields``.
* **Trust is explicit.** ``MetricDefinition.trusted`` is only True for a
  human-reviewed metric with no unknown fields. Phase 1 marks every metric
  ``reviewed=False``; ``MetricRegistry.trusted_metrics()`` therefore returns an
  empty list and no metric may be used as a hard SQL constraint yet.
* **Fingerprints exclude timestamps.** ``KnowledgeSnapshot.content_fingerprint()``
  hashes only knowledge content, never ``built_at``.
"""
from __future__ import annotations

from enum import Enum
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field


UNKNOWN = "UNKNOWN"
SCHEMA_VERSION = "1"
METRIC_VERSION = "1"
KNOWLEDGE_VERSION = "1"


# --------------------------------------------------------------------------- #
# Deterministic helpers
# --------------------------------------------------------------------------- #
def stable_table_id(database_id: str, table_name: str) -> str:
    """``enterprise_sales.orders`` — never collides with the HR database."""
    return f"{database_id}.{table_name}"


def stable_column_id(database_id: str, table_name: str, column_name: str) -> str:
    """``enterprise_sales.orders.order_id`` — database-scoped, collision free."""
    return f"{database_id}.{table_name}.{column_name}"


def stable_relation_id(database_id: str, source_table: str, source_columns: list[str],
                       target_table: str, target_columns: list[str], origin: str) -> str:
    """Deterministic FK id; column order is preserved for composite keys."""
    left = ",".join(source_columns)
    right = ",".join(target_columns)
    return f"{database_id}:{source_table}({left})->{target_table}({right}):{origin}"


def stable_edge_id(edge_type: str, source_id: str, target_id: str) -> str:
    return f"{edge_type}|{source_id}|{target_id}"


def canonical_json(payload: Any) -> str:
    """Order-independent JSON text used for all content fingerprints."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def content_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Cardinality(str, Enum):
    """Cardinality in the *declared foreign key direction* (source -> target)."""

    MANY_TO_ONE = "MANY_TO_ONE"
    ONE_TO_ONE = "ONE_TO_ONE"
    UNKNOWN = "UNKNOWN"


class RelationOrigin(str, Enum):
    DECLARED = "declared"   # PRAGMA foreign_key_list — verified fact
    MANUAL = "manual"       # human-supplied business relation — not verified


class EdgeType(str, Enum):
    """Relations reserved for the Phase 3 GraphRAG subgraph retriever."""

    METRIC_DEPENDS_ON_COLUMN = "METRIC_DEPENDS_ON_COLUMN"
    METRIC_USES_TABLE = "METRIC_USES_TABLE"
    TABLE_HAS_COLUMN = "TABLE_HAS_COLUMN"
    COLUMN_FOREIGN_KEY_TO_COLUMN = "COLUMN_FOREIGN_KEY_TO_COLUMN"
    METRIC_ALLOWS_DIMENSION = "METRIC_ALLOWS_DIMENSION"
    METRIC_DEPENDS_ON_METRIC = "METRIC_DEPENDS_ON_METRIC"


class EvidenceStatus(str, Enum):
    COMPLETE = "complete"   # source file already has formula/tables/fields/time_grain
    PARTIAL = "partial"     # some structured fields had to be derived or are UNKNOWN


# --------------------------------------------------------------------------- #
# Schema knowledge
# --------------------------------------------------------------------------- #
class SchemaColumn(BaseModel):
    column_id: str
    table_id: str
    database_id: str
    column_name: str
    data_type: str
    nullable: bool
    description: str = UNKNOWN
    primary_key: bool = False
    primary_key_position: int | None = None
    version: str = SCHEMA_VERSION


class UniqueConstraint(BaseModel):
    name: str
    columns: list[str] = Field(default_factory=list)  # ordered, composite safe
    origin: Literal["u", "pk", "c"] = "u"             # UNIQUE / PK / CREATE INDEX


class SchemaTable(BaseModel):
    table_id: str
    database_id: str
    table_name: str
    description: str = UNKNOWN
    primary_keys: list[str] = Field(default_factory=list)  # ordered by pk position
    unique_constraints: list[UniqueConstraint] = Field(default_factory=list)
    columns: list[SchemaColumn] = Field(default_factory=list)
    version: str = SCHEMA_VERSION


class SchemaRelation(BaseModel):
    relation_id: str
    database_id: str
    source_table: str                       # table_id
    source_columns: list[str] = Field(default_factory=list)       # column names, ordered
    source_column_ids: list[str] = Field(default_factory=list)
    target_table: str                       # table_id
    target_columns: list[str] = Field(default_factory=list)       # column names, ordered
    target_column_ids: list[str] = Field(default_factory=list)
    cardinality: Cardinality = Cardinality.UNKNOWN
    source_ref: str
    verified: bool = False
    origin: RelationOrigin = RelationOrigin.DECLARED
    duplicate_aggregation_risk: bool = False
    version: str = SCHEMA_VERSION


class DatabaseSchema(BaseModel):
    """Everything extracted from one SQLite database, plus its stable fingerprint."""

    database_id: str
    version: str = SCHEMA_VERSION
    tables: list[SchemaTable] = Field(default_factory=list)
    relations: list[SchemaRelation] = Field(default_factory=list)
    schema_fingerprint: str = ""
    source_ref: str = "sqlite:PRAGMA"

    def compute_fingerprint(self) -> str:
        """Hash schema structure only; no row counts, paths or build times."""
        payload = {
            "database_id": self.database_id,
            "version": self.version,
            "tables": [_dump(table) for table in self.tables],
            "relations": [_dump(relation) for relation in self.relations],
        }
        return content_hash(payload)

    def with_fingerprint(self) -> DatabaseSchema:
        return self.model_copy(update={"schema_fingerprint": self.compute_fingerprint()})

    def table(self, table_name: str) -> SchemaTable | None:
        return next((t for t in self.tables if t.table_name == table_name), None)

    def column(self, table_name: str, column_name: str) -> SchemaColumn | None:
        table = self.table(table_name)
        if table is None:
            return None
        return next((c for c in table.columns if c.column_name == column_name), None)

    @property
    def column_ids(self) -> list[str]:
        return [column.column_id for table in self.tables for column in table.columns]

    @property
    def table_ids(self) -> list[str]:
        return [table.table_id for table in self.tables]


# --------------------------------------------------------------------------- #
# Metric knowledge
# --------------------------------------------------------------------------- #
class AggregationRisk(BaseModel):
    """Structured risk metadata — recorded in Phase 1, enforced from Phase 4."""

    risk_id: str
    metric_id: str
    kind: Literal["duplicate_aggregation", "mixed_grain", "division_by_zero"]
    description: str
    affected_tables: list[str] = Field(default_factory=list)
    required_mitigation: str
    source_ref: str
    reviewed: bool = False


class MetricDefinition(BaseModel):
    """Structured successor of one entry in ``business_metrics.json``.

    ``raw_*`` fields keep the original text verbatim so the existing
    ``EnterpriseKnowledgeBase`` keyword/vector retrieval is unaffected.
    """

    metric_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    domain: str
    database_id: str
    description: str

    expression: str = UNKNOWN
    expression_kind: Literal["verbatim", "derived", "unknown"] = "unknown"
    aggregation: str = UNKNOWN
    grain: str = UNKNOWN
    source_tables: list[str] = Field(default_factory=list)
    source_columns: list[str] = Field(default_factory=list)   # "table.column"
    time_field: str = UNKNOWN
    allowed_dimensions: list[str] = Field(default_factory=list)
    required_filters: list[str] = Field(default_factory=list)
    join_constraints: list[str] = Field(default_factory=list)
    unit: str = UNKNOWN
    depends_on_metrics: list[str] = Field(default_factory=list)
    aggregation_risks: list[AggregationRisk] = Field(default_factory=list)

    version: str = METRIC_VERSION
    source_ref: str
    reviewed: bool = False
    evidence_status: EvidenceStatus = EvidenceStatus.PARTIAL
    unknown_fields: list[str] = Field(default_factory=list)
    provenance: dict[str, str] = Field(default_factory=dict)

    # Verbatim source text (never rewritten, never used to change retrieval).
    keywords: list[str] = Field(default_factory=list)
    raw_definition: str = ""
    raw_sql_rule: str = ""
    raw_formula: str | None = None
    raw_tables: list[str] = Field(default_factory=list)
    raw_fields: list[str] = Field(default_factory=list)
    raw_time_grain: str | None = None

    @property
    def trusted(self) -> bool:
        """A metric is a hard SQL constraint only after human review."""
        return self.reviewed and not self.unknown_fields

    @property
    def table_ids(self) -> list[str]:
        return [stable_table_id(self.database_id, name) for name in self.source_tables]

    @property
    def column_ids(self) -> list[str]:
        result = []
        for reference in self.source_columns:
            if "." not in reference:
                continue
            # rsplit: table names may contain a SQL Server schema prefix
            # (e.g. "Sales.SalesOrderHeader.SubTotal").
            table, column = reference.rsplit(".", 1)
            result.append(stable_column_id(self.database_id, table, column))
        return result


# --------------------------------------------------------------------------- #
# Graph-ready relations, validation and snapshot
# --------------------------------------------------------------------------- #
class KnowledgeEdge(BaseModel):
    edge_id: str
    edge_type: EdgeType
    source_id: str
    target_id: str
    database_id: str
    metric_id: str | None = None
    verified: bool = False
    source_ref: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    version: str = KNOWLEDGE_VERSION


class ValidationIssue(BaseModel):
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    metric_id: str | None = None
    database_id: str | None = None
    reference: str | None = None


class KnowledgeSnapshot(BaseModel):
    """Whole knowledge build. ``built_at`` is intentionally excluded from the hash."""

    knowledge_version: str = KNOWLEDGE_VERSION
    database_ids: list[str] = Field(default_factory=list)
    schema_fingerprints: dict[str, str] = Field(default_factory=dict)
    metric_versions: dict[str, str] = Field(default_factory=dict)
    built_at: str | None = None
    metric_count: int = 0
    table_count: int = 0
    column_count: int = 0
    relation_count: int = 0
    edge_count: int = 0
    trusted_metric_count: int = 0
    pending_review_metric_count: int = 0
    nodes: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    relations: list[KnowledgeEdge] = Field(default_factory=list)
    validation: list[ValidationIssue] = Field(default_factory=list)

    def content_payload(self) -> dict[str, Any]:
        """The exact payload covered by ``content_fingerprint``."""
        data = _dump(self)
        data.pop("built_at", None)
        return data

    def content_fingerprint(self) -> str:
        return content_hash(self.content_payload())

    def to_json(self) -> str:
        return json.dumps(_dump(self), ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> KnowledgeSnapshot:
        return cls.model_validate(json.loads(text))
