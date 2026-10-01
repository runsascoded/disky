from abc import ABC, abstractmethod
from dataclasses import dataclass
from os import remove
from shutil import move
from uuid import uuid4

import pandas as pd

from .. import blobfs

# Rows are sorted (depth, path), so bounded row groups give parquet min/max
# stats real pruning power for both the depth and path-prefix pushdowns — a
# single whole-file row group (pandas' default at these sizes) makes every
# pushdown a full read.
# Read-side granularity: a directory listing (`load(min_depth=max_depth=d,
# path_prefix=…)`) decodes every row group whose stats overlap, so this is the
# unit of work per directory read. 64K rows ≈ 4 ms per listing vs
# ~40 ms at 1M (measured 2026-08-28, 4.5M-row home scan); footer cost is
# negligible either way.
BLOB_ROW_GROUP_SIZE = 65_536


def path_prefix_bounds(prefix: str) -> tuple[str, str]:
    """Half-open range `[lo, hi)` containing *exactly* the strings that start
    with `prefix + '/'` — `'0' == chr(ord('/') + 1)`, so nothing sorts between
    `pfx/…` and `pfx0`. Lets SQL backends express "descendant of" as a pure
    range predicate and parquet prune row groups on `path` min/max stats.
    """
    return prefix + '/', prefix + chr(ord('/') + 1)


@dataclass
class PathStats:
    """Stats for a single path in a scan."""
    size: int
    n_desc: int
    n_children: int
    mtime: float | None = None


class StorageBackend(ABC):
    """Abstract interface for scan data storage.

    Implementations store the file tree data from scans. The SQLite metadata
    (Scan table with id, path, time, blob reference) remains separate.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for this backend."""
        pass

    @property
    @abstractmethod
    def supports_updates(self) -> bool:
        """Whether this backend supports in-place updates (delete_path)."""
        pass

    @abstractmethod
    def save(self, df: pd.DataFrame, scan_path: str) -> str:
        """Save scan data and return a blob reference.

        Args:
            df: DataFrame with columns: path, size, mtime, kind, parent, uri, n_desc, n_children, depth
            scan_path: The root path that was scanned (for organizing storage)

        Returns:
            A blob reference string (file path, table name, etc.) to store in Scan.blob
        """
        pass

    def adopt_parquet(self, parquet_path: str, scan_path: str) -> str:
        """Adopt an already-written canonical layer-2 parquet as a scan blob,
        without reading it into memory (a 185M-row layer-2 doesn't fit in RAM
        as a DataFrame — the `import -e duckdb` path writes parquet directly
        and hands the file over here).

        Default implementation covers file-backed backends (anything with a
        `scans_dir`); table-backed backends must override or don't support it.
        """
        scans_dir = getattr(self, 'scans_dir', None)
        if scans_dir is None:
            raise NotImplementedError(f"{self.name} backend cannot adopt parquet files")
        blob_ref = f'{uuid4()}.parquet'
        blob_path = blobfs.join(scans_dir, blob_ref)
        if blobfs.exists(blob_path):
            raise RuntimeError(f"Blob path already exists: {blob_path}")
        if blobfs.is_url(scans_dir):
            # No cross-store rename: upload, then drop the local file.
            blobfs.put(parquet_path, blob_path)
            remove(parquet_path)
        else:
            move(parquet_path, blob_path)
        return blob_ref

    @abstractmethod
    def load(
        self,
        blob_ref: str,
        max_depth: int | None = None,
        min_depth: int | None = None,
        follow_refs: bool = False,
        path_prefix: str | None = None,
    ) -> pd.DataFrame:
        """Load scan data with optional depth filtering.

        Args:
            blob_ref: The blob reference from Scan.blob
            max_depth: Only return rows with depth <= max_depth
            min_depth: Only return rows with depth >= min_depth
            follow_refs: If True, recursively load child chunks (hybrid backend only)
            path_prefix: Only return the row with `path == path_prefix` and rows
                with `path.startswith(path_prefix + '/')` (exact semantics, not a
                superset). Rows are sorted `(depth, path)`, so within each depth
                a prefix is a contiguous run — parquet prunes row groups via
                min/max stats on `path`; SQL backends use a range predicate.

        Returns:
            DataFrame with scan data
        """
        pass

    @abstractmethod
    def get_path_stats(self, blob_ref: str, rel_path: str) -> PathStats | None:
        """Get stats for a specific path within a scan.

        Args:
            blob_ref: The blob reference from Scan.blob
            rel_path: Path relative to scan root (e.g., 'foo/bar' for /scan/root/foo/bar)

        Returns:
            PathStats if found, None otherwise
        """
        pass

    @abstractmethod
    def delete(self, blob_ref: str) -> None:
        """Delete the stored scan data.

        Args:
            blob_ref: The blob reference to delete
        """
        pass

    def delete_path(self, blob_ref: str, rel_path: str) -> PathStats | None:
        """Delete a path from scan data and update ancestor stats.

        Only supported by backends where supports_updates=True.
        For immutable backends, returns None (caller should rescan).

        Args:
            blob_ref: The blob reference from Scan.blob
            rel_path: Path relative to scan root to delete

        Returns:
            PathStats of deleted item if successful, None if not supported/found
        """
        if not self.supports_updates:
            return None
        return self._delete_path_impl(blob_ref, rel_path)

    def _delete_path_impl(self, blob_ref: str, rel_path: str) -> PathStats | None:
        """Implementation of delete_path for backends that support updates."""
        raise NotImplementedError("Backend claims to support updates but didn't implement _delete_path_impl")
