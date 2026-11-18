"""AdventureWorks OLTP sample database: provenance, registration and SQL dialect.

IMPORTANT — data provenance
---------------------------
* This is a **public Microsoft sample database**, not real production data.
  Upstream: https://github.com/microsoft/sql-server-samples
  (samples/databases/adventure-works/oltp-install-script), MIT licensed.
* The SQLite port was downloaded from https://adventureworks.demodb.dev/downloads/
  and verified against the provider's pinned SHA-256 checksums.
* Every policy document under ``docs/simulated-policies/`` is a **SIMULATED**
  enterprise policy written for this project's knowledge-governance demo.
  They are NOT Microsoft business rules and have NOT been reviewed by any real
  company. Metrics derived from them stay ``reviewed=False`` / untrusted.

The runtime registration is opt-in (``ENABLE_ADVENTUREWORKS``) so the existing
two enterprise databases, the 15-question metric-RAG benchmark and the
``app.cli check`` readiness report keep their exact current behaviour.
"""
from __future__ import annotations

import os
from pathlib import Path

DATABASE_ID = "adventureworks"
RELATIVE_PATH = "data/adventureworks/adventureworks.sqlite"

# Opt-in switch. The value index for this database requires an online embedding
# model (DashScope qwen3.7-text-embedding-flash); without an index, preflight
# would report the whole application as not ready. Enabling it is therefore an
# explicit operator decision, and the missing-index state is reported honestly.
ENABLE_ENV_VAR = "ENABLE_ADVENTUREWORKS"

# Verified facts (see docs/adventureworks/PROVENANCE.md).
SOURCE_SHA256 = "6a105e1982becfe003fc7a307d7cad9d738390cedd79d166c2167fd817168109"
SOURCE_BYTES = 126820352
TABLE_COUNT = 71
COLUMN_COUNT = 486
ROW_COUNT = 759240

DISPLAY_NAME = "AdventureWorks OLTP（微软公开示例库）"
DESCRIPTION = "销售、商品、采购、库存与人力示例数据；非真实生产企业数据"


def is_enabled() -> bool:
    return os.environ.get(ENABLE_ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


def database_path(project_root: Path) -> Path:
    """Return the curated, read-only AdventureWorks SQLite path (no side effects)."""
    return Path(project_root).resolve() / RELATIVE_PATH


def registered_path(project_root: Path) -> Path | None:
    """Return the path only when the operator enabled it AND the file exists."""
    if not is_enabled():
        return None
    path = database_path(project_root)
    return path.resolve() if path.is_file() else None


# --------------------------------------------------------------------------- #
# SQL dialect constraints — injected as evidence, never as generated SQL code.
# DeepEye-SQL's schema linking / SQL generation algorithms are untouched.
# --------------------------------------------------------------------------- #
SQL_DIALECT_HINT = """[AdventureWorks SQLite 移植版 · 数据源方言约束]
1. 表名含 SQL Server Schema 点号前缀（如 Sales.SalesOrderHeader）。
   在 SQL 中必须用双引号包裹完整表名：FROM "Sales.SalesOrderHeader"。
   禁止写 FROM Sales.SalesOrderHeader，也禁止写 "Sales"."SalesOrderHeader"（SQLite 会视为库.表）。
2. 金额/成本列声明为 DECIMAL_TEXT(...)，实际以 TEXT 存储。
   聚合、比较、排序时必须显式 CAST(... AS REAL)，例如 SUM(CAST("SubTotal" AS REAL))；
   直接对文本列做 MAX/ORDER BY 会得到字典序结果。
3. 日期列为 TEXT，格式 'YYYY-MM-DD HH:MM:SS.mmm'。
   时间过滤使用 substr("OrderDate",1,10) 或 date("OrderDate")，不要依赖 datetime() 隐式转换。
4. 复合外键 Sales.SalesOrderDetail(SpecialOfferID, ProductID) -> Sales.SpecialOfferProduct 必须两列同时关联。
5. 仅允许 SELECT；只读执行沙箱独立于本提示词强制生效。"""


def sql_dialect_hint(database_id: str | None) -> str:
    """Return dialect evidence only for AdventureWorks; empty for all other DBs."""
    return SQL_DIALECT_HINT if database_id == DATABASE_ID else ""
