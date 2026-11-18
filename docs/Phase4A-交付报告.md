# Phase 4A 交付报告：选择性 Value Index

> 范围：Phase 4 规格 §二（4A）与 §九 Step 1–3。
> 原则：不消耗在线配额、不新增依赖、不改既有配置；所有数字来自真实执行。
> 时间：2026-11-13。

## 1. 本轮结论

Phase 4A 已**完整落地并真实构建**：选择性值索引的字段白名单、独立敏感字段排除、dry-run 预算统计、部分索引 manifest（含覆盖率/数据库指纹/Embedding 模型/策略版本）、运行时逐列安全降级、preflight 部分状态如实展示，全部经真实执行与单测验证。

- 真实本地构建：**7 列 / 5 表 / 1,193 个值 / 1024 维 / 19 次 embedding 请求 / 1m50s / 4.72 MiB**，使用本地 `Qwen3-Embedding-0.6B`，**零在线配额**。
- 全量回归：**Ran 226 tests OK**（215 基线 + 11 新增），compileall OK。
- **未做**（需另行授权）：在线端到端 SQL 生成（Step 4）、SQL 语义验证器（Step 6，未引入 sqlglot）、公平 Join 路径评测（Step 5）、指标人工审核。

## 2. 修改 / 新增文件清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `app/vector_db/value_index_policy.py` | 新增 | `ValueIndexPolicy` 模型 + 独立敏感字段注册表 + 白/黑名单匹配 |
| `app/vector_db/local_index.py` | 修改 | manifest 支持 `meta`（覆盖率/指纹/模型/策略）；新增 `read_local_index_manifest` / `classify_index_status` |
| `app/commands/build_index.py` | 修改 | 新增 `--tables/--columns/--exclude-columns/--max-values/--max-total-values/--max-requests/--include-all/--dry-run`；白名单严格默认、预算守卫、部分索引 manifest |
| `app/web/preflight.py` | 修改 | 新增 `value_index_kind / value_index_covered_tables / value_index_covered_columns` |
| `tests/test_value_index_policy.py` | 新增 | 11 项测试 |
| `config/local/adventureworks-value-index.toml` | 新增 | Phase 4A 专用配置（本地 Qwen3、独立 `store_root_path`）；`config/local` 已被 `.gitignore` 忽略 |
| `workspace/evals/value_index_dryrun.json` | 产物 | dry-run 真实报告（gitignore 覆盖的 workspace） |

> 未改动：`business_metrics.json`、`.env`、依赖、历史评测产物、既有 config。

## 3. 安全默认与白名单语义

- **真实构建默认拒绝全库**：未给 `--tables/--columns` 且未加 `--include-all` 时直接报错，杜绝「无意中索引全库」。
- 逐列判定顺序：**敏感字段 → 排除列 → 表白名单 → 列白名单**，敏感字段优先级最高。
- 支持 `Table.Column` 与裸列名、`*` 通配；正确处理 AdventureWorks 带点号前缀的表名（`"Production.Product"`）。

## 4. 敏感字段排除（独立安全策略）

排除逻辑不依赖单一列名正则，而是「显式 (表,列) 对 + 已知敏感表 + 已知敏感列名 + 真实表名校验」三层：

- 真实排除 **44** 个敏感项，含 `Person.EmailAddress.EmailAddress`、`Person.Password.PasswordHash/PasswordSalt`、`HumanResources.Employee.NationalIDNumber/LoginID`、`Sales.CreditCard.CardNumber`、`Person.PersonPhone.PhoneNumber`、`Person.Person.AdditionalContactInfo/Demographics`、`rowguid`（低分析价值高基数）。
- 反例验证：`Production.Product.Name` 不被误杀（列名为 Name 但非敏感）。

## 5. dry-run 真实结果（零 embedding）

命令见 §13。真实输出（`workspace/evals/value_index_dryrun.json`）：

| 纳入字段 | DISTINCT 值 |
|---|---|
| Production.Product.Name | 504 |
| Production.Product.ProductNumber | 504 |
| Production.ProductCategory.Name | 4 |
| Production.ProductModel.Name | 128 |
| Production.ProductSubcategory.Name | 37 |
| Sales.SalesTerritory.Name | 10 |
| Sales.SalesTerritory.CountryRegionCode | 6 |

- 纳入 7 列 / 累计 **1,193** 值；`batch_size=64`；预计 **19** 次 embedding 请求；`within_max_total_values=true`、`within_max_requests=true`。
- 排除 **246** 列：`table_not_in_whitelist=184`、`sensitive_field=44`、`column_not_in_whitelist=18`。
- 数据库指纹 `6a105e1982becfe0…` 与既有 AdventureWorks 记录一致。

## 6. 真实本地索引构建

```
Complete: adventureworks, 7 columns, kind=partial, covered_tables=5, excluded=246
```

- 模型：本地 `data/models/Qwen/Qwen3-Embedding-0___6B`（`api_type=local`、`use_qwen3_embedding=true`、`local_files_only=true`、`device=cpu`），**无任何在线调用**。
- 耗时 1m50s（含模型加载）；19 次 embedding 批调用。
- 存储：**4,950,467 B ≈ 4.72 MiB**，其中 npy 4,772.9 KiB；16 个文件（7×`.embeddings.npy` + 7×`.documents.json` + `manifest.json` 41.9 KB + `embedding_provenance.json`）。

## 7. manifest 元数据与状态分类

真实 `manifest.json` 关键字段：

```
index_kind           = partial
covered_tables       = [production.product, production.productcategory,
                        production.productmodel, production.productsubcategory,
                        sales.salesterritory]
covered_column_count = 7
embedding_model      = data/models/Qwen/Qwen3-Embedding-0___6B
embedding_dim        = 1024
database_fingerprint = 6a105e1982becfe0…
policy_version       = 1.0
excluded_columns     = 246 条（含 reason）
```

`classify_index_status()` 明确区分五态：`missing / incompatible（Embedding 构建配置不一致）/ stale（数据库已变）/ partial / complete`；旧 manifest 无新字段时按 `complete` 处理，保证 Sales/HR 既有索引行为不变。

## 8. 运行时兼容与安全降级

- `LocalValueIndex.retrieve_values_for_column` 对未覆盖列返回空值（真实验证：`Production.Product.Color → 0 值`），已覆盖列返回真实近邻（`Sales.SalesTerritory.Name → ['Germany','France','Southwest',…]`）。
- `sql_skill.py` 的 fail-fast 仅在**无 manifest** 时触发；部分索引有真实 manifest，不会无条件失败。未覆盖字段退化为空值示例，不编造。
- 查询与构建必须同一 Embedding 模型：`embedding_signature` 写入 manifest 与 provenance，`classify_index_status` 可检出不一致。

## 9. preflight 部分状态

`ENABLE_ADVENTUREWORKS=1` + AW 配置下真实输出：

```
{"id":"adventureworks","database_ready":true,"value_index_ready":true,
 "value_index_kind":"partial","value_index_covered_tables":5,"value_index_covered_columns":7}
{"id":"enterprise_hr","value_index_kind":"missing", ...}
{"id":"enterprise_sales","value_index_kind":"missing", ...}
```

部分索引不再被压成单一 ready 位；Sales/HR 状态未变。

## 10. 测试

- 新增 `tests/test_value_index_policy.py`：11 项，覆盖白名单、敏感排除、dry-run 预算、请求估算、部分 manifest、未覆盖列降级、签名/指纹不兼容、缺失索引。
- 全量：`Ran 226 tests OK`（215 → 226），`compileall` OK。

## 11. 未完成 / 需授权

| 项 | 状态 | 阻塞原因 |
|---|---|---|
| Step 4 在线端到端 Baseline/Enhanced | 未开始 | 需在线 LLM 配额批准（见 §14） |
| Step 5 公平 Join 路径评测（Baseline1/2 + Ours） | 未开始 | Baseline2 需 LLM；Baseline1 可离线 |
| Step 6 SQL 语义验证器 | 未开始 | 规格要求先获批 sqlglot；未获批前应做规则型 |
| §六 指标人工审核工作流 | 未开始 | 需具名人工决定 |
| hard_filter / 文档后台 / 全局社区摘要 / 多 Agent | 按规格不做 | — |

## 12. 红线与资源

- 未新增依赖；未改 `.env`；未改既有 config；未跑在线评测；未创建任何空 manifest 冒充就绪。
- 本地构建仅消耗 CPU；`config/local` 在 `.gitignore` 内。

## 13. 复现命令

```bash
# dry-run（离线，零 embedding）
./.venv/Scripts/python.exe -m app.cli build-index --dry-run \
  --database data/adventureworks/adventureworks.sqlite \
  --tables "Production.Product,Production.ProductCategory,Production.ProductSubcategory,Sales.SalesTerritory,Production.ProductModel" \
  --columns "Name,CountryRegionCode,ProductNumber" \
  --max-values 2000 --max-total-values 5000 --max-requests 100 \
  > workspace/evals/value_index_dryrun.json

# 真实本地部分索引（本地 Qwen3，零在线配额）
./.venv/Scripts/python.exe -m app.cli build-index \
  --config config/local/adventureworks-value-index.toml \
  --database data/adventureworks/adventureworks.sqlite \
  --tables "Production.Product,Production.ProductCategory,Production.ProductSubcategory,Sales.SalesTerritory,Production.ProductModel" \
  --columns "Name,CountryRegionCode,ProductNumber" \
  --max-values 2000 --max-total-values 5000 --max-requests 100

# 回归
./.venv/Scripts/python.exe -m unittest discover -s tests -q
```

产物：`workspace/runs/adventureworks-local/vector_database/adventureworks/local_index/{manifest.json,embedding_provenance.json,columns/*}`。

## 14. 待批准（Step 4 在线预算）

在线端到端**未执行**。若批准，建议上限：**3–5 题**（1 基础销售额 / 2 商品类别需中间表 / 3 订单头明细粒度 / 4 历史成本 / 5 多候选 Join），每题 Baseline 与 Enhanced 各 1 次，固定模型与参数；预估 **10 次 Agent 运行**，按六阶段 8 候选估计约 **~80 次 LLM 调用/题**（含重试预算），总 Token 上限设 ~2,000,000，超限即停。是否批准，以及是否先做离线规则型 SQL 验证器（Phase 4C，不引入 sqlglot），请确认。
