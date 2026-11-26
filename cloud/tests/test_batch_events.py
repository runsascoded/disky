"""`dt-cloud batch-events`: a Batch state-change push → reconcile + converge on a terminal state, nothing otherwise."""

import base64
import json

from dt_cloud import scan_runs
from dt_cloud.batch_events import catch_up_id, catch_up_spec, compaction, handle, job_ref, scan_ended
from dt_cloud.d1_schema import table_ddl


def push(attrs: dict | None, data: bytes = b"", mid: str = "m1") -> bytes:
    msg: dict = {"data": base64.b64encode(data).decode(), "messageId": mid}
    if attrs is not None:
        msg["attributes"] = attrs
    return json.dumps({"message": msg, "subscription": "projects/p/subscriptions/s"}).encode()


def changed(state: str, uid: str = "gcs-usage-snapshot-abc") -> dict:
    return {"Type": "JOB_STATE_CHANGED", "JobUID": uid, "JobName": f"projects/p/locations/r/jobs/{uid}", "NewJobState": state}


def run(body: bytes, fail: bool = False) -> tuple[int, list[dict]]:
    seen: list[dict] = []

    def act(attrs):
        seen.append(dict(attrs))
        if fail:
            raise RuntimeError("D1 down")

    return handle(body, act), seen


def test_a_terminal_state_reconciles_and_converges():
    for state in ("SUCCEEDED", "FAILED", "CANCELLED"):
        assert run(push(changed(state))) == (204, [changed(state)])


def test_other_changes_are_acked_untouched():
    assert run(push(changed("RUNNING"))) == (204, [])
    assert run(push(changed("CANCELLATION_IN_PROGRESS"))) == (204, [])
    assert run(push({"Type": "TASK_STATE_CHANGED", "NewJobState": "FAILED"})) == (204, [])
    assert run(push(None)) == (204, [])


def test_a_failing_action_is_redelivered():
    assert run(push(changed("CANCELLED")), fail=True) == (500, [changed("CANCELLED")])


def test_a_body_that_isnt_a_push_is_refused():
    assert run(b"not json") == (400, [])
    assert run(json.dumps({"nope": 1}).encode()) == (400, [])
    assert run(json.dumps({"message": {"data": "!!"}}).encode()) == (400, [])


# ── an interval-store compaction's end (#82): the catch-up, or an alert ──

JOB = "iv-compact-2026-11-21-0703-071234"
IMAGE = "us-docker.pkg.dev/p/r/scan:abc123"


def compact_job(event: str = "catch-up", profile: str | None = "/app/job/interval-store.json") -> dict:
    """Batch's record of a compaction job, as `gcp.batch_job` returns it (the fields `catch_up_spec` reads)."""
    variables = {"STATIC_NAMES_BUCKET": "data", "DISKY_LABELS": "app=disky,deployment=d", "DT_JOB_NAME": JOB, "DT_JOB_IMAGE": "scan:abc123"}
    if profile:
        variables["INTERVAL_STORE_PROFILE"] = profile
    return {
        "name": f"projects/123/locations/r/jobs/{JOB}",
        "taskGroups": [{"name": f"projects/123/locations/r/jobs/{JOB}/taskGroups/group0", "taskCount": "1", "parallelism": "1", "taskSpec": {
            "runnables": [{"container": {"imageUri": IMAGE, "entrypoint": "bash", "commands": ["-c", "python3 -m dt_cloud.interval_append compact"],
                                         "volumes": ["/mnt/disks/gcs/data:/gcs/data:ro"]}}],
            "environment": {"variables": variables, "secretVariables": {"CLOUDFLARE_API_TOKEN": "projects/p/secrets/t/versions/latest"}},
            "computeResource": {"cpuMilli": "32000", "memoryMib": "246400"}, "maxRetryCount": 0, "maxRunDuration": "57600s",
        }}],
        "allocationPolicy": {"instances": [{"policy": {"machineType": "n2-highmem-32"}}], "labels": {"app": "disky", "component": "interval-store"}},
        "labels": {"app": "disky", "deployment": "d", "component": "interval-store", "purpose": "interval-store", "stage": "compact",
                   "gen": "g3-c001", "events": event},
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
        "notifications": [{"pubsubTopic": "projects/p/topics/t", "message": {"type": "JOB_STATE_CHANGED"}}],
    }


def ended(state: str, job: dict | None) -> tuple[str | None, list]:
    calls: list = []

    def submit(spec, jid, region, project):
        calls.append(("submit", jid, region, project, spec["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"]))
        return jid

    attrs = {**changed(state, JOB), "JobName": f"projects/123/locations/r/jobs/{JOB}"}
    return compaction(attrs, job, submit, lambda level, message: calls.append(("alert", level, message))), calls


def test_job_ref_and_catch_up_id():
    assert job_ref(changed("SUCCEEDED", JOB)) == ("p", "r", JOB)
    assert job_ref({"JobName": "jobs/x"}) is None
    assert catch_up_id(JOB) == f"{JOB}-catch-up"
    assert catch_up_id("a" * 60) == "a" * 54 + "-catch-up"


def test_the_catch_up_runs_the_append_in_the_compactions_image_account_and_env():
    job = compact_job()
    assert catch_up_spec(job) == {
        "taskGroups": [{"taskCount": 1, "parallelism": 1, "taskSpec": {
            "runnables": [{"container": {"imageUri": IMAGE, "entrypoint": "bash", "commands": ["-c", "dt-cloud interval-store append -c -l"],
                                         "volumes": ["/mnt/disks/gcs/data:/gcs/data:ro"]}}],
            "environment": {"variables": {"STATIC_NAMES_BUCKET": "data", "DISKY_LABELS": "app=disky,deployment=d",
                                          "INTERVAL_STORE_PROFILE": "/app/job/interval-store.json", "INTERVAL_STORE_IMAGE": IMAGE},
                            "secretVariables": {"CLOUDFLARE_API_TOKEN": "projects/p/secrets/t/versions/latest"}},
            "computeResource": {"cpuMilli": "32000", "memoryMib": "246400"}, "maxRetryCount": 0, "maxRunDuration": "57600s",
        }}],
        "allocationPolicy": {"instances": [{"policy": {"machineType": "n2-highmem-32"}}], "labels": {"app": "disky", "component": "interval-store"}},
        "labels": {"app": "disky", "deployment": "d", "component": "interval-store", "purpose": "interval-store", "stage": "catch-up", "gen": "g3-c001"},
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }
    assert catch_up_spec(compact_job(profile=None)) is None


def test_a_compactions_end_submits_the_catch_up_or_alerts():
    cmd = ["-c", "dt-cloud interval-store append -c -l"]
    assert ended("SUCCEEDED", compact_job()) == (f"compaction {JOB} SUCCEEDED: catch-up {JOB}-catch-up submitted",
                                                 [("submit", f"{JOB}-catch-up", "r", "123", cmd)])
    assert ended("SUCCEEDED", compact_job("alert")) == (f"compaction {JOB} SUCCEEDED: a step before the pointer's, nothing to do", [])
    for state in ("FAILED", "CANCELLED"):
        for event in ("catch-up", "alert"):
            assert ended(state, compact_job(event)) == (f"compaction {JOB} {state}: alerted", [(
                "alert", "fail", f"interval-store compaction job {JOB} (generation g3-c001) ended {state}: the generation isn't compacted. "
                                 "Appends wait on the compaction lease until it lapses; while the compaction is due, the next append submits it again.")])
    assert ended("SUCCEEDED", compact_job(profile=None)) == (f"compaction {JOB} SUCCEEDED: no profile, alerted", [(
        "alert", "warn", f"interval-store compaction {JOB} (generation g3-c001) published, but its job names no $INTERVAL_STORE_PROFILE: "
                         "no catch-up submitted; the scans it deferred go in with the next scan's append")])


def test_a_static_names_compactions_failure_alerts_and_its_success_does_nothing():
    job = compact_job("alert", profile=None)
    job["labels"] = {**job["labels"], "component": "static-names", "purpose": "static-names", "gen": "2026-10-15cw-c003"}
    assert ended("SUCCEEDED", job) == (f"compaction {JOB} SUCCEEDED: nothing to do", [])
    for state in ("FAILED", "CANCELLED"):
        assert ended(state, job) == (f"compaction {JOB} {state}: alerted", [(
            "alert", "fail", f"static-names compaction job {JOB} (generation 2026-10-15cw-c003) ended {state}: the generation isn't compacted. "
                             "Its runs keep piling up (each scan's anchors and drill read them all); while the compaction is due, "
                             "the next scan's append submits it again.")])


def test_any_other_job_is_left_to_the_scan_path():
    head = compact_job()
    head["labels"] = {"app": "disky", "component": "scan"}
    unlabelled = compact_job()
    del unlabelled["labels"]["events"]
    assert [ended("SUCCEEDED", j) for j in (head, unlabelled, None)] == [(None, []), (None, []), (None, [])]


# ── a scan head's end: its run reconciled, a cancel recorded `cancelled` ──


def head_cancelled(job: str) -> dict:
    return {**changed("CANCELLED", job), "JobName": f"projects/p/locations/r1/jobs/{job}"}


def test_a_cancelled_heads_trapped_run_is_cancelled_unless_a_step_had_failed_first():
    # the head's exit trap recorded the cancel's SIGTERM (its running phase closed at the trap's write); in `early`, a
    # step had failed on its own 10 minutes before
    s = scan_runs.SqliteSink(":memory:", ddl=table_ddl(scan_runs.TABLES))
    profile = scan_runs.parse_profile({"name": "test", "project": "p"})
    t0, end = 1_800_000_000, 1_800_003_600
    for rid in ("pure", "early"):
        s.execute([scan_runs.run_sql(scan_runs.Run(rid, "2026-11-21T0700", "scan", "failed", started_ts=t0, finished_ts=end, job_name=f"j-{rid}",
                                                   region="r1", failed_phase="index", error="head terminated (signal; session s1)"), end),
                   scan_runs.phase_sql(rid, scan_runs.Phase("index", 1, t0 + 600, end, "failed", "failed (exit 143)"))])
    s.execute([scan_runs.phase_sql("early", scan_runs.Phase("listing", 0, t0, end - 600, "failed", "failed (exit 1)"))])

    def lookup(name: str, region: str):
        raise AssertionError(f"no running row to look up: {name} {region}")

    for job in ("j-pure", "j-early"):
        scan_ended(head_cancelled(job), s, profile, lookup, end + 60)
    assert [tuple(r.values()) for r in s.query("SELECT run_id, status, error, finished_ts, updated_ts FROM scan_runs ORDER BY run_id")] == [
        ("early", "failed", "head terminated (signal; session s1)", end, end),
        ("pure", "cancelled", "cancelled", end, end + 60),
    ]


def test_a_cancelled_heads_running_run_is_reconciled_cancelled_unless_a_step_had_failed_first():
    # no trap ran: the rows are still `running`, and Batch says the job was CANCELLED at `end`
    s = scan_runs.SqliteSink(":memory:", ddl=table_ddl(scan_runs.TABLES))
    profile = scan_runs.parse_profile({"name": "test", "project": "p"})
    t0, end = 1_800_000_000, 1_800_003_600
    for rid in ("pure", "early"):
        s.execute([scan_runs.run_sql(scan_runs.Run(rid, "2026-11-21T0700", "scan", "running", started_ts=t0, job_name=f"j-{rid}", region="r1"), end - 60),
                   scan_runs.phase_sql(rid, scan_runs.Phase("index", 1, t0 + 600, None, "running"))])
    s.execute([scan_runs.phase_sql("early", scan_runs.Phase("listing", 0, t0, end - 600, "failed", "failed (exit 1)"))])
    cancelled = {"status": {"state": "CANCELLED", "statusEvents": [
        {"eventTime": "2027-01-15T09:00:00Z", "description": "Job state is set from RUNNING to CANCELLED."}]}}  # `end`
    scan_ended(head_cancelled("j-pure"), s, profile, lambda name, region: cancelled, end + 60)
    assert [tuple(r.values()) for r in s.query("SELECT run_id, status, error, failed_phase, finished_ts, updated_ts FROM scan_runs ORDER BY run_id")] == [
        ("early", "failed", "cancelled", "index", end, end + 60),
        ("pure", "cancelled", "cancelled", "index", end, end + 60),
    ]
    assert [tuple(r.values()) for r in s.query("SELECT run_id, phase, status, note, finished_ts FROM scan_run_phases ORDER BY run_id, seq")] == [
        ("early", "listing", "failed", "failed (exit 1)", end - 600),
        ("early", "index", "failed", "cancelled", end),
        ("pure", "index", "failed", "cancelled", end),
    ]
