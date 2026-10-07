"""Reuse a completed DR's pinned object manifest, without re-scanning."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from .staged_plan import load_plan


def reuse_manifest(
    source: str,
    plan_path: str,
    date: str,
    out: str,
    only_buckets: tuple[str, ...] = (),
) -> dict:
    """Validate the reviewed DR, then copy its exact manifests server-side.

    This reuses the original scan objects, not the DR's live-generation log.
    The real executor still performs its normal replacement and drift checks.
    """
    import fsspec
    import pyarrow.parquet as pq

    plan = load_plan(plan_path)
    buckets = [b for b in plan.buckets if not only_buckets or b in only_buckets]
    if not buckets:
        raise SystemExit("no staged bucket selected for manifest reuse")
    source_fs, source_path = fsspec.core.url_to_fs(source, version_aware=True)
    out_fs, out_path = fsspec.core.url_to_fs(out, version_aware=True)
    if source_fs.protocol != out_fs.protocol:
        raise SystemExit("manifest reuse requires the same source and destination filesystem")
    if source_path.rstrip("/") == out_path.rstrip("/"):
        raise SystemExit("manifest reuse destination must differ from its source")
    with source_fs.open(f"{source_path}/plan-summary.json") as fh:
        original = json.load(fh)
    with source_fs.open(f"{source_path}/would-delete-summary.json") as fh:
        dry = json.load(fh)
    approved = sorted(a for b in buckets for a in plan.bands(b))
    if original.get("date") != date or original.get("plan_id") != plan.plan_id:
        raise SystemExit("reviewed manifest scan/plan does not match the new dispatch")
    if sorted(original.get("approved", [])) != approved or sorted(original.get("buckets", {})) != buckets:
        raise SystemExit("reviewed manifest prefix/bucket set does not match the new dispatch")
    eligible_buckets = [b for b in buckets if original["buckets"][b].get("eligible", {}).get("objects", 0)]
    if original.get("diagnostic") or dry.get("for_real") is not False or sorted(dry.get("buckets", {})) != eligible_buckets:
        raise SystemExit("manifest reuse requires a completed dry-run of the same buckets")
    if any(entry.get("interrupted") or entry.get("failed_dirs") for entry in dry["buckets"].values()):
        raise SystemExit("cannot reuse an interrupted or failed dry-run")
    if out_fs.exists(f"{out_path}/plan-summary.json") or out_fs.glob(f"{out_path}/manifest/*.parquet"):
        raise SystemExit("manifest reuse destination already contains a manifest")

    files = []
    for bucket in buckets:
        entry = original["buckets"][bucket]
        objects = entry.get("eligible", {}).get("objects", 0)
        if not objects:
            continue
        path = f"{source_path}/manifest/{bucket}.parquet"
        protocols = source_fs.protocol if isinstance(source_fs.protocol, tuple) else (source_fs.protocol,)
        if "gcs" in protocols or "gs" in protocols:
            generation = source_fs.info(path).get("generation")
            if not generation:
                raise SystemExit(f"{bucket}: cannot pin reviewed manifest artifact generation")
            path = f"{path}#{generation}"
        with source_fs.open(path, "rb") as fh:
            rows = pq.ParquetFile(fh).metadata.num_rows
        if rows != objects:
            raise SystemExit(f"{bucket}: reviewed manifest has {rows} rows, expected {objects}")
        files.append((path, f"{out_path}/manifest/{bucket}.parquet"))

    for src, dst in files:
        if out_fs.protocol in ("file", "local") or isinstance(out_fs.protocol, tuple) and "file" in out_fs.protocol:
            out_fs.makedirs(str(PurePosixPath(dst).parent), exist_ok=True)
        out_fs.copy(src, dst)
    summary = {**original, "reused_from": source.rstrip("/")}
    with out_fs.open(f"{out_path}/plan-summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)
    return summary
