"""Generation-pinned manifests and jobs for GCS Storage Batch Operations.

The executor's dry-run log is the review artifact: it names the exact live
generation observed for every object whose decision was ``delete``.  This
module copies only those rows into the CSV shape accepted by Storage Batch
Operations.  A later real job therefore cannot delete a replacement object at
the same path.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import PurePosixPath
from typing import Any


def _parts(fs, plan_path: str, bucket: str) -> list[str]:
    directory = sorted(fs.glob(f"{plan_path}/would-delete/{bucket}/part-*.parquet"))
    single = f"{plan_path}/would-delete/{bucket}.parquet"
    return ([single] if fs.exists(single) else []) + directory


def _csv_bytes(bucket: str, names: list[str], generations: list[int]) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["bucket", "name", "generation"])
    writer.writerows(zip([bucket] * len(names), names, generations))
    return out.getvalue().encode()


def build_batch_manifests(
    plan_dir: str,
    only_buckets: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Convert a completed dry run into one wildcard CSV manifest per bucket.

    Completed output is refused: the generation list is the reviewed contract,
    so silently replacing it would make a later real job target a different
    object set. An interrupted build without ``summary.json`` is safely
    resumed because every part is a deterministic projection of an immutable
    dry-run part.
    """
    import fsspec
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    fs, plan_path = fsspec.core.url_to_fs(plan_dir)
    with fs.open(f"{plan_path}/would-delete-summary.json") as fh:
        dry_summary = json.load(fh)
    if dry_summary.get("for_real"):
        raise SystemExit(f"{plan_dir}/would-delete-summary.json is not a dry-run summary")

    root = f"{plan_path}/batch-manifest"
    if fs.exists(f"{root}/summary.json"):
        raise SystemExit(f"{plan_dir}/batch-manifest already exists; refusing to replace the reviewed generation set")

    summary: dict[str, Any] = {
        "source": plan_dir,
        "contract": "dry-run-delete-generations",
        "buckets": {},
    }
    for bucket, bucket_summary in dry_summary["buckets"].items():
        if only_buckets and bucket not in only_buckets:
            continue
        source_parts = _parts(fs, plan_path, bucket)
        expected_objects = int(bucket_summary.get("decisions", {}).get("delete", 0))
        expected_bytes = int(bucket_summary.get("delete_bytes", 0))
        if expected_objects == 0:
            if expected_bytes:
                raise SystemExit(f"{bucket}: dry-run summary has no deletes but reports {expected_bytes:,} delete bytes")
            continue
        if expected_objects and not source_parts:
            raise SystemExit(f"{bucket}: dry-run summary names {expected_objects:,} deletes but its decision log is missing")

        objects = 0
        size_bytes = 0
        output_parts: list[dict[str, Any]] = []
        for source in source_parts:
            with fs.open(source, "rb") as fh:
                table = pq.read_table(fh, columns=["name", "size_bytes", "generation", "decision"])
            table = table.filter(pc.equal(table["decision"], "delete"))
            if not len(table):
                continue
            raw_generations = table["generation"].to_pylist()
            if any(value is None or int(value) <= 0 for value in raw_generations):
                raise SystemExit(f"{bucket}: a delete decision has no live generation; refusing an unsafe Batch manifest")
            generations = [int(value) for value in raw_generations]
            names = table["name"].to_pylist()
            payload = _csv_bytes(bucket, names, generations)
            part = f"part-{len(output_parts):05d}.csv"
            path = f"{root}/{bucket}/{part}"
            fs.makedirs(str(PurePosixPath(path).parent), exist_ok=True)
            with fs.open(path, "wb") as fh:
                fh.write(payload)
            part_bytes = int(pc.sum(table["size_bytes"]).as_py() or 0)
            objects += len(table)
            size_bytes += part_bytes
            output_parts.append({
                "path": part,
                "objects": len(table),
                "bytes": part_bytes,
                "sha256": hashlib.sha256(payload).hexdigest(),
            })

        if objects != expected_objects or size_bytes != expected_bytes:
            raise SystemExit(
                f"{bucket}: dry-run log has {objects:,} deletes / {size_bytes:,} bytes; "
                f"summary says {expected_objects:,} / {expected_bytes:,}"
            )
        summary["buckets"][bucket] = {
            "objects": objects,
            "bytes": size_bytes,
            "source_parts": len(source_parts),
            "parts": output_parts,
            "manifest_location": f"{plan_dir.rstrip('/')}/batch-manifest/{bucket}/part-*.csv",
        }

    fs.makedirs(root, exist_ok=True)
    with fs.open(f"{root}/summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)
    return summary


def _job_id(plan_dir: str, bucket: str, dry_run: bool) -> str:
    stem = plan_dir.rstrip("/").rsplit("/", 1)[-1]
    raw = f"gcs-sweep-batch-{'dry' if dry_run else 'real'}-{stem}-{bucket}"
    return re.sub(r"[^a-z0-9-]", "-", raw.lower()).strip("-")[:128].rstrip("-")


def submit_batch_jobs(
    plan_dir: str,
    *,
    dry_run: bool = True,
    only_buckets: tuple[str, ...] = (),
    project: str | None = None,
    http=None,
) -> dict[str, Any]:
    """Create one generation-pinned Storage Batch Operations job per bucket."""
    import fsspec

    from .gcp import gcp_project, session

    fs, plan_path = fsspec.core.url_to_fs(plan_dir)
    with fs.open(f"{plan_path}/batch-manifest/summary.json") as fh:
        manifests = json.load(fh)
    project = project or gcp_project()
    http = http or session()
    jobs = []
    for bucket, info in manifests["buckets"].items():
        if only_buckets and bucket not in only_buckets:
            continue
        location = info["manifest_location"]
        if not location.startswith("gs://"):
            raise SystemExit(f"{bucket}: Batch manifest must be in GCS, got {location}")
        job_id = _job_id(plan_dir, bucket, dry_run)
        body = {
            "description": f"disk-tree reviewed sweep: {plan_dir}",
            "bucketList": {"buckets": [{"bucket": bucket, "manifest": {"manifestLocation": location}}]},
            "deleteObject": {"permanentObjectDeletionEnabled": False},
            "dryRun": dry_run,
            "loggingConfig": {"logActions": ["TRANSFORM"], "logActionStates": ["FAILED"]},
        }
        response = http.post(
            f"https://storagebatchoperations.googleapis.com/v1/projects/{project}/locations/global/jobs",
            params={"jobId": job_id},
            json=body,
        )
        response.raise_for_status()
        jobs.append({"bucket": bucket, "job_id": job_id, "operation": response.json()})
    return {"source": plan_dir, "project": project, "dry_run": dry_run, "jobs": jobs}
