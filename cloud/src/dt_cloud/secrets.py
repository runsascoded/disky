"""Secrets from the environment, whitespace-stripped.

A secret lands in Secret Manager or a Pages secret via `echo "$X" | … --data-file=-`
often enough that a trailing newline is the normal failure mode — it broke the
Discord digest for two days in 2026-09 (`curl exit 3`: the webhook URL grew a
`\\n` before `/messages/<id>`). Batch injects secret versions into env
verbatim, so every read goes through here and strips. Ad-hoc `-t/--token`
values are stripped the same way (pasted with a newline as often as not).
"""
from __future__ import annotations

import os


def env_secret(name: str, default: str | None = None) -> str | None:
    """``$name`` with surrounding whitespace removed; ``default`` when unset."""
    v = os.environ.get(name)
    return v.strip() if v is not None else default


def secret(value: str | None, env_name: str) -> str | None:
    """An explicit ``value`` (stripped) if given, else ``env_secret(env_name)``."""
    return value.strip() if value else env_secret(env_name)
