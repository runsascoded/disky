"""Opt-in scan stage workers: keep orchestration off the aggregation VM.

Clones only submission fields from the controller's Batch spec. Secrets remain
Secret Manager references, never copied from the runtime environment. Compute
keeps the known-good memory/disk limits; access keeps its own memory headroom.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256

from click import Choice, argument, command, option

from .cost_labels import label_batch_spec
from .scan_id import check_scan_id

SHAPES = {
    "compute": {"machine": "n2-highmem-32", "memory_mib": 250000, "ssd_gb": 1500, "duckdb_mem": "100GB", "threads": "16"},
    "access": {"machine": "n2-highmem-16", "memory_mib": 123200, "ssd_gb": 750, "duckdb_mem": "48GB", "threads": "16"},
}


def task_spec(
    root: Mapping,
    stage: str,
    scan: str,
    context: Mapping[str, str],
) -> dict:
    check_scan_id(scan)
    if stage not in SHAPES:
        raise ValueError(f"unknown scan task stage: {stage!r}")
    shape = SHAPES[stage]
    if len(root["taskGroups"]) != 1:
        raise ValueError("scan controller must have one task group")
    source = root["taskGroups"][0]["taskSpec"]
    source_vars = source.get("environment", {}).get("variables", {})
    if any(source_vars.get(k) not in (None, "", "0") for k in ("GATE", "SWEEP", "SWEEP_BENCHMARK_PLAN", "ACCESS_ONLY", "SCAN_COMPUTE_ONLY")):
        raise ValueError("split tasks require a scan controller, not another job mode")
    image = context.get("JOB_IMAGE", "")
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("split scan requires a digest-pinned JOB_IMAGE")
    if image != source["runnables"][0]["container"]["imageUri"]:
        raise ValueError("worker image must match the controller's pinned image")
    # The parent spec holds references, not resolved credentials. Copy those
    # references and config, never the caller's complete os.environ.
    environment = deepcopy(source.get("environment", {}))
    variables = environment.setdefault("variables", {})
    variables.update({k: v for k, v in context.items() if k in {
        "SCAN_RUN_ID", "GEN", "SNAP_PATH", "INDEX_PATH", "DATA_BUCKET", "JOB_IMAGE", "DISKY_LABELS",
    }})
    variables.update({
        "SNAP_ID": scan, "SCAN_ID": scan, "SCAN_ROOT_UID": root["uid"], "SPLIT_COMPUTE": "0",
        "SCAN_RUN_ID": variables.get("SCAN_RUN_ID") or root["uid"],
        "DUCKDB_MEM": shape["duckdb_mem"], "DUCKDB_THREADS": shape["threads"], "STAGE_DIR": "/stage",
    })
    variables.pop("NOP_IF_PUBLISHED", None)
    if stage == "compute":
        variables.update({"SCAN_COMPUTE_ONLY": "1", "REPROC": "1", "SKIP_ACCESS": "1"})
        variables.pop("ACCESS_ONLY", None)
        for name in ("GEN", "SNAP_PATH", "INDEX_PATH"):
            if not variables.get(name):
                raise ValueError(f"compute task needs {name}")
    else:
        variables["ACCESS_ONLY"] = "1"
        variables["DUCKDB_MEM_ACCESS"] = shape["duckdb_mem"]
        variables["DUCKDB_THREADS_ACCESS"] = "16"
        variables.pop("SCAN_COMPUTE_ONLY", None)
    group = {
        "taskCount": 1,
        "taskSpec": {
            "runnables": [{"container": {
                "imageUri": image, "entrypoint": "bash", "commands": ["job/run.sh"],
                "volumes": [v for v in source["runnables"][0]["container"].get("volumes", []) if not v.startswith("/mnt/disks/stage:")] + ["/mnt/disks/stage:/stage:rw"],
            }}],
            "environment": environment,
            "computeResource": {"cpuMilli": int(shape["machine"].rsplit("-", 1)[-1]) * 1000,
                                "memoryMib": shape["memory_mib"]},
            "maxRetryCount": 0,
            "maxRunDuration": "21600s",
            "volumes": deepcopy([v for v in source.get("volumes", []) if v.get("deviceName") != "stage"]) + [{"deviceName": "stage", "mountPath": "/mnt/disks/stage"}],
        },
    }
    spec = {
        "taskGroups": [group],
        "allocationPolicy": {
            "instances": [{"policy": {
                "machineType": shape["machine"], "provisioningModel": "STANDARD",
                "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
                "disks": [{"newDisk": {"type": "local-ssd", "sizeGb": str(shape["ssd_gb"])}, "deviceName": "stage"}],
            }}],
            "serviceAccount": deepcopy(root["allocationPolicy"]["serviceAccount"]),
            "location": deepcopy(root["allocationPolicy"]["location"]),
        },
        "labels": {"purpose": "scan-pipeline", "stage": stage},
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }
    label_env = {"DISKY_LABELS": variables.get("DISKY_LABELS", ""),
                 "SCAN_RUN_ID": variables.get("SCAN_RUN_ID") or root["uid"], "SCAN_ID": scan}
    spec = label_batch_spec(spec, "scan-compute" if stage == "compute" else "access", label_env, scan_id=scan)
    digest = sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    spec["labels"]["task_config"] = digest[:32]
    return spec


def task_name(root_uid: str, stage: str, scan: str) -> str:
    identity = sha256(root_uid.encode()).hexdigest()[:16]
    return f"sc-{stage}-{scan.lower().replace('t', '-')}-{identity}"


def compatible(actual: object, expected: object, key: str = "") -> bool:
    """Compare every submitted field, ignoring API output/default fields only."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        if key in ("variables", "secretVariables"):
            return actual == expected
        return all(compatible(actual.get(k, 0 if v == 0 else "STANDARD" if k == "provisioningModel" else None), v, k) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(compatible(a, e, key) for a, e in zip(actual, expected))
    if key in ("cpuMilli", "memoryMib", "taskCount", "sizeGb", "maxRetryCount"):
        return str(actual) == str(expected)
    return actual == expected


@command("task")
@option("-n", "--dry-run", is_flag=True, help="Print safe allocation/context summary; do not submit")
@option("-s", "--stage", type=Choice(list(SHAPES)), required=True, help="Dedicated scan stage")
@argument("scan")
def task_cmd(dry_run: bool, stage: str, scan: str) -> None:
    """Submit/resume one scan worker from inside its Batch controller and wait.

    Requires a pinned JOB_IMAGE; child failure fails this command. The caller
    decides whether access-ingest failure is nonfatal. No production defaults
    or runtime secret values are embedded in the new job spec.
    """
    from requests import HTTPError

    from .batch import submit_job, wait_jobs
    from .gcp import REGION, batch_job, batch_jobs, gcp_project
    from .static_names import err

    uid = os.environ.get("BATCH_JOB_UID")
    if not uid:
        raise ValueError("scan task requires BATCH_JOB_UID inside its controller")
    roots = [j for j in batch_jobs(gcp_project(), REGION) if j.get("uid") == uid]
    if len(roots) != 1:
        raise ValueError(f"expected one Batch controller for {uid!r}; found {len(roots)}")
    root = roots[0]
    spec = task_spec(root, stage, scan, os.environ)
    name = task_name(root["uid"], stage, scan)
    if dry_run:
        print(json.dumps({"job": name, "shape": SHAPES[stage], "labels": spec["labels"]}, sort_keys=True))
        return
    try:
        existing = batch_job(name, region=REGION)
    except HTTPError as error:
        if error.response.status_code != 404:
            raise
        submit_job(spec, name, REGION)
        err(f"scan task submitted: {name} [{REGION}]")
    else:
        if not compatible(existing, spec):
            raise ValueError(f"{name}: existing task differs from the requested immutable configuration")
        err(f"scan task resumed: {name} [{REGION}]")
    states = wait_jobs([(name, REGION)], interval=15, log=err)
    if states[name] != "SUCCEEDED":
        raise SystemExit(f"{name}: {states[name]}")
