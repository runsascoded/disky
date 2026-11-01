"""The monthly usage digest engine (`dt-cloud digest`): one Slack thread (or
its Discord twin) per calendar month — an OP edited in place as scans land
(month-to-date headline, per-ISO-week bullets, a hosted plot) plus one reply
per scan or per day, each reply's arrow colour-coding its trend. Posts through
`thrds`; converge state is a per-month JSON beside the snapshots, so every run
is idempotent and an interrupted backfill resumes.

What a deployment chooses is DATA, a :class:`DigestConfig` (a preset in
:data:`PRESETS`, overlaid by ``--config FILE``): its template, title, site,
snapshot root, state layout, plot host, bucket labels and quotas, prices.
What differs in the posts lives in two TEMPLATES — opinionated styles,
neither intrinsic to one store:

- ``gcs`` (`digest_gcs`): a reply per day (its first scheduled scan), the headline as the sender name,
  $/mo by storage class in the body, the site's storage-class image.
- ``cw`` (`digest_cw`): a reply per UTC day in two variants (``sender``: the
  headline as the sender name, posted once from the morning scan — optionally
  preceded by a ``provisional`` reply the earlier scans edit; ``body``: bold
  in the body, edited as the day's scans land), multi-bucket `% of quota
  (free)` clauses when quotas are configured, a quota sparkline + diff
  treemap plot.

A template is a small object (`load`, `op_body`, `units`, `provisional`,
`render_plot`); this module holds everything else: arrow math, formatters,
scan ids and `?d=` link tokens, Discord emoji, snapshot listing, state IO,
plot hosting (`wrangler pages deploy`), and the converge shells
(`converge_slack`, `converge_slack_now`, `converge_discord`,
`redo_replies`). Design: specs/done/digest-unification.md; history:
specs/done/slack-digest-shape-c.md, specs/cw-slack-digest.md."""
from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Protocol

from .scan_id import META_PATH, min_slug, scan_slug, scan_time, start_key, times_needed

TIB = 1024**4
GIB = 1024**3
# Weekly-halving arrow buckets: |dpct| >= THRESH[i] -> deg (i+1)*10 (capped 80).
THRESH = [0.39, 0.78, 1.5, 3.1, 6.25, 12.5, 25, 50]
MINUS = "−"  # matches the site's unicode minus
# A Slack link's ↗: the NE arrow + the text-presentation selector (U+FE0E) renders as a font glyph in link colour; a
# bare ↗ is emoji-ized by Slack into :arrow_upper_right:, which a link can't wrap
LINK_ARROW = "\u2197\ufe0e"
HOURS_PER_WEEK = 168.0
# bump when the av_deg glyphs change: Slack caches avatars per-URL at post
# time, so a stable URL serves MIXED generations after a redesign.
AVATAR_REV = 4


@dataclass(frozen=True)
class Units:
    """The digest's size unit: its ``name`` (`TiB`), ``bytes`` per unit, and the
    ``short`` suffix of the reply tail's clauses (`Ti`)."""

    name: str
    bytes: int
    short: str


# `units:` in a digest config: binary TiB (the default), or decimal TB (a store billed in TB, e.g. Hugging Face)
UNITS = {"TiB": Units("TiB", TIB, "Ti"), "TB": Units("TB", 10**12, "T")}


# ---- config -------------------------------------------------------------------


@dataclass(frozen=True)
class Quota:
    """A bucket's quota: ``bytes``, its long ``name`` (the OP + plot: `1 PB`)
    and an optional ``short`` label (the reply tail: `1P`; None → SI from
    ``bytes``, see `digest_cw._qlabel`). ``classes``: the storage-class ids it
    counts (`meta.json` `class_bytes`; Hugging Face's public allowance counts
    class 1, its private one class 5); empty: every byte."""

    bytes: int
    name: str
    short: str | None = None
    classes: tuple[int, ...] = ()


@dataclass(frozen=True)
class Bucket:
    """Per-bucket display config: the reply tail's short ``label`` (default:
    the bucket name) and its ``quota`` (None: the tail shows raw TiB). Buckets
    naming the same ``zone`` share one quota (a CoreWeave zone quota covers
    every bucket in the zone): they must carry equal quotas, and the tail
    renders them as one clause against it. ``path`` is the bucket's path on
    the site, its links' target (None: ``/<bucket>``, a union-of-buckets
    site's; ``""``: the site root, a single-bucket store served there).
    ``more``: further quotas, each beside ``quota`` wherever it renders (YAML:
    ``quota`` as a list), e.g. a private allowance beside a public one; the
    plot draws ``quota`` only."""

    label: str | None = None
    quota: Quota | None = None
    zone: str | None = None
    path: str | None = None
    more: tuple[Quota, ...] = ()

    @property
    def quotas(self) -> tuple[Quota, ...]:
        return (self.quota, *self.more) if self.quota else ()


@dataclass(frozen=True)
class DigestConfig:
    """Everything a deployment passes the digest; no code forks.

    ``root``/``state``/``discord_state`` are templates: ``{DATA_BUCKET}`` reads
    the env (required); ``state`` is the Slack state dir
    under the data root (``{channel}``, ``{variant}`` interpolated),
    ``discord_state`` the Discord one (``{webhook}`` = the webhook id — a
    webhook can only edit its own messages), ``discord_webhook_env`` the env
    var holding the Discord webhook URL (None: `-w` only). ``icons_dir`` is where the plot is
    rendered and `wrangler pages deploy`ed from (relative: the cwd's or the
    repo checkout's); it lands on ``plot_project``'s ``plot_branch``, served at
    ``plot_base`` unless wrangler names its deployment URL. ``primary`` +
    ``buckets`` (cw template) pick the headline bucket and label/quota each;
    ``prices`` (gcs template) are $/GiB-month by storage-class id.
    ``op_icon``: the month OP's sender icon (and the cw day line's), a URL or a
    file under ``icons_base`` (the deployment's mark, e.g. ``mark.png``); unset, 📆.
    ``units`` (cw template): `TiB` (default) or `TB`, every size the digest
    renders (text and plot). ``provisional`` (cw ``sender``): post a day's early scans as one
    provisional reply, edited in place, replaced by the final reply when the
    day's reply scan lands (see `digest_cw`). ``alert_pct`` (cw): a quota at
    or over this percent gets a `:warning:` in the daily replies, and the
    month's thread one alert reply also sent to the channel (Slack
    `reply_broadcast`), edited as its numbers move and deleted once every quota
    is back under; None: neither."""

    template: str
    title: str
    site_url: str
    root: str
    state: str
    discord_state: str = "digest/discord/{webhook}"
    discord_webhook_env: str | None = None
    icons_base: str = ""
    icons_dir: str = "job/icons"
    op_icon: str = ""
    plot_project: str = ""
    plot_branch: str = "main"
    plot_base: str = ""
    variant: str = "sender"
    reply_hour: int = 12
    provisional: bool = False
    primary: str | None = None
    buckets: dict[str, Bucket] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)
    units: str = "TiB"
    alert_pct: float | None = None

    @property
    def unit(self) -> Units:
        return UNITS[self.units]

    def need(self, key: str) -> str:
        """A deployment-resource field (``icons_base``, ``plot_base``,
        ``plot_project``): its value, or an exit naming it when it's unset.
        The presets carry no deployment's hosts; the ``-C`` file sets them."""
        v = getattr(self, key)
        if not v:
            raise SystemExit(f"digest config `{key}` is unset: set it in the deployment's config file (-C)")
        return v

    def op_sender(self) -> dict[str, str]:
        """The month's OP (and the cw template's day line) sender icon: ``op_icon`` (a URL, or a file under ``icons_base``, e.g.
        the deployment's mark) as ``icon_url``, else the 📆 emoji."""
        if not self.op_icon:
            return {"icon_emoji": ":calendar:"}
        return {"icon_url": self.op_icon if "://" in self.op_icon else f"{self.need('icons_base')}/{self.op_icon}"}

    @property
    def primary_quota(self) -> Quota | None:
        b = self.buckets.get(self.primary) if self.primary else None
        return b.quota if b else None

    @property
    def primary_quotas(self) -> tuple[Quota, ...]:
        b = self.buckets.get(self.primary) if self.primary else None
        return b.quotas if b else ()

    @property
    def host(self) -> str:
        """The site's host, the plot's corner credit."""
        return self.site_url.split("://", 1)[-1].rstrip("/")

    @property
    def slug(self) -> str:
        """`GCS usage` → `gcs-usage` (the Discord plot attachment's name)."""
        return re.sub(r"\s+", "-", self.title.strip().lower())

    def resolve_root(self) -> str:
        from .deploy import data_bucket
        return self.root.format(DATA_BUCKET=data_bucket())


PRESETS: dict[str, DigestConfig] = {
    # Template shape only: a deployment's site, hosts, buckets and quotas come
    # from its config file (``-C``); an empty ``site_url`` reads ``$SITE_URL``.
    "gcs": DigestConfig(
        template="gcs",
        title="Storage usage",
        site_url="",
        root="gs://{DATA_BUCKET}/snapshots",
        state="digest",
        # US list $/GiB-mo by GCS storage class id (1 Standard / 2 Nearline / 3 Coldline / 4 Archive)
        prices={"1": 0.02, "2": 0.01, "3": 0.004, "4": 0.0012},
    ),
    "cw": DigestConfig(
        template="cw",
        title="Storage usage",
        site_url="",
        root="gs://{DATA_BUCKET}/snapshots",
        # keyed by channel so a staging converge never masquerades as prod, and by
        # variant so both can be staged side by side
        state="digest/{channel}/{variant}",
    ),
}

_UNITS = {"": 1, "B": 1, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "KI": 2**10, "MI": 2**20, "GI": 2**30, "TI": 2**40, "PI": 2**50}


def parse_bytes(v: int | str) -> int:
    """`910 TiB` / `100Ti` / `1 PB` / `10**15`-style int → bytes."""
    if isinstance(v, int):
        return v
    m = re.fullmatch(r"\s*([\d.]+)\s*([KMGTP]i?)?B?\s*", str(v), re.I)
    if not m:
        raise ValueError(f"not a size: {v!r}")
    return round(float(m.group(1)) * _UNITS[(m.group(2) or "").upper()])


TEMPLATES = ("gcs", "cw")
# the scalar fields' accepted YAML types (`buckets` / `prices` are parsed below)
_SCALAR_TYPES: dict[str, tuple[type, ...]] = {
    "template": (str,), "title": (str,), "site_url": (str,), "root": (str,), "state": (str,),
    "discord_state": (str,), "discord_webhook_env": (str, type(None)), "icons_base": (str,),
    "icons_dir": (str,), "op_icon": (str,), "plot_project": (str,), "plot_branch": (str,), "plot_base": (str,),
    "variant": (str,), "reply_hour": (int,), "provisional": (bool,), "primary": (str, type(None)),
    "units": (str,), "alert_pct": (int, float, type(None)),
}


def _mapping(where: str, d: Any) -> dict:
    if not isinstance(d, dict):
        raise ValueError(f"digest config {where}: expected a mapping, got {type(d).__name__}")
    return d


def _keys(where: str, d: Any, allowed: set[str], required: set[str] = frozenset()) -> dict:
    """``d`` as a mapping with only ``allowed`` keys and every ``required`` one."""
    if bad := set(_mapping(where or "file", d)) - allowed:
        raise ValueError(f"unknown digest config keys{f' in {where}' if where else ''}: {sorted(bad)}")
    if missing := required - set(d):
        raise ValueError(f"digest config {where}: missing {sorted(missing)}")
    return d


def _bucket(name: str, b: dict | None) -> Bucket:
    b = _keys(f"buckets.{name}", b or {}, {"label", "quota", "zone", "path"})
    for k in ("label", "zone", "path"):
        if b.get(k) is not None and not isinstance(b[k], str):
            raise ValueError(f"digest config buckets.{name}.{k}: expected a string, got {b[k]!r}")
    q = b.get("quota")
    if q is None:
        return Bucket(b.get("label"), zone=b.get("zone"), path=b.get("path"))
    qs = [_quota(f"buckets.{name}.quota[{i}]", x) for i, x in enumerate(q)] if isinstance(q, list) else [_quota(f"buckets.{name}.quota", q)]
    if not qs:
        raise ValueError(f"digest config buckets.{name}.quota: an empty list")
    if b.get("zone") is not None and (len(qs) > 1 or qs[0].classes):
        raise ValueError(f"digest config buckets.{name}: a zone's quota is one quota over every class")
    return Bucket(b.get("label"), qs[0], b.get("zone"), b.get("path"), tuple(qs[1:]))


def _quota(where: str, q: Any) -> Quota:
    q = _keys(where, q, {"bytes", "name", "short", "classes"}, {"bytes", "name"})
    cs = q.get("classes", [])
    if not isinstance(cs, list) or not all(isinstance(c, int) and not isinstance(c, bool) for c in cs):
        raise ValueError(f"digest config {where}.classes: expected a list of storage-class ids, got {cs!r}")
    return Quota(parse_bytes(q["bytes"]), str(q["name"]), None if q.get("short") is None else str(q["short"]), tuple(cs))


def _check_zones(buckets: dict[str, Bucket]) -> None:
    """Buckets sharing a ``zone`` share its quota, so they must state the same one."""
    seen: dict[str, tuple[str, Quota | None]] = {}
    for name, b in buckets.items():
        if b.zone is None:
            continue
        first, q = seen.setdefault(b.zone, (name, b.quota))
        if q != b.quota:
            raise ValueError(f"digest config buckets.{name}: zone {b.zone!r} quota differs from buckets.{first}'s")


def config_from_dict(d: dict, base: DigestConfig | None = None) -> DigestConfig:
    """A config from a parsed YAML/JSON mapping, overlaid on ``base`` (default:
    the preset its ``template`` names). ``buckets`` map names to ``{label,
    quota: {bytes, name, short, classes}, zone, path}`` (``quota`` may be a list) (``bytes`` may be `910 TiB`), ``prices``
    storage-class ids to $/GiB-month. Validated: unknown keys (at any level),
    a wrong-typed value, an unknown template or ``units``, or an out-of-range
    ``reply_hour`` raise ``ValueError``."""
    d = dict(_keys("", d, {f.name for f in fields(DigestConfig)}))
    for k, types in _SCALAR_TYPES.items():
        # bool is an int subclass; `reply_hour: true` is a typo, not hour 1
        if k in d and (not isinstance(d[k], types) or (isinstance(d[k], bool) and bool not in types)):
            want = " or ".join("null" if t is type(None) else t.__name__ for t in types)
            raise ValueError(f"digest config {k}: expected {want}, got {d[k]!r}")
    if base is None and "template" not in d:
        raise ValueError(f"digest config: no template (one of {TEMPLATES}): a deployment names its own")
    if "template" in d and d["template"] not in TEMPLATES:
        raise ValueError(f"digest config template: {d['template']!r} is not one of {TEMPLATES}")
    if "units" in d and d["units"] not in UNITS:
        raise ValueError(f"digest config units: {d['units']!r} is not one of {tuple(UNITS)}")
    if d.get("alert_pct") is not None and not 0 < d["alert_pct"] <= 100:
        raise ValueError(f"digest config alert_pct: {d['alert_pct']} is not a percent (0–100]")
    if not 0 <= d.get("reply_hour", 0) <= 23:
        raise ValueError(f"digest config reply_hour: {d['reply_hour']} is not a UTC hour (0–23)")
    base = base or PRESETS[d["template"]]
    if "buckets" in d:
        d["buckets"] = {str(name): _bucket(name, b) for name, b in _mapping("buckets", d["buckets"] or {}).items()}
        _check_zones(d["buckets"])
    if "prices" in d:
        prices = _mapping("prices", d["prices"] or {})
        if bad := {k: v for k, v in prices.items() if not isinstance(v, (int, float)) or isinstance(v, bool)}:
            raise ValueError(f"digest config prices: expected numbers, got {bad}")
        d["prices"] = {str(k): float(v) for k, v in prices.items()}
    return replace(base, **d)


def load_config(template: str, path: str | Path | None = None) -> DigestConfig:
    """The ``template`` preset, overlaid by the YAML/JSON file at ``path``."""
    if path is None:
        return PRESETS[template]
    import yaml

    return config_from_dict({"template": template, **_mapping(str(path), yaml.safe_load(Path(path).read_text()) or {})})


# ---- pure helpers ---------------------------------------------------------------


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


def _tb(v: float) -> str:
    return f"+{v:.1f}" if v >= 0 else f"{MINUS}{abs(v):.1f}"


def _pct(dtb: float, tb: float) -> str:
    prev = tb - dtb
    return f"{abs(dtb / prev * 100) if prev else 0:.1f}"


def _pct_val(dtb: float, tb: float) -> float:
    prev = tb - dtb
    return dtb / prev * 100 if prev else 0.0


def _md(date: str) -> str:
    d = dt.date.fromisoformat(date[:10])  # a scan id's day (`YYYY-MM-DD[THHMM]`)
    return f"{d.month}/{d.day}"


# A scan id's UTC instant: `dt_cloud.scan_id`'s, the one definition.
scan_ts = scan_time


def _dlink(scan: str, slug: str | None = None) -> str:
    """A scan's `?d=` slug in a link: its canonical one (`slug`, from `scan_slugs`), else its minute (`scan_slug`)."""
    return slug or scan_slug(scan)


def scan_slugs(dated_meta: list[tuple[str, dict]], now: dt.datetime | None = None) -> dict[str, str]:
    """Each scan's canonical `?d=` slug among ``dated_meta``'s scans (`min_slug`: ``261008``, ``26100904``,
    ``2610091236``), as the site writes it: a date-only scan sharing its day keyed by its ``meta.started``
    (`start_key`; only those, `times_needed`, as the site reads them). ``dated_meta`` must hold every scan of each
    day it touches (`load_window`'s lead is a whole day). ``now``: default the current time."""
    scans = [s for s, _ in dated_meta]
    meta = dict(dated_meta)
    times = {s: k for s in times_needed(scans) if (k := start_key(s, meta[s].get("started")))}
    return {s: min_slug(s, scans, times, now) for s in scans}


def _span(a: dt.datetime, b: dt.datetime) -> str:
    """`?d=` look-back token for the interval a→b (`1d12h`, `7d`, `12h`),
    matching the site's `encodeSpan`."""
    # round to whole hours first so a scan that drifted a minute (00:02 →
    # 12:01) still reads `1d`, not `24h`
    hours, days = divmod(round((b - a).total_seconds() / 3600), 24)[::-1]
    return (f"{days}d" if days else "") + (f"{hours}h" if hours else "") or "0h"


#: The OP's window reaches back this many days before the latest scan (specs/digest-rolling-window.md).
WINDOW_DAYS = 30


def window_start(month: dt.date, today: dt.date) -> dt.date:
    """The first day of ``month``'s OP window: the longer of the last ``WINDOW_DAYS`` days to ``today`` (the latest
    scan's date) and the month to date. On 11/1 it's 10/2; once ``today`` is ``WINDOW_DAYS`` days into the month,
    the month's 1st."""
    return min(month.replace(day=1), today - dt.timedelta(days=WINDOW_DAYS))


def week_of(d: dt.date) -> dt.date:
    """``d``'s ISO week's Monday (a weekly summary's key)."""
    return d - dt.timedelta(days=d.weekday())


def prior_month(month: dt.date) -> dt.date:
    """The 1st of the month before ``month``'s."""
    return (month.replace(day=1) - dt.timedelta(days=1)).replace(day=1)


def window_label(start: dt.date, since: str) -> str:
    """The OP's look-back figure's label: `last 30d` from a scan at or before ``start`` (the window's first day), else
    `since M/D` (``since``: the scan it starts from; a store with less history than the window)."""
    return f"last {WINDOW_DAYS}d" if since[:10] <= start.isoformat() else f"since {_md(since)}"


def prev_month_link(month: dt.date, url: str | None) -> list[str]:
    """The OP's link to the previous month's thread (`[← October](url)`), as lines; none without one."""
    return [f"[← {prior_month(month):%B}]({url})"] if url else []


#: The quota alert reply's sender icon (`converge_slack`).
ALERT_ICON = ":warning:"


@dataclass(frozen=True)
class Reply:
    """A reply's post parameters: ``username``/``icon_url``/``icon_emoji`` are
    fixed at post time (Slack), ``body`` is what an edit can change."""

    username: str
    body: str
    icon_url: str | None = None
    icon_emoji: str | None = None


@dataclass(frozen=True)
class Unit:
    """One reply slot in the month's thread: its state ``key`` (the UTC date),
    the ``scan`` it renders, and the rendered ``reply``."""

    key: str
    scan: str
    reply: Reply


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


# ---- templates ------------------------------------------------------------------


class PlotUnavailable(Exception):
    """A template can't draw its plot here (e.g. the site draws it, and there is no site token): fatal to a post,
    a note in a dry run, whose text needs no image."""


class Template(Protocol):
    """A digest style over one deployment's :class:`DigestConfig`.

    ``variants`` are its reply styles (the first is the default);
    ``edited_variants`` re-edit a day's reply when a later scan of it lands;
    ``track_scan`` stores each posted reply as ``{ts, scan}`` (else the bare
    ts — the gcs template's existing state format). ``provisional`` is the
    still-open unit's stand-in reply (``cfg.provisional``), or None."""

    cfg: DigestConfig
    variants: tuple[str, ...]
    edited_variants: tuple[str, ...]
    track_scan: bool

    def load(self, root: str, month: dt.date) -> Any | None: ...
    def n_scans(self, data: Any) -> int: ...
    def op_body(self, data: Any, month: dt.date, plot_url: str | None, prev_url: str | None = None) -> str: ...
    def units(self, data: Any, variant: str, platform: str = "slack") -> list[Unit]: ...
    def provisional(self, data: Any, variant: str) -> Unit | None: ...
    def alert(self, data: Any) -> str | None: ...
    def render_plot(self, data: Any, month: dt.date, out: Path, root: str | None = None) -> None: ...


def template(cfg: DigestConfig) -> Template:
    if not cfg.site_url:
        from .deploy import site_url

        cfg = replace(cfg, site_url=site_url())
    if cfg.template == "gcs":
        from .digest_gcs import Gcs

        return Gcs(cfg)
    if cfg.template == "cw":
        from .digest_cw import Cw

        return Cw(cfg)
    raise ValueError(f"unknown digest template {cfg.template!r} (gcs | cw)")


# ---- IO (side-effecting) --------------------------------------------------------


def _err(*a) -> None:
    print(*a, file=sys.stderr)


def list_scans(root: str) -> list[str]:
    """The scan ids under ``root`` (one ``<id>/meta.json`` per published scan),
    sorted — ids sort chronologically. ``root``: a local dir or any URL
    `blobfs` resolves (`gs://`, `r2://`, `s3://`)."""
    from disk_tree import blobfs

    return sorted(
        m.group(1)
        for p in blobfs.glob(blobfs.join(root.rstrip("/"), "*/meta.json"))
        if (m := META_PATH.search(p))
    )


def load_window(root: str, month: dt.date, scans: list[str] | None = None) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]] | None:
    """``(lead, in_month)`` ``(scan id, meta.json)`` pairs for ``month``: the
    month's scans, and ``lead`` = every scan of the last calendar day before it
    (the first delta's baseline; empty for the first month ever). None if the
    month has no scans. ``scans``: ``list_scans(root)``, if already listed."""
    scans = list_scans(root) if scans is None else scans
    pfx = f"{month:%Y-%m}-"
    in_month = [s for s in scans if s.startswith(pfx)]
    if not in_month:
        return None
    before = [s for s in scans if s < in_month[0]]
    lead = [s for s in before if s[:10] == before[-1][:10]] if before else []

    return read_metas(root, lead), read_metas(root, in_month)


def read_metas(root: str, scans: Iterable[str]) -> list[tuple[str, dict]]:
    """``(scan id, meta.json)`` for each of ``scans``, under ``root``."""
    from disk_tree import blobfs

    return [(s, json.loads(blobfs.read_text(f"{root.rstrip('/')}/{s}/meta.json"))) for s in scans]


def window_scans(scans: list[str], month: dt.date, before: str) -> list[str]:
    """The scans (sorted ids) before ``before`` (the month's lead-in) that ``month``'s OP window draws: every scan
    from the Monday of `window_start`'s week (its first weekly summary) on, after the last day's scans before that
    (the first summary's baseline), whole days; the window ends at the month's latest scan."""
    in_month = [s for s in scans if s.startswith(f"{month:%Y-%m}-")]
    if not in_month:
        return []
    mon = week_of(window_start(month, dt.date.fromisoformat(in_month[-1][:10]))).isoformat()
    older = [s for s in scans if s[:10] < mon]
    first = older[-1][:10] if older else mon
    return [s for s in scans if first <= s[:10] and s < before]


def state_path(root: str, month: dt.date, state_dir: str) -> str:
    """The converge-state JSON for one month's thread: ``<data
    root>/<state_dir>/<YYYY-MM>.json``, beside ``root``'s `snapshots/`."""
    base = root.rsplit("/snapshots", 1)[0]
    return f"{base}/{state_dir}/{month:%Y-%m}.json"


def load_state(path: str) -> dict:
    """A month's converge state (a local path or a `blobfs` URL); `{}` before
    the first converge."""
    from disk_tree import blobfs

    try:
        return json.loads(blobfs.read_text(path))
    except (FileNotFoundError, OSError):
        return {}


def save_state(path: str, state: dict) -> None:
    from disk_tree import blobfs

    if not blobfs.is_url(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    blobfs.write_text(path, json.dumps(state, indent=2))


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


def pages_deploy(icons_dir: Path, project: str, branch: str) -> str | None:
    """`wrangler pages deploy` ``icons_dir`` (the CORS `_headers` + the fresh
    plot) to ``project``'s ``branch``; needs CLOUDFLARE_* + wrangler. Returns
    the deployment-specific URL (served instantly), which the OP image uses to
    avoid racing alias propagation (→ Slack `invalid_blocks`)."""
    import shutil
    import subprocess

    if not project:
        raise SystemExit("digest config `plot_project` is unset: set it in the deployment's config file (-C)")
    # The job image installs wrangler globally (`npm install -g`) but has
    # no `npx` shim, so prefer the binary; `npx` only serves a laptop run.
    wrangler = [shutil.which("wrangler")] if shutil.which("wrangler") else ["npx", "wrangler"] if shutil.which("npx") else None
    if wrangler is None:
        raise SystemExit("digest: neither `wrangler` nor `npx` on PATH — can't publish the plot")
    r = subprocess.run(
        [*wrangler, "pages", "deploy", str(icons_dir), "--project-name", project, "--branch", branch, "--commit-dirty=true"],
        check=True, capture_output=True, text=True,
    )
    _err(r.stdout)
    m = re.search(rf"https://[a-z0-9]+\.{re.escape(project)}\.pages\.dev", r.stdout + r.stderr)
    return m.group(0) if m else None


def _record(tpl: Template, ts: str, scan: str) -> str | dict:
    return {"ts": ts, "scan": scan} if tpl.track_scan else ts


def _ts(rec: str | dict) -> str:
    return rec["ts"] if isinstance(rec, dict) else rec


def converge_slack(tpl: Template, root: str, month: dt.date, client, channel: str, variant: str | None = None, *, icons_dir=None, deploy_plot=None, reply_delay: float = 0.0, edit_replies: bool = False) -> dict:
    """Converge the month's Slack thread: render+host the plot, post/edit the
    OP, then per reply unit post it if none exists — or, on an edited variant,
    edit it when a later scan has landed. Then the provisional reply
    (``cfg.provisional``): delete every one whose unit is no longer open
    (its final reply just posted, or a delete that failed last run), and
    post the open unit's — or edit it when a later scan has landed. Persist
    and return state; the provisional's ``{ts, scan}`` lives under
    ``state["provisional"][<key>]`` (absent when there is none), so a re-run
    edits rather than duplicates.

    ``icons_dir`` is where to write the PNG; ``deploy_plot(local, basename)``
    publishes it and returns the host serving it (None → ``cfg.plot_base``).
    ``reply_delay`` sleeps between new replies (>0 for a spaced backfill, so
    Slack doesn't collapse same-sender chrome). ``edit_replies`` re-edits every
    already-posted reply to its current body (a format change's backfill; the
    sender variant's name and avatar are fixed at post time, so only its body
    changes). ``client`` is a thrds ``SlackClient`` (or a fake)."""
    import time

    cfg = tpl.cfg
    variant = variant or tpl.variants[0]
    data = tpl.load(root, month)
    if not data:
        _err(f"digest: no scans for {month:%Y-%m}")
        return {}
    path = state_path(root, month, cfg.state.format(channel=channel, variant=variant))
    state = load_state(path)

    plot_name = state.get("plot_name") or f"plot-{secrets.token_hex(16)}.png"
    base = cfg.need("plot_base")
    if icons_dir is not None:
        local = Path(icons_dir) / plot_name
        tpl.render_plot(data, month, local, root)
        if deploy_plot is not None:
            # the deployment-specific host serves the just-uploaded plot
            # immediately (no alias propagation race → no invalid_blocks)
            dep = deploy_plot(local, plot_name)
            if dep:
                base = dep
    plot_url = f"{base}/{plot_name}?v={int(dt.datetime.now(dt.timezone.utc).timestamp())}"
    state["plot_name"] = plot_name
    if len(tpl.variants) > 1:
        state["variant"] = variant
    # A just-deployed Pages asset isn't instantly served at an alias; if we post
    # before it propagates, Slack's image-block validation 500s the whole message
    # with `invalid_blocks`. Poll until the URL is live (or give up + warn).
    if icons_dir is not None and deploy_plot is not None:
        _wait_reachable(plot_url)

    # the previous month's thread, linked from the OP (its converge state, same channel and variant)
    prev_op = load_state(state_path(root, prior_month(month), cfg.state.format(channel=channel, variant=variant))).get("op_ts")
    body = tpl.op_body(data, month, plot_url, client.permalink(prev_op) if prev_op else None)
    op_ts = state.get("op_ts")
    if op_ts:
        client.edit(op_ts, body)
        _err(f"digest: edited OP {op_ts} ({tpl.n_scans(data)} scans)")
    else:
        op_ts = client.post(body, username=f"{cfg.title} — {month:%B %Y}", **cfg.op_sender()).id
        state["op_ts"] = op_ts
        _err(f"digest: posted OP {op_ts}")

    posted = state.setdefault("posted", {})
    new = 0
    for u in tpl.units(data, variant):
        r = u.reply
        have = posted.get(u.key)
        if have is None:
            if new and reply_delay:
                time.sleep(reply_delay)
            rm = client.post(r.body, thread_id=op_ts, username=r.username, icon_url=r.icon_url, icon_emoji=r.icon_emoji)
            posted[u.key] = _record(tpl, rm.id, u.scan)
            new += 1
            save_state(path, state)   # persist after each → a spaced backfill is resumable
            _err(f"digest: reply {u.key} ({u.scan}) -> {rm.id}")
        elif edit_replies or (variant in tpl.edited_variants and have["scan"] != u.scan):
            client.edit(_ts(have), r.body)
            if isinstance(have, dict):
                have["scan"] = u.scan
            save_state(path, state)
            _err(f"digest: edited reply {u.key} -> {u.scan}")

    open_ = tpl.provisional(data, variant) if cfg.provisional else None
    provs = state.setdefault("provisional", {})
    for key in [k for k in provs if open_ is None or k != open_.key]:
        try:
            client.delete(provs[key]["ts"], orphans_ok=True)
        except Exception as e:  # noqa: BLE001 — the next run retries; a lingering provisional reads `so far`
            _err(f"digest: WARN could not delete provisional reply {key}: {e}")
            continue
        del provs[key]
        save_state(path, state)
        _err(f"digest: deleted provisional reply {key}")
    if open_ is not None:
        have = provs.get(open_.key)
        r = open_.reply
        if have is None:
            rm = client.post(r.body, thread_id=op_ts, username=r.username, icon_url=r.icon_url, icon_emoji=r.icon_emoji)
            provs[open_.key] = {"ts": rm.id, "scan": open_.scan}
            save_state(path, state)
            _err(f"digest: provisional reply {open_.key} ({open_.scan}) -> {rm.id}")
        elif have["scan"] != open_.scan:
            client.edit(have["ts"], r.body)
            have["scan"] = open_.scan
            save_state(path, state)
            _err(f"digest: edited provisional reply {open_.key} -> {open_.scan}")
    if not provs:
        state.pop("provisional")

    # The quota alert: one reply, also in the channel, while any quota is at or over `alert_pct`; Slack can't take a
    # reply back out of the channel, so a resolved alert is deleted (its days' replies keep their `:warning:`s)
    text, have = tpl.alert(data), state.get("alert")
    if text is None and have is not None:
        try:
            client.delete(have["ts"], orphans_ok=True)
        except Exception as e:  # noqa: BLE001 — the next run retries; a lingering alert names its scan
            _err(f"digest: WARN could not delete the quota alert: {e}")
        else:
            del state["alert"]
            save_state(path, state)
            _err("digest: deleted the quota alert (every quota back under, or its month closed)")
    elif text is not None and have is None:
        rm = client.post(text, thread_id=op_ts, broadcast=True, username=f"{cfg.title}: quota alert", icon_emoji=ALERT_ICON)
        state["alert"] = {"ts": rm.id, "body": text}
        save_state(path, state)
        _err(f"digest: posted the quota alert -> {rm.id}")
    elif text is not None and have["body"] != text:
        client.edit(have["ts"], text)
        have["body"] = text
        save_state(path, state)
        _err("digest: edited the quota alert")

    save_state(path, state)
    return state


def converge_slack_now(tpl: Template, root: str, month: dt.date, client, channel: str, variant: str | None = None, **kw) -> dict:
    """The scheduled run's converge: ``month``'s thread (the current one),
    after first re-converging the PREVIOUS month's if its state still holds a
    provisional reply. That happens when a month's last day never got its
    reply scan: only a scan after the month (this month's first) lets the
    stand-in rule give that day a final reply, and only then can the
    provisional go. Idempotent like `converge_slack`; ``kw`` passes through."""
    variant = variant or tpl.variants[0]
    prev = (month - dt.timedelta(days=1)).replace(day=1)
    prev_state = load_state(state_path(root, prev, tpl.cfg.state.format(channel=channel, variant=variant)))
    if prev_state.get("provisional") or prev_state.get("alert"):
        _err(f"digest: {prev:%Y-%m} still has a {'provisional reply' if prev_state.get('provisional') else 'quota alert'} — closing it first")
        converge_slack(tpl, root, prev, client, channel, variant, **kw)
    return converge_slack(tpl, root, month, client, channel, variant, **kw)


def redo_replies(tpl: Template, root: str, month: dt.date, client, channel: str, variant: str | None = None, *, since: dt.date | None = None, icons_dir=None, deploy_plot=None, reply_delay: float = 0.0, for_real: bool = False) -> dict:
    """Re-post the month's replies under the CURRENT unit rule and retire the
    old ones (a rule change, e.g. evening→morning scan). Post-new-then-delete-
    old on purpose: no empty-thread window, and the old block vanishes at
    once. No strike/edit step — the headline lives in the sender name, which
    `chat.update` can't touch, so a strike would look broken.

    ``since`` redoes only the days from it on. An earlier day keeps its posted
    reply, re-keyed to the current unit's key (the record under that exact key,
    else under the unit's scan, else the day's earliest key); the day's other replies (an old rule's extras)
    stay in the thread and move to the state's ``extra`` (never edited; a later redo over their days deletes
    them, as an unrestricted one does all). A unit with no reply that day is
    posted by the converge, after the redone days.

    Dry-run (default) returns the plan — ``old`` replies (key, record), ``new``
    (key, scan, headline), ``rekey`` (current key, old key) and ``left`` (old
    keys kept in the thread, as ``extra``) — and posts nothing. ``for_real``: the old
    ts list is stashed in the state as ``stale`` first, ``posted`` is
    cleared, the normal converge appends the new replies to the same thread,
    and only if every post succeeded are the stale ts deleted (a failed
    delete is logged and left for a re-run — a leftover old reply is
    harmless); a failed post stops before any delete, ``stale`` persisted."""
    variant = variant or tpl.variants[0]
    data = tpl.load(root, month)
    if not data:
        _err(f"digest: no scans for {month:%Y-%m}")
        return {}
    path = state_path(root, month, tpl.cfg.state.format(channel=channel, variant=variant))
    state = load_state(path)
    plan, kept = _redo_plan(tpl.units(data, variant), _replies(state), since, lambda r: r.username if variant == "sender" else r.body)
    if not for_real:
        return plan
    if not state.get("op_ts"):
        raise SystemExit(f"digest: no OP for {month:%Y-%m} in {channel} — nothing to re-thread under")
    _stash(state, plan, kept)
    save_state(path, state)
    state = converge_slack(tpl, root, month, client, channel, variant, icons_dir=icons_dir, deploy_plot=deploy_plot, reply_delay=reply_delay)
    _delete_stale(state, lambda ts: client.delete(ts, orphans_ok=True))
    save_state(path, state)
    return state


def _redo_plan(units: Iterable[Any], posted: dict[str, Any], since: dt.date | None, headline) -> tuple[dict, dict[str, Any]]:
    """A redo's plan over a thread's ``posted`` replies and the current ``units``
    (see `redo_replies`): ``old`` (key, record) to delete, ``new`` (key, scan,
    ``headline(reply)``) to post, ``rekey`` (current key, old key) and ``left``
    (old keys the state keeps as ``extra``) for the days before ``since``; and the
    re-keyed records the state keeps."""
    cut = since.isoformat() if since else ""
    units = list(units)
    old = [(k, e) for k, e in posted.items() if k[:10] >= cut]
    new = [(u.key, u.scan, headline(u.reply)) for u in units if u.key[:10] >= cut]
    kept: dict[str, Any] = {}
    rekey = []
    for u in units:
        if u.key[:10] >= cut:
            continue
        day = sorted((k for k in posted if k[:10] == u.key[:10]), key=lambda k: (k != u.key, k != u.scan, k))
        if day:
            kept[u.key] = posted[day[0]]
            rekey.append((u.key, day[0]))
    used = {k for _, k in rekey}
    left = [k for k in posted if k[:10] < cut and k not in used]
    return {"old": old, "new": new, "rekey": rekey, "left": left}, kept


def _replies(state: dict) -> dict[str, Any]:
    """Every reply a thread's state tracks: ``posted`` and an earlier ``since`` redo's ``extra``."""
    return {**state.get("extra", {}), **state.get("posted", {})}


def _stash(state: dict, plan: dict, kept: dict[str, Any]) -> None:
    """A redo's first write: the old replies' ids queued as ``stale``, ``posted`` = the kept ones, ``extra`` = the
    left ones (still in the thread)."""
    replies = _replies(state)
    state["stale"] = [_ts(e) for _, e in plan["old"]] + state.get("stale", [])
    state["posted"] = kept
    state.pop("extra", None)
    if plan["left"]:
        state["extra"] = {k: replies[k] for k in plan["left"]}


def _delete_stale(state: dict, delete) -> None:
    """A redo's last step, once every new reply posted: delete the ``stale`` ids; a failed delete is logged and kept
    for a re-run (an old reply lingering is harmless)."""
    failed = []
    for ts in state.pop("stale", []):
        try:
            delete(ts)
            _err(f"digest: deleted old reply {ts}")
        except Exception as e:  # noqa: BLE001 — leave it for a re-run; an old reply lingering is harmless
            _err(f"digest: WARN could not delete old reply {ts}: {e}")
            failed.append(ts)
    if failed:
        state["stale"] = failed


def converge_discord(tpl: Template, data: Any, month: dt.date, state: dict, *, hook, bot, emoji: dict[str, str], plot, save=None, edit_replies: bool = False, reply_hook=None) -> dict:
    """Bring one month's Discord thread to the desired state; returns ``state``.

    The Slack thread's shape on Discord's split transports: the OP is a
    *webhook* message (custom sender = month title + calendar avatar; the plot
    rides along as a file attachment, re-uploaded on every edit) that the
    *bot* then opens a thread off (webhooks can't); each not-yet-posted unit
    becomes a webhook reply into that thread under its sender + avatar.
    Discord groups consecutive messages by *displayed* sender and every
    headline differs, so replies need no spacing (Slack needs ~5 min).

    ``hook``/``bot`` are thrds's `DiscordWebhookClient`/`DiscordClient` (or
    fakes), ``emoji`` maps app-emoji names to ids, ``save(state)`` persists
    after each step so an interrupted run resumes without duplicates.
    ``edit_replies`` re-edits every already-posted reply to its current body
    (a backfill after a format change) through ``reply_hook``, a webhook client
    bound to the thread — a webhook edit inside a thread must carry the thread
    id, which the OP-level ``hook`` doesn't."""
    save = save or (lambda s: None)
    title = f"{tpl.cfg.title} — {month:%B %Y}"
    body = discordify(tpl.op_body(data, month, None), emoji)
    op_id = state.get("op_id")
    if op_id:
        hook.edit(op_id, body, files=[plot])
        _err(f"digest: edited OP {op_id} ({tpl.n_scans(data)} scans)")
    else:
        op_id = hook.post(body, username=title, icon_url=tpl.cfg.op_sender().get("icon_url") or f"{tpl.cfg.need('icons_base')}/calendar.png?v=2", files=[plot]).id
        state["op_id"] = op_id
        state["thread_id"] = bot.create_thread(op_id, title)
        save(state)
        _err(f"digest: posted OP {op_id}, thread {state['thread_id']}")
    thread_id = state["thread_id"]
    posted = state.setdefault("posted", {})
    for u in tpl.units(data, tpl.variants[0], "discord"):
        r = u.reply
        if u.key in posted:
            if edit_replies:
                reply_hook.edit(_ts(posted[u.key]), r.body)
                _err(f"digest: re-edited reply {u.key} ({_ts(posted[u.key])})")
            continue
        posted[u.key] = _record(tpl, hook.post(r.body, thread_id=thread_id, username=r.username, icon_url=r.icon_url).id, u.scan)
        save(state)
        _err(f"digest: reply {u.key} -> {_ts(posted[u.key])}")
    save(state)
    return state


def redo_discord(tpl: Template, data: Any, month: dt.date, state: dict, *, since: dt.date | None = None, for_real: bool = False, hook, reply_hook, bot, emoji: dict[str, str], plot, save=None) -> dict:
    """`redo_replies` on the Discord thread: the same plan (dry-run returns it),
    then the kept replies re-keyed, the new ones posted by `converge_discord`
    and, only once all posted, the old ones deleted through ``reply_hook`` (the
    thread-bound webhook client; a webhook deletes only its own messages)."""
    save = save or (lambda s: None)
    plan, kept = _redo_plan(tpl.units(data, tpl.variants[0], "discord"), _replies(state), since, lambda r: r.username)
    if not for_real:
        return plan
    if not state.get("thread_id"):
        raise SystemExit(f"digest: no Discord thread for {month:%Y-%m} — nothing to re-thread under")
    _stash(state, plan, kept)
    save(state)
    state = converge_discord(tpl, data, month, state, hook=hook, bot=bot, emoji=emoji, plot=plot, save=save)
    _delete_stale(state, reply_hook.delete)
    save(state)
    return state


def post_digest_discord(tpl: Template, root: str, month: dt.date, webhook: str, bot_token: str, plot_dir=None, edit_replies: bool = False, redo: bool = False, since: dt.date | None = None, for_real: bool = False) -> dict:
    """`converge_discord` (``redo``: `redo_discord`) against real Discord: resolve the webhook's channel,
    load that webhook's month state, render the plot, converge, persist. There
    is no plot-hosting step — the PNG is an attachment."""
    import tempfile

    from thrds.discord import NO_MENTIONS, DiscordClient, DiscordWebhookClient

    from . import discord_api

    data = tpl.load(root, month)
    if not data:
        _err(f"digest: no scans for {month:%Y-%m}")
        return {}
    info = discord_api.webhook_info(webhook)
    channel, key = info["channel_id"], info["id"]
    path = state_path(root, month, tpl.cfg.discord_state.format(webhook=key))
    state = load_state(path)
    plot = Path(plot_dir or tempfile.gettempdir()) / f"{tpl.cfg.slug}-{month:%Y-%m}.png"
    tpl.render_plot(data, month, plot, root)
    thread_id = state.get("thread_id")
    clients = dict(
        hook=DiscordWebhookClient(webhook, suppress_embeds=True, allowed_mentions=NO_MENTIONS),
        reply_hook=DiscordWebhookClient(webhook, thread_id, suppress_embeds=True, allowed_mentions=NO_MENTIONS) if thread_id else None,
        bot=DiscordClient(bot_token, channel),
        emoji=discord_api.app_emojis(bot_token),
        plot=plot,
        save=lambda s: save_state(path, s),
    )
    if redo:
        return redo_discord(tpl, data, month, state, since=since, for_real=for_real, **clients)
    return converge_discord(tpl, data, month, state, edit_replies=edit_replies, **clients)


def dry_run(tpl: Template, root: str, month: dt.date, variant: str | None = None, plot_dir=None) -> str:
    """Render the plot (into ``plot_dir``, default the temp dir; skipped with a note when the template can't draw it
    here, `PlotUnavailable`) and return the OP (the previous month's thread linked as `<prev-thread>`) + every reply (+ the open day's provisional reply, last) as text;
    posts and hosts nothing."""
    import tempfile

    variant = variant or tpl.variants[0]
    data = tpl.load(root, month)
    if not data:
        raise SystemExit(f"digest: no scans for {month:%Y-%m}")
    out = Path(plot_dir or tempfile.gettempdir()) / f"{tpl.cfg.slug}-{month:%Y%m}.png"
    try:
        tpl.render_plot(data, month, out, root)
        _err(f"rendered plot → {out}")
    except PlotUnavailable as e:
        _err(f"plot not rendered: {e}")
    lines = [tpl.op_body(data, month, "<plot-url>", "<prev-thread>"), "", f"--- replies ({variant}: username | body | icon) ---"]
    open_ = tpl.provisional(data, variant) if tpl.cfg.provisional else None
    for u in tpl.units(data, variant) + ([open_] if open_ else []):
        r = u.reply
        lines.append(f"{r.username} | {r.body} | {(r.icon_url or r.icon_emoji or '').split('/')[-1]}")
    if (alert := tpl.alert(data)) is not None:
        lines.append(f"{tpl.cfg.title}: quota alert | {alert} | {ALERT_ICON} (also sent to the channel)")
    return "\n".join(lines)
