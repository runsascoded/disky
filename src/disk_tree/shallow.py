"""The shallow sidecar — `<root-stem>.shallow.parquet` beside a chunked scan's
root blob: every chunk's depth-1 rows, in chunk-local coordinates (`path` is
the child's name, `parent == '.'`, `depth == 1`) plus a ``chunk_ref`` column
naming the chunk blob. Written by `HybridBackend.save`, refreshed after an
in-place delete, removed with the scan (spec `scan-page-r2-latency.md`).

A chunked root's top-level view is each chunk's top level — ~100 rows per
chunk. Reading them from the chunk blobs pulled the whole blob (3.6M rows,
131 MiB from R2) per chunk per request. With the sidecar, a page load reads
one small file; without one (scans saved before this landed), `chunk_top_rows`
falls back to a `depth == 1` read of the chunk, projected to the requested
columns and cached per process. Blobs are immutable except for the in-place
rewrite a delete does, so the caches key on `(path, mtime, size)`.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Callable, Iterable

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from . import blobfs
from .listing_format import readable_columns

SHALLOW_SUFFIX = '.shallow.parquet'
CHUNK_REF = 'chunk_ref'
_DEPTH1 = [('depth', '==', 1)]

Resolve = Callable[[str], str]


def shallow_path(root_blob_path: str) -> str:
    """``…/<uuid>.parquet`` → ``…/<uuid>.shallow.parquet`` (local or URL)."""
    stem = root_blob_path[:-len('.parquet')] if root_blob_path.endswith('.parquet') else root_blob_path
    return stem + SHALLOW_SUFFIX


def top_rows(table: pa.Table) -> pa.Table:
    """A chunk table's depth-1 rows (its direct children)."""
    return table.filter(pc.equal(table['depth'], 1))


def write_shallow(root_blob_path: str, tops: Iterable[tuple[str, pa.Table]]) -> str | None:
    """Write the sidecar from ``(chunk_ref, depth-1 rows)`` pairs; `None` (and
    no file) when there are no chunks."""
    from .storage.base import BLOB_ROW_GROUP_SIZE
    parts = [t.append_column(CHUNK_REF, pa.array([ref] * t.num_rows, pa.string())) for ref, t in tops]
    if not parts:
        return None
    path = shallow_path(root_blob_path)
    blobfs.write_table(pa.concat_tables(parts, promote_options='default'), path, BLOB_ROW_GROUP_SIZE)
    return path


def remove_shallow(root_blob_path: str) -> None:
    path = shallow_path(root_blob_path)
    if blobfs.exists(path):
        blobfs.remove(path)


def build_shallow(root_blob_path: str, resolve: Resolve, force: bool = False) -> str | None:
    """Build (or, with ``force``, rebuild) the sidecar from the chunk blobs — for
    scans saved before it was written at save time, and after an in-place
    delete changed a chunk. Returns the sidecar path, or `None` (removing any
    stale sidecar) when the root has no chunks."""
    path = shallow_path(root_blob_path)
    if not force and blobfs.exists(path):
        return path
    tops: list[tuple[str, pa.Table]] = []
    if 'child_scan_id' in blobfs.read_schema(root_blob_path).names:
        refs = blobfs.read_parquet(root_blob_path, columns=['child_scan_id'])['child_scan_id'].dropna().unique()
        for ref in refs:
            chunk = resolve(ref)
            if not blobfs.exists(chunk):
                continue
            df = blobfs.read_parquet(chunk, filters=_DEPTH1)
            tops.append((ref, pa.Table.from_pandas(df, preserve_index=False)))
    written = write_shallow(root_blob_path, tops)
    if written is None:
        remove_shallow(root_blob_path)
    return written


@lru_cache(maxsize=256)
def _sidecar(path: str, stamp: tuple) -> pd.DataFrame:
    return blobfs.read_parquet(path)


@lru_cache(maxsize=1024)
def _chunk_top(path: str, stamp: tuple, columns: tuple[str, ...] | None) -> pd.DataFrame:
    cols = None
    if columns is not None:
        # A v2 chunk has no `uri` column, but the read derives it.
        names = readable_columns(blobfs.read_schema(path))
        cols = [c for c in columns if c in names]
    return blobfs.read_parquet(path, filters=_DEPTH1, columns=cols)


def shallow_rows(root_blob_path: str) -> pd.DataFrame | None:
    """The sidecar's rows (cached), or `None` when the root has none."""
    path = shallow_path(root_blob_path)
    stamp = blobfs.stat(path)
    return None if stamp is None else _sidecar(path, stamp)


def chunk_top_rows(
    root_blob_path: str,
    chunk_ref: str,
    resolve: Resolve,
    columns: list[str] | None = None,
) -> pd.DataFrame | None:
    """Chunk ``chunk_ref``'s depth-1 rows, from the root's sidecar when it has
    one, else a filtered + projected read of the chunk blob; `None` when the
    chunk blob is missing. ``columns`` (those the caller emits) are intersected
    with what the blob has."""
    side = shallow_rows(root_blob_path)
    if side is not None:
        rows = side[side[CHUNK_REF] == chunk_ref].drop(columns=[CHUNK_REF])
        if columns is not None:
            rows = rows[[c for c in columns if c in rows.columns]]
        return rows.reset_index(drop=True)
    chunk = resolve(chunk_ref)
    stamp = blobfs.stat(chunk)
    if stamp is None:
        return None
    return _chunk_top(chunk, stamp, tuple(columns) if columns is not None else None)


def clear_cache() -> None:
    _sidecar.cache_clear()
    _chunk_top.cache_clear()
