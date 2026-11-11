"""gcs's job scripts (`job/`): the Batch submit spec, `run.sh`'s phases and alerts, the static-names recovery
controller. Deployment-specific (project ids, machine shapes), so kept out of the shared `cloud` tests."""
from json import loads
import json
import os
from os import environ
from pathlib import Path
from shutil import which
from subprocess import run

import pytest

from dt_cloud.scan_runs import load_profile
from dt_cloud.scan_runs_backfill import classify, failure_of, reconstruct
from dt_cloud.scan_tasks import task_spec
from test_scan_tasks import CONTEXT, IMAGE, SCAN, controller

ROOT_DIR = Path(__file__).resolve().parents[2]


def test_manual_labels_and_cron_spec_are_not_frozen_to_a_run(monkeypatch):
    monkeypatch.setenv("DISKY_LABELS", "app=disky,deployment=gcs")
    monkeypatch.setenv("JOB_ID", "root-123")
    monkeypatch.setenv("SNAP_ID", "2026-10-13T1300")
    monkeypatch.setenv("DRY", "1")
    script = Path(__file__).parents[2] / "job/batch-submit.sh"
    manual = json.loads(run(["bash", str(script)], check=True, capture_output=True, text=True).stdout)
    want = {"app": "disky", "deployment": "gcs", "component": "scan", "scan_run": "root-123", "scan_id": "2026-10-13t1300"}
    assert (manual["labels"], manual["allocationPolicy"]["labels"]) == (want, want)
    assert {k: manual["taskGroups"][0]["taskSpec"]["environment"]["variables"][k] for k in ("SNAP_ID", "SCAN_RUN_ID")} == {
        "SNAP_ID": "2026-10-13T1300", "SCAN_RUN_ID": "root-123",
    }
    monkeypatch.setenv("PIN", "1")
    cron = json.loads(run(["bash", str(script)], check=True, capture_output=True, text=True).stdout)
    monkeypatch.setenv("SCAN_RUN_ID", "ambient-456")
    monkeypatch.setenv("SNAP_ID", "2026-10-14T0700")
    other = json.loads(run(["bash", str(script)], check=True, capture_output=True, text=True).stdout)
    assert cron == other
    assert (cron["labels"], cron["allocationPolicy"]["labels"]) == (
        {"app": "disky", "deployment": "gcs", "component": "scan"}, {"app": "disky", "deployment": "gcs", "component": "scan"},
    )


@pytest.mark.parametrize("context, expected", [
    ({"BATCH_JOB_UID": "daily-root-uid"}, "daily-root-uid"),
    ({"BATCH_JOB_UID": "child-uid", "SCAN_RUN_ID": "original-root-uid"}, "original-root-uid"),
])
def test_job_start_derives_or_preserves_root_identity(context, expected):
    source = (Path(__file__).parents[2] / "job/run.sh").read_text().split("\nDATE=", 1)[0]
    env = {"PATH": os.environ["PATH"], "SNAP_ID": "2026-10-13T1300", **context}
    result = run(["bash", "-c", source + '\nprintf "%s\\n%s\\n" "$SCAN_RUN_ID" "$SCAN_ID"'], env=env, capture_output=True, text=True)
    assert (result.returncode, result.stdout.splitlines()) == (0, [expected, "2026-10-13T1300"])


def test_recovery_controller_waits_for_success_and_pins_child_image():
    root = Path(__file__).resolve().parents[2]
    result = run(["bash", "job/static-names.sh", "resume-after", "drill-job", "2026-10-12T1042"], cwd=root, env={
        "PATH": environ["PATH"], "DRY": "1", "IMAGE": "registry@sha256:test", "DISKY_LABELS": "app=disky,deployment=gcs",
    }, capture_output=True, text=True)
    assert (result.returncode, result.stderr) == (0, "")
    spec = loads(result.stdout)
    task = spec["taskGroups"][0]["taskSpec"]
    assert task["environment"] == {"variables": {
        "DISKY_LABELS": "app=disky,deployment=gcs",
        "GCP_PROJECT": "oa-internal-450019",
        "CLOUDFLARE_ACCOUNT_ID": "74981a43be0de7712369306c7b19133d",
        "R2_ENDPOINT": "https://74981a43be0de7712369306c7b19133d.r2.cloudflarestorage.com",
        "SCAN_ID": "2026-10-12T1042",
    }}
    assert task["runnables"][0]["container"]["commands"] == ["-c", "set -euo pipefail; BATCH_REGION=us-east1 dt-cloud job watch -L -i 30 drill-job && cd /app && STATIC_NAMES_PROFILE=job/static-names.json STATIC_NAMES_IMAGE=registry@sha256:test STATIC_NAMES_SPOT=0 dt-cloud static-names runs add -c -M 2026-10-12T1042"]
    assert (task["maxRetryCount"], spec["allocationPolicy"]["instances"][0]["policy"]["machineType"], spec["allocationPolicy"]["instances"][0]["policy"]["provisioningModel"]) == (0, "n2-highmem-4", "STANDARD")
    labels = {"app": "disky", "deployment": "gcs", "component": "static-names", "scan_id": "2026-10-12t1042"}
    assert (spec["labels"], spec["allocationPolicy"]["labels"]) == ({"purpose": "static-names", "stage": "resume", **labels}, labels)


@pytest.mark.parametrize("pin, expected", [("", ("n2-standard-4", 4000, 14000, "8GB", "4", "1")), ("1", ("n2-highmem-32", 8000, 250000, "100GB", "16", None))])
def test_split_is_manual_only_and_does_not_change_cron(pin, expected):
    # NF=0: the one-VM spec (the cron's default is the Nextflow head, `test_cron_is_the_nextflow_head`)
    result = run(["bash", "job/batch-submit.sh"], cwd=ROOT_DIR, env={
        "PATH": environ["PATH"], "DRY": "1", "PIN": pin, "NF": "0", "SPLIT_COMPUTE": "1", "IMAGE": IMAGE,
    }, capture_output=True, text=True)
    assert (result.returncode, result.stderr) == (0, "")
    spec = loads(result.stdout)
    task = spec["taskGroups"][0]["taskSpec"]
    env = task["environment"]["variables"]
    assert (spec["allocationPolicy"]["instances"][0]["policy"]["machineType"], task["computeResource"]["cpuMilli"],
            task["computeResource"]["memoryMib"], env["DUCKDB_MEM"], env["DUCKDB_THREADS"], env.get("SPLIT_COMPUTE")) == expected
    assert spec["allocationPolicy"]["instances"][0]["policy"]["disks"] == ([] if not pin else [{"newDisk": {"type": "local-ssd", "sizeGb": "1500"}, "deviceName": "stage"}])
    assert [v for v in task["volumes"] if v.get("deviceName") == "stage"] == ([] if not pin else [{"deviceName": "stage", "mountPath": "/mnt/disks/stage"}])
    assert env["STAGE_DIR"] == ("/tmp" if not pin else "/stage")


def test_downstream_backfill_retains_explicit_parent():
    profile = load_profile(ROOT_DIR / "job/scan-runs.json")
    jobs = []
    for stage in ("compute", "access"):
        spec = task_spec(controller(), stage, SCAN, CONTEXT)
        spec.update(uid=stage, name=f"projects/p/locations/us-central1/jobs/{stage}", createTime="2026-10-13T13:20:00Z", status={"state": "RUNNING"})
        jobs.append(spec)
    assert [classify(profile, job).kind for job in jobs] == ["compute", "access"]
    assert [(r.run.run_id, r.run.parent, r.run.kind) for r in reconstruct(profile, jobs, [], None, now=1791900000)] == [
        ("access", "root-uid", "access"), ("compute", "root-uid", "compute"),
    ]


def test_error_marker_matches_the_traced_err_trap():
    """`run.sh`'s ERR trap (its own lines), as xtrace logs it: bash traces a trap's command one level down (`++`), so
    the backfill's `error_marker` must match that line — and the pre-wrapper `fail_alert` trap's, in older logs."""
    profile = load_profile(ROOT_DIR / "job/scan-runs.json")
    lines = (ROOT_DIR / "job/run.sh").read_text().splitlines()
    trap = [ln for ln in lines if ln.startswith(("scan_err() ", "trap 'scan_err "))]
    assert trap == ['scan_err() { SR_ERR="exit $1: $3 (run.sh:$2)"; }', "trap 'scan_err $? $LINENO \"$BASH_COMMAND\"' ERR"]
    script = "\n".join(trap + ["set -x", "false", 'echo "$SR_ERR"'])
    traced = run(["bash", "-c", script], capture_output=True, text=True)
    assert traced.stderr.splitlines() == ["+ false", "++ scan_err 1 4 false", "++ SR_ERR='exit 1: false (run.sh:4)'", "+ echo 'exit 1: false (run.sh:4)'"]
    assert failure_of(profile, {}, list(enumerate(traced.stderr.splitlines()))) == "exit 1: false (line 4)"
    assert failure_of(profile, {}, [(0, "++ fail_alert 1 237 dt-cloud stage -o /stage")]) == "exit 1: dt-cloud stage -o /stage (line 237)"


@pytest.mark.parametrize("code, scratch, expected", [
    (0, "0", (0, ["START scan-compute", "WORKER compute s g snapshots/s listing/s/index/g/path-index.parquet", "END scan-compute", "POST-PUBLISH"])),
    (7, "0", (7, ["START scan-compute", "WORKER compute s g snapshots/s listing/s/index/g/path-index.parquet"])),
    (0, "1", (0, ["START scan-compute", "WORKER compute s g snapshots/s listing/s/index/g/path-index.parquet", "END scan-compute"])),
])
def test_controller_waits_for_compute_and_scratch_never_advances_pointers(code, scratch, expected):
    script = (ROOT_DIR / "job/run.sh").read_text()
    start = script.index('if [ "${SPLIT_COMPUTE:-0}" = "1" ]; then\n  sr phase-begin scan-compute')
    block = script[start:].split("else\nsr phase-begin path-index", 1)[0] + "fi\n"
    preamble = r'''
set -eu
SPLIT_COMPUTE=1 SNAP_ID=s GEN=g SNAP_PATH=snapshots/s INDEX_PATH=listing/s/index/g/path-index.parquet
sr() { printf '%s %s\n' "$([ "$1" = phase-begin ] && echo START || echo END)" "$2"; }
dt-cloud() { printf 'WORKER %s %s %s %s %s\n' "$4" "$5" "$GEN" "$SNAP_PATH" "$INDEX_PATH"; return "$CODE"; }
'''
    result = run(["bash", "-c", preamble + block + "printf 'POST-PUBLISH\\n'"], env={"PATH": "/usr/bin:/bin", "CODE": str(code), "SCRATCH": scratch}, capture_output=True, text=True)
    assert (result.returncode, result.stdout.splitlines()) == expected
    assert result.stderr == ("SCRATCH — split computation complete; no serving pointers changed\n" if scratch == "1" and code == 0 else "")


@pytest.mark.parametrize("overrides, expected", [
    ({}, (0, "")),
    ({"JOB_IMAGE": "registry:latest"}, (1, "ERROR: split scan requires a digest-pinned JOB_IMAGE\n")),
    ({"GATE": "1"}, (1, "ERROR: split scan does not support GATE\n")),
    ({"SCRATCH": "1"}, (1, "ERROR: split SCRATCH requires SNAP_PATH and INDEX_PATH under scratch/\n")),
    ({"SCRATCH": "1", "SNAP_PATH": "scratch/check/snapshot", "INDEX_PATH": "scratch/check/index/path-index.parquet"}, (0, "")),
])
def test_controller_preflight(overrides, expected):
    script = (ROOT_DIR / "job/run.sh").read_text()
    block = script.split('INDEX_PATH=${INDEX_PATH:-$INDEX_DIR/path-index.parquet}\n', 1)[1].split("# Scan-run record", 1)[0]
    result = run(["bash", "-c", "set -eu\n" + block], env={
        "PATH": "/usr/bin:/bin", "SPLIT_COMPUTE": "1", "JOB_IMAGE": IMAGE, "BATCH_JOB_UID": "root",
        "SNAP_PATH": CONTEXT["SNAP_PATH"], "INDEX_PATH": CONTEXT["INDEX_PATH"], **overrides,
    }, capture_output=True, text=True)
    assert (result.returncode, result.stderr) == expected
    assert result.stdout == ""


def test_split_bad_image_refused_before_allocating_a_vm():
    result = run(["bash", "job/batch-submit.sh"], cwd=ROOT_DIR, env={"PATH": environ["PATH"], "SPLIT_COMPUTE": "1", "IMAGE": "registry:latest"}, capture_output=True, text=True)
    assert (result.returncode, result.stdout, result.stderr) == (1, "", "ERROR: split scan requires a digest-pinned IMAGE\n")


@pytest.mark.parametrize("pin, expected", [("", ["zones/us-central1-f"]), ("1", ["regions/us-central1"])])
def test_manual_zone_override_does_not_change_cron(pin, expected):
    result = run(["bash", "job/batch-submit.sh"], cwd=ROOT_DIR, env={"PATH": environ["PATH"], "DRY": "1", "PIN": pin, "ZONE": "us-central1-f"}, capture_output=True, text=True)
    assert (result.returncode, result.stderr) == (0, "")
    assert loads(result.stdout)["allocationPolicy"]["location"] == {"allowedLocations": expected}


def test_four_core_controller_override_keeps_a_satisfiable_request():
    result = run(["bash", "job/batch-submit.sh"], cwd=ROOT_DIR, env={"PATH": environ["PATH"], "DRY": "1", "NF": "0", "MACHINE": "e2-standard-4", "SPLIT_COMPUTE": "1", "IMAGE": IMAGE}, capture_output=True, text=True)
    assert (result.returncode, result.stderr) == (0, "")
    spec = loads(result.stdout)
    assert spec["taskGroups"][0]["taskSpec"]["computeResource"] == {"cpuMilli": 4000, "memoryMib": 14000}
    assert spec["allocationPolicy"]["instances"][0]["policy"]["machineType"] == "e2-standard-4"


@pytest.mark.parametrize("mode, expected_env", [({}, "record= alerts="), ({"SCRATCH": "1"}, "record=off alerts=stderr"), ({"GATE": "1"}, "record=off alerts=stderr")])
def test_a_failure_finishes_the_run_with_the_err_trap_reason(mode, expected_env):
    """`run.sh`'s own wrapper preamble: an unphased command failing under `set -e` exits with its code, and the EXIT
    trap's `scan-run finish` gets that code and the ERR trap's reason; a SCRATCH / GATE run records nothing and keeps
    its alerts on stderr (`DT_RECORD` / `DT_ALERTS`, which the wrapper reads)."""
    script = (ROOT_DIR / "job/run.sh").read_text()
    preamble = script[script.index("export DT_JOB_START="):].split("\ncd /app\n", 1)[0] + "\n"
    stub = r'''set -e
dt-cloud() { printf 'DT %s | record=%s alerts=%s\n' "$*" "${DT_RECORD:-}" "${DT_ALERTS:-}"; }
'''
    body = "trap 'sr finish -x $? ${SR_ERR:+-e \"$SR_ERR\"}' EXIT\nsh -c 'exit 7'\nprintf 'UNEXPECTED SUCCESS\\n'\n"
    src = stub + preamble + body
    line = src.splitlines().index("sh -c 'exit 7'") + 1
    # the caller's bash (the job image's is 5.x): macOS's /bin/bash 3.2 reports this `$LINENO` one short
    result = run([which("bash"), "-c", src], env={"PATH": "/usr/bin:/bin", **mode}, capture_output=True, text=True)
    assert (result.returncode, result.stdout, result.stderr) == (7, f"DT scan-run finish -x 7 -e exit 7: sh -c 'exit 7' (run.sh:{line}) | {expected_env}\n", "")


def test_cron_is_the_nextflow_head():
    result = run(["bash", "job/batch-submit.sh"], cwd=ROOT_DIR, env={"PATH": environ["PATH"], "DRY": "1", "PIN": "1"}, capture_output=True, text=True)
    assert (result.returncode, result.stderr) == (0, "")
    spec = loads(result.stdout)
    task = spec["taskGroups"][0]["taskSpec"]
    c = task["runnables"][0]["container"]
    assert (c["entrypoint"], c["commands"], task["maxRunDuration"], task["computeResource"], task["volumes"],
            sorted(task["environment"]["secretVariables"]), task["environment"]["variables"]["DT_TRIGGER"],
            spec["allocationPolicy"]["instances"][0]["policy"]) == (
        "dt-cloud", ["scan", "head", "/app/job/profiles/gcs.yml"], "43200s", {"cpuMilli": 2000, "memoryMib": 7000}, [],
        ["CLOUDFLARE_API_TOKEN", "SLACK_BOT_TOKEN"], "schedule",
        {"machineType": "e2-standard-2", "bootDisk": {"type": "pd-balanced", "sizeGb": "30"}},
    )


def test_cron_spec_survives_the_submitters_labelling():
    """The daily submitter (`job/scan-submit.sh`) posts the PIN spec through `dt-cloud batch submit -c scan`, whose
    labelling (`label_batch_spec`) must leave the spec as the stack rendered it (and `pulumi preview` showed it)."""
    from dt_cloud.cost_labels import label_batch_spec

    env = {"PATH": environ["PATH"], "DRY": "1", "PIN": "1", "DISKY_LABELS": "app=disky,deployment=gcs"}
    spec = loads(run(["bash", "job/batch-submit.sh"], cwd=ROOT_DIR, env=env, check=True, capture_output=True, text=True).stdout)
    assert label_batch_spec(spec, "scan", {"DISKY_LABELS": env["DISKY_LABELS"]}) == spec


def scan_submit(tmp_path: Path, dt_rc: int, dt_err: str, spec: str = '{"labels": {"component": "scan"}}', **env_extra: str) -> tuple:
    """Run `job/scan-submit.sh` as its Cloud Run job does (`bash -c` its text) against fake `dt-cloud` (`dt_err` on
    stderr, exit `dt_rc`), `disk-tree` and `date` (2026-11-08 15:30:00 UTC), with `$BATCH_SPEC` = `spec` and `env_extra`
    (a run's overrides); returns (exit code, stdout, dt-cloud's argv, the spec file it was handed, disk-tree's argv or
    None, disk-tree's `$DISKY_NOTIFY` line or None)."""
    bin_, rec = tmp_path / "bin", tmp_path / "rec"
    bin_.mkdir()
    rec.mkdir()
    (tmp_path / "dt-err").write_text(dt_err)
    fakes = {
        "dt-cloud": f'printf "%s\\0" "$@" > {rec}/dt; for a; do last=$a; done; cp "$last" {rec}/spec\n'
                    f'cat {tmp_path}/dt-err >&2\nif [ {dt_rc} -eq 0 ]; then echo gcs-usage-snapshot-20261108; fi\nexit {dt_rc}\n',
        "disk-tree": f'printf "%s\\0" "$@" > {rec}/notify; echo "DISKY_NOTIFY=$DISKY_NOTIFY" > {rec}/notify-env\n',
        "date": 'case "$*" in *%H*) echo gcs-usage-snapshot-20261108-153000 ;; *) echo gcs-usage-snapshot-20261108 ;; esac\n',
    }
    for name, body in fakes.items():
        (bin_ / name).write_text("#!/usr/bin/env bash\n" + body)
        (bin_ / name).chmod(0o755)
    env = {"PATH": f"{bin_}:{environ['PATH']}", "BATCH_SPEC": spec, "CLOUD_RUN_EXECUTION": "gcs-usage-snapshot-submit-x7k2p", **env_extra}
    r = run(["bash", "-c", (ROOT_DIR / "job/scan-submit.sh").read_text()], env=env, capture_output=True, text=True)

    def recorded(name: str) -> str | None:
        return (rec / name).read_text() if (rec / name).exists() else None

    argv = lambda name: None if (text := recorded(name)) is None else text.split("\0")[:-1]  # noqa: E731
    return r.returncode, r.stdout, argv("dt"), recorded("spec"), argv("notify"), recorded("notify-env")


DT_ARGS = ["batch", "submit", "-c", "scan", "-p", "oa-internal-450019", "-L", "us-central1", "-t", "-e", "-r", "1800",
           "gcs-usage-snapshot-%Y%m%d"]


def test_scan_submit_names_the_day_and_alerts_nobody_on_success(tmp_path):
    exists = "note: Batch job gcs-usage-snapshot-20261108 already exists: counted as submitted"
    rc, out, dt, spec, notify, _ = scan_submit(tmp_path, 0, exists + "\n")
    assert (rc, dt[:-1], spec, notify) == (0, DT_ARGS, '{"labels": {"component": "scan"}}', None)
    assert out.splitlines() == [exists, "gcs-usage-snapshot-20261108"]


def test_scan_submit_posts_to_gcs_admin_when_the_submit_gives_up(tmp_path):
    failed = "Batch job gcs-usage-snapshot-20261108: submit failed 12 times over 1800s; the last: HTTP 502"
    retrying = "note: Batch job gcs-usage-snapshot-20261108: attempt 11 failed (HTTP 502); retrying in 300s"
    rc, _, dt, _, notify, notify_env = scan_submit(tmp_path, 1, f"{retrying}\n{failed}\n")
    logs = "https://console.cloud.google.com/run/jobs/executions/details/us-central1/gcs-usage-snapshot-submit-x7k2p/logs?project=oa-internal-450019"
    assert (rc, dt[:-1], notify_env) == (1, DT_ARGS, "DISKY_NOTIFY=slack\n")
    assert notify == [
        "notify", "-t", "gcs daily scan: not submitted", "-u", logs, "-l", "submitter logs",
        f"No scan today: Batch job gcs-usage-snapshot-20261108 was not created (exit 1). {failed}\n"
        "Re-run once Batch answers (a no-op if the job exists): "
        "`gcloud run jobs execute gcs-usage-snapshot-submit --region us-central1 --project oa-internal-450019`",
    ]


#: A spec as the cron's carries its trigger: every task group's env, `DT_TRIGGER=schedule`.
SCHEDULED = {"taskGroups": [{"taskSpec": {"environment": {"variables": {"DATA_BUCKET": "d", "DT_TRIGGER": "schedule"}}}}], "labels": {"component": "scan"}}


@pytest.mark.parametrize("trigger", [{}, {"DT_TRIGGER": "schedule"}])
def test_scan_submit_without_a_manual_trigger_is_the_cron_unchanged(tmp_path, trigger):
    spec = json.dumps(SCHEDULED)
    rc, _, dt, handed, notify, _ = scan_submit(tmp_path, 0, "", spec, **trigger)
    assert (rc, dt[:-1], handed, notify) == (0, DT_ARGS, spec, None)


def test_scan_submit_manual_names_the_second_and_marks_the_spec_manual(tmp_path):
    rc, _, dt, handed, notify, _ = scan_submit(tmp_path, 0, "", json.dumps(SCHEDULED), DT_TRIGGER="manual")
    assert (rc, dt[:-1], json.loads(handed), notify) == (
        0,
        ["batch", "submit", "-c", "scan", "-p", "oa-internal-450019", "-L", "us-central1", "-e", "-r", "1800", "gcs-usage-snapshot-20261108-153000"],
        {"taskGroups": [{"taskSpec": {"environment": {"variables": {"DATA_BUCKET": "d", "DT_TRIGGER": "manual"}}}}], "labels": {"component": "scan"}},
        None,
    )


def test_scan_submit_manual_alert_says_manual_and_points_at_the_site(tmp_path):
    failed = "Batch job gcs-usage-snapshot-20261108-153000: submit failed 12 times over 1800s; the last: HTTP 502"
    rc, _, _, _, notify, _ = scan_submit(tmp_path, 1, failed + "\n", json.dumps(SCHEDULED), DT_TRIGGER="manual")
    logs = "https://console.cloud.google.com/run/jobs/executions/details/us-central1/gcs-usage-snapshot-submit-x7k2p/logs?project=oa-internal-450019"
    assert (rc, notify) == (1, [
        "notify", "-t", "gcs manual scan: not submitted", "-u", logs, "-l", "submitter logs",
        f"The manual scan started from /scans didn't start: Batch job gcs-usage-snapshot-20261108-153000 was not created (exit 1). {failed}\n"
        "Start it again from https://gcs.oa.dev/scans once Batch answers.",
    ])


@pytest.mark.parametrize("trigger, spec, stderr", [
    ("cron", SCHEDULED, "DT_TRIGGER=cron: want schedule (the cron) or manual\n"),
    ("manual", {"taskGroups": [{"taskSpec": {"environment": {"variables": {"DT_TRIGGER": "manual"}}}}]},
     "BATCH_SPEC: want DT_TRIGGER=schedule in every task group's env, to make it manual\n"),
])
def test_scan_submit_refuses_an_unknown_trigger_or_a_spec_it_cant_mark(tmp_path, trigger, spec, stderr):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "dt-cloud").write_text("#!/usr/bin/env bash\necho submitted\n")
    (bin_ / "dt-cloud").chmod(0o755)
    env = {"PATH": f"{bin_}:{environ['PATH']}", "BATCH_SPEC": json.dumps(spec), "DT_TRIGGER": trigger}
    r = run(["bash", "-c", (ROOT_DIR / "job/scan-submit.sh").read_text()], env=env, capture_output=True, text=True)
    assert (r.returncode, r.stdout, r.stderr) == (2, "", stderr)
