"""CLI subcommands behind the ``enterprise-agent`` console script.

Each module exposes ``add_arguments(parser)`` and ``run(args) -> int`` so that
``app/cli.py`` only wires names to functions and every command stays testable
without going through argparse.

``build-knowledge`` (Phase 1) is fully offline: it reads
``app/enterprise/business_metrics.json`` plus SQLite PRAGMA metadata and writes
deterministic JSON artifacts into ``workspace/knowledge/``.
"""

COMMAND_MODULES = (
    "ask",
    "build_index",
    "build_knowledge",
    "check",
    "evaluate",
    "evaluate_graphrag",
    "evaluate_join_paths",
    "evaluate_rag",
    "final_eval",
    "generate_data",
    "mcp",
    "phase4b_smoke",
    "serve",
)

__all__ = ["COMMAND_MODULES"]
