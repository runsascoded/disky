"""`dt-cloud scan head` (specs/pipelines-everywhere.md §2.3): one scan as a Nextflow run of `job/nf/scan.nf`.

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
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

from .cost_labels import check_label
from .scan_profile import PRIMARY_STORE, Disk, Machine, ScanProfile
from .scan_steps import DATA_MOUNT_ENV, PROFILE_JSON_ENV, STEPS


def err(*a: object) -> None:
    print(*a, file=sys.stderr)

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
    """The tasks' Batch labels: the profile's, the stack's (`$DISKY_LABELS`, `k=v,…`), `component: scan`, `scan_id`
    (lowercased, as `cost_labels` writes it); scan.nf adds `nf-session` and each task's `step`, so a billing export
    groups a scan's spend by scan and step."""
    out = dict(profile.labels)
    for i in (env.get("DISKY_LABELS") or "").split(","):
        if i.strip():
            k, _, v = i.strip().partition("=")
            out[k] = v
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
           env: Mapping[str, str], *, reproc: bool = False, fresh: bool = False) -> dict:
    """`scan.nf`'s params file."""
    enabled = profile.enabled()
    step_env = dict(profile.env)
    step_env.update({k: env[k] for k in profile.passthrough if k in env})
    if reproc:
        step_env["REPROC"] = "1"
    if profile.executor.kind == "google-batch":
        step_env[DATA_MOUNT_ENV] = BATCH_MOUNT
    lst = profile.machines.get("list")
    return {
        "scan": scan, "gen": gen, "job_start": job_start, "run_id": run_id, "session": session,
        # a source's `secrets`: its list call's whole binding list (else the list step's), when it has its own
        # REPROC runs from `index`, but an adopted listing (`sources[].listing`) is still fetched: its shards have no
        # other copy on a local data dir
        "sources": [{"uri": s.uri, "machine": s.machine, "disk": _disk(s.disk),
                     **({"secrets": _bindings(profile.secrets_for("list", s.uri))} if s.secrets else {})}
                    for s in profile.sources if not reproc or s.listing],
        "list_default": {"machine": lst.type if lst else None, "disk": _disk(lst.disk) if lst else None},
        "steps": list(enabled),
        "pre": [n for n in enabled if STEPS[n].stage == "pre" and n != "bucket-check"],
        "pre_label": "pre" if "pre" in profile.machines else "small",
        "interval_label": "interval" if "interval" in profile.machines else "big",
        "post": [n for n in enabled if STEPS[n].stage == "post" and n != "over-time"],
        "over_time": "over-time" in enabled,
        "over_time_label": "over_time" if "over_time" in profile.machines else "post",
        "fold": list(profile.fold),
        "skip_list": reproc and not any(s.listing for s in profile.sources),
        "secrets": _bindings(profile.secrets),
        "step_secrets": step_secrets(profile),
        "secrets_project": profile.executor.project or "",
        "env": step_env,
        "profile_json": profile_json,
        "labels": labels(profile, env, scan),
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
             f"workDir = {_q(profile.work.rstrip('/') + '/work')}"]
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


def finish_args(rc: int, error: str | None, cost: list[str], dag: Path, run_id: str = "") -> list[str]:
    """`scan-run finish`'s arguments: the exit status, the error, the cost labels, and the run's DAG (`-D`, stored with
    the run for the site's pipeline view) when Nextflow wrote one."""
    return [*(["-r", run_id] if run_id else []), "-x", str(rc), *(["-e", error] if error else []), *cost,
            *(["-D", str(dag)] if dag.is_file() else [])]


def _groovy(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def run_config(run_id: str, profile_json: str) -> str:
    """The run's own task env, as a Nextflow `env` scope: this head's run id and the profile JSON. Nextflow's task hash
    leaves the `env` scope out (unlike anything in a task's script), so a resume from a new head job (a new
    `BATCH_JOB_UID`), or after a profile edit, still finds the finished tasks in the cache."""
    return f"env {{\n    DT_RUN_ID = {_groovy(run_id)}\n    DT_SCAN_PROFILE_JSON = {_groovy(profile_json)}\n}}\n"


def nextflow_argv(nf_dir: Path, launch: Path, session: str) -> list[str]:
    """The Nextflow command the head runs in `launch` (after writing its params, executor and run configs and history
    line)."""
    return ["nextflow", "run", str(nf_dir / "scan.nf"), "-c", str(nf_dir / "base.config"), "-c", str(launch / "executor.config"),
            "-c", str(launch / "run.config"),
            "-params-file", str(launch / "params.json"), "-resume", session, "-with-trace", str(launch / "trace.tsv"),
            "-with-dag", str(launch / "dag.mmd"), "-with-timeline", str(launch / "timeline.html"), "-with-report", str(launch / "report.html")]


def head(profile_path: Path, nf_dir: Path, launch: Path | None, *, reproc: bool = False, fresh: bool = False,
         dry_run: bool = False, env: dict[str, str] | None = None) -> int:
    from . import scan_profile
    from .scan_runs import PROFILE_ENV, TRIGGER_ENV, finish_cmd, phase_begin_cmd, phase_end_cmd, start_cmd, trigger_of

    env = os.environ if env is None else env
    profile = scan_profile.load(profile_path)
    profile_json = scan_profile.to_json(profile_path)
    now = dt.datetime.now(dt.timezone.utc)
    scan, gen, job_start = identity(env, now)
    trigger = trigger_of(env)
    run_id = env.get("DT_RUN_ID") or env.get("BATCH_JOB_UID") or ""
    env.update(SNAP_ID=scan, SCAN_ID=scan, GEN=gen, DT_JOB_START=job_start, DT_RUN_ID=run_id)
    for k in (PROFILE_ENV, "D1_DB_ID", "D1_DB_NAME", "SLACK_ALERT_CHANNEL", "DT_ALERTS"):
        if k in profile.env:
            env.setdefault(k, profile.env[k])
    if reproc:
        env.update(REPROC="1", DT_RECORD="off")
    batch = bool(env.get("BATCH_JOB_UID"))
    if not run_id and env.get(PROFILE_ENV) and env.get("DT_RECORD") != "off":
        run_id = env["DT_RUN_ID"] = local_run_id(profile, scan)
        err(f"scan head: not a Batch job; recording as run {run_id}")
    session = session_id(profile.deployment, scan, profile.store)
    cloud = profile.executor.kind != "local"
    if launch is None:
        launch = Path(profile.work) if not cloud else Path(env.get("TMPDIR") or "/tmp") / f"nf-{scan}"
    launch = launch.absolute()
    (launch / ".nextflow").mkdir(parents=True, exist_ok=True)
    (launch / ".nextflow" / "history").write_text(history_line(now, scan, session))
    (launch / "params.json").write_text(json.dumps(
        params(profile, profile_json, scan, gen, job_start, run_id, session, env, reproc=reproc, fresh=fresh), indent=1) + "\n")
    image = env.get("DT_SCAN_IMAGE") or None
    (launch / "executor.config").write_text(executor_config(profile, image))
    (launch / "run.config").write_text(run_config(run_id, profile_json))
    trace = launch / "trace.tsv"
    for name in REPORTS:
        (launch / name).unlink(missing_ok=True)
    argv = nextflow_argv(nf_dir, launch, session)
    nf_env = {**env, "NXF_ANSI_LOG": "false"}
    if cloud:
        nf_env["NXF_CLOUDCACHE_PATH"] = profile.work.rstrip("/") + "/cache"
    err(f"scan head: {profile.deployment}{'' if profile.primary else f' [{profile.store}]'} scan {scan} gen {gen} session {session} "
        f"({trigger or f'${TRIGGER_ENV} unset'}) in {launch}")
    err("+ " + " ".join(argv))
    if dry_run:
        return 0
    recording = bool(env.get(PROFILE_ENV))
    cost = ["-l", f"nf-session={session}"] if profile.executor.kind == "google-batch" else []
    if recording:
        _record(start_cmd, start_args(profile, gen, scan, run_id, batch, image, env))
        for began, ended in pre_phases(env):
            _record(phase_begin_cmd, began)
            _record(phase_end_cmd, ended)

    proc = subprocess.Popen(argv, cwd=launch, env=nf_env, start_new_session=True)
    stopped: list[int] = []

    def stop(sig: int, frame) -> None:  # noqa: ANN001
        if not stopped:
            stopped.append(128 + sig)
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    prev = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        rc = proc.wait()
    finally:
        for s, h in prev.items():
            signal.signal(s, h)
    if stopped:
        rc = stopped[0]
        if profile.executor.kind == "google-batch":
            time.sleep(float(env.get("NF_SWEEP_DELAY", "30")))
            try:
                from .batch import cancel_labelled

                cancel_labelled({"nf-session": session}, region=profile.executor.region, log=err)
            except Exception as e:  # noqa: BLE001 — the record must still finish
                err(f"scan head: batch-cancel failed: {type(e).__name__}: {e}")
        error = f"head terminated (signal; session {session})"
    else:
        f = first_failed(trace) if rc else None
        error = f"nextflow exit {rc}" + (f": {f} failed" if f else "") + f" (session {session})" if rc else None
    publish_reports(profile, scan, launch)
    if recording:
        _record(finish_cmd, finish_args(rc, error, cost, launch / "dag.mmd", run_id))
    if rc == 0:
        print(f"SCAN-JOB-DONE {scan} -> {profile.data.rstrip('/')}/{profile.layout.snapshots_dir()}/{scan}")
    return rc
