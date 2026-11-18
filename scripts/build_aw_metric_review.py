"""Generate a *human review worksheet* for 14 AdventureWorks metric candidates.

This script never approves anything. It only writes
``app/enterprise/adventureworks_metric_review.json``. The separate decision
ledger ``adventureworks_review_decisions.json`` is created empty **only if it
does not exist** and is never overwritten afterwards.

Every reference SQL is executed against the real read-only SQLite sandbox;
results are recorded verbatim. Any missing table/column, missing metric id or
failed SQL aborts the build with exit code 2.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.skills.sql_skill import execute_readonly_table  # noqa: E402
from app.knowledge.sqlite_metadata import list_tables, read_table  # noqa: E402
from app.knowledge.schema_extractor import connect_readonly  # noqa: E402
from contextlib import closing  # noqa: E402

METRICS = ROOT / "app/enterprise/adventureworks_metrics.json"
EVIDENCE = ROOT / "data/adventureworks/query_evidence.json"
DATABASE = ROOT / "data/adventureworks/adventureworks.sqlite"
WORKSHEET = ROOT / "app/enterprise/adventureworks_metric_review.json"
DECISIONS = ROOT / "app/enterprise/adventureworks_review_decisions.json"

# evidence key -> (column name used for this metric, or None = whole result)
CANDIDATES: list[dict] = [
    {
        "metric_id": "aw_order_count", "evidence_key": "q01_orders_sales",
        "amount_scope": "订单数只取 COUNT(DISTINCT SalesOrderID)，与金额字段无关。",
        "questions": ["是否只统计已确认订单？示例库没有可核实的订单状态字典。",
                      "是否需要按 OrderDate 限定统计周期，当前 time_field 仅为候选。"],
    },
    {
        "metric_id": "aw_sales_amount", "evidence_key": "q01_orders_sales",
        "amount_scope": "SubTotal = 订单头小计，不含税与运费；是订单级金额，不能与明细表 JOIN 后重复求和。",
        "questions": ["币种与汇率口径 UNKNOWN，不能对外宣称人民币或美元。",
                      "是否含取消/退货订单无可靠字段依据。"],
    },
    {
        "metric_id": "aw_total_due", "evidence_key": None,
        "sql": 'SELECT ROUND(SUM(CAST("TotalDue" AS REAL)),2) AS total_due FROM "Sales.SalesOrderHeader"',
        "amount_scope": "TotalDue = SubTotal + TaxAmt + Freight，订单级应收总额；与 LineTotal（明细级）不可混用。",
        "questions": ["是否应剔税、剔运费属于业务口径，模拟条款未审核。"],
    },
    {
        "metric_id": "aw_tax_amount", "evidence_key": None,
        "sql": 'SELECT ROUND(SUM(CAST("TaxAmt" AS REAL)),2) AS tax FROM "Sales.SalesOrderHeader"',
        "amount_scope": "TaxAmt 为订单级税额。",
        "questions": ["跨区域税率汇总是否有业务意义未审核。"],
    },
    {
        "metric_id": "aw_freight_amount", "evidence_key": None,
        "sql": 'SELECT ROUND(SUM(CAST("Freight" AS REAL)),2) AS freight FROM "Sales.SalesOrderHeader"',
        "amount_scope": "Freight 为订单级运费。",
        "questions": ["运费是否计入净销售额需人工确认。"],
    },
    {
        "metric_id": "aw_avg_order_value", "evidence_key": None,
        "sql": 'SELECT ROUND(SUM(CAST("SubTotal" AS REAL))/NULLIF(COUNT(DISTINCT "SalesOrderID"),0),2) AS avg_order_value FROM "Sales.SalesOrderHeader"',
        "amount_scope": "分子用订单头 SubTotal（不是明细 LineTotal 之和），分母是订单数；口径需与 aw_sales_amount 保持一致。",
        "questions": ["是否改用 TotalDue 作分子未审核。", "零订单时除零风险已用 NULLIF 缓解，但业务上应返回 NULL 还是 0 未定。"],
    },
    {
        "metric_id": "aw_line_net_sales", "evidence_key": None,
        "sql": 'SELECT ROUND(SUM(CAST("LineTotal" AS REAL)),2) AS line_net_sales FROM "Sales.SalesOrderDetail"',
        "amount_scope": "LineTotal 为明细行净销售额（明细级），与订单头 SubTotal/TotalDue 是不同粒度。",
        "questions": ["LineTotal 是否已扣折扣由示例库计算列给出，但折扣业务含义未经人工审核。",
                      "按商品类别汇总需要 allowed_dimensions，当前指标未定义该维度。"],
    },
    {
        "metric_id": "aw_region_sales_amount", "evidence_key": "q14_territory_named",
        "amount_scope": "使用订单头 TerritoryID 关联 SalesTerritory，属订单归属区域口径；不得改用 Customer.TerritoryID。",
        "questions": ["跨币种区域销售额是否需要汇率换算 UNKNOWN。", "历史区域变更是否要按订单日期回溯未审核。"],
    },
    {
        "metric_id": "aw_on_hand_qty", "evidence_key": "q06_on_hand",
        "amount_scope": "库存总量按所有库位 Quantity 求和，属库位级原始量；商品级比较必须先聚合。",
        "questions": ["是否排除停用库位/报废品无可靠依据。"],
    },
    {
        "metric_id": "aw_product_on_hand_qty", "evidence_key": "q20_inventory_by_product",
        "amount_scope": "先按 ProductID 聚合到商品级，再统计覆盖商品数与守恒总量。",
        "questions": ["商品级库存是否应含零库存商品未审核。"],
    },
    {
        "metric_id": "aw_below_reorder_count", "evidence_key": "q07_below_reorder",
        "amount_scope": "必须先按商品聚合 Quantity，再与商品级 ReorderPoint 比较；逐库位比较是错误口径。",
        "questions": ["ReorderPoint 与 SafetyStockLevel 哪个为阈值需人工确认。"],
    },
    {
        "metric_id": "aw_purchase_total_due", "evidence_key": "q11_purchase",
        "amount_scope": "TotalDue 为采购订单头金额，与 PurchaseOrderDetail 直接 JOIN 会重复计数。",
        "questions": ["采购金额是否含税/运费未审核。"],
    },
    {
        "metric_id": "aw_historical_standard_cost", "evidence_key": "q09_cost_history_707",
        "amount_scope": "按 (ProductID, StartDate) 的历史区间成本，EndDate IS NULL 表示当前有效；不可用 Product.StandardCost 替代。",
        "questions": ["按订单日期匹配时必须限定 StartDate <= OrderDate < EndDate，区间边界开闭需人工确认。"],
    },
    {
        "metric_id": "aw_quota_attainment", "evidence_key": "q10_quota",
        "amount_scope": "SalesYTD 是系统预置快照，与配额历史 1:N 关联会重复；不得与实时订单聚合相加。",
        "questions": ["配额应按期取最新还是按区间匹配未审核。", "快照与实时聚合混用是明确风险。"],
    },
]

PENDING_QUESTIONS = [
    "商品分类销售额：当前 38 条指标中没有独立定义，可用 aw_line_net_sales 经 ProductSubcategory 中间表扩展到 ProductCategory 维度（参考 q03_category_sales），但是否批准为一个新指标、以及未分类商品是否计入，需人工决定。",
    "SubTotal / TotalDue / LineTotal 三种金额字段的业务命名与对外口径需人工确认，本清单仅按字段事实区分。",
    "所有指标 unit 均为 UNKNOWN（示例库未声明币种），不得对外标注具体币种。",
    "模拟制度条款全部 UNREVIEWED，不是微软官方业务规则，也不代表任何真实企业制度。",
]


def _load_evidence() -> dict:
    data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    return data


def _execute(sql: str) -> dict:
    columns, rows, truncated = execute_readonly_table(DATABASE, sql, max_rows=20, timeout=60)
    return {"columns": list(columns), "rows": rows, "truncated": bool(truncated)}


def build(*, validate_only: bool = False) -> dict:
    metrics = {m["id"]: m for m in json.loads(METRICS.read_text(encoding="utf-8"))["metrics"]}
    evidence = _load_evidence()

    with closing(connect_readonly(DATABASE)) as conn:
        real_columns = {
            name: {c.name for c in read_table(conn, name).columns}
            for name in list_tables(conn)
        }

    candidates = []
    for spec in CANDIDATES:
        metric_id = spec["metric_id"]
        if metric_id not in metrics:
            raise SystemExit(f"unknown metric_id: {metric_id}")
        metric = metrics[metric_id]
        structured = metric.get("structured", {})
        key = spec.get("evidence_key")
        if key:
            if key not in evidence:
                raise SystemExit(f"missing query evidence: {key}")
            sql = evidence[key]["sql"]
        else:
            sql = spec["sql"]
        for table in metric.get("tables", []):
            if table not in real_columns:
                raise SystemExit(f"table not in real DB: {table}")
            for field in metric.get("fields", []):
                table_name, column_name = field.rsplit(".", 1)
                if table_name == table and column_name not in real_columns[table]:
                    raise SystemExit(f"column not in real DB: {field}")
        result = _execute(sql)
        candidates.append({
            "metric_id": metric_id,
            "name": metric["name"],
            "business_definition": metric["definition"],
            "formula_complete": metric["formula"],
            "formula_source_ref": structured.get("source_ref", ""),
            "source_tables": list(metric.get("tables", [])),
            "source_columns": list(metric.get("fields", [])),
            "grain": structured.get("grain", "UNKNOWN"),
            "time_field": structured.get("time_field", "UNKNOWN"),
            "required_filters": list(structured.get("required_filters", [])),
            "join_constraints": list(structured.get("join_constraints", [])),
            "unit": structured.get("unit", "UNKNOWN"),
            "amount_field_scope_note": spec["amount_scope"],
            "reference_sql": sql,
            "reference_sql_source": f"data/adventureworks/query_evidence.json#{key}" if key else "本脚本生成并用只读沙箱真实执行",
            "reference_result": result,
            "policy_evidence": [structured.get("source_ref", "")],
            "official_field_basis": sorted({f"sqlite:PRAGMA table_info({t})" for t in metric.get("tables", [])}),
            "unresolved_business_questions": list(spec["questions"]),
            "review_status": "unreviewed",
            "reviewed": False,
            "trusted": False,
        })

    payload = {
        "worksheet_version": "1",
        "database_id": "adventureworks",
        "notice": ("本清单为人工审核准备材料，不是审核决定。所有指标 reviewed=False / trusted=False；"
                   "模拟制度条款是 SIMULATED / UNREVIEWED，不是微软官方业务规则；数据来自公开示例库，不是真实生产企业数据。"),
        "selection": {"registry_size": len(metrics), "selected": len(candidates),
                      "criterion": "覆盖订单/销售额/平均订单额/分类/区域/库存/再订货点/采购/历史成本等核心口径"},
        "metric_candidates": candidates,
        "pending_business_questions": PENDING_QUESTIONS,
        "decision_ledger": str(DECISIONS.relative_to(ROOT).as_posix()),
        "decision_policy": ("只有 decision_ledger 中出现 decision=approved 且 reviewer 为具名人工的显式记录，"
                           "review.apply() 才会把该指标 promoted；本脚本不写决定文件，也不批量批准。"),
    }
    if not validate_only:
        WORKSHEET.parent.mkdir(parents=True, exist_ok=True)
        WORKSHEET.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
        if not DECISIONS.exists():
            DECISIONS.write_text(json.dumps({
                "database_id": "adventureworks",
                "decisions": [],
                "notice": "人工审核决定文件。默认空；不得由脚本自动写入 approved 记录。",
            }, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    payload = build(validate_only=args.validate_only)
    print(json.dumps({
        "validated_only": args.validate_only,
        "selected": len(payload["metric_candidates"]),
        "all_unreviewed": all(not c["reviewed"] for c in payload["metric_candidates"]),
        "reference_sql_executed": len(payload["metric_candidates"]),
        "worksheet": str(WORKSHEET.relative_to(ROOT).as_posix()),
        "decision_ledger": payload["decision_ledger"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
