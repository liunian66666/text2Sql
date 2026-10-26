# 可演示入口

## 当前实现

演示入口已实现为 **FastAPI + 单页静态前端 + SSE阶段进度**。后端复用 `app.skills.SQLSkill`，在线请求与批量评测目录彼此独立；评测页只读取 `evals/` 下已生成的报告。

启动命令：

```powershell
.venv/Scripts/python.exe -B runner/run_demo.py
```

浏览器访问 `http://127.0.0.1:8000`。默认读取 `config/local/baseline-dashscope/config-bird-dev-50-ablation.toml`，也可指定其他已完成索引准备的配置：

```powershell
.venv/Scripts/python.exe -B runner/run_demo.py --config config/local/baseline-dashscope/config-bird-dev-50-ablation.toml --port 8000
```

页面提供单题在线演示和离线评测结果两个入口。单题请求串行运行，状态文件保存在 `workspace/demo_tasks/`，Skill产物保存在 `workspace/demo_requests/`。

## 推荐方案

采用 **FastAPI + 单页静态前端 + SSE阶段进度**。后端直接复用 `app.skills.SQLSkill`，不复制Text-to-SQL逻辑；前端只负责提交问题、展示阶段进度、SQL、结果表、耗时和token。

这个方案适合作为实习项目演示：依赖少、可本地启动、能清楚展示系统过程，也方便后续替换成React或接入企业内部页面。

## 演示页面

单页分成四块：

1. **输入区**：数据库下拉框、自然语言问题、可选证据、最大返回行数、运行按钮。
2. **阶段时间线**：值检索、few-shot准备、Schema Linking、SQL生成、SQL修订、SQL选择、执行。
3. **结果区**：最终SQL、可复制按钮、结果表格、截断提示、错误说明。
4. **指标区**：总耗时、各阶段耗时、输入/输出/总token、请求ID、产物目录。

增加一个“示例问题”下拉框，演示者可以直接选择准备好的成功案例、修订纠错案例和失败案例。

## 后端接口

| 接口 | 作用 |
|---|---|
| `GET /api/databases` | 返回已注册数据库及示例问题 |
| `POST /api/query` | 创建单问题任务，返回 `request_id` |
| `GET /api/query/{request_id}/events` | SSE推送阶段开始、完成、失败事件 |
| `GET /api/query/{request_id}` | 返回最终SQL、结果、耗时和token |
| `GET /api/evaluations` | 返回10题与50题评测摘要 |

第一版一次只运行一个请求。当前SQL Skill内部已有进程级锁，串行队列能避免共享服务配置冲突。页面应显示“等待中/运行中”，不让请求表现为卡死。

## 数据与安全

- 数据库在服务端配置文件中注册；浏览器只能提交数据库标识，不能提交任意文件路径。
- 最终SQL继续使用只读连接、SQLite authorizer、超时和最大返回行数限制。
- API响应不返回模型密钥、数据库绝对路径或内部异常堆栈。
- 运行产物保存在 `workspace/demo_requests/<request_id>/`，页面仅显示请求ID。
- 服务默认监听 `127.0.0.1`，演示需要局域网访问时再显式修改监听地址。

## 建议技术结构

```text
app/demo/api.py              FastAPI路由与SSE
app/demo/registry.py         数据库和示例问题注册
app/demo/task_manager.py     单任务队列、状态与事件
app/demo/static/index.html   单页界面
app/demo/static/app.js       请求、SSE与结果渲染
app/demo/static/style.css    页面样式
runner/run_demo.py           本地启动入口
config/local/demo.toml       本地数据库注册与模型配置
```

## 验收标准

- 从启动命令到页面可用不超过一分钟。
- 页面能完成一个真实问题的端到端运行。
- 每个阶段都有可见状态，失败时能定位到阶段。
- SQL和结果表可查看，空结果与错误结果明确区分。
- 耗时与token和Skill产物一致。
- 刷新页面后仍可用请求ID查看最近结果。
- 至少准备三个固定演示问题，其中一个能展示SQL修订带来的纠错。

## 实施顺序

1. 先实现FastAPI、任务状态和SSE，不做复杂前端框架。
2. 接入SQL Skill并完成一个数据库的真实调用。
3. 加入结果表、指标卡和示例问题。
4. 接入50题消融摘要，形成“在线问答＋离线评测”双演示页。
5. 最后补启动脚本、错误页和演示说明。

预计工作量为1至2个实习工作日。第一版建议坚持本地单用户、串行任务，不在演示阶段引入账号系统、分布式队列或复杂数据库上传功能。
