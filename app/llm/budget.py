"""Per-request LLM budget guard: hard caps on tokens and wall-clock seconds.

A runaway question (e.g. an over-scope query that makes the reasoning model
burn max_tokens on every retry) can otherwise loop through the nested
tenacity x extractor retry layers for hours. This module keeps a lightweight
per-request meter in a ContextVar so it propagates into pipeline thread
pools (they all come from app.llm.usage.ThreadPoolExecutor, which copies the
context) without extra plumbing.

Stdlib-only: app.llm.usage imports it, so importing it must stay cheap.
"""
from __future__ import annotations

import threading
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


class BudgetExceededError(RuntimeError):
    """Raised before a new LLM attempt when the request's token/time cap is hit."""


class TruncatedResponseError(BudgetExceededError):
    """Every choice in one response hit max_tokens without producing final content.

    Distinct from a real budget exhaustion: this is a per-call property, so soft
    callers (e.g. keyword extraction, which has a heuristic fallback) can degrade
    instead of aborting the whole request. Subclasses BudgetExceededError so the
    existing "never retry this" checks keep matching.
    """


@dataclass
class BudgetMeter:
    max_total_tokens: Optional[int] = None
    max_seconds: Optional[int] = None
    started_at: float = field(default_factory=time.monotonic)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "calls": self.calls,
                "elapsed_seconds": round(self.elapsed_seconds, 1),
                "budget_tokens": self.max_total_tokens,
                "budget_seconds": self.max_seconds,
            }

    def add_call(self, prompt_tokens: int, completion_tokens: int) -> None:
        with self._lock:
            self.prompt_tokens += int(prompt_tokens or 0)
            self.completion_tokens += int(completion_tokens or 0)
            self.calls += 1

    def check(self) -> None:
        with self._lock:
            total = self.prompt_tokens + self.completion_tokens
            calls = self.calls
            elapsed = self.elapsed_seconds
        if self.max_total_tokens and total >= self.max_total_tokens:
            raise BudgetExceededError(
                f"LLM token budget exceeded ({total}/{self.max_total_tokens}) after {calls} calls; "
                "the question is likely beyond the database scope or too hard for the current budget"
            )
        if self.max_seconds and elapsed >= self.max_seconds:
            raise BudgetExceededError(
                f"LLM time budget exceeded ({elapsed:.0f}s/{self.max_seconds}s) after {calls} calls"
            )


_meter: ContextVar[Optional[BudgetMeter]] = ContextVar("llm_budget_meter", default=None)
_hook: ContextVar[Optional[Callable[[dict], None]]] = ContextVar("llm_budget_hook", default=None)


def start_meter(max_total_tokens: Optional[int], max_seconds: Optional[int]) -> Token:
    return _meter.set(BudgetMeter(max_total_tokens=max_total_tokens, max_seconds=max_seconds))


def get_meter() -> Optional[BudgetMeter]:
    return _meter.get()


def reset_meter(token: Token) -> None:
    _meter.reset(token)


def set_hook(callback: Optional[Callable[[dict], None]]) -> Token:
    return _hook.set(callback)


def reset_hook(token: Token) -> None:
    _hook.reset(token)


def note_call(prompt_tokens: int, completion_tokens: int) -> None:
    """Called from record_usage after every successful LLM/embedding response."""
    meter = _meter.get()
    if meter is None:
        return
    meter.add_call(prompt_tokens, completion_tokens)
    hook = _hook.get()
    if hook is not None:
        try:
            hook(meter.snapshot())
        except Exception:
            # Progress reporting must never break the pipeline.
            pass
