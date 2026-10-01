"""Scan-blob resolution: blob refs → paths on the search path, a path → the
hybrid chunk blob that holds it, and rebasing a frame onto a subtree root.
Shared by `index`, `capture`, `du`, `scans` and the staged-delete sizing."""

from __future__ import annotations

from functools import lru_cache
from os.path import isabs

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from . import blobfs, config as _config


def resolve_blob(blob_ref: str) -> str:
    """Resolve a parquet blob ref to its absolute path.

    Honors legacy absolute refs. Searches every configured scans dir (blobs
    may sit on an external volume), reading config at call time so tests can
    monkeypatch it.
    """
    if not blob_ref:
        return blob_ref
    return blob_ref if isabs(blob_ref) else _config.resolve_scan_blob(blob_ref)


def _chunk_map(parquet_path: str) -> dict[str, str] | None:
    """path → child_scan_id for the chunk-pointer rows of a hybrid parquet
    (None when the blob has no `child_scan_id` column). Keyed on mtime because
    delete updates rewrite blobs in place."""
    return _chunk_map_cached(parquet_path, blobfs.mtime(parquet_path))


@lru_cache(maxsize=64)
def _chunk_map_cached(parquet_path: str, mtime: float) -> dict[str, str] | None:
    schema = blobfs.read_schema(parquet_path)
    # A chunk blob's `child_scan_id` is often Arrow type `null` (every value
    # None): it holds no pointers, and it has no stats to prune on either
    if 'child_scan_id' not in schema.names or pa.types.is_null(schema.field('child_scan_id').type):
        return None
    # Pushed down: row groups whose `child_scan_id` is all null (null_count ==
    # num_rows) are pruned from the footer stats, so only the few groups holding
    # pointer rows are fetched — reading the whole `path` column of a 3.6M-row
    # chunk cost ~19 s per cold drill over R2.
    tbl = blobfs.read_table(parquet_path, columns=['path', 'child_scan_id'], filters=pc.field('child_scan_id').is_valid())
    return dict(zip(tbl['path'].to_pylist(), tbl['child_scan_id'].to_pylist()))


def resolve_chunk_for_path(blob_ref: str, rel_path: str) -> tuple[str, str]:
    """Resolve the actual blob_ref and rebased rel_path for a path that may be in a chunk.

    If rel_path maps to a chunked subtree (hybrid backend), returns
    (chunk_blob_ref, rebased_path). Otherwise returns (blob_ref, rel_path)
    unchanged.
    """
    if not rel_path or rel_path == '.':
        return blob_ref, rel_path

    chunks = _chunk_map(resolve_blob(blob_ref))
    if chunks is None:
        return blob_ref, rel_path

    # Check if any ancestor of rel_path has a child_scan_id
    parts = rel_path.split('/')
    for i in range(len(parts)):
        chunk_ref = chunks.get('/'.join(parts[:i+1]))
        if chunk_ref is not None and blobfs.exists(resolve_blob(chunk_ref)):
            # Rebase the remaining path relative to chunk root
            remaining = '/'.join(parts[i+1:]) if i + 1 < len(parts) else '.'
            # Recursively resolve in case of nested chunks
            return resolve_chunk_for_path(chunk_ref, remaining)

    return blob_ref, rel_path


def rebase_frame(df: pd.DataFrame, rel_path: str) -> pd.DataFrame:
    """Rebase a scan-relative frame so `rel_path` becomes the root — keeps only
    the subtree's rows (dropping the `rel_path` row itself) and strips the
    prefix from `path`/`depth`."""
    if not rel_path or rel_path == '.':
        return df
    pfx = rel_path + '/'
    sub = df[df['path'].str.startswith(pfx)].copy()
    sub['path'] = sub['path'].str[len(pfx):]
    if 'depth' in sub.columns:
        sub['depth'] = sub['depth'] - (rel_path.count('/') + 1)
    return sub
