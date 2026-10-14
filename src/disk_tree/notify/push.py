"""Push notifications to a person: a failed ingest, a scan that never ran.

Channels are configured by environment alone (secrets by value, as a job's
injected env carries them):
  - ``pushover``: ``PUSHOVER_TOKEN`` (the application's API token) and
    ``PUSHOVER_USER`` (the user or group key)
  - ``discord``: ``DISCORD_NOTIFY_WEBHOOK`` (a channel webhook URL)
  - ``slack``: ``SLACK_BOT_TOKEN`` (a bot token with ``chat:write``) and
    ``SLACK_ALERT_CHANNEL`` (the channel id or name it posts to, top-level)

Each value is read with trailing whitespace stripped (a secret pasted with its
newline must not become a bad header or URL).

``DISK_TREE_NOTIFY`` (comma-separated, in order) picks and orders them; unset,
every channel whose credentials are present is used, in the order above.
:func:`send` posts to all of them, or (``first=True``) down the list until one
succeeds: a fallback chain. Standard library only, so any image can send.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Callable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PUSHOVER_URL = 'https://api.pushover.net/1/messages.json'
SLACK_URL = 'https://slack.com/api/chat.postMessage'
# Discord's edge refuses urllib's default User-Agent (403, error 1010).
USER_AGENT = 'disk-tree (https://github.com/runsascoded/disky, 1)'
CHANNELS = ('pushover', 'discord', 'slack')
# Discord message flag: no link previews.
SUPPRESS_EMBEDS = 1 << 2


@dataclass(frozen=True)
class Notice:
    title: str
    message: str
    url: Optional[str] = None
    url_title: Optional[str] = None
    priority: int = 0  # Pushover's: -2 (silent) … 1 (high); 2 (emergency) needs retry/expire


# `post(url, body, headers) -> (status, response body)`; the seam tests replace.
Post = Callable[[str, bytes, dict[str, str]], tuple[int, str]]


def http_post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
    req = Request(url, data=body, headers={'User-Agent': USER_AGENT, **headers}, method='POST')
    try:
        with urlopen(req, timeout=20) as r:
            return r.status, r.read().decode(errors='replace')
    except HTTPError as e:
        return e.code, e.read().decode(errors='replace')


def secret(env: Mapping[str, str], name: str) -> str:
    """``env[name]`` with trailing whitespace stripped; empty when unset."""
    return (env.get(name) or '').rstrip()


def configured(env: Mapping[str, str] = os.environ) -> list[str]:
    """The channels to use, in order: ``DISK_TREE_NOTIFY``'s, else every one with credentials."""
    has = {
        'pushover': bool(secret(env, 'PUSHOVER_TOKEN') and secret(env, 'PUSHOVER_USER')),
        'discord': bool(secret(env, 'DISCORD_NOTIFY_WEBHOOK')),
        'slack': bool(secret(env, 'SLACK_BOT_TOKEN') and secret(env, 'SLACK_ALERT_CHANNEL')),
    }
    named = env.get('DISK_TREE_NOTIFY')
    if named is None:
        return [c for c in CHANNELS if has[c]]
    chans = [c.strip() for c in named.split(',') if c.strip()]
    for c in chans:
        if c not in has:
            raise ValueError(f'DISK_TREE_NOTIFY: unknown channel {c!r} (known: {", ".join(CHANNELS)})')
        if not has[c]:
            raise ValueError(f'DISK_TREE_NOTIFY names {c!r}, but its credentials are not set')
    return chans


def pushover(n: Notice, env: Mapping[str, str], post: Post) -> None:
    form = {'token': secret(env, 'PUSHOVER_TOKEN'), 'user': secret(env, 'PUSHOVER_USER'), 'title': n.title, 'message': n.message, 'priority': str(n.priority)}
    if n.url:
        form['url'] = n.url
        if n.url_title:
            form['url_title'] = n.url_title
    status, body = post(PUSHOVER_URL, urlencode(form).encode(), {'Content-Type': 'application/x-www-form-urlencoded'})
    if status != 200:
        # The body names the bad field (`{"user":"invalid",…}`), never echoes a credential.
        raise RuntimeError(f'pushover: HTTP {status}: {body[:200]}')


def discord(n: Notice, env: Mapping[str, str], post: Post) -> None:
    link = f'\n{f"[{n.url_title}](<{n.url}>)" if n.url_title else f"<{n.url}>"}' if n.url else ''
    payload = {
        'content': f'**{n.title}**\n{n.message}{link}',
        'username': 'disky',
        'allowed_mentions': {'parse': []},
        'flags': SUPPRESS_EMBEDS,
    }
    status, body = post(secret(env, 'DISCORD_NOTIFY_WEBHOOK'), json.dumps(payload).encode(), {'Content-Type': 'application/json'})
    if status not in (200, 204):
        raise RuntimeError(f'discord: HTTP {status}: {body[:200]}')


def slack(n: Notice, env: Mapping[str, str], post: Post) -> None:
    """``chat.postMessage``, top-level in ``SLACK_ALERT_CHANNEL``. Slack answers HTTP 200 with ``ok: false`` on
    most failures (``channel_not_found``, ``invalid_auth``), so the body decides."""
    link = f'\n<{n.url}|{n.url_title}>' if n.url and n.url_title else f'\n<{n.url}>' if n.url else ''
    payload = {
        'channel': secret(env, 'SLACK_ALERT_CHANNEL'),
        'text': f'*{n.title}*\n{n.message}{link}',
        'unfurl_links': False,
        'unfurl_media': False,
    }
    status, body = post(SLACK_URL, json.dumps(payload).encode(), {
        'Content-Type': 'application/json; charset=utf-8',
        'Authorization': f'Bearer {secret(env, "SLACK_BOT_TOKEN")}',
    })
    try:
        res = json.loads(body) if body else {}
    except ValueError:
        res = {}
    if status != 200 or not res.get('ok'):
        # Slack's error body names the failure (`{"ok":false,"error":"invalid_auth"}`), never the token.
        raise RuntimeError(f'slack: HTTP {status}: {body[:200]}')


SENDERS: dict[str, Callable[[Notice, Mapping[str, str], Post], None]] = {'pushover': pushover, 'discord': discord, 'slack': slack}


def send(
    n: Notice,
    channels: list[str],
    first: bool = False,
    env: Mapping[str, str] = os.environ,
    post: Post = http_post,
) -> list[tuple[str, Optional[str]]]:
    """Post ``n`` to each channel (or, ``first``, until one succeeds). Returns each
    attempt as ``(channel, error or None)``; a failing channel never stops the rest."""
    out: list[tuple[str, Optional[str]]] = []
    for c in channels:
        try:
            SENDERS[c](n, env, post)
        except (RuntimeError, URLError, OSError) as e:
            out.append((c, str(e)))
            continue
        out.append((c, None))
        if first:
            break
    return out
