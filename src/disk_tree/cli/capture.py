"""`disk-tree capture` / `disk-tree reduce` — the scan pipeline split in two
(spec `cloud-reduce.md`), so a laptop whose disk is ~full can still be scanned:

    capture PATH --to URL    gfind → canonical layer-1 listing shards, streamed
                             straight to URL. Bounded memory, zero local disk.
    reduce  CAPTURE [--to]   shards → layer-2 scan blob + Scan row, on any
                             machine that has disk (a CI runner, a VM, the
                             laptop once its SSD is back).

`capture` writes exactly what the bucket listers write (`bucket, name,
size_bytes, created, storage_class_id`; `bucket` = the scan root, `name` = the
path relative to it), so `reduce` is `disk-tree import` pointed at the shards,
with the root and time taken from the capture's `_SUCCESS.json`. Files only:
the engines derive directories from name prefixes, so directory rows would
read as phantom files — empty directories are therefore not captured, and no
bytes are lost (APFS directories hold 0 blocks). Symlinks are captured as
files (their own block size), as `index` does. Shards are unsorted (walk
order); the DuckDB engine handles that, on the reduce side, where the disk is.
"""
from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
from os import getcwd

from click import Choice, argument, option
from utz import err

from disk_tree.cli.base import cli

FORMAT = 'disk-tree-capture'
VERSION = 1
MARKER = '_SUCCESS.json'
_CLOUD = ('s3://', 'gcs://', 'r2://', 'ssh://')


def capture_dir(to: str, root: str, host: str, stamp: str) -> str:
    """`<to>/<host>/<root slug>/<stamp>` — one capture per (machine, root, time)."""
    from disk_tree.blobfs import join
    slug = root.strip('/').replace('/', '__') or 'root'
    return join(join(join(to, host), slug), stamp)


def _frame(root: str, names: list[str], sizes: list[int], mtimes: list[int]):
    """One shard's rows in the canonical layer-1 listing schema (vectorized —
    the listers' `entries_to_frame` parses ISO strings per row)."""
    import pandas as pd
    return pd.DataFrame({
        'bucket': root,
        'name': names,
        'size_bytes': pd.array(sizes, dtype='int64'),
        # Pinned to ms: parquet has no seconds unit, so a `[s]` frame (pandas 3's
        # result for `unit='s'`) would read back as `[ms]` — write what reads.
        'created': pd.to_datetime(mtimes, unit='s', utc=True).astype('datetime64[ms, UTC]'),
        'storage_class_id': pd.array([0] * len(names), dtype='int64'),
    })


def read_marker(capture: str) -> dict:
    """The capture's manifest (`_SUCCESS.json`), local or remote."""
    from disk_tree import blobfs
    path = blobfs.join(capture, MARKER)
    m = json.loads(blobfs.read_text(path))
    if m.get('format') != FORMAT:
        raise SystemExit(f'{path}: not a disk-tree capture (format={m.get("format")!r})')
    return m


@cli.command('capture')
@option('-n', '--batch-rows', default=200_000, help='Rows per shard — bounds memory: one batch is buffered at a time')
@option('-q', '--no-progress', is_flag=True, help='Suppress the tqdm progress bar')
@option('-s', '--sudo', is_flag=True, help='Run `find` as sudo')
@option('-t', '--to', required=True, help='Where the capture goes: a local dir or an fsspec URL (`r2://bucket/prefix`)')
@argument('path', required=False)
def capture_cmd(batch_rows: int, no_progress: bool, sudo: bool, to: str, path: str | None):
    """Stream PATH's listing to --to as layer-1 shards, using no local disk.

    Prints the capture dir (`<to>/<host>/<root>/<stamp>`), which `reduce` takes.
    """
    from disk_tree import blobfs
    from disk_tree.backends import backend_for, ErrorCollector
    from disk_tree.find.bulk import _ROW_GROUP_ROWS

    root = (path or getcwd()).rstrip('/') or '/'
    if root.startswith(_CLOUD):
        raise SystemExit('capture is for local paths; bucket listings come from `bulk-list`')
    if blobfs.is_url(to):
        blobfs.fs_for(to)  # a bad scheme / missing endpoint fails before the walk, not after
    now = datetime.now(timezone.utc)
    # `DISK_TREE_HOST` pins the capture dir's host segment: the same machine
    # reports `Mac` to a shell and `mac.lan` to a launchd job.
    host = os.environ.get('DISK_TREE_HOST') or socket.gethostname()
    out = capture_dir(to, root, host, now.strftime('%Y-%m-%dT%H-%M-%SZ'))
    if not blobfs.is_url(out):
        os.makedirs(out, exist_ok=True)
    errors = ErrorCollector()
    backend = backend_for(root)

    names: list[str] = []
    sizes: list[int] = []
    mtimes: list[int] = []
    n_rows = n_shards = 0

    def flush() -> None:
        nonlocal n_rows, n_shards, names, sizes, mtimes
        if not names:
            return
        blobfs.write_parquet(
            _frame(root, names, sizes, mtimes),
            blobfs.join(out, f'shard-{n_shards:05d}.parquet'),
            _ROW_GROUP_ROWS,
        )
        n_rows += len(names)
        n_shards += 1
        names, sizes, mtimes = [], [], []

    for e in backend.list(root, errors=errors, sudo=sudo, progress=not no_progress):
        if e['kind'] == 'dir' or e['path'] == '':
            continue
        names.append(e['path'])
        sizes.append(e['size'])
        mtimes.append(e['mtime'])
        if len(names) >= batch_rows:
            flush()
    flush()

    manifest = {
        'format': FORMAT,
        'version': VERSION,
        'scheme': 'file',
        'root': root,
        'host': host,
        'time': now.isoformat(),
        'n_rows': n_rows,
        'n_shards': n_shards,
        'error_count': errors.count,
        'error_paths': errors.paths,
    }
    blobfs.write_text(blobfs.join(out, MARKER), json.dumps(manifest, indent=2) + '\n')
    tail = f', {errors.count} permission errors' if errors.count else ''
    err(f'{root}: {n_rows:,} files in {n_shards} shard(s) → {out}{tail}')
    print(out)


@cli.command('reduce')
@option('-D', '--no-diff', is_flag=True, help="Skip building the diff index against the path's previous scan")
@option('-e', '--engine', type=Choice(['pandas', 'duckdb', 'stream']), default='duckdb', help='Aggregation engine; `duckdb` (out-of-core) handles the unsorted shards `capture` writes')
@option('-j', '--jobs', default=1, help='Stream engine only: parallel keyspace partitions (0 = all cores)')
@option('-m', '--mean-mtime', is_flag=True, help='Emit `mtime_mean` (size-weighted mean mtime) per path')
@option('-M', '--memory-limit', default='8GB', help='DuckDB memory cap; excess spills to the work dir')
@option('-T', '--temp-dir', default=None, help='Work dir for downloaded shards + spill (default: a fresh temp dir, removed after)')
@option('-t', '--to', default=None, help='Write the scan blob to a dir or fsspec URL (same as `index --to`)')
@argument('capture')
def reduce_cmd(
    no_diff: bool,
    engine: str,
    jobs: int,
    mean_mtime: bool,
    memory_limit: str,
    temp_dir: str | None,
    to: str | None,
    capture: str,
):
    """Aggregate a capture (dir or URL holding shards + `_SUCCESS.json`) into a scan."""
    import shutil
    import tempfile

    import duckdb

    from disk_tree import blobfs, config as _config
    from disk_tree.cli.import_listing import import_bucket
    from disk_tree.diff import resolve_blob
    from disk_tree.sqla.db import init
    from disk_tree.storage import get_backend

    capture = capture.rstrip('/')
    m = read_marker(capture)
    if to:
        target = _config.set_write_target(to)
        err(f'--to: writing blobs to {target}')
    db = init()
    db.create_all()
    work = temp_dir or tempfile.mkdtemp(prefix='disk-tree-reduce-')
    try:
        if blobfs.is_url(capture):
            # The engines glob local paths (DuckDB could read some URLs, but
            # only with per-scheme httpfs credentials), and this machine has
            # disk — that's why the reduce runs here. Pull the shards down.
            fs, p = blobfs.fs_for(capture)
            shards = sorted(fs.glob(f'{p}/shard-*.parquet'))
            local = os.path.join(work, 'shards')
            os.makedirs(local, exist_ok=True)
            for s in shards:
                fs.get(s, os.path.join(local, s.rsplit('/', 1)[-1]))
            err(f'{capture}: fetched {len(shards)} shard(s) → {local}')
            listing = os.path.join(local, 'shard-*.parquet')
        else:
            listing = os.path.join(capture, 'shard-*.parquet')
        spill = os.path.join(work, 'spill')
        os.makedirs(spill, exist_ok=True)
        scan = import_bucket(
            db=db, storage=get_backend(), con=duckdb.connect(),
            engine=engine, listings=(listing,),
            bucket=m['root'], scheme=m['scheme'],
            # Local wall clock, like `index` rows (SQLite drops the offset).
            snap_time=datetime.fromisoformat(m['time']).astimezone(),
            memory_limit=memory_limit, temp_dir=spill, jobs=jobs, mean_mtime=mean_mtime,
        )
        if m.get('error_count'):
            scan.error_count = m['error_count']
            scan.error_paths = json.dumps(m['error_paths'])
            db.session.commit()
    finally:
        if not temp_dir:
            shutil.rmtree(work, ignore_errors=True)
    blob = resolve_blob(scan.blob)
    if blobfs.is_url(blob):
        # This DB may be a runner's throwaway; the manifest is how the scan
        # reaches another one (`disk-tree scans register`).
        from disk_tree.scan_manifest import write_scan_manifest
        err(f'manifest → {write_scan_manifest(scan, blob)}')
        # Footer sidecar for the serverless reader (see `find/groups.py`).
        from disk_tree.find.groups import write_groups_sidecar
        gs = write_groups_sidecar(blob)
        if gs:
            err(f'groups → {gs.path} ({gs.n_groups} groups)')
    if not no_diff:
        from disk_tree.cli.diff_index import build_previous
        build_previous(scan.id)
    print(f'scan {scan.id}: {scan.path} → {blob}')
