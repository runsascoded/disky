"""Where a job runs (specs/vm-pilot.md "JobBackend (step 1)"): one interface over GCP Batch and this host.

A spec is a Batch job's JSON whatever the backend: every submitter builds one already, and `GcpBatch` posts it as it
is. `Local` runs the same spec's container runnables on this host under a systemd slice (`scan.slice`), one job at a
time per tenant (the spec's `deployment` label), with its state on disk so `status` / `logs` / `cancel` work from any
process and after a restart. The deployment names its backend (`$DISKY_JOB_BACKEND`, `resolve`); unset, it is Batch.

A local job runs as its spec's service account, as on Batch, never as the host: its supervisor serves the job's
containers a GCE metadata server of their own (`job_metadata.MetadataServer`, on the gateway of the job's own Docker
network), whose tokens it mints for that account (IAM Credentials `generateAccessToken`, under the host's credentials,
which hold `iam.serviceAccountTokenCreator` on it) and refreshes while the job runs; the spec's `secretVariables` are
read with such a token. Secrets reach the container only through the engine's environment (`-e NAME`, no value in
argv); nothing of a token or a secret is written. A spec's gcsfuse volumes are mounted on the host as the same account
(gcsfuse pointed at the job's metadata server), as Batch mounts them on its VM.

States are Batch's (`QUEUED`, `SCHEDULED`, `RUNNING`, `SUCCEEDED`, `FAILED`, + `CANCELLED`), so a poller written
against Batch reads either.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Mapping, Protocol, Sequence

from click import argument, group, option

#: The variable a local job's access token is handed to the host's registry credential helper in
#: (`deploy/vm-pilot/docker-credential-disky`: the engine pulls the job's image as the job's account). Never a container's.
TOKEN_ENV = "GOOGLE_OAUTH_ACCESS_TOKEN"
#: An access token's lifetime: IAM Credentials' default maximum (up to 12 h only where the org policy
#: `iam.allowServiceAccountCredentialLifetimeExtension` lists the account). `$DISKY_LOCAL_TOKEN_LIFETIME_S` at submit.
TOKEN_LIFETIME_S = 3600
SCOPE = "https://www.googleapis.com/auth/cloud-platform"

QUEUED, SCHEDULED, RUNNING, SUCCEEDED, FAILED, CANCELLED = "QUEUED", "SCHEDULED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"
#: The states a job doesn't leave (Batch's `DELETION_IN_PROGRESS`: a deleted job, which is how a Batch job is cancelled).
TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED, "DELETION_IN_PROGRESS"})
#: `$DISKY_JOB_BACKEND`'s values; unset is `batch`.
BACKENDS = ("batch", "local")
BACKEND_ENV = "DISKY_JOB_BACKEND"


@dataclass(frozen=True)
class JobStatus:
    """A job's state (Batch's vocabulary), a backend's short note on it (Batch: its task counts; local: an exit code, or
    why a job was lost) and, where the backend counts them, its tasks by state (`job_watch.task_counts`)."""
    state: str
    detail: str = ""
    tasks: Mapping[str, int] | None = field(default=None, compare=False)


class JobBackend(Protocol):
    #: How a message names one of its jobs (`Batch job`, `local job`).
    what: str

    def submit(self, spec: dict, job_id: str, *, buckets: Sequence[str] = (), cross_region: bool | None = None,
               exists_ok: bool = False, retry_for: float = 0) -> str:
        """Create the job `job_id` from a Batch job spec; returns its id. `exists_ok`: a job of that id already there
        counts as submitted. `buckets` / `cross_region` / `retry_for` are `batch.submit_job`'s (a backend with no
        region or network ignores them)."""
        ...

    def status(self, job_id: str) -> JobStatus: ...

    def logs(self, job_id: str, limit: int = 40) -> list[str]:
        """The job's last `limit` output lines, oldest first."""
        ...

    def cancel(self, job_id: str) -> None: ...


# ── GCP Batch: today's ─────────────────────────────────────────────────────


class GcpBatch:
    """Batch jobs in `region` / `project` (each default as `batch.submit_job`'s): `batch.submit_job` (still the one
    place a Batch job is created, with its cost-label and region checks), `gcp.batch_job`, Cloud Logging's task logs,
    and a cancel by delete (as `batch.cancel_labelled`)."""
    what = "Batch job"

    def __init__(self, region: str | None = None, project: str | None = None):
        self.region, self.project = region, project

    def submit(self, spec: dict, job_id: str, *, buckets: Sequence[str] = (), cross_region: bool | None = None,
               exists_ok: bool = False, retry_for: float = 0) -> str:
        from . import batch

        return batch.submit_job(spec, job_id, self.region, self.project, buckets=buckets, cross_region=cross_region,
                                exists_ok=exists_ok, retry_for=retry_for)

    def status(self, job_id: str) -> JobStatus:
        from . import gcp

        from .job_watch import task_counts

        st = gcp.batch_job(job_id, project=self.project, region=self.region).get("status", {})
        counts = " ".join(f"{k}={v}" for g in (st.get("taskGroups") or {}).values() for k, v in sorted(g.get("counts", {}).items()))
        return JobStatus(st.get("state", "?"), counts, task_counts(st.get("taskGroups") or {}))

    def logs(self, job_id: str, limit: int = 40) -> list[str]:
        from . import gcp

        uid = gcp.batch_job(job_id, project=self.project, region=self.region)["uid"]
        entries = gcp.log_entries(gcp.task_log_filter(uid), project=self.project, limit=limit, asc=False,
                                  resource=gcp.task_log_resource(self.project))
        return [(e.get("textPayload") or e.get("jsonPayload", {}).get("message", "")).rstrip() for e in reversed(entries)]

    def cancel(self, job_id: str) -> None:
        from . import batch

        batch.cancel_job(job_id, region=self.region, project=self.project)


# ── Local: this host, under a systemd slice ────────────────────────────────


class Unsupported(ValueError):
    """A spec asks for something the local backend doesn't do (yet): refused before anything is written."""


class JobExists(RuntimeError):
    """A local job of that id is already there (and the submit didn't say `exists_ok`): Batch's 409."""


class NoSuchJob(LookupError):
    """No local job of that id (Batch's 404)."""


@dataclass(frozen=True)
class Run:
    """One container run of a local job: task `task`'s runnable, as `argv`, its container named `name`; `secrets` maps
    each secret variable to its Secret Manager version (a name, never a value; resolved by the supervisor)."""
    task: int
    name: str
    argv: list[str]
    secrets: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GcsMount:
    """A gcsfuse task volume: `bucket` (only `only_dir` of it, when its `remotePath` names one) mounted at host dir
    `host` for the job's runs, with gcsfuse's `options` (the volume's `mountOptions`, as argv)."""
    bucket: str
    host: Path
    only_dir: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    """A spec translated for this host: its runs (task by task, each task's runnables in order), the host dirs standing
    in for its new disks (created before the first run, removed after the last), its existing disks' dirs (`keep`:
    created when missing, never removed), its gcsfuse volumes, and each task's time limit."""
    runs: list[Run]
    dirs: list[Path]
    task_timeout_s: float | None
    service_account: str = ""
    keep: list[Path] = field(default_factory=list)
    gcs: list[GcsMount] = field(default_factory=list)


def _duration_s(v: str | None) -> float | None:
    """A Batch duration (`"14400s"`) in seconds; None for none."""
    return float(v.removesuffix("s")) if v else None


def _env(env: Mapping, where: str, job_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """An `environment` block's plain variables and its secret ones (`{VAR: projects/…/secrets/…/versions/…}`);
    KMS-encrypted ones refused."""
    if env.get("encryptedVariables"):
        raise Unsupported(f"local job {job_id}: {where}.environment.encryptedVariables "
                          f"({', '.join(sorted(env['encryptedVariables']))}): not supported locally; use secretVariables")
    secrets = dict(env.get("secretVariables") or {})
    if bad := sorted(k for k, ref in secrets.items() if not str(ref).startswith("projects/")):
        raise Unsupported(f"local job {job_id}: {where}.environment.secretVariables ({', '.join(bad)}): not a "
                          "projects/…/secrets/…/versions/… name")
    return dict(env.get("variables") or {}), secrets


def service_account(spec: Mapping, job_id: str) -> str:
    """The spec's `allocationPolicy.serviceAccount.email`: who a local job runs as. Required: without it a job would run
    as the host, whose account isn't any tenant's."""
    email = (((spec.get("allocationPolicy") or {}).get("serviceAccount") or {}).get("email") or "").strip()
    if not email:
        raise Unsupported(f"local job {job_id}: no allocationPolicy.serviceAccount.email: a local job runs as its spec's "
                          "service account, never as the host's")
    return email


def sa_project(email: str) -> str | None:
    """`job@<project>.iam.gserviceaccount.com`'s project; None for another form."""
    host = email.partition("@")[2]
    return host.removesuffix(".iam.gserviceaccount.com") if host.endswith(".iam.gserviceaccount.com") else None


def network_name(job_id: str) -> str:
    """The job's own engine network: its containers' only one, its metadata server on the gateway."""
    return f"disky-{job_id}"


def gcsfuse_options(options: Sequence[str]) -> tuple[str, ...]:
    """A gcs volume's `mountOptions` (`["-o rw", "-implicit-dirs"]`, what nf-google passes) as gcsfuse argv: each split
    on whitespace, a single-dash long flag (`-implicit-dirs`, gcsfuse v1's form) given its second dash."""
    out = []
    for o in options:
        for tok in str(o).split():
            out.append(f"-{tok}" if tok.startswith("-") and not tok.startswith("--") and len(tok) > 2 else tok)
    return tuple(out)


def existing_disks(spec: Mapping) -> dict[str, str]:
    """`deviceName` → the existing disk's name, for each `allocationPolicy.instances[].policy.disks[]` naming an
    `existingDisk` (`projects/…/disks/<name>` or the bare name)."""
    out = {}
    for inst in (spec.get("allocationPolicy") or {}).get("instances") or []:
        for d in ((inst or {}).get("policy") or {}).get("disks") or []:
            if d.get("deviceName") and d.get("existingDisk"):
                out[d["deviceName"]] = str(d["existingDisk"]).rstrip("/").rsplit("/", 1)[-1]
    return out


def container_runs(spec: Mapping, job_id: str, *, engine: str, slice_: str, disks: Path, kept: Path, gcs_root: Path) -> Plan:
    """`spec` (a Batch job) as `engine run` command lines on this host, every container in `slice_` (`--cgroup-parent`)
    and on the job's own network (`network_name`).

    - Each of the task group's `taskCount` tasks runs in turn (not `parallelism` at once: one job's tasks share the
      slice), each runnable a `--rm` container named `disky-<job>-<task>-<runnable>`, with `$BATCH_TASK_INDEX` /
      `$BATCH_TASK_COUNT` as Batch sets them.
    - `imageUri`, `entrypoint` (`--entrypoint`), `commands` (after the image), the task's and the runnable's
      `environment.variables` (`-e K=V`, sorted; the runnable's win); its `secretVariables` and the job's metadata
      server's address (`job_metadata.ADDRESS_ENVS`) as `-e K` alone, the engine taking each value from its own
      environment (the supervisor's, `Local._runs`); `$GOOGLE_CLOUD_PROJECT` the service account's project unless the
      spec sets it.
    - The service account (`allocationPolicy.serviceAccount.email`) is who the job runs as: required.
    - A task volume on a disk (`deviceName`) is a host dir: a new disk's (a local SSD, `newDisk`) `disks/<deviceName>`,
      created for the job and removed after it; an existing disk's (`allocationPolicy`'s `existingDisk` for that
      device) `kept/<disk name>`, which outlives the job, as the disk outlives a Batch job's VM. A gcsfuse volume
      (`gcs.remotePath`, its `mountOptions`) is mounted on the host at `gcs_root/<i>` for the job's runs, as the job's
      account. A container volume `HOST:CTR[:mode]` whose `HOST` is under a task volume's `mountPath` is mounted from
      that dir.
    - Refused (`Unsupported`): no service account, an NFS volume, a container volume outside every task volume,
      KMS-encrypted variables, a runnable that isn't a container (a script, a barrier), `background` /
      `ignoreExitStatus` / `alwaysRun`, a container's registry credentials or raw `options`, more than one task group.
    - Not applied: `computeResource` and the rest of `allocationPolicy` (the slice is the budget), `maxRetryCount` (no
      spot here), `logsPolicy` (output goes to the job's `log`)."""
    from .job_metadata import ADDRESS_ENVS

    sa = service_account(spec, job_id)
    groups = spec.get("taskGroups") or []
    if len(groups) != 1:
        raise Unsupported(f"local job {job_id}: one task group, not {len(groups)}")
    group = groups[0]
    task = group.get("taskSpec") or {}
    count = int(group.get("taskCount", 1))
    task_env, task_secrets = _env(task.get("environment") or {}, "taskSpec", job_id)
    existing = existing_disks(spec)
    mounts: dict[str, Path] = {}
    new: list[Path] = []
    keep: list[Path] = []
    gcs: list[GcsMount] = []
    for v in task.get("volumes") or []:
        at = str(v.get("mountPath") or "").rstrip("/")
        if v.get("gcs"):
            remote = str(v["gcs"].get("remotePath") or "").strip("/")
            if not remote:
                raise Unsupported(f"local job {job_id}: gcsfuse volume at {at}: no remotePath")
            bucket, _, only = remote.partition("/")
            mounts[at] = gcs_root / str(len(gcs))
            gcs.append(GcsMount(bucket, mounts[at], only, gcsfuse_options(v.get("mountOptions") or [])))
            continue
        if not v.get("deviceName"):
            raise Unsupported(f"local job {job_id}: volume at {at}: only disk (deviceName) and gcsfuse (gcs) volumes run locally")
        if v["deviceName"] in existing:
            mounts[at] = kept / existing[v["deviceName"]]
            keep.append(mounts[at])
        else:
            mounts[at] = disks / v["deviceName"]
            new.append(mounts[at])

    def host_path(path: str) -> str:
        for at, d in sorted(mounts.items(), key=lambda m: -len(m[0])):  # the innermost volume holding it
            if path == at or path.startswith(f"{at}/"):
                return str(d) + path[len(at):]
        raise Unsupported(f"local job {job_id}: container volume host path {path}: not under a task volume "
                          f"({', '.join(sorted(mounts)) or 'none'})")

    runs: list[Run] = []
    runnables = task.get("runnables") or []
    for t in range(count):
        for r, runnable in enumerate(runnables):
            c = runnable.get("container")
            if not c:
                raise Unsupported(f"local job {job_id}: runnable {r} isn't a container ({', '.join(sorted(runnable))})")
            if bad := [k for k in ("background", "ignoreExitStatus", "alwaysRun") if runnable.get(k)]:
                raise Unsupported(f"local job {job_id}: runnable {r}: {', '.join(bad)} not supported locally")
            if bad := [k for k in ("username", "password", "options") if c.get(k)]:
                raise Unsupported(f"local job {job_id}: runnable {r}: container {', '.join(bad)} not supported locally")
            run_env, run_secrets = _env(runnable.get("environment") or {}, f"runnables[{r}]", job_id)
            secrets = {**task_secrets, **run_secrets}
            # the metadata server's address is the backend's to set: a spec's would point its containers elsewhere
            env = {k: v for k, v in {**task_env, **run_env}.items() if k not in secrets and k not in ADDRESS_ENVS}
            if (project := sa_project(sa)) and "GOOGLE_CLOUD_PROJECT" not in env:
                env["GOOGLE_CLOUD_PROJECT"] = project
            env.update(BATCH_TASK_INDEX=str(t), BATCH_TASK_COUNT=str(count))
            name = f"disky-{job_id}-{t}-{r}"
            argv = [engine, "run", "--rm", "--name", name, f"--cgroup-parent={slice_}", "--network", network_name(job_id)]
            for k in sorted({*env, *secrets, *ADDRESS_ENVS}):
                argv += ["-e", f"{k}={env[k]}" if k in env else k]
            for vol in c.get("volumes") or []:
                src, _, rest = vol.partition(":")
                argv += ["-v", f"{host_path(src)}:{rest}"]
            if c.get("entrypoint"):
                argv += ["--entrypoint", c["entrypoint"]]
            argv += [c["imageUri"], *(c.get("commands") or [])]
            runs.append(Run(t, name, argv, secrets))
    return Plan(runs, sorted(set(new)), _duration_s(task.get("maxRunDuration")), sa, sorted(set(keep)), gcs)


class Host(Protocol):
    """What `Local` does to this host (a fake in tests)."""

    def spawn(self, argv: list[str], log: Path) -> None:
        """Start `argv` detached (it outlives the caller), its output to `log`."""
        ...

    def run(self, argv: list[str], log: Path, timeout: float | None, env: Mapping[str, str] | None = None) -> int | None:
        """Run `argv` to its end, output appended to `log`, `env` added to its environment (only); its exit code, or
        None when `timeout` ran out (it is killed)."""
        ...

    def alive(self, pid: int) -> bool: ...

    def boot_id(self) -> str: ...

    def capture(self, argv: list[str], env: Mapping[str, str] | None = None) -> str:
        """Run `argv` to its end, `env` added to its environment; its stdout (`subprocess.CalledProcessError` on a
        nonzero exit)."""
        ...

    def start(self, argv: list[str], log: Path, env: Mapping[str, str]) -> "subprocess.Popen":
        """Start `argv` as a child (a mount's daemon, `--foreground`), output to `log`, with exactly `env`."""
        ...

    def is_mount(self, path: Path) -> bool: ...

    def mounts_under(self, path: Path) -> list[str]:
        """Every mount point strictly below `path` (`/proc/self/mounts`): a dir with one is never removed."""
        ...

    def cgroup(self, engine: str, container: str) -> Path | None:
        """The running container's cgroup v2 dir; None before it starts or after it exits."""
        ...


class LocalHost:
    """The real host: subprocesses, `kill -0`, Linux's boot id, its mounts and cgroups."""

    def spawn(self, argv: list[str], log: Path) -> None:
        with open(log, "ab") as out:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)

    def run(self, argv: list[str], log: Path, timeout: float | None, env: Mapping[str, str] | None = None) -> int | None:
        with open(log, "ab") as out:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 env={**os.environ, **env} if env else None)
            try:
                return p.wait(timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
                return None

    def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def boot_id(self) -> str:
        p = Path("/proc/sys/kernel/random/boot_id")
        return p.read_text().strip() if p.exists() else ""

    def capture(self, argv: list[str], env: Mapping[str, str] | None = None) -> str:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
                              env={**os.environ, **env} if env else None).stdout

    def start(self, argv: list[str], log: Path, env: Mapping[str, str]) -> subprocess.Popen:
        with open(log, "ab") as out:
            return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=dict(env))

    def is_mount(self, path: Path) -> bool:
        return os.path.ismount(path)

    def mounts_under(self, path: Path) -> list[str]:
        base = str(Path(path).resolve()).rstrip("/") + "/"
        try:
            lines = Path("/proc/self/mounts").read_text().splitlines()
        except OSError:
            return []
        # /proc/self/mounts escapes a space in a path as \040
        return [m for m in (line.split()[1].replace("\\040", " ") for line in lines if len(line.split()) > 1) if m.startswith(base)]

    def cgroup(self, engine: str, container: str) -> Path | None:
        from .job_usage import cgroup_of_pid

        try:
            pid = int(self.capture([engine, "inspect", "-f", "{{.State.Pid}}", container]).strip() or 0)
        except (subprocess.CalledProcessError, ValueError):
            return None
        return cgroup_of_pid(pid) if pid > 0 else None


class Identity(Protocol):
    """Who a local job runs as (a fake in tests)."""

    def access_token(self, service_account: str, lifetime_s: int) -> str:
        """A short-lived access token for `service_account`, minted under the host's own credentials."""
        ...

    def secret(self, ref: str, token: str) -> str:
        """Secret Manager version `ref`'s payload, read with `token` (the job's identity, not the host's)."""
        ...


class GcpIdentity:
    """IAM Credentials' `generateAccessToken` under this host's credentials (on the VM pilot's box, its account, holding
    `iam.serviceAccountTokenCreator` on each tenant's job account and nothing of theirs), and Secret Manager's REST
    `:access` with the minted token. Payloads as Batch injects them (verbatim)."""

    def access_token(self, service_account: str, lifetime_s: int) -> str:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        creds, _ = google.auth.default(scopes=[SCOPE])
        r = AuthorizedSession(creds).post(
            f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{service_account}:generateAccessToken",
            json={"scope": [SCOPE], "lifetime": f"{lifetime_s}s"}, timeout=60)
        r.raise_for_status()
        return r.json()["accessToken"]

    def secret(self, ref: str, token: str) -> str:
        from base64 import b64decode

        import requests

        r = requests.get(f"https://secretmanager.googleapis.com/v1/{ref}:access",
                         headers={"Authorization": f"Bearer {token}"}, timeout=60)
        r.raise_for_status()
        return b64decode(r.json()["payload"]["data"]).decode()


#: How long a submitted job's supervisor has to start and say so before the job counts as lost.
START_GRACE_S = 120
#: How often a queued job's supervisor looks for its turn.
POLL_S = 10
#: How long a stopped container (cancel, a time limit) has to end after its TERM (`engine stop -t`), before the engine
#: kills it: a scan head finishes its run record in it.
STOP_GRACE_S = 60
#: How long `engine stop` itself may take.
STOP_TIMEOUT_S = 120
#: Spawn modes: `systemd-run` (the supervisor its own transient unit in the slice, so it outlives a submitter that is
#: itself a systemd unit) or `direct` (a detached process; a dev box).
SPAWNS = ("systemd-run", "direct")
#: Where existing disks' dirs live, under the work root (or the jobs root): not a job id (`_` isn't allowed in one).
KEPT = "_disks"
#: How long gcsfuse has to mount a bucket, and to exit once it's unmounted.
MOUNT_WAIT_S = 60
UNMOUNT_WAIT_S = 30
#: How often a container run's cgroup is sampled (`job_usage.Sampler`).
SAMPLE_S = 2.0


def _write_json(path: Path, doc: Mapping) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc, sort_keys=True))
    tmp.replace(path)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


class Local:
    """Jobs on this host, state under `root/<job_id>/`:
    `spec.json` (as submitted), `job.json` (tenant, submit order, the translated runs: `container_runs`),
    `status.json` (`{state, detail}`), `supervisor.json` (`{pid, boot, at}`: its supervisor, pid 0 until it starts),
    `log` (every run's output), `exit` (the last run's exit code), `cancel` (a cancel's marker), `usage.tsv` and
    `usage.jsonl` (each run's cgroup, sampled: `job_usage`), `gcs/<i>` (its gcsfuse volumes' mount points) and
    `gcsfuse-<i>.log`.

    A submit writes those and spawns the job's supervisor (`python -m dt_cloud.job_backend supervise ROOT ID`), which
    waits its turn (the tenant's oldest queued job, none of the tenant's running: one at a time per `deployment`),
    sets up the job's network, its metadata server (`job_metadata`) and its gcsfuse mounts, runs the job's containers
    in order, records the end, and takes the setup down. A job whose supervisor is gone (a reboot, a kill) while
    queued or running is `FAILED`, "supervisor lost", the next time anyone asks; a supervisor's TERM (`systemctl stop
    disky-job-<id>`) is a cancel.

    The job's containers (and its gcsfuse mounts) get their tokens from the job's metadata server, minted for the
    spec's service account (`identity`) and refreshed while the job runs; each run's secret values are read with such a
    token and reach the engine's environment only: `job.json` holds the secrets' names, never a value or a token."""
    what = "local job"

    def __init__(self, root: Path | str, *, work: Path | str | None = None, engine: str = "docker",
                 slice_: str = "scan.slice", spawn: str = "systemd-run", host: Host | None = None,
                 identity: Identity | None = None, token_lifetime_s: int = TOKEN_LIFETIME_S,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 poll_s: float = POLL_S, sample_s: float = SAMPLE_S, gcsfuse: str = "gcsfuse"):
        if spawn not in SPAWNS:
            raise ValueError(f"spawn {spawn!r}: one of {', '.join(SPAWNS)}")
        self.root, self.work = Path(root), Path(work) if work else None
        self.engine, self.slice, self.spawn = engine, slice_, spawn
        self.host: Host = host or LocalHost()
        self.identity: Identity = identity or GcpIdentity()
        self.token_lifetime_s = token_lifetime_s
        self.sleep, self.clock, self.poll_s, self.sample_s = sleep, clock, poll_s, sample_s
        self.gcsfuse = gcsfuse

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Local":
        """`$DISKY_LOCAL_JOBS` (the state root, required), `$DISKY_LOCAL_WORK` (the disks' host dirs; default under each
        job's dir), `$DISKY_LOCAL_ENGINE` (`docker`), `$DISKY_LOCAL_SLICE` (`scan.slice`), `$DISKY_LOCAL_SPAWN`
        (`systemd-run`), `$DISKY_LOCAL_TOKEN_LIFETIME_S` (3600)."""
        e = os.environ if environ is None else environ
        root = (e.get("DISKY_LOCAL_JOBS") or "").strip()
        if not root:
            raise SystemExit("DISKY_LOCAL_JOBS is unset: export the dir the local job backend keeps its jobs' state in")
        return cls(root, work=e.get("DISKY_LOCAL_WORK") or None, engine=e.get("DISKY_LOCAL_ENGINE") or "docker",
                   slice_=e.get("DISKY_LOCAL_SLICE") or "scan.slice", spawn=e.get("DISKY_LOCAL_SPAWN") or "systemd-run",
                   token_lifetime_s=int(e.get("DISKY_LOCAL_TOKEN_LIFETIME_S") or TOKEN_LIFETIME_S))

    # ── paths and state ──

    def dir(self, job_id: str) -> Path:
        return self.root / job_id

    @property
    def kept_root(self) -> Path:
        """Where existing disks' host dirs are (`container_runs`): each outlives every job that mounts it."""
        return (self.work or self.root) / KEPT

    def job(self, job_id: str) -> dict:
        """The job's `job.json` (`NoSuchJob` when there is none)."""
        p = self.dir(job_id) / "job.json"
        if not p.exists():
            raise NoSuchJob(f"local job {job_id}: no such job under {self.root}")
        return _read_json(p)

    def _set(self, job_id: str, state: str, detail: str = "") -> None:
        _write_json(self.dir(job_id) / "status.json", {"state": state, "detail": detail})

    def _alive(self, job_id: str) -> bool:
        s = _read_json(self.dir(job_id) / "supervisor.json")
        if s["boot"] != self.host.boot_id():
            return False
        if not s["pid"]:
            return self.clock() - s["at"] < START_GRACE_S
        return self.host.alive(s["pid"])

    @contextmanager
    def _lock(self, tenant: str) -> Iterator[None]:
        import fcntl

        d = self.root / ".locks"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"{tenant}.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def supervisor_argv(self, job_id: str) -> list[str]:
        """The supervisor's command line. As a systemd unit, a stop TERMs only the supervisor (`KillMode=mixed`): it
        stops the job's container and takes the job's setup down (its gcsfuse mounts, children in the unit) itself,
        within `TimeoutStopSec`."""
        cmd = [sys.executable, "-m", "dt_cloud.job_backend", "supervise", str(self.root), job_id]
        if self.spawn == "direct":
            return cmd
        return ["systemd-run", "--quiet", "--collect", f"--unit=disky-job-{job_id}", f"--slice={self.slice}",
                "--property=KillMode=mixed", f"--property=TimeoutStopSec={STOP_TIMEOUT_S + 60}", "--", *cmd]

    # ── the interface ──

    def submit(self, spec: dict, job_id: str, *, buckets: Sequence[str] = (), cross_region: bool | None = None,
               exists_ok: bool = False, retry_for: float = 0) -> str:
        """Refused before anything is written: a bad id (Batch's rule, `batch.check_job_id`), a spec without its cost
        labels (`cost_labels.check_spec`, as on Batch: `deployment` is the tenant), anything `container_runs` refuses.
        `buckets`, `cross_region` and `retry_for` don't apply here."""
        from .batch import check_job_id
        from .cost_labels import check_spec

        check_job_id(job_id)
        check_spec(spec, f"{self.what} {job_id}")
        disks = (self.work / job_id) if self.work else self.dir(job_id) / "disks"
        plan = container_runs(spec, job_id, engine=self.engine, slice_=self.slice, disks=disks,
                              kept=self.kept_root, gcs_root=self.dir(job_id) / "gcs")
        self.root.mkdir(parents=True, exist_ok=True)
        d = self.dir(job_id)
        try:
            d.mkdir()
        except FileExistsError:
            if not exists_ok:
                raise JobExists(f"{self.what} {job_id} already exists under {self.root}") from None
            print(f"note: {self.what} {job_id} already exists: counted as submitted", file=sys.stderr)
            return job_id
        _write_json(d / "spec.json", spec)
        self._set(job_id, QUEUED)
        _write_json(d / "supervisor.json", {"pid": 0, "boot": self.host.boot_id(), "at": self.clock()})
        # last: a job is listed (`job`, `_tenant_jobs`) only once its state is whole
        _write_json(d / "job.json", {
            "tenant": spec["labels"]["deployment"], "seq": time.time_ns(), "engine": self.engine,
            "runs": [{"task": r.task, "name": r.name, "argv": r.argv, "secrets": dict(r.secrets)} for r in plan.runs],
            "dirs": [str(p) for p in plan.dirs], "keep": [str(p) for p in plan.keep],
            "gcs": [{"bucket": g.bucket, "host": str(g.host), "only_dir": g.only_dir, "options": list(g.options)} for g in plan.gcs],
            "network": network_name(job_id), "task_timeout_s": plan.task_timeout_s,
            "service_account": plan.service_account, "token_lifetime_s": self.token_lifetime_s,
        })
        self.host.spawn(self.supervisor_argv(job_id), d / "supervisor.log")
        return job_id

    def status(self, job_id: str) -> JobStatus:
        self.job(job_id)
        path = self.dir(job_id) / "status.json"
        st = _read_json(path)
        if st["state"] in (QUEUED, RUNNING) and not self._alive(job_id):
            st = _read_json(path)  # it may have ended between the two reads
            if st["state"] in (QUEUED, RUNNING):
                st = {"state": FAILED, "detail": f"supervisor lost while {st['state'].lower()}"}
                self._set(job_id, st["state"], st["detail"])
        return JobStatus(st["state"], st["detail"])

    def logs(self, job_id: str, limit: int = 40) -> list[str]:
        self.job(job_id)
        p = self.dir(job_id) / "log"
        return p.read_text(errors="replace").splitlines()[-limit:] if p.exists() else []

    def cancel(self, job_id: str) -> None:
        """A queued job is `CANCELLED` at once; a running one's current container is stopped (`engine stop`), and its
        supervisor records `CANCELLED` and starts no further run. An ended job is left as it is."""
        job = self.job(job_id)
        d = self.dir(job_id)
        (d / "cancel").touch()
        with self._lock(job["tenant"]):
            st = self.status(job_id)
            if st.state == QUEUED:
                self._set(job_id, CANCELLED, "cancelled while queued")
                return
        if st.state == RUNNING:
            self.stop_current(job_id)

    def stop_current(self, job_id: str) -> None:
        """Mark the job cancelled and stop its running container (`engine stop`, `STOP_GRACE_S` to end), without the
        tenant's lock: also what the supervisor's own TERM handler does (`main`), which may interrupt a holder of it."""
        job = self.job(job_id)
        d = self.dir(job_id)
        (d / "cancel").touch()
        if (cur := d / "current").exists():
            self.host.run([job["engine"], "stop", "-t", str(STOP_GRACE_S), cur.read_text()], d / "control.log", STOP_TIMEOUT_S)

    # ── the supervisor ──

    def _tenant_jobs(self, tenant: str) -> list[tuple[int, str]]:
        out = []
        for p in self.root.iterdir():
            if (p / "job.json").exists() and (j := _read_json(p / "job.json"))["tenant"] == tenant:
                out.append((j["seq"], p.name))
        return out

    def try_start(self, job_id: str) -> bool | None:
        """Under the tenant's lock: start `job_id` (`RUNNING`, True) when it is the tenant's oldest queued job and none
        of the tenant's runs; False while it waits; None when it is no longer queued (cancelled, lost)."""
        job = self.job(job_id)
        with self._lock(job["tenant"]):
            if self.status(job_id).state != QUEUED:
                return None
            if (self.dir(job_id) / "cancel").exists():
                self._set(job_id, CANCELLED, "cancelled while queued")
                return None
            peers = self._tenant_jobs(job["tenant"])
            states = {jid: self.status(jid).state for _, jid in peers}
            if RUNNING in states.values() or min(p for p in peers if states[p[1]] == QUEUED) != (job["seq"], job_id):
                return False
            self._set(job_id, RUNNING)
            return True

    def supervise(self, job_id: str) -> JobStatus:
        """The job's supervisor: say it is up, wait its turn, set up the job (`_setup`: its dirs, network, metadata
        server, gcsfuse mounts), run its containers (each task within its time limit), record the end, take the setup
        down and remove its new disks' dirs. Returns the final status."""
        d = self.dir(job_id)
        job = self.job(job_id)
        _write_json(d / "supervisor.json", {"pid": os.getpid(), "boot": self.host.boot_id(), "at": self.clock()})
        while (turn := self.try_start(job_id)) is False:
            self.sleep(self.poll_s)
        if turn is None:
            return self.status(job_id)
        state, detail = FAILED, "supervisor failed"
        try:
            with self._setup(job_id, job) as meta:
                state, detail = self._runs(job_id, job, meta)
        except Exception as e:  # noqa: BLE001 — recorded as the job's failure, then raised
            detail = f"supervisor failed: {type(e).__name__}: {e}"
            raise
        finally:
            if (d / "cancel").exists() and state == FAILED:  # a stopped container exits nonzero
                state, detail = CANCELLED, f"cancelled: {detail}"
            self._set(job_id, state, detail)
            for p in job["dirs"]:
                if under := self.host.mounts_under(Path(p)):
                    print(f"{self.what} {job_id}: not removing {p}: {', '.join(under)} still mounted under it", file=sys.stderr)
                    continue
                shutil.rmtree(p, ignore_errors=True)
        return JobStatus(state, detail)

    @contextmanager
    def _setup(self, job_id: str, job: Mapping) -> Iterator[object]:
        """The job's dirs, its network, its metadata server on the network's gateway (answering only the network's
        subnet), and its gcsfuse mounts (as the job's account, through that server); yields the server. Everything is
        taken down after, in reverse, whatever happened."""
        from .job_metadata import MetadataServer

        d = self.dir(job_id)
        for p in [*job["dirs"], *job.get("keep", [])]:
            Path(p).mkdir(parents=True, exist_ok=True)
        sa = job["service_account"]
        lifetime = int(job.get("token_lifetime_s") or TOKEN_LIFETIME_S)
        net = job.get("network") or network_name(job_id)
        meta = None
        mounted: list[tuple[Mapping, object]] = []
        try:
            gateway, subnet = self._network(job["engine"], net, job_id)
            meta = MetadataServer(sa, sa_project(sa) or "", lambda s: self.identity.access_token(sa, s), bind=gateway,
                                  allow=subnet, lifetime_s=lifetime, clock=self.clock)
            meta.start()
            for i, g in enumerate(job.get("gcs") or []):
                mounted.append((g, self._mount(d, i, g, meta.env())))
            yield meta
        finally:
            for g, proc in reversed(mounted):
                self._unmount(job_id, g, proc)
            if meta is not None:
                meta.stop()
            try:
                self.host.capture([job["engine"], "network", "rm", net])
            except subprocess.CalledProcessError as e:
                print(f"{self.what} {job_id}: network {net} not removed: {e.stderr or e}", file=sys.stderr)

    def _network(self, engine: str, net: str, job_id: str) -> tuple[str, str]:
        """Create the job's network (`network_name`); its gateway and subnet. A network of that name left by a lost
        supervisor is removed first."""
        try:
            self.host.capture([engine, "network", "rm", net])
        except subprocess.CalledProcessError:
            pass  # none there: the usual case
        self.host.capture([engine, "network", "create", "--label", f"disky.job={job_id}", net])
        for n in json.loads(self.host.capture([engine, "network", "inspect", net]) or "[]"):
            # Docker's shape (`IPAM.Config[]`), Podman's (`subnets[]`)
            for c in [*((n.get("IPAM") or {}).get("Config") or []), *(n.get("subnets") or [])]:
                gw, sub = c.get("Gateway") or c.get("gateway"), c.get("Subnet") or c.get("subnet")
                if gw and sub and ":" not in gw:
                    return gw, sub
        raise RuntimeError(f"network {net}: no IPv4 gateway in its inspect")

    def _mount(self, d: Path, i: int, g: Mapping, meta_env: Mapping[str, str]) -> object:
        """gcsfuse `g` (`GcsMount`'s fields) at its host dir, in the foreground as this supervisor's child (so in its
        unit's cgroup, in the slice), its tokens from the job's metadata server; returns the process once mounted."""
        host = Path(g["host"])
        host.mkdir(parents=True, exist_ok=True)
        argv = [self.gcsfuse, "--foreground", *g.get("options", []), *(["--only-dir", g["only_dir"]] if g.get("only_dir") else []),
                g["bucket"], str(host)]
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(d), **meta_env}
        proc = self.host.start(argv, d / f"gcsfuse-{i}.log", env)
        deadline = self.clock() + MOUNT_WAIT_S
        while not self.host.is_mount(host):
            if (rc := proc.poll()) is not None:
                raise RuntimeError(f"gcsfuse {g['bucket']} at {host} exited {rc} before mounting (see {d / f'gcsfuse-{i}.log'})")
            if self.clock() > deadline:
                proc.kill()
                raise RuntimeError(f"gcsfuse {g['bucket']} at {host}: not mounted after {MOUNT_WAIT_S}s")
            self.sleep(0.5)
        return proc

    def _unmount(self, job_id: str, g: Mapping, proc) -> None:  # noqa: ANN001 — `Host.start`'s process
        """Unmount `g` (its gcsfuse then exits), stop gcsfuse if it doesn't, and remove the empty mount point; a mount
        that won't go is left, said, and never removed under."""
        host = Path(g["host"])
        for argv in (["umount", str(host)], ["umount", "-l", str(host)]):
            if not self.host.is_mount(host):
                break
            try:
                self.host.capture(argv)
            except subprocess.CalledProcessError as e:
                print(f"{self.what} {job_id}: {' '.join(argv)}: {e.stderr or e}", file=sys.stderr)
        try:
            proc.wait(UNMOUNT_WAIT_S)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(UNMOUNT_WAIT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self.host.is_mount(host):
            print(f"{self.what} {job_id}: {host} is still mounted: left as it is", file=sys.stderr)
            return
        try:
            host.rmdir()
        except OSError:
            pass

    def _runs(self, job_id: str, job: Mapping, meta) -> tuple[str, str]:  # noqa: ANN001 — a `job_metadata.MetadataServer`
        from .job_usage import Sampler

        d = self.dir(job_id)
        limit = job["task_timeout_s"]
        task, deadline = None, None
        for run in job["runs"]:
            if (d / "cancel").exists():
                return CANCELLED, "cancelled"
            if run["task"] != task:
                task, deadline = run["task"], None if limit is None else self.clock() + limit
            (d / "current").write_text(run["name"])
            env = self._run_env(run, meta)
            sampler = Sampler(run["name"], lambda name=run["name"]: self.host.cgroup(job["engine"], name), d / "usage.tsv",
                              self.sample_s, self.clock)
            started = self.clock()
            sampler.start()
            try:
                rc = self.host.run(run["argv"], d / "log", None if deadline is None else max(0.0, deadline - self.clock()), env=env)
            finally:
                usage = sampler.stop()
            ended = self.clock()
            with open(d / "usage.jsonl", "a") as f:
                f.write(json.dumps({"run": run["name"], "task": run["task"], "started": started, "ended": ended,
                                    "wall_s": round(ended - started, 1), "exit": rc, **usage}, sort_keys=True) + "\n")
            if rc is None:
                self.host.run([job["engine"], "stop", "-t", str(STOP_GRACE_S), run["name"]], d / "control.log", STOP_TIMEOUT_S)
                return FAILED, f"task {task}: maxRunDuration {limit:g}s exceeded"
            (d / "exit").write_text(str(rc))
            if rc != 0:
                return FAILED, f"task {task}: {run['name']} exited {rc}"
        return SUCCEEDED, "exit 0"

    def _run_env(self, run: Mapping, meta) -> dict[str, str]:  # noqa: ANN001 — as `_runs`
        """A run's environment additions, for the engine's process only: the job's metadata server's address (which
        `-e NAME` hands the container), the job's current token (for the engine's registry credential helper: the
        image is pulled as the job's account; never the container's), and each of the run's secret variables, read
        with that token (never with the host's credentials)."""
        token, _ = meta.token()
        return {**{k: self.identity.secret(ref, token) for k, ref in (run.get("secrets") or {}).items()}, TOKEN_ENV: token, **meta.env()}


# ── Selection ──────────────────────────────────────────────────────────────


def resolve(region: str | None = None, project: str | None = None, environ: Mapping[str, str] | None = None) -> JobBackend:
    """The deployment's job backend: `$DISKY_JOB_BACKEND` (`batch`, the default, in `region` / `project`; or `local`,
    `Local.from_env`)."""
    e = os.environ if environ is None else environ
    name = (e.get(BACKEND_ENV) or "batch").strip()
    if name == "batch":
        return GcpBatch(region, project)
    if name == "local":
        return Local.from_env(e)
    raise ValueError(f"${BACKEND_ENV}={name!r}: one of {', '.join(BACKENDS)}")


@group("local-job")
def cli() -> None:
    """This host's local jobs (`$DISKY_LOCAL_JOBS`, the local job backend's state; specs/vm-pilot.md "JobBackend")."""


@cli.command("status")
@option("-n", "--limit", default=10, show_default=True, help="Jobs to list (without JOB_ID)")
@argument("job_id", required=False)
def status_cmd(limit: int, job_id: str | None) -> None:
    """JOB_ID's state, detail and each container run's usage (wall time, the cgroup's `memory.peak`, OOM kills); without
    JOB_ID, the newest jobs' states."""
    jobs = Local.from_env()
    if job_id is None:
        rows = sorted((_read_json(p / "job.json")["seq"], p.name) for p in jobs.root.iterdir() if (p / "job.json").exists())
        for _, jid in reversed(rows[-limit:]):
            st = jobs.status(jid)
            print(f"{jid}\t{st.state}\t{st.detail}")
        return
    st = jobs.status(job_id)
    print(f"{job_id}\t{st.state}\t{st.detail}")
    from .job_usage import gib, read_runs

    for r in read_runs(jobs.dir(job_id) / "usage.jsonl"):
        print(f"  {r['run']}: exit {r['exit']}, {r['wall_s']:.0f}s, memory.peak {gib(r.get('memory_peak'))} GiB, "
              f"max anon {gib(r.get('max_anon'))} GiB, oom_kill {r.get('oom_kill')}")


@cli.command("logs")
@option("-n", "--lines", default=40, show_default=True, help="The last N lines")
@argument("job_id")
def logs_cmd(lines: int, job_id: str) -> None:
    """JOB_ID's output (every container run's), its last N lines."""
    for line in Local.from_env().logs(job_id, lines):
        print(line)


@cli.command("cancel")
@argument("job_id")
def cancel_cmd(job_id: str) -> None:
    """Cancel JOB_ID: a queued job at once; a running one's container is stopped (TERM, then `STOP_GRACE_S` to end), and
    its supervisor records `CANCELLED` and takes the job's setup (network, metadata server, gcsfuse mounts) down."""
    jobs = Local.from_env()
    jobs.cancel(job_id)
    st = jobs.status(job_id)
    print(f"{job_id}\t{st.state}\t{st.detail}")


def main(argv: list[str]) -> int:
    """`python -m dt_cloud.job_backend supervise ROOT JOB_ID`: a local job's supervisor (`Local.supervise`), as a
    submit spawns it. Its exit code is 0 when the job succeeded."""
    if len(argv) != 3 or argv[0] != "supervise":
        print("usage: python -m dt_cloud.job_backend supervise ROOT JOB_ID", file=sys.stderr)
        return 2
    jobs, job_id = Local(argv[1]), argv[2]

    def stop(sig: int, frame) -> None:  # noqa: ANN001
        # `systemctl stop disky-job-<id>`: a cancel. The container's stop returns once it has ended; its run then
        # returns, and `supervise` records CANCELLED and takes the job's setup down.
        jobs.stop_current(job_id)

    signal.signal(signal.SIGTERM, stop)
    return 0 if jobs.supervise(job_id).state == SUCCEEDED else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
