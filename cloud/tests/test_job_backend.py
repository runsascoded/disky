"""`job_backend`: one interface over GCP Batch and this host (specs/vm-pilot.md "JobBackend (step 1)"). `Local` runs
against a fake host (no container engine in CI); `GcpBatch` against stubs of the seams it wraps."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from dt_cloud import append_runner as ar
from dt_cloud import job_backend as jb
from dt_cloud.job_backend import CANCELLED, FAILED, QUEUED, RUNNING, SUCCEEDED, JobStatus, Plan, Run

LABELS = {"app": "disky", "deployment": "t1", "component": "c"}


def spec(deployment: str = "t1", count: int = 1, **task) -> dict:
    labels = {**LABELS, "deployment": deployment}
    return {
        "labels": labels,
        "allocationPolicy": {"labels": labels, "instances": [{"policy": {"machineType": "n2-highmem-8"}}]},
        "taskGroups": [{"taskCount": count, "taskSpec": {
            "runnables": [{"container": {"imageUri": "img", "entrypoint": "bash", "commands": ["-c", "true"]}}],
            **task,
        }}],
    }


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeHost:
    """Records what `Local` asks of the host. Each container run takes the next of `rcs` (default 0; None: timed out)
    and calls `during(argv)` first; `stop` runs answer 0."""

    def __init__(self, rcs: list[int | None] | None = None):
        self.rcs = list(rcs or [])
        self.spawned: list[list[str]] = []
        self.ran: list[tuple[list[str], float | None]] = []
        self.during = lambda argv: None
        self.on_spawn = lambda argv: None
        self.dead: set[int] = set()
        self.boot = "boot-1"

    def spawn(self, argv: list[str], log: Path) -> None:
        self.spawned.append(argv)
        self.on_spawn(argv)

    def run(self, argv: list[str], log: Path, timeout: float | None) -> int | None:
        self.ran.append((argv, timeout))
        if argv[1] == "stop":
            return 0
        self.during(argv)
        rc = self.rcs.pop(0) if self.rcs else 0
        with open(log, "a") as f:
            f.write(f"{argv[4]} out\n")
        return rc

    def alive(self, pid: int) -> bool:
        return pid not in self.dead

    def boot_id(self) -> str:
        return self.boot


def local(tmp_path: Path, host: FakeHost, clock: Clock | None = None, **kw) -> jb.Local:
    return jb.Local(tmp_path / "jobs", work=tmp_path / "work", host=host, clock=clock or Clock(), sleep=lambda s: None,
                    spawn="direct", **kw)


# ── the spec → command translation ─────────────────────────────────────────


def test_container_runs_translates_a_batch_spec_to_engine_runs():
    task = {
        "runnables": [
            {"container": {"imageUri": "img@sha256:ab", "entrypoint": "bash", "commands": ["-c", "run it"],
                           "volumes": ["/mnt/disks/stage:/stage:rw", "/mnt/disks/stage/sub:/sub"]},
             "environment": {"variables": {"B": "runnable"}}},
            {"container": {"imageUri": "img2", "commands": ["x"]}},
        ],
        "environment": {"variables": {"B": "task", "A": "1"}},
        "volumes": [{"deviceName": "stage", "mountPath": "/mnt/disks/stage/"}],
        "computeResource": {"cpuMilli": 8000, "memoryMib": 60000},
        "maxRetryCount": 2,
        "maxRunDuration": "60s",
    }
    plan = jb.container_runs(spec(count=2, **task), "j1", engine="podman", slice_="scan.slice", disks=Path("/w/j1"))

    def first(t: int) -> Run:
        return Run(t, f"disky-j1-{t}-0", [
            "podman", "run", "--rm", "--name", f"disky-j1-{t}-0", "--cgroup-parent=scan.slice",
            "-e", "A=1", "-e", "B=runnable", "-e", "BATCH_TASK_COUNT=2", "-e", f"BATCH_TASK_INDEX={t}",
            "-v", "/w/j1/stage:/stage:rw", "-v", "/w/j1/stage/sub:/sub",
            "--entrypoint", "bash", "img@sha256:ab", "-c", "run it",
        ])

    def second(t: int) -> Run:
        return Run(t, f"disky-j1-{t}-1", [
            "podman", "run", "--rm", "--name", f"disky-j1-{t}-1", "--cgroup-parent=scan.slice",
            "-e", "A=1", "-e", "B=task", "-e", "BATCH_TASK_COUNT=2", "-e", f"BATCH_TASK_INDEX={t}", "img2", "x",
        ])

    assert plan == Plan([first(0), second(0), first(1), second(1)], [Path("/w/j1/stage")], 60.0)


GCS_VOLUME = {"gcs": {"remotePath": "b"}, "mountPath": "/mnt/disks/gcs/b", "mountOptions": ["--implicit-dirs"]}
STAGE = {"deviceName": "stage", "mountPath": "/mnt/disks/stage"}


@pytest.mark.parametrize("s, message", [
    (spec(volumes=[GCS_VOLUME, STAGE]),
     "local job j1: gcsfuse volume 'b' at /mnt/disks/gcs/b: the local backend mounts no buckets yet; read it through the "
     "API, or run this job on Batch"),
    (spec(environment={"variables": {"A": "1"}, "secretVariables": {"R2_KEY": "projects/p/secrets/k/versions/latest"}}),
     "local job j1: taskSpec.environment.secretVariables (R2_KEY): the local backend doesn't resolve secrets yet; run this "
     "job on Batch"),
    (spec(runnables=[{"container": {"imageUri": "i"}, "environment": {"encryptedVariables": {"X": "e"}}}]),
     "local job j1: runnables[0].environment.encryptedVariables (X): the local backend doesn't resolve secrets yet; run "
     "this job on Batch"),
    (spec(volumes=[STAGE], runnables=[{"container": {"imageUri": "i", "volumes": ["/var/run/x:/x"]}}]),
     "local job j1: container volume host path /var/run/x: not under a task volume (/mnt/disks/stage)"),
    (spec(runnables=[{"script": {"text": "echo"}}]), "local job j1: runnable 0 isn't a container (script)"),
    (spec(runnables=[{"container": {"imageUri": "i"}, "background": True}]),
     "local job j1: runnable 0: background not supported locally"),
    (spec(runnables=[{"container": {"imageUri": "i", "options": "--privileged"}}]),
     "local job j1: runnable 0: container options not supported locally"),
    ({**spec(), "taskGroups": spec()["taskGroups"] * 2}, "local job j1: one task group, not 2"),
])
def test_local_refuses_what_it_does_not_run_before_writing_anything(tmp_path, s, message):
    host = FakeHost()
    jobs = local(tmp_path, host)
    with pytest.raises(jb.Unsupported) as e:
        jobs.submit(s, "j1")
    assert (str(e.value), (tmp_path / "jobs" / "j1").exists(), host.spawned) == (message, False, [])


# ── a local job's life ─────────────────────────────────────────────────────


def test_submit_queues_and_spawns_its_supervisor(tmp_path):
    host = FakeHost()
    jobs = local(tmp_path, host)
    assert jobs.submit(spec(), "j1") == "j1"
    root = str(tmp_path / "jobs")
    assert (jobs.status("j1"), host.spawned) == (JobStatus(QUEUED), [[sys.executable, "-m", "dt_cloud.job_backend", "supervise", root, "j1"]])
    assert sorted(p.name for p in (tmp_path / "jobs" / "j1").iterdir()) == ["job.json", "spec.json", "status.json", "supervisor.json"]
    assert jb.Local(root, spawn="systemd-run").supervisor_argv("j1") == [
        "systemd-run", "--quiet", "--collect", "--unit=disky-job-j1", "--slice=scan.slice", "--",
        sys.executable, "-m", "dt_cloud.job_backend", "supervise", root, "j1"]


@pytest.mark.parametrize("rcs, final", [
    ([0, 0], JobStatus(SUCCEEDED, "exit 0")),
    ([3], JobStatus(FAILED, "task 0: disky-j1-0-0 exited 3")),
    ([0, 9], JobStatus(FAILED, "task 1: disky-j1-1-0 exited 9")),
])
def test_supervise_runs_each_task_and_records_the_end(tmp_path, rcs, final):
    host = FakeHost(rcs)
    jobs = local(tmp_path, host)
    jobs.submit(spec(count=2, volumes=[STAGE], maxRunDuration="600s"), "j1")
    seen = []
    host.during = lambda argv: seen.append((argv[4], jobs.status("j1"), (tmp_path / "work" / "j1" / "stage").is_dir()))
    assert jobs.supervise("j1") == final
    ran = ["disky-j1-0-0", "disky-j1-1-0"][:len(rcs)]
    assert seen == [(n, JobStatus(RUNNING), True) for n in ran]
    assert [(argv[4], timeout) for argv, timeout in host.ran] == [(n, 600.0) for n in ran]
    assert (jobs.status("j1"), jobs.logs("j1"), (tmp_path / "work" / "j1" / "stage").exists()) == (final, [f"{n} out" for n in ran], False)
    assert jobs.logs("j1", limit=1) == [f"{ran[-1]} out"]


def test_one_job_at_a_time_per_deployment(tmp_path):
    """A tenant's second job waits (QUEUED) while its first runs, and starts in submit order; another tenant's runs
    beside it."""
    host = FakeHost()
    jobs = local(tmp_path, host)
    for jid, dep in (("a", "t1"), ("b", "t1"), ("c", "t2")):
        jobs.submit(spec(dep), jid)
    assert jobs.try_start("b") is False  # `a` is older and still queued
    seen = []
    host.during = lambda argv: seen.append((jobs.status("b"), jobs.try_start("b"), jobs.try_start("c"), jobs.status("c")))
    assert jobs.supervise("a") == JobStatus(SUCCEEDED, "exit 0")
    assert seen == [(JobStatus(QUEUED), False, True, JobStatus(RUNNING))]
    assert (jobs.try_start("b"), jobs.status("b")) == (True, JobStatus(RUNNING))


def test_resubmitting_an_id_is_idempotent_only_with_exists_ok(tmp_path):
    host = FakeHost()
    jobs = local(tmp_path, host)
    jobs.submit(spec(), "j1")
    assert jobs.submit(spec("t2"), "j1", exists_ok=True) == "j1"
    with pytest.raises(jb.JobExists) as e:
        jobs.submit(spec(), "j1")
    assert (str(e.value), len(host.spawned)) == (f"local job j1 already exists under {tmp_path / 'jobs'}", 1)
    assert jobs.status("j1") == JobStatus(QUEUED)
    with pytest.raises(jb.NoSuchJob):
        jobs.status("nope")


def test_cancel_a_queued_job(tmp_path):
    host = FakeHost()
    jobs = local(tmp_path, host)
    jobs.submit(spec(), "j1")
    jobs.cancel("j1")
    cancelled = JobStatus(CANCELLED, "cancelled while queued")
    assert (jobs.status("j1"), jobs.supervise("j1"), host.ran) == (cancelled, cancelled, [])


def test_cancel_a_running_job_stops_its_container_and_runs_nothing_more(tmp_path):
    host = FakeHost([137])
    jobs = local(tmp_path, host)
    jobs.submit(spec(count=2), "j1")
    host.during = lambda argv: jobs.cancel("j1")
    assert jobs.supervise("j1") == JobStatus(CANCELLED, "cancelled: task 0: disky-j1-0-0 exited 137")
    assert [argv for argv, _ in host.ran] == [
        ["docker", "run", "--rm", "--name", "disky-j1-0-0", "--cgroup-parent=scan.slice",
         "-e", "BATCH_TASK_COUNT=2", "-e", "BATCH_TASK_INDEX=0", "--entrypoint", "bash", "img", "-c", "true"],
        ["docker", "stop", "-t", "30", "disky-j1-0-0"],
    ]
    jobs.cancel("j1")  # ended: left as it is
    assert jobs.status("j1") == JobStatus(CANCELLED, "cancelled: task 0: disky-j1-0-0 exited 137")


def test_a_task_past_its_max_run_duration_is_stopped(tmp_path):
    host = FakeHost([None])
    jobs = local(tmp_path, host)
    jobs.submit(spec(maxRunDuration="60s"), "j1")
    assert jobs.supervise("j1") == JobStatus(FAILED, "task 0: maxRunDuration 60s exceeded")
    assert [(argv[1:], t) for argv, t in host.ran][1:] == [(["stop", "-t", "30", "disky-j1-0-0"], jb.STOP_TIMEOUT_S)]


def test_a_lost_supervisor_fails_its_job(tmp_path):
    """Never started within the grace, gone (its pid), or from before a reboot (another boot id): FAILED."""
    clock = Clock()
    host = FakeHost()
    jobs = local(tmp_path, host, clock)
    jobs.submit(spec(), "j1")
    clock.t += jb.START_GRACE_S - 1
    assert jobs.status("j1") == JobStatus(QUEUED)
    clock.t += 1
    assert jobs.status("j1") == JobStatus(FAILED, "supervisor lost while queued")
    jobs.submit(spec(), "j2")
    seen = []
    host.during = lambda argv: (host.dead.add(os.getpid()), seen.append(jobs.status("j2")), host.dead.clear())
    jobs.supervise("j2")
    assert seen == [JobStatus(FAILED, "supervisor lost while running")]
    jobs.submit(spec(), "j3")
    host.boot = "boot-2"
    assert jobs.status("j3") == JobStatus(FAILED, "supervisor lost while queued")


def test_batch_runner_polls_a_local_job(tmp_path):
    """`BatchRunner` over the local backend: the same submit-then-poll, its states Batch's."""
    host = FakeHost()
    jobs = local(tmp_path, host)
    host.on_spawn = lambda argv: jobs.supervise(argv[-1])
    logs: list[str] = []
    ar.BatchRunner(ar.Profile(region="r", project="p"), logs.append, delay=0, max_delay=0, backend=jobs)("j1", spec())
    assert logs == ["submitted j1", "j1 SUCCEEDED exit 0"]
    host.rcs = [1]
    with pytest.raises(RuntimeError) as e:
        ar.BatchRunner(ar.Profile(region="r", project="p"), logs.append, delay=0, max_delay=0, backend=jobs)("j2", spec())
    assert str(e.value) == "local job j2: FAILED"


# ── GcpBatch: today's seams ────────────────────────────────────────────────


def test_gcp_batch_wraps_the_existing_seams(monkeypatch):
    from dt_cloud import batch, gcp

    calls = []
    monkeypatch.setattr(batch, "submit_job", lambda *a, **kw: calls.append(("submit", a, kw)) or a[1])
    monkeypatch.setattr(batch, "cancel_job", lambda *a, **kw: calls.append(("cancel", a, kw)))
    job = {"uid": "u1", "status": {"state": "RUNNING", "taskGroups": {"g": {"counts": {"RUNNING": 2, "PENDING": 1}}}}}
    monkeypatch.setattr(gcp, "batch_job", lambda *a, **kw: calls.append(("get", a, kw)) or job)
    monkeypatch.setattr(gcp, "task_log_filter", lambda uid: f"uid={uid}")
    entries = [{"textPayload": "two\n"}, {"jsonPayload": {"message": "one"}}]
    monkeypatch.setattr(gcp, "log_entries", lambda *a, **kw: calls.append(("logs", a, kw)) or entries)
    b = jb.GcpBatch("us-east1", "p")
    assert b.submit({"s": 1}, "j1", buckets=["bk"], exists_ok=True, retry_for=300) == "j1"
    assert b.status("j1") == JobStatus("RUNNING", "PENDING=1 RUNNING=2")
    assert b.logs("j1", limit=2) == ["one", "two"]
    b.cancel("j1")
    assert calls == [
        ("submit", ({"s": 1}, "j1", "us-east1", "p"), {"buckets": ["bk"], "cross_region": None, "exists_ok": True, "retry_for": 300}),
        ("get", ("j1",), {"project": "p", "region": "us-east1"}),
        ("get", ("j1",), {"project": "p", "region": "us-east1"}),
        ("logs", ("uid=u1",), {"project": "p", "limit": 2, "asc": False}),
        ("cancel", ("j1",), {"region": "us-east1", "project": "p"}),
    ]


def test_batch_cancel_job_deletes(monkeypatch):
    from dt_cloud import batch

    deleted = []

    class S:
        def delete(self, url):
            deleted.append(url)
            return type("R", (), {"raise_for_status": lambda self: None})()

    monkeypatch.setattr(batch, "session", lambda: S())
    batch.cancel_job("j1", region="us-east1", project="p")
    assert deleted == ["https://batch.googleapis.com/v1/projects/p/locations/us-east1/jobs/j1"]


# ── selection ──────────────────────────────────────────────────────────────


def test_resolve_names_the_deployments_backend(tmp_path, monkeypatch):
    b = jb.resolve("r", "p")
    assert (type(b), b.region, b.project) == (jb.GcpBatch, "r", "p")
    environ = {"DISKY_JOB_BACKEND": "local", "DISKY_LOCAL_JOBS": str(tmp_path), "DISKY_LOCAL_ENGINE": "podman",
               "DISKY_LOCAL_SPAWN": "direct"}
    loc = jb.resolve(environ=environ)
    assert (type(loc), loc.root, loc.work, loc.engine, loc.slice, loc.spawn) == (jb.Local, tmp_path, None, "podman", "scan.slice", "direct")
    with pytest.raises(SystemExit) as e:
        jb.resolve(environ={"DISKY_JOB_BACKEND": "local"})
    assert str(e.value) == "DISKY_LOCAL_JOBS is unset: export the dir the local job backend keeps its jobs' state in"
    with pytest.raises(ValueError) as e2:
        jb.resolve(environ={"DISKY_JOB_BACKEND": "k8s"})
    assert str(e2.value) == "$DISKY_JOB_BACKEND='k8s': one of batch, local"
    monkeypatch.setenv("DISKY_JOB_BACKEND", "local")
    monkeypatch.setenv("DISKY_LOCAL_JOBS", str(tmp_path))
    assert type(jb.resolve()) is jb.Local
