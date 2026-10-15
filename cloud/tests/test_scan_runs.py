"""`dt-cloud scan-run`: the record's SQL path (against the real schema, FKs on), output measuring, and the
backfill's reconstruction from fake Batch jobs, log lines and a fake store (specs/done/scan-runs-ui.md)."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from click.testing import CliRunner

from dt_cloud import scan_runs
from dt_cloud.d1_schema import table_ddl

from dt_cloud.scan_runs import (
    ALL, Output, Phase, PrintSink, Record, Run, SqliteSink, cli, cost, env_run_id, fill, load_profile, measure, measure_after,
    parse_profile, record, record_sql, recovery_record, write_all,
)
from dt_cloud.scan_runs_backfill import classify, failure_of, log_filter, phases_of, reconstruct, task_lines

DDL = table_ddl(scan_runs.TABLES)  # the tables as `site/schema.sql` has them


def ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp())


def test_fill_static_gen_follows_the_profile_file_and_its_env_override(monkeypatch, tmp_path):
    """`{static_gen:<file>}`: the deployment's own profile file (relative to the scan-run profile's dir, anchored at
    load so any working dir resolves it), `STATIC_NAMES_GEN` over it; an absolute path is kept as is."""
    job = tmp_path / "job"
    job.mkdir()
    (job / "static-names.json").write_text(json.dumps({"name": "cw", "gen": "2026-10-10cw"}))
    (job / "scan-runs.json").write_text(json.dumps({"outputs": [
        {"key": "static", "uri": "gs://d/static-names/{static_gen:static-names.json}/manifests/{scan}.json", "manifest": True},
        {"key": "abs", "uri": f"gs://d/{{static_gen:{job}/static-names.json}}/"},
        {"key": "named", "uri": "gs://d/{static_gen:cw}/"},
    ]}))
    monkeypatch.chdir(tmp_path)  # not the profile's dir: the relative file is still found
    p = load_profile(str(job / "scan-runs.json"))
    assert [o.uri for o in p.outputs] == [
        f"gs://d/static-names/{{static_gen:{job.resolve()}/static-names.json}}/manifests/{{scan}}.json",
        f"gs://d/{{static_gen:{job}/static-names.json}}/",
        "gs://d/{static_gen:cw}/",
    ]
    assert [fill(o.uri, "2026-10-10T1201", None) for o in p.outputs[:2]] == [
        "gs://d/static-names/2026-10-10cw/manifests/2026-10-10T1201.json",
        "gs://d/2026-10-10cw/",
    ]
    monkeypatch.setenv("STATIC_NAMES_GEN", "2026-11-01cw")
    assert fill(p.outputs[0].uri, "2026-10-10T1201", None) == "gs://d/static-names/2026-11-01cw/manifests/2026-10-10T1201.json"
    # An example names no live generation: only `STATIC_NAMES_GEN` gives it one.
    assert fill(p.outputs[2].uri, "2026-10-10T1201", None) == "gs://d/2026-11-01cw/"
    monkeypatch.delenv("STATIC_NAMES_GEN")
    with pytest.raises(SystemExit) as e:
        fill(p.outputs[2].uri, "2026-10-10T1201", None)
    assert str(e.value) == "static names: no gen in the deployment profile: set STATIC_NAMES_GEN (or STATIC_NAMES_PROFILE)"


class FakeStore:
    """Objects by full URI → bytes; `.groups.parquet` row counts by URI."""

    def __init__(self, objects: dict[str, bytes | int], rows: dict[str, int] | None = None):
        self.objects = objects
        self.rows = rows or {}

    def list(self, uri):
        return sorted((u, v if isinstance(v, int) else len(v)) for u, v in self.objects.items() if u.startswith(uri))

    def read(self, uri):
        v = self.objects.get(uri)
        if not isinstance(v, bytes):
            raise FileNotFoundError(uri)
        return v

    def last_row_end(self, uri):
        return self.rows[uri]


PROFILE = parse_profile({
    "name": "test",
    "project": "proj",
    "regions": ["r1"],
    "jobs": [
        {"kind": "reproc", "where": {"entrypoint": "^$", "env": ["REPROC"]}, "scan": [{"env": "SNAPSHOT_DATE"}]},
        {"kind": "scan", "where": {"name": "^(job-|snap-)", "entrypoint": "^$", "no_env": ["REPROC"]},
         "scan": [{"env": "SNAP_ID"}, {"log": "^DONE (\\S+)"}, {"created": "%Y-%m-%d"}]},
        {"kind": "listing", "downstream": True, "where": {"commands": "bulk-list"},
         "scan": [{"commands": "/listing/([0-9T-]+)/"}]},
    ],
    "overlapped": ["ingest"],
    "concurrent": ["names", "iv"],
    "nop_marker": "^NOP ",
    "error_marker": [  # the old trap's shape, then the wrapper's `scan_err` function
        "^\\+\\+ fail_alert (?P<rc>\\d+) (?P<line>\\d+) (?P<cmd>.*)",
        "^\\+\\+ scan_err (?P<rc>\\d+) (?P<line>\\d+) (?P<cmd>.*)",
    ],
    "outputs": [
        {"key": "listing", "uri": "gs://data/listing/{scan}/", "exclude": ["index/"], "split": "dir",
         "rows": {"json": "_SUCCESS.json", "field": "objects"}, "after": "listing", "kinds": ["scan"]},
        {"key": "index", "uri": "gs://data/listing/{scan}/index/{gen}/", "split": "stem", "rows": "groups", "after": "index"},
        {"key": "snapshot", "uri": "gs://data/snapshots/{scan}/", "after": "publish"},
        {"key": "static", "uri": "gs://data/static/g1/manifests/{scan}.json", "manifest": True, "kinds": ["scan"]},
    ],
    "prices": {"m-32": 2.0, "m-8": 0.5},
    "spot_factor": 0.25,
})

STORE = FakeStore({
    "gs://data/listing/2026-10-08/b1/part-0.parquet": 100,
    "gs://data/listing/2026-10-08/b1/_SUCCESS.json": json.dumps({"objects": 7}).encode(),
    "gs://data/listing/2026-10-08/b2/part-0.parquet": 300,
    "gs://data/listing/2026-10-08/b2/_SUCCESS.json": json.dumps({"objects": 5}).encode(),
    "gs://data/listing/2026-10-08/index/20261008T070217Z/path-index.parquet": 1000,
    "gs://data/listing/2026-10-08/index/20261008T070217Z/path-index.groups.parquet": 40,
    "gs://data/listing/2026-10-08/index/20261008T070217Z/path-index.groups.json": 60,
    "gs://data/listing/2026-10-08/index/20261008T070217Z/attr.tsv": 9,
    "gs://data/listing/2026-10-08/index/20261008T070217Z/": 0,
    "gs://data/listing/2026-10-08/index/20261008T120000Z/path-index.parquet": 900,
    "gs://data/listing/2026-10-08/index/20261008T120000Z/path-index.groups.parquet": 30,
    "gs://data/snapshots/2026-10-08/meta.json": 20,
    "gs://data/snapshots/2026-10-08/age.json": 30,
    "gs://data/static/g1/manifests/2026-10-08.json": json.dumps(
        {"gen": "g1", "runs": [{"key": "deltas/2026-10-08", "rows": 44, "bytes": 1122}]}).encode(),
}, rows={
    "gs://data/listing/2026-10-08/index/20261008T070217Z/path-index.groups.parquet": 6000,
    "gs://data/listing/2026-10-08/index/20261008T120000Z/path-index.groups.parquet": 5999,
})


def sink() -> SqliteSink:
    return SqliteSink(":memory:", ddl=DDL)


def rows(s: SqliteSink, sql: str) -> list[tuple]:
    return [tuple(r.values()) for r in s.query(sql)]


# ── record: the job's incremental writes ────────────────────────────────────


@pytest.mark.parametrize("note,expected", [
    (None, "done"),
    ("failed (exit 124)", "failed"),
    ("wall, failed (exit 1", "failed"),
    ("wall, failed", "failed"),
    ("0 failed dirs", "done"),
])
def test_phase_failure_in_live_and_backfilled_records(note, expected):
    s = sink()
    record(s, PROFILE, "2026-10-08", "uid-phase", 100, start=True)
    rec = record(s, PROFILE, "2026-10-08", "uid-phase", 200, phase="names", note=note)
    assert [(p.status, p.note) for p in rec.phases] == [(expected, note)]
    marker = "PHASE names: 100s" + (f" ({note})" if note else "")
    assert [(p.status, p.note) for p in phases_of(PROFILE, [(200, marker)], 100)] == [(expected, note.rstrip(")") if note else None)]


def recovery_fixture():
    s = sink()
    record(s, PROFILE, "2026-10-08", "original", 100, start=True)
    record(s, PROFILE, "2026-10-08", "original", 200, phase="names", note="failed (exit 124)")
    record(s, PROFILE, "2026-10-08", "original", 210, exit_code=0)
    job = {
        "uid": "recovery", "name": "projects/proj/locations/r1/jobs/retry-names",
        "status": {"state": "SUCCEEDED", "statusEvents": [
            {"description": "Job state is set to RUNNING", "eventTime": "1970-01-01T00:05:00Z"},
            {"description": "Job state is set to SUCCEEDED", "eventTime": "1970-01-01T00:06:00Z"},
        ]},
    }
    return s, job


def test_recovery_links_verified_batch_facts_without_rewriting_original():
    s, job = recovery_fixture()
    original = s.query("SELECT * FROM scan_runs WHERE run_id = 'original'")
    original_phase = s.query("SELECT * FROM scan_run_phases WHERE run_id = 'original'")
    for now in (400, 500):
        rec = recovery_record(s, PROFILE, "original", "names", job, now, "Resumed anchors; reused completed append and drill")
        write_all(s, [rec], now)
    assert rows(s, "SELECT run_id, parent, scan, kind, status, started_ts, finished_ts, job_name, region FROM scan_runs WHERE run_id = 'recovery'") == [
        ("recovery", "original", "2026-10-08", "phase-recovery", "succeeded", 300, 360, "retry-names", "r1"),
    ]
    assert rows(s, "SELECT run_id, phase, started_ts, finished_ts, status, note FROM scan_run_phases WHERE run_id = 'recovery'") == [
        ("recovery", "names", 300, 360, "done", "Resumed anchors; reused completed append and drill"),
    ]
    assert s.query("SELECT * FROM scan_runs WHERE run_id = 'original'") == original
    assert s.query("SELECT * FROM scan_run_phases WHERE run_id = 'original'") == original_phase


@pytest.mark.parametrize("state,expected", [("FAILED", "failed"), ("CANCELLED", "failed")])
def test_unsuccessful_recovery_is_not_marked_successful(state, expected):
    s, job = recovery_fixture()
    job["status"]["state"] = state
    job["status"]["statusEvents"][-1]["description"] = f"Job state is set to {state}"
    rec = recovery_record(s, PROFILE, "original", "names", job, 400)
    assert (rec.run.status, [p.status for p in rec.phases]) == (expected, ["failed"])


@pytest.mark.parametrize("case,error", [
    ("missing-parent", "scan-run recovery: no parent 'missing' in store 'primary'"),
    ("missing-phase", "scan-run recovery: parent has no failed phase 'other'"),
    ("running", "scan-run recovery: Batch attempt is not terminal ('RUNNING')"),
    ("no-times", "scan-run recovery: missing or invalid Batch attempt times"),
    ("conflict", "scan-run recovery: conflicting existing attempt 'original'"),
])
def test_recovery_refuses_invented_or_conflicting_attempts(case, error):
    s, job = recovery_fixture()
    parent, phase_name = "original", "names"
    if case == "missing-parent":
        parent = "missing"
    elif case == "missing-phase":
        phase_name = "other"
    elif case == "running":
        job["status"]["state"] = "RUNNING"
    elif case == "no-times":
        job["status"]["statusEvents"] = []
    elif case == "conflict":
        job["uid"] = "original"
    with pytest.raises(ValueError) as e:
        recovery_record(s, PROFILE, parent, phase_name, job, 400)
    assert str(e.value) == error


def test_record_start_phases_exit():
    s = sink()
    t0 = ts("2026-10-08T07:01:00")
    record(s, PROFILE, "2026-10-08", "uid-1", t0, store=STORE, start=True)
    record(s, PROFILE, "2026-10-08", "uid-1", t0 + 1500, store=STORE, phase="listing")
    record(s, PROFILE, "2026-10-08", "uid-1", t0 + 1510, store=STORE, phase="ingest", note="overlapped")
    record(s, PROFILE, "2026-10-08", "uid-1", t0 + 3000, store=STORE, phase="index", gen="20261008T070217Z")
    record(s, PROFILE, "2026-10-08", "uid-1", t0 + 3100, store=STORE, phase="publish")
    record(s, PROFILE, "2026-10-08", "uid-1", t0 + 3200, store=STORE, exit_code=0)
    assert rows(s, "SELECT run_id, scan, kind, status, started_ts - %d, finished_ts - %d, gen, source FROM scan_runs" % (t0, t0)) == [
        ("uid-1", "2026-10-08", "scan", "succeeded", 0, 3200, "20261008T070217Z", "job"),
    ]
    assert rows(s, "SELECT phase, seq, started_ts - %d, finished_ts - %d, status, note FROM scan_run_phases ORDER BY seq" % (t0, t0)) == [
        ("listing", 0, 0, 1500, "done", None),
        ("ingest", 1, 0, 1510, "done", "overlapped"),
        ("index", 2, 1500, 3000, "done", None),
        ("publish", 3, 3000, 3100, "done", None),
    ]
    assert rows(s, "SELECT key, uri, bytes, objects, rows, gen FROM scan_run_outputs ORDER BY key") == [
        ("index", "gs://data/listing/2026-10-08/index/20261008T070217Z/", 1109, 4, None, "20261008T070217Z"),
        ("index/attr", "gs://data/listing/2026-10-08/index/20261008T070217Z/attr", 9, 1, None, "20261008T070217Z"),
        ("index/path-index", "gs://data/listing/2026-10-08/index/20261008T070217Z/path-index", 1100, 3, 6000, "20261008T070217Z"),
        ("listing", "gs://data/listing/2026-10-08/", 428, 4, 12, None),
        ("listing/b1", "gs://data/listing/2026-10-08/b1/", 114, 2, 7, None),
        ("listing/b2", "gs://data/listing/2026-10-08/b2/", 314, 2, 5, None),
        ("snapshot", "gs://data/snapshots/2026-10-08/", 50, 2, None, None),
        ("static", "gs://data/static/g1/manifests/2026-10-08.json", 1122, 1, 44, "g1"),
        ("static/deltas/2026-10-08", "gs://data/static/g1/deltas/2026-10-08/", 1122, None, 44, "g1"),
    ]


def test_record_failure_keeps_phases_and_names_the_error():
    s = sink()
    record(s, PROFILE, "2026-10-08", "uid-2", 100, start=True)
    record(s, PROFILE, "2026-10-08", "uid-2", 200, phase="listing")
    record(s, PROFILE, "2026-10-08", "uid-2", 250, exit_code=1, failed_phase="index", error="exit 1: dt-cloud path-index (line 300)")
    assert rows(s, "SELECT status, started_ts, finished_ts, failed_phase, error FROM scan_runs") == [
        ("failed", 100, 250, "index", "exit 1: dt-cloud path-index (line 300)"),
    ]
    assert rows(s, "SELECT phase, started_ts, finished_ts FROM scan_run_phases") == [("listing", 100, 200)]


def test_record_phase_began_beside_the_sequence():
    s = sink()
    t0 = ts("2026-10-08T07:01:00")
    record(s, PROFILE, "2026-10-08", "uid-1", t0, store=STORE, start=True)
    record(s, PROFILE, "2026-10-08", "uid-1", t0 + 1500, store=STORE, phase="listing")
    record(s, PROFILE, "2026-10-08", "uid-1", t0 + 1900, store=STORE, phase="ingest", began=t0 + 1500)
    record(s, PROFILE, "2026-10-08", "uid-1", t0 + 2000, store=STORE, phase="index")
    assert rows(s, "SELECT phase, seq, started_ts - %d, finished_ts - %d FROM scan_run_phases ORDER BY seq" % (t0, t0)) == [
        ("listing", 0, 0, 1500),
        ("ingest", 1, 1500, 1900),
        ("index", 2, 1500, 2000),
    ]


def test_record_start_with_a_job_not_yet_running_keeps_the_start():
    """`scan_run -S -B` reads the Batch job right after it starts: its events may not show RUNNING yet, and the job's
    empty start must not clear the one `-S` stamps (the live cw runs had `started_ts` NULL)."""
    s = sink()
    job = {"name": "projects/p/locations/us-central1/jobs/job-x", "status": {"statusEvents": [
        {"description": "Job state is set from QUEUED to SCHEDULED", "eventTime": "2026-10-10T12:00:40Z"}]}}
    record(s, PROFILE, "2026-10-08", "uid-6", 1000, start=True, job=job)
    record(s, PROFILE, "2026-10-08", "uid-6", 1600, phase="listing")
    assert rows(s, "SELECT status, started_ts, job_name, region FROM scan_runs") == [("running", 1000, "job-x", "us-central1")]
    assert rows(s, "SELECT phase, started_ts, finished_ts FROM scan_run_phases") == [("listing", 1000, 1600)]


def test_record_nop_and_first_call_without_start():
    s = sink()
    record(s, PROFILE, "2026-10-08", "uid-3", 500, nop=True)
    assert rows(s, "SELECT status, started_ts, finished_ts FROM scan_runs") == [("nop", None, 500)]


def test_record_dry_run_prints_the_sql(capsys):
    record(PrintSink(), PROFILE, "2026-10-08", "uid-4", 7, start=True)
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "INSERT INTO scan_runs (run_id, scan, kind, status, source, store, parent, failed_phase, error, started_ts, "
        "finished_ts, job_name, region, machine, spot, tasks, image, cost_usd, gen, updated_ts) VALUES ('uid-4', "
        "'2026-10-08', 'scan', 'running', 'job', 'primary', NULL, NULL, NULL, 7, NULL, NULL, NULL, NULL, NULL, NULL, "
        "NULL, NULL, NULL, 7) ON CONFLICT(run_id) DO UPDATE SET scan = excluded.scan, kind = excluded.kind, status = "
        "excluded.status, store = excluded.store, updated_ts = excluded.updated_ts, parent = COALESCE(excluded.parent, "
        "scan_runs.parent), failed_phase = COALESCE(excluded.failed_phase, scan_runs.failed_phase), error = "
        "COALESCE(excluded.error, scan_runs.error), started_ts = COALESCE(excluded.started_ts, scan_runs.started_ts), "
        "finished_ts = COALESCE(excluded.finished_ts, scan_runs.finished_ts), job_name = COALESCE(excluded.job_name, "
        "scan_runs.job_name), region = COALESCE(excluded.region, scan_runs.region), machine = "
        "COALESCE(excluded.machine, scan_runs.machine), spot = COALESCE(excluded.spot, scan_runs.spot), tasks = "
        "COALESCE(excluded.tasks, scan_runs.tasks), image = COALESCE(excluded.image, scan_runs.image), cost_usd = "
        "COALESCE(excluded.cost_usd, scan_runs.cost_usd), gen = COALESCE(excluded.gen, scan_runs.gen);",
    ]


def test_backfill_upsert_keeps_the_jobs_source_and_quotes():
    s = sink()
    record(s, PROFILE, "2026-10-08", "uid-5", 10, start=True)
    rec = Record(Run("uid-5", "2026-10-08", "scan", "succeeded", source="backfill", finished_ts=99, job_name="job-x",
                     error=None), (Phase("it's", 0, 10, 20),), (Output("k", "gs://b/o'k/", 1, 1),))
    s.execute(record_sql(rec, 100))
    assert rows(s, "SELECT status, source, started_ts, finished_ts, job_name FROM scan_runs") == [("succeeded", "job", 10, 99, "job-x")]
    assert rows(s, "SELECT phase FROM scan_run_phases") == [("it's",)]
    assert rows(s, "SELECT uri FROM scan_run_outputs") == [("gs://b/o'k/",)]


def test_children_need_their_run():
    s = sink()
    with pytest.raises(Exception, match="FOREIGN KEY"):
        s.execute(["INSERT INTO scan_run_phases (run_id, phase, seq) VALUES ('nope', 'p', 0)"])


def test_bad_status_is_refused():
    with pytest.raises(ValueError, match="status 'done'"):
        record_sql(Record(Run("u", "2026-10-08", "scan", "done")), 0)


# ── measuring ───────────────────────────────────────────────────────────────


def test_absent_prefix_measures_nothing_and_gen_needs_a_gen():
    assert measure(PROFILE.outputs[2], "2026-10-07", None, STORE) == []
    assert measure(PROFILE.outputs[1], "2026-10-08", None, STORE) == []


def test_measure_after_filters_by_phase_and_kind():
    assert [o.key for o in measure_after(PROFILE, "listing", "reproc", "2026-10-08", None, STORE)] == []
    assert [o.key for o in measure_after(PROFILE, "publish", "reproc", "2026-10-08", None, STORE)] == ["snapshot"]
    assert [o.key for o in measure_after(PROFILE, None, "scan", "2026-10-08", None, STORE)] == ["static", "static/deltas/2026-10-08"]
    assert [o.key for o in measure_after(PROFILE, ALL, "reproc", "2026-10-08", None, STORE)] == ["snapshot"]


def test_cost():
    assert cost(PROFILE, "m-32", False, 1, 5400) == 3.0
    assert cost(PROFILE, "m-8", True, 4, 3600) == 0.5
    assert cost(PROFILE, "unknown", False, 1, 3600) is None


# ── backfill ────────────────────────────────────────────────────────────────


def job(name, uid, created, state, events, *, env=None, commands=(), entrypoint="", machine="m-32", tasks=1, region="r1"):
    return {
        "name": f"projects/proj/locations/{region}/jobs/{name}",
        "uid": uid,
        "createTime": created,
        "taskGroups": [{"taskCount": str(tasks), "parallelism": str(tasks), "taskSpec": {
            "runnables": [{"container": {"imageUri": "reg/img:latest", "entrypoint": entrypoint, "commands": list(commands)}}],
            "environment": {"variables": env or {}}}}],
        "allocationPolicy": {"instances": [{"policy": {"machineType": machine}}]},
        "status": {"state": state, "statusEvents": [
            {"description": f"Job state is set from {a} to {b} for job x.", "eventTime": t} for a, b, t in events]},
    }


RUNNING = ("SCHEDULED", "RUNNING")
JOBS = [
    job("job-a", "uid-a", "2026-10-08T07:00:00Z", "SUCCEEDED",
        [(*RUNNING, "2026-10-08T07:01:00Z"), ("RUNNING", "SUCCEEDED", "2026-10-08T08:01:00Z")]),
    job("job-l", "uid-l", "2026-10-08T07:02:00Z", "SUCCEEDED",
        [(*RUNNING, "2026-10-08T07:03:00Z"), ("RUNNING", "SUCCEEDED", "2026-10-08T07:30:00Z")],
        commands=["-c", "disk-tree bulk-list gcs://$b -o gs://data/listing/2026-10-08/$b"], entrypoint="/bin/bash",
        machine="m-8", tasks=2),
    job("snap-reproc", "uid-r", "2026-10-08T11:59:00Z", "SUCCEEDED",
        [(*RUNNING, "2026-10-08T12:00:00Z"), ("RUNNING", "SUCCEEDED", "2026-10-08T12:30:00Z")],
        env={"REPROC": "1", "SNAPSHOT_DATE": "2026-10-08"}),
    job("job-f", "uid-f", "2026-10-07T07:00:00Z", "FAILED",
        [(*RUNNING, "2026-10-07T07:01:00Z"), ("RUNNING", "FAILED", "2026-10-07T07:20:00Z")]),
    job("job-n", "uid-n", "2026-10-06T07:00:00Z", "SUCCEEDED",
        [(*RUNNING, "2026-10-06T07:01:00Z"), ("RUNNING", "SUCCEEDED", "2026-10-06T07:02:00Z")]),
    job("job-sweep", "uid-s", "2026-10-06T09:00:00Z", "SUCCEEDED", [], entrypoint="/bin/bash", commands=["-c", "sweep"]),
]


def log(uid, t, text):
    return {"timestamp": t, "labels": {"job_uid": uid}, "textPayload": text}


LOGS = [
    log("uid-a", "2026-10-08T07:30:00.5Z", "PHASE listing: 1740s (wall)"),
    log("uid-a", "2026-10-08T07:30:00.6Z", "PHASE ingest: 1740s (wall, overlapped the listing)"),
    log("uid-a", "2026-10-08T07:50:00Z", "PHASE index: 2940s (wall)"),
    log("uid-a", "2026-10-08T07:55:00Z", "+ echo 'PHASE publish: 3240s (wall)'"),
    log("uid-a", "2026-10-08T07:55:00Z", "PHASE publish: 3240s (wall)"),
    log("uid-a", "2026-10-08T08:00:00Z", "PHASE total: 3540s (wall)"),
    log("uid-a", "2026-10-08T08:00:01Z", "DONE 2026-10-08"),
    log("uid-r", "2026-10-08T12:20:00Z", "PHASE index: 1200s (wall)"),
    log("uid-f", "2026-10-07T07:19:00Z", "++ fail_alert 1 222 'dt-cloud job submit-listing -d 2026-10-07'"),
    log("uid-n", "2026-10-06T07:01:30Z", "NOP 2026-10-06 (already published)"),
]


def test_classify():
    assert [getattr(classify(PROFILE, j), "kind", None) for j in JOBS] == ["scan", "listing", "reproc", "scan", "scan", None]


def test_record_concurrent_phases():
    """Concurrent phases each start where the last serial phase ended; the next serial phase where the last of them did."""
    s = sink()
    t0 = ts("2026-10-08T07:01:00")
    record(s, PROFILE, "2026-10-08", "uid-c", t0, start=True)
    record(s, PROFILE, "2026-10-08", "uid-c", t0 + 1500, phase="listing")
    record(s, PROFILE, "2026-10-08", "uid-c", t0 + 1510, phase="ingest")
    record(s, PROFILE, "2026-10-08", "uid-c", t0 + 3000, phase="publish")
    record(s, PROFILE, "2026-10-08", "uid-c", t0 + 4000, phase="iv")
    record(s, PROFILE, "2026-10-08", "uid-c", t0 + 6000, phase="names")
    record(s, PROFILE, "2026-10-08", "uid-c", t0 + 6500, phase="sync")
    assert rows(s, "SELECT phase, seq, started_ts - %d, finished_ts - %d FROM scan_run_phases ORDER BY seq" % (t0, t0)) == [
        ("listing", 0, 0, 1500),
        ("ingest", 1, 0, 1510),
        ("publish", 2, 1500, 3000),
        ("iv", 3, 3000, 4000),
        ("names", 4, 3000, 6000),
        ("sync", 5, 6000, 6500),
    ]


def test_record_explicit_phase_starts_preserve_overlap_and_completion():
    s = sink()
    record(s, PROFILE, "2026-10-08", "live", 100, start=True)
    record(s, PROFILE, "2026-10-08", "live", 200, phase="publish")
    record(s, PROFILE, "2026-10-08", "live", 205, phase="names", phase_start=True)
    record(s, PROFILE, "2026-10-08", "live", 210, phase="iv", phase_start=True)
    assert rows(s, "SELECT phase, seq, started_ts, finished_ts, status FROM scan_run_phases ORDER BY seq") == [
        ("publish", 0, 100, 200, "done"),
        ("names", 1, 205, None, "running"),
        ("iv", 2, 210, None, "running"),
    ]
    record(s, PROFILE, "2026-10-08", "live", 300, phase="iv")
    record(s, PROFILE, "2026-10-08", "live", 400, phase="names")
    record(s, PROFILE, "2026-10-08", "live", 410, phase="names", phase_start=True)
    assert rows(s, "SELECT phase, seq, started_ts, finished_ts, status FROM scan_run_phases ORDER BY seq") == [
        ("publish", 0, 100, 200, "done"),
        ("names", 1, 205, 400, "done"),
        ("iv", 2, 210, 300, "done"),
    ]


def test_record_failed_exit_closes_running_phases():
    s = sink()
    record(s, PROFILE, "2026-10-08", "live", 100, start=True)
    record(s, PROFILE, "2026-10-08", "live", 200, phase="index", phase_start=True)
    record(s, PROFILE, "2026-10-08", "live", 250, exit_code=1)
    assert rows(s, "SELECT phase, started_ts, finished_ts, status, note FROM scan_run_phases") == [
        ("index", 200, 250, "failed", "failed (exit 1)"),
    ]


def test_phase_start_measures_nothing_and_does_not_reopen_a_finished_run():
    s = sink()
    record(s, PROFILE, "2026-10-08", "live", 100, start=True)
    begun = record(s, PROFILE, "2026-10-08", "live", 200, store=STORE, phase="publish", phase_start=True)
    assert (begun.phases, begun.outputs) == ((Phase("publish", 0, 200, None, "running"),), ())
    ended = record(s, PROFILE, "2026-10-08", "live", 300, store=STORE, phase="publish")
    assert ended.outputs == (Output("snapshot", "gs://data/snapshots/2026-10-08/", 50, 2),)
    record(s, PROFILE, "2026-10-08", "live", 310, exit_code=0)
    repeated = record(s, PROFILE, "2026-10-08", "live", 320, phase="publish", phase_start=True)
    assert (repeated.run.status, repeated.phases) == ("succeeded", ())
    assert rows(s, "SELECT phase, started_ts, finished_ts, status FROM scan_run_phases") == [
        ("publish", 200, 300, "done"),
    ]


def test_phases_from_markers():
    lines = task_lines(LOGS)["uid-a"]
    assert phases_of(PROFILE, lines, ts("2026-10-08T07:01:00")) == [
        Phase("listing", 0, ts("2026-10-08T07:01:00"), ts("2026-10-08T07:30:00"), "done", "wall"),
        Phase("ingest", 1, ts("2026-10-08T07:01:00"), ts("2026-10-08T07:30:00"), "done", "wall, overlapped the listing"),
        Phase("index", 2, ts("2026-10-08T07:30:00"), ts("2026-10-08T07:50:00"), "done", "wall"),
        Phase("publish", 3, ts("2026-10-08T07:50:00"), ts("2026-10-08T07:55:00"), "done", "wall"),
    ]


def test_phases_from_markers_concurrent():
    lines = [
        (ts("2026-10-08T07:30:00"), "PHASE listing: 1740s (wall)"),
        (ts("2026-10-08T07:50:00"), "PHASE publish: 2940s (wall)"),
        (ts("2026-10-08T08:05:00"), "PHASE iv: 3840s (wall)"),
        (ts("2026-10-08T08:40:00"), "PHASE names: 5940s (wall)"),
        (ts("2026-10-08T08:50:00"), "PHASE sync: 6540s (wall)"),
    ]
    assert phases_of(PROFILE, lines, ts("2026-10-08T07:01:00")) == [
        Phase("listing", 0, ts("2026-10-08T07:01:00"), ts("2026-10-08T07:30:00"), "done", "wall"),
        Phase("publish", 1, ts("2026-10-08T07:30:00"), ts("2026-10-08T07:50:00"), "done", "wall"),
        Phase("iv", 2, ts("2026-10-08T07:50:00"), ts("2026-10-08T08:05:00"), "done", "wall"),
        Phase("names", 3, ts("2026-10-08T07:50:00"), ts("2026-10-08T08:40:00"), "done", "wall"),
        Phase("sync", 4, ts("2026-10-08T08:40:00"), ts("2026-10-08T08:50:00"), "done", "wall"),
    ]


def test_phases_from_explicit_start_markers():
    assert phases_of(PROFILE, [
        (100, "PHASE-START listing: 0s (wall)"),
        (105, "PHASE-START ingest: 5s (wall)"),
        (200, "PHASE listing: 100s (wall)"),
        (210, "PHASE ingest: 110s (wall)"),
        (215, "PHASE-START names: 115s (wall)"),
        (220, "PHASE-START iv: 120s (wall)"),
        (250, "PHASE iv: 150s (wall)"),
    ], 100) == [
        Phase("listing", 0, 100, 200, "done", "wall"),
        Phase("ingest", 1, 105, 210, "done", "wall"),
        Phase("names", 2, 215, None, "running"),
        Phase("iv", 3, 220, 250, "done", "wall"),
    ]


def test_reconstruct():
    recs = reconstruct(PROFILE, JOBS, LOGS, STORE, now=ts("2026-10-09T00:00:00"))
    summary = [(r.run.run_id, r.run.scan, r.run.kind, r.run.status, r.run.parent, r.run.gen, r.run.cost_usd, r.run.error,
                len(r.phases), [o.key for o in r.outputs]) for r in recs]
    assert summary == [
        ("uid-n", "2026-10-06", "scan", "nop", None, None, 0.0333, None, 0, []),
        ("uid-f", "2026-10-07", "scan", "failed", None, None, 0.6333, "exit 1: 'dt-cloud job submit-listing -d 2026-10-07' (line 222)", 0, []),
        ("uid-a", "2026-10-08", "scan", "succeeded", None, "20261008T070217Z", 2.0, None, 4,
         ["listing", "listing/b1", "listing/b2", "index", "index/attr", "index/path-index", "static", "static/deltas/2026-10-08"]),
        ("uid-l", "2026-10-08", "listing", "succeeded", "uid-a", None, 0.45, None, 0, []),
        ("uid-r", "2026-10-08", "reproc", "succeeded", None, "20261008T120000Z", 1.0, None, 1,
         ["index", "index/path-index", "snapshot"]),
    ]
    reproc = recs[-1]
    assert reproc.run.job_name == "snap-reproc"
    assert reproc.run.started_ts == ts("2026-10-08T12:00:00")
    assert reproc.run.finished_ts == ts("2026-10-08T12:30:00")
    assert [(o.key, o.bytes, o.objects, o.rows) for o in reproc.outputs] == [
        ("index", 930, 2, None), ("index/path-index", 930, 2, 5999), ("snapshot", 50, 2, None),
    ]


def test_reconstruct_writes_idempotently():
    s = sink()
    now = ts("2026-10-09T00:00:00")
    for _ in range(2):
        for rec in reconstruct(PROFILE, JOBS, LOGS, STORE, now=now):
            s.execute(record_sql(rec, now))
    assert rows(s, "SELECT COUNT(*) FROM scan_runs") == [(5,)]
    assert rows(s, "SELECT COUNT(*) FROM scan_run_phases") == [(5,)]
    assert rows(s, "SELECT COUNT(*) FROM scan_run_outputs") == [(11,)]


def test_log_filter():
    since = dt.datetime(2026, 9, 9, tzinfo=dt.timezone.utc)
    assert log_filter(PROFILE, since) == (
        'log_id("batch_task_logs") AND timestamp >= "2026-09-09T00:00:00Z" AND ('
        'textPayload=~"^PHASE " OR textPayload=~"^PHASE-START " OR textPayload=~"^NOP " OR textPayload=~"^\\\\+\\\\+ fail_alert " OR textPayload=~"^\\\\+\\\\+ scan_err " OR '
        'textPayload=~"^DONE ")'
    )


def test_failure_from_either_error_marker():
    assert [failure_of(PROFILE, {}, [(0, "+ cp a b"), (1, ln)]) for ln in (
        "++ fail_alert 1 222 'dt-cloud job submit-listing -d 2026-10-07'",
        "++ scan_err 3 40 'cp a b'",
        "++ SR_ERR='exit 1: cp a b (run.sh:40)'",
    )] == ["exit 1: 'dt-cloud job submit-listing -d 2026-10-07' (line 222)", "exit 3: 'cp a b' (line 40)", None]


def test_batch_failure_reasons():
    def failed(desc):
        return {"status": {"statusEvents": [{"description": desc}]}}

    assert [failure_of(PROFILE, failed(d), []) for d in (
        'Job state is set from RUNNING to FAILED for job j.Job failed due to task failure. Specifically, task with index 0 failed due to the following task event: "Task state is updated from RUNNING to FAILED on zones/z/instances/1 with exit code 137."',
        'Job state is set from RUNNING to FAILED for job j.Job failed due to task failure. Specifically, task with index 0 failed due to the following task event: "Task state is updated from RUNNING to FAILED on zones/z/instances/1 due to task runs over the maximum runtime with exit code 50005."',
        "Job state is set from CANCELLATION_IN_PROGRESS to CANCELLED for job j.",
    )] == ["Batch: task failed (exit 137)", "Batch: over the maximum runtime (exit 50005)", "Batch: cancelled"]


def test_exit_trap_after_a_nop_keeps_it():
    s = sink()
    record(s, PROFILE, "2026-10-08", "uid-6", 10, start=True)
    record(s, PROFILE, "2026-10-08", "uid-6", 20, nop=True)
    record(s, PROFILE, "2026-10-08", "uid-6", 21, exit_code=0)
    assert rows(s, "SELECT status, started_ts, finished_ts FROM scan_runs") == [("nop", 10, 21)]


# ── the generic seam: stated job facts, a step's own time, the store, unbilled phases ───────────────────


QUEUED = parse_profile({
    "name": "queued",
    "store": "primary",
    "run_id_env": ["JOB_ID", "BATCH_JOB_UID"],
    "unbilled": ["capture", "queue"],
    "prices": {"box-1": 0.36},
    "spot_factor": 0.5,
})


def test_profile_run_id_env_and_unbilled():
    assert (PROFILE.run_id_env, PROFILE.unbilled) == (("BATCH_JOB_UID",), ())
    assert (QUEUED.run_id_env, QUEUED.unbilled) == (("JOB_ID", "BATCH_JOB_UID"), ("capture", "queue"))
    assert [env_run_id(QUEUED, e) for e in ({"JOB_ID": "j1", "BATCH_JOB_UID": "u1"}, {"JOB_ID": " ", "BATCH_JOB_UID": "u1"}, {})] == [
        "j1", "u1", None,
    ]


def test_record_stated_facts_and_cost_over_billed_time():
    """A run that starts before its job (the capture it ingests, then the queue): facts stated at the start, each step
    at its own time, the cost at the finish over the billed time only (here 1000 s of 1600 s)."""
    s = sink()
    facts = dict(job_name="ingest-1", region="us-east-1", machine="box-1", spot=True, tasks=None, image="img:abc")
    record(s, QUEUED, "2026-10-14T0600", "job-1", 1000, start=True, kind="ingest", facts=facts)
    record(s, QUEUED, "2026-10-14T0600", "job-1", 1400, kind="ingest", phase="capture")
    record(s, QUEUED, "2026-10-14T0600", "job-1", 1600, kind="ingest", phase="queue")
    record(s, QUEUED, "2026-10-14T0600", "job-1", 2100, kind="ingest", phase="fetch")
    done = record(s, QUEUED, "2026-10-14T0600", "job-1", 2600, kind="ingest", phase="index-sync")
    assert done.run.cost_usd is None
    fin = record(s, QUEUED, "2026-10-14T0600", "job-1", 2600, kind="ingest", exit_code=0)
    assert fin.run.cost_usd == 0.05  # 0.36 $/h × 0.5 (spot) × 1000 s
    assert rows(s, "SELECT run_id, kind, status, store, started_ts, finished_ts, job_name, region, machine, spot, tasks, image, cost_usd FROM scan_runs") == [
        ("job-1", "ingest", "succeeded", "primary", 1000, 2600, "ingest-1", "us-east-1", "box-1", 1, None, "img:abc", 0.05),
    ]
    assert rows(s, "SELECT phase, seq, started_ts, finished_ts, status FROM scan_run_phases ORDER BY seq") == [
        ("capture", 0, 1000, 1400, "done"),
        ("queue", 1, 1400, 1600, "done"),
        ("fetch", 2, 1600, 2100, "done"),
        ("index-sync", 3, 2100, 2600, "done"),
    ]


def test_record_failed_finish_costs_billed_time_too():
    s = sink()
    record(s, QUEUED, "2026-10-14T0600", "job-2", 0, start=True, facts={"machine": "box-1", "spot": False})
    record(s, QUEUED, "2026-10-14T0600", "job-2", 3000, phase="queue")
    fin = record(s, QUEUED, "2026-10-14T0600", "job-2", 6600, exit_code=1, failed_phase="fetch")
    assert (fin.run.status, fin.run.failed_phase, fin.run.error, fin.run.cost_usd) == ("failed", "fetch", "exit 1", 0.36)


def test_record_without_a_priced_machine_has_no_cost():
    s = sink()
    record(s, QUEUED, "2026-10-14T0600", "job-3", 0, start=True, facts={"machine": "unpriced"})
    assert record(s, QUEUED, "2026-10-14T0600", "job-3", 3600, exit_code=0).run.cost_usd is None
    record(s, QUEUED, "2026-10-14T0600", "job-4", 0, start=True)
    assert record(s, QUEUED, "2026-10-14T0600", "job-4", 3600, exit_code=0).run.cost_usd is None


def test_record_refuses_unknown_facts():
    with pytest.raises(ValueError) as e:
        record(sink(), QUEUED, "2026-10-14T0600", "job-5", 0, start=True, facts={"zone": "z"})
    assert str(e.value) == "scan-run: unknown job facts ['zone']"


def test_record_cli_store_at_facts_and_run_id_env(tmp_path, monkeypatch):
    profile = tmp_path / "scan-runs.json"
    profile.write_text(json.dumps({"store": "primary", "run_id_env": ["JOB_ID"], "unbilled": ["queue"], "prices": {"box-1": 0.36}}))
    db = tmp_path / "runs.sqlite"
    SqliteSink(db, ddl=DDL)
    monkeypatch.setenv("JOB_ID", "job-9")
    monkeypatch.delenv("BATCH_JOB_UID", raising=False)
    base = ["record", "-c", str(profile), "-s", str(db), "-M", "-K", "nas", "-k", "ingest"]
    calls = [
        ["-S", "-t", "100", "-m", "box-1", "--no-spot", "-T", "1", "-j", "ingest-9", "-R", "us-east-1", "-i", "img:1"],
        ["-P", "queue", "-t", "200"],
        ["-P", "fetch", "-t", "3800"],
        ["-x", "0", "-t", "3800"],
    ]
    for c in calls:
        r = CliRunner().invoke(cli, [*base, *c, "2026-10-14T0600"])
        assert (r.exit_code, r.exception, r.stdout) == (0, None, "")
    s = SqliteSink(db)
    assert rows(s, "SELECT run_id, kind, status, store, started_ts, finished_ts, job_name, region, machine, spot, tasks, image, cost_usd, source FROM scan_runs") == [
        ("job-9", "ingest", "succeeded", "nas", 100, 3800, "ingest-9", "us-east-1", "box-1", 0, 1, "img:1", 0.36, "job"),
    ]
    assert rows(s, "SELECT phase, started_ts, finished_ts FROM scan_run_phases ORDER BY seq") == [("queue", 100, 200), ("fetch", 200, 3800)]


def test_record_cli_without_a_run_id_reports_and_exits_zero(tmp_path, monkeypatch):
    profile = tmp_path / "scan-runs.json"
    profile.write_text(json.dumps({"run_id_env": ["JOB_ID"]}))
    monkeypatch.delenv("JOB_ID", raising=False)
    said: list[str] = []
    monkeypatch.setattr(scan_runs, "err", said.append)  # `err` writes the stderr bound at import
    r = CliRunner().invoke(cli, ["record", "-c", str(profile), "-n", "-S", "2026-10-14T0600"])
    assert (r.exit_code, r.stdout, said) == (
        0, "", ["scan-run record failed (the scan continues): SystemExit: scan-run: no run id: pass -r or set $JOB_ID"],
    )
