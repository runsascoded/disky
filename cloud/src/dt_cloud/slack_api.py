"""Slack Web API calls thrds's `SlackClient` doesn't wrap, made through its `_request` (its transport retries
included): joining a channel and pinning / unpinning a message, each idempotent. Shared by the scan-health thread
(`nominal`) and the usage digest's month thread (`digest`)."""
from __future__ import annotations

from typing import Any

ERROR_PREFIX = "Slack API error: "


def error_code(e: BaseException) -> str | None:
    """The Slack error a `_request` failure names (`not_in_channel`, `missing_scope`, …), else None."""
    s = str(e)
    return s[len(ERROR_PREFIX):] if isinstance(e, RuntimeError) and s.startswith(ERROR_PREFIX) else None


def call_ok(client: Any, method: str, args: dict, *benign: str) -> dict:
    """``method`` through ``client._request``; the ``benign`` errors mean the change is already in place (`{}`)."""
    try:
        return client._request(method, args)
    except RuntimeError as e:
        if error_code(e) in benign:
            return {}
        raise


def join(client: Any, channel: str) -> None:
    """Join the public ``channel`` (pinning and reading replies need membership); a private one is a no-op."""
    call_ok(client, "conversations.join", {"channel": channel}, "method_not_supported_for_channel_type")


def pin(client: Any, channel: str, ts: str) -> None:
    call_ok(client, "pins.add", {"channel": channel, "timestamp": ts}, "already_pinned")


def unpin(client: Any, channel: str, ts: str) -> None:
    """Unpin ``ts``: one already unpinned (by hand) or deleted is not an error."""
    call_ok(client, "pins.remove", {"channel": channel, "timestamp": ts}, "no_pin", "message_not_found")
