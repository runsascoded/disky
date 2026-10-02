"""The `cw` digest template: a reply per UTC day, quota headroom per bucket.

The scan covers every bucket of a store (specs/done/cw-multi-bucket.md); the
headline, deltas and quota line are the PRIMARY bucket's (``cfg.primary``,
read from `meta.buckets[<primary>]` when the scan has it, else the flat
totals, which were the primary's), the others ride along. The OP: month-to-
date headline (`· NN% of <quota>` when the primary has one, ` · <bucket> <TiB>
TiB (Δ)` per other bucket), per-ISO-week bullets, and a quota sparkline + diff
treemap (`digest_plot.render_quota`). ONE REPLY PER UTC DAY, Δ over the prior
day's reply scan, the arrow normalised to the real interval; its tail is a
linked `[<label>](<over-time>): NN.N% of <quota> (<free> Ti free)` clause per
bucket (`cfg.buckets`; a bucket without a quota shows raw TiB).

Two reply VARIANTS because Slack fixes a message's username + icon at post
time (`chat.update` can't change them):
- ``sender``: the headline IS the sender name, the arrow the avatar. Posted
  once, from the day's MORNING scan — the first at/after ``cfg.reply_hour``
  UTC (12:01Z = 8:01 am ET, the same calendar date in both zones) — with a
  ~24 h delta to the prior day's reply scan; the 00:01Z scan only
  re-converges the OP + plot.
- ``body``: the headline is bold body text under a static sender/avatar, so
  the day's reply is EDITED whenever a later scan of the day lands — text and
  sparkline agree intra-day (at the cost of the first edit's Δ spanning 12 h
  until the day's last scan makes it 24 h).

Content + deltas: specs/cw-slack-digest.md; the engine: `digest`."""
from __future__ import annotations

import datetime as dt
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .digest import (
    AVATAR_REV, HOURS_PER_WEEK, TIB, DigestConfig, Quota, Reply, Unit,
    _dlink, _md, _pct, _pct_val, _span, _tb, deg, load_window, scan_ts,
)

VARIANTS = ("sender", "body")


def _quota(tb: float, q: Quota | None) -> str:
    """` · NN.N% of <quota>` for a TiB total; `''` without a quota."""
    return f" · {tb / (q.bytes / TIB) * 100:.1f}% of {q.name}" if q else ""


def _extras(extra: dict[str, float], dextra: dict[str, float | None]) -> str:
    """` · <bucket> <TiB> TiB (Δ)` per non-primary bucket (Δ omitted when the
    prior scan lacked the bucket); `''` with none. The OP + weekly bullets'
    form; the daily reply uses `_tail` (per-bucket quota clauses)."""
    out = ""
    for b, tb in extra.items():
        d = dextra.get(b)
        out += f" · {b} {tb:,.0f} TiB" + (f" ({_tb(d)})" if d is not None else "")
    return out


def _qlabel(b: int) -> str:
    """SI quota label: 1 PB → `1P`, 100 TB → `100T`."""
    return f"{b / 10**15:g}P" if b >= 10**15 else f"{b / 10**12:g}T"


def _diff_url(scan: str, since: dt.datetime | None, site_url: str, bucket: str = "") -> str:
    """The dashboard's over-time view (optionally scoped to ``bucket``) pinned
    to ``scan``, looking back to ``since`` (None: the baked previous scan)."""
    span = f"-{_span(since, scan_ts(scan))}" if since is not None else ""
    return f"{site_url}/{bucket}?d={_dlink(scan)}{span}#over-time"


def _bucket_clause(bucket: str, tb: float, scan: str, since: dt.datetime | None, cfg: DigestConfig) -> str:
    """`[<label>](<over-time url>): NN.N% of <quota> (<free> Ti free)` for one
    bucket; a bucket with no known quota renders its raw TiB."""
    b = cfg.buckets.get(bucket)
    label = (b.label if b else None) or bucket
    url = _diff_url(scan, since, cfg.site_url, bucket)
    q = b.quota if b else None
    if q is None:
        return f"[{label}]({url}): {tb:,.0f} Ti"
    qt = q.bytes / TIB
    return f"[{label}]({url}): {tb / qt * 100:.1f}% of {q.short or _qlabel(q.bytes)} ({qt - tb:,.1f} Ti free)"


def _tail(day: DayRow, cfg: DigestConfig) -> str:
    """The daily reply's per-bucket tail: the primary then each extra bucket as
    a linked `% of quota (free)` clause, ` · `-joined."""
    clauses = [_bucket_clause(cfg.primary, day.tb, day.scan, day.since, cfg)]
    for b, tb in day.extra.items():
        clauses.append(_bucket_clause(b, tb, day.scan, day.since, cfg))
    return " · ".join(clauses)


@dataclass(frozen=True)
class Scan:
    """One scan's row: TiB total + object count, with deltas vs. the previous
    scan (``dtb``/``dobjs``/``hours`` are ``None`` only if no prior scan)."""

    scan: str
    tb: float
    objs: int
    dtb: float | None
    dobjs: int | None
    hours: float | None
    # the non-primary buckets' TiB (`meta.buckets` minus the primary), in
    # meta order; empty on single-bucket scans
    extra: dict[str, float] = field(default_factory=dict)

    @property
    def date(self) -> str:
        return self.scan[:10]


def primary_totals(m: dict, primary: str) -> tuple[int, int, dict[str, float]]:
    """A meta.json's ``(total_bytes, total_objects, extra)`` for the digest:
    the primary bucket's totals from ``buckets`` when present (else the flat
    totals), ``extra`` = the other buckets' TiB."""
    bk = m.get("buckets") or {}
    if primary in bk:
        p = bk[primary]
        return int(p["total_bytes"]), int(p["total_objects"]), {b: v["total_bytes"] / TIB for b, v in bk.items() if b != primary}
    return int(m["total_bytes"]), int(m["total_objects"]), {}


def rows_from_meta(dated_meta: list[tuple[str, dict]], primary: str) -> list[Scan]:
    """Build ``Scan`` rows from ``(scan_id, meta.json)`` pairs in scan order."""
    out: list[Scan] = []
    ptb = pobjs = pts = None
    for scan, m in dated_meta:
        ts = scan_ts(scan)
        b, objs, extra = primary_totals(m, primary)
        tb = b / TIB
        out.append(
            Scan(
                scan=scan,
                tb=round(tb, 1),
                objs=objs,
                dtb=round(tb - ptb, 1) if ptb is not None else None,
                dobjs=objs - pobjs if pobjs is not None else None,
                hours=(ts - pts).total_seconds() / 3600 if pts is not None else None,
                extra={k: round(v, 1) for k, v in extra.items()},
            )
        )
        ptb, pobjs, pts = tb, objs, ts
    return out


def _dextra(cur: Scan, prev: Scan | None) -> dict[str, float | None]:
    """Per extra bucket, Δ TiB vs ``prev`` (None when ``prev`` lacks it)."""
    return {b: round(tb - prev.extra[b], 1) if prev and b in prev.extra else None for b, tb in cur.extra.items()}


@dataclass(frozen=True)
class Month:
    """A month's scans: ``rows`` (in-month, chronological) and ``lead`` (every
    scan of the last calendar day before the month — the baseline for the
    first delta of either reply variant; empty for the first month ever)."""

    lead: list[Scan]
    rows: list[Scan]

    @property
    def base(self) -> Scan:
        """Month-to-date / first-week baseline: the last pre-month scan, else
        the month's first scan (a zero-length month-to-date)."""
        return self.lead[-1] if self.lead else self.rows[0]


@dataclass(frozen=True)
class DayRow:
    """One UTC day's reply: its ``scan`` (the day's first scan for the
    ``sender`` variant, last for ``body``) and the delta to the prior day's
    reply scan (``None`` fields = no prior)."""

    date: str
    scan: str
    tb: float
    dtb: float | None
    hours: float | None
    since: dt.datetime | None  # the prior reply scan's instant (the diff link's look-back)
    extra: dict[str, float] = field(default_factory=dict)  # the other buckets' TiB
    dextra: dict[str, float | None] = field(default_factory=dict)  # …and Δ vs the prior reply scan


def day_rows(month: Month, variant: str, reply_hour: int) -> list[DayRow]:
    """One ``DayRow`` per in-month UTC day that has its reply scan.

    ``sender``: the day's first scan at/after ``reply_hour`` UTC (the morning
    scan; posted once, never edited). A day whose morning scan hasn't landed
    yet has no row — unless a later day has already started, in which case
    the day's LAST scan stands in, so a missed 12:01Z scan still yields
    exactly one reply per day. ``body``: the day's LAST scan so far (the
    reply is re-edited as the day's scans land)."""
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}, not {variant!r}")
    days: OrderedDict[str, list[Scan]] = OrderedDict()
    for r in month.lead + month.rows:
        days.setdefault(r.date, []).append(r)
    out: list[DayRow] = []
    prev: Scan | None = None
    for i, (date, rs) in enumerate(days.items()):
        if variant == "body":
            s = rs[-1]
        else:
            later = i + 1 < len(days)  # some scan of a later day exists
            s = next((r for r in rs if scan_ts(r.scan).hour >= reply_hour), rs[-1] if later else None)
            if s is None:
                continue  # the day's morning scan is still to come
        if date >= month.rows[0].date:
            out.append(
                DayRow(
                    date=date,
                    scan=s.scan,
                    tb=s.tb,
                    dtb=round(s.tb - prev.tb, 1) if prev else None,
                    hours=(scan_ts(s.scan) - scan_ts(prev.scan)).total_seconds() / 3600 if prev else None,
                    since=scan_ts(prev.scan) if prev else None,
                    extra=s.extra,
                    dextra=_dextra(s, prev),
                )
            )
        prev = s
    return out


def op_body(month: Month, m: dt.date, plot_url: str | None, cfg: DigestConfig) -> str:
    """OP markdown: month-to-date headline, per-ISO-week bullets, trailing
    sparkline. The month/year title is NOT in the body -- it's folded into the
    OP's sender name by the poster. ``plot_url=None`` omits the image line."""
    site_url, q = cfg.site_url, cfg.primary_quota
    rows, base = month.rows, month.base
    last = rows[-1]
    mdtb = last.tb - base.tb
    days = (scan_ts(last.scan) - scan_ts(base.scan)).total_seconds() / 86400 or 1.0
    mweekly = (mdtb / base.tb * 100 * 7 / days) if base.tb else 0
    # "month-to-date" opens the Diff section over the whole month so far
    # (lead-in scan -> latest), the same way each weekly bullet links its span
    mtd_url = _diff_url(last.scan, scan_ts(base.scan) if base is not last else None, site_url)
    lines = [
        f":arrow_deg{deg(mweekly)}: **{_tb(mdtb)} TiB** [month-to-date]({mtd_url}) · {last.tb:,.0f} TiB{_quota(last.tb, q)}{_extras(last.extra, _dextra(last, base if base is not last else None))} · [dashboard]({site_url}/)",
        "",
        "*Weekly summaries*",
    ]
    weeks: OrderedDict[dt.date, list[Scan]] = OrderedDict()
    for r in rows:
        d = dt.date.fromisoformat(r.date)
        weeks.setdefault(d - dt.timedelta(days=d.weekday()), []).append(r)
    last_mon = list(weeks)[-1]
    prev_end = base
    for mon, ws in weeks.items():
        end = ws[-1]
        wdtb = end.tb - prev_end.tb
        wpct = wdtb / prev_end.tb * 100 if prev_end.tb else 0
        # partial while the week's Sunday has no scan yet
        partial = " _(partial)_" if mon == last_mon and dt.date.fromisoformat(end.date) < mon + dt.timedelta(days=6) else ""
        # the link opens the Diff section over exactly this bullet's span
        lines.append(
            f":arrow_deg{deg(wpct)}: [wk of {mon.month}/{mon.day}]({_diff_url(end.scan, scan_ts(prev_end.scan) if prev_end is not end else None, site_url)}){partial}: "
            f"**{_tb(wdtb)} TiB** → {end.tb:,.0f} TiB{_quota(end.tb, q)}"
        )
        prev_end = end
    if plot_url is not None:
        lines += ["", f"![{cfg.title} — {m:%B %Y}]({plot_url})"]
    return "\n".join(lines)


def reply(day: DayRow, variant: str, cfg: DigestConfig) -> Reply:
    """One day's reply. ``sender``: headline as the sender name (plain text --
    Slack renders no links/emoji/markdown there), trend-arrow avatar, the
    per-bucket tail as the body. ``body``: everything in the body under the
    static ``cfg.title`` sender, headline bold, arrow as the leading emoji.
    The arrow projects the day's Δ% over its real interval to a weekly rate."""
    dtb = day.dtb or 0
    mult = HOURS_PER_WEEK / day.hours if day.hours else 7.0
    d = deg(_pct_val(dtb, day.tb), mult)
    size = f"{day.tb:,.0f} TiB ({_tb(dtb)}, {_pct(dtb, day.tb)}%)"
    # Each bucket as a linked `% of quota (free)` clause (the bucket names are
    # the over-time links, so no separate ↗ arrow).
    tail = _tail(day, cfg)
    if variant == "sender":
        return Reply(f"{_md(day.date)} — {size}", tail, icon_url=f"{cfg.icons_base}/arrows/av_deg{d}.png?v={AVATAR_REV}")
    if variant == "body":
        url = _diff_url(day.scan, day.since, cfg.site_url)
        return Reply(cfg.title, f":arrow_deg{d}: [{_md(day.date)}]({url}) — **{size}** · {tail}", icon_emoji=":calendar:")
    raise ValueError(f"variant must be one of {VARIANTS}, not {variant!r}")


def load_month(root: str, month: dt.date, primary: str) -> Month | None:
    """The month's scans from ``root`` (``gs://<bucket>/snapshots/cw``), with
    ``lead`` = every scan of the last calendar day before it. None if the
    month has no scans."""
    w = load_window(root, month)
    if w is None:
        return None
    lead, in_month = w
    rows = rows_from_meta(lead + in_month, primary)
    return Month(lead=rows[: len(lead)], rows=rows[len(lead) :])


def primary_node(tree: dict, primary: str) -> dict:
    """The primary bucket's node of a scan's `tree.json` (`{n: <store label>,
    …, c: [<bucket>…]}`): by name, else the first child (single-bucket scans
    named the bucket after the layer-2 file)."""
    return next((c for c in tree["c"] if c["n"] == primary), tree["c"][0])


def load_tree(root: str, scan: str, primary: str) -> dict:
    """The primary bucket's node of a scan's `tree.json` (`<root>/<scan>/tree.json`)."""
    import fsspec

    with fsspec.open(f"{root}/{scan}/tree.json", "rt") as f:
        return primary_node(json.load(f), primary)


class Cw:
    """The `cw` template over a :class:`DigestConfig` (see `digest.Template`)."""

    variants = VARIANTS
    edited_variants = ("body",)
    track_scan = True

    def __init__(self, cfg: DigestConfig):
        self.cfg = cfg

    def load(self, root: str, month: dt.date) -> Month | None:
        return load_month(root, month, self.cfg.primary)

    def n_scans(self, month: Month) -> int:
        return len(month.rows)

    def op_body(self, month: Month, m: dt.date, plot_url: str | None) -> str:
        return op_body(month, m, plot_url, self.cfg)

    def units(self, month: Month, variant: str, platform: str = "slack") -> list[Unit]:
        return [Unit(day.date, day.scan, reply(day, variant, self.cfg)) for day in day_rows(month, variant, self.cfg.reply_hour)]

    def render_plot(self, month: Month, m: dt.date, out: Path, root: str | None = None) -> None:
        """The quota sparkline, plus — when ``root`` is given and the month
        spans two scans — the diff treemap over the OP headline's interval (the
        lead-in scan → the latest). Needs the `[plot]` extra — matplotlib."""
        from .digest_plot import diff_tree, render_quota

        cfg, q = self.cfg, self.cfg.primary_quota
        base, last = month.base, month.rows[-1]
        diff = diff_tree(load_tree(root, base.scan, cfg.primary), load_tree(root, last.scan, cfg.primary), min_frac=0.01) if root and base is not last else None
        render_quota(
            [{"ts": scan_ts(r.scan), "tb": r.tb} for r in month.rows], Path(out), f"{cfg.title} — {m:%B %Y}", cfg.host,
            quota_tib=q.bytes / TIB if q else None, quota_name=q.name if q else "",
            diff=diff, diff_label=f"{_md(base.date)} → {_md(last.date)}",
        )
