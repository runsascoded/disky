"""Deferred carries for the static name index's runs (specs/static-append.md, "Deferred carries"): the static store's
merged-run build plugged into the shared runner's carries (`append_runner`: `plan_carries`, `merge_pending`,
`publish_revision`, the lease, revision manifests `manifests/<id>.m<NNN>.json`).

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
from pathlib import Path
from time import monotonic

from click import Choice, IntRange, group, option

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
from .static_names import PREFIX, connect, err
from .static_profile import data_bucket, scratch_bucket

#: The fraction of the machine's memory the concurrent tier merges' DuckDB limits sum to.
MEM_FRACTION = 0.75
#: Memory set aside per streaming (non-DuckDB) tier merge: `pyrmts.runs` holds a few batches per input.
STREAM_RESERVE = 4 << 30
#: Tiers merged by DuckDB (each gets a memory limit and threads); the others stream through `pyrmts.runs`.
DUCKDB_TIERS = ("drill", "anchors")


def gcs_store(bucket: str, scratch: str | None, gen: str, client=None) -> ar.GcsRunStore:
    """`append_runner.GcsRunStore` over `static-names/<gen>/`."""
    return ar.GcsRunStore(bucket, scratch, gen, prefix=PREFIX, client=client)


# ── Building a merged run ──────────────────────────────────────────────────


def tier_resources(tiers: list[str], ram: int, cpus: int, concurrent: bool) -> dict[str, dict]:
    """Each DuckDB tier merge's `mem` (GB, whole) and `threads`: concurrently, `MEM_FRACTION` of `ram` less a
    `STREAM_RESERVE` per streaming tier, split evenly, and the CPUs likewise; one at a time, each gets all of it."""
    duck = [t for t in tiers if t in DUCKDB_TIERS]
    if not duck:
        return {}
    budget = int(ram * MEM_FRACTION)
    if concurrent:
        budget = (budget - STREAM_RESERVE * (len(tiers) - len(duck))) // len(duck)
        cpus = max(1, cpus // len(duck))
    return {t: {"mem": f"{max(1, budget >> 30)}GB", "threads": cpus} for t in duck}


def _machine() -> tuple[int, int]:
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"), os.cpu_count() or 1


def merge_tier(name: str, dirs: list[Path], outp: Path, run: dict, rule, tmp: Path, mem: str | None = None,
               threads: int | None = None) -> tuple[str, float, dict]:
    """One tier of the merged run `run` from its input run dirs `dirs` (oldest first) into `outp`: `shards` (`sx/`,
    `sidecar*`, `shards.json`), `catalog`, `drill`, or `anchors` (`names/` + `anchors/`). Top-level, so a spawned process
    can run it. Returns `(name, seconds, doc)`."""
    from . import static_append as sa

    t0 = monotonic()
    if name == "shards":
        doc = sa.merge_shards(dirs, outp)
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
                     machine: tuple[int, int] | None = None, log: Callable[[str], None] = err) -> dict:
    """The merged run `run` (`plan_carries`' output) of input dirs `dirs` into `outp`: its tiers (`run_tiers`), each in its
    own spawned process when `jobs` > 1 (DuckDB limits per `tier_resources`), then `meta.json` (`run` + the shards' doc).
    Returns `{rows, bytes, …, "s": {tier: seconds}}`."""
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    shutil.rmtree(outp, ignore_errors=True)
    outp.mkdir(parents=True)
    tiers = run_tiers(dirs, drilled)
    ram, cpus = machine or _machine()
    res = tier_resources(tiers, ram, cpus, concurrent=jobs > 1)
    args = [(t, dirs, outp, run, rule, tmp / f"merge-{t}", res.get(t, {}).get("mem"), res.get(t, {}).get("threads")) for t in tiers]
    got: dict[str, tuple[float, dict]] = {}
    if jobs == 1:
        for a in args:
            name, s, doc = merge_tier(*a)
            got[name] = (s, doc)
            log(f"merge {run['key']}: {name} in {s:.0f}s")
    else:
        with ProcessPoolExecutor(min(jobs, len(args)), mp_context=get_context("spawn")) as ex:
            futures = [ex.submit(merge_tier, *a) for a in args]
            errs = []
            for f in futures:
                try:
                    name, s, doc = f.result()
                    got[name] = (s, doc)
                    log(f"merge {run['key']}: {name} in {s:.0f}s")
                except Exception as e:  # noqa: BLE001 — every tier runs to its end; then the first failure is raised
                    errs.append(e)
            if errs:
                raise errs[0]
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


def carry(rule=None, jobs: int = 1, build: Callable[..., dict] = build_merged_run) -> Carry:
    """The static store's `Carry`: `build` (`build_merged_run`, `jobs` tiers at once, the generation's hex-run `rule`)."""
    def b(dirs, run, outp, *, drilled, tmp, log):
        return build(dirs, run, outp, drilled=drilled, rule=rule, tmp=tmp, jobs=jobs, log=log)
    return Carry(build=b, missing=missing, drilled=drilled_runs, refuse=refuse)


def complete(store: RunStore, run: dict) -> bool:
    """Whether `run`'s dir is whole on the store (`append_runner.complete`)."""
    return ar.complete(store, carry(), run)


def publish_revision(store: RunStore, inputs: list[str], out: dict, *, attempts: int = 5, log: Callable[[str], None] = err) -> Published | None:
    return ar.publish_revision(store, carry(), inputs, out, attempts=attempts, log=log)


def merge_pending(store: RunStore, mount: Path, *, tmp: Path, rule=None, jobs: int = 1, owner: str | None = None, dry_run: bool = False,
                  max_merges: int | None = None, max_level: int | None = None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                  build: Callable[..., dict] = build_merged_run, status: ar.MergeStatus | None = None, heartbeat_s: float | None = ar.HEARTBEAT_S,
                  log: Callable[[str], None] = err) -> dict:
    """The newest manifest's due carries (`append_runner.merge_pending`) with the static store's merged-run `build`."""
    return ar.merge_pending(store, carry(rule, jobs, build), mount, tmp=tmp, owner=owner, dry_run=dry_run, max_merges=max_merges,
                            max_level=max_level, now=now, status=status, heartbeat_s=heartbeat_s, log=log)


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
@option("-g", "--gen", required=True, help="Base generation")
@option("-j", "--jobs", default=4, type=IntRange(min=1), help="Tier merges at once (each in its own process)")
@option("-L", "--compact-level", type=ar.Level(), default=ar.COMPACT_LEVEL, help=ar.LEVEL_HELP)
@option("-m", "--mount", required=True, help="Local mount of the data bucket (the merges read the runs)")
@option("-N", "--max-merges", type=IntRange(min=1), help="Stop after this many merged runs")
@option("-n", "--dry-run", is_flag=True, help="Print the plan; merge and write nothing")
@option("-S", "--scratch", default=scratch_bucket, help="Scratch bucket (the merge lease)")
@option("-T", "--tmp", default="/stage/tmp", help="Scratch dir for merges")
def carry_cmd(bucket, gen, jobs, compact_level, mount, max_merges, dry_run, scratch, tmp) -> None:
    """Run the newest manifest's due carries (`plan_carries`): each merged run into its own dir, then a revision
    `manifests/<id>.m<NNN>.json` listing it. One merger per generation (a lease in the scratch bucket, mirrored to R2's
    `static-names/<gen>/merging.json` for /health when the job has the R2 env); resumable."""
    from .static_names import gen_rule_at

    store = gcs_store(bucket, scratch, gen)
    status = None if dry_run else ar.r2_merge_status(f"{PREFIX}/{gen}")
    doc = merge_pending(store, Path(mount) / PREFIX / gen, tmp=Path(tmp), rule=gen_rule_at(bucket, gen), jobs=jobs, dry_run=dry_run,
                        max_merges=max_merges, max_level=compact_level, status=status)
    print(json.dumps(doc, indent=1))


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
