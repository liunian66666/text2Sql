# Agent 架构决策记录

## 调研基线

本实现主要参考三个官方开源设计：

1. [LangGraph ReAct Agent Template](https://github.com/langchain-ai/react-agent)：将 `state`、`context`、`tools`、`configuration`、`prompts` 和 `graph` 分离，核心循环是 Model → ToolNode → Model。
2. [LangGraph Custom SQL Agent](https://docs.langchain.com/oss/python/langgraph/sql-agent)：SQL Agent 不只靠 Prompt，而是把 Schema、查询检查和执行设为可控制节点，并强调数据库最小权限。
3. [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/servers/)：将工具发现和调用标准化，使相同能力可以被不同 Agent Client 复用。

没有直接照搬通用 `SQLDatabaseToolkit`。本项目已有经过评测的 DeepEye SQLSkill，包含值检索、Few-shot、Schema Linking、三路生成、Checker 修订、一致性选择和只读执行。把它整体封装为一个高质量工具，比另起一套简单“LLM 写 SQL → 执行”更能保留项目优势。

## 当前结构

```text
app/agent/
├── brain.py          OpenAI 原生 tool calling + JSON ReAct 兼容层
├── prompts.py        集中的系统 Prompt 和工具使用策略
├── tools.py          类型化工具定义与执行；不向模型暴露数据库路径
├── graph.py          LangGraph Model ↔ Tools 循环和 checkpointer
├── service.py        对外请求/响应、conversation_id、结果兜底
└── visualization.py  确定性图表配置

app/mcp_server.py     将同一套能力暴露为 MCP 工具
```

## 为什么是混合 Agent

- LLM 负责理解上下文、选择工具、决定是否继续调用以及组织最终答案。
- SQLSkill 负责高风险且可评测的 SQL 工作，避免让通用 Agent 绕过安全与 RAG 流程。
- LangGraph 负责状态、循环次数和会话 checkpoint，不用关键词 if/else 伪装“智能路由”。
- 图表渲染保持确定性，避免模型直接生成不可执行的前端代码。
- SQLite 最终执行继续使用只读连接、authorizer、超时和行数限制。

## 工具协议兼容

优先使用 OpenAI 原生 `tools/tool_calls`。某些 OpenAI 兼容端点只实现基础 Chat Completions，会在收到 `tools` 时返回错误；此时 `brain.py` 自动切换到严格 JSON ReAct 协议。两种模式都由 LLM 决策，区别只在动作的序列化方式。

当前 DashScope 配置实测走的是**原生 tool calling**（`get_capabilities` 与四业务工具均由模型自行选择），JSON 降级路径保留但未被触发，只有离线测试覆盖它。

## 当前限制

- checkpointer 当前在内存中，进程重启后对话上下文丢失。
- 暂未加入写操作、人工审批或 HITL；所有数据库工具只读。
- MCP Server 已支持 stdio/SSE/streamable-http 传输，但尚未提供生产鉴权配置。
- 外部模型不可用时 Agent 会在 `agent_planning` 阶段明确失败，不使用规则结果冒充 LLM 决策。
