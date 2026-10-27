"""OpenAI-compatible tool-calling brain used by the LangGraph agent."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import time
from typing import Any, Protocol
from uuid import uuid4

from openai import OpenAI, OpenAIError

from app.llm.usage import record_usage


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentDecision:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


class AgentBrain(Protocol):
    def decide(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
               system_prompt: str) -> AgentDecision: ...


class OpenAIToolCallingBrain:
    """Thin, testable adapter around an OpenAI-compatible chat endpoint."""

    def __init__(self, config, *, timeout_seconds: int = 90):
        self.config = config
        self.timeout_seconds = timeout_seconds
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url, max_retries=0)
        self._native_tools_supported: bool | None = None

    def decide(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
               system_prompt: str) -> AgentDecision:
        if self._native_tools_supported is not False:
            try:
                decision = self._native_decide(messages, tools, system_prompt)
                self._native_tools_supported = True
                return decision
            except OpenAIError:
                # Several OpenAI-compatible Qwen/vLLM endpoints implement chat
                # completions but reject the native `tools` field. Keep the LLM
                # as planner by switching to an explicit JSON ReAct protocol.
                self._native_tools_supported = False
        return self._json_react_decide(messages, tools, system_prompt)

    def _native_decide(self, messages, tools, system_prompt) -> AgentDecision:
        params: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0,
            "max_tokens": min(self.config.max_tokens, 4096),
            "timeout": self.timeout_seconds,
        }
        if self.config.reasoning_effort is not None:
            params["reasoning_effort"] = self.config.reasoning_effort
        if self.config.extra_body:
            params["extra_body"] = self.config.extra_body
        started = time.perf_counter()
        response = self.client.chat.completions.create(**params)
        record_usage(response, model=self.config.model, elapsed_seconds=time.perf_counter() - started)
        if not response.choices:
            raise RuntimeError("Agent model returned no choices")
        message = response.choices[0].message
        calls = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid tool arguments for {call.function.name}") from exc
            calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))
        usage = response.usage.model_dump() if response.usage else {}
        return AgentDecision(content=message.content or "", tool_calls=calls, usage=usage)

    def _json_react_decide(self, messages, tools, system_prompt) -> AgentDecision:
        catalog = [{"name": item["function"]["name"], "description": item["function"]["description"],
                    "parameters": item["function"]["parameters"]} for item in tools]
        protocol = f"""

当前模型端点不支持原生 function calling。你仍然必须按照 ReAct 工具协议决策。
可用工具：{json.dumps(catalog, ensure_ascii=False)}

只输出一个 JSON 对象，不要输出 Markdown：
- 调用工具：{{"tool_calls":[{{"name":"工具名","arguments":{{...}}}}],"answer":""}}
- 直接回答：{{"tool_calls":[],"answer":"最终中文回答"}}
可以在一轮调用多个互不依赖的工具。不得编造工具结果。
"""
        compatible_messages = [{"role": "system", "content": system_prompt + protocol}]
        for message in messages:
            role = message.get("role")
            if role == "tool":
                compatible_messages.append({"role": "user", "content":
                    f"工具 {message.get('name')} 返回：{message.get('content', '')}"})
            elif role == "assistant" and message.get("tool_calls"):
                compatible_messages.append({"role": "assistant", "content":
                    f"已请求工具：{json.dumps(message['tool_calls'], ensure_ascii=False)}"})
            else:
                compatible_messages.append({"role": role, "content": message.get("content", "")})
        params: dict[str, Any] = {
            "model": self.config.model, "messages": compatible_messages,
            "temperature": 0, "max_tokens": min(self.config.max_tokens, 4096),
            "timeout": self.timeout_seconds,
        }
        if self.config.extra_body:
            params["extra_body"] = self.config.extra_body
        started = time.perf_counter()
        response = self.client.chat.completions.create(**params)
        record_usage(response, model=self.config.model, elapsed_seconds=time.perf_counter() - started)
        if not response.choices:
            raise RuntimeError("Agent model returned no choices")
        content = response.choices[0].message.content or ""
        payload = self._parse_json_object(content)
        calls = [ToolCall(id=f"json_{uuid4().hex[:12]}", name=item["name"],
                          arguments=item.get("arguments") or {}) for item in payload.get("tool_calls", [])]
        usage = response.usage.model_dump() if response.usage else {}
        return AgentDecision(content=payload.get("answer", ""), tool_calls=calls, usage=usage)

    @staticmethod
    def _parse_json_object(content: str) -> dict[str, Any]:
        cleaned = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.IGNORECASE).strip()
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if not match:
                # A natural-language response is a valid direct answer.
                return {"tool_calls": [], "answer": content.strip()}
            payload = json.loads(match.group(0))
        if not isinstance(payload, dict):
            raise RuntimeError("Agent JSON response must be an object")
        return payload
