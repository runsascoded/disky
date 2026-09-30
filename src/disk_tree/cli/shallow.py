"""`disk-tree shallow` — build the shallow sidecar (`<blob-stem>.shallow.parquet`:
every chunk's depth-1 rows beside a chunked scan's root blob) for scans saved
before `index` wrote it at save time (spec `scan-page-r2-latency.md`). With it
a `/api/scan` page load at the scan root never opens a chunk blob."""

from __future__ import annotations

import sqlite3

from click import argument, option
from utz import err

from disk_tree.cli.base import cli
from disk_tree.config import SQLITE_PATH


@cli.command('shallow')
@option('-a', '--all', 'all_scans', is_flag=True, help='Every scan in the DB (default: the freshest scan covering URI)')
@option('-f', '--force', is_flag=True, help='Rebuild even if a sidecar exists')
@option('-s', '--scan-id', default=None, help='A specific scan id (default: freshest covering URI)')
@argument('uri', required=False)
def shallow_cmd(all_scans: bool, force: bool, scan_id: str | None, uri: str | None):
    """Build the shallow sidecar for the scan covering URI (or every scan, -a)."""
    from disk_tree import blobfs
    from disk_tree.diff import resolve_blob
    from disk_tree.registry import freshest_scan_covering
    from disk_tree.shallow import build_shallow

    con = sqlite3.connect(SQLITE_PATH)
    con.row_factory = sqlite3.Row
    if all_scans:
        scans = [dict(r) for r in con.execute('SELECT * FROM scan ORDER BY time DESC')]
    else:
        if uri:
            scan = freshest_scan_covering(con, uri.rstrip('/') or '/', scan_id)
            if not scan:
                raise SystemExit(f"no scan covering {uri!r}")
        elif scan_id:
            row = con.execute('SELECT * FROM scan WHERE id = ?', (scan_id,)).fetchone()
            if not row:
                raise SystemExit(f"no scan {scan_id}")
            scan = dict(row)
        else:
            raise SystemExit('give a URI, -s SCAN_ID, or -a')
        scans = [scan]
    con.close()

    for scan in scans:
        blob = resolve_blob(scan['blob'])
        if blob.startswith(('ddb:', 'sqlite:')):
            err(f"scan {scan['id']} {scan['path']}: rows live in {blob!r}, not a parquet blob — skipped")
            continue
        if not blobfs.exists(blob):
            err(f"scan {scan['id']} {scan['path']}: blob unreachable ({blob}) — skipped")
            continue
        path = build_shallow(blob, resolve_blob, force=force)
        print(f"scan {scan['id']} {scan['path']}: {path or 'no chunks, nothing to write'}")
