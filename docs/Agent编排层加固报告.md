# Agent 编排层加固报告

> 日期：2026-11-16 · 范围：`app/agent/`、`app/knowledge/graph_store.py`、`app/web/static/chat.js`
> 验证方式：**全部离线**（脚本化模型 + 真实 SQLite）：`unittest discover -s tests` → **356 项 OK**，`compileall` 通过。
> **未做在线端到端回归**（不消耗模型配额，未获授权）。因此「真实模型下的行为是否改善」本文不做结论。

## 1. 为什么要做这一轮

2026-11-16 的只读核查在编排层发现 5 个结构性缺口。它们的共同特征不是「功能没做」，而是**失败被伪装成成功**：

| # | 缺口 | 代码证据 |
|---|---|---|
| 1 | 多 SQL 子结果被单值覆盖 | `state.sql_result` 单值槽，`registry.py` 每次写覆盖；`AgentResult` 只取最后一次 |
| 2 | 终止原因只有一处赋值 | `termination_reason` 全库仅 `graph.py:125`；SQL/探查上限、能力缺口都只返回 failed |
| 3 | Agent 层绕过预算闸 | `app/agent/llm.py` 用裸 `ChatOpenAI`，`BudgetMeter` 只保护 SQL pipeline |
| 4 | 无通用去重、无反思节点 | `_question_key` 只做字符归一；5 节点图里没有 reflection |
| 5 | 图只是证据文本 | 每次请求 `from_artifacts` 重建，7 个工具里没有 join-path 能力 |

## 2. 四层修复

### 2.1 结果不再丢（C1）

- `state.sql_results: Annotated[list, operator.add]`，每条记录含 `question / sql / columns / row_count / preview_rows / status / error`。
- `AgentResult.analyses` 按 `turn_id` 过滤后返回，复合任务的每个子问题都可追溯。
- **`sql/columns/rows` 语义不变**（仍是最后一次），画图与前端零改动。
- 预览上限 50 行 + 真实 `row_count`：全量 rows 进 state 会撑大 checkpoint，而聚合本就该在 SQL 里完成。

### 2.2 失败不再装成成功（C2）

四类终止条件统一写入 `termination_reason` 并映射 `status=partial`（无任何产出时为 `error`）：

| reason | 触发点 |
|---|---|
| `capability_gap` | 手动选库且真实 Schema 缺能力（自动切换成功不算） |
| `probe_limit` | 本轮探查达 6 次 |
| `sql_budget_limit` | 本轮业务 SQL 达 3 次 |
| `tool_limit` | 工具轮数达 8 轮 |
| `loop_detected` | 同参连续调用第 3 次 / 反思判定 stop |
| `llm_budget_exhausted` | Agent 层模型预算耗尽 |

前端 `chat.js` 同步给出四种明确文案，不再退化成笼统的「未完成全部要求」。

### 2.3 成本有硬闸（C3）

- `MeteredChatModel`：`invoke` 前 `meter.check()`、后 `note_call()`，`bind_tools` 后仍计费。
- `service.run()` 用 `start_meter(...)` 包住整张图，`finally` 复位（测试证明不跨请求泄漏）。
- 预算来源：显式覆盖 > `AGENT_MAX_TOKENS/CALLS/SECONDS` > 默认 300k / 24 次 / 900 秒。
- 预算是「可识别终止」：`graph.call_model` 与 `planning.generate_plan` 都单独处理，不会被降级成「模型暂时不可用」。

### 2.4 循环有防线（C4）

**指纹层**：`tool_call_signature = 工具名 + sha256(canonical JSON)`。

- 第 2 次同参：不进 `execute`，直接返回缓存 + `cached_tool_result` 与「换招」提示。
- 第 3 次同参：`failed` + `loop_detected` + `termination_reason`。
- 计数按 `turn_id` 隔离；失败结果不缓存（瞬时错误重试有意义）。

**反思层**：`should_reflect` 只在出现「缓存复用 / 连续 2 次失败」时触发，每题一次。输出 `continue | finish | stop`：

- `finish` → 一次无工具调用直接总结；
- `stop` → 同上 + `termination_reason="loop_detected"`；
- 解析失败 → 保守取 `continue`，不误杀。

> 设计取舍：最初按「3 轮必反思」实现，实测直接打爆 4 个既有测试的脚本契约，而且 3-4 轮本就是正常多步分析。**定时反思等于用成本换焦虑**，改成证据驱动后正常路径零额外调用。

### 2.5 图变成状态（C5）

三层拆分：

| 层 | 做法 |
|---|---|
| 资源 | `GraphStore` 进程级单例，key = `(路径, database_id, mtime_ns+size)`；重跑 `build-knowledge` 自动失效 |
| 能力 | 新工具 `inspect_join_path(tables)`：返回候选路径、跳数、复合外键完整性、风险与规避要求；短名/Schema.Table 都能解析 |
| 状态 | `state.join_path_context` 只存 `{tables, paths, risks}` —— networkx 对象不进 checkpoint |
| 闭环 | SQL 生成后 `validate_sql_joins` 对账：`unexpected_table` / `composite_fk_incomplete` 写进 `analyses` 与模型可见的 `compact` |

## 3. 顺手抓到的真实缺陷

| # | 缺陷 | 影响 | 修复 |
|---|---|---|---|
| 1 | `AgentResult(**{**skill}, **{status: partial})` 两个 `**` 并列 | 「已有成功 SQL + 后触发终止」直接 `TypeError` 500 —— 真实路径，此前从未被测试覆盖 | 先合成 payload 再 `update` |
| 2 | `termination_reason` 存在但无产物时仍 return `success` | 反思判定循环、零产出 → 用户看到「分析完成」 | 补 `error` 分支 |
| 3 | `test_probe_then_plan_then_answer` 断言 `success` | 该库确实没有成本历史表，旧断言本身就是假成功 | 改为 `partial + capability_gap` |
| 4 | `test_tool_round_limit` 用 9 次同参调用 | 新去重层会让它第 3 次就 `loop_detected`，测不到 8 轮闸门 | 改用不同参数 |

## 4. 验证证据

```
unittest tests.test_multi_sql_aggregation   Ran 5 tests   OK   (C1)
unittest tests.test_termination_status      Ran 6 tests   OK   (C2)
unittest tests.test_agent_budget_gate       Ran 8 tests   OK   (C3)
unittest tests.test_loop_governance         Ran 13 tests  OK   (C4)
unittest tests.test_graph_state             Ran 12 tests  OK   (C5)
unittest discover -s tests                  Ran 356 tests OK   (基线 312 + 新增 44)
compileall app runner tests                 OK
```

## 5. 仍然不解决（诚实边界）

1. **单次 SQL 的执行成功 ≠ 业务口径正确**。本轮只做「与图一致性」校验，没有 result-level 语义校验（未新增 Validator，按既定收口）。
2. **检索路径质量未改善**。图校验能发现「绕开路径」，但 Top-1 完整率 0.077 这类检索算法问题不在本轮范围。
3. **能力识别仍是窄关键词**（3 类中文词），换说法仍会漏。
4. **planner 仍是一次性软约束**，`normalize_plan` 仍会把 `database_id` 改成当前选库。
5. **所有证据都是离线脚本化模型**。真实模型下反思触发频率、去重命中率、`analyses` 是否真的被模型用起来，需要一次在线回归才能回答 —— 未执行。

## 6. 面试讲法（三句话）

1. 循环调用的显性入口我用两层堵：Infra 层对「工具名+参数」做哈希，第二次复用、第三次判定循环；认知层在出现循环证据时插一次反思，决定 continue/finish/stop。
2. 反思不做定时器，只在有证据时触发 —— 正常多步分析不该为「可能的循环」付一次模型调用。
3. 图我没有塞进 state（networkx 会撑爆 checkpoint），而是做成进程级单例 + 一个 `inspect_join_path` 工具 + SQL 生成后的边对账，让图从「提示文本」变成「可查询、可校验的状态」。
