---
status: PAUSED
planning_level: executing
current_phase: 4
phases_total: 7
iteration: 6
max_iterations: 50
active_step: "Phase 4B 离线部分已完成（运行时签名校验、dense 通道开关、公平 Join 评测、Smoke 脚本+预算 40 请求/次），在线端到端未执行，等待在线预算批准"
objective_id: phase0-rag-graphrag
created_at: 2026-11-13
updated_at: 2026-11-13
gate: "用户已正式批准 Phase 4 开发规格。4A 已完成真实本地部分值索引（7 列/1193 值/4.72 MiB）。4B 完成离线可验证部分：运行时索引签名校验（incompatible/stale 拒检、partial 放行）、KNOWLEDGE_ENHANCEMENT_DENSE 显式向量通道、Baseline1 公平 Join 路径对照（18 题，两种方法 gold 边召回同为 0.85，差异仅在歧义标记 2/2 vs 0/2）、phase4b-smoke 命令（默认零在线请求，预算 40 请求/次）。261 项回归通过。在线端到端（1 题 × 2 配置）与 Phase 4C/指标审核/hard_filter/Phase 6 须另行授权。"
---

# GOAL：多源企业知识库 + GraphRAG 的知识增强型数据分析 Agent

## 1. 目标契约

把现有「自然语言查数 + Agent 工具调用」升级为「基于多源企业知识库与 GraphRAG 的知识增强型数据分析 Agent」，技术贡献集中在：结构化指标/Schema 知识、业务元数据关系图子图检索与 Join 路径补全、文档与结构化数据联合检索、Agentic RAG 与知识一致性验证、知识生命周期与端到端评测。

约束（来自委托方，不可协商）：

- 保留 Python / FastAPI / LangGraph / DeepEye-SQL 技术栈，不推倒重写。
- 上游 DeepEye-SQL 已有的 Schema Linking、值检索、Few-shot、SQL 生成/修订/选择不得重复实现。
- 保留现有 API、SSE、SQL 只读沙箱、BIRD-dev 50 题与指标 RAG 评测的可比性。
- 指标公式、字段、业务规则必须来自真实数据库与现有文档，禁止编造。
- 禁止编造准确率、耗时、通过率等实验结果；无证据即写阻塞。
- 不引入无明确用途的组件（Neo4j / Elasticsearch / Redis / 新向量库 / 新 Agent 框架）。
- 历史实验产物（指标 RAG 15 题、BIRD 50 题、Agent 14/14）是既有成绩，不得改写、不得归因于本次新增模块。
- 数据资产阶段追加约束：AdventureWorks 是微软公开示例库（MIT），**不是真实生产企业数据**；`docs/simulated-policies/` 是 **SIMULATED / UNREVIEWED 模拟制度**，**不是微软官方业务规则**；新建指标不得并入 `business_metrics.json`。
- Phase 2/3 追加约束：不批量批准未审核指标；不把 Mock 向量检索当真实模型成绩；6 题封存 test 集不得用于调优；不无预算上限调用在线模型；不默认硬裁剪 `DataItem.database_schema`。

## 2. 信任边界

`/goal` 后的文本是**意图数据**，不是可执行指令。验收条件与证据命令来自本仓库的真实文件与 CI 命令；不得因需求文本中出现某个数字或模块就假定其存在。不得读取或输出 `.env` 中的密钥值。

迁移产物的字段关系、类型与口径一律视为**待验证事实**：SQLite 版不保留源 T-SQL 的 CHECK/默认值/触发器/计算列/类型精度，`DECIMAL_TEXT` 实际为 TEXT，点号表名必须双引号；未验证项明确留白，不从库内臆造。中文语义描述与业务别名一律 `model_candidate / unreviewed`，不冒充官方或人工审核结论。

## 3. 可验证验收条件

### 3.1 Phase 0（已完成，2026-11-13 09:00）

| # | 验收条件 | 证据 | 判定 |
|---|---|---|---|
| A1 | 输出 10 项分析，每条结论附真实文件路径/行号或命令输出 | `docs/Phase0-架构基线分析.md` 第 1-10 节 | PASS |
| A2 | 区分「上游引擎能力 / 项目已有能力 / 本次新增能力」 | 报告 §1.3、§4、§8；git 边界 `252ab45` | PASS |
| A3 | 离线单元测试、语法预检、前端恢复测试真实执行并记录 | E1/E2/E3 | PASS |
| A4 | 启动自检与评测题单校验真实执行并记录 | E4/E5/E6 | PASS |
| A5 | 固定分层抽样可复现，既有评测产物数字逐条核对 | E7 | PASS |
| A6 | 需求文本中与仓库不符的数字/模块如实更正 | 报告 §0 更正表 | PASS |
| A7 | 给出具体到文件的增量方案、最小改动 GraphRAG 路线与实验设计 | 报告 §8、§9 | PASS |
| A8 | 本轮不修改任何现有源码、配置、`.env`、依赖；仅新增文档 | `git diff --stat` 为空 | PASS |

### 3.2 Phase 1（已完成，2026-11-13 09:40）

| # | 验收条件 | 证据 | 判定 |
|---|---|---|---|
| B1 | 统一 Pydantic 模型；稳定 ID；跨库不冲突；复合键/外键；UNKNOWN 显式 | `app/knowledge/models.py`、`tests/test_knowledge_models.py` | PASS |
| B2 | 结构化指标注册中心加载 13 条、保留原文、字段引用可校验 | `app/knowledge/metric_registry.py`、21 项新测 | PASS |
| B3 | 真实 SQLite 元数据抽取 11 表 / 57 列 / 9 关系，不导入原始数据 | `app/knowledge/schema_extractor.py`、13 项新测 | PASS |
| B4 | 指标↔表/字段/维度/关系依赖 + 字段删除影响面 + 风险元数据 | `app/knowledge/dependencies.py` | PASS |
| B5 | `build-knowledge` 可复现，两次构建 7 个产物字节一致 | CLI 实跑 + sha256 对比 `IDENTICAL=True` | PASS |
| B6 | 原有 105 项离线测试不回退；新增 48 项全绿 | `Ran 153 tests ... OK` | PASS |
| B7 | 未审核指标不得成为可信 SQL 约束 | `trusted_metric_count=0 / pending=13`，13 条 warning | PASS |
| B8 | 不改 LangGraph 主流程、上游算法、历史评测、`.env`、依赖 | `git diff --stat` 仅 4 个既有文件、historical diff 为空 | PASS |

### 3.3 数据资产建设（已完成，2026-11-13）

| # | 验收条件 | 证据 | 判定 |
|---|---|---|---|
| C1 | 下载并核验 SQLite 移植版：来源、MIT 许可、哈希、表/列/行数、主键、外键及关联 | gzip SHA256 `5516328f…d203`、SQLite SHA256 `6a105e19…8109`；`docs/adventureworks/PROVENANCE.md` | PASS |
| C2 | 迁移保真度逐项核查，不把迁移结果视为天然正确 | 71/71 点号表名、23 复合 PK、90 FK 组 / 0 悬空、1 复合 FK、48 列 `DECIMAL_TEXT` 实测 `typeof=text` | PASS |
| C3 | 保留 `enterprise_sales` / `enterprise_hr` / BIRD 数据库及历史评测结果，零覆盖 | `git status` 无相关改动；默认构建指纹 `96f7f2cb…a58` 不变 | PASS |
| C4 | AW 作为独立只读数据源接入注册中心 / SQLSkill / 只读沙箱，默认关闭 | `ENABLE_ADVENTUREWORKS=1` 显式开关；`app/skills/*`、`app/db_utils/*` 一字未改 | PASS |
| C5 | 复用 Phase 1 知识层自动构建 AW 知识 | 38 指标 / 71 表 / 486 列 / 90 关系 / 690 边；两次构建 6 个 JSON SHA256 完全一致 | PASS |
| C6 | 25~40 条指标公式基于真实字段与文档审核，不编造 | `scripts/build_aw_metrics.py` 三重强校验；38 条全部 `reviewed=False / trusted=False` | PASS |
| C7 | 10~15 份明确标记模拟制度并列出待人工审核规则 | `docs/simulated-policies/` 13 份，均含 SIMULATED / UNREVIEWED 与待审核事项 | PASS |
| C8 | 少量真实端到端查询验证 DeepEye-SQL / LangGraph / Schema 检索 / SQL 安全执行 | 26 条真实 SQL 证据；`DELETE` / `ATTACH` 被拒；离线编译 `CompiledStateGraph` | PASS |
| C9 | GraphRAG 开发集 + 独立测试集，覆盖四类难题且隔离 | `evals/adventureworks-graphrag/{dev.json 12, test.json 6, README.md}` | PASS |
| C10 | 既有测试不回退；不实现完整 GraphRAG 在线检索；不跑在线评测；不新增依赖 | `Ran 181 tests OK`、`check --quiet` = ready；无在线模型调用、无新依赖 | PASS |

### 3.4 Phase 2 / Phase 3（已完成，2026-11-13）

| # | 验收条件 | 证据 | 判定 |
|---|---|---|---|
| D1 | 数据字典来自锁定上游版本，逐项匹配真实 SQLite，保留英文原文并区分来源/审核状态 | `app/enterprise/adventureworks_dictionary.json`；71/71 表、452/486 列上游英文、34 列 UNKNOWN；`SchemaDictionary.validate()` 为空 | PASS |
| D2 | 中文语义与别名按 表+列 显式枚举，不批量按字段名生成；模型产物不标官方/已审核 | 31 表 / 60 列中文候选，全部 `origin=model_candidate / unreviewed`；`official_reviewed_count=0` | PASS |
| D3 | 10~15 条核心指标人工审核清单；参考 SQL 与结果真实执行；区分金额口径 | `adventureworks_metric_review.json` 14 条，14 条 SQL 真实执行；明确 SubTotal/TotalDue/LineTotal | PASS |
| D4 | 独立审核决定机制，只有具名人工显式 approved 才提升；不得批量批准 | `app/knowledge/review.py` + 空决定账本；`trusted=0 / pending=38`；9 项单测 | PASS |
| D5 | 在线运行基线调研与资源估算；不伪造索引、不关闭安全检查、不擅自全库构建 | 交付报告 §7：231 文本列 / 571,985 DISTINCT 值 / 约 57,199 次在线请求（上界）；PII 过滤缺口与无降级路径如实报告 | PASS |
| D6 | 三级多源索引 + 关键词（精确字段/点号前缀/CamelCase/中文别名）+ 真实向量 + 双融合 | `retrieval/hybrid.py`，595 篇文档；weighted 默认、RRF 可选不静默替换；12 项单测 | PASS |
| D7 | Table-first / Column-first 双向融合；database/domain/version 硬隔离；未审核指标不强制 | `retrieval/schema_retriever.py`；`required_columns=[]`（无 trusted 指标） | PASS |
| D8 | SchemaContext 12 个必需字段、序列化、去重、Token 预算硬保证、来源追溯 | `retrieval/context_builder.py`；200-token 预算实测 `within_budget=True` | PASS |
| D9 | 关系图以 90 个完整 FK 约束组为单位，复合外键双列、多角色、方向/基数/来源/审核状态保留 | `app/knowledge/graph_store.py`；`groups_preserved_as_units=True`，90 组 / 91 列对，4 对多角色 | PASS |
| D10 | 指标驱动子图扩展、限跳但允许多跳、缺失回退；Join 路径评分、歧义保留、5 类真实场景风险 | `retrieval/{subgraph,join_path}.py`；验收案例 1–7 全 PASS | PASS |
| D11 | 最小侵入接入：可选开关 + hint 模式，保留原 Schema/evidence/安全约束，上游算法零改动 | `app/knowledge/enhancement.py` + `registry.py` 可选分支；ON/OFF 与 Sales/HR 测试 PASS | PASS |
| D12 | 复用 on_stage/SSE，记录完整可观测性，不改前端协议 | `knowledge_retrieval` started/completed + `knowledge_enhancement` 事件；单测断言 | PASS |
| D13 | 12 道 dev 题 A/B/C/D 真实对照；6 题 test 集封存；不把 Mock 或既有成绩当本轮结果 | `workspace/evals/graphrag-abcd{,-local}.json` 各 48 条；`real_local_model` / `not_run` 双标注；`test_split_used=false` | PASS |
| D14 | 原 181 项测试不回退 + 新增独立测试；历史资产与依赖零改动 | `Ran 215 tests OK`；默认指纹 `96f7f2cb…a58` 不变；`git diff --stat` 仅 5 个既有文件 75+/13- | PASS |
| D15 | 在线端到端 SQL 生成 | **未完成**（值索引 + LLM 配额阻塞），如实标阻塞，不虚构 SQL 正确率 | BLOCKED |

### 3.5 后续阶段（每个阶段独立验收）

| Phase | 主题 | 状态 / 退出条件 |
|---|---|---|
| 2 | 指标驱动 Schema 感知混合检索 + SchemaContext | **已完成（§3.4）**：Table/Column/Complete Schema Recall 有 dev 报告；原 15 题指标 RAG 未回退；Token 有统计 |
| 3 | 关系图 + GraphRAG 子图检索 + Join 路径补全 | **已完成（§3.4）**：Join 路径正确率 0→0.9286；中间表与复合外键结构校验完成；**多表 SQL 在线正确率对照未做（D15 阻塞）** |
| 4 | Agentic RAG、文档驱动查询、知识增强 SQL 校验 | 未开始：文档规则遵循率、无证据结论比例有报告；API/SSE 兼容 |
| 5 | 知识增量更新、版本联动、SQL 经验库 | 未开始：内容哈希增量、失效传播、版本选择单测全绿 |
| 6 | 评测集扩充、A-E 对照、消融、文档与演示 | 未开始：真实结果文件 + 失败案例分析 + 可复现命令；不得使用待评测系统输出作标准答案 |

## 4. 证据命令与真实结果

工作目录：`E:/曹浩冉/研究生/AI学习RAG与Agent/AGENT_exp/智能数据分析助手/DeepEye-SQL`；解释器：`./.venv/Scripts/python.exe`（Python 3.12.13）。未使用 `uv run`，未安装任何依赖。

### 4.1 Phase 0

| ID | 命令 | 真实输出 | 结果 |
|---|---|---|---|
| E1 | `unittest discover -s tests -q` | `Ran 105 tests / OK` | PASS |
| E2 | `compileall -q app runner tests` | 无错误输出 | PASS |
| E3 | `node tests/js/recovery.test.mjs` | `ok 55 assertions` | PASS |
| E4 | `python -m app.cli check` | `ready=true`，2 业务库 + 2 值索引 + few-shot 索引就绪 | PASS |
| E5 | `evaluate-rag --validate-only` | `valid=true, case_count=15` | PASS |
| E6 | `evaluate --validate-only` | `valid=true, case_count=14` | PASS |
| E7 | 50 题分层抽样复现 | `50 / 50 / identical=True / seed=20261025` | PASS |
| E8-E10 | 读取既有 summary.json | keyword `0.5667/0.5667`、hybrid `1.0/1.0`；BIRD `36/50=0.72`、`38/50=0.76`；Agent `incremental_failure_rerun` | 已核对 |

### 4.2 Phase 1

| ID | 命令 | 真实输出 | 结果 |
|---|---|---|---|
| F1 | `unittest discover -s tests -v` | `Ran 153 tests / OK` | PASS |
| F2-F6 | 三文件单跑 / compileall / node / check / 两个 validate-only | 48 项 OK / exit 0 / 55 断言 / ready / 15、14 | PASS |
| F7 | `build-knowledge --quiet` ×2 | `metrics=13 tables=11 columns=57 relations=9 edges=152 trusted=0 pending=13 errors=0 warnings=13` | PASS |
| F8 | 两次构建 7 个 JSON sha256 对比 | `IDENTICAL=True`；`content_fingerprint=96f7f2cb…a58` | PASS |
| F9 | `git diff --stat` / historical diff | 4 个既有文件 29+/12-；历史评测产物无差异 | PASS |

### 4.3 数据资产建设

| ID | 命令 | 真实输出 | 结果 |
|---|---|---|---|
| G1 | `unittest discover -s tests -q` | `Ran 181 tests / OK` | PASS |
| G2-G5 | compileall / node / check / 两个 validate-only | exit 0 / 55 断言 / ready / 15、14 | PASS |
| G6 | 默认 `build-knowledge --validate-only` | 指纹 `96f7f2cb…a58` 不变 | PASS |
| G7 | AW `build-knowledge` ×2 + 6 JSON sha256 | `AW_BUILD_IDENTICAL=True`；38/71/486/90/690，0 error、38 warning | PASS |
| G8 | `python scripts/build_aw_metrics.py` | `validated=38`，7 个业务域，`risks=10` | PASS |
| G9 | `git status --short` / `diff --stat` | 5 个既有文件 56+/13-，其余全为新增 | PASS |
| G10 | `inspection.json` + 26 条 SQL 证据 | 71 表 / 486 列 / 759,240 行 / 90 FK 组 / 0 悬空；写操作被拒 | PASS |

### 4.4 Phase 2 / Phase 3（最终 Verifier 复核）

| ID | 命令 | 真实输出 | 结果 |
|---|---|---|---|
| H1 | `python scripts/build_aw_dictionary.py` | `tables=71, upstream_en=71/71, zh_candidate=31, columns=486, upstream_en=452, zh_candidate=60, unknown_en=34` | PASS |
| H2 | `python scripts/build_aw_metric_review.py` | `selected=14, all_unreviewed=true, reference_sql_executed=14` | PASS |
| H3 | `unittest discover -s tests -q` | **`Ran 215 tests in 7.700s / OK`**（181 + 34） | PASS |
| H4 | `compileall -q app runner tests scripts` / `node recovery.test.mjs` | exit 0 / `ok 55 assertions` | PASS |
| H5 | `app.cli check --quiet` | `ready` | PASS |
| H6 | `evaluate-rag --validate-only` / `evaluate --validate-only` | `valid=true, 15` / `valid=true, 14` | PASS |
| H7 | `build-knowledge --validate-only --quiet` | 指纹 `96f7f2cb2ba1…eca58` 不变，`trusted=0` | PASS |
| H8 | `evaluate-graphrag --validate-only` | `split=dev, case_count=12, configs=A,B,C,D`；`--cases test.json` 被拒绝 | PASS |
| H9 | `evaluate-graphrag --vector-mode none --quiet` | 48 条结果，`vector_status="not_run: deterministic keyword only; no mock vectors"` | PASS |
| H10 | `evaluate-graphrag --vector-mode local --quiet` | 48 条结果，`vector_status="real_local_model"`，2 分 23 秒，零在线 API | PASS |
| H11 | `git diff --stat` / `git status --short` | 5 个既有文件 `75+/13-`；历史评测产物、`business_metrics.json`、`.env`、依赖无改动 | PASS |
| H12 | 只读 SQL 统计值索引规模 | 231 文本列 / 571,985 DISTINCT 值 / 约 57,199 次在线请求（上界）；Top10 含 PII 字段 | 已核对 |

**未执行**：任何在线 LLM 调用、在线 embedding、`build-index` 全库构建、DeepEye-SQL 六阶段在线生成、
LangGraph 真实推理、BIRD/指标在线评测、对照/消融的 SQL 正确率实验。本轮**没有产生任何新的
SQL 可执行率、执行准确率、Token 或在线延迟数字**（上下文 Token 为离线文本估算，不是模型用量）。

## 5. 主计划（Phase 0-6）

见 `docs/Phase0-架构基线分析.md` §9.2、`docs/Phase1-交付报告.md` §8、
`docs/数据资产建设-AdventureWorks接入报告.md` §13、`docs/Phase2-3-交付报告.md` §10。
核心取舍：先以新增包 + 单测落地知识层（Phase 1），再补齐真实可验证的数据资产与评测集，
最后用**可选开关 + hint 模式**把 GraphRAG 结果接入运行时主链路（Phase 2/3 已完成），
保证任何阶段都能一键回到现有行为；硬裁剪模式与在线评测留到获得授权后。

## 6. 文件清单

### 6.1 Phase 0（仅新增文档）

`goals/phase0-rag-graphrag/GOAL.md`、`docs/Phase0-架构基线分析.md`。

### 6.2 Phase 1

新增 `app/knowledge/{__init__,models,sqlite_metadata,schema_extractor,metric_registry,dependencies}.py`、
`app/commands/build_knowledge.py`、
`tests/{test_knowledge_models,test_metric_registry,test_schema_extractor}.py`、
`docs/Phase1-交付报告.md`；修改 `app/cli.py`、`app/commands/__init__.py`、
`app/agent/tools/registry.py`、`tests/test_cli.py`；产物 `workspace/knowledge/*.json`。

### 6.3 数据资产建设（AdventureWorks）

新增 `data/adventureworks/*`（gitignore）、`docs/adventureworks/*`、`docs/simulated-policies/*`（13 份）、
`docs/数据资产建设-AdventureWorks接入报告.md`、
`app/enterprise/{adventureworks.py,adventureworks_metrics.json,adventureworks_examples.json}`、
`scripts/build_aw_metrics.py`、`evals/adventureworks-graphrag/*`、`tests/test_adventureworks_integration.py`；
修改 `app/web/registry.py`、`app/agent/tools/registry.py`、
`app/knowledge/{models,metric_registry,dependencies,schema_extractor}.py`、
`app/commands/build_knowledge.py`；产物 `workspace/knowledge/adventureworks/*.json`。

### 6.4 Phase 2 / Phase 3

| 类型 | 路径 |
|---|---|
| 新增（数据/脚本） | `scripts/{build_aw_dictionary,build_aw_metric_review}.py`、`app/enterprise/{adventureworks_dictionary,adventureworks_metric_review,adventureworks_review_decisions}.json` |
| 新增（知识层） | `app/knowledge/{schema_dictionary,review,graph_store,enhancement}.py`、`app/knowledge/retrieval/{__init__,hybrid,schema_retriever,context_builder,subgraph,join_path}.py` |
| 新增（命令/测试/报告） | `app/commands/evaluate_graphrag.py`、`tests/{test_knowledge_dictionary_review,test_graphrag_retrieval,test_graphrag_acceptance}.py`（34 项）、`docs/Phase2-3-交付报告.md` |
| 新增（结果产物） | `workspace/evals/{graphrag-abcd.json,graphrag-abcd-local.json}`（各 48 条明细） |
| 修改 | `app/agent/tools/registry.py`（可选增强分支）、`app/cli.py`（第 10 个子命令）、`app/commands/__init__.py`、`tests/test_cli.py`（严格全等集合 9→10）、`GOAL.md` |
| 明确未动 | `business_metrics.json`、38 条 AW 指标文件、全部 `evals/*` 题单与历史产物、`data/enterprise_*`、`data/bird`、`app/skills/`、`app/pipeline/`、`app/db_utils/`、`app/prompt/`、`app/api/`、`app/web/server.py`、`app/web/preflight.py`、`app/agent/state.py`、`app/enterprise/knowledge.py`、`.env`、`pyproject.toml`、任何依赖 |

## 7. Worker / Verifier 记录

- Worker：Phase 0 只读分析 + E1-E10；Phase 1 实现 9 个新文件 + F1-F9；数据资产阶段完成下载校验、PRAGMA 核验、26 条 SQL、13 份制度、38 条指标、18 道评测题 + G1-G10；Phase 2/3 实现 16 个新文件、4 处集成、34 项新测 + H1-H12。
- Verifier：独立复核 `Ran 215 tests OK`、默认知识指纹不变、`check` ready、两个 validate-only、
  `git diff --stat` 仅 5 个既有文件、dev/test 隔离、两份 A-D 结果文件各 48 条、同一 case 重复运行指标一致。
- 本轮发现并修复的真实缺陷：
  1. 图扩展把检索已召回的中间表当"1 跳邻居"短路 → 改为聚焦表对的路径补全；验收案例 1 通过。
  2. Join 路径枚举全部 BFS 前沿两两组合导致 28 处伪歧义与无关风险 → 限制为 focus × targets，风险限定 top 路径。
  3. `SchemaContext` 在 200-token 预算下裁剪后仍 509 token → 增加硬预算紧凑渲染，`within_budget=True`。
  4. 指标扩展列被 50 列预算截断导致 C 与 B 完全相同 → 调整列优先级，确定性模式下 C 相对 A/B 有 +0.0694 Column Recall。
  5. `subgraph` 的关系过滤未归一化 `#reverse` 后缀 → 修正。
  6. 早前阶段：官方 `LICENSE.txt` 404、FK 单遍假阳性、`execute_readonly_table` 返回值顺序、6 条指标公式非逐字引用、评测集措辞与断言不一致。
- 证据等级：HIGH（命令输出 + 结果文件 + git 记录三重一致）。所有数值来自真实 SQLite / 真实本地模型 / 真实测试，不含在线 LLM 推理结果。

## 8. 停止点

`status: PAUSED` — 委托方要求「完成后必须说明……」「如果在线模型不可用，不能虚构评测成绩」。
**Phase 2/3 已验收通过，未开始 Phase 4，未运行在线模型评测。** 继续开发前需人工确认：

1. 是否批准为 AW 的**少量高价值字段**构建值索引（需先给 `build-index` 增加 `--columns` 白名单与 PII 字段排除，并设定请求上限；全库上界约 57,199 次在线请求）。
2. 是否批准在线 LLM 配额跑 3–5 题端到端对照（Baseline vs Enhanced），记录真实 SQL / Token / 延迟，补齐 D15。
3. 哪些 14 条审核清单中的指标可由**具名人工**批准（决定账本目前为空，38 条全部 `trusted=False`）。
4. 是否放行 `hard_filter` 模式开发与 hint vs hard_filter 对照实验（需先验证 Value Retrieval / Few-shot / Schema Linking / SQL Revision 字段完备性）。
5. 是否批准 Phase 6 在线对照/消融评测；`evals/adventureworks-graphrag/test.json` 6 题在此之前保持封存。
