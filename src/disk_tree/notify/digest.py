"""The generic digest engine: converge a period's Slack/Discord thread from a
per-deployment :class:`~disk_tree.notify.profile.DigestProfile`.

One thread per period (month, by default): an OP edited in place as the period
progresses, plus one reply per scan. The mechanism — converge state, post/edit
the OP, post the not-yet-posted replies, host or attach the plot, day-keyed
replies, the trend-arrow math and Slack→Discord emoji rewrite — lives here and
is deployment-agnostic; every metric-specific string (the OP body, a reply's
sender/body/avatar, the plot) comes from the profile.

Generalized from ``marin-gcs-usage``'s ``dt_cloud.digest`` (``specs/comms-notify.md``):
gcs's shape-C monthly GCS digest is now the reference profile's cousin — the
engine is what both deployments share.

The pure functions (``deg``, ``emoji_name``, ``discordify``) and the
``converge_*`` lifecycles are unit-tested against the reference profile with
fake posting clients; ``post_digest``/``post_digest_discord`` are the thin
side-effecting shells that drive real ``thrds`` clients (the ``notify`` extra).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from .profile import DigestProfile, Row

MINUS = "−"  # the site's unicode minus (U+2212), not ASCII hyphen
# Weekly-halving arrow buckets: |dpct| * mult >= THRESH[i] -> degree (i+1)*10,
# capped at 80. deg80 ~ ±50%/week, so a daily reply (mult=7) and a weekly bullet
# (mult=1) at the same underlying rate draw the same arrow. Shared UX convention;
# a profile that wants a different anchor overrides `deg` (or passes its own).
THRESH = [0.39, 0.78, 1.5, 3.1, 6.25, 12.5, 25, 50]


def deg(pct_signed: float, mult: float = 1.0) -> int:
    """Signed arrow degree for a percent change, time-normalized by ``mult``.

    Anchored on a weekly halving (deg80 ~ ±50%/week). A daily reply passes
    ``mult=7`` (project the day's rate to a weekly-equivalent), a weekly bullet
    ``mult=1`` — so a daily arrow and a weekly arrow mean the same rate."""
    a = abs(pct_signed) * mult
    d = 0
    for i, t in enumerate(THRESH):
        if a >= t:
            d = (i + 1) * 10
    d = min(80, d)
    return -d if pct_signed < 0 else d


# ---- shared content-format helpers (a profile may use these) --------------


def signed(v: float, digits: int = 1) -> str:
    """A signed number with the site's unicode minus (e.g. ``+30.0`` / ``−20.0``)."""
    return f"+{v:.{digits}f}" if v >= 0 else f"{MINUS}{abs(v):.{digits}f}"


def pct(delta: float, total: float) -> str:
    """``|delta|`` as a percent of the *previous* total (``total - delta``), 1 dp."""
    prev = total - delta
    return f"{abs(delta / prev * 100) if prev else 0:.1f}"


def pct_val(delta: float, total: float) -> float:
    """Signed percent change vs. the previous total (for ``deg``)."""
    prev = total - delta
    return delta / prev * 100 if prev else 0.0


def yymmdd(date: str) -> str:
    """``2026-08-03`` -> ``260803`` (the site's compact date query param)."""
    return date[2:].replace("-", "")


# ---- Slack <-> Discord emoji ----------------------------------------------

_EMOJI_RE = re.compile(r":arrow_deg(-?\d+):")


def emoji_name(d: int) -> str:
    """Discord application-emoji name for a signed arrow degree. Discord names
    are ``[A-Za-z0-9_]`` only (no ``-``), so a negative reads ``arrow_degm30``
    where the Slack custom emoji is ``arrow_deg-30``."""
    return f"arrow_deg{d}" if d >= 0 else f"arrow_degm{-d}"


def discordify(text: str, emoji: dict[str, str]) -> str:
    """Rewrite Slack ``:arrow_degN:`` shortcodes in ``text`` to Discord's
    ``<:name:id>`` form via ``emoji`` (app-emoji name -> id). The rest of the
    markdown (bold, italics, masked links) renders the same on both platforms."""
    def sub(m: re.Match) -> str:
        name = emoji_name(int(m.group(1)))
        if name not in emoji:
            raise ValueError(f"discord app emoji {name!r} missing — upload the arrow set")
        return f"<:{name}:{emoji[name]}>"
    return _EMOJI_RE.sub(sub, text)


# ---- period model ----------------------------------------------------------


@dataclass(frozen=True)
class Period:
    """One digest window: a ``kind`` (``month`` | ``week`` | ``day``) and its
    ``start`` date. ``key`` names the converge-state file; the human title is the
    profile's (``profile.title``)."""

    kind: str
    start: dt.date

    @property
    def key(self) -> str:
        if self.kind == "month":
            return f"{self.start:%Y-%m}"
        if self.kind == "week":
            return f"{self.start:%G-W%V}"
        if self.kind == "day":
            return f"{self.start:%Y-%m-%d}"
        raise ValueError(f"unknown digest period {self.kind!r}")


def period_of(date: dt.date, kind: str = "month") -> Period:
    """The :class:`Period` of ``kind`` that contains ``date``."""
    if kind == "month":
        return Period(kind, date.replace(day=1))
    if kind == "week":
        return Period(kind, date - dt.timedelta(days=date.weekday()))
    if kind == "day":
        return Period(kind, date)
    raise ValueError(f"unknown digest period {kind!r}")


def select_window(dates: list[str], period: Period) -> tuple[list[str], bool]:
    """The scan dates for ``period`` plus one lead-in scan (for the first
    delta), from a sorted ``dates`` list. Returns ``(window, has_lead_in)`` —
    the caller slices the lead-in off the built rows when ``has_lead_in``."""
    if period.kind == "month":
        pfx = f"{period.key}-"
        in_period = [d for d in dates if d.startswith(pfx)]
    elif period.kind == "day":
        in_period = [d for d in dates if d == period.key]
    else:
        end = period.start + dt.timedelta(days=6)
        in_period = [d for d in dates if period.start <= dt.date.fromisoformat(d) <= end]
    if not in_period:
        return [], False
    first = dates.index(in_period[0])
    window = dates[max(0, first - 1) : dates.index(in_period[-1]) + 1]
    return window, first > 0


# ---- converge state (side-effecting, via blobfs) ---------------------------


def _err(*a) -> None:
    print(*a, file=sys.stderr)


def state_path(root: str, period: Period, platform: str = "slack", key: str | None = None) -> str:
    """Converge-state JSON for one period's thread. Slack: ``digest/<key>.json``
    (one prod thread). Discord: ``digest/discord/<webhook_id>/<key>.json`` —
    keyed by webhook, because a webhook can only edit its own messages, so its
    OP/replies are only reachable through it (and a staging webhook never
    masquerades as the prod thread). ``root`` is the scans dir/URL."""
    base = root.rstrip("/").rsplit("/snapshots", 1)[0].rstrip("/")
    if platform == "slack":
        return f"{base}/digest/{period.key}.json"
    if platform == "discord":
        if not key:
            raise ValueError("discord digest state is keyed by webhook id")
        return f"{base}/digest/discord/{key}/{period.key}.json"
    raise ValueError(f"unknown digest platform {platform!r}")


def load_state(root: str, period: Period, platform: str = "slack", key: str | None = None) -> dict:
    from .. import blobfs

    path = state_path(root, period, platform, key)
    if not blobfs.exists(path):
        return {}
    return json.loads(blobfs.read_text(path))


def save_state(root: str, period: Period, state: dict, platform: str = "slack", key: str | None = None) -> None:
    from .. import blobfs

    blobfs.write_text(state_path(root, period, platform, key), json.dumps(state, indent=2))


# ---- converge lifecycles (generic; unit-tested with fake clients) ----------


def converge_slack(
    profile: DigestProfile,
    rows: list[Row],
    period: Period,
    state: dict,
    *,
    client,
    plot_url: str | None = None,
    save=None,
    reply_delay: float = 0.0,
) -> dict:
    """Bring one period's Slack thread to the desired state; returns ``state``.

    ``client`` is a ``thrds`` ``SlackClient`` (or a fake) with ``post``/``edit``.
    ``plot_url`` is the hosted plot image the OP references (``None`` omits it).
    ``save(state)`` persists after each reply so a spaced backfill is resumable.
    """
    import time

    save = save or (lambda s: None)
    body = profile.op_body(rows, period, plot_url)
    op_ts = state.get("op_ts")
    if op_ts:
        client.edit(op_ts, body)
        _err(f"digest: edited OP {op_ts} ({len(rows)} scans)")
    else:
        op_ts = client.post(body, username=profile.title(period), icon_emoji=profile.op_icon).id
        state["op_ts"] = op_ts
        save(state)
        _err(f"digest: posted OP {op_ts}")
    posted = state.setdefault("posted", {})
    todo = [r for r in rows if r.date not in posted]
    for i, r in enumerate(todo):
        sender, rbody, avatar = profile.reply(r, "slack")
        posted[r.date] = client.post(rbody, thread_id=op_ts, username=sender, icon_url=avatar).id
        save(state)
        _err(f"digest: reply {r.date} -> {posted[r.date]}")
        if reply_delay and i < len(todo) - 1:
            time.sleep(reply_delay)
    save(state)
    return state


def converge_discord(
    profile: DigestProfile,
    rows: list[Row],
    period: Period,
    state: dict,
    *,
    hook,
    bot,
    emoji: dict[str, str],
    plot,
    save=None,
    edit_replies: bool = False,
    reply_hook=None,
) -> dict:
    """Bring one period's Discord thread to the desired state; returns ``state``.

    The Slack twin's shape on Discord's split transports: the OP is a *webhook*
    message (custom sender = the profile's title + ``op_avatar``; the plot rides
    along as a file attachment, re-uploaded on every edit) that the *bot* then
    opens a thread off (webhooks can't); each not-yet-posted scan becomes a
    webhook reply into that thread under its headline sender + avatar.

    ``hook``/``bot`` are ``thrds`` ``DiscordWebhookClient``/``DiscordClient`` (or
    fakes); ``emoji`` maps app-emoji names to ids; ``save(state)`` persists after
    each step so an interrupted run resumes without duplicates. ``edit_replies``
    re-edits every already-posted reply to its current body through
    ``reply_hook`` (a webhook bound to the thread — a webhook edit inside a
    thread must carry the thread id, which the OP-level ``hook`` doesn't)."""
    save = save or (lambda s: None)
    title = profile.title(period)
    body = discordify(profile.op_body(rows, period, None), emoji)
    op_id = state.get("op_id")
    if op_id:
        hook.edit(op_id, body, files=[plot])
        _err(f"digest: edited OP {op_id} ({len(rows)} scans)")
    else:
        op_id = hook.post(body, username=title, icon_url=profile.op_avatar, files=[plot]).id
        state["op_id"] = op_id
        state["thread_id"] = bot.create_thread(op_id, title)
        save(state)
        _err(f"digest: posted OP {op_id}, thread {state['thread_id']}")
    thread_id = state["thread_id"]
    posted = state.setdefault("posted", {})
    for r in rows:
        sender, rbody, avatar = profile.reply(r, "discord")
        if r.date in posted:
            if edit_replies:
                reply_hook.edit(posted[r.date], rbody)
                _err(f"digest: re-edited reply {r.date} ({posted[r.date]})")
            continue
        posted[r.date] = hook.post(rbody, thread_id=thread_id, username=sender, icon_url=avatar).id
        save(state)
        _err(f"digest: reply {r.date} -> {posted[r.date]}")
    save(state)
    return state


# ---- real-client shells (the `notify` extra: thrds) ------------------------


def post_digest_slack(
    profile: DigestProfile,
    root: str,
    period: Period,
    rows: list[Row],
    token: str,
    channel: str,
    *,
    plot_url: str | None = None,
    reply_delay: float = 0.0,
) -> dict:
    """``converge_slack`` against a real Slack workspace: load the period's
    thread state, converge, persist. ``plot_url`` is a hosted image the OP
    references (Slack has no attach-and-reference, so a deployment that wants
    the plot must host it; ``None`` posts text-only)."""
    from thrds.slack import SlackClient

    state = load_state(root, period, "slack")
    return converge_slack(
        profile, rows, period, state,
        client=SlackClient(token, channel),
        plot_url=plot_url,
        reply_delay=reply_delay,
        save=lambda s: save_state(root, period, s, "slack"),
    )


def post_digest_discord(
    profile: DigestProfile,
    root: str,
    period: Period,
    rows: list[Row],
    webhook: str,
    bot_token: str,
    *,
    plot_dir=None,
    edit_replies: bool = False,
) -> dict:
    """``converge_discord`` against real Discord: resolve the webhook's channel,
    load that webhook's period state, render the plot (profile-supplied), and
    converge. No plot-hosting step — the PNG is an attachment."""
    import tempfile
    from pathlib import Path

    from thrds.discord import NO_MENTIONS, DiscordClient, DiscordWebhookClient

    from . import discord_api

    info = discord_api.webhook_info(webhook)
    channel, key = info["channel_id"], info["id"]
    state = load_state(root, period, "discord", key)
    plot = Path(plot_dir or tempfile.gettempdir()) / f"digest-{period.key}.png"
    profile.render_plot(rows, period, plot)
    thread_id = state.get("thread_id")
    return converge_discord(
        profile, rows, period, state,
        hook=DiscordWebhookClient(webhook, suppress_embeds=True, allowed_mentions=NO_MENTIONS),
        reply_hook=DiscordWebhookClient(webhook, thread_id, suppress_embeds=True, allowed_mentions=NO_MENTIONS) if thread_id else None,
        edit_replies=edit_replies,
        bot=DiscordClient(bot_token, channel),
        emoji=discord_api.app_emojis(bot_token),
        plot=plot,
        save=lambda s: save_state(root, period, s, "discord", key),
    )
