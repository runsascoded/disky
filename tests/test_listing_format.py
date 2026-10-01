"""v2 layer-2 listings (spec `listing-slim.md` phase 1): what the duckdb and
stream engines write (no `uri`, single-valued pivots implied, the switch codec, format in
the key-value metadata), and that every reader sees the v1 shape."""
import datetime as dt
import json
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow.parquet as pq
import pytest

from disk_tree import listing_format as lf
from disk_tree.blobfs import read_parquet
from disk_tree.find.aggregate_duckdb import aggregate_listing_to_parquet
from disk_tree.find.aggregate_stream import aggregate_stream
from disk_tree.find.import_listing import import_listing
from disk_tree.find.tiers import write_tiers
from disk_tree.listing import prepare_listing

TS = dt.datetime(2026, 7, 28, tzinfo=dt.timezone.utc)
NAMES = ['a.txt', 'sub/b.txt', 'sub/c.txt', 'sub/deep/d.txt', 'other/e.txt']
SIZES = [100, 200, 300, 400, 50]
BASE = ['path', 'size', 'mtime', 'n_desc', 'n_files', 'n_children', 'kind', 'parent']


@pytest.fixture(params=['snappy', 'zstd'])
def codec(request, monkeypatch) -> str:
    """Every check runs under both `$DISK_TREE_PARQUET_CODEC` values; returns
    the parquet footer's codec name."""
    monkeypatch.setenv('DISK_TREE_PARQUET_CODEC', request.param)
    return request.param.upper()


def _listing(path: Path, classes: list) -> str:
    pd.DataFrame({
        'bucket': ['b1'] * len(NAMES),
        'name': NAMES,
        'size_bytes': SIZES,
        'created': [TS] * len(NAMES),
        'storage_class_id': pd.array(classes, dtype='Int64'),
    }).to_parquet(path)
    return str(path)


def _duckdb(listing: str, out: Path) -> str:
    con = duckdb.connect()
    aggregate_listing_to_parquet(
        prepare_listing(con, (listing,)), bucket='b1', scheme='gcs', out_parquet=str(out), con=con,
        pivot_sums=('storage_class_id',), mean_mtime=True,
    )
    return str(out)


def _stream(listing: str, out: Path) -> str:
    aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=str(out), pivot_sums=('storage_class_id',), mean_mtime=True)
    return str(out)


def _kv(path: str) -> dict[str, str]:
    return {k.decode(): v.decode() for k, v in pq.read_metadata(path).metadata.items() if k != b'ARROW:schema'}


def _codecs(path: str) -> set[str]:
    md = pq.read_metadata(path)
    return {md.row_group(g).column(c).compression for g in range(md.num_row_groups) for c in range(md.num_columns)}


@pytest.mark.parametrize('engine', [_duckdb, _stream])
@pytest.mark.parametrize('classes, written, implied', [
    # One class, no NULLs: the pivot equals `size` everywhere → implied, not written.
    ([1, 1, 1, 1, 1], [], {'sum_storage_class_id_1': 'size'}),
    # Two classes: both written.
    ([1, 2, 1, 2, 1], ['sum_storage_class_id_1', 'sum_storage_class_id_2'], {}),
    # One class plus a NULL: the pivot misses the NULL row's bytes → written.
    ([1, 1, None, 1, 1], ['sum_storage_class_id_1'], {}),
])
def test_v2_shape(tmp_path: Path, codec, engine, classes, written, implied):
    out = engine(_listing(tmp_path / 'l.parquet', classes), tmp_path / 'out.parquet')
    v1_pivots = sorted({*written, *implied})
    assert pq.read_schema(out).names == [*BASE, *written, 'mtime_mean', 'depth']
    assert _kv(out) == {
        'disk_tree.listing_format': '2',
        'disk_tree.scan_root': 'gcs://b1',
        **({'disk_tree.implied': json.dumps(implied, separators=(',', ':'))} if implied else {}),
        'disk_tree.columns': json.dumps([*BASE, *v1_pivots, 'mtime_mean', 'uri', 'depth'], separators=(',', ':')),
    }
    assert _codecs(out) == {codec}


@pytest.mark.parametrize('classes', [[1, 1, 1, 1, 1], [1, 2, 1, 2, 1]])
def test_restored_v2_is_the_v1_frame(tmp_path: Path, codec, classes):
    """Both engines' v2 output reads back (`blobfs.read_parquet`) as exactly the
    pandas engine's v1 frame: `uri`, the implied pivot, the v1 column order."""
    listing = _listing(tmp_path / 'l.parquet', classes)
    ref = import_listing((listing,), bucket='b1', scheme='gcs', pivot_sums=('storage_class_id',), mean_mtime=True).df
    for engine in (_duckdb, _stream):
        got = read_parquet(engine(listing, tmp_path / f'{engine.__name__}.parquet'))
        assert list(got.columns) == json.loads(_kv(str(tmp_path / f'{engine.__name__}.parquet'))['disk_tree.columns'])
        want = ref[list(got.columns)].sort_values(['depth', 'path']).reset_index(drop=True)
        pd.testing.assert_frame_equal(got.reset_index(drop=True), want, check_dtype=False)
    uris = read_parquet(str(tmp_path / '_duckdb.parquet'), columns=['path', 'uri'])
    assert uris.values.tolist() == [
        ['.', 'gcs://b1'], ['a.txt', 'gcs://b1/a.txt'], ['other', 'gcs://b1/other'], ['sub', 'gcs://b1/sub'],
        ['other/e.txt', 'gcs://b1/other/e.txt'], ['sub/b.txt', 'gcs://b1/sub/b.txt'], ['sub/c.txt', 'gcs://b1/sub/c.txt'],
        ['sub/deep', 'gcs://b1/sub/deep'], ['sub/deep/d.txt', 'gcs://b1/sub/deep/d.txt'],
    ]


def test_tiers_from_either_format(tmp_path: Path, codec):
    """Tiers cut from a v1 layer-2 and from the v2 layer-2 of the same input read
    back row-for-row identical (bytes differ: the v2 tiers carry no `uri` column
    and the v2 format keys)."""
    v2 = _duckdb(_listing(tmp_path / 'l.parquet', [1, 1, 1, 1, 1]), tmp_path / 'v2.parquet')
    v1 = str(tmp_path / 'v1.parquet')
    v1_df = read_parquet(v2)
    duckdb.connect().execute(f"COPY (SELECT * FROM v1_df) TO '{v1}' (FORMAT PARQUET, ROW_GROUP_SIZE 65536)")
    assert lf.format_of(v1) == lf.V1
    w1 = write_tiers(v1, str(tmp_path / 't1'))
    w2 = write_tiers(v2, str(tmp_path / 't2'))
    assert list(w1.values()) == list(w2.values()) == [9, 9]
    for a, b in zip(w1, w2):
        pd.testing.assert_frame_equal(read_parquet(a), read_parquet(b))
        assert _codecs(a) == _codecs(b) == {codec}
        assert lf.format_of(b) == lf.format_of(v2)
