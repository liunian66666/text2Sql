"""Chat model construction for the product-facing Agent layer.

The staged DeepEye-SQL pipeline intentionally keeps using ``app.llm.LLM``.
Only the conversational orchestration layer depends on LangChain's chat-model
interface, which keeps the boundary between the product and the research
engine explicit.
"""
from __future__ import annotations

import os
from typing import Any

from langchain_openai import ChatOpenAI

from app.llm.budget import get_meter, note_call

DEFAULT_MAX_TOKENS = 300_000
DEFAULT_MAX_CALLS = 24
DEFAULT_MAX_SECONDS = 900


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def agent_budget_limits(overrides: dict[str, Any] | None = None) -> dict[str, int]:
    """Resolve the Agent-layer LLM budget (explicit override > env > default)."""
    overrides = overrides or {}
    return {
        "max_total_tokens": int(overrides.get("max_total_tokens")
                                or _env_int("AGENT_MAX_TOKENS", DEFAULT_MAX_TOKENS)),
        "max_calls": int(overrides.get("max_calls")
                         or _env_int("AGENT_MAX_CALLS", DEFAULT_MAX_CALLS)),
        "max_seconds": int(overrides.get("max_seconds")
                           or _env_int("AGENT_MAX_SECONDS", DEFAULT_MAX_SECONDS)),
    }


class MeteredChatModel:
    """LangChain-compatible wrapper that charges every call to the BudgetMeter.

    Why this exists: the orchestration layer used a naked ``ChatOpenAI``, so
    ``BudgetMeter`` only guarded the SQL pipeline. Planner turns, assistant
    turns and the tool-limit summary were free — on the Web path there was no
    hard token/call cap at all. The wrapper checks the meter *before* the HTTP
    request (the same point the pipeline uses, so retries cannot bypass it) and
    books usage afterwards.
    """

    def __init__(self, inner):
        self._inner = inner
        self._bound = inner

    def bind_tools(self, tools, **kwargs):
        clone = self.__class__.__new__(self.__class__)
        clone._inner = self._inner
        clone._bound = self._inner.bind_tools(tools, **kwargs)
        return clone

    def invoke(self, *args, **kwargs):
        meter = get_meter()
        if meter is not None:
            meter.check()
        response = self._bound.invoke(*args, **kwargs)
        if meter is not None:
            usage = getattr(response, "usage_metadata", None) or {}
            note_call(int(usage.get("input_tokens") or 0),
                      int(usage.get("output_tokens") or 0))
        return response

    def __getattr__(self, item):
        # Anything the graph may reach for (astream, with_retry, …) falls back to
        # the underlying runnable instead of failing on the wrapper.
        return getattr(self._inner, item)


def create_chat_model(config, *, timeout_seconds: int = 90) -> ChatOpenAI:
    """Create the OpenAI-compatible model used by the LangGraph Agent."""

    kwargs: dict[str, Any] = {
        "model": config.model,
        "api_key": config.api_key,
        "base_url": config.base_url,
        "temperature": 0,
        "timeout": timeout_seconds,
        "max_retries": 1,
        "max_tokens": min(config.max_tokens, 4096),
    }
    if config.reasoning_effort is not None:
        kwargs["reasoning_effort"] = config.reasoning_effort
    if config.extra_body:
        kwargs["extra_body"] = dict(config.extra_body)
    return ChatOpenAI(**kwargs)


__all__ = ["create_chat_model", "MeteredChatModel", "agent_budget_limits"]
