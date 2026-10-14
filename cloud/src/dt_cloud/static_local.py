"""The static name index on one machine (`dt-cloud static-names build` / `add`): every stage of the staged (Batch) path,
in-process and in order, over a local work dir, for deployments small enough that one machine builds a generation in
minutes. The stages are the staged path's own functions (`static_names`, `static_catalog`, `static_roots`, `static_drill`,
`static_append`, `static_merge`); this module only chains them, and moves files between the store and the work dir.

- **Store** (`-o`): a local dir or an fsspec URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`), the root the readers'
  `INDEX_R2` keys are relative to: a generation lives under `static-names/<gen>/`, as the staged path lays it out.
- **Scans** (`-s`, `-L`): a root (dir or URL) and layout templates of the scans' `path` sorts under it (`{id}` the scan id,
  `{gen}` the store generation; a scan's newest wins), as a deployment profile's `layouts`.

`build` writes a base generation (never over one): `scans.json`, `ranges.json`, the coalesced versions (`cintervals/`,
`chist/`), the suffix shards (`shards.json`, `sx/`, `sidecar/`, `sidecar.parquet`), the catalog (`catalog/`), and the
heavy-term drilldown (`drill/`, `roots-measure/`). Anchored search (`names/`, `anchors/`) is not built: readers decline
anchored terms on a generation without `anchors/meta.json`.

`add` appends every scan published after the generation's newest (base + runs), oldest first, each as a run
(`deltas/<id>/`: `static_append`'s per-scan append, shards, catalog delta and the drill's run), publishes its manifest
(`manifests/<id>.json`, written last), then the binary counter's due carries (`static_merge`; revisions
`manifests/<id>.m<NNN>.json`). The open-version state is refolded from the base's `cintervals/` and the runs' `cdelta/`
(`static_append.rebuild_open`), so nothing beyond the published runs is kept between invocations.

With a URL store, the generation's files are mirrored into the work dir first; afterwards the new files go up, every
run's files before its liveness markers (`meta.json`s), and the manifests last, so no reader sees a manifest whose runs
are incomplete.
"""
from __future__ import annotations

import json
import resource
import shutil
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

import pyarrow.parquet as pq
from click import IntRange, command, option

from . import static_append as sa
from . import static_catalog as sc
from . import static_drill as sd
from . import static_names as sn
from . import static_roots as sr
from .append_runner import COMPACT_LEVEL, LocalRunStore, latest_key, run_key, scans_of
from .hex_runs import parse_rule, rule_json
from .static_names import PREFIX, err, q

#: Ranges, shard rows: one machine, so a few ranges (DuckDB parallelizes within each) and the staged path's shard size.
RANGES = 8
SHARD_ROWS = sa.RUN_SHARD_ROWS
#: The catalog's membership bound V (the staged path's), the drill's R and K (`static-names drill build`'s defaults),
#: and the measurement floor (`roots measure -f`).
MAX_ROWS = 100_000
R_ROWS, K_KEPT, FLOOR = 100_000, 256, 10_000


# ── Resource peaks: what a job sizing needs, reported by every run ─────────


GB = 1 << 30


def rss_peak_gb() -> float:
    """This process's peak RSS so far (`ru_maxrss`: bytes on macOS, KiB on Linux)."""
    m = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(m / (GB if sys.platform == "darwin" else GB >> 10), 2)


class DiskPeak:
    """Peak bytes used on the work dir's filesystem above its level at start, sampled every `every_s` on a daemon
    thread (DuckDB's spill and the stages' intermediates land there between samples, so a stage's peak is a lower
    bound to within one interval). `take()` returns the peak since the previous `take()` (one stage) and resets it.
    The filesystem is the job's own scratch, so other writers' bytes count too; on a shared disk this over-states."""

    def __init__(self, path: Path, every_s: float = 2.0):
        self.path, self.every_s = path, every_s
        self.base = shutil.disk_usage(path).used
        self.peak = self.total = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def sample(self) -> None:
        used = max(0, shutil.disk_usage(self.path).used - self.base)
        self.peak, self.total = max(self.peak, used), max(self.total, used)

    def run(self) -> None:
        while not self.stop.wait(self.every_s):
            self.sample()

    def take(self) -> float:
        self.sample()
        peak, self.peak = self.peak, 0
        return round(peak / GB, 2)

    def close(self) -> float:
        self.stop.set()
        self.thread.join()
        self.sample()
        return round(self.total / GB, 2)


# ── The store: a local dir or an fsspec URL ────────────────────────────────


class Store:
    """The readers' key space: `root` a local dir or a URL. `listing(prefix)` is `{key: size}`."""

    def __init__(self, root: str):
        from disk_tree.blobfs import fs_for, is_url

        self.root = root.rstrip("/")
        self.url = is_url(root)
        if self.url:
            self.fs, self.base = fs_for(self.root)
            self.base = self.base.rstrip("/")

    def listing(self, prefix: str) -> dict[str, int]:
        if not self.url:
            d = Path(self.root) / prefix
            return {p.relative_to(self.root).as_posix(): p.stat().st_size for p in sorted(d.rglob("*")) if p.is_file()} if d.is_dir() else {}
        found = self.fs.find(f"{self.base}/{prefix}", detail=True)
        return {k[len(self.base) + 1:]: int(v["size"]) for k, v in sorted(found.items()) if v.get("type", "file") == "file"}

    def exists(self, key: str) -> bool:
        return (Path(self.root) / key).is_file() if not self.url else self.fs.exists(f"{self.base}/{key}")

    def get(self, key: str, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if self.url:
            self.fs.get(f"{self.base}/{key}", str(dst))
        else:
            shutil.copyfile(Path(self.root) / key, dst)

    def put(self, src: Path, key: str) -> None:
        if self.url:
            self.fs.put(str(src), f"{self.base}/{key}")
        else:
            dst = Path(self.root) / key
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)


def publish_order(keys: list[str]) -> list[str]:
    """The upload order: data files first, then liveness markers (`meta.json`, the drill's last: a run is a drill tier iff
    its `drill/meta.json` exists), then the manifests (each lists runs that must be whole), in key order within each."""
    def rank(k: str) -> tuple:
        if k.split("/", 1)[0] == "manifests":
            return (3, k)
        name = k.rsplit("/", 1)[-1]
        if name == "meta.json":
            return (2 if k.endswith("drill/meta.json") else 1, k)
        return (0, k)
    return sorted(keys, key=rank)


def upload_new(store: Store, local: Path, prefix: str, before: dict[str, int]) -> list[str]:
    """Every file under `local` (the mirror of `prefix`) not in `before` → `store`, in `publish_order`. Files in `before`
    are never rewritten (a generation's files are written once)."""
    keys = [p.relative_to(local).as_posix() for p in local.rglob("*") if p.is_file()]
    new = publish_order([k for k in keys if f"{prefix}/{k}" not in before])
    for k in new:
        store.put(local / k, f"{prefix}/{k}")
    return new


def mirror(store: Store, prefix: str, local: Path) -> dict[str, int]:
    """`prefix` (a generation) copied into `local` (files already there with the same size are kept); returns the listing."""
    listing = store.listing(prefix)
    for key, size in listing.items():
        dst = local / key[len(prefix) + 1:]
        if not (dst.is_file() and dst.stat().st_size == size):
            store.get(key, dst)
    return listing


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
              max_rows: int = MAX_ROWS, R: int = R_ROWS, K: int = K_KEPT, floor: int = FLOOR, threads: int = 4, mem: str = "4GB") -> dict:
    """A base generation over `scans` (`scans.json`'s doc; their files under `mount`) in `gen_dir`, every stage in turn:
    ranges → coalesced versions → suffix map → shards → sidecar → census → members → cells → short literals → catalog
    → drill. Returns a summary (rows, files, and per stage its seconds, peak work-disk GiB and peak RSS GiB so far)."""
    t0, times, peaks = monotonic(), {}, {}
    gen_dir.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    disk = DiskPeak(work)

    def done(stage: str, t: float) -> float:
        times[stage] = round(monotonic() - t, 1)
        peaks[stage] = {"disk_gb": disk.take(), "rss_gb": rss_peak_gb()}
        err(f"build {gen}: {stage} in {times[stage]}s (work disk peak {peaks[stage]['disk_gb']} GiB, RSS peak {peaks[stage]['rss_gb']} GiB)")
        return monotonic()

    tmp = work / "duckdb"
    con = sn.connect(threads, mem, tmp)
    doc = {**{k_: v for k_, v in scans.items() if k_ != "mount"}, **rule_json(rule)}
    (gen_dir / "scans.json").write_text(json.dumps(doc, indent=1) + "\n")
    t = monotonic()
    ranges = sn.plan_ranges(doc, k, mount)
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
    drill = build_base_drill(con, gen_dir, R=R, K=K, floor=floor, rule=rule)
    done("drill", t)
    disk.close()
    return {"gen": gen, "scans": len(doc["scans"]), "ranges": k, "suffix_rows": int(sum(side.column("rows").to_pylist())),
            "shards": len(shard_names), "members": members["members"], "cells_rows": meta["cells_rows"], "drill": drill,
            "s": round(monotonic() - t0, 1), "stages": times, "peaks": peaks}


def build_base_drill(con, gen_dir: Path, *, R: int, K: int, floor: int, rule) -> dict:
    """The base generation's `drill/` (`static_drill.build_drill`: every member's roots and rollups, in the layout the runs'
    drills share) and its measurement (`roots-measure/`: per member, the directories with at least `floor` root rows, which
    a run's drill reads to decide heaviness), from the generation's `sx/`, `catalog/members.parquet` and `cintervals/`.
    Long members with equal root sets share their least member's rows (`aliases.parquet`, by root-set digest, as
    `roots digest` → `roots alias-plan`)."""
    sx = "[" + ", ".join(q(str(p)) for p in sorted((gen_dir / "sx").glob("*.parquet"))) + "]"
    members = str(gen_dir / "catalog" / "members.parquet")
    con.execute(f"CREATE OR REPLACE TABLE mem AS SELECT q, rows FROM read_parquet({q(members)})")
    con.execute(f"DROP TABLE IF EXISTS dg; CREATE TABLE dg ({sr.DIGEST_COLS})")
    rows = sr.sx_rows_sql(f"read_parquet({sx})")
    if con.execute("SELECT count(*) FROM mem").fetchone()[0]:
        sr.member_roots(con, rows, "mem", "dg", agg="digest", rule=rule)
    con.execute("""CREATE OR REPLACE TABLE dgq AS SELECT q, sum(n)::BIGINT AS n, (sum(h1) % 18446744073709551616)::UBIGINT AS h1,
        (sum(h2) % 18446744073709551616)::UBIGINT AS h2 FROM dg GROUP BY q""")
    con.execute("CREATE OR REPLACE TABLE al AS SELECT q, min(q) OVER (PARTITION BY n, h1, h2) AS canonical FROM dgq")
    missing = con.execute("SELECT count(*) FROM mem ANTI JOIN al USING (q)").fetchone()[0]
    if missing:
        raise RuntimeError(f"{missing} members without a root-set digest")
    aliases = con.execute("SELECT q, canonical FROM al ORDER BY q").to_arrow_table()
    con.execute("CREATE OR REPLACE TABLE canon AS SELECT DISTINCT canonical AS q FROM al")
    con.execute("DROP TABLE IF EXISTS lcan; DROP TABLE IF EXISTS sall")
    sr.member_roots(con, rows, "canon", "lcan", rule=rule)
    ci = "[" + ", ".join(q(str(p)) for p in sorted((gen_dir / "cintervals").glob("*.parquet"))) + "]"
    sr.short_roots(con, f"SELECT * FROM read_parquet({ci})", "sall", rule=rule)
    meas = gen_dir / "roots-measure"
    for kind, table, sub in (("long", "lcan", "dirs"), ("short", "sall", "short/dirs")):
        con.execute(f"""CREATE OR REPLACE TABLE rp AS SELECT q, depth, path, count(*) AS n, count(*) FILTER (WHERE vt = {sn.OPEN}) AS n_open
            FROM {table} GROUP BY q, depth, path""")
        dirs, _, _ = sr.dir_stats(con, "rp", floor)
        con.register("dirs_in", dirs.select(["q", "k", "dir", "rows"]))
        # every long member's (as `roots measure` measures them): an alias's roots, so its directories, are its canonical's
        expand = "SELECT al.q, d.k, d.dir, d.rows FROM dirs_in d JOIN al ON al.canonical = d.q" if kind == "long" else "SELECT * FROM dirs_in"
        dirs = con.execute(f"SELECT * FROM ({expand}) ORDER BY q, k, dir").to_arrow_table()
        con.unregister("dirs_in")
        (meas / sub).mkdir(parents=True, exist_ok=True)
        pq.write_table(dirs, meas / sub / "s0000.parquet", compression=sn.CODEC)
    meta = sd.build_drill(con, gen_dir / "drill", long_roots="lcan", short_roots="sall", aliases=aliases, R=R, K=K,
                          meta={"floor": floor})
    for t in ("dg", "dgq", "al", "canon", "lcan", "sall", "rp", "mem"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    return {"members": aliases.num_rows, "canonical": len(set(aliases.column("canonical").to_pylist())), **{k: meta[k] for k in ("R", "K")}}


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
    drill = run_drill(con, gen_dir, run, scan, runs, days, gen=gen, rule=rule)
    manifest = sa.publish_run(store, d, rule, max_level=compact_level)
    peak = {"disk_gb": disk.close(), "rss_gb": rss_peak_gb()}
    err(f"add {d}: published {len(manifest['runs'])} runs in {monotonic() - t0:.1f}s (work disk peak {peak['disk_gb']} GiB, RSS peak {peak['rss_gb']} GiB)")
    return {"scan": d, "drill": drill, "runs": [r["key"] for r in manifest["runs"]], "s": round(monotonic() - t0, 1), "peak": peak}


def run_drill(con, gen_dir: Path, run: Path, scan: dict, runs: list[dict], days: list[str], *, gen: str, rule) -> dict:
    """The run's `drill/` over the base's and the live runs' drills (`static-names drill build`'s inputs, from `gen_dir`)."""
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
                        rule=rule, tier={"gen": gen, "first": d, "last": d, "level": 0, "scans": [d], "prior": [t.name for t in prior]})
    con.execute("DROP TABLE IF EXISTS pnew")
    return {"new_members": len(new_members), "rows": {k: v for k, v in meta.items() if k.endswith("_rows")}}


def merge_due(gen_dir: Path, work: Path, *, gen: str, rule, compact_level: int | None) -> dict:
    """The newest manifest's due carries (`static_merge.merge_pending` over the generation dir), each published as a
    revision of it."""
    from .static_merge import merge_pending

    store = LocalRunStore(gen_dir, work / "lease", gen)
    return merge_pending(store, gen_dir, tmp=work / "merge", rule=rule, max_level=compact_level, heartbeat_s=None)


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
@option("-f", "--floor", default=FLOOR, type=IntRange(min=1), help="The drill's measurement floor (root rows under a directory)")
@option("-g", "--gen", required=True, help="The generation to write: `<store>/static-names/GEN/` (never over an existing one)")
@option("-k", "--ranges", "k", default=RANGES, type=IntRange(min=1), help="Key ranges (each one DuckDB pass over every scan)")
@option("-K", "--keep", "K", default=K_KEPT, type=IntRange(min=1), help="The drill's kept children per heavy directory")
@option("-L", "--layout", "layouts", multiple=True, required=True, help="Key template of the scans' `path` sorts under -s (`{id}` the scan id, `{gen}` the store generation); repeat")
@option("-M", "--mem", default="4GB", help="DuckDB memory limit")
@option("-n", "--shard-rows", default=SHARD_ROWS, type=IntRange(min=1), help="Suffix rows per shard file")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (`r2://bucket[/prefix]`, `s3://…`), the readers' INDEX_R2 root")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-R", "--read-rows", "R", default=R_ROWS, type=IntRange(min=1), help="The drill's heavy bound: more root rows under a directory")
@option("-s", "--scans", "scans_root", required=True, help="Where the scans are: a dir or URL, the root of -L's keys")
@option("-t", "--through", help="Last scan id (inclusive; default: the newest)")
@option("-V", "--max-rows", default=MAX_ROWS, type=IntRange(min=1), help="The catalog's membership bound V (range rows)")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after)")
@option("-x", "--hex-runs", required=True, help="The hex-run rule the generation is built with: `MIN,TAIL` (e.g. `16,8`) or `off`")
def build_cmd(floor, gen, k, K, layouts, mem, shard_rows, store_root, threads, R, scans_root, through, max_rows, work, hex_runs) -> None:
    """Build a base generation of the static name index on this machine, every stage in-process: the staged (Batch)
    path's stages over a local work dir, then the generation copied into the store (data files first, `meta.json`s last).
    Prints a JSON summary."""
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
                            R=R, K=K, floor=floor, threads=threads, mem=mem)
        summary["files"] = len(upload_new(store, gen_dir, prefix, {}))
    print(json.dumps(summary, indent=1))


@command("add")
@option("-C", "--compact-level", default=COMPACT_LEVEL, type=IntRange(min=1), help="The binary counter's compaction level (carries stop below it)")
@option("-g", "--gen", required=True, help="The base generation (`<store>/static-names/GEN/`)")
@option("-L", "--layout", "layouts", multiple=True, required=True, help="Key template of the scans' `path` sorts under -s; repeat")
@option("-M", "--mem", default="4GB", help="DuckDB memory limit")
@option("-N", "--no-merge", is_flag=True, help="Skip the due carries (a later `add` merges them)")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL, the readers' INDEX_R2 root")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-s", "--scans", "scans_root", required=True, help="Where the scans are: a dir or URL, the root of -L's keys")
@option("-t", "--through", help="Last scan id to append (inclusive; default: the newest)")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after); a URL store's generation is mirrored into it")
def add_cmd(compact_level, gen, layouts, mem, no_merge, store_root, threads, scans_root, through, work) -> None:
    """Append every scan after the generation's newest (base + runs), oldest first, each as a run published by its
    manifest; then the binary counter's due carries. The generation's files are never rewritten; new ones go to the store
    data first, `meta.json`s next, manifests last. Prints a JSON summary."""
    _check_gen(gen)
    store = Store(store_root)
    prefix = f"{PREFIX}/{gen}"
    with workdir(work) as w:
        gen_dir = w / "store" / prefix
        before = mirror(store, prefix, gen_dir)
        if f"{prefix}/scans.json" not in before:
            raise SystemExit(f"{store.root}/{prefix}/scans.json: no such generation")
        base = json.loads((gen_dir / "scans.json").read_text())
        rule = sn.gen_rule(base)
        lstore = LocalRunStore(gen_dir, w / "lease", gen)
        newest = latest_key(lstore.keys("manifests/"))
        have = scans_of(base, lstore.read_json(newest)["runs"] if newest else [])
        scans = find_scans(scans_root, list(layouts), w / "scans", start=have[-1], through=through)
        pending = [s for s in scans["scans"] if s["id"] > have[-1]]
        con = sn.connect(threads, mem, w / "duckdb")
        added = [append_scan(con, gen_dir, s, scans["mount"], w, gen=gen, rule=rule, threads=threads, mem=mem, compact_level=compact_level)
                 for s in pending]
        merged = None if no_merge or not pending else merge_due(gen_dir, w, gen=gen, rule=rule, compact_level=compact_level)
        uploaded = upload_new(store, gen_dir, prefix, before)
    print(json.dumps({"gen": gen, "after": have[-1], "added": added, "merge": merged, "files": len(uploaded)}, indent=1))
