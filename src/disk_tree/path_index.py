"""Read the site's path index (`dt-cloud path-index` → `path-index.parquet`) for
the CLI: the laptop's scans already publish one per ingest (`listing/laptop/
<date>/index/<gen>/` in R2), so `du` can answer from it instead of a separate
scan blob.

Rows are every path (dirs and files) with sizes rolled up; `path` is absolute
without the leading `/` (`Users/ryan/c`), `depth` its segment count, and the
file is sorted `(depth, path)` in small row groups — so a subtree read with
`depth` / `path` range filters fetches only the groups that hold it.
"""
from __future__ import annotations

import re

import pandas as pd

from disk_tree import blobfs

FILE = 'path-index.parquet'
_DATE = re.compile(r'\d{4}-\d{2}-\d{2}$')
COLUMNS = ['path', 'depth', 'size', 'kind', 'mtime', 'n_desc']


def _ls(d: str) -> list[str]:
    """Child names of a local dir or URL prefix (empty if it doesn't exist)."""
    if blobfs.is_url(d):
        fs, p = blobfs.fs_for(d)
        try:
            return [e.rstrip('/').rsplit('/', 1)[-1] for e in fs.ls(p, detail=False)]
        except FileNotFoundError:
            return []
    import os
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def latest_path_index(src: str) -> str:
    """`src` itself if it names a parquet; else the newest generation under a
    `<date>/index/<gen>/` root (newest date that has one, then newest gen)."""
    if src.endswith('.parquet'):
        return src
    root = src.rstrip('/')
    for date in sorted((n for n in _ls(root) if _DATE.match(n)), reverse=True):
        gens = sorted(_ls(blobfs.join(blobfs.join(root, date), 'index')), reverse=True)
        for gen in gens:
            path = blobfs.join(blobfs.join(blobfs.join(blobfs.join(root, date), 'index'), gen), FILE)
            if blobfs.exists(path):
                return path
    raise FileNotFoundError(f'no {FILE} under {src} (<date>/index/<gen>/)')


def source_label(path: str) -> str:
    """`<date>/index/<gen>` for a path under such a root, else the path."""
    m = re.search(r'(\d{4}-\d{2}-\d{2}/index/[^/]+)/' + re.escape(FILE) + '$', path)
    return m.group(1) if m else path


def read_subtree(path: str, uri: str, depth: int) -> tuple[pd.DataFrame, int]:
    """`uri`'s descendants down to `depth` levels below it, as `du`'s frame
    (`path` relative to `uri`, `depth` 1-based below it), and `uri`'s own
    size. Owner slices (`usr`, where a store has them) are summed per path."""
    prefix = uri.strip('/')
    d0 = prefix.count('/') + 1 if prefix else 0
    filters = [('depth', '>=', d0 + 1), ('depth', '<=', d0 + depth)]
    if prefix:
        # (depth, path)-sorted: a `[prefix/, prefix0)` range prunes row groups.
        filters += [('path', '>=', prefix + '/'), ('path', '<', prefix + '0')]
    df = blobfs.read_table(path, columns=COLUMNS, filters=filters).to_pandas()
    df = df.groupby('path', as_index=False).agg(
        depth=('depth', 'first'), size=('size', 'sum'), kind=('kind', 'first'),
        mtime=('mtime', 'max'), n_desc=('n_desc', 'max'),
    )
    if prefix:
        own = blobfs.read_table(path, columns=['path', 'size'], filters=[('depth', '=', d0), ('path', '=', prefix)]).to_pandas()
        root_size = int(own['size'].sum())
        df['path'] = df['path'].str.slice(len(prefix) + 1)
    else:
        root_size = int(df.loc[df['depth'] == 1, 'size'].sum())
    df['depth'] = df['depth'] - d0
    return df, root_size
