"""`disk-tree index --to` and the low-space warn / `--auto-remote` redirect, end
to end through the CLI over a `file://` target — a real fsspec URL that survives
across processes, unlike `memory://`. Spec `remote-scan-targets.md`, Phase 1.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from utz import iec

from disk_tree import config
from disk_tree.cli.index import LOW_SPACE_VAR, REMOTE_TARGET_VAR

pytest.importorskip('fsspec')

UUID_PARQUET = r'[0-9a-f-]{36}\.parquet'
LOW = str(10**18)  # any disk is "low" against 888 PiB


def _run(args: list[str], root: Path, **env_extra: str) -> subprocess.CompletedProcess:
    env = {**os.environ, config.DISK_TREE_ROOT_VAR: str(root)}
    for k in (config.DISK_TREE_SCAN_DIRS_VAR, LOW_SPACE_VAR, REMOTE_TARGET_VAR):
        env.pop(k, None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, '-m', 'disk_tree.cli.main', *args],
        env=env, capture_output=True, text=True, check=False,
    )


def _stderr_lines(r: subprocess.CompletedProcess) -> list[str]:
    return r.stderr.rstrip('\n').split('\n')


@pytest.fixture
def src(tmp_path: Path) -> Path:
    d = tmp_path / 'src'
    (d / 'sub').mkdir(parents=True)
    (d / 'a.txt').write_text('aaaa')
    (d / 'sub' / 'b.txt').write_text('bb')
    return d


def test_to_writes_the_blob_to_the_url_target_and_reads_it_back(src: Path, tmp_path: Path):
    root, remote = tmp_path / 'root', tmp_path / 'remote'
    target = f'file://{remote}'
    r = _run(['index', '-C', '-D', '-t', target, str(src)], root)
    assert r.returncode == 0, r.stderr
    assert _stderr_lines(r)[0] == f'--to: writing blobs to {target}'

    blobs = sorted(p.name for p in remote.glob('*.parquet'))
    assert len(blobs) == 1
    assert re.fullmatch(UUID_PARQUET, blobs[0])
    assert list((root / 'scans').glob('*.parquet')) == []
    assert [l for l in r.stdout.split('\n') if l.startswith('Scan cached path: ')] == [
        f'Scan cached path: {target}/{blobs[0]} ({iec(os.path.getsize(remote / blobs[0]))})',
    ]

    # A later process finds the blob through the search path — no `--to` needed.
    r2 = _run(['scans', 'dirs'], root, **{config.DISK_TREE_SCAN_DIRS_VAR: f'{target}:{root / "scans"}'})
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout.rstrip('\n').split('\n') == [
        f'write: {target}',
        f'  * {target}  (1 blobs, remote)',
        f'    {root / "scans"}  (absent)',
    ]


def test_to_writes_a_groups_json_footer_sidecar(src: Path, tmp_path: Path):
    """`index --to <url>` precomputes the `.groups.json` footer beside the blob,
    so the serverless reader (`ui/cfn`) plans range reads without a cold
    thrift-footer parse (`find/groups.py`). Absent-safe: a reader without it
    falls back to the blob's own footer."""
    import json

    import pyarrow.parquet as pq

    from disk_tree.find.groups import GROUP_FIELDS, groups_path

    root, remote = tmp_path / 'root', tmp_path / 'remote'
    target = f'file://{remote}'
    r = _run(['index', '-C', '-D', '-t', target, str(src)], root)
    assert r.returncode == 0, r.stderr

    blob = next(remote.glob('*.parquet'))
    sidecar = Path(groups_path(str(blob)))
    assert sidecar.exists()
    doc = json.loads(sidecar.read_text())

    md = pq.read_metadata(str(blob))
    # One `groups` entry per row group, each an array in GROUP_FIELDS order; a
    # main blob carries no coarse floor; schema leaves are the blob's columns.
    assert [len(g) for g in doc['groups']] == [len(GROUP_FIELDS)] * md.num_row_groups
    assert (doc['v'], doc['floor_bytes']) == (1, None)
    assert [leaf['name'] for leaf in doc['schema'][1:]] == md.schema.names
    # The publish announced it on stdout (the URL path, matching the `--to` target).
    assert [l.split(' (')[0] for l in r.stdout.split('\n') if l.startswith('Scan groups: ')] == [
        f'Scan groups: {groups_path(f"{target}/{blob.name}")}',
    ]


def test_low_space_warns_and_suggests(src: Path, tmp_path: Path):
    root = tmp_path / 'root'
    r = _run(['index', '-C', '-D', str(src)], root, **{LOW_SPACE_VAR: LOW})
    assert r.returncode == 0, r.stderr
    first = re.sub(r'only .+? free', 'only <free> free', _stderr_lines(r)[0])
    assert first == (
        f'warning: only <free> free on {root / "scans"} (< {iec(10**18)}); '
        f'consider --to r2://<bucket>/<prefix> (or set {REMOTE_TARGET_VAR})'
    )
    assert len(list((root / 'scans').glob('*.parquet'))) == 1


def test_low_space_names_the_configured_remote(src: Path, tmp_path: Path):
    root, remote = tmp_path / 'root', f'file://{tmp_path / "remote"}'
    r = _run(['index', '-C', '-D', str(src)], root, **{LOW_SPACE_VAR: LOW, REMOTE_TARGET_VAR: remote})
    assert r.returncode == 0, r.stderr
    first = re.sub(r'only .+? free', 'only <free> free', _stderr_lines(r)[0])
    assert first == (
        f'warning: only <free> free on {root / "scans"} (< {iec(10**18)}); consider --to {remote}'
        ' — pass -R/--auto-remote to redirect automatically'
    )
    assert not (tmp_path / 'remote').exists()


def test_auto_remote_redirects_when_low(src: Path, tmp_path: Path):
    root, remote_dir = tmp_path / 'root', tmp_path / 'remote'
    remote = f'file://{remote_dir}'
    r = _run(['index', '-C', '-D', '-R', str(src)], root, **{LOW_SPACE_VAR: LOW, REMOTE_TARGET_VAR: remote})
    assert r.returncode == 0, r.stderr
    first = re.sub(r'low space: .+? free', 'low space: <free> free', _stderr_lines(r)[0])
    assert first == (
        f'low space: <free> free on {root / "scans"} (< {iec(10**18)}); --auto-remote: writing blobs to {remote}'
    )
    assert len(list(remote_dir.glob('*.parquet'))) == 1
    assert list((root / 'scans').glob('*.parquet')) == []


def test_diff_index_builds_over_remote_blobs(src: Path, tmp_path: Path):
    root, remote = tmp_path / 'root', f'file://{tmp_path / "remote"}'
    for _ in range(2):
        r = _run(['index', '-C', '-t', remote, str(src)], root)
        assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in (root / 'diffs').glob('*.parquet')) == ['1-2.parquet']
