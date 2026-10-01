"""Import a pre-made object listing → canonical layer-2 scan.

Alternative producer to :func:`disk_tree.find.index` (the live walker): reads
one or more parquet globs conforming to the layer-1 listing schema (raw
object listing, GCS Storage Insights, or S3 Inventory — schema-normalized by
:mod:`disk_tree.listing`), filters to a single bucket, and runs the same
bottom-up aggregation as ``index()`` to produce a canonical per-path
DataFrame.

Phase-0 aggregation is in-memory pandas — fine for laptop-scale or
subsampled cloud listings; the multi-PB regime needs the out-of-core DuckDB
aggregation path (spec item B).
"""

from __future__ import annotations

import re
from os.path import dirname
from typing import TYPE_CHECKING

import pandas as pd

from disk_tree.listing import prepare_listing
from .index import aggregate, IndexResult

if TYPE_CHECKING:
    import duckdb


# Collapse consecutive slashes in an object key so `a//b` reads as `a/b`.
# Rationale: some object stores accept keys with empty path components (real
# marin example: `tokenized/finemath_3_plus-a26b0f//.artifact.json`).
# `os.path.dirname` treats `a/b/` (trailing slash) as its own thing (parent
# `a/b`), which then breaks parent-walking (the trailing-slash dir gets no
# further ancestor, so it hops to the tree root instead of nesting under its
# real prefix). Collapsing at ingest matches user intent, keeps the tree
# subtree-preserving, and lets every downstream regex stay simple.
_SLASHES = re.compile(r'/+')


def _canonicalize(name: str) -> str:
    """`a//b` → `a/b`; strip trailing slashes. Leading slashes stay (unusual)."""
    n = _SLASHES.sub('/', name)
    return n.rstrip('/')


def import_listing(
    listings: tuple[str, ...],
    bucket: str,
    scheme: str = 'gcs',
    con: "duckdb.DuckDBPyConnection | None" = None,
    pivot_sums: tuple[str, ...] = (),
    mean_mtime: bool = False,
) -> IndexResult:
    """Read `bucket`'s rows from `listings` and aggregate to a canonical scan.

    `listings` is a tuple of parquet globs; multiple sources are merged with
    earlier-source-wins-per-bucket semantics (see :func:`prepare_listing`).
    `scheme` sets the URI prefix (e.g. ``gcs`` → ``gcs://<bucket>/<name>``).
    `pivot_sums` / `mean_mtime` are the opt-in aggregation extensions
    (see :mod:`disk_tree.find.agg_ext`).
    """
    import duckdb as _duckdb
    from .agg_ext import MT_WSUM, check_pivot_values, pivot_col
    if con is None:
        con = _duckdb.connect()
    src = prepare_listing(con, listings)
    extra_sel = ''.join(f', {c}' for c in pivot_sums)
    df = con.execute(
        f"SELECT name, size_bytes, floor(epoch(created))::BIGINT AS created{extra_sel} FROM {src} WHERE bucket = ? ORDER BY name",
        [bucket],
    ).df()
    if df.empty:
        raise ValueError(f"no rows for bucket {bucket!r} in listings {listings}")
    from disk_tree.backends.url import canonical
    scan_root = canonical(f'{scheme}://{bucket}')  # `file` roots → the bare path
    # Collapse `//` empty-component keys here so the parent-walking downstream
    # never sees a trailing-slash path.
    names = df['name'].astype(str).map(_canonicalize)
    files = pd.DataFrame({
        'path': names,
        'size': df['size_bytes'].fillna(0).astype('int64'),
        'mtime': df['created'].fillna(0).astype('int64'),
        'kind': 'file',
        'parent': names.apply(dirname),
        'uri': [f'{scan_root}/{n}' for n in names],
    })
    sum_cols: list[str] = []
    for col in pivot_sums:
        vals = check_pivot_values(col, sorted(pd.unique(df[col].dropna())))
        for v in vals:
            name = pivot_col(col, v)
            files[name] = files['size'].where(df[col] == v, 0)
            sum_cols.append(name)
    if mean_mtime:
        # Exact Σ mtime·size partials — Python bigints in an object column
        # (int64 would overflow at PB·epoch scale; see agg_ext module doc).
        files[MT_WSUM] = pd.Series(
            [int(m) * int(s) for m, s in zip(files['mtime'], files['size'])],
            dtype=object,
        )
    # Synthesize dir rows for every unique parent path in the listing — walk
    # backends emit these inline (see backends/s3.py:84-92), and their absence
    # would leave `n_children` under-counted (it counts input rows per parent,
    # so missing sub-dir rows means the sub-dir wouldn't count toward its
    # parent's direct-child tally).
    # Include '' (scan root) so root's n_desc includes-self, matching walk
    # backends (gfind and the S3 lister emit `path='' kind='dir'` for the scan root).
    dir_paths = {''}
    for p in files['parent']:
        while p and p not in dir_paths:
            dir_paths.add(p)
            p = dirname(p)
    dir_names = sorted(dir_paths)
    dirs = pd.DataFrame({
        'path': dir_names,
        'size': 0,
        'mtime': 0,
        'kind': 'dir',
        'parent': [dirname(p) for p in dir_names],
        'uri': [f'{scan_root}/{p}' for p in dir_names],
        **{c: 0 for c in sum_cols},
        **({MT_WSUM: 0} if mean_mtime else {}),
    })
    rows = pd.concat([files, dirs], ignore_index=True)
    return IndexResult(
        df=aggregate(rows, scan_root=scan_root, sum_cols=tuple(sum_cols), mean_mtime=mean_mtime),
        error_count=0,
        error_paths=[],
    )


def list_buckets(listings: tuple[str, ...], con: "duckdb.DuckDBPyConnection | None" = None) -> list[str]:
    """Distinct bucket names present in `listings`."""
    import duckdb as _duckdb
    if con is None:
        con = _duckdb.connect()
    src = prepare_listing(con, listings)
    return [r[0] for r in con.execute(f"SELECT DISTINCT bucket FROM {src} ORDER BY bucket").fetchall()]
