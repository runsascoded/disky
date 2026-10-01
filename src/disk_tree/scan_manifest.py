"""A scan's metadata as a small JSON beside a *remote* blob — the portable
"DB row" that lets another machine (or a runner whose own SQLite is thrown
away) hand a cloud-stored scan to this laptop's DB:
`disk-tree scans register URL`. The manifest route of
`done/remote-scan-targets.md`, made concrete for `cloud-reduce.md`.

Written by `index --to <url>`. A local blob needs none:
the local DB already has the row.
"""
from __future__ import annotations

import json
from datetime import datetime

from disk_tree import blobfs

FORMAT = 'disk-tree-scan'
VERSION = 1
SUFFIX = '.scan.json'
#: The `Scan` columns a manifest carries (plus `time`, formatted).
FIELDS = ('path', 'blob', 'size', 'n_children', 'n_desc', 'mtime', 'error_count')


def manifest_path(blob_url: str) -> str:
    return blob_url + SUFFIX


def scan_manifest(scan) -> dict:
    # `Scan.time` is naive wall-clock time in the writer's zone (`index` stamps
    # `now().astimezone()`, SQLite drops the offset). The manifest crosses
    # machines — a UTC cloud runner writes, a laptop registers — so it carries
    # the offset; `register` converts back to the reader's local wall clock.
    m = {'format': FORMAT, 'version': VERSION, 'time': scan.time.astimezone().isoformat()}
    for f in FIELDS:
        m[f] = getattr(scan, f)
    # Stored as JSON text on the row; a manifest is JSON already.
    m['error_paths'] = json.loads(scan.error_paths) if scan.error_paths else None
    return m


def write_scan_manifest(scan, blob_url: str) -> str:
    path = manifest_path(blob_url)
    blobfs.write_text(path, json.dumps(scan_manifest(scan), indent=2) + '\n')
    return path


def read_scan_manifest(path: str) -> dict:
    m = json.loads(blobfs.read_text(path))
    if m.get('format') != FORMAT:
        raise ValueError(f'{path}: not a disk-tree scan manifest (format={m.get("format")!r})')
    return m


def list_scan_manifests(d: str) -> list[str]:
    """Every `*.scan.json` directly under a dir or URL, as full paths/URLs."""
    if blobfs.is_url(d):
        fs, p = blobfs.fs_for(d)
        names = sorted(x.rsplit('/', 1)[-1] for x in fs.glob(f"{p.rstrip('/')}/*{SUFFIX}"))
    else:
        import os
        from glob import glob
        names = sorted(os.path.basename(x) for x in glob(os.path.join(d, f'*{SUFFIX}')))
    return [blobfs.join(d, n) for n in names]


def register(db, m: dict):
    """Insert the `Scan` row a manifest describes. Returns `(scan, created)`;
    a row with the same path and blob already present is returned unchanged."""
    from disk_tree.sqla.model import Scan
    existing = db.session.query(Scan).filter_by(path=m['path'], blob=m['blob']).first()
    if existing is not None:
        return existing, False
    time = datetime.fromisoformat(m['time'])
    if time.tzinfo is not None:
        time = time.astimezone().replace(tzinfo=None)
    scan = Scan(
        path=m['path'],
        time=time,
        blob=m['blob'],
        error_count=m.get('error_count'),
        error_paths=json.dumps(m['error_paths']) if m.get('error_paths') is not None else None,
        size=m.get('size'),
        n_children=m.get('n_children'),
        n_desc=m.get('n_desc'),
        mtime=m.get('mtime'),
    )
    db.session.add(scan)
    db.session.commit()
    return scan, True
