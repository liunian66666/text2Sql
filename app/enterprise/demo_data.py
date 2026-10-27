"""Create a deterministic, reproducible enterprise sales demo database."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import csv
import random
import sqlite3


SCHEMA_VERSION = 1


def ensure_enterprise_demo_database(project_root: Path, *, force: bool = False) -> Path:
    destination = project_root / "data" / "enterprise_sales" / "enterprise_sales.sqlite"
    if destination.exists() and not force:
        try:
            with sqlite3.connect(destination) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            if version == SCHEMA_VERSION and count >= 800:
                _write_descriptions(destination.parent)
                return destination.resolve()
        except sqlite3.Error:
            pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    _build(destination)
    _write_descriptions(destination.parent)
    return destination.resolve()


def _write_descriptions(database_dir: Path) -> None:
    descriptions = {
        "categories": [("category_id", "分类ID", "商品分类主键", "integer", ""), ("category_name", "分类名称", "商品所属业务分类", "text", "")],
        "products": [("product_id", "商品ID", "商品主键", "integer", ""), ("product_name", "商品名称", "可售商品名称", "text", ""), ("category_id", "分类ID", "关联categories.category_id", "integer", ""), ("unit_cost", "单位成本", "单件商品成本", "real", "人民币元"), ("list_price", "标价", "商品标准售价", "real", "人民币元"), ("launch_date", "上市日期", "商品上市日期", "date", "YYYY-MM-DD")],
        "customers": [("customer_id", "客户ID", "客户主键", "integer", ""), ("customer_name", "客户名称", "客户显示名称", "text", ""), ("region", "地区", "客户所属销售地区", "text", "华东、华南、华北、西南、华中"), ("register_date", "注册日期", "客户注册日期", "date", "YYYY-MM-DD"), ("customer_level", "客户等级", "客户会员或企业等级", "text", "普通、银卡、金卡、企业")],
        "orders": [("order_id", "订单ID", "订单主键", "integer", ""), ("customer_id", "客户ID", "关联customers.customer_id", "integer", ""), ("order_date", "下单日期", "订单创建日期", "date", "YYYY-MM-DD"), ("status", "订单状态", "订单当前状态", "text", "paid、shipped、completed为有效订单，cancelled为取消订单"), ("channel", "销售渠道", "订单来源渠道", "text", "网页商城、移动端、线下门店"), ("region", "销售地区", "订单所属地区", "text", "")],
        "order_items": [("order_item_id", "订单明细ID", "订单商品明细主键", "integer", ""), ("order_id", "订单ID", "关联orders.order_id", "integer", ""), ("product_id", "商品ID", "关联products.product_id", "integer", ""), ("quantity", "购买数量", "该订单明细购买件数", "integer", ""), ("unit_price", "成交单价", "折扣前单件成交价", "real", "人民币元"), ("discount_rate", "折扣率", "0到1之间的小数，例如0.1表示九折", "real", "")],
        "payments": [("payment_id", "支付ID", "支付记录主键", "integer", ""), ("order_id", "订单ID", "关联orders.order_id", "integer", ""), ("paid_amount", "支付金额", "订单实际支付金额", "real", "人民币元"), ("payment_method", "支付方式", "客户支付方式", "text", "支付宝、微信支付、银行卡、企业转账"), ("paid_at", "支付日期", "完成支付日期", "date", "YYYY-MM-DD")],
        "refunds": [("refund_id", "退款ID", "退款记录主键", "integer", ""), ("order_id", "订单ID", "关联orders.order_id", "integer", ""), ("refund_amount", "退款金额", "该次退款金额", "real", "人民币元"), ("refund_date", "退款日期", "发生退款的日期", "date", "YYYY-MM-DD"), ("refund_reason", "退款原因", "客户申请退款的原因", "text", "")],
    }
    root = database_dir / "database_description"
    root.mkdir(parents=True, exist_ok=True)
    header = ["original_column_name", "column_name", "column_description", "data_format", "value_description"]
    for table, rows in descriptions.items():
        with (root / f"{table}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)


def _build(path: Path) -> None:
    rng = random.Random(20261027)
    regions = ["华东", "华南", "华北", "西南", "华中"]
    channels = ["网页商城", "移动端", "线下门店"]
    categories = [
        (1, "智能设备"), (2, "办公用品"), (3, "家居生活"),
        (4, "运动户外"), (5, "食品饮料"),
    ]
    product_roots = {
        1: ["智能手表", "无线耳机", "平板电脑", "智能音箱"],
        2: ["人体工学椅", "机械键盘", "显示器", "移动硬盘"],
        3: ["空气净化器", "台灯", "咖啡机", "扫地机器人"],
        4: ["跑步鞋", "瑜伽垫", "登山包", "运动手环"],
        5: ["坚果礼盒", "精品咖啡", "健康麦片", "茶饮礼盒"],
    }
    with sqlite3.connect(path) as connection:
        connection.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE categories (
          category_id INTEGER PRIMARY KEY,
          category_name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE products (
          product_id INTEGER PRIMARY KEY,
          product_name TEXT NOT NULL,
          category_id INTEGER NOT NULL REFERENCES categories(category_id),
          unit_cost REAL NOT NULL,
          list_price REAL NOT NULL,
          launch_date TEXT NOT NULL
        );
        CREATE TABLE customers (
          customer_id INTEGER PRIMARY KEY,
          customer_name TEXT NOT NULL,
          region TEXT NOT NULL,
          register_date TEXT NOT NULL,
          customer_level TEXT NOT NULL
        );
        CREATE TABLE orders (
          order_id INTEGER PRIMARY KEY,
          customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
          order_date TEXT NOT NULL,
          status TEXT NOT NULL,
          channel TEXT NOT NULL,
          region TEXT NOT NULL
        );
        CREATE TABLE order_items (
          order_item_id INTEGER PRIMARY KEY,
          order_id INTEGER NOT NULL REFERENCES orders(order_id),
          product_id INTEGER NOT NULL REFERENCES products(product_id),
          quantity INTEGER NOT NULL,
          unit_price REAL NOT NULL,
          discount_rate REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE payments (
          payment_id INTEGER PRIMARY KEY,
          order_id INTEGER NOT NULL UNIQUE REFERENCES orders(order_id),
          paid_amount REAL NOT NULL,
          payment_method TEXT NOT NULL,
          paid_at TEXT NOT NULL
        );
        CREATE TABLE refunds (
          refund_id INTEGER PRIMARY KEY,
          order_id INTEGER NOT NULL REFERENCES orders(order_id),
          refund_amount REAL NOT NULL,
          refund_date TEXT NOT NULL,
          refund_reason TEXT NOT NULL
        );
        CREATE INDEX idx_orders_date ON orders(order_date);
        CREATE INDEX idx_orders_customer ON orders(customer_id);
        CREATE INDEX idx_items_order ON order_items(order_id);
        CREATE INDEX idx_items_product ON order_items(product_id);
        CREATE INDEX idx_refunds_order ON refunds(order_id);
        """)
        connection.executemany("INSERT INTO categories VALUES (?, ?)", categories)

        products = []
        product_id = 1
        for category_id, roots in product_roots.items():
            for root in roots:
                for suffix in ("标准版", "专业版", "旗舰版"):
                    cost = round(rng.uniform(30, 1200), 2)
                    price = round(cost * rng.uniform(1.35, 2.1), 2)
                    launch = date(2023, 1, 1) + timedelta(days=rng.randrange(900))
                    products.append((product_id, f"{root}{suffix}", category_id, cost, price, launch.isoformat()))
                    product_id += 1
        connection.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?)", products)

        customers = []
        levels = ["普通", "银卡", "金卡", "企业"]
        surnames = ["王", "李", "张", "刘", "陈", "杨", "赵", "黄", "周", "吴"]
        names = ["晨", "宇", "欣", "浩", "宁", "嘉", "涵", "博", "敏", "杰"]
        for customer_id in range(1, 241):
            registered = date(2023, 1, 1) + timedelta(days=rng.randrange(1050))
            customers.append((customer_id, surnames[customer_id % len(surnames)] + names[(customer_id * 3) % len(names)] + f"{customer_id:03d}",
                              rng.choice(regions), registered.isoformat(), rng.choices(levels, [55, 25, 15, 5])[0]))
        connection.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", customers)

        order_rows, item_rows, payment_rows, refund_rows = [], [], [], []
        item_id = payment_id = refund_id = 1
        start = date(2025, 1, 1)
        for order_id in range(1, 1001):
            customer = rng.choice(customers)
            order_date = start + timedelta(days=rng.randrange(730))
            status = rng.choices(["completed", "shipped", "paid", "cancelled"], [72, 10, 10, 8])[0]
            channel = rng.choices(channels, [45, 40, 15])[0]
            order_rows.append((order_id, customer[0], order_date.isoformat(), status, channel, customer[2]))
            total = 0.0
            for product in rng.sample(products, rng.randint(1, 4)):
                quantity = rng.randint(1, 4)
                discount = rng.choice([0, 0, 0.05, 0.1, 0.15, 0.2])
                unit_price = product[4]
                item_rows.append((item_id, order_id, product[0], quantity, unit_price, discount))
                item_id += 1
                total += quantity * unit_price * (1 - discount)
            if status != "cancelled":
                payment_rows.append((payment_id, order_id, round(total, 2), rng.choice(["支付宝", "微信支付", "银行卡", "企业转账"]),
                                     (order_date + timedelta(days=rng.randrange(3))).isoformat()))
                payment_id += 1
                if rng.random() < 0.11:
                    refund_rows.append((refund_id, order_id, round(total * rng.uniform(0.15, 1.0), 2),
                                        (order_date + timedelta(days=rng.randrange(3, 25))).isoformat(),
                                        rng.choice(["质量问题", "不符合预期", "重复下单", "物流问题"])))
                    refund_id += 1
        connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", order_rows)
        connection.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?, ?)", item_rows)
        connection.executemany("INSERT INTO payments VALUES (?, ?, ?, ?, ?)", payment_rows)
        connection.executemany("INSERT INTO refunds VALUES (?, ?, ?, ?, ?)", refund_rows)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()
