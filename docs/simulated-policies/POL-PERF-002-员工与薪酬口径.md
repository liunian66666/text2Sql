# POL-PERF-002 员工在职、部门与薪酬口径

> **SIMULATED / 模拟企业制度** — 项目自行编写的演示条款，**不是微软官方业务规则**，**未经任何真实企业审核**。数据来自微软公开示例库 AdventureWorks OLTP（MIT），**不是真实生产企业数据**。状态 `UNREVIEWED`，引用指标一律 `reviewed=False` / `trusted=False`。

| 项 | 值 |
|---|---|
| 文档 ID | POL-PERF-002 |
| 版本 | 0.1-simulated |
| 状态 | 待人工审核 UNREVIEWED |
| 适用范围 | `HumanResources.Employee`、`HumanResources.EmployeePayHistory`、`HumanResources.Department` |
| 生效日期 | 模拟值 2022-01-01（无真实效力） |

## §2 字段依据与隐私边界

| 字段 | 真实表 | 类型 | 说明 |
|---|---|---|---|
| `BusinessEntityID` | `HumanResources.Employee` | INTEGER | 员工主键 |
| `CurrentFlag` | `HumanResources.Employee` | INTEGER | 在职标记（取值含义为模拟约定） |
| `HireDate` | `HumanResources.Employee` | TEXT | 入职日期 |
| `VacationHours` / `SickLeaveHours` | `HumanResources.Employee` | INTEGER | 假期小时 |
| `Rate` / `RateChangeDate` | `HumanResources.EmployeePayHistory` | `DECIMAL_TEXT(19,4)` / TEXT | **复合主键**，历史费率 |
| `DepartmentID` | `HumanResources.Department` | INTEGER | 部门主键 |

**§2.1 隐私边界**：`NationalIDNumber`、`LoginID`、`BirthDate`、`Gender`、`MaritalStatus` 等属于个人隐私字段，**禁止**写入向量库、指标定义、LLM Prompt 或日志。Phase 1 `SchemaExtractor` 只抽取元数据（表名/列名/类型），不导入任何原始数据行，本制度沿用该约束。

## §3 指标口径（模拟条款）

- **§3.1 在职员工数**：`COUNT(*) FROM "HumanResources.Employee" WHERE "CurrentFlag" = 1`。
- **§3.2 员工总数**：`COUNT(*) FROM "HumanResources.Employee"`。
- **§3.3 平均薪资费率**：`AVG(CAST("Rate" AS REAL)) FROM "HumanResources.EmployeePayHistory"`；这是**历史费率记录**的均值，不等于当前薪资。
- **§3.4 当前薪资费率**：按 `BusinessEntityID` 取 `RateChangeDate` 最大的记录；同日多条时须人工去重，不得随机取一条。
- **§3.5 部门数**：`COUNT("DepartmentID") FROM "HumanResources.Department"`。
- **§3.6 平均休假小时**：`AVG("VacationHours") FROM "HumanResources.Employee"`。

## §4 粒度与关联

**§4.1** `EmployeePayHistory` 为「员工 × 调薪日期」粒度，与 `Employee` 一对多。直接 JOIN 后对员工级字段求和会重复计数。

**§4.2** 员工与部门的关联必须经过 `HumanResources.EmployeeDepartmentHistory`（含 `StartDate` / `EndDate` / `ShiftID` 的复合主键）。这是后续 GraphRAG「必要中间表补全」评测的典型场景：**只召回 `Employee` 与 `Department` 两张表不足以正确关联**。

## §5 待审核事项

| 编号 | 待审核内容 |
|---|---|
| R-P002-1 | `CurrentFlag` 取值字典（示例库未给出业务说明） |
| R-P002-2 | 部门归属是否只取 `EndDate IS NULL` 的当前记录 |
| R-P002-3 | 薪资币种与 `PayFrequency` 换算规则 |
