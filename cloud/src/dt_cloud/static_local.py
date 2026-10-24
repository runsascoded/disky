"""The static name index on one machine (`dt-cloud static-names build` / `add`): every stage of the staged (Batch) path,
in-process and in order, over a local work dir, for deployments small enough that one machine builds a generation in
minutes. The stages are the staged path's own functions (`static_names`, `static_catalog`, `static_roots`, `static_drill`,
`static_append`, `static_merge`); this module only chains them, and moves files between the store and the work dir.

- **Store** (`-o`): a local dir or an fsspec URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`), the root the readers'
  `INDEX_R2` keys are relative to: a generation lives under `static-names/<gen>/`, as the staged path lays it out.
- **Scans** (`-s`, `-L`): a root (dir or URL) and layout templates of the scans' `path` sorts under it (`{id}` the scan id,
  `{gen}` the store generation; a scan's newest wins), as a deployment profile's `layouts`.

`build` writes a base generation (never over one): `scans.json`, `ranges.json`, the coalesced versions (`cintervals/`,
`chist/`), the suffix shards (`shards.json`, `sx/`, `sidecar/`, `sidecar.parquet`), the catalog (`catalog/`), the
heavy-term drilldown (`drill/`, `roots-measure/`), and anchored search (`static_anchors`: the name index `names/`, the
`q$` / `^q$` rollups and the starts-with catalog under `anchors/`, at the drill's R and K) unless `-A` — without it the
readers decline `^q`, `^q$`, a heavy `q$` and path terms (`anchor-not-indexed`).

`add` appends every scan published after the generation's newest (base + runs), oldest first, each as a run
(`deltas/<id>/`: `static_append`'s per-scan append, shards, catalog delta, the drill's run and, when the base has
`anchors/meta.json`, the run's `names/` + `anchors/`), publishes its manifest
(`manifests/<id>.json`, written last), then the binary counter's due carries (`static_merge`; revisions
`manifests/<id>.m<NNN>.json`; a merged run gets `names/` + `anchors/` when its inputs carry them). An anchorless
generation (built with `-A`, or before anchors were built here) keeps appending without them: a rebuild into a new
generation is how it gets them. The open-version state is refolded from the base's `cintervals/` and the runs' `cdelta/`
(`static_append.rebuild_open`), so nothing beyond the published runs is kept between invocations.

With a URL store, the generation's files are mirrored into the work dir first; afterwards the new files go up, every
run's files before its liveness markers (`meta.json`s), and the manifests last, so no reader sees a manifest whose runs
are incomplete. Then the runs a carry folded are deleted from the store (`append_runner.prune_folded`: those listed by no
manifest but the newest and those superseded less than `-G` s ago, 1 h by default; a folded run keeps its `cdelta/` and `scans.json`, which later appends refold).
"""
from __future__ import annotations

import json
import os
import resource
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

import pyarrow.parquet as pq
from click import IntRange, command, option

from . import heartbeat as hb
from . import static_anchors as an
from . import static_append as sa
from . import static_catalog as sc
from . import static_drill as sd
from . import static_names as sn
from . import static_roots as sr
from .append_runner import (
    COMPACT_LEVEL, GRACE_S, MIN_GRACE_S, POLICY_HELP, LocalRunStore, PolicyType, compact_store, due_in, forced, latest_key, prune_folded,
    resolve_gen, rm_local, run_key, scans_of,
)
from .blobstore import Store, mirror
from .blobstore import upload_new as _upload_new
from .hex_runs import parse_rule, rule_json
from .static_merge import FOLDED_RETAIN
from .static_names import PREFIX, err, q

#: Ranges, shard rows: one machine, so a few ranges (DuckDB parallelizes within each) and the staged path's shard size.
RANGES = 8
SHARD_ROWS = sa.RUN_SHARD_ROWS
#: The catalog's membership bound V (the staged path's), the drill's R and K (`static-names drill build`'s defaults),
#: and the measurement floor (`roots measure -f`).
MAX_ROWS = 100_000
R_ROWS, K_KEPT, FLOOR = 100_000, 256, 10_000
#: The drill's chunks (`-c`): suffix rows per member-loop chunk, roots per measurement / rollup `q` range.
CHUNK_ROWS = 1 << 22


# ── Resource peaks: what a job sizing needs, reported by every run ─────────


GB = 1 << 30


def rss_peak_gb() -> float:
    """This process's peak RSS so far (`ru_maxrss`: bytes on macOS, KiB on Linux)."""
    m = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(m / (GB if sys.platform == "darwin" else GB >> 10), 2)


class DiskPeak:
    """Peak bytes used on the work dir's filesystem above its level at start, sampled every `every_s` on a daemon
    thread (DuckDB's spill and the stages' intermediates land there between samples, so a stage's peak is a lower
    bound to within one interval). `take()` returns the peak since the previous `take()` (one stage) and resets it;
    `take_step()` the same within a stage (since the previous `take_step()` or `take()`), leaving the stage's peak.
    The filesystem is the job's own scratch, so other writers' bytes count too; on a shared disk this over-states."""

    def __init__(self, path: Path, every_s: float = 2.0, usage: Callable[[], int] | None = None):
        self.path, self.every_s = path, every_s
        # `usage`: the bytes used (a test's own sequence, not a patched `shutil`: other samplers' threads read that too)
        self.usage = usage or (lambda: shutil.disk_usage(path).used)
        self.base = self.usage()
        self.peak = self.total = self.step = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def sample(self) -> None:
        used = max(0, self.usage() - self.base)
        self.peak, self.total, self.step = max(self.peak, used), max(self.total, used), max(self.step, used)

    def run(self) -> None:
        while not self.stop.wait(self.every_s):
            self.sample()

    def take(self) -> float:
        self.sample()
        peak, self.peak, self.step = self.peak, 0, 0
        return round(peak / GB, 2)

    def take_step(self) -> float:
        self.sample()
        step, self.step = self.step, 0
        return round(step / GB, 2)

    def close(self) -> float:
        self.stop.set()
        self.thread.join()
        self.sample()
        return round(self.total / GB, 2)


# ── The store: a local dir or an fsspec URL (`blobstore`) ──────────────────

class GenStore:
    """A generation of `store` (`prefix`, e.g. `static-names/<gen>`) as an `append_runner.PruneTarget`: its keys relative
    to the generation."""

    def __init__(self, store: Store, prefix: str):
        self.store, self.prefix = store, prefix

    def listing(self, prefix: str) -> dict[str, int]:
        return {k[len(self.prefix) + 1:]: n for k, n in self.store.listing(f"{self.prefix}/{prefix}".rstrip("/")).items()}

    def mtimes(self, prefix: str) -> dict[str, float]:
        return {k[len(self.prefix) + 1:]: t for k, t in self.store.mtimes(f"{self.prefix}/{prefix}".rstrip("/")).items()}

    def read_json(self, key: str) -> dict:
        return self.store.read_json(f"{self.prefix}/{key}")

    def delete(self, keys: list[str]) -> None:
        self.store.delete([f"{self.prefix}/{k}" for k in keys])


def publish_order(keys: list[str]) -> list[str]:
    """The upload order: data files first, then liveness markers (`meta.json`; the drill's and `anchors/meta.json` last: a
    run is a drill tier iff its `drill/meta.json` exists, an anchored one iff its `anchors/meta.json` does, which follows
    its `anchors/start/meta.json` as on the staged path), then the manifests (each lists runs that must be whole), in key
    order within each."""
    def rank(k: str) -> tuple:
        if k.split("/", 1)[0] == "manifests":
            return (3, k)
        name = k.rsplit("/", 1)[-1]
        if name == "meta.json":
            return (2 if k.endswith(("drill/meta.json", f"{an.ANCHORS}/meta.json")) else 1, k)
        return (0, k)
    return sorted(keys, key=rank)


def publish_tier(key: str) -> int | None:
    """`blobstore.upload_new`'s tier of a key in `publish_order`: the data files (rank 0) go up together; a liveness marker,
    a manifest, and the generation's `scans.json` (what a reader opens the generation by) each go up alone, after
    everything before it in `publish_order`."""
    if key == "scans.json" or key.split("/", 1)[0] == "manifests" or key.rsplit("/", 1)[-1] == "meta.json":
        return None
    return 0


def read_gen(store: Store, read_from: str | None, prefix: str, gen_dir: Path, keep=lambda rel: True, *, label: str = "read") -> tuple[dict, dict[str, int]]:
    """The generation `prefix` of `store` mirrored into `gen_dir` (what `keep` takes), from `store` itself or (`read_from`,
    `-F`) another copy of it (refused when the copy lacks a file `store` has: `append_runner.read_tree`). Returns the
    read (`{source, files, bytes, fetched, s}`, logged) and `store`'s listing of the generation (what `upload_new` skips)."""
    from .append_runner import read_tree

    if read_from:
        before = store.listing(prefix)
        return read_tree(Store(read_from), prefix, gen_dir, keep=keep, want=before, label=label), before
    t0 = monotonic()
    before = mirror(store, prefix, gen_dir, keep=keep)
    kept = {k: n for k, n in before.items() if keep(k[len(prefix) + 1:])}
    doc: dict = {"source": store.root, "files": len(kept), "bytes": sum(kept.values()), "fetched": None, "s": round(monotonic() - t0, 1)}
    err(f"{label}: {doc['files']} files, {doc['bytes'] / 2**30:.3f} GiB from {doc['source']} in {doc['s']}s")
    return doc, before


def upload_new(store: Store, local: Path, prefix: str, before: dict[str, int]) -> list[str]:
    """`blobstore.upload_new` in this store's `publish_order` (its data files in parallel: `publish_tier`)."""
    return _upload_new(store, local, prefix, before, publish_order, tier=publish_tier)


# ── Scans ──────────────────────────────────────────────────────────────────


def source_version(path: Path) -> int:
    """A `path` sort's format from its schema (the column names decide, as `static_names._version_from_footer`)."""
    names = set(pq.read_schema(path).names)
    if {"n_files", "kind"} <= names:
        return 2
    if {"wts", "wb"} <= names:
        return 1
    raise ValueError(f"{path}: unknown source format")


def find_scans(root: str, layouts: list[str], local: Path, *, start: str | None = None, through: str | None = None) -> dict:
    """The scans manifest (`scans.json`) over `root`: each scan id's newest `path` sort under `layouts` (`pick_scans`),
    pinned by size; a URL root's picked files are copied into `local` (keys kept), so the stages read them from there."""
    src = Store(root)
    objects = []
    for t in layouts:
        lead = sn.layout_glob(t)[0].split("*", 1)[0]
        prefix = lead.rsplit("/", 1)[0] if "/" in lead else ""
        objects += [(k, {"size": n}) for k, n in src.listing(prefix).items()]
    scans = sn.pick_scans(objects, layouts, start=start, through=through)
    mount = local if src.url else Path(root)
    for s in scans:
        if src.url:
            dst = local / s["src"]
            if not (dst.is_file() and dst.stat().st_size == s["size"]):
                src.get(s["src"], dst)
        s["version"] = source_version(mount / s["src"])
    return {"bucket": root, "layouts": list(layouts), "scans": scans, "mount": str(mount)}


# ── build: a base generation ───────────────────────────────────────────────


def build_gen(scans: dict, mount: str, gen_dir: Path, work: Path, *, gen: str, rule, k: int = RANGES, shard_rows: int = SHARD_ROWS,
              max_rows: int = MAX_ROWS, R: int = R_ROWS, K: int = K_KEPT, floor: int = FLOOR, chunk_rows: int = CHUNK_ROWS, threads: int = 4,
              mem: str = "4GB", with_anchors: bool = True, ranges: dict | None = None) -> dict:
    """A base generation over `scans` (`scans.json`'s doc; their files under `mount`) in `gen_dir`, every stage in turn:
    ranges → coalesced versions → suffix map → shards → sidecar → census → members → cells → short literals → catalog
    → drill → anchored search (`with_anchors`: `names/` + `anchors/`). `ranges`: these key ranges (a `ranges.json`)
    instead of planning `k` (a compaction's reference: the rebuild over its base's ranges). Returns a summary (rows, files, and per stage its
    seconds, peak work-disk GiB and peak RSS GiB so far)."""
    t0, times, peaks = monotonic(), {}, {}
    gen_dir.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    disk = DiskPeak(work)

    def done(stage: str, t: float) -> float:
        times[stage] = round(monotonic() - t, 1)
        peaks[stage] = {"disk_gb": disk.take(), "rss_gb": rss_peak_gb()}
        err(f"build {gen}: {stage} in {times[stage]}s (work disk peak {peaks[stage]['disk_gb']} GiB, RSS peak {peaks[stage]['rss_gb']} GiB)")
        hb.after(stage)
        return monotonic()

    steps: dict[str, float] = {}

    def step(name: str, t: float, **counts: int) -> float:
        """A sub-step's line within a stage (`drill/<step>`): its seconds, work-disk and RSS peaks, and row counts."""
        steps[name] = round(monotonic() - t, 1)
        n = "".join(f", {k} {v:,}" for k, v in counts.items())
        err(f"build {gen}: {name} in {steps[name]}s (work disk peak {disk.take_step()} GiB, RSS peak {rss_peak_gb()} GiB{n})")
        hb.after(name)
        return monotonic()

    tmp = work / "duckdb"
    con = sn.connect(threads, mem, tmp)
    doc = {**{k_: v for k_, v in scans.items() if k_ != "mount"}, **rule_json(rule)}
    (gen_dir / "scans.json").write_text(json.dumps(doc, indent=1) + "\n")
    t = monotonic()
    ranges = ranges or sn.plan_ranges(doc, k, mount)
    (gen_dir / "ranges.json").write_text(json.dumps(ranges, indent=1) + "\n")
    names = [f"r{r['i']:04d}" for r in ranges["ranges"]]
    for r in ranges["ranges"]:
        sn.build_range(doc, ranges, r["i"], gen_dir, mount=mount, threads=threads, mem=mem, tmp=tmp, con=con, coalesced=True, rule=rule)
    t = done("intervals", t)
    plan = sn.plan_shards(sorted((gen_dir / "chist").glob("*.parquet")), shard_rows, 1)
    (gen_dir / "shards.json").write_text(json.dumps(plan, indent=1) + "\n")
    mapped = work / "map"
    for r in ranges["ranges"]:
        sn.map_range(str(gen_dir / "cintervals" / f"r{r['i']:04d}.parquet"), plan, r["i"], mapped, con, rule)
    for g in range(len(plan["tasks"])):
        files = sorted(str(f) for f in (mapped / "sxmap" / f"g{g:03d}").glob("*.parquet"))
        sn.build_shards(files, plan, g, gen_dir, threads=threads, mem=mem, tmp=tmp)
    shutil.rmtree(mapped)
    side = sn.concat_sidecars([pq.read_table(f) for f in sorted((gen_dir / "sidecar").glob("*.parquet"))])
    sn.write_sidecar(side, gen_dir / "sidecar.parquet")
    t = done("shards", t)
    cat = gen_dir / "catalog"
    census = work / "census"
    census.mkdir(parents=True, exist_ok=True)
    shard_names = [f"s{s['i']:04d}" for s in plan["shards"]]
    for name in shard_names:
        pq.write_table(sc.census(con, f"read_parquet({q(str(gen_dir / 'sx' / f'{name}.parquet'))})", max_rows + 1), census / f"{name}.parquet",
                       compression=sn.CODEC)
    cat.mkdir(parents=True, exist_ok=True)
    members = sc.cut_members(census, cat / "members.parquet", gen=gen, max_rows=max_rows)
    (cat / "members.json").write_text(json.dumps(members, indent=1) + "\n")
    con.execute(f"CREATE OR REPLACE TABLE allm AS SELECT q, shard, rows FROM read_parquet({q(str(cat / 'members.parquet'))})")
    cells = work / "cells"
    for s in plan["shards"]:
        con.execute(f"CREATE OR REPLACE TABLE mem AS SELECT q, rows FROM allm WHERE shard = {s['i']}")
        sc.shard_cells(con, str(gen_dir / "sx" / f"s{s['i']:04d}.parquet"), "mem", cells / f"s{s['i']:04d}.parquet", rule=rule)
    short = work / "short"
    for name in names:
        sc.range_short(con, str(gen_dir / "cintervals" / f"{name}.parquet"), short, name, rule)
    t1 = monotonic()
    assembled = sc.assemble(con, str(short / "events" / "*.parquet"), str(short / "vocab" / "*.parquet"), str(cells / "*.parquet"), cat)
    meta = sc.catalog_meta(gen, assembled, round(monotonic() - t1, 1), members, rule)
    (cat / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    t = done("catalog", t)
    drill = build_base_drill(con, gen_dir, work / "drill", R=R, K=K, floor=floor, rule=rule, chunk_rows=chunk_rows, step=step)
    t = done("drill", t)
    anchors = None
    if with_anchors:
        anchors = build_base_anchors(con, gen_dir, R=R, K=K, scans=[s["id"] for s in doc["scans"]], rule=rule)
        done("anchors", t)
    disk.close()
    return {"gen": gen, "scans": len(doc["scans"]), "ranges": ranges["k"], "suffix_rows": int(sum(side.column("rows").to_pylist())),
            "shards": len(shard_names), "members": members["members"], "cells_rows": meta["cells_rows"], "drill": drill,
            "anchors": anchors, "s": round(monotonic() - t0, 1), "stages": times, "steps": steps, "peaks": peaks}


def build_base_anchors(con, gen_dir: Path, *, R: int, K: int, scans: list[str], rule, fanout: Callable[[], object] | None = None) -> dict:
    """The base generation's anchored search (`static_anchors`; `^q`, `q$`, `^q$` and path terms): the name index
    (`names/`, from the coalesced versions, as `static-names anchors names`), then `anchors/` — the `q$` rollups over the
    suffix shards, the `^q$` ones over the name index and the starts-with catalog `anchors/start/` (`build_tier_local`,
    each `meta.json` written last), at the drill's R and K. `fanout()`: the rollups' `static_fanout.AnchorFanout` (a
    compaction's helper tasks; made once `names/` is written, its shards being their inputs)."""
    cint = sorted(str(f) for f in (gen_dir / "cintervals").glob("*.parquet"))
    t = monotonic()
    hb.set_stage("anchors/names")
    names = an.write_names(con, an.files_sql(cint, an.VERSION_COLS), gen_dir / an.NAMES, jobs=sn.pool_jobs(con, 1 << 30, an.ANCHOR_THREADS))
    sn.err(f"anchors/names: {names['rows']:,} rows in {names['shards']} shards in {monotonic() - t:.0f}s")
    tier = an.Tier(gen_dir)
    t = monotonic()
    doc = an.build_tier_local(con, tier.files("end"), tier.files("exact"), gen_dir / an.ANCHORS, R, K, scans, rule,
                              fanout=fanout() if fanout is not None else None)
    sn.err(f"anchors/rollups: {', '.join(f'{k} {v['rows']:,} rows' for k, v in doc['sets'].items())} in {monotonic() - t:.0f}s")
    return {"names": {k: names[k] for k in ("rows", "shards")}, **{k: v["rows"] for k, v in doc["sets"].items()}}


def _nop_step(name: str, t: float, **counts: int) -> float:
    return monotonic()


def q_bounds(counts: list[tuple[str, int]], chunk_rows: int) -> list[str]:
    """Each `q` range's first literal (the first range's is `''`) over `counts` `(q, rows)` (in `q` order): ranges of at
    most `chunk_rows` rows, contiguous in `q` order (a literal with more is a range of its own)."""
    bounds, n = [""], 0
    for qq, c in counts:
        if n and n + c > chunk_rows:
            bounds.append(qq)
            n = 0
        n += c
    return bounds


def spill_roots(con, table: str, out: Path) -> None:
    """Table `table`'s roots (`ROOT_COLS`) → `out/grp=<g>/*.parquet` by `q` range (table `qg (lo, grp)`)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({sr.ranged(table, 'qg', 'grp')}) TO {q(str(out))} (FORMAT parquet, PARTITION_BY (grp), COMPRESSION zstd)")


def read_roots(files: list[Path]) -> str:
    """A query of spilled roots files' `ROOT_COLS`."""
    lst = "[" + ", ".join(q(str(f)) for f in files) + "]"
    return f"SELECT q, depth, path, usr, vf, vt, size, n_files FROM read_parquet({lst}, hive_partitioning = false)"


def measure_rollups(con, roots: str, *, floor: int, R: int, K: int, dirs_into: str, rup_into: str) -> dict:
    """`roots`' (`ROOT_COLS`) measurement — per member the directories with at least `floor` root rows under them,
    `(q, k, dir, rows)` → table `dirs_into` — and rollup cells (`static_roots.rollup_sql` over the measurement's heavy
    rows, `ROLLUP_COLS`) → table `rup_into`. Both are per member: a `q` range's are its members' share of the whole's."""
    t = monotonic()
    con.execute(f"""CREATE OR REPLACE TABLE rp AS SELECT q, depth, path, count(*) AS n, count(*) FILTER (WHERE vt = {sn.OPEN}) AS n_open
        FROM {roots} GROUP BY q, depth, path""")
    dirs, _, _ = sr.dir_stats(con, "rp", min(floor, R + 1))
    con.register("dirs_in", dirs.select(["q", "k", "dir", "rows"]))
    con.execute(f"INSERT INTO {dirs_into} SELECT * FROM dirs_in WHERE rows >= {floor}")
    con.unregister("dirs_in")
    t1 = monotonic()
    con.execute(f"INSERT INTO {rup_into} {sr.rollup_sql(con, roots, R, K, dirs)}")  # the heavy dirs are the measurement's
    for tb in ("rev2", "rkeep", "hv", "rp"):
        con.execute(f"DROP TABLE IF EXISTS {tb}")
    return {"measure_s": t1 - t, "rollup_s": monotonic() - t1}


def plan_groups(con, counts: list[tuple[str, int]], chunk_rows: int) -> int:
    """Table `qg (lo, grp)`: `q` ranges of about `chunk_rows` roots over `counts` `(q, rows)` (`q_bounds`); their count."""
    bounds = q_bounds(counts, chunk_rows)
    con.execute("CREATE OR REPLACE TABLE qg (lo VARCHAR, grp INTEGER)")
    sd.insert_rows(con, "qg", [(lo, g) for g, lo in enumerate(bounds)])
    return len(bounds)


#: DuckDB threads per measurement process (`measure_ranges`): a `q` range is ~`CHUNK_ROWS` roots and a few dozen small
#: statements (one per depth), which one DuckDB at 48 threads runs mostly serially; so the ranges run `threads //
#: MEASURE_THREADS` at a time, each in its own process (its own database, `memory_limit` split between them).
MEASURE_THREADS = 4
#: Overrides `MEASURE_THREADS`: fewer, wider measurement processes, each with a bigger share of `memory_limit` (a range
#: whose working set passes its share spills to its temp dir and re-reads it per statement).
MEASURE_THREADS_ENV = "DT_MEASURE_THREADS"
#: A `q` range whose spilled roots pass this many bytes is heavy (gcs c001's: single-`q` ranges of 100M–440M roots,
#: 0.8–5.2 GB zstd, against a median of 64 MB / 4M roots): `measure_ranges` measures the heavy ones first, `HEAVY_THREADS`
#: DuckDB threads each (fewer processes, each a bigger share of `memory_limit`); at a light range's 1/13 they spilled
#: 20–37 GiB apiece and set the stage's wall clock. `$DT_MEASURE_HEAVY_BYTES` overrides it.
HEAVY_BYTES = 1 << 30
HEAVY_BYTES_ENV = "DT_MEASURE_HEAVY_BYTES"
HEAVY_THREADS = 16


def measure_jobs(con, ranges: int) -> int:
    """How many `q` ranges `measure_ranges` measures at a time over `con`'s threads (`MEASURE_THREADS` each, or
    `$DT_MEASURE_THREADS`)."""
    return sn.pool_jobs(con, ranges, int(os.environ.get(MEASURE_THREADS_ENV) or MEASURE_THREADS))


def _measure_one(con, files: list[str], sorted_out: str, dirs_out: str, rup_out: str, *, floor: int, R: int, K: int, measure: bool = True) -> dict:
    """One `q` range's spill `files` measured and rolled up (`measure_rollups`) → parquet `dirs_out`, `rup_out`, and its
    roots sorted → `sorted_out`; the spill's directories removed. Its seconds and spilled bytes. `measure` false: only
    the sorted roots (`dirs_out`, `rup_out` written empty; a compaction's incremental measurement, `static_measure`)."""
    con.execute("CREATE OR REPLACE TABLE mr_dirs (q VARCHAR, k INTEGER, dir VARCHAR, rows BIGINT)")
    con.execute(f"CREATE OR REPLACE TABLE mr_rup ({sd.ROLLUP_COLS})")
    t0 = monotonic()
    con.execute(f"CREATE OR REPLACE TABLE qg_roots AS {read_roots([Path(f) for f in files])}")
    temp = [temp_bytes(con)]
    m = (measure_rollups(con, "qg_roots", floor=floor, R=R, K=K, dirs_into="mr_dirs", rup_into="mr_rup") if measure
         else {"measure_s": 0.0, "rollup_s": 0.0})
    temp.append(temp_bytes(con))
    for tb, f in (("mr_dirs", dirs_out), ("mr_rup", rup_out)):
        con.execute(f"COPY {tb} TO {q(f)} (FORMAT parquet, COMPRESSION zstd)")
    con.execute(f"COPY (SELECT {', '.join(sr.ROOT_SCHEMA.names)} FROM qg_roots ORDER BY q, path, usr, vf) TO {q(sorted_out)} (FORMAT parquet, COMPRESSION zstd)")
    temp.append(temp_bytes(con))
    rows = count(con, "qg_roots")
    for tb in ("qg_roots", "mr_dirs", "mr_rup"):
        con.execute(f"DROP TABLE IF EXISTS {tb}")
    nbytes = sum(Path(f).stat().st_size for f in files)
    for d in sorted({str(Path(f).parent) for f in files}):
        shutil.rmtree(d)
    return {**m, "nbytes": nbytes, "rows": rows, "files": len(files), "temp": temp, "s": monotonic() - t0}


def temp_bytes(con) -> int:
    """The bytes of `con`'s DuckDB temp files now (buffers evicted past `memory_limit`)."""
    return int(con.execute("SELECT coalesce(sum(size), 0) FROM duckdb_temporary_files()").fetchone()[0])


def measure_report(kind: str, done: dict[int, dict], top: int = 5) -> None:
    """A measurement's per-range costs: how many ranges spilled past their `memory_limit` share (temp bytes after
    loading, measuring and the sorted copy), and the `top` slowest with their rows, input files, bytes and temp."""
    spilled = [d for d in done.values() if max(d["temp"]) > 0]
    sn.err(f"drill/measure/{kind}: {len(spilled):,} of {len(done):,} ranges spilled to temp (largest {fmt_b(max((max(d['temp']) for d in done.values()), default=0))})")
    for g, d in sorted(done.items(), key=lambda kv: -kv[1]["s"])[:top]:
        sn.err(f"drill/measure/{kind}: range {g}: {d['s']:.1f}s, {d['rows']:,} roots from {d['files']:,} files ({fmt_b(d['nbytes'])}); "
               f"temp after load / measure / sort {' / '.join(fmt_b(b) for b in d['temp'])}")


def fmt_b(n: int) -> str:
    return hb.fmt_bytes(n)


def _measure_task(files: list[str], sorted_out: str, dirs_out: str, rup_out: str, floor: int, R: int, K: int, threads: int, mem: str,
                  tmp: str, measure: bool = True) -> dict:
    """`_measure_one` in a process of `measure_ranges`' pool, on a database of its own."""
    con = sn.connect(threads, mem, tmp)
    try:
        return _measure_one(con, files, sorted_out, dirs_out, rup_out, floor=floor, R=R, K=K, measure=measure)
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)


def spill_files(work: Path, kind: str) -> dict[int, list[Path]]:
    """Each `q` range's spill files of `kind` (`work/<kind>/<producer>/grp=<g>/*.parquet`), sorted, in one walk: a glob
    per range listed every producer's dir per range (gcs's c001: 5,371 ranges × ~4,900 fold chunks, minutes on one core
    before the measurement or a fan-out's plan began)."""
    import os

    out: dict[int, list[Path]] = {}
    root = work / kind
    if not root.is_dir():
        return out
    with os.scandir(root) as producers:
        for prod in producers:
            if not prod.is_dir():
                continue
            with os.scandir(prod.path) as groups:
                for grp in groups:
                    if grp.is_dir() and grp.name.startswith("grp="):
                        with os.scandir(grp.path) as files:
                            out.setdefault(int(grp.name[4:]), []).extend(Path(f.path) for f in files if f.name.endswith(".parquet"))
    return {g: sorted(fs) for g, fs in out.items()}


def measure_ranges(con, work: Path, kind: str, n: int, dirs_into: str, rup_into: str, *, floor: int, R: int, K: int,
                   jobs: int | None = None, remote=None, on_range: Callable[[int, tuple, dict], None] | None = None,
                   measure: bool = True) -> tuple[list[Path], dict]:
    """Measure and roll up each of `kind`'s spilled `q` ranges (`work/<kind>/*/grp=<g>/`) into tables `dirs_into` and
    `rup_into`, and write each range's roots sorted (`work/<kind>-sorted/g<g>.parquet`, its spill removed): the ranges
    are `q`-disjoint and in `q` order, so the files are what `static_drill.write_kind` streams (`chunks`) instead of
    sorting every root again. The sorted files, in `q` order.

    The heavy ranges (spilled bytes ≥ `HEAVY_BYTES`) first, heaviest first, `HEAVY_THREADS` threads each; then the rest,
    heaviest first, `jobs` at a time (default `measure_jobs`). Each range in a process of its own (spawned; `con`'s threads
    and memory limit split between them, `con`'s own limit lowered to a share meanwhile); 1: in turn on `con`. Each
    range's measurement and rollups go to parquet first and into the tables in `q`-range order, so neither the order nor
    `jobs` changes what the tables hold; their consumers sort them (`write_kind`, `roots-measure/`).

    `remote` (a `static_fanout.Fanout`): the ranges it deals to its helper tasks are not measured here; once these are,
    `remote.collect` waits for the helpers' outputs and puts them where this measurement's would be (their spills are
    then removed), so the tables and files are the same. `on_range(g, task, stats)`: called once range `g` is measured
    (`task`: `(g, spill files, sorted, dirs, rup)`, before the per-range files are folded in): a helper uploads them.
    `measure` false: each range's roots are only sorted (`_measure_one`), the tables left empty."""
    from concurrent.futures import as_completed

    for tb, cols in ((dirs_into, "q VARCHAR, k INTEGER, dir VARCHAR, rows BIGINT"), (rup_into, sd.ROLLUP_COLS)):
        con.execute(f"CREATE OR REPLACE TABLE {tb} ({cols})")
    out, meas = work / f"{kind}-sorted", work / f"{kind}-measured"
    out.mkdir(parents=True, exist_ok=True)
    meas.mkdir(parents=True, exist_ok=True)
    tasks = []
    spilled = spill_files(work, kind)
    # the ranges dealt away need no spills here (a resumed fold fetches only its own share's: `Fanout.start`)
    away = remote.groups(kind) if remote is not None else set()
    for g in range(n):
        files = spilled.get(g, [])
        if files or g in away:
            tasks.append((g, [str(f) for f in files], str(out / f"g{g:06d}.parquet"), str(meas / f"dirs-g{g:06d}.parquet"),
                          str(meas / f"rup-g{g:06d}.parquet")))
    local = [t for t in tasks if t[0] not in away]
    size = {t[0]: sum(Path(f).stat().st_size for f in t[1]) for t in local}
    by_size = sorted(local, key=lambda t: (-size[t[0]], t[0]))
    heavy_bytes = int(os.environ.get(HEAVY_BYTES_ENV) or HEAVY_BYTES)
    heavy = [t for t in by_size if size[t[0]] >= heavy_bytes]
    light = [t for t in by_size if size[t[0]] < heavy_bytes]
    fixed = jobs
    prog = hb.Progress(f"drill/measure/{kind}", n, unit="ranges")
    prog.tick(n - len(tasks), nbytes=0)  # the ranges without roots
    t0, done = monotonic(), {}

    def run_group(group: list, jobs: int, label: str) -> None:
        if not group:
            return
        task = {t[0]: t for t in group}
        if jobs == 1:
            for g, files, so, do, ro in group:
                done[g] = _measure_one(con, files, so, do, ro, floor=floor, R=R, K=K, measure=measure)
                prog.tick(nbytes=done[g]["nbytes"])
                if on_range:
                    on_range(g, task[g], done[g])
            return
        with sn.ConPool(con, jobs, label) as pool:
            futs = {pool.submit(_measure_task, files, so, do, ro, floor, R, K, pool.task_threads, pool.mem,
                                str(work / "measure-tmp" / f"{kind}-g{g:06d}"), measure): g for g, files, so, do, ro in group}
            for f in as_completed(futs):
                g = futs[f]
                done[g] = f.result()
                prog.tick(nbytes=done[g]["nbytes"])
                if on_range:
                    on_range(g, task[g], done[g])

    if heavy:
        hj = max(1, min(fixed, len(heavy))) if fixed is not None else sn.pool_jobs(con, len(heavy), HEAVY_THREADS)
        sn.err(f"drill/measure/{kind}: {len(heavy)} heavy ranges (≥ {heavy_bytes:,} B spilled, up to {max(size.values()):,}) first, {hj} at a time")
        run_group(heavy, hj, f"drill/measure/{kind} (heavy)")
    jobs = measure_jobs(con, len(light)) if fixed is None else max(1, min(fixed, len(light) or 1))
    run_group(light, jobs, f"drill/measure/{kind}")
    if away:
        theirs = [t for t in tasks if t[0] in away]
        got = remote.collect(kind, {t[0]: (t[2], t[3], t[4]) for t in theirs})
        done.update(got)
        prog.tick(len(got), nbytes=sum(d["nbytes"] for d in got.values()))
        for d in sorted({str(Path(f).parent) for t in theirs for f in t[1]}):
            shutil.rmtree(d, ignore_errors=True)
    prog.done()
    if done:
        measure_report(kind, done)
    for tb, i in ((dirs_into, 3), (rup_into, 4)):
        if tasks:
            con.execute(f"INSERT INTO {tb} SELECT * FROM read_parquet([{', '.join(q(t[i]) for t in tasks)}])")
    shutil.rmtree(meas)
    # wall seconds, split as the ranges' own measure / rollup seconds were (they overlap at `jobs` > 1)
    wall, ms, rs = monotonic() - t0, sum(d["measure_s"] for d in done.values()), sum(d["rollup_s"] for d in done.values())
    frac = ms / (ms + rs) if ms + rs else 1.0
    ranges = {g: {k: d[k] for k in ("s", "rows", "files", "nbytes", "temp")} for g, d in sorted(done.items())}
    return [Path(t[2]) for t in tasks], {"groups": len(tasks), "measure_s": wall * frac, "rollup_s": wall * (1 - frac), "jobs": jobs, "ranges": ranges}


def finish_drill(con, gen_dir: Path, work: Path, *, aliases, long_groups: int, short_groups: int, R: int, K: int, floor: int,
                 step=_nop_step, t: float | None = None, fanout=None, incremental: tuple[list, Path] | None = None) -> dict:
    """A base drill's second half, from roots already spilled under `work/{long,short}/*/grp=<g>/` (canonical members'
    long roots; short literals' roots) by `q` range (`long_groups`, `short_groups` ranges) and the member classes (table
    `al (q, canonical)`, and `aliases` as Arrow) and their digests (table `dgq (q, n, h1, h2)`, every long member's): each
    range measured and rolled up, `roots-measure/{dirs,short/dirs}/` (an alias gets its canonical's directories), then
    `drill/` (`static_drill.build_drill`) with the members' digests (`static_drill.write_digests`: what a later
    compaction finds its classes from, specs/static-incremental-compaction.md step 3). What `build_base_drill`
    and a compaction's fold (`static_compact`) share. Returns the drill's `meta.json` and the groups measured.
    `fanout` (`static_fanout.Fanout`): the ranges measured in part by helper tasks (a compaction's, its spills saved).

    `incremental` `(tiers, meas)` (a compaction whose runs all count only new roots: `static_measure.blocker`): the
    ranges' roots are only sorted, and `roots-measure/` and the rollups come from base ⊕ runs instead
    (`static_measure.incremental_measure`, `incremental_rollups`: the tiers' drills, the base's measurement `meas`,
    `gen_dir/catalog/members.parquet`): the same bytes, with no measurement or rollup over every root."""
    from . import static_measure as sme

    t = monotonic() if t is None else t
    measure = incremental is None
    if fanout is not None:
        fanout.start(work, {"long": long_groups, "short": short_groups}, R=R, K=K, floor=floor, measure=measure)
    lcan, lm = measure_ranges(con, work, "long", long_groups, "ldirs", "lrup", floor=floor, R=R, K=K, remote=fanout, measure=measure,
                              on_range=fanout.keep("long") if fanout is not None else None)
    end = monotonic()
    if measure:
        step("drill/measure/long", end - lm["measure_s"] - lm["rollup_s"], groups=lm["groups"], dirs=count(con, "ldirs"))
        t = step("drill/rollup/long", end - lm["rollup_s"], rup=count(con, "lrup"))
    else:
        t = step("drill/sort/long", t, groups=lm["groups"])
    sall, m = measure_ranges(con, work, "short", short_groups, "sdirs", "srup", floor=floor, R=R, K=K, remote=fanout, measure=measure,
                              on_range=fanout.keep("short") if fanout is not None else None)
    if fanout is not None:
        fanout.finish()
    end = monotonic()
    if measure:
        step("drill/measure/short", end - m["measure_s"] - m["rollup_s"], groups=m["groups"], dirs=count(con, "sdirs"))
        t = step("drill/rollup/short", end - m["rollup_s"], rup=count(con, "srup"))
    else:
        t = step("drill/sort/short", t, groups=m["groups"])
    meas = gen_dir / "roots-measure"
    if measure:
        # every long member's (as `roots measure` measures them): an alias's roots, so its directories, are its canonical's
        for sub, sql in (("dirs", "SELECT al.q, d.k, d.dir, d.rows FROM ldirs d JOIN al ON al.canonical = d.q"), ("short/dirs", "SELECT * FROM sdirs")):
            (meas / sub).mkdir(parents=True, exist_ok=True)
            pq.write_table(con.execute(f"SELECT * FROM ({sql}) ORDER BY q, k, dir").to_arrow_table(), meas / sub / "s0000.parquet", compression=sn.CODEC)
        t = step("drill/write/measure", t)
    else:
        tiers, base_meas = incremental
        sme.incremental_measure(con, tiers, base_meas, str(gen_dir / "catalog" / "members.parquet"), meas, floor=floor)
        t = step("drill/measure/incremental", t)
        for kind, into in (("long", "lrup"), ("short", "srup")):
            sme.incremental_rollups(con, tiers, kind, meas, into, R=R, aliases=aliases)
        t = step("drill/rollup/incremental", t, rup=count(con, "lrup") + count(con, "srup"))
    sd.write_digests(con, gen_dir / "drill")
    meta = sd.build_drill(con, gen_dir / "drill", long_roots=None, short_roots=None, aliases=aliases, R=R, K=K, meta={"floor": floor},
                          rollups={"long": "lrup", "short": "srup"}, sorted_roots={"long": lcan, "short": sall}, step=step)
    for tb in ("dmem", "dg", "dgq", "al", "canon", "qg", "ldirs", "sdirs", "lrup", "srup", "mem"):
        con.execute(f"DROP TABLE IF EXISTS {tb}")
    shutil.rmtree(work)
    return {"meta": meta, "groups": {"long": lm["groups"], "short": m["groups"]}}


def build_base_drill(con, gen_dir: Path, work: Path, *, R: int, K: int, floor: int, rule, chunk_rows: int = CHUNK_ROWS,
                     step=_nop_step) -> dict:
    """The base generation's `drill/` (`static_drill.build_drill`: every member's roots and rollups, in the layout the runs'
    drills share) and its measurement (`roots-measure/`: per member, the directories with at least `floor` root rows, which
    a run's drill reads to decide heaviness), from the generation's `sx/`, `catalog/members.parquet` and `cintervals/`.
    Long members with equal root sets share their least member's rows (`aliases.parquet`, by root-set digest, as
    `roots digest` → `roots alias-plan`).

    Chunked as the staged path's `roots digest` / `roots build` / `short-map` → `short-reduce`, so the working set is
    `chunk_rows`, not the corpus (specs/architecture/static-name-search.md, "One machine: the drill's cost"):
    - the long members' loops (digest, then the canonical members' roots) run shard by shard over each shard's own
      members, in `(s, path)` chunks of about `chunk_rows` suffix rows (`static_roots.chunk_wheres`);
    - the short literals' roots are cut per coalesced-versions file in `hash(path)` pieces of about `chunk_rows` roots;
    - each chunk's roots go to zstd parquet under `work`, split by `q` range of about `chunk_rows` roots (planned from
      the digests' and a counting pass's per-literal rows), and each range is measured and rolled up on its own.
    Each piece is exact (a `(q, path)`'s rows share one suffix; measurement and rollups are per member), so the output is
    the unchunked build's, byte for byte. `step(name, t, **counts)` reports each sub-step (`build_gen`'s `drill/…`
    lines; a loop's are summed over its iterations)."""
    t = monotonic()
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    plan = json.loads((gen_dir / "shards.json").read_text())
    members = str(gen_dir / "catalog" / "members.parquet")
    con.execute(f"CREATE OR REPLACE TABLE dmem AS SELECT q, shard FROM read_parquet({q(members)})")
    n_mem = count(con, "dmem")
    sxf = {s["i"]: gen_dir / "sx" / f"s{s['i']:04d}.parquet" for s in plan["shards"]}
    have = {i for (i,) in con.execute("SELECT DISTINCT shard FROM dmem").fetchall()}
    chunks = {i: sr.chunk_wheres(str(sxf[i]), chunk_rows) for i in sorted(have)}

    def member_loop(i: int, into: str, agg: bool | str, each=None) -> None:
        if count(con, "mem"):
            for k, where in enumerate(chunks[i]):
                sr.member_roots(con, sr.sx_rows_sql(f"(SELECT * FROM read_parquet({q(str(sxf[i]))}) WHERE {where})"), "mem", into, agg=agg, rule=rule)
                if each:
                    each(k)

    con.execute(f"DROP TABLE IF EXISTS dg; CREATE TABLE dg ({sr.DIGEST_COLS})")
    for i in chunks:
        con.execute(f"CREATE OR REPLACE TABLE mem AS SELECT q FROM dmem WHERE shard = {i}")
        member_loop(i, "dg", "digest")
    t = step("drill/digest", t, members=n_mem, shards=len(chunks), chunks=sum(map(len, chunks.values())), digest_rows=count(con, "dg"))
    con.execute("""CREATE OR REPLACE TABLE dgq AS SELECT q, sum(n)::BIGINT AS n, (sum(h1) % 18446744073709551616)::UBIGINT AS h1,
        (sum(h2) % 18446744073709551616)::UBIGINT AS h2 FROM dg GROUP BY q""")
    con.execute("CREATE OR REPLACE TABLE al AS SELECT q, min(q) OVER (PARTITION BY n, h1, h2) AS canonical FROM dgq")
    missing = con.execute("SELECT count(*) FROM dmem ANTI JOIN al USING (q)").fetchone()[0]
    if missing:
        raise RuntimeError(f"{missing} members without a root-set digest")
    aliases = con.execute("SELECT q, canonical FROM al ORDER BY q").to_arrow_table()
    con.execute("CREATE OR REPLACE TABLE canon AS SELECT DISTINCT canonical AS q FROM al")
    groups = plan_groups(con, con.execute("SELECT q, n FROM dgq SEMI JOIN canon USING (q) WHERE n > 0 ORDER BY q").fetchall(), chunk_rows)
    t = step("drill/aliases", t, members=aliases.num_rows, canonical=count(con, "canon"))
    n_long = 0
    for i in chunks:  # each shard's canonical members' roots, chunk by chunk, spilled by q range
        def spill(k: int, i=i) -> None:
            nonlocal n_long
            n_long += count(con, "rt")
            spill_roots(con, "rt", work / "long" / f"s{i:04d}-c{k:05d}")
            con.execute("DELETE FROM rt")

        con.execute(f"DROP TABLE IF EXISTS rt; CREATE TABLE rt ({sr.ROOT_COLS})")
        con.execute(f"CREATE OR REPLACE TABLE mem AS SELECT q FROM dmem SEMI JOIN canon USING (q) WHERE shard = {i}")
        member_loop(i, "rt", False, spill)
    con.execute("DROP TABLE IF EXISTS rt")
    t = step("drill/canonical-roots", t, lcan=n_long, groups=groups)
    long_groups = groups
    cis = sorted((gen_dir / "cintervals").glob("*.parquet"))
    con.execute(f"DROP TABLE IF EXISTS sc; CREATE TABLE sc (f INTEGER, {sr.COUNT_COLS})")
    for f, ci in enumerate(cis):  # a counting pass: the q ranges and each file's pieces
        con.execute(f"DROP TABLE IF EXISTS scf; CREATE TABLE scf ({sr.COUNT_COLS})")
        sr.short_roots(con, f"SELECT * FROM read_parquet({q(str(ci))})", "scf", agg="count", rule=rule)
        con.execute(f"INSERT INTO sc SELECT {f}, q, n FROM scf")
    con.execute("DROP TABLE IF EXISTS scf")
    groups = plan_groups(con, con.execute("SELECT q, sum(n)::BIGINT FROM sc GROUP BY q ORDER BY q").fetchall(), chunk_rows)
    per_file = dict(con.execute("SELECT f, sum(n)::BIGINT FROM sc GROUP BY f").fetchall())
    n_short = pieces = 0
    for f, ci in enumerate(cis):
        k_f = max(1, -(-per_file.get(f, 0) // chunk_rows))
        for k in range(k_f):
            con.execute(f"DROP TABLE IF EXISTS rt; CREATE TABLE rt ({sr.ROOT_COLS})")
            where = f" WHERE hash(path) % {k_f} = {k}" if k_f > 1 else ""
            sr.short_roots(con, f"SELECT * FROM read_parquet({q(str(ci))}){where}", "rt", rule=rule)
            n_short += count(con, "rt")
            spill_roots(con, "rt", work / "short" / f"f{f:04d}-p{k:04d}")
            pieces += 1
    con.execute("DROP TABLE IF EXISTS rt; DROP TABLE IF EXISTS sc")
    t = step("drill/short-roots", t, sall=n_short, pieces=pieces, groups=groups)
    done = finish_drill(con, gen_dir, work, aliases=aliases, long_groups=long_groups, short_groups=groups, R=R, K=K, floor=floor, step=step, t=t)
    return {"members": aliases.num_rows, "canonical": len(set(aliases.column("canonical").to_pylist())), **{k: done["meta"][k] for k in ("R", "K")},
            "chunks": sum(map(len, chunks.values())), "pieces": pieces, "groups": done["groups"]}


def count(con, table: str) -> int:
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# ── add: scans appended as runs ────────────────────────────────────────────


def append_scan(con, gen_dir: Path, scan: dict, mount: str, work: Path, *, gen: str, rule, threads: int, mem: str,
                compact_level: int | None) -> dict:
    """Scan `scan` appended to the generation in `gen_dir` (the base, its runs and manifests) as the run `deltas/<id>/`,
    and published (`manifests/<id>.json`): `static_append`'s per-range append over the refolded open versions, the run's
    shards and catalog delta, the drill's run (`static_drill.build_day`), then `publish_run`."""
    t0 = monotonic()
    disk = DiskPeak(work)
    d = scan["id"]
    store = LocalRunStore(gen_dir, work / "lease", gen)
    base = store.read_json("scans.json")
    prev = latest_key(store.keys("manifests/"), before=d)
    runs = store.read_json(prev)["runs"] if prev else []
    have = scans_of(base, runs)
    if d <= have[-1]:
        raise SystemExit(f"{d}: not after the generation's newest scan {have[-1]}")
    days = [s for r in runs for s in r["scans"]]
    run = gen_dir / run_key(d, d)
    if run.exists():
        shutil.rmtree(run)  # an unpublished run (no manifest lists it): redo it whole
    run.mkdir(parents=True)
    (run / "scans.json").write_text(json.dumps({"bucket": base["bucket"], "scans": [scan]}, indent=1) + "\n")
    ranges = store.read_json("ranges.json")
    for r in ranges["ranges"]:
        name = f"r{r['i']:04d}"
        open_file = work / "open" / f"{name}.parquet"
        open_file.parent.mkdir(parents=True, exist_ok=True)
        sa.rebuild_open(con, str(gen_dir / "cintervals" / f"{name}.parquet"),
                        [str(gen_dir / run_key(x, x) / "cdelta" / f"{name}.parquet") for x in days], open_file)
        sa.append_open(con, f"SELECT * FROM read_parquet({q(str(open_file))})", scan, r, name, run, bucket=base["bucket"], mount=mount, rule=rule)
    shutil.rmtree(run / "copen")
    shutil.rmtree(work / "open")
    cdelta = sorted(str(f) for f in (run / "cdelta").glob("*.parquet"))
    sa.delta_shards(con, cdelta, run, rule=rule)
    deltas = [sorted(str(f) for f in (gen_dir / run_key(x, x) / "cdelta").glob("*.parquet")) for x in [*days, d]]
    tiers = [gen_dir / "catalog", *(gen_dir / r["key"] / "catalog" for r in runs)]
    V = store.read_json("catalog/meta.json")["membership"]["max_rows"]
    side = pq.read_table(gen_dir / "sidecar.parquet")
    cat_work = work / "catalog-work"
    sa.catalog_delta(con, tiers, sc.BaseShards(str(gen_dir), side), deltas, V, run / "catalog", cat_work, rule)
    shutil.rmtree(cat_work, ignore_errors=True)

    def step(name: str, t: float, **counts: int) -> float:
        """A drill sub-step's line (`drill/<step>/<kind>`, as `build_gen`'s): its seconds, work-disk and RSS peaks, and counts."""
        n = "".join(f", {k} {v:,}" for k, v in counts.items())
        err(f"add {d}: {name} in {round(monotonic() - t, 1)}s (work disk peak {disk.take_step()} GiB, RSS peak {rss_peak_gb()} GiB{n})")
        hb.after(name)
        return monotonic()

    drill = run_drill(con, gen_dir, run, scan, runs, days, gen=gen, rule=rule, step=step)
    anchors = run_anchors(con, gen_dir, run, d, runs, rule=rule) if (gen_dir / an.ANCHORS / "meta.json").exists() else None
    manifest = sa.publish_run(store, d, rule, max_level=compact_level)
    peak = {"disk_gb": disk.close(), "rss_gb": rss_peak_gb()}
    err(f"add {d}: published {len(manifest['runs'])} runs in {monotonic() - t0:.1f}s (work disk peak {peak['disk_gb']} GiB, RSS peak {peak['rss_gb']} GiB)")
    return {"scan": d, "drill": drill, "anchors": anchors, "runs": [r["key"] for r in manifest["runs"]], "s": round(monotonic() - t0, 1), "peak": peak}


def run_anchors(con, gen_dir: Path, run: Path, d: str, runs: list[dict], *, rule) -> dict:
    """The run's `names/` (from its `cdelta/`) and `anchors/` (+ `anchors/start/`; `static_anchors.build_run_local`, as
    `static-names anchors run`) over the base's and the live runs' (`runs`: the manifest before it), at the base's R and K.
    Every earlier tier must carry them: the readers cut the anchored stack at the first run without `anchors/meta.json`,
    so a run built over a gap would never be read."""
    prior = [an.Tier(gen_dir), *(an.Tier(gen_dir / r["key"]) for r in runs)]
    missing = [r["key"] for r in runs if not (gen_dir / r["key"] / an.ANCHORS / "meta.json").exists()]
    if missing:
        raise SystemExit(f"{missing[0]}: no {an.ANCHORS}/meta.json under a base that has one; rebuild into a new generation")
    meta = json.loads((gen_dir / an.ANCHORS / "meta.json").read_text())
    cdelta = sorted(str(f) for f in (run / "cdelta").glob("*.parquet"))
    doc = an.build_run_local(con, prior, an.Tier(run), sn.scan_epoch(d), meta["R"], meta["K"], [d],
                             versions_sql=an.files_sql(cdelta, an.VERSION_COLS), rule=rule)
    return {k: v["rows"] for k, v in doc["sets"].items()}


def run_drill(con, gen_dir: Path, run: Path, scan: dict, runs: list[dict], days: list[str], *, gen: str, rule, step=_nop_step) -> dict:
    """The run's `drill/` over the base's and the live runs' drills (`static-names drill build`'s inputs, from `gen_dir`);
    `step` reports its sub-steps (`static_drill.build_day`'s)."""
    prior = [sd.Tier(gen_dir / "drill", "base"), *(sd.Tier(gen_dir / r["key"] / "drill", r["key"]) for r in runs)]
    for t in prior[1:]:
        if t.meta.get("last") != t.name.split("/")[-1].split("_")[-1]:
            raise SystemExit(f"{t.name}: its drill is for {t.meta.get('last')}")
    base_meta = prior[0].meta
    sx = [str(p) for p in sorted((run / "sx").glob("*.parquet"))]
    cdelta = [str(p) for p in sorted((run / "cdelta").glob("*.parquet"))]
    hist = [str(p) for p in sorted((gen_dir / "cintervals").glob("*.parquet"))]
    hist += [str(p) for x in days for p in sorted((gen_dir / run_key(x, x) / "cdelta").glob("*.parquet"))]
    con.execute(f"CREATE OR REPLACE TABLE pnew AS {sd.pnew_sql(cdelta, hist)}")
    mount, prefix = str(gen_dir.parent.parent), f"{PREFIX}/{gen_dir.name}"
    new_members = sd._new_members(mount, prefix, run.relative_to(gen_dir).as_posix(), prior[-1].alias_table())
    history = sd.history_table(con, [str(gen_dir), *(str(gen_dir / r["key"]) for r in runs), str(run)], new_members) if new_members else None
    meas_dir = gen_dir / "roots-measure"
    meas = {"long": sd.meas_sql("long", [str(p) for p in sorted((meas_dir / "dirs").glob("*.parquet"))]),
            "short": sd.meas_sql("short", [str(p) for p in sorted((meas_dir / "short" / "dirs").glob("*.parquet"))],
                                 [str(p) for p in sorted((meas_dir / "short" / "top").glob("*.parquet"))])}
    d = scan["id"]
    meta = sd.build_day(con, prior, run / "drill", D=sn.scan_epoch(d), R=base_meta["R"], K=base_meta["K"], floor=base_meta.get("floor", FLOOR),
                        sx_files=sx, cdelta_files=cdelta, new_members=new_members, history=history, meas=meas, rg=sd.RUN_RG,
                        rule=rule, tier={"gen": gen, "first": d, "last": d, "level": 0, "scans": [d], "prior": [t.name for t in prior]}, step=step)
    con.execute("DROP TABLE IF EXISTS pnew")
    return {"new_members": len(new_members), "rows": {k: v for k, v in meta.items() if k.endswith("_rows")}}


def merge_due(gen_dir: Path, work: Path, *, gen: str, rule, compact_level: int | None) -> dict:
    """The newest manifest's due carries (`static_merge.merge_pending` over the generation dir), each published as a
    revision of it."""
    from .static_merge import merge_pending

    store = LocalRunStore(gen_dir, work / "lease", gen)
    return merge_pending(store, gen_dir, tmp=work / "merge", rule=rule, max_level=compact_level, heartbeat_s=None)


def now() -> float:
    """The prune's clock (tests move it past the grace)."""
    return time.time()


# ── CLI ────────────────────────────────────────────────────────────────────


@contextmanager
def workdir(path: str | None):
    if path:
        Path(path).mkdir(parents=True, exist_ok=True)
        yield Path(path)
    else:
        with tempfile.TemporaryDirectory(prefix="static-names-") as d:
            yield Path(d)


def _check_gen(gen: str) -> None:
    import re

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", gen):
        raise SystemExit(f"bad generation {gen!r}: one path segment of letters, digits, `.`, `_`, `-`")


@command("build")
@option("-A", "--no-anchors", is_flag=True, help="Skip anchored search (`names/`, `anchors/`): readers then decline `^q`, `^q$`, a heavy `q$` and path terms, and `add` appends runs without them")
@option("-c", "--chunk-rows", default=CHUNK_ROWS, type=IntRange(min=1), help="The drill's chunks: suffix rows per member-loop chunk, roots per measurement / rollup q range (bounds its working set)")
@option("-f", "--floor", default=FLOOR, type=IntRange(min=1), help="The drill's measurement floor (root rows under a directory)")
@option("-g", "--gen", required=True, help="The generation to write: `<store>/static-names/GEN/` (never over an existing one)")
@option("-k", "--ranges", "k", default=RANGES, type=IntRange(min=1), help="Key ranges (each one DuckDB pass over every scan)")
@option("-K", "--keep", "K", default=K_KEPT, type=IntRange(min=1), help="The drill's and the anchors' kept children per heavy directory")
@option("-L", "--layout", "layouts", multiple=True, required=True, help="Key template of the scans' `path` sorts under -s (`{id}` the scan id, `{gen}` the store generation); repeat")
@option("-M", "--mem", default="4GB", help="DuckDB memory limit")
@option("-n", "--shard-rows", default=SHARD_ROWS, type=IntRange(min=1), help="Suffix rows per shard file")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (`r2://bucket[/prefix]`, `s3://…`), the readers' INDEX_R2 root")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-r", "--ranges-file", help="Key ranges from this `ranges.json` instead of planning -k (e.g. a compacted generation's: the rebuild it must equal)")
@option("-R", "--read-rows", "R", default=R_ROWS, type=IntRange(min=1), help="The drill's and the anchors' heavy bound: more root rows under a directory")
@option("-s", "--scans", "scans_root", required=True, help="Where the scans are: a dir or URL, the root of -L's keys")
@option("-t", "--through", help="Last scan id (inclusive; default: the newest)")
@option("-u", "--current-key", help="Publish this lineage's independent current-pointer key after the generation is whole")
@option("-V", "--max-rows", default=MAX_ROWS, type=IntRange(min=1), help="The catalog's membership bound V (range rows)")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after)")
@option("-x", "--hex-runs", required=True, help="The hex-run rule the generation is built with: `MIN,TAIL` (e.g. `16,8`) or `off`")
def build_cmd(no_anchors, chunk_rows, floor, gen, k, K, layouts, mem, shard_rows, store_root, threads, ranges_file, R, scans_root, through, current_key, max_rows, work,
              hex_runs) -> None:
    """Build a base generation of the static name index on this machine, every stage in-process: the staged (Batch)
    path's stages over a local work dir, then the generation copied into the store (data files first, `meta.json`s last).
    Anchored search (`names/`, `anchors/`) is built unless `-A`. Prints a JSON summary."""
    _check_gen(gen)
    rule = parse_rule(hex_runs)
    store = Store(store_root)
    prefix = f"{PREFIX}/{gen}"
    if store.listing(prefix):
        raise SystemExit(f"{store.root}/{prefix}/ exists: a generation is never rewritten")
    with workdir(work) as w:
        scans = find_scans(scans_root, list(layouts), w / "scans", through=through)
        if not scans["scans"]:
            raise SystemExit(f"no scans under {scans_root} matching {list(layouts)}")
        gen_dir = w / "store" / prefix
        if gen_dir.exists():
            shutil.rmtree(gen_dir)
        summary = build_gen(scans, scans["mount"], gen_dir, w / "build", gen=gen, rule=rule, k=k, shard_rows=shard_rows, max_rows=max_rows,
                            R=R, K=K, floor=floor, chunk_rows=chunk_rows, threads=threads, mem=mem, with_anchors=not no_anchors,
                            ranges=json.loads(Path(ranges_file).read_text()) if ranges_file else None)
        summary["files"] = len(upload_new(store, gen_dir, prefix, {}))
    publish_current(store, gen, current_key)
    print(json.dumps(summary, indent=1))


def publish_current(store: Store, gen: str, key: str | None) -> None:
    """An explicitly configured lineage's pointer; a second lineage never overwrites the first one's pointer."""
    if key:
        newest = resolve_gen(store, PREFIX, gen)
        store.write_bytes(key, (json.dumps({"gen": newest}) + "\n").encode())


@command("add")
@option("-c", "--chunk-rows", default=CHUNK_ROWS, type=IntRange(min=1), help="A compaction's drill chunks: roots per measurement / rollup q range")
@option("-C", "--compact", "--compact-level", "compact", type=PolicyType(), default=str(COMPACT_LEVEL), envvar="STATIC_NAMES_COMPACT", show_envvar=True, help=POLICY_HELP)
@option("-F", "--read-from", help="Read the generation from this copy of the store (a dir or URL, e.g. the R2 copy of a GCS store) instead of -o (refused when it lacks a file -o has); writes still go to -o")
@option("-g", "--gen", required=True, help="The base generation (`<store>/static-names/GEN/`); one compacted into a newer one is followed to it")
@option("-G", "--grace-s", default=GRACE_S, type=IntRange(min=MIN_GRACE_S), help=f"Keep a superseded manifest's runs this many seconds (default {GRACE_S}, at least {MIN_GRACE_S})")
@option("-K", "--compact-now", is_flag=True, help="Compact into a new generation now (when the generation has runs), whatever the policy says")
@option("-L", "--layout", "layouts", multiple=True, required=True, help="Key template of the scans' `path` sorts under -s; repeat")
@option("-M", "--mem", default="4GB", help="DuckDB memory limit")
@option("-n", "--prune-dry-run", is_flag=True, help="Report the folded runs the prune would delete from the store; delete nothing")
@option("-N", "--no-merge", is_flag=True, help="Skip the due carries and compaction (a later `add` makes them)")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL, the readers' INDEX_R2 root")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-s", "--scans", "scans_root", required=True, help="Where the scans are: a dir or URL, the root of -L's keys")
@option("-t", "--through", help="Last scan id to append (inclusive; default: the newest)")
@option("-u", "--current-key", help="Publish this lineage's independent current-pointer key after append/compaction succeeds")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after); a URL store's generation is mirrored into it")
def add_cmd(chunk_rows, compact, read_from, gen, grace_s, compact_now, layouts, mem, prune_dry_run, no_merge, store_root, threads, scans_root, through, current_key, work) -> None:
    """Append every scan after the generation's newest (base + runs), oldest first, each as a run published by its
    manifest; then the binary counter's due carries. The generation's files are never rewritten; new ones go to the store
    data first, `meta.json`s next, manifests last. Then, the publish done, the runs a carry folded are deleted from the
    store, kept while a manifest superseded less than `-G` s ago lists them (`append_runner.prune_folded`; a folded run's `cdelta/` and `scans.json` stay: later
    appends refold them). When the compaction policy (`-C`) fires or `-K` forces it, the base and every run fold into a new
    generation (`static_compact`: `<gen>.c<NNN>`, a rebuild's bytes), published, then pointed to (`successor.json`, and
    `static-names/CURRENT.json` when it named this generation or nothing); the old generation stays (`store-gc -k`).
    Prints a JSON summary."""
    from .static_compact import compactor

    _check_gen(gen)
    store = Store(store_root)
    gen = resolve_gen(store, PREFIX, gen)
    prefix = f"{PREFIX}/{gen}"
    with workdir(work) as w:
        gen_dir = w / "store" / prefix
        reads, before = read_gen(store, read_from, prefix, gen_dir, label=f"add {gen}: read")
        if f"{prefix}/scans.json" not in before:
            raise SystemExit(f"{store.root}/{prefix}/scans.json: no such generation")
        base = json.loads((gen_dir / "scans.json").read_text())
        rule = sn.gen_rule(base)
        if not (gen_dir / an.ANCHORS / "meta.json").exists():
            err(f"{prefix}: no {an.ANCHORS}/meta.json: runs are appended without anchored search (a generation rebuilt without -A has it)")
        lstore = LocalRunStore(gen_dir, w / "lease", gen)
        newest = latest_key(lstore.keys("manifests/"))
        have = scans_of(base, lstore.read_json(newest)["runs"] if newest else [])
        scans = find_scans(scans_root, list(layouts), w / "scans", start=have[-1], through=through)
        pending = [s for s in scans["scans"] if s["id"] > have[-1]]
        con = sn.connect(threads, mem, w / "duckdb")
        added = [append_scan(con, gen_dir, s, scans["mount"], w, gen=gen, rule=rule, threads=threads, mem=mem, compact_level=compact.carry_level)
                 for s in pending]
        merged = None if no_merge or not pending else merge_due(gen_dir, w, gen=gen, rule=rule, compact_level=compact.carry_level)
        uploaded = upload_new(store, gen_dir, prefix, before)
        prune = prune_folded(GenStore(store, prefix), now=now(), grace_s=grace_s, retain=FOLDED_RETAIN, dry_run=prune_dry_run, log=err)
        if not prune_dry_run:
            rm_local(gen_dir, prune["keys"])  # the mirror too: a later `add` over this work dir must not upload them again
        compacted = None
        why = None if no_merge else forced(gen_dir) if compact_now else due_in(gen_dir, compact)
        if why is not None:
            compacted = compact_store(store, compactor(chunk_rows), gen, gen_dir, w, policy=compact, why=why, threads=threads, mem=mem, reads=reads)
    publish_current(store, gen, current_key)
    print(json.dumps({"gen": gen, "after": have[-1], "added": added, "merge": merged, "files": len(uploaded),
                      "prune": {k: v for k, v in prune.items() if k != "keys"}, "compact": compacted}, indent=1))
