"""Announce deletion runs to Slack/Discord (spec ``specs/staged-delete.md``, CP4).

The drainer calls :func:`make_announcer` once (from the deployment's ``delete:``
chat config) and passes the returned callback to ``drain.drain_once``, which
invokes it per finished run. Secrets are named by their env var (``*_env``),
read at post time — never inlined. Needs the ``notify`` extra (``thrds``).
"""
from __future__ import annotations

import os
from typing import Callable, Optional

Announcer = Callable[[dict], None]


def format_run(summary: dict) -> str:
    """A chat message for one finished run (the same shape both platforms get)."""
    from humanize import naturalsize

    who = f" by {summary['actor']}" if summary.get("actor") else ""
    lines = [
        f":wastebasket: run `{summary['run_id']}`{who}: deleted "
        f"{naturalsize(summary['deleted_bytes'])} ({summary['deleted_objects']} object(s)) "
        f"across {summary['items']} path(s)"
    ]
    errors = summary.get("errors") or []
    if errors:
        lines.append(f":warning: {len(errors)} failed:")
        lines.extend(f"  • {uri}: {msg}" for uri, msg in errors[:10])
    return "\n".join(lines)


def make_announcer(chat_cfg: Optional[dict]) -> Optional[Announcer]:
    """A per-run announce callback from a ``delete:`` chat config, or ``None``
    when chat is off. ``chat: slack|discord|none`` selects the transport; the
    matching ``slack``/``discord`` sub-block names the channel + secret env vars.
    """
    platform = (chat_cfg or {}).get("chat", "none")
    if platform == "none":
        return None
    assert chat_cfg is not None
    if platform == "slack":
        from thrds.slack import SlackClient

        sc = chat_cfg["slack"]
        client = SlackClient(os.environ[sc["token_env"]], sc["channel"])
        return lambda s: client.post(format_run(s), username="disk-tree", icon_emoji=":wastebasket:")
    if platform == "discord":
        from thrds.discord import NO_MENTIONS, DiscordWebhookClient

        dc = chat_cfg["discord"]
        hook = DiscordWebhookClient(os.environ[dc["webhook_env"]], suppress_embeds=True, allowed_mentions=NO_MENTIONS)
        return lambda s: hook.post(format_run(s), username="disk-tree")
    raise ValueError(f"unknown delete.chat platform {platform!r}")
