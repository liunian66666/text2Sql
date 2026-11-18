# 数据资产建设：AdventureWorks OLTP 接入与知识资产落地

> 本轮为**数据资产建设阶段**（Phase 1 之后的增量），不是完整 GraphRAG 在线检索阶段。
> 依据：`docs/Phase0-架构基线分析.md`、`docs/Phase1-交付报告.md`、`goals/phase0-rag-graphrag/GOAL.md`。
>
> **性质声明**：AdventureWorks 是微软公开发布的**示例数据库**（MIT），**不是真实生产企业数据**；
> `docs/simulated-policies/` 下 13 份业务制度均为 **SIMULATED / UNREVIEWED 模拟条款**，
> **不是微软官方业务规则**，未经任何真实企业审核，不得用于生产业务口径。

---

## 1. 本轮解决的实际问题

| 问题 | 处理 |
|---|---|
| 原计划「从零设计约 22 张电商表」缺乏真实数据与可信 Schema | 改为接入公开示例库 AdventureWorks OLTP（71 表 / 759,240 行），数据、主键、外键、复合键均为真实 |
| 知识层缺少可验证的多表/中间表/粒度场景 | 真实库提供 `ProductSubcategory`、`EmployeeDepartmentHistory` 等必要中间表，以及 23 个复合主键、1 个复合外键 |
| 模拟业务规则与官方规则容易混淆 | 全部文档与产物双重标注 SIMULATED / UNREVIEWED；38 条指标全部 `reviewed=False / trusted=False` |
| 新增数据源可能污染既有 15 题指标 RAG 评测 | 新指标放独立 JSON，绝不追加进 `business_metrics.json`（该评测 `score()` 无 database 过滤） |
| 71 张带点号前缀的表名与 TEXT 金额会让 LLM 生成的 SQL 静默失败 | 以 evidence 形式注入数据源方言约束，不改任何上游 SQL 生成算法 |
| 在线 embedding 值索引需消耗配额 | 新库注册做成显式开关，默认关闭；未建索引的状态在自检中如实报告 |

---

## 2. 数据来源、许可与哈希（任务 1）

| 项 | 值 |
|---|---|
| 官方上游 | `github.com/microsoft/sql-server-samples` → `samples/databases/adventure-works` |
| 固定 commit | `beaab06ef72831089ca80e5355d65e661fd19b26`，源版本标识 SQL Server 2025 build `17.0.1000.3` |
| 许可 | **MIT**，官方仓库根目录 `license.txt`（`Copyright (c) Microsoft Corporation`）；许可文本已随库存档 |
| 许可文件 SHA256 | `201f3705979e932c92cd3c6ddbddf4dc3c71dcc5a82089a1e4018227cfe1cf09`（与研究包 manifest 记录的官方 LICENSE 哈希一致） |
| SQLite 移植方 | `https://adventureworks.demodb.dev/downloads/`（页面声明数据来自官方仓库、按 MIT 分发、要求保留许可与归属） |
| 拼接后 gzip | 35,367,459 B，SHA256 `5516328f621cbbe21083a20f325bc1a6f841e6f709bb6d8bebfebbf48b81d203` |
| **最终 SQLite** | **126,820,352 B**，SHA256 **`6a105e1982becfe003fc7a307d7cad9d738390cedd79d166c2167fd817168109`** |

两个哈希均与 provider `parts.json` 声明**逐字节一致**。完整来源、分片清单、许可与统计见
`docs/adventureworks/PROVENANCE.md`；许可文本见 `docs/adventureworks/LICENSE-microsoft-sql-server-samples.txt`。

### 真实结构统计（PRAGMA 实测）

| 项 | 值 |
|---|---|
| 物理表 / 视图 / 索引 | **71** / 11 / 122（其中 UNIQUE 86） |
| 列 / 数据行 | **486** / **759,240**（与 provider 契约一致） |
| 有主键的表 / 复合主键 | 71 / **23** |
| 外键约束组 / 悬空引用 | **90** / **0**（两阶段校验） |
| 复合外键 | **1**：`Sales.SalesOrderDetail(SpecialOfferID, ProductID) → Sales.SpecialOfferProduct` |
| 空表 | `dbo.DatabaseLog`、`dbo.ErrorLog` |
| 最大表 | `Sales.SalesOrderDetail` 121,317 行 |

provider 声明 91 个 FK、本地 90 组，**差异不是丢失**：provider 按「列条目」计数（复合外键 2 列记 2 条），SQLite `PRAGMA foreign_key_list` 按约束分组（该复合外键 1 组 2 行），`91 = 90 + 1`。

---

## 3. SQLite 迁移保真度核查（任务 2）

| 检查项 | 结论 | 处置 |
|---|---|---|
| SQL Server Schema 前缀 | **71/71 表名保留点号**，如 `Sales.SalesOrderHeader` | 裸写 `FROM Sales.SalesOrderHeader` 实测报 `no such table`；必须 `FROM "Sales.SalesOrderHeader"` |
| 主键 / 复合主键 / 唯一约束 | 全部保留，顺序按 `pk` 序号；86 个 UNIQUE 索引 | Phase 1 `SchemaExtractor` 直接复用 |
| 外键 / 复合外键 | 全部保留，顺序按 `(id, seq)`；0 悬空 | 复合外键纳入评测集反例 |
| **金额/成本类型** | 源 `DECIMAL(19,4)` 等 → `DECIMAL_TEXT(...)` **48 列**，实测 `typeof()` 全为 **`text`** | 聚合/比较/排序必须 `CAST(... AS REAL)` |
| **日期类型** | 源 `datetime` → TEXT，值 `'2022-05-30 00:00:00.000'` | 用 `substr(col,1,10)` / `date(col)` |
| BLOB | 7 列 | 知识层只抽元数据，不导入原始数据行 |
| 原始 SQL Server 类型精度 | **未随 SQLite 保留**，`provider-schema.json` 的 `tableSql` 是转换后 DDL | 记为限制，不从库内臆造源类型 |
| 源视图 | 20 → 11 可移植（XML / APPLY 不兼容） | 不假定源视图存在 |
| `PRAGMA foreign_keys` | 连接默认 0（关闭） | 只读分析不受影响；FK 仅作结构事实 |

**未验证项（明确留白）**：源 T-SQL 的 CHECK 约束、默认值、触发器、计算列、XML Schema、
`hierarchyid`/`geography` 语义均未在 SQLite 版核查，本项目不把它们当业务规则使用。

---

## 4. 原有资产保护（任务 3）

- **未删除、未覆盖、未重写**：`data/enterprise_sales/`、`data/enterprise_hr/`、`data/bird/`、
  `evals/metric-rag/`、`evals/bird-dev-50/`、`evals/agent/`、`business_metrics.json`。
- 历史评测产物 mtime 仍为 10 月（`metric-rag/latest` Oct 28、`bird-dev-50/ablation` Oct 26、
  `agent/latest` Oct 28），本轮未重新运行任何在线评测。
- 默认 `build-knowledge` 输出与 Phase 1 **逐字节一致**：`metrics.json` `c513db69…`、
  `summary.json` `ddc0482a…`、`snapshot.json` `5dece159…`，内容指纹仍 `96f7f2cb…a58`。
- 既有 153 项测试与 9 个 CLI 子命令集合未改一条断言。

---

## 5. 数据源接入（任务 4）

| 层 | 接入方式 | 是否改动上游算法 |
|---|---|---|
| 数据源注册中心 | `app/web/registry.py`：`ENABLE_ADVENTUREWORKS=1` 时把 `data/adventureworks/adventureworks.sqlite` 注册为 `database_id="adventureworks"`，`setdefault` 不覆盖任何现有条目；补展示名/元数据/顺序/示例题 | 否 |
| SQLSkill | 通过 `registry.database_paths` 动态感知；`SQLSkill.__init__` 的路径校验天然支持新 ID | **否**：`app/skills/sql_skill.py` 一字未改 |
| 只读执行沙箱 | `execute_readonly_table` 是通用实现，直接接受新库路径；已用真实 SQL + 写操作拒绝验证 | **否**：`app/db_utils/execution.py` 未改 |
| LLM 输入侧 | `app/agent/tools/registry.py` 在组装 `request["evidence"]` 时，**仅对 adventureworks** 追加 `SQL_DIALECT_HINT`（点号表名引号、DECIMAL_TEXT CAST、日期格式、复合外键、只读） | 否：只加上下文，不改 schema linking / SQL 生成 |
| 上游 DeepEye-SQL | `app/pipeline/`、`app/db_utils/schema.py`、`app/prompt/`、`app/services/` 全部未动 | 否 |

**为何默认关闭**：该库的值向量索引需在线 embedding（`api_type=openai` / DashScope
`qwen3.7-text-embedding-flash`），71 表全量建索引会消耗配额，本轮未获批准。默认关闭保证
`app.cli check` 仍为 `ready`、既有测试与 15 题评测零影响。显式启用后的真实自检输出：

```json
{"ready": false, "database_count": 3,
 "databases": [{"id":"adventureworks","database_ready":true,"value_index_ready":false},
               {"id":"enterprise_hr","database_ready":true,"value_index_ready":true},
               {"id":"enterprise_sales","database_ready":true,"value_index_ready":true}]}
```

`ready=false` 是**预期且如实**的状态：数据库文件就绪，值索引未建。补齐命令（在线、需配额）：
`python -m app.cli build-index --database data/adventureworks/adventureworks.sqlite`。

---

## 6. 知识构建结果（任务 5、8）

复用了 Phase 1 的 `SchemaExtractor`、`MetricRegistry`、`KnowledgeSnapshot`，仅做两处泛化：
合法数据源白名单扩展为 `KNOWN_DATABASE_IDS`（默认值仍是原两库）；
4 处 `split(".", 1)` → `rsplit(".", 1)` 以解析 `"Schema.Table.Column"`（对原两库等价）。

```bash
python scripts/build_aw_metrics.py
python -m app.cli build-knowledge --database adventureworks \
  --metrics app/enterprise/adventureworks_metrics.json \
  --output workspace/knowledge/adventureworks
```

| 产物 | 值 |
|---|---|
| 指标 / 表 / 列 / Schema 关系 | **38 / 71 / 486 / 90** |
| 知识边 | **690**：`TABLE_HAS_COLUMN=486`、`COLUMN_FOREIGN_KEY_TO_COLUMN=91`、`METRIC_DEPENDS_ON_COLUMN=71`、`METRIC_USES_TABLE=40`、`METRIC_ALLOWS_DIMENSION=2`、`METRIC_DEPENDS_ON_METRIC=0` |
| 校验 | **0 error / 38 warning**（全部为「未审核」）、10 条聚合风险 |
| 可信状态 | `trusted=0`、`pending_review=38` |
| Schema 指纹 | `807757d2a598f804e11fb6d2c741e15a5535d758a51724cb7c7fb5914c36d193` |
| 内容指纹 | `0921eea6bf5c6759b0a57b9039740f659253c93829145bc743288e3c34cef90c` |
| 可复现性 | 连续两次构建，产物目录下 **6 个 JSON 的 SHA256 完全一致** |

产物写到独立目录 `workspace/knowledge/adventureworks/`，**未覆盖** Phase 1 的 `workspace/knowledge/*.json`。

---

## 7. 结构化指标与模拟制度文档（任务 6、7）

**38 条指标**（`app/enterprise/adventureworks_metrics.json`），域分布：
sales 9 / product 8 / inventory 9 / purchasing 6 / hr 2 / region 2 / performance 2。

生成脚本 `scripts/build_aw_metrics.py` **强制校验**三条，否则拒绝写出：
① 引用的表/列在真实 SQLite 中存在；② `expression` 是所引政策条款的**逐字引用**；
③ `time_field` 被 `source_columns` 覆盖。

- 所有 `source_ref` 指向 `docs/simulated-policies/<file>.md#<clause>`，`provenance` 区分
  `verbatim` / `derived` / `verified: PRAGMA` / `unknown`。
- `unit` 一律 `UNKNOWN`（示例库未声明币种），`required_filters` 为空（不对 `Status` 等
  无字典字段做业务假设），缺失项进入 `unknown_fields`。
- **`reviewed` 不可由 JSON 覆盖**：`_apply_structured` 明确禁止，保证未审核指标永远不能变成可信 SQL 约束。
- 10 条 `aggregation_risks` 覆盖 `duplicate_aggregation` / `mixed_grain` / `division_by_zero`。

**13 份模拟制度文档**（`docs/simulated-policies/`），覆盖销售口径、净销售额/折扣、
标准成本/毛利、历史成本匹配、销售绩效/配额、员工薪酬、区域归属、跨币种、库存与库位、
再订货点/报废、采购与供应商、商品分类、指标治理。每份均含：
SIMULATED / UNREVIEWED 声明、真实字段依据表、带 § 编号的口径条款、以及「待审核事项」表
（共记录约 35 条待人工审核规则）。文档不是简单模板复制，条款内容各自独立。

---

## 8. 端到端验证（任务 9）

共 **26 条真实 SQL** 通过 `app.skills.sql_skill.execute_readonly_table` 在真实库上执行，
证据保存在 `data/adventureworks/query_evidence.json`（20 条）与 `test_split_evidence.json`（6 条）。

| 场景 | 正确值 | 错误/对照值 |
|---|---|---|
| 订单数 / 销售额（SubTotal） | 31,465 单 / **109,846,381.40** | 订单头×明细错误 JOIN → **2,596,470,456.68**（约 23.6×） |
| 采购订单 / 应付总额 | 4,012 单 / **70,479,332.64** | 采购头×明细错误 JOIN → 180,741,104.49 |
| 复合外键关联行数 | **121,317**（双列） | 仅 `ProductID` 单列 → **296,983** |
| 商品类别净销售额 | Bikes 94,651,172.70 居首（4 类） | 缺 `ProductSubcategory` 中间表则无法关联 |
| 低于再订货点商品数 | **7**（先按商品聚合） | 逐库位比较口径错误 |
| 库存 | 335,974 / 432 个商品 | 聚合前后守恒 |
| 商品 707 历史成本 | 3 段区间 12.0278 / 13.8782 / 13.0863 | 不得用当前 `StandardCost` 替代 |
| 成本×价格历史仅按 ProductID 关联 | 两表各 395 行 | 关联后 **649** 行（区间粒度膨胀） |

离线验证覆盖四个环节（均不调用任何模型）：

| 环节 | 结果 |
|---|---|
| DeepEye-SQL 输入侧 | `SchemaService.load_sqlite_schema` 加载 71 表；`build_schema_profile` 生成 24,190 字符，含点号表名 |
| LangGraph / Agent 装配 | 6 个工具构造成功，`build_agent_graph` 编译为 `CompiledStateGraph`；`inspect_database_schema` 显式传入点号表名返回 26 列 |
| Schema 知识检索 | Phase 1 `extract_database_schema("adventureworks")` → 71 表 / 486 列 / 90 关系，指纹稳定 |
| SQL 安全执行 | 只读查询全部成功；`DELETE`、`ATTACH` 被 authorizer 拒绝 |

**未运行**：DeepEye-SQL 六阶段在线 SQL 生成、LangGraph Agent 真实推理、BIRD/指标在线评测。
原因是需要值索引（在线 embedding）与 LLM 配额；本轮不编造任何在线实验结果。

---

## 9. GraphRAG 评测集（任务 10）

`evals/adventureworks-graphrag/`：

| 文件 | 题量 | 类别分布 |
|---|---|---|
| `dev.json` | **12** | 中间表补全 3 / 多候选 Join 路径 3 / 历史成本匹配 3 / 粒度冲突 3 |
| `test.json` | **6**（独立封存） | 中间表 1 / Join 路径 2 / 历史成本 1 / 粒度冲突 2 |

每题含 `question`、`required_tables`、`required_intermediate_tables`、`acceptable_join_paths`、
`policy_evidence`、`reference_sql`、`reference_result`（columns + rows）、`key_conclusion`、
`validation_rules`、`counter_example`（反例对照）。所有 `reference_sql` 与结果均由
`execute_readonly_table` **真实执行取得**，不是 Agent 输出；集成测试会逐题重放并比对结果。

隔离规则（`README.md` + POL-GOV-001 §5）：`test.json` 不得用于 Prompt 调优、不得进入
Few-shot / SQL 经验库；示例题文件与测试集题目不重复；区分「执行成功」与「业务语义正确」。

---

## 10. 文件清单

### 本轮新增

| 路径 | 用途 |
|---|---|
| `data/adventureworks/adventureworks.sqlite` 及 8 份来源/证据文件 | 数据库本体、许可、provider schema、manifest、inspection、查询证据（该目录 gitignore） |
| `docs/adventureworks/PROVENANCE.md` | 来源、许可、哈希、迁移保真度、未验证项 |
| `docs/adventureworks/LICENSE-microsoft-sql-server-samples.txt` | 官方 MIT 许可文本（可跟踪副本） |
| `app/enterprise/adventureworks.py` | 来源常量、显式开关、SQL 方言 evidence |
| `app/enterprise/adventureworks_metrics.json` | 38 条结构化指标（独立文件，不污染基线） |
| `app/enterprise/adventureworks_examples.json` | 5 条真实可查的示例问题 |
| `scripts/build_aw_metrics.py` | 可复现指标生成 + 三重强校验 |
| `docs/simulated-policies/*.md`（13 份） | 模拟制度与待审核规则 |
| `evals/adventureworks-graphrag/{dev.json,test.json,README.md}` | 开发集 / 独立测试集 / 隔离规则 |
| `tests/test_adventureworks_integration.py` | 28 项离线 + 真实库集成测试 |
| `docs/数据资产建设-AdventureWorks接入报告.md` | 本报告 |

### 本轮修改

| 路径 | 改动 |
|---|---|
| `app/web/registry.py` | 显式开关注册、展示元数据、示例题加载、排序 |
| `app/agent/tools/registry.py` | 仅 AW 追加 SQL 方言 evidence（Phase 1 已改过共享 PRAGMA 复用） |
| `app/knowledge/schema_extractor.py` | 新增 `ADVENTUREWORKS_DATABASE_ID` / `KNOWN_DATABASE_IDS`，默认值不变 |
| `app/commands/build_knowledge.py` | 白名单校验改用 `KNOWN_DATABASE_IDS`，默认构建行为不变 |
| `app/knowledge/metric_registry.py` | `rsplit` 泛化、`from_file` 来源可追溯、`structured` 可选覆盖（`reviewed` 不可覆盖） |
| `app/knowledge/models.py`、`app/knowledge/dependencies.py` | `rsplit` 泛化 |
| `goals/phase0-rag-graphrag/GOAL.md` | 契约状态 |

### 明确未动

`business_metrics.json`、`enterprise/knowledge.py`、`evals/metric-rag/*`、`evals/bird-dev-50/*`、
`evals/agent/*`、`data/enterprise_*`、`data/bird`、`app/skills/`、`app/pipeline/`、
`app/db_utils/`、`app/prompt/`、`app/api/`、`app/web/server.py`、`app/web/preflight.py`、
`app/cli.py`（Phase 1 已改，本轮未再动）、`.env`、`pyproject.toml`、任何依赖。

---

## 11. 真实测试结果

| 命令 | 结果 |
|---|---|
| `python -m unittest discover -s tests -q` | **Ran 181 tests / OK**（153 基线 + 28 新增） |
| `python -m compileall -q app runner tests scripts` | exit 0 |
| `node tests/js/recovery.test.mjs` | `ok 55 assertions` |
| `python -m app.cli check --quiet` | `ready`（AW 默认关闭） |
| `evaluate-rag --validate-only` / `evaluate --validate-only` | `15` / `14` valid |
| `build-knowledge --validate-only --quiet`（默认） | 13 指标 / 11 表 / 57 列 / 9 关系 / 152 边 / 0 error，指纹 `96f7f2cb…a58` |
| AW 构建 ×2 | 6 个 JSON SHA256 完全一致 |
| 26 条真实 SQL | 全部执行成功；写操作/ATTACH 被拒 |

**未运行任何在线模型评测**，未产生新的准确率、Token 或延迟数字；本轮所有数值均为
真实 SQLite 查询结果或结构统计，不涉及模型推理。

---

## 12. 限制与遗留问题

1. **AW 默认关闭**：值索引需在线 embedding（配额），启用后 `check` 会报告 `value_index_ready=false`；
   完整在线查询链路（六阶段 SQL 生成 + Agent 推理）尚未端到端跑通。
2. **38 条指标全部待审核**：`trusted=0`，`unit` / `required_filters` / 多数 `join_constraints`
   为 UNKNOWN；模拟制度不是可信业务口径，不能作为硬 SQL 约束。
3. **表/字段业务描述缺失**：Phase 1 `SchemaExtractor` 只抽元数据，AW 库无
   `database_description/*.csv`，故 description 全为 UNKNOWN（自检仅一条 warning）。
4. **原始 SQL Server 类型精度不可从 SQLite 还原**；CHECK/默认值/触发器未核查。
5. 未实现文档切分、向量索引、增量更新、版本联动、权限过滤，也未实现 GraphRAG 在线检索、
   业务语义 AST 校验、跨查询一致性验证。
6. `sqlglot`、`pytest`、`sqlparse` 仍未安装，未新增任何依赖。
7. 本轮未做 A–E 对照实验与消融实验，**不宣称任何 SQL 正确率或检索指标提升**。

---

## 13. 下一阶段 GraphRAG 接入方案（Phase 2/3，待放行）

1. **构图**：把本轮 690 条知识边载入已安装的 `networkx`，节点为
   Database/Table/Column/Metric/Dimension，边带 `origin`（declared/manual）与 `verified`；
   人工补充关系必须标 `manual + verified=False`，禁止 LLM 生成可信外键。
2. **子图检索**：新增 `app/knowledge/retrieval/{schema_retriever,context_builder}.py`，
   Table-first / Column-first 双向召回融合；用指标的 `source_columns/table_ids`
   **强制保留**依赖字段，不被相似度裁剪；限跳扩展（max hops / max tables / Token 预算）。
3. **Join 路径**：基于 90 条真实 FK 做候选路径搜索与排序，计入复合外键、基数
   （`duplicate_aggregation_risk`）、粒度；多路径歧义返回风险而非静默选择。
   `evals/adventureworks-graphrag/dev.json` 的 12 题直接作为首轮验证集。
4. **运行时接入**：新增 `retrieve_schema_context` 工具 → 经 `SQLSkillRequest.schema_context`
   **默认关闭**的可选开关注入；关闭时逐字节保持现有行为；复用 `on_stage` 推 SSE，前端协议不变。
5. **评测**：用 `test.json`（封存）跑 A–E 对照 + 消融，指标含必要中间表召回率、
   Join 路径正确率、SchemaContext Token 数、SQL 执行准确率与业务语义正确率、
   无证据结论比例；BIRD 50 题样本与 EX 规则保持原样，不把既有 72%→76% 归因于新模块。

---

## 14. 需你确认后才能继续

① 是否放行 Phase 2/3 GraphRAG 子图检索；
② 是否批准为 AW 运行 `build-index`（在线 embedding，消耗配额）以打通完整在线查询链路；
③ 哪些模拟条款可由人工审核升级为可信指标（`reviewed=True`）；
④ 是否批准 `sqlglot` 依赖（仅后续 AST 语义校验可能需要）；
⑤ 是否批准 Phase 6 在线对照/消融评测。

**本轮到此停止，未开始 Phase 2。**
