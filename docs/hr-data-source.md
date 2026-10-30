# HR 第二数据源

## 目标与边界

`enterprise_hr` 是用于证明跨业务域扩展能力的轻量 SQLite 数据源，不是多数据源管理平台。它复用同一套 LangGraph Agent、业务指标 RAG、Schema Linking、Text-to-SQL、只读执行和可视化流程；不支持跨库 JOIN、真实员工隐私数据或薪酬分析。

## 可复现数据

运行以下命令会用固定随机种子生成销售库和 HR 库，并同步生成字段描述：

```powershell
uv run python -B runner/create_enterprise_demo.py
```

HR 数据截止日固定为 `2026-10-28`。当前数据包含 6 个部门、180 名员工、916 条月度出勤记录和 262 条绩效记录，共 1,364 条业务记录。生成器会校验外键、入离职日期、出勤数值范围和绩效范围；重复运行结果一致。

| 表 | 作用 | 关键字段 |
| --- | --- | --- |
| `departments` | 部门主数据 | `department_id`、`department_name`、`cost_center` |
| `employees` | 员工及入离职 | `employee_id`、`department_id`、`hire_date`、`termination_date` |
| `attendance` | 月度出勤 | `employee_id`、`attendance_month`、`scheduled_days`、`attended_days`、`late_days` |
| `performance_reviews` | 绩效记录 | `employee_id`、`review_period`、`performance_score`、`performance_grade` |

`attendance` 的天数字段使用 REAL，避免 SQLite 聚合后发生整数除法；业务结果仍表示天数。

## HR 指标知识

指标定义位于 `app/enterprise/business_metrics.json`，均带有别名、定义、公式、相关表字段、时间口径和 SQL 约束。

| 指标 | 核心口径 |
| --- | --- |
| 在职人数 | 截止日已入职，且离职日为空或晚于截止日 |
| 新入职人数 | `hire_date` 位于统计周期内 |
| 离职人数 | `termination_date` 位于统计周期内 |
| 员工流失率 | 周期内离职人数 / 期初在职人数 × 100% |
| 平均绩效 | 指定或最新绩效周期的 `AVG(performance_score)` |
| 部门人员占比 | 部门在职人数 / 公司总在职人数 × 100% |
| 出勤率 | 实际出勤天数 / 应出勤天数 × 100% |
| 迟到率 | 迟到天数 / 实际出勤天数 × 100% |

指标检索按 `database_id` 过滤。HR 问题不会召回销售指标，销售问题也不会召回 HR 指标。

## 数据源选择

- `database_id=auto`：LLM 通过 `select_data_source` 工具在 `enterprise_sales` 和 `enterprise_hr` 之间选择。
- 手动选择：用户指定的数据源优先，Agent 不允许工具覆盖。
- 切换数据源：清空上一数据库的 Schema、指标、SQL 和结果状态；会话记忆以 `conversation_id + database_id` 隔离。
- 显式保护：明显的 HR 问题不能选择销售库，明显的销售问题不能选择 HR 库。

## 验收命令

```powershell
# 数据完整性、6 个可信 SQL 和指标召回
uv run python -B runner/verify_hr_demo.py

# 6 个 HR Agent 端到端问题
uv run python runner/evaluate_agent.py --cases evals/hr/questions.json --output evals/hr/latest/summary.json

# 接入 HR 后的销售域回归
uv run python runner/evaluate_agent.py --cases evals/hr/sales-regression.json --output evals/hr/sales-regression-summary.json
```

最终结果：HR 6/6，销售回归 1/1，全量自动化测试 80/80（含后续新增的 `.env` 配置与刷新恢复测试）。详细耗时和 Token 见 [HR 评测报告](../evals/hr/report.md)。
