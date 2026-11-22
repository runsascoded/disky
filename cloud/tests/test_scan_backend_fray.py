"""`dt_cloud.scan_backend_fray` (specs/orchestrator-abstraction.md §4): the scan's DAG scheduled over a fray `Client`.
A fake client scripts each job's statuses; `test_local_client` runs real subprocesses through fray's `LocalClient`
where fray is installed."""
from __future__ import annotations

import datetime as dt
import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from dt_cloud.scan_backend import Run
from dt_cloud.scan_backend_fray import FrayBackend, job_name, machine_shape, task_script
from dt_cloud.scan_dag import build
from dt_cloud.scan_head import params, session_id
from dt_cloud.scan_profile import Disk, Machine, parse

SCAN = "2026-11-21T1200"


class Handle:
    def __init__(self, job_id: str, statuses: list[str]):
        self.job_id, self.statuses, self.terminated = job_id, statuses, False

    def status(self) -> str:
        if self.terminated:
            return "stopped"
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def terminate(self) -> None:
        self.terminated = True

    def logs(self, max_lines: int = 0) -> tuple[str, ...]:
        return (f"{self.job_id}: log line",)


class Client:
    """Each job's statuses by job name suffix (the node and tag): `succeeded` after one poll unless scripted."""
    def __init__(self, script: dict[str, list[str]] | None = None, on_submit=None):
        self.script, self.on_submit, self.submitted, self.handles = script or {}, on_submit, [], {}

    def submit(self, req) -> Handle:
        self.submitted.append(req)
        key = next((k for k in self.script if k in req.name), None)
        h = self.handles[req.name] = Handle(req.name, list(self.script.get(key, ["succeeded"])))
        if self.on_submit:
            self.on_submit(req)
        return h


def request(name, script, cpu, ram, disk, image, retries):
    return SimpleNamespace(name=name, script=script, cpu=cpu, ram=ram, disk=disk, image=image, retries=retries)


def profile(tmp_path: Path, **kw):
    return parse({"deployment": "fix", "executor": {"kind": "local"}, "work": str(tmp_path / "work"),
                  "data": str(tmp_path / "data"), "layout": {"layer2": "l2/{scan}/", "snapshots": ""},
                  "sources": ["s3://a", {"uri": "s3://b", "machine": "n2-highmem-4", "disk": {"request": 200}}],
                  "skip": ["static-names", "digest", "interval-store", "warm-cache", "probe"],
                  "options": {"list": {"import": True}}, "env": {"SITE_URL": "https://x"}, **kw})


def mkrun(tmp_path: Path, prof=None, **kw) -> Run:
    prof = prof or profile(tmp_path)
    env = {"DISKY_LABELS": "app=disky,deployment=fix"}
    return Run(prof, json.dumps({"deployment": "fix"}), build(prof), SCAN, "G1", "1", "run-1",
               session_id("fix", SCAN), tmp_path / "launch", env, dt.datetime(2026, 11, 21, 12, tzinfo=dt.timezone.utc), **kw)


def go(backend: FrayBackend, run: Run) -> int:
    backend.prepare(run)
    backend.start(run)
    rc = None
    while rc is None:
        rc = backend.wait(5)
    return rc


def submitted(client: Client) -> list[str]:
    return [r.name.removeprefix(f"disky-fix-{SCAN.lower()}-") for r in client.submitted]


def test_runs_the_dag_in_waves(tmp_path):
    client = Client()
    b = FrayBackend(client, request, poll=0)
    run = mkrun(tmp_path)
    assert go(b, run) == 0
    assert submitted(client) == ["list_source-s3-a", "list_source-s3-b", "index", "index_rows", "publish_r2", "post", "over_time"]
    assert [(r.cpu, r.ram, r.disk, r.retries) for r in client.submitted[:3]] == [
        (1.0, "4g", "64g", 1), (4.0, "32g", "200g", 1), (1.0, "4g", "64g", 0)]
    markers = sorted(p.relative_to(tmp_path / "work").as_posix() for p in (tmp_path / "work").rglob("*.done"))
    assert markers == sorted(f"fray/{run.session}/{n}.done" for n in (
        "list_source/s3%3A%2F%2Fa", "list_source/s3%3A%2F%2Fb", "index/_", "index_rows/_", "publish_r2/_", "post/_", "over_time/_"))
    assert json.loads(b.dag_file(run).read_text()) == json.loads(run.dag.graph().to_json())


def test_a_failure_lets_running_tasks_finish_and_starts_nothing(tmp_path):
    """s3://a's listing fails while s3://b's is still running: b finishes (its marker kept for a resume), index never
    starts; the error names the first failed task."""
    client = Client({"list_source-s3-a": ["failed"], "list_source-s3-b": ["running", "running", "succeeded"]})
    b = FrayBackend(client, request, poll=0)
    run = mkrun(tmp_path)
    assert go(b, run) == 1
    assert submitted(client) == ["list_source-s3-a", "list_source-s3-b"]
    assert [(t.label, t.state) for t in b.tasks[:3]] == [
        ("list_source (s3://a)", "failed"), ("list_source (s3://b)", "succeeded"), ("index", "waiting")]
    assert b.error(run, 1) == f"fray exit 1: list_source (s3://a) failed (session {run.session})"

    # the resume (same session) runs only what didn't finish, and says which tasks it found done
    client2 = Client()
    b2 = FrayBackend(client2, request, poll=0)
    assert go(b2, run) == 0
    assert submitted(client2) == ["list_source-s3-a", "index", "index_rows", "publish_r2", "post", "over_time"]
    assert json.loads(b2.dag_file(run).read_text())["cached_tasks"] == ["list_source (s3://b)"]
    # `fresh`: every task again
    client3 = Client()
    assert go(FrayBackend(client3, request, poll=0), mkrun(tmp_path, fresh=True)) == 0
    assert len(client3.submitted) == 7


def test_interrupt_terminates_the_running_tasks(tmp_path):
    client = Client({"list_source-s3-": ["running"]})
    b = FrayBackend(client, request, poll=0)
    run = mkrun(tmp_path)
    b.prepare(run)
    b.start(run)
    while [t.state for t in b.tasks[:2]] != ["running", "running"]:
        assert b.wait(0.01) is None
    b.interrupt()
    rc = None
    while rc is None:
        rc = b.wait(5)
    assert (rc, [t.state for t in b.tasks[:3]], [h.terminated for h in client.handles.values()]) == (
        1, ["stopped", "stopped", "waiting"], [True, True])
    assert submitted(client) == ["list_source-s3-a", "list_source-s3-b"]


def test_task_script(tmp_path):
    """A task's env, then each call through `secrets exec` with its bindings: a source's own for its list call."""
    from dt_cloud.scan_steps import profile_env_value

    prof = profile(tmp_path, sources=[{"uri": "s3://a", "secrets": {"AWS_ACCESS_KEY_ID": "a-key"}}, "s3://b"],
                   secrets={"AWS_ACCESS_KEY_ID": "base-key"})
    run = mkrun(tmp_path, prof)
    p = params(prof, run.profile_json, SCAN, "G1", "1", "run-1", run.session, run.env, dag=run.dag)
    node = run.dag["list_source"]
    lines = task_script(run, node, node.tasks[0], p, "e2-standard-4").splitlines()
    env = [("SNAP_ID", SCAN), ("SCAN_ID", SCAN), ("GEN", "G1"), ("DT_JOB_START", "1"), ("NF_PROCESS", "list_source"),
           ("NF_TAG", "s3://a"), ("NF_MACHINE", "e2-standard-4"), ("SITE_URL", "https://x"),
           ("DISKY_LABELS", "app=disky,deployment=fix"), ("DT_RUN_ID", "run-1"),
           ("DT_SCAN_PROFILE_JSON", profile_env_value(run.profile_json))]
    assert [lines[0], *map(shlex.split, lines[1:])] == [
        "set -e",
        *[["export", f"{k}={v}"] for k, v in env],
        ["dt-cloud", "secrets", "exec", "-s", "AWS_ACCESS_KEY_ID=base-key", "--", "dt-cloud", "scan", "step", "bucket-check"],
        ["dt-cloud", "secrets", "exec", "-s", "AWS_ACCESS_KEY_ID=a-key", "--", "dt-cloud", "scan", "step", "list", "s3://a"],
    ]


@pytest.mark.parametrize("m, shape", [
    (Machine("n2-standard-8"), (8.0, "32g")),
    (Machine("n2-highmem-16"), (16.0, "128g")),
    (Machine("e2-medium"), (2.0, "4g")),
    (Machine("n2-standard-8", cpus=6, memory="20 GB"), (6.0, "20g")),
    (Machine(cpus=2), (2.0, "4g")),
    (Machine(disk=Disk(100)), (1.0, "4g")),
])
def test_machine_shape(m: Machine, shape: tuple[float, str]):
    assert machine_shape(m) == shape


def test_machine_shape_unknown():
    with pytest.raises(ValueError) as e:
        machine_shape(Machine("a3-megagpu-8g"))
    assert str(e.value) == "machine type 'a3-megagpu-8g': no vCPU / memory shape known (set the label's `cpus` and `memory`)"


def test_job_name(tmp_path):
    run = mkrun(tmp_path, profile(tmp_path, store="meta"))
    node = run.dag["list_source"]
    assert [job_name(run, node, t) for t in node.tasks] == [
        f"disky-fix-meta-{SCAN.lower()}-list_source-s3-a", f"disky-fix-meta-{SCAN.lower()}-list_source-s3-b"]


def test_local_client(tmp_path, monkeypatch):
    """fray's own `LocalClient`, each task a real `bash -c` (every step faked: `DT_SCAN_STEP_FAKE`)."""
    local = pytest.importorskip("fray.local_backend")
    import os
    import sys

    marks = tmp_path / "marks"
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "dt-cloud").write_text(f"#!/bin/sh\nexec {sys.executable} -m dt_cloud.cli \"$@\"\n")
    (shim / "dt-cloud").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("DT_SCAN_STEP_FAKE", str(marks))
    client = local.LocalClient()
    b = FrayBackend(client, poll=0.2)
    assert go(b, mkrun(tmp_path)) == 0
    assert sorted(marks.read_text().splitlines()) == sorted(
        ["bucket-check", "list s3://a", "list s3://b", "index", "index-rows", "publish-r2", "index-sync", "over-time", "index-gc"])
