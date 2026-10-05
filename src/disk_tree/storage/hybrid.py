"""Hybrid storage backend - chunked parquets with SQLite metadata.

Large scans are automatically split into smaller chunks:
- Root scan contains depth-1 summary + refs to child chunks
- Child chunks contain full subtrees (if small) or further refs
- Threshold: subtrees with n_desc >= CHUNK_THRESHOLD get their own chunk

This keeps parquets small for fast updates while preserving full history.
"""
from os import makedirs
from os.path import isabs
from uuid import uuid4

import pandas as pd

from .base import BLOB_ROW_GROUP_SIZE, StorageBackend, PathStats, path_prefix_bounds
from .. import blobfs, config as _config
from ..config import ROOT_DIR
from ..listing_format import write_listing
from ..shallow import build_shallow, remove_shallow, shallow_path, top_rows, write_shallow

# Subtrees with >= this many descendants get chunked into separate parquets
CHUNK_THRESHOLD = 100_000


class HybridBackend(StorageBackend):
    """Hybrid parquet + SQLite chunked storage.

    Large scans are split into manageable chunks. Each chunk is a parquet file
    referenced by the parent scan. Updates only rewrite affected chunks.
    """

    def __init__(self, scans_dir: str | None = None, chunk_threshold: int = CHUNK_THRESHOLD):
        # Resolve via the config module so tests can monkeypatch SCANS_DIR at runtime.
        self.scans_dir = scans_dir or _config.SCANS_DIR
        self.chunk_threshold = chunk_threshold
        if not blobfs.is_url(self.scans_dir):
            makedirs(self.scans_dir, exist_ok=True)
        # Cache for loaded dataframes
        self._cache: dict[str, pd.DataFrame] = {}

    def _resolve(self, blob_ref: str) -> str:
        # Legacy absolute paths are honored; new refs are basenames.
        # Blobs may live on an external volume; search every read dir.
        return blob_ref if isabs(blob_ref) else _config.resolve_scan_blob(blob_ref, self.scans_dir)

    def _exists(self, blob_ref: str) -> bool:
        return blobfs.exists(self._resolve(blob_ref))

    @property
    def name(self) -> str:
        return "hybrid"

    @property
    def supports_updates(self) -> bool:
        return True  # Via chunked updates

    def save(self, df: pd.DataFrame, scan_path: str, parent_scan_id: int | None = None) -> str:
        """Save scan data, chunking large subtrees.

        Returns blob_ref for the root/summary parquet.
        Creates additional chunk parquets as needed.

        For a ~7M-row home-dir scan, the full df is ~3 GiB; the prior implementation
        kept three copies coexisting (main df + extracted subtree + rebased subtree)
        plus the pyarrow Table inside `to_parquet`, which drove the peak above 8 GiB.
        We now (1) skip the upfront full-df copy, (2) extract+rebase each subtree
        into a single new df instead of two, and (3) drop the subtree references
        before the next iteration so its memory can be reclaimed.
        """
        import gc
        import pyarrow as pa

        # Ensure required column exists. Mutate in place to avoid copying the
        # entire 7M-row df just to add one column of Nones.
        if 'child_scan_id' not in df.columns:
            df['child_scan_id'] = None

        # Find subtrees that need chunking (depth-1 dirs with n_desc >= threshold).
        # Only chunk at depth 1 to avoid explosion of tiny chunks.
        depth1_dirs = df[(df['depth'] == 1) & (df['kind'] == 'dir')]
        large_subtrees = depth1_dirs[depth1_dirs['n_desc'] >= self.chunk_threshold]

        chunk_refs = {}  # path -> blob_ref
        tops = []  # (blob_ref, depth-1 rows) per chunk → the shallow sidecar

        for _, row in large_subtrees.iterrows():
            subtree_path = row['path']
            subtree_prefix = subtree_path + '/'

            descendant_mask = df['path'].str.startswith(subtree_prefix)
            subtree_mask = (df['path'] == subtree_path) | descendant_mask
            if subtree_mask.sum() <= 1:
                continue

            # Build the rebased chunk df, convert to an Arrow table, then drop
            # the pandas frame BEFORE writing — keeps only Arrow buffers (not
            # also Python string objects) resident during disk I/O.
            subtree_df = self._extract_and_rebase(df, subtree_mask, subtree_path)
            table = pa.Table.from_pandas(subtree_df, preserve_index=False)
            del subtree_df
            chunk_ref = chunk_refs[subtree_path] = self._save_parquet_arrow(table)
            tops.append((chunk_ref, top_rows(table)))
            del table

            # Drop the descendants now; only the summary row stays in df.
            df = df[~descendant_mask]
            gc.collect()

        # Stamp child_scan_id refs into the surviving summary rows.
        for subtree_path, chunk_blob_ref in chunk_refs.items():
            df.loc[df['path'] == subtree_path, 'child_scan_id'] = chunk_blob_ref

        # Same drop-pandas-before-write trick for the root summary parquet.
        table = pa.Table.from_pandas(df, preserve_index=False)
        del df
        root_ref = self._save_parquet_arrow(table)
        # Each chunk's top level beside the root, so a page load at the root
        # never opens a chunk blob (spec `specs/done/scan-page-r2-latency.md`).
        write_shallow(self._resolve(root_ref), tops)
        return root_ref

    def _extract_and_rebase(self, df: pd.DataFrame, mask: pd.Series, root_path: str) -> pd.DataFrame:
        """Materialize the masked subset with paths/parents/depth rebased to `root_path`.

        Returns a fresh DataFrame; the source `df` is not mutated.
        """
        prefix = root_path + '/'
        plen = len(prefix)

        def rebase_path(p):
            if p == root_path:
                return '.'
            if p.startswith(prefix):
                return p[plen:]
            return p

        def rebase_parent(p):
            if p == root_path:
                return '.'
            if p.startswith(prefix):
                return p[plen:]
            if not p or p == '.':
                return ''
            return p

        new_paths = df.loc[mask, 'path'].map(rebase_path)
        new_parents = df.loc[mask, 'parent'].map(rebase_parent)
        new_depths = new_paths.map(lambda p: 0 if p == '.' else p.count('/') + 1)
        # `assign` returns a new df sharing untouched column data with the source
        # while replacing path/parent/depth with our rebased versions.
        return df.loc[mask].assign(path=new_paths, parent=new_parents, depth=new_depths)

    def _save_parquet(self, df: pd.DataFrame) -> str:
        """Save a single parquet file, return basename blob_ref."""
        blob_ref = f'{uuid4()}.parquet'
        blob_path = blobfs.join(self.scans_dir, blob_ref)
        write_listing(df, blob_path, BLOB_ROW_GROUP_SIZE)
        return blob_ref

    def _save_parquet_arrow(self, table: 'pa.Table') -> str:
        """Write an already-constructed Arrow table; caller is expected to have
        dropped the source DataFrame so peak memory holds Arrow buffers only.

        Written as a v2 listing (spec `listing-slim.md`): a chunk's scan root
        is its own `.` row's `uri` (the subtree's absolute location — chunk
        rows are rebased, their `uri`s are not), so `blobfs.read_parquet`
        restores exactly the `uri` column the chunk was cut with.
        """
        blob_ref = f'{uuid4()}.parquet'
        blob_path = blobfs.join(self.scans_dir, blob_ref)
        write_listing(table, blob_path, BLOB_ROW_GROUP_SIZE)
        return blob_ref

    def _rebase_paths(self, df: pd.DataFrame, root_path: str) -> pd.DataFrame:
        """Rebase paths so root_path becomes '.'"""
        df = df.copy()
        prefix = root_path + '/'

        def rebase_path(p):
            if p == root_path:
                return '.'
            elif p.startswith(prefix):
                return p[len(prefix):]
            return p

        def rebase_parent(p):
            if p == root_path:
                # Parent of root's direct children is '.'
                return '.'
            elif p.startswith(prefix):
                # Strip prefix to get rebased parent path
                return p[len(prefix):]
            elif not p or p == '.':
                # Root's parent stays empty
                return ''
            return p

        df['path'] = df['path'].apply(rebase_path)
        df['parent'] = df['parent'].apply(rebase_parent)

        # Recalculate depth
        df['depth'] = df['path'].apply(lambda p: 0 if p == '.' else p.count('/') + 1)

        return df

    def load(
        self,
        blob_ref: str,
        max_depth: int | None = None,
        min_depth: int | None = None,
        follow_refs: bool = False,
        path_prefix: str | None = None,
    ) -> pd.DataFrame:
        """Load scan data with optional depth/path-prefix filtering.

        Args:
            follow_refs: If True, recursively load child chunks and merge.
                With `path_prefix` this loads the full parquet (a chunk
                placeholder that is an *ancestor* of the prefix wouldn't
                survive a range pushdown), expands refs, then applies the
                prefix as an exact mask.
        """
        # Check cache
        cache_key = f"{blob_ref}:{min_depth}:{max_depth}:{follow_refs}:{path_prefix}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        # Load parquet with predicate pushdown (depth and, when not following
        # refs, path-prefix — rows are sorted (depth, path), so row-group
        # min/max stats on both columns prune effectively).
        df = None
        pushdown_prefix = path_prefix if not follow_refs else None
        if max_depth is not None or min_depth is not None or pushdown_prefix:
            try:
                schema = blobfs.read_schema(self._resolve(blob_ref))
                conds = []
                if 'depth' in schema.names:
                    if max_depth is not None:
                        conds.append(('depth', '<=', max_depth))
                    if min_depth is not None:
                        conds.append(('depth', '>=', min_depth))
                if pushdown_prefix:
                    # Exact DNF: the prefix row itself, OR descendants — see
                    # path_prefix_bounds for why the range is exact.
                    lo, hi = path_prefix_bounds(pushdown_prefix)
                    filters = [
                        conds + [('path', '==', pushdown_prefix)],
                        conds + [('path', '>=', lo), ('path', '<', hi)],
                    ]
                elif conds:
                    filters = conds
                else:
                    filters = None
                if filters is not None:
                    df = blobfs.read_parquet(self._resolve(blob_ref), filters=filters)
            except Exception:
                pass  # Fall back to full load + pandas masks

        if df is None:
            df = blobfs.read_parquet(self._resolve(blob_ref))
            if 'depth' in df.columns:
                if max_depth is not None:
                    df = df[df['depth'] <= max_depth]
                if min_depth is not None:
                    df = df[df['depth'] >= min_depth]
            if pushdown_prefix:
                lo, hi = path_prefix_bounds(pushdown_prefix)
                df = df[(df['path'] == pushdown_prefix) | ((df['path'] >= lo) & (df['path'] < hi))]

        # Optionally follow child_scan_id references
        if follow_refs and 'child_scan_id' in df.columns:
            refs = df[df['child_scan_id'].notna()]
            for _, row in refs.iterrows():
                child_blob_ref = row['child_scan_id']
                child_path = row['path']
                if self._exists(child_blob_ref):
                    child_df = self.load(child_blob_ref, follow_refs=True)
                    # Rebase child paths back to parent coordinate system
                    child_df = self._unbase_paths(child_df, child_path)
                    # Remove the placeholder row and add expanded children
                    df = df[df['path'] != child_path]
                    df = pd.concat([df, child_df], ignore_index=True)

        if follow_refs and path_prefix:
            # Prefix wasn't pushed down (see docstring); apply it post-expansion.
            lo, hi = path_prefix_bounds(path_prefix)
            df = df[(df['path'] == path_prefix) | ((df['path'] >= lo) & (df['path'] < hi))]

        self._cache[cache_key] = df
        return df

    def _unbase_paths(self, df: pd.DataFrame, parent_path: str) -> pd.DataFrame:
        """Inverse of _rebase_paths - expand '.' back to parent_path."""
        df = df.copy()

        # Vectorized (a per-row .apply here cost ~10s per million rows when a
        # follow_refs load expanded a large chunk).
        path = df['path']
        df['path'] = (parent_path + '/' + path).mask(path == '.', parent_path)
        parent = df['parent'].fillna('')
        grandparent = parent_path.rsplit('/', 1)[0] if '/' in parent_path else '.'
        df['parent'] = (
            (parent_path + '/' + parent)
            .mask(parent == '', grandparent)
            .mask(parent == '.', parent_path)
        )
        if 'depth' in df.columns:
            # Chunk-relative depths are stale once paths are re-rooted.
            df['depth'] = df['path'].str.count('/') + 1

        return df

    def get_path_stats(self, blob_ref: str, rel_path: str) -> PathStats | None:
        """Get stats for a specific path, following refs if needed."""
        df = blobfs.read_parquet(self._resolve(blob_ref))

        # Direct match
        match = df[df['path'] == rel_path]
        if not match.empty:
            row = match.iloc[0]
            return PathStats(
                size=int(row['size']) if pd.notna(row['size']) else 0,
                n_desc=int(row.get('n_desc', 0) or 0),
                n_children=int(row.get('n_children', 0) or 0),
                mtime=float(row['mtime']) if pd.notna(row.get('mtime')) else None,
            )

        # Check if path is inside a chunked subtree
        if 'child_scan_id' in df.columns:
            for _, row in df[df['child_scan_id'].notna()].iterrows():
                chunk_root = row['path']
                if rel_path.startswith(chunk_root + '/'):
                    # Path is inside this chunk
                    child_blob_ref = row['child_scan_id']
                    if self._exists(child_blob_ref):
                        # Rebase the path relative to chunk root
                        child_rel_path = rel_path[len(chunk_root) + 1:]
                        return self.get_path_stats(child_blob_ref, child_rel_path)

        return None

    def delete(self, blob_ref: str) -> None:
        """Delete the parquet file and any child chunks."""
        blob_path = self._resolve(blob_ref)
        if not blobfs.exists(blob_path):
            return

        # Load to find child refs
        try:
            df = blobfs.read_parquet(blob_path)
            if 'child_scan_id' in df.columns:
                for child_ref in df['child_scan_id'].dropna():
                    if self._exists(child_ref):
                        self.delete(child_ref)  # Recursive delete
        except Exception:
            pass

        # Delete this parquet (and its sidecar)
        if blobfs.exists(blob_path):
            blobfs.remove(blob_path)
        remove_shallow(blob_path)

        # Clear cache
        self._cache = {k: v for k, v in self._cache.items() if not k.startswith(blob_ref)}

    def _delete_path_impl(self, blob_ref: str, rel_path: str) -> PathStats | None:
        """Delete a path, updating only the affected chunk."""
        blob_path = self._resolve(blob_ref)
        df = blobfs.read_parquet(blob_path)

        # Check if path is in a child chunk
        if 'child_scan_id' in df.columns:
            for idx, row in df[df['child_scan_id'].notna()].iterrows():
                chunk_root = row['path']
                if rel_path == chunk_root or rel_path.startswith(chunk_root + '/'):
                    # Deletion is inside this chunk
                    child_blob_ref = row['child_scan_id']
                    if self._exists(child_blob_ref):
                        if rel_path == chunk_root:
                            # Deleting the entire chunked subtree
                            stats = PathStats(
                                size=int(row['size']),
                                n_desc=int(row.get('n_desc', 0)),
                                n_children=int(row.get('n_children', 0)),
                            )
                            # Delete the chunk file
                            self.delete(child_blob_ref)
                            # Remove row from parent and update ancestors
                            df = df[df['path'] != chunk_root]
                            # Add 1 because the deleted item itself counts as a descendant
                            self._update_ancestors(df, chunk_root, stats.size, stats.n_desc + 1)
                            write_listing(df, blob_path, BLOB_ROW_GROUP_SIZE)
                            self._refresh_shallow(blob_path)
                            self._cache.clear()
                            return stats
                        else:
                            # Deletion is inside the chunk - recurse
                            child_rel_path = rel_path[len(chunk_root) + 1:]
                            stats = self._delete_path_impl(child_blob_ref, child_rel_path)
                            if stats:
                                # Update summary row in parent (stats already has +1 applied from recursion)
                                # But for the summary row itself, we subtract the raw n_desc + 1
                                df.loc[df['path'] == chunk_root, 'size'] -= stats.size
                                df.loc[df['path'] == chunk_root, 'n_desc'] -= (stats.n_desc + 1)
                                # Update root ancestors
                                self._update_ancestors(df, chunk_root, stats.size, stats.n_desc + 1)
                                write_listing(df, blob_path, BLOB_ROW_GROUP_SIZE)
                                self._refresh_shallow(blob_path)
                                self._cache.clear()
                            return stats

        # Path is directly in this parquet (not chunked)
        match = df[df['path'] == rel_path]
        if match.empty:
            return None

        row = match.iloc[0]
        stats = PathStats(
            size=int(row['size']) if pd.notna(row['size']) else 0,
            n_desc=int(row.get('n_desc', 0) or 0),
            n_children=int(row.get('n_children', 0) or 0),
        )

        # Remove the path and its descendants
        deleted_mask = (df['path'] == rel_path) | df['path'].str.startswith(rel_path + '/')
        df = df[~deleted_mask].copy()

        # Update ancestors - add 1 because the deleted item itself counts as a descendant
        self._update_ancestors(df, rel_path, stats.size, stats.n_desc + 1)

        write_listing(df, blob_path, BLOB_ROW_GROUP_SIZE)
        self._cache.clear()
        return stats

    def _refresh_shallow(self, blob_path: str) -> None:
        """A chunk was rewritten (or dropped) under this root: rebuild its shallow
        sidecar from the chunks that remain, if it has one."""
        if blobfs.exists(shallow_path(blob_path)):
            build_shallow(blob_path, self._resolve, force=True)

    def _update_ancestors(self, df: pd.DataFrame, deleted_path: str, deleted_size: int, deleted_n_desc: int):
        """Update ancestor stats after deletion (in-place)."""
        parts = deleted_path.split('/') if deleted_path != '.' else []
        ancestors = ['.']
        for i in range(1, len(parts)):
            ancestors.append('/'.join(parts[:i]))

        for ancestor in ancestors:
            mask = df['path'] == ancestor
            if mask.any():
                df.loc[mask, 'size'] = df.loc[mask, 'size'] - deleted_size
                df.loc[mask, 'n_desc'] = df.loc[mask, 'n_desc'] - deleted_n_desc
                # n_children only for direct parent
                if ancestor == ('/'.join(parts[:-1]) if len(parts) > 1 else '.'):
                    df.loc[mask, 'n_children'] = df.loc[mask, 'n_children'] - 1

    def clear_cache(self):
        """Clear the in-memory cache."""
        self._cache.clear()

    def get_chunk_stats(self, blob_ref: str) -> dict:
        """Get info about chunking for a scan."""
        blob_path = self._resolve(blob_ref)
        if not blobfs.exists(blob_path):
            return {'error': 'not found'}

        df = blobfs.read_parquet(blob_path)
        chunks = []
        if 'child_scan_id' in df.columns:
            for _, row in df[df['child_scan_id'].notna()].iterrows():
                child_ref = row['child_scan_id']
                chunk_info = {
                    'path': row['path'],
                    'size': int(row['size']),
                    'n_desc': int(row.get('n_desc', 0)),
                    'blob_ref': child_ref,
                }
                # Recursively get chunk stats
                if self._exists(child_ref):
                    chunk_info['nested'] = self.get_chunk_stats(child_ref)
                chunks.append(chunk_info)

        return {
            'blob_ref': blob_ref,
            'rows': len(df),
            'chunks': chunks,
            'total_chunks': len(chunks) + sum(
                c.get('nested', {}).get('total_chunks', 0) for c in chunks
            ),
        }
