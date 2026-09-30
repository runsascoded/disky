"""Stage GCS parquet inputs onto local disk before DuckDB runs.

gcsfuse reads cap out around 20-50 MB/s and path-index makes ~4 full passes over
its inputs; a parallel download to local (NVMe) disk turns hours of FUSE reads
into minutes of copy plus fast local scans.
"""
from __future__ import annotations

import sys
from fnmatch import fnmatchcase
from functools import partial
from pathlib import Path

err = partial(print, file=sys.stderr)

GLOB_CHARS = "*?["


def split_glob(glob: str) -> tuple[str, str, str]:
    """``/gcs/<bucket>/<pattern>`` → (bucket, list prefix, pattern).

    The list prefix is the literal part of the pattern before its first
    wildcard — what ``list_blobs(prefix=...)`` can pre-filter on.
    """
    if not glob.startswith("/gcs/"):
        raise ValueError(f"stage glob must start with /gcs/: {glob}")
    bucket, sep, pattern = glob.removeprefix("/gcs/").partition("/")
    if not sep or not pattern:
        raise ValueError(f"stage glob must include an object pattern: {glob}")
    idxs = [i for c in GLOB_CHARS if (i := pattern.find(c)) != -1]
    prefix = pattern[: min(idxs)] if idxs else pattern
    return bucket, prefix, pattern


def stage_globs(
    globs: tuple[str, ...],
    out_root: Path,
    workers: int,
) -> None:
    """Download every object matching each ``/gcs/<bucket>/<pattern>`` glob to
    ``out_root/<bucket>/<object name>``, skipping files already present with a
    matching size (idempotent re-runs)."""
    from google.cloud import storage
    from google.cloud.storage import transfer_manager

    client = storage.Client()
    for glob in globs:
        bucket_name, prefix, pattern = split_glob(glob)
        bucket = client.bucket(bucket_name)
        blobs = [b for b in client.list_blobs(bucket_name, prefix=prefix) if fnmatchcase(b.name, pattern)]
        dest = out_root / bucket_name
        todo = []
        for b in blobs:
            p = dest / b.name
            if not (p.exists() and p.stat().st_size == b.size):
                todo.append(b.name)
        total = sum(b.size for b in blobs)
        err(f"stage {glob}: {len(blobs)} objects ({total / 1e9:.1f} GB), {len(todo)} to fetch")
        if not todo:
            continue
        results = transfer_manager.download_many_to_path(
            bucket,
            todo,
            destination_directory=str(dest),
            max_workers=workers,
        )
        failed = [(n, r) for n, r in zip(todo, results) if isinstance(r, Exception)]
        if failed:
            for n, r in failed[:5]:
                err(f"stage FAILED {bucket_name}/{n}: {r}")
            raise RuntimeError(f"{len(failed)}/{len(todo)} downloads failed for {glob}")
