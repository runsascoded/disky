"""Tests for the opt-in aggregation extensions (spec: aggregation-extensions.md).

`--pivot-sum <col>` (per-category byte sums) + `--mean-mtime` (size-weighted
mean mtime), byte-identical across all 3 engines, exact expected values on a
hand-computed fixture, cardinality guard, no-flags regression (covered by the
untouched pre-existing suites).
"""

import datetime as dt
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from disk_tree.blobfs import read_parquet as read_listing
import pytest

from disk_tree.find.agg_ext import PIVOT_MAX
from disk_tree.find.aggregate_duckdb import aggregate_listing_to_parquet
from disk_tree.find.aggregate_stream import aggregate_stream
from disk_tree.find.import_listing import import_listing
from disk_tree.listing import prepare_listing

from test_aggregate_duckdb import _normalize as _normalize_base, NUMERIC


def _ts(day: int) -> dt.datetime:
    return dt.datetime(2026, 7, day, tzinfo=dt.timezone.utc)


# (name, size, created-day, storage_class_id) — covers: multi-class dirs,
# single-class dirs, a zero-byte file (excluded from mtime_mean terms), a
# zero-size-total dir (mtime_mean NULL), and multi-day mtimes.
_EXT_LISTING = [
    ('data/hot.bin',       1000, 10, 1),
    ('data/warm.bin',      2000, 20, 2),
    ('data/cold/old.bin',  4000, 5,  4),
    ('archive/a.bin',      8000, 1,  4),
    ('archive/b.bin',      8000, 3,  4),
    ('empty/marker',       0,    15, 1),
    ('top.txt',            500,  25, 1),
]


def _write_ext_listing(path: Path) -> str:
    rows = sorted(_EXT_LISTING)
    pd.DataFrame({
        'bucket': ['b1'] * len(rows),
        'name': [n for n, *_ in rows],
        'size_bytes': [s for _, s, *_ in rows],
        'created': [_ts(d) for *_, d, _ in rows],
        'storage_class_id': [c for *_, c in rows],
    }).to_parquet(path)
    return str(path)


EXT_COLS = ['sum_storage_class_id_1', 'sum_storage_class_id_2', 'sum_storage_class_id_4', 'mtime_mean']


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    base = _normalize_base(df)
    ext = df[EXT_COLS].astype({c: 'int64' for c in EXT_COLS[:-1]}).copy()
    ext = ext.loc[df[NUMERIC + ['path']].sort_values(['depth', 'path']).index].reset_index(drop=True)
    return pd.concat([base, ext], axis=1)


def _all_engines(tmp_path: Path, listing: str) -> dict[str, pd.DataFrame]:
    kw = dict(pivot_sums=('storage_class_id',), mean_mtime=True)
    out = {}
    out['pandas'] = import_listing((listing,), bucket='b1', scheme='gcs', **kw).df

    con = duckdb.connect()
    ddb = str(tmp_path / 'ddb.parquet')
    aggregate_listing_to_parquet(
        prepare_listing(con, (listing,)),
        bucket='b1', scheme='gcs', out_parquet=ddb, con=con, **kw,
    )
    out['duckdb'] = read_listing(ddb)

    stream = str(tmp_path / 'stream.parquet')
    aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=stream, **kw)
    out['stream'] = read_listing(stream)
    return out


def test_three_engine_identity_with_extensions(tmp_path: Path):
    listing = _write_ext_listing(tmp_path / 'l.parquet')
    got = {k: _normalize(v) for k, v in _all_engines(tmp_path, listing).items()}
    pd.testing.assert_frame_equal(got['pandas'], got['duckdb'])
    pd.testing.assert_frame_equal(got['pandas'], got['stream'])


def test_exact_values(tmp_path: Path):
    """Hand-computed expectations — the spec of the two extensions."""
    listing = _write_ext_listing(tmp_path / 'l.parquet')
    df = _normalize(_all_engines(tmp_path, listing)['pandas'])

    def row(p):
        r = df[df.path == p]
        assert len(r) == 1
        return r.iloc[0]

    ep = {d: int(_ts(d).timestamp()) for d in (1, 3, 5, 10, 15, 20, 25)}

    # data/: class 1 = 1000, class 2 = 2000, class 4 = 4000
    data = row('data')
    assert [int(data[c]) for c in EXT_COLS[:-1]] == [1000, 2000, 4000]
    # mtime_mean(data) = (ep10·1000 + ep20·2000 + ep5·4000) / 7000
    expect = float(ep[10] * 1000 + ep[20] * 2000 + ep[5] * 4000) / float(7000)
    assert float(data['mtime_mean']) == expect

    # archive/: all class 4; equal weights → midpoint of day1/day3
    arch = row('archive')
    assert [int(arch[c]) for c in EXT_COLS[:-1]] == [0, 0, 16000]
    assert float(arch['mtime_mean']) == float(ep[1] * 8000 + ep[3] * 8000) / float(16000)

    # empty/: only a zero-byte file → all sums 0, mtime_mean NULL
    empty = row('empty')
    assert [int(empty[c]) for c in EXT_COLS[:-1]] == [0, 0, 0]
    assert np.isnan(empty['mtime_mean'])

    # the zero-byte file itself: files carry their own mtime
    marker = row('empty/marker')
    assert float(marker['mtime_mean']) == float(ep[15])

    # a file row's pivot columns are its own contribution
    warm = row('data/warm.bin')
    assert [int(warm[c]) for c in EXT_COLS[:-1]] == [0, 2000, 0]

    # root: totals conserved across classes (class 4 = data/cold 4000 + archive 16000)
    root = row('.')
    assert [int(root[c]) for c in EXT_COLS[:-1]] == [1500, 2000, 20000]
    total_w = sum(int(_ts(d).timestamp()) * s for _, s, d, _ in _EXT_LISTING)
    total_s = sum(s for _, s, _, _ in _EXT_LISTING)
    assert float(root['mtime_mean']) == float(total_w) / float(total_s)


def test_no_flags_output_unchanged(tmp_path: Path):
    """Extensions off → no extra columns (schema regression guard)."""
    listing = _write_ext_listing(tmp_path / 'l.parquet')
    df = import_listing((listing,), bucket='b1', scheme='gcs').df
    assert [c for c in df.columns if c.startswith('sum_') or c == 'mtime_mean'] == []


def test_cardinality_guard(tmp_path: Path):
    n = PIVOT_MAX + 1
    listing = tmp_path / 'wide.parquet'
    pd.DataFrame({
        'bucket': ['b1'] * n,
        'name': [f'f{i:03d}' for i in range(n)],
        'size_bytes': [1] * n,
        'created': [_ts(1)] * n,
        'storage_class_id': list(range(n)),
    }).to_parquet(listing)

    match = rf"--pivot-sum storage_class_id: {n} distinct values"
    with pytest.raises(ValueError, match=match):
        import_listing((str(listing),), bucket='b1', scheme='gcs', pivot_sums=('storage_class_id',))
    con = duckdb.connect()
    with pytest.raises(ValueError, match=match):
        aggregate_listing_to_parquet(
            prepare_listing(con, (str(listing),)),
            bucket='b1', scheme='gcs', out_parquet=str(tmp_path / 'o.parquet'),
            con=con, pivot_sums=('storage_class_id',),
        )
    with pytest.raises(ValueError, match=match):
        aggregate_stream(
            (str(listing),), bucket='b1', scheme='gcs',
            out_parquet=str(tmp_path / 'o2.parquet'), pivot_sums=('storage_class_id',),
        )


def test_extensions_with_dirty_keys(tmp_path: Path):
    """Pivot/mean values must survive the `//` dirty-key side-merge in the
    stream engine (and the canonicalization in the other two)."""
    rows = pd.DataFrame({
        'bucket': ['b1'] * 3,
        'name': sorted(['tok/a//x.bin', 'tok/a/y.bin', 'other/z.bin']),
        'size_bytes': [100, 300, 500],
        'created': [_ts(2), _ts(4), _ts(6)],
        'storage_class_id': [1, 2, 1],
    }).sort_values('name')
    listing = tmp_path / 'l.parquet'
    rows.to_parquet(listing)

    kw = dict(pivot_sums=('storage_class_id',), mean_mtime=True)
    got_pandas = import_listing((str(listing),), bucket='b1', scheme='gcs', **kw).df
    out = str(tmp_path / 's.parquet')
    aggregate_stream((str(listing),), bucket='b1', scheme='gcs', out_parquet=out, **kw)
    got_stream = read_listing(out)

    cols = ['sum_storage_class_id_1', 'sum_storage_class_id_2', 'mtime_mean']
    norm = lambda d: pd.concat(
        [_normalize_base(d), d[cols].reset_index(drop=True)], axis=1,
    )
    pd.testing.assert_frame_equal(norm(got_pandas), norm(got_stream))


def test_mean_mtime_exact_at_scale_boundary(tmp_path: Path):
    """Σ mtime·size beyond int64: a 5 EB file (near the int64 size ceiling)
    makes mtime·size ≈ 9e27 — far past int64 max (~9.2e18), forcing the
    exact-integer (bigint/HUGEINT) path in every engine."""
    big = 5 * 10**18
    listing = tmp_path / 'big.parquet'
    pd.DataFrame({
        'bucket': ['b1'] * 2,
        'name': ['huge.bin', 'tiny.bin'],
        'size_bytes': [big, 1],
        'created': [_ts(10), _ts(20)],
        'storage_class_id': [1, 1],
    }).to_parquet(listing)

    kw = dict(mean_mtime=True)
    got_pandas = import_listing((str(listing),), bucket='b1', scheme='gcs', **kw).df
    out = str(tmp_path / 's.parquet')
    aggregate_stream((str(listing),), bucket='b1', scheme='gcs', out_parquet=out, **kw)
    got_stream = read_listing(out)
    con = duckdb.connect()
    ddb = str(tmp_path / 'd.parquet')
    aggregate_listing_to_parquet(
        prepare_listing(con, (str(listing),)),
        bucket='b1', scheme='gcs', out_parquet=ddb, con=con, **kw,
    )
    got_duckdb = read_listing(ddb)

    e10, e20 = int(_ts(10).timestamp()), int(_ts(20).timestamp())
    expect = float(e10 * big + e20 * 1) / float(big + 1)
    for name, df in [('pandas', got_pandas), ('duckdb', got_duckdb), ('stream', got_stream)]:
        root = df[df.path == '.'].iloc[0]
        assert float(root['mtime_mean']) == expect, name


def test_mtime_mean_hugeint_rounding(tmp_path):
    """`mt_wsum` ≥ 2^64 with an unlucky bit pattern: DuckDB's direct
    HUGEINT→DOUBLE cast rounds up 1 ULP where Python's int→float (the
    pandas/stream engines' `mean_of`) rounds to even. The duckdb engine must
    route the conversion through VARCHAR (correctly-rounded parse) so all
    three engines stay byte-identical.

    Fixture: one file, size=20_064_072_762, mtime=1_785_542_400 →
    wsum = 35_825_252_633_236_108_800 (65 bits, tie-ish pattern)."""
    import duckdb
    import pandas as pd
    from disk_tree.find.aggregate_stream import aggregate_stream
    from disk_tree.find.aggregate_duckdb import aggregate_listing_to_parquet
    from disk_tree.listing import prepare_listing

    listing = str(tmp_path / 'l.parquet')
    pd.DataFrame({
        'bucket': ['b1'],
        'name': ['d/f.bin'],
        'size_bytes': [20_064_072_762],
        'created': [pd.Timestamp(1_785_542_400, unit='s', tz='UTC')],
        'storage_class_id': [1],
    }).to_parquet(listing)
    exact = float(1_785_542_400 * 20_064_072_762) / float(20_064_072_762)

    out_s = str(tmp_path / 's.parquet')
    aggregate_stream((listing,), bucket='b1', scheme='s3', out_parquet=out_s, mean_mtime=True)
    con = duckdb.connect()
    out_d = str(tmp_path / 'd.parquet')
    aggregate_listing_to_parquet(
        prepare_listing(con, (listing,)), bucket='b1', scheme='s3',
        out_parquet=out_d, con=con, mean_mtime=True,
    )
    ds = read_listing(out_s).set_index('path')['mtime_mean']
    dd = read_listing(out_d).set_index('path')['mtime_mean']
    assert ds['d'] == exact
    assert dd['d'] == exact


@pytest.mark.parametrize('depth', [1, 2])
def test_extensions_survive_partitioned_cascade(tmp_path: Path, depth: int):
    """Pivot sums and the exact `mt_wsum` partial must fold across partition
    stubs exactly as they do through one cascade (spec mgu-scale-unification.md A.2)."""
    listing = _write_ext_listing(tmp_path / 'l.parquet')
    kw = dict(pivot_sums=('storage_class_id',), mean_mtime=True)
    con = duckdb.connect()
    base = str(tmp_path / 'base.parquet')
    aggregate_listing_to_parquet(
        prepare_listing(con, (listing,)), bucket='b1', scheme='gcs', out_parquet=base, con=con, **kw,
    )
    part = str(tmp_path / 'part.parquet')
    aggregate_listing_to_parquet(
        prepare_listing(con, (listing,)), bucket='b1', scheme='gcs', out_parquet=part, con=con,
        partition_depth=depth, **kw,
    )
    pd.testing.assert_frame_equal(_normalize(read_listing(base)), _normalize(read_listing(part)))


# ---------- Attribution slices as cascade group keys (spec mgu-scale-unification.md, item B) ----------

_LABEL_COLS = ['team', 'usr']
_LABEL_NUMERIC = ['size', 'mtime', 'n_desc', 'n_files', 'n_children', *EXT_COLS[:-1]]


def _write_labels(path: Path) -> str:
    """Deepest-prefix-wins fixture: `data/cold` overrides `data`; a trailing
    slash and a file-level prefix are both legal; `empty/` has no label."""
    pd.DataFrame({
        'prefix': ['data', 'data/cold/', 'archive', 'top.txt'],
        'team': ['t1', 't1', 't2', 't3'],
        'usr': ['alice', 'bob', None, 'carol'],
    }).to_parquet(path)
    return str(path)


def _run_labeled(tmp_path: Path, listing: str, labels: str | None, name: str, **kw) -> pd.DataFrame:
    con = duckdb.connect()
    out = str(tmp_path / f'{name}.parquet')
    aggregate_listing_to_parquet(
        prepare_listing(con, (listing,)), bucket='b1', scheme='gcs', out_parquet=out, con=con,
        pivot_sums=('storage_class_id',), mean_mtime=True, label=labels, **kw,
    )
    return read_listing(out)


def _slices(df: pd.DataFrame) -> list[tuple]:
    cols = ['path', *_LABEL_COLS, *_LABEL_NUMERIC]
    return [
        tuple(None if pd.isna(v) else (int(v) if isinstance(v, (int, float)) else v) for v in row)
        for row in df[cols].itertuples(index=False)
    ]


def test_label_slices_exact(tmp_path: Path):
    """The whole labeled layer-2, hand-computed: one row per (path, team, usr),
    in the output's own order `(depth, path, team, usr)` with NULLs first.
    `n_desc` counts a dir itself in the dir's own slice; `n_children` counts a
    child in the slice of the *child's* label."""
    listing = _write_ext_listing(tmp_path / 'l.parquet')
    df = _run_labeled(tmp_path, listing, _write_labels(tmp_path / 'labels.parquet'), 'labeled')
    ep = {d: int(_ts(d).timestamp()) for d in (1, 3, 5, 10, 15, 20, 25)}
    assert list(df.columns) == [
        'path', 'team', 'usr', 'size', 'mtime', 'n_desc', 'n_files', 'n_children', 'kind', 'parent',
        *EXT_COLS, 'uri', 'depth',
    ]
    #                                              size  mtime   n_desc n_files n_children  c1    c2    c4
    assert _slices(df) == [
        ('.',                 None, None,              0, ep[15],  3, 1, 1,      0,    0,     0),
        ('.',                 't1', 'alice',        3000, ep[20],  3, 2, 1,   1000, 2000,     0),
        ('.',                 't1', 'bob',          4000, ep[5],   2, 1, 0,      0,    0,  4000),
        ('.',                 't2', None,          16000, ep[3],   3, 2, 1,      0,    0, 16000),
        ('.',                 't3', 'carol',         500, ep[25],  1, 1, 1,    500,    0,     0),
        ('archive',           't2', None,          16000, ep[3],   3, 2, 2,      0,    0, 16000),
        ('data',              't1', 'alice',        3000, ep[20],  3, 2, 2,   1000, 2000,     0),
        ('data',              't1', 'bob',          4000, ep[5],   2, 1, 1,      0,    0,  4000),
        ('empty',             None, None,              0, ep[15],  2, 1, 1,      0,    0,     0),
        ('top.txt',           't3', 'carol',         500, ep[25],  1, 1, 0,    500,    0,     0),
        ('archive/a.bin',     't2', None,           8000, ep[1],   1, 1, 0,      0,    0,  8000),
        ('archive/b.bin',     't2', None,           8000, ep[3],   1, 1, 0,      0,    0,  8000),
        ('data/cold',         't1', 'bob',          4000, ep[5],   2, 1, 1,      0,    0,  4000),
        ('data/hot.bin',      't1', 'alice',        1000, ep[10],  1, 1, 0,   1000,    0,     0),
        ('data/warm.bin',     't1', 'alice',        2000, ep[20],  1, 1, 0,      0, 2000,     0),
        ('empty/marker',      None, None,              0, ep[15],  1, 1, 0,      0,    0,     0),
        ('data/cold/old.bin', 't1', 'bob',          4000, ep[5],   1, 1, 0,      0,    0,  4000),
    ]
    # mtime_mean per slice: Σ mtime·size / Σ size over the slice's own files.
    root = df[df.path == '.'].set_index(['team', 'usr'])['mtime_mean']
    assert float(root.loc[('t1', 'alice')]) == float(ep[10] * 1000 + ep[20] * 2000) / float(3000)
    assert float(root.loc[('t2', None)]) == float(ep[1] * 8000 + ep[3] * 8000) / float(16000)
    assert np.isnan(root.loc[(None, None)])


def test_label_slices_sum_to_unlabeled_rows(tmp_path: Path):
    """Acceptance B: Σ over a path's slices == the unlabeled path row, every
    additive column; `mtime` is the MAX."""
    listing = _write_ext_listing(tmp_path / 'l.parquet')
    base = _run_labeled(tmp_path, listing, None, 'base')
    lab = _run_labeled(tmp_path, listing, _write_labels(tmp_path / 'labels.parquet'), 'labeled')
    additive = [c for c in _LABEL_NUMERIC if c != 'mtime']
    folded = lab.groupby('path').agg({**{c: 'sum' for c in additive}, 'mtime': 'max'})
    expect = base.set_index('path')[[*additive, 'mtime']]
    pd.testing.assert_frame_equal(
        folded.sort_index().astype('int64'), expect.sort_index().astype('int64'),
    )
    assert len(lab) == 17
    assert len(base) == 12


@pytest.mark.parametrize('kw', [
    dict(partition_depth=1),
    dict(partition_depth=2),
    dict(partition_depth=2, db='.'),
], ids=['k1', 'k2', 'k2+db'])
def test_label_slices_survive_partitioning(tmp_path: Path, kw: dict):
    listing = _write_ext_listing(tmp_path / 'l.parquet')
    labels = _write_labels(tmp_path / 'labels.parquet')
    if kw.get('db') == '.':
        kw = {**kw, 'db': str(tmp_path)}
    one = _run_labeled(tmp_path, listing, labels, 'one')
    part = _run_labeled(tmp_path, listing, labels, 'part', **kw)
    pd.testing.assert_frame_equal(one, part)


def test_label_cols_subset_and_root_default(tmp_path: Path):
    """`label_cols` picks a subset; a `''` prefix is the catch-all default,
    still overridden by deeper prefixes."""
    listing = _write_ext_listing(tmp_path / 'l.parquet')
    labels = tmp_path / 'labels.parquet'
    pd.DataFrame({
        'prefix': ['', 'archive'],
        'team': ['pool', 't2'],
        'usr': ['nobody', 'x'],
    }).to_parquet(labels)
    df = _run_labeled(tmp_path, listing, str(labels), 'sub', label_cols=('team',))
    assert 'usr' not in df.columns
    assert [tuple(r) for r in df[df.depth <= 1][['path', 'team', 'size']].itertuples(index=False)] == [
        ('.', 'pool', 7500),
        ('.', 't2', 16000),
        ('archive', 't2', 16000),
        ('data', 'pool', 7000),
        ('empty', 'pool', 0),
        ('top.txt', 'pool', 500),
    ]


def test_label_table_validation(tmp_path: Path):
    listing = _write_ext_listing(tmp_path / 'l.parquet')

    def attempt(frame: pd.DataFrame, name: str, **kw):
        path = tmp_path / f'{name}.parquet'
        frame.to_parquet(path)
        return lambda: _run_labeled(tmp_path, listing, str(path), name, **kw)

    with pytest.raises(ValueError, match=r"no `prefix` column \(has \['team'\]\)"):
        attempt(pd.DataFrame({'team': ['t']}), 'noprefix')()
    with pytest.raises(ValueError, match=r"missing label column\(s\) \['usr'\]"):
        attempt(pd.DataFrame({'prefix': ['a'], 'team': ['t']}), 'missing', label_cols=('usr',))()
    with pytest.raises(ValueError, match=r"duplicate prefix\(es\) \['a'\]"):
        attempt(pd.DataFrame({'prefix': ['a', 'a/'], 'team': ['t', 'u']}), 'dupe')()
    with pytest.raises(ValueError, match=r"label column\(s\) \['size'\] collide"):
        attempt(pd.DataFrame({'prefix': ['a'], 'size': [1]}), 'clash')()
    with pytest.raises(ValueError, match=r"no label columns besides `prefix`"):
        attempt(pd.DataFrame({'prefix': ['a']}), 'bare')()


def test_label_requires_duckdb_engine(tmp_path: Path):
    from disk_tree.cli.import_listing import import_bucket
    with pytest.raises(ValueError, match="--label is a duckdb-engine feature; got engine='pandas'"):
        import_bucket(
            db=None, storage=None, con=None, engine='pandas', listings=('x',), bucket='b1',
            scheme='gcs', snap_time=_ts(1), label='labels.parquet',
        )


# ---------- Side table → subtree MAX columns (spec mgu-scale-unification.md, item D.4) ----------

def _write_side(path: Path) -> str:
    """A per-scan access state: `.` is the root (2a convention); a `bucket`
    column restricts the join; `top.txt` is a file with its own read."""
    pd.DataFrame({
        'bucket': ['b1', 'b1', 'b1', 'b1', 'b2'],
        'path': ['.', 'data/cold/old.bin', 'data/hot.bin', 'top.txt', 'archive'],
        'last_ts': [_ts(2), _ts(9), _ts(12), _ts(7), _ts(30)],
        'read_ops': [10, 1, 2, 3, 99],
    }).to_parquet(path)
    return str(path)


def _run_side(tmp_path: Path, listing: str, side: str | None, name: str, max_cols=('last_ts',), **kw) -> pd.DataFrame:
    con = duckdb.connect()
    out = str(tmp_path / f'{name}.parquet')
    aggregate_listing_to_parquet(
        prepare_listing(con, (listing,)), bucket='b1', scheme='gcs', out_parquet=out, con=con,
        pivot_sums=('storage_class_id',), mean_mtime=True, side=side, max_cols=max_cols, **kw,
    )
    return read_listing(out)


def test_side_max_col_is_subtree_max(tmp_path: Path):
    """`last_ts` per path = MAX over the path's own side row and its subtree;
    NULL where nothing beneath was read; the `b2` row is ignored."""
    listing = _write_ext_listing(tmp_path / 'l.parquet')
    df = _run_side(tmp_path, listing, _write_side(tmp_path / 'side.parquet'), 'side')
    assert list(df.columns) == [
        'path', 'size', 'mtime', 'n_desc', 'n_files', 'n_children', 'kind', 'parent',
        *EXT_COLS, 'last_ts', 'uri', 'depth',
    ]
    got = [(r.path, None if pd.isna(r.last_ts) else r.last_ts.to_pydatetime()) for r in df.itertuples()]
    assert got == [
        ('.', _ts(12)),
        ('archive', None),
        ('data', _ts(12)),
        ('empty', None),
        ('top.txt', _ts(7)),
        ('archive/a.bin', None),
        ('archive/b.bin', None),
        ('data/cold', _ts(9)),
        ('data/hot.bin', _ts(12)),
        ('data/warm.bin', None),
        ('empty/marker', None),
        ('data/cold/old.bin', _ts(9)),
    ]
    # Everything else is untouched by the side table.
    base = _run_side(tmp_path, listing, None, 'base', max_cols=())
    pd.testing.assert_frame_equal(df.drop(columns=['last_ts']), base)


def test_side_max_cols_with_labels_and_partitions(tmp_path: Path):
    """Two MAX columns, sliced by labels, under the partitioned cascade: each
    slice's max is over its own rows (slices are disjoint), identical for every
    partition depth."""
    listing = _write_ext_listing(tmp_path / 'l.parquet')
    side = _write_side(tmp_path / 'side.parquet')
    labels = _write_labels(tmp_path / 'labels.parquet')
    one = _run_side(tmp_path, listing, side, 'one', max_cols=('last_ts', 'read_ops'), label=labels)
    for k in (1, 2):
        part = _run_side(tmp_path, listing, side, f'k{k}', max_cols=('last_ts', 'read_ops'), label=labels, partition_depth=k)
        pd.testing.assert_frame_equal(one, part)
    # A missing label reads back as None or NaN depending on pandas' string
    # dtype (3.x: NaN); the spec is "no label", so both normalize to None.
    rows = [
        (r.path, _none(r.team), _none(r.usr), None if pd.isna(r.last_ts) else r.last_ts.to_pydatetime(),
         None if pd.isna(r.read_ops) else int(r.read_ops))
        for r in one[one.depth <= 1].itertuples()
    ]
    assert rows == [
        # the root's own read (10 ops, day 2) lands in the root dir's own slice (NULL, NULL)
        ('.', None, None, _ts(2), 10),
        ('.', 't1', 'alice', _ts(12), 2),
        ('.', 't1', 'bob', _ts(9), 1),
        ('.', 't2', None, None, None),
        ('.', 't3', 'carol', _ts(7), 3),
        ('archive', 't2', None, None, None),
        ('data', 't1', 'alice', _ts(12), 2),
        ('data', 't1', 'bob', _ts(9), 1),
        ('empty', None, None, None, None),
        ('top.txt', 't3', 'carol', _ts(7), 3),
    ]


def test_side_validation(tmp_path: Path):
    listing = _write_ext_listing(tmp_path / 'l.parquet')
    side = _write_side(tmp_path / 'side.parquet')
    with pytest.raises(ValueError, match="--max-col needs --side"):
        _run_side(tmp_path, listing, None, 'x')
    with pytest.raises(ValueError, match="--side needs at least one --max-col"):
        _run_side(tmp_path, listing, side, 'x', max_cols=())
    with pytest.raises(ValueError, match=r"side table .*: missing column\(s\) \['nope'\]"):
        _run_side(tmp_path, listing, side, 'x', max_cols=('nope',))
    with pytest.raises(ValueError, match=r"side column\(s\) \['size'\] collide"):
        pd.DataFrame({'path': ['.'], 'size': [1]}).to_parquet(tmp_path / 'clash.parquet')
        _run_side(tmp_path, listing, str(tmp_path / 'clash.parquet'), 'x', max_cols=('size',))
    pd.DataFrame({'path': ['a', 'a'], 'last_ts': [_ts(1), _ts(2)]}).to_parquet(tmp_path / 'dupe.parquet')
    with pytest.raises(ValueError, match=r"duplicate path\(s\) \['a'\]"):
        _run_side(tmp_path, listing, str(tmp_path / 'dupe.parquet'), 'x')
    pd.DataFrame({'p': ['a']}).to_parquet(tmp_path / 'nopath.parquet')
    with pytest.raises(ValueError, match=r"no `path` column \(has \['p'\]\)"):
        _run_side(tmp_path, listing, str(tmp_path / 'nopath.parquet'), 'x', max_cols=('p',))
    from disk_tree.cli.import_listing import import_bucket
    with pytest.raises(ValueError, match="--side/--max-col is a duckdb-engine feature; got engine='stream'"):
        import_bucket(
            db=None, storage=None, con=None, engine='stream', listings=('x',), bucket='b1',
            scheme='gcs', snap_time=_ts(1), side=side, max_cols=('last_ts',),
        )


# ---------- Size histogram column (spec mgu-scale-unification.md, item E) ----------

from disk_tree.find.agg_ext import SIZE_HIST_BINS, size_bin


def _none(x):
    """Missing → None (pandas 3's default string dtype reads a NULL as NaN)."""
    return None if pd.isna(x) else x

# Sizes at every edge that matters: 0, both sides of several powers of two,
# both sides of the last closed bin's top (2^39) and the open-ended last bin.
_HIST_LISTING = [
    ('a/zero', 0), ('a/one', 1), ('a/two', 2), ('a/three', 3), ('a/four', 4),
    ('a/b/seven', 7), ('a/b/eight', 8), ('a/b/k1', 1023), ('a/b/k2', 1024),
    ('c/p39m', (1 << 39) - 1), ('c/p39', 1 << 39), ('c/p39p', (1 << 39) + 1),
    ('c/bigger', (1 << 40) + 5), ('c/d/huge', 1 << 62),
    ('top', 5),
]


def test_size_bin():
    assert [size_bin(s) for _, s in _HIST_LISTING] == [0, 1, 2, 2, 3, 3, 4, 10, 11, 39, 40, 40, 40, 40, 3]
    assert size_bin((1 << 53) + 1) == 40
    assert SIZE_HIST_BINS == 41
    with pytest.raises(ValueError, match="negative size -1"):
        size_bin(-1)


def _expected_hists() -> dict[str, tuple[list[int], list[int]]]:
    """Direct computation over the listing, per path (acceptance E)."""
    out: dict[str, tuple[list[int], list[int]]] = {}
    for name, size in _HIST_LISTING:
        b = min(size_bin(size), SIZE_HIST_BINS - 1)
        parts = name.split('/')
        paths = ['.'] + ['/'.join(parts[:i]) for i in range(1, len(parts) + 1)]
        for p in paths:
            n, by = out.setdefault(p, ([0] * SIZE_HIST_BINS, [0] * SIZE_HIST_BINS))
            n[b] += 1
            by[b] += size
    return out


def test_size_hist_matches_direct_computation(tmp_path: Path):
    listing = tmp_path / 'l.parquet'
    pd.DataFrame({
        'bucket': ['b1'] * len(_HIST_LISTING),
        'name': [n for n, _ in _HIST_LISTING],
        'size_bytes': [s for _, s in _HIST_LISTING],
        'created': [_ts(1)] * len(_HIST_LISTING),
        'storage_class_id': [1] * len(_HIST_LISTING),
    }).to_parquet(listing)
    con = duckdb.connect()
    out = str(tmp_path / 'h.parquet')
    aggregate_listing_to_parquet(
        prepare_listing(con, (str(listing),)), bucket='b1', scheme='gcs', out_parquet=out, con=con,
        size_hist=True, pivot_sums=('storage_class_id',),
    )
    df = read_listing(out)
    assert list(df.columns) == [
        'path', 'size', 'mtime', 'n_desc', 'n_files', 'n_children', 'kind', 'parent',
        'sum_storage_class_id_1', 'size_hist_n', 'size_hist_bytes', 'uri', 'depth',
    ]
    got = {r.path: ([int(v) for v in r.size_hist_n], [int(v) for v in r.size_hist_bytes]) for r in df.itertuples()}
    assert sorted(got) == sorted(_expected_hists())
    assert got == _expected_hists()
    # A file's histogram is its own single bin; the top closed bin's edge is exact.
    assert got['c/p39m'][0].index(1) == 39 and got['c/p39'][0].index(1) == 40 and got['c/p39p'][0].index(1) == 40
    assert got['c/d/huge'] == ([0] * 40 + [1], [0] * 40 + [1 << 62])
    assert got['c'] == (
        [0] * 39 + [1, 4],
        [0] * 39 + [(1 << 39) - 1, (1 << 39) + ((1 << 39) + 1) + ((1 << 40) + 5) + (1 << 62)],
    )
    # Root totals reconcile with the plain columns.
    root = df[df.path == '.'].iloc[0]
    assert sum(got['.'][0]) == int(root['n_files']) == len(_HIST_LISTING)
    assert sum(got['.'][1]) == int(root['size']) == sum(s for _, s in _HIST_LISTING)
    # Without the flag: no histogram columns (schema regression guard).
    plain = str(tmp_path / 'p.parquet')
    aggregate_listing_to_parquet(
        prepare_listing(con, (str(listing),)), bucket='b1', scheme='gcs', out_parquet=plain, con=con,
    )
    assert [c for c in read_listing(plain).columns if c.startswith('size_hist')] == []


def test_size_hist_under_labels_and_partitions(tmp_path: Path):
    listing = _write_ext_listing(tmp_path / 'l.parquet')
    labels = _write_labels(tmp_path / 'labels.parquet')
    con = duckdb.connect()

    def run(name: str, **kw) -> pd.DataFrame:
        out = str(tmp_path / f'{name}.parquet')
        aggregate_listing_to_parquet(
            prepare_listing(con, (listing,)), bucket='b1', scheme='gcs', out_parquet=out, con=con,
            size_hist=True, mean_mtime=True, label=labels, **kw,
        )
        return read_listing(out)

    one = run('one')
    for k in (1, 2):
        pd.testing.assert_frame_equal(one, run(f'k{k}', partition_depth=k))
    # Each slice's histogram is over its own files: data/(t1,alice) = hot 1000 (bin 10) + warm 2000 (bin 11).
    row = one[(one.path == 'data') & (one.usr == 'alice')].iloc[0]
    n = [int(v) for v in row['size_hist_n']]
    by = [int(v) for v in row['size_hist_bytes']]
    assert (n[10], n[11], sum(n)) == (1, 1, 2)
    assert (by[10], by[11], sum(by)) == (1000, 2000, 3000)


def test_size_hist_requires_duckdb_engine():
    from disk_tree.cli.import_listing import import_bucket
    with pytest.raises(ValueError, match="--size-hist is a duckdb-engine feature; got engine='pandas'"):
        import_bucket(
            db=None, storage=None, con=None, engine='pandas', listings=('x',), bucket='b1',
            scheme='gcs', snap_time=_ts(1), size_hist=True,
        )
