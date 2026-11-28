"""`job_backend`: one interface over GCP Batch and this host (specs/vm-pilot.md "JobBackend (step 1)"). `Local` runs
against a fake host (no container engine in CI); `GcpBatch` against stubs of the seams it wraps."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from dt_cloud import append_runner as ar
from dt_cloud import job_backend as jb
from dt_cloud.job_backend import CANCELLED, FAILED, QUEUED, RUNNING, SUCCEEDED, JobStatus, Plan, Run

LABELS = {"app": "disky", "deployment": "t1", "component": "c"}
SA = "job@p.iam.gserviceaccount.com"


def spec(deployment: str = "t1", count: int = 1, **task) -> dict:
    labels = {**LABELS, "deployment": deployment}
    return {
        "labels": labels,
        "allocationPolicy": {"labels": labels, "instances": [{"policy": {"machineType": "n2-highmem-8"}}],
                             "serviceAccount": {"email": SA}},
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


class FakeIdentity:
    """Mints `tok-<n>` per request (recording the account and lifetime) and answers `secrets[ref]` only to a token it
    minted (recording which)."""

    def __init__(self, secrets: dict[str, str] | None = None):
        self.secrets = secrets or {}
        self.minted: list[tuple[str, int]] = []
        self.read: list[tuple[str, str]] = []

    def access_token(self, service_account: str, lifetime_s: int) -> str:
        self.minted.append((service_account, lifetime_s))
        return f"tok-{len(self.minted)}"

    def secret(self, ref: str, token: str) -> str:
        assert token.startswith("tok-")
        self.read.append((ref, token))
        return self.secrets[ref]


class FakeProc:
    """A started child (`Host.start`): alive until `wait`ed or killed."""

    def __init__(self):
        self.rc: int | None = None

    def poll(self) -> int | None:
        return self.rc

    def wait(self, timeout: float | None = None) -> int:
        self.rc = 0 if self.rc is None else self.rc
        return self.rc

    def terminate(self) -> None:
        self.rc = -15

    def kill(self) -> None:
        self.rc = -9


class FakeHost:
    """Records what `Local` asks of the host. Each container run takes the next of `rcs` (default 0; None: timed out)
    and calls `during(argv)` first; `stop` runs answer 0. The engine's network commands succeed (a network's inspect
    names `gateway` / `subnet`: the loopback, so the job's metadata server is reachable from the test), except a `rm` of
    a network that isn't there; a started child (gcsfuse) mounts its last argument until `umount`ed; `cgroups` maps a
    container's name to its cgroup dir."""

    def __init__(self, rcs: list[int | None] | None = None):
        self.rcs = list(rcs or [])
        self.spawned: list[list[str]] = []
        self.ran: list[tuple[list[str], float | None]] = []
        self.envs: list[dict[str, str] | None] = []
        self.captured: list[list[str]] = []
        self.started: list[tuple[list[str], dict[str, str]]] = []
        self.mounted: set[str] = set()
        self.networks: set[str] = set()
        self.cgroups: dict[str, Path] = {}
        self.under: dict[str, list[str]] = {}
        self.during = lambda argv: None
        self.on_spawn = lambda argv: None
        self.dead: set[int] = set()
        self.boot = "boot-1"
        self.gateway, self.subnet = "127.0.0.1", "127.0.0.0/8"

    def spawn(self, argv: list[str], log: Path) -> None:
        self.spawned.append(argv)
        self.on_spawn(argv)

    def run(self, argv: list[str], log: Path, timeout: float | None, env: dict[str, str] | None = None) -> int | None:
        self.ran.append((argv, timeout))
        self.envs.append(env)
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

    def capture(self, argv: list[str], env: dict[str, str] | None = None) -> str:
        self.captured.append(argv)
        if argv[1:3] == ["network", "create"]:
            self.networks.add(argv[-1])
        elif argv[1:3] == ["network", "rm"]:
            if argv[-1] not in self.networks:
                raise subprocess.CalledProcessError(1, argv, stderr=f"network {argv[-1]} not found")
            self.networks.discard(argv[-1])
        elif argv[1:3] == ["network", "inspect"]:
            return json.dumps([{"Name": argv[-1], "IPAM": {"Config": [{"Subnet": self.subnet, "Gateway": self.gateway}]}}])
        elif argv[0] == "umount":
            self.mounted.discard(argv[-1])
        return ""

    def start(self, argv: list[str], log: Path, env: dict[str, str]) -> FakeProc:
        self.started.append((argv, dict(env)))
        self.mounted.add(argv[-1])
        return FakeProc()

    def is_mount(self, path: Path) -> bool:
        return str(path) in self.mounted

    def mounts_under(self, path: Path) -> list[str]:
        return self.under.get(str(path), [])

    def cgroup(self, engine: str, container: str) -> Path | None:
        return self.cgroups.get(container)


def local(tmp_path: Path, host: FakeHost, clock: Clock | None = None, identity: FakeIdentity | None = None, **kw) -> jb.Local:
    return jb.Local(tmp_path / "jobs", work=tmp_path / "work", host=host, clock=clock or Clock(), sleep=lambda s: None,
                    spawn="direct", identity=identity or FakeIdentity(), **kw)


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
    plan = jb.container_runs(spec(count=2, **task), "j1", engine="podman", slice_="scan.slice", disks=Path("/w/j1"),
                             kept=Path("/w/_disks"), gcs_root=Path("/jobs/j1/gcs"))

    def first(t: int) -> Run:
        return Run(t, f"disky-j1-{t}-0", [
            "podman", "run", "--rm", "--name", f"disky-j1-{t}-0", "--cgroup-parent=scan.slice", "--network", "disky-j1",
            "-e", "A=1", "-e", "B=runnable", "-e", "BATCH_TASK_COUNT=2", "-e", f"BATCH_TASK_INDEX={t}",
            *META_E, "-e", "GOOGLE_CLOUD_PROJECT=p",
            "-v", "/w/j1/stage:/stage:rw", "-v", "/w/j1/stage/sub:/sub",
            "--entrypoint", "bash", "img@sha256:ab", "-c", "run it",
        ])

    def second(t: int) -> Run:
        return Run(t, f"disky-j1-{t}-1", [
            "podman", "run", "--rm", "--name", f"disky-j1-{t}-1", "--cgroup-parent=scan.slice", "--network", "disky-j1",
            "-e", "A=1", "-e", "B=task", "-e", "BATCH_TASK_COUNT=2", "-e", f"BATCH_TASK_INDEX={t}",
            *META_E, "-e", "GOOGLE_CLOUD_PROJECT=p", "img2", "x",
        ])

    assert plan == Plan([first(0), second(0), first(1), second(1)], [Path("/w/j1/stage")], 60.0, SA)


#: The metadata server's address, handed to every container by name (its value from the engine's environment).
META_E = ["-e", "GCE_METADATA_HOST", "-e", "GCE_METADATA_IP", "-e", "GCE_METADATA_ROOT"]
GCS_VOLUME = {"gcs": {"remotePath": "b/pre"}, "mountPath": "/mnt/disks/b", "mountOptions": ["-o rw", "-implicit-dirs"]}
STAGE = {"deviceName": "stage", "mountPath": "/mnt/disks/stage"}
NF = {"deviceName": "nf", "mountPath": "/mnt/disks/nf"}


def with_existing(s: dict, device: str, disk: str) -> dict:
    s["allocationPolicy"]["instances"][0]["policy"]["disks"] = [{"deviceName": device, "existingDisk": disk}]
    return s


def test_container_runs_mounts_gcs_volumes_and_keeps_existing_disks():
    """A gcsfuse volume: a host mount point under the job's state (its options as gcsfuse argv); an existing disk: a dir
    under `kept` that outlives the job; a new disk: the job's own. Each container volume from its innermost one."""
    task = {"runnables": [{"container": {"imageUri": "i", "volumes": [
                "/mnt/disks/b:/mnt/disks/b:rw", "/mnt/disks/nf:/mnt/disks/nf", "/mnt/disks/stage:/stage"]}}],
            "volumes": [GCS_VOLUME, NF, STAGE]}
    s = with_existing(spec(**task), "nf", "projects/p/zones/z/disks/t1-nf")
    plan = jb.container_runs(s, "j1", engine="docker", slice_="scan.slice", disks=Path("/w/j1"), kept=Path("/w/_disks"),
                             gcs_root=Path("/jobs/j1/gcs"))
    assert (plan.dirs, plan.keep, plan.gcs) == (
        [Path("/w/j1/stage")], [Path("/w/_disks/t1-nf")],
        [jb.GcsMount("b", Path("/jobs/j1/gcs/0"), "pre", ("-o", "rw", "--implicit-dirs"))])
    assert plan.runs[0].argv[-7:] == [
        "-v", "/jobs/j1/gcs/0:/mnt/disks/b:rw", "-v", "/w/_disks/t1-nf:/mnt/disks/nf", "-v", "/w/j1/stage:/stage", "i"]


@pytest.mark.parametrize("s, message", [
    (spec(volumes=[{"nfs": {"server": "s", "remotePath": "/x"}, "mountPath": "/mnt/nfs"}, STAGE]),
     "local job j1: volume at /mnt/nfs: only disk (deviceName) and gcsfuse (gcs) volumes run locally"),
    ({**spec(), "allocationPolicy": {"labels": LABELS}},
     "local job j1: no allocationPolicy.serviceAccount.email: a local job runs as its spec's service account, never as "
     "the host's"),
    (spec(environment={"secretVariables": {"R2_KEY": "k"}}),
     "local job j1: taskSpec.environment.secretVariables (R2_KEY): not a projects/…/secrets/…/versions/… name"),
    (spec(runnables=[{"container": {"imageUri": "i"}, "environment": {"encryptedVariables": {"X": "e"}}}]),
     "local job j1: runnables[0].environment.encryptedVariables (X): not supported locally; use secretVariables"),
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
        "systemd-run", "--quiet", "--collect", "--unit=disky-job-j1", "--slice=scan.slice", "--property=KillMode=mixed",
        "--property=TimeoutStopSec=180", "--", sys.executable, "-m", "dt_cloud.job_backend", "supervise", root, "j1"]


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


R2_KEY = "projects/p/secrets/r2-key/versions/latest"
SLACK = "projects/p/secrets/slack/versions/3"


def test_a_job_runs_as_its_service_account_and_its_secrets_resolve_through_it(tmp_path):
    """The job's metadata server mints its account's token (reused while it is fresh); each run's secret variables are
    read with it. Secrets reach the engine's environment only (`-e NAME` in argv), the token only the engine's (its
    registry helper), never a container's; no file under the jobs or work dirs holds either."""
    identity = FakeIdentity({R2_KEY: "r2-secret-value", SLACK: "slack-secret-value"})
    host = FakeHost()
    jobs = local(tmp_path, host, identity=identity, token_lifetime_s=1800)
    task = {"environment": {"variables": {"A": "1", "GCE_METADATA_HOST": "elsewhere:80"}, "secretVariables": {"R2_KEY": R2_KEY}},
            "runnables": [{"container": {"imageUri": "img", "commands": ["x"]},
                           "environment": {"secretVariables": {"SLACK": SLACK}}}],
            "volumes": [STAGE]}
    jobs.submit(spec(count=2, **task), "j1")
    assert jobs.supervise("j1") == JobStatus(SUCCEEDED, "exit 0")
    assert identity.minted == [(SA, 1800)]
    assert identity.read == [(R2_KEY, "tok-1"), (SLACK, "tok-1")] * 2
    meta = {k: "127.0.0.1:<port>" for k in ("GCE_METADATA_HOST", "GCE_METADATA_IP", "GCE_METADATA_ROOT")}
    assert [{k: "127.0.0.1:<port>" if k in meta else v for k, v in e.items()} for e in host.envs] == [
        {"R2_KEY": "r2-secret-value", "SLACK": "slack-secret-value", "GOOGLE_OAUTH_ACCESS_TOKEN": "tok-1", **meta}] * 2
    assert [argv for argv, _ in host.ran][0] == [
        "docker", "run", "--rm", "--name", "disky-j1-0-0", "--cgroup-parent=scan.slice", "--network", "disky-j1",
        "-e", "A=1", "-e", "BATCH_TASK_COUNT=2", "-e", "BATCH_TASK_INDEX=0", *META_E, "-e", "GOOGLE_CLOUD_PROJECT=p",
        "-e", "R2_KEY", "-e", "SLACK", "img", "x"]
    files = sorted(f for f in tmp_path.rglob("*") if f.is_file())
    assert [str(f.relative_to(tmp_path / "jobs")) for f in files] == [
        ".locks/t1.lock", "j1/current", "j1/exit", "j1/job.json", "j1/log", "j1/spec.json", "j1/status.json",
        "j1/supervisor.json", "j1/usage.jsonl"]
    # A secondary check over every byte written: neither a token nor a secret value.
    assert [(str(f), v) for f in files for v in ("tok-1", "r2-secret-value", "slack-secret-value")
            if v.encode() in f.read_bytes()] == []
    job = json.loads((tmp_path / "jobs/j1/job.json").read_text())
    assert (job["service_account"], job["token_lifetime_s"], [r["secrets"] for r in job["runs"]]) == (
        SA, 1800, [{"R2_KEY": R2_KEY, "SLACK": SLACK}] * 2)


def test_a_token_that_cannot_be_minted_fails_the_job_before_its_container(tmp_path):
    class Refused(FakeIdentity):
        def access_token(self, service_account: str, lifetime_s: int) -> str:
            raise PermissionError(f"403 on {service_account}")

    host = FakeHost()
    jobs = local(tmp_path, host, identity=Refused())
    jobs.submit(spec(), "j1")
    with pytest.raises(PermissionError):
        jobs.supervise("j1")
    assert (jobs.status("j1"), host.ran, host.networks) == (
        JobStatus(FAILED, "supervisor failed: PermissionError: 403 on job@p.iam.gserviceaccount.com"), [], set())


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
        ["docker", "run", "--rm", "--name", "disky-j1-0-0", "--cgroup-parent=scan.slice", "--network", "disky-j1",
         "-e", "BATCH_TASK_COUNT=2", "-e", "BATCH_TASK_INDEX=0", *META_E, "-e", "GOOGLE_CLOUD_PROJECT=p",
         "--entrypoint", "bash", "img", "-c", "true"],
        ["docker", "stop", "-t", "60", "disky-j1-0-0"],
    ]
    jobs.cancel("j1")  # ended: left as it is
    assert jobs.status("j1") == JobStatus(CANCELLED, "cancelled: task 0: disky-j1-0-0 exited 137")


def test_a_task_past_its_max_run_duration_is_stopped(tmp_path):
    host = FakeHost([None])
    jobs = local(tmp_path, host)
    jobs.submit(spec(maxRunDuration="60s"), "j1")
    assert jobs.supervise("j1") == JobStatus(FAILED, "task 0: maxRunDuration 60s exceeded")
    assert [(argv[1:], t) for argv, t in host.ran][1:] == [(["stop", "-t", "60", "disky-j1-0-0"], jb.STOP_TIMEOUT_S)]


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
    ar.BatchRunner(ar.Profile(region="r", project="p"), logs.append, delay=0, max_delay=0, backend=jobs, clock=lambda: 0.0)("j1", spec())
    assert logs == ["submitted j1", "j1 SUCCEEDED after 0s (exit 0)"]
    host.rcs = [1]
    with pytest.raises(RuntimeError) as e:
        ar.BatchRunner(ar.Profile(region="r", project="p"), logs.append, delay=0, max_delay=0, backend=jobs)("j2", spec())
    assert str(e.value) == "local job j2: FAILED"


# ── a job's setup: its network, metadata server, mounts, disks; its usage ──


def fetch(address: str, path: str, flavor: bool = True) -> tuple[int, str]:
    req = urllib.request.Request(f"http://{address}{path}", headers={"Metadata-Flavor": "Google"} if flavor else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_a_job_has_its_own_network_and_metadata_server_for_its_runs_only(tmp_path):
    """The supervisor creates the job's network (removing a leftover of that name first), serves the job's account's
    token on its gateway while the containers run, and takes both down after."""
    host = FakeHost()
    host.networks.add("disky-j1")  # a lost supervisor's
    jobs = local(tmp_path, host)
    jobs.submit(spec(), "j1")
    seen = []

    def during(argv: list[str]) -> None:
        address = host.envs[-1]["GCE_METADATA_HOST"]
        seen.append((json.loads(fetch(address, "/computeMetadata/v1/instance/service-accounts/default/token")[1]),
                     fetch(address, "/computeMetadata/v1/project/project-id"), sorted(host.networks)))

    host.during = during
    assert jobs.supervise("j1") == JobStatus(SUCCEEDED, "exit 0")
    assert seen == [({"access_token": "tok-1", "expires_in": 3600, "token_type": "Bearer"}, (200, "p"), ["disky-j1"])]
    assert host.captured == [
        ["docker", "network", "rm", "disky-j1"],
        ["docker", "network", "create", "--label", "disky.job=j1", "disky-j1"],
        ["docker", "network", "inspect", "disky-j1"],
        ["docker", "network", "rm", "disky-j1"],
    ]
    with pytest.raises(OSError):  # stopped with the job
        urllib.request.urlopen(f"http://{host.envs[-1]['GCE_METADATA_HOST']}/", timeout=5)


def test_gcs_volumes_mount_as_the_job_through_its_metadata_server_and_go_after(tmp_path):
    host = FakeHost()
    jobs = local(tmp_path, host)
    task = {"runnables": [{"container": {"imageUri": "i", "volumes": ["/mnt/disks/b:/mnt/disks/b:rw"]}}], "volumes": [GCS_VOLUME]}
    jobs.submit(spec(**task), "j1")
    point = str(tmp_path / "jobs/j1/gcs/0")
    seen = []
    host.during = lambda argv: seen.append((argv[argv.index("-v") + 1], sorted(host.mounted)))
    assert jobs.supervise("j1") == JobStatus(SUCCEEDED, "exit 0")
    assert seen == [(f"{point}:/mnt/disks/b:rw", [point])]
    [(argv, env)] = host.started
    address = host.envs[0]["GCE_METADATA_HOST"]
    assert (argv, env) == (
        ["gcsfuse", "--foreground", "-o", "rw", "--implicit-dirs", "--only-dir", "pre", "b", point],
        {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(tmp_path / "jobs/j1"),
         "GCE_METADATA_HOST": address, "GCE_METADATA_IP": address, "GCE_METADATA_ROOT": address})
    net = [["docker", "network", "rm", "disky-j1"], ["docker", "network", "create", "--label", "disky.job=j1", "disky-j1"],
           ["docker", "network", "inspect", "disky-j1"]]
    assert (host.captured, host.mounted, (tmp_path / "jobs/j1/gcs/0").exists()) == (
        [*net, ["umount", point], ["docker", "network", "rm", "disky-j1"]], set(), False)


def test_an_existing_disk_outlives_the_job_and_a_new_one_does_not(tmp_path):
    host = FakeHost()
    jobs = local(tmp_path, host)
    task = {"runnables": [{"container": {"imageUri": "i", "volumes": ["/mnt/disks/nf:/nf", "/mnt/disks/stage:/stage"]}}],
            "volumes": [NF, STAGE]}
    jobs.submit(with_existing(spec(**task), "nf", "t1-nf"), "j1")
    kept, new = tmp_path / "work/_disks/t1-nf", tmp_path / "work/j1/stage"
    host.during = lambda argv: (kept / "trace.tsv").write_text("kept\n")
    assert jobs.supervise("j1") == JobStatus(SUCCEEDED, "exit 0")
    assert ((kept / "trace.tsv").read_text(), new.exists()) == ("kept\n", False)


def test_a_dir_with_a_mount_under_it_is_never_removed(tmp_path):
    host = FakeHost()
    jobs = local(tmp_path, host)
    jobs.submit(spec(volumes=[STAGE]), "j1")
    stage = tmp_path / "work/j1/stage"
    host.under[str(stage)] = [f"{stage}/bucket"]
    assert (jobs.supervise("j1"), stage.is_dir()) == (JobStatus(SUCCEEDED, "exit 0"), True)


def write_cgroup(d: Path, current: int, anon: int, file: int, peak: int, oom_kill: int = 0, usec: int = 0) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "memory.current").write_text(f"{current}\n")
    (d / "memory.stat").write_text(f"anon {anon}\nfile {file}\nkernel 1\n")
    (d / "memory.peak").write_text(f"{peak}\n")
    (d / "memory.events").write_text(f"low 0\nhigh 0\nmax 3\noom 0\noom_kill {oom_kill}\n")
    (d / "cpu.stat").write_text(f"usage_usec {usec}\nuser_usec 1\n")
    (d.parent / "memory.current").write_text(f"{current + 100}\n")


def test_each_run_records_its_wall_time_and_cgroup_usage(tmp_path):
    """The run's cgroup is read until it goes (here: once, at the run's end), its summary appended to `usage.jsonl` and
    each reading to `usage.tsv`."""
    clock = Clock()
    host = FakeHost([0, 3])
    jobs = local(tmp_path, host, clock)
    jobs.submit(spec(count=2), "j1")
    cg = tmp_path / "cg/scan.slice/docker-x.scope"
    host.cgroups["disky-j1-0-0"] = cg
    host.during = lambda argv: (write_cgroup(cg, 500, 300, 150, 900, 1, 2_500_000), setattr(clock, "t", clock.t + 42))
    assert jobs.supervise("j1") == JobStatus(FAILED, "task 1: disky-j1-1-0 exited 3")
    runs = [json.loads(line) for line in (tmp_path / "jobs/j1/usage.jsonl").read_text().splitlines()]
    assert runs == [
        {"run": "disky-j1-0-0", "task": 0, "started": 1000.0, "ended": 1042.0, "wall_s": 42.0, "exit": 0, "cgroup": str(cg),
         "samples": 1, "memory_peak": 900, "max_current": 500, "max_anon": 300, "max_file": 150, "max_slice_current": 600,
         "oom_kill": 1, "cpu_s": 2.5},
        {"run": "disky-j1-1-0", "task": 1, "started": 1042.0, "ended": 1084.0, "wall_s": 42.0, "exit": 3, "cgroup": None,
         "samples": 0, "memory_peak": None, "max_current": None, "max_anon": None, "max_file": None,
         "max_slice_current": None, "oom_kill": 0, "cpu_s": None},
    ]
    assert (tmp_path / "jobs/j1/usage.tsv").read_text().splitlines() == [
        "t\trun\tcurrent\tanon\tfile\tpeak\tslice_current", "1042.0\tdisky-j1-0-0\t500\t300\t150\t900\t600"]


def test_the_local_job_cli_reads_and_cancels(tmp_path, monkeypatch):
    from click.testing import CliRunner

    host = FakeHost()
    jobs = local(tmp_path, host)
    jobs.submit(spec(), "j1")
    jobs.supervise("j1")
    jobs.submit(spec(), "j2")
    monkeypatch.setenv("DISKY_LOCAL_JOBS", str(tmp_path / "jobs"))
    monkeypatch.setattr(jb.Local, "from_env", classmethod(lambda cls, environ=None: jobs))
    run = CliRunner().invoke
    assert run(jb.cli, ["status"]).output.splitlines() == ["j2\tQUEUED\t", "j1\tSUCCEEDED\texit 0"]
    assert run(jb.cli, ["status", "j1"]).output.splitlines() == [
        "j1\tSUCCEEDED\texit 0", "  disky-j1-0-0: exit 0, 0s, memory.peak - GiB, max anon - GiB, oom_kill 0"]
    assert run(jb.cli, ["logs", "-n", "1", "j1"]).output == "disky-j1-0-0 out\n"
    assert run(jb.cli, ["cancel", "j2"]).output == "j2\tCANCELLED\tcancelled while queued\n"


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
    monkeypatch.delenv(gcp.TASK_LOG_VIEW_ENV, raising=False)
    b = jb.GcpBatch("us-east1", "p")
    assert b.submit({"s": 1}, "j1", buckets=["bk"], exists_ok=True, retry_for=300) == "j1"
    assert b.status("j1") == JobStatus("RUNNING", "PENDING=1 RUNNING=2")
    assert b.logs("j1", limit=2) == ["one", "two"]
    b.cancel("j1")
    assert calls == [
        ("submit", ({"s": 1}, "j1", "us-east1", "p"), {"buckets": ["bk"], "cross_region": None, "exists_ok": True, "retry_for": 300}),
        ("get", ("j1",), {"project": "p", "region": "us-east1"}),
        ("get", ("j1",), {"project": "p", "region": "us-east1"}),
        ("logs", ("uid=u1",), {"project": "p", "limit": 2, "asc": False, "resource": "projects/p"}),
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
