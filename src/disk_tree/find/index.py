from array import array
from dataclasses import dataclass, field
from os.path import dirname
import time as time_module
from typing import Callable

import pandas as pd

from disk_tree import time
from ..backends import backend_for, ErrorCollector
from ..listing_format import uri_of


# Type for progress callback: (items_found, items_per_sec, error_count) -> None
ProgressCallback = Callable[[int, float | None, int], None]


@dataclass
class IndexResult:
    df: pd.DataFrame
    error_count: int = 0
    error_paths: list[str] = field(default_factory=list)


def aggregate(
    rows: pd.DataFrame,
    scan_root: str,
    sum_cols: tuple[str, ...] = (),
    mean_mtime: bool = False,
) -> pd.DataFrame:
    """Bottom-up aggregation: leaf (+ optional dir) rows → canonical layer-2 frame.

    Input columns: `path, size, mtime, kind, parent, uri` (`kind` in {'file','dir'}).
    Walk backends emit both files and dirs; listing imports emit files only —
    the missing dir rows get synthesized by the group-by cascade below. Output
    adds `n_desc`, `n_children`, `depth`; sorted breadth-first for parquet
    row-group pruning.

    `sum_cols` are extra monoid columns already present in `rows` (per-file
    contributions; 0 on dir rows) summed through the cascade unchanged —
    e.g. `--pivot-sum` per-class byte columns. With `mean_mtime`, `rows` must
    also carry `mt_wsum` (exact-int Σ mtime·size contributions, object dtype
    to dodge int64 overflow); it cascades like a sum_col and converts to the
    `mtime_mean` output column at the end (see find/agg_ext.py).
    """
    from .agg_ext import MT_WSUM, MTIME_MEAN, mean_of
    cascade_cols = [*sum_cols, *([MT_WSUM] if mean_mtime else [])]
    df = rows
    df['n_desc'] = 1
    df['n_children'] = 0
    # `n_files` = objects-only count, distinct from `n_desc` which also counts
    # every synthesized dir row. Consumers coming from S3/GCS think in
    # "objects" — `n_files` gives them that; `n_desc` stays the mechanism.
    df['n_files'] = (df.kind == 'file').astype('int64')

    dirs0 = df[df.kind == 'dir']
    # Aggregation only needs these 6 columns. Slicing here keeps the per-iter
    # `.copy()` below 6-column-wide instead of dragging `kind` / `uri` /
    # `n_children` along (the per-row Python-string overhead dominates).
    cur = df[['path', 'parent', 'size', 'mtime', 'n_desc', 'n_files', *cascade_cols]]
    dir_dfs = [dirs0]
    level = 0
    while True:
        with time(f"index-agg-{level}"):
            cur = cur[cur.path != ''].copy()
            if cur.empty:
                break
            cur['path'] = cur['parent']
            grouped = cur.groupby('path')
            sizes = grouped['size'].sum()
            n_children = grouped.size() if level == 0 else 0
            mtimes = grouped['mtime'].max()
            n_desc = grouped['n_desc'].sum()
            n_files = grouped['n_files'].sum()
            dirs = pd.DataFrame({
                'path': sizes.index,
                'size': sizes,
                'mtime': mtimes,
                'n_desc': n_desc,
                'n_files': n_files,
                'n_children': n_children,
                **{c: grouped[c].sum() for c in cascade_cols},
            }).reset_index(drop=True)
            dirs['parent'] = dirs.path.apply(dirname)
            dir_dfs.append(dirs)
            cur = dirs
            level += 1

    with time("index-agg-dirs"):
        # Materialize the files slice only now (deferred to free RAM during
        # the agg loop; the loop doesn't need file rows in their wide form).
        files = df[df.kind == 'file']
        dirs = pd.concat(dir_dfs)
        if dirs.empty:
            dirs = pd.DataFrame([{
                'path': '.',
                'size': 0,
                'mtime': 0,
                'n_desc': 0,
                'n_files': 0,
                'n_children': 0,
                'kind': 'dir',
                'parent': '',
                'uri': scan_root,
                **{c: 0 for c in cascade_cols},
            }])
        else:
            grouped = dirs.groupby('path')
            sizes = grouped['size'].sum()
            dirs = pd.DataFrame({
                'path': sizes.index,
                'size': sizes,
                'mtime': grouped['mtime'].max(),
                'n_desc': grouped['n_desc'].sum(),
                'n_files': grouped['n_files'].sum(),
                'n_children': grouped['n_children'].sum(),
                'kind': 'dir',
                **{c: grouped[c].sum() for c in cascade_cols},
            }).reset_index(drop=True)
            dirs['parent'] = dirs.path.apply(dirname)
            dirs.loc[dirs.parent == '', 'parent'] = '.'
            dirs.loc[dirs.path == '', ['path', 'parent']] = ['.', '']
            # The same join the backends use for file rows (`/` root → `/foo`,
            # not `//foo`), so `uri` is `uri_of(root, path)` on every row and
            # the v2 writer can leave it implied (spec `listing-slim.md`).
            dirs['uri'] = dirs.path.apply(lambda p: uri_of(scan_root, p))
        out = pd.concat([dirs, files], ignore_index=True)
        # Add depth column for efficient parquet filtering
        # '.' = 0, 'foo' = 1, 'foo/bar' = 2, etc.
        out['depth'] = out['path'].apply(lambda p: 0 if p == '.' else p.count('/') + 1)
        if mean_mtime:
            # Files carry their own mtime; dirs divide the exact partial sums
            # (the single blessed double division — see agg_ext.mean_of).
            out[MTIME_MEAN] = [
                float(m) if k == 'file' else mean_of(w, s)
                for k, m, w, s in zip(out['kind'], out['mtime'], out[MT_WSUM], out['size'])
            ]
            out = out.drop(columns=[MT_WSUM])
        # Sort by depth first (breadth-first order) for efficient parquet row group filtering
        return out.sort_values(['depth', 'path']).reset_index(drop=True)


def index(
    path: str,
    sudo: bool = False,
    mean_mtime: bool = False,
    progress_callback: ProgressCallback | None = None,
    progress_interval: float = 1.0,
    excludes: list[str] | None = None,
    progress: bool = True,
) -> IndexResult:
    path0 = path.rstrip('/') or '/'
    errors = ErrorCollector()
    backend = backend_for(path0)

    last_progress_time = time_module.time()
    items_count = 0
    start_time = last_progress_time

    # Accumulate columns in parallel lists (much cheaper than 7M dict objects).
    path_l: list[str] = []
    size_l = array('q')  # signed int64
    mtime_l = array('q')
    kind_l: list[str] = []
    parent_l: list[str | None] = []
    uri_l: list[str] = []

    def collect():
        nonlocal last_progress_time, items_count
        kwargs = dict(errors=errors, excludes=excludes, sudo=sudo, progress=progress)
        for e in backend.list(path0, **kwargs):
            items_count += 1
            now = time_module.time()
            if progress_callback and (now - last_progress_time) >= progress_interval:
                elapsed = now - start_time
                items_per_sec = items_count / elapsed if elapsed > 0 else None
                progress_callback(items_count, items_per_sec, errors.count)
                last_progress_time = now
            path_l.append(e['path'])
            size_l.append(e['size'])
            mtime_l.append(e['mtime'])
            kind_l.append(e['kind'])
            parent_l.append(e['parent'])
            uri_l.append(e['uri'])

    with time("files_iter"):
        collect()

    if progress_callback:
        elapsed = time_module.time() - start_time
        items_per_sec = items_count / elapsed if elapsed > 0 else None
        progress_callback(items_count, items_per_sec, errors.count)

    # Handle empty bucket/directory: return early with just a root row
    if not path_l:
        df = pd.DataFrame([{
            'path': '.',
            'size': 0,
            'mtime': 0,
            'n_desc': 0,
            'n_children': 0,
            'kind': 'dir',
            'parent': '',
            'uri': path0,
            'depth': 0,
        }])
        if mean_mtime:
            from .agg_ext import MTIME_MEAN
            df[MTIME_MEAN] = pd.array([None], dtype='float64')
        return IndexResult(
            df=df,
            error_count=errors.count,
            error_paths=errors.paths,
        )

    df = pd.DataFrame({
        'path': path_l,
        'size': size_l,
        'mtime': mtime_l,
        'kind': kind_l,
        'parent': parent_l,
        'uri': uri_l,
    })
    if mean_mtime:
        from .agg_ext import MT_WSUM
        # Every row contributes size·mtime — including dir/symlink inodes,
        # whose own block sizes cascade into `size`; matching them here keeps
        # weights summing to exactly `size` (an empty dir would otherwise get
        # wsum=0 over size>0, i.e. a nonsense epoch-1970 mean). Python bigints
        # (object dtype): Σ mtime·size overflows int64 at PB scale.
        df[MT_WSUM] = pd.array([int(s) * int(m) for s, m in zip(size_l, mtime_l)], dtype=object)
    # Free the per-column lists now that the DataFrame owns the data — peak memory
    # otherwise has both representations resident through the aggregation passes.
    del path_l, size_l, mtime_l, kind_l, parent_l, uri_l
    return IndexResult(
        df=aggregate(df, scan_root=path0, mean_mtime=mean_mtime),
        error_count=errors.count,
        error_paths=errors.paths,
    )
