"""GCP Batch job specs + submit/wait for the DIY fleet-listing fan-out.

DIY mode lists all buckets ourselves instead of depending on SII reports
(whose generation times scatter 02:26-13:00 UTC, lag ~30h on day one, and are
plain unavailable in us-central2). Buckets are embarrassingly parallel — one
Batch *task* per bucket (``BATCH_TASK_INDEX`` picks from the bucket list), so
fleet wall-clock ~= the slowest bucket. Within a bucket, ``bulk-list``'s
own procs x threads prefix streams do the scaling.
"""
from __future__ import annotations

import time
from typing import Sequence

from .gcp import PROJECT, REGION, batch_job, session

FLEET_BUCKETS = [
    "marin-us-central2",
    "marin-eu-west4",
    "marin-us-central1",
    "marin-us-east5",
    "marin-us-east1",
    "marin-us-west4",
]
# List from a VM near the bucket: cross-ocean list pages pay full RTT per page
# (eu-west4 from us-central1 measured ~10x slower than same-continent). US
# buckets do fine from us-central1; central2 must anyway (no Batch in the
# limited TPU region).
BUCKET_JOB_REGIONS = {"marin-eu-west4": "europe-west4"}
DATA_BUCKET = "oa-gcs-usage-dvx"
SERVICE_ACCOUNT = f"gcs-usage-job@{PROJECT}.iam.gserviceaccount.com"
IMAGE = f"us-central1-docker.pkg.dev/{PROJECT}/cloud-run-source-deploy/gcs-usage-snapshot:latest"


def listing_dir(data_bucket: str, date: str, bucket: str) -> str:
    """Canonical per-bucket listing location (DIY layout)."""
    return f"{data_bucket}/listing/{date}/{bucket}"


def listing_job_spec(
    date: str,
    buckets: Sequence[str] = tuple(FLEET_BUCKETS),
    data_bucket: str = DATA_BUCKET,
    machine: str = "n2-standard-32",
    procs: int = 24,
    threads: int = 10,
    region: str = REGION,
) -> dict:
    """One task per bucket; each runs ``disk-tree bulk-list`` straight to gs://.

    Chunk weights come from, in order: the newest prior completed listing of
    the bucket (DIY layout, then legacy ``central2-listing/`` for central2),
    else the bucket's newest SII inventory report — unweighted chunks let one
    worker draw a 100M-object prefix and straggle for hours (eu-west4's first
    run). All fleet buckets are FUSE-mounted read-only for the weights scan;
    object pages stream via the API and shards write via gs://.
    """
    script = f"""#!/usr/bin/env bash
set -euxo pipefail
BUCKETS=({" ".join(buckets)})
b=${{BUCKETS[$BATCH_TASK_INDEX]}}
W=()
for d in $(ls -d /gcs/{data_bucket}/listing/*/$b /gcs/{data_bucket}/central2-listing/* 2>/dev/null | sort -r); do
  case "$d" in */listing/{date}/$b) continue;; */central2-listing/*) [ "$b" = marin-us-central2 ] || continue;; esac
  if [ -f "$d/_SUCCESS.json" ]; then W=(-W "$d/*.parquet"); break; fi
done
if [ ${{#W[@]}} -eq 0 ]; then
  sii=$(ls "/gcs/$b/inventory-reports/" 2>/dev/null | grep -oE '[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' | sort -ru | head -1)
  [ -n "$sii" ] && W=(-W "/gcs/$b/inventory-reports/*_${{sii}}T*_*.parquet")
fi
disk-tree bulk-list "gcs://$b" -o "gs://{listing_dir(data_bucket, date, "$b")}" -P {procs} -w {threads} -x reuse "${{W[@]}}"
"""
    mounts = [(data_bucket, "rw")] + [(b, "ro") for b in FLEET_BUCKETS]
    # Size the task request off the machine, not a constant: a hardcoded
    # cpuMilli that exceeds the chosen machine's vCPUs is a hard 400 from Batch
    # ("machine_type cannot satisfy compute_resource"). Leave 2 vCPU for the
    # agent; listing is CPU-bound so memory can be generous-but-modest.
    vcpus = int(machine.rsplit("-", 1)[-1])
    return {
        "taskGroups": [
            {
                "taskCount": len(buckets),
                "parallelism": len(buckets),
                "taskSpec": {
                    "runnables": [
                        {
                            "container": {
                                "imageUri": IMAGE,
                                "entrypoint": "/bin/bash",
                                "commands": ["-c", script],
                                "volumes": [f"/mnt/disks/gcs/{b}:/gcs/{b}:{m}" for b, m in mounts],
                            }
                        }
                    ],
                    "computeResource": {"cpuMilli": (vcpus - 2) * 1000, "memoryMib": vcpus * 1500},
                    "maxRetryCount": 1,
                    "maxRunDuration": "14400s",
                    "volumes": [
                        {
                            "gcs": {"remotePath": b},
                            "mountPath": f"/mnt/disks/gcs/{b}",
                            "mountOptions": ["--implicit-dirs"],
                        }
                        for b, _ in mounts
                    ],
                },
            }
        ],
        "allocationPolicy": {
            "instances": [{"policy": {"machineType": machine, "bootDisk": {"type": "pd-balanced", "sizeGb": "50"}}}],
            "serviceAccount": {"email": SERVICE_ACCOUNT},
            "location": {"allowedLocations": [f"regions/{region}"]},
        },
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def submit_job(spec: dict, job_id: str | None = None, region: str = REGION) -> str:
    """POST a Batch job; returns its short name (server-generated if no id)."""
    url = f"https://batch.googleapis.com/v1/projects/{PROJECT}/locations/{region}/jobs"
    params = {"job_id": job_id} if job_id else None
    r = session().post(url, json=spec, params=params)
    r.raise_for_status()
    return r.json()["name"].rsplit("/", 1)[-1]


def wait_jobs(jobs: Sequence[tuple[str, str]], interval: int = 60, log=None) -> dict[str, str]:
    """Poll (name, region) Batch jobs to terminal states; returns name -> state."""
    states: dict[str, str] = {}
    while len(states) < len(jobs):
        for name, region in jobs:
            if name in states:
                continue
            state = batch_job(name, region=region)["status"].get("state", "?")
            if log:
                log(f"{name} [{region}]: {state}")
            if state in ("SUCCEEDED", "FAILED", "DELETION_IN_PROGRESS"):
                states[name] = state
        if len(states) < len(jobs):
            time.sleep(interval)
    return states
