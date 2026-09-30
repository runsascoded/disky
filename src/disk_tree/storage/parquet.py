import time
from os import makedirs
from os.path import isabs
from uuid import uuid4

import pandas as pd

from .base import BLOB_ROW_GROUP_SIZE, StorageBackend, PathStats, path_prefix_bounds
from .. import blobfs, config as _config
from ..listing_format import write_listing


# LRU cache for parquet DataFrames
_cache: dict[str, tuple[float, pd.DataFrame]] = {}
CACHE_TTL = 300  # 5 minutes
CACHE_MAX_SIZE = 10


class ParquetBackend(StorageBackend):
    """Parquet-based storage backend.

    Stores each scan as a separate .parquet file. Immutable - does not support
    in-place updates. Best compression, but requires full rewrite for any changes.
    """

    def __init__(self, scans_dir: str | None = None):
        # Resolve via the config module so tests can monkeypatch SCANS_DIR at runtime.
        self.scans_dir = scans_dir or _config.SCANS_DIR
        if not blobfs.is_url(self.scans_dir):
            makedirs(self.scans_dir, exist_ok=True)

    @property
    def name(self) -> str:
        return "parquet"

    @property
    def supports_updates(self) -> bool:
        return False

    def _resolve(self, blob_ref: str) -> str:
        # Legacy absolute paths are honored; new refs are basenames.
        # Blobs may live on an external volume; search every read dir.
        return blob_ref if isabs(blob_ref) else _config.resolve_scan_blob(blob_ref, self.scans_dir)

    def save(self, df: pd.DataFrame, scan_path: str) -> str:
        """Save DataFrame to a new parquet file. Returns basename ref."""
        blob_ref = f'{uuid4()}.parquet'
        blob_path = blobfs.join(self.scans_dir, blob_ref)
        if blobfs.exists(blob_path):
            raise RuntimeError(f"Blob path already exists: {blob_path}")
        write_listing(df, blob_path, BLOB_ROW_GROUP_SIZE)
        return blob_ref

    def load(
        self,
        blob_ref: str,
        max_depth: int | None = None,
        min_depth: int | None = None,
        follow_refs: bool = False,
        path_prefix: str | None = None,
    ) -> pd.DataFrame:
        """Load parquet with optional depth/path-prefix filtering via predicate pushdown.

        Args:
            follow_refs: Ignored (parquet doesn't use chunked refs)
        """
        cache_key = f"{blob_ref}:{min_depth}:{max_depth}:{path_prefix}"
        now = time.time()

        # Check cache
        if cache_key in _cache:
            cached_time, cached_df = _cache[cache_key]
            if now - cached_time < CACHE_TTL:
                return cached_df

        blob_path = self._resolve(blob_ref)
        df = None
        if max_depth is not None or min_depth is not None or path_prefix:
            # Check if parquet has 'depth' column for predicate pushdown
            try:
                schema = blobfs.read_schema(blob_path)
                conds = []
                if 'depth' in schema.names:
                    if max_depth is not None:
                        conds.append(('depth', '<=', max_depth))
                    if min_depth is not None:
                        conds.append(('depth', '>=', min_depth))
                if path_prefix:
                    # Exact DNF: the prefix row itself, OR descendants — see
                    # path_prefix_bounds for why the range is exact.
                    lo, hi = path_prefix_bounds(path_prefix)
                    filters = [
                        conds + [('path', '==', path_prefix)],
                        conds + [('path', '>=', lo), ('path', '<', hi)],
                    ]
                elif conds:
                    filters = conds
                else:
                    filters = None
                if filters is not None:
                    df = blobfs.read_parquet(blob_path, filters=filters)
            except Exception:
                pass  # Fall back to full load

        if df is None:
            df = blobfs.read_parquet(blob_path)
            if path_prefix:
                # Fallback load must still honor the exact-semantics contract.
                lo, hi = path_prefix_bounds(path_prefix)
                df = df[(df['path'] == path_prefix) | ((df['path'] >= lo) & (df['path'] < hi))]

        # Update cache (simple LRU)
        if len(_cache) >= CACHE_MAX_SIZE:
            oldest_key = min(_cache.keys(), key=lambda k: _cache[k][0])
            del _cache[oldest_key]
        _cache[cache_key] = (now, df)

        return df

    def get_path_stats(self, blob_ref: str, rel_path: str) -> PathStats | None:
        """Get stats for a path by loading just that depth level."""
        target_depth = rel_path.count('/') + 1 if rel_path else 0
        prefix = rel_path if rel_path and rel_path != '.' else None
        df = self.load(blob_ref, max_depth=target_depth, min_depth=target_depth, path_prefix=prefix)
        match = df[df['path'] == rel_path]
        if match.empty:
            return None
        row = match.iloc[0]
        return PathStats(
            size=int(row['size']),
            n_desc=int(row.get('n_desc', 1) or 1),
            n_children=int(row.get('n_children', 0) or 0),
            mtime=float(row['mtime']) if pd.notna(row.get('mtime')) else None,
        )

    def delete(self, blob_ref: str) -> None:
        """Delete the parquet file."""
        blob_path = self._resolve(blob_ref)
        if blobfs.exists(blob_path):
            blobfs.remove(blob_path)
        # Clear from cache
        keys_to_remove = [k for k in _cache if k.startswith(blob_ref)]
        for k in keys_to_remove:
            del _cache[k]

    def clear_cache(self):
        """Clear the in-memory cache."""
        _cache.clear()
