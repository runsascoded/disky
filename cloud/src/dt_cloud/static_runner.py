"""`dt-cloud static-names runs add SCAN_ID`: append one scan to the static name index as a run (specs/done/static-append.md),
in Python, so a deployment's scan job calls it directly (no `gcloud`: GCS and Batch over their APIs):

    [backfill: each listed run's missing drill / anchors, oldest first] →
    prepare → append (key ranges on Batch) → shards ∥ catalog → [drill (long ∥ short)] ∥ [anchors] → publish (local:
      `manifests/<id>.json`, the run at level 0) → R2 (each run the manifest lists, its `drill/meta.json` last; then a check
      that every served file of those runs is on R2; then the manifest, last; then the runs a carry folded, off R2) [→ verify]
      → prune (the open-version states; GCS's folded runs, each keeping `static_merge.FOLDED_RETAIN`)
    then, once after the scans: merge (the newest manifest's due carries, `static_merge`, one Batch job waited on up to
      `-w` s; then R2 for the revision it publishes) — non-fatal: the store is servable whatever becomes of it

First, every run the newest manifest lists gets the profile's drill and anchors when it lacks them (`Runner.backfill`):
a level-0 run's built as its scan's stage builds it, a merged run's merged from its scans' level-0 runs (`static_merge
tier`), oldest first, since each build reads the earlier runs' (a run published without one cuts the readers' stack there
and fails every later scan's build). Its runs reach R2 with the scan's R2 job (or, nothing appended, the newest manifest's).

The drill stage (the heavy-term drilldown, `static_drill`) runs when the profile says so (`drill`): one Batch job of two
tasks, `build -k task` (task 0 the long kind, task 1 the short; the second to finish writes `meta.json`). The anchors
stage (`static_anchors run`, the profile's `anchors`) reads none of the drill's output (nor the drill its), so the two
run at once; each keeps its own skip-if-done and post-check, and a failure of either fails the run once both end.

The unit is a scan: any deployment may scan once a day, every 6 h, or once more on demand, and each scan is its own
run (specs/done/scan-ids-not-dates.md). Scans are added **strictly in scan-id order**: given SCAN_ID, every published scan
after the generation's newest (base + live runs) and up to SCAN_ID is pending; the oldest pending must be SCAN_ID
itself, else it refuses (exit 3), or with `-c` catches up every pending scan in order. Each stage is skipped when its
output is in the data bucket, so a rerun resumes at the first missing one; the R2 copy skips objects already there,
so it always runs.

Exit status: 0 done, 3 not published yet or not the next scan, 1 a stage failed (the merge stage never fails the run).

The chain's machinery (order, stage skipping, Batch, the merge stage, exit codes) is `append_runner`'s, shared with the
interval store; this module is the static store's stage list and jobs.

Config: the deployment profile (`static_profile`: `STATIC_NAMES_PROFILE` = an example or a JSON file, then each
field's env var); a run needs its generation, buckets, layouts, region, image, accounts, R2 bucket and R2 secrets.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from math import ceil

from click import argument, command, option

from . import append_runner as ar
from .append_runner import (  # noqa: F401 — the shared names, as this module has always offered them
    NOT_NEXT,
    BatchRunner,
    NotNext,
    StillRunning,
    duckdb_args,
    exit_on,
    pending_scans,
    task_command,
)
from .static_merge import FOLDED_RETAIN, LONG_ROWS
from .static_names import PREFIX, err
from .static_profile import Profile, level_arg, profile

#: A stage's cost-attribution `component` (`cost_labels`); the rest of the chain is `static-names`.
STAGE_COMPONENTS = {"drill": "drill", "anchors": "anchors", "drill-merge": "drill", "anchors-merge": "anchors"}


def ready(p: Profile, gen: str | None = None) -> Profile:
    """`p` (with `gen` over its own) checked for everything a run needs; the project defaults to the credentials'."""
    if gen:
        p = replace(p, gen=gen)
    if not p.project:
        from .gcp import gcp_project

        p = replace(p, project=gcp_project())
    for f in ("gen", "bucket", "scratch", "layouts", "region", "image", "sa", "r2_bucket"):
        p.need(f)
    p.hex_rule()
    p.r2_env_secrets()
    return p


def job_spec(cfg: Profile, name: str, tasks: int, commands: list[str], *, stage: str, scratch: bool = True, r2: bool = False,
             machine: str | None = None, ssd_gb: int | None = None, purpose: str = "static-names", component: str | None = None, long: bool = False) -> dict:
    """`append_runner.job_spec` labelled `purpose` (default `static-names`), its cost `component` the stage's
    (`STAGE_COMPONENTS`, else `static-names`)."""
    return ar.job_spec(cfg, name, tasks, commands, stage=stage, scratch=scratch, r2=r2, machine=machine, ssd_gb=ssd_gb, purpose=purpose,
                       component=component or STAGE_COMPONENTS.get(stage, "static-names"), long=long)


def job_id(stage: str, scan_id: str, now: datetime | None = None) -> str:
    """`sn-<stage>-<scan>-<hhmmss>`: Batch ids are lowercase letters, digits and hyphens."""
    return ar.job_id("sn", stage, scan_id, now)


def drill_duckdb_args(machine: str) -> list[str]:
    """Reserve half the task's memory for Arrow, Python indexes/caches and allocations outside DuckDB's buffer manager."""
    vcpus = int(machine.rsplit("-", 1)[-1])
    return ["-M", f"{vcpus * 7700 // 2 // 1024}GB", "-p", str(vcpus)]


#: vCPUs per chunk the compaction's drill fold runs at once: one chunk's query kept ~9 of 48 cores busy (gcs 10-23).
FOLD_VCPUS = 6


def fold_jobs(machine: str) -> int:
    """The compaction's `-j` on `machine` (`static_merge compact -j`): key ranges, census shards and drill-fold chunks at a time, one per `FOLD_VCPUS`."""
    return max(1, int(machine.rsplit("-", 1)[-1]) // FOLD_VCPUS)


# ── The chain ──────────────────────────────────────────────────────────────


@dataclass(kw_only=True)
class Runner(ar.Runner):
    """The static store's chain (`append_runner.Runner`): `publish(scan_id)` is the `runs` CLI's local publish;
    `verify_terms`: also run `runs verify` with this terms file."""
    publish: Callable[[str], None]
    verify_terms: str | None = None

    store_prefix = PREFIX
    job_prefix = "sn"
    long_merge_rows = LONG_ROWS

    def layouts(self, base: dict):
        """The base's recorded layouts (None: the profile's). Runs follow the base's recorded hex-run rule (every stage
        reads it from the base's `scans.json`), so a run's tiers always agree; a profile whose rule differs (a deployment
        that adopts the rule at its next generation) is logged, not applied."""
        from .hex_runs import rule_from_json

        recorded, wanted = rule_from_json(base.get("hex_runs")), self.cfg.hex_rule()
        if recorded != wanted:
            self.log(f"{self.cfg.gen}: built with hex_runs {recorded.to_json() if recorded else 'off'}, the profile says "
                     f"{wanted.to_json() if wanted else 'off'}: its runs keep the generation's rule (the profile's applies to the next generation)")
        return base.get("layouts")

    def spec(self, name: str, tasks: int, commands: list[str], *, stage: str, **kw) -> dict:
        return job_spec(self.cfg, name, tasks, commands, stage=stage, **kw)

    def drilled(self, runs: list[dict]) -> set[str]:
        return {r["key"] for r in runs if self.exists(f"{self.root}/{r['key']}/drill/meta.json")}

    def merge_job(self, scan: str, long: bool = False) -> tuple[str, dict]:
        return self.job("merge", scan, 1, "static_merge", ["carry", "-g", self.cfg.gen, "-L", level_arg(self.cfg.policy.carry_level), *self.read_args()], r2=self.merge_r2(),
                        long=long)

    def compact_jobs(self, scan: str) -> list[tuple[str, dict]]:
        r2 = self.merge_r2()
        out = [self.job("compact", scan, 1, "static_merge", ["compact", "-g", self.cfg.gen, *(["-S", self.cfg.scratch] if self.cfg.scratch else []),
                                                           "-C", str(self.cfg.policy), *(["-K"] if self.compact_now else []), *self.read_args(),
                                                           *(["-r"] if r2 else []), "-j", str(self.cfg.fold_jobs or fold_jobs(self.cfg.machine_for("compact"))),
                                                           *(["-D", str(self.cfg.measure_tasks)] if self.cfg.measure_tasks > 1 else []),
                                                           *(["-A", str(self.cfg.anchor_tasks)] if self.cfg.anchor_tasks > 1 else []),
                                                           *(["-L", str(self.cfg.fold_tasks)] if self.cfg.fold_tasks > 1 else []),
                                                           *duckdb_args(self.cfg.machine_for("compact"))], r2=r2)]
        if self.cfg.measure_tasks > 1 or self.cfg.anchor_tasks > 1 or self.cfg.fold_tasks > 1:
            # its helper jobs are built from the profile, as this runner's are
            from .static_profile import to_mapping

            out[0][1]["taskGroups"][0]["taskSpec"]["environment"]["variables"]["STATIC_NAMES_PROFILE"] = json.dumps(to_mapping(self.cfg))
        if not r2:
            out.append(self.job("compact-r2", scan, 1, "static_merge", ["compact-r2", "-g", self.cfg.gen], r2=True, mount=False))
        return out

    def with_gen(self, gen: str) -> Profile:
        return replace(self.cfg, gen=gen)

    def one(self, d: str) -> None:
        for _, step in self.steps(d):
            step()

    def steps(self, d: str) -> list[tuple[str, Callable[[], None]]]:
        """Scan `d`'s chain, as `(name, fn)` steps in order; each checks its own outputs when it runs (a done one logs and
        returns), so a rerun resumes at the first missing one."""
        run = f"{self.root}/deltas/{d}"
        g = ["-g", self.cfg.gen, "-d", d]

        def prepare() -> None:
            # pins the scan as the run's `scans.json` (checks it is the next one) and lists it as pending
            self.log(f"{d}: gen {self.cfg.gen}")
            if self.exists(f"{run}/scans.json"):
                self.log(f"{d} prepare: done")
            else:
                self.stage(f"{d} prepare", lambda: self.prepare(d))

        def append() -> None:
            # every key range, `append_tasks` tasks
            k = self.read_json(f"{self.root}/ranges.json")["k"]
            n = self.count(f"{run}/dhist/", ".parquet")
            if n >= k:
                self.log(f"{d} append: done ({n}/{k} ranges)")
            else:
                per = ceil(k / self.cfg.append_tasks)
                name, spec = self.job("append", d, ceil(k / per), "static_append", ["append", *g, "-n", str(per)])
                self.stage(f"{d} append ({n}/{k} ranges)", lambda: self.run_job(name, spec))

        def shards_catalog() -> None:
            # both read only the run's `cdelta/`
            jobs = []
            for stage, done in (("shards", "sidecar.parquet"), ("catalog", "catalog/meta.json")):
                if self.exists(f"{run}/{done}"):
                    self.log(f"{d} {stage}: done")
                else:
                    jobs.append((stage, partial(self.run_job, *self.job(stage, d, 1, "static_append", [stage, *g]))))
            self.concurrently(d, jobs)

        def publish() -> None:
            # local, seconds: `manifests/<d>.json` (written once), the run at level 0; carries come after (`carries`)
            if self.exists(f"{self.root}/manifests/{d}.json"):
                self.log(f"{d} publish: done")
            else:
                self.stage(f"{d} publish", lambda: self.publish(d))

        def verify() -> None:
            # optional: brute force from the scan file vs the base + runs
            if not self.verify_terms:
                return
            if self.exists(f"{run}/verify.json"):
                self.log(f"{d} verify: done")
            else:
                name, spec = self.job("verify", d, 1, "static_append", ["verify", *g, "-t", self.verify_terms])
                self.stage(f"{d} verify", lambda: self.run_job(name, spec))

        return [
            ("prepare", prepare),
            ("append", append),
            ("shards ∥ catalog", shards_catalog),
            # the profile's `drill`, `anchors`: neither reads the other's output, so they run at once
            ("drill ∥ anchors", lambda: self.drill_anchors(d)),
            ("publish", publish),
            # the runs the manifest lists, then the manifest, last
            ("r2", lambda: self.r2(d)),
            ("verify", verify),
            # only the newest complete open-version state is kept
            ("prune", lambda: self.stage(f"{d} prune", lambda: self.prune(d))),
        ]

    def run_scans(self, todo: list[str]) -> None:
        """A backlog's chains, `pipeline_depth` scans in flight (specs/static-backlog-pipeline.md): step `j` of a scan
        starts once its own step `j − 1` and step `j` of the scan before it are done, so the backlog advances at its
        slowest step, not their sum; every step of a scan follows the same step of the scans before it (publishes in
        order; a scan's prepare lists it as pending, so the next one's stages read it before it publishes). A failed step
        stops its scan and every later one before their next step (the earlier ones go on: they never read it); the steps
        running finish; then the earliest scan's failure is raised. At depth 1, or one scan, `one` in turn."""
        depth = max(1, self.cfg.pipeline_depth)
        if depth == 1 or len(todo) < 2:
            super().run_scans(todo)
            return
        from threading import Event, Lock

        steps = [self.steps(s) for s in todo]
        done = [[Event() for _ in st] for st in steps]
        failed: list[tuple[int, BaseException]] = []
        lock = Lock()

        def chain(i: int) -> None:
            try:
                for j, (_, step) in enumerate(steps[i]):
                    if i:
                        done[i - 1][j].wait()
                    if any(k < i for k, _ in failed):
                        return
                    if j == 0:
                        self.wait_compaction()
                    step()
                    done[i][j].set()
            except BaseException as e:
                with lock:
                    failed.append((i, e))
            finally:
                # a stopped scan releases the scans after it, which then see the failure and stop too
                for e in done[i]:
                    e.set()

        self.log(f"pipelining {len(todo)} scans, {depth} in flight")
        with ThreadPoolExecutor(depth) as ex:
            list(ex.map(chain, range(len(todo))))
        if failed:
            raise min(failed, key=lambda f: f[0])[1]

    # ── Backfill: every listed run's drill and anchors ───────────────────────

    def tier_done(self, tier: str, key: str) -> bool:
        """Whether run `key` has `tier` (`drill`: `drill/meta.json`; `anchors`: `anchors/meta.json`, and the starts-with
        catalog's `anchors/start/meta.json` when the base carries one)."""
        if tier == "drill":
            return self.exists(f"{self.root}/{key}/drill/meta.json")
        return self.exists(f"{self.root}/{key}/anchors/meta.json") and (
            not self.exists(f"{self.root}/anchors/start/meta.json") or self.exists(f"{self.root}/{key}/anchors/start/meta.json"))

    def backfill_plan(self, tier: str) -> list[tuple[str, str]]:
        """The steps that give every run the newest manifest lists its `tier`, oldest first: `("build", scan)` a level-0 run's
        (`static_drill build` / `static_anchors run`, which read the runs of the newest manifest before that scan, so
        those come first), `("merge", key)` a merged run's from its scans' level-0 runs (`static_merge tier`, after
        theirs). A run that has it is skipped, and so is everything it was built over: a build refuses missing earlier
        tiers, so no existing drill or anchors was built over a stack that lacked one, and none needs rebuilding."""
        keys = self.manifests()
        if not keys:
            return []
        names = [k.removeprefix(f"{self.root}/") for k in keys]
        plan: list[tuple[str, str]] = []
        seen: set[str] = set()

        def ensure(key: str, scans: list[str]) -> None:
            if key in seen or self.tier_done(tier, key):
                return
            seen.add(key)
            if len(scans) == 1:
                prior = ar.latest_key(names, before=scans[0])
                for r in self.read_json(f"{self.root}/{prior}")["runs"] if prior else []:
                    ensure(r["key"], r["scans"])
                plan.append(("build", scans[0]))
            else:
                for sc in scans:
                    ensure(f"deltas/{sc}", [sc])
                plan.append(("merge", key))

        for r in self.read_json(keys[-1])["runs"]:
            ensure(r["key"], r["scans"])
        return plan

    def backfill(self) -> bool:
        """Every run the newest manifest lists gets the profile's drill (`drill`) and anchors (`anchors`, when the base has
        them), each missing one in `backfill_plan`'s order: cw's 10-10 catch-up published the merged run
        `deltas/2026-10-09T1801_2026-10-10T0001` without `drill/` (an image from before cw's `drill`), which cut the
        readers' drill stack there and failed every later scan's drill build. The drill's and the anchors' steps run as
        two chains at once (neither reads the other's); a failure fails the run, as the drill stage's does, once both
        end. Returns whether any step ran (its runs then need the R2 copy: the caller's)."""
        chains = []
        for tier, on in (("drill", self.cfg.drill), ("anchors", self.cfg.anchors and self.exists(f"{self.root}/anchors/meta.json"))):
            if on and (plan := self.backfill_plan(tier)):
                self.log(f"backfill {tier}: {', '.join(f'{k} {x}' for k, x in plan)}")
                chains.append((f"{tier} backfill ({len(plan)} steps)", partial(self._backfill_chain, tier, plan)))
        self.concurrently("backfill", chains)
        return bool(chains)

    def _backfill_chain(self, tier: str, plan: list[tuple[str, str]]) -> None:
        for kind, x in plan:
            if kind == "build":
                job = self.drill(x) if tier == "drill" else self.anchors(x)
                if job:
                    self.stage(f"{x} {job[0]}", job[1])
            else:
                self.stage(f"{x} {tier} merge", partial(self.tier_merge, tier, x))

    def tier_merge(self, tier: str, key: str) -> None:
        """Merged run `key`'s `tier` from its scans' level-0 runs: one job, `static_merge tier` (`backfill_tier`), then a
        check that its liveness marker is there."""
        last = key.rsplit("_", 1)[-1]
        name, spec = self.job(f"{tier}-merge", last, 1, "static_merge", ["tier", "-g", self.cfg.gen, "-r", key, "-t", tier,
                                                                         *duckdb_args(self.cfg.machine_for(f"{tier}-merge"))])
        self.run_job(name, spec)
        if not self.tier_done(tier, key):
            raise RuntimeError(f"{self.root}/{key}: its {tier} not written (the merge job succeeded)")

    def drill_anchors(self, d: str) -> None:
        """The run's drill and anchors (each when the profile says so and its output is missing), at once: the drill reads
        the run's `sx/`, `cdelta/`, `sidecar.parquet` and catalog and the earlier runs' `drill/`; the anchors the run's
        `sx/` and `cdelta/` and the earlier tiers' `names/` and `anchors/`; neither the other's (publish, after both,
        merges each)."""
        self.concurrently(d, [j for j in (self.cfg.drill and self.drill(d), self.cfg.anchors and self.anchors(d)) if j])

    def drill(self, d: str) -> tuple[str, Callable[[], None]] | None:
        """The run's `drill/` job (None when it's done): one job of two tasks (long ∥ short), each on a whole machine;
        done once `meta.json` is there."""
        run = f"{self.root}/deltas/{d}"
        if self.exists(f"{run}/drill/meta.json"):
            self.log(f"{d} drill: done")
            return None
        ck = ("-S", self.cfg.drill_checkpoint) if self.cfg.drill_checkpoint else ()
        args = ["build", "-g", self.cfg.gen, "-d", d, "-k", "task", *drill_duckdb_args(self.cfg.machine_for("drill")), *ck]
        name, spec = self.job("drill", d, 2, "static_drill", args)

        def build():
            self.run_job(name, spec)
            if not self.exists(f"{run}/drill/meta.json"):
                raise RuntimeError(f"{run}/drill/meta.json: not written (both tasks succeeded)")
        return "drill (long ∥ short)", build

    def anchors(self, d: str) -> tuple[str, Callable[[], None]] | None:
        """The run's `names/` and `anchors/` job (`static_anchors run`; None when it's done): one task; done once
        `anchors/meta.json` is there, and `anchors/start/meta.json` (the starts-with catalog) when the base carries one."""
        run = f"{self.root}/deltas/{d}"
        start = self.exists(f"{self.root}/anchors/start/meta.json")
        if self.exists(f"{run}/anchors/meta.json") and (not start or self.exists(f"{run}/anchors/start/meta.json")):
            self.log(f"{d} anchors: done")
            return None
        args = ["run", "-g", self.cfg.gen, "-d", d, *duckdb_args(self.cfg.machine_for("anchors"))]
        name, spec = self.job("anchors", d, 1, "static_anchors", args)

        def build():
            self.run_job(name, spec)
            for m in ("anchors/meta.json", *(("anchors/start/meta.json",) if start else ())):
                if not self.exists(f"{run}/{m}"):
                    raise RuntimeError(f"{run}/{m}: not written (the task succeeded)")
        return "anchors", build

    def r2(self, d: str, manifest: str | None = None) -> None:
        """One job: `r2-copy` of each run the manifest `manifests/<manifest>.json` (default `d`'s own) lists (its liveness
        markers last: `drill/meta.json`, then the starts-with catalog's `anchors/start/meta.json` before `anchors/meta.json`,
        so a run's anchors go live with their catalog), then `r2-verify` (every served file of those runs on R2), then
        `r2-copy` of that manifest alone (each copy skips what R2 holds): no manifest reaches R2 before its runs are checked
        there."""
        stem = manifest or d
        key = f"{self.root}/manifests/{stem}.json"
        runs = [r["key"] for r in self.read_json(key)["runs"]] if self.exists(key) else [f"deltas/{d}"]
        if self.dry_run:
            self.log(f"{d} r2: would copy {', '.join(runs)}, check them, then copy manifests/{stem}.json")
            return
        cmds = []
        for r in runs:
            cmds.append(self.command("static_names", ["r2-copy", "-g", f"{self.cfg.gen}/{r}", "-x", "drill/meta.json", "-x", "anchors/meta.json",
                                                                "-x", "anchors/start/meta.json"], mount=False))
            for m in ("drill/meta.json", "anchors/start/meta.json", "anchors/meta.json"):
                cmds.append(self.command("static_names", ["r2-copy", "-g", f"{self.cfg.gen}/{r}", "-o", m], mount=False))
        cmds.append(self.command("static_names", ["r2-verify", "-g", self.cfg.gen, "-m", stem], mount=False))
        cmds.append(self.command("static_names", ["r2-copy", "-g", self.cfg.gen, "-o", f"manifests/{stem}.json"], mount=False))
        # then, the manifest on R2, the runs a carry folded (planned by R2's own manifests)
        cmds.append(self.command("static_merge", ["prune", "-g", self.cfg.gen, "-r"], mount=False))
        name = self.job_name("r2", d)
        spec = self.spec(name, 1, cmds, stage="r2", scratch=False, r2=True, machine="n2-highmem-4", ssd_gb=375)
        spec = ar.label_batch_spec(spec, "static-names", scan_id=d)
        self.stage(f"{d} r2 ({len(runs)} runs + manifests/{stem}.json)", lambda: self.run_job(name, spec))


def gcs_runner(cfg: Profile, *, dry_run: bool = False, verify_terms: str | None = None, merge: bool = True,
               merge_wait: float | None = 0) -> Runner:
    """`Runner` over the real data bucket, Batch, and the `runs` CLI's `prepare` / `publish` / `prune`."""
    from google.cloud import storage

    from . import static_append as sa
    from .static_names import list_scans, read_json

    client = storage.Client(project=cfg.project)
    b = client.bucket(cfg.bucket)

    def published(layouts, start):
        return [s["id"] for s in list_scans(cfg.bucket, layouts=layouts or cfg.layouts, start=start)["scans"]]

    # the local stages read the runner's generation when they run: `follow` moves it to a compacted successor
    def prepare(d):
        try:
            sa.prepare_cmd.callback(bucket=cfg.bucket, date=d, gen=runner.cfg.gen)
        except SystemExit as e:
            raise NotNext(str(e)) from e

    def publish(d):
        sa.publish_cmd.callback(bucket=cfg.bucket, date=d, gen=runner.cfg.gen, dry_run=False)

    def prune(d):
        gen = runner.cfg.gen
        k = read_json(f"gs://{cfg.bucket}/{PREFIX}/{gen}/ranges.json")["k"]
        doc = sa.prune_state(client, gen, d, k, bucket=cfg.bucket, scratch=cfg.scratch, dry_run=dry_run)
        err(json.dumps(doc))
        folded = ar.prune_folded(ar.GcsRunStore(cfg.bucket, None, gen, prefix=PREFIX, client=client), retain=FOLDED_RETAIN,
                                 dry_run=dry_run, where=" gcs")
        err(json.dumps({k_: v for k_, v in folded.items() if k_ != "keys"}))

    runner = Runner(
        cfg=cfg,
        exists=lambda key: b.blob(key).exists(),
        count=lambda prefix, suffix: sum(1 for x in client.list_blobs(cfg.bucket, prefix=prefix) if x.name.endswith(suffix)),
        read_json=lambda key: read_json(f"gs://{cfg.bucket}/{key}"),
        published=published,
        run_job=BatchRunner(cfg, err),
        prepare=prepare,
        prune=prune,
        publish=publish,
        dry_run=dry_run,
        verify_terms=verify_terms,
        merge=merge,
        merge_wait=merge_wait,
        list_keys=lambda prefix: [x.name for x in client.list_blobs(cfg.bucket, prefix=prefix)],
        coordination=None if dry_run else lambda: ar.gcs_coordinator_lease(cfg.scratch, PREFIX, client),
        compacting=None if dry_run or not cfg.scratch else lambda: ar.gcs_compaction_lease_file(cfg.scratch, PREFIX, client),
    )
    return runner


@command("add")
@option("-c", "--catch-up", is_flag=True, help="Append every earlier published scan still pending first, in scan-id order")
@option("-g", "--gen", help="Base generation (default: the profile's, $STATIC_NAMES_GEN)")
@option("-K", "--compact-now", is_flag=True, help="After the merge stage, compact into a new generation whatever the policy says (when the generation has runs)")
@option("-M", "--no-merge", is_flag=True, help="Skip the merge stage (the due carries wait for a later run, or `runs merge`)")
@option("-n", "--dry-run", is_flag=True, help="Report each stage's state and what would run; submit and write nothing")
@option("-t", "--verify-terms", help="Also run `runs verify` with this terms file (a gs:// URL)")
@option("-w", "--merge-wait", default=0, type=float, help="Seconds to wait on the merge job (default 0: submit it and go; it publishes on GCS when done)")
@argument("scan_id")
def add_cmd(catch_up: bool, gen: str | None, compact_now: bool, no_merge: bool, dry_run: bool, verify_terms: str | None, merge_wait: float,
            scan_id: str) -> None:
    """Append SCAN_ID to the static name index: prepare → append → shards ∥ catalog → [drill] ∥ [anchors] → publish → R2 → prune, each stage
    skipped when its output exists; then the merge stage (the due carries, non-fatal). Exit 3 when SCAN_ID is not published
    yet, or an earlier published scan is pending (without -c); exit 4 when another `runs add` holds the coordinator lease.
    A generation compacted since the profile named it is followed to its successor; when the compaction policy fires (or
    `-K`), the compaction stage follows the merge stage."""
    cfg = ready(profile(), gen)
    runner = gcs_runner(cfg, dry_run=dry_run, verify_terms=verify_terms, merge=not no_merge, merge_wait=merge_wait)
    runner.compact_now = compact_now
    done = exit_on(f"static-names runs add {scan_id}", lambda: runner.run(scan_id, catch_up=catch_up), lambda m: err(m))
    print(json.dumps({"gen": runner.cfg.gen, "scan": scan_id, "appended": done, "dry_run": dry_run, "compaction": runner.compaction}))


#: The static index lags when more than `LAG_SCANS` published scans are past its newest, or any one has been for longer
#: than `LAG_S` (specs/static-backlog-pipeline.md): search refuses those scans, the site's default view among them.
LAG_SCANS, LAG_S = 1, 12 * 3600
#: The lag episode's marker in the scratch bucket (`runs lag`): one alert per episode, not one per run.
LAG_MARKER = "lag-alert.json"


def lag_state(newest: str | None, published: list[str], scan: str) -> dict:
    """How far the index (newest scan `newest`) is behind `scan`: the published scans after `newest` up to `scan`
    (`behind`), the seconds from the oldest of them to `scan` (`s`), and whether that is lagging (`LAG_SCANS`, `LAG_S`)."""
    from .scan_id import scan_epoch

    behind = sorted(x for x in published if (newest is None or x > newest) and x <= scan)
    secs = scan_epoch(scan) - scan_epoch(behind[0]) if behind else 0
    return {"newest": newest, "behind": behind, "s": secs, "lagging": len(behind) > LAG_SCANS or (bool(behind) and secs > LAG_S)}


def lag_episode(lagging: bool, marked: bool) -> str | None:
    """`start` when a lag begins (no marker yet: alert, then mark), `end` when it clears (unmark), else None."""
    if lagging and not marked:
        return "start"
    if marked and not lagging:
        return "end"
    return None


@command("lag")
@option("-g", "--gen", help="Base generation (default: the profile's, $STATIC_NAMES_GEN)")
@argument("scan_id")
def lag_cmd(gen: str | None, scan_id: str) -> None:
    """Whether the static index lags SCAN_ID (`lag_state`), as JSON, with `episode`: `start` the first time it does (the
    marker `<gen>/lag-alert.json` in the scratch bucket is then written: the caller alerts once), `end` once it no longer
    does (the marker removed), else null. Read-only but for the marker."""
    from google.cloud import storage

    cfg = ready(profile(), gen)
    runner = gcs_runner(cfg, dry_run=True)
    runner.follow()
    have, layouts = runner.have()
    doc = lag_state(have[-1] if have else None, runner.published(layouts, have[-1] if have else ""), scan_id)
    blob = storage.Client(project=cfg.project).bucket(cfg.scratch).blob(f"{PREFIX}/{runner.cfg.gen}/{LAG_MARKER}") if cfg.scratch else None
    episode = lag_episode(doc["lagging"], bool(blob and blob.exists()))
    if blob is not None and episode == "start":
        blob.upload_from_string(json.dumps({"scan": scan_id, **doc}) + "\n")
    elif blob is not None and episode == "end":
        blob.delete()
    print(json.dumps({"gen": runner.cfg.gen, "scan": scan_id, **doc, "episode": episode}))


def check_priors(read_json: Callable[[str], dict], exists: Callable[[str], bool], root: str, scan: str) -> dict:
    """Scan `scan`'s run: the tiers its drill and anchors record they were built over (`meta.json`'s `prior`) against
    the runs its own manifest (`manifests/<scan>.json`) lists before it, `base` first: what a pipelined backlog could get
    wrong (specs/static-backlog-pipeline.md). Per tier `ok`, `differs` (with both lists), `unrecorded` (a build from
    before the field), or `absent`."""
    m = read_json(f"{root}/manifests/{scan}.json")
    keys = [r["key"] for r in m["runs"]]
    want = ["base", *keys[:keys.index(f"deltas/{scan}")]]
    out: dict = {"scan": scan, "want": want}
    for tier in ("drill", "anchors"):
        key = f"{root}/deltas/{scan}/{tier}/meta.json"
        if not exists(key):
            out[tier] = "absent"
            continue
        got = read_json(key).get("prior")
        out[tier] = "unrecorded" if got is None else "ok" if got == want else {"differs": got}
    return out


@command("check-priors")
@option("-g", "--gen", help="Base generation (default: the profile's, $STATIC_NAMES_GEN)")
@argument("scans", nargs=-1, required=True)
def check_priors_cmd(gen: str | None, scans: tuple[str, ...]) -> None:
    """Each SCAN's run's drill and anchors built over the tiers its manifest lists before it (`check_priors`), one JSON
    line per scan; then that no pending listing is left (`pending/`). Exit 1 when any tier differs or a listing is left."""
    cfg = ready(profile(), gen)
    runner = gcs_runner(cfg, dry_run=True)
    runner.follow()
    root = f"{PREFIX}/{runner.cfg.gen}"
    bad = False
    for scan in scans:
        doc = check_priors(runner.read_json, runner.exists, root, scan)
        bad |= any(isinstance(doc[t], dict) for t in ("drill", "anchors"))
        print(json.dumps(doc))
    left = runner.list_keys(f"{root}/{ar.PENDING}")
    print(json.dumps({"pending": left}))
    if bad or left:
        raise SystemExit(1)


@command("merge")
@option("-g", "--gen", help="Base generation (default: the profile's, $STATIC_NAMES_GEN)")
@option("-n", "--dry-run", is_flag=True, help="Report the due carries; submit and write nothing")
@option("-w", "--wait", type=float, help="Seconds to wait on the merge job (default: to its end, Batch's own cap)")
def merge_cmd(gen: str | None, dry_run: bool, wait: float | None) -> None:
    """The newest manifest's due carries, on their own: first every listed run's missing drill / anchors (the profile's;
    `Runner.backfill`, then the R2 job for the newest manifest), then the merge job (`static_merge carry`), then R2 for the
    revision it publishes (`runs add`'s merge stage, alone: for a schedule of its own, or to catch up). Exit 1 when either
    fails (the store stays as it was: servable)."""
    cfg = ready(profile(), gen)
    runner = gcs_runner(cfg, dry_run=dry_run, merge_wait=wait)

    def merge() -> None:
        if runner.backfill():
            runner.r2_newest()
        runner.carries(fatal=True)
    exit_on("static-names runs merge", merge, lambda m: err(m))
    print(json.dumps({"gen": cfg.gen, "manifest": (runner.manifests() or [None])[-1], "dry_run": dry_run}))
