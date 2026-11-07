"""Bounded dry-only profile of the real executor's merge/decision-log path."""

from __future__ import annotations

import json
import sys
import time
from functools import partial
from pathlib import Path
from typing import Any, Iterator

import fsspec
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .sweep_exec import execute_plan

err = partial(print, file=sys.stderr)


class RangeClient:
    """Read live objects only within the sampled manifest's disjoint spans."""

    def __init__(
        self,
        client: Any,
        ranges: list[tuple[str, str]],
        limit: int,
    ) -> None:
        self.client = client
        self.ranges = ranges
        self.limit = limit

    def __getattr__(self, key: str) -> Any:
        return getattr(self.client, key)

    def list_blobs(self, bucket: str, prefix: str = "") -> Iterator[Any]:
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1) if prefix else None
        for lo, hi in self.ranges:
            start = max(lo, prefix)
            end = min(hi, upper) if upper else hi
            if start < end:
                yield from self.client.list_blobs(bucket, prefix=prefix, start_offset=start, end_offset=end, max_results=self.limit)


def profile_executor(
    plan_dir: str,
    bucket: str,
    work_dir: str,
    groups: int = 4,
    rows: int = 10_000,
    workers: int = 4,
    client=None,
    artifact_out: str | None = None,
) -> dict:
    """Run the unmodified dry merge on bounded samples; never record to D1.

    Range-limited listings are a diagnostic, not a full-prefix drift audit.
    The artifacts are not valid reviewed DRs and cannot be reused for deletes.
    """
    from google.cloud import storage

    if min(groups, rows, workers) <= 0:
        raise ValueError("groups, rows and workers must be positive")
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):
        raise ValueError("executor profile work directory must be empty")
    fs, path = fsspec.core.url_to_fs(plan_dir)
    out_fs = out_path = None
    if artifact_out:
        out_fs, out_path = fsspec.core.url_to_fs(artifact_out)
        if (out_fs, out_path) == (fs, path) or out_fs.glob(f"{out_path}/**"):
            raise ValueError("executor profile artifact directory must be empty")
    started = time.monotonic()
    err(f"executor profile: sampling up to {groups * rows:,} objects from {bucket}")
    with fs.open(f"{path}/plan-summary.json") as fh:
        original = json.load(fh)
    if bucket not in original["buckets"]:
        raise ValueError("bucket is not in the reviewed plan")
    samples = []
    ranges = []
    with fs.open(f"{path}/manifest/{bucket}.parquet", "rb") as fh:
        parquet = pq.ParquetFile(fh)
        n = min(groups, parquet.metadata.num_row_groups)
        selected = sorted({round(i * (parquet.metadata.num_row_groups - 1) / max(1, n - 1)) for i in range(n)})
        columns = ["name", "size_bytes", "created", "dir"]
        if "generation" in parquet.schema.names:
            columns.append("generation")
        for group in selected:
            table = parquet.read_row_group(group, columns=columns).sort_by([("name", "ascending")]).slice(0, rows)
            if not len(table):
                continue
            lo = table["name"][0].as_py()
            last = table["name"][-1].as_py()
            hi = last[:-1] + chr(ord(last[-1]) + 1)
            ranges.append((lo, hi))
            samples.append(table)
    if not samples:
        raise ValueError("profile manifest is empty")
    ranges.sort()
    if any(ranges[i][0] < ranges[i - 1][1] for i in range(1, len(ranges))):
        raise ValueError("sampled manifest ranges overlap; choose fewer groups")
    table = pa.concat_tables(samples)
    profile_plan = work / "sample"
    (profile_plan / "manifest").mkdir(parents=True)
    pq.write_table(table, profile_plan / "manifest" / f"{bucket}.parquet")
    eligible = {"objects": len(table), "bytes": int(pc.sum(table['size_bytes']).as_py() or 0)}
    summary = {**original, "diagnostic": True, "buckets": {bucket: {"eligible": eligible}}, "total": {"eligible": eligible}}
    (profile_plan / "plan-summary.json").write_text(json.dumps(summary) + "\n")
    execution_dir = str(profile_plan)
    if out_fs is not None:
        out_fs.makedirs(f"{out_path}/manifest", exist_ok=True)
        out_fs.put_file(str(profile_plan / "manifest" / f"{bucket}.parquet"), f"{out_path}/manifest/{bucket}.parquet")
        out_fs.put_file(str(profile_plan / "plan-summary.json"), f"{out_path}/plan-summary.json")
        execution_dir = artifact_out
    err(f"executor profile: executing {len(table):,} sampled objects, dry-only, {workers} workers; artifacts={execution_dir}")
    result = execute_plan(
        execution_dir, for_real=False, workers=workers,
        client=RangeClient(client or storage.Client(), ranges, rows * 2),
        profile_dir=str(work / "profiles"),
    )
    profiles = sorted((work / "profiles").glob("*.pstats"))
    result = {"contract": "diagnostic-sample-not-a-reviewed-dry-run", "sample_objects": len(table), "ranges": ranges, "workers": workers, "profiled_roots": len(profiles), "seconds": round(time.monotonic() - started, 3), "result": result}
    (work / "profile.json").write_text(json.dumps(result, indent=2) + "\n")
    if out_fs is not None:
        for profile in [work / "profile.json", *profiles]:
            out_fs.put_file(str(profile), f"{out_path}/{profile.name}")
    err(f"executor profile: finished in {result['seconds']}s")
    return result
