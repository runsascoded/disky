"""Push notifications to a person: a failed ingest, a scan that never ran.

Channels are configured by environment alone (secrets by value, as a job's
injected env carries them):
  - ``pushover``: ``PUSHOVER_TOKEN`` (the application's API token) and
    ``PUSHOVER_USER`` (the user or group key)
  - ``discord``: ``DISCORD_NOTIFY_WEBHOOK`` (a channel webhook URL)

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
# Discord's edge refuses urllib's default User-Agent (403, error 1010).
USER_AGENT = 'disk-tree (https://github.com/runsascoded/disky, 1)'
CHANNELS = ('pushover', 'discord')
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


def configured(env: Mapping[str, str] = os.environ) -> list[str]:
    """The channels to use, in order: ``DISK_TREE_NOTIFY``'s, else every one with credentials."""
    has = {
        'pushover': bool(env.get('PUSHOVER_TOKEN') and env.get('PUSHOVER_USER')),
        'discord': bool(env.get('DISCORD_NOTIFY_WEBHOOK')),
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
    form = {'token': env['PUSHOVER_TOKEN'], 'user': env['PUSHOVER_USER'], 'title': n.title, 'message': n.message, 'priority': str(n.priority)}
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
    status, body = post(env['DISCORD_NOTIFY_WEBHOOK'], json.dumps(payload).encode(), {'Content-Type': 'application/json'})
    if status not in (200, 204):
        raise RuntimeError(f'discord: HTTP {status}: {body[:200]}')


SENDERS: dict[str, Callable[[Notice, Mapping[str, str], Post], None]] = {'pushover': pushover, 'discord': discord}


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
