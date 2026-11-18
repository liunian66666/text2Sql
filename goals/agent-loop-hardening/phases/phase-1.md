---
phase_id: 1
goal_id: agent-loop-hardening
name: "结果聚合与终止状态（C1+C2）"
status: completed
criterion_id: C1, C2
---

# Phase 1: 结果聚合与终止状态

## Objective

让「一次请求里跑了多条 SQL」的结果可逐条追溯，并让四种未完成/降级终止条件如实写进 `termination_reason`，使 `status=partial` 真实可信。

## Exit Criterion

- C1：`tests.test_multi_sql_aggregation` 全绿 —— 3 条 SQL 的 SQL 文本、列名、行数、预览行都能从 `AgentResult.analyses` 逐条取回。
- C2：`tests.test_termination_status` 全绿 —— `capability_gap` / `probe_limit` / `sql_budget_limit` / `tool_limit` 四路径都产出 `partial` + 对应 reason。
- 全量 `unittest discover` ≥ 312 且 OK。

## Dependencies

无（首阶段）。

## Plan

- [x] Step 1.1: 记录基线 `Ran 312 tests OK`，写入 GOAL Progress Log
- [x] Step 1.2: `state.py` 新增 `sql_results: Annotated[list, operator.add]` 与 `sql_results_turn_id`；`service.py` 初始化两键
- [x] Step 1.3: `registry.py::_run_text_to_sql` 每次成功/失败都追加一条子结果记录（`question`/`sql`/`columns`/`row_count`/`preview_rows`/`status`/`error`），预览上限 50 行，不修改既有 `sql_result` 单值语义
- [x] Step 1.4: `AgentResult` 新增 `analyses: list[dict]`；`service.py` 两处返回装配按 `turn_id` 过滤注入
- [x] Step 1.5: 三处补写 `termination_reason`：能力缺口（仅真缺口，不含自动切换成功）→ `capability_gap`；探查上限 → `probe_limit`；SQL 上限 → `sql_budget_limit`
- [x] Step 1.6: 新增 `tests/test_multi_sql_aggregation.py`（5 例）、`tests/test_termination_status.py`（6 例）
- [x] Step 1.7: 跑 `phase1` + `test` 验证命令，更新 GOAL/phase 进度，进入 Phase 2

## 关键设计约束

- **不改变 `sql_result` 单值语义**：它仍代表「最后一次」，`service.py` 现有的 `sql/columns/rows` 装配、可视化、前端渲染全部依赖它，动它会连锁破坏。
- **`sql_results` 只存预览**：`preview_rows` 上限 50 行 + 真实 `row_count`。理由：AW 单条查询可能返回上万行，全量入 state 会让 checkpoint 与最终 JSON 响应膨胀；聚合本来就该在 SQL 里完成，模型需要的是「结构 + 行数 + 样本」，不是全量数据。
- **能力缺口只在真缺失时报**：`requested == "auto"` 且成功切换数据源属于「已解决」，不写 reason、不降级为 partial。

## Phase Progress Log

### Phase iteration 0 — plan created

- **Status**: in_progress
- **Baseline**: `.venv/Scripts/python.exe -m unittest discover -s tests` → `Ran 312 tests in 11.520s / OK`

### Phase iteration 1 — completed

- **C1 证据**：`unittest tests.test_multi_sql_aggregation -v` → `Ran 5 tests ... OK`
  - 3 条 SQL 的 `sql`/`row_count`/`columns`/`question` 逐条可取；`sql`=最后一条兼容不变；120 行查询 → `row_count=120` + `preview_rows=50`；失败 SQL 也留痕；第二轮 `analyses` 只含本轮。
- **C2 证据**：`unittest tests.test_termination_status -v` → `Ran 6 tests ... OK`
  - 手动选缺能力库 → `capability_gap`；auto 成功切换 → 不写 reason；探查 6 次 → `probe_limit`；SQL 3 次 → `sql_budget_limit`；两条端到端断言 `status=partial`。
- **顺带修掉的真 bug**：`service.py` 用两个 `**` 并列展开，`skill` 自带 `status` 时与 partial 的 `status` 撞成 `got multiple values for keyword argument`。「已有成功 SQL + 后续触发终止」这条真实路径原本会 500。
- **行为变化**：`tests/test_complex_analysis_flow.py::test_probe_then_plan_then_answer` 从 `success` 改为 `partial + capability_gap` —— 该库确实没有成本历史表，旧断言本身就是假成功，已同步更新并注明原因。
- **回归**：`Ran 323 tests ... OK`；`compileall` 通过。
- **Files**：`app/agent/state.py`、`app/agent/service.py`、`app/agent/tools/registry.py`、`app/web/static/chat.js`（四类 reason 文案）、`tests/test_multi_sql_aggregation.py`、`tests/test_termination_status.py`
