"""`disk-tree capture` — the walk half of the split scan pipeline (spec
`cloud-reduce.md`), so a laptop whose disk is ~full can still be scanned:
`capture PATH --to URL` streams gfind → canonical layer-1 listing shards
straight to URL (bounded memory, zero local disk); the aggregation runs where
there is disk (m3's AWS Batch ingest: `dt-cloud path-index` over the shards).

`capture` writes exactly what the bucket listers write (`bucket, name,
size_bytes, created, storage_class_id`; `bucket` = the scan root, `name` = the
path relative to it), with the root and time in the capture's
`_SUCCESS.json`. Files only:
the engines derive directories from name prefixes, so directory rows would
read as phantom files — empty directories are therefore not captured, and no
bytes are lost (APFS directories hold 0 blocks). Symlinks are captured as
files (their own block size), as `index` does. Shards are unsorted (walk
order); the DuckDB engine handles that, on the aggregation side.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime, timezone
from os import getcwd

from click import argument, option
from utz import err

from disk_tree.cli.base import cli

FORMAT = 'disk-tree-capture'
VERSION = 1
MARKER = '_SUCCESS.json'
_CLOUD = ('s3://', 'gcs://', 'r2://')


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


@cli.command('capture')
@option('-n', '--batch-rows', default=200_000, help='Rows per shard — bounds memory: one batch is buffered at a time')
@option('-o', '--one-fs', is_flag=True, help="Don't descend into filesystems mounted below PATH. With PATH `/` on macOS: the System volume + the Data volume (via its firmlinks), once — the whole machine")
@option('-q', '--no-progress', is_flag=True, help='Suppress the tqdm progress bar')
@option('-s', '--sudo', is_flag=True, help='Run `find` as sudo')
@option('-t', '--to', required=True, help='Where the capture goes: a local dir or an fsspec URL (`r2://bucket/prefix`)')
@argument('path', required=False)
def capture_cmd(
    batch_rows: int,
    one_fs: bool,
    no_progress: bool,
    sudo: bool,
    to: str,
    path: str | None,
):
    """Stream PATH's listing to --to as layer-1 shards, using no local disk.

    Prints the capture dir (`<to>/<host>/<root>/<stamp>`).
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

    for e in backend.list(root, errors=errors, sudo=sudo, one_fs=one_fs, progress=not no_progress):
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
    if sys.platform == 'darwin':
        # The APFS container the walk sits on (volumes, snapshots, free): what
        # the walk can't attribute, so a UI can draw the whole disk. A root on a
        # non-APFS volume (ExFAT external) legitimately has none; and the field
        # is an annotation, so a `diskutil` failure is logged rather than
        # costing the walk its manifest.
        from subprocess import CalledProcessError
        from disk_tree.apfs import container_for
        try:
            manifest['container'] = container_for(root).to_json()
        except (ValueError, CalledProcessError) as e:
            err(f'{root}: no APFS container recorded: {e}')
    blobfs.write_text(blobfs.join(out, MARKER), json.dumps(manifest, indent=2) + '\n')
    tail = f', {errors.count} permission errors' if errors.count else ''
    err(f'{root}: {n_rows:,} files in {n_shards} shard(s) → {out}{tail}')
    print(out)
