"""Deferred carries for the static name index's runs (specs/done/static-append.md, "Deferred carries"): the static store's
merged-run build plugged into the shared runner's carries (`append_runner`: `plan_carries`, `merge_pending`,
`publish_revision`, the lease, revision manifests `manifests/<id>.m<NNN>.json`).

Once a revision is published, the runs it folded are deleted (`append_runner.prune_folded`: a run listed by
no manifest but the newest and those superseded less than `GRACE_S`, 1 h, ago): on GCS by the merge job (`carry`) and the chain's prune stage,
each folded run keeping `FOLDED_RETAIN` (what later appends refold), and on R2 by the R2 job (`prune -r`), after the
manifest's copy.

A scan's `publish` only adds its own level-0 run and writes `manifests/<id>.json`. The binary counter's carries run apart
(`dt-cloud static-names runs merge`, or the `merge` stage `runs add` submits after R2, non-fatal): each merged run is
built here (`build_merged_run`: shards, catalog, drill, names + anchors, each tier in its own process), and published as a
revision of the newest manifest. A revision is refused while a run it lists lacks a reader file (`static_append`'s rule
1), or while its drill would cover fewer scans than the manifest it revises (rule 3); drilled runs merge only with each
other (`plan_carries`' drill parity).
"""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from faulthandler import enable
from pathlib import Path
from time import monotonic, sleep

from click import Choice, IntRange, UsageError, argument, group, option

from . import append_runner as ar
from .append_runner import (  # noqa: F401 — the shared names, as this module has always offered them
    LEASE_S,
    MANIFEST,
    REV_DIGITS,
    Carry,
    LocalRunStore,
    Published,
    RunStore,
    Superseded,
    lease_stale,
    manifest_keys,
    manifest_name,
    newest,
    parse_manifest,
    plan_carries,
    rebase,
)
from .maint_runs import NULL, Recorder, from_env
from .static_names import PREFIX, connect, err, q
from .static_profile import data_bucket, scratch_bucket
from .resource_monitor import monitor_resources, resource_stage

#: The fraction of the machine's memory the concurrent tier merges' DuckDB limits sum to.
MEM_FRACTION = 0.75
#: Memory set aside per streaming (non-DuckDB) tier merge: `pyrmts.runs` holds a few batches per input.
STREAM_RESERVE = 4 << 30
#: Memory per `static_append.merge_shards` worker process (peak ≈1 GiB measured merging 7 real inputs, 2026-10-22).
SHARD_JOB_MEM = 3 << 29
#: Tiers merged by DuckDB (each gets a memory limit and threads); the others stream through `pyrmts.runs`.
DUCKDB_TIERS = ("drill", "anchors")
#: What a folded run keeps where scans are appended (GCS, `static-names add`'s store; `append_runner.plan_prune`'s
#: `retain`): every later scan's append refolds the open versions, and cuts its catalog delta and drill, from every
#: earlier scan's `cdelta/` (`rebuild_open`, `catalog_delta`, `pnew_sql`). R2 keeps none of it (nothing appends there).
FOLDED_RETAIN = ("cdelta/", "scans.json")


def gcs_store(bucket: str, scratch: str | None, gen: str, client=None) -> ar.GcsRunStore:
    """`append_runner.GcsRunStore` over `static-names/<gen>/`."""
    return ar.GcsRunStore(bucket, scratch, gen, prefix=PREFIX, client=client)


# ── Building a merged run ──────────────────────────────────────────────────


#: How often a merge logs the tiers still running (`build_merged_run`).
TICK_S = 30.0


def tier_resources(tiers: list[str], ram: int, cpus: int, concurrent: bool) -> dict[str, dict]:
    """Each DuckDB tier merge's `mem` (GB, whole) and `threads`, and the shards merge's worker processes (`threads`):
    concurrently, the shards tier gets an even share of the CPUs among itself and the DuckDB tiers, the DuckDB tiers
    `MEM_FRACTION` of `ram` less a `STREAM_RESERVE` per streaming tier (the shards tier's at least `SHARD_JOB_MEM` per
    worker), split evenly, and the CPUs likewise; one at a time, each gets all of it (the shards tier as many workers
    as `SHARD_JOB_MEM` each fit in `MEM_FRACTION` of `ram`)."""
    duck = [t for t in tiers if t in DUCKDB_TIERS]
    budget = int(ram * MEM_FRACTION)
    out: dict[str, dict] = {}
    if "shards" in tiers:
        jobs = max(1, cpus // (1 + len(duck))) if concurrent else max(1, min(cpus, budget // SHARD_JOB_MEM))
        out["shards"] = {"threads": jobs}
    if not duck:
        return out
    if concurrent:
        reserve = sum(max(STREAM_RESERVE, out["shards"]["threads"] * SHARD_JOB_MEM) if t == "shards" else STREAM_RESERVE
                      for t in tiers if t not in duck)
        budget = (budget - reserve) // len(duck)
        cpus = max(1, cpus // len(duck))
    return {**out, **{t: {"mem": f"{max(1, budget >> 30)}GB", "threads": cpus} for t in duck}}


def _machine() -> tuple[int, int]:
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"), os.cpu_count() or 1


@monitor_resources(prefix="MERGE_RESOURCE")
def merge_tier(name: str, dirs: list[Path], outp: Path, run: dict, rule, tmp: Path, mem: str | None = None,
               threads: int | None = None) -> tuple[str, float, dict]:
    """One tier of the merged run `run` from its input run dirs `dirs` (oldest first) into `outp`: `shards` (`sx/`,
    `sidecar*`, `shards.json`), `catalog`, `drill`, or `anchors` (`names/` + `anchors/`). Top-level, so a spawned process
    can run it. Returns `(name, seconds, doc)`."""
    from . import static_append as sa

    enable(file=2)  # Native faults must leave a traceback even under a Python stderr wrapper.
    t0 = monotonic()
    resource_stage(f"merge/{name}", inputs=len(dirs), output=run["key"], mem=mem, threads=threads)
    if name == "shards":
        doc = sa.merge_shards(dirs, outp, jobs=threads, tmp=tmp)
    elif name == "catalog":
        membership = json.loads((dirs[-1] / "catalog" / "meta.json").read_text())["membership"]
        doc = sa.merge_catalogs([d / "catalog" for d in dirs], outp / "catalog", membership, rule)
    elif name == "drill":
        doc = sa.merge_drills(dirs, outp / "drill", run, tmp, threads=threads or 16, mem=mem or "100GB")
    elif name == "anchors":
        from .hex_runs import rule_from_json
        from .static_anchors import Tier as ATier
        from .static_anchors import merge_run_local

        meta = json.loads((dirs[-1] / "anchors" / "meta.json").read_text())
        doc = merge_run_local(connect(threads or 16, mem or "100GB", tmp), [ATier(d) for d in dirs], ATier(outp), meta["R"], meta["K"],
                              run["scans"], rule=rule_from_json(meta.get("hex_runs")))
    else:
        raise ValueError(f"no tier {name!r}")
    return name, round(monotonic() - t0, 1), doc


def run_tiers(dirs: list[Path], drilled: bool) -> list[str]:
    """The tiers a merge of `dirs` writes: shards and catalog always; the drill when the inputs carry it (`plan_carries`
    merges drilled runs only with each other); names + anchors when every input carries them (else the merged run has
    none, and the anchored stack is cut there, as before)."""
    return ["shards", "catalog", *(["drill"] if drilled else []),
            *(["anchors"] if all((d / "anchors" / "meta.json").exists() for d in dirs) else [])]


def build_merged_run(dirs: list[Path], run: dict, outp: Path, *, drilled: bool, rule, tmp: Path, jobs: int = 1,
                     machine: tuple[int, int] | None = None, log: Callable[[str], None] = err, rec: Recorder | None = None) -> dict:
    """The merged run `run` (`plan_carries`' output) of input dirs `dirs` into `outp`: its tiers (`run_tiers`), each in its
    own spawned process when `jobs` > 1 (DuckDB limits per `tier_resources`), then `meta.json` (`run` + the shards' doc).
    A tier whose process is killed (`BrokenProcessPool`: the OOM killer; cw's drill tier beside its anchors on a
    highmem-16, 2026-10-20, 4 attempts) is rerun here, one at a time, with the whole machine (`concurrent=False`).
    Each tier is a phase of `rec` (the run's record: begun as it starts, ended as it finishes, in either mode), and while
    any runs a line every `TICK_S` names them and their seconds so far (gcs's 10-22 L3 carry logged nothing for its last
    ~70 min, through drill and anchors). Returns `{rows, bytes, …, "s": {tier: seconds}}`."""
    import threading
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
    from concurrent.futures.process import BrokenProcessPool
    from multiprocessing import get_context

    rec = rec or NULL
    running: dict[str, float] = {}
    lock, stop = threading.Lock(), threading.Event()

    def begin(t: str) -> None:
        with lock:
            running[t] = monotonic()
        rec.begin(t)

    def end(t: str, note: str, failed: bool = False) -> None:
        with lock:
            running.pop(t, None)
        rec.end(t, note, failed)

    def tick() -> None:
        while not stop.wait(TICK_S):
            with lock:
                now, items = monotonic(), list(running.items())
            if items:
                log(f"merge {run['key']}: running {', '.join(f'{t} {now - t0:.0f}s' for t, t0 in items)}")

    shutil.rmtree(outp, ignore_errors=True)
    outp.mkdir(parents=True)
    tiers = run_tiers(dirs, drilled)
    ram, cpus = machine or _machine()
    res = tier_resources(tiers, ram, cpus, concurrent=jobs > 1)
    args = [(t, dirs, outp, run, rule, tmp / f"merge-{t}", res.get(t, {}).get("mem"), res.get(t, {}).get("threads")) for t in tiers]
    got: dict[str, tuple[float, dict]] = {}

    def serial(a: tuple, suffix: str = "") -> None:
        if a[0] not in running:
            begin(a[0])
        try:
            name, s, doc = merge_tier(*a)
        except BaseException as e:
            end(a[0], f"failed: {type(e).__name__}: {e}", failed=True)
            raise
        got[name] = (s, doc)
        end(name, f"{s:.0f}s{suffix}")
        log(f"merge {run['key']}: {name} in {s:.0f}s{suffix}")

    ticker = threading.Thread(target=tick, daemon=True)
    ticker.start()
    try:
        if jobs == 1:
            for a in args:
                serial(a)
        else:
            with ProcessPoolExecutor(min(jobs, len(args)), mp_context=get_context("spawn")) as ex:
                futures = {}
                for a in args:
                    begin(a[0])
                    futures[ex.submit(merge_tier, *a)] = a[0]
                errs, killed = [], []
                pending = set(futures)
                while pending:
                    finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for f in sorted(finished, key=lambda f: tiers.index(futures[f])):
                        t = futures[f]
                        try:
                            name, s, doc = f.result()
                        except BrokenProcessPool:
                            killed.append(t)
                            rec.note(t, "killed in the pool (out of memory?): rerunning alone")
                            continue
                        except Exception as e:  # noqa: BLE001 — every tier runs to its end; then the first failure is raised
                            errs.append(e)
                            end(t, f"failed: {type(e).__name__}: {e}", failed=True)
                            continue
                        got[name] = (s, doc)
                        end(name, f"{s:.0f}s")
                        log(f"merge {run['key']}: {name} in {s:.0f}s")
                if errs:
                    for t in killed:
                        end(t, "killed in the pool (out of memory?); not rerun: another tier failed", failed=True)
                    raise errs[0]
            if killed:
                killed.sort(key=tiers.index)
                log(f"merge {run['key']}: {', '.join(killed)} killed in the pool (out of memory?): rerunning one at a time with the whole machine")
                alone = tier_resources(tiers, ram, cpus, concurrent=False)
                for a in args:
                    if a[0] in killed:
                        serial((*a[:6], alone.get(a[0], {}).get("mem"), alone.get(a[0], {}).get("threads")), " (alone)")
    finally:
        stop.set()
        ticker.join()
    doc = got["shards"][1]
    (outp / "meta.json").write_text(json.dumps({**run, **doc}, indent=1) + "\n")
    return {**doc, "s": {k: v[0] for k, v in got.items()}}


# ── The static store's carry ───────────────────────────────────────────────


def drilled_runs(store: RunStore, runs: list[dict]) -> set[str]:
    return {r["key"] for r in runs if store.exists(f"{r['key']}/drill/meta.json")}


def _drill_meta(store: RunStore) -> Callable[[str], dict | None]:
    return lambda k: store.read_json(f"{k}/drill/meta.json") if store.exists(f"{k}/drill/meta.json") else None


def missing(store: RunStore, runs: list[dict]) -> list[str]:
    """`static_append.missing_files` over the store: every reader file a listed run lacks, its drill's included."""
    from .static_append import missing_files

    return missing_files(runs, store.exists, _drill_meta(store))


def refuse(store: RunStore, before: list[dict], after: list[dict]) -> str | None:
    """A revision whose runs' drill covers fewer scans than the manifest it revises (`static_append`'s rule 3)."""
    from .static_append import drill_scans

    if lost := sorted(set(drill_scans(before, drilled_runs(store, before))) - set(drill_scans(after, drilled_runs(store, after)))):
        return f"its runs' drill would no longer cover {lost}"
    return None


#: Input rows (the runs' suffix rows) from which a merge job runs long (`append_runner.merge_is_long`): gcs's level-3
#: carry (2026-10-22, 2.03 B rows from 4 runs) took 3 h 7 min on a spot n2-highmem-32, its drill 3 h 5 min, close to
#: `MAX_RUN_S`, and a reclaimed or killed spot task restarts from zero.
LONG_ROWS = 1_000_000_000


def carry(rule=None, jobs: int = 1, build: Callable[..., dict] = build_merged_run) -> Carry:
    """The static store's `Carry`: `build` (`build_merged_run`, `jobs` tiers at once, the generation's hex-run `rule`)."""
    def b(dirs, run, outp, *, drilled, tmp, log, rec=None):
        return build(dirs, run, outp, drilled=drilled, rule=rule, tmp=tmp, jobs=jobs, log=log, rec=rec)
    return Carry(build=b, missing=missing, drilled=drilled_runs, refuse=refuse, long_rows=LONG_ROWS)


def complete(store: RunStore, run: dict) -> bool:
    """Whether `run`'s dir is whole on the store (`append_runner.complete`)."""
    return ar.complete(store, carry(), run)


def publish_revision(store: RunStore, inputs: list[str], out: dict, *, attempts: int = 5, log: Callable[[str], None] = err) -> Published | None:
    return ar.publish_revision(store, carry(), inputs, out, attempts=attempts, log=log)


def merge_pending(store: RunStore, mount: Path, *, tmp: Path, rule=None, jobs: int = 1, owner: str | None = None, dry_run: bool = False,
                  max_merges: int | None = None, max_level: int | None = None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                  build: Callable[..., dict] = build_merged_run, status: ar.MergeStatus | None = None, heartbeat_s: float | None = ar.HEARTBEAT_S,
                  log: Callable[[str], None] = err, source=None, rec=None) -> dict:
    """The newest manifest's due carries (`append_runner.merge_pending`) with the static store's merged-run `build`."""
    return ar.merge_pending(store, carry(rule, jobs, build), mount, tmp=tmp, owner=owner, dry_run=dry_run, max_merges=max_merges,
                            max_level=max_level, now=now, status=status, heartbeat_s=heartbeat_s, log=log, source=source, rec=rec)


# ── Backfilling a merged run's tier ────────────────────────────────────────


#: A tier a merged run can be backfilled with: its liveness markers (uploaded last, in this order; the first is the one
#: that says it's done), and the subdirs it writes.
BACKFILL_TIERS = {
    "drill": {"last": ("drill/meta.json",), "dirs": ("drill",)},
    "anchors": {"last": ("names/sidecar.parquet", "anchors/start/meta.json", "anchors/meta.json"), "dirs": ("names", "anchors")},
}


def backfill_tier(store: RunStore, mount: Path, key: str, tier: str, *, tmp: Path, mem: str | None = None, threads: int | None = None,
                  merge: Callable[..., tuple[str, float, dict]] = merge_tier, log: Callable[[str], None] = err) -> dict | None:
    """A merged run a manifest lists (`key`; the newest's, or an older one's that an inner scan's build reads), published without `tier` (`drill`, or `anchors`: `names/` +
    `anchors/`), gets it from its scans' level-0 runs (`deltas/<scan>`, each of which must have it: build those first,
    oldest first), as a carry would have merged it (`merge_tier`; the tiers' merges are associative). Uploaded into the
    run's dir with the tier's liveness markers last, so readers see it whole or not at all. None when it's there
    already. Refuses a level-0 run (built, not merged), a run no manifest lists, and a tier dir holding keys this
    doesn't write."""
    spec = BACKFILL_TIERS[tier]
    if store.exists(f"{key}/{spec['last'][-1]}"):
        return None
    # The newest manifest listing it: the newest's own runs, or (a run a later carry folded) an older one's, which an
    # inner scan's build reads as its earlier tiers.
    run = None
    for m in reversed(manifest_keys(store.keys("manifests/"))):
        if run := next((r for r in store.read_json(m)["runs"] if r["key"] == key), None):
            break
    if run is None:
        raise ValueError(f"{key}: no manifest lists it")
    if len(run["scans"]) < 2:
        raise ValueError(f"{key}: a level-0 run (its {tier} is built, not merged)")
    inputs = [f"deltas/{s}" for s in run["scans"]]
    if lack := [k for k in inputs if not store.exists(f"{k}/{spec['last'][-1]}")]:
        raise ValueError(f"{key}: its scans' runs lack {spec['last'][-1]}: {lack} (build them first, oldest first)")
    outp = tmp / "backfill" / key
    shutil.rmtree(outp, ignore_errors=True)
    outp.mkdir(parents=True)
    t0 = monotonic()
    _, s, doc = merge(tier, [mount / k for k in inputs], outp, {k: run[k] for k in ("key", "first", "last", "level", "scans")}, None,
                      tmp / f"backfill-{tier}", mem, threads)
    ours = {f"{key}/{f.relative_to(outp).as_posix()}" for f in outp.rglob("*") if f.is_file()}
    if stray := [f for f in ours if f.removeprefix(f"{key}/").split("/")[0] not in spec["dirs"]]:
        raise RuntimeError(f"{key}: the {tier} merge wrote outside {spec['dirs']}: {sorted(stray)[:3]}")
    if stale := sorted(k for d in spec["dirs"] for k in store.keys(f"{key}/{d}/") if k not in ours):
        raise RuntimeError(f"{key}/ holds {len(stale)} {tier} objects this backfill doesn't write (e.g. {stale[0]}): not writing into it")
    up = store.upload(outp, key, last=spec["last"])
    shutil.rmtree(outp)
    log(f"backfill {key} {tier}: {len(inputs)} runs merged in {s:.0f}s, {len(up)} files uploaded, {monotonic() - t0:.0f}s in all")
    return {"key": key, "tier": tier, "inputs": inputs, "files": len(up), "doc": doc}


# ── CLI (the Batch task) ───────────────────────────────────────────────────


@group("merge")
def cli() -> None:
    """Deferred carries of the static name index's runs (the Batch side of `runs merge`)."""


@cli.command("carry")
@option("-b", "--bucket", default=data_bucket, help="Data bucket")
@option("-F", "--read-from", type=Choice(ar.READ_FROM), default="gcs", help="Read the input runs from the data bucket's mount (gcs) or the R2 copy (r2, mirrored locally; the job's R2 env)")
@option("-g", "--gen", required=True, help="Base generation")
@option("-j", "--jobs", default=4, type=IntRange(min=1), help="Tier merges at once (each in its own process)")
@option("-L", "--compact-level", type=ar.Level(), default=ar.COMPACT_LEVEL, help=ar.LEVEL_HELP)
@option("-m", "--mount", required=True, help="Local mount of the data bucket (the merges read the runs)")
@option("-N", "--max-merges", type=IntRange(min=1), help="Stop after this many merged runs")
@option("-n", "--dry-run", is_flag=True, help="Print the plan; merge and write nothing")
@option("-S", "--scratch", default=scratch_bucket, help="Scratch bucket (the merge lease)")
@option("-T", "--tmp", default="/stage/tmp", help="Scratch dir for merges")
def carry_cmd(bucket, read_from, gen, jobs, compact_level, mount, max_merges, dry_run, scratch, tmp) -> None:
    """Run the newest manifest's due carries (`plan_carries`): each merged run into its own dir, then a revision
    `manifests/<id>.m<NNN>.json` listing it. One merger per generation (a lease in the scratch bucket, mirrored to R2's
    `static-names/<gen>/merging.json` for /health when the job has the R2 env); resumable."""
    from .static_names import gen_rule_at

    store = gcs_store(bucket, scratch, gen)
    status = None if dry_run else ar.r2_merge_status(f"{PREFIX}/{gen}")
    rec = NULL if dry_run else from_env("merge")
    doc = merge_pending(store, Path(mount) / PREFIX / gen, tmp=Path(tmp), rule=gen_rule_at(bucket, gen), jobs=jobs, dry_run=dry_run,
                        max_merges=max_merges, max_level=compact_level, status=status,
                        source=(ar.R2Source(), f"{PREFIX}/{gen}") if read_from == "r2" else None, rec=rec)
    doc["prune"] = _summary(ar.finish_merge(rec, doc, lambda: ar.prune_folded(store, retain=FOLDED_RETAIN, dry_run=dry_run, where=" gcs")))
    print(json.dumps(doc, indent=1))


def _summary(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "keys"}


def _helper_runner(gen: str):
    """The static runner the fan-outs' helper jobs are built and run with (`StaticRunner.job`: a stage's machine, spot
    with retries when the profile is), from the profile the runner passed in `$STATIC_NAMES_PROFILE`."""
    from dataclasses import replace

    from .static_profile import profile
    from .static_runner import gcs_runner

    # only what the helper jobs need (a race's `compact-probe -F` serves nothing: no R2, no layouts)
    cfg = replace(profile(), gen=gen)
    if not cfg.project:
        from .gcp import gcp_project

        cfg = replace(cfg, project=gcp_project())
    for f in ("bucket", "scratch", "region", "image", "sa"):
        cfg.need(f)
    return gcs_runner(cfg)


def measure_fanout(tasks: int, gen: str) -> Callable:
    """The compaction's drill measurement over `tasks` tasks (`static_fanout.Fanout`, given the fold's checkpoint): its
    helper jobs (`helper_job`, stage `measure`) run by `_helper_runner`, and its drill write's (`write_helper_job`)."""
    from .checkpoint import code_version
    from .static_fanout import Fanout

    runner = _helper_runner(gen)

    def submit(url: str, plan: str, n: int, code: str, scan: str) -> None:
        runner.run_job(*helper_job(runner, url, plan, n, code, scan))

    def write(url: str, plan: str, n: int, code: str, scan: str) -> None:
        runner.run_job(*write_helper_job(runner, url, plan, n, code, scan))

    return lambda ck, scan: Fanout(ck, tasks, submit, code=code_version(), scan=scan, write_submit=write)


def anchor_fanout(tasks: int, gen: str) -> Callable:
    """The compaction's base anchors' rollups over `tasks` tasks (`static_fanout.AnchorFanout`, given the compaction's
    checkpoint): its helper jobs (`anchor_helper_job`, stage `anchor-shard`) run by `_helper_runner`."""
    from .checkpoint import code_version
    from .static_fanout import AnchorFanout

    runner = _helper_runner(gen)

    def submit(url: str, plan: str, n: int, code: str, scan: str) -> None:
        runner.run_job(*anchor_helper_job(runner, url, plan, n, code, scan))

    return lambda ck, scan: AnchorFanout(ck, tasks, submit, code=code_version(), scan=scan)


def fold_fanout(tasks: int, gen: str) -> Callable:
    """The compaction's drill fold over `tasks` tasks (`static_fanout.FoldFanout`, given the fold's checkpoint): its
    helper jobs, one per wave (`fold_helper_job`, stage `FOLD_STAGE`), run by `_helper_runner`."""
    from .checkpoint import code_version
    from .static_fanout import FoldFanout

    runner = _helper_runner(gen)

    def submit(url: str, plan: str, n: int, code: str, scan: str) -> None:
        runner.run_job(*fold_helper_job(runner, url, plan, n, code, scan))

    return lambda ck, scan: FoldFanout(ck, tasks, submit, code=code_version(), scan=scan)


#: The fold fan-out's helper stage (spot with retries when the profile is; `machines` / `ssd_gbs["fold-shard"]` size it).
FOLD_STAGE = "fold-shard"


def fold_helper_job(runner, url: str, plan: str, n: int, code: str, scan: str) -> tuple[str, dict]:
    """The fold fan-out's helper job (`runner.job`, stage `FOLD_STAGE`, named and labelled by the compaction's newest
    `scan`): `n` tasks of `fold-shard` over the plan `plan` of the fold's checkpoint `url`, the fold's `code` version,
    `static_runner.fold_jobs` units at a time on the stage's machine."""
    from .append_runner import duckdb_args
    from .static_runner import fold_jobs

    machine = runner.cfg.machine_for(FOLD_STAGE)
    args = ["fold-shard", "-c", url, "-P", plan, "-V", code, "-j", str(fold_jobs(machine)), *duckdb_args(machine)]
    return _helper_recorded(runner, *runner.job(FOLD_STAGE, scan, n, "static_merge", args, mount=False), scan)


def helper_job(runner, url: str, plan: str, n: int, code: str, scan: str) -> tuple[str, dict]:
    """The fan-out's helper job (`runner.job`, stage `measure`, named and labelled by the compaction's newest `scan`): `n`
    tasks of `measure-shard` over the plan `plan` of the fold's checkpoint `url`, the fold's `code` version."""
    from .append_runner import duckdb_args

    args = ["measure-shard", "-c", url, "-P", plan, "-V", code, *duckdb_args(runner.cfg.machine_for("measure"))]
    return _helper_recorded(runner, *runner.job("measure", scan, n, "static_merge", args, mount=False), scan)


def write_helper_job(runner, url: str, plan: str, n: int, code: str, scan: str) -> tuple[str, dict]:
    """The drill write fan-out's helper job (`runner.job`, the measurement's stage and machine, named and labelled by
    the compaction's newest `scan`): `n` tasks of `write-shard` over the plan `plan` of the fold's checkpoint `url`."""
    args = ["write-shard", "-c", url, "-P", plan, "-V", code]
    return _helper_recorded(runner, *runner.job("measure", scan, n, "static_merge", args, mount=False), scan)


#: The anchors fan-out's helper stage (its own: `anchors` is a long single-task stage, STANDARD; the helpers are
#: retryable, so spot when the profile is, on `machines["anchor-shard"]` when set).
ANCHOR_STAGE = "anchor-shard"


def anchor_helper_job(runner, url: str, plan: str, n: int, code: str, scan: str) -> tuple[str, dict]:
    """The anchors fan-out's helper job (`runner.job`, stage `ANCHOR_STAGE`, named and labelled by the compaction's
    newest `scan`, never the plan): `n` tasks of `anchor-shard` over the plan `plan` of the compaction's checkpoint
    `url`, the fold's `code` version."""
    from .append_runner import duckdb_args

    args = ["anchor-shard", "-c", url, "-P", plan, "-V", code, *duckdb_args(runner.cfg.machine_for(ANCHOR_STAGE))]
    return _helper_recorded(runner, *runner.job(ANCHOR_STAGE, scan, n, "static_merge", args, mount=False), scan)


def _task_recorded(kind: str):
    """A fan-out helper command recorded per task on `/scans` (`maint_runs.task_recorder`, a child row of the
    compaction's run): one phase `kind` around the body, then the run finished succeeded or failed (re-raised)."""
    from functools import wraps

    def wrap(fn):
        @wraps(fn)
        def run(*args, **kw):
            from .maint_runs import phase, task_recorder

            rec = task_recorder(kind)
            try:
                with phase(rec, kind):
                    fn(*args, **kw)
            except BaseException as e:
                rec.finish("failed", f"{type(e).__name__}: {e}")
                raise
            rec.finish("succeeded")

        return run

    return wrap


def _helper_recorded(runner, name: str, spec: dict, scan: str) -> tuple[str, dict]:
    """A fan-out helper job with what its tasks record with (`runner.record_env`; `maint_runs.task_recorder`): each task
    a child row on `/scans` of this process's job (`$BATCH_JOB_UID`: the compaction's), under its newest `scan`."""
    from .maint_runs import RECORD_GEN_ENV, RECORD_SCAN_ENV

    runner.record_env(name, spec)
    v = spec["taskGroups"][0]["taskSpec"]["environment"]["variables"]
    v.update({RECORD_SCAN_ENV: scan, RECORD_GEN_ENV: f"{PREFIX}/{runner.cfg.gen}"})
    if (parent := (os.environ.get("BATCH_JOB_UID") or "").strip()):
        v["DT_PARENT_RUN"] = parent
    return name, spec


@cli.command("compact")
@option("-b", "--bucket", default=data_bucket, help="Data bucket")
@option("-c", "--chunk-rows", default=1 << 22, type=IntRange(min=1), help="The drill fold's chunks: roots per measurement / rollup q range")
@option("-g", "--gen", required=True, help="Base generation (followed to its newest successor)")
@option("-A", "--anchor-tasks", default=1, type=IntRange(min=1), help="The base anchors' rollups over this many tasks: this one and helper Batch tasks (`static_fanout.AnchorFanout`; needs -S and the profile in $STATIC_NAMES_PROFILE)")
@option("-D", "--measure-tasks", default=1, type=IntRange(min=1), help="The drill measurement over this many tasks: this one and helper Batch tasks (`static_fanout`; needs -S and the profile in $STATIC_NAMES_PROFILE)")
@option("-L", "--fold-tasks", default=1, type=IntRange(min=1), help="The drill fold over this many tasks: this one and helper Batch tasks, a job per wave (`static_fanout.FoldFanout`; needs -S and the profile in $STATIC_NAMES_PROFILE)")
@option("-P", "--prebuilt", help="Publish a fold done elsewhere (`compact-probe -U URL`) instead of folding: checked against its report, then published as this compaction's")
@option("-S", "--scratch", help="Scratch bucket: the compaction lease, held throughout (none: appends aren't held off)")
@ar.compact_options
def compact_cmd(anchor_tasks, bucket, chunk_rows, fold_tasks, gen, measure_tasks, prebuilt, scratch, policy, read_from, grace_s, jobs, force, mount, mem, threads,
                to_r2, tmp) -> None:
    """The compaction stage (`append_runner.run_compaction` with `static_compact`): when the policy fires (or `-K`), the
    generation's base and runs folded into `<gen>.c<NNN>` (a rebuild's bytes), published on GCS (`compaction.json`
    last, `successor.json`, `static-names/CURRENT.json`), then with `-r` on R2 (its pointer last). The old generation
    stays (readers pin `STATIC_GEN`; `store-gc -k`). With `-S`, holds the compaction lease (`static-names/compacting.lease.json`)
    throughout: appends to the generation wait; exit 4 (`BUSY`) while another compaction holds it. With `-S`, the fold is also
    checkpointed there part by part (`static_compact.compact_checkpoint`): a resubmitted job resumes it; cleared once the new
    generation is published (or superseded). Prints a JSON summary."""
    from .blobstore import Store
    from .static_compact import clear_checkpoints, compactor

    if (measure_tasks > 1 or anchor_tasks > 1 or fold_tasks > 1) and not scratch:
        raise UsageError("-D / -A / -L need -S: the helpers read the fold's checkpoint")
    fanout = measure_fanout(measure_tasks, gen) if measure_tasks > 1 else None
    afan = anchor_fanout(anchor_tasks, gen) if anchor_tasks > 1 else None
    ffan = fold_fanout(fold_tasks, gen) if fold_tasks > 1 else None
    try:
        doc = ar.run_compaction(Store(f"gs://{bucket}"), compactor(chunk_rows, checkpoint=scratch, fanout=fanout, prebuilt=prebuilt, anchor_fanout=afan,
                                                                   fold_fanout=ffan), gen,
                                Path(mount), Path(tmp),
                                lease=ar.gcs_compaction_lease_file(scratch, PREFIX), policy=policy, force=force, r2_bucket=bucket if to_r2 else None,
                                grace_s=grace_s, threads=threads, mem=mem, jobs=jobs, read_from=read_from, rec=from_env("compact"))
    except ar.Busy as e:
        err(f"compact: {e}")
        raise SystemExit(ar.BUSY)
    if scratch and doc.get("compact"):
        n = clear_checkpoints(scratch, doc["compact"]["gen"])
        err(f"compact: {n} checkpoint files of {doc['compact']['gen']}'s fold cleared")
    print(json.dumps(doc, indent=1))


@cli.command("compact-r2")
@option("-b", "--bucket", default=data_bucket, help="Data bucket")
@option("-g", "--gen", required=True, help="The generation a compaction started from (its successors are copied)")
def compact_r2_cmd(bucket, gen) -> None:
    """Every compaction from the generation on, to R2 (the R2 account's job): each new generation, then its pointer, last."""
    from .static_compact import compactor

    print(json.dumps(ar.publish_r2_chain(bucket, PREFIX, gen, last=compactor().last), indent=1))
@cli.command("probe")
@option("-d", "--manifest", required=True, help="Frozen manifest stem (scan id, optionally .mNNN), whose first due carry is built")
@option("-g", "--gen", required=True, help="Base generation")
@option("-j", "--jobs", default=4, type=IntRange(min=1), help="Tier merges at once")
@option("-L", "--compact-level", type=ar.Level(), default=ar.COMPACT_LEVEL, help=ar.LEVEL_HELP)
@option("-m", "--mount", required=True, help="Read-only local mount of the data bucket")
@option("-o", "--out", required=True, help="New local output directory outside the mounted generation")
@option("-T", "--tmp", default="/stage/tmp/probe", help="Local scratch dir")
def probe_cmd(
    manifest: str,
    gen: str,
    jobs: int,
    compact_level: int | None,
    mount: str,
    out: str,
    tmp: str,
) -> None:
    """Build one due carry from a frozen manifest locally, without upload, publication, leases or pruning.

    Uses the production merge kernel and resource diagnostics over real mounted inputs; only OUT and TMP are written.
    """
    from .hex_runs import rule_from_json

    stem = manifest.removesuffix(".json")
    if parse_manifest(f"{stem}.json") is None:
        raise UsageError(f"invalid manifest stem: {manifest}")
    source = Path(mount)
    base, output, scratch = source / PREFIX / gen, Path(out), Path(tmp)
    for path in (output, scratch):
        if path.resolve().is_relative_to(source.resolve()):
            raise UsageError(f"probe output/scratch must be outside the source mount: {path}")
    if output.exists():
        raise UsageError(f"probe output already exists: {output}")
    doc = json.loads((base / "manifests" / f"{stem}.json").read_text())
    drilled = {r["key"] for r in doc["runs"] if (base / r["key"] / "drill/meta.json").exists()}
    _, merges = plan_carries(doc["runs"], drilled, compact_level)
    if not merges:
        raise UsageError(f"{stem}: no carries due")
    inputs, run = merges[0]
    rule = rule_from_json(json.loads((base / "scans.json").read_text()).get("hex_runs"))
    result = build_merged_run([base / r["key"] for r in inputs], run, output, drilled=all(r["key"] in drilled for r in inputs),
                              rule=rule, tmp=scratch, jobs=jobs)
    print(json.dumps({"gen": gen, "manifest": stem, "inputs": [r["key"] for r in inputs], "output": run["key"], "result": result}, indent=1))


@cli.command("compact-probe")
@option("-A", "--anchor-tasks", default=1, type=IntRange(min=1), help="The base anchors' rollups over this many tasks (`static_fanout.AnchorFanout`; needs -K and the profile in $STATIC_NAMES_PROFILE)")
@option("-b", "--bucket", default=lambda: os.environ.get("DATA_BUCKET") or None, help="The mount's bucket: the drill's roots are staged from it by the GCS API (default $DATA_BUCKET; none: copied from the mount)")
@option("-c", "--chunk-rows", default=1 << 22, type=IntRange(min=1), help="The drill fold's chunks: roots per measurement / rollup q range")
@option("-F", "--measure-tasks", default=1, type=IntRange(min=1), help="The drill measurement over this many tasks (`static_fanout`; needs -K and the profile in $STATIC_NAMES_PROFILE)")
@option("-g", "--gen", required=True, help="The generation whose base and newest manifest's runs are folded")
@option("-j", "--jobs", default=1, type=IntRange(min=1), help="The drill fold's chunks at a time")
@option("-K", "--checkpoint", "ck_root", help="This fold's own checkpoint root (a scratch URL, never the compaction's): its steps saved there, resumed from there")
@option("-L", "--fold-tasks", default=1, type=IntRange(min=1), help="The drill fold over this many tasks (`static_fanout.FoldFanout`; needs -K and the profile in $STATIC_NAMES_PROFILE)")
@option("-m", "--mount", required=True, help="Read-only local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-N", "--no-units", is_flag=True, help="With -R: seed the steps only, not the fan-outs' finished units of the step after -t, so its helpers rebuild them (a race timing them)")
@option("-o", "--out", required=True, help="New local output directory outside the mount: the folded generation")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-R", "--resume-from", help="Another run's checkpoint root of this same fold (e.g. the running compaction's scratch bucket): its steps through -t seeded into -K (`seed_checkpoint`; the source never written)")
@option("-s", "--stage", default="auto", type=Choice(["auto", "on", "off"]), help="Stage the drill's roots on the work disk first (`static_compact.STAGE_MODES`; auto: when it has room)")
@option("-t", "--through", default="fold/spilled", help="With -R: the last step seeded (`static_compact.STEPS`); the rest run on this code")
@option("-T", "--tmp", default="/stage/tmp/compact-probe", help="Local work dir")
@option("-u", "--report", help="Also write the JSON report to this URL (`gs://<scratch>/…/<name>.json`: the VM's disks go with it)")
@option("-U", "--upload", help="Upload the folded generation to this URL (`<url>/gen/`, then `<url>/probe.json`, last): what `compact --prebuilt` publishes")
def compact_probe_cmd(anchor_tasks, bucket, chunk_rows, measure_tasks, gen, jobs, ck_root, fold_tasks, mount, mem, no_units, out, threads, resume_from, stage,
                      through, tmp, report, upload) -> None:
    """Fold the generation into its next one locally (`static_compact.fold_gen`, the compaction's own fold), read in place
    from the mount: no upload, no successor / pointer / manifests in the bucket, no lease, no checkpoint, no pruning; only
    OUT and TMP are written. Logs the fold's stage lines; prints a JSON of the summary (per-part seconds and peaks) and
    every output file's size and md5 (to compare with a published compaction's `compaction.json`), also written beside
    OUT and to `-u`; stdout gets it without the files.

    A race beside a running compaction (`-K`, `-R`, `-F`, `-U`): resumed from the running one's saved steps (copied, on
    this code), the measurement fanned out, its own checkpoint under `-K`; the output uploaded under `-U` for `compact
    --prebuilt` to publish under the lease (the running one cancelled, or it finished first and this one is dropped).
    Still no lease, no publish, no pointer, and nothing written to the running compaction's checkpoint."""
    import hashlib
    from concurrent.futures import ThreadPoolExecutor

    from .static_compact import fold_gen

    source = Path(mount)
    gen_dir, output, work = source / PREFIX / gen, Path(out), Path(tmp)
    for path in (output, work):
        if path.resolve().is_relative_to(source.resolve()):
            raise UsageError(f"probe output/work must be outside the source mount: {path}")
    if output.exists():
        raise UsageError(f"probe output already exists: {output}")
    if (resume_from or measure_tasks > 1 or anchor_tasks > 1 or fold_tasks > 1) and not ck_root:
        raise UsageError("-R, -F, -A and -L need -K: this fold's own checkpoint root")
    manifest, runs = ar.newest_runs(gen_dir)
    new_gen = ar.next_gen(gen)
    err(f"compact-probe {gen} → {new_gen}: manifest {manifest}, {len(runs)} runs, -j {jobs} -M {mem} -p {threads} -c {chunk_rows} -s {stage}"
        + (f" -F {measure_tasks}" if measure_tasks > 1 else "") + (f" -A {anchor_tasks}" if anchor_tasks > 1 else "") + (f" -L {fold_tasks}" if fold_tasks > 1 else "")
        + (f", resumed from {resume_from} through {through}" if resume_from else ""))
    from .static_compact import compact_checkpoint, seed_checkpoint

    from .maint_runs import PhaseHook, from_env, phase

    # a race (`-U`) is a run of its own on /scans (`race`), phased as a compaction's fold is
    rec = from_env("race") if upload else NULL
    scan, _ = ar.parse_manifest(manifest.removeprefix("manifests/")) or (None, 0)
    if scan:
        rec.start(scan, f"{PREFIX}/{gen}")
    hook = PhaseHook(rec, "fold", note_s=ar.NOTE_S)
    try:
        if resume_from:
            seed_checkpoint(resume_from, ck_root, gen, new_gen, runs, through, units=not no_units)
        ck = compact_checkpoint(ck_root, gen, new_gen, runs, work=work / "ckpt") if ck_root else None
        hook.begin()
        try:
            summary = fold_gen(gen_dir, output, work, gen=gen, new_gen=new_gen, runs=runs, threads=threads, mem=mem, chunk_rows=chunk_rows, jobs=jobs,
                               source=f"gs://{bucket}/{PREFIX}/{gen}" if bucket else None, stage=stage, ck=ck, progress=hook,
                               fanout=measure_fanout(measure_tasks, gen) if measure_tasks > 1 else None,
                               anchor_fanout=anchor_fanout(anchor_tasks, gen) if anchor_tasks > 1 else None,
                               fold_fanout=fold_fanout(fold_tasks, gen) if fold_tasks > 1 else None)
        except BaseException as e:
            hook.end(f"failed: {type(e).__name__}: {e}", failed=True)
            raise
        finally:
            if ck is not None:
                ck.close()
        hook.end(f"{new_gen}: {summary.get('s', '?')}s")
    except BaseException as e:
        rec.finish("failed", f"{type(e).__name__}: {e}")
        raise

    def md5(p: Path) -> str:
        h = hashlib.md5()
        with open(p, "rb") as fh:
            while b := fh.read(1 << 24):
                h.update(b)
        return h.hexdigest()

    t = monotonic()
    files = sorted(p for p in output.rglob("*") if p.is_file())
    with ThreadPoolExecutor(16) as ex:
        sums = list(ex.map(md5, files))
    rels = [p.relative_to(output).as_posix() for p in files]
    doc = {"gen": gen, "new_gen": new_gen, "manifest": manifest, "runs": [r["key"] for r in runs], "jobs": jobs, "summary": summary,
           "md5_s": round(monotonic() - t, 1), "files": {k: {"size": p.stat().st_size, "md5": s} for k, p, s in zip(rels, files, sums)}}
    text = json.dumps(doc, indent=1) + "\n"
    (output.parent / f"{output.name}.probe.json").write_text(text)
    if report:
        from .blobstore import Store

        root, _, key = report.rpartition("/")
        Store(root).write_text(key, text)
        err(f"compact-probe: report → {report}")
    if upload:
        from .blobstore import Store

        try:
            with phase(rec, "upload") as ph:
                dst = Store(upload.rstrip("/"))
                dst.put_tree(output, "gen")
                dst.write_text("probe.json", text)
                ph.note = f"{len(rels)} files to {upload.rstrip('/')}/gen/ (`compact --prebuilt`)"
        except BaseException as e:
            rec.finish("failed", f"{type(e).__name__}: {e}")
            raise
        err(f"compact-probe: {len(rels)} files → {upload.rstrip('/')}/gen/, its report last (`compact --prebuilt {upload}`)")
        rec.finish("succeeded", gen=f"{PREFIX}/{gen} → {new_gen} (race, not published)")
    print(json.dumps({**{k: v for k, v in doc.items() if k != "files"}, "n_files": len(rels)}, indent=1))


@cli.command("measure-probe")
@option("-c", "--checkpoint", required=True, help="A compaction's fold checkpoint (`…/compact-checkpoints/<gen>/<inputs>/fold`, or a copy of it) holding a saved `spilled` step")
@option("-g", "--gen", required=True, help="The generation the checkpoint's compaction folds (its `drill/meta.json`: R, K, floor)")
@option("-H", "--heaviest", type=IntRange(min=0), help="Only the N ranges of each kind with the most spilled bytes (with `-s`: and a sample of the rest)")
@option("-m", "--mount", required=True, help="Read-only local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-o", "--out", required=True, help="New local output directory: the measurement's tables and sorted roots")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-s", "--sample", help="Only N[:SEED] ranges of each kind drawn at random (seed default 0; with `-H`: from the others)")
@option("-S", "--shard", help="I/N: only task I's (0-based) share of the chosen ranges, balanced by spilled bytes (default: $BATCH_TASK_INDEX/$BATCH_TASK_COUNT when set)")
@option("-T", "--tmp", default="/stage/tmp/measure-probe", help="Local work dir (the spills, ~the checkpoint's size)")
@option("-u", "--report", help="Also write the JSON report to this URL (sharded: a prefix, the report at `<prefix>/<I>-of-<N>.json`)")
@option("-w", "--workers", default=64, type=IntRange(min=1), help="Concurrent downloads of the checkpoint's files")
def measure_probe_cmd(checkpoint, gen, heaviest, mount, mem, out, threads, sample, shard, tmp, report, workers) -> None:
    """The drill measurement alone (`static_local.measure_ranges`, long then short), over a compaction's saved spills: the
    checkpoint's `spilled` files are downloaded into TMP, nothing is written to it or the bucket (no `code.json` claim,
    so another build's checkpoint is read, not cleared). `$DT_MEASURE_THREADS` sets the pool's width as in a compaction.
    Logs `measure_report`'s lines; prints a JSON of each kind's seconds and jobs and every output file's size and md5 (the
    measured tables sorted, and the sorted roots: equal bytes at any pool width), also written beside OUT and to `-u`.

    `-H` / `-s`: a subset of each kind's ranges (every range is measured on its own, so a subset's are the same bytes as
    the whole's): the heaviest by spilled bytes, and a seeded sample; only their files are downloaded. The report then
    lists the ranges chosen and each one's seconds, roots, files, bytes and temp bytes.

    `-S I/N`: task I of N measures its share of the chosen ranges (every range when neither `-H` nor `-s`), the ranges
    dealt heaviest first to the least-loaded task (by spilled bytes), and downloads only theirs: one Batch job of N tasks
    measures the whole in about 1/N of the time. `measure-probe-merge` unions the tasks' reports."""
    import hashlib
    from concurrent.futures import ThreadPoolExecutor

    from .checkpoint import Checkpoint
    from . import static_local as sl

    output, work = Path(out), Path(tmp)
    if output.exists():
        raise UsageError(f"probe output already exists: {output}")
    dm = json.loads((Path(mount) / PREFIX / gen / "drill" / "meta.json").read_text())
    root, _, prefix = checkpoint.rstrip("/").rpartition("/")
    ck = Checkpoint(root, prefix, work=work / "ckpt")
    spills = work / "spills"
    shutil.rmtree(spills, ignore_errors=True)
    t = monotonic()
    if shard is None and os.environ.get("BATCH_TASK_COUNT"):
        shard = f"{os.environ.get('BATCH_TASK_INDEX', '0')}/{os.environ['BATCH_TASK_COUNT']}"
    part = tuple(int(x) for x in shard.split("/")) if shard else None
    if part is not None and not (len(part) == 2 and 0 <= part[0] < part[1]):
        raise UsageError(f"-S {shard}: want I/N with 0 ≤ I < N")
    if heaviest is None and sample is None and part is None:
        groups = ck.load_files("spilled", spills, workers=workers)
        chosen = None
    else:
        groups, chosen = _fetch_ranges(ck, spills, heaviest, sample, workers, part)
    fetched = round(monotonic() - t, 1)
    con = connect(threads, mem, work / "duckdb")
    output.mkdir(parents=True)
    kinds = {}
    for kind in ("long", "short"):
        t = monotonic()
        sorted_files, m = sl.measure_ranges(con, spills, kind, groups[kind], "pd", "pr", floor=dm.get("floor", sl.FLOOR), R=dm["R"], K=dm["K"])
        con.execute(f"COPY (SELECT * FROM pd ORDER BY ALL) TO {q(str(output / f'{kind}-dirs.parquet'))} (FORMAT parquet, COMPRESSION zstd)")
        con.execute(f"COPY (SELECT * FROM pr ORDER BY ALL) TO {q(str(output / f'{kind}-rup.parquet'))} (FORMAT parquet, COMPRESSION zstd)")
        (output / f"{kind}-sorted").mkdir()
        for f in sorted_files:
            shutil.move(f, output / f"{kind}-sorted" / f.name)
        kinds[kind] = {**m, "s": round(monotonic() - t, 1), **({"chosen": chosen[kind]} if chosen else {})}

    def md5(p: Path) -> str:
        h = hashlib.md5()
        with open(p, "rb") as fh:
            while b := fh.read(1 << 24):
                h.update(b)
        return h.hexdigest()

    files = sorted(p for p in output.rglob("*") if p.is_file())
    with ThreadPoolExecutor(16) as ex:
        sums = list(ex.map(md5, files))
    doc = {"checkpoint": checkpoint, "gen": gen, "shard": list(part) if part else None, "threads": threads, "mem": mem, "measure_threads": os.environ.get(sl.MEASURE_THREADS_ENV),
           "fetch_s": fetched, "kinds": kinds,
           "files": {p.relative_to(output).as_posix(): {"size": p.stat().st_size, "md5": s} for p, s in zip(files, sums)}}
    text = json.dumps(doc, indent=1) + "\n"
    (output.parent / f"{output.name}.probe.json").write_text(text)
    if report:
        from .blobstore import Store

        if part is not None:
            report = f"{report.rstrip('/')}/{part[0]}-of-{part[1]}.json"
        r, _, key = report.rpartition("/")
        Store(r).write_text(key, text)
        err(f"measure-probe: report → {report}")
    print(json.dumps({**{k: v for k, v in doc.items() if k != "files"}, "n_files": len(files)}, indent=1))


def _fetch_ranges(ck, spills: Path, heaviest: int | None, sample: str | None, workers: int,
                  part: tuple[int, int] | None = None) -> tuple[dict, dict[str, list[int]]]:
    """`measure-probe -H/-s/-S`: per kind, the `heaviest` ranges by spilled bytes, then `sample` (`N[:SEED]`) of the rest
    at random (neither: every range); then, with `part` `(i, n)`, task `i`'s share of them (`deal`); only their files
    downloaded from the checkpoint's `spilled` step into `spills`. The step's doc and the ranges chosen per kind."""
    import random

    n, _, seed = (sample or "0").partition(":")
    sizes, by = _spilled(ck)
    chosen: dict[str, list[int]] = {}
    weights: dict[tuple[str, int], int] = {}
    for kind in ("long", "short"):
        weight = {g: sum(sizes[r] for r in rels) for (k, g), rels in by.items() if k == kind}
        if heaviest is None and sample is None:
            picked = set(weight)
        else:
            top = sorted(weight, key=lambda g: (-weight[g], g))[:heaviest or 0]
            rest = sorted(set(weight) - set(top))
            picked = {*top, *random.Random(int(seed or 0)).sample(rest, min(int(n), len(rest)))}
        weights.update({(kind, g): weight[g] for g in picked})
    mine = set(deal(weights, part[1])[part[0]]) if part else set(weights)
    for kind in ("long", "short"):
        chosen[kind] = sorted(g for k, g in mine if k == kind)
    rels = _fetch_spills(ck, spills, by, [(kind, g) for kind, gs in chosen.items() for g in gs], workers)
    err(f"measure-probe: {len(rels):,} files ({sum(sizes[r] for r in rels):,} B) of {', '.join(f'{len(v)} {k}' for k, v in chosen.items())} ranges")
    return ck.store.read_json(ck.key("spilled/done.json"))["doc"], chosen


#: Under a fold checkpoint: each spill file's rows (`{path under spilled/files/: rows}`), written before the `spilled`
#: marker; absent when the fold predates it, or a unit an older helper spilled had none.
SPILL_ROWS = "spilled/rows.json"


def spill_rows(ck, rels: list[str]) -> dict[str, int] | None:
    """`SPILL_ROWS`' rows of `rels`, or None when it's absent or misses one of them."""
    if not ck.store.exists(ck.key(SPILL_ROWS)):
        return None
    rows = ck.store.read_json(ck.key(SPILL_ROWS))
    return rows if all(r in rows for r in rels) else None


def _spilled(ck) -> tuple[dict[str, int], dict[tuple[str, int], list[str]]]:
    """A fold checkpoint's saved spills: each file's bytes (by its path under `spilled/files/`) and the files of each
    `(kind, range)`. The files are the `spilled` marker's list (`spilled/done.json`), not whatever is under the prefix:
    an aborted attempt's orphans (a fold fan-out unit written twice) are never read; the sizes are one listing's. A
    listed file missing raises."""
    import re

    base = ck.key("spilled/files/")
    listed = ck.store.listing(ck.key("spilled/files"))
    rels = ck.store.read_json(ck.key("spilled/done.json"))["files"]
    if missing := [r for r in rels if base + r not in listed]:
        raise RuntimeError(f"{ck.key('spilled')}: {len(missing)} files its marker lists are missing (e.g. {missing[0]})")
    sizes = {r: listed[base + r] for r in rels}
    by: dict[tuple[str, int], list[str]] = {}
    for rel in sizes:
        if m := re.match(r"(long|short)/[^/]+/grp=(\d+)/", rel):
            by.setdefault((m[1], int(m[2])), []).append(rel)
    return sizes, by


def _fetch_spills(ck, spills: Path, by: dict[tuple[str, int], list[str]], ranges: list[tuple[str, int]], workers: int) -> list[str]:
    """The saved spill files of `ranges` (`(kind, range)`) downloaded into `spills` (their paths under it as under
    `spilled/files/`); their paths."""
    from concurrent.futures import ThreadPoolExecutor

    base = ck.key("spilled/files/")
    rels = [r for key in ranges for r in by.get(key, [])]
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(lambda r: ck.store.get(base + r, spills / r), rels))
    return rels


def deal(weights: dict, n: int, away: set | frozenset = frozenset()) -> list[list]:
    """`weights`' keys dealt to `n` tasks, the heaviest first, each to the least-loaded task so far (ties: the lower
    task, then the lower key): a few heavy ranges don't serialize one task. A key of `away` never goes to task 0 (a
    fan-out's own share, which may not build it as its helpers do) while there are others."""
    import heapq

    tasks: list[list] = [[] for _ in range(n)]
    heap = [(0, i) for i in range(n)]
    for key in sorted(weights, key=lambda k: (-weights[k], k)):
        load, i = heapq.heappop(heap)
        if i == 0 and key in away and heap:
            load, i = heapq.heapreplace(heap, (load, i))
        tasks[i].append(key)
        heapq.heappush(heap, (load + weights[key], i))
    return tasks


@cli.command("measure-shard")
@option("-c", "--checkpoint", required=True, help="The compaction's fold checkpoint (its `spilled` step saved), as a URL")
@option("-i", "--index", type=int, help="This task's share of the plan (default: $BATCH_TASK_INDEX)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-P", "--plan", required=True, help="The fan-out's plan, its key under the checkpoint (`measured/plan-<n>.json`)")
@option("-T", "--tmp", default="/stage/tmp/measure-shard", help="Local work dir")
@option("-V", "--code", required=True, help="The fold's code version (`checkpoint.code_version`): another refuses")
@option("-w", "--workers", default=64, type=IntRange(min=1), help="Concurrent downloads and uploads")
@_task_recorded("measure-shard")
def measure_shard_cmd(checkpoint, index, mem, threads, plan, tmp, code, workers) -> None:
    """A compaction's drill measurement helper (`static_fanout`, specs/static-measure-fanout.md): task INDEX's share of
    the plan's `q` ranges, each fetched from the fold's saved spills, measured (`static_local.measure_ranges`; only
    sorted when the plan's `measure` is false: an incremental compaction's) and
    uploaded as `measured/<kind>/g<g>/{sorted,dirs,rup}.parquet`, then its `done.json` (the range's stats). A range
    whose `done.json` is there is skipped (a retry resumes); then `measured/tasks/<plan>-<index>.json`. Refuses to run
    other code than the fold's (its bytes would differ)."""
    from functools import partial

    from . import static_local as sl
    from .checkpoint import Checkpoint, code_version
    from .static_fanout import MEASURED, range_key

    if (mine := code_version()) != code:
        raise SystemExit(f"measure-shard: this is code {mine}, the fold's is {code}: refusing to measure for it")
    root, _, prefix = checkpoint.rstrip("/").rpartition("/")
    i = index if index is not None else int(os.environ.get("BATCH_TASK_INDEX", "0"))
    # the task's own dir, cleared first: Batch runs a job's later tasks on the VMs its earlier ones used
    work = Path(tmp) / f"{plan.rsplit('/', 1)[-1].removesuffix('.json')}-{i}"
    shutil.rmtree(work, ignore_errors=True)
    ck = Checkpoint(root, prefix, work=work / "ckpt")
    doc = ck.store.read_json(ck.key(plan))
    share = [(k, g) for k, g in doc["tasks"][i]]
    todo = [(k, g) for k, g in share if not ck.store.exists(ck.key(f"{range_key(k, g)}/done.json"))]
    err(f"measure-shard {plan} #{i}: {len(share)} ranges, {len(todo)} to measure")
    spills = work / "spills"
    t0 = monotonic()
    _, by = _spilled(ck)
    _fetch_spills(ck, spills, by, todo, workers)

    def upload(kind: str, g: int, task: tuple, stats: dict) -> None:
        base = range_key(kind, g)
        for name, f in (("sorted", task[2]), ("dirs", task[3]), ("rup", task[4])):
            ck.store.put(Path(f), ck.key(f"{base}/{name}.parquet"))
        ck.store.write_text(ck.key(f"{base}/done.json"), json.dumps(stats) + "\n")

    con = connect(threads, mem, work / "duckdb")
    for kind in ("long", "short"):
        gs = [g for k, g in todo if k == kind]
        if gs:
            sl.measure_ranges(con, spills, kind, max(gs) + 1, "pd", "pr", floor=doc["floor"], R=doc["R"], K=doc["K"],
                              on_range=partial(upload, kind), measure=doc.get("measure", True))
    name = plan.rsplit("/", 1)[-1].removesuffix(".json")
    ck.store.write_text(ck.key(f"{MEASURED}/tasks/{name}-{i}.json"), json.dumps({"ranges": len(todo), "s": round(monotonic() - t0, 1)}) + "\n")
    err(f"measure-shard {plan} #{i}: {len(todo)} ranges in {monotonic() - t0:.0f}s")


@cli.command("fold-shard")
@option("-c", "--checkpoint", required=True, help="The compaction's fold checkpoint (its `fan/plan` saved), as a URL")
@option("-i", "--index", type=int, help="This task's share of the plan (default: $BATCH_TASK_INDEX)")
@option("-j", "--jobs", default=1, type=IntRange(min=1), help="Units folded at a time, on cursors of one DuckDB database (-p threads and -M memory shared)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-P", "--plan", required=True, help="The fan-out's plan, its key under the checkpoint (`folded/plan-<n>.json`)")
@option("-T", "--tmp", default="/stage/tmp/fold-shard", help="Local work dir")
@option("-V", "--code", required=True, help="The fold's code version (`checkpoint.code_version`): another refuses")
@_task_recorded("fold-shard")
def fold_shard_cmd(checkpoint, index, jobs, mem, threads, plan, tmp, code) -> None:
    """A compaction's drill fold helper (`static_fanout.FoldFanout`, specs/static-fold-fanout.md): task INDEX's share of
    the plan's units (a long chunk's digests or canonical spills, a short range's spills), their roots files staged from
    the generation's store (the plan's `source`) and the runs' spill partitions from `fan/runs`, each unit folded
    (`static_compact.fold_units`) and uploaded (spills under `spilled/files/`), then its `done.json`. A unit whose
    `done.json` is there is skipped (a retry resumes); then `folded/tasks/<plan>-<index>.json`. Refuses to run other code
    than the fold's (its bytes would differ)."""
    from .checkpoint import Checkpoint, code_version
    from .static_compact import fold_units
    from .static_fanout import FOLDED, fold_unit_key

    if (mine := code_version()) != code:
        raise SystemExit(f"fold-shard: this is code {mine}, the fold's is {code}: refusing to fold for it")
    root, _, prefix = checkpoint.rstrip("/").rpartition("/")
    i = index if index is not None else int(os.environ.get("BATCH_TASK_INDEX", "0"))
    name = plan.rsplit("/", 1)[-1].removesuffix(".json")
    # the task's own dir, cleared first: Batch runs a job's later tasks on the VMs its earlier ones used
    work = Path(tmp) / f"{name}-{i}"
    shutil.rmtree(work, ignore_errors=True)
    ck = Checkpoint(root, prefix, work=work / "ckpt")
    doc = ck.store.read_json(ck.key(plan))
    share = [(k, u) for k, u in doc["tasks"][i]]
    listing = ck.store.listing(ck.key(FOLDED))
    todo = [(k, u) for k, u in share if ck.key(f"{fold_unit_key(k, u)}/done.json") not in listing]
    err(f"fold-shard {plan} #{i}: wave {doc.get('wave')}, {len(share)} units, {len(todo)} to fold")
    con = connect(threads, mem, work / "duckdb")
    try:
        got = fold_units(con, ck, todo, work, work / "out", jobs=jobs, log=err)
    finally:
        con.close()
        ck.close()
    ck.store.write_text(ck.key(f"{FOLDED}/tasks/{name}-{i}.json"), json.dumps(got) + "\n")
    err(f"fold-shard {plan} #{i}: {got['units']} units, {got['rows']:,} rows in {got['s']}s")


@cli.command("write-shard")
@option("-c", "--checkpoint", required=True, help="The fold's checkpoint (its measured ranges' sorted roots under `measured/`), as a URL")
@option("-i", "--index", type=int, help="This task's share of the plan (default: $BATCH_TASK_INDEX)")
@option("-j", "--jobs", type=IntRange(min=1), help="Files written at a time, each in a process (default: every core)")
@option("-P", "--plan", required=True, help="The fan-out's plan, its key under the checkpoint (`written/plan-<n>.json`)")
@option("-T", "--tmp", default="/stage/tmp/write-shard", help="Local work dir")
@option("-V", "--code", required=True, help="The fold's code version (`checkpoint.code_version`): another refuses")
@_task_recorded("write-shard")
def write_shard_cmd(checkpoint, index, jobs, plan, tmp, code) -> None:
    """A compaction's drill write helper (`static_fanout.WriteFanout`, specs/static-write-fanout.md): task INDEX's share
    of the plan's roots files, their measured ranges' sorted roots fetched from the checkpoint, each file written as the
    fold VM writes it (`static_drill._write_chunk_file`) and uploaded with its group index rows as
    `written/<kind>/r<i>/{file,index}.parquet`, then its `done.json` (rows, bytes). A file whose `done.json` is there is
    skipped (a retry resumes); then `written/tasks/<plan>-<index>.json`. Refuses to run other code than the fold's."""
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from multiprocessing import get_context

    import pyarrow.parquet as pq

    from . import static_drill as sd
    from . import static_roots as sr
    from .checkpoint import Checkpoint, code_version
    from .static_fanout import WRITTEN, fetch_files, write_key

    if (mine := code_version()) != code:
        raise SystemExit(f"write-shard: this is code {mine}, the fold's is {code}: refusing to write for it")
    root, _, prefix = checkpoint.rstrip("/").rpartition("/")
    i = index if index is not None else int(os.environ.get("BATCH_TASK_INDEX", "0"))
    name = plan.rsplit("/", 1)[-1].removesuffix(".json")
    work = Path(tmp) / f"{name}-{i}"
    shutil.rmtree(work, ignore_errors=True)
    ck = Checkpoint(root, prefix, work=work / "ckpt")
    doc = ck.store.read_json(ck.key(plan))
    kind, rg = doc["kind"], doc["rg"]
    share = [int(u) for _, u in doc["tasks"][i]]
    todo = [u for u in share if not ck.store.exists(ck.key(f"{write_key(kind, u)}/done.json"))]
    err(f"write-shard {plan} #{i}: {len(share)} {kind} files, {len(todo)} to write")
    t0 = monotonic()
    files = {u: doc["files"][str(u)] for u in todo}
    need = sorted({k for f in files.values() for k, _, _ in f["spans"]})
    listing = ck.store.listing(ck.key("measured"))
    fetch_files(ck, {k: work / "in" / k for k in need}, listing, "write-shard: sorted ranges", label="write-shard")
    out = work / "out"
    pool = ProcessPoolExecutor(jobs or os.cpu_count() or 1, mp_context=get_context("spawn"), initializer=sd._writer_init)
    with pool:
        futs = {pool.submit(sd._write_chunk_file, [(work / "in" / k, a, b) for k, a, b in f["spans"]], sr.ROOT_SCHEMA, f["lo"], f["hi"], out,
                            f["name"], "path", ["q", "usr"], rg): u for u, f in files.items()}
        for fut in as_completed(futs):
            u = futs[fut]
            rows, idx, size = fut.result()
            base = write_key(kind, u)
            local = out / files[u]["name"]
            ck.store.put(local, ck.key(f"{base}/file.parquet"))
            ix = work / "idx" / f"r{u:04d}.parquet"
            ix.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(idx, ix)
            ck.store.put(ix, ck.key(f"{base}/index.parquet"))
            ck.store.write_text(ck.key(f"{base}/done.json"), json.dumps({"rows": rows, "bytes": size}) + "\n")
            local.unlink()
            ix.unlink()
    ck.store.write_text(ck.key(f"{WRITTEN}/tasks/{name}-{i}.json"), json.dumps({"files": len(todo), "s": round(monotonic() - t0, 1)}) + "\n")
    err(f"write-shard {plan} #{i}: {len(todo)} {kind} files in {monotonic() - t0:.0f}s")


def _timed_shard_task(*args) -> tuple[dict, float]:
    """`static_anchors._shard_task(*args)`'s doc and its seconds (top-level: a spawned pool process runs it)."""
    from .static_anchors import _shard_task

    t0 = monotonic()
    doc = _shard_task(*args)
    return doc, round(monotonic() - t0, 1)



#: How long an `anchor-shard` task submitted ahead of its plan waits for it, and the longest pause between looks.
PLAN_WAIT_S = 1800
PLAN_POLL_S = 10

@cli.command("anchor-shard")
@option("-c", "--checkpoint", required=True, help="The compaction's checkpoint (its `anchors-in/` uploaded), as a URL")
@option("-i", "--index", type=int, help="This task's share of the plan (default: $BATCH_TASK_INDEX)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit (split among the shard tasks run at once)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads (`static_anchors.ANCHOR_THREADS` per shard task at once)")
@option("-P", "--plan", required=True, help="The fan-out's plan, its key under the checkpoint (`anchored/plan-<n>.json`)")
@option("-T", "--tmp", default="/stage/tmp/anchor-shard", help="Local work dir")
@option("-V", "--code", required=True, help="The fold's code version (`checkpoint.code_version`): another refuses")
@option("-w", "--workers", default=16, type=IntRange(min=1), help="Concurrent downloads")
@_task_recorded("anchor-shard")
def anchor_shard_cmd(checkpoint, index, mem, threads, plan, tmp, code, workers) -> None:
    """A compaction's base anchors helper (`static_fanout.AnchorFanout`, specs/static-anchors-fanout.md): task INDEX's
    share of the plan's `(kind, shard)` rollup tasks, each one's input shard fetched from `anchors-in/`, its rollups
    built (`static_anchors._shard_task`: a shard of the plan's `heavy_rows` or more alone first, on all `-p` threads and
    `-M`; the rest biggest first, each on threads by its rows out of the `-p` free, `-M` shared in proportion) and
    uploaded as `anchored/<kind>/s<i>/cells.parquet`, then its `done.json` (the task's doc, seconds, rows). A task whose
    `done.json` is there is skipped (a retry resumes); then `anchored/tasks/<plan>-<index>.json`. Refuses to run other
    code than the fold's (its bytes would differ)."""
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
    from multiprocessing import get_context

    import pyarrow.parquet as pq

    from .checkpoint import Checkpoint, code_version
    from .hex_runs import rule_from_json
    from .static_anchors import ANCHOR_THREADS
    from .static_append import _Inline
    from .static_fanout import ANCHORED, anchor_input_key, anchor_task_key
    from .static_names import _pool_init, mem_bytes

    if (mine := code_version()) != code:
        raise SystemExit(f"anchor-shard: this is code {mine}, the fold's is {code}: refusing to build for it")
    root, _, prefix = checkpoint.rstrip("/").rpartition("/")
    i = index if index is not None else int(os.environ.get("BATCH_TASK_INDEX", "0"))
    name = plan.rsplit("/", 1)[-1].removesuffix(".json")
    # the task's own dir, cleared first: Batch runs a job's later tasks on the VMs its earlier ones used
    work = Path(tmp) / f"{name}-{i}"
    shutil.rmtree(work, ignore_errors=True)
    ck = Checkpoint(root, prefix, work=work / "ckpt")
    # a job submitted ahead of its plan (`Helpers.presubmit`): wait for it
    deadline, pause = monotonic() + PLAN_WAIT_S, 0.5
    while not ck.store.exists(ck.key(plan)):
        if monotonic() > deadline:
            raise SystemExit(f"anchor-shard {plan} #{i}: no plan after {PLAN_WAIT_S}s")
        sleep(pause)
        pause = min(2 * pause, PLAN_POLL_S)
    doc = ck.store.read_json(ck.key(plan))
    if i >= len(doc["tasks"]):
        err(f"anchor-shard {plan} #{i}: nothing dealt to this task ({len(doc['tasks'])} shares)")
        return
    rule = rule_from_json(doc.get("hex_runs"))
    share = [(k, s) for k, s in doc["tasks"][i]]
    todo = [(k, s) for k, s in share if not ck.store.exists(ck.key(f"{anchor_task_key(k, s)}/done.json"))]
    err(f"anchor-shard {plan} #{i}: {len(share)} shard tasks, {len(todo)} to build")
    t0 = monotonic()
    inputs = {anchor_input_key(k, s): work / "in" / anchor_input_key(k, s) for k, s in todo}
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(lambda kv: ck.store.get(ck.key(kv[0]), kv[1]), sorted(inputs.items())))

    rows_of = {t: pq.ParquetFile(inputs[anchor_input_key(*t)]).metadata.num_rows for t in todo}

    def size(t: tuple[str, int]) -> int:
        return rows_of[t]

    def cells_of(k: str, s: int) -> Path:
        cells = work / "out" / f"{k}-{s:05d}.parquet"
        cells.parent.mkdir(parents=True, exist_ok=True)
        return cells

    def put(k: str, s: int, cells: Path, d: dict, secs: float) -> None:
        base = anchor_task_key(k, s)
        ck.store.put(cells, ck.key(f"{base}/cells.parquet"))
        ck.store.write_text(ck.key(f"{base}/done.json"), json.dumps({"doc": d, "s": secs, "rows": pq.ParquetFile(cells).metadata.num_rows}) + "\n")
        cells.unlink()

    todo.sort(key=lambda t: -size(t))
    # a heavy shard alone, on every thread and all the memory: at an even `ANCHOR_THREADS` share gcs's 1.35 GB `end` shards
    # took ~2240s each (one OOMing a neighbour); alone on 16 threads, 769s
    def alone(k: str, s: int, why: str) -> None:
        err(f"anchor-shard {plan} #{i}: {k} s{s:05d} alone ({why}; {size((k, s)):,} rows, {threads} threads)")
        cells = cells_of(k, s)
        shutil.rmtree(work / "tmp" / f"{k}-{s:05d}", ignore_errors=True)
        put(k, s, cells, *_timed_shard_task(k, [str(inputs[anchor_input_key(k, s)])], doc["R"], doc["K"], rule, str(cells), threads, mem,
                                            str(work / "tmp" / f"{k}-{s:05d}")))

    heavy = [t for t in todo if size(t) >= doc["heavy_rows"]]
    for k, s in heavy:
        alone(k, s, "heavy")
    rest = todo[len(heavy):]
    oom: list[tuple[str, int]] = []
    # the rest, biggest first, each on threads by its rows (its input's footer: `ANCHOR_THREADS` per median shard, at
    # least that) out of those free, and memory in proportion: never more threads in flight than the task's (gcs's rkeep
    # race: the tasks' tails were ~108M-row shards at a flat 4 threads, `levels` most of them; by bytes they were under
    # the median and kept 4; f26321b2's weights over every shard oversubscribed the cores)
    slots = max(1, min(len(rest), threads // ANCHOR_THREADS))
    mid = max(1, size(rest[len(rest) // 2])) if rest else 1
    total = mem_bytes(mem)
    queue, running, free = list(rest), {}, threads
    with _Inline() if slots == 1 else ProcessPoolExecutor(slots, mp_context=get_context("spawn"), initializer=_pool_init) as pool:
        while queue or running:
            while queue and len(running) < slots and free >= min(ANCHOR_THREADS, threads):
                k, s = queue.pop(0)
                t = min(free, max(ANCHOR_THREADS, round(ANCHOR_THREADS * size((k, s)) / mid)))
                m = f"{max(1 << 30, total * t // (threads + ANCHOR_THREADS)) >> 20}MiB"
                cells = cells_of(k, s)
                fut = pool.submit(_timed_shard_task, k, [str(inputs[anchor_input_key(k, s)])], doc["R"], doc["K"], rule, str(cells), t, m,
                                  str(work / "tmp" / f"{k}-{s:05d}"))
                running[fut] = (k, s, cells, t, m)
                free -= t
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in done:
                k, s, cells, t, m = running.pop(fut)
                free += t
                try:
                    d, secs = fut.result()
                except Exception as e:
                    # a pooled shard over its memory share (gcs: a 0.35 GB one at 18.6 GiB failed its whole Batch task):
                    # built again alone once the pool is done
                    if type(e).__name__ != "OutOfMemoryException":
                        raise
                    err(f"anchor-shard {plan} #{i}: {k} s{s:05d} over its {m}: {e}")
                    oom.append((k, s))
                    continue
                put(k, s, cells, d, secs)
    for k, s in oom:
        alone(k, s, "out of memory in the pool")
    ck.store.write_text(ck.key(f"{ANCHORED}/tasks/{name}-{i}.json"), json.dumps({"tasks": len(todo), "s": round(monotonic() - t0, 1)}) + "\n")
    err(f"anchor-shard {plan} #{i}: {len(todo)} shard tasks in {monotonic() - t0:.0f}s")


@cli.command("measure-probe-merge")
@option("-o", "--out", help="Also write the merged report here (a local path or URL)")
@argument("prefix")
def measure_probe_merge_cmd(out, prefix) -> None:
    """The `measure-probe -S I/N -u PREFIX` tasks' reports (`PREFIX/<I>-of-<N>.json`) as one: every task's present (else
    exit 1, naming the missing), each kind's ranges and chosen ranges unioned, its seconds the slowest task's, and the
    files' md5s: each range's sorted roots as themselves (the same bytes as an unsharded run's), each task's measured
    tables under `<I>-of-<N>/`."""
    import re

    from .blobstore import Store

    store = Store(prefix.rstrip("/"))
    names = sorted(k for k in store.keys("") if re.fullmatch(r"\d+-of-\d+\.json", k.rsplit("/", 1)[-1]))
    docs = [store.read_json(k) for k in names]
    counts = {d["shard"][1] for d in docs}
    if len(counts) != 1:
        raise UsageError(f"{prefix}: reports of different task counts {sorted(counts)}")
    [n] = counts
    missing = sorted(set(range(n)) - {d["shard"][0] for d in docs})
    if missing:
        err(f"measure-probe-merge: {prefix}: tasks {missing} of {n} have no report")
        raise SystemExit(1)
    kinds = {}
    for kind in ("long", "short"):
        ks = [d["kinds"][kind] for d in docs]
        kinds[kind] = {"chosen": sorted(g for k in ks for g in k["chosen"]), "groups": sum(k["groups"] for k in ks),
                       "s": max(k["s"] for k in ks), "s_sum": round(sum(k["s"] for k in ks), 1),
                       "ranges": {g: r for k in ks for g, r in k["ranges"].items()}}
    files = {}
    for d in docs:
        tag = f"{d['shard'][0]}-of-{n}"
        for k, v in d["files"].items():
            files[k if "-sorted/" in k else f"{tag}/{k}"] = v
    doc = {"checkpoint": docs[0]["checkpoint"], "gen": docs[0]["gen"], "tasks": n, "measure_threads": docs[0]["measure_threads"],
           "fetch_s": max(d["fetch_s"] for d in docs), "kinds": kinds, "files": dict(sorted(files.items()))}
    text = json.dumps(doc, indent=1) + "\n"
    if out:
        r, _, key = out.rpartition("/")
        Store(r or ".").write_text(key, text)
    print(json.dumps({**{k: v for k, v in doc.items() if k not in ("files", "kinds")},
                      "kinds": {k: {x: v[x] for x in ("groups", "s", "s_sum")} for k, v in kinds.items()}, "n_files": len(files)}, indent=1))


@cli.command("prune")
@option("-b", "--bucket", default=data_bucket, help="Data bucket")
@option("-g", "--gen", required=True, help="Base generation")
@option("-G", "--grace-s", default=ar.GRACE_S, type=IntRange(min=ar.MIN_GRACE_S), help=f"Keep a superseded manifest's runs this many seconds (default {ar.GRACE_S}, at least {ar.MIN_GRACE_S})")
@option("-n", "--dry-run", is_flag=True, help="Report what would be deleted; delete nothing")
@option("-r", "--r2", is_flag=True, help="Prune the R2 copy (`R2_ENDPOINT`, `R2_BUCKET`, keys from the env), planned by R2's own manifests; default: GCS")
def prune_cmd(bucket, gen, grace_s, dry_run, r2) -> None:
    """Delete the runs a carry folded (`append_runner.prune_folded`): every run dir listed by no manifest but the newest
    and those superseded less than `-G` s ago, inside one of the newest's runs. On GCS a folded run keeps `FOLDED_RETAIN` (later appends read it);
    on R2, nothing. Run after a publish to that store (the merge job and the R2 job do)."""
    if r2:
        doc = ar.prune_folded(ar.r2_run_store(f"{PREFIX}/{gen}"), grace_s=grace_s, dry_run=dry_run, where=" r2")
    else:
        doc = ar.prune_folded(gcs_store(bucket, None, gen), grace_s=grace_s, retain=FOLDED_RETAIN, dry_run=dry_run, where=" gcs")
    print(json.dumps({"gen": gen, "store": "r2" if r2 else "gcs", **_summary(doc)}, indent=1))


@cli.command("tier")
@option("-b", "--bucket", default=data_bucket, help="Data bucket")
@option("-g", "--gen", required=True, help="Base generation")
@option("-m", "--mount", required=True, help="Local mount of the data bucket (the merge reads the runs)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-r", "--run", "key", required=True, help="The merged run's key (`deltas/<first>_<last>`), listed by a manifest")
@option("-t", "--tier", required=True, type=Choice(sorted(BACKFILL_TIERS)), help="The tier to backfill")
@option("-T", "--tmp", default="/stage/tmp", help="Scratch dir")
def tier_cmd(bucket, gen, mount, mem, threads, key, tier, tmp) -> None:
    """Backfill a listed merged run's missing `drill/` (or `names/` + `anchors/`) from its scans' level-0 runs, which must
    each have it (`backfill_tier`); a no-op when it's there."""
    doc = backfill_tier(gcs_store(bucket, None, gen), Path(mount) / PREFIX / gen, key, tier, tmp=Path(tmp), mem=mem, threads=threads)
    print(json.dumps(doc or {"key": key, "tier": tier, "done": True}, indent=1))


if __name__ == "__main__":
    cli()
