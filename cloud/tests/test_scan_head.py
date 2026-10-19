"""`dt-cloud scan head`'s pure parts: identity, session, history line, params + executor config (golden files from
the cw-s3-shaped fixture profile), the trace's first failure, and a dry run's files."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from dt_cloud import scan_head as H
from dt_cloud.cli import main
from dt_cloud.scan_profile import load

FIX = Path(__file__).parent / "fixtures" / "scan_profiles"
NOW = dt.datetime(2026, 10, 16, 12, 0, 1, tzinfo=dt.timezone.utc)


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
                   H.session_id("example", "2026-10-16T1200", "meta"), {})
    assert got == json.loads((FIX / "meta-store.params.json").read_text())


def test_history_line():
    assert H.history_line(NOW, "2026-10-16T1200", "S") == "2026-10-16 12:00:01\t-\tscan_2026_10_16T1200\tOK\t-\tS\tnextflow run scan.nf\n"


def test_executor_config_golden():
    assert H.executor_config(load(FIX / "cw-s3.yml")) == (FIX / "cw-s3.executor.config").read_text()


def test_params_golden():
    p = load(FIX / "cw-s3.yml")
    got = H.params(p, "<profile>", "2026-10-16T1200", "20261016T120001Z", "1792152001", "run-1",
                   H.session_id("cw-s3", "2026-10-16T1200"), {"DT_RECORD": "off", "DISKY_LABELS": "stack=prod,team=x", "OTHER": "1"})
    assert got == json.loads((FIX / "cw-s3.params.json").read_text())


def test_params_reproc_fresh_and_extras():
    from dt_cloud.scan_profile import parse

    p = parse({"deployment": "fix", "executor": {"kind": "local"}, "work": "/w", "data": "/d",
               "layout": {"layer2": "l2/{scan}/", "snapshots": ""}, "sources": ["gs://a"],
               "steps": ["access-ingest", "healthcheck"], "skip": ["digest", "static-names"],
               "options": {"list": {"import": True}}})
    got = H.params(p, "{}", "2026-10-16", "G", "1", "", "S", {}, reproc=True, fresh=True)
    assert {k: got[k] for k in ("steps", "pre", "pre_label", "post", "over_time", "over_time_label", "skip_list", "cache", "env",
                                "work_bucket", "secrets_project")} == {
        "steps": ["bucket-check", "access-ingest", "list", "index", "publish-r2", "interval-store", "index-rows", "index-sync",
                  "over-time", "index-gc", "healthcheck", "warm-cache", "probe"],
        "pre": ["access-ingest"],
        "pre_label": "small",
        "post": ["index-gc", "healthcheck", "warm-cache", "probe"],
        "over_time": True,
        "over_time_label": "post",
        "skip_list": True,
        "cache": False,
        "env": {"REPROC": "1"},
        "work_bucket": "",
        "secrets_project": "",
    }


@pytest.mark.parametrize("poller, steps, pre", [
    (False, ["bucket-check", "access-ingest", "list", "index", "index-rows", "index-sync"], ["access-ingest"]),
    (True, ["bucket-check", "list", "index", "index-rows", "index-sync"], []),
])
def test_access_ingest_poller_leaves_the_scan(poller: bool, steps: list[str], pre: list[str]):
    """`options.access-ingest.poller` (specs/access-log-freshness.md, Order 2): the deployment's hourly poller runs the
    ingest, so the scan has no `pre` task and `index` waits on the lists only, reading `options.index.access` as it
    stands. Off (the default), the step runs in `pre` as before."""
    from dt_cloud.scan_profile import parse

    p = parse({"deployment": "fix", "executor": {"kind": "local"}, "work": "/w", "data": "gs://d",
               "layout": {"layer2": "l2/{scan}/", "snapshots": "", "listing": "l2/{scan}/"}, "sources": ["gs://a"],
               "steps": ["access-ingest"],
               "skip": ["static-names", "publish-r2", "interval-store", "over-time", "index-gc", "warm-cache", "probe", "digest"],
               "options": {"access-ingest": {"args": ["-P", "4"], "poller": poller},
                           "index": {"mode": "path-index", "access": "access/agg/*/*.parquet"}}})
    got = H.params(p, "{}", "2026-10-18T0700", "G", "1", "", "S", {})
    assert (got["steps"], got["pre"], p.opts("index")["access"]) == (steps, pre, "access/agg/*/*.parquet")


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


def test_dry_run_writes_the_launch_files(tmp_path, monkeypatch):
    for k in ("SNAP_ID", "GEN", "DT_JOB_START", "DT_RUN_ID", "BATCH_JOB_UID", "SCAN_RUNS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SNAP_ID", "2026-10-16T1200")
    monkeypatch.setenv("GEN", "G1")
    monkeypatch.setenv("DT_JOB_START", "100")
    launch = tmp_path / "launch"
    r = CliRunner().invoke(main, ["scan", "head", "-n", "-N", str(tmp_path / "nf"), "-w", str(launch), str(FIX / "cw-s3.yml")])
    assert r.exit_code == 0, r.output
    session = H.session_id("cw-s3", "2026-10-16T1200")
    assert sorted(p.name for p in launch.iterdir()) == [".nextflow", "executor.config", "params.json", "run.config"]
    assert (launch / "executor.config").read_text() == (FIX / "cw-s3.executor.config").read_text()
    params = json.loads((launch / "params.json").read_text())
    assert (params["scan"], params["gen"], params["job_start"], params["session"]) == ("2026-10-16T1200", "G1", "100", session)
    assert json.loads(params["profile_json"])["deployment"] == "cw-s3"
    assert (launch / "run.config").read_text() == H.run_config(params["run_id"], params["profile_json"])
    line = (launch / ".nextflow" / "history").read_text().split("\t")
    assert line[1:] == ["-", "scan_2026_10_16T1200", "OK", "-", session, "nextflow run scan.nf\n"]
    assert r.stderr.rstrip().split("\n")[-1] == (
        f"+ nextflow run {tmp_path}/nf/scan.nf -c {tmp_path}/nf/base.config -c {launch}/executor.config -c {launch}/run.config "
        f"-params-file {launch}/params.json -resume {session} -with-trace {launch}/trace.tsv "
        f"-with-dag {launch}/dag.mmd -with-timeline {launch}/timeline.html -with-report {launch}/report.html"
    )


def test_head_records_into_the_profiles_database_and_store(tmp_path, monkeypatch):
    """`head()` with `D1_DB_ID` (and `SCAN_RUNS_PROFILE`) only in the profile's `env` (cw's first prod run on the generic
    head: its own start / finish found no database), and a non-primary `store`: both writes go to the profile's D1, the
    run filed under the store. Nextflow is a fake process that exits 0."""
    from dt_cloud import index_footer, scan_runs

    for k in ("D1_DB_ID", "SCAN_RUNS_PROFILE", "DT_RECORD", "BATCH_JOB_UID", "DT_RUN_ID", "SNAP_ID", "GEN", "DT_JOB_START",
              "REPROC", "DISKY_LABELS", "DT_TRIGGER"):
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
    monkeypatch.setattr(H.subprocess, "Popen", lambda argv, **kw: SimpleNamespace(pid=0, wait=lambda: 0))
    assert H.head(prof, tmp_path / "nf-dir", tmp_path / "launch") == 0
    assert (runs_written[0], runs_written[-1][:2], set(dbs)) == (
        ("2026-10-16T1200", "meta", "running", "schedule"), ("2026-10-16T1200", "meta"), {"db-from-profile"})
    # a resume by hand: its run is manual (the scan stays the cron's: its first run's, `digest_cw.scan_triggers`)
    monkeypatch.setenv("DT_TRIGGER", "manual")
    monkeypatch.setenv("DT_RUN_ID", "run-2")
    runs_written.clear()
    assert H.head(prof, tmp_path / "nf-dir", tmp_path / "launch") == 0
    assert runs_written[0] == ("2026-10-16T1200", "meta", "running", "manual")
    # a deployment that doesn't state its triggers: none recorded (the digest goes by the clock)
    monkeypatch.delenv("DT_TRIGGER")
    monkeypatch.setenv("DT_RUN_ID", "run-3")
    monkeypatch.setenv("SNAP_ID", "2026-10-16T1300")
    runs_written.clear()
    assert H.head(prof, tmp_path / "nf-dir", tmp_path / "launch") == 0
    assert runs_written[0] == ("2026-10-16T1300", "meta", "running", None)


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
    got = [{k: H.params(p, "{}", "2026-10-16", "G", "1", "", "S", {}, reproc=r)[k] for k in ("sources", "skip_list")}
           for p, r in ((adopted, True), (adopted, False), (listed, True))]
    assert got == [
        {"sources": [{"uri": "r2://disk-tree", "machine": None, "disk": None}], "skip_list": False},
        {"sources": [{"uri": "r2://disk-tree", "machine": None, "disk": None}, {"uri": "r2://other", "machine": None, "disk": None}],
         "skip_list": False},
        {"sources": [], "skip_list": True},
    ]


def test_a_local_data_dir_orders_static_names_after_publish():
    """On a local data dir `static-names` reads earlier scans from the serving store, so `scan.nf` runs it after this
    scan's publish (`static_after_publish`); on a GCS one it overlaps publish as before."""
    from dt_cloud.scan_profile import parse

    def prof(data, skip):
        return parse({"deployment": "fix", "executor": {"kind": "local"}, "work": "/w", "data": data,
                      "layout": {"layer2": "l2/{scan}/", "snapshots": "", "listing": "cap/{scan}/"}, "sources": ["r2://b"],
                      "skip": ["digest", *skip], "options": {"static-names": {"gen": "g"}}})
    got = [H.params(prof(d, skip), "{}", "2026-10-16", "G", "1", "", "S", {})["static_after_publish"]
           for d, skip in (("/d", []), ("gs://dvx", []), ("/d", ["publish-r2"]))]
    assert got == [True, False, False]
