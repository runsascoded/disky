"""`scan_box`: a deployment's scan head as a local job on the VM pilot's box."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from dt_cloud import job_backend as jb
from dt_cloud import scan_box as B

SA = "demo-vm@proj.iam.gserviceaccount.com"
IMAGE = "us-west1-docker.pkg.dev/proj/repo/scan@sha256:" + "a" * 64
GIB = 1 << 30
ENDPOINT = "https://acct.r2.cloudflarestorage.com"

PROFILE = f"""
deployment: demo
executor:
  kind: google-batch
  project: proj
  region: us-west1
  service_account: demo-job@proj.iam.gserviceaccount.com
  image: us-west1-docker.pkg.dev/proj/repo/scan:nf
  boot_disk: 50
work: gs://data-bucket/nf
data: gs://data-bucket
layout: {{layer2: "listing/{{scan}}/", snapshots: demo, listing: "listing/{{scan}}/"}}
sources:
  - {{uri: r2://src-a, endpoint: {ENDPOINT}, procs: 4, threads: 8}}
  - {{uri: r2://src-b, endpoint: {ENDPOINT}, procs: 8, threads: 4, machine: e2-standard-8, disk: 100}}
machines:
  small: e2-standard-2
  big: {{type: e2-standard-8, disk: 100}}
  list: {{type: e2-standard-4, disk: 50}}
  static: {{type: e2-highmem-4, cpus: 4, memory: 30GB, disk: 100, time: 6h}}
skip: [bucket-check, index-gc, over-time]
steps: [access-r2, retire, gcp-cost, run-cost, vm-pack]
secrets:
  R2_ACCESS_KEY_ID: idx-key-id
  R2_SECRET_ACCESS_KEY: idx-secret
  CLOUDFLARE_API_TOKEN: d1-token
  SLACK_BOT_TOKEN: slack-bot
env:
  SCAN_RUNS_PROFILE: /app/job/scan-runs.json
  DUCKDB_MEM: 12GB
  DUCKDB_THREADS: "8"
  AWS_DEFAULT_REGION: auto
labels: {{deployment: demo-r2}}
options:
  list:
    secrets: {{AWS_ACCESS_KEY_ID: src-key-id, AWS_SECRET_ACCESS_KEY: src-secret}}
  access-r2: {{state: "r2://logs/state.json", mem: 2GB, threads: 2}}
  index: {{mode: path-index, args: [-U, -O], mem: 12GB, age_pyramids: true}}
  static-names: {{gen: g1, mem: 12GB}}
  interval-store: {{mem: 12GB, source: data}}
  retire: {{keep: 1}}
  digest:
    template: quota
    config: /app/job/digest.yml
    secrets: {{CF_PAGES_TOKEN: pages-token}}
"""


def test_budgets_leave_the_head_its_share():
    assert (B.budgets(8 * GIB), B.budgets(16 * GIB)) == (("6656MiB", "3GB"), ("14848MiB", "8GB"))
    with pytest.raises(ValueError) as e:
        B.budgets(2 * GIB)
    assert str(e.value) == "a 2.0 GiB budget leaves the steps under 1 GiB beside the head (1.5 GiB)"


def test_box_profile_runs_the_deployments_scan_locally_within_the_budget():
    raw = yaml.safe_load(PROFILE)
    box = B.box_profile(raw, task_mem="6656MiB", mem="3GB", nproc=4, drop_secrets=("CF_PAGES_TOKEN",),
                        skip=("gcp-cost", "run-cost"))
    assert box == {
        "deployment": "demo",
        "executor": {"kind": "local"},
        "work": "/mnt/disks/nf",
        "data": "gs://data-bucket",
        "layout": {"layer2": "listing/{scan}/", "snapshots": "demo", "listing": "listing/{scan}/"},
        "sources": [
            {"uri": "r2://src-a", "endpoint": ENDPOINT, "procs": 4, "threads": 4},
            {"uri": "r2://src-b", "endpoint": ENDPOINT, "procs": 4, "threads": 4},
        ],
        "machines": {"static": {"time": "6h"}},
        "skip": ["bucket-check", "index-gc", "over-time"],
        "steps": ["access-r2", "retire", "vm-pack"],
        "secrets": {"R2_ACCESS_KEY_ID": "idx-key-id", "R2_SECRET_ACCESS_KEY": "idx-secret", "CLOUDFLARE_API_TOKEN": "d1-token",
                    "SLACK_BOT_TOKEN": "slack-bot"},
        "env": {"SCAN_RUNS_PROFILE": "/app/job/scan-runs.json", "DUCKDB_MEM": "3GB", "DUCKDB_THREADS": "4",
                "AWS_DEFAULT_REGION": "auto", "DT_DATA_MOUNT": "/mnt/disks", "DT_MEM": "6656MiB", "DT_LIVENESS_PID": "",
                "GCP_PROJECT": "proj"},
        "labels": {"deployment": "demo-r2"},
        "options": {
            "list": {"secrets": {"AWS_ACCESS_KEY_ID": "src-key-id", "AWS_SECRET_ACCESS_KEY": "src-secret"}},
            "access-r2": {"state": "r2://logs/state.json", "mem": "2GB", "threads": 2},
            "index": {"mode": "path-index", "args": ["-U", "-O"], "mem": "3GB", "age_pyramids": True},
            "static-names": {"gen": "g1", "mem": "3GB"},
            "interval-store": {"mem": "3GB", "source": "data"},
            "retire": {"keep": 1},
            "digest": {"template": "quota", "config": "/app/job/digest.yml"},
        },
        "queue_size": 1,
    }
    assert raw == yaml.safe_load(PROFILE)  # the input untouched


@pytest.mark.parametrize("edit, message", [
    (lambda p: p.update(data="s3://bucket"), "data 's3://bucket': the box mounts a gs:// data store (its gcsfuse volume)"),
    (lambda p: p["options"].update({"vm-pack": {"batch": {}}}),
     "the box runs no Batch jobs (its job accounts have no Batch): options.vm-pack.batch submit them; skip the step (-S) "
     "or run this scan on Batch"),
])
def test_box_profile_refuses_what_the_box_cant_run(edit, message):
    raw = yaml.safe_load(PROFILE)
    edit(raw)
    with pytest.raises(ValueError) as e:
        B.box_profile(raw, task_mem="6656MiB", mem="3GB", nproc=4)
    assert str(e.value) == message


def test_head_spec():
    box = B.box_profile(yaml.safe_load(PROFILE), task_mem="6656MiB", mem="3GB", nproc=4)
    spec = B.head_spec(box, image=IMAGE, account=SA, project="proj", job_id="demo-r2-box-20261128-120000",
                       profile_path="/mnt/disks/nf/profiles/demo-r2-box-20261128-120000.yml", disk="demo-r2-nf",
                       env={"DT_TRIGGER": "manual", "DT_TRIGGER_BY": "someone"})
    labels = {"app": "disky", "deployment": "demo-r2", "component": "scan"}
    assert spec == {
        "labels": labels,
        "allocationPolicy": {"labels": labels, "serviceAccount": {"email": SA},
                             "instances": [{"policy": {"disks": [{"deviceName": "nf", "existingDisk": "demo-r2-nf"}]}}]},
        "taskGroups": [{"taskCount": 1, "taskSpec": {
            "runnables": [{"container": {
                "imageUri": IMAGE, "entrypoint": "dt-cloud",
                "commands": ["scan", "head", "/mnt/disks/nf/profiles/demo-r2-box-20261128-120000.yml"],
                "volumes": ["/mnt/disks/data-bucket:/mnt/disks/data-bucket:rw", "/mnt/disks/nf:/mnt/disks/nf:rw"],
            }}],
            "volumes": [
                {"gcs": {"remotePath": "data-bucket"}, "mountPath": "/mnt/disks/data-bucket",
                 "mountOptions": ["-o rw", "-implicit-dirs", "--app-name disky-vm-pilot"]},
                {"deviceName": "nf", "mountPath": "/mnt/disks/nf"},
            ],
            "environment": {
                "variables": {"DISKY_LABELS": "app=disky,deployment=demo-r2,component=scan", "DT_SCAN_IMAGE": IMAGE,
                              "DT_RUN_ID": "demo-r2-box-20261128-120000", "DT_NF_DIR": "/app/job/nf",
                              "NXF_OPTS": "-Xms128m -Xmx768m", "DT_TRIGGER": "manual", "DT_TRIGGER_BY": "someone"},
                "secretVariables": {"CLOUDFLARE_API_TOKEN": "projects/proj/secrets/d1-token/versions/latest",
                                    "SLACK_BOT_TOKEN": "projects/proj/secrets/slack-bot/versions/latest"},
            },
            "maxRunDuration": "21600s",
        }}],
    }
    # the local backend runs it: the bucket mounted, the head's disk kept, the job on its own network
    plan = jb.container_runs(spec, "demo-r2-box-20261128-120000", engine="docker", slice_="scan.slice",
                             disks=Path("/w/j"), kept=Path("/w/_disks"), gcs_root=Path("/jobs/j/gcs"))
    assert (plan.dirs, plan.keep, plan.gcs) == (
        [], [Path("/w/_disks/demo-r2-nf")],
        [jb.GcsMount("data-bucket", Path("/jobs/j/gcs/0"), "", ("-o", "rw", "--implicit-dirs", "--app-name", "disky-vm-pilot"))])


class Host:
    """What `submit` asks of the host: the image's profile (`capture`), a supervisor's spawn (recorded)."""

    def __init__(self):
        self.captured: list[tuple[list[str], dict | None]] = []
        self.spawned: list[list[str]] = []

    def capture(self, argv, env=None):
        self.captured.append((argv, env))
        return PROFILE

    def spawn(self, argv, log):
        self.spawned.append(argv)

    def boot_id(self):
        return "boot-1"


class Identity:
    def access_token(self, service_account, lifetime_s):
        return f"tok-{service_account}-{lifetime_s}"


NOW = datetime(2026, 11, 28, 12, 0, 0, tzinfo=timezone.utc)
JOB = "demo-r2-box-20261128-120000"


def submit(tmp_path: Path, dry_run: bool = False) -> tuple[jb.Local, Host, dict]:
    host = Host()
    jobs = jb.Local(tmp_path / "jobs", work=tmp_path / "work", host=host, identity=Identity(), spawn="direct")
    doc = B.submit(jobs, IMAGE, "/app/job/profiles/demo.yml", account=SA, budget=8 * GIB, nproc=4,
                   drop_secrets=("CF_PAGES_TOKEN",), skip=("gcp-cost", "run-cost"),
                   env={"DT_TRIGGER": "manual"}, dry_run=dry_run, now=NOW)
    return jobs, host, doc


DAG = ["pre", "list_source", "index", "index_rows", "publish_r2", "static_names", "interval_store", "post"]


def test_submit_reads_the_images_profile_writes_the_box_one_and_queues_the_head(tmp_path):
    jobs, host, doc = submit(tmp_path)
    kept = tmp_path / "work/_disks/demo-r2-nf"
    assert doc == {"job": JOB, "image": IMAGE, "account": SA, "profile": str(kept / f"profiles/{JOB}.yml"), "work": str(kept),
                   "budget": 8 * GIB, "task_mem": "6656MiB", "mem": "3GB", "queue_size": 1, "dag": DAG}
    assert host.captured == [(["docker", "run", "--rm", "--network", "none", "--entrypoint", "cat", IMAGE,
                               "/app/job/profiles/demo.yml"], {"GOOGLE_OAUTH_ACCESS_TOKEN": f"tok-{SA}-900"})]
    box = yaml.safe_load((kept / f"profiles/{JOB}.yml").read_text())
    assert (box["executor"], box["env"]["DT_MEM"], json.loads((kept / f"profiles/{JOB}.box.json").read_text())) == (
        {"kind": "local"}, "6656MiB", doc)
    assert (jobs.status(JOB), host.spawned) == (jb.JobStatus(jb.QUEUED), [jobs.supervisor_argv(JOB)])
    spec = json.loads((tmp_path / f"jobs/{JOB}/spec.json").read_text())
    assert spec["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"] == [
        "scan", "head", f"/mnt/disks/nf/profiles/{JOB}.yml"]


def test_a_dry_run_writes_and_submits_nothing(tmp_path):
    jobs, host, doc = submit(tmp_path, dry_run=True)
    assert (sorted(doc), doc["dag"], (tmp_path / "work").exists(), (tmp_path / "jobs").exists(), host.spawned) == (
        ["account", "box_profile", "budget", "dag", "image", "job", "mem", "profile", "queue_size", "spec", "task_mem",
         "work"], DAG, False, False, [])


def test_submit_wants_a_pinned_image(tmp_path):
    jobs = jb.Local(tmp_path / "jobs", host=Host(), identity=Identity(), spawn="direct")
    with pytest.raises(ValueError) as e:
        B.submit(jobs, "repo/scan:nf", "/p.yml", account=SA, budget=8 * GIB, nproc=4)
    assert str(e.value) == "image 'repo/scan:nf': pin it by digest (the deployment's scan image, as its scheduler runs it)"


def test_report_reads_the_run_back_per_task(tmp_path):
    jobs, _, _ = submit(tmp_path)
    d = tmp_path / f"jobs/{JOB}"
    kept = tmp_path / "work/_disks/demo-r2-nf"
    (d / "log").write_text("scan head: demo scan 2026-11-28T1200 gen 20261128T120000Z session abc (manual) in /mnt/disks/nf\n")
    t0 = datetime(2026, 11, 28, 12, 1, tzinfo=timezone.utc).timestamp()
    (d / "usage.tsv").write_text("t\trun\tcurrent\tanon\tfile\tpeak\tslice_current\n"
                                 f"{t0 + 5:.1f}\tdisky-{JOB}-0-0\t{3 * GIB}\t{2 * GIB}\t{GIB}\t{3 * GIB}\t{4 * GIB}\n"
                                 f"{t0 + 50:.1f}\tdisky-{JOB}-0-0\t{5 * GIB}\t{GIB}\t{4 * GIB}\t{5 * GIB}\t{6 * GIB}\n")
    run = {"run": f"disky-{JOB}-0-0", "task": 0, "exit": 0, "wall_s": 100.0, "memory_peak": 5 * GIB, "max_anon": 2 * GIB,
           "max_slice_current": 6 * GIB, "oom_kill": 0, "cpu_s": 80.0}
    (d / "usage.jsonl").write_text(json.dumps(run) + "\n")
    (kept / "runs/2026-11-28T1200").mkdir(parents=True)
    (kept / "runs/2026-11-28T1200/trace.tsv").write_text(
        "task_id\tname\tstatus\texit\tsubmit\tduration\trealtime\t%cpu\tpeak_rss\n"
        "1\tindex\tCOMPLETED\t0\t2026-11-28 12:01:00.000\t20s\t15s\t280.0%\t2.5 GB\n"
        "2\tpost\tCOMPLETED\t0\t2026-11-28 12:01:30.000\t30s\t25s\t20.0%\t1 GB\n")
    doc = B.report(jobs, JOB)
    expected = [
        "task\tstatus\twall_s\tpeak_rss_gib\tmax_anon_gib\tmax_current_gib\tmax_slice_gib\tcpu_pct\tsamples",
        "index\tCOMPLETED\t15\t2.50\t2.00\t3.00\t4.00\t280.0%\t1",
        "post\tCOMPLETED\t25\t1.00\t1.00\t5.00\t6.00\t20.0%\t1",
        f"# run disky-{JOB}-0-0: exit 0, wall 100s, memory.peak 5.00 GiB, max anon 2.00 GiB, max slice 6.00 GiB, "
        "oom_kill 0, cpu 80s (budget 8.00 GiB)",
    ]
    out = kept / "runs/2026-11-28T1200"
    assert ((doc["scan"], doc["gen"], doc["budget"], [t["task"] for t in doc["tasks"]]),
            (out / "usage.tsv").read_text().splitlines(), json.loads((out / "usage.json").read_text()) == doc) == (
        ("2026-11-28T1200", "20261128T120000Z", 8 * GIB, ["index", "post"]), expected, True)
