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
import re
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from .cost_labels import check_spec, label_batch_spec
from .deploy import batch_region, data_bucket as env_data_bucket, env, require, words
from .gcp import batch_job, gcp_project, session


def fleet_buckets() -> list[str]:
    """The buckets the listing covers (`$FLEET_BUCKETS`, space-separated), when
    `submit-listing` names none with `-b`."""
    return words("FLEET_BUCKETS", what="the buckets to list (or pass -b per bucket)")


def listing_regions() -> dict[str, str]:
    """`{bucket: region}` (`$LISTING_REGIONS`, JSON): list a bucket from a VM near
    it. Cross-ocean list pages pay full RTT per page (a European bucket listed
    from another continent measured ~10x slower than same-continent). A bucket
    it doesn't name is listed from `$BATCH_REGION` (`deploy.batch_region`)."""
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
    region: str | None = None,
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
    data_bucket, region = data_bucket or env_data_bucket(), batch_region(region)
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
    return label_batch_spec({
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
    }, "listing", scan_id=date)


#: A multi-region bucket location → the prefix of the regions inside it. By name, so by continent: `US` is every `us-*`
#: region; `EU` takes every `europe-*` one (London and Zurich too, which the EU multi-region stores nothing in); the
#: `northamerica-*` / `southamerica-*` regions are in none.
MULTI_REGIONS = {"US": "us-", "EU": "europe-", "ASIA": "asia-"}
#: A predefined dual-region location → its two regions (a configurable one names its own: `customPlacementConfig`).
DUAL_REGIONS = {
    "ASIA1": ("asia-northeast1", "asia-northeast2"),
    "EUR4": ("europe-north1", "europe-west4"),
    "EUR5": ("europe-west1", "europe-west2"),
    "EUR7": ("europe-west2", "europe-west3"),
    "EUR8": ("europe-west3", "europe-west6"),
    "NAM4": ("us-central1", "us-east1"),
}
#: `$BATCH_CROSS_REGION=1`: `submit_job`'s `cross_region` for a submitter with no flag of its own (the static-names and
#: interval-store runners, `scan-run task`).
CROSS_REGION_ENV = "BATCH_CROSS_REGION"

_LOCATIONS: dict[str, dict | str] = {}


class CrossRegion(ValueError):
    """A Batch job would run outside the region of a bucket it mounts or names (`check_region`)."""


def bucket_location(bucket: str) -> dict | str:
    """`bucket`'s `location`, `locationType` and `customPlacementConfig` (`buckets.get`), or why they can't be read
    (`HTTP <status>`: no `storage.buckets.get` there, or no such bucket). One GET per bucket per process."""
    if bucket not in _LOCATIONS:
        r = session().get(f"https://storage.googleapis.com/storage/v1/b/{bucket}", params={"fields": "location,locationType,customPlacementConfig"})
        _LOCATIONS[bucket] = r.json() if r.status_code == 200 else f"HTTP {r.status_code}"
    return _LOCATIONS[bucket]


def in_location(region: str, location: Mapping) -> bool | None:
    """Whether a VM in `region` is inside a bucket's `location` (`bucket_location`): the region itself; one of a
    dual-region's two (`DUAL_REGIONS`, or its `customPlacementConfig.dataLocations`); any region of a multi-region's
    continent (`MULTI_REGIONS`). None: a multi- or dual-region code neither table knows."""
    if placed := (location.get("customPlacementConfig") or {}).get("dataLocations"):
        return region in {r.lower() for r in placed}
    code = location["location"].upper()
    if code in DUAL_REGIONS:
        return region in DUAL_REGIONS[code]
    if code in MULTI_REGIONS:
        return region.startswith(MULTI_REGIONS[code])
    return None if location.get("locationType") in ("multi-region", "dual-region") else region == code.lower()


def job_regions(spec: Mapping, region: str) -> list[str]:
    """The regions a job's VMs may run in: its `allocationPolicy.location.allowedLocations` (`regions/<r>`, or a
    `zones/<r>-<z>`'s region), else the `region` it is submitted to."""
    allowed = ((spec.get("allocationPolicy") or {}).get("location") or {}).get("allowedLocations") or []
    regions = [name.rsplit("-", 1)[0] if kind == "zones" else name for kind, _, name in (a.partition("/") for a in allowed)]
    return list(dict.fromkeys(regions)) or [region]


def mounted_buckets(spec: Mapping) -> list[str]:
    """The buckets a job's tasks mount by gcsfuse (`taskSpec.volumes[].gcs.remotePath`, a bucket or `<bucket>/<dir>`)."""
    return list(dict.fromkeys(v["gcs"]["remotePath"].split("/", 1)[0] for g in spec.get("taskGroups", [])
                              for v in g.get("taskSpec", {}).get("volumes", []) if v.get("gcs", {}).get("remotePath")))


def check_region(spec: Mapping, region: str, buckets: Sequence[str] = (), what: str = "Batch job") -> list[str]:
    """Refuse (`CrossRegion`) a job whose VMs (`job_regions`) run outside the location of a bucket it mounts
    (`mounted_buckets`) or that its submitter names (`buckets`): every byte it reads or writes there is billed as
    inter-region transfer. Returns a note per bucket it could not check (its location unreadable, or a location code
    `in_location` doesn't know). A bucket the job reaches only by URL, unnamed by its submitter, is not seen."""
    regions, notes, far = job_regions(spec, region), [], []
    for b in dict.fromkeys([*mounted_buckets(spec), *buckets]):
        location = bucket_location(b)
        if isinstance(location, str):
            notes.append(f"{what}: bucket {b}: location unreadable ({location}); its region is not checked")
            continue
        inside = [in_location(r, location) for r in regions]
        if None in inside:
            notes.append(f"{what}: bucket {b}: location {location['location']} ({location.get('locationType')}) is not one this check knows; its region is not checked")
        elif not all(inside):
            placed = (location.get("customPlacementConfig") or {}).get("dataLocations")
            far.append(f"{b} ({'+'.join(placed) if placed else location['location']})")
    if far:
        raise CrossRegion(f"{what} in {', '.join(regions)}: not in the region of its bucket(s) {', '.join(far)}. Run it beside them, or pass "
                          f"-X/--cross-region (or export {CROSS_REGION_ENV}=1) to submit it and pay for the transfer")
    return notes


#: Batch's rule for a job id (`jobs.create`'s `job_id`): at most 63 characters, a lowercase letter first, then lowercase
#: letters, digits and `-`, never ending in `-`.
JOB_ID_RE = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
#: The statuses a retried submit (`submit_job`'s `retry_for`) tries again: rate limited, and the server's own failures.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
#: A retried submit's first wait and its cap (each wait doubles the last), and each attempt's (connect, read) timeout,
#: in seconds.
RETRY_FIRST_S, RETRY_CAP_S, ATTEMPT_TIMEOUT_S = 5.0, 300.0, (10, 60)


class SubmitFailed(RuntimeError):
    """A retried submit (`submit_job`'s `retry_for`) still failing when its window ran out."""


def check_job_id(job_id: str) -> str:
    """`job_id`, or a `ValueError` naming Batch's rule (`JOB_ID_RE`): Batch refuses any other id with a 400, which no
    retry fixes."""
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError(f"Batch job id {job_id!r}: at most 63 characters, a lowercase letter first, then lowercase letters, "
                         "digits and '-', not ending in '-'")
    return job_id


def job_id_at(template: str, when: datetime) -> str:
    """`template` as a `strftime` format over `when` in UTC, checked (`check_job_id`): e.g. `scan-%Y%m%d` names one job
    per UTC day, so every attempt to submit that day's job, a retry's or a repeated trigger's, names the same one."""
    return check_job_id(when.astimezone(timezone.utc).strftime(template))


def _transient(e: BaseException) -> bool:
    """A connection-level failure a retried submit tries again: refused, reset, timed out, or the token fetch failing."""
    import requests
    from google.auth.exceptions import TransportError

    return isinstance(e, (requests.ConnectionError, requests.Timeout, TransportError))


def submit_job(spec: dict, job_id: str | None = None, region: str | None = None, project: str | None = None, *,
               buckets: Sequence[str] = (), cross_region: bool | None = None, exists_ok: bool = False,
               retry_for: float = 0, sleep: Callable[[float], None] = time.sleep,
               clock: Callable[[], float] = time.monotonic) -> str:
    """POST a Batch job (in `region`, default `$BATCH_REGION`; in `project`, default `gcp_project()`); returns its short
    name (server-generated if no id).

    The one place dt-cloud creates a Batch job (`tests/test_cost_labels.py` greps for others), so the two refusals are
    here, before the POST: a spec missing a required cost label on the job or its allocation policy
    (`cost_labels.check_spec`), and a job outside the region of the buckets it mounts or `buckets` names (`check_region`;
    its notes go to stderr) unless `cross_region` (default `$BATCH_CROSS_REGION`) says so.

    `exists_ok`: a job already named `job_id` (409) counts as submitted, and its name is returned. `retry_for` (seconds;
    needs a `job_id` and `exists_ok`): a 429 or 5xx (`RETRY_STATUS`) or a connection failure is tried again, after
    `RETRY_FIRST_S`, each wait doubling up to `RETRY_CAP_S`, until that long has passed; then `SubmitFailed`. The id is
    what makes a retry safe: an attempt whose answer was lost after Batch created the job meets that job on the next
    one (409), never starts a second. Each retried attempt is bounded (`ATTEMPT_TIMEOUT_S`). Any other refusal is raised
    at once."""
    what = f"Batch job {job_id or '(unnamed)'}"
    if retry_for and not (job_id and exists_ok):
        raise ValueError(f"{what}: a retried submit needs a job id and exists_ok: without them, a retry after a lost answer starts a second job")
    check_spec(spec, what)
    region = batch_region(region)
    if cross_region is None:
        cross_region = env(CROSS_REGION_ENV) == "1"
    if not cross_region:
        for note in check_region(spec, region, buckets, what):
            print(f"note: {note}", file=sys.stderr)
    url = f"https://batch.googleapis.com/v1/projects/{project or gcp_project()}/locations/{region}/jobs"
    params = {"job_id": job_id} if job_id else None
    bounded = {"timeout": ATTEMPT_TIMEOUT_S} if retry_for else {}
    deadline, wait, attempt = clock() + retry_for, RETRY_FIRST_S, 0
    while True:
        attempt += 1
        try:
            r = session().post(url, json=spec, params=params, **bounded)
        except Exception as e:  # noqa: BLE001 — only a transient failure of a retried submit is caught
            if not (retry_for and _transient(e)):
                raise
            failure = f"{type(e).__name__}: {e}"
        else:
            if exists_ok and r.status_code == 409:
                print(f"note: {what} already exists: counted as submitted", file=sys.stderr)
                return job_id
            if not (retry_for and r.status_code in RETRY_STATUS):
                r.raise_for_status()
                return r.json()["name"].rsplit("/", 1)[-1]
            failure = f"HTTP {r.status_code}"
        left = deadline - clock()
        if left <= 0:
            raise SubmitFailed(f"{what}: submit failed {attempt} times over {retry_for:g}s; the last: {failure}")
        pause = min(wait, left)
        print(f"note: {what}: attempt {attempt} failed ({failure}); retrying in {pause:g}s", file=sys.stderr)
        sleep(pause)
        wait = min(wait * 2, RETRY_CAP_S)


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


LIVE_STATES = ("QUEUED", "SCHEDULED", "RUNNING")


def live_labelled(jobs: Sequence[Mapping], labels: Mapping[str, str]) -> list[str]:
    """The short names of ``jobs`` carrying every one of ``labels`` and not yet in a terminal state."""
    return [j["name"].rsplit("/", 1)[-1] for j in jobs
            if j.get("status", {}).get("state") in LIVE_STATES
            and all(j.get("labels", {}).get(k) == v for k, v in labels.items())]


def cancel_labelled(labels: Mapping[str, str], *, region: str | None = None, dry_run: bool = False, log=None) -> list[str]:
    """Delete (= cancel) every live Batch job in ``region`` (default ``$BATCH_REGION``) carrying all of ``labels``;
    returns their names. A driver killed mid-submit leaves jobs it never saw submitted: a run's own label finds them (a
    Nextflow head's session)."""
    from .gcp import batch_jobs

    region, project = batch_region(region), gcp_project()
    names = live_labelled(batch_jobs(project, region), labels)
    for n in names:
        if log:
            log(f"{'would cancel' if dry_run else 'cancelling'} {n}")
        if not dry_run:
            r = session().delete(f"https://batch.googleapis.com/v1/projects/{project}/locations/{region}/jobs/{n}")
            r.raise_for_status()
    return names
