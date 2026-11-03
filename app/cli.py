"""``enterprise-agent`` 命令行入口。

    enterprise-agent                  # 等价于 serve
    enterprise-agent serve            # 启动 Web 应用
    enterprise-agent check            # 启动前自检
    enterprise-agent ask --question …  # 单问题跑一次 Agent
    enterprise-agent mcp              # 启动 MCP Server
    enterprise-agent evaluate         # Agent 专项评测
    enterprise-agent evaluate-rag     # 业务口径 RAG 评测
    enterprise-agent generate-data    # 生成合成企业库
    enterprise-agent build-index      # 构建值向量索引

子命令实现在 ``app/commands/``，每个模块只暴露 ``add_arguments`` 与 ``run``，
这个文件只负责把名字接到函数上，不放任何业务逻辑。
"""
from __future__ import annotations

import argparse
import sys

from app.commands import (
    ask,
    build_index,
    check,
    evaluate,
    evaluate_rag,
    generate_data,
    mcp,
    serve,
)


COMMANDS = {
    "serve": (serve, "启动 Web 应用（默认命令）"),
    "check": (check, "启动前自检：数据库、索引、离线报告、.env"),
    "ask": (ask, "单问题跑一次 Agent，输出 JSON"),
    "mcp": (mcp, "启动 MCP Server（stdio / sse / streamable-http）"),
    "evaluate": (evaluate, "Agent 专项端到端评测"),
    "evaluate-rag": (evaluate_rag, "业务口径关键词 vs 混合检索对照"),
    "generate-data": (generate_data, "生成销售与 HR 合成企业库"),
    "build-index": (build_index, "增量构建值向量索引"),
}


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("enterprise-data-analyst-agent")
    except Exception:  # 未安装（直接跑源码）时不要因此报错
        return "0.1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enterprise-agent",
        description="企业数据分析 Agent：LLM 规划 + RAG 口径 + 只读 Text-to-SQL + 可视化。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="不带子命令时等价于 `enterprise-agent serve`。",
    )
    parser.add_argument("--version", action="version", version=f"enterprise-agent {_version()}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for name, (module, help_text) in COMMANDS.items():
        child = subparsers.add_parser(name, help=help_text, description=help_text)
        module.add_arguments(child)
        child.set_defaults(handler=module.run)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 顶层只认 --help / --version；其余情况（无参数、或直接给 serve 的参数）
    # 都按 serve 处理，这样 `enterprise-agent --port 8010` 与 `... serve --port 8010` 等价。
    top_level = {"-h", "--help", "--version"}
    if not argv or (argv[0].startswith("-") and argv[0] not in top_level):
        argv = ["serve", *argv]
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:  # 只有顶层 --help/--version 会走到这里
        parser.print_help()
        return 0
    try:
        return int(handler(args) or 0)
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
