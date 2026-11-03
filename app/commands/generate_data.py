"""``enterprise-agent generate-data`` — 生成销售与 HR 两套合成企业库（幂等）。"""
from __future__ import annotations

import argparse


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--force", action="store_true",
                        help="忽略既有库的 schema 版本校验，直接重建（会覆盖现有数据）")


def run(args: argparse.Namespace) -> int:
    from app.config import PROJECT_ROOT
    from app.enterprise import ensure_enterprise_sales_database, ensure_enterprise_hr_database

    sales = ensure_enterprise_sales_database(PROJECT_ROOT, force=args.force)
    hr = ensure_enterprise_hr_database(PROJECT_ROOT, force=args.force)
    print(f"enterprise_sales: {sales}")
    print(f"enterprise_hr   : {hr}")
    return 0
