"""Explicit, pinned Batch dispatch for the reviewed-generation executor."""

from __future__ import annotations

from copy import deepcopy
from shlex import join


def reviewed_job_spec(
    template: dict,
    source: str,
    plan: str,
    out: str,
    image: str,
    workers: int = 32,
    max_rate: int = 8000,
    for_real: bool = False,
    buckets: tuple[str, ...] = (),
    after_run: str | None = None,
    bucket_workers: int = 6,
    exclude_runs: tuple[str, ...] = (),
    pacing: str = "guided",
) -> dict:
    """Reuse deployment credentials/resources, not an old job's identity."""
    import re

    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("reviewed job image must be pinned by sha256 digest")
    if not 1 <= workers <= 64 or max_rate <= 0 or not 1 <= bucket_workers <= 6 or pacing not in ("guided", "adaptive"):
        raise ValueError("invalid reviewed deletion workers/rate")
    task = template["taskGroups"][0]["taskSpec"]
    environment = deepcopy(task["environment"])
    for key in ("DATA_BUCKET", "PLAN_ID", "PLAN_DIGEST", "D1_DB_ID", "SITE_URL"):
        if not environment["variables"].get(key):
            raise ValueError(f"reviewed job template lacks {key}")
    command = ["dt-cloud", "sweep", "execute-reviewed", "-e", "xml", "-j", str(workers), "-r", str(max_rate), "-p", plan, "-o", out, "-w", "/work/reviewed", "-B", str(bucket_workers), "-c", pacing]
    for run in exclude_runs:
        command.extend(["-x", run])
    for bucket in buckets:
        command.extend(["-b", bucket])
    if for_real:
        command.append("--for-real")
    command.append(source)
    script_lines = [
        "set -euo pipefail",
        'trap \'curl -fsS -m 60 -o /dev/null -H "Authorization: Bearer $SITE_TOKEN" "$SITE_URL/api/sweep/jobs" || true\' EXIT',
    ]
    if after_run:
        script_lines.append(join(["dt-cloud", "sweep", "wait-drained", after_run]))
    script = "\n".join([*script_lines, join(command)])
    allocation = template["allocationPolicy"]
    return {
        "taskGroups": [{"taskCount": 1, "taskSpec": {
            "runnables": [{"container": {"imageUri": image, "entrypoint": "/bin/bash", "commands": ["-c", script]}}],
            "computeResource": deepcopy(task["computeResource"]),
            "maxRetryCount": 0,
            "maxRunDuration": "259200s",
            "environment": environment,
        }}],
        "allocationPolicy": {
            "instances": [{"policy": deepcopy(allocation["instances"][0]["policy"])}],
            "serviceAccount": deepcopy(allocation["serviceAccount"]),
            "location": {"allowedLocations": [loc for loc in allocation["location"]["allowedLocations"] if loc.startswith("regions/")]},
        },
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def wait_drained(run: str, timeout: int = 7200, interval: float = 30) -> dict:
    """Wait for the old executor's post-pool-shutdown summary, never kill it."""
    import json
    import time

    import fsspec

    fs, path = fsspec.core.url_to_fs(run)
    if not fs.exists(f"{path}/STOP"):
        raise ValueError("handoff requires the old run's STOP marker")
    deadline = time.monotonic() + timeout
    while True:
        if fs.exists(f"{path}/deleted-summary.json"):
            with fs.open(f"{path}/deleted-summary.json") as fh:
                summary = json.load(fh)
            if summary.get("for_real") is not True or summary.get("plan") != run:
                raise ValueError("old run drain summary does not identify the real executor")
            return summary
        if time.monotonic() >= deadline:
            raise TimeoutError("old executor did not drain before the handoff timeout; no XML deletions started")
        from .sweep_exec import err

        err(f"handoff: waiting for {run}/deleted-summary.json (old executor still draining)", flush=True)
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
