"""LLM 层对「模型不支持 temperature 参数」的自动适配单测。

背景：dashscope 托管的 kimi-k3 系列对 temperature 返回
400 ``Parameter 'temperature'=... is not supported``。``LLM.ask`` 必须
自动去掉 temperature 重试，而不是让整个流水线失败。纯离线 mock，零网络。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from openai import BadRequestError
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.completion_usage import CompletionUsage

from app.config.config import LLMConfig
from app.llm.llm import LLM


def _ok_response(content: str = "OK") -> ChatCompletion:
    return ChatCompletion(
        id="fake",
        object="chat.completion",
        created=0,
        model="kimi-k3.1-pro",
        choices=[Choice(index=0, finish_reason="stop",
                        message=ChatCompletionMessage(role="assistant", content=content))],
        usage=CompletionUsage(prompt_tokens=5, completion_tokens=1, total_tokens=6),
    )


def _temperature_rejected() -> BadRequestError:
    request = httpx.Request("POST", "https://fake/v1/chat/completions")
    response = httpx.Response(400, request=request)
    return BadRequestError(
        "Error code: 400 - Parameter 'temperature'=0.7 is not supported for kimi-k3 model.",
        response=response, body=None,
    )


class TemperatureFallbackTests(unittest.TestCase):
    def _llm(self):
        return LLM(LLMConfig(model="kimi-k3.1-pro", base_url="https://fake/v1",
                             api_key="test", api_type="openai", temperature=0.7))

    def test_retries_without_temperature_and_succeeds(self):
        calls: list[dict] = []

        def fake_create(**kwargs):
            calls.append(kwargs)
            if "temperature" in kwargs:
                raise _temperature_rejected()
            return _ok_response()

        client = type("C", (), {"chat": type("Chat", (), {"completions": type(
            "Completions", (), {"create": staticmethod(fake_create)})()})()})()
        llm = self._llm()
        with patch.object(LLM, "_get_client", return_value=client):
            choices, usage = llm.ask([{"role": "user", "content": "hi"}])
        self.assertEqual(choices[0].content, "OK")
        self.assertEqual(len(calls), 2)
        self.assertIn("temperature", calls[0])
        self.assertNotIn("temperature", calls[1])

    def test_no_temperature_error_keeps_single_call(self):
        calls: list[dict] = []

        def fake_create(**kwargs):
            calls.append(kwargs)
            return _ok_response()

        client = type("C", (), {"chat": type("Chat", (), {"completions": type(
            "Completions", (), {"create": staticmethod(fake_create)})()})()})()
        llm = LLM(LLMConfig(model="qwen3.8-max", base_url="https://fake/v1",
                            api_key="test", api_type="openai", temperature=0.7))
        with patch.object(LLM, "_get_client", return_value=client):
            llm.ask([{"role": "user", "content": "hi"}])
        self.assertEqual(len(calls), 1)
        self.assertIn("temperature", calls[0])


if __name__ == "__main__":
    unittest.main()
