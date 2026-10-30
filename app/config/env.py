"""Optional project-local ``.env`` overrides for endpoints and credentials.

Precedence is deliberately one-way: **real environment variables > .env > TOML**.
The file is read while ``app.config`` is imported, so every later ``os.environ``
lookup (``CONFIG_PATH``, ``OPENAI_API_KEY``, ...) observes the same values, and a
value already exported by the shell is never overwritten.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_FILENAME = ".env"


def load_env_file(path: str | Path | None = None, *, override: bool = False) -> dict:
    """Load ``path`` into ``os.environ`` and describe the outcome.

    The returned summary carries the resolved path, whether the file exists, the
    names that were applied or skipped, and an error tag when parsing failed.
    Values are never echoed, so the summary is safe to log or expose in a health
    check.
    """
    resolved = Path(path) if path is not None else None
    summary: dict[str, object] = {
        "path": str(resolved) if resolved is not None else None,
        "present": False,
        "loaded": [],
        "skipped": [],
        "error": None,
    }
    if resolved is None or not resolved.is_file():
        return summary
    summary["present"] = True
    try:
        from dotenv import dotenv_values
    except ImportError:  # optional dependency: behave like a missing file
        summary["error"] = "python-dotenv is not installed"
        return summary
    try:
        values = dotenv_values(resolved, encoding="utf-8-sig")
    except Exception as exc:  # a broken .env must not stop the demo
        summary["error"] = type(exc).__name__
        return summary
    loaded: list[str] = summary["loaded"]  # type: ignore[assignment]
    skipped: list[str] = summary["skipped"]  # type: ignore[assignment]
    for key, value in values.items():
        if value is None:
            continue
        if not override and key in os.environ:
            skipped.append(key)
            continue
        os.environ[key] = value
        loaded.append(key)
    return summary
