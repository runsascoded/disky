"""Bounded, exact-output benchmark of manifest read and write paths."""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from functools import partial

from pyarrow import fs as pafs
import pyarrow.parquet as pq

from .staged_plan import load_plan
from .sweep_manifest import MANIFEST_SCHEMA, ShardResult, _groups_bands, minimal_bands, resolve_fs, scan_shard

err = partial(print, file=sys.stderr)


def shard_counts(result: ShardResult) -> tuple[int, ...]:
    return (result.objects, result.elig_objects, result.elig_bytes, result.out_objects, result.out_bytes, result.pruned_groups, result.groups)


def benchmark_manifest_io(
    root: str,
    date: str,
    plan_path: str,
    bucket: str,
    out: str,
    work_dir: str,
    shards: int = 4,
    workers: int = 4,
) -> dict:
    """Compare current ranges, Arrow prefetch, and full-shard local staging.

    Footers select the largest staged-overlapping ``shards`` inputs. Output files are diagnostic
    samples, never executor plans. No target bucket is written or deleted.
    """
    if shards <= 0 or workers <= 0:
        raise ValueError("shards and workers must be positive")
    plan = load_plan(plan_path)
    if bucket not in plan.sweep:
        raise ValueError("bucket is not staged in this plan")
    fs, path = resolve_fs(root)
    out_fs, out_path = resolve_fs(out)
    infos = fs.get_file_info(pafs.FileSelector(f"{path}/listing/{date}/{bucket}"))
    candidates = [i for i in infos if i.is_file and i.path.endswith(".parquet")]
    if not candidates:
        raise ValueError("no listing shards found")
    bands = minimal_bands(plan.sweep[bucket])
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):
        raise ValueError("benchmark work directory must be empty")
    if out_fs.get_file_info(pafs.FileSelector(out_path, allow_not_found=True)):
        raise ValueError("benchmark artifact directory must be empty")
    if out_fs.type_name not in ("gcs", "s3"):
        out_fs.create_dir(out_path, recursive=True)
    selection_started = time.monotonic()
    err(f"manifest benchmark: checking {len(candidates)} footers for staged overlap")

    def eligible_rows(info: pafs.FileInfo) -> tuple[pafs.FileInfo, int]:
        with fs.open_input_file(info.path) as fh:
            md = pq.ParquetFile(fh).metadata
            groups = _groups_bands(md, bands)
            return info, sum(md.row_group(i).num_rows for i, matching in enumerate(groups) if matching)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        ranked = sorted(pool.map(eligible_rows, candidates), key=lambda pair: (-pair[1], -pair[0].size, pair[0].path))
    selected = [info for info, rows in ranked if rows][:shards]
    if not selected:
        raise ValueError("no listing shard overlaps the staged prefixes")
    selection_seconds = time.monotonic() - selection_started
    baseline = {}
    trials = []

    for mode in ("ranges", "prebuffer", "local"):
        err(f"manifest benchmark: starting {mode}, {len(selected)} shards, {workers} workers")
        started = time.monotonic()
        cpu_started = time.process_time()
        staging_seconds = 0.0
        if mode == "local":
            for info in selected:
                pafs.copy_files(info.path, str(work / Path(info.path).name), source_filesystem=fs, destination_filesystem=pafs.LocalFileSystem())
            staging_seconds = time.monotonic() - started

        def read(info: pafs.FileInfo) -> tuple[str, ShardResult, float]:
            read_started = time.monotonic()
            read_fs = pafs.LocalFileSystem() if mode == "local" else fs
            read_path = str(work / Path(info.path).name) if mode == "local" else info.path
            result = scan_shard(read_fs, read_path, bands, pre_buffer=mode == "prebuffer")
            return info.path, result, time.monotonic() - read_started

        scan_worker_seconds = write_seconds = close_seconds = verification_seconds = 0.0
        objects = eligible_objects = eligible_bytes = 0
        writer = None
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for shard, result, seconds in pool.map(read, selected):
                    scan_worker_seconds += seconds
                    objects += result.objects
                    eligible_objects += result.elig_objects
                    eligible_bytes += result.elig_bytes
                    verify_started = time.monotonic()
                    if mode == "ranges":
                        baseline[shard] = result
                    else:
                        original = baseline[shard]
                        if shard_counts(result) != shard_counts(original) or (result.table is None) != (original.table is None):
                            raise RuntimeError(f"{mode}: manifest totals differ for {shard}")
                        if result.table is not None and not result.table.equals(original.table):
                            raise RuntimeError(f"{mode}: manifest rows differ for {shard}")
                    verification_seconds += time.monotonic() - verify_started
                    write_started = time.monotonic()
                    if result.table is not None:
                        if writer is None:
                            writer = pq.ParquetWriter(f"{out_path}/{mode}.parquet", MANIFEST_SCHEMA, filesystem=out_fs)
                        writer.write_table(result.table)
                    write_seconds += time.monotonic() - write_started
        finally:
            close_started = time.monotonic()
            if writer is not None:
                writer.close()
            close_seconds = time.monotonic() - close_started
        trials.append({
            "mode": mode,
            "seconds": round(time.monotonic() - started, 3),
            "cpu_seconds": round(time.process_time() - cpu_started, 3),
            "staging_seconds": round(staging_seconds, 3),
            "scan_worker_seconds": round(scan_worker_seconds, 3),
            "write_seconds": round(write_seconds, 3),
            "close_seconds": round(close_seconds, 3),
            "verification_seconds": round(verification_seconds, 3),
            "objects": objects, "eligible_objects": eligible_objects, "eligible_bytes": eligible_bytes,
        })
        err(f"manifest benchmark: finished {mode}: {json.dumps(trials[-1], sort_keys=True)}")
    result = {
        "bucket": bucket, "date": date, "workers": workers,
        "shards": [{"path": i.path, "bytes": i.size} for i in selected],
        "input_bytes": sum(i.size for i in selected),
        "selection_seconds": round(selection_seconds, 3),
        "contract": "diagnostic-sample-not-a-deletion-plan",
        "trials": trials,
    }
    with out_fs.open_output_stream(f"{out_path}/benchmark.json") as fh:
        fh.write((json.dumps(result, indent=2) + "\n").encode())
    return result
