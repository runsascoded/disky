"""`disk-tree capture` → `disk-tree reduce`: the split scan pipeline (spec
`cloud-reduce.md`). End to end through the CLI, offline (`file://` stands in
for the object store), each process under an isolated `DISK_TREE_ROOT`.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from disk_tree.blobfs import read_parquet as read_listing
import pytest
from pandas.testing import assert_frame_equal

from disk_tree import blobfs, config
from disk_tree.cli.capture import MARKER

pytest.importorskip('fsspec')

HOST = socket.gethostname()


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


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    d = tmp_path / 'src'
    (d / 'sub' / 'deep').mkdir(parents=True)
    (d / 'empty').mkdir()
    (d / 'a.txt').write_bytes(b'a' * 5000)
    (d / 'sub' / 'b.txt').write_bytes(b'b' * 100)
    (d / 'sub' / 'deep' / 'c.txt').write_bytes(b'c')
    return d


def _blocks(p: Path) -> int:
    return os.stat(p).st_blocks * 512


def _expected_listing(tree: Path) -> pd.DataFrame:
    """The layer-1 rows `capture` must write for `tree`: files only, sorted."""
    rows = [('a.txt', tree / 'a.txt'), ('sub/b.txt', tree / 'sub' / 'b.txt'), ('sub/deep/c.txt', tree / 'sub' / 'deep' / 'c.txt')]
    return pd.DataFrame({
        'bucket': str(tree),
        'name': [n for n, _ in rows],
        'size_bytes': pd.array([_blocks(p) for _, p in rows], dtype='int64'),
        'created': pd.to_datetime([int(os.stat(p).st_mtime) for _, p in rows], unit='s', utc=True).astype('datetime64[ms, UTC]'),
        'storage_class_id': pd.array([0, 0, 0], dtype='int64'),
    })


def _only_capture(to: Path) -> Path:
    caps = list(to.glob(f'*/*/*/{MARKER}'))
    assert len(caps) == 1
    return caps[0].parent


def _listing(cap: Path) -> pd.DataFrame:
    shards = sorted(cap.glob('shard-*.parquet'))
    return pd.concat([read_listing(s) for s in shards]).sort_values('name').reset_index(drop=True)


def _scans(root: Path) -> list[dict]:
    r = _ok(_run(['scans', 'list'], root))
    return [json.loads(l) for l in r.stdout.split('\n') if l]


def _layer2(root: Path, blob: str) -> pd.DataFrame:
    return read_listing(root / 'scans' / blob).sort_values('path').reset_index(drop=True)


def test_capture_writes_files_only_shards_and_a_manifest(tree: Path, tmp_path: Path):
    root, to = tmp_path / 'root', tmp_path / 'cap'
    r = _ok(_run(['capture', '-q', '-t', str(to), str(tree)], root))
    cap = _only_capture(to)
    assert r.stdout.rstrip('\n') == str(cap)
    assert r.stderr.rstrip('\n').split('\n')[-1] == f'{tree}: 3 files in 1 shard(s) → {cap}'
    # `<to>/<host>/<root slug>/<stamp>`
    assert cap.parent.parent.name == HOST
    assert cap.parent.name == str(tree).strip('/').replace('/', '__')
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z', cap.name)
    assert sorted(p.name for p in cap.iterdir()) == [MARKER, 'shard-00000.parquet']
    # Directories (including `empty/`) are not rows; the engines imply them.
    assert_frame_equal(_listing(cap), _expected_listing(tree))
    m = json.loads((cap / MARKER).read_text())
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+\+00:00', m['time'])
    assert m == {
        'format': 'disk-tree-capture', 'version': 1, 'scheme': 'file',
        'root': str(tree), 'host': HOST, 'time': m['time'],
        'n_rows': 3, 'n_shards': 1, 'error_count': 0, 'error_paths': [],
    }


def test_capture_batches_into_multiple_shards(tree: Path, tmp_path: Path):
    root, to = tmp_path / 'root', tmp_path / 'cap'
    _ok(_run(['capture', '-q', '-n', '2', '-t', str(to), str(tree)], root))
    cap = _only_capture(to)
    assert sorted(p.name for p in cap.iterdir()) == [MARKER, 'shard-00000.parquet', 'shard-00001.parquet']
    assert_frame_equal(_listing(cap), _expected_listing(tree))
    assert json.loads((cap / MARKER).read_text())['n_shards'] == 2


def test_capture_to_a_url_target(tree: Path, tmp_path: Path):
    root, to = tmp_path / 'root', f'file://{tmp_path / "cap"}'
    r = _ok(_run(['capture', '-q', '-t', to, str(tree)], root))
    cap = r.stdout.rstrip('\n')
    assert cap.startswith(f'{to}/{HOST}/')
    assert blobfs.list_parquets(cap) == ['shard-00000.parquet']
    assert blobfs.exists(blobfs.join(cap, MARKER)) is True


@pytest.mark.parametrize('engine', ['pandas', 'duckdb'])
def test_reduce_reproduces_index(tree: Path, tmp_path: Path, engine: str):
    """capture → reduce yields the same scan `index` does, minus the empty dir."""
    idx_root, red_root, to = tmp_path / 'idx', tmp_path / 'red', tmp_path / 'cap'
    _ok(_run(['index', '-C', '-D', '-q', str(tree)], idx_root))
    cap = _ok(_run(['capture', '-q', '-t', str(to), str(tree)], red_root)).stdout.rstrip('\n')
    r = _ok(_run(['reduce', '-D', '-e', engine, cap], red_root))
    (idx,), (red,) = _scans(idx_root), _scans(red_root)
    assert r.stdout.rstrip('\n') == f'scan 1: {tree} → {red_root / "scans" / red["blob"]}'
    assert red['path'] == idx['path'] == str(tree)
    # `Scan.time` is the capture's instant as local wall-clock time (naive, the
    # convention `index` rows follow); `scans list` renders it at second precision.
    m = json.loads((Path(cap) / MARKER).read_text())
    assert datetime.fromisoformat(red['time']) == datetime.fromisoformat(m['time']).astimezone().replace(tzinfo=None, microsecond=0)
    # The empty dir is invisible to a listing: it is gone, and its parent (the
    # root) has one child / one descendant fewer. A listing has no directory
    # rows either, so a directory's *own* blocks (0 on APFS, 4 KiB on ext4)
    # are absent from it and every ancestor. Everything else is identical.
    own = _dir_own_bytes(tree)
    assert (red['size'], red['n_children'], red['n_desc']) == (idx['size'] - sum(own.values()), idx['n_children'] - 1, idx['n_desc'] - 1)

    a, b = _layer2(idx_root, idx['blob']), _layer2(red_root, red['blob'])
    cols = ['path', 'kind', 'parent', 'uri', 'size', 'n_desc', 'n_children', 'depth']
    expected = a[a['path'] != 'empty'][cols].reset_index(drop=True)
    root = expected['path'] == '.'
    expected.loc[root, ['n_children', 'n_desc']] -= 1
    for i, row in expected[expected['kind'] == 'dir'].iterrows():
        p = row['path']
        expected.loc[i, 'size'] -= sum(v for d, v in own.items() if p == '.' or d == p or d.startswith(p + '/'))
    assert_frame_equal(b[cols], expected)


def _dir_own_bytes(tree: Path) -> dict[str, int]:
    """Blocks allocated to each directory *itself* under `tree`, keyed by
    layer-2 path (`.` for the root)."""
    return {
        ('.' if d == tree else str(d.relative_to(tree))): _blocks(d)
        for d in [tree, *tree.rglob('*')] if d.is_dir()
    }


def test_reduce_from_a_url_capture_writes_a_remote_blob(tree: Path, tmp_path: Path):
    root = tmp_path / 'root'
    to, blobs = f'file://{tmp_path / "cap"}', f'file://{tmp_path / "blobs"}'
    cap = _ok(_run(['capture', '-q', '-t', to, str(tree)], root)).stdout.rstrip('\n')
    r = _ok(_run(['reduce', '-D', '-e', 'pandas', '-t', blobs, cap], root))
    lines = r.stderr.rstrip('\n').split('\n')
    assert lines[0] == f'--to: writing blobs to {blobs}'
    assert lines[1] == f'{cap}: fetched 1 shard(s) → ' + lines[1].rsplit(' → ', 1)[1]
    (scan,) = _scans(root)
    assert sorted(p.name for p in (tmp_path / 'blobs').glob('*.parquet')) == [scan['blob']]
    assert not (root / 'scans').exists()


def test_reduce_builds_the_diff_index_against_the_previous_scan(tree: Path, tmp_path: Path):
    root, to = tmp_path / 'root', tmp_path / 'cap'
    for _ in range(2):
        cap = _ok(_run(['capture', '-q', '-t', str(to), str(tree)], root)).stdout.rstrip('\n')
        _ok(_run(['reduce', '-e', 'pandas', cap], root))
    assert sorted(p.name for p in (root / 'diffs').glob('*.parquet')) == ['1-2.parquet']
