"""Parquet-file backends over a remote (URL) scans dir — spec
`remote-scan-targets.md`, Phase 1.

`memory://` exercises every URL branch (fsspec `filesystem=` IO, remote
`exists` / `remove` / `put`) with no network and no extra drivers; `file://`
covers the same code path through fsspec's local driver.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from disk_tree import blobfs
from disk_tree.storage.base import PathStats
from disk_tree.storage.hybrid import HybridBackend
from disk_tree.storage.parquet import ParquetBackend

pytest.importorskip('fsspec')

ROWS = [
    {'path': '.', 'size': 1000, 'mtime': 1000.0, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 5, 'n_children': 2, 'depth': 0},
    {'path': 'foo', 'size': 400, 'mtime': 1001.0, 'kind': 'dir', 'parent': '.', 'uri': '/test/foo', 'n_desc': 2, 'n_children': 2, 'depth': 1},
    {'path': 'bar', 'size': 600, 'mtime': 1002.0, 'kind': 'dir', 'parent': '.', 'uri': '/test/bar', 'n_desc': 1, 'n_children': 1, 'depth': 1},
    {'path': 'foo/a.txt', 'size': 100, 'mtime': 1003.0, 'kind': 'file', 'parent': 'foo', 'uri': '/test/foo/a.txt', 'n_desc': 0, 'n_children': 0, 'depth': 2},
    {'path': 'foo/b.txt', 'size': 300, 'mtime': 1004.0, 'kind': 'file', 'parent': 'foo', 'uri': '/test/foo/b.txt', 'n_desc': 0, 'n_children': 0, 'depth': 2},
    {'path': 'bar/c.txt', 'size': 600, 'mtime': 1005.0, 'kind': 'file', 'parent': 'bar', 'uri': '/test/bar/c.txt', 'n_desc': 0, 'n_children': 0, 'depth': 2},
]


def _df() -> pd.DataFrame:
    return pd.DataFrame(ROWS)


def _mem() -> str:
    return f'memory://{uuid4()}'


def _sorted(df: pd.DataFrame) -> pd.DataFrame:
    return df.sort_values('path').reset_index(drop=True)


@pytest.fixture(params=[HybridBackend, ParquetBackend], ids=['hybrid', 'parquet'])
def backend(request):
    return request.param(scans_dir=_mem())


def test_save_and_load_round_trip(backend):
    ref = backend.save(_df(), '/test')
    assert blobfs.list_parquets(backend.scans_dir) == [ref]
    loaded = backend.load(ref)
    # Hybrid `save` adds its chunk-pointer column (null when nothing chunked);
    # the plain parquet backend stores the frame as given.
    extra = sorted(set(loaded.columns) - set(_df().columns))
    assert extra == (['child_scan_id'] if backend.name == 'hybrid' else [])
    if extra:
        assert loaded['child_scan_id'].isna().all()
    assert_frame_equal(_sorted(loaded.drop(columns=extra)), _sorted(_df()), check_like=True)


def test_depth_pushdown(backend):
    ref = backend.save(_df(), '/test')
    assert sorted(backend.load(ref, max_depth=1, min_depth=1)['path']) == ['bar', 'foo']


def test_path_prefix_pushdown(backend):
    ref = backend.save(_df(), '/test')
    assert sorted(backend.load(ref, path_prefix='foo')['path']) == ['foo', 'foo/a.txt', 'foo/b.txt']


def test_get_path_stats(backend):
    ref = backend.save(_df(), '/test')
    assert backend.get_path_stats(ref, 'foo') == PathStats(size=400, n_desc=2, n_children=2, mtime=1001.0)


def test_delete_removes_the_remote_blob(backend):
    ref = backend.save(_df(), '/test')
    backend.delete(ref)
    assert blobfs.exists(blobfs.join(backend.scans_dir, ref)) is False
    assert blobfs.list_parquets(backend.scans_dir) == []


def test_adopt_parquet_uploads_and_consumes_the_local_file(backend, tmp_path: Path):
    local = tmp_path / 'layer2.parquet'
    _df().to_parquet(local, index=False)
    ref = backend.adopt_parquet(str(local), '/test')
    assert local.exists() is False
    assert blobfs.list_parquets(backend.scans_dir) == [ref]
    assert_frame_equal(_sorted(backend.load(ref)), _sorted(_df()), check_like=True)


def test_hybrid_chunks_land_remotely_and_follow_refs_reassembles():
    b = HybridBackend(scans_dir=_mem(), chunk_threshold=1)
    ref = b.save(_df(), '/test')
    stats = b.get_chunk_stats(ref)
    assert stats['total_chunks'] == 2
    chunk_refs = [c['blob_ref'] for c in stats['chunks']]
    assert sorted(blobfs.list_parquets(b.scans_dir)) == sorted([ref, *chunk_refs])

    full = b.load(ref, follow_refs=True)
    assert sorted(full['path']) == ['.', 'bar', 'bar/c.txt', 'foo', 'foo/a.txt', 'foo/b.txt']
    assert b.get_path_stats(ref, 'foo/a.txt') == PathStats(size=100, n_desc=0, n_children=0, mtime=1003.0)

    b.delete(ref)
    assert blobfs.list_parquets(b.scans_dir) == []


def test_file_url_scans_dir(tmp_path: Path):
    b = ParquetBackend(scans_dir=f'file://{tmp_path}')
    ref = b.save(_df(), '/test')
    assert sorted(p.name for p in tmp_path.glob('*.parquet')) == [ref]
    assert sorted(b.load(ref, max_depth=0)['path']) == ['.']
