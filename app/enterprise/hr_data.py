"""Create the deterministic HR demo database used by the second business domain."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from contextlib import closing
import calendar
import csv
import random
import sqlite3


SCHEMA_VERSION = 2
AS_OF_DATE = date(2026, 10, 28)


def ensure_enterprise_hr_database(project_root: Path, *, force: bool = False) -> Path:
    """Build a reproducible, synthetic-only HR SQLite database."""
    destination = project_root / "data" / "enterprise_hr" / "enterprise_hr.sqlite"
    if destination.exists() and not force:
        try:
            with closing(sqlite3.connect(destination)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                employee_count = connection.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
                attendance_count = connection.execute("SELECT COUNT(*) FROM attendance").fetchone()[0]
            if version == SCHEMA_VERSION and employee_count == 180 and attendance_count >= 700:
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


def _month_end(value: date) -> date:
    return value.replace(day=calendar.monthrange(value.year, value.month)[1])


def _write_descriptions(database_dir: Path) -> None:
    descriptions = {
        "departments": [
            ("department_id", "部门ID", "部门主键", "integer", ""),
            ("department_name", "部门名称", "公司组织部门名称", "text", ""),
            ("cost_center", "成本中心", "部门成本中心编码", "text", ""),
        ],
        "employees": [
            ("employee_id", "员工ID", "员工主键，仅为模拟数据", "integer", ""),
            ("employee_name", "员工姓名", "脱敏模拟姓名", "text", ""),
            ("department_id", "部门ID", "关联departments.department_id", "integer", ""),
            ("job_level", "职级", "员工当前职级", "text", "P1至P5"),
            ("hire_date", "入职日期", "员工正式入职日期", "date", "YYYY-MM-DD"),
            ("termination_date", "离职日期", "为空表示截至数据日期仍在职", "date", "YYYY-MM-DD或NULL"),
        ],
        "attendance": [
            ("attendance_id", "出勤记录ID", "月度出勤记录主键", "integer", ""),
            ("employee_id", "员工ID", "关联employees.employee_id", "integer", ""),
            ("attendance_month", "出勤月份", "月度统计周期首日", "date", "YYYY-MM-01"),
            ("scheduled_days", "应出勤天数", "当月计划工作日；使用REAL保证SQLite比率计算不是整数除法", "real", "天"),
            ("attended_days", "实际出勤天数", "当月实际到岗天数；使用REAL保证SQLite比率计算不是整数除法", "real", "天"),
            ("late_days", "迟到天数", "当月发生迟到的工作日数", "integer", "天"),
            ("leave_days", "请假天数", "当月已批准请假天数", "integer", "天"),
        ],
        "performance_reviews": [
            ("review_id", "绩效记录ID", "绩效评审主键", "integer", ""),
            ("employee_id", "员工ID", "关联employees.employee_id", "integer", ""),
            ("review_period", "绩效周期", "半年绩效周期", "text", "2025-H2、2026-H1"),
            ("review_date", "评审日期", "绩效确认日期", "date", "YYYY-MM-DD"),
            ("performance_score", "绩效得分", "1至5分的绩效得分", "real", "1.0至5.0"),
            ("performance_grade", "绩效等级", "由绩效得分映射的等级", "text", "A、B、C、D"),
        ],
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
    rng = random.Random(20261029)
    departments = [
        (1, "研发部", "CC-RD"), (2, "销售部", "CC-SALES"),
        (3, "市场部", "CC-MKT"), (4, "客户成功部", "CC-CS"),
        (5, "财务部", "CC-FIN"), (6, "人力资源部", "CC-HR"),
    ]
    department_weights = [32, 25, 12, 14, 9, 8]
    surnames = ["王", "李", "张", "刘", "陈", "杨", "赵", "黄", "周", "吴"]
    given_names = ["晨", "宇", "欣", "浩", "宁", "嘉", "涵", "博", "敏", "杰", "悦", "远"]

    with closing(sqlite3.connect(path)) as connection:
        connection.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE departments (
          department_id INTEGER PRIMARY KEY,
          department_name TEXT NOT NULL UNIQUE,
          cost_center TEXT NOT NULL UNIQUE
        );
        CREATE TABLE employees (
          employee_id INTEGER PRIMARY KEY,
          employee_name TEXT NOT NULL,
          department_id INTEGER NOT NULL REFERENCES departments(department_id),
          job_level TEXT NOT NULL,
          hire_date TEXT NOT NULL,
          termination_date TEXT
        );
        CREATE TABLE attendance (
          attendance_id INTEGER PRIMARY KEY,
          employee_id INTEGER NOT NULL REFERENCES employees(employee_id),
          attendance_month TEXT NOT NULL,
          scheduled_days REAL NOT NULL CHECK(scheduled_days > 0),
          attended_days REAL NOT NULL CHECK(attended_days >= 0),
          late_days INTEGER NOT NULL CHECK(late_days >= 0),
          leave_days INTEGER NOT NULL CHECK(leave_days >= 0),
          UNIQUE(employee_id, attendance_month)
        );
        CREATE TABLE performance_reviews (
          review_id INTEGER PRIMARY KEY,
          employee_id INTEGER NOT NULL REFERENCES employees(employee_id),
          review_period TEXT NOT NULL,
          review_date TEXT NOT NULL,
          performance_score REAL NOT NULL CHECK(performance_score BETWEEN 1 AND 5),
          performance_grade TEXT NOT NULL,
          UNIQUE(employee_id, review_period)
        );
        CREATE INDEX idx_employees_department ON employees(department_id);
        CREATE INDEX idx_employees_hire_date ON employees(hire_date);
        CREATE INDEX idx_employees_termination_date ON employees(termination_date);
        CREATE INDEX idx_attendance_month ON attendance(attendance_month);
        CREATE INDEX idx_reviews_period ON performance_reviews(review_period);
        """)
        connection.executemany("INSERT INTO departments VALUES (?, ?, ?)", departments)

        employees = []
        hire_start = date(2023, 1, 1)
        for employee_id in range(1, 181):
            department_id = rng.choices(range(1, 7), weights=department_weights)[0]
            hire_date = hire_start + timedelta(days=rng.randrange((AS_OF_DATE - hire_start).days - 13))
            termination_date = None
            if hire_date <= AS_OF_DATE - timedelta(days=180) and rng.random() < 0.18:
                earliest = hire_date + timedelta(days=120)
                termination_date = earliest + timedelta(days=rng.randrange((AS_OF_DATE - earliest).days + 1))
            name = surnames[employee_id % len(surnames)] + given_names[(employee_id * 5) % len(given_names)] + f"{employee_id:03d}"
            level = rng.choices(["P1", "P2", "P3", "P4", "P5"], [20, 32, 28, 15, 5])[0]
            employees.append((employee_id, name, department_id, level, hire_date.isoformat(),
                              termination_date.isoformat() if termination_date else None))
        connection.executemany("INSERT INTO employees VALUES (?, ?, ?, ?, ?, ?)", employees)

        attendance_rows = []
        attendance_id = 1
        months = [date(2026, month, 1) for month in range(5, 11)]
        for employee in employees:
            hire_date = date.fromisoformat(employee[4])
            termination_date = date.fromisoformat(employee[5]) if employee[5] else None
            for month in months:
                if hire_date > _month_end(month) or (termination_date and termination_date < month):
                    continue
                scheduled = rng.choice([20, 21, 22, 23])
                leave = rng.choices([0, 1, 2, 3, 4], [55, 24, 13, 6, 2])[0]
                absence = rng.choices([0, 1, 2], [84, 13, 3])[0]
                attended = max(0, scheduled - leave - absence)
                late = min(attended, rng.choices([0, 1, 2, 3], [70, 20, 8, 2])[0])
                attendance_rows.append((attendance_id, employee[0], month.isoformat(), scheduled,
                                        attended, late, leave))
                attendance_id += 1
        connection.executemany("INSERT INTO attendance VALUES (?, ?, ?, ?, ?, ?, ?)", attendance_rows)

        review_rows = []
        review_id = 1
        review_periods = [("2025-H2", date(2025, 12, 31)), ("2026-H1", date(2026, 6, 30))]
        department_bias = {1: 0.18, 2: 0.08, 3: 0.02, 4: 0.12, 5: -0.02, 6: 0.05}
        for employee in employees:
            hire_date = date.fromisoformat(employee[4])
            termination_date = date.fromisoformat(employee[5]) if employee[5] else None
            for period, review_date in review_periods:
                if hire_date > review_date - timedelta(days=90) or (termination_date and termination_date < review_date):
                    continue
                score = round(min(5.0, max(1.0, rng.gauss(3.55 + department_bias[employee[2]], 0.55))), 1)
                grade = "A" if score >= 4.3 else "B" if score >= 3.5 else "C" if score >= 2.5 else "D"
                review_rows.append((review_id, employee[0], period, review_date.isoformat(), score, grade))
                review_id += 1
        connection.executemany("INSERT INTO performance_reviews VALUES (?, ?, ?, ?, ?, ?)", review_rows)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()
