"""Monthly CoreWeave-usage digest -> a Slack thread (`dt-cloud digest`).

One thread per calendar month: an OP that's edited in place as scans land
(month-to-date headline, per-ISO-week rollup bullets, dashboard link, and a
hosted sparkline of every scan pinned to the 1 PB quota), plus ONE REPLY PER
UTC DAY: `M/D — <TiB> (Δ, Δ%) · NN% of 1 PB · <free> TiB free`, Δ over the
prior day's reply scan, a colour-coded trend arrow normalised per day. Posts
via the `thrds` `SlackClient` (per-message username/icon overrides need a bot
token). Converge state lives in a per-channel, per-variant, per-month JSON in
the data bucket.

Two reply VARIANTS exist because Slack fixes a message's username + icon at
post time (`chat.update` can't change them):
- ``sender`` (gcs-style): the headline IS the sender name, the arrow the
  avatar. Posted once, from the day's MORNING scan — the first at/after
  ``REPLY_HOUR_UTC`` (12:01Z = 8:01 am ET, the same calendar date in both
  zones) — with a ~24 h delta to the prior day's reply scan; the 00:01Z scan
  only re-converges the OP + plot.
- ``body``: the headline is bold body text under a static sender/avatar, so
  the day's reply is EDITED whenever a later scan of the day lands — text and
  sparkline agree intra-day (at the cost of the first edit's Δ spanning 12 h
  until the day's last scan makes it 24 h).

Mechanism ported from gcs's `digest.py` (specs/done/slack-digest-shape-c.md
there); the CoreWeave content + deltas are in specs/cw-slack-digest.md. Pure
functions (`deg`, `scan_ts`, `rows_from_meta`, `day_rows`, `op_body`, `reply`,
the formatters, the `_dlink`/`_span` link tokens) are unit-tested;
`post_digest` is the side-effecting shell (render+host plot, post/edit OP,
post/edit replies, persist state), tested against a fake client."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import sys
from collections import OrderedDict
from dataclasses import dataclass, field

TIB = 1024**4
# The scan covers every CoreWeave bucket (specs/cw-multi-bucket.md); the
# digest's headline, deltas and quota line are the PRIMARY's (the 1 PB
# bucket), read from `meta.buckets[<primary>]` when the scan has it and from
# the flat totals (which were the primary's) before that. The other buckets
# ride along as a ` · <bucket> <TiB> TiB (Δ)` clause.
PRIMARY_BUCKET = os.environ.get("CW_BUCKET", "marin-us-east-02a")
# The bucket's quota is 1 PB *decimal* (10^15 bytes) = 909.49 TiB — the
# "910 TiB" in the site's comments and the zones memo is this number rounded.
# Owned here, once; headroom renders as "% of 1 PB".
QUOTA_BYTES = 10**15
QUOTA_TIB = QUOTA_BYTES / TIB
# Weekly-halving arrow buckets: |dpct| >= THRESH[i] -> deg (i+1)*10 (capped 80).
THRESH = [0.39, 0.78, 1.5, 3.1, 6.25, 12.5, 25, 50]
MINUS = "−"  # matches the site's unicode minus
DEFAULT_URL = "https://cw-s3.oa.dev"
# The avatars are gcs's arrow set, served from the icons project's production
# alias; cw's plots go to its own preview branch (ICONS_BRANCH) so a cw deploy
# never replaces what that alias serves.
ICONS_BASE = "https://gcs-usage-icons.pages.dev"
ICONS_PROJECT = "gcs-usage-icons"
ICONS_BRANCH = "cw"
# bump when the av_deg glyphs change: Slack caches avatars per-URL at post
# time, so a stable URL serves MIXED generations after a redesign.
AVATAR_REV = 4
HOURS_PER_WEEK = 168.0
SCAN_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2}))?$")
VARIANTS = ("sender", "body")
# The `sender` variant's daily reply is the day's first scan at/after this UTC
# hour: 12 → the 12:01Z scan, 8:01 am ET, so the reply carries the same date
# on both coasts (the 00:01Z scan is 8:01 pm ET the evening before).
REPLY_HOUR_UTC = 12
OP_SENDER = "CoreWeave usage"  # + " — <Month YYYY>" on the OP; bare on `body`-variant replies


def deg(pct_signed: float, mult: float = 1.0) -> int:
    """Signed arrow degree for a percent change, time-normalized by ``mult``.

    Anchored on a weekly halving (deg80 ~ +/-50%/week). A daily reply passes
    ``168 / hours`` (7 for a clean 24 h), a weekly bullet ``mult=1``,
    month-to-date ``7 / days_elapsed`` -- so every arrow means the same
    underlying rate."""
    a = abs(pct_signed) * mult
    d = 0
    for i, t in enumerate(THRESH):
        if a >= t:
            d = (i + 1) * 10
    d = min(80, d)
    return -d if pct_signed < 0 else d


def scan_ts(scan: str) -> dt.datetime:
    """A scan id's UTC instant; date-only ids read as midnight (site/src/scan.ts)."""
    m = SCAN_RE.match(scan)
    if not m:
        raise ValueError(f"not a scan id: {scan!r}")
    y, mo, d, hh, mm = m.groups()
    return dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), tzinfo=dt.timezone.utc)


def _tb(v: float) -> str:
    return f"+{v:.1f}" if v >= 0 else f"{MINUS}{abs(v):.1f}"


def _pct(dtb: float, tb: float) -> str:
    prev = tb - dtb
    return f"{abs(dtb / prev * 100) if prev else 0:.1f}"


def _pct_val(dtb: float, tb: float) -> float:
    prev = tb - dtb
    return dtb / prev * 100 if prev else 0.0


def _quota(tb: float) -> str:
    """`NN.N% of 1 PB` for a TiB total."""
    return f"{tb / QUOTA_TIB * 100:.1f}% of 1 PB"


def _free(tb: float) -> str:
    return f"{QUOTA_TIB - tb:,.1f} TiB free"


def _extras(extra: dict[str, float], dextra: dict[str, float | None]) -> str:
    """` · <bucket> <TiB> TiB (Δ)` per non-primary bucket (Δ omitted when the
    prior scan lacked the bucket); `''` with none."""
    out = ""
    for b, tb in extra.items():
        d = dextra.get(b)
        out += f" · {b} {tb:,.0f} TiB" + (f" ({_tb(d)})" if d is not None else "")
    return out


def _md(date: str) -> str:
    d = dt.date.fromisoformat(date)
    return f"{d.month}/{d.day}"


def _dlink(scan: str) -> str:
    """The site's compact `?d=` token for a scan (`260915-0001`; date-only ids
    stay `260915`)."""
    y, mo, d, hh, mm = SCAN_RE.match(scan).groups()
    return f"{y[2:]}{mo}{d}" + (f"-{hh}{mm}" if hh else "")


def _span(a: dt.datetime, b: dt.datetime) -> str:
    """`?d=` look-back token for the interval a→b (`1d12h`, `7d`, `12h`),
    matching the site's `encodeSpan`."""
    # round to whole hours first so a scan that drifted a minute (00:02 →
    # 12:01) still reads `1d`, not `24h`
    hours, days = divmod(round((b - a).total_seconds() / 3600), 24)[::-1]
    return (f"{days}d" if days else "") + (f"{hours}h" if hours else "") or "0h"


def _diff_url(scan: str, since: dt.datetime | None, site_url: str) -> str:
    """The dashboard's Diff section pinned to ``scan``, looking back to
    ``since`` (None: the baked previous scan)."""
    span = f"-{_span(since, scan_ts(scan))}" if since is not None else ""
    return f"{site_url}/?d={_dlink(scan)}{span}#over-time"


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


def primary_totals(m: dict, primary: str = PRIMARY_BUCKET) -> tuple[int, int, dict[str, float]]:
    """A meta.json's ``(total_bytes, total_objects, extra)`` for the digest:
    the primary bucket's totals from ``buckets`` when present (else the flat
    totals), ``extra`` = the other buckets' TiB."""
    bk = m.get("buckets") or {}
    if primary in bk:
        p = bk[primary]
        return int(p["total_bytes"]), int(p["total_objects"]), {b: v["total_bytes"] / TIB for b, v in bk.items() if b != primary}
    return int(m["total_bytes"]), int(m["total_objects"]), {}


def rows_from_meta(dated_meta: list[tuple[str, dict]], primary: str = PRIMARY_BUCKET) -> list[Scan]:
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


def day_rows(month: Month, variant: str, reply_hour: int = REPLY_HOUR_UTC) -> list[DayRow]:
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


# ---- Content (framing A: quota headroom) ------------------------------------


def op_body(month: Month, m: dt.date, plot_url: str | None, site_url: str = DEFAULT_URL) -> str:
    """OP markdown: month-to-date headline, per-ISO-week bullets, trailing
    sparkline. The month/year title is NOT in the body -- it's folded into the
    OP's sender name by the poster. ``plot_url=None`` omits the image line."""
    rows, base = month.rows, month.base
    last = rows[-1]
    mdtb = last.tb - base.tb
    days = (scan_ts(last.scan) - scan_ts(base.scan)).total_seconds() / 86400 or 1.0
    mweekly = (mdtb / base.tb * 100 * 7 / days) if base.tb else 0
    # "month-to-date" opens the Diff section over the whole month so far
    # (lead-in scan -> latest), the same way each weekly bullet links its span
    mtd_url = _diff_url(last.scan, scan_ts(base.scan) if base is not last else None, site_url)
    lines = [
        f":arrow_deg{deg(mweekly)}: **{_tb(mdtb)} TiB** [month-to-date]({mtd_url}) · {last.tb:,.0f} TiB · {_quota(last.tb)}{_extras(last.extra, _dextra(last, base if base is not last else None))} · [dashboard]({site_url}/)",
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
            f"**{_tb(wdtb)} TiB** → {end.tb:,.0f} TiB · {_quota(end.tb)}"
        )
        prev_end = end
    if plot_url is not None:
        lines += ["", f"![CoreWeave usage — {m:%B %Y}]({plot_url})"]
    return "\n".join(lines)


@dataclass(frozen=True)
class Reply:
    """A reply's post parameters: ``username``/``icon_url``/``icon_emoji`` are
    fixed at post time (Slack), ``body`` is what an edit can change."""

    username: str
    body: str
    icon_url: str | None = None
    icon_emoji: str | None = None


def reply(day: DayRow, variant: str, site_url: str = DEFAULT_URL) -> Reply:
    """One day's reply. ``sender``: headline as the sender name (plain text --
    Slack renders no links/emoji/markdown there), trend-arrow avatar, the rest
    in the body with the diff link at EOL. ``body``: everything in the body
    under the static month sender, headline bold, arrow as the leading emoji.
    The arrow projects the day's Δ% over its real interval to a weekly rate."""
    dtb = day.dtb or 0
    mult = HOURS_PER_WEEK / day.hours if day.hours else 7.0
    d = deg(_pct_val(dtb, day.tb), mult)
    url = _diff_url(day.scan, day.since, site_url)
    size = f"{day.tb:,.0f} TiB ({_tb(dtb)}, {_pct(dtb, day.tb)}%)"
    tail = f"{_quota(day.tb)} · {_free(day.tb)}{_extras(day.extra, day.dextra)}"
    if variant == "sender":
        # ↗︎ = NE arrow + text-presentation selector: renders as a
        # font glyph in link colour (bare ↗ gets emoji-ized by Slack)
        return Reply(f"{_md(day.date)} — {size}", f"{tail} [↗︎]({url})", icon_url=f"{ICONS_BASE}/arrows/av_deg{d}.png?v={AVATAR_REV}")
    if variant == "body":
        return Reply(OP_SENDER, f":arrow_deg{d}: [{_md(day.date)}]({url}) — **{size}** · {tail}", icon_emoji=":calendar:")
    raise ValueError(f"variant must be one of {VARIANTS}, not {variant!r}")


# ---- Diff treemap data ------------------------------------------------------


@dataclass(frozen=True)
class DiffCell:
    """One cell of the month's diff treemap: a depth-2 path (`marin/skyrl`,
    `tmp/ttl=14d`), its top-level ``group``, and its byte delta base→latest.
    ``<group>/…`` is a group's residual (children pruned from the tree or below
    the fold threshold); ``other`` gathers whole groups below the threshold."""

    path: str
    group: str
    delta: int


def _kids(node: dict) -> dict[str, dict]:
    return {c["n"]: c for c in node.get("c", [])}


def tree_diff(base: dict, latest: dict, min_frac: float = 0.0) -> list[DiffCell]:
    """Diff two size trees (the bucket node of each scan's `tree.json`, nodes
    `{n,b,o,d,c}`) into depth-2 cells by path: each top-level dir's children
    get a cell (a dir present on one side only counts as fully grown/shrunk),
    and whatever the children don't account for — the tree is pruned at 0.02%
    of bytes, so small dirs vanish from `c` — lands in the group's `…` cell.
    A top-level dir with no children on either side is its own single cell.

    ``min_frac`` folds small cells: a cell under ``min_frac`` of the total
    |Δ| joins its group's `…`; a whole group under it joins `other`. Groups
    are ordered by Σ|Δ| desc, cells within a group by |Δ| desc then path."""
    bk, lk = _kids(base), _kids(latest)
    groups: dict[str, list[DiffCell]] = {}
    for name in sorted(set(bk) | set(lk)):
        b, l = bk.get(name, {}), lk.get(name, {})
        dtotal = l.get("b", 0) - b.get("b", 0)
        bc, lc = _kids(b), _kids(l)
        cells = [DiffCell(f"{name}/{k}", name, lc.get(k, {}).get("b", 0) - bc.get(k, {}).get("b", 0)) for k in sorted(set(bc) | set(lc))]
        cells = [c for c in cells if c.delta]
        if not bc and not lc:
            cells = [DiffCell(name, name, dtotal)] if dtotal else []
        elif (rest := dtotal - sum(c.delta for c in cells)):
            cells.append(DiffCell(f"{name}/…", name, rest))
        if cells:
            groups[name] = cells
    total = sum(abs(c.delta) for cs in groups.values() for c in cs)
    thresh = min_frac * total
    out: list[DiffCell] = []
    other = 0
    for name, cells in groups.items():
        if sum(abs(c.delta) for c in cells) < thresh:
            other += sum(c.delta for c in cells)
            continue
        keep = [c for c in cells if abs(c.delta) >= thresh and not c.path.endswith("/…")]
        rest = sum(c.delta for c in cells if c not in keep)
        if rest:
            keep.append(DiffCell(f"{name}/…", name, rest))
        out.extend(sorted(keep, key=lambda c: (-abs(c.delta), c.path)))
    if other:
        out.append(DiffCell("other", "other", other))
    order = {}
    for c in out:
        order[c.group] = order.get(c.group, 0) + abs(c.delta)
    return sorted(out, key=lambda c: (-order[c.group], c.group, -abs(c.delta), c.path))


# ---- IO (side-effecting) --------------------------------------------------


def _err(*a) -> None:
    print(*a, file=sys.stderr)


def load_month(root: str, month: dt.date) -> Month | None:
    """The month's scans from ``root`` (``gs://<bucket>/snapshots/cw``): one
    ``meta.json`` per published scan, ids sorting chronologically. ``lead`` =
    every scan of the last calendar day before the month. None if the month
    has no scans."""
    import fsspec

    fs, _, _ = fsspec.get_fs_token_paths(root)
    scans = sorted(
        m.group(1)
        for p in fs.glob(f"{root.split('://', 1)[-1]}/*/meta.json")
        if (m := re.search(r"/(\d{4}-\d{2}-\d{2}(?:T\d{4})?)/meta\.json$", p))
    )
    pfx = f"{month:%Y-%m}-"
    in_month = [s for s in scans if s.startswith(pfx)]
    if not in_month:
        return None
    before = [s for s in scans if s < in_month[0]]
    lead = [s for s in before if s[:10] == before[-1][:10]] if before else []
    dated_meta: list[tuple[str, dict]] = []
    for s in lead + in_month:
        with fsspec.open(f"{root}/{s}/meta.json", "rt") as f:
            dated_meta.append((s, json.load(f)))
    rows = rows_from_meta(dated_meta)
    return Month(lead=rows[: len(lead)], rows=rows[len(lead) :])


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


def _state_path(root: str, month: dt.date, channel: str, variant: str) -> str:
    """Converge-state JSON for one month's thread:
    ``digest/cw/<channel>/<variant>/<YYYY-MM>.json`` — namespaced under ``cw/``
    (gcs's prod state is ``digest/<YYYY-MM>.json``), keyed by channel so a
    staging converge never masquerades as prod, and by variant so both can be
    staged side by side."""
    base = root.rsplit("/snapshots", 1)[0]
    return f"{base}/digest/cw/{channel}/{variant}/{month:%Y-%m}.json"


def load_state(root: str, month: dt.date, channel: str, variant: str) -> dict:
    import fsspec

    try:
        with fsspec.open(_state_path(root, month, channel, variant), "rt") as f:
            return json.load(f)
    except (FileNotFoundError, OSError):
        return {}


def save_state(root: str, month: dt.date, channel: str, variant: str, state: dict) -> None:
    import fsspec

    with fsspec.open(_state_path(root, month, channel, variant), "wt", auto_mkdir=True) as f:
        json.dump(state, f, indent=2)


def primary_node(tree: dict, primary: str = PRIMARY_BUCKET) -> dict:
    """The primary bucket's node of a scan's `tree.json` (`{n: <store label>,
    …, c: [<bucket>…]}`): by name, else the first child (single-bucket scans
    named the bucket after the layer-2 file)."""
    return next((c for c in tree["c"] if c["n"] == primary), tree["c"][0])


def load_tree(root: str, scan: str) -> dict:
    """The primary bucket's node of a scan's `tree.json`
    (`snapshots/cw/<scan>/tree.json`)."""
    import fsspec

    with fsspec.open(f"{root}/{scan}/tree.json", "rt") as f:
        return primary_node(json.load(f))


def render_plot(month: Month, m: dt.date, out_path, root: str | None = None) -> None:
    """Render the month's PNG to ``out_path`` (in-process; needs the `[plot]`
    extra — matplotlib): the quota sparkline, plus — when ``root`` is given
    and the month spans two scans — the diff treemap over the OP headline's
    interval (the lead-in scan → the latest)."""
    from pathlib import Path

    from .cw_digest_plot import render

    base, last = month.base, month.rows[-1]
    diff = tree_diff(load_tree(root, base.scan), load_tree(root, last.scan), min_frac=0.01) if root and base is not last else None
    render([{"scan": r.scan, "tb": r.tb} for r in month.rows], Path(out_path), f"CoreWeave usage — {m:%B %Y}", diff=diff, diff_label=f"{_md(base.date)} → {_md(last.date)}")


def post_digest(root, m, token, channel, variant="sender", site_url=DEFAULT_URL, icons_dir=None, deploy_plot=None, reply_delay=0.0, client=None, reply_hour=REPLY_HOUR_UTC) -> dict:
    """Converge the month's thread: render+host the plot, post/edit the OP, then
    per in-month day post its reply if none exists — or, on the ``body``
    variant, edit it when a later scan of that day has landed. Persist and
    return state. ``icons_dir`` is where to write the PNG; ``deploy_plot(local,
    basename)`` publishes it and returns the host that serves it (None → the
    branch alias). ``reply_delay`` sleeps between new replies (>0 for a spaced
    backfill so Slack doesn't collapse same-sender chrome). ``client``
    overrides the thrds ``SlackClient`` (tests)."""
    import time
    from pathlib import Path

    month = load_month(root, m)
    if month is None:
        _err(f"digest: no scans for {m:%Y-%m}")
        return {}
    state = load_state(root, m, channel, variant)
    if client is None:
        from thrds.slack import SlackClient

        client = SlackClient(token, channel)

    plot_name = state.get("plot_name") or f"plot-{secrets.token_hex(16)}.png"
    base = ICONS_BASE.replace("https://", f"https://{ICONS_BRANCH}.")
    if icons_dir is not None:
        local = Path(icons_dir) / plot_name
        render_plot(month, m, local, root)
        if deploy_plot is not None:
            # the deployment-specific host serves the just-uploaded plot
            # immediately (no alias propagation race → no invalid_blocks)
            dep = deploy_plot(local, plot_name)
            if dep:
                base = dep
    plot_url = f"{base}/{plot_name}?v={int(dt.datetime.now(dt.timezone.utc).timestamp())}"
    state["plot_name"] = plot_name
    state["variant"] = variant
    # A just-deployed Pages asset isn't instantly served at the branch alias; if
    # we post before it propagates, Slack's image-block validation 500s the whole
    # message with `invalid_blocks`. Poll until the URL is live (or give up + warn).
    if icons_dir is not None and deploy_plot is not None:
        _wait_reachable(plot_url)

    body = op_body(month, m, plot_url, site_url)
    op_ts = state.get("op_ts")
    if op_ts:
        client.edit(op_ts, body)
        _err(f"digest: edited OP {op_ts} ({len(month.rows)} scans)")
    else:
        msg = client.post(body, username=f"{OP_SENDER} — {m:%B %Y}", icon_emoji=":calendar:")
        op_ts = msg.id
        state["op_ts"] = op_ts
        _err(f"digest: posted OP {op_ts}")

    posted = state.setdefault("posted", {})
    new = 0
    for day in day_rows(month, variant, reply_hour):
        r = reply(day, variant, site_url)
        have = posted.get(day.date)
        if have is None:
            if new and reply_delay:
                time.sleep(reply_delay)
            rm = client.post(r.body, thread_id=op_ts, username=r.username, icon_url=r.icon_url, icon_emoji=r.icon_emoji)
            posted[day.date] = {"ts": rm.id, "scan": day.scan}
            new += 1
            save_state(root, m, channel, variant, state)   # persist after each → a spaced backfill is resumable
            _err(f"digest: reply {day.date} ({day.scan}) -> {rm.id}")
        elif variant == "body" and have["scan"] != day.scan:
            client.edit(have["ts"], r.body)
            have["scan"] = day.scan
            save_state(root, m, channel, variant, state)
            _err(f"digest: edited reply {day.date} -> {day.scan}")

    save_state(root, m, channel, variant, state)
    return state


def redo_replies(root, m, token, channel, variant="sender", site_url=DEFAULT_URL, icons_dir=None, deploy_plot=None, reply_delay=0.0, client=None, reply_hour=REPLY_HOUR_UTC, for_real=False) -> dict:
    """Re-post the month's replies under the CURRENT day rule and retire the
    old ones (a rule change, e.g. evening→morning scan). Post-new-then-delete-
    old on purpose: no empty-thread window, and the old block vanishes at
    once. No strike/edit step — the headline lives in the sender name, which
    `chat.update` can't touch, so a strike would look broken.

    Dry-run (default) returns the plan — ``old`` replies (day, {ts, scan})
    and ``new`` (day, scan, headline) — and posts nothing. ``for_real``: the
    old ts list is stashed in the state as ``stale`` first, ``posted`` is
    cleared, the normal converge appends the new replies to the same thread,
    and only if every post succeeded are the stale ts deleted (a failed
    delete is logged and left for a re-run — a leftover old reply is
    harmless); a failed post stops before any delete, ``stale`` persisted."""
    month = load_month(root, m)
    if month is None:
        _err(f"digest: no scans for {m:%Y-%m}")
        return {}
    state = load_state(root, m, channel, variant)
    old = list(state.get("posted", {}).items())
    days = day_rows(month, variant, reply_hour)
    new = [(day.date, day.scan, reply(day, variant, site_url).username if variant == "sender" else reply(day, variant, site_url).body) for day in days]
    if not for_real:
        return {"old": old, "new": new}
    if not state.get("op_ts"):
        raise SystemExit(f"digest: no OP for {m:%Y-%m} in {channel} — nothing to re-thread under")
    state["stale"] = [e["ts"] for _, e in old] + state.get("stale", [])
    state["posted"] = {}
    save_state(root, m, channel, variant, state)
    state = post_digest(root, m, token, channel, variant, site_url=site_url, icons_dir=icons_dir, deploy_plot=deploy_plot, reply_delay=reply_delay, client=client, reply_hour=reply_hour)
    if client is None:
        from thrds.slack import SlackClient

        client = SlackClient(token, channel)
    failed = []
    for ts in state.pop("stale", []):
        try:
            client.delete(ts, orphans_ok=True)
            _err(f"digest: deleted old reply {ts}")
        except Exception as e:  # noqa: BLE001 — leave it for a re-run; an old reply lingering is harmless
            _err(f"digest: WARN could not delete old reply {ts}: {e}")
            failed.append(ts)
    if failed:
        state["stale"] = failed
    save_state(root, m, channel, variant, state)
    return state
