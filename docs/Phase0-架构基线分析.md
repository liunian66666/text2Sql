# Phase 0 架构基线分析与升级方案

> 项目根：`E:/曹浩冉/研究生/AI学习RAG与Agent/AGENT_exp/智能数据分析助手/DeepEye-SQL`（下称 `ROOT`）
> 生成时间：2026-11-13 09:00 GMT+8 · goal-mode 契约：`goals/phase0-rag-graphrag/GOAL.md`
> 本轮性质：**只读代码分析 + 基线测试 + 实施计划**。未修改任何现有源码、配置、`.env`、依赖或数据库；仅新增本文件与 GOAL.md 两份文档。

---

## 0. 结论摘要与事实更正

### 0.1 一句话结论

应用层真实存在且完整（FastAPI + SSE、LangGraph ReAct、6 个工具、只读沙箱、指标 RAG、两个真实企业库、三套评测），不是需求文档的设想；但**知识层非常薄**：13 条 JSON 指标定义、无切分、无持久索引、无文档知识、无关系图、无版本，且指标知识只以自然语言 `evidence` 拼接进 Prompt，**没有形成对 SQL 的结构化约束**。这正是本次升级最有价值的切入点。

### 0.2 需求文本与仓库不符之处（必须更正，不得沿用）

| 需求文本 | 仓库真实情况 | 证据 |
|---|---|---|
| 指标 Recall@3 / MRR 由 0.567 提升至 **0.985** | 实际为 **0.567 → 1.000**（绝对提升 0.433）；仓库全部评测产物与文档中均无 `0.985` 这一指标值（该字符串仅作为 BIRD 二进制库字节与 `llm_usage.jsonl` 时间戳出现） | `evals/metric-rag/latest/summary.json`；`evals/metric-rag/report.md:5`；`README.md:26,235` |
| BIRD-dev 50 题由 **60%** 提升至 76% | 实际为 **72% → 76%**（仅生成 36/50 → 完整流程 38/50）；`60%` 只出现在分库结果 Thrombosis Prediction | `evals/bird-dev-50/ablation/summary.json`；`docs/ablation-50.md:70` |
| 「已有 Router / 意图分类器 / HITL / LangSmith / Python 3.13 / M1-M5」 | 仅外层 `docs/项目设计文档.md` 的规划描述，代码未实现；当前是 4 节点 ReAct，无 `interrupt()`，Python 3.12.13 | `app/agent/graph.py:102-113`；`docs/agent-architecture.md:85`；`.python-version` |
| 离线测试项数 | README 写 105；外层 HTML 写 55 / 88；交付清单写 100。**本轮实测 105** | E1 命令输出 |
| Agent 专项 `14/14` | 真实，但 `evaluation_mode = incremental_failure_rerun`，是增量失败题回归，**不是整批独立成绩** | `evals/agent/latest/summary.json` |

### 0.3 三类能力的边界（git 溯源）

`git remote -v`：`upstream = HKUSTDial/DeepEye-SQL`，`origin = liunian66666/text2Sql`。上游最后一个提交为 `252ab45`（Exclude unuseful columns from schema prompts），其后 **20 个中文提交**（`bbf42c7` 起，作者 曹浩冉）为本项目二次开发。`git status --short` 为空，工作树干净。

| 类别 | 范围 |
|---|---|
| 开源引擎能力 | `app/pipeline/`（3 linker / 3 generator / 8 checker / 一致性选择）、`app/few_shot/`、`app/vector_db/`、`app/services/`、`app/db_utils/`、`app/prompt/`、`runner/` 批量脚本、`results/` 公开预测 |
| 项目已有能力（二次开发） | `app/agent/`、`app/api/`、`app/web/`、`app/enterprise/`、`app/skills/`、`app/commands/`、`app/persistence/`、`app/llm/budget.py|usage.py`、`evals/`、`runner/measured_stage.py|select_bird_sample.py|report_*.py`、`tests/` 11 个新测试 |
| 本次新增能力 | 尚未开始；候选清单见 §8 |

---

## 1. 真实架构与完整请求调用链路

### 1.1 分层与入口

| 层 | 文件 | 作用 |
|---|---|---|
| 浏览器 | `app/web/static/chat.js`、`recovery.js` | 原生单页；SSE 订阅、断线重连、刷新恢复、SVG 图表 |
| API | `app/api/app.py:15`、`app/api/routes/chat.py`、`database.py`、`evaluation.py` | FastAPI；`POST /api/chat` 202 + `GET /api/query/{id}/events?after=` SSE |
| 任务 | `app/web/task_manager.py`、`app/web/registry.py`、`app/persistence/sqlite.py` | 单进程串行队列、事件持久化、LangGraph `SqliteSaver` |
| Agent | `app/agent/graph.py`、`state.py`、`service.py`、`tools/*` | ChatOpenAI + ToolNode ReAct |
| 知识 | `app/enterprise/knowledge.py`、`business_metrics.json` | 指标混合检索 |
| SQL 引擎 | `app/skills/sql_skill.py` → `app/pipeline/*` → `app/db_utils/execution.py` | 单题六阶段 + 只读执行 |
| 评测 | `app/commands/evaluate*.py`、`runner/evaluation.py`、`evals/*` | 指标 RAG / Agent / BIRD 三档消融 |

CLI 入口 `app/cli.py:33-42` 提供 8 个子命令：`serve / check / ask / mcp / evaluate / evaluate-rag / generate-data / build-index`。

### 1.2 完整请求链路（按真实代码顺序）

1. `app/web/static/chat.js` → `POST /api/chat`（`app/api/routes/chat.py:18-28`）→ `TaskManager.submit()` 返回 `request_id`。
2. `app/web/task_manager.py` 在后台线程调用 `DataAnalysisAgent`（`app/agent/service.py`），并把 `on_stage` 回调注入 `config["configurable"]`。
3. `app/agent/graph.py:46-70` `call_model()`：拼 `SystemMessage(build_system_prompt(...))` + 最近 40 条消息，`model_with_tools.invoke()`；回调推 `agent_planning` 事件。
4. `ToolNode`（`graph.py:72`）执行模型选中的工具；每个工具经 `app/agent/tools/registry.py:54-84` `ToolRegistry.invoke()` 包装，异常统一转成 `ToolMessage`，并以 `Command(update=...)` 写回强类型 State。
5. 业务口径工具 `search_business_metrics`（`metric_tool.py:10`）→ `registry._search_metrics()`（`registry.py:182-203`）→ `EnterpriseKnowledgeBase.search()` → 结果写入 `business_context`，并调 `knowledge.as_evidence()` 生成 `trusted_evidence` 文本。
6. Schema 工具 `inspect_database_schema`（`schema_tool.py:10`）→ `registry.inspect_database_schema()`（`registry.py:205-223`）用 `PRAGMA table_info` / `foreign_key_list` 读真实库 → 写入 `schema_context`。
7. SQL 工具 `run_text_to_sql`（`app/agent/tools/text2sql_tool.py`）→ `registry._run_text_to_sql()`（`registry.py:235-278`）：**硬约束** 先验 `schema_context_database_id == database_id`，把 `trusted_evidence` 与 `user_hint` 拼成 `request["evidence"]`，再调 `SQLSkill.run()`。
8. `app/skills/sql_skill.py:221-323`：构造 `DataItem(question, evidence, database_schema=SchemaService().load_sqlite_schema(...))`，按 `STAGES = (value_retrieval, few_shot_preparation, schema_linking, sql_generation, sql_revision, sql_selection)`（`sql_skill.py:30`）逐阶段调用上游 Runner，最后 `execute_readonly_table()`（`:100-123`）只读执行；每阶段通过 `on_stage` 推事件。
9. 结果与用量写回 `result.json` / `usage.jsonl`；`TaskManager` 把 `progress` 事件落 `workspace/state/agent_state.sqlite`；SSE 路由（`chat.py:61-81`）按 `after` 游标回放；前端 `build_visualization()` 生成确定性图表 spec。

**注意**：真实阶段顺序是「值检索 → few-shot → schema linking → 生成 → 修订 → 选择」，与需求文本中「schema linking → 值检索 → few-shot」不同，以 `sql_skill.py:30` 与 `script/run_pipeline.sh` 为准。

### 1.3 关键注入点（后续所有阶段都要复用）

- `on_stage` 回调贯穿 `graph.py:47-49` → `registry.py:57-59` → `sql_skill.py:257-276` → `TaskManager` → SSE `event: progress`。**新增知识检索/图扩展事件不需要改 SSE 协议**。
- `AgentState`（`state.py:11-35`，`TypedDict(total=False)`）已有 `business_context` / `schema_context` / `trusted_evidence` / `tool_trace`，新增字段可向后兼容。
- `SQLSkillRequest`（`sql_skill.py:41-54`）已有 `evidence`；新增可选 `schema_context` 字段不影响现有调用。

---

## 2. 现有 RAG 模块的知识来源、切分、索引、Embedding、召回与重排

| 维度 | 真实实现 | 证据 |
|---|---|---|
| 知识来源 | 仅 `app/enterprise/business_metrics.json` 的 **13 条**指标定义（5 销售 + 8 HR） | `knowledge.py:27-28` |
| 切分策略 | **无 chunk**；1 指标 = 1 文档，`_document()` 拼接「域/名称/别名/定义/公式/时间口径/SQL 口径」 | `knowledge.py:43-48` |
| 索引结构 | **无持久索引**；语料向量进程内 lazy 构建 + `_query_cache` 查询向量缓存，全量 13 条遍历算余弦 | `knowledge.py:39-41,79-89` |
| Embedding | 云：`qwen3.7-text-embedding-flash`（DashScope OpenAI 兼容端点）；本地备选 `Qwen3-Embedding-0.6B` | `config/local/app-fast/config.toml`（不记密钥）；`app/vector_db/metered_embedding.py`、`vector_db.py:62-98` |
| 召回 | `_lexical_score()` 指标名 +3.0、命中别名每个 +2.0（上限 1.0）；语义 cosine ≥ `semantic_min_score=0.25` 才可入选；按 `database_id` 过滤候选 | `knowledge.py:67-74,106-118` |
| 融合 | 归一化加权和 `0.6 * lexical + 0.4 * semantic`，`scored.sort()` 取 top-k | `knowledge.py:35-37,117,127` |
| 重排 | **无独立 rerank**；文档所称「加权重排」实为上述加权后一次排序，无 cross-encoder / 二次打分 | `knowledge.py:127`；`README.md:63` |
| 降级 | 向量异常时 `mode_used="keyword"`，记录 `fallback_reason`，不阻断 SQL | `knowledge.py:98-103` |
| 未找到 | BM25、RRF、faiss、独立 rerank、文档 chunk/PDF 切分、父子块检索 | 全库检索无命中；`app/commands/evaluate_rag.py:34` 的 `reciprocal_rank` 是 MRR 指标，不是融合算法 |

另外两套**上游** RAG 能力（不要重写）：

- **值检索**：`app/pipeline/value_retrieval/` + `app/vector_db/`（Chroma `PersistentClient` 或自研 `LocalValueIndex` 稠密向量余弦，每列一个 `.npy` + `manifest.json`）；LLM 抽关键词 → embedding → 按 TEXT 列召回真实值 → 写回 `DataItem.database_schema.value_examples`。未使用 LSH/MinHash。
- **Few-shot 检索**：`app/few_shot/`（掩码 `masker.py` → `index_builder.py` 建 `examples.jsonl` + 双路 `.npy` → `retriever.py` `FewShotRetriever.retrieve_by_texts()` 按 question/SQL 双路加权 cosine → `runtime.py` 注入 `item.few_shot_examples`）；索引实体在 `workspace/runs/_shared/bird/train100-qwen3.7-text-embedding-flash/`。

`app/db_utils/cloud_schema.py` 有 Spider2 外部 `.md` 整文件读取，但 `data/**/documents/` 目录不存在，不构成文档知识库。

---

## 3. 指标知识库的存储结构与检索实现

### 3.1 存储：`app/enterprise/business_metrics.json`（JSON，非 sqlite/yaml）

| 字段 | 销售 5 条 | HR 8 条 |
|---|---|---|
| `id` / `domain` / `database_id` / `name` / `keywords` / `definition` / `sql_rule` | 有 | 有 |
| `formula` / `tables` / `fields` / `time_grain` | **缺失** | 有 |

**这是 Phase 1 最具体的补齐点**：销售域的公式与时间口径只以自然语言散落在 `definition` / `sql_rule`，例如：

- 销售额：`quantity * unit_price * (1 - discount_rate)`，过滤 `orders.status IN ('paid','shipped','completed')`；
- 净销售额：退款须「按 `order_id` 聚合后再连接，避免订单明细导致退款金额重复」；
- 平均客单价：分母 `COUNT(DISTINCT orders.order_id)`；退款率需 `NULLIF(...,0)`。

这些能由现有文本 + 真实 schema（§6）推出，但**必须人工审核并标 `source_ref` / `reviewed` 后才能结构化入库**，不能让 LLM 猜测。

### 3.2 检索：`app/enterprise/knowledge.py`

`EnterpriseKnowledgeBase.search(question, limit, database_id) -> {metrics[], retrieval{...}}`，返回可解释分数；`retrieve()` 为兼容方法；`create_enterprise_knowledge(app_config, mode)` 复用 `vector_database` 的同一 embedding 配置。评测在 `app/commands/evaluate_rag.py:25-47`（`score()` 计算 Recall@K 与 MRR），CLI 对照 keyword / hybrid 两种模式。

### 3.3 关键痛点：知识只进 Prompt，不进约束

```
knowledge.as_evidence()  (knowledge.py:145-152)
  → registry._search_metrics(): trusted_evidence  (registry.py:191-202)
  → registry._run_text_to_sql(): "[系统业务口径]\n{...}" + "[用户补充说明，未经系统验证]\n{...}"  (registry.py:255-261)
  → SQLSkillRequest.evidence  (sql_skill.py:45)
  → DataItem(evidence=...)  (sql_skill.py:252)
  → 上游 prompt 模板
```

也就是说：`sql_rule` 是自然语言字符串，没有程序可校验的字段级约束；`user_hint` 与系统口径混在同一段文本；没有指标 ID、版本、来源、聚合方式、允许维度、Join 约束的结构化传递；最终 SQL 是否遵守口径，全靠 LLM 自觉。这是 §9 全部方案要解决的根问题。

---

## 4. DeepEye-SQL 已有能力（不得重复实现）

| 阶段 | 模块 | 已有能力 | 可复用接口 |
|---|---|---|---|
| Schema Linking | `app/pipeline/schema_linking/linkers/` | `DirectLinker` LLM 直选、`ReversedLinker` 由 ICL SQL 反推、`ValueLinker` 取值链接；三路结果 `merge_schema_linking_results()` 取并集 + 渐进 schema 裁剪；自带 table/column recall 评估 | `SchemaLinkingRunner.from_config(cfg).run()` |
| 值检索 | `app/pipeline/value_retrieval/`、`app/vector_db/` | 关键词抽取、embedding、按列召回真实值、写回 schema；Chroma / `LocalValueIndex` 双后端 | `ValueRetrievalRunner`、`get_embedding_function()`、`LocalValueIndex` |
| Few-shot | `app/few_shot/` | LLM 掩码骨架、训练样本索引、question/SQL 双路加权检索、初步 SQL 一致性选择 | `FewShotRetriever.from_index_path()`、`prepare_few_shot_examples_for_item()`、`build_few_shot_index()` |
| SQL 生成 | `app/pipeline/sql_generation/generators/` | DC / Skeleton / ICL 三路并行采样产出多候选 | `SQLGenerationRunner` |
| 错误修复 | `app/pipeline/sql_revision/checkers/` | Syntax / Join / OrderByLimit / Time / Select / MaxMin / OrderByNull / Result 共 8 个 checker，按正则错误模式触发修订 | `SQLRevisionRunner` |
| SQL 选择 | `app/pipeline/sql_selection/sql_selection.py` | 候选执行 → 结果哈希聚类 → 一致性分数；≥ 阈值短路选中，否则 LLM A/B/TIE 多票投票 + 一致性加权 | `SQLSelectionRunner`、`ExecutionService.hash_result()` |
| 执行 | `app/db_utils/execution.py` | 只读连接、`query_only`、authorizer 白名单、超时 | `execute_sql()` 家族 |

**结论**：上游已经解决「单条 SQL 怎么生成/选对」。本次升级的知识层应作用于 **输入侧（更准的 schema / 指标约束 / Join 路径）与输出侧（SQL 语义校验 / 证据追溯）**，而不是再写一遍 schema linking 或 few-shot。上游唯一可直接借力的信号是 `sql_selection` 的执行结果一致性（可作为「SQL 经验知识」入库前的验证依据，而非执行成功即入库）。

---

## 5. 现有 LangGraph Agent 的节点、状态、边与工具

### 5.1 状态 `app/agent/state.py:11-35`

`AgentState(TypedDict, total=False)`：消息 `Annotated[list[AnyMessage], add_messages]`；请求与轮次 `request / turn_id / question / database_id / requested_database_id / available_databases / user_hint`；知识与结果 `trusted_evidence / business_context / business_context_turn_id / schema_context / schema_context_database_id / sql_result / sql_result_turn_id / sql_result_database_id / visualization / analysis_intent`；可观测 `tool_trace / tool_rounds / model_calls / agent_usage / agent_error`。

`analysis_intent` 仅为 `Literal["query","ranking","trend","proportion"]`，**没有结构化 `QueryIntent`**（指标、维度、时间范围、比较方式、歧义）。

### 5.2 节点与边 `app/agent/graph.py:102-113`

```
START → assistant(call_model) ──conditional──→ tools(ToolNode) → increment_tool_round → assistant
                              ├→ tool_limit(stop_at_tool_limit) → END
                              └→ END
```

`MAX_TOOL_ROUNDS = 6`，`MAX_HISTORY_MESSAGES = 40`；`checkpointer` 默认 `InMemorySaver`，Web 运行时由 `app/persistence/sqlite.py` 提供 `SqliteSaver`，thread_id = `conversation_id + database_id`。**无 Router 节点、无 `interrupt()` HITL、无知识充分性判断节点**。

### 5.3 六个工具（`app/agent/tools/registry.py:37-52`）

| 工具 | 文件 | 状态写入 |
|---|---|---|
| `select_data_source` | `source_tool.py` | 切换数据源时清空 schema/口径/SQL 缓存；显式业务域关键词校验 |
| `search_business_metrics` | `metric_tool.py` | `business_context` + `trusted_evidence` |
| `inspect_database_schema` | `schema_tool.py` | `schema_context` |
| `run_text_to_sql` | `text2sql_tool.py` | `sql_result` + `analysis_intent`；每轮限一次，前置 schema 校验 |
| `build_visualization` | `chart_tool.py` | `visualization`；确定性 spec |
| `get_capabilities` | `capability_tool.py` | 只读能力清单 |

工具用 `InjectedState` / `InjectedToolCallId` / `RunnableConfig` 注入运行时依赖，模型只能传业务参数（`docs/agent-architecture.md:45-55`）。**新增知识工具应沿用同样的注入方式**，不改工具协议。

---

## 6. 企业数据库 Schema、表关联与业务场景

数据资产实测：`data/enterprise_sales/enterprise_sales.sqlite`（335,872 B）、`data/enterprise_hr/enterprise_hr.sqlite`（151,552 B），另有 BIRD-dev 11 个公开库、`workspace/state/agent_state.sqlite` 状态库；外层 `identifier.sqlite` 为 0 字节空文件。

### 6.1 `enterprise_sales`（7 表）

| 表 | 行数 | 主键 | 外键 |
|---|---:|---|---|
| `categories` | 5 | `category_id` | — |
| `customers` | 240 | `customer_id` | — |
| `products` | 60 | `product_id` | `category_id → categories` |
| `orders` | 1000 | `order_id` | `customer_id → customers` |
| `order_items` | 2501 | `order_item_id` | `order_id → orders`；`product_id → products` |
| `payments` | 924 | `payment_id`，`order_id` UNIQUE | `order_id → orders` |
| `refunds` | 92 | `refund_id` | `order_id → orders` |

字段要点：`order_items(quantity, unit_price, discount_rate)`、`payments(paid_amount, paid_at)`、`refunds(refund_amount, refund_date, refund_reason)`、`orders(status, channel, region, order_date)`。业务链路：订单—明细—支付—退款；对应 5 个销售指标（销售额、净销售额、客单价、退款率、新增客户）。

### 6.2 `enterprise_hr`（4 表）

`departments`(6) / `employees`(180，`hire_date`, `termination_date`, `department_id`) / `attendance`(916，`attendance_month`, `scheduled_days`, `attended_days`, `late_days`, `leave_days`) / `performance_reviews`(262，`review_period`, `performance_score`)。对应 8 个 HR 指标（在职、新入职、离职、流失率、平均绩效、部门占比、出勤率、迟到率）。造数脚本 `app/enterprise/sales_data.py`、`hr_data.py`（固定随机种子，可复现）。

### 6.3 对 GraphRAG 的直接影响

真实外键已经构成一张可用的元数据图（12 条左右 FK 边），且存在**一对多聚合风险**的典型形态：`orders → order_items`（1:N）、`orders → refunds`（1:N）。「净销售额」指标定义里明确写了「退款按 order_id 聚合后再连接，避免订单明细导致退款金额重复」——这正是 Join 路径补全必须标记基数风险、且能构造出可验证验收案例（案例三）的真实依据。

---

## 7. 当前系统在知识构建、检索、更新与 SQL 语义正确性方面的不足

| # | 问题 | 代码证据 | 影响 | 对应 Phase |
|---|---|---|---|---|
| 1 | 指标定义非结构化，销售 5 条缺 `formula/tables/fields/time_grain` | `business_metrics.json:3-47` | 无法程序化约束 SQL | 1 |
| 2 | 指标知识只拼成文本 `evidence`，与 `user_hint` 混在一起 | `registry.py:255-261` → `sql_skill.py:252` | 口径靠 LLM 自觉，无校验 | 1/2/4 |
| 3 | 无文档知识库、无切分、无父子块 | 全库无 chunk 实现 | 文档驱动查询无法做 | 4/5 |
| 4 | Schema 元数据只在工具调用时临时返回，无稳定 ID / 描述 / 枚举 / 版本 | `registry.py:205-223` | 无法做 Table-first / Column-first 与版本联动 | 1/2 |
| 5 | 无业务元数据关系图、无 Join 路径搜索 | 无 networkx 使用（但 networkx 3.5 已安装） | 多表查询漏中间表、错 Join | 3 |
| 6 | 检索仅 13 条全量遍历 + 加权求和，无 RRF / 类型过滤 / 元数据过滤 / 二次重排 | `knowledge.py:91-137` | 知识库扩容后不可用，无充分性判断 | 2 |
| 7 | 无知识版本、无内容哈希、无失效传播 | 仅 `sql_skill.py:242-248` 有值索引 `embedding_provenance.json` 的 db sha256 校验（可复用雏形） | 改指标/字段后旧知识仍被召回 | 5 |
| 8 | SQL 执行成功不等于业务正确；无 AST 语义校验、无跨查询一致性、无证据追溯 | `runner/evaluation.py:29-46` 只做集合式 EX | 口径错也能「执行成功」 | 4 |
| 9 | `sqlglot` / `pytest` / `sqlparse` 未安装，`pyproject.toml` 也未声明 | 环境实测 | Phase 4 AST 校验需先决策依赖 | 4 |
| 10 | 评测集只有 15 题指标问法 + 14 题 Agent + BIRD 50 题，无知识增强 SQL 集 | `evals/` | 无法证明新增检索模块的收益 | 6 |

**已有雏形必须复用**（不要重造）：`inspect_database_schema()` 的真实外键抽取、`sql_skill.py:242-248` 的 db 哈希失效检测、`app/few_shot/retriever.py` 的双路检索、`on_stage` 事件链、`runner/select_bird_sample.py` 的确定性抽样 + `source_sha256` 锁定。

---

## 8. 建议新增、修改、复用的文件清单

### 8.1 新增（按阶段，均为新文件，不覆盖现有实现）

| Phase | 路径 | 职责 |
|---|---|---|
| 1 | `app/knowledge/__init__.py`、`models.py` | Pydantic：`MetricDefinition` / `SchemaNode` / `KnowledgeEdge` / `QueryIntent` / `SchemaContext` / `EvidenceRef` / `KnowledgeVersion` |
| 1 | `app/knowledge/metric_registry.py` | 结构化指标注册中心：加载、字段校验、版本/生效时间/来源、别名索引 |
| 1 | `app/knowledge/schema_extractor.py` | 抽取 DB/Table/Column + PK/FK/UNIQUE + 稳定 ID + 可选字段统计；复用 `registry.inspect_database_schema()` 的 PRAGMA 逻辑 |
| 1 | `app/commands/build_knowledge.py` | CLI：从 `business_metrics.json` + 两个真实库构建知识产物 |
| 2 | `app/knowledge/retrieval/hybrid.py` | 在 `EnterpriseKnowledgeBase` 之上扩展类型过滤、RRF 融合、元数据过滤；保留 `search()` 兼容 |
| 2 | `app/knowledge/retrieval/schema_retriever.py` | Table-first / Column-first 双向召回 + 指标依赖字段强制保留 |
| 2 | `app/knowledge/retrieval/context_builder.py` | `SchemaContext` 构建、去重、Token 预算裁剪、充分性标记 |
| 3 | `app/knowledge/graph_store.py` | networkx 关系图：节点/边、来源、可信度、版本、序列化 |
| 3 | `app/knowledge/retrieval/subgraph.py` | 种子节点 → 限跳扩展 → Join 路径搜索 → 基数风险标记 → 观测日志 |
| 3 | `app/agent/tools/knowledge_tool.py` | 新工具 `retrieve_schema_context`（沿用 InjectedState 注入） |
| 4 | `app/knowledge/document_ingest.py` | Markdown/TXT 按标题切分、父子块、原文证据、候选知识标记 |
| 4 | `app/knowledge/validation/sql_semantics.py`、`evidence.py` | 业务语义校验 + 证据追溯（AST 依赖待决策） |
| 5 | `app/knowledge/incremental.py`、`sql_experience.py` | 内容哈希增量更新、失效传播、SQL 案例库（复用 `FewShotRetriever`） |
| 6 | `evals/knowledge-sql/{dev,test}/questions.json`、`reference/*.sql`、`registry/` | 80 题评测集 + 标准答案 + 版本登记 |
| 6 | `runner/evaluate_knowledge.py`、`runner/report_knowledge_ablation.py` | A-E 对照与消融实验、检索/端到端指标统计 |
| 全阶段 | `tests/test_metric_registry.py`、`test_schema_extractor.py`、`test_hybrid_retrieval.py`、`test_subgraph.py`、`test_join_path.py`、`test_incremental.py`、`test_knowledge_eval.py` | 单测与集成测试 |
| 全阶段 | `docs/knowledge-architecture.md`、`docs/evaluation-protocol.md` | 架构图与评测协议 |

### 8.2 修改（尽量小、且全部可开关）

| 路径 | 改动 | 兼容策略 |
|---|---|---|
| `app/enterprise/business_metrics.json` | 为销售 5 条补结构化字段；新增 `version` / `effective_from` / `source_ref` | 保留全部原字段，`knowledge.py` 仍可读 |
| `app/enterprise/knowledge.py` | 增加可选 `knowledge_type` / 版本过滤输出；`search()` 签名不变 | 默认行为与现有一致 |
| `app/agent/state.py` | 增 `query_intent` / `schema_context_v2` / `retrieval_trace` 字段 | `TypedDict(total=False)`，旧 checkpoint 不受影响 |
| `app/agent/tools/registry.py` | 注册新工具；把结构化约束写入 `request`（而非只写文本） | 保留现有 6 个工具与全部硬约束 |
| `app/skills/sql_skill.py` | `SQLSkillRequest` 增可选 `schema_context`，默认 `None` | 不传则完全走原链路 |
| `app/commands/__init__.py`、`app/cli.py` | 注册 `build-knowledge` / `evaluate-knowledge` 两个子命令 | 原有 8 个命令不变 |
| `app/api/routes/chat.py` | 可选：SSE `progress` 增加 `knowledge` / `graph` 阶段名 | 不改事件结构、不改字段含义 |
| `pyproject.toml` | 仅当批准 `sqlglot` 时新增一行 | 未获批准前不动 |

### 8.3 明确复用、不改动

`app/pipeline/*`（上游全部算法）、`app/few_shot/*`、`app/vector_db/*`、`app/services/*`、`app/db_utils/execution.py`、`app/prompt/*`、`app/web/task_manager.py`、`app/persistence/*`、`app/api/app.py`、`app/agent/graph.py` 的现有 4 节点、`runner/evaluation.py`、`runner/select_bird_sample.py`、`evals/metric-rag/*`、`evals/bird-dev-50/*`、`tests/` 现有 13 个测试文件。

---

## 9. 增量开发方案与技术选型

### 9.1 如何以最小改动实现指标驱动的 GraphRAG

技术选型：**networkx 3.5（已安装）** 做业务元数据关系图，不引入 Neo4j；**不换向量库、不换 Embedding**（继续 Chroma / `LocalValueIndex` + `qwen3.7-text-embedding-flash`）；关系边只来自两类可信来源——真实 `PRAGMA foreign_key_list` 与人工审核的业务配置（带 `source` / `confidence` / `version`），LLM 不得直接产出可信外键。

最小改动落地路径（四步，全部避开上游算法）：

1. **构图（离线）**：`schema_extractor.py` 从两个真实库抽节点（Database/Table/Column），`metric_registry.py` 从审核后的指标定义抽 Metric/Dimension 节点与 `DEPENDS_ON` / `USES_TABLE` / `USES_METRIC` 边，FK 边标 `source=PRAGMA`。产物落 `workspace/knowledge/<version>/graph.json`（本地文件，非新数据库）。
2. **检索（在线，可选开关）**：`retrieve_schema_context` 工具以 `EnterpriseKnowledgeBase.search()` 命中的 metric_id + `inspect_database_schema()` 命中的 column 为种子 → `subgraph.py` 限跳扩展（默认 2 跳、表数 ≤ 8、字段数 ≤ 40、Token 预算）→ Join 路径搜索按「主外键方向 + 基数风险 + 指标粒度 + 关系可信度」排序，**多路径不确定时返回歧义而不是硬选**。
3. **注入（唯一改动主链路之处）**：`SchemaContext` 经 `registry._run_text_to_sql()` 写入 `SQLSkillRequest.schema_context`（默认关闭）；`SQLSkill.run()` 在构造 `DataItem` 时用它收紧 `database_schema`（上游 `filter_used_database_schema()` 会继续裁剪），并把指标约束作为结构化字段传入；**同时保留现有 `trusted_evidence` 文本**，保证开关关闭时行为逐字节不变。
4. **可观测**：所有种子、扩展节点、选中/排除路径、Token 数、耗时通过现有 `on_stage` 回调推 `progress` 事件，前端无需改协议；日志落 `workspace/knowledge/<version>/retrieval.jsonl`。

这条路线不新增节点到 LangGraph 也能先跑通（工具级扩展），Phase 4 再视需要加 `knowledge_router` / `context_evaluation` 节点——符合「不为了新架构删除现有可用功能」。

### 9.2 分阶段计划与验收标准

| Phase | 解决的实际问题 | 主要文件 | 验收命令 / 标准 |
|---|---|---|---|
| 1 | 指标口径不可约束、Schema 无稳定 ID | §8.1 第一批 | `unittest tests/test_metric_registry.py tests/test_schema_extractor.py`；13 条指标与真实外键逐条一致；`evaluate-rag --validate-only` 与 keyword/hybrid 基线不回退 |
| 2 | 召回无类型/版本过滤、无 SchemaContext | `retrieval/hybrid.py`、`schema_retriever.py`、`context_builder.py` | dev 集 Table/Column/Complete Schema Recall + 上下文 Token 报告；原 15 题 Recall@3/MRR 保持 1.000 |
| 3 | 多表查询漏中间表、错 Join | `graph_store.py`、`subgraph.py`、`knowledge_tool.py` | Join 路径正确率、必要中间表召回率；`tests/test_join_path.py` 覆盖 `orders→order_items→products` 与退款重复聚合风险 |
| 4 | 文档与数据无法联合分析、SQL 语义无校验 | `document_ingest.py`、`validation/*`、`registry.py` | 105 项离线测试不回退；文档规则遵循率、无证据结论比例有报告；API/SSE 兼容（现有 `tests/test_web.py` 全绿） |
| 5 | 知识更新后旧定义仍被召回 | `incremental.py`、`sql_experience.py` | 内容哈希只重建变化条目；改字段后失效指标/案例被标记；复用 `sql_skill.py:242-248` 的 provenance 思路 |
| 6 | 无法证明贡献 | `evals/knowledge-sql/`、`runner/evaluate_knowledge.py` | 真实结果文件 + 失败案例分析 + 可复现命令；测试题不进 few-shot/经验库 |

每阶段完成均按委托方要求输出 7 项交付（问题 / 文件 / 设计与取舍 / 关键代码与链路 / 真实测试结果 / 限制 / 下一阶段）。

### 9.3 如何设计实验验证新增知识检索模块的实际贡献

**对照组**（统一模型、数据库、题集、预算；只切换待比较模块）：

| 组 | 配置 |
|---|---|
| A | 原始 DeepEye-SQL（上游批量 pipeline，无业务口径） |
| B | 当前系统：指标 RAG（`EnterpriseKnowledgeBase`）+ DeepEye-SQL |
| C | B + 结构化指标注册中心 + Schema 感知双向检索 |
| D | C + GraphRAG 子图扩展 + Join 路径补全 |
| E | D + Agentic RAG 自适应补充检索 + 知识语义验证 |

**检索层指标**：Recall@K / MRR（复用 `app/commands/evaluate_rag.py:25-47` 的口径）、Table Recall、Column Recall、Complete Schema Recall、必要中间表召回率、Join 路径正确率、文档证据召回率、指标口径选择正确率、有效版本命中率、上下文 Token 数、检索耗时。

**端到端指标**：集合式 EX（复用 `runner/evaluation.py:29-46`，与 72%→76% 可比）、SQL 执行成功率（与 EX 分开统计）、业务语义正确率、文档规则遵循率、最终答案正确率、无证据结论比例、平均 SQL/LLM 调用次数、Token、平均与 P95 延迟。

**消融**（在 E 上逐一移除）：指标依赖扩展 / 双向 Schema 检索 / 图关系扩展 / SQL 经验检索 / 自适应补充检索 / 业务语义验证。

**防泄漏与公平性**：沿用 `runner/select_bird_sample.py` 的 `source_sha256` + 固定种子机制；`evals/knowledge-sql/test/` 的题目与标准 SQL 不得进入 `app/few_shot` 训练索引、`business_metrics.json` 别名或 Prompt 调优；标准答案由人工审核 + 参考 SQL + 确定性 Python 计算产生，**禁止用待评测 Agent 输出当标准答案**；dev/test 隔离，test 只在最终跑一次。BIRD-dev 50 题样本、gold SQL 与 EX 规则保持原样，确保与既有 76% 可比，且不把既有提升归因于新模块。

### 9.4 已知取舍

- 不实现 Microsoft GraphRAG 的社区发现/全局摘要：业务元数据图规模小（约 20 节点、十余条边），局部子图检索才是真实需求。
- 不引入新向量库：13→数百条知识量级下，现有 `EnterpriseKnowledgeBase` + Chroma 足够；若 Phase 2 实测 Recall/Token 不达标，再以实验数据论证替换。
- 不默认使用 `sqlglot`：当前未安装且 `pyproject.toml` 未声明；Phase 4 先用标准库 + SQLite `EXPLAIN` + 确定性规则做轻量校验，**是否新增依赖需用户明确批准**。
- 不启用多租户权限后台：保留 `ToolRegistry` 现有的 `database_id` 白名单与 `authorized` 接口，新增知识检索沿用同一权限上下文。

---

## 10. 当前已有测试和评测能否正常执行（真实结果）

环境：`Python 3.12.13`；已装 `fastapi 0.162.0 / langgraph / pandas 2.3.2 / chromadb 1.0.21 / networkx 3.5 / pydantic 2.14.1 / numpy 2.3.3 / torch 2.8.0+cpu`；**未装** `sqlglot`、`pytest`、`sqlparse`；`uv` CLI 0.11.32 可用；`node v22.22.2` 可用。CI 正式命令为 `unittest`，不依赖 pytest（`.github/workflows/ci.yml:31-34`）。

| 命令 | 真实结果 | 结论 |
|---|---|---|
| `python -m unittest discover -s tests -q` | `Ran 105 tests in 3.681s` · `OK` · exit 0 | 可执行 |
| `python -m compileall -q app runner tests` | exit 0 | 可执行 |
| `node tests/js/recovery.test.mjs` | `ok 55 assertions` · exit 0 | 可执行 |
| `python -m app.cli check` | `ready=true`；2 业务库 + 2 值索引 + few-shot 索引均就绪；8 份评测报告；`config/local/app-fast/config.toml` | 可执行，无需模型 |
| `python -m app.cli evaluate-rag --validate-only` | `valid=true, case_count=15` | 题单校验可执行（完整评测需模型端点） |
| `python -m app.cli evaluate --validate-only` | `valid=true, case_count=14` | 同上 |
| 50 题分层抽样复现 | `50/50/identical=True/seed=20261025` | 抽样确定性与既有产物一致 |
| `evals/metric-rag/latest/summary.json` | keyword `recall@3=0.5667, mrr=0.5667`；hybrid `1.0/1.0` | 既有结果可复核 |
| `evals/bird-dev-50/ablation/summary.json` | 仅生成 `36/50=0.72`；生成+修订 `36/50=0.72`；完整流程 `38/50=0.76` | 既有结果可复核 |

**未执行且有阻塞的部分**（如实说明）：所有在线评测（`evaluate` / `evaluate-rag` 非 `--validate-only`、BIRD 批量跑批、`.tmp-tests/probe_real_questions.py` 真实问题探针）都需要 DashScope 模型端点与配额，会写 `evals/**/latest` 并消耗 Token；本轮为 Phase 0 基线，按委托方「先分析、不大规模修改」的要求不触发。上一轮 `evals/bird-dev-10/report.md:14` 还记录过 provider 403 配额降级，说明在线复现存在真实外部依赖风险，Phase 6 需单独安排预算与重试策略。

---

## 11. 本阶段交付（按委托方 7 项要求）

1. **解决的实际技术问题**：确认了真实项目边界（上游 / 二次开发 / 待新增），定位「指标知识只进 Prompt、不进约束」这一根问题，核实了两个真实业务库与全部已有能力，并纠正了需求文本中与仓库不符的数字与模块描述。
2. **新增和修改的真实文件**：新增 `goals/phase0-rag-graphrag/GOAL.md`、`docs/Phase0-架构基线分析.md`；**未修改**任何现有源码、配置、`.env`、依赖或数据库（运行测试产生的 `__pycache__` 与临时库为既有测试行为，已被 `.gitignore` 忽略）。
3. **核心设计及技术选型理由**：goal-mode 契约 + 只读基线；GraphRAG 选 networkx（已安装、规模小、无需 Neo4j），知识层以新增包 + 可选开关接入，保留上游算法与现有 API/SSE/沙箱；详细理由见 §9.1、§9.4。
4. **关键代码和调用链路**：§1.2 九步链路、§3.3 evidence 注入链路、§5 LangGraph 状态/节点/工具、§9.1 四步最小改动路径，均附文件路径与关键函数名。
5. **实际执行的测试及结果**：§10 表格，E1-E10 全部有真实命令输出；105 项离线测试 OK、55 项 JS 断言 OK、启动自检 ready=true、50 题抽样完全一致。
6. **当前存在的限制和未解决问题**：§7 十项不足；`sqlglot`/`pytest` 未安装；在线评测受模型端点与配额约束；`evals/agent/latest` 的 14/14 为增量失败题回归而非整批独立成绩；文档间存在互相矛盾的测试项数与架构描述。
7. **下一阶段工作建议**：按 §8.1 与 §9.2 进入 Phase 1——先建 `app/knowledge/models.py` + `metric_registry.py` + `schema_extractor.py` 与对应单测，**不接入运行时主链路**；同时需要人工确认三件事（是否开始编码、是否允许 `sqlglot` 依赖、Phase 6 在线评测预算）。

---

## 12. 复现命令清单

```bash
cd <ROOT>
PY=./.venv/Scripts/python.exe

$PY -m unittest discover -s tests -q
$PY -m compileall -q app runner tests
node tests/js/recovery.test.mjs
$PY -m app.cli check
$PY -m app.cli evaluate-rag --validate-only
$PY -m app.cli evaluate --validate-only
$PY - <<'PY'
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path.cwd()))
from runner.select_bird_sample import select_sample, DEFAULT_DATABASES, DEFAULT_QUOTAS
ids = [r['question_id'] for r in select_sample(
    pathlib.Path('data/bird/dev/dev.json'), DEFAULT_DATABASES, DEFAULT_QUOTAS, 20261025)]
saved = json.loads(pathlib.Path('evals/bird-dev-50/sample.json').read_text(encoding='utf-8'))
print(len(ids), ids == saved['question_ids'])
PY
git status --short && git log --oneline -5 && git remote -v
```

> 契约状态：`goals/phase0-rag-graphrag/GOAL.md` 为 `PAUSED`（Phase 0 验收通过，等待人工放行 Phase 1）。
