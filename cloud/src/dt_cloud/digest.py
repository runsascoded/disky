"""Shape-C monthly GCS-usage digest -> a Slack thread (`dt-cloud digest`),
or its Discord twin (`dt-cloud digest -P discord`).

One thread per calendar month: an OP (month-to-date headline, per-week rollup
bullets, and a 2-panel mosaic plot) that's edited in place as the month
progresses, plus one reply per scan. Each reply's sender name is the scan's
headline (date . TB . delta), its body the $/mo + a linked arrow to the day's
scan, and its avatar a colour-coded trend arrow (`av_deg{N}.png?v=REV`). Posts
via the `thrds` `SlackClient` (per-message username/icon overrides need a bot
token). Converge state lives in a per-month JSON in the data bucket.

Design + rationale: specs/done/slack-digest-shape-c.md.

The pure content functions (`deg`, `op_body`, `reply`, `rows_from_meta`,
`discordify`) hold all the formatting and are unit-tested; `post_digest` is the
thin side-effecting shell (render+host plot, post/edit OP, post new replies,
persist state). The Discord twin reuses every content function: `converge_discord`
drives thrds's webhook + bot clients (the OP is a webhook message with the plot
attached, the bot opens the thread off it, replies are webhook posts under their
headline sender), and `post_digest_discord` is its shell."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import sys
from collections import OrderedDict
from dataclasses import dataclass

TIB = 1024**4
GIB = 1024**3
# US list $/GiB-mo by GCS storage class id (1 Standard / 2 Nearline / 3 Coldline / 4 Archive).
PRICE = {"1": 0.02, "2": 0.01, "3": 0.004, "4": 0.0012}
# Weekly-halving arrow buckets: |dpct| >= THRESH[i] -> deg (i+1)*10 (capped 80).
THRESH = [0.39, 0.78, 1.5, 3.1, 6.25, 12.5, 25, 50]
MINUS = "−"  # matches the site's unicode minus
DEFAULT_URL = "https://gcs.oa.dev"
ICONS_BASE = "https://gcs-usage-icons.pages.dev"
# bump when the av_deg glyphs change: Slack caches avatars per-URL at post
# time, so a stable URL serves MIXED generations after a redesign.
AVATAR_REV = 4


def deg(pct_signed: float, mult: float = 1.0) -> int:
    """Signed arrow degree for a percent change, time-normalized by ``mult``.

    Anchored on a weekly halving (deg80 ~ +/-50%/week). A daily reply passes
    ``mult=7`` (project the day's rate to a weekly-equivalent), a weekly bullet
    ``mult=1``, month-to-date ``mult=7/days_elapsed`` -- so a daily arrow and a
    weekly arrow mean the same underlying rate."""
    a = abs(pct_signed) * mult
    d = 0
    for i, t in enumerate(THRESH):
        if a >= t:
            d = (i + 1) * 10
    d = min(80, d)
    return -d if pct_signed < 0 else d


def _tb(v: float) -> str:
    return f"+{v:.1f}" if v >= 0 else f"{MINUS}{abs(v):.1f}"


def _usd(v: float) -> str:
    return ("+$" if v >= 0 else f"{MINUS}$") + f"{abs(v):,}"


def _pct(dtb: float, tb: float) -> str:
    prev = tb - dtb
    return f"{abs(dtb / prev * 100) if prev else 0:.1f}"


def _pct_val(dtb: float, tb: float) -> float:
    prev = tb - dtb
    return dtb / prev * 100 if prev else 0.0


def _yy(date: str) -> str:
    return date[2:].replace("-", "")


@dataclass(frozen=True)
class Scan:
    """One scan's row: TiB total + per-class TiB + $/mo, with deltas vs. the
    previous scan (``dtb``/``dcost`` are ``None`` only if no prior scan)."""

    date: str
    tb: float
    cost: int
    dtb: float | None
    dcost: int | None
    std: float
    near: float
    cold: float
    arch: float


def _cost(class_bytes: dict) -> float:
    return sum(class_bytes.get(c, 0) / GIB * PRICE[c] for c in PRICE)


def rows_from_meta(dated_meta: list[tuple[str, dict]]) -> list[Scan]:
    """Build ``Scan`` rows from ``(date, meta.json)`` pairs in date order.

    The first pair seeds the delta for the second; callers pass one scan of
    lead-in before the window they want, then slice it off."""
    out: list[Scan] = []
    ptb = pcost = None
    for date, m in dated_meta:
        tb = m["total_bytes"] / TIB
        cb = m["class_bytes"]
        cost = round(_cost(cb))
        out.append(
            Scan(
                date=date,
                tb=round(tb, 1),
                cost=cost,
                dtb=round(tb - ptb, 1) if ptb is not None else None,
                dcost=cost - pcost if pcost is not None else None,
                std=round(cb.get("1", 0) / TIB, 1),
                near=round(cb.get("2", 0) / TIB, 1),
                cold=round(cb.get("3", 0) / TIB, 1),
                arch=round(cb.get("4", 0) / TIB, 1),
            )
        )
        ptb, pcost = tb, cost
    return out


def op_body(rows: list[Scan], month: dt.date, plot_url: str | None, site_url: str = DEFAULT_URL) -> str:
    """OP markdown: month-to-date headline, per-week bullets, trailing plot image.

    The month/year title is NOT in the body -- it's folded into the OP's sender
    name by the poster. ``plot_url=None`` omits the image line (Discord attaches
    the plot as a file instead of hosting it)."""
    base_tb = rows[0].tb - (rows[0].dtb or 0)
    base_cost = rows[0].cost - (rows[0].dcost or 0)
    mdtb = rows[-1].tb - base_tb
    mweekly = (mdtb / base_tb * 100 * 7 / len(rows)) if base_tb else 0
    lines = [
        f":arrow_deg{deg(mweekly)}: **{_tb(mdtb)} TB** month-to-date · [dashboard]({site_url}/)",
        "",
        "*Weekly summaries*",
    ]
    weeks: OrderedDict[dt.date, list[Scan]] = OrderedDict()
    for r in rows:
        d = dt.date.fromisoformat(r.date)
        mon = d - dt.timedelta(days=d.weekday())
        weeks.setdefault(mon, []).append(r)
    prev_end: Scan | None = None
    last_mon = list(weeks)[-1]
    # the lead-in scan (sliced off `rows`) is the first week's baseline; the
    # daily cadence puts it one day before the first row
    base_date = dt.date.fromisoformat(rows[0].date) - dt.timedelta(days=1)
    for mon, ws in weeks.items():
        end = ws[-1]
        b_tb, b_cost = (prev_end.tb, prev_end.cost) if prev_end is not None else (base_tb, base_cost)
        b_date = dt.date.fromisoformat(prev_end.date) if prev_end is not None else base_date
        wdtb = end.tb - b_tb
        wpct = wdtb / b_tb * 100 if b_tb else 0
        partial = " _(partial)_" if len(ws) < 7 and mon == last_mon else ""
        # the link selects exactly this bullet's span on the site (`?d=<end>-<N>d`:
        # the end scan, N days back to the baseline) and lands on the
        # size-over-time chart, where the week shows as the highlighted window
        # with the Diff section right below it
        span = (dt.date.fromisoformat(end.date) - b_date).days
        lines.append(
            f":arrow_deg{deg(wpct)}: [wk of {mon.month}/{mon.day}]({site_url}/?d={_yy(end.date)}-{span}d#over-time){partial} — "
            f"**{end.tb:,.0f} TB** ({_tb(wdtb)}, {_pct(wdtb, end.tb)}%) · ${end.cost:,}/mo ({_usd(end.cost - b_cost)})"
        )
        prev_end = end
    if plot_url is not None:
        lines += ["", f"![GCS usage — {month:%B %Y}]({plot_url})"]
    return "\n".join(lines)


def reply(r: Scan, site_url: str = DEFAULT_URL, platform: str = "slack") -> tuple[str, str, str]:
    """One scan's reply -> (sender_username, body, avatar_url).

    Style B, mobile-first: the SENDER is the size headline (bold, plain text --
    Slack renders no links/emoji/markdown there), sized to not wrap on a phone;
    the BODY is one line: the cost + a link to the day's Diff section at EOL.
    The link text is per platform: Slack renders the bare ↗︎ glyph
    fine, Discord's is too small to notice, so there it reads "view →"
    (picked from a dozen candidates on 2026-09-15).
    The avatar is the day's colour-coded trend arrow (URL carries AVATAR_REV --
    Slack caches avatars per-URL, so glyph redesigns must bust it)."""
    d = dt.date.fromisoformat(r.date)
    dtb = r.dtb or 0
    dcost = r.dcost or 0
    sender = f"{d.month}/{d.day} — {r.tb:,.0f} TB ({_tb(dtb)}, {_pct(dtb, r.tb)}%)"
    # \u2197\ufe0e = NE arrow + text-presentation selector: renders as a font
    # glyph in link colour (bare \u2197 gets emoji-ized by Slack into the
    # cartoonish :arrow_upper_right:)
    url = f"{site_url}/?d={_yy(r.date)}#diff"
    link = f"\u00b7 [view \u2192]({url})" if platform == "discord" else f"[\u2197\ufe0e]({url})"
    body = f"${r.cost:,}/mo ({_usd(dcost)}) {link}"
    avatar = f"{ICONS_BASE}/arrows/av_deg{deg(_pct_val(dtb, r.tb), 7)}.png?v={AVATAR_REV}"
    return sender, body, avatar


_EMOJI_RE = re.compile(r":arrow_deg(-?\d+):")


def emoji_name(d: int) -> str:
    """Discord application-emoji name for a signed arrow degree. Discord emoji
    names are ``[A-Za-z0-9_]`` only (no ``-``), so a negative reads
    ``arrow_degm30`` where the Slack custom emoji is ``arrow_deg-30``."""
    return f"arrow_deg{d}" if d >= 0 else f"arrow_degm{-d}"


def discordify(text: str, emoji: dict[str, str]) -> str:
    """Rewrite the Slack ``:arrow_degN:`` shortcodes in ``text`` to Discord's
    ``<:name:id>`` form via ``emoji`` (app-emoji name -> id; uploaded by
    `dt-cloud discord-emoji`). The rest of the markdown (bold, italics, masked
    links) renders the same on both platforms."""
    def sub(m: re.Match) -> str:
        name = emoji_name(int(m.group(1)))
        if name not in emoji:
            raise ValueError(f"discord app emoji {name!r} missing — run `dt-cloud discord-emoji` to upload the arrow set")
        return f"<:{name}:{emoji[name]}>"
    return _EMOJI_RE.sub(sub, text)


# ---- IO (side-effecting) --------------------------------------------------


def _err(*a) -> None:
    print(*a, file=sys.stderr)


def load_month(root: str, month: dt.date) -> list[Scan]:
    """Per-scan ``Scan`` rows for ``month`` (UTC), read from ``root`` snapshots.

    ``root`` = ``gs://<bucket>/snapshots``. Lists the scan dates (one
    ``meta.json`` per published scan), keeps the month's dates plus one lead-in
    scan for the first delta, reads each scan's ``meta.json``, then slices the
    lead-in off."""
    import re

    import fsspec

    fs, _, _ = fsspec.get_fs_token_paths(root)
    dates = sorted(
        m.group(1)
        for p in fs.glob(f"{root.split('://', 1)[-1]}/*/meta.json")
        if (m := re.search(r"/(\d{4}-\d{2}-\d{2})/meta\.json$", p))
    )
    pfx = f"{month:%Y-%m}-"
    in_month = [d for d in dates if d.startswith(pfx)]
    if not in_month:
        return []
    first_idx = dates.index(in_month[0])
    window = dates[max(0, first_idx - 1) : dates.index(in_month[-1]) + 1]
    dated_meta: list[tuple[str, dict]] = []
    for d in window:
        with fsspec.open(f"{root}/{d}/meta.json", "rt") as f:
            dated_meta.append((d, json.load(f)))
    rows = rows_from_meta(dated_meta)
    return rows[1:] if first_idx > 0 else rows


def _wait_reachable(url: str, timeout: float = 90, interval: float = 3) -> None:
    """Block until ``url`` serves 200 (Pages CDN propagation after a deploy)."""
    import time
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "gcs-usage-digest/1.0"})
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(interval)
    _err(f"digest: WARN {url} not reachable after {timeout:.0f}s — posting anyway")


def _state_path(root: str, month: dt.date, platform: str = "slack", key: str | None = None) -> str:
    """Converge-state JSON for one month's thread. Slack: ``digest/<YYYY-MM>.json``
    (one prod thread). Discord: ``digest/discord/<webhook_id>/<YYYY-MM>.json`` —
    keyed by webhook because a webhook can only edit its own messages, so the
    OP/replies it recorded are only reachable through it (and a staging webhook
    never masquerades as the prod thread)."""
    base = root.rsplit("/snapshots", 1)[0]
    if platform == "slack":
        return f"{base}/digest/{month:%Y-%m}.json"
    if platform == "discord":
        if not key:
            raise ValueError("discord digest state is keyed by webhook id")
        return f"{base}/digest/discord/{key}/{month:%Y-%m}.json"
    raise ValueError(f"unknown digest platform {platform!r}")


def load_state(root: str, month: dt.date, platform: str = "slack", key: str | None = None) -> dict:
    import fsspec

    try:
        with fsspec.open(_state_path(root, month, platform, key), "rt") as f:
            return json.load(f)
    except (FileNotFoundError, OSError):
        return {}


def save_state(root: str, month: dt.date, state: dict, platform: str = "slack", key: str | None = None) -> None:
    import fsspec

    with fsspec.open(_state_path(root, month, platform, key), "wt") as f:
        json.dump(state, f, indent=2)


def render_plot(rows: list[Scan], month: dt.date, out_path) -> None:
    """Render the mosaic PNG for ``rows`` to ``out_path`` (in-process; needs
    the `[plot]` extra — matplotlib)."""
    from pathlib import Path

    from .digest_plot import render

    tiers = [{"date": r.date, "std": r.std, "near": r.near, "cold": r.cold, "arch": r.arch} for r in rows]
    render(tiers, Path(out_path), f"GCS usage — {month:%B %Y}")


def post_digest(root, month, token, channel, site_url=DEFAULT_URL, icons_dir=None, deploy_plot=None, reply_delay=0.0) -> dict:
    """Converge the month's thread: render+host the plot, post/edit the OP, post
    one reply per not-yet-posted scan, persist and return state. ``icons_dir`` is
    where to write the PNG; ``deploy_plot(local_png, basename)`` publishes it.
    ``reply_delay`` sleeps that many seconds between replies (>0 for a spaced
    backfill, so Slack doesn't collapse the per-reply sender chrome)."""
    import time
    from pathlib import Path

    from thrds.slack import SlackClient

    rows = load_month(root, month)
    if not rows:
        _err(f"digest: no scans for {month:%Y-%m}")
        return {}
    state = load_state(root, month)
    client = SlackClient(token, channel)

    plot_name = state.get("plot_name") or f"plot-{secrets.token_hex(16)}.png"
    base = ICONS_BASE
    if icons_dir is not None:
        local = Path(icons_dir) / plot_name
        render_plot(rows, month, local)
        if deploy_plot is not None:
            # the deployment-specific host serves the just-uploaded plot
            # immediately (no root-alias propagation race → no invalid_blocks)
            dep = deploy_plot(local, plot_name)
            if dep:
                base = dep
    plot_url = f"{base}/{plot_name}?v={int(dt.datetime.now(dt.timezone.utc).timestamp())}"
    state["plot_name"] = plot_name
    # A just-deployed Pages asset isn't instantly served at the root alias; if we
    # post before it propagates, Slack's image-block validation 500s the whole
    # message with `invalid_blocks`. Poll until the URL is live (or give up + warn).
    if icons_dir is not None and deploy_plot is not None:
        _wait_reachable(plot_url)

    body = op_body(rows, month, plot_url, site_url)
    op_ts = state.get("op_ts")
    if op_ts:
        client.edit(op_ts, body)
        _err(f"digest: edited OP {op_ts} ({len(rows)} scans)")
    else:
        m = client.post(body, username=f"GCS usage — {month:%B %Y}", icon_emoji=":calendar:")
        op_ts = m.id
        state["op_ts"] = op_ts
        _err(f"digest: posted OP {op_ts}")

    posted = state.setdefault("posted", {})
    todo = [r for r in rows if r.date not in posted]
    for i, r in enumerate(todo):
        sender, rbody, avatar = reply(r, site_url)
        rm = client.post(rbody, thread_id=op_ts, username=sender, icon_url=avatar)
        posted[r.date] = rm.id
        save_state(root, month, state)   # persist after each → a spaced backfill is resumable
        _err(f"digest: reply {r.date} -> {rm.id}")
        if reply_delay and i < len(todo) - 1:
            time.sleep(reply_delay)

    save_state(root, month, state)
    return state


# ---- Discord twin ---------------------------------------------------------

CALENDAR_URL = f"{ICONS_BASE}/calendar.png?v=2"  # the OP sender's avatar (Slack uses :calendar:); ?v busts Discord's per-URL avatar cache


def converge_discord(rows: list[Scan], month: dt.date, state: dict, *, hook, bot, emoji: dict[str, str], plot, site_url: str = DEFAULT_URL, save=None, edit_replies: bool = False, reply_hook=None) -> dict:
    """Bring one month's Discord thread to the desired state; returns ``state``.

    The Slack twin's shape on Discord's split transports: the OP is a *webhook*
    message (custom sender = month title + calendar avatar; the plot rides
    along as a file attachment, re-uploaded on every edit) that the *bot* then
    opens a thread off (webhooks can't); each not-yet-posted scan becomes a
    webhook reply into that thread under its headline sender + trend-arrow
    avatar. Discord groups consecutive messages by *displayed* sender and every
    headline differs, so replies need no spacing (Slack needs ~5 min).

    ``hook``/``bot`` are thrds's `DiscordWebhookClient`/`DiscordClient` (or
    fakes), ``emoji`` maps app-emoji names to ids, ``save(state)`` persists
    after each step so an interrupted run resumes without duplicates.
    ``edit_replies`` re-edits every already-posted reply to its current body
    (a backfill after a format change) through ``reply_hook``, a webhook client
    bound to the thread — a webhook edit inside a thread must carry the thread
    id, which the OP-level ``hook`` doesn't."""
    save = save or (lambda s: None)
    title = f"GCS usage — {month:%B %Y}"
    body = discordify(op_body(rows, month, None, site_url), emoji)
    op_id = state.get("op_id")
    if op_id:
        hook.edit(op_id, body, files=[plot])
        _err(f"digest: edited OP {op_id} ({len(rows)} scans)")
    else:
        op_id = hook.post(body, username=title, icon_url=CALENDAR_URL, files=[plot]).id
        state["op_id"] = op_id
        state["thread_id"] = bot.create_thread(op_id, title)
        save(state)
        _err(f"digest: posted OP {op_id}, thread {state['thread_id']}")
    thread_id = state["thread_id"]
    posted = state.setdefault("posted", {})
    for r in rows:
        sender, rbody, avatar = reply(r, site_url, "discord")
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


def post_digest_discord(root: str, month: dt.date, webhook: str, bot_token: str, site_url: str = DEFAULT_URL, plot_dir=None, edit_replies: bool = False) -> dict:
    """`converge_discord` against real Discord: resolve the webhook's channel,
    load that webhook's month state, render the plot, converge, persist. There
    is no plot-hosting step — the PNG is an attachment."""
    import tempfile
    from pathlib import Path

    from thrds.discord import NO_MENTIONS, DiscordClient, DiscordWebhookClient

    from . import discord_api

    rows = load_month(root, month)
    if not rows:
        _err(f"digest: no scans for {month:%Y-%m}")
        return {}
    info = discord_api.webhook_info(webhook)
    channel, key = info["channel_id"], info["id"]
    state = load_state(root, month, "discord", key)
    plot = Path(plot_dir or tempfile.gettempdir()) / f"gcs-usage-{month:%Y-%m}.png"
    render_plot(rows, month, plot)
    thread_id = state.get("thread_id")
    return converge_discord(
        rows, month, state,
        hook=DiscordWebhookClient(webhook, suppress_embeds=True, allowed_mentions=NO_MENTIONS),
        reply_hook=DiscordWebhookClient(webhook, thread_id, suppress_embeds=True, allowed_mentions=NO_MENTIONS) if thread_id else None,
        edit_replies=edit_replies,
        bot=DiscordClient(bot_token, channel),
        emoji=discord_api.app_emojis(bot_token),
        plot=plot,
        site_url=site_url,
        save=lambda s: save_state(root, month, s, "discord", key),
    )
