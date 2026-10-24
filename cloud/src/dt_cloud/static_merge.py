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
from time import monotonic

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


def measure_fanout(tasks: int, gen: str) -> Callable:
    """The compaction's drill measurement over `tasks` tasks (`static_fanout.Fanout`, given the fold's checkpoint): its
    helper jobs built and run as the runner's stage jobs are (`StaticRunner.job`, stage `measure`: the profile's machine
    for it, spot with retries when the profile is), from the profile the runner passed in `$STATIC_NAMES_PROFILE`."""
    from .append_runner import duckdb_args
    from .checkpoint import code_version
    from .static_fanout import Fanout
    from .static_profile import profile
    from .static_runner import gcs_runner, ready

    cfg = ready(profile(), gen)
    runner = gcs_runner(cfg)

    def submit(url: str, plan: str, n: int, code: str) -> None:
        args = ["measure-shard", "-c", url, "-P", plan, "-V", code, *duckdb_args(cfg.machine_for("measure"))]
        runner.run_job(*runner.job("measure", plan.rsplit("/", 1)[-1].removesuffix(".json"), n, "static_merge", args, mount=False))

    return lambda ck: Fanout(ck, tasks, submit, code=code_version())


@cli.command("compact")
@option("-b", "--bucket", default=data_bucket, help="Data bucket")
@option("-c", "--chunk-rows", default=1 << 22, type=IntRange(min=1), help="The drill fold's chunks: roots per measurement / rollup q range")
@option("-g", "--gen", required=True, help="Base generation (followed to its newest successor)")
@option("-F", "--measure-tasks", default=1, type=IntRange(min=1), help="The drill measurement over this many tasks: this one and helper Batch tasks (`static_fanout`; needs -S and the profile in $STATIC_NAMES_PROFILE)")
@option("-S", "--scratch", help="Scratch bucket: the compaction lease, held throughout (none: appends aren't held off)")
@ar.compact_options
def compact_cmd(bucket, chunk_rows, gen, measure_tasks, scratch, policy, read_from, grace_s, jobs, force, mount, mem, threads, to_r2, tmp) -> None:
    """The compaction stage (`append_runner.run_compaction` with `static_compact`): when the policy fires (or `-K`), the
    generation's base and runs folded into `<gen>.c<NNN>` (a rebuild's bytes), published on GCS (`compaction.json`
    last, `successor.json`, `static-names/CURRENT.json`), then with `-r` on R2 (its pointer last). The old generation
    stays (readers pin `STATIC_GEN`; `store-gc -k`). With `-S`, holds the compaction lease (`static-names/compacting.lease.json`)
    throughout: appends to the generation wait; exit 4 (`BUSY`) while another compaction holds it. With `-S`, the fold is also
    checkpointed there part by part (`static_compact.compact_checkpoint`): a resubmitted job resumes it; cleared once the new
    generation is published (or superseded). Prints a JSON summary."""
    from .blobstore import Store
    from .static_compact import clear_checkpoints, compactor

    if measure_tasks > 1 and not scratch:
        raise UsageError("-F needs -S: the helpers read the fold's checkpoint")
    fanout = measure_fanout(measure_tasks, gen) if measure_tasks > 1 else None
    try:
        doc = ar.run_compaction(Store(f"gs://{bucket}"), compactor(chunk_rows, checkpoint=scratch, fanout=fanout), gen, Path(mount), Path(tmp),
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
@option("-c", "--chunk-rows", default=1 << 22, type=IntRange(min=1), help="The drill fold's chunks: roots per measurement / rollup q range")
@option("-g", "--gen", required=True, help="The generation whose base and newest manifest's runs are folded")
@option("-j", "--jobs", default=1, type=IntRange(min=1), help="The drill fold's chunks at a time")
@option("-m", "--mount", required=True, help="Read-only local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-o", "--out", required=True, help="New local output directory outside the mount: the folded generation")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp/compact-probe", help="Local work dir")
@option("-u", "--report", help="Also write the JSON report to this URL (`gs://<scratch>/…/<name>.json`: the VM's disks go with it)")
def compact_probe_cmd(chunk_rows, gen, jobs, mount, mem, out, threads, tmp, report) -> None:
    """Fold the generation into its next one locally (`static_compact.fold_gen`, the compaction's own fold), read in place
    from the mount: no upload, no successor / pointer / manifests in the bucket, no lease, no checkpoint, no pruning; only
    OUT and TMP are written. Logs the fold's stage lines; prints a JSON of the summary (per-part seconds and peaks) and
    every output file's size and md5 (to compare with a published compaction's `compaction.json`), also written beside
    OUT and to `-u`; stdout gets it without the files."""
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
    manifest, runs = ar.newest_runs(gen_dir)
    new_gen = ar.next_gen(gen)
    err(f"compact-probe {gen} → {new_gen}: manifest {manifest}, {len(runs)} runs, -j {jobs} -M {mem} -p {threads} -c {chunk_rows}")
    summary = fold_gen(gen_dir, output, work, gen=gen, new_gen=new_gen, runs=runs, threads=threads, mem=mem, chunk_rows=chunk_rows, jobs=jobs)

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


def _spilled(ck) -> tuple[dict[str, int], dict[tuple[str, int], list[str]]]:
    """A fold checkpoint's saved spills: each file's bytes (by its path under `spilled/files/`) and the files of each
    `(kind, range)`."""
    import re

    base = ck.key("spilled/files/")
    sizes = {k[len(base):]: v for k, v in ck.store.listing(ck.key("spilled/files")).items() if k.startswith(base)}
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


def deal(weights: dict, n: int) -> list[list]:
    """`weights`' keys dealt to `n` tasks, the heaviest first, each to the least-loaded task so far (ties: the lower
    task, then the lower key): a few heavy ranges don't serialize one task."""
    import heapq

    tasks: list[list] = [[] for _ in range(n)]
    heap = [(0, i) for i in range(n)]
    for key in sorted(weights, key=lambda k: (-weights[k], k)):
        load, i = heapq.heappop(heap)
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
