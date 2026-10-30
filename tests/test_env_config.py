import json
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from app.config.env import load_env_file


class EnvFileTest(unittest.TestCase):
    def test_missing_file_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = load_env_file(Path(tmp) / ".env")
        self.assertFalse(summary["present"])
        self.assertEqual(summary["loaded"], [])
        self.assertIsNone(summary["error"])

    def test_dotenv_values_apply_but_shell_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(
                "# 注释行\n"
                "OPENAI_API_KEY=sk-from-dotenv\n"
                'DEFAULT_LLM_MODEL="demo-model"\n',
                encoding="utf-8")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-from-shell"}, clear=False):
                # 仓库根目录的开发者 .env 可能已经注入同名变量，先清掉保证隔离
                os.environ.pop("DEFAULT_LLM_MODEL", None)
                summary = load_env_file(env)
                self.assertTrue(summary["present"])
                # 已导出的真实环境变量不被覆盖，缺失的才补齐
                self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-from-shell")
                self.assertEqual(os.environ["DEFAULT_LLM_MODEL"], "demo-model")
                self.assertEqual(summary["skipped"], ["OPENAI_API_KEY"])
                self.assertEqual(summary["loaded"], ["DEFAULT_LLM_MODEL"])

    def test_override_flag_lets_dotenv_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("OPENAI_API_KEY=sk-from-dotenv\n", encoding="utf-8")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-from-shell"}, clear=False):
                summary = load_env_file(env, override=True)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-from-dotenv")
                self.assertEqual(summary["loaded"], ["OPENAI_API_KEY"])

    def test_summary_never_echoes_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("OPENAI_API_KEY=sk-super-secret-value\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OPENAI_API_KEY", None)
                summary = load_env_file(env)
        payload = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("sk-super-secret-value", payload)
        self.assertIn("OPENAI_API_KEY", payload)

    def test_broken_file_reports_error_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_bytes(b"OPENAI_API_KEY=\xff\xfe\x00bad\n")
            summary = load_env_file(env)
        self.assertTrue(summary["present"])
        self.assertIsNotNone(summary["error"])


class EnvOverTomlTest(unittest.TestCase):
    TOML = textwrap.dedent("""
        [run]
        default_llm_profile = "p"

        [llm_profiles.p]
        model = "deepseek-v4.1-flash-1017"
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        api_key = "from-toml"
        extra_body = { enable_thinking = false }
        """)

    def _load(self, overrides):
        from app.config.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            toml = Path(tmp) / "config.toml"
            toml.write_text(self.TOML, encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "DEFAULT_LLM_MODEL"):
                    os.environ.pop(key, None)
                os.environ.update(overrides)
                return Config._load_config(toml)["llm_profiles"]["p"]

    def test_exported_key_overrides_toml_profile(self):
        profile = self._load({"OPENAI_API_KEY": "sk-from-env"})
        self.assertEqual(profile["api_key"], "sk-from-env")

    def test_model_swap_on_same_endpoint_keeps_declared_extra_body(self):
        # 换模型名但端点不变时，enable_thinking=false 必须保留，
        # 否则每次调用都会退回强制思考（实测 150 秒/次，整条流水线超时）。
        profile = self._load({"DEFAULT_LLM_MODEL": "qwen3.8-max-0902"})
        self.assertEqual(profile["model"], "qwen3.8-max-0902")
        self.assertEqual(profile["extra_body"], {"enable_thinking": False})

    def test_provider_switch_drops_provider_specific_extra_body(self):
        profile = self._load({"DEFAULT_LLM_MODEL": "qwen3.8-max-0902",
                              "OPENAI_BASE_URL": "https://self-hosted.example/v1"})
        self.assertEqual(profile["base_url"], "https://self-hosted.example/v1")
        self.assertEqual(profile["extra_body"], {})


class EnvSummaryExposureTest(unittest.TestCase):
    def test_config_module_exposes_summary_without_secrets(self):
        from app.config import ENV_FILE

        self.assertIn("present", ENV_FILE)
        self.assertIn("loaded", ENV_FILE)
        self.assertNotIn("sk-", json.dumps(ENV_FILE, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
