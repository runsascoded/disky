"""The `gcs` digest template: a reply per day, cost by storage class.

A day's reply is over its sender scan (`senders`): its first scan not started by hand (`Scan.trigger`, the site's
`digest_cw.site_triggers`); a manual scan stands in only for a day without one, once a later day has a scan (or the
month is over). Deltas run sender to sender.

Each reply's SENDER is the scan's size headline (date · TiB · Δ), its BODY the
$/mo (priced from the scan's storage-class bytes, ``cfg.prices``) + a linked
arrow to the day's diff, its avatar the colour-coded trend arrow
(`av_deg{N}.png?v=REV`). The OP: month-to-date headline beside the last 30 days' change, per-ISO-week bullets
(TiB + $/mo) over its window (the longer of the last 30 days and the month to date, specs/digest-rolling-window.md),
the previous month's thread, and a storage-class image over the window drawn by the site (`POST /api/digest-image`,
`kind: tiers`, with the month-to-date diff map).
Scan ids are `YYYY-MM-DD` or sub-daily `YYYY-MM-DDTHHMM` (UTC): a day may hold
several scans, its sender's link its own (specs/done/scan-ids-not-dates.md);
meta.json carries `total_bytes` + `class_bytes`. Design + rationale: specs/done/slack-digest-shape-c.md; the
engine: `digest`."""
from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .digest import (
    AVATAR_REV, GIB, LINK_ARROW, MINUS, TIB, DigestConfig, PlotUnavailable, Reply, Unit, _dlink, _pct, _pct_val, _span, _tb, deg,
    list_scans, load_window, prev_month_link, read_metas, scan_slugs, scan_ts, week_of, window_label, window_scans, window_start,
)


def _usd(v: float) -> str:
    return ("+$" if v >= 0 else f"{MINUS}$") + f"{abs(v):,}"


@dataclass(frozen=True)
class Scan:
    """One scan's row: TiB total + per-class TiB + $/mo, with deltas vs. the
    previous scan (``dtb``/``dcost`` are ``None`` only if no prior scan)."""

    date: str  # the scan id (`YYYY-MM-DD[THHMM]`), not necessarily a bare date
    tb: float
    cost: int
    dtb: float | None
    dcost: int | None
    std: float
    near: float
    cold: float
    arch: float
    # the previous scan's id (the delta's baseline); None only if no prior scan
    prev: str | None = None
    # its canonical `?d=` slug (`scan_slugs`); None: its minute
    slug: str | None = field(default=None, compare=False)

    @property
    def day(self) -> dt.date:
        return dt.date.fromisoformat(self.date[:10])


@dataclass(frozen=True)
class Month:
    """A month's per-day rows (``rows``, each a reply) and the OP window's days before it (``pre``, `window_scans`:
    its earlier weekly summaries, look-back figure and plot; the first's delta over the day before)."""

    rows: list[Scan]
    pre: list[Scan] = field(default_factory=list)

    def window(self, month: dt.date) -> tuple[dt.date, list[Scan]]:
        """The OP window's first day (`window_start`, to the latest row) and every row this holds, in order."""
        return window_start(month, self.rows[-1].day), self.pre + self.rows

    def back(self, month: dt.date) -> tuple[float, str]:
        """The window's baseline (its look-back figure's, its diff map's start), as ``(TiB, scan id)``: the last row
        before its first day, else the earliest row's own baseline (a store younger than the window)."""
        start, seen = self.window(month)
        before = [r for r in seen if r.day < start]
        if before:
            return before[-1].tb, before[-1].date
        return seen[0].tb - (seen[0].dtb or 0), seen[0].prev or seen[0].date


def _cost(class_bytes: dict, prices: dict[str, float]) -> float:
    return sum(class_bytes.get(c, 0) / GIB * prices[c] for c in prices)


def rows_from_meta(dated_meta: list[tuple[str, dict]], prices: dict[str, float], now: dt.datetime | None = None,
                   slugs: Mapping[str, str] | None = None) -> list[Scan]:
    """Build ``Scan`` rows from ``(date, meta.json)`` pairs in date order, each with its canonical slug
    (``slugs``; default `scan_slugs` over the pairs, as of ``now``).

    The first pair seeds the delta for the second; callers pass one scan of
    lead-in before the window they want, then slice it off."""
    out: list[Scan] = []
    ptb = pcost = prev = None
    slugs = scan_slugs(dated_meta, now) if slugs is None else slugs
    for date, m in dated_meta:
        tb = m["total_bytes"] / TIB
        cb = m["class_bytes"]
        cost = round(_cost(cb, prices))
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
                prev=prev,
                slug=slugs[date],
            )
        )
        ptb, pcost, prev = tb, cost, date
    return out


def op_body(data: Month, month: dt.date, plot_url: str | None, cfg: DigestConfig, prev_url: str | None = None) -> str:
    """OP markdown: month-to-date headline beside the window's look-back figure, per-week bullets over the window
    (weeks ending before the month marked with its predecessor's name), the previous month's thread (``prev_url``;
    none: no link), trailing plot image.

    The month/year title is NOT in the body -- it's folded into the OP's sender
    name by the poster. ``plot_url=None`` omits the image line (Discord attaches
    the plot as a file instead of hosting it)."""
    site_url, rows = cfg.site_url, data.rows
    base_tb = rows[0].tb - (rows[0].dtb or 0)
    base_cost = rows[0].cost - (rows[0].dcost or 0)
    mdtb = rows[-1].tb - base_tb
    mweekly = (mdtb / base_tb * 100 * 7 / len(rows)) if base_tb else 0
    start, seen = data.window(month)
    # the look-back figure, over the window
    b30_tb, b30_at = data.back(month)
    last = rows[-1]
    b30_url = f"{site_url}/?d={_dlink(last.date, last.slug)}-{_span(scan_ts(b30_at), scan_ts(last.date))}#over-time"
    # (none when it starts where month-to-date does: a store whose first scan is this month's)
    look = f"**{_tb(last.tb - b30_tb)} TiB** [{window_label(start, b30_at)}]({b30_url}) · " if b30_at != (rows[0].prev or rows[0].date) else ""
    lines = [
        f":arrow_deg{deg(mweekly)}: **{_tb(mdtb)} TiB** month-to-date · {look}[dashboard]({site_url}/)",
        "",
        "*Weekly summaries*",
    ]
    weeks: OrderedDict[dt.date, list[Scan]] = OrderedDict()
    for r in seen:
        if r.day >= week_of(start):
            weeks.setdefault(week_of(r.day), []).append(r)
    prev_end: Scan | None = None
    last_mon = list(weeks)[-1]
    first = next(iter(weeks.values()))[0]
    # the first week's baseline: its first row's delta baseline (`prev`; its values: the row's less its deltas);
    # absent one, a day before that row
    base = scan_ts(first.prev) if first.prev else scan_ts(first.date) - dt.timedelta(days=1)
    w_tb, w_cost = first.tb - (first.dtb or 0), first.cost - (first.dcost or 0)
    for mon, ws in weeks.items():
        end = ws[-1]
        if end is first and first.dtb is None:
            # a week holding only the store's first scan: nothing before it to measure from
            continue
        b_tb, b_cost = (prev_end.tb, prev_end.cost) if prev_end is not None else (w_tb, w_cost)
        b_at = scan_ts(prev_end.date) if prev_end is not None else base
        wdtb = end.tb - b_tb
        wpct = wdtb / b_tb * 100 if b_tb else 0
        partial = " _(partial)_" if len({w.day for w in ws}) < 7 and mon == last_mon else ""
        # a week over before the month began, named for the month its last scan fell in (the previous one's, but
        # for a week straddling the month before that)
        earlier = f" ({end.day:%b})" if mon + dt.timedelta(days=6) < month.replace(day=1) else ""
        # the link selects exactly this bullet's span on the site (`?d=<end>-<N>d`:
        # the end scan, N days back to the baseline) and lands on the
        # size-over-time chart, where the week shows as the highlighted window
        # with the Diff section right below it
        lines.append(
            f":arrow_deg{deg(wpct)}: [wk of {mon.month}/{mon.day}]({site_url}/?d={_dlink(end.date, end.slug)}-{_span(b_at, scan_ts(end.date))}#over-time){earlier}{partial} — "
            f"**{end.tb:,.0f} TiB** ({_tb(wdtb)}, {_pct(wdtb, end.tb)}%) · ${end.cost:,}/mo ({_usd(end.cost - b_cost)})"
        )
        prev_end = end
    if prev_url:
        lines += ["", *prev_month_link(month, prev_url)]
    if plot_url is not None:
        lines += ["", f"![{cfg.title} — {month:%B %Y}]({plot_url})"]
    return "\n".join(lines)


# the image's tiers, bottom → top (cold → hot), as `Scan` fields
TIERS = (("arch", "Archive"), ("cold", "Coldline"), ("near", "Nearline"), ("std", "Standard"))


def image_body(data: Month, month: dt.date, cfg: DigestConfig) -> dict:
    """`POST /api/digest-image`'s request for ``month`` (`kind: tiers`): each day's sender's bytes per storage
    class, bottom → top (cold → hot), over the OP's window from the last day before it (`from`: its first day, the
    site shades the month after it), drawn as the total line over the stacked classes, over the window's diff map
    (its baseline, `Month.back` → the latest; none until there are two scans)."""
    rows = data.rows
    start, seen = data.window(month)
    before = [r for r in seen if r.day < start]
    by_day: dict[str, Scan] = {}
    for r in before[-1:] + [r for r in seen if r.day >= start]:
        by_day[r.date[:10]] = r
    body = {
        "kind": "tiers",
        "title": f"{cfg.title} — {month:%B %Y}",
        "note": f"{cfg.host} · TiB",
        "month": f"{month:%Y-%m}",
        "from": start.isoformat(),
        "units": "TiB",
        "tiers": [name for _, name in TIERS],
        "points": [{"scan": r.date, "bytes": [round(getattr(r, k) * TIB) for k, _ in TIERS]} for r in by_day.values()],
    }
    base, last = data.back(month)[1], rows[-1].date
    if base < last:
        a, z = scan_ts(base), scan_ts(last)
        body["diff"] = {"from": base, "to": last, "path": "", "title": f"What changed — {a.month}/{a.day} → {z.month}/{z.day}"}
    return body


def senders(scans: list[str], triggers: Mapping[str, str], closed: bool = False) -> list[str]:
    """Each day's sender among ``scans`` (sorted ids): its first scan whose trigger (``triggers``, `scan_triggers`) isn't
    `manual` (unrecorded counts as scheduled); a day with only manual scans its last, once a later day has a scan or
    the month is ``closed`` (until then the day has none: its scheduled scan may yet land)."""
    days: OrderedDict[str, list[str]] = OrderedDict()
    for s in scans:
        days.setdefault(s[:10], []).append(s)
    out = []
    for i, ss in enumerate(days.values()):
        if (s := next((s for s in ss if triggers.get(s) != "manual"), None)) is not None:
            out.append(s)
        elif i + 1 < len(days) or closed:
            out.append(ss[-1])
    return out


def reply(r: Scan, cfg: DigestConfig, platform: str = "slack") -> Reply:
    """One day's reply, over its sender scan ``r``.

    Style B, mobile-first: the SENDER is the size headline (bold, plain text --
    Slack renders no links/emoji/markdown there), sized to not wrap on a phone;
    the BODY is one line: the cost + a link to the day's Diff section at EOL.
    The link text is per platform: Slack renders the bare ↗︎ glyph
    fine, Discord's is too small to notice, so there it reads "view →"
    (picked from a dozen candidates on 2026-09-15).
    The avatar is the day's colour-coded trend arrow (URL carries AVATAR_REV --
    Slack caches avatars per-URL, so glyph redesigns must bust it)."""
    d = r.day
    dtb = r.dtb or 0
    dcost = r.dcost or 0
    sender = f"{d.month}/{d.day} — {r.tb:,.0f} TiB ({_tb(dtb)}, {_pct(dtb, r.tb)}%)"
    url = f"{cfg.site_url}/?d={_dlink(r.date, r.slug)}#diff"
    link = f"· [view →]({url})" if platform == "discord" else f"[{LINK_ARROW}]({url})"
    body = f"${r.cost:,}/mo ({_usd(dcost)}) {link}"
    # project the scan's Δ% over its real interval to a weekly rate (a day: ×7)
    # (a daily scan keeps the fixed ×7 even across a missed day)
    hours = (scan_ts(r.date) - scan_ts(r.prev)).total_seconds() / 3600 if r.prev and "T" in r.date + r.prev else 24
    avatar = f"{cfg.need('icons_base')}/arrows/av_deg{deg(_pct_val(dtb, r.tb), 168 / hours if hours > 0 else 7)}.png?v={AVATAR_REV}"
    return Reply(sender, body, icon_url=avatar)


def load_month(root: str, month: dt.date, prices: dict[str, float], triggers: Mapping[str, str] | None = None) -> Month | None:
    """Per-day ``Scan`` rows for ``month`` (UTC), over each day's sender (`senders`, by ``triggers``), from ``root``'s
    snapshots (``gs://<bucket>/snapshots``: one ``<id>/meta.json`` per scan), each delta vs. the day before's sender,
    and the OP window's days before it (``pre``, `window_scans`); the earliest day's seeds the first delta and is
    sliced off. None if the month has no scans."""
    scans = list_scans(root)
    w = load_window(root, month, scans)
    if w is None:
        return None
    lead, in_month = w
    pairs = read_metas(root, window_scans(scans, month, (lead or in_month)[0][0])) + lead + in_month
    keep = set(senders([s for s, _ in pairs], triggers or {}, closed=scans[-1] > in_month[-1][0]))
    rows = rows_from_meta([p for p in pairs if p[0] in keep], prices, slugs=scan_slugs(pairs))
    n_before = len([s for s, _ in pairs[: len(pairs) - len(in_month)] if s in keep])
    # the earliest day only seeds the next one's delta
    return Month(rows=rows[n_before:], pre=rows[1:n_before])


class Gcs:
    """The `gcs` template over a :class:`DigestConfig` (see `digest.Template`)."""

    variants = ("sender",)
    edited_variants = ()
    track_scan = False

    def __init__(self, cfg: DigestConfig):
        self.cfg = cfg

    def load(self, root: str, month: dt.date) -> Month | None:
        from . import digest_cw

        return load_month(root, month, self.cfg.prices, digest_cw.site_triggers(self.cfg))

    def n_scans(self, data: Month) -> int:
        return len(data.rows)

    def op_body(self, data: Month, month: dt.date, plot_url: str | None, prev_url: str | None = None) -> str:
        return op_body(data, month, plot_url, self.cfg, prev_url)

    def units(self, data: Month, variant: str, platform: str = "slack") -> list[Unit]:
        return [Unit(r.date[:10], r.date, reply(r, self.cfg, platform)) for r in data.rows]

    def provisional(self, data: Month, variant: str) -> None:
        """A day's reply is posted once its sender is known: never provisional."""
        return None

    def alert(self, data: Month) -> None:
        """No quotas: never a quota alert."""
        return None

    def render_plot(self, data: Month, month: dt.date, out: Path, root: str | None = None) -> None:
        """The OP's image, drawn by the site (`POST /api/digest-image`, `kind: tiers`): the month's total line over
        its stacked storage classes. Authenticated as the job (`$SITE_TOKEN`); ``root`` is unused."""
        from .deploy import site_token
        from .site import post_json

        token = site_token()
        if not token:
            raise PlotUnavailable("digest image: no site token ($SITE_TOKEN)")
        Path(out).write_bytes(post_json(self.cfg.site_url, token, "/api/digest-image", image_body(data, month, self.cfg)))
