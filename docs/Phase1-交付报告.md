# Phase 1 交付报告：结构化企业指标知识库与 Schema 知识库

> 项目根：`E:/曹浩冉/研究生/AI学习RAG与Agent/AGENT_exp/智能数据分析助手/DeepEye-SQL`（下称 `ROOT`）
> 完成时间：2026-11-13 09:40 GMT+8 · 契约：`goals/phase0-rag-graphrag/GOAL.md`（`current_phase: 1`，`status: PAUSED`）
> 本轮性质：**离线知识层基础设施建设**。未改 LangGraph 主工作流、未改上游 SQL 算法、未改历史评测数据与报告、未改 `.env`、未新增任何依赖、未运行在线模型评测。

---

## 1. 本轮解决的实际问题

| # | Phase 0 定位的问题 | Phase 1 的解法 |
|---|---|---|
| 1 | 指标口径只有自然语言 `sql_rule`，只能拼进 Prompt，无法程序化约束 SQL | `MetricDefinition` 把口径拆成 `expression / aggregation / source_tables / source_columns / required_filters / join_constraints / unit / grain` 等可校验字段，并保留全部原文 |
| 2 | Schema 元数据只在工具调用时临时返回，无稳定 ID、无版本、无关系记录 | `schema_extractor.py` 从 PRAGMA 抽 11 表 / 57 列 / 9 条 FK，生成 `database.table.column` 稳定 ID 与 SHA256 结构指纹 |
| 3 | 指标与表/字段之间没有显式依赖，无法回答「改字段影响哪些指标」 | `dependencies.py` 生成 152 条 `KnowledgeEdge`，提供 6 类查询 API 与字段删除影响面分析 |
| 4 | 净销售额的「退款重复聚合」只存在于一段自然语言里，无法被后续模块消费 | 结构化为 2 条 `AggregationRisk`（`duplicate_aggregation` / `mixed_grain`），并与真实 FK 基数对齐验证 |
| 5 | 知识构建不可复现，缺少可验证产物 | `build-knowledge` CLI 输出 7 个确定性 JSON；两次构建字节完全一致 |
| 6 | 无法区分「原文事实」「推导字段」「无证据字段」 | 每个字段带 `provenance` 说明；无证据字段一律 `UNKNOWN` + 列入 `unknown_fields`；`reviewed=False` / `trusted=False` |

**核心边界**：Phase 1 只把知识变成「可检索、可关联、可校验、可版本化」的数据，**没有接入运行时主链路**。`EnterpriseKnowledgeBase` 与 `SQLSkill` 的行为逐字节不变，因此所有既有检索、SQL 查询、API/SSE 兼容性完全保留。

---

## 2. 所有新增和修改文件

### 2.1 新增（10 个）

| 路径 | 职责 |
|---|---|
| `app/knowledge/__init__.py` | 包导出 |
| `app/knowledge/models.py` | Pydantic v2：`MetricDefinition` / `SchemaColumn` / `SchemaTable` / `SchemaRelation` / `UniqueConstraint` / `DatabaseSchema` / `AggregationRisk` / `KnowledgeEdge` / `ValidationIssue` / `KnowledgeSnapshot` + 稳定 ID 与指纹工具 |
| `app/knowledge/sqlite_metadata.py` | **唯一**的 SQLite PRAGMA 解析实现，供 Agent 工具与知识层共用；只含标准库 |
| `app/knowledge/schema_extractor.py` | 抽取表/字段/PK/复合 FK/UNIQUE、基数判定、稳定 Schema 指纹、人工关系区分、JSON 序列化 |
| `app/knowledge/metric_registry.py` | 13 条指标结构化、原文保留、销售域补充映射（带 provenance）、校验与查询 API |
| `app/knowledge/dependencies.py` | 关系构建、`KnowledgeIndex` 六类查询、风险元数据、快照构建 |
| `app/commands/build_knowledge.py` | `build-knowledge` CLI，7 个产物，可复现 |
| `tests/test_knowledge_models.py` | 14 项：稳定 ID、序列化、时间戳无关指纹、CLI 可复现与严格模式 |
| `tests/test_metric_registry.py` | 21 项：13 条加载、原文保留、可信边界、坏引用检测、净销售额风险、检索兼容、字段影响面 |
| `tests/test_schema_extractor.py` | 13 项：真实库表/键/基数、复合主键与外键、UNKNOWN 兜底、无 PII、工具兼容性 |

### 2.2 修改（4 个既有文件，均为必要集成）

| 路径 | 改动 | 兼容性说明 |
|---|---|---|
| `app/cli.py` | 新增 `build_knowledge` import、docstring 一行、`COMMANDS` 一项 | 原 8 个子命令不变 |
| `app/commands/__init__.py` | 新增 `COMMAND_MODULES` 清单与 Phase 1 说明 | 纯文档 + 常量，不改变导入行为 |
| `app/agent/tools/registry.py` | `inspect_database_schema()` 改为调用 `sqlite_metadata.compact_view(read_table(...))` | 输出结构逐字段不变（`name/type/nullable/primary_key`、`from/to_table/to_column` 均保留，含 `type=None` 情形），已用 `tests/test_schema_extractor.py::ToolRegistryCompatibilityTest` 验证；避免两处重复实现 PRAGMA |
| `tests/test_cli.py` | 子命令全等断言集合 8 → 9（增加 `build-knowledge`） | **不是降低要求**：仍是严格 `assertEqual` 全等断言，只是同步新增功能；原 105 项全部通过 |

`git diff --stat`：`4 files changed, 29 insertions(+), 12 deletions(-)`。`git diff --name-only -- app/enterprise/business_metrics.json evals/ README.md` 为空；`evals/**/latest/summary.json` 的 mtime 仍为 10 月，确认历史实验产物未被改写。

### 2.3 产物（gitignore 覆盖，不入库）

`workspace/knowledge/`：`metrics.json`、`schema_enterprise_sales.json`、`schema_enterprise_hr.json`、`relations.json`、`snapshot.json`、`summary.json`、`validation.json`。

---

## 3. 核心数据模型与调用链路

### 3.1 模型与稳定 ID

| 模型 | 关键字段 | ID 规则 |
|---|---|---|
| `SchemaColumn` | `column_id / data_type / nullable / primary_key / primary_key_position / description / version` | `stable_column_id(db, table, col)` → `enterprise_sales.orders.order_id` |
| `SchemaTable` | `table_id / primary_keys(有序) / unique_constraints(有序) / columns` | `stable_table_id(db, table)` → `enterprise_sales.orders` |
| `SchemaRelation` | `source/target_columns(有序) / cardinality / source_ref / verified / origin / duplicate_aggregation_risk` | 含库名 + 源/目标表 + 有序列名 + origin |
| `MetricDefinition` | 用户要求的 19 个字段 + `raw_*` 原文 + `unknown_fields` + `provenance` + `aggregation_risks` + `trusted` 属性 | 复用原 JSON 的 `id` |
| `KnowledgeSnapshot` | `database_ids / schema_fingerprints / metric_versions / built_at / nodes / relations / validation` | `content_fingerprint()` 排除 `built_at` |

**复合键**：`primary_keys` 按 `PRAGMA table_info.pk` 序号排序；FK 按 `(constraint_id, seq)` 分组，源列与目标列顺序严格对应（内存库夹具已验证 `child(x,y) → parent(a,b)`）。`REFERENCES parent` 省略目标列时，按 SQLite 规范回落到目标表主键，不按列名猜测。

**基数判定**（FK 方向 source → target）：

| 条件 | 结果 |
|---|---|
| 目标列组合是 PK / UNIQUE，源列组合也是唯一 | `ONE_TO_ONE`（如 `payments.order_id → orders.order_id`，因 `payments.order_id` 有 UNIQUE 约束） |
| 目标唯一、源不唯一 | `MANY_TO_ONE` + `duplicate_aggregation_risk=True`（如 `order_items.order_id`、`refunds.order_id`） |
| 目标不唯一或无法确认 | `UNKNOWN`，且不标记风险 |

`verified=True` **仅**用于 `PRAGMA foreign_key_list` 声明的关系；`manual_relations` 参数进来的业务关系固定 `origin=manual / verified=False / cardinality=UNKNOWN`，并保留 `source_ref`。

### 3.2 调用链路

```
python -m app.cli build-knowledge
  → app/commands/build_knowledge.py:run()
      → MetricRegistry.from_file(app/enterprise/business_metrics.json)   # 只读，13 条
      → extract_database_schema(db, data/<db>/<db>.sqlite)               # 只读 PRAGMA
          → sqlite_metadata.read_table() （与 ToolRegistry 共用同一实现）
          → DatabaseSchema.with_fingerprint()                             # SHA256
      → registry.validate(schemas)   → list[ValidationIssue]
      → build_edges(registry, schemas) → 152 KnowledgeEdge
      → KnowledgeIndex(...)          → 6 类依赖查询 + 字段影响面
      → build_snapshot(...)          → KnowledgeSnapshot.content_fingerprint()
      → 写 7 个确定性 JSON 到 workspace/knowledge/
```

运行时主链路（`app/agent/graph.py` → `ToolRegistry` → `SQLSkill` → `app/pipeline/*`）**完全未改动**；Phase 2 才会通过可选开关把 `SchemaContext` 接进去。

### 3.3 可信边界（重要）

`MetricDefinition.trusted = reviewed and not unknown_fields`。Phase 1 没有人工审核签字，因此 **13 条全部 `reviewed=False / trusted=False`**，`MetricRegistry.trusted_metrics()` 返回空列表。构建产物明确输出 `trusted_metric_count=0`、`pending_review_metric_count=13` 与 13 条 `unreviewed_metric` warning。这意味着本轮知识**不得**被当作硬 SQL 约束使用，符合「不自行补造业务规则」的要求。

所有 `expression` 都是 `business_metrics.json` 的 `definition / formula / sql_rule` **逐字引用**（`expression_kind="verbatim"`），已由单测逐条断言；`aggregation / time_field / unit / source_tables` 等推导字段在 `provenance` 里标注依据，且引用字段全部通过真实库校验。

---

## 4. 实际构建出的数量（真实产物，非估算）

| 项目 | 数量 | 明细 |
|---|---:|---|
| 结构化指标 | **13** | 销售 5 + HR 8 |
| 数据库 | **2** | `enterprise_sales`、`enterprise_hr` |
| 表 | **11** | 销售 7（`categories/customers/order_items/orders/payments/products/refunds`）+ HR 4（`attendance/departments/employees/performance_reviews`） |
| 字段 | **57** | 销售 35 + HR 22 |
| Schema 关系（声明 FK） | **9** | 销售 6 + HR 3，全部 `verified=True`、全部 `origin=declared` |
| UNIQUE 约束 | **6** | 销售 2：`categories.category_name`、`payments.order_id`；HR 4：`attendance(employee_id,attendance_month)`、`departments` 两个（`cost_center` / `department_name`）、`performance_reviews(employee_id,review_period)` |
| 知识边 | **152** | `TABLE_HAS_COLUMN=57`、`METRIC_DEPENDS_ON_COLUMN=51`、`METRIC_USES_TABLE=30`、`COLUMN_FOREIGN_KEY_TO_COLUMN=9`、`METRIC_DEPENDS_ON_METRIC=5`、`METRIC_ALLOWS_DIMENSION=0` |
| 聚合风险元数据 | **4** | `net_revenue` 2 条（重复聚合 / 混合粒度）、`refund_rate` 2 条（除零 / 继承重复聚合） |
| 校验结果 | 0 error / 13 warning | warning 全部是「未人工审核」 |
| 可信指标 / 待审核 | **0 / 13** | — |

指纹：`enterprise_sales = 6fd47d2b…893`、`enterprise_hr = f08946c1…d76`、知识内容指纹 `96f7f2cb…a58`。两次构建 7 个 JSON 的 sha256 完全一致（`IDENTICAL=True`）。

字段影响面示例（来自 `summary.json`）：`enterprise_sales.order_items.unit_price` → `revenue / net_revenue / average_order_value / refund_rate`；`enterprise_sales.refunds.refund_amount` → `net_revenue / refund_rate`。

> `METRIC_ALLOWS_DIMENSION=0` 是**预期结果**：`EdgeType` 已定义并预留该关系，但现有文档与指标定义没有给出任何经过审核的允许维度清单，Phase 1 不编造维度，因此不生成实例。

---

## 5. 哪些指标已结构化，哪些仍需人工审核

**13 条全部已生成 `MetricDefinition`**（结构化完成），但 **13 条全部 `reviewed=False`，都需要人工审核后才能作为可信约束**。分层如下：

| 分类 | 指标 | `evidence_status` | 状态 |
|---|---|---|---|
| 原文已带 `formula/tables/fields/time_grain` | HR 8 条：`active_headcount`、`new_hires`、`departures`、`attrition_rate`、`average_performance`、`department_headcount_share`、`attendance_rate`、`late_rate` | `complete` | 结构化字段直接来自原文，字段引用已逐条对真实库校验通过；仍缺人工签字 |
| 原文缺结构化字段，本轮补齐可推导部分 | 销售 5 条：`revenue`、`net_revenue`、`average_order_value`、`refund_rate`、`new_customer` | `partial` | 补齐了逐字 `expression`、经真实库验证的 `source_tables/source_columns`、`required_filters`、`join_constraints`（净销售额）、`depends_on_metrics`、风险元数据 |

逐条待审核缺口（`unknown_fields`，来自 `workspace/knowledge/summary.json`）：

| 指标 | 未知业务定义 |
|---|---|
| `revenue` | grain, time_field, unit, allowed_dimensions, join_constraints |
| `net_revenue` | aggregation, grain, time_field, unit, allowed_dimensions, required_filters |
| `average_order_value` | grain, time_field, unit, allowed_dimensions, join_constraints |
| `refund_rate` | grain, time_field, allowed_dimensions, join_constraints, required_filters |
| `new_customer` | aggregation, grain, unit, allowed_dimensions, join_constraints |
| `active_headcount` | grain, time_field, unit, allowed_dimensions, join_constraints |
| `new_hires` / `departures` | grain, unit, allowed_dimensions, join_constraints |
| `attrition_rate` / `attendance_rate` | grain, allowed_dimensions, join_constraints |
| `average_performance` | grain, time_field, unit, allowed_dimensions, join_constraints |
| `department_headcount_share` | grain, time_field, allowed_dimensions, join_constraints |
| `late_rate` | grain, time_field, allowed_dimensions, join_constraints |

**没有自行确定的内容**：销售指标的统计时间字段（用 `order_date` 还是 `paid_at`）、统计粒度、金额单位、允许维度、Join 约束、净销售额/新增客户的聚合方式，均保留 `UNKNOWN`。`new_customer.time_field=orders.order_date`、`refund_rate.unit=%`、部分 HR 指标的 `time_field` / `unit` 属于**有原文逐字依据**的确定性推导，已在 `provenance` 标注来源。

净销售额约束已按原文逐字记录：`join_constraints = ["退款按 order_id 聚合后再与订单连接，避免订单明细导致退款金额重复。"]`，并用真实 FK 基数验证 `order_items.order_id → orders.order_id`、`refunds.order_id → orders.order_id` 均为 `MANY_TO_ONE` 且带重复聚合风险。

---

## 6. 真实测试命令及结果

用 `./.venv/Scripts/python.exe`（Python 3.12.13），未使用 `uv run`，未安装依赖，未调用模型端点。

| ID | 命令 | 真实输出 | 结果 |
|---|---|---|---|
| F1 | `python -m unittest discover -s tests -v` | `Ran 153 tests in 4.706s` · `OK` · exit 0 | PASS（105 基线 + 48 新增） |
| F2 | `unittest discover -p test_knowledge_models.py -v` | `Ran 14 tests` · `OK` | PASS |
| F3 | `unittest discover -p test_metric_registry.py -v` | `Ran 21 tests` · `OK` | PASS |
| F4 | `unittest discover -p test_schema_extractor.py -v` | `Ran 13 tests` · `OK` | PASS |
| F5 | `python -m compileall -q app runner tests` | exit 0 | PASS |
| F6 | `node tests/js/recovery.test.mjs` | `ok 55 assertions` · exit 0 | PASS |
| F7 | `python -m app.cli check --quiet` | `ready` · exit 0 | PASS |
| F8 | `python -m app.cli evaluate-rag --validate-only` | `valid=true, case_count=15` | PASS |
| F9 | `python -m app.cli evaluate --validate-only` | `valid=true, case_count=14` | PASS |
| F10 | `python -m app.cli build-knowledge --quiet`（实跑两次） | `metrics=13 tables=11 columns=57 relations=9 edges=152 trusted=0 pending=13 errors=0 warnings=13` · exit 0 | PASS |
| F11 | 两次构建 7 个 JSON 的 `sha256sum` 对比 | `IDENTICAL=True` | PASS |
| F12 | `python -m app.cli build-knowledge --validate-only` | 返回 0 且不创建产物目录 | PASS（单测覆盖） |
| F13 | `python -m app.cli build-knowledge --strict` | 因 13 条 `unreviewed_metric` warning 返回 1 | 符合设计（单测覆盖） |

**过程中出现并修复的真实问题**：`tests/test_schema_extractor.py` 的 4 个复合键夹具最初用 `tempfile.TemporaryDirectory` + 临时 sqlite 文件，Windows 下 `sqlite3.connect` 的 context manager 不关闭连接，导致目录清理报 `PermissionError: [WinError 32]`。改为 `:memory:` 内存库夹具并显式 `close()` 后 13 项全部通过。**这是测试夹具问题，不是业务逻辑缺陷**；未降低任何断言。

**未执行**：所有在线模型评测（`evaluate` / `evaluate-rag` 完整跑、BIRD 批量跑批、真实问题探针）。本轮无新增实验结果，也没有对指标 RAG `0.5667→1.000`、BIRD `36/50→38/50`、Agent `14/14` 做任何复测或改写。

---

## 7. 当前仍存在的问题与限制

| # | 问题 | 影响 | 建议处理阶段 |
|---|---|---|---|
| 1 | 13 条指标全部未人工审核、`trusted=0` | 知识暂时不能作为硬 SQL 约束；Phase 2 只能用于检索增强 | Phase 2 前需人工确认口径，Phase 4 才能启用语义校验 |
| 2 | `grain / allowed_dimensions / join_constraints` 全库缺失 | 无法回答「该指标允许哪些维度」；`METRIC_ALLOWS_DIMENSION` 边为 0 | 需要业务文档或人工配置，Phase 2/4 |
| 3 | 知识层尚未接入运行时主链路 | 目前只有离线产物，端到端 SQL 正确率没有任何变化，也不应声称有提升 | Phase 2（可选开关） |
| 4 | 表/字段业务描述全部 `UNKNOWN` | 向量检索只能靠名称与指标文本，字段语义召回弱 | Phase 2 引入人工审核的描述 + SchemaContext |
| 5 | 无文档知识库、无 chunk、无版本联动、无增量更新 | 文档驱动查询与知识失效检测做不了 | Phase 4/5 |
| 6 | `sqlglot` / `pytest` / `sqlparse` 未安装，未新增依赖 | AST 语义校验尚无实现途径 | Phase 4 前需批准依赖，否则用标准库 + SQLite `EXPLAIN` |
| 7 | 关系图只用纯 Python 边列表，未用 NetworkX、未做子图扩展 | Join 路径补全与「最短路径≠业务正确路径」尚未验证 | Phase 3 |
| 8 | 无知识增强 SQL 评测集与对照实验 | 无法证明新增知识模块的收益 | Phase 6，必须用真实对照实验 |
| 9 | 人工补充关系目前只能靠 CLI 参数，尚无审核过的配置文件 | 业务关系无法持久化 | Phase 2/3 |

---

## 8. Phase 2 如何接入指标驱动的 Schema 检索和 GraphRAG

**前提**：Phase 2 不改动本轮已稳定的模型与产物格式，只新增检索层与一个可选工具，保持「开关关闭时行为逐字节不变」。

### 8.1 接入路径（最小改动）

1. **检索层新增包** `app/knowledge/retrieval/`：
   - `hybrid.py`：在 `EnterpriseKnowledgeBase.search()` 之上加知识类型 / 数据库 / 版本 / 审核状态过滤，RRF 融合；**不改** `knowledge.py` 的 `search()` 签名。
   - `schema_retriever.py`：Table-first（问题 → 表 → 表内字段）与 Column-first（问题 → 字段 → 所属表）双向召回；用本轮 `MetricDefinition.source_columns / table_ids` **强制保留**指标依赖字段，避免被相似度排序裁掉。
   - `context_builder.py`：`SchemaContext`（已解析指标、表、字段、Join 路径、业务过滤、粒度、文档证据、Schema 版本、充分性标记），带 Token 预算裁剪与去重。
2. **唯一接入主链路之处**：新增工具 `retrieve_schema_context`（沿用 `InjectedState` 注入方式，与现有 6 个工具协议一致），把 `SchemaContext` 写入 `AgentState`（`TypedDict(total=False)`，加 `query_intent` / `schema_context_v2` / `retrieval_trace` 字段即可向后兼容）。`SQLSkillRequest` 增可选 `schema_context=None`，`SQLSkill.run()` 在构造 `DataItem` 时用它收紧 `database_schema`；**同时保留现有 `trusted_evidence` 文本**，不传时完全走原链路。
3. **可观测**：复用现有 `on_stage` 回调（`graph.py` → `registry.py` → `sql_skill.py` → `TaskManager` → SSE `progress`），新增 `knowledge` / `graph` 阶段名即可，不改 SSE 事件结构与前端协议。
4. **关系图消费**：Phase 3 直接把本轮 `workspace/knowledge/relations.json` 的 152 条 `KnowledgeEdge` 载入 NetworkX（**已安装 3.5，不需新组件**），以指标/字段为种子做限跳扩展、Join 路径搜索与基数风险排序；本轮 `duplicate_aggregation_risk` 与 `cardinality` 就是路径排序的核心特征。

### 8.2 需要先解决的依赖

- **人工审核**：至少确认销售的 `time_field / grain / unit / allowed_dimensions`，否则 `SchemaContext` 里的指标约束仍是不可信的。
- **对照实验设计**（Phase 6 才能出数字）：dev/test 隔离，测试题与标准 SQL 不得进入 `app/few_shot` 索引或 Prompt 调优；BIRD-dev 50 题样本、gold SQL 与集合式 EX 规则保持原样，确保与既有 `38/50=0.76` 可比，且**不得把既有提升归因于新模块**。
- **评测指标**：Table Recall / Column Recall / Complete Schema Recall / 必要中间表召回率 / Join 路径正确率 / 上下文 Token 数 / 检索耗时；端到端仍用 EX 与执行成功率分开统计。

### 8.3 明确不做

不实现 Microsoft GraphRAG 的社区发现与全局摘要（当前图约 13+11+57 个节点、9 条声明关系，局部子图检索才是真实需求）；不引入 Neo4j / 新向量库 / 新 Embedding；不改 LangGraph 的 4 节点 ReAct 结构（除非 Phase 4 明确需要 `context_evaluation` 节点）。

---

## 9. 复现命令清单

```bash
cd <ROOT>
PY=./.venv/Scripts/python.exe

$PY -m app.cli build-knowledge                    # 构建全部企业库
$PY -m app.cli build-knowledge --database enterprise_hr
$PY -m app.cli build-knowledge --validate-only    # 只校验不落盘
$PY -m app.cli build-knowledge --strict           # 待审核知识也会令 CI 返回 1
$PY -m unittest discover -s tests -v              # 153 项
$PY -m compileall -q app runner tests
node tests/js/recovery.test.mjs
$PY -m app.cli check --quiet
```

> 契约状态：`goals/phase0-rag-graphrag/GOAL.md` 为 `PAUSED`（Phase 1 验收通过，等待人工放行 Phase 2）。本轮到此停止。
