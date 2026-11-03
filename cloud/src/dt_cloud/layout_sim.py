"""Offline: what `/api/diff/level` reads from a served sort (specs/diff-progressive.md §3b), under candidate layouts,
from a generation's own groups sidecar and row groups. Modes per request (dir P, from scan D1, to scan D2):

- `groups` (today): the row groups `planChanged` keeps (P's children's rect `(dP+1, [P/, P0))`, and `vf`/`vt` bounds
  that can hold a version opening or closing in (D1, D2]), each decoded whole.
- pages of p rows (2b.1, `-p`): within each kept group, the p-row pages a page index keeps: those overlapping the rect's
  contiguous `(depth, path)` range (or straddling where it would sit, when it is empty) whose own `vf`/`vt` bounds pass
  the same test. Pages never cross a group.
- B bands (§3b "Next" 3, `-b`): the sort re-laid `(band, depth, path, vf)`, each version in a band by its dyadic
  segment's level (the open versions band 0, closed levels 0..KMAX split evenly over bands 1..B-1; B = 1 is one band),
  groups free to cross segments, cut in `-P`-row pages kept by their `vf`/`vt` bounds. A band's pages are computed over
  the rect's own rows in band order, so its edge pages' neighbour rows are ignored (≤ 2 pages a band).
- chunked dyadic, per K0 (`-k`): the open versions one band; a closed version at level k ≥ K0 keeps its own segment
  `(k, block)` as a band (today's segments); one below K0 goes to chunk `block >> (K0 − k)`, the fixed 2^K0-scan window
  it lies in. Bands, pages and runs as `-b`'s. K0 = 0 is today's seg-major layout; K0 > KMAX is B = 2.

Every version's band is computed from its first / last live scan indices (i, j) in `scans.json` (`seg_sql`'s rule:
level `k = bit_length(i ^ j)`, block `i >> k`); KMAX = `bit_length(scans − 1)`, the highest level a version can reach.

History (`-H R`): the band and chunked modes again in a store R× as old: the rect's versions in R copies of the scan
sequence end to end (copy m's stamps = the originals + m·(span + last step)), each copy's open versions closing at its
end except the last copy's, segments recomputed over the extended sequence, each request shifted into the last copy.
The `groups` and page modes are today's physical file and stay at R = 1 (`-k 0` is the seg-major layout at R).

Per mode: rows decoded, and GET runs: the kept spans merged as the site's `planRuns` merges them (`RUN_GAP`, `RUN_BYTES`),
converted to rows at the sort's bytes per row (`served/<sort>.json`); bands are separate regions of the file, so their
runs never merge. A request's GET rounds for a read of `w` dependent waves are `w · ⌈runs / RUN_READS⌉`.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from math import ceil
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from click import IntRange, argument, command, option

from .static_names import OPEN

err = partial(print, file=sys.stderr, flush=True)

#: Mirrors of the site's range-read constants (`site/functions/_lib/index.ts`, same names): spans closer than `RUN_GAP`
#: bytes merge into one read, a merged read stays within `RUN_BYTES`, and `RUN_READS` reads are in flight at once.
RUN_GAP = 512 << 10
RUN_BYTES = 2 << 20
RUN_READS = 6
#: A dyadic segment's id: `1 + level · SEG_LEVEL + block`; 0 is the open versions (`interval_store.SEG_LEVEL`).
SEG_LEVEL = 1 << 16
#: The served sort's band-mode page (`interval_store.SERVED_PAGE_ROWS`).
BAND_PAGE = 256
#: The columns a simulation reads from each row group (cached locally as such).
COLS = ["depth", "path", "vf", "vt"]
SIDECAR_COLS = ["rg", "seg", "d_min", "d_max", "p_min", "p_max", "vf_min", "vf_max", "vt_min", "vt_max", "row_start", "row_end"]
TOP = "\U0010ffff"
#: The waves a level read takes: a whole-group read is two (metadata, data), a paged read three (+ the page index).
WAVES = (2, 3)


def rect(P: str) -> tuple[int, str, str]:
    """P's children's rect: `(depth, lo, hi)`, paths in `[lo, hi)` (the root's children: depth 1, every path)."""
    return (1, "", TOP) if P == "" else (P.count("/") + 2, P + "/", P + "0")


def changes_in(vf_min, vf_max, vt_min, vt_max, d1: int, d2: int):
    """Whether `vf`/`vt` bounds can hold a version opening or closing in (D1, D2] (`planChanged`'s test)."""
    return ((vf_max > d1) & (vf_min <= d2)) | ((vt_max > d1) & (vt_min <= d2))


def plan_runs(spans: list[tuple[int, int]], gap: int, cap: int) -> int:
    """How many merged reads `spans` (`[start, end)`) take, as the site's `planRuns`: sorted by start, a run grows while
    the next span starts within `gap` of its end and the run stays within `cap` (a span bigger than `cap` is its own)."""
    runs = 0
    start = end = None
    for s, e in sorted(spans):
        if end is not None and s - end <= gap and max(end, e) - start <= cap:
            end = max(end, e)
        else:
            runs += 1
            start, end = s, e
    return runs


def adjacent_runs(spans: list[tuple[int, int]]) -> int:
    """Runs of spans that touch end to start (no gap merging)."""
    runs, end = 0, None
    for s, e in sorted(spans):
        if s != end:
            runs += 1
        end = e
    return runs


def parse_ints(s: str | None) -> list[int]:
    """`"1024,256"` → `[1024, 256]`; `""` / `"none"` → `[]` (that mode skipped)."""
    s = (s or "").strip()
    return [] if s in ("", "none") else [int(x) for x in s.split(",")]


def levels(i: np.ndarray, j: np.ndarray) -> np.ndarray:
    """A closed version's dyadic level from its first and last live scan indices (`seg_sql`): `bit_length(i ^ j)`."""
    x = np.bitwise_xor(i, j)
    k = np.zeros(len(x), dtype=np.int64)
    pos = x > 0
    k[pos] = np.floor(np.log2(x[pos])).astype(np.int64) + 1
    return k


def band_of(is_open: np.ndarray, k: np.ndarray, B: int, kmax: int) -> np.ndarray:
    """Each version's band: B = 1 one band; else the open versions band 0 and closed levels 0..KMAX split evenly over
    bands 1..B-1."""
    if B == 1:
        return np.zeros(len(k), dtype=np.int64)
    return np.where(is_open, 0, 1 + (k * (B - 1)) // (kmax + 1))


def chunk_of(is_open: np.ndarray, i: np.ndarray, k: np.ndarray, k0: int) -> np.ndarray:
    """Each version's band in the chunked dyadic layout: the open versions one band; a closed version at level k ≥ K0
    its own segment's (k, block = i >> k); below, chunk `i >> K0` (its 2^K0-scan window: `block >> (K0 − k)`)."""
    own = 1 + k * SEG_LEVEL + (i >> k)  # its segment id (> 0)
    return np.where(is_open, 0, np.where(k >= k0, own, -1 - (i >> k0)))


def replicate(i: np.ndarray, j: np.ndarray, is_open: np.ndarray, n: int, R: int):
    """The rect's versions in a store R× as old: R copies of the `n`-scan sequence end to end; copy m's versions at
    scan indices `+ m·n`; an open version of copy m < R − 1 closes at that copy's end (live through its last scan:
    `j = (m + 1)·n − 1`, `vt` the next copy's first stamp); the last copy's stay open. `(i, j, open)` over copies."""
    I, J, O = [], [], []
    for m in range(R):
        last = m == R - 1
        I.append(i + m * n)
        J.append(np.where(is_open, -1 if last else (m + 1) * n - 1, j + m * n))
        O.append(is_open if last else np.zeros_like(is_open))
    return np.concatenate(I), np.concatenate(J), np.concatenate(O)


def extended(stamps: np.ndarray, R: int) -> np.ndarray:
    """R copies of the scan stamps end to end: copy m's are the originals + m·(span + step), step the last interval."""
    n = len(stamps)
    step = int(stamps[-1] - stamps[-2]) if n > 1 else 1
    off = int(stamps[-1] - stamps[0]) + step
    return np.concatenate([stamps + m * off for m in range(R)])


@dataclass
class Sim:
    """One generation's sort, as the simulation needs it: its groups sidecar, scan epochs, the modes, and where its
    fetched row groups are cached. Pickled once into each worker process."""
    side: pd.DataFrame
    ts: dict[str, int]
    cache: str
    pages: list[int]
    bands: list[int]
    band_page: int
    row_bytes: float
    run_gap: int = RUN_GAP
    chunks: list[int] = field(default_factory=list)
    history: int = 1

    def __post_init__(self):
        self.stamps = np.array(sorted(self.ts.values()), dtype=np.int64)
        self.gap_rows = int(self.run_gap / self.row_bytes)
        self.cap_rows = int(RUN_BYTES / self.row_bytes)

    @property
    def layouts(self) -> bool:
        return bool(self.bands or self.chunks)

    def kmax(self, R: int = 1) -> int:
        """KMAX: the highest dyadic level a version can reach over the generation's scans (R× as many)."""
        return max(len(self.stamps) * R - 1, 0).bit_length()

    def cache_file(self, rg: int) -> str:
        return f"{self.cache}/rg-{rg}.parquet"

    def rect_groups(self, P: str) -> pd.DataFrame:
        """The sidecar rows whose `(depth, path)` range meets P's rect, as composite keys."""
        d, lo, hi = rect(P)
        s = self.side[(self.side.d_min <= d) & (self.side.d_max >= d)]
        return s[((s.d_max > d) | (s.p_max >= lo)) & ((s.d_min < d) | (s.p_min < hi))]

    def kept_groups(self, P: str, d1: int, d2: int) -> pd.DataFrame:
        s = self.rect_groups(P)
        return s[changes_in(s.vf_min, s.vf_max, s.vt_min, s.vt_max, d1, d2)]

    def groups_to_read(self, q: dict) -> list[int]:
        """The row groups request `q` reads: its kept groups, and every rect group when layouts are simulated."""
        d1, d2 = self.ts[q["from"]], self.ts[q["to"]]
        s = self.rect_groups(q["path"]) if self.layouts else self.kept_groups(q["path"], d1, d2)
        return s.rg.tolist()

    def simulate(self, q: dict) -> dict:
        P, d1, d2 = q["path"], self.ts[q["from"]], self.ts[q["to"]]
        d, lo, hi = rect(P)
        rg_all = self.rect_groups(P)
        kept = rg_all[changes_in(rg_all.vf_min, rg_all.vf_max, rg_all.vt_min, rg_all.vt_max, d1, d2)]
        out = {**q, "rect_groups": len(rg_all), "kept_groups": len(kept), "group_rows": int((kept.row_end - kept.row_start).sum())}
        g_spans = list(zip(kept.row_start.tolist(), kept.row_end.tolist()))
        out["groups"] = {"rows": out["group_rows"], "runs": plan_runs(g_spans, self.gap_rows, self.cap_rows), "adj_runs": adjacent_runs(g_spans)}
        spans: dict[int, list[tuple[int, int]]] = {p: [] for p in self.pages}
        in_rect = needed = 0
        for rg, row_start in zip(kept.rg.tolist(), kept.row_start.tolist()):
            t = pq.read_table(self.cache_file(rg))
            dep, pth = t["depth"].to_numpy(), t["path"]
            vf, vt = t["vf"].to_numpy(), t["vt"].to_numpy()
            n = len(dep)
            inr = (dep == d) & pc.and_(pc.greater_equal(pth, lo), pc.less(pth, hi)).to_numpy(zero_copy_only=False)
            ch = changes_in(vf, vf, vt, vt, d1, d2)
            in_rect += int(inr.sum())
            needed += int((inr & ch).sum())
            idx = np.flatnonzero(inr)
            if len(idx):
                i0, i1 = int(idx[0]), int(idx[-1]) + 1
            else:  # where the rect would sit
                less = (dep < d) | ((dep == d) & pc.less(pth, lo).to_numpy(zero_copy_only=False))
                i0 = i1 = int(less.sum())
            for p in self.pages:
                if i1 > i0:
                    pages = range(i0 // p, (i1 - 1) // p + 1)
                elif 0 < i0 < n and (i0 - 1) // p == i0 // p:
                    pages = range(i0 // p, i0 // p + 1)  # one page straddles the empty rect
                else:
                    pages = range(0)
                for pg in pages:
                    s0, e0 = pg * p, min(n, pg * p + p)
                    if changes_in(vf[s0:e0].min(), vf[s0:e0].max(), vt[s0:e0].min(), vt[s0:e0].max(), d1, d2):
                        spans[p].append((row_start + s0, row_start + e0))
        out["in_rect"], out["needed"] = in_rect, needed
        out["pages"] = {
            str(p): {"pages": len(sp), "rows": sum(e - s for s, e in sp), "runs": plan_runs(sp, self.gap_rows, self.cap_rows), "adj_runs": adjacent_runs(sp)}
            for p, sp in spans.items()
        }
        if self.layouts:
            out.update(self.simulate_layouts(rg_all, d, lo, hi, d1, d2))
        return out

    def rect_versions(self, rg_all: pd.DataFrame, d: int, lo: str, hi: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """The rect's versions from every rect group: `(path rank, i, j, open)`, i / j their first / last live scan
        index in `scans.json` (j = −1 while open)."""
        parts = []
        for rg in rg_all.rg.tolist():
            t = pq.read_table(self.cache_file(rg))
            t = t.filter(pc.and_(pc.equal(t["depth"], d), pc.and_(pc.greater_equal(t["path"], lo), pc.less(t["path"], hi))))
            if t.num_rows:
                parts.append(t)
        if not parts:
            z = np.zeros(0, dtype=np.int64)
            return z, z, z, z.astype(bool)
        t = pa.concat_tables(parts)
        _, rank = np.unique(t["path"].to_numpy(zero_copy_only=False), return_inverse=True)
        vf, vt = t["vf"].to_numpy(), t["vt"].to_numpy()
        st, last = self.stamps, len(self.stamps) - 1
        i = np.searchsorted(st, vf)
        if not ((i <= last) & (st[np.minimum(i, last)] == vf)).all():
            raise ValueError("a version's `vf` is not a scan in the generation's scans.json")
        is_open = vt == OPEN
        j = np.where(is_open, -1, np.searchsorted(st, vt) - 1)
        if not (is_open | ((j + 1 <= last) & (st[np.clip(j + 1, 0, last)] == vt))).all():
            raise ValueError("a closed version's `vt` is not a scan in the generation's scans.json")
        return rank.astype(np.int64), i.astype(np.int64), j.astype(np.int64), is_open

    def layout_modes(self, rank, i, j, is_open, ext: np.ndarray, d1: int, d2: int, kmax: int) -> dict:
        """Rows decoded, runs and bands hit per band count and per chunked K0, for versions at scan indices (i, j) of
        the stamps `ext`, over (d1, d2]."""
        vt = np.where(is_open, OPEN, ext[np.clip(j + 1, 0, len(ext) - 1)])
        vf = ext[i]
        k = np.where(is_open, 0, levels(i, np.where(is_open, i, j)))
        out = {}
        if self.bands:
            out["bands"] = {str(B): self.paged(band_of(is_open, k, B, kmax), rank, vf, vt, d1, d2) for B in self.bands}
        if self.chunks:
            out["chunks"] = {str(k0): self.paged(chunk_of(is_open, i, k, k0), rank, vf, vt, d1, d2) for k0 in self.chunks}
        return out

    def paged(self, band: np.ndarray, rank: np.ndarray, vf: np.ndarray, vt: np.ndarray, d1: int, d2: int) -> dict:
        """Each band's rows in `(path, vf)` order, cut in `band_page`-row pages; the pages whose `vf`/`vt` bounds can
        hold a change in (d1, d2] are read, merged into runs within a band (bands never merge)."""
        if not len(band):
            return {"rows": 0, "runs": 0, "bands_hit": 0}
        o = np.lexsort((vf, rank, band))
        band, vf, vt = band[o], vf[o], vt[o]
        cuts = (np.flatnonzero(np.diff(band)) + 1).tolist()
        rows = runs = hit = 0
        for b0, b1 in zip([0, *cuts], [*cuts, len(band)]):
            f, t = vf[b0:b1], vt[b0:b1]  # this band's rows: page bounds over its own pages only
            starts = np.arange(0, b1 - b0, self.band_page)
            ends = np.minimum(starts + self.band_page, b1 - b0)
            ok = changes_in(
                np.minimum.reduceat(f, starts), np.maximum.reduceat(f, starts),
                np.minimum.reduceat(t, starts), np.maximum.reduceat(t, starts), d1, d2,
            )
            sp = [(int(s), int(e)) for s, e in zip(starts[ok], ends[ok])]
            if sp:
                hit += 1
                rows += sum(e - s for s, e in sp)
                runs += plan_runs(sp, self.gap_rows, self.cap_rows)
        return {"rows": rows, "runs": runs, "bands_hit": hit}

    def simulate_layouts(self, rg_all: pd.DataFrame, d: int, lo: str, hi: str, d1: int, d2: int) -> dict:
        """The band / chunked layouts over the rect's versions, and with `history` R > 1 the same in a store R× as
        old, the request shifted into its last copy (`history`)."""
        rank, i, j, is_open = self.rect_versions(rg_all, d, lo, hi)
        out = {"rect_rows": len(rank), **self.layout_modes(rank, i, j, is_open, self.stamps, d1, d2, self.kmax())}
        R = self.history
        if R > 1:
            n = len(self.stamps)
            ext = extended(self.stamps, R)
            I, J, O = replicate(i, j, is_open, n, R)
            sh = (R - 1) * n
            e1, e2 = int(ext[np.searchsorted(self.stamps, d1) + sh]), int(ext[np.searchsorted(self.stamps, d2) + sh])
            vf, vt = ext[I], np.where(O, OPEN, ext[np.clip(J + 1, 0, len(ext) - 1)])
            out["history"] = {
                "R": R, "rect_rows": len(I), "needed": int(changes_in(vf, vf, vt, vt, e1, e2).sum()),
                **self.layout_modes(np.tile(rank, R), I, J, O, ext, e1, e2, self.kmax(R)),
            }
        return out


_SIM: Sim | None = None


def _init(sim: Sim) -> None:
    global _SIM
    _SIM = sim


def _simulate(q: dict) -> dict:
    return _SIM.simulate(q)


def fetch(store, key: str, rgs: list[int], sim: Sim, threads: int) -> None:
    """Cache each of `rgs` not cached yet (only `COLS`), `threads` at a time, over ONE footer read."""
    todo = [g for g in rgs if not os.path.exists(sim.cache_file(g))]
    if not todo:
        return
    t0 = time.monotonic()
    with store.open(key) as f:
        meta = pq.ParquetFile(f).metadata
    err(f"footer: {meta.num_row_groups} groups in {time.monotonic() - t0:.1f}s; fetching {len(todo)}")

    def one(g: int) -> None:
        with store.open(key) as f:
            t = pq.ParquetFile(f, metadata=meta).read_row_group(g, columns=COLS)
        tmp = sim.cache_file(g) + ".tmp"
        pq.write_table(t, tmp)
        os.replace(tmp, sim.cache_file(g))

    done = 0
    with ThreadPoolExecutor(threads) as ex:
        for _ in ex.map(one, todo):
            done += 1
            if done % 1000 == 0:
                err(f"  fetched {done}/{len(todo)} in {time.monotonic() - t0:.0f}s")
    err(f"fetched {len(todo)} in {time.monotonic() - t0:.0f}s")


def pct(xs: list[int]) -> list[int]:
    """p50 / p90 / p99 / max, each an observed value (nearest rank)."""
    if not xs:
        return [0, 0, 0, 0]
    a = np.asarray(xs)
    return [int(np.percentile(a, q, method="inverted_cdf")) for q in (50, 90, 99)] + [int(a.max())]


def layouts(r: dict, suffix: str = "") -> list[tuple[str, dict]]:
    return [*((f"B={b}{suffix}", v) for b, v in r.get("bands", {}).items()), *((f"k={k}{suffix}", v) for k, v in r.get("chunks", {}).items())]


def modes(r: dict) -> list[tuple[str, dict]]:
    """A result's modes, labelled: `groups`, `p<N>` per page size, `B=<N>` per band count, `k=<K0>` per chunked K0, and
    with history the layouts again as `B=<N>@R<R>` / `k=<K0>@R<R>`."""
    h = r.get("history")
    return [("groups", r["groups"]), *((f"p{p}", v) for p, v in r["pages"].items()), *layouts(r), *(layouts(h, f"@R{h['R']}") if h else [])]


def summary(results: list[dict]) -> list[str]:
    """Per mode, p50 / p90 / p99 / max over requests of rows decoded, runs, and GET rounds at each of `WAVES`."""
    if not results:
        return ["0 requests"]
    lines = [f"{len(results)} requests: p50 / p90 / p99 / max", f"{'needed':<9} rows {pct([r['needed'] for r in results])}"]
    if h := results[0].get("history"):
        lines.append(f"{'needed@R' + str(h['R']):<9} rows {pct([r['history']['needed'] for r in results])}")
    for label, _ in modes(results[0]):
        ms = [dict(modes(r))[label] for r in results]
        line = f"{label:<9} rows {pct([m['rows'] for m in ms])} runs {pct([m['runs'] for m in ms])}"
        for w in WAVES:
            line += f" rounds×{w} {pct([w * ceil(m['runs'] / RUN_READS) for m in ms])}"
        lines.append(line)
    return lines


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")


@command("layout-sim")
@option("-b", "--bands", default="1,2,4,8", help="Band counts to simulate (`(band, depth, path, vf)` layouts); `none` skips the band mode")
@option("-c", "--cache", default="tmp/layout-sim", help="Local cache of fetched row groups (`depth, path, vf, vt` only), under `<cache>/<generation>/<sort>/`")
@option("-g", "--run-gap", default=RUN_GAP, type=IntRange(min=0), help="Bytes between spans still merged into one read (default the site's `RUN_GAP`)")
@option("-H", "--history", default=1, type=IntRange(min=1), help="Also simulate the band and chunked layouts in a store this many times as old (R copies of the scans end to end, requests in the last)")
@option("-j", "--procs", default=os.cpu_count(), type=IntRange(min=1), help="Simulation processes (1: in-process)")
@option("-k", "--chunks", default="none", help="Chunked dyadic layouts to simulate, by K0 (levels below K0 grouped in 2^K0-scan chunks), e.g. `2,3,4,5`")
@option("-o", "--out", help="Output JSONL (default stdout): each request with its modes' rows decoded and runs")
@option("-p", "--pages", default="1024,256,128", help="Page sizes (rows) to simulate within today's groups; `none` skips the page mode")
@option("-P", "--band-page", default=BAND_PAGE, type=IntRange(min=1), help="The band layout's page (rows)")
@option("-s", "--sort", default="path", help="The served sort (one led by `(depth, path)`)")
@option("-t", "--threads", default=32, type=IntRange(min=1), help="Row-group fetches in flight")
@argument("gen")
@argument("requests")
def layout_sim_cmd(bands, cache, run_gap, history, procs, chunks, out, pages, band_page, sort, threads, gen, requests) -> None:
    """Simulate `/api/diff/level` reads of generation GEN's served SORT (a dir or URL: `gs://…/interval-store/<gen>`,
    `s3://…`, `r2://…`) under today's groups, page sizes, band and chunked dyadic layouts (and the latter in an older store). REQUESTS: JSONL of `{"path", "from", "to"}`
    (scan ids in GEN's `scans.json`); other keys are copied to the output. A summary goes to stderr."""
    from .blobstore import Store
    from .interval_store import RANGE_LED, SORTS

    if sort not in SORTS or not SORTS[sort][1].startswith(RANGE_LED):
        raise SystemExit(f"{sort}: not a served sort led by `{RANGE_LED}` ({', '.join(s for s, v in SORTS.items() if v[1].startswith(RANGE_LED))})")
    store = Store(gen)
    with store.open(f"served/{sort}.groups.parquet") as f:
        side = pq.read_table(f, columns=SIDECAR_COLS).to_pandas()
    ts = {s["id"]: int(s["ts"]) for s in store.read_json("scans.json")["scans"]}
    meta = store.read_json(f"served/{sort}.json")
    cache_dir = Path(cache) / slug(gen) / sort
    cache_dir.mkdir(parents=True, exist_ok=True)
    sim = Sim(side, ts, str(cache_dir), parse_ints(pages), parse_ints(bands), band_page, meta["bytes"] / meta["rows"], run_gap,
              parse_ints(chunks), history)
    reqs = [json.loads(line) for line in open(requests) if line.strip()]
    if bad := sorted({s for q in reqs for s in (q["from"], q["to"]) if s not in ts}):
        raise SystemExit(f"scans not in {gen}'s scans.json: {', '.join(bad)}")
    uniq = list({(q["path"], q["from"], q["to"]): q for q in reqs}.values())
    rgs = sorted({g for q in uniq for g in sim.groups_to_read(q)})
    err(f"{len(reqs)} requests ({len(uniq)} distinct), {len(rgs)} groups to read; KMAX {sim.kmax()}, "
        f"{sim.row_bytes:.1f} bytes/row: RUN_GAP {sim.gap_rows} rows, RUN_BYTES {sim.cap_rows} rows")
    fetch(store, f"served/{sort}.parquet", rgs, sim, threads)
    t0 = time.monotonic()
    if procs == 1:
        got = [sim.simulate(q) for q in uniq]
    else:
        with ProcessPoolExecutor(procs, initializer=_init, initargs=(sim,)) as ex:
            got = list(ex.map(_simulate, uniq, chunksize=4))
    err(f"simulated {len(uniq)} in {time.monotonic() - t0:.0f}s")
    by_key = {(r["path"], r["from"], r["to"]): r for r in got}
    results = [{**by_key[(q["path"], q["from"], q["to"])], **q} for q in reqs]
    lines = "".join(json.dumps(r) + "\n" for r in results)
    if out:
        Path(out).write_text(lines)
    else:
        sys.stdout.write(lines)
    for line in summary(results):
        err(line)
