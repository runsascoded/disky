"""Tests for `disk-tree du` — per-level top-N view of a cached scan."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

TS = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)


def _run_dt(env_root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, 'DISK_TREE_ROOT': str(env_root)}
    return subprocess.run(
        [sys.executable, '-m', 'disk_tree.cli.main', *args],
        env=env, capture_output=True, text=True, check=False,
    )


@dataclass(frozen=True)
class Row:
    size: str
    indent: int
    name: str


def parse(stdout: str) -> list[Row]:
    """Drop the header line; parse `  <size>  <date>  <indent><name>` rows.

    The date column is dropped rather than normalized — `du`'s contract is
    size/shape, and the fixture's mtime is already pinned by TS.
    """
    rows = []
    for line in stdout.rstrip('\n').split('\n')[1:]:
        # Fixed columns: `{size:>9}  {date:>10}  {'  ' * indent}{name}`.
        size, date, rest = line[:9], line[11:21], line[23:]
        assert line[9:11] == '  ' and line[21:23] == '  ', f'unparsed: {line!r}'
        indent = (len(rest) - len(rest.lstrip(' '))) // 2
        rows.append(Row(size.strip(), indent, rest.strip()))
    return rows


@pytest.fixture
def scanned(tmp_path: Path) -> Path:
    """One imported bucket: a.txt=100, sub/b.txt=200, sub/c.txt=300."""
    env_root = tmp_path / 'root'
    env_root.mkdir()
    listing = tmp_path / 'l.parquet'
    pd.DataFrame([
        {'bucket': 'b1', 'name': 'a.txt',     'size_bytes': 100, 'created': TS, 'storage_class_id': 1},
        {'bucket': 'b1', 'name': 'sub/b.txt', 'size_bytes': 200, 'created': TS, 'storage_class_id': 1},
        {'bucket': 'b1', 'name': 'sub/c.txt', 'size_bytes': 300, 'created': TS, 'storage_class_id': 1},
    ]).to_parquet(listing)
    r = _run_dt(env_root, 'import', '-l', str(listing), '-b', 'b1', '-t', TS.isoformat())
    assert r.returncode == 0, r.stderr
    return env_root


def test_dirs_only_depth_1(scanned: Path):
    r = _run_dt(scanned, 'du', 'gcs://b1')
    assert r.returncode == 0, r.stderr
    assert r.stdout.split('\n')[0].startswith('gcs://b1 — 600 Bytes ')
    assert parse(r.stdout) == [Row('500 Bytes', 0, 'sub/')]


def test_all_kinds_nested(scanned: Path):
    r = _run_dt(scanned, 'du', 'gcs://b1', '-a', '-d', '2')
    assert r.returncode == 0, r.stderr
    assert parse(r.stdout) == [
        Row('500 Bytes', 0, 'sub/'),
        Row('300 Bytes', 1, 'c.txt'),
        Row('200 Bytes', 1, 'b.txt'),
        Row('100 Bytes', 0, 'a.txt'),
    ]


def test_top_n_folds_the_rest(scanned: Path):
    r = _run_dt(scanned, 'du', 'gcs://b1', '-a', '-n', '1')
    assert r.returncode == 0, r.stderr
    assert parse(r.stdout) == [
        Row('500 Bytes', 0, 'sub/'),
        Row('100 Bytes', 0, '… 1 more'),
    ]


def test_raw_bytes_and_json(scanned: Path):
    r = _run_dt(scanned, 'du', 'gcs://b1', '-a', '-d', '2', '-H')
    assert r.returncode == 0, r.stderr
    assert [row.size for row in parse(r.stdout)] == ['500', '300', '200', '100']

    r = _run_dt(scanned, 'du', 'gcs://b1', '-a', '-d', '2', '-j')
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d['uri'] == 'gcs://b1'
    assert d['size'] == 600
    assert [(n['path'], n['size'], [(c['path'], c['size']) for c in n['children']]) for n in d['rows']] == [
        ('sub', 500, [('sub/c.txt', 300), ('sub/b.txt', 200)]),
        ('a.txt', 100, []),
    ]


def test_no_scan_covering(scanned: Path):
    r = _run_dt(scanned, 'du', 'gcs://nope')
    assert r.returncode != 0
    assert r.stderr.rstrip().split('\n')[-1] == "no scan covering 'gcs://nope'"


def test_du_shows_reclaimable_when_sidecar_present(scanned: Path, monkeypatch):
    """When a .reclaim sidecar sits beside the blob, du joins it as a `frees`
    column keyed by scan-relative path."""
    import subprocess
    from disk_tree.extents import write_reclaim_sidecar

    env = {**os.environ, 'DISK_TREE_ROOT': str(scanned)}
    r = subprocess.run(
        [sys.executable, '-m', 'disk_tree.cli.main', 'scans', 'list'],
        env=env, capture_output=True, text=True, check=True,
    )
    scan = [json.loads(l) for l in r.stdout.splitlines() if l.strip().startswith('{')][0]
    blob = resolve_blob_env(env, scan['blob'])
    write_reclaim_sidecar(blob, {'.': 600, 'sub': 250})

    out = subprocess.run(
        [sys.executable, '-m', 'disk_tree.cli.main', 'du', 'gcs://b1', '-j'],
        env=env, capture_output=True, text=True, check=True,
    )
    d = json.loads(out.stdout)
    assert d['reclaimable'] == 600
    assert [(row['path'], row['reclaimable']) for row in d['rows'] if row['kind'] == 'dir'] == [('sub', 250)]


def resolve_blob_env(env, blob):
    """Resolve a blob basename to its path under the test's DISK_TREE_ROOT."""
    import subprocess, sys
    code = (
        "from disk_tree.resolve import resolve_blob;"
        f"print(resolve_blob({blob!r}))"
    )
    return subprocess.run([sys.executable, '-c', code], env=env,
                          capture_output=True, text=True, check=True).stdout.strip()
