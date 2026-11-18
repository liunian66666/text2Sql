# Phase 2 / Phase 3 交付报告：知识增强检索与 GraphRAG 核心闭环

> 本轮是**实际开发 + 真实可复现评测**，不是架构设计文档。
> 依据：`docs/Phase0-架构基线分析.md`、`docs/Phase1-交付报告.md`、
> `docs/数据资产建设-AdventureWorks接入报告.md`、`goals/phase0-rag-graphrag/GOAL.md`。
>
> **性质声明**：AdventureWorks 是微软公开**示例库**（MIT），**不是真实生产企业数据**；
> `docs/simulated-policies/` 13 份制度均为 **SIMULATED / UNREVIEWED**，**不是微软官方业务规则**；
> 本轮**没有运行任何在线 LLM 或在线 embedding**，B/C/D 的向量通道使用仓库内已有的
> **本地 Qwen3-Embedding-0.6B** 真实模型（1024 维），不消耗配额、不是 Mock。

---

## 1. 本轮真正完成的事

| 目标 | 交付 | 状态 |
|---|---|---|
| 知识质量增强 | 71 表 / 486 列数据字典，上游英文描述 452 列，中文语义与别名候选；14 条指标人工审核清单 + 独立审核决定账本 | 已完成 |
| 检索基线 | 指标/表/字段三级索引 595 篇文档；关键词 + 真实本地向量；weighted / RRF 双融合 | 已完成 |
| 双向 Schema 检索 | Table-first / Column-first 融合，database/domain/version 硬隔离 | 已完成 |
| SchemaContext | 12 个必需字段、Token 预算硬保证、来源追溯、可序列化 | 已完成 |
| GraphRAG | networkx MultiDiGraph：90 组外键按完整约束组、复合外键双列、多角色保留；子图扩展 + 多候选 Join 路径评分 + 结构化风险 | 已完成 |
| DeepEye-SQL 接入 | 可选开关 + hint 模式，经现有 `request["evidence"]` 注入，不裁剪 `DataItem.database_schema` | 已完成 |
| 评测 | 12 道 dev 题 A/B/C/D 对照（确定性 + 真实本地向量各一轮），6 道 test 题保持封存 | 已完成 |
| 回归 | **215 项 unittest 全通过**（原 181 + 新增 34） | 已完成 |
| 在线端到端 SQL 生成 | **未完成**，见 §7 与 §10（值索引 + LLM 配额阻塞，未虚构成绩） | 明确阻塞 |

---

## 2. 具体新增与修改的文件

### 2.1 新增（本轮）

| 路径 | 用途 |
|---|---|
| `scripts/build_aw_dictionary.py` | 从已下载的 `provider-schema.json` 生成数据字典，逐项 PRAGMA 校验 |
| `app/enterprise/adventureworks_dictionary.json` | 71 表 / 486 列的英文原文、中文候选、业务别名、逐字段 provenance 与 review_status |
| `scripts/build_aw_metric_review.py` | 生成 14 条指标人工审核清单，14 条参考 SQL 全部真实执行 |
| `app/enterprise/adventureworks_metric_review.json` | 审核工作表（定义/公式/依赖/粒度/时间/过滤/Join/参考 SQL/真实结果/待明确问题） |
| `app/enterprise/adventureworks_review_decisions.json` | **空的**人工审核决定账本，脚本只创建、从不写入 `approved` |
| `app/knowledge/schema_dictionary.py` | `SchemaDictionary` / `TableDescription` / `ColumnDescription`、覆盖率与来源校验 |
| `app/knowledge/review.py` | `ReviewLedger`：只有具名人工 `approved` 决定才能提升 `reviewed` |
| `app/knowledge/retrieval/__init__.py`、`hybrid.py` | 595 篇三级文档、CamelCase/点号前缀/中文别名分词、`HybridIndex`、`LocalSentenceTransformerBackend`、weighted/RRF |
| `app/knowledge/retrieval/schema_retriever.py` | Table-first / Column-first 双向召回融合、指标扩展、可信边界 |
| `app/knowledge/retrieval/context_builder.py` | `SchemaContext` + `ContextBudget` + hint 渲染与硬预算紧凑渲染 |
| `app/knowledge/graph_store.py` | NetworkX `MultiDiGraph`，90 个 FK 约束组为关系单位 |
| `app/knowledge/retrieval/subgraph.py` | 指标驱动 BFS + 聚焦路径补全 + 有限扩展与回退 |
| `app/knowledge/retrieval/join_path.py` | 多候选 Join 路径评分、歧义保留、5 类真实场景结构化风险 |
| `app/knowledge/enhancement.py` | 编排、可选开关、失败降级、`on_stage` 可观测性事件 |
| `app/commands/evaluate_graphrag.py` | 第 10 个 CLI 子命令 `evaluate-graphrag`，A/B/C/D 对照与封存测试集保护 |
| `tests/test_knowledge_dictionary_review.py`（9 项） | 字典覆盖率、来源区分、审核账本边界 |
| `tests/test_graphrag_retrieval.py`（12 项） | 三级索引、分词、隔离、双向召回、SchemaContext |
| `tests/test_graphrag_acceptance.py`（13 项） | 10 个必须通过的验收案例 + 3 项运行时集成 |
| `workspace/evals/graphrag-abcd.json` | 确定性关键词 A/B/C/D 真实结果（48 条 case×config 明细） |
| `workspace/evals/graphrag-abcd-local.json` | **真实本地模型** A/B/C/D 结果（48 条明细） |
| `docs/Phase2-3-交付报告.md` | 本报告 |

### 2.2 修改

| 路径 | 改动 | 兼容性 |
|---|---|---|
| `app/agent/tools/registry.py` | `_run_text_to_sql` 增加可选知识增强分支（默认关闭）；发送 `knowledge_enhancement` SSE 事件 | OFF 时 evidence 逐字节不变 |
| `app/cli.py` | 注册 `evaluate-graphrag` | 原 9 个子命令行为不变 |
| `app/commands/__init__.py` | 命令模块清单 +1 | 不变 |
| `tests/test_cli.py` | 严格全等子命令集合 9→10 | **未降低断言强度** |
| `goals/phase0-rag-graphrag/GOAL.md` | `iteration=4`、Phase 2/3 验收表与证据 | 契约状态 |

### 2.3 明确未动

`business_metrics.json`、`adventureworks_metrics.json`（38 条原指标文件）、`evals/metric-rag/*`、
`evals/bird-dev-50/*`、`evals/agent/*`、`evals/adventureworks-graphrag/dev.json` 与 `test.json`、
`data/enterprise_*`、`data/bird`、`app/skills/*`、`app/pipeline/*`、`app/db_utils/*`、
`app/prompt/*`、`app/api/*`、`app/web/server.py`、`app/web/preflight.py`、
`app/agent/state.py`、`app/enterprise/knowledge.py`、`.env`、`pyproject.toml`、任何依赖。

`git diff --stat`：仅 5 个既有文件 `75 insertions / 13 deletions`；其余全部为新增文件。

---

## 3. 数据字典覆盖率（真实统计）

| 项 | 数量 | 覆盖率 |
|---|---|---|
| 表总数 / 上游英文描述 | 71 / **71** | **100.00%** |
| 列总数 / 上游英文描述 | 486 / **452** | **93.00%** |
| 英文描述缺失（保留 `UNKNOWN`） | 34 | 7.00% |
| 中文语义候选（表 / 列） | 31 / 60 | 按 表+列 显式枚举，非字段名批量生成 |
| 业务别名候选（表 / 列） | 31 / 60 | 同上 |
| **官方已审核描述** | **0** | 无 |

来源与可信边界：

- 英文描述逐字取自 `data/adventureworks/provider-schema.json` 的 `description`，该元数据锁定官方
  commit `beaab06ef72831089ca80e5355d65e661fd19b26` 与 `oltp-install-script/instawdb.sql`；
  **未逐条与 SQL Server 扩展属性 `MS_Description` 原文比对**，故标 `upstream_description`，
  不冒充 `official_reviewed`。
- 中文语义与别名标 `origin=model_candidate` + `review_status=unreviewed`，
  **不是微软官方文本、不是人工审核结论**；`SchemaDictionary.validate()` 会拒绝任何把中文标为官方来源的记录。
- 类型/主键标 `database_fact`（SQLite PRAGMA 事实）；34 列无可靠描述一律 `UNKNOWN`，不臆造。
- 生成器对每个表名/列名与真实 SQLite 做集合比对，不一致直接 `SystemExit`。

---

## 4. 可信指标与候选指标（数量与机制）

| 类别 | 数量 |
|---|---|
| AdventureWorks 结构化指标总数 | 38 |
| **可信指标 `trusted=True`** | **0** |
| 待审核候选指标 `pending_review` | **38** |
| 进入人工审核工作表的候选 | 14 |
| 人工审核决定账本中的 `approved` 记录 | **0** |

机制：`app/enterprise/adventureworks_metric_review.json`（工作表）与
`app/enterprise/adventureworks_review_decisions.json`（决定账本）是**两个不同文件**。
生成器只写工作表，绝不写决定文件；`ReviewLedger.apply()` 只接受
`decision="approved"` 且 `reviewer` 为具名人工（拒绝 `system/auto/model/script/unknown`）且
`metric_id` 存在于该库的显式记录。即便被批准，`trusted = reviewed AND not unknown_fields`，
当前 38 条全部含 `unknown_fields`，因此 `trusted` 仍为 0 —— **没有任何指标能成为硬 SQL 约束**。

审核工作表明确区分了三种金额口径：`SubTotal`（订单头小计，不含税运费）、
`TotalDue`（SubTotal+TaxAmt+Freight，订单级）、`LineTotal`（明细行净销售额，明细级）；
14 条参考 SQL 全部经 `execute_readonly_table` 真实执行并记录 columns/rows。
「商品分类销售额」当前**没有独立指标定义**，被列入待人工决定的扩展口径问题，未编造成已有指标。

---

## 5. 核心调用链路

```
用户问题
  → ToolRegistry._run_text_to_sql（app/agent/tools/registry.py）
      ├─ trusted_evidence / user_hint / AW 方言 hint（原有逻辑，全部保留）
      └─ [可选 ENABLE_KNOWLEDGE_ENHANCEMENT=1 且 database_id=adventureworks]
           build_enhancement（app/knowledge/enhancement.py）
             ① MetricRegistry + ReviewLedger → 38 条指标（trusted=0）
             ② DatabaseSchema（schema_adventureworks.json）+ SchemaDictionary
             ③ GraphStore.from_artifacts → networkx.MultiDiGraph（90 FK 组 / 690 知识边）
             ④ HybridIndex（595 文档，keyword + 可选真实本地向量）
             ⑤ SchemaRetriever：Table-first ∪ Column-first + 指标依赖扩展
             ⑥ SubgraphExpander：BFS(max_hops=4) + 聚焦路径补全 + 有限扩展/回退
             ⑦ JoinPathFinder：多候选路径评分 + 歧义 + 5 类结构化风险
             ⑧ build_context → SchemaContext → to_evidence_text()（hint）
             ⑨ on_stage 发 knowledge_retrieval started/completed 与 knowledge_enhancement 事件
  → request["evidence"] 追加提示文本
  → SQLSkill.run → DataItem.evidence → PromptFactory 的 HINT
  → DeepEye-SQL 六阶段（Value Retrieval / Few-shot / Schema Linking / SQL Generation /
     SQL Revision / SQL Selection）与只读沙箱 —— 全部未改
```

关键设计：**不裁剪 `DataItem.database_schema`**。调研证实上游
`filter_used_database_schema`（`app/db_utils/schema.py:563`）会丢弃顶层/表级自定义字段，
`get_database_schema_profile`（`:344`）只渲染固定列字段；把结构化结果塞进 schema dict 不会进 prompt。
因此采用现有 `request["evidence"]` 字符串作为唯一注入通道，保证 Value Retrieval、Few-shot、
Schema Linking、SQL Revision 仍拿到完整原始 Schema。

`KNOWLEDGE_ENHANCEMENT_MODE` 只接受 `hint`；`hard_filter` 模式**未启用**，理由与验证清单见 §10。
开关默认关闭，且只对 `adventureworks` 生效，`enterprise_sales` / `enterprise_hr` 即使开关打开也完全不受影响。

---

## 6. GraphRAG 实际检索效果（12 道 dev 题）

### 6.1 结构校验（真实 PRAGMA，不是题目准确率）

| 项 | 值 |
|---|---|
| FK 约束组 / 字段对 | **90 / 91**（唯一复合外键 2 列） |
| 复合外键 | `Sales.SalesOrderDetail(SpecialOfferID, ProductID) → Sales.SpecialOfferProduct`，双列完整保留 |
| 复合外键完整率 | **1.0**（1/1 组） |
| 同表对多外键角色 | 4 对（如 `BillOfMaterials → Product`、`SalesOrderHeader → Address`） |
| 图中 FK 边按约束组为单位 | `groups_preserved_as_units=True`（90 条，未退化为 91 条独立字段边） |
| 关系方向 / 基数 / 来源 / 审核状态 | 全部保留在边属性，反向遍历边显式 `reverse=True` |

dev.json 12 题中没有直接引用该复合外键的题目，因此 **case 级 `composite_fk_complete` 为 null**；
上表是结构校验值，不冒充题目成绩。复合外键相关行为由验收案例 2 与集成测试断言。

### 6.2 真实本地模型对照（`workspace/evals/graphrag-abcd-local.json`）

| 指标 | A 关键词 | B +真实本地向量 | C +指标扩展 | D +GraphRAG |
|---|---|---|---|---|
| Table Recall | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| Column Recall | 0.7609 | **0.8909** | 0.8909 | 0.8909 |
| Complete Schema Recall | 0.2500 | **0.5833** | 0.5833 | 0.5833 |
| 必要中间表召回率 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| **Join 路径正确率** | 0.0000 | 0.0000 | 0.0000 | **0.9286** |
| 候选表数（均值） | 9.58 | 10.00 | 10.00 | 12.50 |
| 候选字段数（均值） | 28.75 | 50.00 | 50.00 | 60.00 |
| 上下文 Token（均值） | 321.4 | 505.4 | 506.6 | 664.0 |
| Join 路径数 | 0 | 0 | 0 | 8.0 |
| 单题检索耗时（均值，秒） | 0.039 | 9.341* | 0.256 | 0.255 |
| 实际模式 | keyword | **hybrid（真实模型）** | hybrid | hybrid |

\* B 首题含 595 篇语料的懒加载向量化；语料向量在全部 48 个 case 间共享缓存，之后约 0.25 秒/题。
本地模型：`data/models/Qwen/Qwen3-Embedding-0___6B`，1024 维，首次加载约 54 秒，
整轮评测 2 分 23 秒，**零在线 API 调用、零配额消耗**。

### 6.3 确定性关键词对照（`workspace/evals/graphrag-abcd.json`）

`--vector-mode none` 时 `vector_status = "not_run: deterministic keyword only; no mock vectors"`，
B 明确退化为 keyword，不把任何 Mock 当模型成绩：

| 指标 | A / B | C | D |
|---|---|---|---|
| Column Recall | 0.7609 | **0.8304** | 0.8304 |
| Complete Schema Recall | 0.2500 | **0.4167** | 0.4167 |
| Join 路径正确率 | 0 | 0 | **0.9286** |
| 上下文 Token | 321.4 | 360.8 | 690.3 |

### 6.4 如何解读（不夸大）

1. **Table Recall 在 12 题上全部配置都是 1.0000** —— 关键词 + 中文字典已足以召回表，
   该指标在本题集上**已饱和**，不能用来宣称 GraphRAG 提升表召回。
2. 真实收益在两处：
   - A→B 的**真实本地向量**让 Column Recall +0.1300、Complete Schema Recall +0.3333；
   - D 让 **Join 路径正确率从 0 → 0.9286**，并输出结构化业务风险（重复聚合、历史区间、粒度冲突）、
     必要中间表与歧义 —— 这正是本轮核心验收目标：补齐**完整关联路径与业务语义**，而非只建静态图谱 JSON。
3. C 在真实向量模式下与 B 相同（列预算已被 50 列打满，指标扩展未带来额外 required 列）；
   在确定性模式下 C 相对 A/B 有 +0.0694 Column Recall / +0.1667 Complete Schema Recall。如实记录。
4. 成本：D 的上下文 Token 是 A 的约 2.07 倍，候选表 9.58→12.50。收益与成本并列给出。
5. D 有 1 道题触发 `fallback_triggered`（目标在限跳内不可达，保留 BFS 前沿而非静默丢弃）。
6. 融合策略默认沿用 `EnterpriseKnowledgeBase` 的加权求和；RRF 已实现为可选，但**未在 dev 集上证明优于加权，故未替换默认实现**。
7. 关系图不声称解决所有 SQL 语义错误：本阶段只输出结构化风险与约束提示，未做 AST 校验。
8. 6 题 `test.json` 完全未参与：CLI 默认拒绝 `--cases test.json`（需 `--allow-test-split` 显式放行），
   结果文件 `isolation.test_split_used=false`。不把 6 题包装成整体准确率。

---

## 7. 在线运行基线与资源估算（只读调研，未擅自构建）

| 问题 | 真实结论 | 证据 |
|---|---|---|
| 索引入口 | `app/commands/build_index.py`，CLI `build-index --config/--reuse-legacy-columns/--database` | `build_index.py:21-24` |
| 处理哪些表/字段 | **全库 71 表、全部 TEXT/VARCHAR/CHAR 列**，类型白名单 + `max_value_length=100` + UUID/纯数字启发式排除 | `vector_db.py:35-37`、`build_index.py:45-70` |
| **真实规模** | **231 个文本列有值、571,985 个 DISTINCT 值**（保守上界，未模拟 UUID/数字过滤）；按 DashScope 每请求 10 条 → **约 57,199 次在线 embedding 请求** | 只读 SQL 实跑 2.29 秒 |
| 是否支持只索引有查询价值的字段 | **不支持**：无 `--tables/--columns/--fields` 参数 | `build_index.py:21-24` |
| 本地 embedding 模型 | **可用**：`data/models/Qwen/Qwen3-Embedding-0___6B`（1024 维），`sentence-transformers` / `torch` 已装；模板配置默认是在线 DashScope `qwen3.7-text-embedding-flash` | `vector_db.py:62-100`、`config/template/app-fast/config.toml:51-55` |
| BLOB / 敏感信息规避 | BLOB（7 列）与 `DECIMAL_TEXT`（48 列）通过类型白名单间接排除；**但 `Person.EmailAddress`、`Person.Password.PasswordHash/PasswordSalt` 等 PII/无查询价值字段会被现有构建器写入向量库**（Top10 文本列中 6 个是 rowguid/邮箱/密码散列）。现有代码**没有 PII 过滤** | 实跑统计 Top10 + `build_index.py:45-70` |
| 索引未建立时的降级 | **无合法降级**：`SQLSkill.run` 检查 `local_index/manifest.json` 后抛 `FileNotFoundError`；`ValueRetrievalRunner._get_local_value_index` 同样 fail-fast。仅 Spider2 数据集类型可跳过值检索；单列缺失返回空值属局部降级 | `sql_skill.py:241-250`、`value_retrieval.py:169-173`、`local_index.py:176-183` |
| `ready` 自检 | `preflight.py:31-35` 严格判定 manifest 是否存在，无绕过开关；AW 默认关闭故 `check=ready` | `preflight.py:19-56` |

**资源判断**：全库在线 embedding 约 5.7 万次请求、无预算上限，本轮**未执行**；改用本地模型
（57 万值按本轮 595 文档约 110 秒线性外推需数十小时 CPU）亦不现实。
因此未运行 `build-index`、未伪造索引产物、未关闭任何安全检查。
若要打通在线链路，建议先增加 `--tables/--columns` 白名单 + PII 字段排除，再对 3–5 张核心表的
少量高价值列做小范围索引，并给出明确请求上限。

### 端到端 SQL 生成是否真正跑通

**没有。** 阻塞是值索引缺失（fail-fast）+ LLM 在线配额未获批准。本轮**未运行**
DeepEye-SQL 六阶段在线生成、LangGraph 真实推理，也没有产生任何新的 SQL 可执行率、
执行准确率、Token 或延迟数字。已完成的是离线可验证部分：SchemaContext 能补齐
`required_tables`、完整 Join 字段与业务风险；26 条参考 SQL 仍在只读沙箱真实执行通过；
`SchemaService.load_sqlite_schema` 加载 71 表、`build_agent_graph` 编译为 `CompiledStateGraph`。
不把 BIRD 既有 72%→76% 或指标 RAG 1.0000 归因于本轮模块。

---

## 8. 10 个必须通过的验收案例

| # | 案例 | 结果 | 测试 |
|---|---|---|---|
| 1 | 只给 Product / ProductCategory → 自动补 ProductSubcategory | PASS（2 跳，唯一中间表） | `test_01_*` |
| 2 | 复合外键完整保留双列 | PASS（`SpecialOfferID` + `ProductID`；90 组 / 91 列对 / 完整率 1.0；单列关联风险可检出） | `test_02_*` |
| 3 | 订单头 SubTotal × 明细 → 重复聚合风险 | PASS（输出 23.6× 放大与缓解约束） | `test_03_*` |
| 4 | 历史成本 × 历史价格 → 缺区间条件风险 | PASS（649 行膨胀、区间提示有无可区分） | `test_04_*` |
| 5 | 低于再订货点 → 商品级聚合约束 | PASS（`GROUP BY ProductID`） | `test_05_*` |
| 6 | 多候选路径语义不明确 → 返回歧义 | PASS（4 对多角色表 + role/near_tie 歧义） | `test_06_*` |
| 7 | 遗漏必要字段可扩展 / 不可达可回退 | PASS（单种子扩展补中间表；限跳不可达 `fallback_triggered=True`） | `test_07_*` |
| 8 | GraphRAG ON/OFF 均可运行，Sales/HR 不受影响 | PASS（OFF evidence 不变；ON 保留 trusted_evidence 与 AW 方言；HR/Sales 不注入） | `test_08_*` |
| 9 | 原 181 项测试保持通过 + 新增独立测试 | PASS（**215 项 OK**，新增 34 项；CLI 严格全等断言 9→10） | 全量回归 |
| 10 | 真实可复现检索对照结果 | PASS（两份结果文件、48 条明细、同一 case 重复运行指标逐项一致） | `test_10_*` |

---

## 9. 测试结果与资产保护

| 命令 | 真实输出 |
|---|---|
| `unittest discover -s tests -q` | **Ran 215 tests / OK**（181 基线 + 34 新增） |
| `compileall -q app runner tests scripts` | exit 0 |
| `node tests/js/recovery.test.mjs` | `ok 55 assertions` |
| `app.cli check --quiet` | `ready` |
| `evaluate-rag --validate-only` | `valid=true, case_count=15` |
| `evaluate --validate-only` | `valid=true, case_count=14` |
| `build-knowledge --validate-only --quiet` | 指纹 **`96f7f2cb…a58`** 不变（13 指标 / 11 表 / 57 列 / 152 边 / trusted=0） |
| `evaluate-graphrag --validate-only` | `split=dev, case_count=12` |
| `evaluate-graphrag --vector-mode none` | 48 条结果，`vector_status=not_run` |
| `evaluate-graphrag --vector-mode local` | 48 条结果，`vector_status=real_local_model`（2 分 23 秒） |
| `git diff --stat` | 仅 5 个既有文件 `75+/13-`，其余全为新增 |

历史评测产物 mtime 未变，BIRD 50 题、指标 RAG 15 题、Agent 14 题均未被重写；
`business_metrics.json` 与 38 条 AW 指标文件未被批量批准；未新增任何依赖。

---

## 10. 限制、阻塞与下一阶段

### 本轮明确未做

1. **在线端到端 SQL 生成未跑通**（值索引 + LLM 配额），见 §7。
2. **硬裁剪模式（`hard_filter`）未启用**：上游 `filter_used_database_schema` 会丢自定义字段、
   `get_database_schema_profile` 只渲染固定字段；硬裁剪还可能导致 Value Retrieval / Few-shot /
   Schema Linking / SQL Revision 缺少必要字段。本轮按「优先实现安全模式」只交付 `hint`，
   `KNOWLEDGE_ENHANCEMENT_MODE` 遇到 `hard_filter` 会明确拒绝而不是静默裁剪。
   若后续启用，需独立可选模式 + 候选范围扩大 + 必要字段补齐 + 回退完整 Schema + 四阶段字段完备性验证，
   并用同一 dev 集做 hint vs hard_filter 收益对照。
3. 未做文档 RAG 联合检索、增量更新/版本联动、SQL AST 语义校验、跨查询一致性验证。
4. 未新增 `sqlglot`、`pytest`、`sqlparse`；未引入 Neo4j 或任何新中间件。
5. `Table Recall` 在 12 题上饱和，因此不能据此宣称 GraphRAG 提升表召回；结论限定在关联路径与业务语义。
6. 中文语义/别名是 `model_candidate`，未经人工审核，不计入官方覆盖率。

### 需你确认后才能继续

1. 是否批准为 AW 的**少量高价值字段**构建值索引（需先加 `--columns` 白名单 + PII 排除并设定请求上限）。
2. 是否批准在线 LLM 配额跑 3–5 题端到端对照（Baseline vs Enhanced），记录真实 SQL/Token/延迟。
3. 哪些 14 条审核清单中的指标可由**具名人工**批准（决定账本目前为空）。
4. 是否放行 `hard_filter` 模式的开发与对照实验。
5. 是否批准 Phase 6 在线对照/消融评测；6 题封存测试集在此之前不解封。

**本轮到此停止，未开始 Phase 4，未运行在线模型评测。**
