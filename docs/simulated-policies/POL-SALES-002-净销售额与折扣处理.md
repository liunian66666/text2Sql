# POL-SALES-002 净销售额、明细粒度与折扣处理规则

> **SIMULATED / 模拟企业制度** — 项目自行编写的演示条款，**不是微软官方业务规则**，**未经任何真实企业审核**。数据来自微软公开示例库 AdventureWorks OLTP（MIT），**不是真实生产企业数据**。状态 `UNREVIEWED`，引用指标一律 `reviewed=False` / `trusted=False`。

| 项 | 值 |
|---|---|
| 文档 ID | POL-SALES-002 |
| 版本 | 0.1-simulated |
| 状态 | 待人工审核 UNREVIEWED |
| 适用范围 | `Sales.SalesOrderDetail`、`Sales.SalesOrderHeader`、`Sales.SpecialOfferProduct` |
| 生效日期 | 模拟值 2022-01-01（无真实效力） |

## §2 字段与粒度

| 字段 | 真实表 | 类型 | 说明 |
|---|---|---|---|
| `SalesOrderDetailID` | `Sales.SalesOrderDetail` | INTEGER | 明细行主键 |
| `LineTotal` | `Sales.SalesOrderDetail` | `DECIMAL_TEXT(38,6)` | 明细行净额（TEXT 存储） |
| `UnitPrice` / `UnitPriceDiscount` | `Sales.SalesOrderDetail` | `DECIMAL_TEXT(19,4)` | 单价 / 折扣率 |
| `OrderQty` | `Sales.SalesOrderDetail` | INTEGER | 数量 |
| `SpecialOfferID` / `ProductID` | `Sales.SalesOrderDetail` | INTEGER | 复合外键两列 |

**§2.1 粒度声明**：`Sales.SalesOrderDetail` 为「订单明细行」粒度，`Sales.SalesOrderHeader` 为「订单头」粒度。二者为一对多关系（实测 `SalesOrderDetail.SalesOrderID` 非唯一、`SalesOrderHeader.SalesOrderID` 为主键）。

**§2.2 复合外键**：`Sales.SalesOrderDetail(SpecialOfferID, ProductID) → Sales.SpecialOfferProduct(SpecialOfferID, ProductID)`。该复合外键在 SQLite 移植版中保留（见 inspection.json）。关联特惠商品时**必须两列同时匹配**，仅用 `ProductID` 会造成行膨胀。

## §3 指标口径（模拟条款）

- **§3.1 明细净销售额**：`SUM(CAST("LineTotal" AS REAL)) FROM "Sales.SalesOrderDetail"`。
- **§3.2 销售件数**：`SUM("OrderQty") FROM "Sales.SalesOrderDetail"`。
- **§3.3 订单明细行数**：`COUNT("SalesOrderDetailID")`。
- **§3.4 折扣金额**：`SUM(CAST("UnitPrice" AS REAL) * "OrderQty") - SUM(CAST("LineTotal" AS REAL))`；两处均需显式 CAST。
- **§3.5 明细平均单价**：`SUM(CAST("LineTotal" AS REAL)) / SUM("OrderQty")`，分母为零返回 `NULL`。

## §4 重复聚合风险（重点）

**§4.1** 禁止把订单头金额（`SubTotal`、`TotalDue`）与订单明细表直接 JOIN 后求和：一个订单对应多条明细，会导致订单头金额被重复计入。若需同时使用两层金额，必须先在明细层按 `SalesOrderID` 聚合，再与订单头 1:1 关联。

**§4.2** 该风险与项目 Phase 1 中 `enterprise_sales.net_revenue` 的退款重复聚合风险同构，但**本库没有退款表**，不得移植该库的退款公式或表名字段。

**§4.3** 特惠商品关联（§2.2）同样是一对多潜在风险点：`SpecialOfferProduct` 对同一 `ProductID` 可存在多条 `SpecialOfferID` 记录，直接 JOIN 会放大明细行。

## §5 待审核事项

| 编号 | 待审核内容 |
|---|---|
| R-S002-1 | `LineTotal` 是否已含折扣、税、运费（示例库未给出业务字典） |
| R-S002-2 | §3.4 折扣金额公式与 `UnitPriceDiscount` 字段的关系 |
| R-S002-3 | 复合外键在业务上是否应限定 `SpecialOfferID` 取值范围 |
