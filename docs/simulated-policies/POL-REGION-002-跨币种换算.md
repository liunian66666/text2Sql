# POL-REGION-002 跨币种换算与汇率口径

> **SIMULATED / 模拟企业制度** — 项目自行编写的演示条款，**不是微软官方业务规则**，**未经任何真实企业审核**。数据来自微软公开示例库 AdventureWorks OLTP（MIT），**不是真实生产企业数据**。状态 `UNREVIEWED`，引用指标一律 `reviewed=False` / `trusted=False`。

| 项 | 值 |
|---|---|
| 文档 ID | POL-REGION-002 |
| 版本 | 0.1-simulated |
| 状态 | 待人工审核 UNREVIEWED |
| 适用范围 | `Sales.CurrencyRate`、`Sales.Currency`、`Sales.SalesOrderHeader` |
| 生效日期 | 模拟值 2022-01-01（无真实效力） |

## §2 字段依据

| 字段 | 真实表 | 类型 | 说明 |
|---|---|---|---|
| `CurrencyRateID` / `CurrencyRateDate` | `Sales.CurrencyRate` | INTEGER / TEXT | 汇率记录主键与日期 |
| `FromCurrencyCode` / `ToCurrencyCode` | `Sales.CurrencyRate` | TEXT | 源/目标币种 |
| `AverageRate` / `EndOfDayRate` | `Sales.CurrencyRate` | `DECIMAL_TEXT(19,4)` | 平均/收盘汇率（TEXT 存储） |
| `CurrencyRateID` | `Sales.SalesOrderHeader` | INTEGER | 可空，订单汇率引用 |

实测：`Sales.CurrencyRate` 13,532 行，`Sales.Currency` 105 行，`Sales.CountryRegionCurrency` 109 行。

## §3 换算规则（模拟条款，待审核）

- **§3.1** 订单金额换算：`CAST("SubTotal" AS REAL) * CAST("EndOfDayRate" AS REAL)`，通过 `CurrencyRateID` 关联。
- **§3.2** `CurrencyRateID IS NULL` 表示本币订单，不做换算，金额保持原值；**不得**用 `AverageRate` 替代或填充。
- **§3.3** 汇率日期与订单日期的匹配口径未在本示例中定义，本模拟制度**不假设**二者对齐。
- **§3.4** 汇率记录数：`COUNT(*) FROM "Sales.CurrencyRate"`；币种数：`COUNT(*) FROM "Sales.Currency"`。

## §4 风险与一致性

**§4.1** 汇率表与订单表按 `CurrencyRateID` 关联，若误按 `CurrencyRateDate` 与 `OrderDate` 关联会产生多对多放大，导致金额重复计算。

**§4.2** 跨币种汇总必须先逐单换算再求和，不能先求和再乘单一汇率；该规则属于**半可加指标**约束，与项目 Phase 0 报告 §5.3 的「可加/半可加/不可加」分类一致。

**§4.3** 由于示例库未提供官方汇率使用说明，本文件所有换算公式均为**模拟待审核假设**，禁止作为可信 SQL 约束。

## §5 待审核事项

| 编号 | 待审核内容 |
|---|---|
| R-R002-1 | 使用 `AverageRate` 还是 `EndOfDayRate` |
| R-R002-2 | 汇率日期与订单日期的对齐规则 |
| R-R002-3 | 本币判定是否确实可用 `CurrencyRateID IS NULL` |
