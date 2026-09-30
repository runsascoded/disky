"""Tests for `disk-tree fetch` / `pull` / `sync` (config-driven refresh).

Config parsing / selection are tested in-process (no db); command flows run
in subprocesses against a temp `DISK_TREE_ROOT` with pre-populated dated
listing dirs (a complete `_SUCCESS.json` makes fetch a no-op, so no cloud
SDK / credentials are touched).
"""

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
from disk_tree.blobfs import read_parquet as read_listing
import pytest

from disk_tree.cli.sync import BucketCfg, load_config, select_buckets, SyncCfg

TS = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
DATE = '2026-08-15'


# ---------- config parsing (in-process) ----------

def _write_cfg(path: Path, text: str) -> str:
    path.write_text(text)
    return str(path)


def test_load_config_full(tmp_path: Path):
    cfg = load_config(_write_cfg(tmp_path / 'buckets.yml', """\
listings: /data/listings
defaults:
  procs: 4
  engine: duckdb
buckets:
  - s3://plain
  - uri: r2://mine
    endpoint_url: https://acct.r2.cloudflarestorage.com
    engine: stream
    pivot_sums: [storage_class_id]
    mean_mtime: true
"""))
    assert cfg.listings == '/data/listings'
    assert [(b.uri, b.host, b.scheme) for b in cfg.buckets] == [
        ('s3://plain', 'plain', 's3'),
        ('r2://mine', 'mine', 'r2'),
    ]
    plain, mine = cfg.buckets
    assert (plain.procs, plain.engine) == (4, 'duckdb')  # defaults applied
    assert (mine.engine, mine.pivot_sums, mine.mean_mtime) == ('stream', ('storage_class_id',), True)
    assert mine.endpoint_url == 'https://acct.r2.cloudflarestorage.com'


def test_load_config_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match='no config at'):
        load_config(str(tmp_path / 'nope.yml'))


@pytest.mark.parametrize('text,match', [
    ("buckets: []\n", r'`buckets:` list is empty'),
    ("bukets:\n  - s3://b\n", r"unknown top-level key\(s\) \['bukets'\]"),
    ("buckets:\n  - uri: s3://b\n    engin: stream\n", r"unknown key\(s\) \['engin'\]"),
    ("defaults:\n  uri: s3://x\nbuckets:\n  - s3://b\n", r"unknown `defaults` key\(s\) \['uri'\]"),
    ("buckets:\n  - uri: s3://b\n    engine: fast\n", r"engine must be one of"),
    ("buckets:\n  - /local/path\n", r'must be cloud URIs'),
])
def test_load_config_schema_errors(tmp_path: Path, text: str, match: str):
    with pytest.raises((ValueError, FileNotFoundError), match=match):
        load_config(_write_cfg(tmp_path / 'buckets.yml', text))


def test_select_buckets():
    cfg = SyncCfg(listings='/l', buckets=[BucketCfg(uri='s3://a'), BucketCfg(uri='r2://b')])
    assert [b.uri for b in select_buckets(cfg, ())] == ['s3://a', 'r2://b']
    assert [b.uri for b in select_buckets(cfg, ('b',))] == ['r2://b']
    assert [b.uri for b in select_buckets(cfg, ('r2://b', 'a'))] == ['r2://b', 's3://a']
    assert [b.uri for b in select_buckets(cfg, ('a', 's3://a'))] == ['s3://a']  # de-duped
    with pytest.raises(ValueError, match=r"unknown bucket\(s\) \['nope'\]"):
        select_buckets(cfg, ('nope',))


# ---------- command flows (subprocess, pre-populated listings) ----------

def _seed_listing(root: Path, bucket: str, rows: list[tuple[str, int]], date: str = DATE) -> Path:
    d = root / 'listings' / date / bucket
    d.mkdir(parents=True)
    pd.DataFrame({
        'bucket': [bucket] * len(rows),
        'name': [n for n, _ in sorted(rows)],
        'size_bytes': [s for _, s in sorted(rows)],
        'created': [TS] * len(rows),
        'storage_class_id': [1] * len(rows),
    }).to_parquet(d / 'shard-000.parquet')
    (d / '_SUCCESS.json').write_text(json.dumps({'total': len(rows)}))
    return d


def _dt(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, 'DISK_TREE_ROOT': str(root)}
    return subprocess.run(
        [sys.executable, '-m', 'disk_tree.cli.main', *args],
        env=env, capture_output=True, text=True, check=False,
    )


def _scans(root: Path) -> list[tuple]:
    conn = sqlite3.connect(root / 'disk-tree.db')
    rows = conn.execute("SELECT path, time, size, n_desc, n_children FROM scan ORDER BY path").fetchall()
    conn.close()
    return rows


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / 'dt-root'
    root.mkdir()
    (root / 'buckets.yml').write_text("""\
buckets:
  - s3://b1
""")
    return root


def test_pull_imports_and_is_idempotent(root: Path):
    _seed_listing(root, 'b1', [('a.txt', 100), ('sub/b.txt', 200)])

    r = _dt(root, 'pull', '-d', DATE)
    assert r.returncode == 0, r.stderr
    assert _scans(root) == [('s3://b1', f'{DATE} 00:00:00.000000', 300, 4, 2)]

    # Second run: fetch skips (complete listing) + import skips (scan exists).
    r2 = _dt(root, 'pull', '-d', DATE)
    assert r2.returncode == 0, r2.stderr
    assert f'listing {DATE} already complete' in r2.stderr
    assert f'scan for {DATE} already imported (id=1' in r2.stderr
    assert _scans(root) == [('s3://b1', f'{DATE} 00:00:00.000000', 300, 4, 2)]


def test_pull_force_replaces_scan_row(root: Path):
    d = _seed_listing(root, 'b1', [('a.txt', 100)])
    assert _dt(root, 'pull', '-d', DATE).returncode == 0
    assert _scans(root) == [('s3://b1', f'{DATE} 00:00:00.000000', 100, 2, 1)]

    # Grow the listing in place (as `fetch -f` would), then force re-import:
    # same (path, time) row updated, not duplicated.
    pd.DataFrame({
        'bucket': ['b1'] * 2,
        'name': ['a.txt', 'b.txt'],
        'size_bytes': [100, 900],
        'created': [TS] * 2,
        'storage_class_id': [1, 1],
    }).to_parquet(d / 'shard-000.parquet')
    r = _dt(root, 'pull', '-d', DATE, '-f')
    assert r.returncode == 0, r.stderr
    assert _scans(root) == [('s3://b1', f'{DATE} 00:00:00.000000', 1000, 3, 2)]


def test_fetch_skips_complete_listing(root: Path):
    _seed_listing(root, 'b1', [('a.txt', 1)])
    r = _dt(root, 'fetch', '-d', DATE)
    assert r.returncode == 0, r.stderr
    assert f'listing {DATE} already complete' in r.stderr
    assert not (root / 'disk-tree.db').exists()  # fetch never touches the db


def test_sync_all_buckets_with_extensions(tmp_path: Path):
    root = tmp_path / 'dt-root'
    root.mkdir()
    (root / 'buckets.yml').write_text("""\
defaults:
  engine: stream
buckets:
  - s3://b1
  - uri: r2://b2
    endpoint_url: https://acct.r2.cloudflarestorage.com
    pivot_sums: [storage_class_id]
    mean_mtime: true
""")
    _seed_listing(root, 'b1', [('x.bin', 10)])
    _seed_listing(root, 'b2', [('y.bin', 20), ('z/w.bin', 30)])

    r = _dt(root, 'sync', '-d', DATE)
    assert r.returncode == 0, r.stderr
    assert _scans(root) == [
        ('r2://b2', f'{DATE} 00:00:00.000000', 50, 4, 2),
        ('s3://b1', f'{DATE} 00:00:00.000000', 10, 2, 1),
    ]

    # b2's blob parquet carries the extension columns; b1's doesn't.
    conn = sqlite3.connect(root / 'disk-tree.db')
    blobs = dict(conn.execute("SELECT path, blob FROM scan").fetchall())
    conn.close()
    ext_cols = {'sum_storage_class_id_1', 'mtime_mean'}
    b2_cols = set(read_listing(root / 'scans' / blobs['r2://b2']).columns)
    b1_cols = set(read_listing(root / 'scans' / blobs['s3://b1']).columns)
    assert ext_cols <= b2_cols
    assert ext_cols & b1_cols == set()


def test_pull_named_bucket_and_unknown_errors(root: Path):
    _seed_listing(root, 'b1', [('a.txt', 1)])
    assert _dt(root, 'pull', '-d', DATE, 'b1').returncode == 0
    r = _dt(root, 'pull', '-d', DATE, 'nope')
    assert r.returncode != 0
    assert "unknown bucket(s) ['nope']" in r.stderr


def test_missing_config_errors(tmp_path: Path):
    root = tmp_path / 'dt-root'
    root.mkdir()
    r = _dt(root, 'sync')
    assert r.returncode != 0
    assert 'no config at' in r.stderr
