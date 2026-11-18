"""Reproducible generator for ``app/enterprise/adventureworks_metrics.json``.

Run: ``python scripts/build_aw_metrics.py``

The generator is deliberately strict. It refuses to write the metric registry if:
* a referenced table or column does not exist in the real SQLite database,
* a metric formula is not a VERBATIM quote from its cited policy clause,
* a declared time field is not covered by the metric's source columns.

All policies are SIMULATED / UNREVIEWED (docs/simulated-policies/), and the
underlying AdventureWorks OLTP data is a public Microsoft SAMPLE database,
not real production data. Generated metrics stay reviewed=False / untrusted.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "adventureworks" / "adventureworks.sqlite"
OUT = ROOT / "app" / "enterprise" / "adventureworks_metrics.json"
POLICY_DIR = ROOT / "docs" / "simulated-policies"

SOH = "Sales.SalesOrderHeader"
SOD = "Sales.SalesOrderDetail"
ST = "Sales.SalesTerritory"
SP = "Sales.SalesPerson"
SPQ = "Sales.SalesPersonQuotaHistory"
P = "Production.Product"
PSC = "Production.ProductSubcategory"
PC = "Production.ProductCategory"
PCH = "Production.ProductCostHistory"
PI = "Production.ProductInventory"
LOC = "Production.Location"
TH = "Production.TransactionHistory"
WO = "Production.WorkOrder"
POH = "Purchasing.PurchaseOrderHeader"
POD = "Purchasing.PurchaseOrderDetail"
PV = "Purchasing.ProductVendor"
V = "Purchasing.Vendor"
E = "HumanResources.Employee"
EPH = "HumanResources.EmployeePayHistory"

POLICIES = {
    "S1": "POL-SALES-001-销售口径与订单确认",
    "S2": "POL-SALES-002-净销售额与折扣处理",
    "C1": "POL-COST-001-标准成本与毛利口径",
    "C2": "POL-COST-002-历史成本匹配",
    "PF1": "POL-PERF-001-销售绩效与配额",
    "PF2": "POL-PERF-002-员工与薪酬口径",
    "R1": "POL-REGION-001-区域归属与汇总",
    "I1": "POL-INV-001-库存数量与库位",
    "I2": "POL-INV-002-再订货点与报废",
    "PU1": "POL-PUR-001-采购订单与供应商",
    "PR1": "POL-PROD-001-商品分类与在售状态",
}

R_DUP = ("duplicate_aggregation",
         "订单头金额与订单明细行直接 JOIN 会按明细行数重复计入",
         "先在明细层按 SalesOrderID 聚合，再与订单头 1:1 关联")
R_GRAIN = ("mixed_grain",
           "源表为多对一/一对多粒度，直接 JOIN 或逐行比较会重复计数或误判",
           "先按业务主键聚合到目标粒度，再关联")
R_ZERO = ("division_by_zero",
          "分母可能为零，直接相除会得到 NULL 或运行时错误",
          "使用 NULLIF(denominator, 0)，分母为零时返回 NULL 而非 0")


def S(metric_id, name, domain, policy, clause, quote, tables, fields, agg,
      *, grain="UNKNOWN", time_field=None, dims=None, joins=None,
      risks=None, keywords=None):
    return dict(id=metric_id, name=name, domain=domain, policy=policy,
                clause=clause, quote=quote, tables=tables, fields=fields,
                agg=agg, grain=grain, time_field=time_field, dims=dims or [],
                joins=joins or [], risks=risks or [],
                keywords=keywords or [name, metric_id])


SPECS = [
    S("aw_sales_amount", "销售额（不含税运费）", "sales", "S1", "3.1",
      'SUM(CAST("SubTotal" AS REAL)) FROM "Sales.SalesOrderHeader"', [SOH],
      [f"{SOH}.SubTotal", f"{SOH}.OrderDate"], "SUM", grain="订单头",
      time_field=f"{SOH}.OrderDate", dims=[f"{SOH}.TerritoryID"],
      keywords=["销售额", "营收", "sales amount"]),
    S("aw_total_due", "应收总额", "sales", "S1", "3.4",
      'SUM(CAST("TotalDue" AS REAL)) FROM "Sales.SalesOrderHeader"', [SOH],
      [f"{SOH}.TotalDue", f"{SOH}.OrderDate"], "SUM", grain="订单头",
      time_field=f"{SOH}.OrderDate"),
    S("aw_tax_amount", "税额", "sales", "S1", "3.5",
      'SUM(CAST("TaxAmt" AS REAL)) FROM "Sales.SalesOrderHeader"', [SOH],
      [f"{SOH}.TaxAmt", f"{SOH}.OrderDate"], "SUM", grain="订单头",
      time_field=f"{SOH}.OrderDate"),
    S("aw_freight_amount", "运费", "sales", "S1", "3.6",
      'SUM(CAST("Freight" AS REAL)) FROM "Sales.SalesOrderHeader"', [SOH],
      [f"{SOH}.Freight", f"{SOH}.OrderDate"], "SUM", grain="订单头",
      time_field=f"{SOH}.OrderDate"),
    S("aw_order_count", "订单数", "sales", "S1", "3.2",
      'COUNT(DISTINCT "SalesOrderID") FROM "Sales.SalesOrderHeader"', [SOH],
      [f"{SOH}.SalesOrderID", f"{SOH}.OrderDate"], "COUNT_DISTINCT",
      grain="订单头", time_field=f"{SOH}.OrderDate"),
    S("aw_avg_order_value", "平均订单金额", "sales", "S1", "3.3",
      'SUM(CAST("SubTotal" AS REAL)) / NULLIF(COUNT(DISTINCT "SalesOrderID"), 0) FROM "Sales.SalesOrderHeader"',
      [SOH], [f"{SOH}.SubTotal", f"{SOH}.SalesOrderID", f"{SOH}.OrderDate"],
      "RATIO", grain="订单头", time_field=f"{SOH}.OrderDate", risks=[R_ZERO]),
    S("aw_line_net_sales", "明细净销售额", "sales", "S2", "3.1",
      'SUM(CAST("LineTotal" AS REAL)) FROM "Sales.SalesOrderDetail"', [SOD],
      [f"{SOD}.LineTotal", f"{SOD}.SalesOrderID"], "SUM", grain="订单明细行",
      risks=[R_DUP]),
    S("aw_units_sold", "销售件数", "sales", "S2", "3.2",
      'SUM("OrderQty") FROM "Sales.SalesOrderDetail"', [SOD],
      [f"{SOD}.OrderQty"], "SUM", grain="订单明细行"),
    S("aw_discount_amount", "折扣金额", "sales", "S2", "3.4",
      'SUM(CAST("UnitPrice" AS REAL) * "OrderQty") - SUM(CAST("LineTotal" AS REAL))',
      [SOD], [f"{SOD}.UnitPrice", f"{SOD}.OrderQty", f"{SOD}.LineTotal"],
      "SUM", grain="订单明细行"),
    S("aw_standard_cost", "当前标准成本", "product", "C1", "3.1",
      'CAST("StandardCost" AS REAL)', [P],
      [f"{P}.StandardCost", f"{P}.ProductID"], "NONE", grain="商品"),
    S("aw_list_price", "当前目录价", "product", "C1", "3.2",
      'CAST("ListPrice" AS REAL)', [P],
      [f"{P}.ListPrice", f"{P}.ProductID"], "NONE", grain="商品"),
    S("aw_unit_margin", "单位毛利", "product", "C1", "3.3",
      'CAST("ListPrice" AS REAL) - CAST("StandardCost" AS REAL)', [P],
      [f"{P}.ListPrice", f"{P}.StandardCost", f"{P}.ProductID"], "NONE",
      grain="商品"),
    S("aw_gross_margin_rate", "毛利率", "product", "C1", "3.4",
      '(CAST("ListPrice" AS REAL) - CAST("StandardCost" AS REAL)) / NULLIF(CAST("ListPrice" AS REAL), 0)',
      [P], [f"{P}.ListPrice", f"{P}.StandardCost", f"{P}.ProductID"],
      "RATIO", grain="商品", risks=[R_ZERO]),
    S("aw_product_count", "商品数", "product", "PR1", "3.1",
      'COUNT("ProductID") FROM "Production.Product"', [P],
      [f"{P}.ProductID"], "COUNT", grain="商品"),
    S("aw_active_product_count", "在售商品数", "product", "PR1", "3.2",
      'COUNT(*) WHERE "FinishedGoodsFlag" = 1 AND "SellEndDate" IS NULL', [P],
      [f"{P}.ProductID", f"{P}.FinishedGoodsFlag", f"{P}.SellEndDate"],
      "COUNT", grain="商品"),
    S("aw_subcategory_count", "商品子类别数", "product", "PR1", "3.3",
      'COUNT("ProductSubcategoryID") FROM "Production.ProductSubcategory"',
      [PSC], [f"{PSC}.ProductSubcategoryID"], "COUNT", grain="子类别"),
    S("aw_category_count", "商品类别数", "product", "PR1", "3.4",
      'COUNT("ProductCategoryID") FROM "Production.ProductCategory"', [PC],
      [f"{PC}.ProductCategoryID"], "COUNT", grain="类别",
      joins=["Product.ProductSubcategoryID -> ProductSubcategory.ProductSubcategoryID -> "
             "ProductCategory.ProductCategoryID（必要中间表 ProductSubcategory）"]),
    S("aw_on_hand_qty", "库存总量", "inventory", "I1", "3.1",
      'SUM("Quantity") FROM "Production.ProductInventory"', [PI],
      [f"{PI}.Quantity"], "SUM", grain="商品×库位"),
    S("aw_product_on_hand_qty", "商品库存量", "inventory", "I1", "3.2",
      'SELECT "ProductID", SUM("Quantity") FROM "Production.ProductInventory" GROUP BY "ProductID"',
      [PI], [f"{PI}.ProductID", f"{PI}.Quantity"], "SUM",
      grain="商品（需先聚合）", risks=[R_GRAIN]),
    S("aw_location_count", "库位数", "inventory", "I1", "3.3",
      'COUNT("LocationID") FROM "Production.Location"', [LOC],
      [f"{LOC}.LocationID"], "COUNT", grain="库位"),
    S("aw_transaction_count", "库存事务数", "inventory", "I1", "3.4",
      'COUNT("TransactionID") FROM "Production.TransactionHistory"', [TH],
      [f"{TH}.TransactionID"], "COUNT", grain="库存事务"),
    S("aw_below_reorder_count", "低于再订货点商品数", "inventory", "I2", "3.1",
      'SELECT COUNT(*) FROM (SELECT "ProductID", SUM("Quantity") AS qty FROM '
      '"Production.ProductInventory" GROUP BY "ProductID") inv JOIN '
      '"Production.Product" p ON inv."ProductID" = p."ProductID" '
      'WHERE inv.qty < p."ReorderPoint"', [PI, P],
      [f"{PI}.ProductID", f"{PI}.Quantity", f"{P}.ProductID", f"{P}.ReorderPoint"],
      "COUNT", grain="商品（必须先聚合）", risks=[R_GRAIN]),
    S("aw_work_order_count", "工单数", "inventory", "I2", "3.2",
      'COUNT("WorkOrderID") FROM "Production.WorkOrder"', [WO],
      [f"{WO}.WorkOrderID"], "COUNT", grain="工单"),
    S("aw_work_order_qty", "工单计划产量", "inventory", "I2", "3.3",
      'SUM("OrderQty") FROM "Production.WorkOrder"', [WO],
      [f"{WO}.OrderQty"], "SUM", grain="工单"),
    S("aw_scrap_rate", "报废率", "inventory", "I2", "3.5",
      'SUM("ScrappedQty") / NULLIF(SUM("OrderQty"), 0) FROM "Production.WorkOrder"',
      [WO], [f"{WO}.ScrappedQty", f"{WO}.OrderQty"], "RATIO", grain="工单",
      risks=[R_ZERO]),
    S("aw_historical_standard_cost", "历史标准成本", "inventory", "C2", "3.5",
      'SELECT CAST("StandardCost" AS REAL) FROM "Production.ProductCostHistory" '
      'WHERE "ProductID" = :product AND "StartDate" <= :event AND '
      '("EndDate" IS NULL OR :event < "EndDate")', [PCH],
      [f"{PCH}.ProductID", f"{PCH}.StartDate", f"{PCH}.EndDate",
       f"{PCH}.StandardCost"], "NONE", grain="商品×成本区间", risks=[R_GRAIN]),
    S("aw_purchase_order_count", "采购订单数", "purchasing", "PU1", "3.1",
      'COUNT(DISTINCT "PurchaseOrderID") FROM "Purchasing.PurchaseOrderHeader"',
      [POH], [f"{POH}.PurchaseOrderID"], "COUNT_DISTINCT", grain="采购订单头"),
    S("aw_purchase_total_due", "采购应付总额", "purchasing", "PU1", "3.2",
      'SUM(CAST("TotalDue" AS REAL)) FROM "Purchasing.PurchaseOrderHeader"',
      [POH], [f"{POH}.TotalDue"], "SUM", grain="采购订单头"),
    S("aw_purchase_units", "采购件数", "purchasing", "PU1", "3.3",
      'SUM("OrderQty") FROM "Purchasing.PurchaseOrderDetail"', [POD],
      [f"{POD}.OrderQty"], "SUM", grain="采购明细行"),
    S("aw_received_qty", "收货数量", "purchasing", "PU1", "3.4",
      'SUM(CAST("ReceivedQty" AS REAL)) FROM "Purchasing.PurchaseOrderDetail"',
      [POD], [f"{POD}.ReceivedQty"], "SUM", grain="采购明细行"),
    S("aw_active_vendor_count", "活跃供应商数", "purchasing", "PU1", "3.5",
      'COUNT(*) FROM "Purchasing.Vendor" WHERE "ActiveFlag" = 1', [V],
      [f"{V}.BusinessEntityID", f"{V}.ActiveFlag"], "COUNT", grain="供应商"),
    S("aw_avg_lead_time", "平均交付提前期", "purchasing", "PU1", "3.6",
      'AVG("AverageLeadTime") FROM "Purchasing.ProductVendor"', [PV],
      [f"{PV}.AverageLeadTime"], "AVG", grain="商品×供应商"),
    S("aw_employee_count", "在职员工数", "hr", "PF2", "3.1",
      'COUNT(*) FROM "HumanResources.Employee" WHERE "CurrentFlag" = 1', [E],
      [f"{E}.BusinessEntityID", f"{E}.CurrentFlag"], "COUNT", grain="员工"),
    S("aw_avg_pay_rate", "平均薪资费率（历史记录）", "hr", "PF2", "3.3",
      'AVG(CAST("Rate" AS REAL)) FROM "HumanResources.EmployeePayHistory"',
      [EPH], [f"{EPH}.Rate", f"{EPH}.RateChangeDate"], "AVG",
      grain="员工×调薪日期", risks=[R_GRAIN]),
    S("aw_region_count", "销售区域数", "region", "R1", "3.1",
      'COUNT("TerritoryID") FROM "Sales.SalesTerritory"', [ST],
      [f"{ST}.TerritoryID"], "COUNT", grain="区域"),
    S("aw_region_sales_amount", "区域销售额", "region", "R1", "3.2",
      'SELECT "TerritoryID", SUM(CAST("SubTotal" AS REAL)) FROM '
      '"Sales.SalesOrderHeader" GROUP BY "TerritoryID"', [SOH],
      [f"{SOH}.TerritoryID", f"{SOH}.SubTotal"], "SUM", grain="区域",
      dims=[f"{SOH}.TerritoryID"]),
    S("aw_quota_attainment", "配额达成率", "performance", "PF1", "3.3",
      'CAST("SalesYTD" AS REAL) / NULLIF(CAST("SalesQuota" AS REAL), 0)',
      [SP, SPQ], [f"{SP}.SalesYTD", f"{SP}.BusinessEntityID",
                  f"{SPQ}.SalesQuota", f"{SPQ}.BusinessEntityID"],
      "RATIO", grain="销售人员×配额期", risks=[R_ZERO, R_GRAIN]),
    S("aw_salesperson_count", "销售人员数", "performance", "PF1", "3.4",
      'COUNT(DISTINCT "BusinessEntityID") FROM "Sales.SalesPerson"', [SP],
      [f"{SP}.BusinessEntityID"], "COUNT_DISTINCT", grain="销售人员"),
]


def validate(specs):
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    columns = {}
    for table in tables:
        escaped = table.replace('"', '""')
        columns[table] = {row[1] for row in connection.execute(
            f'PRAGMA table_info("{escaped}")')}
    errors = []
    for spec in specs:
        for table in spec["tables"]:
            if table not in tables:
                errors.append(f"{spec['id']}: missing table {table}")
        for reference in spec["fields"]:
            if "." not in reference:
                errors.append(f"{spec['id']}: bad column reference {reference}")
                continue
            table, column = reference.rsplit(".", 1)
            if table not in columns or column not in columns[table]:
                errors.append(f"{spec['id']}: missing column {reference}")
        if spec["time_field"] and spec["time_field"] not in spec["fields"]:
            errors.append(f"{spec['id']}: time_field not covered by source columns")
        policy = POLICY_DIR / f"{POLICIES[spec['policy']]}.md"
        if spec["quote"] not in policy.read_text(encoding="utf-8"):
            errors.append(f"{spec['id']}: formula not verbatim in {policy.name}")
    connection.close()
    return errors


def build_record(spec):
    policy = POLICIES[spec["policy"]]
    source_ref = f"docs/simulated-policies/{policy}.md#{spec['clause']}"
    risks = [
        {"kind": kind, "description": description,
         "affected_tables": list(spec["tables"]),
         "required_mitigation": mitigation, "source_ref": source_ref}
        for kind, description, mitigation in spec["risks"]
    ]
    return {
        "id": spec["id"], "name": spec["name"], "keywords": spec["keywords"],
        "domain": spec["domain"], "database_id": "adventureworks",
        "definition": (f"模拟条款 {policy} §{spec['clause']}（UNREVIEWED，"
                       "非微软官方业务规则）。底层为微软公开示例库 "
                       "AdventureWorks OLTP，非真实生产企业数据。"),
        "formula": spec["quote"], "tables": spec["tables"],
        "fields": spec["fields"],
        "structured": {
            "expression": spec["quote"], "aggregation": spec["agg"],
            "grain": spec["grain"],
            "time_field": spec["time_field"] or "UNKNOWN", "unit": "UNKNOWN",
            "allowed_dimensions": spec["dims"],
            "join_constraints": spec["joins"], "required_filters": [],
            "source_ref": source_ref, "evidence_status": "partial",
            "provenance": {
                "expression": f"verbatim: {policy} §{spec['clause']}（模拟待审核条款）",
                "source_tables": "verified: SQLite PRAGMA table_info（真实库元数据）",
                "source_columns": "verified: SQLite PRAGMA table_info（真实库元数据）",
                "aggregation": "derived: 模拟条款中的聚合函数字面",
                "grain": ("derived: 由真实主键/复合主键推断"
                          if spec["grain"] != "UNKNOWN" else "unknown: 原文未指定统计粒度"),
                "unit": "unknown: 示例库未声明币种与计量单位",
                "join_constraints": ("derived: 真实外键与必要中间表"
                                     if spec["joins"] else "unknown: 未人工审核关联约束"),
                "allowed_dimensions": ("derived: 真实维度字段（模拟制度允许）"
                                       if spec["dims"] else "unknown: 未人工审核允许维度"),
            },
            "aggregation_risks": risks,
        },
    }


def main():
    errors = validate(SPECS)
    if errors:
        for item in errors:
            print("ERROR:", item)
        raise SystemExit(2)
    payload = {
        "knowledge_version": "1",
        "source": {
            "database": "adventureworks",
            "database_origin": "https://github.com/microsoft/sql-server-samples (MIT)",
            "sqlite_port": "https://adventureworks.demodb.dev/downloads/",
            "policies": "docs/simulated-policies/ (SIMULATED, UNREVIEWED — not Microsoft official rules)",
            "notice": "示例数据 + 模拟制度；不得描述为真实生产企业数据或真实业务结论。",
        },
        "metrics": [build_record(spec) for spec in SPECS],
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    domains = {}
    for spec in SPECS:
        domains[spec["domain"]] = domains.get(spec["domain"], 0) + 1
    print(json.dumps({"validated": len(SPECS), "written": OUT.as_posix(),
                      "domains": dict(sorted(domains.items())),
                      "risks": sum(len(s["risks"]) for s in SPECS)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
