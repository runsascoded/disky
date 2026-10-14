"""Opt-in split workers preserve publication, immutable config and attribution."""
from copy import deepcopy
from json import loads

import pytest
from click.testing import CliRunner
from requests import HTTPError, Response

from dt_cloud.scan_tasks import SHAPES, compatible, task_cmd, task_name, task_spec

IMAGE = "registry/scan@sha256:" + "a" * 64
SCAN = "2026-10-13T1300"
CONTEXT = {"JOB_IMAGE": IMAGE, "DATA_BUCKET": "data", "GEN": "20261013T130000Z", "SNAP_PATH": "snapshots/" + SCAN,
           "INDEX_PATH": "listing/" + SCAN + "/index/20261013T130000Z/path-index.parquet"}


def controller() -> dict:
    return {
        "uid": "root-uid", "name": "projects/p/locations/us-central1/jobs/root",
        "createTime": "2026-10-13T13:00:00Z", "status": {"state": "RUNNING"},
        "taskGroups": [{"taskCount": 1, "taskSpec": {
            "runnables": [{"container": {"imageUri": IMAGE, "volumes": ["/mnt/disks/stage:/stage:rw"]}}],
            "environment": {
                "variables": {"DISKY_LABELS": "app=disky,deployment=gcs", "SNAP_ID": SCAN,
                              "SPLIT_COMPUTE": "1", "DUCKDB_MEM": "8GB", "DUCKDB_THREADS": "4"},
                "secretVariables": {"TOKEN": "projects/p/secrets/token/versions/1"},
            },
            "volumes": [{"deviceName": "stage", "mountPath": "/mnt/disks/stage"}],
        }}],
        "allocationPolicy": {"serviceAccount": {"email": "job@p"}, "location": {"allowedLocations": ["regions/us-central1"]}},
    }


@pytest.mark.parametrize("stage, expected", [
    ("compute", {"machine": "n2-highmem-32", "memory_mib": 250000, "ssd_gb": 1500, "duckdb_mem": "100GB", "threads": "16"}),
    ("access", {"machine": "n2-highmem-16", "memory_mib": 123200, "ssd_gb": 750, "duckdb_mem": "48GB", "threads": "16"}),
])
def test_worker_shape_context_and_secret_references(stage, expected):
    root = controller()
    original = deepcopy(root)
    out = task_spec(root, stage, SCAN, {**CONTEXT, "TOKEN": "resolved-secret-not-to-copy", "OTHER": "not-config"})
    assert root == original
    assert SHAPES[stage] == expected
    task = out["taskGroups"][0]["taskSpec"]
    assert task["environment"]["secretVariables"] == {"TOKEN": "projects/p/secrets/token/versions/1"}
    assert task["environment"]["variables"] == {
        "DISKY_LABELS": "app=disky,deployment=gcs", "SNAP_ID": SCAN, "SCAN_ID": SCAN,
        "SPLIT_COMPUTE": "0", "SCAN_RUN_ID": "root-uid", "SCAN_ROOT_UID": "root-uid",
        "DUCKDB_MEM": expected["duckdb_mem"], "DUCKDB_THREADS": "16", "STAGE_DIR": "/stage",
        **CONTEXT,
        **({"SCAN_COMPUTE_ONLY": "1", "REPROC": "1", "SKIP_ACCESS": "1"} if stage == "compute" else {"ACCESS_ONLY": "1", "DUCKDB_MEM_ACCESS": "48GB", "DUCKDB_THREADS_ACCESS": "16"}),
    }
    assert task["computeResource"] == {"cpuMilli": 32000 if stage == "compute" else 16000, "memoryMib": expected["memory_mib"]}
    assert (task["maxRetryCount"], task["maxRunDuration"]) == (0, "21600s")
    assert task["runnables"] == [{"container": {"imageUri": IMAGE, "entrypoint": "bash", "commands": ["job/run.sh"], "volumes": ["/mnt/disks/stage:/stage:rw"]}}]
    assert out["allocationPolicy"]["instances"] == [{"policy": {
        "machineType": expected["machine"], "provisioningModel": "STANDARD",
        "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
        "disks": [{"newDisk": {"type": "local-ssd", "sizeGb": str(expected["ssd_gb"])}, "deviceName": "stage"}],
    }}]
    labels = {"app": "disky", "deployment": "gcs", "component": "scan-compute" if stage == "compute" else "access",
              "scan_run": "root-uid", "scan_id": "2026-10-13t1300", "stage": stage}
    assert out["allocationPolicy"]["labels"] == labels
    assert {k: v for k, v in out["labels"].items() if k != "task_config"} == {**labels, "purpose": "scan-pipeline"}
    assert len(out["labels"]["task_config"]) == 32
    assert task["volumes"] == original["taskGroups"][0]["taskSpec"]["volumes"]


@pytest.mark.parametrize("image", ["registry:latest", "registry@sha256:x", "registry@sha256:" + "a" * 63])
def test_bad_image_refused(image):
    with pytest.raises(ValueError, match="^split scan requires a digest-pinned JOB_IMAGE$"):
        task_spec(controller(), "compute", SCAN, {**CONTEXT, "JOB_IMAGE": image})


def test_worker_cannot_change_controller_image():
    with pytest.raises(ValueError, match="^worker image must match the controller's pinned image$"):
        task_spec(controller(), "compute", SCAN, {**CONTEXT, "JOB_IMAGE": "registry/scan@sha256:" + "b" * 64})


@pytest.mark.parametrize("missing", ["GEN", "SNAP_PATH", "INDEX_PATH"])
def test_missing_publication_identity_refused(missing):
    with pytest.raises(ValueError, match=f"^compute task needs {missing}$"):
        task_spec(controller(), "compute", SCAN, {k: v for k, v in CONTEXT.items() if k != missing})


def test_sweep_controller_refused():
    root = controller()
    root["taskGroups"][0]["taskSpec"]["environment"]["variables"]["SWEEP"] = "real"
    with pytest.raises(ValueError, match="^split tasks require a scan controller, not another job mode$"):
        task_spec(root, "compute", SCAN, CONTEXT)


def test_idempotent_name_and_immutable_config():
    assert [task_name("root-uid", stage, scan) for stage, scan in [("compute", SCAN), ("access", SCAN), ("compute", "2026-10-13T1400")]] == [
        "sc-compute-2026-10-13-1300-dc437d2b6821380d",
        "sc-access-2026-10-13-1300-dc437d2b6821380d",
        "sc-compute-2026-10-13-1400-dc437d2b6821380d",
    ]
    expected = task_spec(controller(), "compute", SCAN, CONTEXT)
    actual = deepcopy(expected)
    actual.update(uid="child-uid", status={"state": "RUNNING"})
    actual["taskGroups"][0]["name"] = "api-group-name"
    actual["taskGroups"][0]["taskSpec"].pop("maxRetryCount")
    actual["taskGroups"][0]["taskSpec"]["computeResource"]["memoryMib"] = "250000"
    assert compatible(actual, expected) is True
    actual["taskGroups"][0]["taskSpec"]["environment"]["variables"]["GATE"] = "1"
    assert compatible(actual, expected) is False
    actual = deepcopy(expected)
    actual["allocationPolicy"]["instances"][0]["policy"]["machineType"] = "n2-highmem-16"
    assert compatible(actual, expected) is False


@pytest.mark.parametrize("mode, state, expected_code", [("dry", "SUCCEEDED", 0), ("new", "SUCCEEDED", 0), ("resume", "SUCCEEDED", 0), ("resume", "FAILED", 1)])
def test_cli_submit_resume_and_failure(monkeypatch, mode, state, expected_code):
    from dt_cloud import batch, gcp

    root = controller()
    for k, v in {**CONTEXT, "BATCH_JOB_UID": "root-uid"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(gcp, "batch_jobs", lambda *_: [root])
    monkeypatch.setattr(gcp, "gcp_project", lambda: "p")
    spec = task_spec(root, "compute", SCAN, CONTEXT)
    name = task_name("root-uid", "compute", SCAN)
    calls = []
    logs = []
    monkeypatch.setattr("dt_cloud.static_names.err", logs.append)

    def existing(*args, **kwargs):
        if mode == "new":
            response = Response()
            response.status_code = 404
            raise HTTPError(response=response)
        return spec

    monkeypatch.setattr(gcp, "batch_job", existing)
    monkeypatch.setattr(batch, "submit_job", lambda s, n, r: calls.append(("submit", s, n, r)))

    def wait(jobs, interval, log):
        calls.append(("wait", jobs, interval))
        return {name: state}

    monkeypatch.setattr(batch, "wait_jobs", wait)
    result = CliRunner().invoke(task_cmd, ["-s", "compute", *(["-n"] if mode == "dry" else []), SCAN])
    assert result.exit_code == expected_code
    if mode == "dry":
        assert loads(result.output) == {"job": name, "shape": SHAPES["compute"], "labels": spec["labels"]}
        assert calls == []
    else:
        assert calls == ([("submit", spec, name, "us-central1")] if mode == "new" else []) + [("wait", [(name, "us-central1")], 15)]
        assert logs == [f"scan task {'submitted' if mode == 'new' else 'resumed'}: {name} [us-central1]"]
        assert result.output.splitlines() == ([f"{name}: FAILED"] if state == "FAILED" else [])


def test_worker_owns_ssd_mount_even_with_a_boot_disk_only_controller():
    root = controller()
    source = root["taskGroups"][0]["taskSpec"]
    source["volumes"] = [{"gcs": {"remotePath": "data"}, "mountPath": "/mnt/disks/gcs/data"}]
    source["runnables"][0]["container"]["volumes"] = ["/mnt/disks/gcs/data:/gcs/data:rw"]
    task = task_spec(root, "compute", SCAN, CONTEXT)["taskGroups"][0]["taskSpec"]
    assert task["volumes"] == [
        {"gcs": {"remotePath": "data"}, "mountPath": "/mnt/disks/gcs/data"},
        {"deviceName": "stage", "mountPath": "/mnt/disks/stage"},
    ]
    assert task["runnables"][0]["container"]["volumes"] == ["/mnt/disks/gcs/data:/gcs/data:rw", "/mnt/disks/stage:/stage:rw"]
