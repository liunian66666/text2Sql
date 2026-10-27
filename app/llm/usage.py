"""Optional response-level usage ledger. Never records prompts or credentials."""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()
_item_id = ContextVar("sql_item_id", default=None)


class ThreadPoolExecutor(_ThreadPoolExecutor):
    """Propagate per-question attribution into nested pipeline worker pools."""
    def submit(self, fn, /, *args, **kwargs):
        return super().submit(copy_context().run, fn, *args, **kwargs)


def meter_item(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        item = next((arg for arg in (*args, *kwargs.values()) if hasattr(arg, "get_item_id")), None)
        token = _item_id.set(item.get_item_id() if item is not None else _item_id.get())
        try:
            return fn(*args, **kwargs)
        finally:
            _item_id.reset(token)
    return wrapped


def record_failure(*, model: str, elapsed_seconds: float, error: BaseException, kind: str = "chat") -> None:
    """Ledger line for an attempt that produced no response.

    Successful calls alone make wall time unaccountable: three 45s timeouts look
    like a mysterious slow stage in the report while the token ledger stays quiet.
    Records only the provider status code and error class -- no request body, no
    message text, no credentials.
    """
    destination = os.environ.get("SQL_USAGE_LEDGER")
    if not destination:
        return
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": os.environ.get("SQL_RUN_STAGE", "unspecified"),
        "item_id": _item_id.get(),
        "response_id": None,
        "kind": kind,
        "model": model,
        "elapsed_seconds": elapsed_seconds,
        "status": "failed",
        "usage_available": False,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "error_type": type(error).__name__,
        "status_code": getattr(error, "status_code", None),
    }
    path = Path(destination)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")


def record_usage(response, *, model: str, elapsed_seconds: float, kind: str = "chat") -> None:
    usage = response.usage

    # Feed the per-request budget meter (no-op when no meter is active).
    # Done before the ledger short-circuit so budgets hold even unmeasured runs.
    from .budget import note_call
    note_call(
        getattr(usage, "prompt_tokens", 0) if usage else 0,
        getattr(usage, "completion_tokens", 0) if usage else 0,
    )

    destination = os.environ.get("SQL_USAGE_LEDGER")
    if not destination:
        return
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": os.environ.get("SQL_RUN_STAGE", "unspecified"),
        "item_id": _item_id.get(),
        "response_id": getattr(response, "id", None),
        "kind": kind,
        "model": model,
        "elapsed_seconds": elapsed_seconds,
        "status": "success",
        "usage_available": usage is not None,
        "prompt_tokens": usage.prompt_tokens if usage else None,
        "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else None,
        "total_tokens": usage.total_tokens if usage else None,
    }
    path = Path(destination)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
