"""CLI subcommands behind the ``enterprise-agent`` console script.

Each module exposes ``add_arguments(parser)`` and ``run(args) -> int`` so that
``app/cli.py`` only wires names to functions and every command stays testable
without going through argparse.
"""
