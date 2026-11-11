"""`dt-cloud monitoring-relay`: Cloud Monitoring incidents → the deployment's own alert channel (`disk-tree notify`).

A GCP deployment's alert policies (e.g. `CronAlerts`) notify a Pub/Sub notification channel; a push subscription
delivers each message to a Cloud Run service running `serve` (infra `MonitoringRelay`, spec specs/monitoring-relay.md),
which renders the incident and posts it with `disk_tree.notify.push`, as every other disky alert is posted: the
deployment's own Slack bot (`SLACK_BOT_TOKEN`, `SLACK_ALERT_CHANNEL`), no Monitoring Slack app and no console OAuth.

The message is Monitoring's notification JSON (schema `version` 1.2: an `incident` with `policy_name`,
`condition_name`, `state` `open` | `closed`, `started_at` / `ended_at` epoch seconds, `documentation.content`, `url`).
An open incident renders the policy, the condition, when it opened, the policy's documentation and a link to the
incident; a closed one, a short "resolved" notice (`notify` posts top-level, so not in the open one's thread).
Text is Slack mrkdwn: `&`, `<`, `>` escaped, Markdown links in the documentation made Slack links.

`serve` answers 204 once a channel took the notice, 500 when none did (Pub/Sub redelivers, with backoff). A body that
isn't a Monitoring notification is posted raw rather than dropped, so a schema change is seen, not lost."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

from click import File, argument, group, option

if TYPE_CHECKING:
    from disk_tree.notify.push import Notice

# `disk_tree.notify.push` is imported where it's used: `dt_cloud.cli` loads this module, and must load without the
# engine installed (`tests/test_cli_import.py`).

err = partial(print, file=sys.stderr, flush=True)

#: The raw body's share of an unrecognized notification's message.
RAW_CHARS = 1500

_MD_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")


def escape(text: str) -> str:
    """Slack mrkdwn's three control characters, escaped (an unescaped `<region>` would be read as a link)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def mrkdwn(markdown: str) -> str:
    """A policy's Markdown documentation as Slack mrkdwn: escaped, `[text](url)` → `<url|text>`. Backticks and
    code fences read the same in both; other Markdown passes through as written."""
    return _MD_LINK.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", escape(markdown))


def utc(ts: int | float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def duration(seconds: int | float) -> str:
    """`45s`, `30m`, `2h05m`, `3d4h`."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h}h"


def render(payload: Mapping[str, Any]) -> Notice:
    """The notice for one Monitoring notification (`{"version": "1.2", "incident": {…}}`). Raises `ValueError` on a
    payload without an incident, its policy name or a known state."""
    inc = payload.get("incident")
    if not isinstance(inc, Mapping):
        raise ValueError("no incident")
    policy = inc.get("policy_name")
    state = inc.get("state")
    if not policy or state not in ("open", "closed"):
        raise ValueError(f"incident without a policy name or a known state (state={state!r})")
    from disk_tree.notify.push import Notice

    condition = escape(inc.get("condition_name") or "its condition")
    url = inc.get("url") or None
    started = inc.get("started_at")
    if state == "closed":
        ended = inc.get("ended_at")
        when = ""
        if started is not None and ended is not None:
            a, b = utc(started), utc(ended)
            end = b.strftime("%H:%M" if a.date() == b.date() else "%Y-%m-%d %H:%M")
            when = f" · open {a:%Y-%m-%d %H:%M} → {end} UTC ({duration(ended - started)})"
        return Notice(f"✅ resolved: {escape(policy)}", f"{condition}{when}", url, "incident" if url else None)
    title = f"⚠️ {escape(policy)}" + (" (still open)" if inc.get("renotify") else "")
    lines = [condition + (f" · opened {utc(started):%Y-%m-%d %H:%M} UTC" if started is not None else "")]
    doc = ((inc.get("documentation") or {}).get("content") or "").strip()
    if doc:
        lines.append(mrkdwn(doc))
    return Notice(title, "\n".join(lines), url, "incident" if url else None, 1)


def unrecognized(raw: str, why: str) -> Notice:
    """A body that isn't a Monitoring notification, posted as it came (truncated)."""
    from disk_tree.notify.push import Notice

    body = raw if len(raw) <= RAW_CHARS else raw[:RAW_CHARS] + "…"
    return Notice("⚠️ Monitoring notification (unrecognized)", f"{escape(why)}\n```{escape(body)}```", priority=1)


def notice_for(data: bytes) -> Notice:
    """The notice for one Pub/Sub message's data: `render`'s, else `unrecognized`'s."""
    raw = data.decode(errors="replace")
    try:
        payload = json.loads(raw)
    except ValueError as e:
        return unrecognized(raw, f"not JSON: {e}")
    if not isinstance(payload, Mapping):
        return unrecognized(raw, "not a JSON object")
    try:
        return render(payload)
    except ValueError as e:
        return unrecognized(raw, str(e))


Send = Callable[["Notice"], list[tuple[str, str | None]]]


def handle(body: bytes, send: Send) -> int:
    """One push delivery (`{"message": {"data": <base64>, "messageId": …}, "subscription": …}`) → an HTTP status:
    204 once a channel took the notice (Pub/Sub acks), 500 when every one failed (it redelivers), 400 for a body
    that isn't a push envelope at all (nothing to post; Pub/Sub redelivers it too, and the log says why)."""
    try:
        msg = json.loads(body)["message"]
        data = base64.b64decode(msg.get("data") or "", validate=True)
        mid = msg.get("messageId") or msg.get("message_id")
    except (ValueError, KeyError, TypeError, binascii.Error) as e:
        err(f"monitoring-relay: not a Pub/Sub push envelope: {e}")
        return 400
    n = notice_for(data)
    results = send(n)
    for c, e in results:
        err(f"monitoring-relay: message {mid}: {n.title}: {c}: {e or 'sent'}")
    if not results or all(e for _, e in results):
        return 500
    return 204


def sender(channels: list[str], env: Mapping[str, str]) -> Send:
    from disk_tree.notify import push

    return lambda n: push.send(n, channels, env=env, post=push.http_post)


def channels_from(spec: str, env: Mapping[str, str]) -> tuple[list[str], dict[str, str]]:
    """`-c`'s channels as `push.configured` resolves them (raising when one lacks its credentials)."""
    from disk_tree.env import name as env_name
    from disk_tree.notify import push

    e = {**env, env_name("NOTIFY"): spec}
    return push.configured(e), e


def server(port: int, send: Send, host: str = "") -> ThreadingHTTPServer:
    """An HTTP server answering each POST with `handle` (any path: the push endpoint is the service's root)."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(handle(body, send))
            self.end_headers()

        def log_message(self, fmt: str, *args) -> None:
            err(f"monitoring-relay: {self.address_string()} {fmt % args}")

    return ThreadingHTTPServer((host, port), Handler)


@group("monitoring-relay")
def cli() -> None:
    """Cloud Monitoring incidents (a Pub/Sub notification channel's messages) → `disk-tree notify`'s channels."""


@cli.command("serve")
@option("-c", "--channels", default="slack", show_default=True, help="Channels to post to, in order (`DISKY_NOTIFY`'s syntax); each needs its credentials in the env")
@option("-p", "--port", type=int, default=None, help="Port to listen on (default `$PORT`, as Cloud Run sets it, else 8080)")
def serve_cmd(channels: str, port: int | None) -> None:
    """Answer a Pub/Sub push subscription's deliveries: each Monitoring incident posted to `-c`'s channels. The
    caller is authenticated by Cloud Run (the push's OIDC token, `run.invoker`), not here."""
    chans, env = channels_from(channels, os.environ)
    port = port or int(os.environ.get("PORT") or 8080)
    err(f"monitoring-relay: posting to {', '.join(chans)}; listening on :{port}")
    server(port, sender(chans, env)).serve_forever()


@cli.command("render")
@option("-c", "--channels", default="slack", show_default=True, help="With `-s`: the channels to post to")
@option("-s", "--send", "do_send", is_flag=True, help="Post the notice (a by-hand test of the channel) instead of printing it")
@argument("file", type=File("rb"), default="-")
def render_cmd(channels: str, do_send: bool, file) -> None:
    """Print the Slack text the relay would post for FILE (a Monitoring notification's JSON; default stdin)."""
    from disk_tree.notify import push

    n = notice_for(file.read())
    if not do_send:
        print(push.slack_text(n))
        return
    chans, env = channels_from(channels, os.environ)
    results = sender(chans, env)(n)
    for c, e in results:
        err(f"monitoring-relay: {c}: {e or 'sent'}")
    if not results or all(e for _, e in results):
        sys.exit(1)
