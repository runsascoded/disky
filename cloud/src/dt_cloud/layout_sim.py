"""Offline: what `/api/diff/level` reads from a served sort (specs/diff-progressive.md §3b), under candidate layouts,
from a generation's own groups sidecars and row groups. Modes per request (dir P, from scan D1, to scan D2):

- `groups` (today): the base's row groups `planChanged` keeps (P's children's rect `(dP+1, [P/, P0))`, and `vf`/`vt`
  bounds that can hold a version opening or closing in (D1, D2]), each decoded whole.
- pages of p rows (2b.1, `-p`): within each kept group, the p-row pages a page index keeps: those overlapping the rect's
  contiguous `(depth, path)` range (or straddling where it would sit, when it is empty) whose own `vf`/`vt` bounds pass
  the same test. Pages never cross a group.
- B bands (§3b "Next" 3, `-b`): the base re-laid `(band, depth, path, vf)`, each version in a band by its dyadic
  segment's level (the open versions band 0, closed levels 0..KMAX split evenly over bands 1..B-1; B = 1 is one band),
  groups free to cross segments, cut in `-P`-row pages kept by their `vf`/`vt` bounds. A band's pages are computed over
  the rect's own rows in band order, so its edge pages' neighbour rows are ignored (≤ 2 pages a band).
- `level` (`-l`): a dedicated sort for the level query, one row per child version TRANSITION keyed
  `(parent depth, parent path, b, child)`, b the transition's scan stamp: each version's `vf` (it opened), and the `vt`
  of a closed version no later version of the same child starts at (it was deleted). One such file per store file: the
  base and each run of the newest manifest (`manifests/`), a transition in the file holding its scan (`-f N`: the
  generation's scans split instead into N contiguous files of about equal scan counts). A request reads, in each file
  holding a scan in (D1, D2], P's transitions with b in it: one contiguous key range, so one run a file touched.
  Transitions come from P's children's versions in the base and every run (`path` sort rows, every segment; a version's
  rows across files are one version, the smallest `vt` winning, as the reader combines them). Rows decoded are an upper
  bound: each file's range at the worst page alignment (⌈(c + p − 1) / p⌉ pages of p rows; a file with none, 1 page).
  `-C` adds the store's cost: every path's transitions over the whole generation, and bytes estimated from the served
  `path` sort's own per-column bytes (`level_cost`).

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
#: The served `path` sort's columns a `level` row carries, for its bytes estimate: the child's name (`path`), the parent
#: key's depth, b (as `vf`), the size and object deltas (as `size`, `n_files`), `churn_cum`, `churn_end`. The parent's
#: path isn't counted: it repeats down each parent's sorted run of rows, which RLE / dictionary pages store about once.
LEVEL_COLS = ["path", "depth", "vf", "size", "n_files", "churn_cum", "churn_end"]
#: The base's files key prefix (a run's is its `deltas/<…>` key).
BASE = ""


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


def max_level(segs) -> int:
    """KMAX: the highest dyadic level among a sort's closed segments (0 when it has none)."""
    closed = [int(s) for s in segs if s > 0]
    return max(((s - 1) // SEG_LEVEL for s in closed), default=0)


def band_of(seg: np.ndarray, B: int, kmax: int) -> np.ndarray:
    """Each version's band: B = 1 one band; else the open versions band 0 and closed levels 0..KMAX split evenly over
    bands 1..B-1."""
    if B == 1:
        return np.zeros_like(seg)
    k = np.where(seg > 0, (seg - 1) // SEG_LEVEL, -1)
    return np.where(seg == 0, 0, 1 + (k * (B - 1)) // (kmax + 1))


def transitions(path: np.ndarray, vf: np.ndarray, vt: np.ndarray) -> np.ndarray:
    """The level sort's b's for version rows `(path, vf, vt)` (one version's rows from several files are one version, its
    smallest `vt`): every version's `vf`, and each closed version's `vt` when no version of its path starts there."""
    df = pd.DataFrame({"path": path, "vf": vf, "vt": vt}).groupby(["path", "vf"], sort=False, as_index=False).vt.min()
    starts = pd.MultiIndex.from_arrays([df.path, df.vf])
    closed = df[df.vt != OPEN]
    gone = ~pd.MultiIndex.from_arrays([closed.path, closed.vt]).isin(starts)
    return np.concatenate([df.vf.to_numpy(), closed.vt.to_numpy()[gone]])


def level_pages(c: int, p: int) -> int:
    """The pages of p rows a contiguous range of c rows decodes at the worst alignment (an empty one: the page it falls
    in)."""
    return ceil((c + p - 1) / p) if c else 1


def file_model(order: list[str], files: list[list[str]], n: int | None) -> list[list[str]]:
    """The level sort's files, each its scan ids: the store's own (`files`: the base's, then each run's), or with `n` the
    scans (`order`) split into n contiguous files of about equal scan counts."""
    return files if n is None else [list(a) for a in np.array_split(np.array(order, dtype=object), n) if len(a)]


@dataclass
class Sim:
    """One generation's sort, as the simulation needs it: its groups sidecars (the base's, and with `level` each run's),
    scan epochs, the modes, and where its fetched row groups are cached. Pickled once into each worker process."""
    side: pd.DataFrame
    ts: dict[str, int]
    cache: str
    pages: list[int]
    bands: list[int]
    band_page: int
    row_bytes: float
    run_gap: int = RUN_GAP
    #: The level mode's inputs: the runs' sidecars by key, and its files (each its scan ids); empty: no level mode.
    runs: dict[str, pd.DataFrame] = field(default_factory=dict)
    files: list[list[str]] = field(default_factory=list)

    def __post_init__(self):
        self.kmax = max_level(self.side.seg)
        self.gap_rows = int(self.run_gap / self.row_bytes)
        self.cap_rows = int(RUN_BYTES / self.row_bytes)
        self.file_of = {self.ts[s]: f for f, scans in enumerate(self.files) for s in scans}

    @property
    def level(self) -> bool:
        return bool(self.files)

    def sources(self) -> dict[str, pd.DataFrame]:
        return {BASE: self.side, **self.runs}

    def cache_file(self, src: str, rg: int) -> str:
        return f"{self.cache}/{slug(src) or 'base'}/rg-{rg}.parquet"

    @staticmethod
    def rect_groups(side: pd.DataFrame, P: str) -> pd.DataFrame:
        """The sidecar rows whose `(depth, path)` range meets P's rect, as composite keys."""
        d, lo, hi = rect(P)
        s = side[(side.d_min <= d) & (side.d_max >= d)]
        return s[((s.d_max > d) | (s.p_max >= lo)) & ((s.d_min < d) | (s.p_min < hi))]

    def groups_to_read(self, q: dict) -> list[tuple[str, int]]:
        """The row groups request `q` reads, as `(source, rg)`: the base's kept groups; every base rect group when bands
        are simulated; every rect group of every file for the level mode."""
        d1, d2 = self.ts[q["from"]], self.ts[q["to"]]
        s = self.rect_groups(self.side, q["path"])
        if not self.bands:
            s = s[changes_in(s.vf_min, s.vf_max, s.vt_min, s.vt_max, d1, d2)]
        out = [(BASE, g) for g in s.rg.tolist()]
        if self.level:
            out += [(k, g) for k, side in self.sources().items() for g in self.rect_groups(side, q["path"]).rg.tolist()]
        return out

    def rect_rows(self, src: str, rgs: list[int], d: int, lo: str, hi: str) -> list[pa.Table]:
        parts = []
        for rg in rgs:
            t = pq.read_table(self.cache_file(src, rg))
            t = t.filter(pc.and_(pc.equal(t["depth"], d), pc.and_(pc.greater_equal(t["path"], lo), pc.less(t["path"], hi))))
            if t.num_rows:
                parts.append(t)
        return parts

    def simulate(self, q: dict) -> dict:
        P, d1, d2 = q["path"], self.ts[q["from"]], self.ts[q["to"]]
        d, lo, hi = rect(P)
        rg_all = self.rect_groups(self.side, P)
        kept = rg_all[changes_in(rg_all.vf_min, rg_all.vf_max, rg_all.vt_min, rg_all.vt_max, d1, d2)]
        out = {**q, "rect_groups": len(rg_all), "kept_groups": len(kept), "group_rows": int((kept.row_end - kept.row_start).sum())}
        g_spans = list(zip(kept.row_start.tolist(), kept.row_end.tolist()))
        out["groups"] = {"rows": out["group_rows"], "runs": plan_runs(g_spans, self.gap_rows, self.cap_rows), "adj_runs": adjacent_runs(g_spans)}
        spans: dict[int, list[tuple[int, int]]] = {p: [] for p in self.pages}
        in_rect = needed = 0
        for rg, row_start in zip(kept.rg.tolist(), kept.row_start.tolist()):
            t = pq.read_table(self.cache_file(BASE, rg))
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
        if self.bands:
            out.update(self.simulate_bands(rg_all, d, lo, hi, d1, d2))
        if self.level:
            out["level"] = self.simulate_level(P, d, lo, hi, d1, d2)
        return out

    def simulate_bands(self, rg_all: pd.DataFrame, d: int, lo: str, hi: str, d1: int, d2: int) -> dict:
        parts = []
        for rg, seg in zip(rg_all.rg.tolist(), rg_all.seg.tolist()):
            for t in self.rect_rows(BASE, [rg], d, lo, hi):
                parts.append(t.append_column("seg", pa.array(np.full(t.num_rows, seg, dtype=np.int64))))
        empty = {"rows": 0, "runs": 0, "bands_hit": 0}
        if not parts:
            return {"rect_rows": 0, "bands": {str(B): dict(empty) for B in self.bands}}
        t = pa.concat_tables(parts)
        t = t.take(pc.sort_indices(t, sort_keys=[("path", "ascending"), ("vf", "ascending")]))
        vf, vt, seg = t["vf"].to_numpy(), t["vt"].to_numpy(), t["seg"].to_numpy()
        bp = self.band_page
        out = {}
        for B in self.bands:
            band = band_of(seg, B, self.kmax)
            rows = runs = hit = 0
            for b in np.unique(band):
                ix = np.flatnonzero(band == b)  # this band's rect rows, in (path, vf) order
                sp = []
                for s0 in range(0, len(ix), bp):
                    s = ix[s0:s0 + bp]
                    if changes_in(vf[s].min(), vf[s].max(), vt[s].min(), vt[s].max(), d1, d2):
                        sp.append((s0, s0 + len(s)))
                if sp:
                    hit += 1
                    rows += sum(e - s for s, e in sp)
                    runs += plan_runs(sp, self.gap_rows, self.cap_rows)
            out[str(B)] = {"rows": rows, "runs": runs, "bands_hit": hit}
        return {"rect_rows": int(t.num_rows), "bands": out}

    def simulate_level(self, P: str, d: int, lo: str, hi: str, d1: int, d2: int) -> dict:
        """The level sort's read for (P, D1, D2]: P's transitions per file in range, the files touched, and per page
        size the rows decoded (worst alignment) and runs (one a file touched)."""
        parts = [t for k, side in self.sources().items() for t in self.rect_rows(k, self.rect_groups(side, P).rg.tolist(), d, lo, hi)]
        if parts:
            t = pa.concat_tables(parts)
            b = transitions(t["path"].to_numpy(zero_copy_only=False), t["vf"].to_numpy(), t["vt"].to_numpy())
        else:
            b = np.zeros(0, dtype=np.int64)
        if unknown := sorted({int(x) for x in b} - set(self.file_of)):
            raise ValueError(f"{P}: transitions at {unknown[:3]}…, not scans of the generation")
        touched = sorted({f for s, f in self.file_of.items() if d1 < s <= d2})
        per = {f: 0 for f in touched}
        for x in b[(b > d1) & (b <= d2)].tolist():
            per[self.file_of[x]] += 1
        return {
            "transitions": len(b), "needed": sum(per.values()), "files": len(touched), "files_total": len(self.files),
            "pages": {str(p): {"rows": sum(level_pages(c, p) * p for c in per.values()), "runs": len(touched)} for p in self.pages},
        }


_SIM: Sim | None = None


def _init(sim: Sim) -> None:
    global _SIM
    _SIM = sim


def _simulate(q: dict) -> dict:
    return _SIM.simulate(q)


def served_key(src: str, sort: str, ext: str = "parquet") -> str:
    return f"{src}/served/{sort}.{ext}" if src else f"served/{sort}.{ext}"


def fetch(store, sort: str, todo_all: list[tuple[str, int]], sim: Sim, threads: int) -> None:
    """Cache each of `todo_all` (`(source, rg)`) not cached yet (only `COLS`), `threads` at a time, over ONE footer read a
    source file."""
    todo = [(s, g) for s, g in todo_all if not os.path.exists(sim.cache_file(s, g))]
    if not todo:
        return
    t0 = time.monotonic()
    metas = {}
    for src in sorted({s for s, _ in todo}):
        with store.open(served_key(src, sort)) as f:
            metas[src] = pq.ParquetFile(f).metadata
        os.makedirs(os.path.dirname(sim.cache_file(src, 0)), exist_ok=True)
    err(f"footers: {len(metas)} files in {time.monotonic() - t0:.1f}s; fetching {len(todo)} groups")

    def one(sg: tuple[str, int]) -> None:
        src, g = sg
        with store.open(served_key(src, sort)) as f:
            t = pq.ParquetFile(f, metadata=metas[src]).read_row_group(g, columns=COLS)
        tmp = sim.cache_file(src, g) + ".tmp"
        pq.write_table(t, tmp)
        os.replace(tmp, sim.cache_file(src, g))

    done = 0
    with ThreadPoolExecutor(threads) as ex:
        for _ in ex.map(one, todo):
            done += 1
            if done % 1000 == 0:
                err(f"  fetched {done}/{len(todo)} in {time.monotonic() - t0:.0f}s")
    err(f"fetched {len(todo)} in {time.monotonic() - t0:.0f}s")


def level_cost(store, sort: str, srcs: list[str], threads: int) -> dict:
    """The level sort's size over the whole generation: every path's versions (rows of one version across the base and
    runs counted once), its deletions, their sum (the transitions), and bytes: transitions × the served base sort's
    bytes a row of `LEVEL_COLS` (each column's compressed chunk bytes over its rows, from the footer)."""
    hs, vfs, vts = [], [], []

    def one(src_g: tuple[str, int, object]) -> tuple:
        src, g, meta = src_g
        with store.open(served_key(src, sort)) as f:
            t = pq.ParquetFile(f, metadata=meta).read_row_group(g, columns=["path", "vf", "vt"])
        h = pd.util.hash_array(t["path"].to_numpy(zero_copy_only=False))
        return h, t["vf"].to_numpy(), t["vt"].to_numpy()

    jobs = []
    base_meta = None
    for src in srcs:
        with store.open(served_key(src, sort)) as f:
            meta = pq.ParquetFile(f).metadata
        base_meta = base_meta or meta
        jobs += [(src, g, meta) for g in range(meta.num_row_groups)]
    with ThreadPoolExecutor(threads) as ex:
        for h, vf, vt in ex.map(one, jobs):
            hs.append(h)
            vfs.append(vf)
            vts.append(vt)
    h = np.concatenate(hs).astype(np.uint64)
    vf, vt = np.concatenate(vfs).astype(np.uint64), np.concatenate(vts)
    mix = np.uint64(0x9E3779B97F4A7C15)
    key = h ^ (vf * mix)
    o = np.argsort(key, kind="stable")
    key, h, vt = key[o], h[o], vt[o]
    first = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    vkey, vh, vvt = key[first], h[first], np.minimum.reduceat(vt, first)
    closed = vvt != OPEN
    gone = ~np.isin(vh[closed] ^ (vvt[closed].astype(np.uint64) * mix), vkey)
    versions, deletions = len(vkey), int(gone.sum())
    names = [base_meta.schema.column(i).name for i in range(base_meta.num_columns)]
    rows = base_meta.num_rows
    col_bytes = {c: sum(base_meta.row_group(g).column(names.index(c)).total_compressed_size for g in range(base_meta.num_row_groups))
                 for c in LEVEL_COLS if c in names}
    bpr = sum(col_bytes.values()) / rows if rows else 0.0
    return {
        "versions": versions, "deletions": deletions, "transitions": versions + deletions, "bytes_per_row": round(bpr, 2),
        "bytes": round((versions + deletions) * bpr),
        "basis": f"base `{sort}` sort's compressed bytes/row of {', '.join(col_bytes)} (parent path excluded: run-length "
                 f"within each parent's rows); {rows} base rows",
    }


def pct(xs: list[int]) -> list[int]:
    """p50 / p90 / p99 / max, each an observed value (nearest rank)."""
    if not xs:
        return [0, 0, 0, 0]
    a = np.asarray(xs)
    return [int(np.percentile(a, q, method="inverted_cdf")) for q in (50, 90, 99)] + [int(a.max())]


def modes(r: dict) -> list[tuple[str, dict]]:
    """A result's modes, labelled: `groups`, `p<N>` per page size, `B=<N>` per band count, `L<N>` per level page size."""
    return [
        ("groups", r["groups"]),
        *((f"p{p}", v) for p, v in r["pages"].items()),
        *((f"B={b}", v) for b, v in r.get("bands", {}).items()),
        *((f"L{p}", v) for p, v in r.get("level", {}).get("pages", {}).items()),
    ]


def summary(results: list[dict]) -> list[str]:
    """Per mode, p50 / p90 / p99 / max over requests of rows decoded, runs, and GET rounds at each of `WAVES`."""
    if not results:
        return ["0 requests"]
    lines = [f"{len(results)} requests: p50 / p90 / p99 / max", f"{'needed':<8} rows {pct([r['needed'] for r in results])}"]
    if "level" in results[0]:
        lines.append(f"{'needed L':<8} rows {pct([r['level']['needed'] for r in results])} files {pct([r['level']['files'] for r in results])}")
    for label, _ in modes(results[0]):
        ms = [dict(modes(r))[label] for r in results]
        line = f"{label:<8} rows {pct([m['rows'] for m in ms])} runs {pct([m['runs'] for m in ms])}"
        for w in WAVES:
            line += f" rounds×{w} {pct([w * ceil(m['runs'] / RUN_READS) for m in ms])}"
        lines.append(line)
    return lines


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")


@command("layout-sim")
@option("-b", "--bands", default="1,2,4,8", help="Band counts to simulate (`(band, depth, path, vf)` layouts); `none` skips the band mode")
@option("-c", "--cache", default="tmp/layout-sim", help="Local cache of fetched row groups (`depth, path, vf, vt` only), under `<cache>/<generation>/<sort>/<file>/`")
@option("-C", "--cost", is_flag=True, help="With -l: the level sort's size over the whole generation (reads every row group's `path, vf, vt` once)")
@option("-f", "--files", type=IntRange(min=1), help="With -l: model the level sort as this many files (the scans split evenly), not the store's base + runs")
@option("-g", "--run-gap", default=RUN_GAP, type=IntRange(min=0), help="Bytes between spans still merged into one read (default the site's `RUN_GAP`)")
@option("-j", "--procs", default=os.cpu_count(), type=IntRange(min=1), help="Simulation processes (1: in-process)")
@option("-l", "--level", is_flag=True, help="Simulate the level sort `(parent depth, parent path, b, child)` over the base and the newest manifest's runs (at each -p page size)")
@option("-o", "--out", help="Output JSONL (default stdout): each request with its modes' rows decoded and runs")
@option("-p", "--pages", default="1024,256,128", help="Page sizes (rows) to simulate within today's groups (and the level sort's); `none` skips the page mode")
@option("-P", "--band-page", default=BAND_PAGE, type=IntRange(min=1), help="The band layout's page (rows)")
@option("-s", "--sort", default="path", help="The served sort (one led by `(depth, path)`)")
@option("-t", "--threads", default=32, type=IntRange(min=1), help="Row-group fetches in flight")
@argument("gen")
@argument("requests")
def layout_sim_cmd(bands, cache, cost, files, run_gap, procs, level, out, pages, band_page, sort, threads, gen, requests) -> None:
    """Simulate `/api/diff/level` reads of generation GEN's served SORT (a dir or URL: `gs://…/interval-store/<gen>`,
    `s3://…`, `r2://…`) under today's groups, page sizes, band layouts and a dedicated level sort. REQUESTS: JSONL of
    `{"path", "from", "to"}` (scan ids of GEN: its `scans.json`'s, and with -l its runs'); other keys are copied to the
    output. A summary goes to stderr."""
    from .append_runner import latest_key
    from .blobstore import Store
    from .interval_store import RANGE_LED, SORTS

    if sort not in SORTS or not SORTS[sort][1].startswith(RANGE_LED):
        raise SystemExit(f"{sort}: not a served sort led by `{RANGE_LED}` ({', '.join(s for s, v in SORTS.items() if v[1].startswith(RANGE_LED))})")
    pages_l = parse_ints(pages)
    if level and not pages_l:
        raise SystemExit("-l needs -p page sizes")
    if (cost or files) and not level:
        raise SystemExit("-C / -f need -l")
    store = Store(gen)

    def sidecar(src: str) -> pd.DataFrame:
        with store.open(served_key(src, sort, "groups.parquet")) as f:
            return pq.read_table(f, columns=SIDECAR_COLS).to_pandas()

    scans = store.read_json("scans.json")["scans"]
    ts = {s["id"]: int(s["ts"]) for s in scans}
    order = [s["id"] for s in scans]
    runs: dict[str, pd.DataFrame] = {}
    file_scans = [list(order)]
    if level:
        manifest = latest_key(store.listing("manifests"))
        for r in store.read_json(manifest)["runs"] if manifest else []:
            runs[r["key"]] = sidecar(r["key"])
            file_scans.append(list(r["scans"]))
            order += r["scans"]
        if manifest:
            ts.update({k: int(v) for k, v in store.read_json(manifest)["stamps"].items()})
        err(f"level: base + {len(runs)} runs ({manifest or 'no manifest'}); {len(file_model(order, file_scans, files))} files modelled")
    meta = store.read_json(served_key(BASE, sort, "json"))
    cache_dir = Path(cache) / slug(gen) / sort
    cache_dir.mkdir(parents=True, exist_ok=True)
    sim = Sim(sidecar(BASE), ts, str(cache_dir), pages_l, parse_ints(bands), band_page, meta["bytes"] / meta["rows"], run_gap,
              runs, file_model(order, file_scans, files) if level else [])
    reqs = [json.loads(line) for line in open(requests) if line.strip()]
    if bad := sorted({s for q in reqs for s in (q["from"], q["to"]) if s not in ts}):
        raise SystemExit(f"scans not in {gen}: {', '.join(bad)}")
    uniq = list({(q["path"], q["from"], q["to"]): q for q in reqs}.values())
    todo = sorted({g for q in uniq for g in sim.groups_to_read(q)})
    err(f"{len(reqs)} requests ({len(uniq)} distinct), {len(todo)} groups to read; KMAX {sim.kmax}, "
        f"{sim.row_bytes:.1f} bytes/row: RUN_GAP {sim.gap_rows} rows, RUN_BYTES {sim.cap_rows} rows")
    fetch(store, sort, todo, sim, threads)
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
    if cost:
        err(f"level sort cost: {json.dumps(level_cost(store, sort, [BASE, *runs], threads))}")
