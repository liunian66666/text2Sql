# 10 题可测量基线

当前配置通过 OpenAI 协议调用 `已脱敏的 OpenAI 兼容模型服务地址`，模型名为 `qwen`；向量仍使用 `text-embedding-v4`，并复用 100 条训练样本 few-shot 索引。原有各生成、修复和选择采样预算保持不变。样本只有一个数据库，因此不能把结果作为完整 BIRD 分数。

## 配置与运行

无业务密钥模板：`config/template/baseline-cloud/config-bird-dev-10.toml`。聊天接口使用占位密钥 `EMPTY`；复制到 `config/local/baseline-dashscope/config-bird-dev-cloud.toml` 后，只需填写 DashScope 向量接口密钥。不要提交本地配置。OpenAI SDK 的 `base_url` 必须停在 `/v1`，SDK 会自动追加 `/chat/completions`。

数据要求：`data/bird/dev/dev.json`、对应 SQLite 数据库，以及 `data/bird/train/train.json`。训练标签来源和转换过程见 `script/download_dataset.sh`，无需为 few-shot 标签检索下载所有训练数据库。

PowerShell：

```powershell
$env:CONFIG_PATH="config/local/baseline-dashscope/config-bird-dev-cloud.toml"
.venv/Scripts/python.exe -B runner/preprocess_dataset.py
.venv/Scripts/python.exe -B runner/run_measured_baseline.py --config config/local/baseline-dashscope/config-bird-dev-cloud.toml --output workspace/runs/bird-dev-baseline-cloud
```

全流程依次完成值索引、训练 few-shot 索引、值检索、目标 few-shot 准备、schema linking、生成、修复和选择。原有阶段快照可断点续跑；失败会停止后续阶段。`measurement/stage_runs.jsonl` 追加每次尝试的时间和状态，`measurement/llm_usage.jsonl` 逐次记录响应 usage，训练索引与在线查询分开统计。

`complete_value_index.py` 会核对已完成列的文档后复用向量，并在同次构建中复用重复文本。更换向量模型须使用新目录。旧版没有模型来源记录的部分索引，必须先确认模型一致，再显式使用 `--reuse-legacy-columns`；本次云端运行没有复用本地模型向量。

## 导出报告

```powershell
.venv/Scripts/python.exe -B runner/report_baseline.py --run workspace/runs/bird-dev-baseline-qwen-http --output evals/bird-dev-10-qwen-http --reused-setup-ledger workspace/runs/bird-dev-baseline/measurement/llm_usage.jsonl
```

最后一个参数用于计入此前已完成、随后复用的训练掩码调用。全新运行无需这个参数。可重复导出报告而不调用模型。本次报告的前置生成来自已有 `qwen3.8-max` 快照，SQL 修订与选择由 `qwen` 完成，因此属于明确记录的混合模型续跑；从头运行模板时所有 LLM 阶段均使用 `qwen`。

输出：

- `report.md`：正确率、耗时、用量、逐题结果与统计口径。
- `summary.json`：脱敏配置、数据库哈希、分阶段指标。
- `questions.json`：逐题问题、预测/金标 SQL、执行状态及结果预览。
- `questions.csv`：可直接查看的逐题指标表。

EX 沿用项目已有评测器的行集合比较；另记录考虑重复行的多重集合比较。金标执行失败单列统计，不静默当作预测正确。每题 token 来自传播到嵌套工作线程的题号上下文，覆盖 few-shot 掩码和解析重试。

## 本地验证

```powershell
.venv/Scripts/python.exe -B -m unittest discover -s tests -v
```

若运行环境限制系统临时目录，可将 `TEMP` 和 `TMP` 指向仓库内可写的临时目录。
