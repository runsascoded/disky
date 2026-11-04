"""Offline: the range reads one `readLevel` makes (`site/functions/_lib/index.ts`, specs/diff-progressive.md §3b) under
candidate coalescing, over a generation's real `level` sort: its sidecars' bounds and metadata, and the page indexes of
the groups a request keeps. Mirrors the reader: each file holding a scan in (D1, D2] (the base, the newest manifest's
runs), its groups meeting any directory's key and the range, their page-index spans (every column's OffsetIndex, the
planning columns' ColumnIndex), then the pages meeting them and their spans (each column's pages, its dictionary), each
wave's spans merged per file and every run on `RUN_READS` slots. Rows are the kept pages' (what the decoder is handed).

A config is a fixed `(gap, cap)` as `planRuns`' arguments, or `fit:B`: per wave, gaps under `FIT_FLOOR` closed, then the
rest smallest first to the predicted fastest wave whose fetched bytes stay within B. `whole` prices a read with no index
wave: each kept group's chunks whole (`chunkSpan`), one wave, at today's `planRuns`, every row of the groups decoded. A
wave's time: its runs, in file order, list-scheduled on
`RUN_READS` slots, each `ttfb + bytes / throughput`. A request's: its two waves in series (the plan's footer reads and
any CPU are not modelled, and don't depend on the config).

`-s`/`--small` prices held page indexes and the sort cut in small row groups with no page index, nothing re-cut: its
table's rows are the modes of `modes`, read back from the same file (see `SmallGroup` for the approximation). A level's
batches run together, a batch's chain rounds in series (a request's `case`, `level`, `batch`, `round`).
"""
from __future__ import annotations

import heapq
import json
import os
import re
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial
from math import ceil
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from click import IntRange, argument, command, option

from .image_sha import expect_sha_options
from .interval_store import GROUPS_SCHEMA, GROUPS_STAT_COLS
from .layout_sim import RUN_BYTES, RUN_GAP, RUN_READS, pct
from .parquet_pages import _Reader

err = partial(print, file=sys.stderr, flush=True)

#: The sort's columns, as `IV_LEVEL_COLS`, and those a page is kept by (`LEVEL_PLAN`).
LEVEL_COLS = ["depth", "path", "ts", "name", "op", "kind", "kind_prev", "size", "n_files", "size_prev", "n_files_prev", "churn_cum", "churn_prev"]
PLAN_COLS = ["depth", "path", "ts"]
#: A decoded row's bytes as V8 holds it (the site's `ROW_BYTES`).
ROW_BYTES = 400
#: The R2 probe through the binding (cold dev, 2026-11-03): time to first byte, and throughput from its 2 MiB get.
TTFB_MS = 195.0
THROUGHPUT_MIB_S = 28.6
SIDECAR_COLS = ["rg", "d_min", "d_max", "p_min", "p_max", "vf_min", "vf_max", "vt_max", "rg_json"]
#: One colo-cache lookup, an assumption (not measured on the level read): they run `RUN_READS` at a time.
COLO_MS = 5.0
MIB = 1 << 20
#: `fit` always closes a gap under this: a get's first byte costs far more than its transfer.
FIT_FLOOR = 64 << 10

Span = tuple[int, int]


@dataclass(frozen=True)
class Config:
    """How a wave's spans merge: fixed `planRuns` arguments, or `budget`: the fastest merge within so many bytes."""
    label: str
    gap: int = RUN_GAP
    cap: int = RUN_BYTES
    budget: int | None = None


def parse_size(s: str) -> int:
    """`512K`, `2M`, `1048576` → bytes."""
    m = re.fullmatch(r"(\d+)([KMG]?)", s.strip().upper())
    if not m:
        raise ValueError(f"{s}: not a size")
    return int(m[1]) << {"": 0, "K": 10, "M": 20, "G": 30}[m[2]]


def size_label(n: int) -> str:
    return f"{n >> 20}M" if n >= MIB and not n % MIB else f"{n >> 10}K"


def configs(gaps: str, caps: str, budgets: str) -> list[Config]:
    """The cross of `gaps` and `caps` (`none` / an empty cap: no ceiling), then a `fit:` config per budget."""
    out = []
    for c in filter(None, caps.split(",")):
        cap = 1 << 62 if c.strip() == "none" else parse_size(c)
        for g in filter(None, gaps.split(",")):
            out.append(Config(f"g{size_label(parse_size(g))}/c{'∞' if c.strip() == 'none' else size_label(cap)}", parse_size(g), cap))
    return out + [Config(f"fit:{b.strip().upper()}", budget=parse_size(b)) for b in filter(None, budgets.split(","))]


def merged(spans: list[Span], gap: int, cap: int) -> list[Span]:
    """`planRuns`: sorted by start, a run grows while the next span starts within `gap` of its end and it stays within
    `cap` (a span bigger than `cap` is a run of its own)."""
    runs: list[list[int]] = []
    for s, e in sorted(spans):
        if runs and s - runs[-1][1] <= gap and max(runs[-1][1], e) - runs[-1][0] <= cap:
            runs[-1][1] = max(runs[-1][1], e)
        else:
            runs.append([s, e])
    return [(s, e) for s, e in runs]


def wave_ms(sizes: list[int], ttfb: float, mib_s: float, slots: int = RUN_READS) -> float:
    """The wall time of gets of `sizes` bytes, in order, each `ttfb + bytes / throughput` on the first free of `slots`."""
    free = [0.0] * slots
    end = 0.0
    for n in sizes:
        at = heapq.heappop(free) + ttfb + n / MIB / mib_s * 1000
        heapq.heappush(free, at)
        end = max(end, at)
    return end


def fit(per_file: list[list[Span]], budget: int, ttfb: float, mib_s: float) -> list[list[Span]]:
    """Each file's runs merged, smallest gaps first over all files, to the predicted fastest wave within `budget` fetched
    bytes (gaps under `FIT_FLOOR` always close; a wave those alone put over it goes no further)."""
    runs = [merged(sp, FIT_FLOOR, 1 << 62) for sp in per_file]
    gaps = sorted((b[0] - a[1], f, i) for f, r in enumerate(runs) for i, (a, b) in enumerate(zip(r, r[1:])))
    closed = [set() for _ in runs]
    used = sum(e - s for r in runs for s, e in r)

    def now() -> list[list[Span]]:
        out = []
        for f, r in enumerate(runs):
            cur: list[Span] = []
            for i, (s, e) in enumerate(r):
                cur[-1:] = [(cur[-1][0], e)] if i and i - 1 in closed[f] else [*cur[-1:], (s, e)]
            out.append(cur)
        return out

    def cost(rs: list[list[Span]]) -> float:
        return wave_ms([e - s for r in rs for s, e in r], ttfb, mib_s)

    best = now()
    best_ms = cost(best)
    n = sum(len(r) for r in runs)
    for g, f, i in gaps:
        if used + g > budget:
            break
        used += g
        closed[f].add(i)
        n -= 1
        # A wave's time moves in rounds: a merge only pays once it empties a slot's last round.
        if n % RUN_READS == 0 or n <= RUN_READS:
            cand = now()
            ms = cost(cand)
            if ms < best_ms:
                best, best_ms = cand, ms
    return best


@dataclass
class PageIx:
    """A group's page index: per column its pages `(offset, compressed size, first row)`, and the planning columns' page
    bounds."""
    locs: dict[str, list[tuple[int, int, int]]]
    depth: list[tuple[int, int]]
    path: list[tuple[str, str]]
    ts: list[tuple[int, int]]


class Tier:
    """One file's `level` sort: its sidecar's bounds, and the stored metadata of its groups, parsed on demand."""

    def __init__(self, key: str, sidecar_path: Path, label: str):
        self.key, self.label = key, label
        t = pq.read_table(sidecar_path, columns=SIDECAR_COLS)
        self.rg = t["rg"].to_numpy()
        self.d_min, self.d_max = t["d_min"].to_numpy(), t["d_max"].to_numpy()
        self.vf_min, self.vf_max, self.vt_max = t["vf_min"].to_numpy(), t["vf_max"].to_numpy(), t["vt_max"].to_numpy()
        self.p_min, self.p_max = t["p_min"].to_pylist(), t["p_max"].to_pylist()
        self.rg_json = t["rg_json"]
        self.sidecar_path = sidecar_path
        md = pq.read_metadata(sidecar_path)
        self.sidecar_footer = md.serialized_size
        # The sidecar's own row groups (the reader's footer groups): their rows, bytes and bounds (`FooterGroup.bounds`).
        self.fg_rows = [md.row_group(g).num_rows for g in range(md.num_row_groups)]
        self.fg_bytes = [sum(md.row_group(g).column(c).total_compressed_size for c in range(md.num_columns)) for g in range(md.num_row_groups)]
        self.fg_start = np.concatenate([[0], np.cumsum(self.fg_rows)]).astype(np.int64)
        self._rows: np.ndarray | None = None

    def footer_groups(self, keys: list[tuple[int, str]], d1: int, d2: int) -> list[int]:
        """The footer groups a read of `keys` over `(d1, d2]` decodes (`pqGroups`' `pass`; a run's closes keep any `vt`)."""
        out = []
        for n in range(len(self.fg_rows)):
            a, b = int(self.fg_start[n]), int(self.fg_start[n + 1])
            if self.vf_min[a:b].min() > d2 or (self.label == "base" and self.vt_max[a:b].max() <= d1):
                continue
            d0, d9, p0, p9 = self.d_min[a:b].min(), self.d_max[a:b].max(), min(self.p_min[a:b]), max(self.p_max[a:b])
            if any(d0 <= d <= d9 and p0 <= p <= p9 for d, p in keys):
                out.append(n)
        return out

    @property
    def rows(self) -> np.ndarray:
        """Each group's rows."""
        if self._rows is None:
            self._rows = np.array([json.loads(j)[0] for j in self.rg_json.to_pylist()], dtype=np.int64)
        return self._rows

    def small_base(self, rows: int) -> np.ndarray:
        """Per group, the ordinal of its first small group were the file cut in `rows`-row groups (and the total, last)."""
        return np.concatenate([[0], np.cumsum(np.ceil(self.rows / rows))]).astype(np.int64)

    def in_range(self, d1: int, d2: int) -> np.ndarray:
        """Positions of the groups that can hold a transition in `(d1, d2]`."""
        return np.flatnonzero((self.vf_max > d1) & (self.vf_min <= d2) & (self.vt_max > d1))

    def kept(self, keys: list[tuple[int, str]], ix: np.ndarray) -> list[int]:
        """Of positions `ix`, the groups meeting any `(depth, path)` of `keys` (`meets`), as positions."""
        out = []
        for i in ix.tolist():
            lo, hi = self.p_min[i], self.p_max[i]
            if any(self.d_min[i] <= d <= self.d_max[i] and lo <= p <= hi for d, p in keys):
                out.append(i)
        return out

    def chunks(self, i: int) -> tuple[int, dict[str, list[int]]]:
        """Position `i`'s rows, and per column its compact metadata
        (`[data page, compressed size, dictionary page, ColumnIndex offset, length, OffsetIndex offset, length]`)."""
        n, _, cols = json.loads(self.rg_json[i].as_py())
        if len(cols) != len(LEVEL_COLS):
            raise ValueError(f"{self.key}: group {i} has {len(cols)} columns")
        return n, dict(zip(LEVEL_COLS, cols))


def index_spans(chunks: dict[str, list[int]]) -> list[Span]:
    """A group's page-index wave: the planning columns' ColumnIndexes, every column's OffsetIndex."""
    return [(chunks[c][3], chunks[c][3] + chunks[c][4]) for c in PLAN_COLS] + [(chunks[c][5], chunks[c][5] + chunks[c][6]) for c in LEVEL_COLS]


def _stat(col: str, v: bytes) -> int | str:
    if col == "path":
        return v.decode("utf-8", errors="replace")
    return struct.unpack("<q" if col == "ts" else "<i", v)[0]


def parse_ix(chunks: dict[str, list[int]], read) -> PageIx:
    """A group's `PageIx` from its index bytes (`read(offset, length)`)."""
    locs = {c: [(p[1], p[2], p[3]) for p in _Reader(read(chunks[c][5], chunks[c][6])).struct()[1]] for c in LEVEL_COLS}
    stats = {}
    for c in PLAN_COLS:
        ci = _Reader(read(chunks[c][3], chunks[c][4])).struct()
        stats[c] = [(_stat(c, a), _stat(c, b)) for a, b in zip(ci[2], ci[3])]
    return PageIx(locs, stats["depth"], stats["path"], stats["ts"])


def pages_of(locs: list[tuple[int, int, int]], a: int, b: int, n: int) -> tuple[int, int]:
    """`pagesOf`: the pages of `locs` overlapping rows `[a, b)` of a group of `n`: `(first, last)`, inclusive; (-1, -1) none."""
    first = last = -1
    for i, (_, _, s) in enumerate(locs):
        e = locs[i + 1][2] if i + 1 < len(locs) else n
        if e > a and s < b:
            if first < 0:
                first = i
            last = i
    return first, last


def plan_group(ix: PageIx, n: int, keys: list[tuple[int, str]], d1: int, d2: int) -> tuple[list[tuple[int, int]], int]:
    """A group's kept pages as row ranges, merged where they share a page in any column, and how many it keeps (`readLevel`)."""
    pages = ix.locs["depth"]
    ranges: list[list[int]] = []
    kept = 0
    for i in range(len(pages)):
        d0, d9 = ix.depth[i]
        p0, p9 = ix.path[i]
        t0, t9 = ix.ts[i]
        if not any(d0 <= d <= d9 and p0 <= p <= p9 for d, p in keys) or not (t9 > d1 and t0 <= d2):
            continue
        kept += 1
        a = pages[i][2]
        b = pages[i + 1][2] if i + 1 < len(pages) else n
        if ranges and ranges[-1][1] == a:
            ranges[-1][1] = b
        else:
            ranges.append([a, b])
    out: list[list[int]] = []
    for r in ranges:
        if out and any(pages_of(ix.locs[c], r[0], r[1], n)[0] <= pages_of(ix.locs[c], out[-1][0], out[-1][1], n)[1] for c in LEVEL_COLS):
            out[-1][1] = r[1]
        else:
            out.append(list(r))
    return [(a, b) for a, b in out], kept


def data_spans(ix: PageIx, chunks: dict[str, list[int]], ranges: list[tuple[int, int]], n: int) -> list[Span]:
    """What the decoder slices for a group's row ranges: per range and column, its dictionary page and its pages."""
    out = []
    for a, b in ranges:
        for c in LEVEL_COLS:
            locs = ix.locs[c]
            f, l = pages_of(locs, a, b, n)
            if f < 0:
                continue
            dpo, _, dict_off = chunks[c][:3]
            start = dict_off or dpo
            if dict_off or dpo < locs[0][0]:
                out.append((start, locs[0][0]))
            out.append((locs[f][0], locs[l][0] + locs[l][1]))
    return out


def chunk_span(chunks: dict[str, list[int]]) -> Span:
    """A group's whole-read span (`chunkSpan`): its first chunk's first page to its last chunk's end."""
    return (min(c[2] or c[0] for c in chunks.values()), max((c[2] or c[0]) + c[1] for c in chunks.values()))


def wave(per_file: list[list[Span]], cfg: Config, ttfb: float, mib_s: float, budget: int | None = None) -> dict:
    """A wave under `cfg`: `gets`, `rounds` (⌈gets / RUN_READS⌉), `bytes` fetched, the largest get, predicted `ms`."""
    if cfg.budget is not None:
        runs = fit(per_file, budget if budget is not None else cfg.budget, ttfb, mib_s)
    else:
        runs = [merged(sp, cfg.gap, cfg.cap) for sp in per_file]
    sizes = [e - s for r in runs for s, e in r]
    return {"gets": len(sizes), "rounds": ceil(len(sizes) / RUN_READS), "bytes": sum(sizes), "max_get": max(sizes, default=0), "ms": round(wave_ms(sizes, ttfb, mib_s), 1)}


@dataclass(frozen=True)
class SmallGroup:
    """One group of the sort cut in small groups with no page index, from a real group's page index. **The
    approximation:** a `R`-row group is the real pages holding its rows, whatever `R` (all columns' pages align at `R`'s
    divisors on gcs), its bytes those pages' compressed sizes, stored contiguously from the real group's first byte, plus
    each dictionary page once. A page of a real column chunk compresses as the same page of a small group's would (parquet
    compresses by page); at `R` > a page, one page over those rows would compress better than the sum, and a dictionary
    of the group's values alone is smaller than the chunk's: both make this an upper bound on the bytes, by little. Its
    bounds are those pages' page-index bounds, which the sidecar would carry."""
    rows: int
    start: int
    end: int
    d0: int
    d9: int
    p0: str
    p9: str
    t0: int
    t9: int
    cols: tuple[int, ...]

    def meets(self, keys: list[tuple[int, str]], d1: int, d2: int) -> bool:
        return self.t9 > d1 and self.t0 <= d2 and any(self.d0 <= d <= self.d9 and self.p0 <= p <= self.p9 for d, p in keys)


def small_groups(ix: PageIx, chunks: dict[str, list[int]], n: int, rows: int) -> list[SmallGroup]:
    """Real group `ix` as `rows`-row groups (`SmallGroup`)."""
    pages = ix.locs["depth"]
    for c in LEVEL_COLS:
        if [p[2] for p in ix.locs[c]] != [p[2] for p in pages]:
            raise ValueError(f"column {c}'s pages don't align with depth's")
    by: dict[int, list[int]] = defaultdict(list)
    for i, p in enumerate(pages):
        by[p[2] // rows].append(i)
    out = []
    at = chunk_span(chunks)[0]
    for b in sorted(by):
        ps = by[b]
        if len({pages[i][2] // rows for i in ps}) != 1 or pages[ps[0]][2] != b * rows or (ps[-1] + 1 < len(pages) and pages[ps[-1] + 1][2] != (b + 1) * rows):
            raise ValueError(f"pages at rows {[pages[i][2] for i in ps]} don't divide {rows}-row groups")
        end_row = pages[ps[-1] + 1][2] if ps[-1] + 1 < len(pages) else n
        cols = tuple(ix.locs[c][0][0] - (chunks[c][2] or chunks[c][0]) + sum(ix.locs[c][i][1] for i in ps) for c in LEVEL_COLS)
        size = sum(cols)
        out.append(SmallGroup(end_row - pages[ps[0]][2], at, at + size, min(ix.depth[i][0] for i in ps), max(ix.depth[i][1] for i in ps),
                              min(ix.path[i][0] for i in ps), max(ix.path[i][1] for i in ps), min(ix.ts[i][0] for i in ps), max(ix.ts[i][1] for i in ps), cols))
        at += size
    return out


class Sim:
    """A generation's level sort, the page indexes fetched so far (cached on disk), and the configs to price."""

    def __init__(self, store, tiers: list[Tier], stamps: dict[str, int], tier_scans: dict[str, set[int]], cfgs: list[Config], ttfb: float, mib_s: float, threads: int, cache: Path):
        self.store, self.tiers, self.stamps, self.tier_scans = store, tiers, stamps, tier_scans
        self.cfgs, self.ttfb, self.mib_s, self.threads, self.cache = cfgs, ttfb, mib_s, threads, cache
        self.raw: dict[tuple[str, int], bytes] = {}
        self.parsed: dict[tuple[str, int], PageIx] = {}

    def groups(self, q: dict) -> list[tuple[Tier, list[int]]]:
        d1, d2 = self.stamps[q["from"]], self.stamps[q["to"]]
        keys = [(0 if p == "" else p.count("/") + 1, p) for p in q["dirs"]]
        out = []
        for t in self.tiers:
            if not any(d1 < s <= d2 for s in self.tier_scans[t.key]):
                continue
            out.append((t, t.kept(keys, t.in_range(d1, d2))))
        return out

    def _cache_file(self, t: Tier, i: int) -> Path:
        return self.cache / re.sub(r"[^A-Za-z0-9._-]+", "_", t.key) / f"{i}.bin"

    def prefetch(self, qs: list[dict]) -> None:
        """Each kept group's index bytes, once, as a single concatenation per group on disk (`-c`)."""
        want: dict[tuple[str, int], Tier] = {}
        for q in qs:
            for t, ix in self.groups(q):
                for i in ix:
                    want[(t.key, i)] = t
        todo = [(t, i) for (_, i), t in want.items() if not self._cache_file(t, i).exists()]
        err(f"{len(want)} distinct groups kept over {len(qs)} requests; fetching {len(todo)} page indexes")
        t0 = time.monotonic()

        def one(ti: tuple[Tier, int]) -> None:
            t, i = ti
            _, chunks = t.chunks(i)
            spans = merged(index_spans(chunks), 0, 1 << 62)
            data = b"".join(self.store.read_range(t.key, s, e) for s, e in spans)
            f = self._cache_file(t, i)
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, f)

        with ThreadPoolExecutor(self.threads) as ex:
            for n, _ in enumerate(ex.map(one, todo), 1):
                if n % 2000 == 0:
                    err(f"  {n}/{len(todo)} in {time.monotonic() - t0:.0f}s")
        err(f"fetched {len(todo)} in {time.monotonic() - t0:.0f}s")

    def ix(self, t: Tier, i: int, chunks: dict[str, list[int]]) -> PageIx:
        k = (t.key, i)
        if k not in self.parsed:
            spans = merged(index_spans(chunks), 0, 1 << 62)
            buf = self._cache_file(t, i).read_bytes()
            at = {}
            o = 0
            for s, e in spans:
                at[s] = o
                o += e - s

            def read(off: int, n: int) -> bytes:
                s = max(x for x, _ in spans if x <= off)
                return buf[at[s] + off - s:at[s] + off - s + n]

            self.parsed[k] = parse_ix(chunks, read)
        return self.parsed[k]

    def simulate(self, q: dict) -> dict:
        d1, d2 = self.stamps[q["from"]], self.stamps[q["to"]]
        keys = [(0 if p == "" else p.count("/") + 1, p) for p in q["dirs"]]
        ix_spans, dt_spans, whole = [], [], []
        whole_rows = 0
        groups = pages = rows = group_max = 0
        for t, kept in self.groups(q):
            ii, dd, ww = [], [], []
            for i in kept:
                n, chunks = t.chunks(i)
                ii += index_spans(chunks)
                ww.append(chunk_span(chunks))
                whole_rows += n
                ranges, k = plan_group(self.ix(t, i, chunks), n, keys, d1, d2)
                groups += 1
                pages += k
                r = sum(b - a for a, b in ranges)
                rows += r
                group_max = max(group_max, r)
                if ranges:
                    dd += data_spans(self.ix(t, i, chunks), chunks, ranges, n)
            ix_spans.append(ii)
            dt_spans.append(dd)
            whole.append(ww)
        out = {**{k: v for k, v in q.items() if k != "dirs"}, "dirs": len(q["dirs"]), "groups": groups, "pages": pages, "rows": rows,
               "needed_bytes": sum(e - s for sp in dt_spans for s, e in merged(sp, 0, 1 << 62)), "index_needed_bytes": sum(e - s for sp in ix_spans for s, e in merged(sp, 0, 1 << 62)),
               "decoded_bytes": rows * ROW_BYTES, "cfg": {}}
        for c in self.cfgs:
            a = wave(ix_spans, c, self.ttfb, self.mib_s)
            b = wave(dt_spans, c, self.ttfb, self.mib_s, None if c.budget is None else max(c.budget - a["bytes"], 0))
            out["cfg"][c.label] = {"index": a, "data": b, "ms": round(a["ms"] + b["ms"], 1), "fetched": a["bytes"] + b["bytes"]}
        w = wave(whole, Config("whole"), self.ttfb, self.mib_s)
        out["whole"] = {"data": w, "ms": w["ms"], "fetched": w["bytes"], "rows": whole_rows, "decoded_bytes": whole_rows * ROW_BYTES}
        return out


class FirstTouch:
    """What's held so far, from empty, in a case's read order: a thing read at a `(level, round)` is held from the next;
    a concurrent batch of the same `(level, round)` that wants it joins the read (waits for it, reads nothing)."""

    def __init__(self):
        self.held: set = set()
        self.reading: dict = {}
        self.at = None

    def split(self, at: tuple[int, int], ids: Iterable) -> tuple[list, list]:
        """Of `ids`, those to read here and those another batch's read holds; (the rest are held)."""
        if at != self.at:
            self.held |= set(self.reading)
            self.reading, self.at = {}, at
        mine, joined = [], []
        for i in ids:
            if i in self.held:
                continue
            (joined if i in self.reading else mine).append(i)
        return mine, joined

    def read(self, ids: list, ms: float) -> None:
        for i in ids:
            self.reading[i] = ms

    def wait(self, joined: list) -> float:
        return max((self.reading[i] for i in joined), default=0.0)


def small_sidecar(sim: "Sim", rows: int, tmp: Path) -> tuple[float, int]:
    """A `rows`-row cut's sidecar: bytes per row and rows measured by cutting one with the sort's real writer (`write_groups`'
    schema, codec and stats, 512-row groups, no page-index offsets in `rg_json`) over the page indexes held (the kept
    groups', in file order: shorter paths than the file's mean, so low if anything), and its groups' rows: the rows the
    file would have."""
    out = []
    ordinal = 0
    held = [(t, int(f.stem)) for t in sim.tiers for f in sorted(sim._cache_file(t, 0).parent.glob("*.bin"), key=lambda f: int(f.stem))]
    for t, i in held:
        n, chunks = t.chunks(i)
        ix = sim.ix(t, i, chunks)
        row = 0
        for g in small_groups(ix, chunks, n, rows):
            at = g.start
            cols = []
            for size in g.cols:
                cols.append([at, size, 0])
                at += size
            out.append({"rg": ordinal, "seg": 0, "d_min": g.d0, "d_max": g.d9, "p_min": g.p0, "p_max": g.p9, "b_min": 0, "b_max": 0, "u_min": None, "u_max": None,
                        "vf_min": g.t0, "vf_max": g.t9, "vt_min": g.t0, "vt_max": g.t9, "row_start": row, "row_end": row + g.rows, "rg_json": json.dumps([g.rows, "ZSTD", cols], separators=(",", ":"))})
            ordinal += 1
            row += g.rows
    tmp.mkdir(parents=True, exist_ok=True)
    f = tmp / f"small-{rows}.groups.parquet"
    tbl = pa.table({c: [r[c] for r in out] for c in GROUPS_SCHEMA.names}, schema=GROUPS_SCHEMA)
    with pq.ParquetWriter(f, GROUPS_SCHEMA, compression="zstd", write_statistics=GROUPS_STAT_COLS, store_schema=False) as w:
        w.write_table(tbl, row_group_size=512)
    md = pq.read_metadata(f)
    per_row = (f.stat().st_size - md.serialized_size) / len(out)
    return per_row, int(sum(t.small_base(rows)[-1] for t in sim.tiers))


def modes(sim: "Sim", reqs: list[dict], smalls: list[int], fg_rows: list[int], per_row: dict[int, float]) -> dict[str, dict[int, dict]]:
    """Per mode, per request (its index in `reqs`), its priced cost. The modes:

    - `8K pages, today`: every read its page indexes and pages; footer groups from first touch (one case, from empty).
    - `8K pages, held, cold`: indexes and footer groups held from first touch, one case, from empty: what a first paint
      and its expansion cost in a cold colo.
    - `8K pages, held, colo warm`: a new isolate over a warm colo: indexes and footer groups from the colo's, once each.
    - `8K pages, held, warm`: every index and footer group held: pages only.
    - `<R> rows/F<F>, cold`: small groups, footer groups of `F` rows from first touch, then the kept groups; `, colo warm`:
      those footer groups from the colo's; `, warm`: every footer group held: the kept groups only.

    A footer group or index read from the store is looked for in the colo first; a joined one isn't. An `<R>`'s footer groups
    are those holding a kept group (a lower bound: `pqGroups`' bounds also pass neighbours that hold none, as today's do).
    """
    today, cold, colo_warm, warm = {}, {}, {}, {}
    small_cold: dict[tuple[int, int], dict] = {(r, f): {} for r in smalls for f in fg_rows}
    small_warm: dict[int, dict] = {r: {} for r in smalls}
    small_colo: dict[int, dict] = {r: {} for r in smalls}
    by_case: dict[object, list[int]] = defaultdict(list)
    for i, q in enumerate(reqs):
        by_case[q.get("case", i)].append(i)
    cfg = Config("today")
    groups_memo: dict[tuple[str, int, int], list[SmallGroup]] = {}

    def smalls_of(t: Tier, i: int, n: int, chunks: dict[str, list[int]], r: int) -> list[SmallGroup]:
        k = (t.key, i, r)
        if k not in groups_memo:
            groups_memo[k] = small_groups(sim.ix(t, i, chunks), chunks, n, r)
        return groups_memo[k]

    def sizes(per_file: list[list[Span]]) -> list[int]:
        return [e - s for sp in per_file for s, e in merged(sp, cfg.gap, cfg.cap)]

    for case, idx in by_case.items():
        ix_cold, ix_colo, fg_cold, fg_colo = FirstTouch(), FirstTouch(), FirstTouch(), FirstTouch()
        small_fg = {(r, f): FirstTouch() for r in smalls for f in fg_rows}
        small_fg_colo = {r: FirstTouch() for r in smalls}
        ordered = sorted(idx, key=lambda j: (reqs[j].get("level", 0), reqs[j].get("round", 0), reqs[j].get("batch", 0)))
        for j in ordered:
            q = reqs[j]
            at = (q.get("level", 0), q.get("round", 0))
            d1, d2 = sim.stamps[q["from"]], sim.stamps[q["to"]]
            keys = [(0 if p == "" else p.count("/") + 1, p) for p in q["dirs"]]
            ix_by: dict[tuple[str, int], list[Span]] = {}
            data: list[list[Span]] = []
            rows = 0
            fgs: list[tuple[str, int, int]] = []
            small_kept = {r: [] for r in smalls}
            small_rows = dict.fromkeys(smalls, 0)
            small_ids = {(r, f): set() for r in smalls for f in fg_rows}
            for t, kept in sim.groups(q):
                dd: list[Span] = []
                fgs += [(t.key, n, t.fg_bytes[n]) for n in t.footer_groups(keys, d1, d2)]
                for i in kept:
                    n, chunks = t.chunks(i)
                    ix = sim.ix(t, i, chunks)
                    ix_by[(t.key, i)] = index_spans(chunks)
                    ranges, _ = plan_group(ix, n, keys, d1, d2)
                    rows += sum(b - a for a, b in ranges)
                    if ranges:
                        dd += data_spans(ix, chunks, ranges, n)
                    for r in smalls:
                        base = t.small_base(r)
                        for g_i, g in enumerate(smalls_of(t, i, n, chunks, r)):
                            if g.meets(keys, d1, d2):
                                small_rows[r] += g.rows
                                small_kept[r].append((t.key, g))
                                for f in fg_rows:
                                    small_ids[(r, f)].add((t.key, int(base[i] + g_i) // f))
                data.append(dd)
            dsizes = sizes(data)

            def ix_sizes(ids: list) -> list[int]:
                return sizes([[s for k in ids for s in ix_by[k] if k[0] == key] for key in sorted({k[0] for k in ids})])

            def touch(state: FirstTouch, ids: list, price) -> dict:
                """`ids` read from first touch: `gets`, `looked` (colo lookups ahead of them), and what joining others' waits past them."""
                mine, joined = state.split(at, ids)
                wv = price(mine)
                ms = wave_ms(wv, sim.ttfb, sim.mib_s)
                state.read(mine, ms)
                return {"gets": wv, "looked": len(mine), "wait": max(0.0, state.wait(joined) - ms)}

            def colo_touch(state: FirstTouch, ids: list) -> int:
                """`ids` from a warm colo from first touch: its lookups."""
                mine, _ = state.split(at, ids)
                state.read(mine, 0.0)
                return len(mine)

            fg_bytes = {(key, n): b for key, n, b in fgs}
            fg = touch(fg_cold, list(fg_bytes), lambda ids: [fg_bytes[k] for k in ids])
            ix = touch(ix_cold, list(ix_by), ix_sizes)
            today[j] = dict(waves=[fg["gets"], sizes(_by_tier(ix_by)), dsizes], rows=rows, lookups=fg["looked"], extra=fg["wait"], fgs=len(fg_bytes), groups=len(ix_by))
            cold[j] = dict(waves=[fg["gets"], ix["gets"], dsizes], rows=rows, lookups=fg["looked"] + ix["looked"], extra=fg["wait"] + ix["wait"], fgs=len(fg_bytes), groups=len(ix_by))
            colo_warm[j] = dict(waves=[dsizes], rows=rows, lookups=colo_touch(ix_colo, list(ix_by)) + colo_touch(fg_colo, list(fg_bytes)), fgs=len(fg_bytes), groups=len(ix_by))
            warm[j] = dict(waves=[dsizes], rows=rows, fgs=len(fg_bytes), groups=len(ix_by))
            for r in smalls:
                by_file: dict[str, list[Span]] = defaultdict(list)
                for key, g in small_kept[r]:
                    by_file[key].append((g.start, g.end))
                sw = sizes(list(by_file.values()))
                kept_n = len(small_kept[r])
                small_warm[r][j] = dict(waves=[sw], rows=small_rows[r], groups=kept_n)
                small_colo[r][j] = dict(waves=[sw], rows=small_rows[r], groups=kept_n, lookups=colo_touch(small_fg_colo[r], sorted(small_ids[(r, fg_rows[0])])), fgs=len(small_ids[(r, fg_rows[0])]))
                for f in fg_rows:
                    each = round(per_row[r] * f)
                    w = touch(small_fg[(r, f)], sorted(small_ids[(r, f)]), lambda ids: [each] * len(ids))
                    small_cold[(r, f)][j] = dict(waves=[w["gets"], sw], rows=small_rows[r], lookups=w["looked"], extra=w["wait"], fgs=len(small_ids[(r, f)]), groups=kept_n)
    out = {"8K pages, today": today, "8K pages, held, cold": cold, "8K pages, held, colo warm": colo_warm, "8K pages, held, warm": warm}
    for r in smalls:
        for f in fg_rows:
            out[f"{r} rows/F{f}, cold"] = small_cold[(r, f)]
        out[f"{r} rows/F{fg_rows[0]}, colo warm"] = small_colo[r]
        out[f"{r} rows, warm"] = small_warm[r]
    return out


def _by_tier(ix_by: dict[tuple[str, int], list[Span]]) -> list[list[Span]]:
    tiers: dict[str, list[Span]] = defaultdict(list)
    for (key, _), spans in ix_by.items():
        tiers[key] += spans
    return list(tiers.values())


def mode_ms(m: dict, ttfb: float, mib_s: float, colo_ms: float) -> float:
    """A request's predicted time in a mode: its colo lookups, its waves in series, and past them what it waits on others."""
    return ceil(m.get("lookups", 0) / RUN_READS) * colo_ms + sum(wave_ms(w, ttfb, mib_s) for w in m["waves"]) + m.get("extra", 0.0)


def mode_table(reqs: list[dict], ms: dict[str, dict[int, dict]], ttfb: float, mib_s: float, colo_ms: float) -> list[dict]:
    """Per mode: `gets`, `rounds` at `RUN_READS` (each wave's, summed), `bytes`, decoded `rows`, and predicted ms: the first
    level's (`first_*`) and the full expansion's (`full_*`), a level's batches together, a batch's rounds in series."""
    rows = []
    for name, per in ms.items():
        by: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
        for j, m in per.items():
            q = reqs[j]
            by[q.get("case", j)][q.get("level", 0)][q.get("batch", 0)] += mode_ms(m, ttfb, mib_s, colo_ms)
        first = [max(lv[min(lv)].values()) for lv in by.values()]
        full = [sum(max(b.values()) for b in lv.values()) for lv in by.values()]
        rows.append({"mode": name, "gets": sum(len(w) for m in per.values() for w in m["waves"]), "rounds": sum(ceil(len(w) / RUN_READS) for m in per.values() for w in m["waves"]),
                     "bytes": sum(b for m in per.values() for w in m["waves"] for b in w), "rows": sum(m["rows"] for m in per.values()), "lookups": sum(m.get("lookups", 0) for m in per.values()),
                     "fgs_max": max(m.get("fgs", 0) for m in per.values()), "groups_max": max(m.get("groups", 0) for m in per.values()),
                     "first_p50": float(np.median(first)), "first_max": max(first), "full_p50": float(np.median(full)), "full_max": max(full), "full_sum": sum(full)})
    return rows


def summarize(results: list[dict], labels: list[str]) -> list[str]:
    """Per config, over requests: p50 / p90 / p99 / max of gets, rounds, fetched MiB and predicted ms."""
    lines = [f"{len(results)} requests: rows {pct([r['rows'] for r in results])}, groups {pct([r['groups'] for r in results])} (p50 / p90 / p99 / max)"]
    for c in labels:
        ms = [r["cfg"][c] for r in results]
        lines.append(f"{c:<12} gets {pct([m['index']['gets'] + m['data']['gets'] for m in ms])} rounds {pct([m['index']['rounds'] + m['data']['rounds'] for m in ms])} "
                     f"fetched KiB {pct([m['fetched'] >> 10 for m in ms])} ms {pct([round(m['ms']) for m in ms])}")
    w = [r["whole"] for r in results]
    lines.append(f"{'whole':<12} gets {pct([m['data']['gets'] for m in w])} rounds {pct([m['data']['rounds'] for m in w])} fetched KiB {pct([m['fetched'] >> 10 for m in w])} "
                 f"ms {pct([round(m['ms']) for m in w])} rows {pct([m['rows'] for m in w])}")
    return lines


@command("level-sim")
@expect_sha_options("interval-store level-sim")
@option("-b", "--budgets", default="", help="`fit:B` configs: the fastest merge within so many fetched bytes (`16M,32M`)")
@option("-c", "--cache", default="tmp/level-sim", help="Local cache of the sidecars and the kept groups' page indexes")
@option("-C", "--caps", default="2M,none", help="`RUN_BYTES` values (`none`: no ceiling), crossed with the gaps")
@option("-g", "--gaps", default="512K,1M,2M,4M,8M,16M,32M", help="`RUN_GAP` values")
@option("-o", "--out", help="Output JSONL (default stdout)")
@option("-F", "--footer-rows", default="512", help="With `-s`: the sidecar's row-group sizes to price")
@option("-K", "--colo-ms", default=COLO_MS, type=float, help="With `-s`: one colo lookup's time, ms")
@option("-O", "--table", help="With `-s`: the mode table as JSONL")
@option("-R", "--throughput", default=THROUGHPUT_MIB_S, type=float, help="One get's throughput, MiB/s")
@option("-s", "--small", default="", help="Price held page indexes and row-group sizes with no page index (`256,512,1024`): the mode table to stderr")
@option("-t", "--threads", default=32, type=IntRange(min=1), help="Page-index fetches in flight")
@option("-T", "--ttfb", default=TTFB_MS, type=float, help="One get's time to first byte, ms")
@argument("gen")
@argument("requests")
def level_sim_cmd(budgets, cache, caps, colo_ms, footer_rows, gaps, out, small, table, throughput, threads, ttfb, gen, requests) -> None:
    """Price `readLevel`'s two waves over generation GEN's `level` sort (a dir or URL) under a sweep of coalescing configs.
    REQUESTS: JSONL of `{"from", "to", "dirs": [...]}`, one per read (a chain's round is its own); other keys are copied to
    the output. A summary goes to stderr."""
    from .append_runner import latest_key
    from .blobstore import Store

    store = Store(gen)
    scans = store.read_json("scans.json")["scans"]
    stamps = {s["id"]: int(s["ts"]) for s in scans}
    srcs: list[tuple[str, str, set[int]]] = [("served/level", "base", {int(s["ts"]) for s in scans})]
    manifest = latest_key(store.listing("manifests"))
    if manifest:
        m = store.read_json(manifest)
        stamps.update({k: int(v) for k, v in m["stamps"].items()})
        srcs += [(f"{r['key']}/served/level", r["key"], {int(m["stamps"][x]) for x in r["scans"]}) for r in m["runs"]]
    root = Path(cache) / re.sub(r"[^A-Za-z0-9._-]+", "_", gen)
    root.mkdir(parents=True, exist_ok=True)
    tiers, tier_scans = [], {}
    for stem, label, ts in srcs:
        side = root / f"{re.sub(r'[^A-Za-z0-9._-]+', '_', stem)}.groups.parquet"
        if not side.exists():
            store.get(f"{stem}.groups.parquet", side)
        t = Tier(f"{stem}.parquet", side, label)
        tiers.append(t)
        tier_scans[t.key] = ts
    cfgs = configs(gaps, caps, budgets)
    reqs = [json.loads(line) for line in open(requests) if line.strip()]
    if bad := sorted({s for q in reqs for s in (q["from"], q["to"]) if s not in stamps}):
        raise SystemExit(f"scans not in {gen}: {', '.join(bad)}")
    sim = Sim(store, tiers, stamps, tier_scans, cfgs, ttfb, throughput, threads, root / "ix")
    sim.prefetch(reqs)
    t0 = time.monotonic()
    results = [sim.simulate(q) for q in reqs]
    err(f"simulated {len(reqs)} in {time.monotonic() - t0:.0f}s")
    lines = "".join(json.dumps(r) + "\n" for r in results)
    if out:
        Path(out).write_text(lines)
    else:
        sys.stdout.write(lines)
    for line in summarize(results, [c.label for c in cfgs]):
        err(line)
    smalls = [int(x) for x in small.split(",") if x]
    if smalls:
        fg_rows = [int(x) for x in footer_rows.split(",") if x]
        sized = {r: small_sidecar(sim, r, root / "small") for r in smalls}
        tbl = mode_table(reqs, modes(sim, reqs, smalls, fg_rows, {r: v[0] for r, v in sized.items()}), ttfb, throughput, colo_ms)
        if table:
            Path(table).write_text("".join(json.dumps(r) + "\n" for r in tbl))
        err(f"{'mode':<30}{'gets':>7}{'rounds':>8}{'MiB':>8}{'rows':>10}{'looked':>8}{'fgs/rq':>7}{'grps/rq':>8}{'first p50':>11}{'first max':>11}{'full p50':>10}{'full max':>10}{'full sum':>10}")
        for r in tbl:
            err(f"{r['mode']:<30}{r['gets']:>7}{r['rounds']:>8}{r['bytes'] / MIB:>8.1f}{r['rows']:>10}{r['lookups']:>8}{r['fgs_max']:>7}{r['groups_max']:>8}{r['first_p50']:>11.0f}{r['first_max']:>11.0f}{r['full_p50']:>10.0f}{r['full_max']:>10.0f}{r['full_sum']:>10.0f}")
        t = tiers[0]
        for r, (per_row, n) in sized.items():
            for f in fg_rows:
                fgs = ceil(n / f)
                err(f"{r}-row cut, F{f}: {n} groups, sidecar {per_row:.0f} B/row = {per_row * n / MIB:.0f} MiB, {fgs} footer groups of {per_row * f / 1024:.0f} KiB, "
                    f"sidecar footer {t.sidecar_footer / len(t.fg_rows) * fgs / MIB:.1f} MiB of thrift")
        real = store.size(t.key)
        tail = struct.unpack("<i", store.read_range(t.key, real - 8, real - 4))[0]
        data_end = min(c[3] for c in t.chunks(0)[1].values() if c[3])
        err(f"today: file {real / MIB:.0f} MiB = data {data_end / MIB:.0f} + page indexes {(real - 8 - tail - data_end) / MIB:.0f} + footer {tail / MIB:.0f} ({tail / len(t.rows):.0f} B/group); "
            f"sidecar {t.sidecar_path.stat().st_size / MIB:.1f} MiB, {len(t.fg_rows)} footer groups")
