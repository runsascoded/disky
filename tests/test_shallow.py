"""Spec `scan-page-r2-latency.md`: scan blobs are written in bounded row groups
(ask 2); `/api/scan` gets each chunk's depth-1 rows from the root blob's
`.shallow.parquet` sidecar written at save time (ask 3), falling back to a
filtered, projected, per-process-cached read of the chunk (ask 1) — so a page
load never pulls a whole chunk blob, local or R2."""
import os
import sqlite3
from uuid import uuid4

import pandas as pd
import pyarrow.parquet as pq
import pytest
from pandas.testing import assert_frame_equal

from disk_tree import blobfs, shallow
from disk_tree.storage import reset_backend
from disk_tree.storage.base import BLOB_ROW_GROUP_SIZE
from disk_tree.storage.hybrid import HybridBackend

N_LARGE = 100


def _row(path: str, size: int, kind: str, parent: str, n_desc: int, n_children: int, depth: int) -> dict:
    uri = '/test' if path == '.' else f'/test/{path}'
    return {'path': path, 'size': size, 'mtime': 1000.0 + depth, 'kind': kind, 'parent': parent, 'uri': uri,
            'n_desc': n_desc, 'n_children': n_children, 'depth': depth}


def chunked_df() -> pd.DataFrame:
    """A root with a small dir and a `large` dir of `N_LARGE` files — `large`
    chunks at `chunk_threshold=50`."""
    rows = [
        _row('.', 100 + 99 * N_LARGE, 'dir', '', 3 + N_LARGE, 2, 0),
        _row('small', 100, 'dir', '.', 1, 1, 1),
        _row('small/a.txt', 100, 'file', 'small', 0, 0, 2),
        _row('large', 99 * N_LARGE, 'dir', '.', N_LARGE, N_LARGE, 1),
        *[_row(f'large/f{i:03d}.txt', 99, 'file', 'large', 0, 0, 2) for i in range(N_LARGE)],
    ]
    return pd.DataFrame(rows)


def _sorted(df: pd.DataFrame) -> pd.DataFrame:
    return df.sort_values('path').reset_index(drop=True)


def _chunked(scans_dir: str) -> tuple[HybridBackend, str, str, str]:
    """Save `chunked_df` chunked → (backend, root ref, root path, chunk ref)."""
    b = HybridBackend(scans_dir=scans_dir, chunk_threshold=50)
    ref = b.save(chunked_df(), '/test')
    chunks = b.get_chunk_stats(ref)['chunks']
    assert [c['path'] for c in chunks] == ['large']
    return b, ref, blobfs.join(scans_dir, ref), chunks[0]['blob_ref']


def _record_reads(monkeypatch) -> list[tuple[str, object, object]]:
    """Every `blobfs.read_parquet` call as `(path, filters, columns)`."""
    calls: list[tuple[str, object, object]] = []
    real = blobfs.read_parquet

    def spy(path, filters=None, columns=None):
        calls.append((path, filters, columns))
        return real(path, filters=filters, columns=columns)
    monkeypatch.setattr(blobfs, 'read_parquet', spy)
    monkeypatch.setattr(shallow.blobfs, 'read_parquet', spy)
    return calls


def test_row_groups_are_bounded(tmp_path):
    """Root and chunk blobs land in `BLOB_ROW_GROUP_SIZE` row groups (pyarrow's
    1Mi default put a home scan's `depth == 1` rows in a 35 MiB group)."""
    n = BLOB_ROW_GROUP_SIZE + 10
    rows = [_row('.', n, 'dir', '', n, n, 0), *[_row(f'f{i:06d}', 1, 'file', '.', 0, 0, 1) for i in range(n)]]
    b = HybridBackend(scans_dir=str(tmp_path), chunk_threshold=10**9)
    ref = b.save(pd.DataFrame(rows), '/test')
    md = pq.read_metadata(tmp_path / ref)
    assert [md.row_group(i).num_rows for i in range(md.num_row_groups)] == [BLOB_ROW_GROUP_SIZE, 11]


def test_chunked_save_writes_shallow_sidecar(tmp_path):
    b, ref, root, chunk_ref = _chunked(str(tmp_path))
    side = shallow.shallow_path(root)
    assert side == str(tmp_path / (ref[:-len('.parquet')] + '.shallow.parquet'))
    s = pd.read_parquet(side)
    assert sorted(s.columns) == sorted([*chunked_df().columns, 'child_scan_id', shallow.CHUNK_REF])
    assert (s[shallow.CHUNK_REF] == chunk_ref).all()
    # chunk-local coordinates, exactly what a `depth == 1` read of the chunk returns
    assert (s['depth'] == 1).all() and (s['parent'] == '.').all()
    assert sorted(s['path']) == [f'f{i:03d}.txt' for i in range(N_LARGE)]
    direct = blobfs.read_parquet(blobfs.join(str(tmp_path), chunk_ref), filters=[('depth', '==', 1)])
    assert_frame_equal(_sorted(shallow.chunk_top_rows(root, chunk_ref, b._resolve)), _sorted(direct))


def test_sidecar_serves_without_touching_the_chunk(tmp_path, monkeypatch):
    b, ref, root, chunk_ref = _chunked(str(tmp_path))
    calls = _record_reads(monkeypatch)
    shallow.clear_cache()
    top = shallow.chunk_top_rows(root, chunk_ref, b._resolve, columns=['path', 'size', 'depth'])
    assert list(top.columns) == ['path', 'size', 'depth']
    assert len(top) == N_LARGE
    assert [c[0] for c in calls] == [shallow.shallow_path(root)]
    shallow.chunk_top_rows(root, chunk_ref, b._resolve, columns=['path', 'size', 'depth'])
    assert len(calls) == 1  # per-process cache: the sidecar is read once


def test_without_sidecar_reads_filtered_projected_and_cached(tmp_path, monkeypatch):
    b, ref, root, chunk_ref = _chunked(str(tmp_path))
    os.remove(shallow.shallow_path(root))
    calls = _record_reads(monkeypatch)
    shallow.clear_cache()
    chunk_path = blobfs.join(str(tmp_path), chunk_ref)
    top = shallow.chunk_top_rows(root, chunk_ref, b._resolve, columns=['path', 'size', 'depth', 'rel_path'])
    # `rel_path` is a server-side column, not in the blob: dropped from the projection
    assert calls == [(chunk_path, [('depth', '==', 1)], ['path', 'size', 'depth'])]
    assert list(top.columns) == ['path', 'size', 'depth']
    assert sorted(top['path']) == [f'f{i:03d}.txt' for i in range(N_LARGE)]
    shallow.chunk_top_rows(root, chunk_ref, b._resolve, columns=['path', 'size', 'depth', 'rel_path'])
    assert len(calls) == 1


def test_missing_chunk_is_none(tmp_path):
    b, ref, root, chunk_ref = _chunked(str(tmp_path))
    os.remove(shallow.shallow_path(root))
    os.remove(tmp_path / chunk_ref)
    shallow.clear_cache()
    assert shallow.chunk_top_rows(root, chunk_ref, b._resolve) is None


def test_delete_inside_chunk_refreshes_sidecar(tmp_path):
    b, ref, root, chunk_ref = _chunked(str(tmp_path))
    assert b.delete_path(ref, 'large/f000.txt').size == 99
    s = pd.read_parquet(shallow.shallow_path(root))
    assert sorted(s['path']) == [f'f{i:03d}.txt' for i in range(1, N_LARGE)]
    shallow.clear_cache()
    assert len(shallow.chunk_top_rows(root, chunk_ref, b._resolve)) == N_LARGE - 1


def test_delete_scan_removes_sidecar(tmp_path):
    b, ref, root, chunk_ref = _chunked(str(tmp_path))
    b.delete(ref)
    assert sorted(os.listdir(tmp_path)) == []


def test_build_shallow_backfills_an_existing_scan(tmp_path):
    b, ref, root, chunk_ref = _chunked(str(tmp_path))
    before = pd.read_parquet(shallow.shallow_path(root))
    os.remove(shallow.shallow_path(root))
    assert shallow.build_shallow(root, b._resolve) == shallow.shallow_path(root)
    assert_frame_equal(_sorted(pd.read_parquet(shallow.shallow_path(root))), _sorted(before))
    # an unchunked root has nothing to denormalize: no sidecar
    plain = HybridBackend(scans_dir=str(tmp_path), chunk_threshold=10**9).save(chunked_df(), '/test')
    assert shallow.build_shallow(blobfs.join(str(tmp_path), plain), b._resolve) is None


def test_remote_scans_dir_gets_the_sidecar_too():
    pytest.importorskip('fsspec')
    scans_dir = f'memory://{uuid4()}'
    b, ref, root, chunk_ref = _chunked(scans_dir)
    assert blobfs.exists(shallow.shallow_path(root))
    direct = blobfs.read_parquet(blobfs.join(scans_dir, chunk_ref), filters=[('depth', '==', 1)])
    assert_frame_equal(_sorted(shallow.chunk_top_rows(root, chunk_ref, b._resolve)), _sorted(direct))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A Flask test client over a hybrid backend rooted in `tmp_path`."""
    from disk_tree.server import app, clear_cache, init_db
    scans_dir = str(tmp_path / 'scans')
    os.makedirs(scans_dir)
    db_path = str(tmp_path / 'disk-tree.db')
    monkeypatch.setenv('DISK_TREE_BACKEND', 'hybrid')
    monkeypatch.setattr('disk_tree.server.DB_PATH', db_path)
    monkeypatch.setattr('disk_tree.config.SQLITE_PATH', db_path)
    monkeypatch.setattr('disk_tree.config.ROOT_DIR', str(tmp_path))
    monkeypatch.setattr('disk_tree.config.SCANS_DIR', scans_dir)
    monkeypatch.setattr('disk_tree.server.AUTO_INDEX', False)
    reset_backend()
    init_db()
    clear_cache()
    shallow.clear_cache()
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c, db_path, scans_dir
    reset_backend()


def _scan_rows(client, uri: str) -> list[dict]:
    r = client.get(f'/api/scan?uri={uri}&depth=2&expand_single=false')
    assert r.status_code == 200, r.get_json()
    return sorted(r.get_json()['rows'], key=lambda x: x['path'])


def test_api_scan_serves_chunk_tops_from_the_sidecar(client, monkeypatch):
    from disk_tree.server import clear_cache
    c, db_path, scans_dir = client
    b, ref, root, chunk_ref = _chunked(scans_dir)
    con = sqlite3.connect(db_path)
    con.execute('INSERT INTO scan (path, time, blob, size, n_desc, n_children) VALUES (?, ?, ?, ?, ?, ?)',
                ('/test', '2026-09-26T00:00:00', ref, 100 + 99 * N_LARGE, 3 + N_LARGE, 2))
    con.commit()
    con.close()
    chunk_path = blobfs.join(scans_dir, chunk_ref)
    calls = _record_reads(monkeypatch)

    rows = _scan_rows(c, '/test')
    assert [r['path'] for r in rows] == sorted(['large', *[f'large/f{i:03d}.txt' for i in range(N_LARGE)], 'small', 'small/a.txt'])
    assert {(r['parent'], r['depth']) for r in rows if r['path'].startswith('large/')} == {('large', 2)}
    assert [c for c in calls if c[0] == chunk_path] == []  # the chunk blob was never opened

    # No sidecar (a scan written before this landed): one filtered, projected
    # read of the chunk, then the per-process cache answers.
    os.remove(shallow.shallow_path(root))
    clear_cache()
    calls.clear()
    assert _scan_rows(c, '/test') == rows
    chunk_reads = [c for c in calls if c[0] == chunk_path]
    assert len(chunk_reads) == 1
    assert chunk_reads[0][1] == [('depth', '==', 1)]
    assert sorted(chunk_reads[0][2]) == sorted([*chunked_df().columns, 'child_scan_id'])
    clear_cache()
    calls.clear()
    assert _scan_rows(c, '/test') == rows
    assert [c for c in calls if c[0] == chunk_path] == []


def test_chunk_map_reads_only_pointer_row_groups(tmp_path, monkeypatch):
    """`_chunk_map` pushes `child_scan_id IS NOT NULL` down: an all-null row
    group is pruned from its footer stats, and a chunk whose column is Arrow
    type `null` (no stats at all) is answered from the schema alone — reading
    a 1.4M-row chunk's `path` column to find zero pointers cost ~16 s over R2."""
    import pyarrow as pa
    from disk_tree import diff
    n = 3 * BLOB_ROW_GROUP_SIZE
    ids = [None] * n
    ids[1] = 'c.parquet'
    root = str(tmp_path / 'root.parquet')
    blobfs.write_table(pa.table({'path': [f'p{i:06d}' for i in range(n)], 'child_scan_id': pa.array(ids, pa.string())}), root, BLOB_ROW_GROUP_SIZE)
    chunk = str(tmp_path / 'chunk.parquet')
    blobfs.write_table(pa.table({'path': ['a', 'b'], 'child_scan_id': pa.nulls(2)}), chunk, BLOB_ROW_GROUP_SIZE)
    reads: list[int] = []
    real = blobfs.read_table
    monkeypatch.setattr(blobfs, 'read_table', lambda *a, **kw: reads.append(real(*a, **kw).num_rows) or real(*a, **kw))
    diff._chunk_map_cached.cache_clear()
    assert diff._chunk_map(root) == {'p000001': 'c.parquet'}
    assert diff._chunk_map(chunk) is None
    assert reads == [1]
