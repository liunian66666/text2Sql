"""Chat model construction for the product-facing Agent layer.

The staged DeepEye-SQL pipeline intentionally keeps using ``app.llm.LLM``.
Only the conversational orchestration layer depends on LangChain's chat-model
interface, which keeps the boundary between the product and the research
engine explicit.
"""
from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI


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


__all__ = ["create_chat_model"]
