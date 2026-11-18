# POL-REGION-001 销售区域归属与汇总口径

> **SIMULATED / 模拟企业制度** — 项目自行编写的演示条款，**不是微软官方业务规则**，**未经任何真实企业审核**。数据来自微软公开示例库 AdventureWorks OLTP（MIT），**不是真实生产企业数据**。状态 `UNREVIEWED`，引用指标一律 `reviewed=False` / `trusted=False`。

| 项 | 值 |
|---|---|
| 文档 ID | POL-REGION-001 |
| 版本 | 0.1-simulated |
| 状态 | 待人工审核 UNREVIEWED |
| 适用范围 | `Sales.SalesTerritory`、`Sales.SalesOrderHeader`、`Sales.Customer`、`Person.StateProvince` |
| 生效日期 | 模拟值 2022-01-01（无真实效力） |

## §2 字段依据

| 字段 | 真实表 | 类型 | 说明 |
|---|---|---|---|
| `TerritoryID` / `Name` / `Group` | `Sales.SalesTerritory` | INTEGER / TEXT / TEXT | 区域主键与分组 |
| `CountryRegionCode` | `Sales.SalesTerritory` | TEXT | 国家/地区代码 |
| `SalesYTD` / `CostYTD` | `Sales.SalesTerritory` | `DECIMAL_TEXT(19,4)` | 区域快照 |
| `TerritoryID` | `Sales.SalesOrderHeader` | INTEGER | 订单归属区域 |
| `TerritoryID` | `Sales.Customer` | INTEGER | 客户归属区域 |
| `TerritoryID` | `Person.StateProvince` | INTEGER | 省州归属区域 |

**§2.1 区域数**：实测 `Sales.SalesTerritory` 10 行；`Sales.SalesPerson` 17 行；`Sales.Customer` 19,820 行。

## §3 指标口径（模拟条款）

- **§3.1 销售区域数**：`COUNT("TerritoryID") FROM "Sales.SalesTerritory"`。
- **§3.2 区域销售额**：`SELECT "TerritoryID", SUM(CAST("SubTotal" AS REAL)) FROM "Sales.SalesOrderHeader" GROUP BY "TerritoryID"`；按 `Sales.SalesOrderHeader.TerritoryID` 分组汇总。
- **§3.3 区域订单数**：按同一字段分组 `COUNT(DISTINCT "SalesOrderID")`。
- **§3.4 区域销售额占比**：`SUM(CAST("SubTotal" AS REAL)) / NULLIF((SELECT SUM(CAST("SubTotal" AS REAL)) FROM "Sales.SalesOrderHeader"), 0)`，按 `TerritoryID` 分组；分母为零返回 `NULL`。

## §4 多候选 Join 路径（重点）

**§4.1** 「区域」在库中至少有三处可关联入口：订单头 `TerritoryID`、客户 `TerritoryID`、省州 `StateProvince.TerritoryID`。三者语义不同：
- 订单头 `TerritoryID` 表示**下单归属区域**；
- 客户 `TerritoryID` 表示**客户注册区域**；
- 省州 `TerritoryID` 表示**地理行政区划映射**。

**§4.2** 当问题为「某区域销售额」时，本模拟制度默认使用**订单头** `TerritoryID`；若用户明确说「客户所属区域」，须改用客户表路径并显式说明。**不得**静默混用，也不得同时 JOIN 两个 `TerritoryID` 而不加区分条件。

**§4.3** `SalesTerritory.SalesYTD` 为快照字段，与 §3.2 的订单聚合结果不要求相等，禁止用其做一致性校验。该场景进入 GraphRAG 开发集「多候选 Join 路径」类别。

## §5 待审核事项

| 编号 | 待审核内容 |
|---|---|
| R-R001-1 | 区域归属以订单、客户还是收货地址为准 |
| R-R001-2 | 跨区域调拨/转运订单的处理规则 |
| R-R001-3 | `Group` 字段的业务含义与分组上报口径 |
