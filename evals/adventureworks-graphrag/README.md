# AdventureWorks GraphRAG 评测集（开发集 / 独立测试集）

> **数据来源声明**：底层数据为微软公开**示例数据库** AdventureWorks OLTP（MIT 许可），**不是真实生产企业数据**。
> 业务口径来自 `docs/simulated-policies/` 下 **SIMULATED / UNREVIEWED 模拟制度**，**不是微软官方业务规则**。
> 本评测集用于验证知识检索与 SQL 生成能力，任何结果不得描述为真实业务结论。

## 文件

| 文件 | 题量 | 用途 |
|---|---|---|
| `dev.json` | 12 | 开发集：可用于错误分析、Prompt 与检索策略调参 |
| `test.json` | 6 | **独立测试集**：封存，不得用于调参 |

## 覆盖的四类问题

| 类别 | dev | test | 考察点 |
|---|---|---|---|
| `intermediate_table` 必要中间表补全 | 3 | 1 | 商品→类别必经 `ProductSubcategory`；员工→部门必经 `EmployeeDepartmentHistory`；供应商→商品必经 `ProductVendor` |
| `multi_join_path` 多候选 Join 路径 | 3 | 2 | 订单头/客户/省州三处 `TerritoryID`；销售快照与实时订单两条路径；多路径须显式标注口径 |
| `historical_cost` 历史成本匹配 | 3 | 1 | `ProductCostHistory` 区间匹配、`EndDate IS NULL`、成本/价格历史表笛卡尔积风险 |
| `grain_conflict` 粒度冲突 | 3 | 2 | 订单头×明细重复聚合、库存按商品聚合、复合外键单列关联膨胀 |

## 每题字段

`id`、`question`、`database_id`、`category`、`difficulty`、`required_tables`、
`required_intermediate_tables`、`acceptable_join_paths`、`policy_evidence`、
`reference_sql`、`reference_result`（columns + rows）、`key_conclusion`、
`validation_rules`、`counter_example`（可选，用于反例对照）、
`reference_sql_verified`、`split`。

`reference_sql` 与 `reference_result` **全部由 `app.skills.sql_skill.execute_readonly_table`
在真实 SQLite 上实际执行取得**，原始证据保存在
`data/adventureworks/query_evidence.json` 与 `test_split_evidence.json`（该目录被 gitignore，属本地构建产物）。
禁止使用待评测 Agent 的输出作为标准答案。

## 已验证的真实数值（节选）

| 场景 | 正确值 | 错误/对照值 |
|---|---|---|
| 订单销售额（SubTotal 合计） | 109,846,381.40 / 31,465 单 | 订单头×明细错误 JOIN → 2,596,470,456.68（约 23.6×） |
| 采购应付总额 | 70,479,332.64 / 4,012 单 | 采购头×明细错误 JOIN → 180,741,104.49 |
| 复合外键关联行数 | 121,317（双列） | 仅 `ProductID` 单列 → 296,983 |
| 低于再订货点商品数 | 7（先按商品聚合） | 逐库位比较口径错误 |
| 库存总量 | 335,974 / 432 个商品 | 按商品聚合前后守恒 |
| 成本×价格历史仅按 ProductID 关联 | 两表各 395 行 | 关联后 649 行（区间粒度膨胀） |

## 隔离规则（POL-GOV-001 §5）

1. `test.json` 的题目、参考 SQL 与结论**不得**进入 Few-shot 示例、SQL 经验知识库、指标别名或任何调参语料。
2. 不得用 `test.json` 反复优化 Prompt；每次复评须记录模型、预算、样本与结果文件。
3. `dev.json` 与 `test.json` 的题目不得互相泄漏；示例题文件
   `app/enterprise/adventureworks_examples.json` 中的问题**不与测试集题目重复**。
4. 需区分「SQL 执行成功」与「业务语义正确」：本评测集的 `grain_conflict` 类题目专门考察执行成功但语义错误的情况。

## 暂未实现（下一阶段）

本轮**不实现完整 GraphRAG 在线检索**，也不运行在线模型评测。后续 Phase 3 接入子图检索后，
可用本评测集测量：必要中间表召回率、Join 路径正确率、SchemaContext Token 数、SQL 执行准确率与业务语义正确率。
独立测试集须保持封存，直到最终对照实验时才使用。
