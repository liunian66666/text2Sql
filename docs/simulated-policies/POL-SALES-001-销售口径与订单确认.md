# POL-SALES-001 销售口径与订单确认规则

> **SIMULATED / 模拟企业制度** — 本文件由「智能数据分析助手」项目为演示「多源企业知识库 → 结构化指标 → GraphRAG 检索」链路自行编写，**不是微软官方业务规则**，**未经任何真实企业审核**，**不得用于生产业务口径**。
> 底层数据来自微软公开示例数据库 AdventureWorks OLTP（MIT 许可），**不是真实生产企业数据**。
> 状态 `UNREVIEWED`。引用本文件的指标一律 `reviewed=False` / `trusted=False`。

| 项 | 值 |
|---|---|
| 文档 ID | POL-SALES-001 |
| 版本 | 0.1-simulated |
| 状态 | 待人工审核 UNREVIEWED |
| 适用范围 | `Sales.SalesOrderHeader`、`Sales.SalesOrderDetail` |
| 生效日期 | 模拟值 2022-01-01（无真实效力） |

## §2 字段与口径边界

| 字段 | 真实表 | SQLite 声明类型 | 说明 |
|---|---|---|---|
| `SalesOrderID` | `Sales.SalesOrderHeader` | INTEGER | 订单主键 |
| `SubTotal` | `Sales.SalesOrderHeader` | `DECIMAL_TEXT(19,4)` | 订单头小计，**实际以 TEXT 存储** |
| `TaxAmt` / `Freight` | `Sales.SalesOrderHeader` | `DECIMAL_TEXT(19,4)` | 税额 / 运费 |
| `TotalDue` | `Sales.SalesOrderHeader` | `DECIMAL_TEXT(38,4)` | 应收总额 |
| `OrderDate` | `Sales.SalesOrderHeader` | TEXT | `'YYYY-MM-DD HH:MM:SS.mmm'` |
| `Status` | `Sales.SalesOrderHeader` | INTEGER | 状态码 |

**§2.1 状态口径待审核**：示例库未提供 `Status` 取值字典。本模拟制度**不对状态码含义做任何业务假设**，所有销售额指标默认覆盖全部订单；若需按状态过滤，必须先由人工补充并审核状态字典，禁止 LLM 自行推断。

**§2.2 金额类型约束**：金额列在 SQLite 移植版中存为 TEXT。聚合/比较/排序必须 `CAST(... AS REAL)`，否则得到字典序或隐式转换结果。此事实由 `data/adventureworks/inspection.json` 实测确认。

## §3 指标口径（模拟条款）

- **§3.1 销售额（不含税运费）**：`SUM(CAST("SubTotal" AS REAL)) FROM "Sales.SalesOrderHeader"`。不得改用 `TotalDue`，后者含税与运费。
- **§3.2 订单数**：`COUNT(DISTINCT "SalesOrderID") FROM "Sales.SalesOrderHeader"`。禁止用订单明细行数代替订单数。
- **§3.3 平均订单金额**：`SUM(CAST("SubTotal" AS REAL)) / NULLIF(COUNT(DISTINCT "SalesOrderID"), 0) FROM "Sales.SalesOrderHeader"`；§3.1 结果 ÷ §3.2 结果，分母为零返回 `NULL`，不得返回 0。
- **§3.4 应收总额**：`SUM(CAST("TotalDue" AS REAL)) FROM "Sales.SalesOrderHeader"`。
- **§3.5 税额**：`SUM(CAST("TaxAmt" AS REAL)) FROM "Sales.SalesOrderHeader"`。
- **§3.6 运费**：`SUM(CAST("Freight" AS REAL)) FROM "Sales.SalesOrderHeader"`。

## §4 时间口径

**§4.1** 统计日期以订单头 `OrderDate` 为准，取 `substr("OrderDate", 1, 10)` 得到 `YYYY-MM-DD`；使用 `DueDate`、`ShipDate` 或 `ModifiedDate` 均视为口径错误。

**§4.2** 跨期比较须显式声明区间边界（含首不含尾），未声明时不得推断"本季度/上月"等相对时间。

## §5 待审核事项

| 编号 | 待审核内容 |
|---|---|
| R-S001-1 | `Status` 状态字典及有效订单过滤条件 |
| R-S001-2 | 销售额是否应剔除取消/退货订单 |
| R-S001-3 | 跨币种订单是否统一换算（见 POL-REGION-002） |
