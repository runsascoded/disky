"""GCP Batch job specs + submit/wait for the DIY fleet-listing fan-out.

DIY mode lists all buckets ourselves instead of depending on SII reports
(whose generation times scatter 02:26-13:00 UTC, lag ~30h on day one, and are
plain unavailable in us-central2). Buckets are embarrassingly parallel — one
Batch *task* per bucket (``BATCH_TASK_INDEX`` picks from the bucket list), so
fleet wall-clock ~= the slowest bucket. Within a bucket, ``bulk-list``'s
own procs x threads prefix streams do the scaling.
"""
from __future__ import annotations

import json
import time
from typing import Mapping, Sequence

from .deploy import data_bucket as env_data_bucket, env, require, words
from .gcp import REGION, batch_job, gcp_project, session


def fleet_buckets() -> list[str]:
    """The buckets the listing covers (`$FLEET_BUCKETS`, space-separated), when
    `submit-listing` names none with `-b`."""
    return words("FLEET_BUCKETS", what="the buckets to list (or pass -b per bucket)")


def listing_regions() -> dict[str, str]:
    """`{bucket: region}` (`$LISTING_REGIONS`, JSON): list a bucket from a VM near
    it. Cross-ocean list pages pay full RTT per page (a European bucket listed
    from us-central1 measured ~10x slower than same-continent). Unset = every
    bucket from `REGION`."""
    return json.loads(env("LISTING_REGIONS") or "{}")


def job_sa() -> str:
    """The service account the listing job runs as (`$JOB_SA`)."""
    return require("JOB_SA", what="the service account the listing job runs as")


def job_image() -> str:
    """The listing job's container image (`$JOB_IMAGE`)."""
    return require("JOB_IMAGE", what="the listing job's container image")


def listing_dir(data_bucket: str, date: str, bucket: str) -> str:
    """Canonical per-bucket listing location (DIY layout)."""
    return f"{data_bucket}/listing/{date}/{bucket}"


def listing_job_spec(
    date: str,
    buckets: Sequence[str],
    data_bucket: str | None = None,
    machine: str = "n2-standard-32",
    procs: int = 24,
    threads: int = 10,
    region: str = REGION,
    legacy: Mapping[str, str] | None = None,
    image: str | None = None,
    service_account: str | None = None,
) -> dict:
    """One task per bucket; each runs ``disk-tree bulk-list`` straight to gs://.

    Chunk weights come from, in order: the newest prior completed listing of
    the bucket (DIY layout, then any ``legacy`` dir named for that bucket:
    ``{bucket: <subdir of the data bucket>}``), else the bucket's newest SII
    inventory report — unweighted chunks let one worker draw a 100M-object
    prefix and straggle for hours. The data bucket and the job's buckets are
    FUSE-mounted (the buckets read-only, for the weights scan); object pages
    stream via the API and shards write via gs://.
    """
    data_bucket = data_bucket or env_data_bucket()
    legacy = dict(legacy or {})
    old_dirs = "".join(f" /gcs/{data_bucket}/{sub}/*" for sub in legacy.values())
    old_cases = "".join(f' */{sub}/*) [ "$b" = {b} ] || continue;;' for b, sub in legacy.items())
    script = f"""#!/usr/bin/env bash
set -euxo pipefail
BUCKETS=({" ".join(buckets)})
b=${{BUCKETS[$BATCH_TASK_INDEX]}}
W=()
for d in $(ls -d /gcs/{data_bucket}/listing/*/$b{old_dirs} 2>/dev/null | sort -r); do
  case "$d" in */listing/{date}/$b) continue;;{old_cases} esac
  if [ -f "$d/_SUCCESS.json" ]; then W=(-W "$d/*.parquet"); break; fi
done
if [ ${{#W[@]}} -eq 0 ]; then
  sii=$(ls "/gcs/$b/inventory-reports/" 2>/dev/null | grep -oE '[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' | sort -ru | head -1)
  [ -n "$sii" ] && W=(-W "/gcs/$b/inventory-reports/*_${{sii}}T*_*.parquet")
fi
disk-tree bulk-list "gcs://$b" -o "gs://{listing_dir(data_bucket, date, "$b")}" -P {procs} -w {threads} -x reuse "${{W[@]}}"
"""
    mounts = [(data_bucket, "rw")] + [(b, "ro") for b in buckets]
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
                                "imageUri": image or job_image(),
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
            "serviceAccount": {"email": service_account or job_sa()},
            "location": {"allowedLocations": [f"regions/{region}"]},
        },
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def submit_job(spec: dict, job_id: str | None = None, region: str = REGION) -> str:
    """POST a Batch job; returns its short name (server-generated if no id)."""
    url = f"https://batch.googleapis.com/v1/projects/{gcp_project()}/locations/{region}/jobs"
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
