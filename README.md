<div align="center">

# 企业数据分析 Agent

**FastAPI · LangGraph · RAG · Text-to-SQL · SQLite · SSE**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-SSE-009688?logo=fastapi)
![SQLite](https://img.shields.io/badge/SQLite-ReadOnly%20%2B%20Authorizer-6e40c9?logo=sqlite)
![LangGraph](https://img.shields.io/badge/LangGraph-Agent-1C3C3C)
![Tests](https://img.shields.io/badge/Tests-88%20passed-success)

🖥️ **本地演示项目**：不依赖云端部署，`python runner/run_demo.py` 起服务后浏览器直接看效果

</div>

> 基于开源 DeepEye-SQL 引擎构建的对话式企业数据分析系统，覆盖销售与人力资源场景，支持自然语言查数、排名、趋势、占比、多轮追问及结果自动可视化。

---

## 项目概述

本项目面向企业自然语言问数场景，将 DeepEye-SQL 的 Text-to-SQL 能力封装为可由 LangGraph Agent 调用的分析工具，并补齐业务口径检索、安全执行、过程可观测、多轮交互与 Web 演示能力。

- **业务口径 RAG**：针对用户问法多样、企业指标口径难以仅靠关键词匹配的问题，构建关键词与向量混合检索，加入加权重排、向量缓存和故障降级。在自建的 15 题指标问法开发集上，Recall@3 和 MRR 均由 **0.567 提升至 1.000**。
- **Agent 化 Text-to-SQL**：将值检索、Few-shot、Schema Linking、SQL 生成、修订和选择封装为单问题调用能力，由 LangGraph Agent 按需调用业务口径、Schema、SQL 和图表工具。在 BIRD-dev 五库固定分层抽样的 50 题评测中，完整流程集合式执行准确率为 **76%**，仅生成方案为 **72%**。该结果仅代表本仓库固定样本与评测口径，不等同于 BIRD 官方榜单成绩。
- **只读执行与资源保护**：针对模型生成 SQL 的执行风险，设计 SQLite 只读执行沙箱，组合只读连接、`query_only` 与执行权限白名单，仅允许查询和必要的元数据读取，阻止写入、附加数据库及扩展加载；同时设置 SQL 超时、结果行数和请求 Token／时间上限。
- **可演示交互闭环**：FastAPI Web 页面通过 SSE 展示阶段进度与用量，支持断线重连、页面刷新后的任务状态恢复，以及表格、指标卡和常用图表自动呈现。

一句话概括：**这是一个以 RAG 增强业务口径、以 LangGraph 编排 Text-to-SQL、并具备安全执行与可观测 Web 交互的企业数据分析 Agent。**

---

## 📸 界面预览

| 结果页 · 三版 SQL 与阶段耗时 | 运行中 · 流水线进度与实时用量 |
|---|---|
| ![结果页](./docs/screenshots/结果页-三版SQL与阶段耗时.png) | ![运行中](./docs/screenshots/运行中-流水线进度与实时用量.png) |

> 📖 **答辩用一页纸说明**（三种启动方式、页面结构、故障排查、推荐演示问题）：[docs/demo-guide.md](./docs/demo-guide.md) · **交付范围界定**：[docs/internship-release-checklist.md](./docs/internship-release-checklist.md)

## 🧭 本仓库做了什么

本仓库是独立的企业数据分析 Agent 应用。[HKUSTDial/DeepEye-SQL](https://github.com/HKUSTDial/DeepEye-SQL) 仅作为底层 **Text-to-SQL 引擎**：DC / Skeleton / ICL 生成策略、八个 Checker 判定逻辑、一致性过滤 + LLM 投票选择、渐进式 schema 裁剪等推理算法沿用上游。Agent 架构并不来源于 DeepEye-SQL，而是采用 ChatOpenAI、LangGraph 标准消息、ToolNode 和 MCP 工具边界。

| 改动 | 说明 | 位置 |
|---|---|---|
| 只读执行沙箱 | `mode=ro` 只能保护主库，挡不住 `ATTACH` 一个可写库。补 `PRAGMA query_only=ON` + `set_authorizer` 白名单（仅放行 SELECT/READ/FUNCTION/RECURSIVE 与元数据 PRAGMA），显式 DENY `load_extension`，并用 URI 规范化数据库路径 | `app/db_utils/execution.py`、`tests/test_sql_skill.py` |
| 嵌套线程池死锁修复 | dc/skeleton 子任务曾被提交到与 item 级相同的线程池，池被占满后子任务永久排队（现象：跑批 0 进度冻结）。改为当前线程顺序执行，并保留 item 级并行 | `app/few_shot/preliminary_sql.py` |
| 延迟与成本熔断 | 每请求 token / 墙钟硬上限（默认 120k / 600s）；当一次响应的全部候选都被 `max_tokens` 截断（思考模型典型失控形态）时抛 `TruncatedResponseError` 立即失败，不再在 tenacity × extractor 两层重试里循环小时级。实测同一失控请求由 **75 次调用** 收敛为 **1 次** | `app/llm/budget.py`、`app/llm/llm.py`、`app/llm_extractor/extractor.py` |
| 逐调用用量台账 | 基于 ContextVar 记录每次 LLM / embedding 调用的阶段、题目、耗时与 token，自定义 `ThreadPoolExecutor` 保证跨线程池传播；请求产物目录落 `usage.jsonl`，可精确回答"时间花在哪一阶段" | `app/llm/usage.py`、`app/vector_db/metered_embedding.py` |
| 单问题 Skill 封装 | 把批量评测脚本改造成一次请求一个 Skill 调用：独立产物目录、七阶段进度回调、异常统一收敛为 `failed_stage` + 本地 `error.txt`（密钥脱敏），对外响应不含原始 provider 报错 | `app/skills/sql_skill.py` |
| LangGraph 数据分析 Agent | `ChatOpenAI.bind_tools()` 在标准 ReAct 循环中选择业务口径、Schema、Text-to-SQL 和可视化工具；SQLite Checkpointer 按 `conversation_id + database_id` 持久化会话状态 | `app/agent/`、`app/persistence/` |
| MCP 工具服务 | 通过官方 MCP Python SDK 暴露数据库发现、业务口径、Schema 和只读 Text-to-SQL，供其他 Agent/MCP Client 复用 | `app/mcp_server.py`、`runner/run_mcp_server.py` |
| 双业务域演示数据与指标 RAG | 固定随机种子生成销售 7 表与 HR 4 表；指标检索按业务域隔离，Agent 可自动选择 `enterprise_sales` / `enterprise_hr`，手动指定优先 | `app/enterprise/`、`runner/create_enterprise_demo.py` |
| Web 应用 | FastAPI + 原生单页（零构建）：串行任务队列、SSE 进度流、断线重连、按请求 ID 回放；展示意图、业务口径、三版 SQL、阶段耗时、结果表与自动图表 | `app/web/`、`runner/run_demo.py` |
| 持久化会话与任务 | 独立状态库保存 LangGraph Checkpoint、会话目录、任务结果和 SSE 事件；重启后已完成会话可继续追问，遗留 `queued` / `running` 安全标记为 `interrupted`，不自动重复调用模型 | `app/persistence/`、`app/web/task_manager.py` |
| 刷新不丢任务 | 页面刷新只重建前端，不取消也不重复提交后端任务：`sessionStorage` 只存 `request_id` / `conversation_id` / 问题文本等白名单字段，初始化时查 `GET /api/query/{id}`，按 `queued` / `running` / 终态分别重连 SSE、回填结果或提示失效；本地无记录时退化为问 `GET /api/query/active` 接手服务端正在跑的任务，新标签页因此不会提重复任务 | `app/web/static/recovery.js`、`app/web/static/chat.js`、`app/web/task_manager.py` |
| 启动自检 | `--check-only` 输出数据库、值索引、few-shot 索引、离线报告的就绪状态 JSON，不泄漏任何凭据 | `app/web/preflight.py` |
| 评测基础设施 | 固定种子分层抽样、三档消融报告（仅生成 / 生成＋修订 / 完整流程）、阶段实测计时、进度看板、基线报告与逐题明细落盘为可只读浏览的 `evals/` | `runner/report_ablation.py`、`runner/report_baseline.py`、`runner/select_bird_sample.py` |
| 配置分层 | 按模型组织模板；`demo-fast` 用低延迟档（采样预算 4→1、few-shot 7→5）与评测口径配置分离；本地实验配置与被忽略的密钥统一放在 `config/local/`；端点与密钥可用 `.env` 覆盖（shell > .env > TOML） | `config/template/`、`app/config/config.py`、`app/config/env.py` |
| 业务口径混合 RAG | 关键词基线 + 向量语义召回 + 加权重排，支持命中分数解释、查询/语料向量缓存和向量故障自动降级 | `app/enterprise/knowledge.py`、`runner/evaluate_metric_rag.py` |

## ✨ 核心能力

| 能力 | 说明 |
|------|------|
| 🔍 值检索接地 | LLM 抽取关键词 → 向量检索库内真实值 → 把取值样例写回 schema，缓解"字段存在但值对不上" |
| 🧬 三路 SQL 生成 | Divide-and-Conquer、Skeleton、ICL 三种策略并行采样，产出多个候选 |
| 🛠️ Checker 修订 | 八个基于 SQL 错误模式的检查器（语法 / JOIN / ORDER BY+LIMIT / MIN-MAX / NULL / 时间 / SELECT / 结果空）逐个判定并修复 |
| 🗳️ 一致性选择 | 按执行结果分组统计一致性，超过阈值直接选定，否则交 LLM 两两比较投票 |
| 🔒 安全执行 | 只读连接 + authorizer 白名单 + SQL 超时 + 返回行数上限 + 每请求 token/时间熔断 |
| 📡 实时进度 | SSE 推送阶段起止与每次模型调用的累计用量；断线自动重连并回退到轮询 |
| 📊 可复现评测 | 固定种子抽样、三档消融、集合式与严格多重集双指标、逐题 SQL 与结果预览留档 |
| 🧾 成本可观测 | 阶段耗时条、token 归因、模型响应数、候选数在同一页面呈现，可定位瓶颈 |
| 🗄️ 数据库白名单 | 当前同时暴露企业销售与 HR 两个已就绪业务库；Agent 自动路由，且不允许跨数据库取表或串用指标 |
| 🧭 LLM Agent 决策 | 模型根据系统 Prompt、历史消息和工具描述自主选择动作，工具结果返回模型后再规划或形成最终回答 |
| 📈 自动可视化 | 按意图和结果列自动选择指标卡、柱状图、折线图、饼图或表格；用户点名图表类型时可传 `chart_type`（白名单校验，数据不支持自动回退并说明）；前端使用本地 SVG/CSS，无 CDN 依赖 |
| 🧠 业务口径检索 | 关键词基线 + 云向量语义召回 + 加权重排，覆盖销售额、客单价、退款率等定义和 SQL 约束；返回可解释分数，向量异常自动降级 |
| 💾 持久化记忆 | LangGraph Checkpoint、会话目录、任务和 SSE 事件统一写入独立 SQLite 状态库；支持历史会话查看、删除和服务重启后继续追问 |

## 🏗️ 系统架构

```
浏览器（原生单页，零构建）
   │  POST /api/chat                     ┌─────────────────────────────────┐
   ├─ 202 request_id ───────────────────►│ TaskManager  串行任务队列         │
   │  GET /api/query/{id}/events ◄─SSE───│  · 事件可回放（断线重连不丢进度） │
   │  GET /api/query/{id}                │  · SQLite 状态与 SSE 事件持久化   │
   └─────────────────────────────────────└───────────────┬─────────────────┘
                                                         ▼
                      LangGraph：ChatOpenAI ◄───────────────┐
                               │ tool_calls                │ tool results
                               ▼                           │
             业务口径 / Schema / Text-to-SQL / 可视化工具 ──┘
                                      │
                                      └─ SQLSkill.run()：七阶段串行
   ┌──────────────┬───────────────────┬──────────────┬────────────────┐
   │ value_       │ few_shot_         │ schema_      │ sql_generation │
   │ retrieval    │ preparation       │ linking      │ (DC/Skeleton/  │
   │ 关键词→真实值│ 初步SQL+掩码+检索 │ 正反向链接   │  ICL 三路采样) │
   └──────────────┴───────────────────┴──────────────┴────────┬───────┘
                                                    ┌──────────────────┐
                                                    │ sql_revision      │
                                                    │ sql_selection     │
                                                    │ execution（只读） │
                                                    └───────────────────┘
                                                         │
                                  结果 ──► 可视化节点 ──► 指标卡/柱状/折线/饼图/表格
              每次模型/向量调用 → usage.jsonl（阶段、题目、耗时、token）
              每请求产物 → workspace/demo_requests/<request_id>/（快照 + result.json + error.txt）

外部依赖：DashScope OpenAI 兼容端点 deepseek-v4.1-flash-1017（可关思考） · qwen3.7-text-embedding-flash 向量 · 本地 SQLite
```

## 🚀 快速开始

```bash
# 1. 安装依赖
uv sync                       # 或 pip install -e .

# 2. 准备配置（config/local/ 被 git 忽略，需自备模型服务地址与密钥）
#    模板见 config/template/<模型>/，本地实验配置放 config/local/<实验>/
#    密钥两种放法，任选其一：
#      a) 直接写本地 TOML 的 profile（默认方式，换终端也不会 401）
#      b) cp .env.example .env 后填 OPENAI_API_KEY（适合 CI / 多环境切换）
#    优先级固定为：shell 环境变量 > .env > TOML；三者都不填则无法调用模型服务

# 3. 生成可复现企业示例库（首次运行）
uv run python -B runner/create_enterprise_demo.py

# 4. 为两个企业库建立值索引（首次运行，CONFIG_PATH 使用自己的本地配置）
$env:CONFIG_PATH="config/local/demo-fast/config-bird-demo.toml"
uv run python -B runner/complete_value_index.py --database data/enterprise_sales/enterprise_sales.sqlite
uv run python -B runner/complete_value_index.py --database data/enterprise_hr/enterprise_hr.sqlite

# 5. 启动前自检：数据库、值索引、few-shot 索引、离线报告是否就绪
uv run python -B runner/run_demo.py --check-only

# 6. 启动演示站
uv run python -B runner/run_demo.py --config config/local/demo-fast/config-bird-demo.toml
#    等价：uvicorn app.web.server:create_app_from_env --factory --port 8000

# 7. 打开 http://127.0.0.1:8000
```

**配置从哪来（优先级由高到低）**：

| 来源 | 位置 | 用途 |
|---|---|---|
| shell 环境变量 | 当前终端 | 临时覆盖，优先级最高 |
| `.env` | 仓库根目录，`cp .env.example .env` 后填写 | 本地与 CI 的统一入口，不入库 |
| TOML | `config/local/<实验>/config.toml` | 完整配置与默认值 |

支持的变量只有这几个，其余字段一律走 TOML：`CONFIG_PATH`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`DEFAULT_LLM_MODEL`、`EMBEDDING_MODEL`、`DEMO_MAX_TOTAL_TOKENS`、`DEMO_MAX_SECONDS`。`.env` 在 `app.config` 导入时读取，文件缺失或解析失败都不会阻断启动；`--check-only` 会报告是否命中 `.env`（只报变量个数，不回显变量名和值）。

用 `DEFAULT_LLM_MODEL` 换模型时只改模型名：同一端点下 TOML 声明的 `extra_body`（如 `enable_thinking=false`）会保留，只有同时换 `OPENAI_BASE_URL` 才会清空它——否则关思考开关会被静默丢掉，单题从 60 秒级退化到 600 秒超时。

**单题命令行入口**（不启服务，直接看 JSON 结果与产物目录）：

```bash
python runner/run_sql_skill.py --config <toml> --database data/enterprise_sales/enterprise_sales.sqlite \
  --database-id enterprise_sales --question "统计2026年有效订单的销售额、订单量和平均客单价。"
```

**企业 Agent 命令行入口**：

```powershell
uv run python -B runner/run_agent.py --config config/local/demo-fast/config-bird-demo.toml --question "按月统计2026年的销售额和订单量变化趋势。"
```

**MCP Server**（stdio，供 Claude Desktop、Codex 或其他 MCP Client 配置）：

```powershell
uv run python -B runner/run_mcp_server.py --config config/local/demo-fast/config-bird-demo.toml
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `--config` | `demo-fast` 档 | 决定模型服务、索引路径与采样预算 |
| `--host` | `127.0.0.1` | 仅本机；局域网投屏时显式 `--host 0.0.0.0` |
| `--port` | `8000` | 端口占用时换一个 |
| `DEMO_MAX_TOTAL_TOKENS` / `DEMO_MAX_SECONDS` | `120000` / `600` | 单请求熔断阈值，环境变量可临时放宽 |

> ⚠️ **启动前确认模型服务可达**：`curl <base_url>/models` 自检。演示站是**单进程串行队列**，同一时刻只处理一个请求，后提交的会排队。

**刷新页面不会取消后端任务**。任务跑在服务端线程里，浏览器只是订阅者；刷新后页面用 `sessionStorage` 里的 `request_id` 查一次 `GET /api/query/{request_id}`，再按状态决定动作：`queued` / `running` 用原 ID 重连 SSE（不重新提交、不重复消耗 Token），`success` / `empty_result` 直接回填回答、图表、表格、SQL 与耗时，`error` 恢复失败卡片，`interrupted` 提示后端重启并保留问题文本。记录里只存白名单字段，不落 SQL 结果、模型原始响应、密钥或绝对路径。

**新开一个标签页也能接手正在跑的任务**。`sessionStorage` 是标签页私有的，新页面天然是空的，所以页面在本地没有记录时会问一次 `GET /api/query/active`：服务端串行队列返回当前 `queued` / `running` 的任务，新标签页据此直接接管（沿用原 `conversation_id` 与数据源），而不是再提一个重复任务把队列占满。任务结束后可以从左侧“历史对话”恢复结果和上下文。

**服务重启后仍可继续历史会话**。Web 应用将 LangGraph Checkpoint、会话目录、任务结果和 SSE 事件写入 `workspace/state/agent_state.sqlite`；左侧“历史对话”可以恢复已完成轮次并继续追问。旧版 `workspace/demo_tasks/*.json` 会在首次启动时幂等导入。运行中任务不会自动重放外部模型调用，而会转成 `interrupted` 等待用户确认重试。

## ⚠️ 能力边界（未实现与未解决的部分）

写在这里是为了避免被当成"什么都能做"的演示：

- **推理算法未改进**：生成、修订、选择三阶段的策略完全沿用上游，本仓库不宣称任何算法创新。
- **修订阶段当前测不出收益**：50 题三档消融中，仅生成与生成＋修订两档**完全同为 36/50**，Checker 修订造成 0 修复 / 0 退化。八个检查器都基于 SQL 正则错误模式（如 `ORDER BY` 内使用 `MIN/MAX`、`= (SELECT ... LIMIT n)`），演示实测中该阶段常以零次模型调用、0.07 秒通过，说明模式未命中生成的候选。这是已知薄弱环节，不能表述为"修订能力已验证"。
- **评测口径不等于 BIRD 官方榜单**：集合式 EX 用 `set(pred_rows) == set(gold_rows)` 判定，未实现官方对列序/列数的容忍规则；实测同一份上游预测在随机 200 题上为 64.3%，而官方 README 声称全量 dev 为 74.5%，差值无法用截断或列投影解释。跨口径比较无效。
- **准确率提升不显著**：完整流程 76.0% 对上游同题预测 72.0%，n=50 的 95% 置信区间约 ±12 个百分点，只能说"未退步"。
- **没有用户画像或跨会话长期记忆**：当前持久化的是 conversation thread 内的消息和工具状态，不会跨会话学习用户偏好，也不会把问答自动回灌 Few-shot。
- **单进程、无并发、无鉴权**：SQLite Saver 和串行任务队列面向本地轻量应用，不支持多 worker 部署，也不适合公网暴露。
- **示例题单为英文**（BIRD 原题），交互语言为中文；中文超纲问题目前只有熔断兜底，没有"该库无法回答"的前置判定。
- **尾延迟受推理端点支配**：强制思考模型同一 prompt 实测 completion 为 435 / 1,726 / 8,192 token，端点不接受 `thinking_budget` 类参数（一律返回 500），无法从客户端约束思考长度。

## 🧪 验证与测试

| 检查项 | 结果 | 命令 / 位置 |
|---|---|---|
| 自动化单元测试 | **88 项通过**（本地，不触网）；覆盖 `.env` 与 TOML 的优先级、双业务域路由与隔离、visualization、多轮图表复用、SQLite Checkpoint 重启恢复、任务/SSE 持久化、会话删除、RAG 降级、API、MCP、只读执行、刷新恢复与用量台账 | `uv run python -m unittest discover -s tests -q` |
| Agent 与工具 | LLM 工具循环、会话记忆、工具轨迹以及指标卡/柱状/折线/饼图规则均有离线测试 | `tests/test_agent.py` |
| Agent 专项评测 | 14 个查数、排名、趋势、占比、闲聊、安全和多轮案例；首轮 11/14，修复口径检索遗漏与意图边界后逐项回归至 **14/14**。这是增量失败题回归，非新模型整批重跑 | `evals/agent/report.md` |
| HR 业务域端到端 | 6 个固定问题覆盖查数、排名、趋势、占比和复杂指标，自动选库、指标命中、SQL 真实结果及图表严格检查 **6/6**；最终为增量失败题合并报告，平均 67.35 秒/题、241,264 Token | `evals/hr/report.md` |
| 销售域回归 | 新增 HR 后，销售问题仍自动选择 `enterprise_sales`；有效订单数 453 与可信 SQL 一致，1/1 通过 | `evals/hr/sales-regression-summary.json` |
| 业务口径混合 RAG | 15 题开发集：关键词 Recall@3/MRR 0.567/0.567，混合检索 **1.000/1.000**；655 embedding token，平均 0.180 秒/题 | `evals/metric-rag/report.md` |
| 真实 Agent 冒烟 | DashScope 完整跑通指标检索 → Schema → Text-to-SQL → 图表：结果 2,513,691.692，Agent 44.842 秒，SQL Skill 34.157 秒，总计 32,516 tokens | `runner/run_agent.py` |
| MCP | 4 个只读分析工具可被协议层发现 | `tests/test_mcp_server.py` |
| 只读执行安全性 | 写操作 / 多语句 / `ATTACH` / `load_extension` 全部被拒；递归查询超时被中断 | `tests/test_sql_skill.py` |
| 任务队列与重启 | 事件按序回放；遗留 `running` 记录被标记 `interrupted` 并可终结 SSE | `tests/test_demo.py` |
| 持久化记忆 | 对话跨重启恢复、销售/HR thread 隔离、任务/事件重开可读、旧 JSON 幂等导入、级联删除与敏感信息隔离 | `tests/test_persistence.py` |
| 启动自检与凭据隔离 | 缺失索引能报就绪状态；对外响应与报告不含 API Key | `tests/test_demo.py` |
| 50 题三档消融 | 仅生成 72.0% / 生成＋修订 72.0% / 完整流程 **76.0%**，三组均 100% 可执行 | `evals/bird-dev-50/ablation/` |
| 上游同题对照 | 上游发布预测在同一 50 题子集 72.0%（本仓库口径复算） | `results/bird-dev/qwen3.6-27b.json` |
| 企业 Agent 端到端（2026-10-27 实测） | 查数 / 排名 / 趋势 3 题全部成功：平均 **60.0 秒** Agent 墙钟（含 4 次 Planner 决策）、48.5 秒 SQL Skill、36,010 token 每题，0 次失败调用；指标结果用两种 SQL 写法独立复算一致 | `docs/demo-guide.md`、`evals/demo-fast/`（旧配置） |
| 旧 BIRD 3 题冒烟（自建端点 + text-embedding-v4） | 集合式与严格多重集 3/3，平均 41.62 秒 / 51,145 token 每题。**切换到 DashScope 与 qwen3.7 向量后，这组数字只作历史记录** | `evals/demo-fast/report.md` |
| 失控请求收敛 | 全候选被截断时 1 次调用即熔断（修复前 15 × 5 = 75 次） | `app/llm/llm.py` 截断判定 |
| 错误分层与账本完整性 | 硬预算熔断逐层上抛（不再伪装成字段缺失）、无响应尝试也计入 `failed_responses/failed_seconds`、Agent 墙钟独立于 SQL Skill 计时 | `tests/test_few_shot_masker.py`、`tests/test_sql_skill.py`、`tests/test_agent.py` |

**尚未覆盖**：14 题 Agent 专项评测主要检查工具策略；HR 6 题与销售回归增加了数据库可信 SQL 逐行比对，但仍是小规模合成业务库评测。图表尚无像素级回归。

## 📁 目录结构

```
DeepEye-SQL/
├── app/
│   ├── skills/               # 单问题 Skill：七阶段编排、只读执行、用量汇总
│   ├── agent/                # ChatOpenAI、Prompt、LangGraph、标准工具、会话状态
│   ├── api/                  # 对话、数据库和评测路由
│   ├── persistence/          # LangGraph Checkpoint + 会话/任务/SSE SQLite 状态库
│   ├── mcp_server.py         # 同一套分析工具的 MCP 协议入口
│   ├── enterprise/           # 可复现企业示例库、指标知识与示例问题
│   ├── web/                  # Web 应用：task_manager / registry / preflight / static
│   ├── llm/                  # LLM 封装 + 预算熔断(budget.py) + 用量台账(usage.py)
│   ├── pipeline/             # 六个阶段（上游算法 + 本仓库可观测性改造）
│   ├── few_shot/             # 动态少样本：掩码、索引、检索、初步 SQL
│   ├── vector_db/            # 值索引与 embedding 计量
│   ├── db_utils/             # 只读执行与 authorizer 沙箱
│   └── dataset/              # 数据项、阶段产物字段与校验
├── runner/                   # 命令行入口：演示站、单题、建索引、评测报告、抽样
├── config/
│   ├── template/             # 按模型组织的公开模板（含 demo-fast 低延迟档）
│   └── local/                # 本地实验配置与真实密钥（.gitignore 排除）
├── evals/                    # 题单、逐题明细与汇总报告（演示站只读浏览）
├── results/                  # 上游发布的预测文件（用于同题对照）
├── docs/                     # 运行手册、评测报告、失败分析、交付清单、截图
├── tests/                    # 88 项离线单元测试（tests/js/ 为 Node 侧恢复状态机测试）
└── workspace/                # 运行产物与索引（不入库）
```

> 数据集（3.2 GB）、模型权重、向量索引与运行产物均在 `.gitignore` 中排除；`data/` 需按 [docs/bird.md](./docs/bird.md) 自行下载。

## 📚 文档导航

| 文档 | 内容 | 适用 |
|------|------|------|
| [🖥️ 演示站说明](./docs/demo-guide.md) | 启动方式、页面结构、故障排查、推荐演示问题 | 所有人 |
| [📊 50 题三档消融](./docs/ablation-50.md) | 抽样方法、三组对照结果与阶段贡献 | 评测/面试 |
| [📉 失败案例分析](./docs/failure-analysis.md) | 五类典型错误的题号、现象与可行改进 | 开发 |
| [🧪 10 题工程基线](./docs/baseline-10.md) | 逐题耗时、token 与有效性说明 | 开发 |
| [🧩 单问题 Skill](./docs/sql-skill.md) | `SQLSkill` 输入输出、阶段产物、异常约定 | 开发者 |
| [🤖 企业数据分析 Agent](./docs/enterprise-agent.md) | 五类场景、节点流程、企业库与演示方式 | 面试/开发者 |
| [👥 HR 第二数据源](./docs/hr-data-source.md) | 可复现造数、表结构、指标口径、路由和验收结果 | 面试/开发者 |
| [🧠 Agent 架构决策](./docs/agent-architecture.md) | 开源基线、LLM 工具循环、MCP 与混合 Agent 取舍 | 面试/开发者 |
| [🚀 BIRD 复现手册](./docs/bird.md) | 数据准备、索引构建、分阶段执行与导出 | 开发者 |
| [🕷️ Spider 运行手册](./docs/spider.md) | Spider test 的数据准备与分阶段执行 | 开发者 |
| [❄️ Spider2 运行手册](./docs/spider2.md) | Spider2-Lite / Snow 的云端执行与跳过值检索说明 | 开发者 |
| [📋 交付清单](./docs/internship-release-checklist.md) | 项目定位、已完成项与发布前检查 | 自己/面试官 |
| [🎤 面试讲解稿](./docs/interview-pitch.md) | 30 秒/3 分钟介绍、简历描述与高频追问 | 面试/答辩 |
| [📐 项目设计文档](./docs/项目设计文档.md) | 需求背景与整体设计 | 产品/架构 |

## ❓ 常见问题

**Q：一个问题为什么要跑好几分钟？**
延迟几乎全部来自思考模型的输出量。消融档单题实测 430 秒 / 140,135 token，其中约 11 万是思考 token；端点单流吞吐约 92 tok/s。演示用 `demo-fast` 配置（采样预算 4→1），并在 DashScope profile 里 `extra_body = { enable_thinking = false }`：同一条抽关键词的请求从 8.6 秒 / 378 token 降到 1.2 秒 / 30 token，企业库单题实测 48.5 秒（SQL Skill）/ 60.0 秒（含 Agent 规划）。注意"思考不可关闭"只是旧自建端点的限制——DashScope 支持 `enable_thinking`、`reasoning_effort`，但 `thinking_budget` 会被忽略（实测反而更慢）。

**Q：界面上"Checker 修订"和"最终选择"内容一样？**
说明这次修订没有命中任何错误模式、选择走了一致性捷径（top-1 分数 ≥ 阈值即直接选定），两者都会是零模型调用、耗时接近 0。50 题消融中修订阶段的修复与退化均为 0，这是当前已知的薄弱环节。

**Q：报"预算熔断"是什么意思？**
该请求累计 token 或墙钟触到硬上限（默认 120k / 600s），通常是问题超出所选数据库可回答范围，或思考模型在某个候选上无限展开。可临时用 `DEMO_MAX_TOTAL_TOKENS` 放宽，但更该换问题或换库。

**Q：服务重启后旧请求查不到了？**
已完成请求和会话保存在 `workspace/state/agent_state.sqlite`，可以从左侧历史对话恢复。重启时仍在运行的请求会被标记为 `interrupted`，不会自动重复调用模型；历史 Skill 产物仍可在 `workspace/demo_requests/<request_id>/` 下翻查。

**Q：为什么日志里出现"empty final content"警告？**
思考模型把 `max_tokens` 全部花在推理上、没产出最终内容。现在这类响应会被判定为确定性失败并快速熔断，不再逐次重试；若只是部分候选为空，其余有效候选会被保留。

**Q：模型服务连不上 / 返回 500？**
先 `curl <base_url>/models` 确认服务在线。该端点对 `enable_thinking`、`thinking_budget` 等参数一律返回 500，只能在配置里不传这些参数。

**Q：能在公网部署吗？**
不建议。当前是单进程串行队列、SQLite 状态库且无鉴权；执行的又是模型生成 SQL，虽有只读沙箱与超时/行数上限，仍应在受控环境使用。

## 📄 开源协议与致谢

本仓库以 **[MIT](./LICENSE)** 分发，与上游项目保持一致。

| 范围 | 归属 |
|---|---|
| 六阶段 Text-to-SQL 流水线、DC/Skeleton/ICL 生成、Checker 修订、一致性选择、渐进式 schema 裁剪等核心算法 | © [HKUSTDial/DeepEye-SQL](https://github.com/HKUSTDial/DeepEye-SQL) 原作者（SIGMOD 2026） |
| 只读执行沙箱、线程池死锁修复、成本熔断与用量台账、单问题 Skill、Web 演示站与 SSE 进度、重启状态恢复、启动自检、三档消融评测体系、低延迟演示配置 | 本仓库维护者 |

论文：[DeepEye-SQL: A Software-Engineering-Inspired Text-to-SQL Framework](https://doi.org/10.1145/3802035), *Proc. ACM Manag. Data*, 4(3), Article 158, 2026.

```bibtex
@article{10.1145/3802035,
  author    = {Li, Boyan and Chen, Chong and Xue, Zhujun and Mei, Yinan and Luo, Yuyu},
  title     = {DeepEye-SQL: A Software-Engineering-Inspired Text-to-SQL Framework},
  year      = {2026},
  journal   = {Proc. ACM Manag. Data},
  volume    = {4},
  number    = {3},
  articleno = {158},
  doi       = {10.1145/3802035},
  url       = {https://doi.org/10.1145/3802035}
}
```

如果这个项目对你有帮助，欢迎点 ⭐ Star 或提 Issue 🙌

---

<div align="center">

**© 2026** · [MIT](./LICENSE) · 核心算法归属 [HKUSTDial/DeepEye-SQL](https://github.com/HKUSTDial/DeepEye-SQL) 原作者

</div>
