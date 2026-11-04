"""The `cw` digest template: a reply per UTC day, quota headroom per bucket.

The scan covers every bucket of a store (specs/done/cw-multi-bucket.md); the
headline, deltas and quota line are the PRIMARY bucket's (``cfg.primary``,
read from `meta.buckets[<primary>]` when the scan has it, else the flat
totals, which were the primary's), the others ride along. The OP: month-to-
date headline (`· NN% of <quota>` when the primary has one, ` · <bucket> <N>
TiB (Δ)` per other bucket; sizes in ``cfg.units``, TiB or decimal TB) beside the last 30 days' change, per-ISO-week
bullets over the OP's window (the longer of the last 30 days and the month to date, specs/digest-rolling-window.md),
the previous month's thread, and a quota sparkline + diff treemap over the window (drawn by the site:
`POST /api/digest-image`). ONE REPLY PER UTC DAY, Δ over the prior
day's reply scan, the arrow normalised to the real interval; its tail is a
linked `[<label>](<over-time>): NN.N% of <quota> (<free> Ti free)` clause per
bucket (`cfg.buckets`; a bucket without a quota shows raw TiB; buckets sharing
a `zone` collapse into one clause against the zone's quota). `quota_marks: circles`
sorts the clauses fullest first and leads each with a 🔴 / 🟠 / 🟡 / 🟢 by its share (`_mark`).

Two reply VARIANTS because Slack fixes a message's username + icon at post
time (`chat.update` can't change them):
- ``sender``: the headline IS the sender name, the arrow the avatar. Posted
  once, from the day's MORNING scan — the first at/after ``cfg.reply_hour``
  UTC (12:01Z = 8:01 am ET, the same calendar date in both zones) — with a
  ~24 h delta to the prior day's reply scan; the day's other scans only
  re-converge the OP + plot.
- ``body``: the headline is bold body text under a static sender/avatar, so
  the day's reply is EDITED whenever a later scan of the day lands — text and
  sparkline agree intra-day (at the cost of the first edits' Δ spanning less than
  a day until the day's last scan makes it ~24 h).

``cfg.provisional`` (``sender`` only): a day's scans BEFORE its reply scan
(at a 6 h cadence and ``reply_hour`` 12: the 00Z and 06Z scans) post one
PROVISIONAL reply for that UTC date — a fixed neutral sender (`10/3 · so
far`) and icon, the latest numbers in the body, edited by each later early
scan — which the engine deletes once the day's final reply posts, so a
finished thread reads exactly as without it. Scans after the reply scan
(18Z) belong to no provisional: the day has its final reply, and the next
date's provisional starts with that date's first scan. ``body`` replies are
already edited intra-day, so they never have one.

Content + deltas: specs/cw-slack-digest.md; the engine: `digest`."""
from __future__ import annotations

import datetime as dt
import json
import math
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .digest import (
    AVATAR_REV, HOURS_PER_WEEK, LINK_ARROW, TIB, UNITS, DigestConfig, PlotUnavailable, Quota, Reply, Unit, Units,
    _dlink, _err, _md, _pct, _pct_val, _span, _tb, deg, list_scans, load_window, read_metas, site_render,
    scan_slugs, scan_ts, window_scans, window_start,
)

VARIANTS = ("sender", "body")
# a provisional reply's avatar: neutral (no trend), and fixed at post time like its sender
PROVISIONAL_ICON = ":hourglass_flowing_sand:"


def _quota(tb: float, q: Quota | None, u: Units = UNITS["TiB"]) -> str:
    """` · NN.N% of <quota>` for a total in ``u``; `''` without a quota."""
    return f" · {tb / (q.bytes / u.bytes) * 100:.1f}% of {q.name}" if q else ""


def _scoped(tb: float, classes: dict[int, float] | None, q: Quota) -> float:
    """What ``q`` counts of a scan whose total is ``tb`` and per-class sizes ``classes`` (both in the digest's
    units): its ``classes``' sum, or the total for a quota over every class."""
    if not q.classes:
        return tb
    if classes is None:
        raise ValueError(f"quota {q.name!r} counts storage classes {list(q.classes)}, but the scan's meta.json has no class_bytes")
    return sum(classes.get(c, 0.0) for c in q.classes)




def _qlabel(b: int) -> str:
    """SI quota label: 1 PB → `1P`, 100 TB → `100T`."""
    return f"{b / 10**15:g}P" if b >= 10**15 else f"{b / 10**12:g}T"


def _diff_url(scan: str, since: dt.datetime | None, site_url: str, bucket: str = "", slug: str | None = None) -> str:
    """The dashboard's over-time view (optionally scoped to ``bucket``) pinned
    to ``scan`` (by ``slug``, its canonical slug, else its minute), looking back
    to ``since`` (None: the baked previous scan)."""
    span = f"-{_span(since, scan_ts(scan))}" if since is not None else ""
    return f"{site_url}/{bucket}?d={_dlink(scan, slug)}{span}#over-time"


def _bucket_link(bucket: str, scan: str, since: dt.datetime | None, cfg: DigestConfig, slug: str | None = None) -> str:
    """`[<label>](<over-time url>)` for one bucket (at its site ``path``, default ``/<bucket>``)."""
    b = cfg.buckets.get(bucket)
    path = bucket if b is None or b.path is None else b.path
    return f"[{(b.label if b else None) or bucket}]({_diff_url(scan, since, cfg.site_url, path, slug)})"


#: `quota_marks: circles` / `runway`: a pool under this share renders as a bare `NN%` (no quota, no headroom).
QUIET_PCT = 25
#: The `quota_marks` whose clauses are brief (`QUIET_PCT`) and marked by a circle.
BRIEF_MARKS = ("circles", "runway", "runway-hue")
#: The `quota_marks` that mark each pool by its runway, in config order.
RUNWAY_MARKS = ("runway", "runway-hue")


def _quota_clause(tb: float, q: Quota, u: Units = UNITS["TiB"], brief: bool = False) -> str:
    """`NN.N% of <quota> (<n> Ti free)` (`over` past the quota); ``brief`` (`quota_marks: circles`): under
    `QUIET_PCT`, just `NN%`."""
    qt = q.bytes / u.bytes
    pct = tb / qt * 100
    if brief and pct < QUIET_PCT:
        return f"{pct:.0f}%"
    room = f"{qt - tb:,.1f} {u.short} free" if tb <= qt else f"{tb - qt:,.1f} {u.short} over"
    return f"{pct:.1f}% of {q.short or _qlabel(q.bytes)} ({room})"


def _bucket_clause(bucket: str, tb: float, scan: str, since: dt.datetime | None, cfg: DigestConfig, slug: str | None = None,
                   classes: dict[int, float] | None = None) -> str:
    """`[<label>](<over-time url>): NN.N% of <quota> (<free> Ti free)` for one
    bucket (`, `-joined per quota, each over its own ``classes``); a bucket
    with no known quota renders its raw TiB."""
    b = cfg.buckets.get(bucket)
    qs = b.quotas if b else ()
    link = _bucket_link(bucket, scan, since, cfg, slug)
    if not qs:
        return f"{link}: {tb:,.0f} {cfg.unit.short}"
    return f"{link}: " + ", ".join(_quota_clause(_scoped(tb, classes, q), q, cfg.unit, cfg.quota_marks in BRIEF_MARKS) for q in qs)


def _zone_clause(members: list[tuple[str, float]], scan: str, since: dt.datetime | None, cfg: DigestConfig, slug: str | None = None) -> str:
    """Buckets sharing a zone quota as one clause: `[<a>](…) 82 + [<b>](…) 3 Ti:
    NN.N% of <quota> (<free> Ti free)` — the free figure is the zone's."""
    q = cfg.buckets[members[0][0]].quota
    parts = " + ".join(f"{_bucket_link(b, scan, since, cfg, slug)} {tb:,.0f}" for b, tb in members)
    total = sum(tb for _, tb in members)
    return f"{parts} {cfg.unit.short}" + (f": {_quota_clause(total, q, cfg.unit, cfg.quota_marks in BRIEF_MARKS)}" if q else "")


def _tail(day: DayRow, cfg: DigestConfig) -> str:
    """The daily reply's per-bucket tail: the primary then each extra bucket as
    a linked `% of quota (free)` clause, ` · `-joined. Buckets sharing a
    ``zone`` collapse into one clause at the first one's position. A store of
    one configured bucket (hf: the whole org) drops the label, which would only
    repeat the store: its clause, then a trailing `[↗︎]` over-time link (`LINK_ARROW`)."""
    if len(cfg.buckets) <= 1 and not day.extra:
        b = cfg.buckets.get(cfg.primary)
        qs = b.quotas if b else ()
        link = _diff_url(day.scan, day.since, cfg.site_url, cfg.primary if b is None or b.path is None else b.path, day.slug)
        body = ", ".join(_quota_clause(_scoped(day.tb, day.classes, q), q, cfg.unit) for q in qs) if qs else f"{day.tb:,.0f} {cfg.unit.short}"
        return f"{body} [{LINK_ARROW}]({link})"
    cs = _clauses(day, cfg)
    if cfg.quota_marks in RUNWAY_MARKS:
        # config order: a reply series reads by position, the marks draw the eye (specs/digest-runway-marks.md)
        hue = cfg.quota_marks == "runway-hue"
        return " · ".join(f"{_runway_hue(r, fill, shape) if hue else _runway_mark(r, fill)} {c}{_runway_text(r, cfg)}" if fill is not None else c
                          for c, fill, _, r, shape in cs)
    if cfg.quota_marks == "circles":
        cs = sorted(cs, key=lambda c: -1.0 if c[1] is None else -c[1])
    return " · ".join(f"{m} {c}" if (m := _mark(fill, over, cfg)) else c for c, fill, over, _, _ in cs)


#: Marks a quota clause at or over `alert_pct` (the daily replies' tail and the alert reply).
ALERT_MARK = ":warning:"
#: `quota_marks: circles`, every pool one: red at or over `mark_pct`, orange within `ORANGE_PTS` below it, yellow
#: within `YELLOW_PTS`, green under that (the site's `digest/pools.ts` `mark`).
RED_MARK, ORANGE_MARK, YELLOW_MARK, GREEN_MARK = ":red_circle:", ":large_orange_circle:", ":large_yellow_circle:", ":large_green_circle:"
ORANGE_PTS, YELLOW_PTS = 10, 40


def _mark(fill: float | None, over: bool, cfg: DigestConfig) -> str | None:
    """A clause's mark: `:warning:` when over ``alert_pct`` (`quota_marks: warning`), or a circle by its fill
    (`circles`); None unmarked or without a quota."""
    if cfg.quota_marks != "circles":
        return ALERT_MARK if over else None
    if fill is None:
        return None
    red = min(cfg.mark_pct, 100)
    return (RED_MARK if fill >= red else ORANGE_MARK if fill >= cfg.mark_pct - ORANGE_PTS
            else YELLOW_MARK if fill >= cfg.mark_pct - YELLOW_PTS else GREEN_MARK)


#: `quota_marks: runway` (specs/digest-runway-marks.md §2; the site's `digest/pools.ts` mirrors these names and rules):
#: growth is the fastest net fill over these look-backs (days), each from the latest scan at or before it.
RUNWAY_WINDOWS_D = (1, 3, 7)
#: 🔴 under `RUNWAY_RED_D` days to full (or over quota), 🟠 under `RUNWAY_ORANGE_D` (or at least `RUNWAY_FULL_PCT` full),
#: 🟡 under `RUNWAY_YELLOW_D`, 🟢 otherwise; a clause under `RUNWAY_TEXT_D` days also shows its rate and runway.
RUNWAY_RED_D, RUNWAY_ORANGE_D, RUNWAY_YELLOW_D, RUNWAY_TEXT_D = 3, 14, 30, 60
RUNWAY_FULL_PCT = 95


@dataclass(frozen=True)
class Runway:
    """A quota group's runway at a scan: ``days`` to full at ``growth`` (its unit per day; None: no earlier scan in
    reach), ``math.inf`` when not growing, 0 when at or over quota."""
    days: float
    growth: float | None


def _growth(hist: list[tuple[dt.datetime, float]], t: dt.datetime, v: float) -> float | None:
    """The fastest net fill per day reaching ``v`` at ``t`` over `RUNWAY_WINDOWS_D`: each window from the latest
    ``hist`` point at or before ``t`` minus it; None when no window has one."""
    rates = []
    for w in RUNWAY_WINDOWS_D:
        then = [(ts, x) for ts, x in hist if ts <= t - dt.timedelta(days=w)]
        if then:
            ts0, v0 = then[-1]
            rates.append((v - v0) / ((t - ts0).total_seconds() / 86400))
    return max(rates) if rates else None


def _runway(free: float, growth: float | None) -> Runway:
    if free <= 0:
        return Runway(0.0, growth)
    return Runway(free / growth if growth is not None and growth > 0 else math.inf, growth)


def _runway_mark(r: Runway, fill: float) -> str:
    """A pool's circle by its runway (`RUNWAY_*`)."""
    if r.days < RUNWAY_RED_D:
        return RED_MARK
    if r.days < RUNWAY_ORANGE_D or fill >= RUNWAY_FULL_PCT:
        return ORANGE_MARK
    return YELLOW_MARK if r.days < RUNWAY_YELLOW_D else GREEN_MARK


#: `quota_marks: runway-hue` (specs/digest-runway-marks.md §3): the workspace emoji `:runway-1:` (red) … `:runway-8:`
#: (green), the step one more than the number of these cut points (days to full) the runway reaches: under 1.8 days
#: (or over quota) 1, 60 days or more (or not filling) 8; log-spaced, 60^(k/7). The names carry no thresholds, so
#: these can move without re-uploading the emoji. A pool ≥ `RUNWAY_FULL_PCT` full is never calmer than
#: `RUNWAY_HUE_FULL_N` (the site's `digest/pools.ts` mirrors all three). A pool with a shape (`Bucket.shape`) gets
#: `:runway-<shape>-N:`, the same step in the same color; a circle keeps the plain `:runway-N:`.
RUNWAY_HUE_D = (1.8, 3, 6, 10, 19, 33, 60)
RUNWAY_HUE_FULL_N = 3


def _runway_hue(r: Runway, fill: float, shape: str = "circle") -> str:
    """A pool's `:runway-N:` / `:runway-<shape>-N:` by its runway (`RUNWAY_HUE_*`) and shape."""
    n = 1 + sum(r.days >= c for c in RUNWAY_HUE_D)
    return f":runway-{'' if shape == 'circle' else f'{shape}-'}{min(n, RUNWAY_HUE_FULL_N) if fill >= RUNWAY_FULL_PCT else n}:"


def _runway_text(r: Runway, cfg: DigestConfig) -> str:
    """`, +4.1 Ti/d, ≈8d to full` for a pool filling within `RUNWAY_TEXT_D` days (commas: ` · ` separates the pools);
    `''` otherwise (flat, shrinking, far off, or already over: its clause says so)."""
    if not 0 < r.days < RUNWAY_TEXT_D or r.growth is None:
        return ""
    days = "<1d" if r.days < 1 else f"{r.days:.0f}d"
    return f", +{r.growth:.1f} {cfg.unit.short}/d, ≈{days} to full"


def _group_used(g: list[str], s: Scan | DayRow, q: Quota, cfg: DigestConfig) -> float:
    """What quota ``q`` counts of group ``g`` (bucket names) at scan / day ``s``: a zone's buckets summed (raw sizes,
    as `_zone_clause`), a lone bucket over ``q``'s classes (the primary's; the others' raw)."""
    def size(b: str) -> float:
        return s.tb if b == cfg.primary else s.extra.get(b, 0.0)
    if len(g) > 1:
        return sum(size(b) for b in g)
    return _scoped(s.tb, s.classes, q) if g[0] == cfg.primary else size(g[0])


def _group_runway(g: list[str], day: DayRow, cfg: DigestConfig) -> Runway | None:
    """The group's shortest runway over its quotas (a zone: the zone's), from ``day.hist``; None without a quota."""
    qs = (cfg.buckets[g[0]].quota,) if len(g) > 1 else (cfg.buckets[g[0]].quotas if g[0] in cfg.buckets else ())
    qs = tuple(q for q in qs if q is not None)
    if not qs:
        return None
    t = scan_ts(day.scan)
    out = []
    for q in qs:
        v = _group_used(g, day, q, cfg)
        hist = [(scan_ts(x.scan), _group_used(g, x, q, cfg)) for x in day.hist if x.scan < day.scan]
        out.append(_runway(q.bytes / cfg.unit.bytes - v, _growth(hist, t, v)))
    return min(out, key=lambda r: r.days)


def _groups(day: DayRow, cfg: DigestConfig) -> list[list[tuple[str, float]]]:
    """The day's buckets (the primary first, then the extras in meta order) grouped by quota: buckets sharing a
    ``zone`` are one group, at the first one's position; every other bucket is its own."""
    groups: dict[str, list[tuple[str, float]]] = {}
    for b, tb in [(cfg.primary, day.tb), *day.extra.items()]:
        z = cfg.buckets[b].zone if b in cfg.buckets else None
        groups.setdefault(f"zone:{z}" if z else f"bucket:{b}", []).append((b, tb))
    return list(groups.values())


def _fill(g: list[tuple[str, float]], day: DayRow, cfg: DigestConfig) -> float | None:
    """A group's fullest quota, in percent (each quota over its own classes; a zone's over its buckets' sum); None
    without a quota."""
    if len(g) > 1:
        q = cfg.buckets[g[0][0]].quota
        return sum(tb for _, tb in g) / (q.bytes / cfg.unit.bytes) * 100 if q else None
    name, tb = g[0]
    b = cfg.buckets.get(name)
    classes = day.classes if name == cfg.primary else None
    return max((_scoped(tb, classes, q) / (q.bytes / cfg.unit.bytes) * 100 for q in b.quotas), default=None) if b else None


def _clauses(day: DayRow, cfg: DigestConfig) -> list[tuple[str, float | None, bool, Runway | None, str]]:
    """Each group's clause (`_bucket_clause` / `_zone_clause`), its fill (`_fill`), whether that is at or over
    ``cfg.alert_pct``, its runway (`quota_marks: runway`; else None), and its shape (`Bucket.shape`: a zone's members
    all state the same one; a bucket the config doesn't name has no quota, so no mark, and reads as a circle)."""
    out = []
    for g in _groups(day, cfg):
        clause = (_bucket_clause(*g[0], day.scan, day.since, cfg, day.slug, day.classes if g[0][0] == cfg.primary else None)
                  if len(g) == 1 else _zone_clause(g, day.scan, day.since, cfg, day.slug))
        fill = _fill(g, day, cfg)
        r = _group_runway([b for b, _ in g], day, cfg) if cfg.quota_marks in RUNWAY_MARKS else None
        b = cfg.buckets.get(g[0][0])
        out.append((clause, fill, cfg.alert_pct is not None and fill is not None and fill >= cfg.alert_pct, r, b.shape if b else "circle"))
    return out


def alert_body(month: Month, cfg: DigestConfig) -> str | None:
    """The quota alert reply's body: every group at or over ``cfg.alert_pct`` as of the month's latest scan, else
    None (no threshold, none over, or a closed month: the next month's thread carries it)."""
    if cfg.alert_pct is None or month.closed:
        return None
    last = month.rows[-1]
    over = [(c, f) for c, f, o, _, _ in _clauses(_row(last.date, last, None), cfg) if o]
    if cfg.quota_marks == "circles":
        over.sort(key=lambda c: -c[1])
    over = [c for c, _ in over]
    if not over:
        return None
    return (f"{ALERT_MARK} **Quota alert**: {' · '.join(over)} · _as of {_md(last.date)} {scan_ts(last.scan):%H:%M}Z_ "
            f"(≥ {cfg.alert_pct:g}% of a quota; removed once every quota is back under)")


@dataclass(frozen=True)
class Scan:
    """One scan's row: total (TiB, or the config's `units`) + object count, with deltas vs. the previous
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
    # its canonical `?d=` slug (`scan_slugs`); None: its minute
    slug: str | None = field(default=None, compare=False)
    # the primary's per-storage-class sizes (`class_bytes`, unrounded, in the digest's units); None: the meta has none
    classes: dict[int, float] | None = field(default=None, compare=False)
    # what started it (its first run's `scan_runs.trigger`, `scan_triggers`); None: unrecorded
    trigger: str | None = field(default=None, compare=False)

    @property
    def date(self) -> str:
        return self.scan[:10]

    def replyable(self, reply_hour: int) -> bool:
        """Can be its day's `sender` reply scan: from ``reply_hour`` UTC on, and not manual (a hand-run scan only
        stands in, `day_rows`; for one without a recorded trigger the clock alone decides)."""
        return scan_ts(self.scan).hour >= reply_hour and self.trigger != "manual"


def primary_totals(m: dict, primary: str, unit: int = TIB) -> tuple[int, int, dict[str, float]]:
    """A meta.json's ``(total_bytes, total_objects, extra)`` for the digest:
    the primary bucket's totals from ``buckets`` when present (else the flat
    totals), ``extra`` = the other buckets' sizes in ``unit`` bytes (TiB)."""
    bk = m.get("buckets") or {}
    if primary in bk:
        p = bk[primary]
        return int(p["total_bytes"]), int(p["total_objects"]), {b: v["total_bytes"] / unit for b, v in bk.items() if b != primary}
    return int(m["total_bytes"]), int(m["total_objects"]), {}


def primary_classes(m: dict, primary: str, unit: int = TIB) -> dict[int, float] | None:
    """A meta.json's per-storage-class sizes (``class_bytes``, in ``unit`` bytes) for the primary bucket: its
    ``buckets`` entry's when the meta has one, else the flat ones; None when they're absent."""
    bk = m.get("buckets") or {}
    cb = bk[primary].get("class_bytes") if primary in bk else m.get("class_bytes")
    return None if cb is None else {int(c): b / unit for c, b in cb.items()}


def rows_from_meta(dated_meta: list[tuple[str, dict]], primary: str, now: dt.datetime | None = None, unit: int = TIB,
                   triggers: Mapping[str, str] | None = None) -> list[Scan]:
    """Build ``Scan`` rows from ``(scan_id, meta.json)`` pairs in scan order, each with its canonical slug
    (`scan_slugs`, as of ``now``) and its trigger (``triggers``, `scan_triggers`); sizes in ``unit`` bytes (TiB)."""
    out: list[Scan] = []
    slugs = scan_slugs(dated_meta, now)
    ptb = pobjs = pts = None
    for scan, m in dated_meta:
        ts = scan_ts(scan)
        b, objs, extra = primary_totals(m, primary, unit)
        tb = b / unit
        out.append(
            Scan(
                scan=scan,
                tb=round(tb, 1),
                objs=objs,
                dtb=round(tb - ptb, 1) if ptb is not None else None,
                dobjs=objs - pobjs if pobjs is not None else None,
                hours=(ts - pts).total_seconds() / 3600 if pts is not None else None,
                extra={k: round(v, 1) for k, v in extra.items()},
                slug=slugs[scan],
                classes=primary_classes(m, primary, unit),
                trigger=(triggers or {}).get(scan),
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
    # a scan exists AFTER the month: its last day is over, so `sender`'s
    # stand-in rule applies to it too (a month converged once it has ended)
    closed: bool = False
    # the OP window's scans before ``lead`` (`window_scans`): its earlier weekly summaries, look-back figure and plot
    pre: list[Scan] = field(default_factory=list)

    def window(self, m: dt.date) -> tuple[dt.date, list[Scan]]:
        """The OP window's first day (`window_start`, to the latest scan) and every scan this holds, in order."""
        return window_start(m, dt.date.fromisoformat(self.rows[-1].date)), self.pre + self.lead + self.rows

    def back(self, m: dt.date) -> Scan:
        """The window's baseline (its look-back figure's, its plot's first point, its diff map's start): the last scan
        before its first day, else the earliest this holds (a store younger than the window)."""
        start, seen = self.window(m)
        before = [r for r in seen if r.date < start.isoformat()]
        return before[-1] if before else seen[0]

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
    provisional: bool = False  # `sender`: the day's latest scan before its reply scan has landed
    slug: str | None = field(default=None, compare=False)  # `scan`'s canonical `?d=` slug; None: its minute
    classes: dict[int, float] | None = field(default=None, compare=False)  # `scan`'s primary per class (`Scan.classes`)
    # the scans up to and including `scan` within the runway look-back (`RUNWAY_WINDOWS_D`; `quota_marks: runway`)
    hist: tuple[Scan, ...] = field(default=(), compare=False)


def day_rows(month: Month, variant: str, reply_hour: int, provisional: bool = False) -> list[DayRow]:
    """One ``DayRow`` per in-month UTC day that has its reply scan.

    ``sender``: the day's first scan at/after ``reply_hour`` UTC that wasn't
    started by hand (`Scan.replyable`: the morning cron scan; posted once,
    never edited). A day whose morning scan hasn't landed yet has no row — unless a later day has already started (or the month
    is ``closed``), in which case the day's LAST scan stands in, so a missed
    12:01Z scan still yields exactly one reply per day; ``provisional``
    gives that still-open day (only ever the last) a ``provisional`` row
    over its latest scan instead. ``body``: the day's LAST scan so far (the
    reply is re-edited as the day's scans land)."""
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}, not {variant!r}")
    seen = month.pre + month.lead + month.rows
    days: OrderedDict[str, list[Scan]] = OrderedDict()
    for r in month.lead + month.rows:
        days.setdefault(r.date, []).append(r)
    out: list[DayRow] = []
    prev: Scan | None = None
    for i, (date, rs) in enumerate(days.items()):
        if variant == "body":
            s = rs[-1]
        else:
            later = i + 1 < len(days) or month.closed  # some scan of a later day exists
            s = next((r for r in rs if r.replyable(reply_hour)), rs[-1] if later else None)
            if s is None:
                if provisional and date >= month.rows[0].date:
                    # the day's morning scan is still to come: its numbers so far
                    out.append(_row(date, rs[-1], prev, provisional=True, seen=seen))
                continue
        if date >= month.rows[0].date:
            out.append(_row(date, s, prev, seen=seen))
        prev = s
    return out


def _row(date: str, s: Scan, prev: Scan | None, provisional: bool = False, seen: list[Scan] | tuple[Scan, ...] = ()) -> DayRow:
    """``date``'s reply over scan ``s``, Δ vs ``prev`` (the prior day's reply scan); ``seen`` (chronological) gives
    its runway history."""
    reach = scan_ts(s.scan) - dt.timedelta(days=max(RUNWAY_WINDOWS_D) + 1)
    return DayRow(
        hist=tuple(x for x in seen if x.scan <= s.scan and scan_ts(x.scan) >= reach),
        date=date,
        scan=s.scan,
        tb=s.tb,
        dtb=round(s.tb - prev.tb, 1) if prev else None,
        hours=(scan_ts(s.scan) - scan_ts(prev.scan)).total_seconds() / 3600 if prev else None,
        since=scan_ts(prev.scan) if prev else None,
        extra=s.extra,
        dextra=_dextra(s, prev),
        provisional=provisional,
        slug=s.slug,
        classes=s.classes,
    )



def reply(day: DayRow, variant: str, cfg: DigestConfig) -> Reply:
    """One day's reply. ``sender``: headline as the sender name (plain text --
    Slack renders no links/emoji/markdown there), trend-arrow avatar, the
    per-bucket tail as the body. ``body``: everything in the body under the
    static ``cfg.title`` sender, headline bold, arrow as the leading emoji.
    The arrow projects the day's Δ% over its real interval to a weekly rate.
    A ``provisional`` day (``sender``) keeps a fixed sender + neutral icon —
    both outlive every edit — and carries the numbers so far, as of its
    latest scan, in the body."""
    dtb = day.dtb or 0
    mult = HOURS_PER_WEEK / day.hours if day.hours else 7.0
    d = deg(_pct_val(dtb, day.tb), mult)
    size = f"{day.tb:,.0f} {cfg.unit.name} ({_tb(dtb)}, {_pct(dtb, day.tb)}%)"
    # Each bucket as a linked `% of quota (free)` clause (the bucket names are
    # the over-time links; one bucket alone gets a trailing ↗ instead).
    tail = _tail(day, cfg)
    if day.provisional:
        url = _diff_url(day.scan, day.since, cfg.site_url, slug=day.slug)
        return Reply(f"{_md(day.date)} · so far", f":arrow_deg{d}: **{size}** · [as of {scan_ts(day.scan):%H:%M}Z]({url}) · {tail}", icon_emoji=PROVISIONAL_ICON)
    if variant == "sender":
        return Reply(f"{_md(day.date)} — {size}", tail, icon_url=f"{cfg.need('icons_base')}/arrows/av_deg{d}.png?v={AVATAR_REV}")
    if variant == "body":
        url = _diff_url(day.scan, day.since, cfg.site_url, slug=day.slug)
        return Reply(cfg.title, f":arrow_deg{d}: [{_md(day.date)}]({url}) — **{size}** · {tail}", **cfg.op_sender())
    raise ValueError(f"variant must be one of {VARIANTS}, not {variant!r}")


def scan_triggers(runs: Iterable[Mapping]) -> dict[str, str]:
    """Each scan's trigger: its first run's (by start; a hand resume of a cron scan doesn't make it manual), from
    `scan_runs` rows. A scan whose first run recorded none is absent."""
    first: dict[str, Mapping] = {}
    for r in sorted(runs, key=lambda r: (r.get("started_ts") is None, r.get("started_ts") or 0)):
        first.setdefault(r["scan"], r)
    return {s: r["trigger"] for s, r in first.items() if r.get("trigger")}


def site_triggers(cfg: DigestConfig) -> dict[str, str]:
    """`scan_triggers` over the site's `/api/scan-runs` (the primary store's runs), as the job (`$SITE_TOKEN`); {} with
    no token (a note on stderr: replies go by the clock) or a site without run records."""
    from .deploy import site_token
    from .site import get_json

    token = site_token()
    if not token:
        _err("digest: no site token ($SITE_TOKEN): no scan triggers, replies go by the clock")
        return {}
    res = get_json(cfg.site_url, token, "/api/scan-runs")
    return scan_triggers(s["run"] for s in res["runs"]) if res.get("configured") else {}


def load_month(root: str, month: dt.date, primary: str, unit: int = TIB, triggers: Mapping[str, str] | None = None) -> Month | None:
    """The month's scans from ``root`` (``gs://<bucket>/snapshots/cw``), sizes in ``unit`` bytes, each with its trigger
    (``triggers``), with ``lead`` = every scan of the last calendar day before it, ``pre`` = the OP window's scans
    before that (`window_scans`), ``closed`` once a later month's scan exists. None if the month has no scans."""
    scans = list_scans(root)
    w = load_window(root, month, scans)
    if w is None:
        return None
    lead, in_month = w
    pre = read_metas(root, window_scans(scans, month, (lead or in_month)[0][0]))
    rows = rows_from_meta(pre + lead + in_month, primary, unit=unit, triggers=triggers)
    n, k = len(pre), len(pre) + len(lead)
    return Month(lead=rows[n:k], rows=rows[k:], closed=scans[-1] > in_month[-1][0], pre=rows[:n])


def primary_node(tree: dict, primary: str) -> dict:
    """The primary bucket's node of a scan's `tree.json` (`{n: <store label>,
    …, c: [<bucket>…]}`): by name, else the first child (single-bucket scans
    named the bucket after the layer-2 file)."""
    return next((c for c in tree["c"] if c["n"] == primary), tree["c"][0])


def load_tree(root: str, scan: str, primary: str) -> dict:
    """The primary bucket's node of a scan's `tree.json` (`<root>/<scan>/tree.json`)."""
    from disk_tree import blobfs

    return primary_node(json.loads(blobfs.read_text(f"{root.rstrip('/')}/{scan}/tree.json")), primary)



class Cw:
    """The `cw` template over a :class:`DigestConfig` (see `digest.Template`)."""

    variants = VARIANTS
    edited_variants = ("body",)
    track_scan = True

    def __init__(self, cfg: DigestConfig):
        self.cfg = cfg

    def load(self, root: str, month: dt.date) -> Month | None:
        return load_month(root, month, self.cfg.primary, self.cfg.unit.bytes, site_triggers(self.cfg))

    def n_scans(self, month: Month) -> int:
        return len(month.rows)

    def op_body(self, month: Month, m: dt.date, plot_url: str | None, prev_url: str | None = None) -> str:
        return site_render(self.cfg, self.rows_json(month), m, plot_url, prev_url)["text"]

    def image_request(self, month: Month, m: dt.date) -> dict:
        return site_render(self.cfg, self.rows_json(month), m)["image"]

    def rows_json(self, month: Month) -> dict:
        """The month's scans as the site's renderer reads them (`src/digest/cw.ts` `CwRows`)."""
        def scan(r: Scan) -> dict:
            return {"scan": r.scan, "slug": r.slug, "tb": r.tb, "extra": r.extra, "classes": None if r.classes is None else {str(c): v for c, v in r.classes.items()}}

        return {"template": "cw", "closed": month.closed, "pre": [scan(r) for r in month.pre],
                "lead": [scan(r) for r in month.lead], "rows": [scan(r) for r in month.rows]}

    def units(self, month: Month, variant: str, platform: str = "slack") -> list[Unit]:
        return [Unit(day.date, day.scan, reply(day, variant, self.cfg)) for day in day_rows(month, variant, self.cfg.reply_hour)]

    def alert(self, month: Month) -> str | None:
        return alert_body(month, self.cfg)

    def provisional(self, month: Month, variant: str) -> Unit | None:
        """The still-open day's provisional reply (``sender`` only), else None."""
        if variant != "sender":
            return None
        rows = day_rows(month, variant, self.cfg.reply_hour, provisional=True)
        day = rows[-1] if rows and rows[-1].provisional else None
        return Unit(day.date, day.scan, reply(day, variant, self.cfg)) if day else None

    def render_plot(self, month: Month, m: dt.date, out: Path, root: str | None = None) -> None:
        """The OP's image, drawn by the site (`POST /api/digest-image`): the month's usage line against the quota,
        over the in-app diff map of the headline interval (the lead-in scan → the latest). Authenticated as the job
        (`$SITE_TOKEN`); ``root`` is unused (the site reads the diff from its own index)."""
        from .deploy import site_token
        from .site import post_json

        token = site_token()
        if not token:
            raise PlotUnavailable("digest image: no site token ($SITE_TOKEN)")
        Path(out).write_bytes(post_json(self.cfg.site_url, token, "/api/digest-image", self.image_request(month, m)))
