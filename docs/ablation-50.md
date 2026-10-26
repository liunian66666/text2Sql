# 50题多数据库消融评测

## 样本

样本清单位于 `evals/bird-dev-50/sample.json`，使用固定种子 `20261025` 从以下5个BIRD-dev数据库分层抽样，每库10题：

- `california_schools`
- `superhero`
- `student_club`
- `toxicology`
- `thrombosis_prediction`

每库包含4道简单、3道中等、3道困难题，总计20道简单、15道中等、15道困难题。清单保存原始数据文件SHA-256、题号、数据库、难度和问题文本。

重新生成相同清单：

```powershell
.venv/Scripts/python.exe -B runner/select_bird_sample.py --output evals/bird-dev-50/sample.json
```

## 实习作品集配置

已完成实验使用 `config/template/baseline-cloud/config-bird-dev-50-portfolio.toml`：并发 4，Schema Linking 各采样 1 次，三个生成器各采样 2 次，最多保留 6 个生成候选。这个预算兼顾多候选选择与实习项目可复跑成本。高采样模板 `config-bird-dev-50-ablation.toml` 仍保留作扩展实验。

复制模板到 `config/local/`，填写向量接口密钥后预处理：

```powershell
$env:CONFIG_PATH="config/local/baseline-dashscope/config-bird-dev-50-portfolio.toml"
.venv/Scripts/python.exe -B runner/preprocess_dataset.py
```

完整运行：

```powershell
.venv/Scripts/python.exe -B runner/run_measured_baseline.py --config config/local/baseline-dashscope/config-bird-dev-50-portfolio.toml --output workspace/runs/bird-dev-portfolio-50-qwen
```

从某个阶段恢复：

```powershell
.venv/Scripts/python.exe -B runner/run_measured_baseline.py --config config/local/baseline-dashscope/config-bird-dev-50-portfolio.toml --output workspace/runs/bird-dev-portfolio-50-qwen --from-stage few_shot_preparation
```

## 三组消融

三组结果来自同一次完整运行，避免重复检索和模型调用：

| 方案 | 预测SQL |
|---|---|
| 仅生成 | `sql_candidates[0]` |
| 生成＋修订 | `sql_candidates_after_revision[0]` |
| 完整流程 | `final_selected_sql` |

导出报告：

```powershell
.venv/Scripts/python.exe -B runner/report_ablation.py --run workspace/runs/bird-dev-portfolio-50-qwen --output evals/bird-dev-50/ablation
```

报告同时给出集合式EX、严格多重集EX、可执行率、分数据库准确率、批次墙钟时间、在线token，以及修订和选择分别修复或破坏了哪些题。

## 实测结果（2026-10-26）

| 方案 | 集合式 EX | 严格 EX | 可执行率 | 批次墙钟时间 | 在线 Token |
| --- | ---: | ---: | ---: | ---: | ---: |
| 仅生成 | 36/50（72%） | 36/50（72%） | 50/50 | 726.99 秒 | 1,866,044 |
| 生成＋修订 | 36/50（72%） | 36/50（72%） | 50/50 | 753.08 秒 | 1,893,495 |
| 完整流程 | 38/50（76%） | 37/50（74%） | 50/50 | 762.34 秒 | 1,896,449 |

本次修订阶段没有改变集合式 EX；最终选择修复题号 77 和 1249，没有引入退化。完整流程分数据库集合式 EX 为：California Schools 70%、Student Club 80%、Superhero 90%、Thrombosis Prediction 60%、Toxicology 80%。一次云向量并发失败后以检查点恢复，重复调用的 43,142 Token 已单独记录，没有混入上表。

## 统计限制

- 这是固定50题分层样本，不是完整BIRD-dev官方成绩。
- 三组共享值检索、few-shot准备、Schema Linking和候选生成；因此差异直接对应修订与选择阶段。
- 在线成本按每个方案实际需要的阶段累计；离线值索引与few-shot索引单独记录。
- 中断尝试的消耗放在运行目录的 `measurement/recovery.json`，不混入三组可比指标。
