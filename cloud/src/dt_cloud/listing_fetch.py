"""`dt-cloud fetch-listing`: an existing listing (a dir of `*.parquet` shards + `_SUCCESS.json`, e.g. a laptop
capture in R2) copied to a local dir, the marker last, so a partial copy is never taken for a listing. The scan
pipeline's `list` step for a source that adopts its listing (`sources[].listing`) instead of listing a bucket."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

MARKER = "_SUCCESS.json"


def _copy(src: str, dst: Path) -> None:
    from disk_tree import blobfs

    tmp = dst.with_name(f".{dst.name}.tmp")
    if blobfs.is_url(src):
        with blobfs.open_read(src) as f, tmp.open("wb") as g:
            shutil.copyfileobj(f, g, 8 << 20)
    else:
        shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def fetch_listing(src: str, out: Path) -> list[str]:
    """`src`'s shards, then its marker, into `out` (its earlier shards cleared first: a retried copy never mixes two
    attempts'); the shard names copied. Refuses a `src` without the marker (an unfinished or absent listing)."""
    from disk_tree import blobfs

    marker = blobfs.join(src, MARKER)
    if not blobfs.exists(marker):
        raise FileNotFoundError(f"{marker}: no finished listing there")
    out.mkdir(parents=True, exist_ok=True)
    for old in [*out.glob("*.parquet"), out / MARKER]:
        old.unlink(missing_ok=True)
    shards = blobfs.glob(blobfs.join(src, "*.parquet"))
    names = [u.rsplit("/", 1)[-1] for u in shards]
    for u, n in zip(shards, names):
        _copy(u, out / n)
    _copy(marker, out / MARKER)
    return names
