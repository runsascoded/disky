"""Weekly storage report for Marin's `#internal-discuss` (specs/weekly-discord-report.md).

One Discord webhook message per week, built from two daily scans a week apart:
totals with deltas and $/mo, what the sweep removed, and the week's biggest
movers with their owners. ``movers`` and ``compose`` are pure functions over
plain rows so the tests pin their exact output; only ``load_table``,
``load_totals``, ``load_swept`` and ``post`` touch the network.

Rows come from the ``path`` sort of each scan's index — its dir rows to
``MAX_DEPTH`` (one row per rolled-up path × owner slice, ``usr`` NULL = the
*unowned* slice, so a path's total is the sum of its rows; exact, no floor:
``movers``' byte threshold is the only cut). ``path`` is bucket-qualified with
no ``gs://`` and no trailing slash (``marin-us-east5/checkpoints``); depth 1 is
the bucket itself, so the report's "depth ≤ 4 under the bucket" is index depth
≤ 5. A pre-store generation (dir rows only, wire names ``b`` / ``o``) reads the
same way.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .digest import GIB, MINUS, TIB, _cost

DEFAULT_URL = "https://gcs.oa.dev"
ICON_URL = "https://gcs-usage-icons.pages.dev/gcs-digest.png"  # the digest app icon (mark.png was never deployed → Discord fell back to its default avatar)
SENDER = "GCS usage"
MESSAGE_LIMIT = 2000
MAX_DEPTH = 5  # index depth: bucket + 4 segments
DESCENT = 0.8  # a child explains its parent when it carries ≥ this share of the parent's delta
TABLE_VARIANT = "path"
TABLE_FILE = "path-index.parquet"

# ---- rows --------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One index row: ``usr`` None = the unowned slice, else that owner's slice."""

    path: str
    depth: int
    b: int
    o: int
    usr: str | None = None


class Table:
    """A scan's rows indexed for the mover walk: totals by path (the sum of
    every slice, unowned included), and the named owner slices by path."""

    def __init__(self, rows: Iterable[Row]):
        self.total: dict[str, int] = {}
        self.depth: dict[str, int] = {}
        self.slices: dict[str, dict[str, int]] = {}
        for r in rows:
            self.total[r.path] = self.total.get(r.path, 0) + r.b
            self.depth[r.path] = r.depth
            if r.usr is not None:
                self.slices.setdefault(r.path, {})[r.usr] = self.slices.get(r.path, {}).get(r.usr, 0) + r.b

    def bytes(self, path: str) -> int:
        return self.total.get(path, 0)

    def children(self, path: str) -> list[str]:
        pfx = path + "/"
        d = path.count("/") + 2
        return sorted(p for p, dp in self.depth.items() if dp == d and p.startswith(pfx))

    def owner(self, path: str) -> str | None:
        """The owner whose slice is a strict majority of the path's bytes, else None."""
        tot = self.bytes(path)
        if not tot:
            return None
        usr, b = max(self.slices.get(path, {}).items(), key=lambda kv: (kv[1], kv[0]), default=(None, 0))
        return usr if b * 2 > tot else None


# ---- movers ------------------------------------------------------------------


@dataclass(frozen=True)
class Mover:
    path: str
    delta: int  # bytes, signed
    status: str  # new | gone | grew | shrank | swept
    owner: str | None  # index user id; None = unowned


@dataclass(frozen=True)
class Movers:
    up: list[Mover]
    down: list[Mover]
    more_up: int  # movers past ``top`` in each direction
    more_down: int


def _explain(a: Table, b: Table, path: str, delta: int, threshold: int) -> str:
    """Descend from ``path`` while one child carries ≥ DESCENT of its delta
    (same sign, and itself ≥ ``threshold``) — the deepest such child names the
    change; a delta spread across children stays at the parent."""
    while path.count("/") + 1 < MAX_DEPTH:
        best, best_d = None, 0
        for c in sorted(set(a.children(path)) | set(b.children(path))):
            cd = b.bytes(c) - a.bytes(c)
            if (cd > 0) == (delta > 0) and abs(cd) > abs(best_d):
                best, best_d = c, cd
        if best is None or abs(best_d) < max(threshold, DESCENT * abs(delta)):
            break
        path, delta = best, best_d
    return path


def movers(
    a: Table,
    b: Table,
    *,
    threshold: int = 100 * GIB,
    top: int = 5,
    swept: Iterable[str] = (),
) -> Movers:
    """Prefixes whose bytes moved by ≥ ``threshold`` between scans ``a`` (from)
    and ``b`` (to), each reported at its maximal explanatory prefix.

    ``swept`` are band prefixes (``bucket/dir``) the week's real sweep runs
    deleted under; a shrinking/vanished mover at or under one is ``swept``.
    Deterministic: sorted by |delta| desc, then path."""
    bands = tuple(swept)
    found: dict[str, int] = {}
    starts = sorted(p for p, d in {**a.depth, **b.depth}.items() if d == 2)
    for p in starts:
        d = b.bytes(p) - a.bytes(p)
        if abs(d) < threshold:
            continue
        deep = _explain(a, b, p, d, threshold)
        found[deep] = b.bytes(deep) - a.bytes(deep)

    def mk(path: str, d: int) -> Mover:
        in_a, in_b = a.bytes(path) > 0, b.bytes(path) > 0
        if not in_a:
            status = "new"
        elif not in_b:
            status = "gone"
        elif d > 0:
            status = "grew"
        else:
            status = "shrank"
        if status in ("gone", "shrank") and any(path == s or path.startswith(s + "/") or s.startswith(path + "/") for s in bands):
            status = "swept"
        owner = (a if status in ("gone", "swept") and not in_b else b).owner(path)
        return Mover(path, d, status, owner)

    ups = sorted((mk(p, d) for p, d in found.items() if d > 0), key=lambda m: (-m.delta, m.path))
    downs = sorted((mk(p, d) for p, d in found.items() if d < 0), key=lambda m: (m.delta, m.path))
    return Movers(ups[:top], downs[:top], max(0, len(ups) - top), max(0, len(downs) - top))


# ---- totals + sweeps ----------------------------------------------------------


@dataclass(frozen=True)
class Totals:
    objects: int
    bytes: int
    cost: int  # $/mo
    d_objects: int
    d_bytes: int
    d_cost: int


@dataclass(frozen=True)
class Swept:
    objects: int
    bytes: int
    runs: int
    undo_deadline: int | None  # epoch seconds of the latest run's undo window


def totals_from_meta(prior: dict, meta: dict) -> Totals:
    cost, pcost = round(_cost(meta["class_bytes"])), round(_cost(prior["class_bytes"]))
    return Totals(
        objects=meta["total_objects"],
        bytes=meta["total_bytes"],
        cost=cost,
        d_objects=meta["total_objects"] - prior["total_objects"],
        d_bytes=meta["total_bytes"] - prior["total_bytes"],
        d_cost=cost - pcost,
    )


def swept_from_runs(runs: list[dict], since: int, until: int) -> Swept:
    """Sum the real runs that finished in ``(since, until]`` (epoch seconds)
    and deleted at least one object (an aborted run leaves a zero row)."""
    real = [
        r for r in runs
        if r.get("mode") == "real" and r.get("finished_ts") and since < r["finished_ts"] <= until and r.get("deleted_objects", 0) > 0
    ]
    undo = [r["undo_deadline"] for r in real if r.get("undo_deadline")]
    return Swept(
        objects=sum(r.get("deleted_objects", 0) for r in real),
        bytes=sum(r.get("deleted_bytes", 0) for r in real),
        runs=len(real),
        undo_deadline=max(undo) if undo else None,
    )


# ---- compose -----------------------------------------------------------------


def short_name(usr: str | None) -> str:
    """``calvin-xu`` → ``Calvin``; None → ``unowned``."""
    return usr.split("-")[0].capitalize() if usr else "unowned"


def _tb(b: int) -> str:
    return f"{b / 1e12:,.1f} TB"


def _dtb(b: int) -> str:
    return (f"+{b / 1e12:,.1f}" if b >= 0 else f"{MINUS}{abs(b) / 1e12:,.1f}") + " TB"


def _tib(b: int) -> str:
    return (f"+{b / TIB:,.1f}" if b >= 0 else f"{MINUS}{abs(b) / TIB:,.1f}") + " TiB"


def _pct(d: int, now: int) -> str:
    prev = now - d
    v = d / prev * 100 if prev else 0.0
    return (f"+{v:.1f}" if v >= 0 else f"{MINUS}{abs(v):.1f}") + "%"


def _usd(v: int) -> str:
    return ("+$" if v >= 0 else f"{MINUS}$") + f"{abs(v):,}"


def _md(date: str) -> str:
    return date[5:]


def _line(m: Mover, name: Callable[[str | None], str]) -> str:
    return f"- `{m.path}` {_tib(m.delta)} ({m.status} · {name(m.owner)})"


def compose(
    totals: Totals,
    swept: Swept | None,
    mv: Movers,
    *,
    date: str,
    prior: str,
    threshold: int = 100 * GIB,
    url: str = DEFAULT_URL,
    name: Callable[[str | None], str] = short_name,
) -> str:
    """The message, ≤ MESSAGE_LIMIT chars: mover lines drop from the tail of
    each list (into the `+N more` count) until it fits; the headline never
    truncates."""
    link = f"{url}/?d={date[2:].replace('-', '')}-7d#diff"
    head = [
        f"**Weekly storage report** (UTC {date}) · [gcs.oa.dev ↗]({link})",
        f"- totals: {totals.objects:,} objects · {_tb(totals.bytes)} ({_dtb(totals.d_bytes)}, {_pct(totals.d_bytes, totals.bytes)} vs {_md(prior)}) · ${totals.cost:,}/mo ({_usd(totals.d_cost)})",
    ]
    if swept and swept.runs:
        undo = f" · undo until {dt.datetime.fromtimestamp(swept.undo_deadline, dt.timezone.utc):%m-%d}" if swept.undo_deadline else ""
        head.append(f"- swept this week: {swept.objects:,} objects · {_tb(swept.bytes)} in {swept.runs} run{'s' if swept.runs != 1 else ''}{undo}")
    head.append("")
    head.append(f"_Changes since {prior} (prefixes that moved ≥ {threshold // GIB} GiB):_")

    def body(up: list[Mover], down: list[Mover], more_up: int, more_down: int) -> str:
        out = list(head)
        for title, rows, more in (("Biggest increases", up, more_up), ("Biggest decreases", down, more_down)):
            out.append("")
            out.append(f"**{title}:**")
            if not rows and not more:
                out.append("- _(none)_")
            out.extend(_line(m, name) for m in rows)
            if more:
                out.append(f"- _(+{more} more in the report)_")
        return "\n".join(out)

    up, down, mu, md = list(mv.up), list(mv.down), mv.more_up, mv.more_down
    text = body(up, down, mu, md)
    while len(text) > MESSAGE_LIMIT and (up or down):
        # drop from whichever list is longer (ties: decreases)
        if len(down) >= len(up):
            down.pop()
            md += 1
        else:
            up.pop()
            mu += 1
        text = body(up, down, mu, md)
    return text


# ---- readers (network) ---------------------------------------------------------


def _err(*a) -> None:
    import sys

    print(*a, file=sys.stderr)


def scan_dates(root: str) -> list[str]:
    """Published scan dates under ``root`` (``gs://<bucket>/snapshots``), ascending."""
    import re

    import fsspec

    fs, _, _ = fsspec.get_fs_token_paths(root)
    return sorted(
        m.group(1)
        for p in fs.glob(f"{root.split('://', 1)[-1]}/*/meta.json")
        if (m := re.search(r"/(\d{4}-\d{2}-\d{2}(?:T\d{4})?)/meta\.json$", p))  # date-only or sub-daily ids
    )


def prior_scan(dates: list[str], date: str, days: int = 7) -> str | None:
    """The newest scan at least ``days`` before ``date`` (a missed daily scan
    shifts the baseline back a day rather than failing)."""
    cutoff = (dt.date.fromisoformat(date) - dt.timedelta(days=days)).isoformat()
    older = [d for d in dates if d <= cutoff]
    return older[-1] if older else None


def load_meta(root: str, date: str) -> dict:
    import fsspec

    with fsspec.open(f"{root}/{date}/meta.json", "rt") as f:
        return json.load(f)


def load_table(bucket: str, date: str) -> Table:
    """The scan's ``path`` sort's dir rows to MAX_DEPTH (a prefix of the file:
    sorted `(depth, path)`, so the depth filter prunes row groups), located
    via the D1 index pointer like every other index reader."""
    import fsspec
    import pyarrow.parquet as pq

    from .index_footer import index_dir

    d = index_dir(date, TABLE_VARIANT)
    if d is None:
        raise RuntimeError(f"weekly: {date} has no synced {TABLE_VARIANT} index tier")
    fs, path = fsspec.core.url_to_fs(f"gs://{bucket}/{d}/{TABLE_FILE}")
    return read_table(path, fs)


def read_table(path: str, filesystem=None) -> Table:
    """`Table` of an index parquet's dir rows to MAX_DEPTH: a store generation's
    (`kind = 'dir'`, `size`, `n_files`) or a pre-store one's (`b`, `o`)."""
    import pyarrow.parquet as pq

    store = "kind" in pq.read_schema(path, filesystem=filesystem).names
    b, o = ("size", "n_files") if store else ("b", "o")
    filters = [("depth", "<=", MAX_DEPTH)] + ([("kind", "==", "dir")] if store else [])
    t = pq.read_table(path, filesystem=filesystem, columns=["path", "depth", "usr", b, o], filters=filters)
    return Table(Row(p, int(dp), int(bb), int(oo), u) for p, dp, u, bb, oo in zip(*(t.column(c).to_pylist() for c in ("path", "depth", "usr", b, o))))


def load_runs(url: str, token: str) -> list[dict]:
    from .site import get_json

    return get_json(url, token, "/api/db/deletion_runs", {"limit": 500}).get("rows", [])


def load_bands(url: str, token: str, run_ids: Iterable[str]) -> list[str]:
    """Band prefixes (``bucket/dir``) of the given runs, from ``deletion_bands``."""
    from .site import get_json

    ids = set(run_ids)
    rows = get_json(url, token, "/api/db/deletion_bands", {"limit": 5000}).get("rows", [])
    return sorted({r["prefix"].removeprefix("gs://").rstrip("/") for r in rows if r.get("run_id") in ids})


def post(webhook: str, content: str, *, files: list = ()) -> str:
    """Post as the GCS-usage sender through thrds's webhook client; returns the message id."""
    from thrds.discord import NO_MENTIONS, DiscordWebhookClient

    # NO_MENTIONS: interpolated paths/owner names can never ping @here/@everyone/@role
    client = DiscordWebhookClient(webhook, username=SENDER, avatar_url=ICON_URL, suppress_embeds=True, allowed_mentions=NO_MENTIONS)
    kwargs = {"files": list(files)} if files else {}
    return client.post(content, **kwargs).id


def scan_ts(date: str) -> int:
    return int(dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc).timestamp())
