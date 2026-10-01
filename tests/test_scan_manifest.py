"""`<blob>.scan.json` + `disk-tree scans register`: a scan indexed with `--to`
under one DB reaches another — the cross-machine hand-off of spec
`cloud-reduce.md`. Two isolated roots stand in for two machines.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from disk_tree import config
from disk_tree.scan_manifest import SUFFIX

pytest.importorskip('fsspec')

SCAN_KEYS = ['path', 'time', 'blob', 'size', 'n_children', 'n_desc', 'mtime', 'error_count', 'error_paths']


def _run(args: list[str], root: Path, **env_extra: str) -> subprocess.CompletedProcess:
    env = {**os.environ, config.DISK_TREE_ROOT_VAR: str(root)}
    env.pop(config.DISK_TREE_SCAN_DIRS_VAR, None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, '-m', 'disk_tree.cli.main', *args],
        env=env, capture_output=True, text=True, check=False,
    )


def _ok(r: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    assert r.returncode == 0, r.stderr
    return r


def _scans(root: Path) -> list[dict]:
    r = _ok(_run(['scans', 'list'], root))
    return [json.loads(l) for l in r.stdout.split('\n') if l]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    d = tmp_path / 'src'
    (d / 'sub').mkdir(parents=True)
    (d / 'a.txt').write_bytes(b'a' * 5000)
    (d / 'sub' / 'b.txt').write_bytes(b'b' * 100)
    return d


def test_index_to_url_writes_a_manifest_that_register_imports(tree: Path, tmp_path: Path):
    a, b = tmp_path / 'a', tmp_path / 'b'  # two "machines"
    blobs = f'file://{tmp_path / "blobs"}'
    _ok(_run(['index', '-C', '-q', '-t', blobs, str(tree)], a))
    (scan_a,) = _scans(a)
    manifest = tmp_path / 'blobs' / f'{scan_a["blob"]}{SUFFIX}'
    m = json.loads(manifest.read_text())
    # The row stores `time` as naive local wall clock; the manifest carries the
    # offset so another zone can register it. `scans list` renders rows at
    # second precision, hence the manifest as the full-precision source.
    full_time = datetime.fromisoformat(m['time']).astimezone().replace(tzinfo=None)
    assert m == {
        'format': 'disk-tree-scan', 'version': 1,
        'time': m['time'],
        'path': str(tree), 'blob': scan_a['blob'],
        'size': scan_a['size'], 'n_children': scan_a['n_children'], 'n_desc': scan_a['n_desc'],
        'mtime': scan_a['mtime'], 'error_count': scan_a['error_count'], 'error_paths': scan_a['error_paths'],
    }

    # Machine b: no scans yet; register from the blobs dir (not on its search path → note).
    assert _scans(b) == []
    r = _ok(_run(['scans', 'register', blobs], b))
    assert r.stdout.rstrip('\n') == '1 registered, 0 skipped'
    assert r.stderr.rstrip('\n').split('\n') == [
        f'registered scan 1: {tree} @ {full_time} → {scan_a["blob"]}',
        f'note: {blobs} is not on the blob search path — add it to {config.DISK_TREE_SCAN_DIRS_VAR} so the blobs resolve',
    ]
    (scan_b,) = _scans(b)
    assert {k: scan_b[k] for k in SCAN_KEYS} == {k: scan_a[k] for k in SCAN_KEYS}

    # Idempotent, and quiet about the search path once the dir is on it.
    r = _ok(_run(['scans', 'register', blobs], b, **{config.DISK_TREE_SCAN_DIRS_VAR: blobs}))
    assert r.stdout.rstrip('\n') == '0 registered, 1 skipped'
    assert r.stderr.rstrip('\n').split('\n') == [f'skipped scan 1: {tree} @ {full_time} → {scan_a["blob"]}']

    # …and machine b now reads the tree from the remote blob.
    r = _ok(_run(['du', '-d', '1', str(tree)], b, **{config.DISK_TREE_SCAN_DIRS_VAR: blobs}))
    first = re.sub(r' — .+? \(scan', ' — <size> (scan', r.stdout.split('\n')[0])
    assert first == f'{tree} — <size> (scan 1 of {tree}, {scan_b["time"]})'


def test_register_a_single_manifest_file(tree: Path, tmp_path: Path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    blobs = f'file://{tmp_path / "blobs"}'
    _ok(_run(['index', '-C', '-q', '-t', blobs, str(tree)], a))
    (scan_a,) = _scans(a)
    r = _ok(_run(['scans', 'register', f'{blobs}/{scan_a["blob"]}{SUFFIX}'], b, **{config.DISK_TREE_SCAN_DIRS_VAR: blobs}))
    assert r.stdout.rstrip('\n') == '1 registered, 0 skipped'
    (scan_b,) = _scans(b)
    assert {k: scan_b[k] for k in SCAN_KEYS} == {k: scan_a[k] for k in SCAN_KEYS}


def test_index_to_url_writes_a_manifest(tree: Path, tmp_path: Path):
    root, blobs = tmp_path / 'root', f'file://{tmp_path / "blobs"}'
    r = _ok(_run(['index', '-C', '-q', '-t', blobs, str(tree)], root))
    (scan,) = _scans(root)
    manifest = f'{blobs}/{scan["blob"]}{SUFFIX}'
    assert [l for l in r.stdout.split('\n') if l.startswith('Scan manifest: ')] == [f'Scan manifest: {manifest}']
    m = json.loads((tmp_path / 'blobs' / f'{scan["blob"]}{SUFFIX}').read_text())
    assert (m['format'], m['path'], m['blob'], m['size']) == ('disk-tree-scan', str(tree), scan['blob'], scan['size'])


def test_index_to_local_dir_writes_no_manifest(tree: Path, tmp_path: Path):
    root, blobs = tmp_path / 'root', tmp_path / 'blobs'
    _ok(_run(['index', '-C', '-q', '-t', str(blobs), str(tree)], root))
    assert sorted(p.suffix for p in blobs.iterdir()) == ['.parquet']
