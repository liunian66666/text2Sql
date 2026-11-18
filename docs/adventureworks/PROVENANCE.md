# AdventureWorks OLTP SQLite 移植版：来源、许可与迁移保真度核查

> **性质声明**：AdventureWorks 是微软公开发布的**示例数据库**（sample database），
> 最初用于演示 SQL Server 2008 数据库设计，**不是真实生产企业数据**。
> 本项目 `docs/simulated-policies/` 下的业务制度均为 **SIMULATED / UNREVIEWED 模拟条款**，
> **不是微软官方业务规则**，不得冒充官方口径，也不得用于生产业务决策。

## 1. 来源与许可

| 项 | 值 |
|---|---|
| 官方上游 | `https://github.com/microsoft/sql-server-samples/tree/master/samples/databases/adventure-works` |
| 官方源脚本 | `samples/databases/adventure-works/oltp-install-script/instawdb.sql` |
| 固定 commit | `beaab06ef72831089ca80e5355d65e661fd19b26` |
| 源版本标识 | SQL Server 2025 sample build `17.0.1000.3`（来自 `AWBuildVersion.csv`） |
| 许可 | **MIT**（官方仓库 README「License」章节 + 根目录 `license.txt`，`Copyright (c) Microsoft Corporation`） |
| 许可文件 | 已保存 `docs/adventureworks/LICENSE-microsoft-sql-server-samples.txt`，SHA256 `201f3705979e932c92cd3c6ddbddf4dc3c71dcc5a82089a1e4018227cfe1cf09`，与研究包 manifest 中记录的官方 LICENSE 哈希一致 |
| SQLite 移植方 | `https://adventureworks.demodb.dev/downloads/`（第三方移植，页面声明数据来自上述官方仓库并按 MIT 分发、要求保留许可与归属声明） |
| 移植契约 | `data/adventureworks/provider-schema.json`（`contractVersion=1`，含 71 表定义、20 个源视图说明、source revision） |

官方 README 明确：AdventureWorks 是 "sample databases that were originally published by Microsoft
to show how to design a SQL Server database"，且 SQL Server 2025 版本仅调整了日期等配置，
2012 之后无重大结构变化。

## 2. 下载与文件哈希（已实测校验）

SQLite 库通过两个 gzip 分片下载并拼接：

| 文件 | 字节 | SHA256 |
|---|---|---|
| `adventureworks.sqlite.gz.*.part-1` | 26,214,400 | `60e1d23ec00f1938e39262cd2e30a85a29aada525def56a65960312e1a9c5ac6`（provider 声明） |
| `adventureworks.sqlite.gz.*.part-2` | 9,153,059 | `899a20c972a8f6fdd25b1544a29804c4872b877061dbaa1d6a85478445071e10`（provider 声明） |
| 拼接后 `adventureworks.sqlite.gz` | 35,367,459 | **`5516328f621cbbe21083a20f325bc1a6f841e6f709bb6d8bebfebbf48b81d203`**（本地实测，与 provider 一致） |
| 解压后 `adventureworks.sqlite` | **126,820,352** | **`6a105e1982becfe003fc7a307d7cad9d738390cedd79d166c2167fd817168109`**（本地实测，与 provider `uncompressedSha256` 一致） |

伴随保存的来源元数据：`adventureworks.sqlite.parts.json`、`provider-schema.json`、
`research-manifest.json`、`inspection.json`、`column_inventory.json`、
`query_evidence.json`、`test_split_evidence.json`、`LICENSE-microsoft-sql-server-samples.txt`。
`data/` 目录被 `.gitignore` 忽略，二进制库不进入版本控制；可跟踪的来源与许可声明放在本目录。

## 3. 真实结构统计（PRAGMA 实测）

| 项 | 值 |
|---|---|
| 物理表 | **71**（前缀分布：HumanResources 6 / Person 13 / Production 25 / Purchasing 5 / Sales 19 / dbo 3） |
| 视图 | 11（源 SQL Server 20 个视图中仅 11 个可移植到 SQLite） |
| 索引 | 122（其中 UNIQUE 索引 86） |
| 列 | **486** |
| 数据行 | **759,240**（与 provider 契约声明完全一致） |
| 有主键的表 | 71 / 71 |
| 复合主键 | **23**（如 `EmployeeDepartmentHistory(BusinessEntityID,StartDate,DepartmentID,ShiftID)`、`ProductCostHistory(ProductID,StartDate)`） |
| 外键约束组 | **90**，两阶段校验 **0 条悬空引用** |
| 复合外键 | **1**：`Sales.SalesOrderDetail(SpecialOfferID, ProductID) → Sales.SpecialOfferProduct(SpecialOfferID, ProductID)` |
| 空表 | `dbo.DatabaseLog`、`dbo.ErrorLog` |
| 最大表 | `Sales.SalesOrderDetail` 121,317 行；`Production.TransactionHistory` 113,443 行 |

**provider 声明 91 个 FK、本地 90 组的差异已查明，不是丢失**：provider 的 `foreignKeys`
按「列条目」计数，复合外键的 2 列记为 2 条；SQLite `PRAGMA foreign_key_list` 按约束 `id`
分组，该复合外键是 1 组（2 行）。`91 = 90 组 + 1 个额外的复合列`，两边一致。

## 4. SQLite 迁移保真度核查（关键结论）

| 检查项 | 结论 | 影响 |
|---|---|---|
| 带 SQL Server Schema 前缀的表名 | **71/71 保留点号原名**，如 `Sales.SalesOrderHeader` | SQL 中必须写 `FROM "Sales.SalesOrderHeader"`；实测裸写 `FROM Sales.SalesOrderHeader` 报 `no such table`。已在 `app/enterprise/adventureworks.py:SQL_DIALECT_HINT` 中作为 evidence 约束注入 |
| 主键 / 复合主键 | 全部保留，顺序按 `pk` 序号 | 已用于 Phase 1 `SchemaExtractor` 的稳定 ID |
| 外键 / 复合外键 | 全部保留，顺序按 `(id, seq)`；0 悬空 | 复合外键已纳入 GraphRAG 评测集（单列关联膨胀 296,983 vs 正确 121,317） |
| 唯一约束 | 86 个 UNIQUE 索引保留 | 用于基数推断（如 `payments` 类 1:1 关系） |
| **金额/成本类型** | 源 `DECIMAL(19,4)`/`DECIMAL(38,6)` 等 → SQLite 声明 `DECIMAL_TEXT(19,4)` 等 **48 列**，实测 `typeof()` 全部为 **`text`** | 聚合/比较/排序必须 `CAST(... AS REAL)`；否则字典序或隐式转换。例：`SubTotal` 首行 `'20565.6206'` |
| **日期类型** | 源 `datetime` → SQLite `TEXT`，值形如 `'2022-05-30 00:00:00.000'` | 时间过滤用 `substr(col,1,10)` 或 `date(col)`，不做隐式 datetime 转换 |
| BLOB 列 | 7 列（如 `OrganizationNode`、`SpatialLocation`、`Document`） | 知识层只抽元数据，不导入原始 BLOB |
| 原始 SQL Server 类型元数据 | **未随 SQLite 文件保留**（provider 的 `tableSql` 是转换后的 SQLite DDL） | 源类型精确精度无法从库内静态还原，只能凭 provider 契约与 `DECIMAL_TEXT(p,s)` 声明推断；已记为限制 |
| SQL Server 视图 | 20 → 11 可移植（XML / APPLY 等不兼容） | 依赖源视图的业务逻辑不可假定存在 |
| `PRAGMA foreign_keys` | 连接默认 **0（关闭）** | 只读分析不受影响；知识层只把 FK 当结构事实，不依赖运行时约束强制 |

**未验证/待人工确认**：源 T-SQL 的完整 CHECK 约束、默认值、触发器、计算列、XML Schema、
`hierarchyid`/`geography` 语义均未在 SQLite 版中核查；本项目不把它们当作业务规则使用。
所有业务口径来自模拟制度文档并标记 `UNREVIEWED`。

## 5. 接入方式与原有资产保护

- 新库以 `database_id="adventureworks"` 注册为**独立只读数据源**，路径
  `data/adventureworks/adventureworks.sqlite`。`app/web/registry.py` 中通过
  `ENABLE_ADVENTUREWORKS` 环境变量**显式启用，默认关闭**——因为该库的值向量索引需要在线
  embedding 模型（DashScope），未建索引时启动自检会报告 `value_index_ready=false`。
- **未删除、未覆盖、未重写** `data/enterprise_sales/`、`data/enterprise_hr/`、`data/bird/`、
  `evals/metric-rag/`、`evals/bird-dev-50/`、`evals/agent/` 及任何既有评测结果。
- `app/enterprise/business_metrics.json`（13 条指标）保持原样；AdventureWorks 的 38 条指标
  放在**独立文件** `app/enterprise/adventureworks_metrics.json`，避免污染现有 15 题
  Recall@3 / MRR 评测（该评测的 `score()` 不做 database 过滤）。
- 只读沙箱 `app/skills/sql_skill.py:execute_readonly_table` 与上游 DeepEye-SQL
  `app/pipeline/`、`app/db_utils/`、`app/prompt/` **均未修改**。

## 6. 复现命令

```bash
# 知识构建（离线、无需模型）
python scripts/build_aw_metrics.py
python -m app.cli build-knowledge --database adventureworks \
  --metrics app/enterprise/adventureworks_metrics.json \
  --output workspace/knowledge/adventureworks

# 原有两库的默认构建保持完全不变
python -m app.cli build-knowledge

# 全量离线回归
python -m unittest discover -s tests -v
```

## 7. 参考

- 官方仓库 README（含 License 章节）：`https://github.com/microsoft/sql-server-samples`
- 移植方下载页：`https://adventureworks.demodb.dev/downloads/`
- 本项目来源核查脚本产物：`data/adventureworks/inspection.json`
- 交付报告：`docs/数据资产建设-AdventureWorks接入报告.md`
