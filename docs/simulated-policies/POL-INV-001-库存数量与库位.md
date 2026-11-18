# POL-INV-001 库存数量、库位与事务口径

> **SIMULATED / 模拟企业制度** — 项目自行编写的演示条款，**不是微软官方业务规则**，**未经任何真实企业审核**。数据来自微软公开示例库 AdventureWorks OLTP（MIT），**不是真实生产企业数据**。状态 `UNREVIEWED`，引用指标一律 `reviewed=False` / `trusted=False`。

| 项 | 值 |
|---|---|
| 文档 ID | POL-INV-001 |
| 版本 | 0.1-simulated |
| 状态 | 待人工审核 UNREVIEWED |
| 适用范围 | `Production.ProductInventory`、`Production.Location`、`Production.TransactionHistory` |
| 生效日期 | 模拟值 2022-01-01（无真实效力） |

## §2 字段依据

| 字段 | 真实表 | 类型 | 说明 |
|---|---|---|---|
| `ProductID` / `LocationID` | `Production.ProductInventory` | INTEGER | **复合主键** |
| `Quantity` | `Production.ProductInventory` | INTEGER | 该库位库存数量 |
| `Shelf` / `Bin` | `Production.ProductInventory` | TEXT / INTEGER | 库位明细 |
| `CostRate` / `Availability` | `Production.Location` | `DECIMAL_TEXT(10,4)` / `DECIMAL_TEXT(8,2)` | 库位成本率/可用量 |
| `ActualCost` / `TransactionDate` | `Production.TransactionHistory` | `DECIMAL_TEXT(19,4)` / TEXT | 库存事务成本与日期 |

实测：`ProductInventory` 1,069 行（复合主键 `(ProductID, LocationID)`），`Location` 14 行，`TransactionHistory` 113,443 行，`TransactionHistoryArchive` 89,253 行。

## §3 指标口径（模拟条款）

- **§3.1 库存总量**：`SUM("Quantity") FROM "Production.ProductInventory"`。
- **§3.2 商品库存量**：`SELECT "ProductID", SUM("Quantity") FROM "Production.ProductInventory" GROUP BY "ProductID"`；按 `ProductID` 分组，**不得**取单行 `Quantity`，因为同一商品分布在多个库位。
- **§3.3 库位数**：`COUNT("LocationID") FROM "Production.Location"`。
- **§3.4 库存事务数**：`COUNT("TransactionID") FROM "Production.TransactionHistory"`。
- **§3.5 事务实际成本**：`SUM(CAST("ActualCost" AS REAL)) FROM "Production.TransactionHistory"`。

## §4 粒度冲突

**§4.1** `ProductInventory` 为「商品 × 库位」粒度，`Production.Product` 为「商品」粒度。若把商品级 `StandardCost` 或 `ReorderPoint` 直接与库存行 JOIN 后求和，会产生重复计数；必须先按 `ProductID` 聚合库存，再与商品表 1:1 关联。

**§4.2** `TransactionHistory` 与 `TransactionHistoryArchive` 是同期与归档两套事务表，字段结构相近但**不可无条件 UNION**；本模拟制度默认只统计当期表，是否合并需人工审核。

**§4.3** 上述粒度冲突进入 GraphRAG 开发集「粒度冲突」类别，用于验证 SchemaContext 是否能携带复合主键与聚合建议。

## §5 待审核事项

| 编号 | 待审核内容 |
|---|---|
| R-I001-1 | 是否合并归档事务表 |
| R-I001-2 | `Quantity` 是否含在途/冻结库存 |
| R-I001-3 | `ActualCost` 与标准成本的差异处理 |
