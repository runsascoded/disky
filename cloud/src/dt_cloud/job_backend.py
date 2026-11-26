"""Where a job runs (specs/vm-pilot.md "JobBackend (step 1)"): one interface over GCP Batch and this host.

A spec is a Batch job's JSON whatever the backend: every submitter builds one already, and `GcpBatch` posts it as it
is. `Local` runs the same spec's container runnables on this host under a systemd slice (`scan.slice`), one job at a
time per tenant (the spec's `deployment` label), with its state on disk so `status` / `logs` / `cancel` work from any
process and after a restart. The deployment names its backend (`$DISKY_JOB_BACKEND`, `resolve`); unset, it is Batch.

States are Batch's (`QUEUED`, `SCHEDULED`, `RUNNING`, `SUCCEEDED`, `FAILED`, + `CANCELLED`), so a poller written
against Batch reads either.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Protocol, Sequence

QUEUED, SCHEDULED, RUNNING, SUCCEEDED, FAILED, CANCELLED = "QUEUED", "SCHEDULED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"
#: The states a job doesn't leave (Batch's `DELETION_IN_PROGRESS`: a deleted job, which is how a Batch job is cancelled).
TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED, "DELETION_IN_PROGRESS"})
#: `$DISKY_JOB_BACKEND`'s values; unset is `batch`.
BACKENDS = ("batch", "local")
BACKEND_ENV = "DISKY_JOB_BACKEND"


@dataclass(frozen=True)
class JobStatus:
    """A job's state (Batch's vocabulary) and a backend's short note on it (Batch: its task counts; local: an exit
    code, or why a job was lost)."""
    state: str
    detail: str = ""


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

        st = gcp.batch_job(job_id, project=self.project, region=self.region).get("status", {})
        counts = " ".join(f"{k}={v}" for g in (st.get("taskGroups") or {}).values() for k, v in sorted(g.get("counts", {}).items()))
        return JobStatus(st.get("state", "?"), counts)

    def logs(self, job_id: str, limit: int = 40) -> list[str]:
        from . import gcp

        uid = gcp.batch_job(job_id, project=self.project, region=self.region)["uid"]
        entries = gcp.log_entries(gcp.task_log_filter(uid), project=self.project, limit=limit, asc=False)
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
    """One container run of a local job: task `task`'s runnable, as `argv`, its container named `name`."""
    task: int
    name: str
    argv: list[str]


@dataclass(frozen=True)
class Plan:
    """A spec translated for this host: its runs (task by task, each task's runnables in order), the host dirs standing
    in for its disks (created before the first run, removed after the last), and each task's time limit."""
    runs: list[Run]
    dirs: list[Path]
    task_timeout_s: float | None


def _duration_s(v: str | None) -> float | None:
    """A Batch duration (`"14400s"`) in seconds; None for none."""
    return float(v.removesuffix("s")) if v else None


def _refuse_env(env: Mapping, where: str, job_id: str) -> dict[str, str]:
    """An `environment` block's plain variables; secret ones refused (the local backend resolves no secrets yet)."""
    for k in ("secretVariables", "encryptedVariables"):
        if env.get(k):
            raise Unsupported(f"local job {job_id}: {where}.environment.{k} ({', '.join(sorted(env[k]))}): the local "
                              "backend doesn't resolve secrets yet; run this job on Batch")
    return dict(env.get("variables") or {})


def container_runs(spec: Mapping, job_id: str, *, engine: str, slice_: str, disks: Path) -> Plan:
    """`spec` (a Batch job) as `engine run` command lines on this host, every container in `slice_` (`--cgroup-parent`).

    - Each of the task group's `taskCount` tasks runs in turn (not `parallelism` at once: one job's tasks share the
      slice), each runnable a `--rm` container named `disky-<job>-<task>-<runnable>`, with `$BATCH_TASK_INDEX` /
      `$BATCH_TASK_COUNT` as Batch sets them.
    - `imageUri`, `entrypoint` (`--entrypoint`), `commands` (after the image), the task's and the runnable's
      `environment.variables` (`-e`, sorted; the runnable's win).
    - A task volume on a disk (`deviceName`: a local SSD, a new disk) is a host dir, `disks/<deviceName>`; a container
      volume `HOST:CTR[:mode]` whose `HOST` is under a task volume's `mountPath` is mounted from that dir.
    - Refused (`Unsupported`): a gcsfuse (`gcs`) or NFS volume, a container volume outside every task volume, secret
      variables, a runnable that isn't a container (a script, a barrier), `background` / `ignoreExitStatus` /
      `alwaysRun`, a container's registry credentials or raw `options`, more than one task group.
    - Not applied: `computeResource` and `allocationPolicy` (the slice is the budget), `maxRetryCount` (no spot here),
      the service account (a job runs as this host's credentials), `logsPolicy` (output goes to the job's `log`)."""
    groups = spec.get("taskGroups") or []
    if len(groups) != 1:
        raise Unsupported(f"local job {job_id}: one task group, not {len(groups)}")
    group = groups[0]
    task = group.get("taskSpec") or {}
    count = int(group.get("taskCount", 1))
    task_env = _refuse_env(task.get("environment") or {}, "taskSpec", job_id)
    mounts: dict[str, Path] = {}
    for v in task.get("volumes") or []:
        if v.get("gcs"):
            raise Unsupported(f"local job {job_id}: gcsfuse volume {v['gcs'].get('remotePath')!r} at {v.get('mountPath')}: "
                              "the local backend mounts no buckets yet; read it through the API, or run this job on Batch")
        if not v.get("deviceName"):
            raise Unsupported(f"local job {job_id}: volume at {v.get('mountPath')}: only disk volumes (deviceName) run locally")
        mounts[v["mountPath"].rstrip("/")] = disks / v["deviceName"]

    def host_path(path: str) -> str:
        for at, d in mounts.items():
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
            env = {**task_env, **_refuse_env(runnable.get("environment") or {}, f"runnables[{r}]", job_id),
                   "BATCH_TASK_INDEX": str(t), "BATCH_TASK_COUNT": str(count)}
            name = f"disky-{job_id}-{t}-{r}"
            argv = [engine, "run", "--rm", "--name", name, f"--cgroup-parent={slice_}"]
            for k in sorted(env):
                argv += ["-e", f"{k}={env[k]}"]
            for vol in c.get("volumes") or []:
                src, _, rest = vol.partition(":")
                argv += ["-v", f"{host_path(src)}:{rest}"]
            if c.get("entrypoint"):
                argv += ["--entrypoint", c["entrypoint"]]
            argv += [c["imageUri"], *(c.get("commands") or [])]
            runs.append(Run(t, name, argv))
    return Plan(runs, sorted(set(mounts.values())), _duration_s(task.get("maxRunDuration")))


class Host(Protocol):
    """What `Local` does to this host (a fake in tests)."""

    def spawn(self, argv: list[str], log: Path) -> None:
        """Start `argv` detached (it outlives the caller), its output to `log`."""
        ...

    def run(self, argv: list[str], log: Path, timeout: float | None) -> int | None:
        """Run `argv` to its end, output appended to `log`; its exit code, or None when `timeout` ran out (it is killed)."""
        ...

    def alive(self, pid: int) -> bool: ...

    def boot_id(self) -> str: ...


class LocalHost:
    """The real host: subprocesses, `kill -0`, Linux's boot id."""

    def spawn(self, argv: list[str], log: Path) -> None:
        with open(log, "ab") as out:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)

    def run(self, argv: list[str], log: Path, timeout: float | None) -> int | None:
        with open(log, "ab") as out:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
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


#: How long a submitted job's supervisor has to start and say so before the job counts as lost.
START_GRACE_S = 120
#: How often a queued job's supervisor looks for its turn.
POLL_S = 10
#: How long `engine stop` waits for a container (cancel, a time limit) before the engine kills it.
STOP_TIMEOUT_S = 60
#: Spawn modes: `systemd-run` (the supervisor its own transient unit in the slice, so it outlives a submitter that is
#: itself a systemd unit) or `direct` (a detached process; a dev box).
SPAWNS = ("systemd-run", "direct")


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
    `log` (every run's output), `exit` (the last run's exit code), `cancel` (a cancel's marker).

    A submit writes those and spawns the job's supervisor (`python -m dt_cloud.job_backend supervise ROOT ID`), which
    waits its turn (the tenant's oldest queued job, none of the tenant's running: one at a time per `deployment`),
    runs the job's containers in order, and records the end. A job whose supervisor is gone (a reboot, a kill) while
    queued or running is `FAILED`, "supervisor lost", the next time anyone asks."""
    what = "local job"

    def __init__(self, root: Path | str, *, work: Path | str | None = None, engine: str = "docker",
                 slice_: str = "scan.slice", spawn: str = "systemd-run", host: Host | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 poll_s: float = POLL_S):
        if spawn not in SPAWNS:
            raise ValueError(f"spawn {spawn!r}: one of {', '.join(SPAWNS)}")
        self.root, self.work = Path(root), Path(work) if work else None
        self.engine, self.slice, self.spawn = engine, slice_, spawn
        self.host: Host = host or LocalHost()
        self.sleep, self.clock, self.poll_s = sleep, clock, poll_s

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Local":
        """`$DISKY_LOCAL_JOBS` (the state root, required), `$DISKY_LOCAL_WORK` (the disks' host dirs; default under each
        job's dir), `$DISKY_LOCAL_ENGINE` (`docker`), `$DISKY_LOCAL_SLICE` (`scan.slice`), `$DISKY_LOCAL_SPAWN`
        (`systemd-run`)."""
        e = os.environ if environ is None else environ
        root = (e.get("DISKY_LOCAL_JOBS") or "").strip()
        if not root:
            raise SystemExit("DISKY_LOCAL_JOBS is unset: export the dir the local job backend keeps its jobs' state in")
        return cls(root, work=e.get("DISKY_LOCAL_WORK") or None, engine=e.get("DISKY_LOCAL_ENGINE") or "docker",
                   slice_=e.get("DISKY_LOCAL_SLICE") or "scan.slice", spawn=e.get("DISKY_LOCAL_SPAWN") or "systemd-run")

    # ── paths and state ──

    def dir(self, job_id: str) -> Path:
        return self.root / job_id

    def _job(self, job_id: str) -> dict:
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
        cmd = [sys.executable, "-m", "dt_cloud.job_backend", "supervise", str(self.root), job_id]
        if self.spawn == "direct":
            return cmd
        return ["systemd-run", "--quiet", "--collect", f"--unit=disky-job-{job_id}", f"--slice={self.slice}", "--", *cmd]

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
        plan = container_runs(spec, job_id, engine=self.engine, slice_=self.slice, disks=disks)
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
        _write_json(d / "job.json", {
            "tenant": spec["labels"]["deployment"], "seq": time.time_ns(), "engine": self.engine,
            "runs": [{"task": r.task, "name": r.name, "argv": r.argv} for r in plan.runs],
            "dirs": [str(p) for p in plan.dirs], "task_timeout_s": plan.task_timeout_s,
        })
        self._set(job_id, QUEUED)
        _write_json(d / "supervisor.json", {"pid": 0, "boot": self.host.boot_id(), "at": self.clock()})
        self.host.spawn(self.supervisor_argv(job_id), d / "supervisor.log")
        return job_id

    def status(self, job_id: str) -> JobStatus:
        self._job(job_id)
        path = self.dir(job_id) / "status.json"
        st = _read_json(path)
        if st["state"] in (QUEUED, RUNNING) and not self._alive(job_id):
            st = _read_json(path)  # it may have ended between the two reads
            if st["state"] in (QUEUED, RUNNING):
                st = {"state": FAILED, "detail": f"supervisor lost while {st['state'].lower()}"}
                self._set(job_id, st["state"], st["detail"])
        return JobStatus(st["state"], st["detail"])

    def logs(self, job_id: str, limit: int = 40) -> list[str]:
        self._job(job_id)
        p = self.dir(job_id) / "log"
        return p.read_text(errors="replace").splitlines()[-limit:] if p.exists() else []

    def cancel(self, job_id: str) -> None:
        """A queued job is `CANCELLED` at once; a running one's current container is stopped (`engine stop`), and its
        supervisor records `CANCELLED` and starts no further run. An ended job is left as it is."""
        job = self._job(job_id)
        d = self.dir(job_id)
        (d / "cancel").touch()
        with self._lock(job["tenant"]):
            st = self.status(job_id)
            if st.state == QUEUED:
                self._set(job_id, CANCELLED, "cancelled while queued")
                return
        if st.state == RUNNING and (cur := d / "current").exists():
            self.host.run([job["engine"], "stop", "-t", "30", cur.read_text()], d / "control.log", STOP_TIMEOUT_S)

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
        job = self._job(job_id)
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
        """The job's supervisor: say it is up, wait its turn, run the job's containers (each task within its time
        limit), record the end, remove its disks' dirs. Returns the final status."""
        d = self.dir(job_id)
        job = self._job(job_id)
        _write_json(d / "supervisor.json", {"pid": os.getpid(), "boot": self.host.boot_id(), "at": self.clock()})
        while (turn := self.try_start(job_id)) is False:
            self.sleep(self.poll_s)
        if turn is None:
            return self.status(job_id)
        state, detail = FAILED, "supervisor failed"
        try:
            for p in job["dirs"]:
                Path(p).mkdir(parents=True, exist_ok=True)
            state, detail = self._runs(job_id, job)
        except Exception as e:  # noqa: BLE001 — recorded as the job's failure, then raised
            detail = f"supervisor failed: {type(e).__name__}: {e}"
            raise
        finally:
            if (d / "cancel").exists() and state == FAILED:  # a stopped container exits nonzero
                state, detail = CANCELLED, f"cancelled: {detail}"
            self._set(job_id, state, detail)
            for p in job["dirs"]:
                shutil.rmtree(p, ignore_errors=True)
        return JobStatus(state, detail)

    def _runs(self, job_id: str, job: Mapping) -> tuple[str, str]:
        d = self.dir(job_id)
        limit = job["task_timeout_s"]
        task, deadline = None, None
        for run in job["runs"]:
            if (d / "cancel").exists():
                return CANCELLED, "cancelled"
            if run["task"] != task:
                task, deadline = run["task"], None if limit is None else self.clock() + limit
            (d / "current").write_text(run["name"])
            rc = self.host.run(run["argv"], d / "log", None if deadline is None else max(0.0, deadline - self.clock()))
            if rc is None:
                self.host.run([job["engine"], "stop", "-t", "30", run["name"]], d / "control.log", STOP_TIMEOUT_S)
                return FAILED, f"task {task}: maxRunDuration {limit:g}s exceeded"
            (d / "exit").write_text(str(rc))
            if rc != 0:
                return FAILED, f"task {task}: {run['name']} exited {rc}"
        return SUCCEEDED, "exit 0"


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


def main(argv: list[str]) -> int:
    """`python -m dt_cloud.job_backend supervise ROOT JOB_ID`: a local job's supervisor (`Local.supervise`), as a
    submit spawns it. Its exit code is 0 when the job succeeded."""
    if len(argv) != 3 or argv[0] != "supervise":
        print("usage: python -m dt_cloud.job_backend supervise ROOT JOB_ID", file=sys.stderr)
        return 2
    return 0 if Local(argv[1]).supervise(argv[2]).state == SUCCEEDED else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
