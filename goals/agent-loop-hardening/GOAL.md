---
goal_id: agent-loop-hardening
title: "Agent 编排层加固：多 SQL 结果聚合、终止状态真实化、预算硬闸、循环治理、图状态化"
status: COMPLETE
planning_level: executing
iteration: 4
max_iterations: 40
current_phase: 4
phases_total: 4
started_at: 2026-11-16
last_evaluation: 2026-11-16
blocker: null
active_step: "all phases complete; 356 tests OK"
execution_mode: run_until_complete
last_memory_checkpoint: 4
max_hours: 6
---

# GOAL: Agent 编排层加固

## Objective

消除 2026-11-16 只读核查在 Agent 编排层发现的五个结构性缺口，使「复杂业务查询」的失败不再以 `success` 掩盖，并使知识图从「证据文本生成器」变成「Agent 可查询、可校验的状态」：

1. 多 SQL 子结果被单值覆盖 → 复合任务无法真正聚合
2. `termination_reason` 只有 tool_limit 一处赋值 → 能力缺口/预算耗尽仍报 success
3. Agent 层裸 `ChatOpenAI` 绕过 `BudgetMeter` → Web 路径无 token 硬闸
4. 无反思节点、无通用调用哈希去重 → 循环只能靠 prompt 自觉
5. 知识图每次请求从磁盘重建、且未暴露为工具 → 「图 RAG」停在检索增强，不是图状态

## 边界约束（继承项目红线，不可协商）

- **离线优先**：所有验证必须在不消耗在线 LLM 配额的前提下可重复；任何在线回归需用户单独批准。
- **不重复上游**：`app/pipeline/`、`app/skills/` 的 Schema Linking / 值检索 / SQL 生成 / 修订逻辑不得重写，只允许在边界处适配。
- **不改历史评测产物**：`workspace/evals/` 下既有结果只读；不得改写、不得归因于本次改动。
- **不新增第三方依赖**：复用已装的 networkx / pydantic / langgraph；若确需新依赖必须先停下来问。
- **不编造数字**：所有结论必须来自本地命令输出；无证据写 BLOCKED，不写"预计提升"。
- **回归不降**：任何阶段结束，全量测试必须 ≥ 312 项且全通过。

## Completion Criteria (ALL must be satisfied)

- [x] **C1 多 SQL 结果不丢失**：同一轮 N 次 `run_text_to_sql` 的结果全部保留；`AgentResult.analyses` 按条列出 SQL 与行数；3 条 SQL 的离线测试通过。
- [x] **C2 终止状态真实**：`capability_gap` / `probe_limit` / `sql_budget_limit` / `tool_limit` 四类都写入 `termination_reason` 并映射 `partial`（零产出映射 `error`）；四路径各有测试。
- [x] **C3 Agent 层预算硬闸**：`MeteredChatModel` + `start_meter/reset_meter` 覆盖 assistant/planner/超限总结；`max_calls=1` 时第 2 次调用在 HTTP 前抛 `BudgetExceededError`。
- [x] **C4 循环治理**：`(tool_name, 归一化参数)` 指纹第 2 次复用、第 3 次 `loop_detected`；反思节点证据驱动、每题一次，`stop` 写 `loop_detected`。
- [x] **C5 图成为可操作状态**：`GraphStore` 进程级复用且产物变更自动失效；`inspect_join_path` 返回路径 + 风险；SQL 边对账产出 `unexpected_table` / `composite_fk_incomplete`。
- [x] **C6 全局回归**：`unittest discover` 356 项全通过（基线 312 + 新增 44），`compileall` 通过，未改动 `app/pipeline` 与 `workspace/evals` 的既有逻辑与产物。

## Evidence Required

| Criterion | Verification command | Result |
|-----------|---------------------|--------|
| C1 | `unittest tests.test_multi_sql_aggregation -v` | `Ran 5 tests ... OK` |
| C2 | `unittest tests.test_termination_status -v` | `Ran 6 tests ... OK` |
| C3 | `unittest tests.test_agent_budget_gate -v` | `Ran 8 tests ... OK` |
| C4 | `unittest tests.test_loop_governance -v` | `Ran 13 tests ... OK` |
| C5 | `unittest tests.test_graph_state -v` | `Ran 12 tests ... OK` |
| C6 | `unittest discover -s tests` + `compileall -q app runner tests` | `Ran 356 tests ... OK` / `COMPILE_OK` |

## Budget Limits

- Max iterations: 40（实际用 4）
- Max hours: 6（实际约 2.5 小时墙钟）
- 在线 LLM 调用：0

## Master Plan

| Phase | Name | Status | Plan file | Exit criterion |
|-------|------|--------|-----------|----------------|
| 1 | 结果聚合与终止状态（C1+C2） | completed | `phases/phase-1.md` | 323 项 OK |
| 2 | Agent 层预算硬闸（C3） | completed | `phases/phase-2.md` | 331 项 OK |
| 3 | 循环治理：指纹去重 + 反思（C4） | completed | `phases/phase-3.md` | 344 项 OK |
| 4 | 图状态化：单例 + 工具 + 边校验（C5） | completed | `phases/phase-4.md` | 356 项 OK |

## Current Execution Context

- **Planning level**: executing
- **Current phase**: 4（完成）
- **Active plan file**: `phases/phase-4.md`
- **Active step**: 无 —— 所有阶段退出条件已验证

## Progress Log

### Iteration 0 — created

- goal 脚手架 + intake + master plan；verify 命令改为项目真实命令
- 基线：`Ran 312 tests in 11.520s / OK`

### Iteration 1 — Phase 1 complete（C1 + C2）

- `sql_results` 追加槽 + `AgentResult.analyses`；三处补写 `termination_reason`；前端四类文案
- 抓真 bug：两个 `**` 并列展开导致「成功 SQL + 终止原因」TypeError 500
- 证据：`Ran 323 tests OK`

### Iteration 2 — Phase 2 complete（C3）

- `MeteredChatModel` + `agent_budget_limits` + `start_meter` 包住 graph + 两处预算异常单独处理
- 证据：`Ran 331 tests OK`

### Iteration 3 — Phase 3 complete（C4）

- 指纹去重（2 次复用 / 3 次阻断）+ 反思节点（证据驱动、每题一次）+ `loop_detected` 通道
- 决策修正：反思从「3 轮定时」改为「循环证据驱动」，避免无谓成本与误杀
- 证据：`Ran 344 tests OK`

### Iteration 4 — Phase 4 complete（C5）+ terminal

- `GraphStore` 单例（mtime 失效）+ `inspect_join_path` 工具 + `join_guard` 边对账
- 证据：`Ran 356 tests OK`，`compileall` 通过
- 交付：`docs/Agent编排层加固报告.md`
- **status: COMPLETE**
- **未做**：在线端到端回归（需用户批准配额）；`app/skills` 与 `app/pipeline` 未改（其未提交改动来自此前修复轮）
