# POL-COST-002 历史成本区间匹配规则

> **SIMULATED / 模拟企业制度** — 项目自行编写的演示条款，**不是微软官方业务规则**，**未经任何真实企业审核**。数据来自微软公开示例库 AdventureWorks OLTP（MIT），**不是真实生产企业数据**。状态 `UNREVIEWED`，引用指标一律 `reviewed=False` / `trusted=False`。

| 项 | 值 |
|---|---|
| 文档 ID | POL-COST-002 |
| 版本 | 0.1-simulated |
| 状态 | 待人工审核 UNREVIEWED |
| 适用范围 | `Production.ProductCostHistory`、`Production.ProductListPriceHistory` |
| 生效日期 | 模拟值 2022-01-01（无真实效力） |

## §2 字段依据与真实基数

| 字段 | 真实表 | 类型 | 说明 |
|---|---|---|---|
| `ProductID` / `StartDate` | `Production.ProductCostHistory` | INTEGER / TEXT | **复合主键** |
| `EndDate` | `Production.ProductCostHistory` | TEXT | 区间上界，可空 |
| `StandardCost` | `Production.ProductCostHistory` | `DECIMAL_TEXT(19,4)` | 该区间标准成本 |
| `StartDate` / `ListPrice` | `Production.ProductListPriceHistory` | TEXT / `DECIMAL_TEXT(19,4)` | 历史目录价 |

实测（inspection.json）：`Production.ProductCostHistory` 395 行、复合主键 `(ProductID, StartDate)`；`Production.ProductListPriceHistory` 395 行。**同一 `ProductID` 存在多条成本记录**，这是历史成本匹配类问题的来源。

## §3 匹配规则（模拟条款，待审核）

- **§3.1 区间匹配**：对事件日期 `d`，成本记录适用条件为 `StartDate <= d AND (EndDate IS NULL OR d < EndDate)`。
- **§3.2 边界约定**：下界含、上界不含；`EndDate IS NULL` 表示当前有效区间。此边界为**模拟约定**，示例库未给出业务说明。
- **§3.3 最邻近回退**：若无区间命中，取 `StartDate <= d` 的最大 `StartDate` 记录；仍无则结果为 `NULL`，不得回退到 `Production.Product.StandardCost`（两者口径不同）。
- **§3.4 历史目录价**：`Production.ProductListPriceHistory` 采用与 §3.1 相同的区间规则，但**不得**与成本表按 `ProductID` 直接 JOIN（会产生笛卡尔积）。
- **§3.5 历史标准成本取值**：`SELECT CAST("StandardCost" AS REAL) FROM "Production.ProductCostHistory" WHERE "ProductID" = :product AND "StartDate" <= :event AND ("EndDate" IS NULL OR :event < "EndDate")`。

## §4 粒度冲突与评测价值

**§4.1** 「订单明细 × 历史成本」是典型粒度冲突：订单明细为行粒度，成本历史为「商品 × 时间段」粒度。直接 JOIN 会把同一明细行复制成多条，导致销售额/成本重复计算。

**§4.2** 该类问题被纳入 `evals/adventureworks-graphrag/` 开发集的「历史成本匹配」类别，用于后续 GraphRAG 验证「是否能召回必要的区间约束与中间表」。

## §5 待审核事项

| 编号 | 待审核内容 |
|---|---|
| R-C002-1 | 区间边界是否含 `EndDate` |
| R-C002-2 | 无命中时是否允许回退到当前 `StandardCost` |
| R-C002-3 | 成本币种与订单币种是否一致（见 POL-REGION-002） |
