# POL-INV-002 再订货点、工单与报废规则

> **SIMULATED / 模拟企业制度** — 项目自行编写的演示条款，**不是微软官方业务规则**，**未经任何真实企业审核**。数据来自微软公开示例库 AdventureWorks OLTP（MIT），**不是真实生产企业数据**。状态 `UNREVIEWED`，引用指标一律 `reviewed=False` / `trusted=False`。

| 项 | 值 |
|---|---|
| 文档 ID | POL-INV-002 |
| 版本 | 0.1-simulated |
| 状态 | 待审核 UNREVIEWED |
| 适用范围 | `Production.Product`、`Production.ProductInventory`、`Production.WorkOrder`、`Production.ScrapReason` |
| 生效日期 | 模拟值 2022-01-01（无真实效力） |

## §2 字段依据

| 字段 | 真实表 | 类型 | 说明 |
|---|---|---|---|
| `SafetyStockLevel` / `ReorderPoint` | `Production.Product` | INTEGER | 安全库存 / 再订货点（商品级） |
| `Quantity` | `Production.ProductInventory` | INTEGER | 库位级库存 |
| `WorkOrderID` / `OrderQty` / `ScrappedQty` | `Production.WorkOrder` | INTEGER | 工单与报废数量 |
| `StartDate` / `EndDate` | `Production.WorkOrder` | TEXT | 工单区间 |
| `ScrapReasonID` | `Production.WorkOrder` | INTEGER | 报废原因外键 |

实测：`WorkOrder` 72,591 行，`WorkOrderRouting` 67,131 行，`ScrapReason` 16 行。

## §3 指标口径（模拟条款）

- **§3.1 低于再订货点商品数**：`SELECT COUNT(*) FROM (SELECT "ProductID", SUM("Quantity") AS qty FROM "Production.ProductInventory" GROUP BY "ProductID") inv JOIN "Production.Product" p ON inv."ProductID" = p."ProductID" WHERE inv.qty < p."ReorderPoint"`；先按 `ProductID` 汇总，再与 `Production.Product.ReorderPoint` 比较。
- **§3.2 工单数**：`COUNT("WorkOrderID") FROM "Production.WorkOrder"`。
- **§3.3 工单计划产量**：`SUM("OrderQty") FROM "Production.WorkOrder"`。
- **§3.4 报废数量**：`SUM("ScrappedQty") FROM "Production.WorkOrder"`。
- **§3.5 报废率**：`SUM("ScrappedQty") / NULLIF(SUM("OrderQty"), 0) FROM "Production.WorkOrder"`；分母为零返回 `NULL`。
- **§3.6 安全库存缺口**：`MAX(0, "ReorderPoint" - SUM("Quantity"))`（按商品聚合后计算）。

## §4 聚合层级约束

**§4.1** §3.1 **必须**先按商品聚合再比较。直接用 `ProductInventory.Quantity < Product.ReorderPoint` 逐库位比较会低估/高估（同一商品分布在多个库位），是本轮「粒度冲突」评测的关键判分点。

**§4.2** `WorkOrder` 与 `WorkOrderRouting` 是一对多（工序明细），把工单级 `OrderQty` 与工序明细直接 JOIN 后求和会重复计算。

## §5 待审核事项

| 编号 | 待审核内容 |
|---|---|
| R-I002-1 | 再订货点比较应含在途量还是仅现有库存 |
| R-I002-2 | 报废率是否按工单加权 |
| R-I002-3 | 工单状态字段缺失（本库无 `Status` 于 WorkOrder），是否需过滤未完成工单 |
