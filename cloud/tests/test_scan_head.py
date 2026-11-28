"""`dt-cloud scan head`'s pure parts: identity, session, history line, params + executor config (golden files from
the cw-s3-shaped fixture profile), the trace's first failure, and a dry run's files."""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from dt_cloud import heartbeat
from dt_cloud import scan_head as H
from dt_cloud.cli import main
from dt_cloud.scan_backend import Backend
from dt_cloud.scan_dag import build
from dt_cloud.scan_dag_nf import render
from dt_cloud.scan_profile import load

FIX = Path(__file__).parent / "fixtures" / "scan_profiles"
#: The head's `nf_dir` (`job/nf`): `base.config` and the prelude each run's `scan.nf` starts with.
NF = Path(__file__).resolve().parents[2] / "job" / "nf"
NOW = dt.datetime(2026, 10, 16, 12, 0, 1, tzinfo=dt.timezone.utc)
#: A head's required cost labels (`$DISKY_LABELS`: `app`, `deployment`), as `conftest` sets them for every test.
LABELS = {"DISKY_LABELS": "app=disky,deployment=test"}


def test_identity_stamped_once():
    assert H.identity({}, NOW) == ("2026-10-16T1200", "20261016T120001Z", "1792152001")
    resumed = {"SNAP_ID": "2026-10-15T0600", "GEN": "20261015T060002Z", "DT_JOB_START": "17"}
    assert H.identity(resumed, NOW) == ("2026-10-15T0600", "20261015T060002Z", "17")


def test_session_id():
    """uuid5(NAMESPACE_URL, `disky/<deployment>/scan/<scan>`): the same scan resumes the same session anywhere, and
    cw-s3's matches its prototype head's (`cw-head.sh`), so a scan started there resumes here."""
    assert H.session_id("cw-s3", "2026-10-16T1200") == "06a5f601-f0b7-58a3-9817-fc0a8fee5e63"
    assert H.session_id("other", "2026-10-16T1200") == "8b68f678-566c-5e77-9089-7305bed64c57"


def test_session_id_per_store():
    """A store other than the primary namespaces its sessions (`disky/<deployment>/<store>/scan/<scan>`): the same scan id
    of the deployment's meta store is another run. The primary's is unchanged, so its sessions still resume."""
    import uuid

    assert H.session_id("cw-s3", "2026-10-16T1200", "primary") == "06a5f601-f0b7-58a3-9817-fc0a8fee5e63"
    assert H.session_id("cw-s3", "2026-10-16T1200", "meta") == str(uuid.uuid5(uuid.NAMESPACE_URL, "disky/cw-s3/meta/scan/2026-10-16T1200"))
    assert H.session_id("cw-s3", "2026-10-16T1200", "meta") == "43a92eb7-2c67-54bb-9a9a-4c56a2b389b0"


def test_start_args_carry_the_run_and_the_store():
    """The run id every task records into (`-r`), the Batch job's facts only from one (`-B`), the store (`-K`)."""
    cw, meta = load(FIX / "cw-s3.yml"), load(FIX / "meta-store.yml")
    assert (H.start_args(cw, "G", "2026-10-16T1200", "uid1"), H.start_args(meta, "G", "2026-10-16T1200", "uid1"),
            H.start_args(cw, "G", "2026-10-16T1200", "cw-s3-nf-2026-10-16t1200", batch=False)) == (
        ["-r", "uid1", "-B", "-g", "G", "2026-10-16T1200"],
        ["-r", "uid1", "-B", "-g", "G", "-K", "meta", "2026-10-16T1200"],
        ["-r", "cw-s3-nf-2026-10-16t1200", "-g", "G", "2026-10-16T1200"],
    )


def test_start_args_take_a_non_batch_heads_facts_from_its_env():
    """A head its launcher started (m3's AWS Batch wrapper) records the facts the launcher knows: kind, machine, spot,
    region, when the run started (its capture's start) and the task count; a GCP Batch head (`-B`) ignores them."""
    cw = load(FIX / "cw-s3.yml")
    env = {"DT_RUN_KIND": "ingest", "DT_RUN_MACHINE": "fargate-arm64-2vcpu-16gib", "DT_RUN_SPOT": "1", "DT_RUN_REGION": "us-east-1",
           "DT_RUN_AT": "1792152001", "DT_RUN_TASKS": "1"}
    assert (H.start_args(cw, "G", "2026-10-16T1200", "aws-job-1", batch=False, env=env), H.start_args(cw, "G", "2026-10-16T1200", "uid1", env=env)) == (
        ["-r", "aws-job-1", "-k", "ingest", "-m", "fargate-arm64-2vcpu-16gib", "-R", "us-east-1", "-t", "1792152001", "-T", "1",
         "--spot", "-g", "G", "2026-10-16T1200"],
        ["-r", "uid1", "-B", "-g", "G", "2026-10-16T1200"],
    )


def test_pre_phases_from_the_env():
    """`DT_PRE_PHASES`: phases that ran before the head (a capture, the queue wait), each as its `phase-begin` and
    `phase-end` arguments."""
    env = {"DT_PRE_PHASES": '[{"name": "capture", "began": 100, "ended": 460, "outputs": ["capture=r2://b/captures/m3/root/s/"]},'
                            ' {"name": "queue", "began": 460, "ended": 506}]'}
    assert (H.pre_phases(env), H.pre_phases({})) == ([
        (["-b", "100", "capture"], ["-t", "460", "-o", "capture=r2://b/captures/m3/root/s/", "capture"]),
        (["-b", "460", "queue"], ["-t", "506", "queue"]),
    ], [])


def test_head_image_pins_the_tasks():
    """`$DT_SCAN_IMAGE` (the head's own image, by digest) replaces the profile's tag as every task's container, and the
    run records it (`start -i`): a rebuild of the tag mid-run can't split the run across images."""
    cw = load(FIX / "cw-s3.yml")
    pinned = "us-central1-docker.pkg.dev/example-project/scans/scan@sha256:" + "ab" * 32
    golden = (FIX / "cw-s3.executor.config").read_text()
    assert H.executor_config(cw, pinned) == golden.replace(
        "    container = 'us-central1-docker.pkg.dev/example-project/scans/scan:cw'\n", f"    container = '{pinned}'\n")
    assert H.executor_config(cw, pinned) != golden
    assert H.start_args(cw, "G", "2026-10-16T1200", "uid1", image=pinned) == [
        "-r", "uid1", "-B", "-g", "G", "-i", pinned, "2026-10-16T1200"]


def test_local_run_id():
    """A head that isn't a Batch job records under one run id of its own, which every task inherits (not a run each)."""
    assert (H.local_run_id(load(FIX / "cw-s3.yml"), "2026-10-17T0005"), H.local_run_id(load(FIX / "meta-store.yml"), "2026-10-17T0005")) == (
        "cw-s3-nf-2026-10-17t0005", "example-meta-nf-2026-10-17t0005")


def test_params_golden_per_call_secrets():
    """The meta-store fixture: a source's own bindings (its list call's whole list) and a step's (`step_secrets`), each
    the profile's overlaid; a step with none of its own reads `secrets`."""
    p = load(FIX / "meta-store.yml")
    got = H.params(p, "<profile>", "2026-10-16T1200", "20261016T120001Z", "1792152001", "run-1",
                   H.session_id("example", "2026-10-16T1200", "meta"), {"DISKY_LABELS": "app=disky,deployment=example"})
    assert got == json.loads((FIX / "meta-store.params.json").read_text())


def test_history_line():
    assert H.history_line(NOW, "2026-10-16T1200", "S") == "2026-10-16 12:00:01\t-\tscan_2026_10_16T1200\tOK\t-\tS\tnextflow run scan.nf\n"


def test_executor_config_golden():
    assert H.executor_config(load(FIX / "cw-s3.yml")) == (FIX / "cw-s3.executor.config").read_text()


def test_params_golden():
    p = load(FIX / "cw-s3.yml")
    got = H.params(p, "<profile>", "2026-10-16T1200", "20261016T120001Z", "1792152001", "run-1",
                   H.session_id("cw-s3", "2026-10-16T1200"), {"DT_RECORD": "off", "DISKY_LABELS": "app=disky,deployment=cw-s3,stack=prod,team=x", "OTHER": "1"})
    assert got == json.loads((FIX / "cw-s3.params.json").read_text())


def test_a_static_machine_gets_its_label():
    from dataclasses import replace

    from dt_cloud.scan_profile import Machine

    default = load(FIX / "cw-s3.yml")
    sized = replace(default, machines={**default.machines, "static": Machine(type="e2-standard-4", cpus=4, memory="14 GB")})
    assert [line for line in H.executor_config(sized).splitlines() if line.startswith("    withLabel: static ")] == [
        "    withLabel: static { machineType = 'e2-standard-4'; cpus = 4; memory = '14 GB' }",
    ]


def test_params_reproc_and_fresh():
    from dt_cloud.scan_profile import parse

    p = parse({"deployment": "fix", "executor": {"kind": "local"}, "work": "/w", "data": "/d",
               "layout": {"layer2": "l2/{scan}/", "snapshots": ""}, "sources": ["gs://a"],
               "steps": ["access-ingest", "healthcheck"], "skip": ["digest", "static-names"],
               "options": {"list": {"import": True}}})
    got = H.params(p, "{}", "2026-10-16", "G", "1", "", "S", LABELS, reproc=True, fresh=True)
    assert {k: got[k] for k in ("sources", "cache", "env", "work_bucket", "secrets_project")} == {
        "sources": [],
        "cache": False,
        "env": {"REPROC": "1", **LABELS},
        "work_bucket": "",
        "secrets_project": "",
    }


def test_local_executor_config():
    from dt_cloud.scan_profile import parse

    p = parse({"deployment": "fix", "executor": {"kind": "local"}, "work": "/w/nf", "data": "/d",
               "layout": {"layer2": "l2/{scan}/", "snapshots": ""}, "sources": ["gs://a"],
               "machines": {"big": {"type": "n2-standard-8", "cpus": 4, "memory": "8 GB"}}, "skip": ["digest", "static-names"],
               "options": {"list": {"import": True}}})
    assert H.executor_config(p).split("\n") == [
        "// Generated by `dt-cloud scan head` from the fix profile: not edited by hand.",
        "workDir = '/w/nf/work'",
        "process {",
        "    executor = 'local'",
        "    withLabel: big { cpus = 4; memory = '8 GB' }",
        "}",
        "",
    ]


def test_first_failed(tmp_path):
    t = tmp_path / "trace.tsv"
    t.write_text("task_id\thash\tnative_id\tname\tstatus\texit\n"
                 "1\tab/1\tj1\tindex\tCOMPLETED\t0\n"
                 "2\tcd/2\tj2\tpost\tFAILED\t1\n"
                 "3\tef/3\tj3\tinterval_store\tFAILED\t137\n")
    assert (H.first_failed(t), H.first_failed(tmp_path / "none.tsv")) == ("post (exit 1)", None)


def test_first_failed_skips_retried(tmp_path):
    """A fallback's queue timeout is a FAILED row followed by its retry's: the run's failure is the first one not retried."""
    t = tmp_path / "trace.tsv"
    t.write_text("task_id\thash\tnative_id\tname\tstatus\texit\n"
                 "1\tab/1\tj1\tindex\tFAILED\t-\n"
                 "2\tcd/2\tj2\tindex\tCOMPLETED\t0\n"
                 "3\tef/3\tj3\tinterval_store\tFAILED\t-\n"
                 "4\tgh/4\tj4\tinterval_store\tFAILED\t1\n")
    assert H.first_failed(t) == "interval_store (exit 1)"


def test_fallback_executor_config():
    """A label with fallbacks (spec §7): its machine by submit attempt, a queue timeout on every attempt but the last, and
    a retry only on Nextflow's submit timeout."""
    from dt_cloud.scan_profile import parse

    p = parse({"deployment": "fix", "executor": {"kind": "google-batch", "project": "p", "region": "r", "image": "img"},
               "work": "gs://w/nf", "data": "gs://w", "layout": {"layer2": "l2/{scan}/", "snapshots": ""}, "sources": ["gs://a"],
               "machines": {"big": {"type": "n2-highmem-32", "disk": {"request": 1500, "type": "local-ssd"}, "memory": "200 GB",
                                    "fallback": ["n2d-highmem-32", "n4-highmem-32"], "queue_timeout": 10},
                            "small": "e2-standard-2"},
               "skip": ["digest", "static-names"], "options": {"list": {"import": True}}})
    assert H.executor_config(p).split("\n") == [
        "// Generated by `dt-cloud scan head` from the fix profile: not edited by hand.",
        "workDir = 'gs://w/nf/work'",
        "plugins {",
        "    id 'nf-google@1.29.1'",
        "    id 'nf-cloudcache@0.6.2'",
        "}",
        "google {",
        "    project = 'p'",
        "    location = 'r'",
        "}",
        "process {",
        "    executor = 'google-batch'",
        "    container = 'img'",
        "    withLabel: big {",
        "        machineType = { ['n2-highmem-32', 'n2d-highmem-32', 'n4-highmem-32'][task.submitAttempt - 1] }",
        "        disk = [request: 1500.GB, type: 'local-ssd']",
        "        memory = '200 GB'",
        "        maxSubmitAwait = { task.submitAttempt < 3 ? '10m' : null }",
        "        maxRetries = 2",
        "        errorStrategy = {",
        "            if( !(task.previousException?.cause instanceof nextflow.exception.ProcessSubmitTimeoutException) ) return 'finish'",
        "            log.warn \"scan head: ${task.process} queued 10 min on ${task.machineType}; retrying on "
        "${['n2-highmem-32', 'n2d-highmem-32', 'n4-highmem-32'][task.submitAttempt]}"
        "${task.submitAttempt + 1 < 3 ? '' : ' (the last: no queue timeout)'}\"",
        "            return 'retry'",
        "        }",
        "    }",
        "    withLabel: small { machineType = 'e2-standard-2' }",
        "}",
        "",
    ]


def test_params_source_fallback():
    """A source with fallbacks: its list task's machines by submit attempt and its queue timeout (a Nextflow duration), and the most
    fallbacks any source has as the list tasks' retry bound."""
    from dt_cloud.scan_profile import parse

    p = parse({"deployment": "fix", "executor": {"kind": "google-batch", "project": "p", "region": "r", "image": "img"},
               "work": "gs://w/nf", "data": "gs://w", "layout": {"layer2": "l2/{scan}/", "snapshots": ""},
               "sources": [{"uri": "s3://a", "machine": "n2-standard-16", "fallback": ["n2d-standard-16", "c2d-standard-16"],
                            "queue_timeout": 10}, "s3://b"],
               "skip": ["digest", "static-names"], "options": {"list": {"import": True}}})
    got = H.params(p, "{}", "2026-10-16", "G", "1", "", "S", {"DISKY_LABELS": "app=disky,deployment=fix"})
    assert {k: got[k] for k in ("sources", "list_retries")} == {
        "sources": [
            {"uri": "s3://a", "machine": "n2-standard-16", "disk": None,
             "machines": ["n2-standard-16", "n2d-standard-16", "c2d-standard-16"], "queue_timeout": "10m"},
            {"uri": "s3://b", "machine": None, "disk": None},
        ],
        "list_retries": 2,
    }


def test_dry_run_writes_the_launch_files(tmp_path, monkeypatch):
    for k in ("SNAP_ID", "GEN", "DT_JOB_START", "DT_RUN_ID", "BATCH_JOB_UID", "SCAN_RUNS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SNAP_ID", "2026-10-16T1200")
    monkeypatch.setenv("GEN", "G1")
    monkeypatch.setenv("DT_JOB_START", "100")
    launch = tmp_path / "launch"
    r = CliRunner().invoke(main, ["scan", "head", "-n", "-N", str(NF), "-w", str(launch), str(FIX / "cw-s3.yml")])
    assert r.exit_code == 0, r.output
    session = H.session_id("cw-s3", "2026-10-16T1200")
    assert sorted(p.name for p in launch.iterdir()) == [".nextflow", "executor.config", "params.json", "run.config", "scan.nf"]
    assert (launch / "scan.nf").read_text() == render(build(load(FIX / "cw-s3.yml")), (NF / "prelude.nf").read_text(), "cw-s3")
    assert (launch / "executor.config").read_text() == (FIX / "cw-s3.executor.config").read_text()
    params = json.loads((launch / "params.json").read_text())
    assert (params["scan"], params["gen"], params["job_start"], params["session"]) == ("2026-10-16T1200", "G1", "100", session)
    assert json.loads(params["profile_json"])["deployment"] == "cw-s3"
    assert (launch / "run.config").read_text() == H.run_config(params["run_id"], params["profile_json"])
    line = (launch / ".nextflow" / "history").read_text().split("\t")
    assert line[1:] == ["-", "scan_2026_10_16T1200", "OK", "-", session, "nextflow run scan.nf\n"]
    assert r.stderr.rstrip().split("\n")[-1] == (
        f"+ nextflow run {launch}/scan.nf -c {NF}/base.config -c {launch}/executor.config -c {launch}/run.config "
        f"-params-file {launch}/params.json -resume {session} -with-trace {launch}/trace.tsv "
        f"-with-dag {launch}/dag.mmd -with-timeline {launch}/timeline.html -with-report {launch}/report.html"
    )


@pytest.mark.parametrize("labels, missing", [(None, ["app", "deployment"]), ("app=disky", ["deployment"]), ("deployment=cw-s3,team=x", ["app"])])
def test_head_refuses_without_cost_labels(tmp_path, labels, missing):
    """No unlabelled scan (specs/done/cost-labels-required.md): `head` refuses before it writes or records anything when
    `$DISKY_LABELS` lacks `app` or `deployment` (the profile's own `labels` don't count)."""
    from dt_cloud.cost_labels import MissingCostLabels

    env = {"SNAP_ID": "2026-10-16T1200", "GEN": "G1", "DT_JOB_START": "100", **({"DISKY_LABELS": labels} if labels else {})}
    launch = tmp_path / "launch"
    with pytest.raises(MissingCostLabels) as e:
        H.head(FIX / "cw-s3.yml", NF, launch, dry_run=True, env=env)
    assert (e.value.missing, launch.exists()) == (missing, False)


def test_head_takes_cost_labels_from_the_profiles_env(tmp_path):
    """`$DISKY_LABELS` unset in the head's env but in the profile's `env`: the head takes it (as `D1_DB_ID` & co.), so
    every task's labels carry it and every step's env forwards it to the Batch jobs it submits."""
    prof = tmp_path / "p.yml"
    prof.write_text(f"deployment: fix\nexecutor: {{kind: local}}\nwork: {tmp_path}/nf\ndata: {tmp_path}/data\n"
                    "layout: {layer2: 'l2/{scan}/', snapshots: ''}\nsources: [s3://a]\n"
                    "skip: [static-names, digest, interval-store, warm-cache, probe]\n"
                    "options: {list: {import: true}}\nenv: {DISKY_LABELS: 'app=disky,deployment=fix'}\n")
    launch = tmp_path / "launch"
    env = {"SNAP_ID": "2026-10-16T1200", "GEN": "G1", "DT_JOB_START": "100", "DT_RECORD": "off"}
    assert H.head(prof, NF, launch, dry_run=True, env=env) == 0
    params = json.loads((launch / "params.json").read_text())
    assert (params["labels"], params["env"]["DISKY_LABELS"]) == (
        {"app": "disky", "deployment": "fix", "component": "scan", "scan_id": "2026-10-16t1200"}, "app=disky,deployment=fix")


def test_a_batch_heads_tasks_carry_its_run(tmp_path):
    """A head running as a Batch job labels its tasks `scan_run` (its job's uid, lowercased) and exports `SCAN_RUN_ID` into
    their env, so the jobs a step submits (static names' append, drill, …: `cost_labels.label_batch_spec`) carry it too
    and the run's cost sums them (specs/scan-cost-by-stage.md)."""
    prof = tmp_path / "p.yml"
    prof.write_text(f"deployment: fix\nexecutor: {{kind: local}}\nwork: {tmp_path}/nf\ndata: {tmp_path}/data\n"
                    "layout: {layer2: 'l2/{scan}/', snapshots: ''}\nsources: [s3://a]\n"
                    "skip: [static-names, digest, interval-store, warm-cache, probe]\n"
                    "options: {list: {import: true}}\nenv: {DISKY_LABELS: 'app=disky,deployment=fix'}\n")
    launch = tmp_path / "launch"
    env = {"SNAP_ID": "2026-10-16T1200", "GEN": "G1", "DT_JOB_START": "100", "DT_RECORD": "off",
           "BATCH_JOB_UID": "fix-usage-snapshot-2-5f3a1c9e-0b1d-4e7a0"}
    assert H.head(prof, NF, launch, dry_run=True, env=env) == 0
    params = json.loads((launch / "params.json").read_text())
    assert params["labels"] == {"app": "disky", "deployment": "fix", "component": "scan", "scan_id": "2026-10-16t1200",
                                "scan_run": "fix-usage-snapshot-2-5f3a1c9e-0b1d-4e7a0"}
    assert (launch / "run.config").read_text().split("\n")[1:3] == [
        "    DT_RUN_ID = 'fix-usage-snapshot-2-5f3a1c9e-0b1d-4e7a0'", "    SCAN_RUN_ID = 'fix-usage-snapshot-2-5f3a1c9e-0b1d-4e7a0'"]


def test_a_runs_jobs_are_priced_by_its_scan_run():
    """`finish -l`: a recorded google-batch run prices the jobs labelled with its run (its tasks and what they submitted);
    an unrecorded one its session's tasks; a local executor none."""
    from types import SimpleNamespace as NS

    backend = H.NextflowBackend(NF)
    run = lambda kind, run_id: NS(profile=NS(executor=NS(kind=kind)), run_id=run_id, session="s1")  # noqa: E731
    assert [backend.cost(run("google-batch", "Fix-Usage-1")), backend.cost(run("google-batch", "")), backend.cost(run("local", "r"))] == [
        ["-l", "scan_run=fix-usage-1"], ["-l", "nf-session=s1"], []]


def test_a_tasks_labels_are_the_heads_on_its_job_and_its_allocation():
    """Nextflow builds each task's Batch job, so no spec of ours is asserted: `base.config` hands nf-google the head's
    `params.labels` (above) + the session + the step as `process.resourceLabels`, which the pinned plugin puts on the
    job's `labels` and on its `allocationPolicy.labels` (1.29.1's `GoogleBatchTaskHandler` calls `putAllLabels` on both
    builders). A plugin bump re-checks that before this pin moves."""
    base = (Path(__file__).parents[2] / "job" / "nf" / "base.config").read_text()
    assert H.NF_GOOGLE == "nf-google@1.29.1"
    assert [ln.strip() for ln in base.splitlines() if ln.strip().startswith(("params.labels", "resourceLabels"))] == [
        "params.labels = [:]",
        "resourceLabels = { params.labels + (params.session ? ['nf-session': params.session] : [:]) "
        "+ [step: task.process.toLowerCase().replaceAll(/[^a-z0-9_-]/, '-').take(63)] }",
    ]


def test_head_records_into_the_profiles_database_and_store(tmp_path, monkeypatch):
    """`head()` with `D1_DB_ID` (and `SCAN_RUNS_PROFILE`) only in the profile's `env` (cw's first prod run on the generic
    head: its own start / finish found no database), and a non-primary `store`: both writes go to the profile's D1, the
    run filed under the store. Nextflow is a fake process that exits 0."""
    from dt_cloud import index_footer, scan_runs

    for k in ("D1_DB_ID", "SCAN_RUNS_PROFILE", "DT_RECORD", "BATCH_JOB_UID", "DT_RUN_ID", "SNAP_ID", "GEN", "DT_JOB_START",
              "REPROC", "DT_TRIGGER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "t"))
    monkeypatch.setenv("DT_TRIGGER", "schedule")
    monkeypatch.setenv("SNAP_ID", "2026-10-16T1200")
    monkeypatch.setenv("GEN", "G1")
    monkeypatch.setenv("DT_JOB_START", "100")
    monkeypatch.setenv("DT_RUN_ID", "run-1")
    runs = tmp_path / "scan-runs.json"
    runs.write_text('{"store": "primary", "run_id_env": ["DT_RUN_ID", "BATCH_JOB_UID"]}')
    prof = tmp_path / "p.yml"
    prof.write_text(f"deployment: fix\nstore: meta\nexecutor: {{kind: local}}\nwork: {tmp_path}/nf\ndata: {tmp_path}/data\n"
                    "layout: {layer2: 'meta-l2/{scan}/', snapshots: meta}\nsources: [s3://a]\n"
                    "skip: [static-names, digest, interval-store, warm-cache, probe]\noptions: {list: {import: true}}\n"
                    f"env: {{SCAN_RUNS_PROFILE: {runs}, D1_DB_ID: db-from-profile}}\n")
    monkeypatch.setattr(index_footer, "D1_DB_ID", "")
    monkeypatch.setattr(index_footer, "_creds", lambda: ("tok", "acct"))
    dbs: list[str] = []
    monkeypatch.setattr(index_footer, "_d1_query", lambda sql, acct, tok, db: dbs.append(db) or [])
    monkeypatch.setattr(scan_runs, "lookup_job", lambda *a, **kw: None)
    runs_written: list[tuple[str, str, str, str | None]] = []
    record_sql = scan_runs.record_sql

    def spy(rec, now):
        runs_written.append((rec.run.scan, rec.run.store, rec.run.status, rec.run.trigger))
        return record_sql(rec, now)

    monkeypatch.setattr(scan_runs, "record_sql", spy)
    monkeypatch.setattr(H.subprocess, "Popen", lambda argv, **kw: SimpleNamespace(pid=0, stdout=BytesIO(), wait=lambda timeout=None: 0))
    assert H.head(prof, NF, tmp_path / "launch") == 0
    assert (runs_written[0], runs_written[-1][:2], set(dbs)) == (
        ("2026-10-16T1200", "meta", "running", "schedule"), ("2026-10-16T1200", "meta"), {"db-from-profile"})
    # a resume by hand: its run is manual (the scan stays the cron's: its first run's, `digest_quota.scan_triggers`)
    monkeypatch.setenv("DT_TRIGGER", "manual")
    monkeypatch.setenv("DT_RUN_ID", "run-2")
    runs_written.clear()
    assert H.head(prof, NF, tmp_path / "launch") == 0
    assert runs_written[0] == ("2026-10-16T1200", "meta", "running", "manual")
    # a deployment that doesn't state its triggers: none recorded (the digest goes by the clock)
    monkeypatch.delenv("DT_TRIGGER")
    monkeypatch.setenv("DT_RUN_ID", "run-3")
    monkeypatch.setenv("SNAP_ID", "2026-10-16T1300")
    runs_written.clear()
    assert H.head(prof, NF, tmp_path / "launch") == 0
    assert runs_written[0] == ("2026-10-16T1300", "meta", "running", None)


def test_head_stores_the_dag_before_the_run(tmp_path, monkeypatch):
    """The run's DAG is stored from a `-preview` right after `start`, before Nextflow runs a task, so the site draws the
    pipeline while the run is going; a preview that fails leaves the run unaffected."""
    from dt_cloud import index_footer, scan_runs

    for k in ("D1_DB_ID", "SCAN_RUNS_PROFILE", "DT_RECORD", "BATCH_JOB_UID", "DT_RUN_ID", "SNAP_ID", "GEN", "DT_JOB_START",
              "REPROC", "DT_TRIGGER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SNAP_ID", "2026-10-16T1200")
    monkeypatch.setenv("GEN", "G1")
    monkeypatch.setenv("DT_JOB_START", "100")
    monkeypatch.setenv("DT_RUN_ID", "run-1")
    runs = tmp_path / "scan-runs.json"
    runs.write_text('{"store": "primary", "run_id_env": ["DT_RUN_ID", "BATCH_JOB_UID"]}')
    prof = tmp_path / "p.yml"
    prof.write_text(f"deployment: fix\nexecutor: {{kind: local}}\nwork: {tmp_path}/nf\ndata: {tmp_path}/data\n"
                    "layout: {layer2: 'l2/{scan}/', snapshots: ''}\nsources: [s3://a]\n"
                    "skip: [static-names, digest, interval-store, warm-cache, probe]\noptions: {list: {import: true}}\n"
                    f"env: {{SCAN_RUNS_PROFILE: {runs}, D1_DB_ID: db}}\n")
    monkeypatch.setattr(index_footer, "D1_DB_ID", "")
    monkeypatch.setattr(index_footer, "_creds", lambda: ("tok", "acct"))
    monkeypatch.setattr(scan_runs, "lookup_job", lambda *a, **kw: None)
    events: list[str] = []
    monkeypatch.setattr(index_footer, "_d1_query",
                        lambda sql, acct, tok, db: events.extend(s.split(" (")[0] for s in ([sql] if isinstance(sql, str) else sql)) or [])
    launch, nf_dir = tmp_path / "launch", NF
    previews: list[list[str]] = []

    def preview(argv, **kw):
        previews.append(argv)
        (launch / H.PREVIEW_DAG).write_text('flowchart TB\n    v2(["list_source"])\n    v6(["index"])\n    v2 --> v6\n')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(H.subprocess, "run", preview)
    monkeypatch.setattr(H.subprocess, "Popen", lambda argv, **kw: events.append("nextflow run") or SimpleNamespace(pid=0, stdout=BytesIO(), wait=lambda timeout=None: 0))
    assert H.head(prof, nf_dir, launch) == 0
    assert previews == [H.preview_argv(nf_dir, launch)]
    assert previews[0][-3:] == ["-preview", "-with-dag", str(launch / "dag-preview.mmd")]
    dag_at = events.index("INSERT INTO scan_run_dags")
    assert (events.index("INSERT INTO scan_runs") < dag_at < events.index("nextflow run")) is True

    def broken(argv, **kw):
        raise OSError("no nextflow")

    monkeypatch.setattr(H.subprocess, "run", broken)
    monkeypatch.setenv("DT_RUN_ID", "run-2")
    events.clear()
    assert H.head(prof, nf_dir, launch) == 0
    assert ("INSERT INTO scan_run_dags" in events, "nextflow run" in events) == (False, True)


def test_head_publishes_cache_hits_while_nextflow_is_running(tmp_path, monkeypatch):
    from dt_cloud import run_dag

    launch = tmp_path / 'launch'
    profile = tmp_path / 'p.yml'
    profile.write_text(f'deployment: fix\nexecutor: {{kind: local}}\nwork: {tmp_path}/nf\ndata: {tmp_path}/data\n'
                       "layout: {layer2: 'l2/{scan}/', snapshots: ''}\nsources: [s3://a]\n"
                       'skip: [static-names, digest, interval-store, warm-cache, probe]\n'
                       'options: {list: {import: true}}\nenv: {SCAN_RUNS_PROFILE: unused}\n')
    monkeypatch.setenv('DT_RUN_ID', 'run-cache')
    monkeypatch.delenv('REPROC', raising=False)
    monkeypatch.delenv('BATCH_JOB_UID', raising=False)
    monkeypatch.setattr(H, 'store_preview_dag', lambda *args: (launch / H.PREVIEW_DAG).write_text('flowchart TB\n    v2(["index"])\n'))
    writes = []

    def record(cmd, args):
        if cmd.name == 'dag':
            writes.append((args[0], run_dag.read_file('nextflow', Path(args[1])).cached_tasks))

    def popen(argv, **kwargs):
        trace = launch / 'trace.tsv'
        trace.write_text('name\tstatus\nindex\tCACHED\n')
        calls = []

        def wait(timeout):
            assert timeout == 10
            calls.append(timeout)
            if len(calls) < 3:
                raise H.subprocess.TimeoutExpired(argv, timeout)
            return 0

        return SimpleNamespace(pid=0, stdout=BytesIO(), wait=wait)

    monkeypatch.setattr(H, '_record', record)
    monkeypatch.setattr(H, 'publish_reports', lambda *args: [])
    monkeypatch.setattr(H, 'echo_task_logs', lambda *args: 0)
    monkeypatch.setattr(H.subprocess, 'Popen', popen)
    assert H.head(profile, NF, launch) == 0
    assert writes == [('run-cache', ('index',))]


def test_publish_reports(tmp_path):
    """The reports that exist go to `<work>/runs/<scan>/`; a missing one is skipped."""
    launch, work = tmp_path / "launch", tmp_path / "work"
    launch.mkdir()
    (launch / "dag.mmd").write_text("flowchart TB\n")
    (launch / "trace.tsv").write_text("task_id\n")
    profile = SimpleNamespace(work=str(work))
    assert H.publish_reports(profile, "2026-10-16T1200", launch) == ["runs/2026-10-16T1200/dag.mmd", "runs/2026-10-16T1200/trace.tsv"]
    assert sorted(p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file()) == [
        "runs/2026-10-16T1200/dag.mmd", "runs/2026-10-16T1200/trace.tsv"]
    assert (work / "runs/2026-10-16T1200/dag.mmd").read_text() == "flowchart TB\n"


def test_head_publishes_independent_failed_and_resumed_ownership_receipts(tmp_path, monkeypatch):
    from uuid import UUID

    prof, launch, work = tmp_path / 'profile.yml', tmp_path / 'launch', tmp_path / 'nf'
    attempts = iter([UUID(int=1), UUID(int=2), UUID(int=3)])
    monkeypatch.setattr(H.uuid, 'uuid4', lambda: next(attempts))
    codes = iter([1, 0, 0])
    task_dir = work / 'work' / 'ab' / ('c' * 30)

    def popen(argv, **kwargs):
        code = next(codes)
        (launch / 'trace.tsv').write_text('task_id\thash\tnative_id\tname\tstatus\texit\tworkdir\n'
                                          f'1\tab/cccccc\t-\tindex\t{"FAILED" if code else "CACHED"}\t{code}\t{task_dir}\n')
        return SimpleNamespace(pid=0, stdout=BytesIO(), wait=lambda timeout: code)

    monkeypatch.setattr(H.subprocess, 'Popen', popen)
    for store, run in [('primary', 'run-1'), ('meta', 'run-1'), ('primary', 'run-2')]:
        prof.write_text(f'deployment: fix\nstore: {store}\nexecutor: {{kind: local}}\nwork: {work}\ndata: {tmp_path}/data\n'
                        "layout: {layer2: 'l2/{scan}/', snapshots: ''}\nsources: [s3://a]\n"
                        'skip: [static-names, digest, interval-store, warm-cache, probe]\noptions: {list: {import: true}}\n')
        env = {**LABELS, 'DT_RUN_ID': run, 'SNAP_ID': '2026-11-09T1200', 'GEN': 'g1', 'DT_RECORD': 'off'}
        assert H.head(prof, NF, launch, env=env) == (1 if (store, run) == ('primary', 'run-1') else 0)
    receipts = [json.loads(p.read_text()) for p in sorted((work / 'ownership').rglob('receipt.json'))]
    assert [(d['identity']['store'], d['identity']['run_id'], d['identity']['head_attempt'], d['outcome']['status'],
             d['trace']['state'], d['trace']['tasks'][0]['cached']) for d in receipts] == [
        ('meta', 'run-1', '00000000-0000-0000-0000-000000000002', 'succeeded', 'observed', True),
        ('primary', 'run-1', '00000000-0000-0000-0000-000000000001', 'failed', 'observed', False),
        ('primary', 'run-2', '00000000-0000-0000-0000-000000000003', 'succeeded', 'observed', True),
    ]
    assert [d['trace']['workdirs'][0]['uri'] for d in receipts] == [str(task_dir)] * 3
    assert [d['deletion_authorized'] for d in receipts] == [False, False, False]
    assert sorted(p.name for p in (work / 'runs/2026-11-09T1200').iterdir()) == ['trace.tsv']


def test_echo_task_logs(tmp_path):
    """Each traced task's log under its header, in trace order; a task whose work dir has no log says so."""
    work = tmp_path / "work"
    for d, text in (("ab/cdef0123", "listed 3 shards\n"), ("12/345678ff", "retire: 22 scans\nkept 7\n")):
        (work / d).mkdir(parents=True)
        (work / d / ".command.log").write_text(text)
    (work / "9f/00000000").mkdir(parents=True)
    trace = tmp_path / "trace.tsv"
    trace.write_text("task_id\thash\tname\tstatus\n1\tab/cdef01\tlist_source (r2://b)\tCOMPLETED\n"
                     "2\t12/345678\tpost\tCOMPLETED\n3\t9f/000000\tdigest\tFAILED\n")
    lines: list[str] = []
    assert H.echo_task_logs(trace, work, lines.append) == 2
    assert lines == [
        "--- task list_source (r2://b) [ab/cdef01] COMPLETED", "listed 3 shards",
        "--- task post [12/345678] COMPLETED", "retire: 22 scans\nkept 7",
        "--- task digest [9f/000000] FAILED (no log)",
    ]
    assert H.echo_task_logs(tmp_path / "missing.tsv", work, lines.append) == 0


def test_finish_args(tmp_path):
    """The DAG goes to `finish -D` only when Nextflow wrote it."""
    dag = tmp_path / "dag.mmd"
    assert H.finish_args(1, "nextflow exit 1", ["-l", "nf-session=s"], dag, "uid1") == ["-r", "uid1", "-x", "1", "-e", "nextflow exit 1", "-l", "nf-session=s"]
    dag.write_text("flowchart TB\n")
    assert H.finish_args(0, None, [], dag) == ["-x", "0", "-D", str(dag)]


def test_run_config_is_a_groovy_env_scope():
    """The run id and the profile JSON reach every task through the head's `run.config` `env` scope (out of the task
    hash, so a resume from a new head job keeps the cache): the JSON base64'd (`profile_env_value`), a run id that
    can't need quoting."""
    import base64

    profile_json = '{"a": "it\'s", "b": "x\\\\y\\n"}'
    assert H.run_config("oa-r2-scan-manual-c51e504f-ecbc-495200", profile_json) == (
        "env {\n"
        "    DT_RUN_ID = 'oa-r2-scan-manual-c51e504f-ecbc-495200'\n"
        "    SCAN_RUN_ID = 'oa-r2-scan-manual-c51e504f-ecbc-495200'\n"
        f"    DT_SCAN_PROFILE_JSON = 'base64:{base64.b64encode(profile_json.encode()).decode()}'\n"
        "}\n"
    )
    assert H.run_config("", profile_json).split("\n")[1] == "    DT_RUN_ID = ''"  # a local run that isn't recorded
    with pytest.raises(ValueError) as e:
        H.run_config('run "1"', profile_json)
    assert str(e.value) == "run id 'run \"1\"': Nextflow exports it into each task's script unescaped"


def test_profile_json_survives_nextflows_task_export(tmp_path):
    """Nextflow writes an `env` scope value into each task's `.command.run` as `export NAME="<value>"`, inner double
    quotes unescaped: raw JSON reached the task as `{data:…}` and every task failed (r2's manual scan 2026-10-18T1626).
    The value the head puts there must come through that export, and the task decode it to the same profile."""
    import subprocess

    from dt_cloud import scan_steps as S

    profile_json = '{"deployment": "d", "say": "it\'s \\"q\\" $HOME `x` \\\\ \\n"}'
    run_id = "oa-r2-scan-manual-c51e504f-ecbc-495200"
    value = S.profile_env_value(profile_json)
    exported = subprocess.run(["bash", "-c", f'export X="{value}" R="{run_id}"; printf "%s\\n%s" "$X" "$R"'],
                              capture_output=True, text=True, check=True).stdout.split("\n")
    assert exported[1] == run_id
    assert S.profile_env_json({S.PROFILE_JSON_ENV: exported[0]}) == profile_json
    assert S.profile_env_json({S.PROFILE_JSON_ENV: profile_json}) == profile_json  # a hand-set raw JSON still reads


def test_reproc_still_fetches_an_adopted_listing():
    """REPROC runs from `index`, but a source adopting its listing (`sources[].listing`) keeps its `list` task: on a
    local data dir the shards have no other copy. Without one, `list` is skipped as before."""
    from dt_cloud.scan_profile import parse

    def prof(sources):
        return parse({"deployment": "fix", "executor": {"kind": "local"}, "work": "/w", "data": "/d",
                      "layout": {"layer2": "l2/{scan}/", "snapshots": "", "listing": "cap/{scan}/"}, "sources": sources,
                      "skip": ["digest", "static-names"]})

    adopted = prof([{"uri": "r2://disk-tree", "listing": "{env:DT_CAPTURE}"}, "r2://other"])
    listed = prof(["r2://disk-tree"])
    got = [H.params(p, "{}", "2026-10-16", "G", "1", "", "S", LABELS, reproc=r)["sources"]
           for p, r in ((adopted, True), (adopted, False), (listed, True))]
    assert got == [
        [{"uri": "r2://disk-tree", "machine": None, "disk": None}],
        [{"uri": "r2://disk-tree", "machine": None, "disk": None}, {"uri": "r2://other", "machine": None, "disk": None}],
        [],
    ]


class FakeBackend(Backend):
    """A backend that runs nothing: it logs each call the head makes; `wait` ends the run on its second call with `rc`,
    or (`term`) sends the head a TERM on its first."""
    engine = "fake"

    def __init__(self, rc: int, term: bool = False):
        self.rc, self.term, self.calls, self.waits = rc, term, [], 0

    def prepare(self, run):
        self.calls.append(("prepare", run.scan, run.session, run.dag.names[0]))
        return ["fake", "run"]

    def store_preview(self, run):
        self.calls.append(("store_preview",))

    def start(self, run):
        self.calls.append(("start",))

    def wait(self, timeout):
        self.waits += 1
        if self.term and self.waits == 1:
            os.kill(os.getpid(), signal.SIGTERM)
        return None if self.waits < 2 else 0 if self.term else self.rc

    def progress(self, run):
        return None

    def interrupt(self):
        self.calls.append(("interrupt",))

    def cancel(self, run):
        self.calls.append(("cancel",))

    def error(self, run, rc):
        return f"fake exit {rc}"

    def finish(self, run, rc, signal):
        self.calls.append(("finish", rc, signal))

    def cost(self, run):
        return ["-l", "fake=1"]

    def dag_file(self, run):
        return run.launch / "fake.dag"


def _drive(tmp_path, monkeypatch, fake: FakeBackend) -> tuple[int, list[tuple[str, list[str]]]]:
    """`scan head` over `fake` on a one-source profile: its exit code and what it recorded."""
    prof = tmp_path / "p.yml"
    prof.write_text(f"deployment: fix\nexecutor: {{kind: local}}\nwork: {tmp_path}/nf\ndata: {tmp_path}/data\n"
                    "layout: {layer2: 'l2/{scan}/', snapshots: ''}\nsources: [s3://a]\noptions: {list: {import: true}}\n"
                    "skip: [static-names, digest, interval-store, warm-cache, probe]\n")
    monkeypatch.setattr(H, "backend_for", lambda profile, nf_dir: fake)
    records: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(H, "_record", lambda cmd, args: records.append((cmd.name, args)))
    env = {**LABELS, "DT_RUN_ID": "run-1", "SNAP_ID": "2026-10-16T1200", "GEN": "G1", "DT_JOB_START": "1",
           "SCAN_RUNS_PROFILE": str(tmp_path / "scan-runs.json"), "DT_SUPERVISE": "1"}
    return H.head(prof, NF, tmp_path / "launch", env=env), records


@pytest.mark.parametrize("rc, term, calls, finish", [
    (0, False, [("finish", 0, None)], ["-r", "run-1", "-x", "0", "-l", "fake=1"]),
    (3, False, [("finish", 3, None)], ["-r", "run-1", "-x", "3", "-e", "fake exit 3", "-l", "fake=1"]),
    (0, True, [("interrupt",), ("cancel",), ("finish", 143, signal.SIGTERM)],
     ["-r", "run-1", "-x", "143", "-e", f"head terminated (signal; session {H.session_id('fix', '2026-10-16T1200')})", "-l", "fake=1"]),
], ids=["ok", "failed", "terminated"])
def test_head_drives_its_backend(tmp_path, monkeypatch, rc, term, calls, finish):
    """specs/orchestrator-abstraction.md §3: the head prepares, stores the preview, starts and waits on its backend; a
    TERM interrupts the run, then cancels what it left; the backend's error, cost and DAG file go on the finish record."""
    fake = FakeBackend(rc, term)
    got, records = _drive(tmp_path, monkeypatch, fake)
    assert got == (143 if term else rc)
    session = H.session_id("fix", "2026-10-16T1200")
    assert fake.calls == [("prepare", "2026-10-16T1200", session, "list_source"), ("store_preview",), ("start",), *calls]
    assert [(n, a) for n, a in records if n != "start"] == [("finish", finish)]


class CountingBackend(FakeBackend):
    """`FakeBackend` whose status counts the waits: a task ends with each, the last as the run exits."""

    def status(self, run):
        return f"{self.waits} done"


def test_the_finish_stage_counts_the_tasks_that_ended_as_the_run_exited(tmp_path, monkeypatch):
    """The run's last task ends, and the engine exits, within one `wait`: the liveness line read the status before that
    wait ("1 done"), so the finish stage reads it again and carries the final count (gcs's 11-28 head: 12 of 13)."""
    details: list[str | None] = []
    monkeypatch.setattr(H, "set_detail", lambda d: details.append(d) or heartbeat.set_detail(d))
    assert _drive(tmp_path, monkeypatch, CountingBackend(0))[0] == 0
    assert (details, heartbeat.current_stage()[:2]) == (["0 done", "1 done", "2 done"], ("fake: finish", "2 done"))


class Tick:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_the_heads_liveness_detail_names_the_tasks_running():
    """The head's view of its Nextflow run (`NfTasks`): each task submitted (Nextflow's stdout) more times than the trace
    has ended it is running, oldest first (then by name), with the time since its latest submission; a retry runs again; cached and
    failed rows are counted; past three running, `+N more`."""
    clock = Tick()
    nf = H.NfTasks(clock)
    for t, line in [
        (0, "[ab/123456] Submitted process > list_source (s3://a)"),
        (0, "[ab/123457] Submitted process > list_source (s3://b)"),
        (300, "[cd/000001] Submitted process > index"),
        (300, "executor >  google-batch (3)"),
        (900, "[ef/000002] Submitted process > static_names"),
        (900, "[ef/000003] Submitted process > interval_store"),
        (960, "[aa/000004] Re-submitted process > index"),
        (1000, "[bb/000005] Submitted process > probe"),
    ]:
        clock.t = t
        nf.feed(line)
    clock.t = 3600
    trace = ("task_id\tname\tstatus\n1\tlist_source (s3://a)\tCOMPLETED\n2\tlist_source (s3://b)\tCOMPLETED\n"
             "3\tindex\tFAILED\n4\twarm_cache\tCACHED\n5\tdigest\tCACHED\n6\tprob")
    assert nf.status(trace) == "4 done (2 cached), 1 failed, running interval_store 45m, static_names 45m, index 44m, +1 more"
    assert H.NfTasks(clock).status("") == "0 done"
