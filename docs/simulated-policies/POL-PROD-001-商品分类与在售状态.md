# POL-PROD-001 商品分类路径与在售状态口径

> **SIMULATED / 模拟企业制度** — 项目自行编写的演示条款，**不是微软官方业务规则**，**未经任何真实企业审核**。数据来自微软公开示例库 AdventureWorks OLTP（MIT），**不是真实生产企业数据**。状态 `UNREVIEWED`，引用指标一律 `reviewed=False` / `trusted=False`。

| 项 | 值 |
|---|---|
| 文档 ID | POL-PROD-001 |
| 版本 | 0.1-simulated |
| 状态 | 待人工审核 UNREVIEWED |
| 适用范围 | `Production.Product`、`Production.ProductSubcategory`、`Production.ProductCategory`、`Production.ProductListPriceHistory` |
| 生效日期 | 模拟值 2022-01-01（无真实效力） |

## §2 分类路径与中间表

| 表 | 主键 | 关联字段 |
|---|---|---|
| `Production.Product` | `ProductID` | `ProductSubcategoryID`（可空） |
| `Production.ProductSubcategory` | `ProductSubcategoryID` | `ProductCategoryID` |
| `Production.ProductCategory` | `ProductCategoryID` | — |

实测：`Product` 504 行，`ProductSubcategory` 37 行，`ProductCategory` 4 行，`ProductListPriceHistory` 395 行。

**§2.1 必要中间表**：从「商品」到「商品类别」**必须经过 `ProductSubcategory`**。若 Schema 检索只召回 `Product` 与 `ProductCategory` 两张表，则不存在直接关联字段，属于典型的「必要中间表遗漏」，是后续 GraphRAG 子图检索的核心评测场景。

**§2.2** `Product.ProductSubcategoryID` 可空，部分商品无子类别；按类别汇总时须显式标注未分类商品数量，不得静默丢弃。

## §3 指标口径（模拟条款）

- **§3.1 商品数**：`COUNT("ProductID") FROM "Production.Product"`。
- **§3.2 在售商品数**：`COUNT(*) WHERE "FinishedGoodsFlag" = 1 AND "SellEndDate" IS NULL`。
- **§3.3 子类别数**：`COUNT("ProductSubcategoryID") FROM "Production.ProductSubcategory"`。
- **§3.4 商品类别数**：`COUNT("ProductCategoryID") FROM "Production.ProductCategory"`。
- **§3.5 价格变更记录数**：`COUNT(*) FROM "Production.ProductListPriceHistory"`。
- **§3.6 当前目录价**：`CAST("ListPrice" AS REAL) FROM "Production.Product"`（见 POL-COST-001 §3.2）。

## §4 版本与粒度

**§4.1** `ProductListPriceHistory` 为「商品 × 生效区间」粒度，`Product.ListPrice` 为商品级当前值；二者不可直接相加或互相覆盖。

**§4.2** 商品分类字段在示例库中无版本时间戳，本模拟制度不假设分类历史可变。

## §5 待审核事项

| 编号 | 待审核内容 |
|---|---|
| R-PROD-1 | 在售判定是否还需 `SellStartDate <= 当日` |
| R-PROD-2 | 未分类商品是否单列「未分类」类别 |
| R-PROD-3 | `DiscontinuedDate` 与 `SellEndDate` 的优先级 |
