"""`dt-cloud scan head` (specs/pipelines-everywhere.md §2.3): one scan's DAG (`scan_dag.build` over the profile) run
by a backend (`scan_backend`; specs/orchestrator-abstraction.md §3). The only one is `NextflowBackend`: the DAG
rendered as `scan.nf` in the launch dir (`scan_dag_nf.render`: `job/nf/prelude.nf` + the processes and workflow).

It stamps the scan's identity once (`SNAP_ID`, `GEN`, `DT_JOB_START`), starts its scan-run record (in the profile's
`store`), derives the Nextflow session from the scan (uuid5 of `disky/<deployment>/scan/<scan>`, or
`disky/<deployment>/<store>/scan/<scan>` for a store other than the primary), writes the session's `.nextflow/history`
line (Nextflow finds a session only there; its cache is in the work store, `NXF_CLOUDCACHE_PATH`), writes the params
and the executor config from the profile, runs Nextflow with `-resume <session>`, and finishes the record with
Nextflow's exit status (the first failed task named) and the labelled cost of its tasks. Re-running the head for a
failed scan (`SNAP_ID=<scan> GEN=<its gen>`), anywhere, re-runs only the tasks that didn't finish.

A TERM / INT (a cancelled or preempted head job) stops Nextflow's process group (the JVM is the launcher script's
child), waits `NF_SWEEP_DELAY` (30 s: a submit in flight lands after Nextflow exits), cancels whatever Batch job still
carries the session's `nf-session` label, and finishes the record.

What started the run (`$DT_TRIGGER`: `schedule` from the deployment's cron body, `manual` from its hand submits; unset:
not recorded) goes on its run record; a scan's trigger is its first run's (the digest's `scan_triggers`). Not into the
steps' params: a resume of a cron scan by hand must hit the same Nextflow cache.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from . import scan_dag
from .cost_labels import ENV as LABELS_ENV, check_label, deployment_labels
from .heartbeat import PUMP_JOIN_S, Tail, clean_line, fmt_secs, set_detail, set_stage
from .scan_backend import Backend, Run
from .scan_dag import ScanDag
from .scan_dag_nf import render
from .scan_profile import PRIMARY_STORE, Disk, Machine, ScanProfile
from .scan_steps import DATA_MOUNT_ENV, PROFILE_JSON_ENV, profile_env_value


def err(*a: object) -> None:
    print(*a, file=sys.stderr)

#: The fixed part of each run's `scan.nf`, in the head's `nf_dir` (`job/nf`): the rest is rendered from the scan's DAG.
PRELUDE = "prelude.nf"
NF_GOOGLE = "nf-google@1.29.1"
NF_CLOUDCACHE = "nf-cloudcache@0.6.2"
#: Where a google-batch task sees its work dir's bucket (and so the data store): Batch mounts it at `/mnt/disks/<b>`.
BATCH_MOUNT = "/mnt/disks"


def identity(env: Mapping[str, str], now: dt.datetime | None = None) -> tuple[str, str, str]:
    """(SNAP_ID, GEN, DT_JOB_START): each from `env` when set (a resumed scan keeps its own), else stamped now."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return (env.get("SNAP_ID") or now.strftime("%Y-%m-%dT%H%M"), env.get("GEN") or now.strftime("%Y%m%dT%H%M%SZ"),
            env.get("DT_JOB_START") or str(int(now.timestamp())))


def session_id(deployment: str, scan: str, store: str = PRIMARY_STORE) -> str:
    """The scan's Nextflow session: uuid5 of `disky/<deployment>/scan/<scan>` (the primary store's: unchanged, so a scan
    started before stores existed resumes), `disky/<deployment>/<store>/scan/<scan>` for another store (its scan of the
    same id is another run)."""
    ns = f"disky/{deployment}/scan/{scan}" if store == PRIMARY_STORE else f"disky/{deployment}/{store}/scan/{scan}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ns))


def history_line(now: dt.datetime, scan: str, session: str) -> str:
    """The one `.nextflow/history` line `-resume <session>` needs."""
    run_name = "scan_" + "".join(c if c.isalnum() else "_" for c in scan)
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')}\t-\t{run_name}\tOK\t-\t{session}\tnextflow run scan.nf\n"


def labels(profile: ScanProfile, env: Mapping[str, str], scan: str) -> dict[str, str]:
    """The tasks' Batch labels: the profile's, the stack's (`$DISKY_LABELS`, `k=v,…`, refused without `app` and
    `deployment`: `cost_labels.deployment_labels`), `component: scan`, `scan_id` (lowercased, as `cost_labels` writes
    it); `base.config` adds `nf-session` and each task's `step`, so a billing export groups a scan's spend by scan and step."""
    out = {**profile.labels, **deployment_labels(dict(env))}
    out["component"] = "scan"
    out["scan_id"] = scan.lower()
    check_label("scan_id", out["scan_id"])
    return out


def _disk(d: Disk | None) -> dict | None:
    return None if d is None else {"request": d.request, "type": d.type}


def _bindings(m: Mapping[str, str]) -> list[str]:
    return [f"{k}={v}" for k, v in m.items()]


def step_secrets(profile: ScanProfile) -> dict[str, list[str]]:
    """Each step whose call fetches other bindings than the profile's `secrets` (`options.<step>.secrets`), its whole
    `ENV=SECRET` list: scan.nf passes a call `params.step_secrets[<step>]` when set, else `params.secrets`."""
    return {n: _bindings(profile.secrets_for(n)) for n, o in profile.options.items() if o.get("secrets")}


def params(profile: ScanProfile, profile_json: str, scan: str, gen: str, job_start: str, run_id: str, session: str,
           env: Mapping[str, str], *, reproc: bool = False, fresh: bool = False, dag: ScanDag | None = None) -> dict:
    """`scan.nf`'s params file: the run's facts, the list tasks' sources, secrets and env. Which processes run, and on
    which labels, is the DAG's (`scan_dag.build`), rendered into `scan.nf` itself."""
    dag = dag or scan_dag.build(profile, reproc=reproc)
    task_labels = labels(profile, env, scan)
    from .image_pin import IMAGE_ENVS
    if run_id:  # the run's tasks, so a scan's cost sums its runs' jobs by it (`scan_cost.select_jobs`)
        task_labels["scan_run"] = run_id.lower()
        check_label("scan_run", task_labels["scan_run"])
    step_env = dict(profile.env)
    step_env.update({k: env[k] for k in profile.passthrough if k in env})
    # every step's Batch helpers (static names, the interval store, …) label themselves from it (`label_batch_spec`)
    step_env[LABELS_ENV] = env[LABELS_ENV]
    if env.get(PIN_ENV):
        # the run's digest reaches the tasks through `run.config`'s env scope (`run_config`), out of the task hash; a
        # profile's tag here would be exported over it in each task's script
        for k in IMAGE_ENVS:
            step_env.pop(k, None)
    if reproc:
        step_env["REPROC"] = "1"
    if profile.executor.kind == "google-batch":
        step_env[DATA_MOUNT_ENV] = BATCH_MOUNT
    lst = profile.machines.get("list")
    listed = [t.tag for t in dag["list_source"].tasks] if "list_source" in dag.names else []
    return {
        "scan": scan, "gen": gen, "job_start": job_start, "run_id": run_id, "session": session,
        # the list tasks' sources (REPROC: only those adopting a listing, `sources[].listing`: its shards have no other
        # copy on a local data dir); a source's `secrets`: its list call's whole binding list, when it has its own;
        # a source's `fallback` (spec §7): its list task's machine by submit attempt (`machines`) and queue timeout
        "sources": [{"uri": s.uri, "machine": s.machine, "disk": _disk(s.disk),
                     **({"machines": list(s.types), "queue_timeout": f"{s.queue_timeout}m"} if s.fallback else {}),
                     **({"secrets": _bindings(profile.secrets_for("list", s.uri))} if s.secrets else {})}
                    for s in map(profile.source, listed)],
        # bounds the list tasks' submit retries: only a fallback source's queue timeouts are retried (`list_source`)
        "list_retries": max((len(s.fallback) for s in profile.sources), default=0),
        "list_default": {"machine": lst.type if lst else None, "disk": _disk(lst.disk) if lst else None},
        "secrets": _bindings(profile.secrets),
        "step_secrets": step_secrets(profile),
        "secrets_project": profile.executor.project or "",
        "env": step_env,
        "profile_json": profile_json,
        "labels": task_labels,
        "cache": not fresh,
        "queue_size": profile.queue_size,
        "work_bucket": urlparse(profile.work).netloc if profile.executor.kind == "google-batch" else "",
    }


def _q(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _disk_cfg(d: Disk) -> str:
    return f"{d.request}.GB" if d.type is None else f"[request: {d.request}.GB, type: {_q(d.type)}]"


def executor_config(profile: ScanProfile, image: str | None = None) -> str:
    """The deployment's executor block, generated (never hand-written per deployment): work dir, plugins, the
    executor's own scope, the image, and one `withLabel` per machine (`list` excepted: scan.nf sets each list task's
    machine from its source, falling back to `machines.list`, since a config selector would override it). A label with
    `fallback` types gets a block choosing its machine by submit attempt (`fallback_block`). `image` (the head's own,
    `$DT_SCAN_IMAGE`) beats `executor.image`: the profile is baked into the image, so it can only name a tag, and a
    rebuild mid-run would split the run across images."""
    ex = profile.executor
    lines = [f"// Generated by `dt-cloud scan head` from the {profile.deployment} profile: not edited by hand.",
             f"workDir = {_q(task_work_root(profile))}"]
    if ex.kind == "google-batch":
        lines += ["plugins {", f"    id {_q(NF_GOOGLE)}", f"    id {_q(NF_CLOUDCACHE)}", "}", "google {",
                  f"    project = {_q(ex.project)}", f"    location = {_q(ex.region)}"]
        if ex.service_account:
            lines.append(f"    batch.serviceAccountEmail = {_q(ex.service_account)}")
        if ex.boot_disk:
            lines.append(f"    batch.bootDiskSize = {ex.boot_disk}.GB")
        if ex.spot is not None:
            lines.append(f"    batch.spot = {'true' if ex.spot else 'false'}")
        lines.append("}")
    elif ex.kind == "awsbatch":
        lines += ["aws {", f"    region = {_q(ex.region)}"]
        if ex.cli_path:
            lines.append(f"    batch.cliPath = {_q(ex.cli_path)}")
        lines.append("}")
    lines.append("process {")
    lines.append(f"    executor = {_q(ex.kind)}")
    if ex.kind == "awsbatch":
        lines += [f"    queue = {_q(ex.queue)}", "    containerOptions = '--entrypoint=/usr/bin/env'"]
    if image or ex.image:
        lines.append(f"    container = {_q(image or ex.image)}")
    for label, m in sorted(profile.machines.items()):
        if label == "list":
            continue
        if m.fallback:
            lines += fallback_block(label, m)
            continue
        ds = []
        if m.type and ex.kind == "google-batch":
            ds.append(f"machineType = {_q(m.type)}")
        if m.disk and ex.kind == "google-batch":
            ds.append(f"disk = {_disk_cfg(m.disk)}")
        if m.cpus:
            ds.append(f"cpus = {m.cpus}")
        if m.memory:
            ds.append(f"memory = {_q(m.memory)}")
        if m.time:
            ds.append(f"time = {_q(m.time)}")
        if ds:
            lines.append(f"    withLabel: {label} {{ {'; '.join(ds)} }}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def task_work_root(profile: ScanProfile) -> str:
    """The exact task work root handed to Nextflow, also recorded by ownership receipts."""
    return profile.work.rstrip('/') + '/work'


#: What Nextflow raises when a task is still SUBMITTED (Batch: QUEUED / SCHEDULED) past its `maxSubmitAwait`. Nextflow
#: then deletes the task's Batch job itself (`handler.kill()`), and the fallback label's errorStrategy retries on this
#: cause alone.
SUBMIT_TIMEOUT = "nextflow.exception.ProcessSubmitTimeoutException"


def fallback_block(label: str, m: Machine) -> list[str]:
    """A label with fallback machine types (spec §7 "Capacity for big tasks"), as its `withLabel` block. Submit attempt
    k runs on `types[k-1]`. Every attempt but the last has `maxSubmitAwait = queue_timeout`: past it Nextflow deletes the
    still-queued Batch job and fails the task with a `ProcessSubmitTimeoutException`. The errorStrategy retries on that
    cause alone (the retry is the next submit attempt) and finishes on any real failure, as every other label does.
    `maxRetries` bounds the submit retries (a submit timeout never counts as a task failure, so `task.attempt` stays 1)."""
    types, n = m.types, len(m.types)
    ts = "[" + ", ".join(_q(t) for t in types) + "]"
    out = [f"    withLabel: {label} {{", f"        machineType = {{ {ts}[task.submitAttempt - 1] }}"]
    if m.disk:
        out.append(f"        disk = {_disk_cfg(m.disk)}")
    if m.cpus:
        out.append(f"        cpus = {m.cpus}")
    if m.memory:
        out.append(f"        memory = {_q(m.memory)}")
    if m.time:
        out.append(f"        time = {_q(m.time)}")
    last = "${task.submitAttempt + 1 < " + str(n) + " ? '' : ' (the last: no queue timeout)'}"
    out += [f"        maxSubmitAwait = {{ task.submitAttempt < {n} ? '{m.queue_timeout}m' : null }}",
            f"        maxRetries = {n - 1}",
            "        errorStrategy = {",
            f"            if( !(task.previousException?.cause instanceof {SUBMIT_TIMEOUT}) ) return 'finish'",
            f'            log.warn "scan head: ${{task.process}} queued {m.queue_timeout} min on ${{task.machineType}}; '
            f'retrying on ${{{ts}[task.submitAttempt]}}{last}"',
            "            return 'retry'",
            "        }",
            "    }"]
    return out


def first_failed(trace: Path) -> str | None:
    """`<task> (exit N)` for the trace's first FAILED task that wasn't retried: a fallback label's queue timeout is a
    FAILED row followed by its retry's row, of the same name (the trace's `error_action` reads `-` for it, not RETRY)."""
    try:
        with trace.open() as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
    except OSError:
        return None
    for i, row in enumerate(rows):
        if row.get("status") == "FAILED" and not any(r.get("name") == row.get("name") for r in rows[i + 1:]):
            return f"{row['name']} (exit {row.get('exit', '-')})"
    return None


#: Nextflow's line for a task it submits (`NXF_ANSI_LOG=false`): `[ab/123456] Submitted process > <task name>` (a retry:
#: `Re-submitted`); the name is the trace's `name`.
NF_SUBMITTED = re.compile(r"^\[[0-9a-f]{2}/[0-9a-f]{6}\] (?:Submitted|Re-submitted) process > (?P<name>.+)$")
#: A traced task's statuses that end one of its submissions (a `CACHED` row was never submitted).
NF_ENDED = frozenset({"COMPLETED", "FAILED", "ABORTED"})
#: The running tasks a head's liveness detail names; past it, `+N more`.
NF_RUNNING_SHOWN = 3


class NfTasks(Tail):
    """The head's view of its Nextflow run, from Nextflow's stdout (fed as a `Tail`: each `NF_SUBMITTED` line, when
    `clock` saw it) and its trace (`status`): what the head's liveness line says while Nextflow runs."""

    def __init__(self, clock=time.monotonic):
        super().__init__(clock)
        self.submitted: dict[str, list[float]] = {}

    def feed(self, text: str) -> None:
        super().feed(text)
        if m := NF_SUBMITTED.match(clean_line(text)):
            with self._lock:
                self.submitted.setdefault(m["name"], []).append(self.clock())

    def status(self, trace: str) -> str:
        """`<n> done[ (<c> cached)][, <f> failed][, running <task> <time>, …]`: the trace's rows (`trace`, its TSV; an
        incomplete last row counts for nothing) and each task submitted more times than the trace has ended it, oldest
        submission first, with the time since its latest."""
        rows = list(csv.DictReader(io.StringIO(trace), delimiter="\t")) if trace else []
        by = {s: sum(r.get("status") == s for r in rows) for s in ("COMPLETED", "CACHED", "FAILED", "ABORTED")}
        ended: dict[str, int] = {}
        for r in rows:
            if r.get("status") in NF_ENDED and r.get("name"):
                ended[r["name"]] = ended.get(r["name"], 0) + 1
        now = self.clock()
        with self._lock:
            running = sorted((ts[-1], n) for n, ts in self.submitted.items() if len(ts) > ended.get(n, 0))
        done = by["COMPLETED"] + by["CACHED"]
        parts = [f"{done} done" + (f" ({by['CACHED']} cached)" if by["CACHED"] else "")]
        if failed := by["FAILED"] + by["ABORTED"]:
            parts.append(f"{failed} failed")
        if running:
            shown = ", ".join(f"{n} {fmt_secs(now - t)}" for t, n in running[:NF_RUNNING_SHOWN])
            more = len(running) - NF_RUNNING_SHOWN
            parts.append(f"running {shown}" + (f", +{more} more" if more > 0 else ""))
        return ", ".join(parts)


def echo_task_logs(trace: Path, work: Path, out=err) -> int:
    """Each traced task's `.command.log` (its stdout + stderr) under a `--- task <name> [<hash>] <status>` line, in trace
    order: the local executor's tasks log only into their work dirs, which a container's exit takes with it (a cloud
    executor's tasks are jobs of their own, logged by their platform). Returns the tasks echoed."""
    try:
        with trace.open() as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
    except OSError:
        return 0
    n = 0
    for row in rows:
        h = row.get("hash") or ""
        dirs = sorted(work.glob(f"{h}*")) if "/" in h else []
        log = dirs[0] / ".command.log" if len(dirs) == 1 else None
        out(f"--- task {row.get('name')} [{h}] {row.get('status')}" + ("" if log and log.is_file() else " (no log)"))
        if log and log.is_file():
            out(log.read_text(errors="replace").rstrip("\n"))
            n += 1
    return n


def head_beat(env: Mapping[str, str], run_id: str, clock=time.monotonic):
    """The head's run heartbeat, for its poll loop: `JobRun.beat` (the run row's `updated_ts`) at most every
    `$DT_HEARTBEAT_S` (default 60 s; first at once), so `/scans` reads a run whose steps wait on Batch VMs as alive. None
    when `scan-run supervise -H` already beats it (`$DT_SUPERVISE`: its entrypoint) or the heartbeat is off; a gcs-style
    submit that runs `scan head` as the container's entrypoint has no supervisor, and this is its beat."""
    from .heartbeat import interval_of
    from .scan_runs import SUPERVISE_ENV, JobRun

    interval = interval_of(env)
    if interval is None or (env.get(SUPERVISE_ENV) or "").strip():
        return None
    run = JobRun(None, None, False, run_id, None, env=env)
    last: list[float] = []

    def beat() -> None:
        now = clock()
        if last and now - last[0] < interval:
            return
        last[:] = [now]
        run.beat()

    return beat


def _record(cmd, args: list[str]) -> None:
    """A `scan-run` subcommand, in-process (they never fail the job)."""
    try:
        cmd.main(args, prog_name=f"dt-cloud scan-run {cmd.name}", standalone_mode=False)
    except SystemExit:
        pass


#: Nextflow's run reports, written in the launch dir and uploaded beside the run (`runs_prefix`): the DAG (Mermaid; the
#: site draws it with each node's phase status), the timeline, the resource report and the trace.
REPORTS = ("dag.mmd", "timeline.html", "report.html", "trace.tsv")


def runs_prefix(scan: str) -> str:
    """Where a scan's run reports go, under the profile's work store: `<work>/runs/<scan>/` (e.g. a deployment's
    `scan-runs.json` output `nf-dag` = `<work>/runs/{scan}/dag.mmd`)."""
    return f"runs/{scan}"


def publish_reports(profile: ScanProfile, scan: str, launch: Path) -> list[str]:
    """Upload the run's reports that exist to `<work>/runs/<scan>/` (a re-run replaces them); returns the keys. Never fails
    the run: a missing report (Nextflow stopped early) or an upload error is logged."""
    from .blobstore import Store

    store, keys = Store(profile.work), []
    for name in REPORTS:
        src = launch / name
        if not src.is_file():
            continue
        key = f"{runs_prefix(scan)}/{name}"
        try:
            store.put(src, key)
            keys.append(key)
        except Exception as e:  # noqa: BLE001 — reports are a view of the run, not its output
            err(f"scan head: upload {key} failed: {type(e).__name__}: {e}")
    if keys:
        err(f"scan head: reports → {store.uri(runs_prefix(scan))}/ ({', '.join(k.rsplit('/', 1)[1] for k in keys)})")
    return keys


#: `scan-run start` facts a head that isn't a GCP Batch job takes from its env (its launcher's: m3's AWS Batch wrapper),
#: env name → the option.
RUN_FACTS = {"DT_RUN_KIND": "-k", "DT_RUN_MACHINE": "-m", "DT_RUN_REGION": "-R", "DT_RUN_AT": "-t", "DT_RUN_TASKS": "-T"}


def start_args(profile: ScanProfile, gen: str, scan: str, run_id: str, batch: bool = True, image: str | None = None,
               env: Mapping[str, str] | None = None) -> list[str]:
    """`scan-run start`'s arguments: the run id (`-r`, the one every task records into), the head's Batch job's facts
    (`-B`, when the head is one; else `RUN_FACTS` and `DT_RUN_SPOT=1` from `env`), the generation, a store other than
    the primary (`-K`: the run's `store`, which `finish` and every task's phase keep), and the image the tasks run (`-i`,
    when the head pins one)."""
    env = env or {}
    facts = [] if batch else [a for k, o in RUN_FACTS.items() if (env.get(k) or "").strip() for a in (o, env[k].strip())]
    if not batch and env.get("DT_RUN_SPOT", "").strip() == "1":
        facts.append("--spot")
    return ["-r", run_id, *(["-B"] if batch else []), *facts, "-g", gen, *([] if profile.primary else ["-K", profile.store]),
            *(["-i", image] if image else []), scan]


def pre_phases(env: Mapping[str, str]) -> list[tuple[str, list[str]]]:
    """`DT_PRE_PHASES` (JSON `[{"name", "began", "ended", "outputs": ["k=v", …]}]`): phases that ran before the head, e.g.
    a laptop's capture and the job's queue wait, as `(phase-begin args, phase-end args)` pairs for the record."""
    raw = (env.get("DT_PRE_PHASES") or "").strip()
    out = []
    for ph in json.loads(raw) if raw else []:
        out.append((["-b", str(int(ph["began"])), ph["name"]],
                    ["-t", str(int(ph["ended"])), *[a for o in ph.get("outputs", []) for a in ("-o", o)], ph["name"]]))
    return out


def local_run_id(profile: ScanProfile, scan: str) -> str:
    """A head that isn't a Batch job (a laptop, a scratch run)'s run id: without one, `start` recorded nothing and
    every task, falling back to its own `BATCH_JOB_UID`, became a run of its own (gcs prod, 2026-10-17T0005: 6)."""
    return "-".join([profile.deployment, *([] if profile.primary else [profile.store]), "nf", scan]).lower()


def finish_args(rc: int, error: str | None, cost: list[str], dag: Path, run_id: str = "", engine: str = "nextflow") -> list[str]:
    """`scan-run finish`'s arguments: the exit status, the error, the cost labels, and the run's DAG (`-D`, stored with
    the run for the site's pipeline view, in `engine`'s format: `-E` unless Nextflow's) when the backend wrote one."""
    return [*(["-r", run_id] if run_id else []), "-x", str(rc), *(["-e", error] if error else []), *cost,
            *(["-D", str(dag), *([] if engine == "nextflow" else ["-E", engine])] if dag.is_file() else [])]


def _groovy(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


#: The head's env var holding the run's image digest (`pin_image`), which `run_config` hands every task.
PIN_ENV = "DT_IMAGE_PIN"


def pin_image(profile: ScanProfile, image: str | None, dry_run: bool) -> str | None:
    """The digest every job the run's tasks submit runs (`image_pin`): the head's own image when it's a digest
    (`$DT_SCAN_IMAGE`), else a google-batch profile's `executor.image` tag resolved now; None for a dry run, a local
    run, or an image that isn't an Artifact Registry tag. The tasks' own container stays as `executor_config` names it,
    so a resume after a promote keeps its cache (a digest there is in each task's hash)."""
    from .image_pin import DIGEST, is_tag, resolve

    if image and DIGEST.search(image):
        return image
    ex = profile.executor
    if dry_run or ex.kind != "google-batch" or not is_tag(ex.image):
        return None
    return resolve(ex.image)


def run_config(run_id: str, profile_json: str, image: str | None = None) -> str:
    """The run's own task env, as a Nextflow `env` scope: this head's run id (as `DT_RUN_ID`, what the steps record
    under, and `SCAN_RUN_ID`, what the Batch jobs a step submits are labelled `scan_run` from: `cost_labels`) and the
    profile JSON. Nextflow's task hash leaves the `env` scope out (unlike anything in a task's script), so a resume from
    a new head job (a new `BATCH_JOB_UID`), or after a profile edit, still finds the finished tasks in the cache."""
    if not re.fullmatch(r"[A-Za-z0-9._-]*", run_id):  # empty: a local run that isn't recorded
        raise ValueError(f"run id {run_id!r}: Nextflow exports it into each task's script unescaped")
    if image is not None and not re.fullmatch(r"[A-Za-z0-9._/@:-]+", image):
        raise ValueError(f"image {image!r}: Nextflow exports it into each task's script unescaped")
    from .image_pin import IMAGE_ENVS

    pinned = "".join(f"    {k} = {_groovy(image)}\n" for k in IMAGE_ENVS) if image else ""
    return (f"env {{\n    DT_RUN_ID = {_groovy(run_id)}\n    SCAN_RUN_ID = {_groovy(run_id)}\n{pinned}"
            f"    DT_SCAN_PROFILE_JSON = {_groovy(profile_env_value(profile_json))}\n}}\n")


def nextflow_argv(nf_dir: Path, launch: Path, session: str) -> list[str]:
    """The Nextflow command the head runs in `launch` (after writing its `scan.nf`, its params, executor and run configs
    and history line)."""
    return ["nextflow", "run", str(launch / "scan.nf"), "-c", str(nf_dir / "base.config"), "-c", str(launch / "executor.config"),
            "-c", str(launch / "run.config"),
            "-params-file", str(launch / "params.json"), "-resume", session, "-with-trace", str(launch / "trace.tsv"),
            "-with-dag", str(launch / "dag.mmd"), "-with-timeline", str(launch / "timeline.html"), "-with-report", str(launch / "report.html")]


#: The DAG the head stores before the run (`preview_argv`), so the site draws the pipeline while it runs; finish's real
#: `dag.mmd` replaces it (`scan_run_dags` upserts).
PREVIEW_DAG = "dag-preview.mmd"


def preview_argv(nf_dir: Path, launch: Path) -> list[str]:
    """The run's own Nextflow command with `-preview` (the workflow built, no process run: seconds) writing just its DAG,
    `PREVIEW_DAG`: the same graph the real run's `-with-dag` reduces to."""
    return ["nextflow", "run", str(launch / "scan.nf"), "-c", str(nf_dir / "base.config"), "-c", str(launch / "executor.config"),
            "-c", str(launch / "run.config"), "-params-file", str(launch / "params.json"), "-preview", "-with-dag", str(launch / PREVIEW_DAG)]


def store_preview_dag(nf_dir: Path, launch: Path, nf_env: dict[str, str], run_id: str) -> None:
    """Store the run's DAG up front from a `-preview` (`scan-run dag`). Never fails the run: a preview that errors, times
    out or writes no DAG only leaves the pipeline view empty until finish stores the real one."""
    from .scan_runs import dag_cmd

    try:
        r = subprocess.run(preview_argv(nf_dir, launch), cwd=launch, env=nf_env, capture_output=True, text=True, timeout=180)
        dag = launch / PREVIEW_DAG
        if r.returncode or not dag.is_file():
            err(f"scan head: DAG preview: exit {r.returncode}{'' if dag.is_file() else ', no DAG'}; the pipeline view waits for finish")
            return
        _record(dag_cmd, [run_id, str(dag)])
    except Exception as e:  # noqa: BLE001 — a view, never the run
        err(f"scan head: DAG preview failed: {type(e).__name__}: {e}")


class NextflowBackend(Backend):
    """The scan's DAG as a Nextflow run: `scan.nf` rendered into the launch dir (`scan_dag_nf`), the session's
    `.nextflow/history` line (Nextflow finds a session only there; its cache is in the work store,
    `NXF_CLOUDCACHE_PATH`), the params and the executor config from the profile, then `nextflow run -resume <session>`
    in its own process group. A cancel waits `NF_SWEEP_DELAY` (30 s: a submit in flight lands after Nextflow exits),
    then cancels whatever Batch job still carries the session's `nf-session` label. Its reports go beside the run, with
    an ownership receipt over its trace."""
    engine = "nextflow"

    def __init__(self, nf_dir: Path):
        self.nf_dir = nf_dir
        self.proc: subprocess.Popen | None = None
        self.seen_cached: tuple[str, ...] = ()
        self.tasks = NfTasks()

    def _cloud(self, run: Run) -> bool:
        return run.profile.executor.kind != "local"

    def prepare(self, run: Run) -> list[str]:
        launch, profile = run.launch, run.profile
        (launch / ".nextflow").mkdir(parents=True, exist_ok=True)
        (launch / ".nextflow" / "history").write_text(history_line(run.now, run.scan, run.session))
        (launch / "scan.nf").write_text(render(run.dag, (self.nf_dir / PRELUDE).read_text(), profile.deployment))
        (launch / "params.json").write_text(json.dumps(
            params(profile, run.profile_json, run.scan, run.gen, run.job_start, run.run_id, run.session, run.env,
                   reproc=run.reproc, fresh=run.fresh, dag=run.dag), indent=1) + "\n")
        (launch / "executor.config").write_text(executor_config(profile, run.image))
        (launch / "run.config").write_text(run_config(run.run_id, run.profile_json, run.env.get(PIN_ENV)))
        for name in (*REPORTS, PREVIEW_DAG):
            (launch / name).unlink(missing_ok=True)
        self.argv = nextflow_argv(self.nf_dir, launch, run.session)
        self.nf_env = {**run.env, "NXF_ANSI_LOG": "false"}
        if self._cloud(run):
            self.nf_env["NXF_CLOUDCACHE_PATH"] = profile.work.rstrip("/") + "/cache"
        return self.argv

    def store_preview(self, run: Run) -> None:
        store_preview_dag(self.nf_dir, run.launch, self.nf_env, run.run_id)

    def start(self, run: Run) -> None:
        from . import scan_ownership

        self.ownership = scan_ownership.context(
            run.profile, run.profile_json, run.scan, run.gen, run.session,
            None if run.run_id_source == "unrecorded" else run.run_id, run.run_id_source, run.run_id, str(uuid.uuid4()),
            run.now.isoformat(), run.env, task_work_root(run.profile))
        # its stdout through `tasks` on the way to ours: what it submits, for the liveness line (`status`)
        self.proc = subprocess.Popen(self.argv, cwd=run.launch, env=self.nf_env, start_new_session=True, stdout=subprocess.PIPE)
        self.pump = self.tasks.pump(self.proc.stdout, sys.stdout)

    def wait(self, timeout: float) -> int | None:
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        self.pump.join(PUMP_JOIN_S)
        return rc

    def status(self, run: Run) -> str | None:
        trace = run.launch / "trace.tsv"
        try:
            text = trace.read_text(errors="replace") if trace.exists() else ""
        except OSError:
            text = ""
        return self.tasks.status(text)

    def progress(self, run: Run) -> Path | None:
        from .run_dag import cached_tasks

        trace = run.launch / "trace.tsv"
        if not trace.exists():
            return None
        cached = cached_tasks(trace.read_text())
        dag = run.launch / "dag.mmd"
        if not dag.exists():
            dag = run.launch / PREVIEW_DAG
        if cached == self.seen_cached or not dag.exists():
            return None
        self.seen_cached = cached
        return dag

    def interrupt(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def cancel(self, run: Run) -> None:
        if run.profile.executor.kind != "google-batch":
            return
        time.sleep(float(run.env.get("NF_SWEEP_DELAY", "30")))
        try:
            from .batch import cancel_labelled

            cancel_labelled({"nf-session": run.session}, region=run.profile.executor.region, log=err)
        except Exception as e:  # noqa: BLE001 — the record must still finish
            err(f"scan head: batch-cancel failed: {type(e).__name__}: {e}")

    def error(self, run: Run, rc: int) -> str:
        f = first_failed(run.launch / "trace.tsv")
        return f"nextflow exit {rc}" + (f": {f} failed" if f else "") + f" (session {run.session})"

    def finish(self, run: Run, rc: int, signal: int | None) -> None:
        from . import scan_ownership
        from .blobstore import Store

        trace = run.launch / "trace.tsv"
        if not self._cloud(run):
            echo_task_logs(trace, Path(run.profile.work.rstrip("/")) / "work")
        reports = publish_reports(run.profile, run.scan, run.launch)
        try:
            with TemporaryDirectory(dir=run.launch, prefix='.ownership-') as snapshot_dir:
                snapshot = scan_ownership.snapshot_trace(trace, Path(snapshot_dir) / 'trace.tsv')
                doc = scan_ownership.receipt(self.ownership, snapshot, dt.datetime.now(dt.timezone.utc).isoformat(), rc,
                                             signal, reports)
                store = Store(run.profile.work)
                key = scan_ownership.publish(store, doc, snapshot)
            err(f"scan head: ownership receipt → {store.uri(key)} (trace {doc['trace']['state']}; observed tasks only)")
        except Exception as e:  # noqa: BLE001 — failed evidence publication cannot prevent the original run's finish
            err(f"scan head: ownership receipt unavailable: {type(e).__name__}: {e}")

    def cost(self, run: Run) -> list[str]:
        """The labels `finish -l` prices the run's other Batch jobs by: its `scan_run` (its tasks and every job they
        submitted: static names' stages, the interval store's), else, unrecorded, the session's tasks (`nf-session`,
        which a resume shares with the runs before it)."""
        if run.profile.executor.kind != "google-batch":
            return []
        return ["-l", f"scan_run={run.run_id.lower()}"] if run.run_id else ["-l", f"nf-session={run.session}"]

    def dag_file(self, run: Run) -> Path:
        return run.launch / "dag.mmd"


def backend_for(profile: ScanProfile, nf_dir: Path) -> Backend:
    """The profile's engine: every executor kind (`google-batch`, `awsbatch`, `local`) runs under Nextflow."""
    return NextflowBackend(nf_dir)


def head(profile_path: Path, nf_dir: Path, launch: Path | None, *, reproc: bool = False, fresh: bool = False,
         dry_run: bool = False, env: dict[str, str] | None = None) -> int:
    from . import scan_profile
    from .scan_runs import PROFILE_ENV, TRIGGER_ENV, dag_cmd, finish_cmd, phase_begin_cmd, phase_end_cmd, start_cmd, trigger_of

    env = os.environ if env is None else env
    profile = scan_profile.load(profile_path)
    profile_json = scan_profile.to_json(profile_path)
    now = dt.datetime.now(dt.timezone.utc)
    scan, gen, job_start = identity(env, now)
    trigger = trigger_of(env)
    run_id_source = "DT_RUN_ID" if env.get("DT_RUN_ID") else "BATCH_JOB_UID" if env.get("BATCH_JOB_UID") else "unrecorded"
    run_id = env.get("DT_RUN_ID") or env.get("BATCH_JOB_UID") or ""
    env.update(SNAP_ID=scan, SCAN_ID=scan, GEN=gen, DT_JOB_START=job_start, DT_RUN_ID=run_id)
    for k in (PROFILE_ENV, "D1_DB_ID", "D1_DB_NAME", "SLACK_ALERT_CHANNEL", "DT_ALERTS", LABELS_ENV):
        if k in profile.env:
            env.setdefault(k, profile.env[k])
    # no unlabelled scan: refuse before anything starts or records (specs/done/cost-labels-required.md)
    deployment_labels(dict(env))
    if reproc:
        env.update(REPROC="1", DT_RECORD="off")
    batch = bool(env.get("BATCH_JOB_UID"))
    if not run_id and env.get(PROFILE_ENV) and env.get("DT_RECORD") != "off":
        run_id = env["DT_RUN_ID"] = local_run_id(profile, scan)
        err(f"scan head: not a Batch job; recording as run {run_id}")
    session = session_id(profile.deployment, scan, profile.store)
    if launch is None:
        launch = Path(profile.work) if profile.executor.kind == "local" else Path(env.get("TMPDIR") or "/tmp") / f"nf-{scan}"
    launch = launch.absolute()
    image = env.get("DT_SCAN_IMAGE") or None
    # one digest for every job the run's tasks submit (`image_pin`): a promote mid-run can't split the run
    if pin := pin_image(profile, image, dry_run):
        env[PIN_ENV] = pin
        err(f"scan head: the run's jobs run {pin}")
    backend = backend_for(profile, nf_dir)
    run = Run(profile, profile_json, scan_dag.build(profile, reproc=reproc), scan, gen, job_start, run_id, session, launch,
              dict(env), now, reproc=reproc, fresh=fresh, image=image, run_id_source=run_id_source)
    argv = backend.prepare(run)
    err(f"scan head: {profile.deployment}{'' if profile.primary else f' [{profile.store}]'} scan {scan} gen {gen} session {session} "
        f"({trigger or f'${TRIGGER_ENV} unset'}) in {launch}")
    err("+ " + " ".join(argv))
    if dry_run:
        return 0
    recording = bool(env.get(PROFILE_ENV))
    if recording:
        _record(start_cmd, start_args(profile, gen, scan, run_id, batch, image, env))
        for began, ended in pre_phases(env):
            _record(phase_begin_cmd, began)
            _record(phase_end_cmd, ended)
        if run_id:
            backend.store_preview(run)

    stopped: list[int] = []
    set_stage(f"{backend.engine}: run")
    backend.start(run)

    def stop(sig: int, frame) -> None:  # noqa: ANN001
        if not stopped:
            stopped.append(sig)
            backend.interrupt()

    prev = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        beat = head_beat(env, run_id) if recording and run_id else None
        while True:
            if beat is not None:
                beat()
            if recording and (dag := backend.progress(run)) is not None:
                _record(dag_cmd, [run_id, str(dag)])
            set_detail(backend.status(run))
            if (rc := backend.wait(10)) is not None:
                break
    finally:
        for s, h in prev.items():
            signal.signal(s, h)
    set_stage(f"{backend.engine}: finish")
    # the run's last tasks can end, and the engine exit, within one `wait`: the finish stage carries the final count
    set_detail(backend.status(run))
    if stopped:
        rc = 128 + stopped[0]
        backend.cancel(run)
        error = f"head terminated (signal; session {session})"
    else:
        error = backend.error(run, rc) if rc else None
    backend.finish(run, rc, stopped[0] if stopped else None)
    if recording:
        _record(finish_cmd, finish_args(rc, error, backend.cost(run), backend.dag_file(run), run_id, backend.engine))
    if rc == 0:
        print(f"SCAN-JOB-DONE {scan} -> {profile.data.rstrip('/')}/{profile.layout.snapshots_dir()}/{scan}")
    return rc
