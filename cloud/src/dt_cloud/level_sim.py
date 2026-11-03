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
from dataclasses import dataclass
from functools import partial
from math import ceil
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from click import IntRange, argument, command, option

from .image_sha import expect_sha_options
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
@option("-R", "--throughput", default=THROUGHPUT_MIB_S, type=float, help="One get's throughput, MiB/s")
@option("-t", "--threads", default=32, type=IntRange(min=1), help="Page-index fetches in flight")
@option("-T", "--ttfb", default=TTFB_MS, type=float, help="One get's time to first byte, ms")
@argument("gen")
@argument("requests")
def level_sim_cmd(budgets, cache, caps, gaps, out, throughput, threads, ttfb, gen, requests) -> None:
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
