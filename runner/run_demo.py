"""演示站启动入口：解析参数 → 装配 app → 启动 uvicorn。

装配逻辑见 app/demo/server.py（build_app）。
等价的 uvicorn 启动方式（无法传 --config，用默认或环境变量 CONFIG_PATH）：

    uv run --no-sync uvicorn app.demo.server:create_app_from_env --factory --port 8000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_HOST = "127.0.0.1"  # 仅本机；局域网投屏时显式 --host 0.0.0.0
DEFAULT_PORT = 8000


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=None,
        help="Pipeline TOML with prepared indexes (default: demo default)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Check databases, indexes and offline reports, then exit without starting the server",
    )
    args = parser.parse_args()

    if args.check_only:
        from app.demo.preflight import preflight_report

        report = preflight_report(args.config)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(0 if report["ready"] else 1)

    import uvicorn

    from app.demo.server import build_app

    uvicorn.run(build_app(args.config), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
