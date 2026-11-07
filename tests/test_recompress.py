"""`disk-tree recompress` (spec `listing-slim.md` phase 2): a v1 layer-2
listing — the old writers' shape: Snappy, `uri` + class pivot columns, no
format keys — rewritten in place as v2, lossless, verify-then-swap; and
`disk-tree listing-format`, the audit that finds the v1 files."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import duckdb
import pandas as pd
import pytest
from click.testing import CliRunner
from pandas.testing import assert_frame_equal

from disk_tree import blobfs
from disk_tree import listing_format as lf
from disk_tree import recompress as rc
from disk_tree.cli import cli
from disk_tree.cli.recompress import _hr
from disk_tree.storage.base import BLOB_ROW_GROUP_SIZE

ROOT = 'gcs://b1'
V1_COLUMNS = [
    'path', 'size', 'mtime', 'n_desc', 'n_files', 'n_children', 'kind', 'parent',
    'sum_storage_class_id_1', 'mtime_mean', 'uri', 'depth',
]


def v1_frame(n_dirs: int = 20, files_per_dir: int = 50, classes: int = 1) -> pd.DataFrame:
    """A v1 layer-2 listing frame: `n_dirs` dirs of `files_per_dir` files under
    the root, every column the duckdb writer emitted, in its order. With
    `classes=2` the bytes split across two class pivots (neither equals `size`)."""
    rows = []
    for d in range(n_dirs):
        for f in range(files_per_dir):
            size = 1000 + 7 * d + f
            rows.append({'path': f'd{d:03d}/f{f:04d}.bin', 'size': size, 'mtime': 1_700_000_000 + 60 * f, 'kind': 'file', 'parent': f'd{d:03d}', 'cls': (f % classes) + 1})
    files = pd.DataFrame(rows)
    files['n_desc'], files['n_files'], files['n_children'] = 0, 1, 0
    dirs = files.groupby('parent').agg(size=('size', 'sum'), mtime=('mtime', 'max'), n_desc=('size', 'count'), n_files=('size', 'count')).reset_index().rename(columns={'parent': 'path'})
    dirs['n_children'] = dirs['n_desc']
    dirs['kind'], dirs['parent'], dirs['cls'] = 'dir', '.', 0
    root = pd.DataFrame([{
        'path': '.', 'size': int(files['size'].sum()), 'mtime': int(files['mtime'].max()), 'n_desc': len(files) + len(dirs),
        'n_files': len(files), 'n_children': len(dirs), 'kind': 'dir', 'parent': '', 'cls': 0,
    }])
    df = pd.concat([root, dirs, files], ignore_index=True)
    for k in range(1, classes + 1):
        col = f'sum_storage_class_id_{k}'
        per_file = files['size'].where(files['cls'] == k, 0)
        by_dir = per_file.groupby(files['parent']).sum()
        df[col] = pd.concat([pd.Series([int(per_file.sum())]), by_dir.reindex(dirs['path']).reset_index(drop=True), per_file.reset_index(drop=True)], ignore_index=True).astype('int64')
    df['mtime_mean'] = (df['mtime'] * 1.0).where(df['kind'] == 'file', (df['mtime'] - 0.5))
    df['uri'] = (f'{ROOT}/' + df['path']).where(df['path'] != '.', ROOT)
    df['depth'] = df['path'].map(lambda p: 0 if p == '.' else p.count('/') + 1)
    df = df.sort_values(['depth', 'path']).reset_index(drop=True)
    cols = [c for c in V1_COLUMNS if c != 'sum_storage_class_id_1'] if classes != 1 else V1_COLUMNS
    if classes != 1:
        cols = cols[:8] + [f'sum_storage_class_id_{k}' for k in range(1, classes + 1)] + cols[8:]
    return df[cols]


def write_v1(df: pd.DataFrame, path: str, writer: str = 'duckdb', row_group_size: int = BLOB_ROW_GROUP_SIZE) -> str:
    """`df` as an old writer left it: `duckdb` = the engine's COPY (Snappy, no
    metadata at all); `pandas` = `to_parquet` (Snappy, a `pandas` metadata key)."""
    if writer == 'duckdb':
        if blobfs.is_url(path):
            raise ValueError('duckdb writer: local paths only (`blobfs.put` after)')
        duckdb.connect().execute(f"COPY (SELECT * FROM df) TO '{path}' (FORMAT PARQUET, COMPRESSION snappy, ROW_GROUP_SIZE {row_group_size})")
    else:
        blobfs.write_parquet(df, path, row_group_size)
    assert lf.format_of(path) == lf.V1
    return path


def _kv(path: str) -> dict[str, str]:
    return {k.decode(): v.decode() for k, v in blobfs.read_schema(path).metadata.items() if k not in (b'ARROW:schema', b'pandas')}


def _codecs(path: str) -> set[str]:
    pf, _, _ = rc._open(path)
    md = pf.metadata
    return {md.row_group(g).column(c).compression for g in range(md.num_row_groups) for c in range(md.num_columns)}


def _bytes(path: str) -> bytes:
    if blobfs.is_url(path):
        fs, p = blobfs.fs_for(path)
        return fs.cat(p)
    return Path(path).read_bytes()


def _ls(d: str) -> list[str]:
    if blobfs.is_url(d):
        fs, p = blobfs.fs_for(d)
        return sorted(x.rsplit('/', 1)[-1] for x in fs.ls(p, detail=False))
    return sorted(x.name for x in Path(d).iterdir())


@pytest.fixture(params=['snappy', 'zstd'])
def codec(request, monkeypatch) -> str:
    monkeypatch.setenv('DISK_TREE_PARQUET_CODEC', request.param)
    return request.param.upper()


@pytest.mark.parametrize('writer', ['duckdb', 'pandas'])
def test_v1_to_v2_lossless(tmp_path: Path, codec, writer):
    """TFFP: a v1 file becomes v2 in place (marker, root, implied pivot, v1
    column order recorded; `uri` and the pivot gone; the switch codec; ≤64K-row
    groups even from a 1M-row-group original), smaller, and reads back through
    `blobfs.read_parquet` as exactly the original frame."""
    df = v1_frame()
    path = write_v1(df, str(tmp_path / 'a.parquet'), writer, row_group_size=1 << 20)
    old = _bytes(path)
    r = CliRunner().invoke(cli, ['recompress', path])
    assert r.exit_code == 0, r.output
    new_size = (tmp_path / 'a.parquet').stat().st_size
    assert new_size < len(old)
    assert r.output.split('\n') == [
        f"{path}: {_hr(len(old))} → {_hr(new_size)} ({100 * new_size / len(old):.1f}%, {len(df):,} rows, root gcs://b1 implied sum_storage_class_id_1)",
        f"rewrote 1 file(s), {_hr(len(old))} → {_hr(new_size)} ({100 * new_size / len(old):.1f}%), 0 already v2",
        '',
    ]
    assert _ls(str(tmp_path)) == ['a.parquet']
    assert _kv(path) == {
        'disk_tree.listing_format': '2',
        'disk_tree.scan_root': 'gcs://b1',
        'disk_tree.implied': '{"sum_storage_class_id_1":"size"}',
        'disk_tree.columns': json.dumps(V1_COLUMNS, separators=(',', ':')),
    }
    assert blobfs.read_schema(path).names == [c for c in V1_COLUMNS if c not in ('uri', 'sum_storage_class_id_1')]
    assert _codecs(path) == {codec}
    assert blobfs.row_group_sizes(path) == [len(df)]
    assert_frame_equal(blobfs.read_parquet(path), df)


def test_row_groups_are_split_to_64k(tmp_path: Path):
    n = BLOB_ROW_GROUP_SIZE + 1000
    df = v1_frame(n_dirs=1, files_per_dir=n - 2)
    assert len(df) == n
    path = write_v1(df, str(tmp_path / 'a.parquet'), row_group_size=1 << 20)
    assert blobfs.row_group_sizes(path) == [n]
    assert rc.recompress(path).status == 'rewritten'
    assert blobfs.row_group_sizes(path) == [BLOB_ROW_GROUP_SIZE, 1000]
    assert_frame_equal(blobfs.read_parquet(path), df)


def test_two_classes_keep_both_pivots(tmp_path: Path):
    """Only a pivot equal to `size` on every row is implied; two real class
    columns are written as they were."""
    df = v1_frame(classes=2)
    path = write_v1(df, str(tmp_path / 'a.parquet'))
    assert rc.recompress(path).implied == {}
    assert _kv(path) == {
        'disk_tree.listing_format': '2',
        'disk_tree.scan_root': 'gcs://b1',
        'disk_tree.columns': json.dumps(list(df.columns), separators=(',', ':')),
    }
    assert blobfs.read_schema(path).names == [c for c in df.columns if c != 'uri']
    assert_frame_equal(blobfs.read_parquet(path), df)


def test_chunk_blob_without_root_row(tmp_path: Path):
    """A hybrid chunk (a subtree's rows, no `.` row) derives its scan root from
    any row's `uri`/`path` pair."""
    df = v1_frame(n_dirs=2, files_per_dir=3)
    chunk = df[df['path'].str.startswith('d001')].reset_index(drop=True)
    path = write_v1(chunk, str(tmp_path / 'c.parquet'))
    r = rc.recompress(path)
    assert (r.status, r.scan_root, r.rows) == ('rewritten', 'gcs://b1', 4)
    assert_frame_equal(blobfs.read_parquet(path), chunk)


def test_verify_failure_keeps_original(tmp_path: Path, monkeypatch):
    """A digest mismatch (here: the digest of the rewritten file is corrupted)
    leaves the original byte-identical, deletes the temp, and fails loudly."""
    df = v1_frame()
    path = write_v1(df, str(tmp_path / 'a.parquet'))
    old = _bytes(path)
    real = rc.digest
    calls = []

    def corrupt(batches, columns=rc.DIGEST_COLUMNS):
        n, d = real(batches, columns)
        calls.append(n)
        return (n, d) if len(calls) == 1 else (n, d ^ 1)

    monkeypatch.setattr(rc, 'digest', corrupt)
    r = CliRunner().invoke(cli, ['recompress', path])
    assert r.exit_code == 1
    assert calls == [len(df), len(df)]
    assert r.output.split('\n') == ['rewrote 0 file(s), 0 already v2, 1 failed', '']
    assert _ls(str(tmp_path)) == ['a.parquet']
    assert _bytes(path) == old
    assert lf.format_of(path) == lf.V1


def test_already_v2_is_skipped(tmp_path: Path):
    df = v1_frame()
    path = write_v1(df, str(tmp_path / 'a.parquet'))
    assert rc.recompress(path).status == 'rewritten'
    v2 = _bytes(path)
    r = CliRunner().invoke(cli, ['recompress', path])
    assert r.exit_code == 0, r.output
    assert r.output.split('\n') == [
        f"{path}: already v2 zstd ({_hr(len(v2))}, {len(df):,} rows)",
        'rewrote 0 file(s), 1 already v2',
        '',
    ]
    assert _bytes(path) == v2


def test_v2_is_recoded_when_the_codec_differs(tmp_path: Path, monkeypatch):
    """TFFP: a v2 file under a `$DISK_TREE_PARQUET_CODEC` other than its own is
    rewritten in place under the switch codec — same columns, same format keys,
    same frame — and is skipped once it matches. The v1 → v2 rewrite is the same
    verify-then-swap, so the codec flip re-runs over already-slim files."""
    df = v1_frame()
    path = write_v1(df, str(tmp_path / 'a.parquet'))
    monkeypatch.setenv('DISK_TREE_PARQUET_CODEC', 'snappy')
    assert rc.recompress(path).status == 'rewritten'
    kv, names, snappy = _kv(path), blobfs.read_schema(path).names, _bytes(path)
    assert _codecs(path) == {'SNAPPY'}
    monkeypatch.setenv('DISK_TREE_PARQUET_CODEC', 'zstd')
    r = CliRunner().invoke(cli, ['recompress', '-n', path])
    assert r.exit_code == 0, r.output
    assert r.output.split('\n') == [
        f"{path}: would rewrite ({_hr(len(snappy))}, {len(df):,} rows, root gcs://b1 implied sum_storage_class_id_1, snappy → zstd)",
        f"would rewrite 1 file(s), {_hr(len(snappy))} before, 0 already v2",
        '',
    ]
    assert _bytes(path) == snappy
    r = CliRunner().invoke(cli, ['recompress', path])
    assert r.exit_code == 0, r.output
    new_size = (tmp_path / 'a.parquet').stat().st_size
    assert new_size < len(snappy)
    assert r.output.split('\n') == [
        f"{path}: {_hr(len(snappy))} → {_hr(new_size)} ({100 * new_size / len(snappy):.1f}%, {len(df):,} rows, root gcs://b1 implied sum_storage_class_id_1, snappy → zstd)",
        f"rewrote 1 file(s), {_hr(len(snappy))} → {_hr(new_size)} ({100 * new_size / len(snappy):.1f}%), 0 already v2",
        '',
    ]
    assert _ls(str(tmp_path)) == ['a.parquet']
    assert _codecs(path) == {'ZSTD'}
    assert _kv(path) == kv
    assert blobfs.read_schema(path).names == names
    assert_frame_equal(blobfs.read_parquet(path), df)
    zstd = _bytes(path)
    r = CliRunner().invoke(cli, ['recompress', '-j', path])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)['results'] == [{
        'path': path, 'status': 'skipped', 'old_size': len(zstd), 'new_size': None, 'ratio': None, 'rows': len(df),
        'scan_root': 'gcs://b1', 'implied': {'sum_storage_class_id_1': 'size'}, 'kept': None, 'codec': 'zstd', 'recoded': None,
    }]
    assert _bytes(path) == zstd


def test_dry_run_writes_nothing(tmp_path: Path):
    df = v1_frame()
    path = write_v1(df, str(tmp_path / 'a.parquet'))
    old = _bytes(path)
    r = CliRunner().invoke(cli, ['recompress', '-n', str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert r.output.split('\n') == [
        f"{path}: would rewrite ({_hr(len(old))}, {len(df):,} rows, root gcs://b1 implied sum_storage_class_id_1)",
        f"would rewrite 1 file(s), {_hr(len(old))} before, 0 already v2",
        '',
    ]
    assert _ls(str(tmp_path)) == ['a.parquet']
    assert _bytes(path) == old


def test_keep_leaves_the_v1_file(tmp_path: Path):
    df = v1_frame()
    path = write_v1(df, str(tmp_path / 'a.parquet'))
    old = _bytes(path)
    r = CliRunner().invoke(cli, ['recompress', '-k', '-j', path])
    assert r.exit_code == 0, r.output
    kept = str(tmp_path / 'a.v1.parquet')
    new_size = (tmp_path / 'a.parquet').stat().st_size
    assert json.loads(r.output) == {
        'results': [{
            'path': path, 'status': 'rewritten', 'old_size': len(old), 'new_size': new_size, 'ratio': new_size / len(old),
            'rows': len(df), 'scan_root': 'gcs://b1', 'implied': {'sum_storage_class_id_1': 'size'}, 'kept': kept,
            'codec': 'zstd', 'recoded': None,
        }],
        'failures': [],
        'totals': {
            'rewritten': 1, 'planned': 0, 'skipped': 0, 'failed': 0,
            'old_size': len(old), 'new_size': new_size, 'ratio': new_size / len(old),
        },
    }
    assert _ls(str(tmp_path)) == ['a.parquet', 'a.v1.parquet']
    assert _bytes(kept) == old
    assert_frame_equal(blobfs.read_parquet(path), blobfs.read_parquet(kept))
    # a dir walk never picks the kept copy up again
    assert rc.expand([str(tmp_path)]) == [path]


def test_not_a_listing_is_refused(tmp_path: Path):
    """No `uri` column, or a `uri` that is not `<root>/<path>` on every row:
    refused, untouched, reported."""
    df = v1_frame(n_dirs=2, files_per_dir=3)
    no_uri = write_v1(df.drop(columns=['uri']), str(tmp_path / 'no_uri.parquet'))
    other = df.copy()
    other.loc[other['path'] == 'd001/f0002.bin', 'uri'] = 's3://elsewhere/d001/f0002.bin'
    mixed = write_v1(other, str(tmp_path / 'mixed.parquet'))
    before = {p: _bytes(p) for p in (no_uri, mixed)}
    r = CliRunner().invoke(cli, ['recompress', str(tmp_path)])
    assert r.exit_code == 1
    assert r.output.split('\n') == ['rewrote 0 file(s), 0 already v2, 2 failed', '']
    assert {p: _bytes(p) for p in (no_uri, mixed)} == before
    assert _ls(str(tmp_path)) == ['mixed.parquet', 'no_uri.parquet']
    with pytest.raises(rc.RecompressError, match=r"^not a v1 layer-2 listing: no `uri` \+ `path` columns$"):
        rc.recompress(no_uri)
    with pytest.raises(rc.RecompressError, match=r"^uri 's3://elsewhere/d001/f0002.bin' is not `gcs://b1/d001/f0002.bin`: not a listing under one scan root$"):
        rc.recompress(mixed)


def test_memory_url_dir(tmp_path: Path):
    """The same over a URL dir (`memory://`): recursive expansion, the temp
    beside the blob, copy-over swap, `--keep` rename."""
    pytest.importorskip('fsspec')
    d = f'memory://{uuid4()}'
    df = v1_frame()
    local = write_v1(df, str(tmp_path / 'a.parquet'))
    blobfs.put(local, f'{d}/sub/a.parquet')
    blobfs.write_parquet(df.head(3), f'{d}/sub/a.shallow.parquet', 64)
    old = _bytes(f'{d}/sub/a.parquet')
    assert rc.expand([d]) == [f'{d}/sub/a.parquet']
    r = rc.recompress(f'{d}/sub/a.parquet', keep=True)
    assert (r.status, r.old_size, r.kept) == ('rewritten', len(old), f'{d}/sub/a.v1.parquet')
    assert _ls(f'{d}/sub') == ['a.parquet', 'a.shallow.parquet', 'a.v1.parquet']
    assert _bytes(f'{d}/sub/a.v1.parquet') == old
    assert lf.format_of(f'{d}/sub/a.parquet') == lf.slim('gcs://b1', V1_COLUMNS, {'sum_storage_class_id_1': 'size'})
    assert_frame_equal(blobfs.read_parquet(f'{d}/sub/a.parquet'), df)
    assert rc.recompress(f'{d}/sub/a.parquet').status == 'skipped'


def test_digest_is_order_insensitive():
    import pyarrow as pa
    df = v1_frame(n_dirs=3, files_per_dir=4)
    t = pa.Table.from_pandas(df, preserve_index=False)
    fwd = rc.digest(iter(t.to_batches(max_chunksize=5)))
    rev = rc.digest(iter(t.take(list(range(len(df) - 1, -1, -1))).to_batches(max_chunksize=7)))
    assert fwd == rev
    assert fwd[0] == len(df)
    bumped = df.copy()
    bumped.loc[0, 'size'] += 1
    assert rc.digest(iter(pa.Table.from_pandas(bumped, preserve_index=False).to_batches())) != fwd


def test_listing_format(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('DISK_TREE_PARQUET_CODEC', 'zstd')
    df = v1_frame()
    v1 = write_v1(df, str(tmp_path / 'v1.parquet'))
    v2 = write_v1(df, str(tmp_path / 'v2.parquet'))
    assert rc.recompress(v2).status == 'rewritten'
    other = str(tmp_path / 'other.parquet')
    pd.DataFrame({'x': [1, 2]}).to_parquet(other)
    sizes = {p: Path(p).stat().st_size for p in (other, v1, v2)}
    r = CliRunner().invoke(cli, ['listing-format', str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert r.output.split('\n') == [
        f"{other}: not-a-listing snappy 1 group(s) 2 rows {_hr(sizes[other])}",
        f"{v1}: v1 snappy 1 group(s) {len(df):,} rows {_hr(sizes[v1])}",
        f"{v2}: v2 zstd 1 group(s) {len(df):,} rows {_hr(sizes[v2])} root gcs://b1 implied sum_storage_class_id_1",
        '',
    ]
    j = CliRunner().invoke(cli, ['listing-format', '-j', v1, v2])
    assert j.exit_code == 0, j.output
    assert json.loads(j.output) == [
        {
            'path': v1, 'listing': True, 'version': 1, 'codec': 'snappy', 'row_groups': 1, 'rows': len(df),
            'size': sizes[v1], 'columns': V1_COLUMNS, 'scan_root': None, 'implied': {},
        },
        {
            'path': v2, 'listing': True, 'version': 2, 'codec': 'zstd', 'row_groups': 1, 'rows': len(df),
            'size': sizes[v2], 'columns': [c for c in V1_COLUMNS if c not in ('uri', 'sum_storage_class_id_1')],
            'scan_root': 'gcs://b1', 'implied': {'sum_storage_class_id_1': 'size'},
        },
    ]


def test_url_source_handles_name_a_cache_and_are_closed(tmp_path: Path, monkeypatch):
    """gcsfs ≥ 2026.8.1 reads through a background prefetcher unless the caller
    names a cache, and a handle left to the interpreter-exit GC then blocks
    forever in its finalizer (a `recompress gs://…` printed its report and
    never exited). Over a URL, every read handle is opened with an explicit
    `cache_type` and is closed before the call returns — not left to a cycle."""
    from fsspec.implementations.memory import MemoryFileSystem
    # `(basename, cache_type, closed)` per read handle. `MemoryFile.close` is a
    # no-op (`.closed` never flips), so the close is observed on the wrapper.
    opened: list[list] = []
    orig = MemoryFileSystem.open

    def spy(self, path, mode='rb', **kw):
        f = orig(self, path, mode, **kw)
        if 'r' in mode:
            rec = [path.rsplit('/', 1)[-1], kw.get('cache_type'), False]
            opened.append(rec)
            close = f.close

            def observed():
                rec[2] = True
                close()
            f.close = observed
        return f

    monkeypatch.setattr(MemoryFileSystem, 'open', spy)
    src = write_v1(v1_frame(), str(tmp_path / 'v1.parquet'))
    url = f'memory://{uuid4()}/v1.parquet'
    blobfs.put(src, url)
    assert rc.info(url).version == 1
    assert rc.recompress(url).status == 'rewritten'
    assert rc.info(url).version == 2
    assert [tuple(rec) for rec in opened] == [
        ('v1.parquet', 'readahead', True),          # listing-format: footer
        ('v1.parquet', 'readahead', True),          # recompress: analyze + rewrite pass
        ('v1.parquet.v2.tmp', 'readahead', True),   # recompress: verify the rewrite
        ('v1.parquet', 'readahead', True),          # listing-format: footer, v2
    ]
