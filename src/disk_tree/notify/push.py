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

``DISKY_NOTIFY`` (comma-separated, in order) picks and orders them; unset,
every channel whose credentials are present is used, in the order above.
:func:`send` posts to all of them, or (``first=True``) down the list until one
succeeds: a fallback chain. Standard library only, so any image can send.
"""
from __future__ import annotations

import json
import os

from disk_tree.env import get as env_get
from dataclasses import dataclass
from typing import Callable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PUSHOVER_URL = 'https://api.pushover.net/1/messages.json'
SLACK_URL = 'https://slack.com/api/chat.postMessage'
SLACK_UPDATE_URL = 'https://slack.com/api/chat.update'
SLACK_HISTORY_URL = 'https://slack.com/api/conversations.history'
#: Message metadata naming a grouped alert (`Notice.group`), and how far back its message is looked for.
SLACK_GROUP_EVENT = 'disky_alert'
SLACK_GROUP_WINDOW_S = 2 * 86400
# Discord's edge refuses urllib's default User-Agent (403, error 1010).
USER_AGENT = 'disk-tree (https://github.com/Open-Athena/disky, 1)'
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
    links: tuple[tuple[str, str], ...] = ()  # additional (label, url) pairs
    #: Slack only: notices sharing a group (a scan run's id) are one message, each notice a line in it (with its own
    #: `url`), the highest priority's title on top and the shared `links` below; other channels post each notice.
    group: Optional[str] = None


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
    """The channels to use, in order: ``DISKY_NOTIFY``'s (or ``DISK_TREE_NOTIFY``'s), else every one with credentials."""
    has = {
        'pushover': bool(secret(env, 'PUSHOVER_TOKEN') and secret(env, 'PUSHOVER_USER')),
        'discord': bool(secret(env, 'DISCORD_NOTIFY_WEBHOOK')),
        'slack': bool(secret(env, 'SLACK_BOT_TOKEN') and secret(env, 'SLACK_ALERT_CHANNEL')),
    }
    named = env_get('NOTIFY', env=env)
    if named is None:
        return [c for c in CHANNELS if has[c]]
    chans = [c.strip() for c in named.split(',') if c.strip()]
    for c in chans:
        if c not in has:
            raise ValueError(f'DISKY_NOTIFY: unknown channel {c!r} (known: {", ".join(CHANNELS)})')
        if not has[c]:
            raise ValueError(f'DISKY_NOTIFY names {c!r}, but its credentials are not set')
    return chans


def pushover(n: Notice, env: Mapping[str, str], post: Post) -> None:
    message = n.message + ''.join(f'\n{label}: {url}' for label, url in n.links)
    form = {'token': secret(env, 'PUSHOVER_TOKEN'), 'user': secret(env, 'PUSHOVER_USER'), 'title': n.title, 'message': message, 'priority': str(n.priority)}
    if n.url:
        form['url'] = n.url
        if n.url_title:
            form['url_title'] = n.url_title
    status, body = post(PUSHOVER_URL, urlencode(form).encode(), {'Content-Type': 'application/x-www-form-urlencoded'})
    if status != 200:
        # The body names the bad field (`{"user":"invalid",…}`), never echoes a credential.
        raise RuntimeError(f'pushover: HTTP {status}: {body[:200]}')


def discord(n: Notice, env: Mapping[str, str], post: Post) -> None:
    links = ([(n.url_title, n.url)] if n.url else []) + list(n.links)
    link = '\n' + ' · '.join(f'[{label}](<{url}>)' if label else f'<{url}>' for label, url in links) if links else ''
    payload = {
        'content': f'**{n.title}**\n{n.message}{link}',
        'username': 'disky',
        'allowed_mentions': {'parse': []},
        'flags': SUPPRESS_EMBEDS,
    }
    status, body = post(secret(env, 'DISCORD_NOTIFY_WEBHOOK'), json.dumps(payload).encode(), {'Content-Type': 'application/json'})
    if status not in (200, 204):
        raise RuntimeError(f'discord: HTTP {status}: {body[:200]}')


def slack_text(n: Notice) -> str:
    """The ``text`` :func:`slack` posts for ``n``: the title bold, the message, then the links."""
    links = ([(n.url_title, n.url)] if n.url else []) + list(n.links)
    link = '\n' + ' · '.join(f'<{url}|{label}>' if label else f'<{url}>' for label, url in links) if links else ''
    return f'*{n.title}*\n{n.message}{link}'


def _slack_call(url: str, payload: dict, env: Mapping[str, str], post: Post, form: bool = False) -> dict:
    """One Slack Web API call; Slack answers HTTP 200 with ``ok: false`` on most failures (``channel_not_found``,
    ``invalid_auth``), so the body decides. ``form``: a read method, which takes no JSON body."""
    auth = {'Authorization': f'Bearer {secret(env, "SLACK_BOT_TOKEN")}'}
    if form:
        body, kind = urlencode(payload).encode(), 'application/x-www-form-urlencoded'
    else:
        body, kind = json.dumps(payload).encode(), 'application/json; charset=utf-8'
    status, text = post(url, body, {'Content-Type': kind, **auth})
    try:
        res = json.loads(text) if text else {}
    except ValueError:
        res = {}
    if status != 200 or not res.get('ok'):
        # Slack's error body names the failure (`{"ok":false,"error":"invalid_auth"}`), never the token.
        raise RuntimeError(f'slack: HTTP {status}: {text[:200]}')
    return res


def _links(links: list[tuple[Optional[str], str]]) -> str:
    return ' · '.join(f'<{url}|{label}>' if label else f'<{url}>' for label, url in links)


def slack_group_text(group: dict) -> str:
    """A grouped alert's ``text``: its title bold, a line per notice (with that notice's link), the shared links."""
    lines = [line['message'] + (f" · {_links([(line.get('url_title'), line['url'])])}" if line.get('url') else '') for line in group['lines']]
    return f"*{group['title']}*\n" + '\n'.join(lines) + (f"\n{_links([(lb, u) for lb, u in group['links']])}" if group['links'] else '')


def slack(n: Notice, env: Mapping[str, str], post: Post, now: Optional[float] = None) -> None:
    """``chat.postMessage``, top-level in ``SLACK_ALERT_CHANNEL``. A grouped notice (``n.group``) whose group already
    has a message of ours (found by its metadata within ``SLACK_GROUP_WINDOW_S``) is added to it (``chat.update``)
    instead; one that can't be looked up posts its own message, so an alert is never lost to the grouping."""
    import time

    channel = secret(env, 'SLACK_ALERT_CHANNEL')
    if not n.group:
        _slack_call(SLACK_URL, {'channel': channel, 'text': slack_text(n), 'unfurl_links': False, 'unfurl_media': False}, env, post)
        return
    line = {k: v for k, v in (('message', n.message), ('url', n.url), ('url_title', n.url_title)) if v}
    found = None
    try:
        res = _slack_call(SLACK_HISTORY_URL, {'channel': channel, 'oldest': str((now or time.time()) - SLACK_GROUP_WINDOW_S),
                                              'limit': 200, 'include_all_metadata': 'true'}, env, post, form=True)
        found = next((m for m in res.get('messages', []) if m.get('metadata', {}).get('event_type') == SLACK_GROUP_EVENT
                      and m['metadata'].get('event_payload', {}).get('group') == n.group), None)
    except (RuntimeError, URLError, OSError):
        pass
    try:
        if found:
            was = found['metadata']['event_payload']
            top = n.priority > was.get('priority', 0)
            group = {**was, 'title': n.title if top else was['title'], 'priority': max(n.priority, was.get('priority', 0)),
                     'lines': [*was['lines'], line], 'links': [list(x) for x in n.links] or was['links']}
            _slack_call(SLACK_UPDATE_URL, {'channel': channel, 'ts': found['ts'], 'text': slack_group_text(group),
                                           'metadata': {'event_type': SLACK_GROUP_EVENT, 'event_payload': group}}, env, post)
            return
        group = {'group': n.group, 'title': n.title, 'priority': n.priority, 'lines': [line], 'links': [list(x) for x in n.links]}
        _slack_call(SLACK_URL, {'channel': channel, 'text': slack_group_text(group), 'unfurl_links': False, 'unfurl_media': False,
                                'metadata': {'event_type': SLACK_GROUP_EVENT, 'event_payload': group}}, env, post)
    except RuntimeError:
        # The grouped write refused (its metadata, or the message gone): the alert posts on its own, as ungrouped.
        _slack_call(SLACK_URL, {'channel': channel, 'text': slack_text(n), 'unfurl_links': False, 'unfurl_media': False}, env, post)


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
