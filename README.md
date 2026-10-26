# Text-to-SQL 应用化改造（基于 DeepEye-SQL 流水线）

把一套研究用的多阶段 Text-to-SQL 流水线，改造成**可安全执行、可观测、可评测、可现场演示**的本地应用。推理算法沿用上游论文实现，改动集中在执行边界、延迟治理、可观测性和评测基础设施。

![](https://img.shields.io/badge/Python-3.12-blue) ![](https://img.shields.io/badge/FastAPI-SSE-009688) ![](https://img.shields.io/badge/Engine-SQLite_RO-6e40c9) ![](https://img.shields.io/badge/Tests-18-brightgreen) ![](https://img.shields.io/badge/License-MIT-lightgrey)

## 关键数据

| 指标 | 数值 | 来源 |
| --- | --- | --- |
| 50 题三档消融（完整流程） | **76.0%** 集合式 EX，三组均 100% 可执行 | `evals/bird-dev-50/ablation/` |
| 上游发布预测在同一 50 题子集 | 72.0%（仓库口径复算） | `results/bird-dev/qwen3.6-27b.json` |
| 快速演示路径单题耗时 | **41.62 秒**（3 题全通过，含严格多重集） | `evals/demo-fast/report.md` |
| 消融配置单题成本 | 430 秒 / 140,135 token（治理前） | `workspace/demo_requests/` |
| 失控请求收敛 | 75 次重试 → **1 次调用即熔断** | `app/llm/budget.py` |
| 自动化测试 | 18 项通过 | `tests/` |

## 技术栈

`Python 3.12` · `FastAPI` + `SSE` · `SQLite`（只读连接 + authorizer 沙箱）· OpenAI 兼容推理端点（自建 vLLM，强制思考模型）· DashScope `text-embedding-v4` · Chroma 本地向量索引 · `tenacity` 重试 · `loguru` · 原生 HTML/CSS/JS 前端（零构建）

## 快速开始

```bash
uv sync                                    # 或 pip install -e .

# 1. 启动前自检：数据库、值索引、few-shot 索引、离线报告是否就绪
python runner/run_demo.py --check-only

# 2. 启动演示站
python runner/run_demo.py --config config/local/demo-fast/config-bird-demo.toml
# 等价：uvicorn app.demo.server:create_app_from_env --factory --port 8000

# 3. 打开 http://127.0.0.1:8000
```

单题命令行入口（不启服务）：

```bash
python runner/run_sql_skill.py --config <toml> --database <sqlite路径> \
  --database-id <白名单ID> --question "每个县分别有多少所学校？"
```

## 架构

```
浏览器（单页 / SSE）
   │  POST /api/query          ┌──────────────────────────┐
   ├─ 202 request_id ─────────►│ TaskManager  串行队列      │
   │  GET  /events ◄─SSE───────│  · 事件回放（可断线重连）   │
   │  GET  /query/{id}         │  · 重启后孤儿任务标 interrupted│
   └───────────────────────────└───────────┬──────────────┘
                                           ▼
                              SQLSkill.run()  七阶段串行
   value_retrieval → few_shot_preparation → schema_linking
   → sql_generation → sql_revision → sql_selection → execution
                                           │
                    每阶段回调进度事件 + 每次模型调用上报用量（meter 事件）
                                           ▼
                     只读执行：SQLite authorizer + 超时 + 行数上限
```

每次请求有独立产物目录 `workspace/demo_requests/<request_id>/`，落盘各阶段快照、`usage.jsonl` 用量台账、`result.json` 与失败时的 `error.txt`（密钥已脱敏）。

## 相对上游：改了什么，没改什么

**没改**：DC / Skeleton / ICL 三种 SQL 生成策略、八个 Checker 的判定逻辑、一致性过滤 + LLM 评估的选择策略、渐进式 schema 裁剪。四个流水线阶段文件的改动只有可观测性装饰器与一处投票校验。

**改了**：

| 类别 | 位置 | 内容 |
| --- | --- | --- |
| 安全执行边界 | `app/db_utils/execution.py` | `PRAGMA query_only=ON` + `set_authorizer` 白名单（仅 SELECT/READ/FUNCTION/RECURSIVE 与元数据 PRAGMA），显式 DENY `load_extension`，用 URI 规范路径修掉 `mode=ro` 挡不住 `ATTACH` 可写库的问题 |
| 吞吐缺陷修复 | `app/few_shot/preliminary_sql.py` | dc/skeleton 曾提交到与 item 级相同的线程池，池被占满后子任务永久排队 → 嵌套自饥饿死锁（现象：跑批 0 进度冻结），改为当前线程顺序执行 |
| 延迟与成本治理 | `app/llm/budget.py`、`app/llm/llm.py` | 每请求 token/墙钟硬上限（默认 120k / 600s）；全部候选被 `max_tokens` 截断时抛 `TruncatedResponseError` 快速失败，不再在 tenacity × extractor 两层重试里循环小时级 |
| 可用性降级 | `app/pipeline/value_retrieval/utils.py` | 关键词抽取被截断时降级到启发式切词，而不是中断整条链路 |
| 可观测性 | `app/llm/usage.py`、`app/vector_db/metered_embedding.py` | 基于 ContextVar 的用量台账，每次 LLM/embedding 调用可归因到阶段与题目；跨线程池传播（自定义 `ThreadPoolExecutor` 复制 context） |
| 演示站 | `app/skills/`、`app/demo/` | 单题 Skill 封装、FastAPI + SSE 进度、串行任务队列、启动自检、重启后任务状态恢复；前端展示三版 SQL、阶段耗时、关联 Schema 与结果表 |
| 评测基础设施 | `runner/report_*.py`、`runner/select_bird_sample.py` | 三档消融报告、基线报告、固定种子分层抽样、阶段实测计时、进度看板 |
| 配置分层 | `config/template/` | 按模型组织模板；`demo-fast` 用低延迟档（采样预算 4→1、few-shot 7→5），与评测口径配置分离 |

## 流水线阶段

| 阶段 | 做什么 | 消融配置预算 | 实测耗时（消融档） |
| --- | --- | --- | --- |
| value_retrieval | LLM 抽关键词 → 向量检索库内真实值 → 增强 schema | 单次采样 | 19.0 s |
| few_shot_preparation | 生成初步 SQL → 掩码问题 → 检索相似样例（7 条） | dc/skeleton 各 4 | 182.9 s |
| schema_linking | 正向 + 反向链接，值距离对齐 | 各 4 | 117.7 s |
| sql_generation | DC / Skeleton / ICL 三路生成 | 各 4 | 110.5 s |
| sql_revision | 八个错误模式 Checker | 4 | 0.07 s（见下） |
| sql_selection | 一致性过滤 + LLM 两两比较投票 | 4 | 0.04 s（见下） |
| execution | 只读执行，行数与超时约束 | — | <0.01 s |

`demo-fast` 配置把上述预算统一降为 1、few-shot 降为 5 条，用于把单题耗时从 430 秒压到 40 秒量级；评测数字一律以消融配置为准。

## 评测

**口径**：集合式 EX = `set(pred_rows) == set(gold_rows)`；严格 EX = 多重集相等；可执行率 = SQL 无错执行。与 BIRD 官方 harness 的列序/列数容忍规则不同，**不要拿本仓库数字直接对比官方榜单**。

**样本**：固定种子 `20261025` 从 5 个 BIRD-dev 数据库各抽 10 题（`evals/bird-dev-50/sample.json`）。注意该抽样比 dev 整体偏易——上游预测在此得 72.0%，在随机 200 题上只有 64.3%。

**三档结果**：

| 档位 | 集合式 EX | 严格 EX | 可执行 | 阶段贡献（修复/退化） |
| --- | --- | --- | --- | --- |
| 仅生成 | 36/50 = 72.0% | 72.0% | 50/50 | — |
| 生成＋修订 | 36/50 = 72.0% | 72.0% | 50/50 | 修订 **0 / 0** |
| 完整流程 | **38/50 = 76.0%** | 74.0% | 50/50 | 选择 **2 / 0** |

**结论边界**：完整流程相对上游同题预测 +4 个百分点，但 n=50 的 95% 置信区间约 ±12 点，不构成统计显著提升；增益全部来自选择阶段。八个 Checker 基于 SQL 正则错误模式（如 `ORDER BY` 内使用 `MIN/MAX`、`= (SELECT ... LIMIT n)`），在 50 题上一次都未触发——这是当前最值得深挖的一处。

## 已知问题

- **强制思考模型的方差主导尾延迟**：同一 prompt 实测 completion 为 435 / 1,726 / 8,192 token，超纲或长列举问题会让模型思考到 `max_tokens` 耗尽且不输出最终内容。端点不接受 `thinking_budget` 类参数（一律 500）。
- **修订阶段零收益**：见上，Checker 触发条件与实际错误形态不匹配。
- **单进程串行队列**：并发度 1，任务状态在内存（`state_dir` 仅用于重启后标记中断），不支持多 worker 部署。
- **演示题单为英文**（BIRD 原题），而交互语言为中文；中文超纲问题缺少拦截与提示。

## 测试

```bash
python -m unittest discover -s tests -q     # 18 项，离线，不触网
```

覆盖：只读执行安全性（写操作 / 多语句 / ATTACH / `load_extension` / 递归查询中断）、单请求产物隔离与缺失索引前置失败、任务队列事件回放与重启后中断标记、启动自检与评测报告不泄漏凭据、schema profile 裁剪、用量归因（含嵌套线程池的 ContextVar 传播与重试去重）、多采样部分有效响应的保留与解析、分层抽样的确定性。

尚未覆盖：预算熔断与截断降级路径、前端渲染、真实模型端点的端到端回归。

## 数据与运行手册

| 数据集 | 说明 | 配置模板 |
| --- | --- | --- |
| BIRD | [docs/bird.md](docs/bird.md) | `config/template/*/config-bird-dev.toml` |
| Spider | [docs/spider.md](docs/spider.md) | `config/template/*/config-spider-test.toml` |
| Spider2 | [docs/spider2.md](docs/spider2.md) | `config/template/*/config-spider2-*.toml` |

数据集、模型权重、索引与运行产物均不入库；本地实验配置放在被忽略的 `config/local/`，密钥不进版本控制。

## 其他文档

[演示站说明（一页纸）](docs/demo-guide.md) · [50 题消融](docs/ablation-50.md) · [10 题基线](docs/baseline-10.md) · [失败案例分析](docs/failure-analysis.md) · [单题 Skill](docs/sql-skill.md) · [交付清单](docs/internship-release-checklist.md) · [项目设计文档](docs/项目设计文档.md)

## 来源与许可

推理流水线来自 [HKUSTDial/DeepEye-SQL](https://github.com/HKUSTDial/DeepEye-SQL)（MIT License），本仓库以其为 base，改动以上述提交记录为准。

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
