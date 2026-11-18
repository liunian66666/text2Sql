# POL-COST-001 标准成本、目录价与毛利口径

> **SIMULATED / 模拟企业制度** — 项目自行编写的演示条款，**不是微软官方业务规则**，**未经任何真实企业审核**。数据来自微软公开示例库 AdventureWorks OLTP（MIT），**不是真实生产企业数据**。状态 `UNREVIEWED`，引用指标一律 `reviewed=False` / `trusted=False`。

| 项 | 值 |
|---|---|
| 文档 ID | POL-COST-001 |
| 版本 | 0.1-simulated |
| 状态 | 待人工审核 UNREVIEWED |
| 适用范围 | `Production.Product`、`Production.ProductSubcategory`、`Production.ProductCategory` |
| 生效日期 | 模拟值 2022-01-01（无真实效力） |

## §2 字段依据

| 字段 | 真实表 | 类型 | 说明 |
|---|---|---|---|
| `ProductID` | `Production.Product` | INTEGER | 商品主键 |
| `StandardCost` | `Production.Product` | `DECIMAL_TEXT(19,4)` | 当前标准成本（TEXT 存储） |
| `ListPrice` | `Production.Product` | `DECIMAL_TEXT(19,4)` | 当前目录价（TEXT 存储） |
| `FinishedGoodsFlag` / `SellEndDate` | `Production.Product` | INTEGER / TEXT | 在售判定字段 |
| `ProductSubcategoryID` | `Production.Product` | INTEGER | 可空，指向子类别 |

**§2.1** `StandardCost` 与 `ListPrice` 是商品主数据上的**当前快照值**，不是按订单发生时刻的历史成本。历史成本匹配见 POL-COST-002。

**§2.2** `ProductSubcategoryID` 允许为 NULL，因此「商品 → 子类别 → 类别」路径存在断链；统计类别指标时须显式处理 NULL，不得静默丢弃或臆造类别。

## §3 指标口径（模拟条款）

- **§3.1 当前标准成本**：`CAST("StandardCost" AS REAL)`，按 `ProductID` 取值。
- **§3.2 当前目录价**：`CAST("ListPrice" AS REAL)`。
- **§3.3 单位毛利**：`CAST("ListPrice" AS REAL) - CAST("StandardCost" AS REAL)`。
- **§3.4 毛利率**：`(CAST("ListPrice" AS REAL) - CAST("StandardCost" AS REAL)) / NULLIF(CAST("ListPrice" AS REAL), 0)`；分母为零或 `ListPrice` 为 0 时返回 `NULL`。
- **§3.5 商品平均毛利率**：§3.4 在商品集合上的平均值；不得用 `SUM(毛利)/SUM(目录价)` 代替，除非人工审核确认加权口径。

## §4 字段真实性约束

**§4.1** 本库**不存在**「实际成交成本」字段，`Production.TransactionHistory.ActualCost` 记录的是库存事务成本，不得直接当作销售成本使用。

**§4.2** 任何指标公式中引用的表名/列名必须通过 `SchemaExtractor` 对真实 SQLite 校验；校验失败的指标不得入库为可信规则（见 POL-GOV-001 §3）。

## §5 待审核事项

| 编号 | 待审核内容 |
|---|---|
| R-C001-1 | 毛利率分母口径与零值处理 |
| R-C001-2 | `StandardCost` 为 0 的商品是否纳入统计（实测存在 `.0000` 值） |
| R-C001-3 | 是否按类别加权汇总毛利率 |
