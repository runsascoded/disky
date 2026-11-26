"""The VM → job leg of attributing bucket requests (specs/access-job-attribution.md phase 2): a deployment's own image
writes each UTC day's hourly scheduler tables under `<root>/<day>/{workers,tasks}.parquet` (`workers`: `hour,
worker_id, …, address`, a worker's internal address that hour; `tasks`: `hour, task_id, owner, job, attempt_id,
worker_id, n_samples`: who and which top-level job, and the task's share of the worker that hour), what
`access_attribute` joins. The scheduler and its client are the deployment's (gcs: Marin's Iris, read from Finelog by
`job/finelog/`), so they live in that image, not here: this submits it as its own Batch job, for the days not yet
written. The image's contract: its entrypoint takes `-o ROOT DAY…`, writes `workers` then `tasks` for each day, and
leaves a day its source doesn't cover yet for a later run."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import sys
from collections.abc import Mapping, Sequence
from functools import partial

from .blobstore import Store, store_url
from .vm_pack import MIB_PER_VCPU, batch_problems as _batch_problems, gcs_bucket, secret_ref

err = partial(print, file=sys.stderr)

COMPONENT = "access-jobs"
#: In write order: `tasks` present means the day is written.
FILES = ("workers.parquet", "tasks.parquet")
MACHINE = "e2-standard-4"
MAX_RUN_S = 2 * 3600
SPOT_RETRIES = 2
SUBMIT_RETRY_S = 300
#: What the step's `options.access-jobs.batch` may name, and each one's type.
BATCH_KEYS: dict[str, type] = {"image": str, "service_account": str, "region": str, "project": str, "machine": str,
                               "spot": bool, "env": dict, "secrets": dict, "cross_region": bool}


def written(store: Store, day: str) -> bool:
    return all(store.exists(f"{day}/{name}") for name in FILES)


def plan(root: str, days: Sequence[str]) -> tuple[list[str], list[str]]:
    """`(missing, overdue)` among `days` (ascending): the days not yet written, and of those every one but the newest
    (a run before this one asked for it, so its job failed or left it uncovered)."""
    store = Store(store_url(root))
    missing = [d for d in sorted(days) if not written(store, d)]
    newest = max(days) if days else None
    return missing, [d for d in missing if d != newest]


def job_id(deployment: str, days: Sequence[str], at: dt.datetime) -> str:
    """`access-jobs-<deployment>-<newest day>-<8 hex of the days>-<MMDDHH of at>`: one job per set of days per UTC
    hour, so a resubmit within the hour (a retried step) meets the job already there (409: counted as submitted), and
    a later run's, after that job failed, is a new job."""
    from .batch import check_job_id

    h = hashlib.sha256(",".join(sorted(days)).encode()).hexdigest()[:8]
    return check_job_id(f"{COMPONENT}-{deployment}-{max(days)}-{h}-{at.astimezone(dt.timezone.utc):%m%d%H}".lower())


def job_spec(root: str, days: Sequence[str], *, image: str, service_account: str, region: str, project: str,
             machine: str = MACHINE, spot: bool = True, env: Mapping[str, str] | None = None,
             secrets: Mapping[str, str] | None = None, environ: Mapping[str, str] | None = None) -> dict:
    """The Batch job running `image` (its own entrypoint) with `-o root DAY…`, as `service_account`, in `region`, on
    `machine` (spot unless `spot` is false). `env` and `secrets` (`{VAR: secret name}` in `project`) reach the task.
    Cost-labelled `component=access-jobs`."""
    from .cost_labels import label_batch_spec

    family = machine.split("-")[1] if machine.count("-") >= 2 else ""
    if family not in MIB_PER_VCPU:
        raise ValueError(f"machine {machine}: expected one of the {', '.join(MIB_PER_VCPU)} families")
    vcpus = int(machine.split("-")[-1])
    environment: dict = {"variables": dict(env or {})}
    if secrets:
        environment["secretVariables"] = {k: secret_ref(v, project) for k, v in sorted(secrets.items())}
    task = {
        "runnables": [{"container": {"imageUri": image, "commands": ["-o", root, *sorted(days)]}}],
        "environment": environment,
        "computeResource": {"cpuMilli": vcpus * 1000, "memoryMib": vcpus * MIB_PER_VCPU[family]},
        "maxRetryCount": SPOT_RETRIES if spot else 0,
        "maxRunDuration": f"{MAX_RUN_S}s",
    }
    return label_batch_spec({
        "taskGroups": [{"taskCount": 1, "taskSpec": task}],
        "allocationPolicy": {
            "instances": [{"policy": {"machineType": machine, "provisioningModel": "SPOT" if spot else "STANDARD"}}],
            "serviceAccount": {"email": service_account},
            "location": {"allowedLocations": [f"regions/{region}"]},
        },
        "labels": {"purpose": COMPONENT},
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }, COMPONENT, dict(os.environ if environ is None else environ))


def submit(root: str, days: Sequence[str], *, image: str, service_account: str, region: str, project: str,
           machine: str = MACHINE, spot: bool = True, env: Mapping[str, str] | None = None,
           secrets: Mapping[str, str] | None = None, cross_region: bool = False, dry_run: bool = False,
           environ: Mapping[str, str] | None = None, now: dt.datetime | None = None) -> dict:
    """Submit the job for `days`' missing ones (`plan`); none missing: no job. Through the deployment's job backend
    (`job_backend.resolve`; on Batch, `batch.submit_job`: cost labels; a `gs://` root's bucket region checked against
    the job's unless `cross_region`; a job of that id already there counts as submitted). Not waited on. Returns `{missing, overdue, job?, submitted?, spec? (dry_run)}`."""
    missing, overdue = plan(root, days)
    out: dict = {"missing": missing, "overdue": overdue}
    if not missing:
        err(f"{COMPONENT}: {', '.join(sorted(days))} already written: no job")
        return out
    from .cost_labels import deployment_labels

    environ = dict(os.environ if environ is None else environ)
    jid = job_id(deployment_labels(environ)["deployment"], missing, now or dt.datetime.now(dt.timezone.utc))
    spec = job_spec(root, missing, image=image, service_account=service_account, region=region, project=project,
                    machine=machine, spot=spot, env=env, secrets=secrets, environ=environ)
    out["job"] = jid
    if dry_run:
        return {**out, "spec": spec}
    from .job_backend import resolve

    bucket = gcs_bucket(root)
    resolve(region, project).submit(spec, jid, buckets=[bucket] if bucket else [], cross_region=cross_region, exists_ok=True,
                                    retry_for=SUBMIT_RETRY_S)
    err(f"{COMPONENT}: job {jid} [{region}] submitted for {', '.join(missing)}")
    return {**out, "submitted": True}


def submit_args(batch: Mapping[str, object]) -> list[str]:
    """The step's `options.access-jobs.batch` (`BATCH_KEYS`) as `dt-cloud access jobs`'s flags."""
    if bad := sorted(set(batch) - set(BATCH_KEYS)):
        raise ValueError(f"options.access-jobs.batch: unknown {', '.join(bad)} (known: {', '.join(BATCH_KEYS)})")
    flags = {"image": "-i", "service_account": "-a", "region": "-L", "project": "-P", "machine": "-m"}
    args: list[str] = []
    for k, f in flags.items():
        if batch.get(k) is not None:
            args += [f, str(batch[k])]
    if batch.get("spot") is False:
        args.append("-S")
    if batch.get("cross_region"):
        args.append("-X")
    for k, v in sorted((batch.get("env") or {}).items()):
        args += ["-e", f"{k}={v}"]
    for k, v in sorted((batch.get("secrets") or {}).items()):
        args += ["-s", f"{k}={v}"]
    return args


def batch_problems(batch: object) -> list[str]:
    """What's wrong with `options.access-jobs.batch`: unknown keys, wrong types, no image."""
    out = _batch_problems(batch, BATCH_KEYS, "options.access-jobs.batch")
    if isinstance(batch, Mapping) and not batch.get("image"):
        out.append("options.access-jobs.batch.image: required (the deployment's job-table image)")
    return out
