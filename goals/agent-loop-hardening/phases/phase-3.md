---
phase_id: 3
goal_id: agent-loop-hardening
name: "循环治理：通用指纹去重 + 任务级反思（C4）"
status: completed
criterion_id: C4
---

# Phase 3: 循环治理

## Objective

补上两层循环防线：Infra 层的「工具名 + 参数」指纹去重，认知层的任务级反思。区分两种多轮：**参数不同 = 正常多步规划**（不拦），**参数完全相同 = 无效循环**（拦）。

## Exit Criterion

- `tests.test_loop_governance` 全绿（13 例）。
- 第 2 次完全相同调用不进 `execute`，直接复用；第 3 次判定 `loop_detected` 并写 `termination_reason`。
- 反思仅在出现缓存复用/连续失败时触发，每题一次；`stop` 写入 `loop_detected`，`finish` 正常收尾。
- 正常路径（无循环证据）**零额外模型调用**。
- 全量回归不降。

## Dependencies

Phase 1（终止原因通道）。

## Plan

- [x] Step 3.1: `registry.tool_call_signature()`（sha256 of canonical JSON）+ `MAX_IDENTICAL_TOOL_CALLS=3`
- [x] Step 3.2: `ToolRegistry.invoke` 三层处理：第 2 次复用缓存（`cached_tool_result` 标记）、第 3 次阻断 + `loop_detected`、其余正常执行；计数按 `turn_id` 隔离
- [x] Step 3.3: `app/agent/reflection.py`：`should_reflect`（证据驱动）/ `build_reflection_context`（只给摘要，不给全历史）/ `parse_decision`（容错）/ `reflect`
- [x] Step 3.4: `graph.py` 新增 `reflection` 节点与 `route_after_reflection`；`finish/stop` 用一次无工具调用收尾，`stop` 写 `loop_detected`
- [x] Step 3.5: `state` 增 `tool_calls_seen` / `reflection_done` / `reflection_decision` / `reflection_notes`；`AgentResult` 增 `reflection_notes`
- [x] Step 3.6: `service` 补一条假成功入口：`termination_reason` 存在但无 probe/visualization/analyses 时，状态从 `success` 改为 `error`
- [x] Step 3.7: 新增 `tests/test_loop_governance.py`（13 例）；更新 `test_agent.py` 的轮数上限用例（同参会被去重层拦截，改用不同参数才能测到 8 轮闸门）

## 关键设计决策

- **反思不用定时器**：最初按 `tool_rounds>=3` 触发，直接打爆 4 个既有测试的脚本契约，而且真实场景里 3-4 轮是正常分析。改成「出现缓存复用 / 连续 2 次失败」才触发 —— 成本几乎为零，且精准命中「原地打转」。
- **第 2 次复用而非直接报错**：模型收到 `cached_tool_result=True` 与明确提示后通常会换招；直接失败会让它以为是工具坏了。
- **失败结果不缓存**：瞬时错误（网络/超时）重试有意义，只有 `completed` 才复用。

## Phase Progress Log

### Phase iteration 0 — completed

- **C4 证据**：`unittest tests.test_loop_governance -v` → `Ran 13 tests ... OK`
  - 指纹与参数顺序无关、能区分工具与参数；第 2 次只执行 1 次（spy 计数）；第 3 次 `failed + loop_detected`；跨轮计数重置；反思在 cached/连续失败时触发、无证据时不触发、每题一次；`parse_decision` 容错；端到端 `stop` → `error + loop_detected`，`continue` → `success` 且 `reflection_notes` 有记录。
- **回归**：`Ran 344 tests ... OK` → Phase 4 后 `Ran 356 tests ... OK`。
- **Files**：`app/agent/reflection.py`（新）、`app/agent/graph.py`、`app/agent/state.py`、`app/agent/service.py`、`app/agent/tools/registry.py`、`app/agent/prompts/planner.py`、`tests/test_loop_governance.py`（新）、`tests/test_agent.py`
- **注意**：`reflection_notes` / `analyses` 都是按 `turn_id` 过滤的追加列表，多轮对话不会串数据。
