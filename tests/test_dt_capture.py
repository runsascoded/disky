"""`dt-capture` (the Rust `capture`, apps/tauri/crates/dt-capture) writes the
same layer-1 capture as `disk-tree capture`: identical rows per shard set, the
same shard count for a given batch size, and the same manifest (bar `time`)."""

import json
import os
import subprocess
import sys
from os.path import dirname, exists, join
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from disk_tree.cli.capture import MARKER

DT_CAPTURE = join(dirname(dirname(__file__)), 'apps', 'tauri', 'target', 'release', 'dt-capture')

pytestmark = [
    pytest.mark.skipif(sys.platform != 'darwin', reason='dt-capture walks with the macOS getattrlistbulk walker'),
    pytest.mark.skipif(not exists(DT_CAPTURE), reason='dt-capture not built (cargo build --release -p dt-capture in apps/tauri)'),
]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    t = tmp_path / 'tree'
    (t / 'sub' / 'deeper').mkdir(parents=True)
    (t / 'empty').mkdir()
    (t / 'a.txt').write_text('hello\n')
    (t / 'big.bin').write_bytes(b'\1' * 300_000)
    (t / 'sub' / 'b.txt').write_text('x\n')
    (t / 'sub' / 'deeper' / 'ünï cødé.txt').write_text('y' * 5000)
    (t / 'link').symlink_to('a.txt')
    return t


def _capture(cmd: list[str], to: Path) -> Path:
    env = {**os.environ, 'DISK_TREE_HOST': 'h'}
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, check=True)
    return Path(r.stdout.strip().split('\n')[-1])


def _rows(cap: Path) -> pd.DataFrame:
    shards = sorted(cap.glob('shard-*.parquet'))
    return pd.concat([pd.read_parquet(s) for s in shards]).sort_values('name').reset_index(drop=True)


def test_dt_capture_matches_python_capture(tree: Path, tmp_path: Path):
    py = _capture([join(dirname(sys.executable), 'disk-tree'), 'capture', '-q', '-n', '2', '-t', str(tmp_path / 'py'), str(tree)], tmp_path)
    rs = _capture([DT_CAPTURE, '-n', '2', '-t', str(tmp_path / 'rs'), str(tree)], tmp_path)
    # Same layout: <to>/<host>/<root slug>/<stamp>, same shard files.
    assert (rs.parent.parent.name, rs.parent.name) == (py.parent.parent.name, py.parent.name) == ('h', str(tree).strip('/').replace('/', '__'))
    assert sorted(p.name for p in rs.iterdir()) == sorted(p.name for p in py.iterdir()) == [MARKER, 'shard-00000.parquet', 'shard-00001.parquet', 'shard-00002.parquet']
    # Same rows (files + the symlink; no dirs), same values and dtypes.
    expected = _rows(py)
    assert expected['name'].tolist() == ['a.txt', 'big.bin', 'link', 'sub/b.txt', 'sub/deeper/ünï cødé.txt']
    pd.testing.assert_frame_equal(_rows(rs), expected)
    # Same parquet schema (names + arrow types).
    rs_schema = pq.read_schema(rs / 'shard-00000.parquet').remove_metadata()
    py_schema = pq.read_schema(py / 'shard-00000.parquet').remove_metadata()
    assert rs_schema == py_schema
    # Same manifest, bar the capture time (and the machine-dependent container,
    # whose shape test_capture.py checks; compared here key by key).
    mp, mr = json.loads((py / MARKER).read_text()), json.loads((rs / MARKER).read_text())
    cp, cr = mp.pop('container', None), mr.pop('container', None)
    assert {k: v for k, v in mr.items() if k != 'time'} == {k: v for k, v in mp.items() if k != 'time'}
    assert (cr is None) == (cp is None)
    if cp:
        assert sorted(cr) == sorted(cp)
        assert [(v['device'], v['name'], v['roles'], v['mount']) for v in cr['volumes']] == [(v['device'], v['name'], v['roles'], v['mount']) for v in cp['volumes']]
