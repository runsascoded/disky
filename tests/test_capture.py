"""`disk-tree capture`: the walk half of the split scan pipeline (spec
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
    # On macOS the manifest also records the APFS container the tree is on
    # (machine-dependent values: assert its shape; `test_apfs.py` covers content).
    container = m.pop('container', None)
    if sys.platform == 'darwin':
        assert sorted(container) == ['capacity', 'device', 'free', 'used', 'volumes']
        assert container['used'] == container['capacity'] - container['free']
        assert [sorted(v) for v in container['volumes']][:1] == [['device', 'mount', 'name', 'roles', 'snapshots', 'used']]
    else:
        assert container is None
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


def _fake_ssh(tmp_path: Path, log: Path) -> str:
    """A stand-in `ssh`: logs its argv, then runs the remote command (the last
    arg) here, with `find` resolving to GNU find (`gfind` on macOS)."""
    bin_dir = tmp_path / 'fake-bin'
    bin_dir.mkdir()
    gnu = subprocess.run(['sh', '-c', 'command -v gfind || command -v find'], capture_output=True, text=True, check=True).stdout.strip()
    (bin_dir / 'find').symlink_to(gnu)
    ssh = tmp_path / 'fake-ssh'
    ssh.write_text(
        '#!/bin/sh\n'
        f'printf "%s\\n" "$@" > {log}\n'
        'for last; do :; done\n'
        f'PATH={bin_dir}:$PATH exec sh -c "$last"\n'
    )
    ssh.chmod(0o755)
    return str(ssh)


def test_capture_over_ssh_runs_find_remotely_and_names_the_remote_host(tree: Path, tmp_path: Path):
    root, to, log = tmp_path / 'root', tmp_path / 'cap', tmp_path / 'ssh-argv'
    # `DISK_TREE_HOST` (pinned by the laptop's agent) must not rename a remote walk.
    r = _ok(_run(['capture', '-q', '-h', 'nas', '-t', str(to), str(tree)], root, DISK_TREE_SSH=_fake_ssh(tmp_path, log), DISK_TREE_HOST='laptop'))
    cap = _only_capture(to)
    assert r.stdout.rstrip('\n') == str(cap)
    assert cap.parent.parent.name == 'nas'
    assert log.read_text().split('\n')[:-1] == ['-o', 'BatchMode=yes', 'nas', f"find {tree} -printf '%y %b %T@ %p\\0'"]
    assert_frame_equal(_listing(cap), _expected_listing(tree))
    m = json.loads((cap / MARKER).read_text())
    assert m == {
        'format': 'disk-tree-capture', 'version': 1, 'scheme': 'file',
        'root': str(tree), 'host': 'nas', 'time': m['time'],
        'n_rows': 3, 'n_shards': 1, 'error_count': 0, 'error_paths': [],
    }


def test_capture_over_ssh_fails_without_a_marker_when_ssh_cannot_connect(tree: Path, tmp_path: Path):
    root, to = tmp_path / 'root', tmp_path / 'cap'
    ssh = tmp_path / 'down-ssh'
    ssh.write_text('#!/bin/sh\necho "ssh: connect to host nas port 22: Connection refused" >&2\nexit 255\n')
    ssh.chmod(0o755)
    r = _run(['capture', '-q', '-h', 'nas', '-t', str(to), str(tree)], root, DISK_TREE_SSH=str(ssh))
    assert r.returncode == 1
    assert r.stderr.rstrip('\n').split('\n')[-1] == f'RuntimeError: {ssh} exited 255: no listing of {tree}'
    assert list(to.glob(f'**/{MARKER}')) == []
