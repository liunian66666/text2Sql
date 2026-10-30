# 企业数据分析 Agent

这是面向实习作品集的应用层 Agent：保留 DeepEye-SQL 的生成、修订、选择能力，在其外部增加 LLM 工具规划、LangGraph ReAct 循环、会话记忆、企业业务口径和结果可视化。重点是完整产品闭环，不宣称新的 Text-to-SQL 算法。

## 五类场景

| 场景 | 例子 | 默认输出 |
| --- | --- | --- |
| 查数 | 统计 2026 年销售额、订单量和客单价 | 指标卡或表格 |
| 排名 | 销售额最高的 10 个商品 | 柱状图 + 表格 |
| 趋势 | 按月统计销售额和订单量 | 折线图 + 表格 |
| 占比 | 各商品分类销售额占比 | 饼图 + 表格 |
| 闲聊 | 你能分析哪些指标 | 由模型直接回答或调用能力工具，不调用 SQL |

## LangGraph 流程

```text
用户消息 + 历史消息 + System Prompt
  └─ LLM Planner
       ├─ 无工具调用 → 最终回答 → END
       └─ tool_calls
            → Tool Executor
               ├─ search_business_metrics
               ├─ inspect_database_schema
               ├─ run_text_to_sql
               ├─ build_visualization
               └─ get_capabilities
            → Tool Message → LLM Planner（最多 5 轮）
```

LLM 是 Agent 的决策核心。系统 Prompt 位于 `app/agent/prompts/`，`ChatOpenAI` 工厂位于 `llm.py`，标准工具位于 `tools/`，图结构位于 `graph.py`。模型通过原生 OpenAI `tools/tool_calls` 选择动作，LangGraph `ToolNode` 执行工具；系统没有关键词意图分类器。

业务口径层对 `business_metrics.json` 执行可解释混合检索：关键词召回提供稳定基线，向量召回覆盖业务问法改写，二者加权重排；每条结果返回综合分、词法分、语义分和命中词。向量服务异常时自动降级为关键词检索，不阻断 SQL 主流程。底层值检索、Few-shot 和 Schema Linking 继续使用原有 RAG 流程。相同 `conversation_id + database_id` 使用 LangGraph checkpointer 保留并隔离消息历史，前端“新建对话”会生成新 ID。

业务口径 RAG 的独立对照评测：

```bash
uv run --no-sync python runner/evaluate_metric_rag.py --validate-only
uv run --no-sync python runner/evaluate_metric_rag.py
```

报告写入 `evals/metric-rag/latest/summary.json`，同时记录 keyword / hybrid 的 Recall@K、MRR、平均耗时和向量 token。

结构参考 LangGraph 官方的 [ReAct Agent 模板](https://github.com/langchain-ai/react-agent)、[自定义 SQL Agent 教程](https://docs.langchain.com/oss/python/langgraph/sql-agent) 和 [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/servers/)。

## 企业演示数据

`runner/create_enterprise_demo.py` 使用固定随机种子生成本地 SQLite 数据库，包含：

- `orders`、`order_items`、`products`
- `customers`、`categories`
- `payments`、`refunds`

数据覆盖 2025—2026 年，适合演示销售、订单、客单价、商品排名、月度趋势、分类占比和退款率。生成脚本同时创建中文字段说明，数据库本体与向量索引均不提交到 Git。

## 首次准备与启动

```powershell
uv sync
uv run python -B runner/create_enterprise_demo.py
$env:CONFIG_PATH="config/local/demo-fast/config-bird-demo.toml"
uv run python -B runner/complete_value_index.py --database data/enterprise_sales/enterprise_sales.sqlite
uv run python -B runner/run_demo.py --check-only
uv run python -B runner/run_demo.py --port 8000
```

浏览器打开 <http://127.0.0.1:8000>，数据源默认“自动识别业务域”，Agent 会在销售库与 HR 库之间自行选择。

配置来源按优先级叠加：**shell 环境变量 > 仓库根目录 `.env` > TOML**。想把密钥从 TOML 里挪出来时，`cp .env.example .env` 后填 `OPENAI_API_KEY` 即可（`CONFIG_PATH`、`OPENAI_BASE_URL`、`DEFAULT_LLM_MODEL`、`EMBEDDING_MODEL` 同样可放 `.env`）。`.env` 在 `app.config` 导入时读取，缺失或解析失败都不阻断启动；`--check-only` 只报告命中的变量个数，不回显名称与值。

也可以只运行一个问题：

```powershell
uv run python -B runner/run_agent.py --config config/local/demo-fast/config-bird-demo.toml --question "按月统计2026年的销售额和订单量变化趋势。"
```

## MCP Server

同一套只读分析能力可以通过 MCP 提供给其他 Agent：

```powershell
uv run python -B runner/run_mcp_server.py --config config/local/demo-fast/config-bird-demo.toml
```

默认 stdio 模式暴露 `list_databases`、`search_business_metrics`、`inspect_database_schema` 和 `run_text_to_sql`。数据库文件路径和密钥不会出现在工具返回中。

## Agent 专项评测

```powershell
uv run python runner/evaluate_agent.py --config config/local/demo-fast/config-bird-demo.toml
```

题单位于 `evals/agent/questions.json`，与 50 题 SQL EX 分开，统计工具策略、intent、图表类型、多轮结果复用、只读安全、Agent 墙钟和总 token。可先用 `--max-cases 2` 做小规模连通性检查。

## 当前边界

- 当前有进程内多轮会话记忆，但服务重启后不会持久化；尚无歧义追问和人工确认节点。
- 业务指标检索仍是轻量本地检索；Agent 规划、工具选择和最终回答由 LLM 完成。
- 只支持服务端注册的 SQLite 数据库，且下拉只列出**值索引与当前向量模型一致**的库；切换 embedding 模型后未重建索引的库不会出现。
- SQL 工作流依赖外部推理与向量服务；服务异常时会返回结构化失败阶段，闲聊分支仍可用。预算/时间熔断按层上抛，`error` 直接写"预算熔断：LLM time budget exceeded (36s/35s) after 6 calls"，不会再伪装成"字段缺失"。
- 实测耗时（2026-10-27，DashScope deepseek-v4-pro-0813 关思考 + qwen3.7 向量）：企业库三题 3/3 成功，平均 60.0 秒 Agent 墙钟、48.5 秒 SQL Skill、36,010 token 每题；`elapsed_seconds` 是 SQL Skill 秒表，`agent_elapsed_seconds` 与 `stage_seconds["agent_orchestration"]` 才是含 Planner 决策的完整口径。
- 状态隔离修复后的单题冒烟：指标检索 → Schema → Text-to-SQL → 图表完整链路成功，销售额结果 2,513,691.692；Agent 44.842 秒、SQL Skill 34.157 秒、Agent 与 Skill 合计 32,516 token。
- 图表按意图和结果列自动推荐；用户点名图表类型时模型可传 `chart_type`（白名单 metric/bar/line/pie/table），数据撑不起时自动回退为按意图选定的图，并在工具返回里写明 `chart_type_applied` 与回退原因。仍不做复杂 BI 自定义配置（轴、颜色、堆叠、双 Y 轴等）。
