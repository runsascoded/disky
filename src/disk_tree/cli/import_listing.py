"""`disk-tree import` — ingest pre-made object listings.

Three aggregation engines:

- `--engine pandas` (default) — fine for laptop-scale inputs or subsampled
  cloud listings; the whole layer-1 frame + intermediates live in RAM.
- `--engine duckdb` — out-of-core, spills to `--temp-dir` under
  `--memory-limit`. Handles unsorted / mixed-schema listings.
- `--engine stream` — O(depth) streaming rollup over sorted raw listings
  (bulk-list output); KBs of working state, one bounded final sort. The
  100M+-row path (spec: streaming-aggregation.md).

All produce byte-identical canonical layer-2 output.
"""

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone

from click import Choice, argument, option
from dateutil.parser import parse as parse_dt
from utz import err

from disk_tree.cli.base import cli
from disk_tree.find.aggregate_duckdb import DEFAULT_PARTITION_FILES


@cli.command('import')
@option('-e', '--engine', type=Choice(['pandas', 'duckdb', 'stream']), default='pandas', help='Aggregation engine: `pandas` (in-memory; small), `duckdb` (out-of-core; big), or `stream` (O(depth) over sorted listings; biggest)')
@option('-l', '--listing', 'listings', required=True, multiple=True, help='Listing parquet glob(s) — raw / SII / S3-Inventory; repeatable, earlier sources win per bucket')
@option('-A', '--max-col', 'max_cols', multiple=True, help='DuckDB engine only: a `--side` column folded through the cascade as a subtree MAX (e.g. `last_ts`); repeatable')
@option('-a', '--side', default=None, help='DuckDB engine only: side parquet keyed by `path` (`.` = root; optional `bucket`), e.g. `disk-tree access state` output, joined onto every row by exact path for `--max-col`')
@option('-b', '--bucket', 'buckets', multiple=True, help='Bucket to import as one scan; repeatable. Default: every distinct bucket in the listings')
@option('-d', '--db', 'db_path', default=None, help='DuckDB engine only: run the cascade in a file-backed database (a `.duckdb` path, kept; or a directory to create a temporary one in). Inert: every cascade table is TEMP and spills to `--temp-dir` under `--memory-limit` from an in-memory database too. Default: in-memory.')
@option('-G', '--groups', is_flag=True, help='Tiers: also write each tier\'s group manifest `<tier>.groups.json` beside it — the footer precomputed as compact JSON (schema + per-row-group stats/offsets) for a serverless range reader (spec mgu-engine-audit-2026-09-07.md §4)')
@option('-H', '--size-hist', is_flag=True, help='DuckDB engine only: emit `size_hist_n` / `size_hist_bytes` — per path, a log2 histogram (41 bins) of descendant files by size, counts and bytes per bin')
@option('-i', '--tiers', is_flag=False, flag_value='path,bysize', default=None, help='Also cut the path store\'s sorts from the layer-2 (spec path-store.md §4.1) under `--tiers-dir` as `<scheme>-<bucket>.<tier>.parquet`: `path` (every row, `(depth, path)`) and `bysize` (every row, `(⌊log2 size⌋ desc, path)`), 8K-row groups, `tier`/`sort` in the parquet metadata. Bare `-i` = both; `-i path` / `-i bysize` for one. duckdb/stream engines only.')
@option('-j', '--jobs', default=1, help='Stream engine only: partition the keyspace into N ranges streamed by parallel worker processes (0 = all cores). Output is byte-identical for any value.')
@option('-L', '--label', default=None, help='DuckDB engine only: attribution label parquet (`prefix` + label columns). Every row is labeled by its deepest matching prefix and the labels become extra group keys — one output row per (path, labels); rows under no prefix get NULLs. Default: none (one row per path).')
@option('-c', '--label-cols', default=None, help='Comma-separated label columns to carry from `--label` (default: every column but `prefix`)')
@option('-k', '--partition-depth', default=0, help='DuckDB engine only: cascade each distinct depth-K *directory* prefix separately (peak memory ∝ the largest cascade, not the listing); rows at depth ≤ K go to the top cascade; 0 = one cascade. Output is byte-identical for any value.')
@option('-P', '--partition-files', default=None, type=int, help=f'DuckDB engine only, with `-k`: pack partitions (in key order) into cascades of up to N files — the memory knob (~4.4 KB/file with every extension on); a key over N is split into its sub-directories, recursively, until it fits or is a flat dir of N+ files; 0 = one cascade per key, no splitting. Default {DEFAULT_PARTITION_FILES:,}')
@option('-n', '--threads', default=8, help='DuckDB engine only: DuckDB `threads` (default 8: fewer → fewer concurrent per-operator buffers; on a big node, more → a faster final sort + parquet write, the largest statement at scale)')
@option('-M', '--memory-limit', default='8GB', help='DuckDB memory cap (duckdb engine only). Excess spills to `--temp-dir`. With `-k`, peak accounted memory is ∝ the largest cascade (the final sort runs per cascade range too), so this can sit well under the node\'s RAM; process RSS runs some tens of percent over it.')
@option('-o', '--out-dir', default=None, help="Aggregate into `<DIR>/<scheme>-<bucket>.parquet` instead of a fresh temp file. Stream engine: makes the `<out>.parts` resume token reachable across invocations, so a run that died in the finalize resumes at the merge instead of re-streaming.")
@option('-m', '--mean-mtime', is_flag=True, help='Emit `mtime_mean` (size-weighted mean mtime over descendant files) per path')
@option('-O', '--tiers-dir', default=None, help='Where `--tiers` go (default: `--out-dir`)')
@option('-p', '--pivot-sum', 'pivot_sums', multiple=True, help='Emit per-value byte-sum columns `sum_<col>_<v>` for this layer-1 column (e.g. storage_class_id); repeatable')
@option('-r', '--row-group-rows', default=8192, help='Tiers: max rows per parquet row group (the HTTP range-read unit)')
@option('-S', '--sort-variant', 'sort_variants', multiple=True, help='Tiers: extra sorted copies of each tier led by these comma-separated columns (e.g. `usr` → `(usr, depth, path)`, file `…path-by-usr.parquet`); repeatable')
@option('-s', '--scheme', default='gcs', help='URI scheme for the scan root (gcs / s3 / r2)')
@option('-T', '--temp-dir', default=None, help='DuckDB spill directory (duckdb engine only; the stream engine is sort-free). Default: fresh per-invocation temp dir (safe under concurrent imports).')
@option('-t', '--time', 'time_str', default=None, help='Snapshot time (ISO 8601) recorded on each Scan; default: now')
@option('-w', '--to', default=None, help='Write the scan blob(s) to a dir or fsspec URL (`r2://bucket/prefix`) instead of the configured write dir — same as `index --to`')
@option('-x', '--max-temp-size', default=None, help="DuckDB `max_temp_directory_size` (duckdb engine only; e.g. `500GiB`). Default: DuckDB's auto-cap = free disk at launch, a stale snapshot under concurrent writers.")
def import_cmd(
    max_cols: tuple[str, ...],
    side: str | None,
    engine: str,
    listings: tuple[str, ...],
    buckets: tuple[str, ...],
    db_path: str | None,
    groups: bool,
    size_hist: bool,
    tiers: str | None,
    jobs: int,
    label: str | None,
    label_cols: str | None,
    partition_depth: int,
    partition_files: int | None,
    threads: int,
    memory_limit: str,
    mean_mtime: bool,
    out_dir: str | None,
    tiers_dir: str | None,
    pivot_sums: tuple[str, ...],
    row_group_rows: int,
    sort_variants: tuple[str, ...],
    scheme: str,
    temp_dir: str | None,
    time_str: str | None,
    to: str | None,
    max_temp_size: str | None,
):
    """Import one or more buckets from listing parquet(s) as canonical scans."""
    import duckdb
    from disk_tree.find.import_listing import list_buckets
    from disk_tree.sqla.db import init
    from disk_tree.storage import get_backend

    if to:
        from disk_tree import config as _config
        err(f"--to: writing blobs to {_config.set_write_target(to)}")
    db = init()
    db.create_all()

    snap_time = parse_dt(time_str) if time_str else datetime.now().astimezone()
    if snap_time.tzinfo is None:
        snap_time = snap_time.replace(tzinfo=timezone.utc)

    con = duckdb.connect()
    if not buckets:
        buckets = tuple(list_buckets(listings, con=con))
        err(f"discovered {len(buckets)} bucket(s): {', '.join(buckets)}")

    tier_opts = None
    if tiers:
        from disk_tree.find.tiers import parse_tiers
        if not (tiers_dir or out_dir):
            raise ValueError("--tiers needs --tiers-dir (or --out-dir)")
        tier_opts = TierOpts(
            tiers=parse_tiers(tiers), out_dir=tiers_dir or out_dir, row_group_rows=row_group_rows,
            sort_variants=tuple(tuple(c for c in v.split(',') if c) for v in sort_variants),
            groups=groups,
        )
    elif groups:
        raise ValueError("--groups needs --tiers")

    storage = get_backend()
    for bucket in buckets:
        err(f"importing {bucket} (engine={engine})…")
        import_bucket(
            db=db, storage=storage, con=con,
            engine=engine, listings=listings, bucket=bucket, scheme=scheme,
            snap_time=snap_time, memory_limit=memory_limit, temp_dir=temp_dir,
            max_temp_size=max_temp_size, jobs=jobs, threads=threads, out_dir=out_dir,
            pivot_sums=pivot_sums, mean_mtime=mean_mtime,
            duckdb_path=db_path, partition_depth=partition_depth,
            partition_files=DEFAULT_PARTITION_FILES if partition_files is None else partition_files,
            label=label, label_cols=tuple(c for c in (label_cols or '').split(',') if c),
            tier_opts=tier_opts, side=side, max_cols=max_cols, size_hist=size_hist,
        )


@dataclass(frozen=True)
class TierOpts:
    """`--tiers` and friends, resolved (see `find/tiers.py`)."""
    tiers: tuple[str, ...]
    out_dir: str
    row_group_rows: int = 8192
    sort_variants: tuple[tuple[str, ...], ...] = ()
    #: Write `<tier>.groups.json` beside each tier (`find/groups.py`).
    groups: bool = False


def import_bucket(
    db,
    storage,
    con,
    engine: str,
    listings: tuple[str, ...],
    bucket: str,
    scheme: str,
    snap_time: datetime,
    memory_limit: str = '8GB',
    temp_dir: str | None = None,
    max_temp_size: str | None = None,
    jobs: int = 1,
    threads: int = 8,
    out_dir: str | None = None,
    pivot_sums: tuple[str, ...] = (),
    mean_mtime: bool = False,
    duckdb_path: str | None = None,
    partition_depth: int = 0,
    partition_files: int = DEFAULT_PARTITION_FILES,
    label: str | None = None,
    label_cols: tuple[str, ...] = (),
    tier_opts: TierOpts | None = None,
    side: str | None = None,
    max_cols: tuple[str, ...] = (),
    size_hist: bool = False,
    replace=None,
):
    """Aggregate one bucket's listing → blob + Scan row.

    `replace`: an existing Scan row to update in place (same path+time)
    instead of inserting a new one — used by `disk-tree pull --force`.
    `out_dir`: aggregate into a deterministic `<out_dir>/<scheme>-<bucket>.parquet`
    rather than a fresh temp name, so the stream engine's `<out>.parts` resume
    token is findable on a rerun (a per-invocation temp name never is).
    `duckdb_path` / `partition_depth` / `partition_files` / `threads`: the
    duckdb engine's fleet-scale knobs (file-backed cascade database — inert;
    per-prefix partitioned cascade; DuckDB thread count).
    `label` / `label_cols`: attribution slices as extra group keys (duckdb only).
    `tier_opts`: also cut the finished blob into index tiers (duckdb/stream).
    `side` / `max_cols`: subtree-MAX columns from a path-keyed side table (duckdb only).
    `size_hist`: per-path log2 size histogram columns (duckdb only).
    Returns the Scan.
    """
    from disk_tree.sqla.model import Scan

    from disk_tree.backends.url import canonical
    if label and engine != 'duckdb':
        raise ValueError(f"--label is a duckdb-engine feature; got engine={engine!r}")
    if (side or max_cols) and engine != 'duckdb':
        raise ValueError(f"--side/--max-col is a duckdb-engine feature; got engine={engine!r}")
    if size_hist and engine != 'duckdb':
        raise ValueError(f"--size-hist is a duckdb-engine feature; got engine={engine!r}")
    if tier_opts is not None and engine == 'pandas':
        raise ValueError("--tiers needs a blob on disk: use the duckdb or stream engine")
    # A `file` root collapses to the bare path, so a reduced capture's
    # `Scan.path` is byte-identical to what `index` records for the same dir.
    scan_path = canonical(f'{scheme}://{bucket}')

    if engine == 'pandas':
        from disk_tree.find.import_listing import import_listing
        df = import_listing(
            listings, bucket=bucket, scheme=scheme, con=con,
            pivot_sums=pivot_sums, mean_mtime=mean_mtime,
        ).df
        blob_ref = storage.save(df, scan_path)
        root_size = _root_stat(df, 'size')
        root_n_children = _root_stat(df, 'n_children')
        root_n_desc = _root_stat(df, 'n_desc')
        root_mtime = _root_stat(df, 'mtime')
        n_rows = len(df)
    else:
        # Aggregate straight to a parquet in a temp location, then have the storage
        # backend adopt it — mirrors what a `save-from-file` API would do if we had one.
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            out_parquet = os.path.join(out_dir, f'{scheme}-{bucket}.parquet')
        else:
            with tempfile.NamedTemporaryFile(suffix='.parquet', delete=False) as fh:
                out_parquet = fh.name
        try:
            if engine == 'duckdb':
                from disk_tree.find.aggregate_duckdb import aggregate_listing_to_parquet
                from disk_tree.listing import prepare_listing
                src = prepare_listing(con, listings)
                stats = aggregate_listing_to_parquet(
                    src, bucket=bucket, scheme=scheme, out_parquet=out_parquet,
                    con=con, memory_limit=memory_limit, temp_dir=temp_dir,
                    max_temp_size=max_temp_size, threads=threads,
                    pivot_sums=pivot_sums, mean_mtime=mean_mtime,
                    db=duckdb_path, partition_depth=partition_depth, partition_files=partition_files,
                    label=label, label_cols=label_cols,
                    side=side, max_cols=max_cols, size_hist=size_hist,
                )
            else:  # stream
                from disk_tree.find.aggregate_stream import aggregate_stream
                stats = aggregate_stream(
                    listings, bucket=bucket, scheme=scheme, out_parquet=out_parquet,
                    con=con, memory_limit=memory_limit, temp_dir=temp_dir,
                    max_temp_size=max_temp_size, jobs=jobs,
                    pivot_sums=pivot_sums, mean_mtime=mean_mtime,
                )
            if tier_opts is not None:
                from disk_tree.find.tiers import write_tiers
                os.makedirs(tier_opts.out_dir, exist_ok=True)
                written = write_tiers(
                    out_parquet, stem=os.path.join(tier_opts.out_dir, f'{scheme}-{bucket}'),
                    tiers=tier_opts.tiers, row_group_rows=tier_opts.row_group_rows, sort_variants=tier_opts.sort_variants,
                    con=con, groups=tier_opts.groups,
                )
                for path, n in written.items():
                    err(f"  tier {os.path.basename(path)}: {n:,} rows" + (" (+ groups manifest)" if tier_opts.groups else ""))
            # Hand the file itself to the storage backend — reading a
            # 92.7M-object bucket's layer-2 (185M rows) back into pandas
            # here OOM-killed a 64GB node after the aggregation had
            # already succeeded.
            blob_ref = storage.adopt_parquet(out_parquet, scan_path)
            root_size = stats['root_size']
            root_n_children = stats['root_n_children']
            root_n_desc = stats['root_n_desc']
            root_mtime = stats['root_mtime']
            n_rows = stats['rows']
        finally:
            if os.path.exists(out_parquet):
                os.remove(out_parquet)

    if replace is not None:
        scan = replace
        scan.blob = blob_ref
        scan.size = root_size
        scan.n_children = root_n_children
        scan.n_desc = root_n_desc
        scan.mtime = root_mtime
    else:
        scan = Scan(
            path=scan_path,
            time=snap_time,
            blob=blob_ref,
            error_count=None,
            error_paths=None,
            size=root_size,
            n_children=root_n_children,
            n_desc=root_n_desc,
            mtime=root_mtime,
        )
        db.session.add(scan)
    db.session.commit()
    err(f"  {scan_path}: {n_rows:,} rows @ {snap_time.isoformat()} → {blob_ref}")
    return scan


def _root_stat(df, col: str) -> int | None:
    root = df[df['parent'] == '']
    if root.empty:
        return None
    v = root.iloc[0][col]
    return int(v) if v is not None else None
