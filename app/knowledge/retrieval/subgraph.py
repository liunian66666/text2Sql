"""Metric-driven local subgraph expansion over declared FK relations.

Two mechanisms work together:

1. Bounded BFS from the retrieved seed tables (default ``max_hops=4`` — never
   a hard 2-hop cap, otherwise Product -> ProductSubcategory -> ProductCategory
   or Employee -> history -> Department could never be recovered).
2. Target-aware path completion: if a relevant table was recalled but is not
   within the BFS frontier, shortest paths from seeds to targets are enumerated
   and their intermediate tables are added explicitly.

If a target is still unreachable, a *limited* hop extension is attempted once.
If it still fails, ``fallback_triggered`` is set and the full BFS frontier is
returned instead of silently dropping the target. Every decision is recorded
in ``trace`` for observability.
"""
from __future__ import annotations

from collections import deque
from typing import Any

import networkx as nx
from pydantic import BaseModel, Field

from ..graph_store import GraphStore


class SubgraphResult(BaseModel):
    seed_tables: list[str] = Field(default_factory=list)
    seed_columns: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    added_tables: list[str] = Field(default_factory=list)
    added_columns: list[str] = Field(default_factory=list)
    intermediate_tables: list[str] = Field(default_factory=list)
    relations_used: list[str] = Field(default_factory=list)
    hops_used: int = 0
    extended_hops: int = 0
    fallback_triggered: bool = False
    unreachable_targets: list[str] = Field(default_factory=list)
    excluded: list[dict[str, str]] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)


def _short(database_id: str, node_id: str) -> str:
    prefix = f"{database_id}."
    return node_id[len(prefix):] if node_id.startswith(prefix) else node_id


class SubgraphExpander:
    def __init__(self, store: GraphStore, *, max_hops: int = 4,
                 max_tables: int = 16, max_columns: int = 80,
                 extension_bonus: int = 2):
        self.store = store
        self.max_hops = max_hops
        self.max_tables = max_tables
        self.max_columns = max_columns
        self.extension_bonus = extension_bonus
        self.projection = store.table_projection()

    def _bfs(self, seeds: list[str], hops: int) -> tuple[dict[str, int], list[str]]:
        distances = {seed: 0 for seed in seeds}
        queue = deque(seeds)
        relations: set[str] = set()
        while queue:
            current = queue.popleft()
            if distances[current] >= hops:
                continue
            for neighbor, edge in self.projection[current].items():
                relations.update(edge.get("relation_ids", []))
                if neighbor not in distances:
                    distances[neighbor] = distances[current] + 1
                    queue.append(neighbor)
        return distances, sorted(relations)

    def expand(self, seed_tables: list[str], seed_columns: list[str], *,
               target_tables: list[str] | None = None,
               focus_tables: list[str] | None = None,
               allow_extension: bool = True) -> SubgraphResult:
        database_id = self.store.database_id
        valid_seeds: list[str] = []
        excluded: list[dict[str, str]] = []
        for table in seed_tables:
            node_id = table if table in self.projection else f"{database_id}.{table}"
            if node_id in self.projection:
                if node_id not in valid_seeds:
                    valid_seeds.append(node_id)
            else:
                excluded.append({"table": table, "reason": "not a table node in graph"})

        distances, bfs_relations = self._bfs(valid_seeds, self.max_hops)
        tables = list(distances.keys())
        target_ids: list[str] = []
        for table in (target_tables or []):
            node_id = table if table in self.projection else f"{database_id}.{table}"
            if node_id in self.projection and node_id not in target_ids:
                target_ids.append(node_id)

        intermediate: set[str] = set()
        path_relations: set[str] = set(bfs_relations)
        unreachable: list[str] = []
        extended_hops = 0
        completed_paths: list[dict[str, Any]] = []

        # Path completion is anchored on the *retrieved focus tables*, not on
        # the whole BFS frontier. Otherwise a nearby frontier node always gives
        # a 1-hop path and necessary intermediate tables are never surfaced.
        focus_ids: list[str] = []
        for table in (focus_tables if focus_tables is not None else seed_tables[:4]):
            node_id = table if table in self.projection else f"{database_id}.{table}"
            if node_id in self.projection and node_id not in focus_ids:
                focus_ids.append(node_id)
        if not focus_ids:
            focus_ids = valid_seeds[:4]

        for target in target_ids:
            focus_paths: list[list[str]] = []
            direct_only = False
            for seed in focus_ids:
                if seed == target:
                    continue
                # A declared direct FK needs no intermediate table; skip it so
                # that only genuinely multi-hop completions are reported.
                if self.projection.has_edge(seed, target):
                    direct_only = True
                    continue
                if not nx.has_path(self.projection, seed, target):
                    continue
                try:
                    path = nx.shortest_path(self.projection, seed, target)
                except nx.NetworkXNoPath:
                    continue
                if 2 <= len(path) - 1 <= self.max_hops:
                    focus_paths.append(path)
            if not focus_paths and not direct_only and allow_extension:
                extended_hops = self.max_hops + self.extension_bonus
                wider, wider_relations = self._bfs(valid_seeds, extended_hops)
                path_relations.update(wider_relations)
                for node in wider:
                    if node not in tables:
                        tables.append(node)
                for seed in focus_ids:
                    if seed == target or not nx.has_path(self.projection, seed, target):
                        continue
                    path = nx.shortest_path(self.projection, seed, target)
                    if 2 <= len(path) - 1 <= extended_hops:
                        focus_paths.append(path)
            if not focus_paths:
                if not direct_only:
                    unreachable.append(_short(database_id, target))
                continue
            best = min(focus_paths, key=lambda item: (len(item), "|".join(item)))
            for node in best[1:-1]:
                intermediate.add(_short(database_id, node))
            for node in best:
                if node not in tables:
                    tables.append(node)
            for left, right in zip(best, best[1:]):
                path_relations.update(self.projection[left][right].get("relation_ids", []))
            completed_paths.append({"from": _short(database_id, best[0]),
                                    "to": _short(database_id, best[-1]),
                                    "tables": [_short(database_id, n) for n in best],
                                    "hops": len(best) - 1})

        fallback = bool(unreachable)
        tables = [node for node in tables if node in self.projection]
        tables = sorted(tables, key=lambda node: (distances.get(node, 99), node))
        if len(tables) > self.max_tables:
            excluded.extend({"table": _short(database_id, node), "reason": "max_tables budget"}
                            for node in tables[self.max_tables:])
            tables = tables[:self.max_tables]

        # Columns: seeds first, then FK join columns and primary keys of kept tables.
        columns: list[str] = list(dict.fromkeys(seed_columns))
        normalized_relations = {rid.replace("#reverse", "") for rid in path_relations}
        for relation in self.store.relations:
            if relation.source_table not in tables and relation.target_table not in tables:
                continue
            if relation.relation_id not in normalized_relations:
                continue
            for column_id in [*relation.source_column_ids, *relation.target_column_ids]:
                short = _short(database_id, column_id)
                if short not in columns:
                    columns.append(short)
        for table in tables:
            attrs = self.store.nodes_by_id.get(table, {})
            for key in attrs.get("primary_keys", []):
                short = f"{_short(database_id, table)}.{key}"
                if short not in columns:
                    columns.append(short)
        if len(columns) > self.max_columns:
            excluded.extend({"column": name, "reason": "max_columns budget"}
                            for name in columns[self.max_columns:])
            columns = columns[:self.max_columns]

        short_tables = [_short(database_id, node) for node in tables]
        result = SubgraphResult(
            seed_tables=[_short(database_id, node) for node in valid_seeds],
            seed_columns=list(seed_columns), tables=short_tables, columns=columns,
            added_tables=[name for name in short_tables if name not in
                          [_short(database_id, node) for node in valid_seeds]],
            added_columns=[name for name in columns if name not in seed_columns],
            intermediate_tables=sorted(intermediate),
            relations_used=sorted(path_relations),
            hops_used=max(distances.values(), default=0),
            extended_hops=extended_hops,
            fallback_triggered=fallback, unreachable_targets=sorted(unreachable),
            excluded=excluded,
            trace={
                "max_hops": self.max_hops, "extended_hops": extended_hops,
                "target_tables": [_short(database_id, n) for n in target_ids],
                "completed_paths": completed_paths,
                "bfs_frontier_size": len(distances),
                "tables_after_budget": len(short_tables),
                "columns_after_budget": len(columns),
                "note": ("中间表来自真实外键路径；fallback 时保留 BFS 前沿，不静默丢弃目标表。"),
            },
        )
        return result
