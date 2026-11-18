# POL-PERF-001 销售绩效、配额与达成率规则

> **SIMULATED / 模拟企业制度** — 项目自行编写的演示条款，**不是微软官方业务规则**，**未经任何真实企业审核**。数据来自微软公开示例库 AdventureWorks OLTP（MIT），**不是真实生产企业数据**。状态 `UNREVIEWED`，引用指标一律 `reviewed=False` / `trusted=False`。

| 项 | 值 |
|---|---|
| 文档 ID | POL-PERF-001 |
| 版本 | 0.1-simulated |
| 状态 | 待人工审核 UNREVIEWED |
| 适用范围 | `Sales.SalesPerson`、`Sales.SalesPersonQuotaHistory`、`Sales.SalesTerritory` |
| 生效日期 | 模拟值 2022-01-01（无真实效力） |

## §2 字段依据

| 字段 | 真实表 | 类型 | 说明 |
|---|---|---|---|
| `BusinessEntityID` / `QuotaDate` | `Sales.SalesPersonQuotaHistory` | INTEGER / TEXT | **复合主键** |
| `SalesQuota` | `Sales.SalesPersonQuotaHistory` | `DECIMAL_TEXT(19,4)` | 当期配额 |
| `SalesYTD` / `SalesLastYear` | `Sales.SalesPerson` | `DECIMAL_TEXT(19,4)` | 人员销售快照 |
| `SalesYTD` / `SalesLastYear` | `Sales.SalesTerritory` | `DECIMAL_TEXT(19,4)` | 区域销售快照 |
| `Bonus` / `CommissionPct` | `Sales.SalesPerson` | `DECIMAL_TEXT(19,4)` / `DECIMAL_TEXT(10,4)` | 奖金 / 佣金比例 |

**§2.1 快照字段警告**：`SalesYTD` / `SalesLastYear` 是示例库中的**预置快照值**，不是由 `SalesOrderHeader` 实时聚合得出。禁止把它与订单明细聚合值相加或互相校验为「应相等」。

## §3 指标口径（模拟条款）

- **§3.1 配额**：`CAST("SalesQuota" AS REAL) FROM "Sales.SalesPersonQuotaHistory"`，按 `(BusinessEntityID, QuotaDate)` 取。
- **§3.2 配额记录数**：`COUNT(*) FROM "Sales.SalesPersonQuotaHistory"`。
- **§3.3 配额达成率**：`CAST("SalesYTD" AS REAL) / NULLIF(CAST("SalesQuota" AS REAL), 0)`；分母为零返回 `NULL`。
- **§3.4 销售人员数**：`COUNT(DISTINCT "BusinessEntityID") FROM "Sales.SalesPerson"`。
- **§3.5 区域本年销售快照**：`CAST("SalesYTD" AS REAL) FROM "Sales.SalesTerritory"`。

## §4 多候选 Join 路径（评测要点）

**§4.1** 「查询某区域销售人员的配额达成率」至少存在两条可达路径：
1. `SalesPerson.TerritoryID → SalesTerritory.TerritoryID`，再按 `BusinessEntityID` 关联 `SalesPersonQuotaHistory`；
2. `SalesOrderHeader.SalesPersonID → SalesPerson.BusinessEntityID` 先聚合订单，再关联配额表。

两条路径语义不同（路径 2 用实时订单，路径 1 用快照），**不得由 LLM 静默选择**。本模拟制度要求返回时显式标注所用路径，若用户未指定则提示口径歧义。该场景被纳入 GraphRAG 开发集「多候选 Join 路径」类别。

## §5 待审核事项

| 编号 | 待审核内容 |
|---|---|
| R-P001-1 | `SalesYTD` 快照统计周期与重置规则 |
| R-P001-2 | 配额是否按季度累计 |
| R-P001-3 | 达成率是否应以订单实时聚合替代快照 |
