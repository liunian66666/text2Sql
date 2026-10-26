# DeepEye-SQL 演示站说明（答辩用一页纸）

本地单用户演示入口：单题在线演示 + 离线评测结果浏览。只读数据库、串行任务队列，不启动任何评测任务。

## 启动

```bash
# 在项目根目录 DeepEye-SQL/ 下执行
.venv/Scripts/python.exe -B runner/run_demo.py --check-only
.venv/Scripts/python.exe -B runner/run_demo.py

# 等价的 uv 启动方式（--no-sync 表示不动现有环境，直接复用 .venv）
uv run --no-sync python -B runner/run_demo.py

# uvicorn 零参工厂方式（不支持 --config，用默认配置或环境变量 CONFIG_PATH）
uv run --no-sync uvicorn app.demo.server:create_app_from_env --factory --port 8000
```

浏览器打开 <http://127.0.0.1:8000> 即可。可选参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--config` | `config/local/demo-fast/config-bird-demo.toml` | 快速演示配置（缺失时兼容回退到50题配置） |
| `--host` | `127.0.0.1` | 仅本机访问；答辩需要局域网投屏时改 `0.0.0.0` |
| `--port` | `8000` | 端口被占用时换一个，如 `--port 8010` |

启动前确认：`--config` 指向的配置文件里模型服务可达（可用 `curl <base_url>/models` 自检）。

## 页面结构

- **单题在线演示**：选数据库 → 选示例问题（或手输）→ 可填证据口径 → 开始分析。
  流水线七个阶段（值检索 / Few-shot 准备 / Schema Linking / SQL 生成 / SQL 修订 / SQL 选择 / 只读执行）实时推送状态与耗时；完成后展示初始生成、Checker 修订、最终选择三版 SQL，可复制，附指标卡（总耗时、Token、模型响应数、候选数、行数）与查询结果表。
- **离线评测结果**：自动读取 `evals/` 下已生成的 `summary.json` 报告，展示三组对照（仅生成 / 生成＋修订 / 完整流程）的正确率、可执行率、耗时、Token，修订与选择的修复/退化题号，以及逐题明细。50 题评测跑完后点“刷新结果”即出现。

## 三条推荐演示问题

三条均选自 `evals/bird-dev-10-qwen-http`，已端到端验证正确（集合式 EX 通过），数据库均为 **California Schools**：

| # | 演示点 | 问题（直接粘贴） | 证据（填“补充证据”框） |
| --- | --- | --- | --- |
| 1 | 快速成功案例：完整端到端流程 | `What is the highest eligible free rate for K-12 students in the schools in Alameda County?` | `Eligible free rate for K-12 = `Free Meal Count (K-12)` / `Enrollment (K-12)`` |
| 2 | 口径理解案例：证据驱动的条件过滤 | `Please list the zip code of all the charter schools in Fresno County Office of Education.` | `Charter schools refers to `Charter School (Y/N)` = 1 in the table fprm` |
| 3 | 复杂多表案例：Schema Linking + 候选选择 | `How many schools with an average score in Math greater than 400 in the SAT test are exclusively virtual?` | `Exclusively virtual refers to Virtual = 'F'` |

“修订纠错”展示点：结果区对比**初始生成**与**Checker 修订**两栏 SQL 的差异；若当次运行修订前后一致，说明初版已通过执行检查，可在评测页用“修订修复”题号做离线佐证。

## 快速配置实测（2026-10-26）

上面三条推荐问题均已用快速配置真实运行，并分别与 BIRD 标准 SQL 的执行结果比较：集合式 EX 3/3，严格多重集合 EX 3/3，平均耗时 41.62 秒，在线 Token 合计 153,436。逐题 SQL 和指标保存在 `evals/demo-fast/`，并会作为“3题基线评测”出现在离线评测页。

这三题的初始候选均已通过执行检查，因此修订没有改写，最终选择使用单一有效候选捷径。现场用它们展示稳定的端到端过程；修订和选择带来的变化在 50 题对照页展示。

## 演示流程建议（约 8 分钟）

1. 先开**离线评测结果**页，讲三组对照与修复/退化统计（1–2 分钟）。
2. 切**单题演示**，跑问题 1，边跑边讲七阶段流水线（等待期 1–3 分钟正好讲解）。
3. 用问题 3 收场，重点讲 Schema Linking 关联出的表.字段 chips 与候选选择。

## 排查

| 现象 | 处理 |
| --- | --- |
| 提交后长期停在“排队/运行中” | 模型服务被 50 题评测占满时会排队，属正常；评测结束后变快。看 `workspace/demo_server.log` 是否有 `retry` 告警 |
| 某阶段红色“失败” | 结果区会显示 `failed_stage`；先 `curl <base_url>/models` 确认模型服务在线，再换推荐问题重试 |
| 刷新页面丢了进度 | 用请求 ID 直接访问 `GET /api/query/<request_id>`；任务状态持久化在 `workspace/demo_tasks/` |
| 端口被占用 | `--port 8010` 换端口重启 |

安全边界：数据库仅服务端白名单注册；SQLite 只读连接 + authorizer 拦截写操作；执行超时与最大行数在页面上可调但服务端有上限；页面不回显密钥、绝对路径与堆栈。
