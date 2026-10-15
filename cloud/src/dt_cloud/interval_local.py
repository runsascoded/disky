"""The interval store on one machine (`dt-cloud interval-store build` / `add`): every stage of the staged (Batch) path,
in-process and in order, over a local work dir, for deployments small enough that one machine builds a generation and
appends a scan in minutes (a GitHub Actions job, a single VM). The stages are the staged path's own functions
(`interval_store`'s range build, fold, slices, cut; `interval_append`'s per-range append, run cut, publish and carries);
this module only chains them, and moves files between the store and the work dir (`blobstore`).

- **Store** (`-o`): a local dir or an fsspec URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`), the root the readers'
  `INDEX_R2` keys are relative to: a generation lives under `interval-store/<gen>/`, as the staged path lays it out.
- **Scans** (`-s`, `-L`): a root (dir or URL) and layout templates of the scans' `path` sorts under it (`{id}` the scan id,
  `{gen}` the store generation; a scan's newest wins), as a deployment profile's `layouts`.

`build` writes a base generation (never over one): `scans.json` (written last: the reader's liveness marker),
`ranges.json`, the range files (`pv/`, `rd/`, `pvl/`, `sv/`, `svt/`, `digest/`, `sv-digest/`: every scan's reconstruction
checked, or the build fails), and the served sorts (`served/<sort>.parquet` + `.groups.parquet` + `<sort>.json`, every
sort of `interval_store.SORTS`, dyadic segments; `served/stats.json`). A generation whose scans name no owner has no owner
axis: no `sv/`, `svt/`, `sv-digest/` or slice sorts, `"slices": false` in `scans.json` (measured from the versions,
`interval_store.owners_of`); its `add` appends `pvl` alone and refuses a scan naming owners (a new base is needed).

`add` appends every scan published after the generation's newest (base + runs), oldest first, each as a run
(`deltas/<id>/`: per key range `interval_append.append_range`'s deltas and its `ranges/r####.json`, the run's served sorts,
`meta.json`), publishes its manifest (`manifests/<id>.json`), then the binary counter's due carries
(`append_runner.merge_pending` with `interval_append.carry`; revisions `manifests/<id>.m<NNN>.json`). The open versions
are refolded from the base's range files and the newest manifest's runs' deltas (`open_state_sql`), so nothing beyond the
published generation is kept between invocations (the staged path keeps them in its scratch bucket, `state/<id>/`).

With a URL store, the generation is mirrored into the work dir first (but the base's served sorts and the range files
no append reads); afterwards the new files go up, data first, liveness markers (`meta.json`, then the base's
`scans.json`) next, manifests last, so no reader sees a manifest whose runs are incomplete. A run left unpublished by an
earlier, interrupted `add` is redone whole, and its stale keys on the store replaced.

Every 2^L scans (`-C L`) `add` also compacts the base and every run into a new generation (`interval_compact`) and
moves the store's pointer (`interval-store/CURRENT.json`) to it; `add` appends to that pointer's generation by default.

The served generation is what the site's interval reader opens (`site/functions/_lib/index.ts` `openInterval`, with
`PATH_STORE`, and `INTERVAL_STORE_GEN` or the pointer): `scans.json`, `served/`, `manifests/`, and each listed run's
`served/` under `INDEX_R2`.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from time import monotonic

from click import IntRange, command, option

from . import append_runner as ar
from . import interval_append as ia
from . import interval_compact as ic
from . import interval_store as ist
from . import static_names as sn
from .append_runner import LocalRunStore, latest_key, run_key, scans_of
from .blobstore import Store, mirror, upload_new
from . import static_local as sl
from .static_local import DiskPeak, GenStore, _check_gen, find_scans, rss_peak_gb, workdir
from .static_names import OPEN, err, q

PREFIX = ist.PREFIX
#: Key ranges: one machine, so a few (DuckDB parallelizes within each).
RANGES = 8
#: What `add` never reads from the base: its served sorts, the read versions (folded into `pvl/`), the digests.
UNREAD = ("served/", "rd/", "digest/", "sv-digest/", "verify/")


def publish_order(keys: list[str]) -> list[str]:
    """The upload order: data files first, then liveness markers (a run's `meta.json`; the base's `scans.json`, which the
    reader opens the generation by), then the manifests (each lists runs that must be whole), in key order within each."""
    def rank(k: str) -> tuple:
        if k.split("/", 1)[0] == "manifests":
            return (3, k)
        if k == "scans.json":
            return (2, k)
        return (1 if k.rsplit("/", 1)[-1] == "meta.json" else 0, k)
    return sorted(keys, key=rank)


# ── build: a base generation ───────────────────────────────────────────────


def build_gen(scans: dict, mount: str, gen_dir: Path, work: Path, *, gen: str, k: int = RANGES, rg_rows: int = ist.SERVED_RG,
              threads: int = 4, mem: str = "4GB") -> dict:
    """A base generation over `scans` (`find_scans`' doc; their files under `mount`) in `gen_dir`, every stage in turn:
    ranges → path and read versions (`build_range`, checked against every scan) → folded (`fold_range`) → owner slices
    (`build_slices_range`, checked) → slices with their path's total (`slice_totals_range`) → the served sorts (`cut`,
    dyadic segments over the scans' stamps) and their churn (`stats.json`). Returns a summary (rows, and per stage its
    seconds, peak work-disk GiB and peak RSS GiB so far)."""
    t0, times, peaks = monotonic(), {}, {}
    gen_dir.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    disk = DiskPeak(work)

    def done(stage: str, t: float) -> float:
        times[stage] = round(monotonic() - t, 1)
        peaks[stage] = {"disk_gb": disk.take(), "rss_gb": rss_peak_gb()}
        err(f"build {gen}: {stage} in {times[stage]}s (work disk peak {peaks[stage]['disk_gb']} GiB, RSS peak {peaks[stage]['rss_gb']} GiB)")
        return monotonic()

    con = sn.connect(threads, mem, work / "duckdb")
    doc = {k_: v for k_, v in scans.items() if k_ != "mount"}
    t = monotonic()
    ranges = sn.plan_ranges(doc, k, mount)
    (gen_dir / "ranges.json").write_text(json.dumps(ranges, indent=1) + "\n")
    bad, digests = [], []
    for r in ranges["ranges"]:
        d = ist.build_range(doc, ranges, r["i"], gen_dir, con, mount=mount)
        digests.append(d)
        bad += [(r["i"], s["id"]) for s in d["scans"] if not (s["eq"] and s["reads_eq"])]
        ist.fold_range(str(gen_dir), r["i"], gen_dir, con)
    t = done("versions", t)
    # The owner axis, from the versions: a generation whose scans name no owner builds no slices.
    doc["slices"] = ist.owners_of(digests)
    if doc["slices"]:
        for r in ranges["ranges"]:
            d = ist.build_slices_range(doc, ranges, r["i"], gen_dir, con, mount=mount)
            bad += [(r["i"], s["id"]) for s in d["scans"] if not s["eq"]]
            ist.slice_totals_range(str(gen_dir), r["i"], gen_dir, con)
        t = done("slices", t)
    if bad:
        raise RuntimeError(f"build {gen}: the versions don't reconstruct every scan: (range, scan) {bad}")
    sorts, stats = ist.cut_gen(con, gen_dir, doc, rg_rows=rg_rows)
    done("cut", t)
    (gen_dir / "scans.json").write_text(json.dumps(doc, indent=1) + "\n")
    disk.close()
    return {"gen": gen, "scans": len(doc["scans"]), "ranges": ranges["k"], "slices": doc["slices"], "versions": stats["versions"], "open": stats["open"], "sorts": sorts,
            "s": round(monotonic() - t0, 1), "stages": times, "peaks": peaks}


# ── add: scans appended as runs ────────────────────────────────────────────


def open_state_sql(root: str, runs: list[str], name: str, sliced: bool = True) -> dict[str, str]:
    """Key range `name`'s open versions after the newest of `runs` (run dirs, the manifest's order), refolded: the base's
    (`interval_append.base_state_sql` over the range files under `root`) and every run's delta rows (opened versions and
    close records, `pvf` carried), one row per version (`IDENT`) with the smallest `vt`, those still open. What the staged
    path reads from `state/<prev>/`, as `STATE_SCHEMA` relations (`interval_append.tables(sliced)`)."""
    base = ia.base_state_sql(root, name, sliced)
    out = {}
    for t in ia.tables(sliced):
        cols = ", ".join(ia.STATE_SCHEMA[t].names)
        parts = [f"SELECT {cols} FROM ({base[t]})", *(f"SELECT {cols} FROM read_parquet({q(f'{r}/{t}/{name}.parquet')})" for r in runs)]
        if len(parts) == 1:
            out[t] = parts[0]
            continue
        ident = ", ".join(ia.IDENT[t])
        out[t] = f"""SELECT {cols} FROM (SELECT * FROM ({" UNION ALL ".join(parts)})
            QUALIFY row_number() OVER (PARTITION BY {ident} ORDER BY vt) = 1) WHERE vt = {OPEN}"""
    return out


def append_scan(con, gen_dir: Path, scan: dict, mount: str, work: Path, *, gen: str, rg_rows: int = ist.SERVED_RG) -> dict:
    """Scan `scan` appended to the generation in `gen_dir` (the base, its runs and manifests) as the run `deltas/<id>/`, and
    published (`manifests/<id>.json`): per key range `append_range` over the refolded open versions (`open_state_sql`),
    the run's served sorts (`cut_run`) and `meta.json`, then `publish_run`."""
    t0 = monotonic()
    disk = DiskPeak(work)
    d = scan["id"]
    store = LocalRunStore(gen_dir, work / "lease", gen)
    base = store.read_json("scans.json")
    sliced = ist.sliced(base)
    prev = latest_key(store.keys("manifests/"), before=d)
    runs = store.read_json(prev)["runs"] if prev else []
    have = scans_of(base, runs)
    if d <= have[-1]:
        raise SystemExit(f"{d}: not after the generation's newest scan {have[-1]}")
    if scan["ts"] <= max(x["ts"] for x in base["scans"]):
        raise SystemExit(f"{d}: its stamp {scan['ts']} is not past the generation's")
    rk = run_key(d, d)
    run = gen_dir / rk
    if run.exists():
        shutil.rmtree(run)  # an unpublished run (no manifest lists it): redo it whole
    run.mkdir(parents=True)
    (run / "scans.json").write_text(json.dumps({"bucket": base["bucket"], "scans": [scan]}, indent=1) + "\n")
    ranges = store.read_json("ranges.json")
    roots = [str(gen_dir / r["key"]) for r in runs]
    for r in ranges["ranges"]:
        name = f"r{r['i']:04d}"
        doc = ia.append_range(con, open_state_sql(str(gen_dir), roots, name, sliced), scan, r, name, run, bucket=base["bucket"], mount=mount,
                              sliced=sliced)
        doc["prev"] = have[-1]
        (run / "ranges").mkdir(exist_ok=True)
        (run / "ranges" / f"{name}.json").write_text(json.dumps(doc) + "\n")
    shutil.rmtree(run / "state")
    t1 = monotonic()
    docs = ia.cut_run(con, str(run), run / "served", rg_rows=rg_rows, sliced=sliced)
    new = {"key": rk, "first": d, "last": d, "level": 0, "scans": [d]}
    meta = {**ia.run_meta(gen, new, {d: scan["ts"]}, docs), "cut_s": round(monotonic() - t1, 1)}
    (run / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    manifest = ia.publish_run(store, d)
    peak = {"disk_gb": disk.close(), "rss_gb": rss_peak_gb()}
    err(f"add {d}: published {len(manifest['runs'])} runs in {monotonic() - t0:.1f}s (work disk peak {peak['disk_gb']} GiB, RSS peak {peak['rss_gb']} GiB)")
    return {"scan": d, "rows": meta["rows"], "bytes": meta["bytes"], "runs": [r["key"] for r in manifest["runs"]], "s": round(monotonic() - t0, 1),
            "peak": peak}


def merge_due(gen_dir: Path, work: Path, *, gen: str, k: int, compact_level: int | None, rg_rows: int, threads: int, mem: str) -> dict:
    """The newest manifest's due carries (`append_runner.merge_pending` with `interval_append.carry`, over the generation
    dir), each published as a revision of it."""
    store = LocalRunStore(gen_dir, work / "lease", gen)
    carry = ia.carry(gen, k, threads=threads, mem=mem, rg_rows=rg_rows, sliced=ist.sliced(store.read_json("scans.json")))
    return ar.merge_pending(store, carry, gen_dir, tmp=work / "merge", max_level=compact_level, heartbeat_s=None)


# ── CLI ────────────────────────────────────────────────────────────────────


@command("build")
@option("-g", "--gen", required=True, help="The generation to write: `<store>/interval-store/GEN/` (never over an existing one)")
@option("-k", "--ranges", "k", default=RANGES, type=IntRange(min=1), help="Key ranges (each one DuckDB pass over every scan)")
@option("-L", "--layout", "layouts", multiple=True, required=True, help="Key template of the scans' `path` sorts under -s (`{id}` the scan id, `{gen}` the store generation); repeat")
@option("-M", "--mem", default="4GB", help="DuckDB memory limit")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`), the readers' INDEX_R2 root")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-r", "--rg-rows", default=ist.SERVED_RG, type=IntRange(min=1), help="Rows per served row group")
@option("-s", "--scans", "scans_root", required=True, help="Where the scans are: a dir or URL, the root of -L's keys")
@option("-t", "--through", help="Last scan id (inclusive; default: the newest)")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after)")
def build_cmd(gen, k, layouts, mem, store_root, threads, rg_rows, scans_root, through, work) -> None:
    """Build a base generation of the interval store on this machine, every stage in-process: the staged (Batch) path's
    stages over a local work dir, then the generation copied into the store (data first, `scans.json` last). Prints a
    JSON summary."""
    _check_gen(gen)
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
        summary = build_gen(scans, scans["mount"], gen_dir, w / "build", gen=gen, k=k, rg_rows=rg_rows, threads=threads, mem=mem)
        summary["files"] = len(upload_new(store, gen_dir, prefix, {}, publish_order))
    if ic.current(store) is None:
        ic.set_current(store, gen)  # the store's first generation: what unpinned readers and `add` follow
        summary["current"] = True
    print(json.dumps(summary, indent=1))


@command("add")
@option("-C", "--compact-level", type=ar.Level(), default=ar.COMPACT_LEVEL, envvar="INTERVAL_STORE_COMPACT_LEVEL", show_envvar=True,
        help=f"Compaction level L: carries stop below it, and once two runs reach L − 1 (every 2^L scans) the base and every run fold into a new generation; `none`: unbounded carries, never compacted (default {ar.COMPACT_LEVEL})")
@option("-g", "--gen", help="The base generation (`<store>/interval-store/GEN/`); default the store's current (`interval-store/CURRENT.json`, else its only generation, adopted). One compacted into a newer one is followed to it")
@option("-G", "--grace-s", default=ar.GRACE_S, type=IntRange(min=ar.MIN_GRACE_S), help=f"Keep a superseded manifest's runs, and a compacted generation, this many seconds (default {ar.GRACE_S}, at least {ar.MIN_GRACE_S})")
@option("-L", "--layout", "layouts", multiple=True, required=True, help="Key template of the scans' `path` sorts under -s; repeat")
@option("-M", "--mem", default="4GB", help="DuckDB memory limit")
@option("-n", "--prune-dry-run", is_flag=True, help="Report the folded runs and compacted generations the prune would delete from the store; delete nothing")
@option("-N", "--no-merge", is_flag=True, help="Skip the due carries and compaction (a later `add` makes them)")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL, the readers' INDEX_R2 root")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-r", "--rg-rows", default=ist.SERVED_RG, type=IntRange(min=1), help="Rows per served row group (the runs', and a compacted generation's)")
@option("-s", "--scans", "scans_root", required=True, help="Where the scans are: a dir or URL, the root of -L's keys")
@option("-t", "--through", help="Last scan id to append (inclusive; default: the newest)")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after); a URL store's generation is mirrored into it")
def add_cmd(compact_level, gen, grace_s, layouts, mem, prune_dry_run, no_merge, store_root, threads, rg_rows, scans_root, through,
            work) -> None:
    """Append every scan after the generation's newest (base + runs), oldest first, each as a run published by its
    manifest; then the binary counter's due carries. The generation's published files are never rewritten; new ones go
    to the store data first, `meta.json`s next, manifests last. Then the runs a carry folded are deleted from the store
    (`append_runner.prune_folded`, `-G` grace; a folded run keeps `interval_append.FOLDED_RETAIN`). When the compaction
    level is due, the base and every run fold into a new generation (`interval_compact`), published, then pointed to
    (`successor.json`, `interval-store/CURRENT.json`); a compacted generation leaves the store `-G` s later. Prints a JSON
    summary."""
    store = Store(store_root)
    if gen is not None:
        _check_gen(gen)
    gen = ic.resolve_gen(store, gen)
    prefix = f"{PREFIX}/{gen}"
    with workdir(work) as w:
        gen_dir = w / "store" / prefix
        before = mirror(store, prefix, gen_dir, keep=lambda rel: not rel.startswith(UNREAD))
        if f"{prefix}/scans.json" not in before:
            raise SystemExit(f"{store.root}/{prefix}/scans.json: no such generation")
        base = json.loads((gen_dir / "scans.json").read_text())
        lstore = LocalRunStore(gen_dir, w / "lease", gen)
        newest = latest_key(lstore.keys("manifests/"))
        have = scans_of(base, lstore.read_json(newest)["runs"] if newest else [])
        scans = find_scans(scans_root, list(layouts), w / "scans", start=have[-1], through=through)
        pending = [s for s in scans["scans"] if s["id"] > have[-1]]
        # A pending scan's run dir on the store is an earlier, unpublished attempt: redone whole, its keys replaced.
        redo = tuple(f"{prefix}/{run_key(s['id'], s['id'])}/" for s in pending)
        stale = sorted(key for key in before if key.startswith(redo)) if redo else []
        before = {key: n for key, n in before.items() if not (redo and key.startswith(redo))}
        con = sn.connect(threads, mem, w / "duckdb")
        if not ist.sliced(base):
            # No owner axis in the generation: a pending scan naming owners is refused before anything is appended.
            for s in pending:
                if ist.names_owners(con, sn._src(base["bucket"], s["src"], scans["mount"])):
                    raise SystemExit(ist.owners_refused(s["id"]))
        added = [append_scan(con, gen_dir, s, scans["mount"], w, gen=gen, rg_rows=rg_rows) for s in pending]
        k = lstore.read_json("ranges.json")["k"]
        merged = None if no_merge or not pending else merge_due(gen_dir, w, gen=gen, k=k, compact_level=compact_level, rg_rows=rg_rows,
                                                                threads=threads, mem=mem)
        ours = {f"{prefix}/{p.relative_to(gen_dir).as_posix()}" for p in gen_dir.rglob("*") if p.is_file()}
        store.delete([key for key in stale if key not in ours])
        uploaded = upload_new(store, gen_dir, prefix, before, publish_order)
        prune = ar.prune_folded(GenStore(store, prefix), now=sl.now(), grace_s=grace_s, retain=ia.FOLDED_RETAIN, dry_run=prune_dry_run, log=err)
        if not prune_dry_run:
            ar.rm_local(gen_dir, prune["keys"])  # the mirror too: a later `add` over this work dir must not upload them again
        # Compaction runs whatever was pending: an interrupted one is redone by the next `add`.
        compacted = None
        if not no_merge and ic.due(gen_dir, compact_level):
            compacted = compact(store, gen, gen_dir, w, level=compact_level, rg_rows=rg_rows, threads=threads, mem=mem)
        preds = ic.prune_predecessors(store, gen, now=sl.now(), grace_s=grace_s, dry_run=prune_dry_run)
    print(json.dumps({"gen": gen, "after": have[-1], "added": added, "merge": merged, "files": len(uploaded),
                      "prune": {k_: v for k_, v in prune.items() if k_ != "keys"}, "compact": compacted, "predecessors": preds}, indent=1))


def compact(store: Store, gen: str, gen_dir: Path, work: Path, *, level: int | None, rg_rows: int, threads: int, mem: str) -> dict:
    """Generation `gen` (published, mirrored in `gen_dir`) folded into the next one (`interval_compact.compact_gen`) and
    published: its files (an earlier, unpublished attempt's keys replaced; data first, `scans.json` last), then
    `successor.json` in `gen`, then `CURRENT.json` when it named `gen` (or nothing)."""
    new = ic.next_gen(gen)
    new_prefix = f"{PREFIX}/{new}"
    if stale := store.listing(new_prefix):
        # Not yet recorded as `gen`'s successor (`resolve_gen` would have followed it): an interrupted compaction's.
        err(f"compact {gen}: {len(stale)} objects of an unpublished {new}: replaced")
        store.delete(sorted(stale))
    out = work / "store" / new_prefix
    t0, disk = monotonic(), DiskPeak(work)
    summary = ic.compact_gen(gen_dir, out, work, gen=gen, new_gen=new, level=level, rg_rows=rg_rows, threads=threads, mem=mem)
    summary["files"] = len(upload_new(store, out, new_prefix, {}, publish_order))
    summary["s"] = round(monotonic() - t0, 1)
    summary["peak"] = {"disk_gb": disk.close(), "rss_gb": rss_peak_gb()}
    err(f"compact {gen} → {new}: {summary['files']} files in {summary['s']}s (work disk peak {summary['peak']['disk_gb']} GiB, "
        f"RSS peak {summary['peak']['rss_gb']} GiB)")
    store.write_text(f"{PREFIX}/{gen}/{ic.SUCCESSOR}", json.dumps({"gen": new}) + "\n")
    summary["current"] = ic.current(store) in (None, gen)
    if summary["current"]:
        ic.set_current(store, new)
    return summary


@command("current")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL, the readers' INDEX_R2 root")
@option("-s", "--set", "to", help="Point the store at this generation instead (it must exist)")
def current_cmd(store_root, to) -> None:
    """Print the store's current generation: `interval-store/CURRENT.json`'s, else its only generation; nothing when it
    has none; exit 1 when it has several and no pointer. With `-s`, set the pointer (what unpinned readers serve and `add`
    appends to)."""
    store = Store(store_root)
    if to is not None:
        _check_gen(to)
        if not store.exists(f"{PREFIX}/{to}/scans.json"):
            raise SystemExit(f"{store.root}/{PREFIX}/{to}/scans.json: no such generation")
        ic.set_current(store, to)
    gen, how = ic.resolve(store)
    if gen is not None:
        print(gen)
    elif how == "ambiguous":
        raise SystemExit(f"{store.root}/{PREFIX}: several generations and no {ic.CURRENT}: name one with `-s`")
    else:
        err(f"{store.root}/{PREFIX}: no generation")
