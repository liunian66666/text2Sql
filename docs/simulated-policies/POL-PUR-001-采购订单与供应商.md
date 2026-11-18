# POL-PUR-001 采购订单、收货与供应商准入规则

> **SIMULATED / 模拟企业制度** — 项目自行编写的演示条款，**不是微软官方业务规则**，**未经任何真实企业审核**。数据来自微软公开示例库 AdventureWorks OLTP（MIT），**不是真实生产企业数据**。状态 `UNREVIEWED`，引用指标一律 `reviewed=False` / `trusted=False`。

| 项 | 值 |
|---|---|
| 文档 ID | POL-PUR-001 |
| 版本 | 0.1-simulated |
| 状态 | 待人工审核 UNREVIEWED |
| 适用范围 | `Purchasing.PurchaseOrderHeader`、`Purchasing.PurchaseOrderDetail`、`Purchasing.ProductVendor`、`Purchasing.Vendor` |
| 生效日期 | 模拟值 2022-01-01（无真实效力） |

## §2 字段依据

| 字段 | 真实表 | 类型 | 说明 |
|---|---|---|---|
| `PurchaseOrderID` / `SubTotal` / `TotalDue` | `Purchasing.PurchaseOrderHeader` | INTEGER / `DECIMAL_TEXT` | 采购订单头 |
| `PurchaseOrderID` / `OrderQty` / `LineTotal` | `Purchasing.PurchaseOrderDetail` | INTEGER / `DECIMAL_TEXT` | 采购明细 |
| `ReceivedQty` / `RejectedQty` / `StockedQty` | `Purchasing.PurchaseOrderDetail` | `DECIMAL_TEXT` | 收货/拒收/入库数量（TEXT 存储） |
| `AverageLeadTime` / `StandardPrice` / `LastReceiptCost` | `Purchasing.ProductVendor` | INTEGER / `DECIMAL_TEXT` | 商品-供应商维度 |
| `ActiveFlag` / `CreditRating` | `Purchasing.Vendor` | INTEGER | 供应商状态与评级 |

实测：`PurchaseOrderHeader` 4,012 行，`PurchaseOrderDetail` 8,845 行，`ProductVendor` 460 行，`Vendor` 104 行。

## §3 指标口径（模拟条款）

- **§3.1 采购订单数**：`COUNT(DISTINCT "PurchaseOrderID") FROM "Purchasing.PurchaseOrderHeader"`。
- **§3.2 采购应付总额**：`SUM(CAST("TotalDue" AS REAL)) FROM "Purchasing.PurchaseOrderHeader"`。
- **§3.3 采购件数**：`SUM("OrderQty") FROM "Purchasing.PurchaseOrderDetail"`。
- **§3.4 收货数量**：`SUM(CAST("ReceivedQty" AS REAL)) FROM "Purchasing.PurchaseOrderDetail"`。
- **§3.5 活跃供应商数**：`COUNT(*) FROM "Purchasing.Vendor" WHERE "ActiveFlag" = 1`。
- **§3.6 平均交付提前期**：`AVG("AverageLeadTime") FROM "Purchasing.ProductVendor"`；这是「商品-供应商对」的均值，不是供应商级均值。

## §4 关联与粒度

**§4.1** `PurchaseOrderDetail.PurchaseOrderID → PurchaseOrderHeader.PurchaseOrderID` 为多对一；采购头金额与明细行直接 JOIN 后求和会重复计算，与 POL-SALES-002 §4.1 同构。

**§4.2** `ProductVendor` 主键为 `(ProductID, BusinessEntityID)`，关联供应商需经 `BusinessEntityID`，且 `Vendor.BusinessEntityID` 同样来自 `Person.BusinessEntity`。这是「必要中间表补全」之外的多跳路径示例。

**§4.3** `ReceivedQty` / `RejectedQty` / `StockedQty` 均为 TEXT 存储，求和必须 `CAST(... AS REAL)`。

## §5 待审核事项

| 编号 | 待审核内容 |
|---|---|
| R-PUR-1 | `Status` 取值字典与「已完结采购单」过滤条件 |
| R-PUR-2 | `ActiveFlag` 是否为唯一准入条件 |
| R-PUR-3 | 提前期是否按订单量加权 |
