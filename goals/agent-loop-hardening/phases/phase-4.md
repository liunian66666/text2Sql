---
phase_id: 4
goal_id: agent-loop-hardening
name: "图状态化：单例复用 + join path 工具 + SQL 边校验（C5）"
status: completed
criterion_id: C5
---

# Phase 4: 图状态化

## Objective

把知识图从「每次请求从磁盘重建、转成一段文字塞进 Prompt」升级为三件事：
① 进程级只读单例（产物变更自动失效）；② 模型可主动调用的 `inspect_join_path`；
③ 最终 SQL 的 FROM/JOIN 与图对账，不一致写进结果元数据。

## Exit Criterion

- `tests.test_graph_state` 全绿（12 例）。
- 同一份产物两次 `from_artifacts` 返回同一对象；产物改写后自动失效。
- `inspect_join_path` 对 AdventureWorks 返回真实候选路径 + `duplicate_aggregation` 等风险；非 AW 库如实拒绝。
- SQL 用了候选路径之外的表 → `join_path_warnings` 出现 `unexpected_table`；复合外键缺列 → `composite_fk_incomplete`。
- 全量回归不降。

## Dependencies

Phase 1（`analyses` 槽位承载校验结论）。

## Plan

- [x] Step 4.1: `GraphStore.from_artifacts(use_cache=True)` + `_STORE_CACHE`（key 含 mtime_ns/size）+ `clear_store_cache()`；同目录旧签名自动淘汰
- [x] Step 4.2: `app/agent/tools/join_path_tool.py` + `ToolRegistry._inspect_join_path`：短名/Schema.Table 均可解析，非 AW 库拒绝，同表组合走缓存
- [x] Step 4.3: `state` 增 `join_path_context` / `join_path_cache`（只存引用与结论，不存 networkx 对象）
- [x] Step 4.4: `app/agent/join_guard.py`：`extract_tables`（排除 CTE）、`validate_sql_joins`（表外溢 / 复合外键缺列）
- [x] Step 4.5: `_run_text_to_sql` 接入校验：结论写进 `compact`（模型可见）与 `analyses`（审计可见），不直接拒绝执行
- [x] Step 4.6: 系统提示新增规则 2.2（多表关联先查 join path，遇 warning 必须重写或说明）
- [x] Step 4.7: 新增 `tests/test_graph_state.py`（12 例）；更新工具数量断言（7→8）

## 关键设计决策

- **图对象不进 state**：71 表 690 边的 networkx 对象过 checkpointer 会膨胀且无法 JSON 化。state 只放 `{tables, paths, risks}` 引用。
- **单例有失效机制**：key 用 `(路径, database_id, mtime_ns+size)`，重跑 `build-knowledge` 后下次请求自动重建，不需要重启进程。
- **校验是提示不是拦截**：只能证明「与图不一致」，不能证明「业务口径错」（模型可能有意识地多关联）。结论交给模型说明或人工判断。
- **不加 sqlglot**：用受限正则 + CTE 名排除，覆盖项目里实际出现的 `"Schema.Table"` 写法；完整 SQL 解析属于另一个量级的依赖成本。

## Phase Progress Log

### Phase iteration 0 — completed

- **C5 证据**：`unittest tests.test_graph_state -v` → `Ran 12 tests ... OK`
  - 同产物 → 同一对象（`assertIs`）；`use_cache=False` 绕开；改写 snapshot + 推进 mtime → 新对象；CTE 名不被当成表；表外溢 → `unexpected_table: Address`；复合外键缺 `SpecialOfferID` → `composite_fk_incomplete`；真实 AW 产物返回 1 条 SalesOrderHeader→SalesOrderDetail 路径 + `duplicate_aggregation` 风险；同表组合（顺序不同）命中缓存；非 AW 库 `failed` 且提示改用 schema 工具；端到端两张 SQL 分别产生 warning / 无 warning。
- **真实图数据**：71 表 / 486 列 / 690 边，节点 id 形如 `adventureworks.Sales.SalesOrderHeader`，工具做了短名映射。
- **回归**：`Ran 356 tests ... OK`；`compileall` 通过。
- **Files**：`app/knowledge/graph_store.py`、`app/agent/tools/join_path_tool.py`（新）、`app/agent/tools/registry.py`、`app/agent/join_guard.py`（新）、`app/agent/state.py`、`app/agent/service.py`、`app/agent/prompts/planner.py`、`tests/test_graph_state.py`（新）、`tests/test_adventureworks_integration.py`
