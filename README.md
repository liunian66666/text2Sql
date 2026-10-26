<div align="center">

<h1>DeepEye-SQL</h1>
<p><strong>SIGMOD 2026 · Software-Engineering-Inspired Text-to-SQL</strong></p>

<p>
  <a href="https://deepeyesql-hahwwmgj.manus.space"><img src="https://img.shields.io/badge/Homepage-Visit-0ea5e9" alt="Homepage"></a>
  <a href="https://doi.org/10.1145/3802035"><img src="https://img.shields.io/badge/Paper-DOI%2010.1145%2F3802035-b31b1b" alt="Paper"></a>
  <img src="https://img.shields.io/badge/Task-Text--to--SQL-0f766e" alt="Task">
  <img src="https://img.shields.io/badge/Benchmarks-BIRD%20%7C%20Spider%20%7C%20Spider2-7c3aed" alt="Benchmarks">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-1f6feb" alt="License"></a>
</p>

<p>
  <a href="https://deepeyesql-hahwwmgj.manus.space">Homepage</a> ·
  <a href="https://doi.org/10.1145/3802035">Paper</a> ·
  <a href="#results">Results</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#runbooks">Runbooks</a> ·
  <a href="#citation">Citation</a>
</p>

</div>

---

## Application Engineering Edition / 实习作品集版本

This working tree is an **application-engineering extension of the open-source DeepEye-SQL research project**. The staged Text-to-SQL method, original benchmark results, paper and authorship belong to the upstream project cited below. The additions in this edition focus on turning that research pipeline into a safe, measurable and demonstrable local application.

本版本的定位是“基于开源研究代码完成可落地的 Text-to-SQL 应用化改造”，不把上游算法或论文结果作为个人原创。主要新增内容如下：

| 新增能力 | 应用价值 |
| --- | --- |
| OpenAI-compatible qwen 与云向量接入 | 可替换模型服务，并保留统一配置接口。 |
| 单问题 `SQLSkill` | 将批量研究流水线封装成应用可调用的 Python 接口。 |
| FastAPI + SSE 演示站 | 实时展示值检索、Few-shot、Schema Linking、生成、修订、选择和执行。 |
| 只读执行与资源熔断 | 拦截写操作，限制 SQL 时间、返回行数、总 Token 和请求时间。 |
| 可观测评测 | 记录逐阶段耗时、Token、失败位置、恢复成本和结构化快照。 |
| 多数据库对照实验 | 固定 50 题、5 个数据库，对比仅生成、生成＋修订和完整流程。 |

当前已完成的 10 题 qwen HTTP 基线为：集合式 EX **10/10**、严格多重集 EX **9/10**、可执行 **10/10**。这是固定小样本应用验证，不代表 BIRD 官方成绩。详见 [10题报告](evals/bird-dev-10-qwen-http/report.md)。

固定 50 题、5 数据库的工程版对照实验也已完成：仅生成 **72%**、生成＋修订 **72%**、完整流程 **76%**，三组可执行率均为 **100%**。最终选择修复 2 题且没有造成退化；在线 Token 分别为 1,866,044、1,893,495 和 1,896,449。详见 [50题对照报告](evals/bird-dev-50/ablation/report.md)。

```mermaid
flowchart LR
    U[自然语言问题] --> API[FastAPI / SQLSkill]
    API --> V[值检索]
    V --> F[Few-shot 准备]
    F --> S[Schema Linking]
    S --> G[多候选 SQL 生成]
    G --> R[执行检查与修订]
    R --> C[候选选择]
    C --> Q[SQLite 只读执行]
    Q --> UI[SSE 进度 / SQL / 表格 / Token]
    M[qwen HTTP] -.生成与判断.-> F
    M -.生成与判断.-> S
    M -.生成与判断.-> G
    E[云向量服务] -.检索.-> V
    E -.检索.-> F
```

本地演示使用独立的快速配置，关闭不必要的思考输出并降低采样预算：

```powershell
# 先检查数据库、索引和离线报告是否就绪
.venv/Scripts/python.exe -B runner/run_demo.py --check-only

# 启动单题在线演示 + 离线评测页面
.venv/Scripts/python.exe -B runner/run_demo.py
```

浏览器打开 <http://127.0.0.1:8000>。演示说明见 [docs/demo-guide.md](docs/demo-guide.md)，实习版完成标准见 [docs/internship-release-checklist.md](docs/internship-release-checklist.md)。

## Overview

DeepEye-SQL treats Text-to-SQL as a software engineering process rather than a single-shot generation task.
It decomposes the problem into grounding, schema linking, implementation, debugging, and final selection, then coordinates those stages with structured snapshots and execution-aware checks.

The repository contains the research pipeline used for our SIGMOD 2026 paper, with support for BIRD, Spider, and Spider2. From-scratch reproduction instructions live in the dataset-specific runbooks under [docs/](docs/README.md).

## Highlights

| Capability | What it provides |
| --- | --- |
| Software-engineering pipeline | A staged workflow for grounding, linking, generation, revision, and selection. |
| Dynamic few-shot retrieval | Automatic training-set indexing with LLM-based question/SQL masking and preliminary-SQL-guided retrieval. |
| Checker-based SQL revision | Syntax, execution, and result-level repair before final selection. |
| Execution-aware selection | Candidate SQLs are compared with database feedback instead of relying only on model preference. |
| Structured snapshots | Long-running experiments are resumable, inspectable, and exportable. |

## News

| Date | Update |
| --- | --- |
| **2026-07-10** | **Qwen3.6-27B achieves 78.4 EX on the BIRD test set.** |
| 2026-07-02 | Added dynamic few-shot retrieval, model-organized config templates, unified dataset wrappers, and public runbooks for BIRD, Spider, and Spider2. |

## Results

| Benchmark | Metric | Score | Model | Public Output |
| --- | --- | ---: | --- | --- |
| BIRD-Dev | EX | **74.5** | Qwen3.6-27B | [prediction JSON](results/bird-dev/qwen3.6-27b.json) |
| BIRD-Test | EX | **78.4** | Qwen3.6-27B | not released |
| Spider2-Lite | official score | **38.2** | DeepSeek-R1 | [outputs](results/spider2-lite/deepseek-r1) |
| Spider2-Snow | official score | **50.5** | DeepSeek-R1 | [outputs](results/spider2-snow/deepseek-r1) |

## Architecture

<p align="center">
  <img src="assets/DeepEye-SQL.png" alt="DeepEye-SQL architecture" width="100%">
</p>

## Runbooks

Local application integration: [measured 10-question baseline](docs/baseline-10.md) · [50-question ablation](docs/ablation-50.md) · [failure analysis](docs/failure-analysis.md) · [single-question SQL Skill](docs/sql-skill.md) · [demo entry plan](docs/demo-entry-plan.md) · [demo guide (答辩演示说明)](docs/demo-guide.md) · [internship release checklist (实习版收口清单)](docs/internship-release-checklist.md).

Start the local demonstration UI with `.venv/Scripts/python.exe -B runner/run_demo.py` (or `uv run --no-sync python -B runner/run_demo.py`), then open `http://127.0.0.1:8000`. See [docs/demo-guide.md](docs/demo-guide.md) for the one-page walkthrough and the three pre-verified demo questions.

The root README is intentionally kept as a project overview. Use the runbooks for setup, config edits, dataset preparation, execution commands, inspection, export, and evaluation.

| Dataset | Runbook | Template families |
| --- | --- | --- |
| BIRD | [docs/bird.md](docs/bird.md) | `config/template/*/config-bird-dev.toml`, `config-bird-test.toml` |
| Spider | [docs/spider.md](docs/spider.md) | `config/template/*/config-spider-test.toml` |
| Spider2 | [docs/spider2.md](docs/spider2.md) | `config/template/*/config-spider2-lite.toml`, `config-spider2-snow.toml` |

Tracked config templates are grouped by model under [config/template](config/template/). Local experiment configs should be copied under `config/local/<model>/`, which is ignored by git.

## Repository Map

| Path | Purpose |
| --- | --- |
| [app/](app) | Core config, dataset, database, LLM, prompt, service, vector index, and pipeline code. |
| [app/few_shot/](app/few_shot) | Dynamic few-shot masking, indexing, retrieval, and runtime preparation. |
| [config/template/](config/template) | Public model-organized TOML templates. |
| [docs/](docs) | Dataset-specific runbooks for fresh-checkout reproduction. |
| [runner/](runner) | Python entry points for individual stages, export, inspection, and evaluation. |
| [script/](script) | Shell wrappers for dataset-level runs. |
| [results/](results) | Released predictions and benchmark outputs. |
| `workspace/` | Generated local snapshots and intermediate outputs. Ignored by git. |

## Citation

If you find DeepEye-SQL useful in your research, please cite:

Paper: [https://doi.org/10.1145/3802035](https://doi.org/10.1145/3802035)

```bibtex
@article{10.1145/3802035,
author = {Li, Boyan and Chen, Chong and Xue, Zhujun and Mei, Yinan and Luo, Yuyu},
title = {DeepEye-SQL: A Software-Engineering-Inspired Text-to-SQL Framework},
year = {2026},
issue_date = {June 2026},
publisher = {Association for Computing Machinery},
address = {New York, NY, USA},
volume = {4},
number = {3},
url = {https://doi.org/10.1145/3802035},
doi = {10.1145/3802035},
journal = {Proc. ACM Manag. Data},
month = may,
articleno = {158},
numpages = {28},
keywords = {text-to-sql, databases, large language models}
}
```

## License

This project is released under the MIT License. See [LICENSE](LICENSE).

## Acknowledgement

DeepEye-SQL builds on public benchmark ecosystems and OpenAI-compatible LLM serving stacks. We thank the maintainers of Spider, BIRD, Spider2, ChromaDB, OpenAI-compatible serving frameworks, and the broader Text-to-SQL research community.
