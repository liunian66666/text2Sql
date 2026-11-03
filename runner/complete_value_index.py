"""兼容包装：逻辑已迁到 ``app.commands.build_index``。

    uv run enterprise-agent build-index --database enterprise_hr
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cli import main as cli_main  # noqa: E402


def main() -> int:
    print("Deprecated: 请改用 `uv run enterprise-agent build-index`", file=sys.stderr)
    return cli_main(["build-index", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
