# Phase 4B 交付报告：在线端到端准备与公平 Join 路径评测

> 交付日期：2026-11-13
> 代码基线：`DeepEye-SQL`（Phase 4A 完成后）
> 本轮定位：Phase 4B 的**离线可验证部分全部完成**；在线端到端（1 题 × 2 配置）**未执行**，因为规格要求先获批具体在线预算。

---

## 一、结论摘要

本轮不猜数字，先给可核对的结论：

| 项 | 结论 | 证据 |
|---|---|---|
| 运行时索引签名校验是否真生效 | **是**。`incompatible` / `stale` 会在读任何向量前中止；`partial` 放行并降级 | `app/skills/sql_skill.py::_guard_value_index`；`tests/test_value_index_signature.py` 9 项 |
| AW 与 Sales/HR 索引是否互不影响 | **是**。AW 用独立 `store_root_path`，Sales/HR 索引文件未被读写 | 见 §三 |
| GraphRAG 运行时是否真的用本地向量 | **此前不是**：Agent 侧默认 `mode="keyword"`、`dense_backend=None`。本轮加显式开关 `KNOWLEDGE_ENHANCEMENT_DENSE`，并修掉「每请求重编码 595 篇文档」的 348 秒缺陷（首次 329 s → 后续 0.6 s） | 见 §五 |
| 公平 Join 路径对照 | 两种方法 gold 边召回**同为 0.85 (17/20)**、Top-1 同为 **1/13**；差异只在**歧义标记 2/2 vs 0/2** | `workspace/evals/phase4b/join_paths.json` |
| 上一轮的「0 → 0.9286」能否当作路径算法提升 | **不能**。那是「不输出路径的配置」与「输出路径的配置」之差 | 本轮用同类方法对照证伪 |
| 在线端到端 SQL 正确率 | **未取得**。本轮零在线请求 | `workspace/evals/phase4b/budget_plan.json` 中 `online_requests_made: 0` |
| 单次运行最坏 LLM 请求数 | 原配置 **1593**；专用 smoke 配置 **40** | 见 §七 |

最重要的负面结果必须写在前面：**在这 18 道开发题上，GraphRAG 的多候选路径评分没有跑赢「纯外键最短路径」的边级召回。** 它唯一可测量的增量是把歧义显式标出来（2/2 命中，基线 0/2），以及少 1 条无关路径。这是真实结果，不修饰。

---

## 二、执行边界（为什么没有在线数字）

规格第三节要求「1 道开发题 × 2 种配置，共 2 次完整 Agent 运行」，第四节要求「实际在线调用前必须明确最大请求次数、Token、重试限制与费用预算，未获批准先做离线准备」。

本轮据此停在线之前：

- 已交付：预算测算、预检、执行脚本、两道授权门、结果比较协议、全部离线评测。
- 未执行：任何在线 LLM 调用。目录 `workspace/evals/phase4b/` 下不存在 `run_baseline.json` / `run_enhanced.json` / `summary.json`。
- 因此本轮**不产出**：SQL 执行成功率、Execution Accuracy、业务语义正确率、端到端延迟、Token 消耗。

---

## 三、配置与运行时兼容核查（规格 §2.1）

### 3.1 `store_root_path` 隔离是否成立

是。AdventureWorks 索引写在独立目录：

```
vector_database.store_root_path = workspace/runs/adventureworks-local/vector_database
Phase 4A 产物：.../adventureworks/local_index/manifest.json  (index_kind=partial)
```

`enterprise_sales` / `enterprise_hr` 的索引在**其他** run 目录下（`workspace/runs/bird-dev-*`），本配置文件从不指向它们，也没有任何代码路径会跨目录读取。

### 3.2 Sales / HR 索引是否仍然完整

`find workspace/runs -name manifest.json` 仍能列出既有各库 manifest（`bird-dev-baseline-cloud/vector_database/{enterprise_sales,enterprise_hr,...}`、`bird-dev-dashscope-qwen37/...`）。本轮对 `local_index.py` 的改动只是**新增**字段与状态判定，`write_local_index_manifest` 对旧调用保持原签名兼容，旧 manifest 缺 `index_kind`/`embedding_dim` 时按 `complete` 处理（有列即完整），不会让历史索引失效，这一点有专门测试：`test_legacy_manifest_without_phase4a_fields_still_works`。

### 3.3 全局 preflight 未全 ready 时能否只跑 AW

能，且这正是本轮采用的路径：`phase4b-smoke --config config/local/adventureworks-smoke.toml`，配置里 `store_root_path` 指向 AW 索引，`ENABLE_ADVENTUREWORKS=1` 让注册表加载 AW 数据源。preflight 显示：

```
adventureworks    → value_index_kind=partial, 5 表 / 7 列, ready=true
enterprise_sales  → missing
enterprise_hr     → missing
```

没有伪造索引、没有删安全检查、没有改全局默认配置。既有 `config/local/app-fast/config.toml` 与 `adventureworks-value-index.toml` 一字未动。

---

## 四、运行时签名校验（规格 §2.2）

### 4.1 此前的问题

`classify_index_status()` 只被 preflight 用来展示，SQLSkill 侧只判断 `manifest.json` 是否存在，以及 `embedding_provenance.json` 里的数据库 sha256。也就是说：**换一个 embedding 模型去查旧索引，不会被拦住**，只会静默返回按错误向量算出来的相似度。

### 4.2 本轮实现

`app/vector_db/local_index.py` 新增：

- `embedding_signature()`（v1，字段集与 Phase 4A 构建时一致，保证旧索引签名继续有效）；
- `canonical_embedding_signature()`（v2，把模型路径归一成目录名，避免「相对路径 vs 运行时展开的绝对路径」被误判为不兼容）；
- `verify_runtime_index()`：把签名、manifest 格式版本、数据库指纹合成一个判定，返回 `missing / incompatible / stale / partial / complete` 与 `reasons`。

`app/skills/sql_skill.py` 拆成两步，顺序很重要：

```python
guard = self._guard_value_index(cfg, index, db)      # 先守门：读向量之前就拒绝
schema = SchemaService().load_sqlite_schema(str(db))
self._record_value_index(guard, cfg, index, db, schema, destination)
```

`incompatible` / `stale` 抛 `ValueError` 并写明原因；`missing` 抛 `FileNotFoundError`；`partial` 放行。落盘 `value_index_status.json` 记录真实覆盖：`covered_tables`、`covered_columns`、`uncovered_text_columns`、`degradation`。

> 这个拆分是被回归测试逼出来的：一开始把校验放在 `load_sqlite_schema` 之后，
> `tests/test_sql_skill.py::test_request_paths_are_isolated_and_missing_index_fails_before_api`
> 立刻失败——缺索引时已经打开了 SQLite 连接，Windows 下临时库被占用无法清理。
> 顺序修对后恢复绿色，也保住了「缺索引必须在开库前失败」这条原有契约。

### 4.3 生效证据

真实 AW 索引（Phase 4A 构建）经运行时校验：

```
status=partial, reasons=[], embedding_model=data/models/Qwen/Qwen3-Embedding-0___6B,
embedding_dim=1024, signature_source=v1, covered_columns=7
```

9 项单元测试覆盖：缺 manifest、换模型、改 `max_value_length`、改数据库指纹、只建部分列、仅 v1 签名、v2 跨路径归一、旧版 manifest 兼容。

---

## 五、知识增强的检索通道（规格 §2.3）

### 5.1 核查结论（此前是「默认关键词」）

`app/agent/tools/registry.py` 原调用是：

```python
build_enhancement(request["question"], state["database_id"], on_stage=on_stage)
```

`build_enhancement` 的 `mode` 默认 `"keyword"`、`dense_backend` 默认 `None`。所以 **Agent 运行时跑的 GraphRAG 是纯关键词检索，Phase 2/3 里那个 1024 维本地 Qwen3 只活在评测脚本里**。规格 §2.3 要求「核实不是默认关键词模式」，这里如实回答：它**此前就是**默认关键词。

### 5.2 本轮实现：显式开关，不隐式加成本

`app/knowledge/enhancement.py` 新增：

- `DENSE_ENV = "KNOWLEDGE_ENHANCEMENT_DENSE"`；
- `dense_model_path_from_env()`：纯函数，`local`/`1`/`true` 解析为仓库内 `data/models/Qwen/Qwen3-Embedding-0___6B`，其他值按路径处理（相对路径按项目根展开）；
- `dense_backend_from_env()`：按模型路径缓存 `LocalSentenceTransformerBackend`，未设置时返回 `None`。

之所以做成开关而不是默认打开：本地 Qwen3 首次加载约 50 秒，且混合检索要为 595 篇知识文档做本地编码，不能让 Web 首请求隐式承担这个延迟。

`registry.py` 相应改为：

```python
dense_backend = dense_backend_from_env()
enhancement = build_enhancement(..., mode="hybrid" if dense_backend else "keyword",
                                dense_backend=dense_backend)
```

并把 `retrieval_mode` 与 `dense_backend` 写进 `observability`——否则「GraphRAG 生效」无法区分是关键词还是真向量，这一点在展示层也算证据。

### 5.3 通道对比（同一问题，同一知识产物，实测）

问题：`AW-DEV-001`「统计各商品类别的明细净销售额，按销售额从高到低排列。」

| 通道 | 耗时 | evidence 字符 | context tokens | 候选表 | 必要中间表 | Join 路径 | 歧义 |
|---|---|---|---|---|---|---|---|
| keyword（默认） | **0.18 s** | 1948 | 634 | 10 | 5 | 8 | 1 |
| hybrid（本地 Qwen3，首次） | **321.9 s** | 2069 | 707 | 12 | 4 | 8 | 2 |
| hybrid（同语料第二次） | **0.63 s** | 2069 | 707 | 12 | 4 | 8 | 2 |

两条通道都识别出 `Production.ProductSubcategory` 这个必要中间表。hybrid 把候选表从 10 扩到 12、歧义从 1 提到 2、上下文多 73 token。

产物：`workspace/evals/phase4b/dense_channel.json`，由 `phase4b-smoke --dense-compare` 生成（只做检索，不调用任何 LLM）。

### 5.4 一个必须修的性能缺陷（已修）

首次实测 hybrid **348 秒**，原因是 `DenseIndex` 懒编码：每次 `build_enhancement` 都新建 `HybridIndex`，于是把 595 篇知识文档**重新编码一遍**。这在 Web 请求里不可用。

修复：`build_hybrid_index()` 模块级缓存，键 = 后端 label + 文档内容指纹（`documents_fingerprint`，对 `doc_id`/`text` 做 sha256），上限 4 个索引；知识产物一变，指纹变，旧缓存自动失效，不会喂过期向量。

效果：首次 321.9 s（一次性语料编码），之后 0.63 s。缓存行为有 4 项测试锁住（同语料只编码一次、换语料重建、keyword 模式不编码、指纹对内容敏感）。

> 仍需注意：**冷启动第一次增强请求会付 300+ 秒**。生产化应做启动预热或离线预编码落盘，本轮未做（见 §十一.6）。

---

## 六、公平的 Join 路径离线评测（规格 §六）

### 6.1 为什么上一轮的 0.9286 不能用

Phase 2/3 的 A/B/C 三个配置**根本不输出 Join 路径**，D 输出。所以 `0 → 0.9286` 度量的是「有没有输出路径」，不是「路径算法准不准」。对比一个不做这件事的方法得到的 0，不能算算法优势。

### 6.2 对照方法

| 方法 | 说明 |
|---|---|
| `baseline_shortest` | 真实外键图上的**无权最短路径**，只按跳数排序，不看完整性 / 基数 / 关系角色 / 业务粒度 |
| `ours` | 现有 `JoinPathFinder`，评分含外键完整性、基数、来源、粒度、历史区间风险，歧义显式返回 |

### 6.3 公平性（代码保证，不是文字承诺）

1. **同一批端点对**：两者都调用 `build_endpoint_pairs`，`focus`/`targets` 来自同一份只由问题文本驱动的检索结果。
2. **同一张图**：都读 `workspace/knowledge/adventureworks/relations.json` 的同一 `table_projection`。
3. **同一预算**：`--max-hops 4` / `--max-paths 8` 同时作用于两者。
4. **同一角色约定**：每跳都取 `roles[0]`，所以「选错角色」是两者共性，不会被伪装成差距。
5. **标准答案不进检索**：`required_tables` / `acceptable_join_paths` 只在评分阶段读取。

### 6.4 题单

- `evals/adventureworks-graphrag/dev.json` 12 题（原有，未改动）
- `evals/adventureworks-graphrag/dev-join.json` 6 题（本轮新增，含：双地址角色、BillOfMaterials 多 Product 角色、复合外键、需补 ProductSubcategory、多可达路径）
- 封存 `test.json` 未读取、未执行、未用于调参（命令强制 `split == "dev"`）

**校验结果**：`all_valid: true`（18 案）。两条 warning 是环境事实，已单列而非隐藏：
- `AW-DEV-001`：`SalesOrderDetail.ProductID -> Product.ProductID` 在本 SQLite 移植库**未声明**（`PRAGMA foreign_key_list` 只有到 `SpecialOfferProduct` 的复合外键与到 `SalesOrderHeader` 的外键）；
- `AW-DEV-008`：`ProductCostHistory` 的「+ 日期区间」是谓词，不是外键。

这两条不计入外键召回分母，避免把「库缺外键」算成「算法召回失败」。

### 6.5 真实结果（`workspace/evals/phase4b/join_paths.json`）

分母：18 案中 13 案有可表示 gold 边，合计 **20** 条 gold 边（另 2 条不可表示，已排除）。

| 指标 | 分子/分母 | `baseline_shortest` | `ours` |
|---|---|---|---|
| 路径找到率 | 有路径案 / 有 gold 案 | 13/13 = 1.000 | 13/13 = 1.000 |
| gold 边召回（micro） | 命中边 / 可表示 gold 边 | **17/20 = 0.85** | **17/20 = 0.85** |
| Top-1 完整率 | Top-1 覆盖全部 gold 的案 / 13 | **1/13 = 0.0769** | **1/13 = 0.0769** |
| Top-1 边召回（micro） | Top-1 命中边 / 20 | 3/20 = 0.15 | 3/20 = 0.15 |
| 复合外键完整率 | 完整案 / 有复合要求的案 | 1/1 = 1.0 | 1/1 = 1.0 |
| 多角色选择正确率 | 无错角色路径的案 / 2 | 2/2 = 1.0 | 2/2 = 1.0 |
| 歧义识别率 | 标记案 / 应标案 | **0/2 = 0.0** | **2/2 = 1.0** |
| 无关路径总数 | — | 76 | 76 |
| 平均路径数/案 | — | 8.0 | 8.0 |

按题单拆开：

| 题单 | 方法 | 边召回 | Top-1 完整 | 无关路径 |
|---|---|---|---|---|
| `dev.json` (12) | baseline / ours | 8/8 = 1.0 | 1/7 = 0.143 | 40 / 39 |
| `dev-join.json` (6) | baseline / ours | 9/12 = 0.75 | 0/6 | 36 / 37 |

### 6.6 结论与根因

- **边级召回打平**。原因是两种方法开局都把 1 跳路径排在前面，输出集合几乎一致；`ours` 的语义评分在「路径条数已被 `max_paths` 截断」时没有机会体现。
- **歧义识别是真实增量**：2/2 vs 0/2。这是 `JoinPathFinder` 独有的多候选检测，其他指标测不出来。
- **Top-1 完整率 0.077 极低**，不是算法烂，而是任务定义苛刻：要求 Top-1 一条路径覆盖该题**全部** gold 边（3 条边的题必须一条路径串起 3 段）。
- **`AW-JOIN-001` / `AW-JOIN-002` 两边都 0 命中**，根因是**种子召回**而非路径排序：
  - `AW-JOIN-001` 问「按收货地址所在州…」，检索出的种子是 `SalesPerson / SalesOrderDetail / Customer / SalesOrderHeader`，`required_table_recall_from_question_only = 0.33`，`Address` / `StateProvince` 从未进入候选；
  - `AW-JOIN-002` 问「装配件 749 使用的组件」，种子是 `Product / ProductCategory / ProductInventory`，`BillOfMaterials` 缺失，召回 0.5。

  → 下一轮若要提升，重点在**问题到种子表的召回**，不在路径评分函数。

---

## 七、在线 Smoke Test 的入口与预算（规格 §三 / §四）

### 7.1 命令

```bash
# 离线：只算预算、查前置条件，零在线请求（默认行为）
python -m app.cli phase4b-smoke --config config/local/adventureworks-smoke.toml

# 在线：必须同时给两个开关与两个硬上限
python -m app.cli phase4b-smoke --config config/local/adventureworks-smoke.toml \
  --case-id AW-DEV-001 --bands baseline,enhanced \
  --execute --approve-online --max-tokens 60000 --max-seconds 900 --max-llm-calls 100
```

### 7.2 预算实测（来自真实配置字段，不是估的）

| 阶段 | 调用数来源 | 原配置（budget=8） | smoke 配置（budget=1） |
|---|---|---|---|
| planner | `MAX_TOOL_ROUNDS=6` + 1 | 7 | 7 |
| few_shot_preparation | preliminary + prepare | 2 | 2 |
| schema_linking | direct + reversed | 16 | 2 |
| sql_generation | dc + skeleton + icl | 24 | 3 |
| sql_revision（最坏） | 候选 × 8 checker × budget | 1536 | 24 |
| sql_selection | C(top_k,2) × evaluator | 8 | 2 |
| **单次运行合计** | | **1593** | **40** |

- 原配置（`adventureworks-value-index.toml`，从 BIRD max-quality 档派生）单次最坏 1593 请求，**不适合**一次受预算约束的 smoke test。
- 因此新建 `config/local/adventureworks-smoke.toml`：只改运行预算与 few-shot 路径，`store_root_path` 仍指向 Phase 4A 那份真实 partial 索引。两个对照配置共用此文件，唯一变量是 `ENABLE_KNOWLEDGE_ENHANCEMENT`。

### 7.3 前置条件（`budget_plan.json` 真实输出）

```
database_file        ok
value_index          ok  status=partial  covered_columns=7  signature_source=v1
few_shot_index       ok  workspace/runs/_shared/localqwen/bird-train100/manifest.json
knowledge_artifacts  ok
case_is_dev          ok
reference_sql_verified ok
ready_for_online: true
```

其中 few-shot 索引是本轮新建的：原 `train_full_index` 只有 `.build_checkpoint`、**没有 manifest**，SQLSkill 会直接 fail-fast。本轮用本地 Qwen3 重建了 100 条 BIRD 索引（`skip_mask_llm=True`，零在线调用，耗时 169 秒）。

### 7.4 两道授权门

`--execute` 必须同时给 `--approve-online` 与 `--max-tokens` / `--max-seconds`，否则在**加载配置之前**就报错。上限不是「跑完再检查」：`max_tokens` / `max_seconds` 直接传给 `SQLSkill`，超限由流水线自己抛 `BudgetExceededError` 中止。

---

## 八、证据采集（规格 §五）

每次运行落 `run_<band>.json`，字段与规格一一对应：

| 规格字段 | 实现位置 |
|---|---|
| `run_id` / `question_id` / `database_id` / `config_name` | `run_band` + `run()` |
| `knowledge_enhancement_enabled` | band 映射 |
| Agent 工具调用序列 | `AgentResult.tool_trace` |
| Value Retrieval 实际命中/未覆盖字段 | SQL Skill artifact `value_index_status.json` |
| SchemaContext / 种子表 / 图扩展 / Join 路径 / 业务风险 | `observability`（`final_schema_context`、`original_seeds`、`graph_added_nodes`、`candidate_join_paths`、`grain_risks`） |
| DeepEye-SQL 各阶段状态 | `on_stage` 事件 + `stage_seconds` |
| 最终生成 SQL / 实际执行 SQL | `AgentResult.sql`（**只用模型生成的 SQL**） |
| 查询结果 / 最终回答 | `AgentResult.rows` / `answer` |
| 参考 SQL 与参考结果 | `reference_sql` + 只读执行得到的 `reference_rows` |
| 执行成功与否 / 结果是否正确 | `sql_executed` / `result_correct`（走显式比较协议） |
| 业务语义是否正确 | 与 `result_correct` 分开记录，**不因执行成功就判正确** |
| LLM 调用次数 / Token / 延迟 | `model_calls` / `usage` / `agent_elapsed_seconds` |
| 失败阶段及原因 | `failed_stage` / `error`（Agent 入口失败也记为完整链路失败） |

关键防作弊点：

- `reference_sql` **只**用于离线算参考结果，从不作为 Agent 输入；
- `evidence_in_prompt` 从 SQL Skill 落盘的 `dataset.snapshot/items.jsonl` 读回真实 `evidence`，用来证明 hint 真的进了流水线入参，而不是只在 enhancement 里自转；
- 有单元测试证明 `ENABLE_KNOWLEDGE_ENHANCEMENT=1` 时 `[企业知识增强 Schema 上下文` 出现在 `request["evidence"]`，且对 `enterprise_sales` 不出现。

### 8.1 结果比较协议（不把「执行成功」当「业务正确」）

显式规则，写死在 `compare_results`：

- 数值：`|a-b| <= max(abs_tol, rel_tol * max(|a|,|b|))`，默认 `abs_tol=0.01`（覆盖 `ROUND(x,2)` 与未取整的显示差异）、`rel_tol=1e-6`；
- 文本：去空白、小写后比较；
- NULL 只与 NULL 相等；
- 行数不等 → 直接不正确；
- 同时输出 `ordered_match` 与 `unordered_match`，结论以 `ordered_match` 为准（参考 SQL 带 `ORDER BY`），但两个都记录，便于区分「排序错」与「口径错」；
- 空结果对空期望算正确，空结果对非空期望算错误。

---

## 九、测试与回归

| 项 | 结果 |
|---|---|
| 全量 unittest | **273 项通过**（Phase 4A 的 226 + 本轮新增 47） |
| 新增测试文件 | `test_value_index_signature.py` (9)、`test_phase4b_smoke.py` (14)、`test_join_path_eval.py` (12)、`test_knowledge_dense_switch.py` (12) → **47 项** |
| 既有测试调整 | `test_cli.py`（子命令集合 +2）、`test_graphrag_acceptance.py`（从「恰好 10 个」改为「原有 10 个齐全 + 新增 2 个」） |
| compileall | `app runner tests scripts` 通过 |
| node 回归 | `ok 55 assertions` |
| `app.cli check --quiet` | `ready` |
| 历史题单校验 | `evaluate-rag` 15 题 valid、`evaluate` 14 题 valid、`evaluate-graphrag` dev 12 题、`evaluate-join-paths` 18 案 all_valid |
| 默认知识指纹 | 仍为 `96f7f2cb2ba1…a58`（13 指标 / 11 表 / 57 列 / 152 边），未被本轮改动影响 |
| 历史评测产物 | 未覆盖（本轮新结果全部写 `workspace/evals/phase4b/`） |

规格 §八 的 14 项必需测试对照：

| # | 要求 | 覆盖 |
|---|---|---|
| 1 | 值索引字段白名单 | Phase 4A `test_value_index_policy` |
| 2 | 敏感字段排除 | Phase 4A 同上 |
| 3 | dry-run 与预算限制 | Phase 4A 同上 |
| 4 | 部分值索引 manifest | `test_value_index_signature.test_partial_index_is_allowed` |
| 5 | 未覆盖字段安全降级 | Phase 4A + `test_sql_skill` 既有覆盖 |
| 6 | Embedding 模型不一致检测 | `test_other_embedding_model_is_incompatible` / `test_v2_signature_matches_...` |
| 7 | 复合外键完整与缺失 | `test_composite_condition_expands_into_two_pairs` / `test_composite_member_uses_declared_relation` |
| 8 | 错误重复聚合与合法预聚合 | 留给 Phase 4C（本轮不开发验证器） |
| 9 | 历史成本区间匹配 | 同上（本轮只把「区间谓词不进外键分母」写进分类） |
| 10 | 多层子查询及 CTE | 同上 |
| 11 | 指标审核状态与版本失效 | Phase 2/3 `test_knowledge_dictionary_review` 已覆盖 |
| 12 | GraphRAG 关闭时旧流程兼容 | `test_knowledge_dense_switch.test_default_is_keyword_only` + 既有 `sales` 不注入断言 |
| 13 | SQL 修复次数限制 | Phase 4C |
| 14 | 只读安全沙箱不回退 | `test_sql_skill` 全绿 |

---

## 十、修改文件清单（本轮）

修改：

| 文件 | 变更 |
|---|---|
| `app/vector_db/local_index.py` | 新增 v1/v2 签名、`verify_runtime_index`、状态常量、`LocalValueIndex` 记录 `embedding_dim` |
| `app/vector_db/__init__.py` | 导出新符号 |
| `app/skills/sql_skill.py` | 拆分 `_guard_value_index` / `_record_value_index`，在校验失败时于开库前中止 |
| `app/knowledge/enhancement.py` | `KNOWLEDGE_ENHANCEMENT_DENSE` 开关、`dense_model_path_from_env`、`dense_backend_from_env`、observability 记录检索通道 |
| `app/agent/tools/registry.py` | 按开关选择 `keyword` / `hybrid` 并传 `dense_backend` |
| `app/cli.py` / `app/commands/__init__.py` | 注册 `evaluate-join-paths`、`phase4b-smoke` |
| `tests/test_cli.py` | 子命令集合同步 |
| `tests/test_graphrag_acceptance.py` | 子命令断言从「恰好 10 个」改为「原有 10 个齐全 + 新增 2 个」 |

新增：

| 文件 | 说明 |
|---|---|
| `app/commands/evaluate_join_paths.py` | 公平 Join 路径对照命令 |
| `app/commands/phase4b_smoke.py` | 在线 Smoke Test（默认离线预算规划） |
| `evals/adventureworks-graphrag/dev-join.json` | 6 道高难度 Join 开发题 |
| `tests/test_value_index_signature.py` | 9 项 |
| `tests/test_phase4b_smoke.py` | 14 项 |
| `tests/test_join_path_eval.py` | 12 项 |
| `tests/test_knowledge_dense_switch.py` | 12 项（含稠密索引缓存 4 项） |
| `config/local/adventureworks-smoke.toml` | 低预算专用配置（gitignore 内，未改既有配置） |

未改：`app/pipeline/`、`app/db_utils/`、依赖、`.env`、`business_metrics.json`、历史评测产物、封存 `test.json`。

---

## 十一、未解决的问题

1. **在线端到端未跑**：无 SQL 正确率、无 Token、无延迟。阻塞在预算授权。
2. **路径评测饱和**：`max_paths=8` 对两方法都是硬顶，边召回打平。要区分算法优劣，需要更大的路径预算或「按 gold 边定位路径」的评测口径。
3. **种子召回是瓶颈**：`AW-JOIN-001`(0.33)、`AW-JOIN-002`(0.5) 的失败来自问题→表召回，不是路径评分。未做改动。
4. **移植库缺外键**：`SalesOrderDetail.ProductID -> Product.ProductID` 在真实 AW 存在、在此 SQLite 移植库未声明。gold 标签保留原样并单列为 `unreachable`，未篡改题单。
5. **few-shot 语料是 BIRD**：100 条 BIRD 示例与 AdventureWorks 不同域，few-shot 检索质量可能拖累绝对正确率。两个 band 共用同一索引，公平性成立，但绝对分不宜外推。
6. **dense 通道冷启动 300+ 秒**：首次请求要为 595 篇文档做本地编码（缓存后 0.6 s）。未做启动预热或离线预编码落盘；在线 smoke 若要跑 enhanced band，须把首次 `--max-seconds` 放宽或先预热一次。
7. **指标 trusted 仍为 0**：14 条审核清单未人工批准，`trusted=False` 全程未用作硬约束。
8. **Join 评测缺 Baseline2**：规格允许的「LLM 直接输出 Join 路径」基线需要在线配额，本轮未做。因此「我们的路径评分优于 LLM 直出」目前**没有证据**。

---

## 十二、下一轮的预算建议（3–5 题）

基于 smoke 配置（40 请求/次最坏）的真实推演：

| 规模 | 配置数 | 运行次数 | 最坏请求 | 建议 `--max-tokens`/次 | 建议 `--max-seconds`/次 | 建议 `--max-llm-calls` |
|---|---|---|---|---|---|---|
| 1 题（本轮计划） | 2 | 2 | 80 | 60,000 | 900 | 100 |
| 3 题 | 2 | 6 | 240 | 60,000 | 900 | 300 |
| 5 题 | 2 | 10 | 400 | 60,000 | 900 | 500 |

建议顺序：先用 1 题打通（`AW-DEV-001`，中间表场景），确认链路能出 SQL 与结果，再扩到 3 题（补 `AW-DEV-004` 多路径、`AW-DEV-011` 粒度口径），最后 5 题。

不批准也能继续的事：Phase 4C 规则型语义验证器（不引 sqlglot）、种子召回改进（纯离线）、指标人工审核工作流。

---

## 十三、可复现命令

```bash
# 1) 公平 Join 路径对照（离线，零在线配额）
python -m app.cli evaluate-join-paths --validate-only
python -m app.cli evaluate-join-paths --output workspace/evals/phase4b/join_paths.json

# 2) Smoke Test 预算与前置条件（离线，零在线请求）
python -m app.cli phase4b-smoke --config config/local/adventureworks-smoke.toml

# 2b) 知识增强两条检索通道对比（离线，只检索不调 LLM；首次约 330 s 为语料编码）
python -m app.cli phase4b-smoke --config config/local/adventureworks-smoke.toml --dense-compare

# 3) 在线端到端（仅在预算获批后执行）
python -m app.cli phase4b-smoke --config config/local/adventureworks-smoke.toml \
  --case-id AW-DEV-001 --bands baseline,enhanced \
  --execute --approve-online --max-tokens 60000 --max-seconds 900 --max-llm-calls 100

# 4) 回归
python -m unittest discover -s tests -q
python -m compileall -q app runner tests scripts
node tests/js/recovery.test.mjs
python -m app.cli check --quiet
```

结果文件：

- `workspace/evals/phase4b/join_paths.json`（18 案 × 2 方法）
- `workspace/evals/phase4b/budget_plan.json`（预算 + 前置条件，`online_requests_made: 0`）
- `workspace/evals/phase4b/dense_channel.json`（keyword / hybrid / hybrid_warm 三通道实测）
- `workspace/evals/phase4b/run_baseline.json` / `run_enhanced.json` / `summary.json`（**仅在线执行后生成，本轮不存在**）
