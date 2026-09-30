"""Read-only GCP API helpers for job/SII ops (Batch, Logging, Monitoring).

Everything here is observational — list/describe/read — so the CLI commands
built on it are safe to auto-approve. REST via ADC (no gcloud subprocess
parsing); ``gcloud auth application-default login`` or plain ``gcloud auth
login`` credentials both work.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

PROJECT = "oa-internal-450019"
REGION = "us-central1"

_session = None


def session():
    global _session
    if _session is None:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        creds, _ = google.auth.default(quota_project_id=PROJECT)
        _session = AuthorizedSession(creds)
    return _session


def _get(url: str, **params: Any) -> dict:
    r = session().get(url, params=params or None)
    r.raise_for_status()
    return r.json()


def _post(url: str, body: dict) -> dict:
    r = session().post(url, json=body)
    r.raise_for_status()
    return r.json()


def batch_jobs(project: str = PROJECT, region: str = REGION) -> list[dict]:
    """Recent Batch jobs, newest first."""
    d = _get(f"https://batch.googleapis.com/v1/projects/{project}/locations/{region}/jobs")
    jobs = d.get("jobs", [])
    return sorted(jobs, key=lambda j: j.get("createTime", ""), reverse=True)


def batch_job(name: str, project: str = PROJECT, region: str = REGION) -> dict:
    return _get(f"https://batch.googleapis.com/v1/projects/{project}/locations/{region}/jobs/{name}")


def log_entries(
    filter_: str,
    project: str = PROJECT,
    limit: int = 50,
    asc: bool = False,
) -> list[dict]:
    entries: list[dict] = []
    body = {
        "resourceNames": [f"projects/{project}"],
        "filter": filter_,
        "orderBy": "timestamp asc" if asc else "timestamp desc",
        "pageSize": min(limit, 1000),
    }
    while len(entries) < limit:
        d = _post("https://logging.googleapis.com/v2/entries:list", body)
        entries.extend(d.get("entries", []))
        tok = d.get("nextPageToken")
        if not tok:
            break
        body["pageToken"] = tok
    return entries[:limit]


def task_log_filter(uid: str, grep: str | None = None) -> str:
    f = f'labels.job_uid="{uid}" log_id("batch_task_logs")'
    if grep:
        f += f' textPayload=~"{grep}"'
    return f


def job_instance_id(uid: str, project: str = PROJECT) -> str | None:
    """The Batch VM's numeric instance id, scraped from agent heartbeat logs."""
    for e in log_entries(f'labels.job_uid="{uid}" log_id("batch_agent_logs")', project, limit=5):
        if m := re.search(r"instance_id:(\d+)", e.get("textPayload", "")):
            return m.group(1)
    return None


METRICS = {
    "cpu": ("compute.googleapis.com/instance/cpu/utilization", 100, "%cpu"),
    "net": ("compute.googleapis.com/instance/network/received_bytes_count", 1e-6, "MB/s rx"),
    "disk": ("compute.googleapis.com/instance/disk/read_bytes_count", 1e-6, "MB/s read"),
}


def vm_metric(
    instance_id: str,
    metric: str,
    minutes: int = 30,
    project: str = PROJECT,
    start: str | None = None,
    end: str | None = None,
) -> list[tuple[str, float]]:
    """(end_time, scaled value) points for a VM metric, newest first.

    Default window is the last ``minutes``; pass RFC3339 ``start``/``end`` to
    cover a finished job's lifetime instead.
    """
    mtype, scale, _unit = METRICS[metric]
    now = datetime.now(timezone.utc)
    d = _get(
        f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries",
        **{
            "filter": f'metric.type="{mtype}" resource.labels.instance_id="{instance_id}"',
            "interval.startTime": start or (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "interval.endTime": end or now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "aggregation.alignmentPeriod": "60s",
            "aggregation.perSeriesAligner": "ALIGN_RATE" if mtype.endswith("_count") else "ALIGN_MEAN",
        },
    )
    pts = []
    for ts in d.get("timeSeries", []):
        for p in ts.get("points", []):
            v = p["value"].get("doubleValue", p["value"].get("int64Value", 0))
            pts.append((p["interval"]["endTime"], float(v) * scale))
    return pts


SII_PROJECT = "hai-gcp-models"


def sii_report_configs(location: str) -> list[dict]:
    d = _get(
        f"https://storageinsights.googleapis.com/v1/projects/{SII_PROJECT}/locations/{location}/reportConfigs"
    )
    return d.get("reportConfigs", [])


def sii_report_details(config_name: str) -> list[dict]:
    d = _get(f"https://storageinsights.googleapis.com/v1/{config_name}/reportDetails")
    return sorted(d.get("reportDetails", []), key=lambda r: r.get("snapshotTime", ""), reverse=True)
