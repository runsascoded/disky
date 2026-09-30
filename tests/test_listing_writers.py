"""The in-memory (pandas) listing writers — local `index`, `import -e pandas`,
the hybrid backend's chunk and delete rewrites — all go through
`listing_format.write_listing` (spec `listing-slim.md` phase 1): each blob is
v2 (no `uri`, single-valued pivots implied, the format keys, the switch
codec) and `blobfs.read_parquet` returns exactly the v1 frame that was saved,
for every root shape the backends produce. Plus the stream engine's resume of
a pre-v2 parts dir."""
import io
import json
import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from disk_tree import listing_format as lf
from disk_tree.blobfs import read_parquet
from disk_tree.find.import_listing import import_listing
from disk_tree.find.index import aggregate, index as index_local
from disk_tree.storage.base import PathStats
from disk_tree.storage.hybrid import HybridBackend
from disk_tree.storage.parquet import ParquetBackend

from test_listing_format import BASE, _codecs, _kv, _listing, _stream, codec  # noqa: F401  (`codec` is a fixture)


def _v1_roundtrip(df: pd.DataFrame) -> pd.DataFrame:
    """What the old writer + reader pair returned for `df`: `df.to_parquet` then
    `pd.read_parquet` (the dtype normalization a parquet round trip applies)."""
    return pd.read_parquet(io.BytesIO(df.to_parquet(index=False)))


def _kv_fmt(path: str) -> dict[str, str]:
    """The listing-format keys of a file's key-value metadata."""
    return {k: v for k, v in _kv(path).items() if k.startswith('disk_tree.')}


def _columns_kv(columns) -> str:
    return json.dumps(list(columns), separators=(',', ':'))


def _walk_rows(uri_for, entries: list[tuple[str, str, int, int]]) -> pd.DataFrame:
    """Rows as a walk backend emits them (`backends/gfind.py`, `backends/s3.py`):
    `path` relative to the root (`''` for the root itself), `uri` from `uri_for`."""
    return pd.DataFrame([
        {'path': p, 'size': s, 'mtime': m, 'kind': k, 'parent': None if p == '' else os.path.dirname(p), 'uri': uri_for(p)}
        for k, p, s, m in entries
    ])


ENTRIES = [
    ('dir', '', 4096, 10), ('dir', 'sub', 4096, 20), ('file', 'a.txt', 100, 30),
    ('file', 'sub/b.txt', 200, 40), ('dir', 'sub/deep', 4096, 50), ('file', 'sub/deep/c.txt', 300, 60),
]
PATHS = ['.', 'a.txt', 'sub', 'sub/b.txt', 'sub/deep', 'sub/deep/c.txt']
AGG_COLUMNS = ['path', 'size', 'mtime', 'n_desc', 'n_files', 'n_children', 'kind', 'parent', 'uri', 'depth']


@pytest.mark.parametrize('root, expected_uris', [
    # local: `uri` is the absolute path (gfind's `%p`); the scan root is the `abspath`ed dir.
    ('/Users/ryan', ['/Users/ryan', '/Users/ryan/a.txt', '/Users/ryan/sub', '/Users/ryan/sub/b.txt', '/Users/ryan/sub/deep', '/Users/ryan/sub/deep/c.txt']),
    # local, the filesystem root: `/foo`, never `//foo`.
    ('/', ['/', '/a.txt', '/sub', '/sub/b.txt', '/sub/deep', '/sub/deep/c.txt']),
    ('s3://bkt', ['s3://bkt', 's3://bkt/a.txt', 's3://bkt/sub', 's3://bkt/sub/b.txt', 's3://bkt/sub/deep', 's3://bkt/sub/deep/c.txt']),
    ('s3://bkt/pre/fix', ['s3://bkt/pre/fix', 's3://bkt/pre/fix/a.txt', 's3://bkt/pre/fix/sub', 's3://bkt/pre/fix/sub/b.txt', 's3://bkt/pre/fix/sub/deep', 's3://bkt/pre/fix/sub/deep/c.txt']),
    ('r2://bkt', ['r2://bkt', 'r2://bkt/a.txt', 'r2://bkt/sub', 'r2://bkt/sub/b.txt', 'r2://bkt/sub/deep', 'r2://bkt/sub/deep/c.txt']),
    ('gs://bkt', ['gs://bkt', 'gs://bkt/a.txt', 'gs://bkt/sub', 'gs://bkt/sub/b.txt', 'gs://bkt/sub/deep', 'gs://bkt/sub/deep/c.txt']),
    ('gcs://b1', ['gcs://b1', 'gcs://b1/a.txt', 'gcs://b1/sub', 'gcs://b1/sub/b.txt', 'gcs://b1/sub/deep', 'gcs://b1/sub/deep/c.txt']),
    ('ssh://me@host:22/srv/data', ['ssh://me@host:22/srv/data', 'ssh://me@host:22/srv/data/a.txt', 'ssh://me@host:22/srv/data/sub', 'ssh://me@host:22/srv/data/sub/b.txt', 'ssh://me@host:22/srv/data/sub/deep', 'ssh://me@host:22/srv/data/sub/deep/c.txt']),
])
def test_scan_root_reproduces_uri_per_scheme(tmp_path: Path, codec, root, expected_uris):
    """`find.index.aggregate` + a backend `save` writes v2 with `scan_root` =
    the root, and `blobfs.read_parquet` restores the exact `uri` column of the
    saved frame — for every root shape the backends produce."""
    uri_for = lambda p: root if p == '' else lf.uri_prefix(root) + p
    df = aggregate(_walk_rows(uri_for, ENTRIES), scan_root=root)
    assert list(df.columns) == AGG_COLUMNS
    assert df['path'].tolist() == PATHS
    assert df['uri'].tolist() == expected_uris
    b = ParquetBackend(scans_dir=str(tmp_path))
    blob = str(tmp_path / b.save(df, root))
    assert pq.read_schema(blob).names == [c for c in AGG_COLUMNS if c != 'uri']
    assert _kv_fmt(blob) == {
        'disk_tree.listing_format': '2',
        'disk_tree.scan_root': root,
        'disk_tree.columns': _columns_kv(AGG_COLUMNS),
    }
    assert _codecs(blob) == {codec}
    pd.testing.assert_frame_equal(read_parquet(blob), _v1_roundtrip(df))
    assert read_parquet(blob, columns=['uri', 'path']).values.tolist() == [[u, p] for u, p in zip(expected_uris, PATHS)]


@patch('subprocess.Popen')
def test_local_index_saves_v2(mock_popen, tmp_path: Path, codec):
    """`disk-tree index` of a local dir: gfind → `find.index` → `HybridBackend.save`."""
    raw = ''.join(f'{k} {b} {m}.0000000000 {p}\0' for k, b, m, p in [
        ('d', 8, 1_000, '/root'), ('d', 8, 2_000, '/root/a'), ('f', 2, 3_000, '/root/a/x.bin'), ('f', 6, 4_000, '/root/y.bin'),
    ]).encode()
    proc = MagicMock()
    proc.stdout, proc.stderr, proc.wait.return_value = io.BytesIO(raw), io.BytesIO(b''), 0
    mock_popen.return_value = proc
    df = index_local('/root').df
    assert df['uri'].tolist() == ['/root', '/root/a', '/root/y.bin', '/root/a/x.bin']
    blob = str(tmp_path / HybridBackend(scans_dir=str(tmp_path)).save(df, '/root'))
    # `save` adds `child_scan_id` to the frame it is handed (all None: no chunks).
    assert list(df.columns) == [*AGG_COLUMNS, 'child_scan_id']
    assert lf.format_of(blob) == lf.slim('/root', list(df.columns))
    assert pq.read_schema(blob).names == [c for c in df.columns if c != 'uri']
    assert _codecs(blob) == {codec}
    pd.testing.assert_frame_equal(read_parquet(blob), _v1_roundtrip(df))


@pytest.mark.parametrize('classes, implied', [([1, 1, 1, 1, 1], {'sum_storage_class_id_1': 'size'}), ([1, 2, 1, 2, 1], {})])
def test_import_pandas_saves_v2(tmp_path: Path, codec, classes, implied):
    """`import -e pandas`: the pandas engine's frame through `storage.save` — a
    single-class pivot column (equal to `size` on every row) is implied, as the
    duckdb/stream engines leave it; two classes are written."""
    listing = _listing(tmp_path / 'l.parquet', classes)
    df = import_listing((listing,), bucket='b1', scheme='gcs', pivot_sums=('storage_class_id',), mean_mtime=True).df
    blob = str(tmp_path / ParquetBackend(scans_dir=str(tmp_path)).save(df, 'gcs://b1'))
    pivots = [f'sum_storage_class_id_{v}' for v in sorted(set(classes))]
    # The pandas engine's own column order (the duckdb/stream engines put the
    # pivots after `parent`); `columns` records it, so the restore matches it.
    assert list(df.columns) == [*BASE[:-1], *pivots, 'parent', 'uri', 'depth', 'mtime_mean']
    assert pq.read_schema(blob).names == [c for c in df.columns if c != 'uri' and c not in implied]
    assert _kv_fmt(blob) == {
        'disk_tree.listing_format': '2',
        'disk_tree.scan_root': 'gcs://b1',
        **({'disk_tree.implied': json.dumps(implied, separators=(',', ':'))} if implied else {}),
        'disk_tree.columns': _columns_kv(df.columns),
    }
    assert _codecs(blob) == {codec}
    pd.testing.assert_frame_equal(read_parquet(blob), _v1_roundtrip(df))


def _chunky(n: int = 6) -> pd.DataFrame:
    """A `/test` scan whose `large` dir (n files) chunks at `chunk_threshold=n`."""
    def row(path, size, kind, parent, n_desc, n_children, depth):
        return {'path': path, 'size': size, 'mtime': 1000.0 + depth, 'kind': kind, 'parent': parent,
                'uri': lf.uri_of('/test', path), 'n_desc': n_desc, 'n_children': n_children, 'depth': depth}
    files = [row(f'large/f{i}.txt', 10, 'file', 'large', 0, 0, 2) for i in range(n)]
    return pd.DataFrame([
        row('.', 10 * n + 5, 'dir', '', n + 3, 2, 0),
        row('large', 10 * n, 'dir', '.', n, n, 1),
        row('small', 5, 'dir', '.', 1, 1, 1),
        row('small/s.txt', 5, 'file', 'small', 0, 0, 2),
        *files,
    ]).sort_values(['depth', 'path']).reset_index(drop=True)


def test_hybrid_chunks_and_delete_rewrites_are_v2(tmp_path: Path, codec):
    """A chunked save writes the root and the chunk as v2 — the chunk's scan root
    is the subtree's absolute location (its rows are rebased, its `uri`s are
    not) — and every in-place delete rewrite (inside a chunk, directly in the
    root, a whole chunk) writes v2 again, restoring exact `uri`s."""
    df = _chunky()
    b = HybridBackend(scans_dir=str(tmp_path), chunk_threshold=6)
    root_ref = b.save(df.copy(), '/test')
    root_blob = str(tmp_path / root_ref)
    root = read_parquet(root_blob)
    chunk_ref = root.loc[root['path'] == 'large', 'child_scan_id'].item()
    chunk_blob = str(tmp_path / chunk_ref)
    assert _kv_fmt(root_blob) == {
        'disk_tree.listing_format': '2', 'disk_tree.scan_root': '/test',
        'disk_tree.columns': _columns_kv([*df.columns, 'child_scan_id']),
    }
    assert _kv_fmt(chunk_blob) == {
        'disk_tree.listing_format': '2', 'disk_tree.scan_root': '/test/large',
        'disk_tree.columns': _columns_kv([*df.columns, 'child_scan_id']),
    }
    assert pq.read_schema(root_blob).names == pq.read_schema(chunk_blob).names == [c for c in df.columns if c != 'uri'] + ['child_scan_id']
    assert _codecs(root_blob) == _codecs(chunk_blob) == {codec}
    assert read_parquet(chunk_blob, columns=['path', 'uri']).values.tolist() == [
        ['.', '/test/large'], *[[f'f{i}.txt', f'/test/large/f{i}.txt'] for i in range(6)],
    ]
    # The whole scan, chunks followed, is the saved frame (`child_scan_id` aside).
    got = b.load(root_ref, follow_refs=True).drop(columns=['child_scan_id']).sort_values(['depth', 'path']).reset_index(drop=True)
    pd.testing.assert_frame_equal(got, _v1_roundtrip(df))

    # Delete inside the chunk → the chunk and the root's summary row rewritten, both v2.
    assert b.delete_path(root_ref, 'large/f0.txt') == PathStats(size=10, n_desc=0, n_children=0)
    assert (lf.format_of(chunk_blob).scan_root, lf.format_of(root_blob).scan_root) == ('/test/large', '/test')
    assert read_parquet(chunk_blob, columns=['path', 'uri', 'size']).values.tolist() == [
        ['.', '/test/large', 50], *[[f'f{i}.txt', f'/test/large/f{i}.txt', 10] for i in range(1, 6)],
    ]
    assert read_parquet(root_blob, columns=['path', 'uri', 'size']).values.tolist() == [
        ['.', '/test', 55], ['large', '/test/large', 50], ['small', '/test/small', 5], ['small/s.txt', '/test/small/s.txt', 5],
    ]
    # Delete directly in the root → the root rewritten, v2.
    assert b.delete_path(root_ref, 'small/s.txt') == PathStats(size=5, n_desc=0, n_children=0)
    assert lf.format_of(root_blob).scan_root == '/test'
    assert read_parquet(root_blob, columns=['path', 'uri', 'size']).values.tolist() == [
        ['.', '/test', 50], ['large', '/test/large', 50], ['small', '/test/small', 0],
    ]
    # Delete the whole chunk → its blob goes, the root rewritten, v2. (The stats
    # come from the root's summary row, whose `n_children` an inside-chunk
    # delete does not touch — still 6 here; only the chunk's own `.` row went
    # to 5. Pre-existing hybrid bookkeeping, not this writer's concern.)
    st = b.delete_path(root_ref, 'large')
    assert (st.size, st.n_desc, st.n_children) == (50, 5, 6)
    assert not os.path.exists(chunk_blob)
    assert lf.format_of(root_blob).scan_root == '/test'
    assert read_parquet(root_blob, columns=['path', 'uri', 'size', 'n_desc']).values.tolist() == [
        ['.', '/test', 0, 1], ['small', '/test/small', 0, 0],
    ]


def test_write_listing_keeps_v1_when_uri_is_not_derivable(tmp_path: Path, capsys):
    """Slimming is lossless only when `uri` is `<root>/<path>` on every row; a
    frame that breaks that (here one row's `uri` disagrees) is written v1 —
    unchanged, `uri` kept — with a note on stderr. No `uri` at all, or no root
    row to name the root, is v1 too, silently."""
    df = _chunky(2)
    df.loc[df['path'] == 'small', 'uri'] = '/elsewhere/small'
    out = str(tmp_path / 'odd.parquet')
    assert lf.write_listing(df, out, 65_536) == lf.V1
    assert capsys.readouterr().err == f"{out}: `uri` is not `<scan root>/<path>` on every row (root '/test'); writing the v1 listing format\n"
    assert lf.format_of(out) == lf.V1
    assert pq.read_schema(out).names == list(df.columns)
    pd.testing.assert_frame_equal(read_parquet(out), _v1_roundtrip(df))

    no_uri = _chunky(2).drop(columns=['uri'])
    assert lf.write_listing(no_uri, out, 65_536) == lf.V1
    pd.testing.assert_frame_equal(read_parquet(out), _v1_roundtrip(no_uri))
    no_root = _chunky(2).query("path != '.'")
    assert lf.write_listing(no_root, out, 65_536) == lf.V1
    pd.testing.assert_frame_equal(read_parquet(out), _v1_roundtrip(no_root))
    assert capsys.readouterr().err == ''


def test_readable_columns():
    """What a reader may ask `blobfs.read_parquet` for, per file: v1 = the
    schema; v2 = the schema plus the derived `uri` and implied pivots, in v1
    order (the projection seam `shallow.chunk_top_rows` intersects with)."""
    v1 = pa.schema([('path', pa.string()), ('uri', pa.string()), ('size', pa.int64())])
    assert lf.readable_columns(v1) == ['path', 'uri', 'size']
    v2 = lf.with_kv(
        pa.schema([('path', pa.string()), ('size', pa.int64()), ('depth', pa.int64())]),
        lf.slim('/r', ['path', 'size', 'sum_x_1', 'uri', 'depth'], {'sum_x_1': 'size'}),
    )
    assert lf.readable_columns(v2) == ['path', 'size', 'sum_x_1', 'uri', 'depth']
    projected = lf.with_kv(pa.schema([('depth', pa.int64())]), lf.slim('/r', ['path', 'uri', 'depth']))
    assert lf.readable_columns(projected) == ['depth']


def test_stream_resume_from_pre_v2_parts_finalizes_v2(tmp_path: Path, codec, capsys, monkeypatch):
    """A parts dir streamed before the v2 format (its manifest has no
    `scan_root` / `all_pivot_names` / `implied`; its parts hold every pivot
    column) still finalizes as v2: the scan root comes from the call, every
    pivot column the parts hold is written, none implied. Output is the fresh
    v2 run's, byte for byte."""
    from disk_tree.find import aggregate_stream as mod
    listing = _listing(tmp_path / 'l.parquet', [1, 2, 1, 2, 1])
    out = tmp_path / 'out.parquet'
    fresh = _stream(listing, tmp_path / 'fresh.parquet')

    def boom(*a, **kw):
        raise RuntimeError('injected finalize failure')

    finalize = mod._finalize_parts
    monkeypatch.setattr(mod, '_finalize_parts', boom)
    with pytest.raises(RuntimeError):
        _stream(listing, out)
    monkeypatch.setattr(mod, '_finalize_parts', finalize)  # not `undo()`: that would drop the `codec` env too
    manifest_path = Path(f'{out}.parts') / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    assert (manifest['scan_root'], manifest['all_pivot_names'], manifest['implied']) == (
        'gcs://b1', ['sum_storage_class_id_1', 'sum_storage_class_id_2'], {},
    )
    for k in ('scan_root', 'all_pivot_names', 'implied'):
        del manifest[k]
    manifest_path.write_text(json.dumps(manifest))
    capsys.readouterr()

    _stream(listing, out)
    stages = [re.sub(r'^\[agg [0-9T:-]+\] ', '[agg] ', line) for line in capsys.readouterr().err.rstrip('\n').split('\n')]
    assert stages[:2] == [
        f'[agg] resuming from streamed parts at {out}.parts (stream pass skipped)',
        '[agg] parts predate the v2 listing format: finalizing as v2 with every pivot column written',
    ]
    assert not Path(f'{out}.parts').exists()
    assert _kv_fmt(str(out)) == _kv_fmt(fresh) == {
        'disk_tree.listing_format': '2', 'disk_tree.scan_root': 'gcs://b1',
        'disk_tree.columns': _columns_kv([*BASE, 'sum_storage_class_id_1', 'sum_storage_class_id_2', 'mtime_mean', 'uri', 'depth']),
    }
    assert _codecs(str(out)) == {codec}
    assert out.read_bytes() == Path(fresh).read_bytes()
