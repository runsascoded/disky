from copy import deepcopy
import json

import pytest
from click.testing import CliRunner

from dt_cloud import gcp
from dt_cloud.cli import main
from dt_cloud.scan_cost import job_estimate, render_report, report, select_jobs, task_usage, timestamp
from dt_cloud.scan_runs import Profile

NOW = timestamp("2026-10-13T15:00:00Z")
PRICES = Profile(prices={"n2-highmem-16": 1.0}, spot_factor=0.3)


def job(
    name="root",
    uid="root-uid",
    labels=None,
    state="SUCCEEDED",
    created="2026-10-13T12:00:00Z",
    tasks=1,
):
    return {
        "name": f"projects/p/locations/us-east1/jobs/{name}", "uid": uid,
        "createTime": created, "updateTime": "2026-10-13T14:00:00Z",
        "status": {"state": state}, "labels": labels or {"app": "disky", "deployment": "gcs", "component": "scan"},
        "allocationPolicy": {"instances": [{"policy": {"machineType": "n2-highmem-16", "provisioningModel": "SPOT",
                                                        "bootDisk": {"type": "pd-balanced", "sizeGb": "100"}}}]},
        "taskGroups": [{"name": f"projects/p/locations/us-east1/jobs/{name}/taskGroups/group0", "taskCount": str(tasks)}],
    }


def task(events, name="t0", state="SUCCEEDED"):
    return {"name": name, "status": {"state": state, "statusEvents": [
        {"eventTime": f"2026-10-13T{time}Z", "taskState": status,
         "description": f"Task state is updated on zones/us-east1-c/instances/{vm}" if vm else ""}
        for time, status, vm in events
    ]}}


def hour_task():
    return task([("12:00:00", "ASSIGNED", "10"), ("12:00:00", "RUNNING", "10"), ("13:00:00", "SUCCEEDED", "10")])


def test_packed_tasks_union_vm_intervals_and_retries():
    a = task([("12:00:00", "ASSIGNED", "10"), ("12:00:10", "RUNNING", "10"),
              ("12:01:00", "FAILED", "10"), ("12:02:00", "ASSIGNED", "11"),
              ("12:02:10", "RUNNING", "11"), ("12:03:00", "SUCCEEDED", "11")])
    b = task([("12:00:20", "ASSIGNED", "10"), ("12:00:30", "RUNNING", "10"), ("12:01:20", "SUCCEEDED", "10")], name="t1")
    assert task_usage([a, b], NOW) == {"attempts": 3, "task_seconds": 150.0, "observed_vm_seconds": 140.0, "observed_vms": 2, "warnings": []}


def test_live_tasks_extend_to_report_clock():
    live = task([("14:00:00", "RUNNING", "11")], state="RUNNING")
    assert task_usage([live], NOW) == {"attempts": 1, "task_seconds": 3600.0, "observed_vm_seconds": 3600.0, "observed_vms": 1, "warnings": []}


def test_missing_vm_identity_is_not_a_zero_cost():
    t = task([("12:00:00", "RUNNING", None), ("13:00:00", "SUCCEEDED", None)])
    got = job_estimate(PRICES, job(), [t], NOW, "root")
    assert (got["estimated_compute_usd"], got["warnings"]) == (None, ["t0: assignment has no VM identity"])


def test_no_task_coverage_is_explicitly_partial():
    got = job_estimate(PRICES, job(), [], NOW, "root")
    assert (got["estimated_compute_usd"], got["warnings"]) == (None, ["task coverage incomplete: 0/1"])


def test_unknown_spot_price_is_not_on_demand():
    got = job_estimate(Profile(prices={"n2-highmem-16": 1}), job(), [hour_task()], NOW, "root")
    assert (got["estimated_compute_usd"], got["warnings"]) == (None, ["no configured Spot multiplier"])


def test_label_association_includes_detached_work_not_other_attempts():
    root = job()
    detached = job("merge", "merge-uid", {"scan_run": "root-uid"}, state="RUNNING", created="2026-10-13T14:30:00Z")
    other = job("retry", "other-uid", {"scan_run": "other-root", "scan_id": "2026-10-13t1300"})
    assert [(j["uid"], a) for j, a in select_jobs([other, root, detached], root, "2026-10-13T1300")] == [
        ("merge-uid", "scan_run_label"), ("root-uid", "root"),
    ]


def test_legacy_requires_exact_scan_window_and_deployment():
    root = job()
    child = job("sn-anchors-2026-10-13-1300-123000", "child", {"app": "disky", "deployment": "gcs", "component": "anchors"})
    wrong_scan = job("sn-anchors-2026-10-13-1301-123000", "wrong-scan")
    late = job("sn-anchors-2026-10-13-1300-143000", "late", created="2026-10-13T14:30:00Z")
    wrong_deploy = deepcopy(child)
    wrong_deploy["uid"] = "cw"
    wrong_deploy["labels"]["deployment"] = "cw"
    all_jobs = [root, child, wrong_scan, late, wrong_deploy]
    assert [(j["uid"], a) for j, a in select_jobs(all_jobs, root, "2026-10-13T1300")] == [("root-uid", "root")]
    assert [(j["uid"], a) for j, a in select_jobs(all_jobs, root, "2026-10-13T1300", legacy=True)] == [
        ("root-uid", "root"), ("child", "legacy_scan_and_window"),
    ]


def test_legacy_includes_numeric_r2_stage():
    root = job()
    child = job("sn-r2-2026-10-13-1300-123000", "r2-uid")
    assert [(j["uid"], a) for j, a in select_jobs([root, child], root, "2026-10-13T1300", legacy=True)] == [
        ("root-uid", "root"), ("r2-uid", "legacy_scan_and_window"),
    ]


def test_wrong_scan_refuses():
    root = job(labels={"scan_id": "2026-10-13t1300"})
    with pytest.raises(ValueError) as e:
        select_jobs([root], root, "2026-10-13T1301")
    assert str(e.value) == "root job is labeled for 2026-10-13t1300, not 2026-10-13T1301"


def test_exact_job_estimate_and_report():
    root = job()
    got = job_estimate(PRICES, root, [hour_task()], NOW, "root")
    assert got == {
        "job": "root", "uid": "root-uid", "region": "us-east1", "association": "root", "component": "scan", "stage": None,
        "scan_id": None, "state": "SUCCEEDED", "machine": "n2-highmem-16", "provisioning_model": "SPOT", "tasks": 1,
        "attempts": 1, "task_seconds": 3600.0, "observed_vm_seconds": 3600.0, "observed_vms": 1,
        "configured_hourly_usd": 0.3, "estimated_compute_usd": 0.3,
        "disks_per_vm": [{"type": "pd-balanced", "size_gb": 100}], "warnings": [],
    }
    doc = report(PRICES, root, "2026-10-13T1300", [(root, "root")], {"root-uid": [hour_task()]}, NOW)
    assert {k: v for k, v in doc.items() if k != "exclusions"} == {
        "scan": "2026-10-13T1300", "scan_run": "root-uid", "root_uid": "root-uid", "as_of": "2026-10-13T15:00:00+00:00",
        "estimate_basis": "union of observed task assignment intervals per VM, at configured profile rates",
        "legacy_inference": False, "ongoing_jobs": [], "unpriced_jobs": [], "estimated_compute_usd": 0.3,
        "components": {"scan": 0.3}, "billing_filter": {"root_batch_job_uid": "root-uid", "child_scan_run": "root-uid"}, "jobs": [got],
    }
    assert render_report(doc).splitlines() == [
        "2026-10-13T1300 · root-uid", "Observed compute estimate (not billed cost):",
        "  scan/-  SUCCEEDED  n2-highmem-16  1.000 VM-h  $0.3000  root",
        "Total priced observed compute: $0.3000",
        "Rates: configured on-demand prices and Spot multiplier; not live regional billing rates.",
        "Excluded: VM time before task assignment and after task completion.",
        "Excluded: boot disks and local SSDs.",
        "Excluded: network transfers, storage operations, and persistent index storage.",
        "Excluded: billing credits, negotiated discounts, and actual Spot prices.",
    ]


def test_api_pagination_and_tasks(monkeypatch):
    calls = []
    def get(url, **params):
        calls.append((url, params))
        return {"tasks": [{"name": "a"}], "nextPageToken": "next"} if not params else {"tasks": [{"name": "b"}]}
    monkeypatch.setattr(gcp, "_get", get)
    assert gcp.batch_tasks(job()) == [{"name": "a"}, {"name": "b"}]
    assert calls == [
        ("https://batch.googleapis.com/v1/projects/p/locations/us-east1/jobs/root/taskGroups/group0/tasks", {}),
        ("https://batch.googleapis.com/v1/projects/p/locations/us-east1/jobs/root/taskGroups/group0/tasks", {"pageToken": "next"}),
    ]


def test_cli_json_saved_read_only(tmp_path, monkeypatch):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"project": "p", "regions": ["us-east1"], "prices": {"n2-highmem-16": 1}, "spot_factor": .3}))
    monkeypatch.setattr(gcp, "batch_jobs", lambda project, region: [job()])
    monkeypatch.setattr(gcp, "batch_tasks", lambda j: [hour_task()])
    out = tmp_path / "cost.json"
    result = CliRunner().invoke(main, ["scan-run", "cost", "-c", str(profile), "-s", "2026-10-13T1300", "-j", "-o", str(out), "root-uid"])
    assert (result.exit_code, result.output) == (0, "")
    saved = json.loads(out.read_text())
    assert (saved["estimated_compute_usd"], saved["components"], saved["ongoing_jobs"], saved["unpriced_jobs"]) == (0.3, {"scan": .3}, [], [])
