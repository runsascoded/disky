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

- ``gcs`` (`digest_gcs`): a reply per scan, the headline as the sender name,
  $/mo by storage class in the body, a storage-class mosaic plot.
- ``cw`` (`digest_cw`): a reply per UTC day in two variants (``sender``: the
  headline as the sender name, posted once from the morning scan; ``body``:
  bold in the body, edited as the day's scans land), multi-bucket `% of quota
  (free)` clauses when quotas are configured, a quota sparkline + diff
  treemap plot.

A template is a small object (`load`, `op_body`, `units`, `render_plot`);
this module holds everything else: arrow math, formatters, scan ids and
`?d=` link tokens, Discord emoji, snapshot listing, state IO, plot hosting
(`wrangler pages deploy`), and the converge shells (`converge_slack`,
`converge_discord`, `redo_replies`). Design: specs/digest-unification.md;
history: specs/done/slack-digest-shape-c.md, specs/cw-slack-digest.md."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Protocol

TIB = 1024**4
GIB = 1024**3
# Weekly-halving arrow buckets: |dpct| >= THRESH[i] -> deg (i+1)*10 (capped 80).
THRESH = [0.39, 0.78, 1.5, 3.1, 6.25, 12.5, 25, 50]
MINUS = "−"  # matches the site's unicode minus
HOURS_PER_WEEK = 168.0
# bump when the av_deg glyphs change: Slack caches avatars per-URL at post
# time, so a stable URL serves MIXED generations after a redesign.
AVATAR_REV = 4
ICONS_BASE = "https://gcs-usage-icons.pages.dev"
SCAN_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2}))?$")


# ---- config -------------------------------------------------------------------


@dataclass(frozen=True)
class Quota:
    """A bucket's quota: ``bytes``, its long ``name`` (the OP + plot: `1 PB`)
    and an optional ``short`` label (the reply tail: `1P`; None → SI from
    ``bytes``, see `digest_cw._qlabel`)."""

    bytes: int
    name: str
    short: str | None = None


@dataclass(frozen=True)
class Bucket:
    """Per-bucket display config: the reply tail's short ``label`` (default:
    the bucket name) and its ``quota`` (None: the tail shows raw TiB)."""

    label: str | None = None
    quota: Quota | None = None


@dataclass(frozen=True)
class DigestConfig:
    """Everything a deployment passes the digest; no code forks.

    ``root``/``state``/``discord_state`` are templates: ``{DATA_BUCKET}`` reads
    the env (default `oa-gcs-usage-dvx`); ``state`` is the Slack state dir
    under the data root (``{channel}``, ``{variant}`` interpolated),
    ``discord_state`` the Discord one (``{webhook}`` = the webhook id — a
    webhook can only edit its own messages), ``discord_webhook_env`` the env
    var holding the Discord webhook URL (None: `-w` only). ``icons_dir`` is where the plot is
    rendered and `wrangler pages deploy`ed from (relative: the cwd's or the
    repo checkout's); it lands on ``plot_project``'s ``plot_branch``, served at
    ``plot_base`` unless wrangler names its deployment URL. ``primary`` +
    ``buckets`` (cw template) pick the headline bucket and label/quota each;
    ``prices`` (gcs template) are $/GiB-month by storage-class id."""

    template: str
    title: str
    site_url: str
    root: str
    state: str
    discord_state: str = "digest/discord/{webhook}"
    discord_webhook_env: str | None = None
    icons_base: str = ICONS_BASE
    icons_dir: str = "job/icons"
    plot_project: str = "gcs-usage-icons"
    plot_branch: str = "main"
    plot_base: str = ICONS_BASE
    variant: str = "sender"
    reply_hour: int = 12
    primary: str | None = None
    buckets: dict[str, Bucket] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)

    @property
    def primary_quota(self) -> Quota | None:
        b = self.buckets.get(self.primary) if self.primary else None
        return b.quota if b else None

    @property
    def host(self) -> str:
        """The site's host, the plot's corner credit."""
        return self.site_url.split("://", 1)[-1].rstrip("/")

    @property
    def slug(self) -> str:
        """`GCS usage` → `gcs-usage` (the Discord plot attachment's name)."""
        return re.sub(r"\s+", "-", self.title.strip().lower())

    def resolve_root(self) -> str:
        return self.root.format(DATA_BUCKET=os.environ.get("DATA_BUCKET", "oa-gcs-usage-dvx"))


PRESETS: dict[str, DigestConfig] = {
    "gcs": DigestConfig(
        template="gcs",
        title="GCS usage",
        site_url="https://gcs.oa.dev",
        root="gs://{DATA_BUCKET}/snapshots",
        state="digest",
        discord_webhook_env="DISCORD_GCS_USAGE_WEBHOOK",
        # US list $/GiB-mo by GCS storage class id (1 Standard / 2 Nearline / 3 Coldline / 4 Archive)
        prices={"1": 0.02, "2": 0.01, "3": 0.004, "4": 0.0012},
    ),
    "cw": DigestConfig(
        template="cw",
        title="CoreWeave usage",
        site_url="https://cw-s3.oa.dev",
        root="gs://{DATA_BUCKET}/snapshots/cw",
        # namespaced under cw/ (gcs's prod state is digest/<YYYY-MM>.json), keyed by
        # channel so a staging converge never masquerades as prod, and by variant so
        # both can be staged side by side
        state="digest/cw/{channel}/{variant}",
        icons_dir="job/icons-cw",
        # cw's plots go to the icons project's `cw` preview branch, so a cw deploy
        # never replaces what the production alias (the shared arrow avatars) serves
        plot_branch="cw",
        plot_base="https://cw.gcs-usage-icons.pages.dev",
        primary=os.environ.get("CW_BUCKET", "marin-us-east-02a"),
        # quotas authoritative from CoreWeave's own `cwobject_quota_info` metric
        # (per zone; via finelog / Grafana `storage.usage`): 02a = exactly 910 TiB
        # (≈ 1.0006 PB decimal, "1 PB"); hero-checkpoints = the US-EAST-08A ZONE
        # quota, 100 TiB, shared with rhoarnet-us-east-08a (~3 TiB, unscanned) — so
        # hero's "free" overstates true zone headroom by ~3 TiB
        buckets={
            "marin-us-east-02a": Bucket("02a", Quota(910 * TIB, "1 PB", "1P")),
            "hero-checkpoints": Bucket("hero", Quota(100 * TIB, "100 TiB", "100Ti")),
        },
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


def config_from_dict(d: dict, base: DigestConfig | None = None) -> DigestConfig:
    """A config from a parsed YAML/JSON mapping, overlaid on ``base`` (default:
    the preset its ``template`` names). ``buckets`` map names to ``{label,
    quota: {bytes, name, short}}`` (``bytes`` may be `910 TiB`); unknown keys
    are an error."""
    d = dict(d)
    base = base or PRESETS[d.get("template", "gcs")]
    known = {f.name for f in fields(DigestConfig)}
    if bad := set(d) - known:
        raise ValueError(f"unknown digest config keys: {sorted(bad)}")
    if "buckets" in d:
        d["buckets"] = {
            name: Bucket(b.get("label"), Quota(parse_bytes(b["quota"]["bytes"]), b["quota"]["name"], b["quota"].get("short")) if b.get("quota") else None)
            for name, b in (d["buckets"] or {}).items()
        }
    return replace(base, **d)


def load_config(template: str, path: str | Path | None = None) -> DigestConfig:
    """The ``template`` preset, overlaid by the YAML/JSON file at ``path``."""
    if path is None:
        return PRESETS[template]
    import yaml

    d = yaml.safe_load(Path(path).read_text()) or {}
    return config_from_dict(d, PRESETS[d.get("template", template)])


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
    d = dt.date.fromisoformat(date)
    return f"{d.month}/{d.day}"


def scan_ts(scan: str) -> dt.datetime:
    """A scan id's UTC instant; date-only ids read as midnight (site/src/scan.ts)."""
    m = SCAN_RE.match(scan)
    if not m:
        raise ValueError(f"not a scan id: {scan!r}")
    y, mo, d, hh, mm = m.groups()
    return dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), tzinfo=dt.timezone.utc)


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


class Template(Protocol):
    """A digest style over one deployment's :class:`DigestConfig`.

    ``variants`` are its reply styles (the first is the default);
    ``edited_variants`` re-edit a day's reply when a later scan of it lands;
    ``track_scan`` stores each posted reply as ``{ts, scan}`` (else the bare
    ts — the gcs template's existing state format)."""

    cfg: DigestConfig
    variants: tuple[str, ...]
    edited_variants: tuple[str, ...]
    track_scan: bool

    def load(self, root: str, month: dt.date) -> Any | None: ...
    def n_scans(self, data: Any) -> int: ...
    def op_body(self, data: Any, month: dt.date, plot_url: str | None) -> str: ...
    def units(self, data: Any, variant: str, platform: str = "slack") -> list[Unit]: ...
    def render_plot(self, data: Any, month: dt.date, out: Path, root: str | None = None) -> None: ...


def template(cfg: DigestConfig) -> Template:
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
    sorted — ids sort chronologically."""
    import fsspec

    fs, _, _ = fsspec.get_fs_token_paths(root)
    return sorted(
        m.group(1)
        for p in fs.glob(f"{root.split('://', 1)[-1]}/*/meta.json")
        if (m := re.search(r"/(\d{4}-\d{2}-\d{2}(?:T\d{4})?)/meta\.json$", p))
    )


def load_window(root: str, month: dt.date) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]] | None:
    """``(lead, in_month)`` ``(scan id, meta.json)`` pairs for ``month``: the
    month's scans, and ``lead`` = every scan of the last calendar day before it
    (the first delta's baseline; empty for the first month ever). None if the
    month has no scans."""
    import fsspec

    scans = list_scans(root)
    pfx = f"{month:%Y-%m}-"
    in_month = [s for s in scans if s.startswith(pfx)]
    if not in_month:
        return None
    before = [s for s in scans if s < in_month[0]]
    lead = [s for s in before if s[:10] == before[-1][:10]] if before else []

    def read(s: str) -> tuple[str, dict]:
        with fsspec.open(f"{root}/{s}/meta.json", "rt") as f:
            return s, json.load(f)

    return [read(s) for s in lead], [read(s) for s in in_month]


def state_path(root: str, month: dt.date, state_dir: str) -> str:
    """The converge-state JSON for one month's thread: ``<data
    root>/<state_dir>/<YYYY-MM>.json``, beside ``root``'s `snapshots/`."""
    base = root.rsplit("/snapshots", 1)[0]
    return f"{base}/{state_dir}/{month:%Y-%m}.json"


def load_state(path: str) -> dict:
    import fsspec

    try:
        with fsspec.open(path, "rt") as f:
            return json.load(f)
    except (FileNotFoundError, OSError):
        return {}


def save_state(path: str, state: dict) -> None:
    import fsspec

    with fsspec.open(path, "wt", auto_mkdir=True) as f:
        json.dump(state, f, indent=2)


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


def converge_slack(tpl: Template, root: str, month: dt.date, client, channel: str, variant: str | None = None, *, icons_dir=None, deploy_plot=None, reply_delay: float = 0.0) -> dict:
    """Converge the month's Slack thread: render+host the plot, post/edit the
    OP, then per reply unit post it if none exists — or, on an edited variant,
    edit it when a later scan has landed. Persist and return state.

    ``icons_dir`` is where to write the PNG; ``deploy_plot(local, basename)``
    publishes it and returns the host serving it (None → ``cfg.plot_base``).
    ``reply_delay`` sleeps between new replies (>0 for a spaced backfill, so
    Slack doesn't collapse same-sender chrome). ``client`` is a thrds
    ``SlackClient`` (or a fake)."""
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
    base = cfg.plot_base
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

    body = tpl.op_body(data, month, plot_url)
    op_ts = state.get("op_ts")
    if op_ts:
        client.edit(op_ts, body)
        _err(f"digest: edited OP {op_ts} ({tpl.n_scans(data)} scans)")
    else:
        op_ts = client.post(body, username=f"{cfg.title} — {month:%B %Y}", icon_emoji=":calendar:").id
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
        elif variant in tpl.edited_variants and have["scan"] != u.scan:
            client.edit(have["ts"], r.body)
            have["scan"] = u.scan
            save_state(path, state)
            _err(f"digest: edited reply {u.key} -> {u.scan}")

    save_state(path, state)
    return state


def redo_replies(tpl: Template, root: str, month: dt.date, client, channel: str, variant: str | None = None, *, icons_dir=None, deploy_plot=None, reply_delay: float = 0.0, for_real: bool = False) -> dict:
    """Re-post the month's replies under the CURRENT unit rule and retire the
    old ones (a rule change, e.g. evening→morning scan). Post-new-then-delete-
    old on purpose: no empty-thread window, and the old block vanishes at
    once. No strike/edit step — the headline lives in the sender name, which
    `chat.update` can't touch, so a strike would look broken.

    Dry-run (default) returns the plan — ``old`` replies (key, record) and
    ``new`` (key, scan, headline) — and posts nothing. ``for_real``: the old
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
    old = list(state.get("posted", {}).items())
    new = [(u.key, u.scan, u.reply.username if variant == "sender" else u.reply.body) for u in tpl.units(data, variant)]
    if not for_real:
        return {"old": old, "new": new}
    if not state.get("op_ts"):
        raise SystemExit(f"digest: no OP for {month:%Y-%m} in {channel} — nothing to re-thread under")
    state["stale"] = [_ts(e) for _, e in old] + state.get("stale", [])
    state["posted"] = {}
    save_state(path, state)
    state = converge_slack(tpl, root, month, client, channel, variant, icons_dir=icons_dir, deploy_plot=deploy_plot, reply_delay=reply_delay)
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
    save_state(path, state)
    return state


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
        op_id = hook.post(body, username=title, icon_url=f"{tpl.cfg.icons_base}/calendar.png?v=2", files=[plot]).id
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


def post_digest_discord(tpl: Template, root: str, month: dt.date, webhook: str, bot_token: str, plot_dir=None, edit_replies: bool = False) -> dict:
    """`converge_discord` against real Discord: resolve the webhook's channel,
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
    return converge_discord(
        tpl, data, month, state,
        hook=DiscordWebhookClient(webhook, suppress_embeds=True, allowed_mentions=NO_MENTIONS),
        reply_hook=DiscordWebhookClient(webhook, thread_id, suppress_embeds=True, allowed_mentions=NO_MENTIONS) if thread_id else None,
        edit_replies=edit_replies,
        bot=DiscordClient(bot_token, channel),
        emoji=discord_api.app_emojis(bot_token),
        plot=plot,
        save=lambda s: save_state(path, s),
    )


def dry_run(tpl: Template, root: str, month: dt.date, variant: str | None = None, plot_dir=None) -> str:
    """Render the plot (into ``plot_dir``, default the temp dir) and return the
    OP + every reply as text; posts and hosts nothing."""
    import tempfile

    variant = variant or tpl.variants[0]
    data = tpl.load(root, month)
    if not data:
        raise SystemExit(f"digest: no scans for {month:%Y-%m}")
    out = Path(plot_dir or tempfile.gettempdir()) / f"{tpl.cfg.slug}-{month:%Y%m}.png"
    tpl.render_plot(data, month, out, root)
    _err(f"rendered plot → {out}")
    lines = [tpl.op_body(data, month, "<plot-url>"), "", f"--- replies ({variant}: username | body | icon) ---"]
    for u in tpl.units(data, variant):
        r = u.reply
        lines.append(f"{r.username} | {r.body} | {(r.icon_url or r.icon_emoji or '').split('/')[-1]}")
    return "\n".join(lines)
