"""D1 progress history and idempotent backfill from Batch task logs."""

from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from requests.exceptions import ConnectionError, HTTPError, Timeout

from .index_footer import _creds, _d1_query, _q

FIELDS = ("bucket", "ts", "deletes", "bytes", "gone", "overwritten", "failed", "done", "bytes_exact")
T = TypeVar("T")


def retry_read(operation: Callable[[], T]) -> T:
    """Keep the monitoring bridge alive through transient GCP read failures.

    Auth/validation errors fail immediately. Exhausted retries remain visible.
    Only observational calls use this helper, not deletion requests.
    """
    for attempt in range(7):
        try:
            return operation()
        except (ConnectionError, Timeout, HTTPError) as exc:
            if isinstance(exc, HTTPError) and (exc.response is None or exc.response.status_code not in (408, 429, 500, 502, 503, 504)):
                raise
            if attempt == 6:
                raise
            delay = min(30, 2 ** attempt)
            print(f"progress sync: transient {type(exc).__name__}; retry {attempt + 1}/6 in {delay}s", file=sys.stderr, flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable retry state")
PATTERN = re.compile(
    r"^execute progress: (\S+) \((?:deleted|would-delete)\)"
    r" · roots=[\d,]+/[\d,]+ · delete=([\d,]+) \(([\d.]+) TB\)"
    r" · gone=([\d,]+) · overwritten=([\d,]+) · unanswered=([\d,]+) · done=(true|false)$",
)


def sample(snapshot: dict) -> dict:
    decisions = snapshot["decisions"]
    ts = datetime.fromisoformat(snapshot["updated"].replace("Z", "+00:00")).timestamp()
    return dict(zip(FIELDS, (
        snapshot["bucket"], ts, decisions.get("delete", 0), snapshot["delete_bytes"],
        decisions.get("skipped_gone", 0), decisions.get("skipped_overwritten", 0),
        decisions.get("delete_failed", 0), int(snapshot["done"]), 1,
    )))


def log_sample(entry: dict) -> dict | None:
    payload = entry.get("jsonPayload", {})
    if payload.get("event") == "sweep_progress":
        return sample(payload["progress"])
    text = entry.get("textPayload", "").strip()
    if text.startswith("{"):
        payload = json.loads(text)
        if payload.get("event") == "sweep_progress":
            return sample(payload["progress"])
        return None
    match = PATTERN.fullmatch(text)
    if not match:
        return None
    bucket, deletes, tb, gone, overwritten, failed, done = match.groups()
    ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).timestamp()
    return dict(zip(FIELDS, (
        bucket, ts, int(deletes.replace(",", "")), round(float(tb) * 1e12),
        int(gone.replace(",", "")), int(overwritten.replace(",", "")), int(failed.replace(",", "")), int(done == "true"), 0,
    )))


def sample_statements(run_id: str, samples: list[dict]) -> list[str]:
    statements = []
    for offset in range(0, len(samples), 100):
        rows = []
        for row in samples[offset:offset + 100]:
            values = [_q(run_id), _q(row["bucket"]), *[str(row[field]) for field in FIELDS[1:]]]
            rows.append("(" + ",".join(values) + ")")
        statements.append("INSERT OR IGNORE INTO deletion_progress (run_id," + ",".join(FIELDS) + ") VALUES " + ",".join(rows))
    return statements


def write_samples(run_id: str, samples: list[dict]) -> None:
    if not samples:
        return
    tok, acct = _creds()
    for sql in sample_statements(run_id, samples):
        _d1_query(sql, acct, tok)


def sync_progress(job_name: str, interval: int = 0, sql_output: str | None = None) -> None:
    """Backfill once, or keep tailing until Batch reaches a terminal state.

    No target mutations. Existing jobs need no restart or new IAM permissions.
    A two-minute overlap accommodates delayed ingestion; the D1 PK deduplicates.
    Legacy byte counters were rounded in task logs, and are labelled approximate.
    """
    from .gcp import batch_job, log_entries, task_log_filter

    if interval and interval < 30:
        raise ValueError("progress sync interval must be zero or at least 30 seconds")
    if interval and sql_output:
        raise ValueError("SQL export is single-shot; omit interval")
    tok, acct = _creds()
    job = retry_read(lambda: batch_job(job_name))
    rows = _d1_query(
        "SELECT run_id FROM deletion_runs WHERE log_dir LIKE " + _q(f"%/sweep/runs/{job_name}"), acct, tok,
    )
    if len(rows) != 1:
        raise ValueError("job must resolve to exactly one recorded deletion run")
    run_id = rows[0]["run_id"]
    latest = None
    while True:
        filter_ = task_log_filter(job["uid"]) + ' (textPayload:"execute progress:" OR jsonPayload.event="sweep_progress")'
        if latest is not None:
            since = datetime.fromtimestamp(latest - 120, timezone.utc).isoformat()
            filter_ += f' timestamp >= "{since}"'
        entries = retry_read(lambda: log_entries(filter_, limit=20_000, asc=True))
        samples = [row for entry in entries if (row := log_sample(entry)) is not None]
        if sql_output:
            Path(sql_output).write_text(";\n".join(sample_statements(run_id, samples)) + ";\n")
        else:
            write_samples(run_id, samples)
        if samples:
            latest = max(row["ts"] for row in samples)
        print(json.dumps({"job": job_name, "run_id": run_id, "samples": len(samples), "latest": latest}), flush=True)
        if not interval:
            return
        job = retry_read(lambda: batch_job(job_name))
        if job.get("status", {}).get("state") in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return
        time.sleep(interval)
