"""The `gcs` digest template: a reply per scan, cost by storage class.

Each reply's SENDER is the scan's size headline (date · TB · Δ), its BODY the
$/mo (priced from the scan's storage-class bytes, ``cfg.prices``) + a linked
arrow to the day's diff, its avatar the colour-coded trend arrow
(`av_deg{N}.png?v=REV`). The OP: month-to-date headline, per-ISO-week bullets
(TB + $/mo), and a storage-class mosaic plot (`digest_plot.render_tiers`).
Scan ids are `YYYY-MM-DD` or sub-daily `YYYY-MM-DDTHHMM` (UTC): a day may hold
several scans, each its own reply, delta and link (specs/scan-ids-not-dates.md);
meta.json carries `total_bytes` + `class_bytes`. Design + rationale: specs/done/slack-digest-shape-c.md; the
engine: `digest`."""
from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .digest import AVATAR_REV, GIB, MINUS, TIB, DigestConfig, Reply, Unit, _dlink, _pct, _pct_val, _span, _tb, deg, load_window, scan_slugs, scan_ts


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


def _cost(class_bytes: dict, prices: dict[str, float]) -> float:
    return sum(class_bytes.get(c, 0) / GIB * prices[c] for c in prices)


def rows_from_meta(dated_meta: list[tuple[str, dict]], prices: dict[str, float], now: dt.datetime | None = None) -> list[Scan]:
    """Build ``Scan`` rows from ``(date, meta.json)`` pairs in date order, each with its canonical slug
    (`scan_slugs`, as of ``now``).

    The first pair seeds the delta for the second; callers pass one scan of
    lead-in before the window they want, then slice it off."""
    out: list[Scan] = []
    ptb = pcost = prev = None
    slugs = scan_slugs(dated_meta, now)
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


def op_body(rows: list[Scan], month: dt.date, plot_url: str | None, cfg: DigestConfig) -> str:
    """OP markdown: month-to-date headline, per-week bullets, trailing plot image.

    The month/year title is NOT in the body -- it's folded into the OP's sender
    name by the poster. ``plot_url=None`` omits the image line (Discord attaches
    the plot as a file instead of hosting it)."""
    site_url = cfg.site_url
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
        mon = r.day - dt.timedelta(days=r.day.weekday())
        weeks.setdefault(mon, []).append(r)
    prev_end: Scan | None = None
    last_mon = list(weeks)[-1]
    # the lead-in scan (sliced off `rows`, named by the first row's `prev`) is
    # the first week's baseline; absent one, a day before the first row
    base = scan_ts(rows[0].prev) if rows[0].prev else scan_ts(rows[0].date) - dt.timedelta(days=1)
    for mon, ws in weeks.items():
        end = ws[-1]
        b_tb, b_cost = (prev_end.tb, prev_end.cost) if prev_end is not None else (base_tb, base_cost)
        b_at = scan_ts(prev_end.date) if prev_end is not None else base
        wdtb = end.tb - b_tb
        wpct = wdtb / b_tb * 100 if b_tb else 0
        partial = " _(partial)_" if len({w.day for w in ws}) < 7 and mon == last_mon else ""
        # the link selects exactly this bullet's span on the site (`?d=<end>-<N>d`:
        # the end scan, N days back to the baseline) and lands on the
        # size-over-time chart, where the week shows as the highlighted window
        # with the Diff section right below it
        lines.append(
            f":arrow_deg{deg(wpct)}: [wk of {mon.month}/{mon.day}]({site_url}/?d={_dlink(end.date, end.slug)}-{_span(b_at, scan_ts(end.date))}#over-time){partial} — "
            f"**{end.tb:,.0f} TB** ({_tb(wdtb)}, {_pct(wdtb, end.tb)}%) · ${end.cost:,}/mo ({_usd(end.cost - b_cost)})"
        )
        prev_end = end
    if plot_url is not None:
        lines += ["", f"![{cfg.title} — {month:%B %Y}]({plot_url})"]
    return "\n".join(lines)


def plot_rows(rows: list[Scan]) -> list[dict]:
    """The mosaic's per-day points (`digest_plot.render_tiers` plots days):
    each day's latest scan, keyed by its date."""
    by_day: dict[str, Scan] = {}
    for r in rows:
        by_day[r.date[:10]] = r
    return [{"date": d, "std": r.std, "near": r.near, "cold": r.cold, "arch": r.arch} for d, r in by_day.items()]


def reply(r: Scan, cfg: DigestConfig, platform: str = "slack") -> Reply:
    """One scan's reply.

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
    # A sub-daily scan names its UTC time: a day's scans are distinct replies.
    when = f"{d.month}/{d.day}" + (f" {r.date[11:13]}:{r.date[13:15]}Z" if "T" in r.date else "")
    sender = f"{when} — {r.tb:,.0f} TB ({_tb(dtb)}, {_pct(dtb, r.tb)}%)"
    # ↗︎ = NE arrow + text-presentation selector: renders as a font
    # glyph in link colour (bare ↗ gets emoji-ized by Slack into the
    # cartoonish :arrow_upper_right:)
    url = f"{cfg.site_url}/?d={_dlink(r.date, r.slug)}#diff"
    link = f"· [view →]({url})" if platform == "discord" else f"[↗︎]({url})"
    body = f"${r.cost:,}/mo ({_usd(dcost)}) {link}"
    # project the scan's Δ% over its real interval to a weekly rate (a day: ×7)
    # (a daily scan keeps the fixed ×7 even across a missed day)
    hours = (scan_ts(r.date) - scan_ts(r.prev)).total_seconds() / 3600 if r.prev and "T" in r.date + r.prev else 24
    avatar = f"{cfg.need('icons_base')}/arrows/av_deg{deg(_pct_val(dtb, r.tb), 168 / hours if hours > 0 else 7)}.png?v={AVATAR_REV}"
    return Reply(sender, body, icon_url=avatar)


def load_month(root: str, month: dt.date, prices: dict[str, float]) -> list[Scan]:
    """Per-scan ``Scan`` rows for ``month`` (UTC), from ``root``'s snapshots
    (``gs://<bucket>/snapshots``: one ``<date>/meta.json`` per scan); the
    lead-in scan seeds the first delta and is sliced off. ``[]`` if none."""
    w = load_window(root, month)
    if w is None:
        return []
    lead, in_month = w
    return rows_from_meta(lead + in_month, prices)[len(lead):]


class Gcs:
    """The `gcs` template over a :class:`DigestConfig` (see `digest.Template`)."""

    variants = ("sender",)
    edited_variants = ()
    track_scan = False

    def __init__(self, cfg: DigestConfig):
        self.cfg = cfg

    def load(self, root: str, month: dt.date) -> list[Scan]:
        return load_month(root, month, self.cfg.prices)

    def n_scans(self, rows: list[Scan]) -> int:
        return len(rows)

    def op_body(self, rows: list[Scan], month: dt.date, plot_url: str | None) -> str:
        return op_body(rows, month, plot_url, self.cfg)

    def units(self, rows: list[Scan], variant: str, platform: str = "slack") -> list[Unit]:
        return [Unit(r.date, r.date, reply(r, self.cfg, platform)) for r in rows]

    def provisional(self, rows: list[Scan], variant: str) -> None:
        """A reply per scan is never provisional."""
        return None

    def render_plot(self, rows: list[Scan], month: dt.date, out: Path, root: str | None = None) -> None:
        """The storage-class mosaic (needs the `[plot]` extra — matplotlib)."""
        from .digest_plot import render_tiers

        tiers = plot_rows(rows)
        render_tiers(tiers, Path(out), f"{self.cfg.title} — {month:%B %Y}", self.cfg.host)
