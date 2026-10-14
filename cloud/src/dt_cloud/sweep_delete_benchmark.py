"""Destructive comparison confined to newly-created, bounded scratch objects."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatchcase
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from .sweep_exec import _soft_delete_days, delete_batch
from .sweep_xml import XmlDeleter

err = partial(print, file=sys.stderr)


def protected_buckets(env: Mapping[str, str] = os.environ) -> list[str]:
    """`PROTECTED_BUCKETS`: comma-separated globs of the deployment's production
    buckets, which the benchmark must never touch. Required (`''` = none), so a
    deployment can't skip the guard by omission."""
    if "PROTECTED_BUCKETS" not in env:
        raise ValueError("delete benchmark needs PROTECTED_BUCKETS (globs of production buckets to refuse; '' for none)")
    return [g.strip() for g in env["PROTECTED_BUCKETS"].split(",") if g.strip()]


def scratch_target(url: str, env: Mapping[str, str] = os.environ) -> tuple[str, str]:
    parsed = urlsplit(url)
    prefix = parsed.path.lstrip("/").rstrip("/")
    if parsed.scheme != "gs" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("delete benchmark requires a gs:// scratch URL")
    globs = protected_buckets(env)
    if any(fnmatchcase(parsed.netloc, g) for g in globs):
        raise ValueError(f"delete benchmark refuses {parsed.netloc}: a protected bucket ({', '.join(globs)})")
    if not re.fullmatch(r"sweep/smoke-tests/delete-[a-z0-9][a-z0-9-]*", prefix):
        raise ValueError("delete benchmark prefix must be sweep/smoke-tests/delete-<unique-id>")
    return parsed.netloc, prefix


def benchmark_deletes(
    target: str,
    objects: int = 10_000,
    workers: int = 16,
    methods: tuple[str, ...] = ("xml", "json", "gcloud"),
    client=None,
    verified_soft_delete_days: int | None = None,
) -> dict:
    """Create synthetic objects, compare methods, verify deletion and rewrites.

    Never reads or copies production objects. A new empty prefix is mandatory;
    no existing object can become a benchmark target. Soft delete is required.
    """
    from google.cloud import storage

    bucket, prefix = scratch_target(target)
    if not 1 <= objects <= 100_000 or not 1 <= workers <= 64:
        raise ValueError("benchmark objects must be 1..100000 and workers 1..64")
    if not methods or len(set(methods)) != len(methods) or any(method not in ("xml", "json", "gcloud", "reviewed", "reviewed-warm") for method in methods):
        raise ValueError("benchmark methods must be distinct xml/json/gcloud/reviewed/reviewed-warm choices")
    client = client or storage.Client()
    if next(iter(client.list_blobs(bucket, prefix=prefix + "/", max_results=1)), None) is not None:
        raise ValueError("delete benchmark prefix must be empty")
    retention = verified_soft_delete_days if verified_soft_delete_days is not None else _soft_delete_days(client, bucket)
    if retention < 7:
        raise ValueError("delete benchmark requires at least seven days of soft delete")
    handle = client.bucket(bucket)
    marker = handle.blob(f"{prefix}/benchmark.json")
    marker.upload_from_string(json.dumps({"target": target, "objects": objects, "methods": methods}), if_generation_match=0)
    xml = XmlDeleter(credentials=client._credentials)
    trials = []
    for method in methods:
        root = f"{prefix}/{method}/"

        def upload(i: int):
            # Six objects per dir exercises the current executor's pathological
            # layout. XML-sensitive names test actual name encoding end-to-end.
            suffix = f"d{i // 6:06d}/o{i:06d}" if i >= 4 else ("nested/é &%.txt", "nested/literal%2Fname", "nested/a+b", "nested/<tag>")[i]
            blob = handle.blob(root + suffix)
            blob.upload_from_string(b"x", if_generation_match=0)
            return blob

        err(f"delete benchmark: preparing {objects:,} new scratch objects for {method}", flush=True)
        setup = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            blobs = list(pool.map(upload, range(objects)))
        setup_seconds = time.monotonic() - setup
        started = time.monotonic()
        outcomes = []
        executor_metrics = None
        if method in ("reviewed", "reviewed-warm"):
            import fsspec
            import pyarrow as pa
            import pyarrow.parquet as pq
            from .sweep_reviewed import execute_reviewed

            source = target + "/reviewed-source"
            plan_path = source + "/plan.json"
            approved = [f"gs://{bucket}/{root}"]
            original = {"plan_id": 0, "date": "scratch", "approved": approved, "buckets": {bucket: {"eligible": {"objects": objects, "bytes": objects}}}, "total": {"eligible": {"objects": objects, "bytes": objects}}}
            dry = {"for_real": False, "buckets": {bucket: {"decisions": {"delete": objects}, "delete_bytes": objects}}}
            for name, value in (("plan.json", {"plan_id": 0, "sweep": approved}), ("plan-summary.json", original), ("would-delete-summary.json", dry)):
                with fsspec.open(source + "/" + name, "w") as fh:
                    json.dump(value, fh)
            table = pa.table({"name": [blob.name for blob in blobs], "size_bytes": [1] * objects, "generation": [int(blob.generation) for blob in blobs], "decision": ["delete"] * objects, "dir": [blob.name.rpartition("/")[0] for blob in blobs]})
            with fsspec.open(source + f"/would-delete/{bucket}.parquet", "wb") as fh:
                pq.write_table(table, fh)
            result = execute_reviewed(source, plan_path, target + f"/{method}-run", str(Path("tmp") / "delete-benchmark" / prefix.rsplit("/", 1)[-1] / method), for_real=True, workers=workers, client=client, initial_rate=8000 if method == "reviewed-warm" else 1000, verified_soft_delete_days=verified_soft_delete_days)
            executor_metrics = result["buckets"][bucket]["performance"]
            outcomes = [decision for decision, count in result["buckets"][bucket]["decisions"].items() for _ in range(count)]
            if result["buckets"][bucket].get("error") or outcomes != ["delete"] * objects:
                raise RuntimeError("reviewed scratch executor did not acknowledge the exact expected delete set")
        elif method == "gcloud":
            # The validated prefix was empty before this benchmark. A wildcard
            # exercises normal prefix enumeration rather than thousands of
            # independent generation URLs (which incur per-URL discovery).
            command = ["gcloud", "storage", "rm", f"gs://{bucket}/{root}**", "--verbosity=error"]
            cli_env = {**os.environ, "CLOUDSDK_STORAGE_PROCESS_COUNT": "1", "CLOUDSDK_STORAGE_THREAD_COUNT": str(workers)}
            result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=cli_env)
            if result.returncode:
                raise RuntimeError(f"gcloud scratch delete exited {result.returncode}: {result.stderr[-2000:]}")
        else:
            batch_size = 1000 if method == "xml" else 100
            batches = [blobs[i:i + batch_size] for i in range(0, len(blobs), batch_size)]
            delete = xml if method == "xml" else delete_batch
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for result in pool.map(lambda batch: delete(client, handle, batch), batches):
                    outcomes.extend(decision for _, decision in result)
            if any(decision not in ("delete", "skipped_gone") for decision in outcomes):
                raise RuntimeError(f"{method}: scratch deletion has unsettled outcomes")
        seconds = time.monotonic() - started
        remaining = list(client.list_blobs(bucket, prefix=root))
        if remaining:
            raise RuntimeError(f"{method}: {len(remaining)} scratch objects remain; request encoding or deletion failed")
        trial = {"method": method, "objects": objects, "workers": workers, "setup_seconds": round(setup_seconds, 3), "seconds": round(seconds, 3), "objects_per_second": round(objects / seconds, 1), "remaining": 0, "rate_limited": method in ("reviewed", "reviewed-warm")}
        if executor_metrics is not None:
            trial["executor"] = executor_metrics
            trial["executor_objects_per_second"] = round(objects / executor_metrics["seconds"], 1)
            trial["initial_rate"] = 8000 if method == "reviewed-warm" else 1000
        trials.append(trial)
        err(f"delete benchmark: {json.dumps(trial, sort_keys=True)}", flush=True)
    # Target an old generation after a rewrite; verify the replacement survives.
    old = handle.blob(f"{prefix}/race/replaced")
    old.upload_from_string(b"old", if_generation_match=0)
    replacement = handle.blob(old.name)
    replacement.upload_from_string(b"replacement", if_generation_match=old.generation)
    gone = handle.blob(f"{prefix}/race/gone")
    gone.upload_from_string(b"gone", if_generation_match=0)
    gone.delete(if_generation_match=gone.generation)
    xml(client, handle, [old, gone])
    if replacement.download_as_bytes(if_generation_match=replacement.generation) != b"replacement":
        raise RuntimeError("XML deletion did not preserve replacement")
    xml(client, handle, [replacement])
    if list(client.list_blobs(bucket, prefix=f"{prefix}/race/")):
        raise RuntimeError("scratch race cleanup left live objects")
    result = {"contract": "synthetic-scratch-only", "target": target, "trials": trials, "replacement_survived": True, "out_of_band_delete_tolerated": True, "cli_includes_listing": True, "cli_processes": 1, "soft_delete_days": retention}
    marker.upload_from_string(json.dumps(result, indent=2) + "\n", if_generation_match=marker.generation)
    return result
