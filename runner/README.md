# runner/ 目录说明

这里**不是应用入口**。面向用户的能力全部收在 `enterprise-agent`：

| 需求 | 命令 |
|---|---|
| 启动 Web 应用 | `uv run enterprise-agent` |
| 启动前自检 | `uv run enterprise-agent check` |
| 单问题调用 | `uv run enterprise-agent ask --question "…"` |
| MCP Server | `uv run enterprise-agent mcp` |
| Agent 专项评测 | `uv run enterprise-agent evaluate` |
| 业务口径 RAG 评测 | `uv run enterprise-agent evaluate-rag` |
| 生成合成企业库 | `uv run enterprise-agent generate-data` |
| 构建值向量索引 | `uv run enterprise-agent build-index` |

旧的 `run_demo.py` / `run_agent.py` / `run_mcp_server.py` / `generate_enterprise_data.py` /
`evaluate_agent.py` / `evaluate_metric_rag.py` 已在新入口跑通真实问题后删除（2026-11-02）。

## 保留的脚本

### 评测与实验

```text
report_ablation.py      三档消融报告
report_baseline.py      基线对照与用量聚合
select_bird_sample.py   固定种子分层抽样
benchmark_execution.py  执行层基准
measured_stage.py       单阶段实测（被 run_measured_baseline.py 调用）
run_measured_baseline.py 按阶段断点续跑基线
evaluation.py           历史评测入口
```

### 数据与索引

```text
preprocess_dataset.py / preprocess_dail_few_shots.py   BIRD / DAIL 预处理
convert_snapshot_to_sql.py                             snapshot 转 SQL
build_few_shot_index.py / create_vector_db_parallel.py 索引构建
complete_value_index.py  值索引增量构建的独立入口（`measured_stage.py` 需要可执行脚本，故保留）
query_few_shot_index.py  few-shot 索引查询
inspect_few_shot_preparation.py  few-shot 阶段排查
progress_dashboard.py    批量运行进度看板
```

### 单阶段调试

```text
run_value_retrieval.py   run_few_shot_preparation.py   run_schema_linking.py
run_sql_generation.py    run_sql_revision.py           run_sql_selection.py
run_sql_skill.py         整条 Skill 直调（不经 Agent）
```

这些脚本顶部保留 `sys.path.insert(...)`，因为它们要能直接 `python runner/xxx.py` 执行；
业务实现不在这个目录里，改动请去 `app/commands/` 或 `app/pipeline/`。
