# POL-GOV-001 指标治理、审核状态与评测隔离规则

> **SIMULATED / 模拟企业制度** — 项目自行编写的演示条款，**不是微软官方业务规则**，**未经任何真实企业审核**。底层数据来自微软公开示例库 AdventureWorks OLTP（MIT），**不是真实生产企业数据**。状态 `UNREVIEWED`。

| 项 | 值 |
|---|---|
| 文档 ID | POL-GOV-001 |
| 版本 | 0.1-simulated |
| 状态 | 待人工审核 UNREVIEWED |
| 适用范围 | `app/knowledge/`、`app/enterprise/adventureworks_metrics.json`、`evals/adventureworks-graphrag/` |
| 生效日期 | 模拟值 2022-01-01（无真实效力） |

## §2 可信边界

**§2.1** 本项目所有来自 `docs/simulated-policies/` 的业务规则均为**模拟待审核条款**。由其派生的指标定义一律 `reviewed=False`、`trusted=False`，**不得**作为硬 SQL 约束、不得写入 SQL 经验库、不得自动回灌 Few-shot。

**§2.2** `trusted = reviewed AND unknown_fields 为空`。Phase 1 已实现的这条规则继续生效；编辑 JSON 文件不能绕过它（`_apply_structured` 明确禁止覆盖 `reviewed`）。

**§2.3** 指标 `expression` 必须逐字引用条款文本；`provenance` 必须记录推导方式；`source_ref` 必须指向 `docs/simulated-policies/<file>.md#<clause>` 或 SQLite PRAGMA 事实。

## §3 字段真实性校验

**§3.1** 指标引用的所有表/列必须通过 `SchemaExtractor` 对真实 SQLite 元数据校验；出现 `missing_table` / `missing_column` 即 `validation.error`，该指标不得进入知识快照。

**§3.2** 表名含 SQL Server Schema 点号前缀（如 `Sales.SalesOrderHeader`）。列引用采用 `<完整表名>.<列名>`，解析使用 `rsplit(".", 1)`；禁止把 `"Sales"."SalesOrderHeader"` 当作两个标识符写入知识层。

**§3.3** 禁止 LLM 推断外键：知识边只来自 `PRAGMA foreign_key_list` 的声明关系，人工补充关系必须标记 `origin=manual`、`verified=False`。

## §4 版本与失效

**§4.1** 指标版本、Schema 指纹、来源文件哈希共同构成 `KnowledgeSnapshot.content_fingerprint`。数据库 Schema 变化后必须重新构建并重新校验所有受影响指标。

**§4.2** 禁止把新增字段自动认定为旧字段的等价替代；字段删除后必须列出受影响的指标（Phase 1 `KnowledgeIndex.metrics_affected_by_column` 已实现该查询）。

## §5 评测隔离

**§5.1** `evals/adventureworks-graphrag/dev.json` 用于开发调参；`test.json` 为**独立测试集**，题目、参考 SQL 与结论不得用于 Prompt 调优、不得进入 Few-shot / 经验知识库。

**§5.2** 参考 SQL 必须在真实 SQLite 上实际执行通过，其结果与预期数值写入评测文件；禁止使用待评测 Agent 的输出作为标准答案。

**§5.3** 所有报告必须区分「执行成功」与「业务语义正确」，并明确标注实验数据来自**公开示例库 + 模拟制度**，不得描述为真实企业数据或真实业务结论。

## §6 待审核事项

| 编号 | 待审核内容 |
|---|---|
| R-GOV-1 | 哪些模拟条款可由业务人员正式审核并升为可信 |
| R-GOV-2 | 指标审核流程、审核人与留痕格式 |
| R-GOV-3 | 测试集封存与复评周期 |
