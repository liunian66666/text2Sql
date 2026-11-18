"""NetworkX MultiDiGraph over the *real* AdventureWorks knowledge artifacts.

Design rules (delegated requirements):

* A foreign key is stored as **one complete constraint group**, never as 91
  independent column-to-column edges. The 90 declared groups contain 91 column
  pairs because exactly one group is composite (2 columns).
* Composite FKs keep the ordered ``source_columns`` / ``target_columns`` lists.
* Multiple FK roles between the same pair of tables are preserved as distinct
  parallel edges keyed by ``relation_id``.
* Direction, cardinality, origin, verification status and duplicate
  aggregation risk all live on the edge. The reverse traversal edge is
  explicitly flagged ``reverse=True`` so direction is never lost.
* No LLM is allowed to invent trusted foreign keys. Only ``SchemaRelation``
  records produced by ``PRAGMA foreign_key_list`` are loaded here.
"""
from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any

import networkx as nx

from .models import KnowledgeEdge, SchemaRelation

DEFAULT_ARTIFACTS = (
    Path(__file__).resolve().parents[2] / "workspace" / "knowledge" / "adventureworks"
)

# 进程级只读缓存：知识图是静态资产，同一次进程内没有任何理由反复重建。
# key 带上两个产物的 (mtime_ns, size)，所以「重跑 build-knowledge」会自动失效，
# 不需要重启进程；而 Agent 每个请求都能拿到同一个对象。
_STORE_CACHE: dict[tuple[str, str, tuple[tuple[int, int], ...]], "GraphStore"] = {}
_STORE_LOCK = threading.Lock()


def _artifact_signature(*paths: Path) -> tuple[tuple[int, int], ...]:
    signature = []
    for path in paths:
        stat = path.stat()
        signature.append((stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def clear_store_cache() -> None:
    """Explicit invalidation hook (tests, rebuild scripts, hot reload)."""
    with _STORE_LOCK:
        _STORE_CACHE.clear()


class GraphStore:
    def __init__(self, nodes: dict[str, list[dict[str, Any]]],
                 relations: list[SchemaRelation], edges: list[KnowledgeEdge],
                 *, database_id: str = "adventureworks"):
        self.database_id = database_id
        self.relations = sorted(relations, key=lambda item: item.relation_id)
        self.relation_by_id = {item.relation_id: item for item in self.relations}
        self.nodes_by_id: dict[str, dict[str, Any]] = {}
        self.graph = nx.MultiDiGraph()
        self._build(nodes, edges)

    @classmethod
    def from_artifacts(cls, directory: Path | str | None = None,
                       *, database_id: str = "adventureworks",
                       use_cache: bool = True) -> "GraphStore":
        root = Path(directory) if directory is not None else DEFAULT_ARTIFACTS
        snapshot_path = root / "snapshot.json"
        relations_path = root / "relations.json"
        for path in (snapshot_path, relations_path):
            if not path.is_file():
                raise FileNotFoundError(f"Knowledge artifact missing: {path}")
        if use_cache:
            key = (str(root.resolve()), database_id,
                   _artifact_signature(snapshot_path, relations_path))
            with _STORE_LOCK:
                cached = _STORE_CACHE.get(key)
            if cached is not None:
                return cached
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        relations_payload = json.loads(relations_path.read_text(encoding="utf-8"))
        relations = [SchemaRelation.model_validate(item)
                     for item in relations_payload["relations"]]
        edges = [KnowledgeEdge.model_validate(item) for item in snapshot.get("relations", [])]
        store = cls(snapshot.get("nodes", {}), relations, edges, database_id=database_id)
        if use_cache:
            with _STORE_LOCK:
                # 只保留最新签名，避免旧版本的图对象长期驻留内存。
                for stale in [item for item in _STORE_CACHE
                              if item[:2] == (str(root.resolve()), database_id)]:
                    _STORE_CACHE.pop(stale, None)
                _STORE_CACHE[key] = store
        return store

    def _build(self, nodes: dict[str, list[dict[str, Any]]],
               edges: list[KnowledgeEdge]) -> None:
        for kind, items in nodes.items():
            for item in items:
                node_id = item["node_id"]
                attrs = {**item, "kind": kind,
                         "database_id": item.get("database_id", self.database_id)}
                self.nodes_by_id[node_id] = attrs
                self.graph.add_node(node_id, **attrs)

        for edge in edges:
            # KnowledgeEdge rows describe metric/schema membership only. The
            # column-level FK pairs inside them are deliberately NOT converted
            # into independent relation edges; the group is the source of truth.
            if self.graph.has_node(edge.source_id) and self.graph.has_node(edge.target_id):
                self.graph.add_edge(
                    edge.source_id, edge.target_id, key=edge.edge_id,
                    edge_kind=edge.edge_type.value, verified=edge.verified,
                    source_ref=edge.source_ref, metadata=edge.metadata,
                )

        for relation in self.relations:
            attrs = {
                "edge_kind": "FK_RELATION",
                "relation_id": relation.relation_id,
                "source_columns": list(relation.source_columns),
                "target_columns": list(relation.target_columns),
                "source_column_ids": list(relation.source_column_ids),
                "target_column_ids": list(relation.target_column_ids),
                "cardinality": relation.cardinality.value if hasattr(relation.cardinality, "value") else str(relation.cardinality),
                "origin": relation.origin.value if hasattr(relation.origin, "value") else str(relation.origin),
                "verified": relation.verified,
                "duplicate_aggregation_risk": relation.duplicate_aggregation_risk,
                "composite": len(relation.source_columns) > 1,
                "source_ref": relation.source_ref,
            }
            self.graph.add_edge(relation.source_table, relation.target_table,
                                key=relation.relation_id, direction="source_to_target",
                                reverse=False, **attrs)
            # Reverse traversal is allowed for path search but never pretends
            # to be the declared FK direction.
            self.graph.add_edge(relation.target_table, relation.source_table,
                                key=f"{relation.relation_id}#reverse",
                                direction="target_to_source", reverse=True, **attrs)

    @property
    def fk_edges(self) -> list[tuple[str, str, dict[str, Any]]]:
        return [(u, v, data) for u, v, key, data in self.graph.edges(keys=True, data=True)
                if data.get("edge_kind") == "FK_RELATION" and not data.get("reverse")]

    def relations_between(self, left: str, right: str) -> list[SchemaRelation]:
        return [relation for relation in self.relations
                if {relation.source_table, relation.target_table} == {left, right}]

    def composite_relations(self) -> list[SchemaRelation]:
        return [relation for relation in self.relations if len(relation.source_columns) > 1]

    def table_projection(self) -> nx.DiGraph:
        """Table-level simple graph used for multi-hop path enumeration."""
        projection = nx.DiGraph()
        for node_id, attrs in self.nodes_by_id.items():
            if attrs.get("kind") == "table":
                projection.add_node(node_id, **attrs)
        for source, target, data in self.fk_edges:
            if not projection.has_edge(source, target):
                projection.add_edge(source, target, relation_ids=[], roles=[])
            edge = projection[source][target]
            edge["relation_ids"].append(data["relation_id"])
            edge["roles"].append({
                "relation_id": data["relation_id"],
                "source_columns": data["source_columns"],
                "target_columns": data["target_columns"],
                "cardinality": data["cardinality"],
                "verified": data["verified"],
                "composite": data["composite"],
            })
        # Reverse direction is available for undirected-style traversal.
        for source, target, data in self.fk_edges:
            if not projection.has_edge(target, source):
                projection.add_edge(target, source, relation_ids=[], roles=[])
            edge = projection[target][source]
            edge["relation_ids"].append(f"{data['relation_id']}#reverse")
            edge["roles"].append({
                "relation_id": data["relation_id"],
                "source_columns": data["target_columns"],
                "target_columns": data["source_columns"],
                "cardinality": data["cardinality"],
                "verified": data["verified"],
                "composite": data["composite"],
                "reverse": True,
            })
        return projection

    def columns_for_table(self, table_id: str, *, limit: int = 40) -> list[str]:
        return sorted(
            target for _, target, _, data in self.graph.out_edges(table_id, keys=True, data=True)
            if data.get("edge_kind") == "TABLE_HAS_COLUMN")[:limit]

    def validate(self) -> dict[str, Any]:
        groups = len(self.relations)
        column_pairs = sum(len(relation.source_columns) for relation in self.relations)
        composite = self.composite_relations()
        pair_counts: dict[tuple[str, str], int] = {}
        for relation in self.relations:
            key = (relation.source_table, relation.target_table)
            pair_counts[key] = pair_counts.get(key, 0) + 1
        multi_role_pairs = {f"{a}->{b}": count for (a, b), count in pair_counts.items()
                            if count > 1}
        declared_edges = len(self.fk_edges)
        return {
            "fk_constraint_groups": groups,
            "fk_column_pairs": column_pairs,
            "composite_fk_groups": len(composite),
            "composite_fk_ids": [item.relation_id for item in composite],
            "composite_fk_columns": [
                {"relation_id": item.relation_id,
                 "source_columns": list(item.source_columns),
                 "target_columns": list(item.target_columns)} for item in composite],
            "multi_role_table_pairs": multi_role_pairs,
            "declared_fk_edges_in_graph": declared_edges,
            "groups_preserved_as_units": declared_edges == groups,
            "verified_groups": sum(1 for item in self.relations if item.verified),
            "node_count": self.graph.number_of_nodes(),
            "edge_count": self.graph.number_of_edges(),
            "note": "90 个约束组 / 91 个列对（唯一复合外键 2 列）；反向遍历边显式标 reverse=True。",
        }
