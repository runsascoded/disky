"""Read-only GCS listing benchmark for the sweep executor.

The benchmark derives roots from an existing object manifest with the same
planner as ``sweep execute``, then re-lists one deterministic root cohort at
several concurrency levels. It never writes to a target bucket.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor


def _representative_roots(roots: list[tuple[str, int]], count: int) -> list[tuple[str, int]]:
    """Choose half of the cohort from the largest roots and spread the other
    half across the remainder. ``roots`` must be ordered largest first."""
    if count <= 0:
        raise ValueError("roots must be positive")
    if len(roots) <= count:
        return roots
    largest_count = (count + 1) // 2
    selected = roots[:largest_count]
    remainder = roots[largest_count:]
    spread_count = count - largest_count
    if spread_count:
        indexes = (
            [len(remainder) - 1]
            if spread_count == 1
            else [round(i * (len(remainder) - 1) / (spread_count - 1)) for i in range(spread_count)]
        )
        selected.extend(remainder[i] for i in indexes)
    return selected


def _plan_roots(
    plan_dir: str,
    bucket: str,
    max_root_objects: int,
) -> tuple[list[tuple[str, int]], float]:
    import fsspec
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    from .sweep_exec import list_roots, split_listing_roots

    started = time.monotonic()
    fs, path = fsspec.core.url_to_fs(plan_dir)
    with fs.open(f"{path}/plan-summary.json") as fh:
        plan = json.load(fh)
    if bucket not in plan.get("buckets", {}):
        raise SystemExit(f"{bucket} is not in {plan_dir}/plan-summary.json")
    manifest_path = f"{path}/manifest/{bucket}.parquet"
    if not fs.exists(manifest_path):
        raise SystemExit(f"missing manifest: {plan_dir}/manifest/{bucket}.parquet")
    with fs.open(manifest_path, "rb") as fh:
        table = pq.read_table(fh, columns=["name", "dir"], read_dictionary=["dir"])
    table = table.set_column(table.schema.get_field_index("name"), "name", pc.cast(table["name"], pa.large_string()))
    table = table.combine_chunks()
    names = table["name"]
    order = pc.sort_indices(table, sort_keys=[("name", "ascending")])
    dirs = set(pc.unique(table["dir"]).to_pylist())

    def lower_bound(key: str) -> int:
        lo, hi = 0, len(order)
        while lo < hi:
            mid = (lo + hi) // 2
            if names[order[mid].as_py()].as_py() < key:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def root_count(root: str) -> int:
        if not root:
            return len(order)
        return lower_bound(root + "/\x7f") - lower_bound(root + "/")

    roots = list_roots(dirs, tuple(plan.get("approved") or ()), bucket)
    roots = split_listing_roots(roots, dirs, root_count, max_root_objects)
    return [(root, root_count(root)) for root in roots], time.monotonic() - started


def benchmark_listings(
    plan_dir: str,
    bucket: str,
    workers: tuple[int, ...] = (8, 16, 32, 64),
    roots: int = 128,
    max_root_objects: int = 250_000,
    max_results_per_root: int = 50_000,
    client=None,
) -> dict:
    """Benchmark the executor's GCS re-list phase without making mutations."""
    from google.cloud import storage

    if not workers or any(worker <= 0 for worker in workers):
        raise ValueError("workers must contain positive integers")
    if max_root_objects <= 0:
        raise ValueError("max_root_objects must be positive")
    if max_results_per_root <= 0:
        raise ValueError("max_results_per_root must be positive")
    planned, planning_seconds = _plan_roots(plan_dir, bucket, max_root_objects)
    selected = _representative_roots(planned, roots)
    client = client or storage.Client()

    def list_one(root: tuple[str, int]) -> tuple[int, int]:
        prefix = f"{root[0]}/" if root[0] else ""
        objects = 0
        size_bytes = 0
        for blob in client.list_blobs(bucket, prefix=prefix, max_results=max_results_per_root):
            objects += 1
            size_bytes += int(blob.size or 0)
        return objects, size_bytes

    trials = []
    for worker_count in workers:
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            listed = list(pool.map(list_one, selected))
        seconds = time.monotonic() - started
        objects = sum(value[0] for value in listed)
        size_bytes = sum(value[1] for value in listed)
        trials.append({
            "workers": worker_count,
            "seconds": round(seconds, 3),
            "objects": objects,
            "bytes": size_bytes,
            "objects_per_second": round(objects / seconds, 1) if seconds else None,
        })
    return {
        "plan": plan_dir,
        "bucket": bucket,
        "planning_seconds": round(planning_seconds, 3),
        "planned_roots": len(planned),
        "selected_roots": len(selected),
        "selected_manifest_objects": sum(count for _, count in selected),
        "max_root_objects": max_root_objects,
        "max_results_per_root": max_results_per_root,
        "roots": [{"prefix": root, "manifest_objects": count} for root, count in selected],
        "trials": trials,
    }
