---
phase_id: 2
goal_id: agent-loop-hardening
name: "Agent 层预算硬闸（C3）"
status: completed
criterion_id: C3
---

# Phase 2: Agent 层预算硬闸

## Objective

把编排层每一次模型调用（assistant / planner / tool_limit 收尾）纳入 `BudgetMeter`，使 Web 路径也有 token 与调用次数硬上限，并且预算耗尽是一种**可识别终止**，不是「模型暂时不可用」。

## Exit Criterion

- `tests.test_agent_budget_gate` 全绿（8 例）。
- `max_calls=1` 时第 2 次调用在发出 HTTP 之前抛 `BudgetExceededError`。
- 预算耗尽后 `AgentResult.status="error"` + `termination_reason="llm_budget_exhausted"`，不再循环。
- ContextVar 随请求复位，跨请求不泄漏。
- 全量回归不降。

## Dependencies

Phase 1（终止原因通道）。

## Plan

- [x] Step 2.1: `app/agent/llm.py` 新增 `MeteredChatModel`（invoke 前 `meter.check()`、后 `note_call`），`bind_tools` 返回同样计费的克隆
- [x] Step 2.2: `agent_budget_limits()` 解析顺序：显式覆盖 > `AGENT_MAX_TOKENS/CALLS/SECONDS` > 默认（300k / 24 / 900s）
- [x] Step 2.3: `DataAnalysisAgent.__init__` 统一包装 chat_model；`run()` 用 `start_meter` 包住 `graph.invoke`，`finally` 复位
- [x] Step 2.4: `graph.call_model` 单独捕获 `BudgetExceededError` → `termination_reason="llm_budget_exhausted"` + 明确文案；`planning.generate_plan` 对该异常上抛而不是降级
- [x] Step 2.5: `service._budget_exhausted_result()` 兜住 planner 阶段抛出的异常，返回带 usage 快照的 error 结果
- [x] Step 2.6: 新增 `tests/test_agent_budget_gate.py`（8 例），跑全量回归

## Phase Progress Log

### Phase iteration 0 — completed

- **C3 证据**：`unittest tests.test_agent_budget_gate -v` → `Ran 8 tests ... OK`
  - 无 meter 时透传；`max_calls=1` 第 2 次调用抛 `call budget exceeded` 且 inner 只执行 1 次；usage 记账 120 tokens 后 token 上限生效；`bind_tools` 后仍计费；assistant 轮超限 → `error + llm_budget_exhausted`；planner 轮超限 → 同样 surface 成 error；两次请求各 1 次调用（无泄漏）。
- **回归**：`Ran 331 tests ... OK`（323 + 8）。
- **Files**：`app/agent/llm.py`、`app/agent/service.py`、`app/agent/graph.py`、`app/agent/planning.py`、`tests/test_agent_budget_gate.py`
- **注意**：`tests` 里注入的 fake model 没有 `usage_metadata`，因此只计调用次数、不计 token —— 这是刻意的：硬闸的可测试性优先，token 计量由 `test_usage_is_booked_and_token_cap_enforced` 单独覆盖。
