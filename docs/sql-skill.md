# 单问题 SQL Skill

`app.skills.SQLSkill` 将原有值检索、few-shot 准备、schema linking、SQL 生成、修复和选择串成一个 Python 接口。接口不要求金标 SQL，也不启动子进程。每个请求有独立快照目录和逐次 API 用量记录。

## 准备条件

- 在仓库根目录完成依赖安装，配置生成模型及向量模型。
- 数据库由应用预先注册，用户请求只提供数据库标识。
- 提前建立对应数据库的值向量索引及训练 few-shot 索引。索引文件夹名称应与注册数据库标识一致。
- 向量模型跟着配置走：DashScope 演示配置使用 `qwen3.7-text-embedding-flash`，旧 `text-embedding-v4` 索引与它**不在同一相似度空间，不能混用**。索引目录按向量模型名分开；切换模型后要用 `runner/complete_value_index.py --database <路径>` 重建，未重建的库不会进入演示白名单（`app/demo/registry.py` 的 `DEMO_READY_DATABASES`），Skill 也会直接报"Prepare the database value index"。

## Python 调用

```python
import os
os.environ["CONFIG_PATH"] = "config/local/baseline-dashscope/config-bird-dev-cloud.toml"

from app.config import get_config
from app.skills import SQLSkill, SQLSkillRequest

skill = SQLSkill(
    get_config(),
    {"california_schools": "data/bird/dev/dev_databases/california_schools/california_schools.sqlite"},
)
result = skill.run(
    SQLSkillRequest(
        question="How many schools are listed in the schools table?",
        database_id="california_schools",
        max_rows=100,
    ),
    on_stage=lambda event: print(event),
)
print(result.model_dump())
```

返回字段包括 `status`、`sql`、`columns`、`rows`、`truncated`、`elapsed_seconds`、`stage_seconds`、`usage`、`artifacts_dir`。失败时返回 `failed_stage` 和经过简化的错误说明。空表使用 `empty_result`，不会直接认定为 SQL 错误。

输入验证失败和未知数据库标识会抛出 `ValueError`；运行期间的阶段失败返回结构化错误结果。`on_stage` 接收每个阶段的开始/完成事件，现已由 `app/agent/` 的 LangGraph 编排和演示站 SSE 直接复用。

## 命令行调用

在仓库根目录执行（以下命令写成单行，适用于 PowerShell）：

```powershell
.venv/Scripts/python.exe -B runner/run_sql_skill.py --config config/local/baseline-dashscope/config-bird-dev-cloud.toml --database data/bird/dev/dev_databases/california_schools/california_schools.sqlite --database-id california_schools --question "How many schools are listed in the schools table?" --max-rows 100
```

请求产物保存在 `workspace/skill_requests/<request_id>/`。`result.json` 为最终输出，`usage.jsonl` 为生成和向量用量明细，阶段快照保留中间 SQL 和检索信息。产物可能包含业务数据，应由调用方控制目录访问权限。

## 当前边界

- 当前支持 SQLite 单问题同步调用，并已通过 `app/agent/` 接入 LangGraph、通过 `app/demo/` 接入 FastAPI/SSE 和自动图表；仍未实现跨轮聊天记忆。
- 原内核使用进程级共享服务，因此同一进程的 Skill 请求通过互斥锁串行执行。需要并发时应使用多个工作进程；不要在其他线程同时直接运行原内核。
- 每次请求复用离线索引，但不复用上一请求的 SQL 快照。带有数据库哈希的索引在数据变更后会被拒绝，需重新建索引。
- 最终执行使用只读连接、SQLite authorizer、超时和最多 1,000 行输出限制；内部候选评估仍需要完整结果集，尚无内存/结果字节硬上限。
- SQL 超时限制每次数据库执行；演示服务另外通过总 Token 和总时间预算对整个 LLM 工作流做熔断，但当前不提供正在运行请求的主动取消按钮。
- 用量来自 API 返回的 usage；没有响应 usage 的失败请求不能精确计费。`elapsed_seconds` 包括锁等待和各阶段初始化，与批量评测的逐题阶段耗时口径不同。
- 本接口复用上游阶段 Runner，未来可以将阶段实现进一步抽为长期驻留的节点，减少索引与客户端初始化开销。
