# Agent 架构决策记录

## 调研基线

本实现主要参考三个官方开源设计：

1. [LangGraph ReAct Agent Template](https://github.com/langchain-ai/react-agent)：将 `state`、`context`、`tools`、`configuration`、`prompts` 和 `graph` 分离，核心循环是 Model → ToolNode → Model。
2. [LangGraph Custom SQL Agent](https://docs.langchain.com/oss/python/langgraph/sql-agent)：SQL Agent 不只靠 Prompt，而是把 Schema、查询检查和执行设为可控制节点，并强调数据库最小权限。
3. [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/servers/)：将工具发现和调用标准化，使相同能力可以被不同 Agent Client 复用。

没有直接照搬通用 `SQLDatabaseToolkit`。本项目已有经过评测的 DeepEye SQLSkill，包含值检索、Few-shot、Schema Linking、三路生成、Checker 修订、一致性选择和只读执行。把它整体封装为一个高质量工具，比另起一套简单“LLM 写 SQL → 执行”更能保留项目优势。

## 项目边界

DeepEye 与 DeepEye-SQL 不是同一项目。本仓库只把上游
`HKUSTDial/DeepEye-SQL` 作为 **Text-to-SQL 引擎**，不把它当成聊天
Agent 架构。产品主系统是企业数据分析 Agent，DeepEye-SQL 位于工具层。

## 当前结构

```text
app/agent/
├── llm.py            统一 ChatOpenAI 工厂
├── prompts/          身份、规划和回答 Prompt
├── state.py          LangChain 消息状态与业务上下文
├── tools/            标准 @tool 工具与依赖注册；不向模型暴露运行时状态
├── graph.py          ChatOpenAI ↔ ToolNode 循环和 checkpointer
├── service.py        对外请求/响应、conversation_id、结果兜底
└── visualization.py  确定性图表配置

app/skills/sql_skill.py
                     DeepEye-SQL 流水线的单问题适配器
app/api/             对话、数据库和评测路由
app/mcp_server.py     将同一套能力暴露为 MCP 工具
```

## 为什么是混合 Agent

- LLM 负责理解上下文、选择工具、决定是否继续调用以及组织最终答案。
- SQLSkill 负责高风险且可评测的 SQL 工作，避免让通用 Agent 绕过安全与 RAG 流程。
- LangGraph 负责状态、循环次数和会话 checkpoint，不用关键词 if/else 伪装“智能路由”。
- 图表渲染保持确定性，避免模型直接生成不可执行的前端代码。
- SQLite 最终执行继续使用只读连接、authorizer、超时和行数限制。

## 工具协议

Agent 使用 `ChatOpenAI.bind_tools()`、标准 `AIMessage/ToolMessage` 和 LangGraph
`ToolNode`。工具的输入 Schema 只包含业务参数，`AgentState`、回调、数据库路径和
SQLSkill 实例均由运行时注入，不允许模型指定。

工具通过 `Command(update=...)` 将业务口径、Schema、SQL 结果、intent、图表和工具轨迹
写入强类型 State，不再通过扫描消息 artifact 恢复真实状态。Checkpointer 的 thread ID
由 `conversation_id + database_id` 组成，跨数据库对话不会复用 Schema 或 SQL 结果。
用户补充说明与系统指标口径分字段保存，并带来源标签注入 SQLSkill；模型的工具参数中不
暴露 evidence。

当前 DashScope 配置必须支持 OpenAI 兼容的原生 `tools/tool_calls`。如果更换为不支持
Function Calling 的端点，应在模型适配层明确报错或单独增加受测适配器，不在主工作流中
加入另一套工具协议。

## 三层状态：浏览器、任务记录、会话记忆

刷新恢复容易和“对话记忆”混为一谈，其实是三份互不替代的状态，各自的寿命和丢失后果不同：

| 层 | 存在哪 | 存什么 | 谁清理 | 丢了会怎样 |
|---|---|---|---|---|
| 浏览器状态 | `sessionStorage`：`deepeye.active_task` / `deepeye.conversation_id` | 只存恢复页面必需的标识：`request_id`、`conversation_id`、问题文本、`database_id`、创建时间、事件游标 | 用户点“新建对话”、切换数据源、任务 404、`interrupted` | 页面回到欢迎页，后端任务继续跑完，结果仍可通过请求 ID 查到 |
| 后端任务记录 | 内存 `TaskManager._tasks` + 落盘 `workspace/demo_tasks/<id>.json` | 状态机（`queued`/`running`/`success`/`empty_result`/`error`/`interrupted`）、请求参数、序列化后的结果与事件序列 | 进程重启时把未完成记录改成 `interrupted`；不自动删除历史文件 | 404，前端清理记录并提示重新提交 |
| LangGraph 会话记忆 | 进程内 Checkpointer，thread ID = `conversation_id + database_id` | 消息历史、业务口径、Schema、上一轮 SQL 与结果，供“做成柱状图”这类追问复用 | 进程退出即丢失 | 追问失去上下文，需要重新描述问题 |

因此三个边界必须讲清楚：

1. **刷新不取消任务**。任务跑在服务端线程里，浏览器只是 SSE 订阅者。刷新后页面用原 `request_id` 查一次 `GET /api/query/{request_id}`，`queued`/`running` 就重连事件流，全程不会 `POST /api/chat`。
2. **新标签页靠服务端接手，不靠本地存储**。`sessionStorage` 是标签页私有的，新页面天然读不到记录；如果让它在本地“猜”，只能退化成重新提交。因此本地没有记录时页面会问一次 `GET /api/query/active`：串行队列返回当前 `queued`/`running` 的任务，新标签页据此接管并沿用原 `conversation_id` 与数据源。这条路径也顺带堵住了“新开页面再问一次 → 队列里多一个重复任务”。任务已结束时该接口返回 `null`，新标签页正常显示欢迎页——跨标签页不追溯历史结果，那是会话中心的职责。
3. **刷新能恢复结果，但重启不能恢复对话**。任务结果已落盘，所以刷新都能把图表、表格、SQL、耗时与 Token 回填；但 LangGraph 记忆只在内存，后端重启后即使结果还在，追问也不再拥有上一轮上下文。
4. **`interrupted` 不是失败**。它只表示“进程在任务执行期间退出，这个任务没有任何 worker 会再推进它”。前端据此停止重连、清掉活动任务、把问题文本填回输入框，而不是无限重试或自动重提。

判定逻辑抽在 `app/demo/static/recovery.js`（纯函数、不碰 DOM），由 `tests/js/recovery.test.mjs` 用 Node 断言；接口字段与静态资源由 `tests/test_demo_recovery.py` 覆盖。

## 当前限制

- checkpointer 当前在内存中，同一数据库内支持多轮，但进程重启后对话上下文丢失。
- 暂未加入写操作、人工审批或 HITL；所有数据库工具只读。
- MCP Server 已支持 stdio/SSE/streamable-http 传输，但尚未提供生产鉴权配置。
- 外部模型不可用时 Agent 会在 `agent_planning` 阶段明确失败，不使用规则结果冒充 LLM 决策。
