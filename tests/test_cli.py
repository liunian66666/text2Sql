"""CLI 契约：子命令齐全、无子命令等于 serve、配置解析带回退。"""
from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from app import cli
from app.commands import common


class ParserTest(unittest.TestCase):
    def test_every_documented_command_is_registered(self):
        parser = cli.build_parser()
        actions = [action for action in parser._actions if action.dest == "command"]
        self.assertEqual(len(actions), 1)
        names = set(actions[0].choices)
        self.assertEqual(names, set(cli.COMMANDS))
        self.assertEqual(
            names,
            {"serve", "check", "ask", "mcp", "evaluate", "evaluate-rag",
             "generate-data", "build-index"},
        )

    def test_subcommands_accept_their_own_flags(self):
        parser = cli.build_parser()
        args = parser.parse_args(["serve", "--port", "8010", "--reload"])
        self.assertEqual((args.command, args.port, args.reload), ("serve", 8010, True))
        args = parser.parse_args(["ask", "--question", "在职人数"])
        self.assertEqual((args.command, args.question), ("ask", "在职人数"))

    def test_missing_question_is_a_usage_error(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit) as raised:
            parser.parse_args(["ask"])
        self.assertEqual(raised.exception.code, 2)


class DispatchTest(unittest.TestCase):
    def test_no_subcommand_dispatches_to_serve(self):
        calls = []

        def fake_run(args):
            calls.append(args)
            return 0

        with patch.object(cli.serve, "run", fake_run):
            self.assertEqual(cli.main(["--port", "8010"]), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].command, "serve")
        self.assertEqual(calls[0].port, 8010)

    def test_handler_exit_code_is_propagated(self):
        with patch.object(cli.check, "run", lambda args: 1):
            self.assertEqual(cli.main(["check"]), 1)

    def test_missing_config_is_reported_without_traceback(self):
        with patch.object(cli.serve, "run", side_effect=FileNotFoundError("配置文件不存在：x.toml")):
            self.assertEqual(cli.main(["serve"]), 2)


class ResolveConfigTest(unittest.TestCase):
    def test_explicit_path_wins(self):
        with patch.dict("os.environ", {}, clear=True):
            path = common.resolve_config("config/template/app-fast/config.toml")
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "config.toml")

    def test_env_var_is_used_when_no_flag(self):
        with patch.dict("os.environ",
                        {"CONFIG_PATH": "config/template/app-fast/config.toml"}):
            path = common.resolve_config(None)
        self.assertTrue(path.is_file())

    def test_missing_file_is_an_explicit_error(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(FileNotFoundError):
                common.resolve_config("config/local/does-not-exist.toml")

    def test_default_candidates_include_both_migration_paths(self):
        names = [item.as_posix() for item in common.CONFIG_CANDIDATES]
        self.assertIn("config/local/app-fast/config.toml", names)
        self.assertIn("config/local/app-fast/config.toml", names)


if __name__ == "__main__":
    unittest.main()
