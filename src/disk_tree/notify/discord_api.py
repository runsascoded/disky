"""Thin Discord REST helpers for what ``thrds``'s clients don't cover:
application emoji (a digest's trend-arrow glyphs) and webhook introspection
(which channel a webhook posts to). Bot calls send Discord's required
``DiscordBot (url, version)`` User-Agent — Cloudflare 403s urllib's default.

Error messages carry a caller-supplied label, never the URL: a webhook URL
embeds its secret token.

Generalized from ``marin-gcs-usage`` (``cloud/dt_cloud/discord_api.py``) as
part of the disk-tree notify/digest engine (``specs/comms-notify.md``); the one
deployment-specific bit, the User-Agent, is a parameter (default names
disk-tree). Stdlib-only — no ``thrds`` needed for these calls."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API = "https://discord.com/api/v10"
DEFAULT_USER_AGENT = "DiscordBot (https://github.com/runsascoded/disk-tree, 0.1)"


def _request(
    method: str,
    url: str,
    *,
    label: str,
    token: str | None = None,
    body: dict | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
) -> Any:
    # ``json.load`` returns a dict *or* a list (Discord's collection endpoints
    # answer with arrays), so the response type is genuinely ``Any`` — each
    # helper below narrows it to what its endpoint returns.
    headers = {"User-Agent": user_agent}
    if token is not None:
        headers["Authorization"] = token if token.startswith("Bot ") else f"Bot {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        snippet = e.read()[:300].decode(errors="replace")
        raise RuntimeError(f"Discord {label}: HTTP {e.code}: {snippet}") from None


def webhook_info(webhook_url: str, *, user_agent: str = DEFAULT_USER_AGENT) -> dict:
    """The webhook object (``channel_id``, ``guild_id``, ``name``, …) — an
    unauthenticated GET on the webhook URL itself."""
    return _request("GET", webhook_url, label="GET webhook", user_agent=user_agent)


def app_id(token: str, *, user_agent: str = DEFAULT_USER_AGENT) -> str:
    """The application id behind a bot token (``GET /applications/@me``)."""
    return _request(
        "GET", f"{API}/applications/@me", label="GET applications/@me", token=token, user_agent=user_agent,
    )["id"]


def app_emojis(token: str, app: str | None = None, *, user_agent: str = DEFAULT_USER_AGENT) -> dict[str, str]:
    """The bot's application emoji, ``name -> id`` (what ``<:name:id>`` needs)."""
    app = app or app_id(token, user_agent=user_agent)
    items = _request(
        "GET", f"{API}/applications/{app}/emojis", label="GET application emojis", token=token, user_agent=user_agent,
    )["items"]
    return {e["name"]: e["id"] for e in items}


def upload_app_emoji(
    token: str,
    app: str,
    name: str,
    png: Path,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
) -> str:
    """Create one application emoji from a PNG (≤ 256 KiB; Discord rescales to
    128px); returns its id. ``name`` must be ``[A-Za-z0-9_]{2,32}``."""
    b64 = base64.b64encode(Path(png).read_bytes()).decode()
    resp = _request(
        "POST", f"{API}/applications/{app}/emojis", label=f"POST application emoji {name}", token=token,
        body={"name": name, "image": f"data:image/png;base64,{b64}"}, user_agent=user_agent,
    )
    return resp["id"]


def guild_channels(token: str, guild: str, *, user_agent: str = DEFAULT_USER_AGENT) -> dict[str, str]:
    """Text channels of a guild, ``name -> id``."""
    chans = _request(
        "GET", f"{API}/guilds/{guild}/channels", label="GET guild channels", token=token, user_agent=user_agent,
    )
    return {c["name"]: c["id"] for c in chans if c["type"] == 0}


def channel_webhooks(token: str, channel: str, *, user_agent: str = DEFAULT_USER_AGENT) -> list[dict]:
    """The channel's webhooks (needs Manage Webhooks there); app-owned ones carry their ``token``."""
    return _request(
        "GET", f"{API}/channels/{channel}/webhooks", label="GET channel webhooks", token=token, user_agent=user_agent,
    )


def create_webhook(token: str, channel: str, name: str, *, user_agent: str = DEFAULT_USER_AGENT) -> dict:
    """Create a webhook owned by the bot's application in ``channel`` (needs Manage Webhooks)."""
    return _request(
        "POST", f"{API}/channels/{channel}/webhooks", label="POST channel webhook", token=token,
        body={"name": name}, user_agent=user_agent,
    )


def webhook_url(hook: dict) -> str:
    return f"{API}/webhooks/{hook['id']}/{hook['token']}"
