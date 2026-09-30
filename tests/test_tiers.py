"""Layer-2 as the path store's sorts (spec path-store.md §1.2, §4.1).

Each tier is the finished layer-2 blob, every row, re-sorted: `path` on
`(depth, path)`, `bysize` on `(⌊log2 size⌋ desc, path)` with size 0 last. The
rows are the same rows (exact sums), the sort is as declared, row groups are
bounded, and the sort is readable from the parquet metadata.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow.parquet as pq
import pytest

from disk_tree.find.aggregate_duckdb import aggregate_listing_to_parquet
from disk_tree.find.tiers import connect, ROW_GROUP_STEP, parse_tiers, size_bucket, tier_path, write_tiers
from disk_tree.listing import prepare_listing

TS = dt.datetime(2026, 7, 28, tzinfo=dt.timezone.utc)

# 6 top-level dirs of 5 files each: file `d<d>/f<i>.bin` is (i+1)·(d+1) MiB,
# so sizes straddle several log2 buckets. Total = 6 × (1+2+3+4+5) × unit.
_UNIT = 1 << 20
_LISTING = [
    (f'd{d}/f{i}.bin', (i + 1) * _UNIT * (d + 1))
    for d in range(6)
    for i in range(5)
]


#: A tier inherits its v2 layer-2's listing-format metadata (spec `listing-slim.md`).
V2_KV = {
    'disk_tree.listing_format': '2',
    'disk_tree.scan_root': 'gcs://b1',
    'disk_tree.columns': '["path","size","mtime","n_desc","n_files","n_children","kind","parent","uri","depth"]',
}


def _listing(tmp_path: Path, rows: list[tuple[str, int]], name: str = 'l.parquet') -> Path:
    listing = tmp_path / name
    pd.DataFrame({
        'bucket': ['b1'] * len(rows),
        'name': [n for n, _ in rows],
        'size_bytes': [s for _, s in rows],
        'created': [TS] * len(rows),
        'storage_class_id': [1] * len(rows),
    }).to_parquet(listing)
    return listing


def _layer2(tmp_path: Path, labels: str | None = None, rows: list[tuple[str, int]] = _LISTING) -> str:
    listing = _listing(tmp_path, rows)
    out = str(tmp_path / 'layer2.parquet')
    con = duckdb.connect()
    aggregate_listing_to_parquet(
        prepare_listing(con, (str(listing),)), bucket='b1', scheme='gcs', out_parquet=out, con=con,
        label=labels,
    )
    return out


def _rows(df: pd.DataFrame, cols: list[str]) -> list[tuple]:
    """`cols` per row as tuples, a NULL as None (pandas 3's string dtype reads
    a NULL back as NaN; the spec is "no value")."""
    return [tuple(None if pd.isna(v) else v for v in row) for row in df[cols].itertuples(index=False)]


def _kv(path: str) -> dict[str, str]:
    return {k.decode(): v.decode() for k, v in pq.read_metadata(path).metadata.items() if k != b'ARROW:schema'}


def _bysize_order(df: pd.DataFrame) -> pd.DataFrame:
    """The `bysize` spec in pandas: bucket desc (None last), then path."""
    b = df['size'].map(size_bucket)
    key = b.fillna(-1).astype(int)
    return df.assign(_b=key).sort_values(['_b', 'path'], ascending=[False, True]).drop(columns='_b').reset_index(drop=True)


def test_size_bucket():
    assert [size_bucket(s) for s in (None, 0, 1, 2, 3, 1023, 1024, (1 << 62) - 1, 1 << 62)] == [
        None, None, 0, 1, 1, 9, 10, 61, 62,
    ]


def test_parse_tiers():
    assert parse_tiers('path,bysize') == ('path', 'bysize')
    assert parse_tiers('bysize') == ('bysize',)
    # The retired names are errors, not silent no-ops.
    with pytest.raises(ValueError, match=r"unknown tier\(s\) \['dirs', 'coarse'\]"):
        parse_tiers('path,dirs,coarse')
    with pytest.raises(ValueError, match="tier repeated in 'path,path'"):
        parse_tiers('path,path')


def test_tier_paths():
    assert tier_path('/x/gcs-b1', 'path') == '/x/gcs-b1.path.parquet'
    assert tier_path('/x/gcs-b1', 'bysize', ('usr',)) == '/x/gcs-b1.bysize-by-usr.parquet'
    assert tier_path('/x/gcs-b1', 'path', ('team', 'usr')) == '/x/gcs-b1.path-by-team-usr.parquet'


def test_tiers_are_every_layer2_row_in_the_declared_sort(tmp_path: Path):
    layer2 = _layer2(tmp_path)
    stem = str(tmp_path / 'gcs-b1')
    written = write_tiers(layer2, stem)
    assert written == {
        f'{stem}.path.parquet': 37,
        f'{stem}.bysize.parquet': 37,
    }
    full = pd.read_parquet(layer2)
    cols = list(full.columns)

    path = pd.read_parquet(f'{stem}.path.parquet')
    assert list(path.columns) == cols
    pd.testing.assert_frame_equal(path, full.sort_values(['depth', 'path']).reset_index(drop=True))
    # Objects are rows here, with their kind, after the dir rows of each depth.
    assert _rows(path, ['path', 'kind'])[:9] == [
        ('.', 'dir'), ('d0', 'dir'), ('d1', 'dir'), ('d2', 'dir'), ('d3', 'dir'), ('d4', 'dir'), ('d5', 'dir'),
        ('d0/f0.bin', 'file'), ('d0/f1.bin', 'file'),
    ]
    assert _kv(f'{stem}.path.parquet') == {'tier': 'path', 'sort': 'depth,path', **V2_KV}

    bysize = pd.read_parquet(f'{stem}.bysize.parquet')
    assert list(bysize.columns) == cols
    pd.testing.assert_frame_equal(bysize, _bysize_order(full))
    # Bucket order, biggest first, path order within a bucket — dirs and
    # objects interleaved by size alone: the root (315 MiB → 2^28), then the
    # two 2^26 dirs, the two 2^25 dirs, then 2^24 holds `d1` beside the files
    # of that size.
    assert [(size_bucket(s), p, k) for p, s, k in _rows(bysize, ['path', 'size', 'kind'])][:14] == [
        (28, '.', 'dir'),
        (26, 'd4', 'dir'), (26, 'd5', 'dir'),
        (25, 'd2', 'dir'), (25, 'd3', 'dir'),
        (24, 'd1', 'dir'), (24, 'd3/f3.bin', 'file'), (24, 'd3/f4.bin', 'file'), (24, 'd4/f3.bin', 'file'),
        (24, 'd4/f4.bin', 'file'), (24, 'd5/f2.bin', 'file'), (24, 'd5/f3.bin', 'file'), (24, 'd5/f4.bin', 'file'),
        (23, 'd0', 'dir'),
    ]
    assert _rows(bysize, ['path'])[-5:] == [('d0/f1.bin',), ('d0/f2.bin',), ('d1/f0.bin',), ('d2/f0.bin',), ('d0/f0.bin',)]
    assert _kv(f'{stem}.bysize.parquet') == {'tier': 'bysize', 'sort': 'size_bucket desc,path', 'bucket': 'log2', **V2_KV}


def test_bysize_puts_empty_rows_last(tmp_path: Path):
    """Size 0 has no bucket: those rows close the file, in path order, after
    every sized row; an empty dir (holding only empty objects) is one of them."""
    rows = [('a/x.bin', 4), ('a/y.bin', 0), ('e/n.bin', 0), ('z.bin', 1), ('b/big.bin', 1 << 30)]
    layer2 = _layer2(tmp_path, rows=rows)
    stem = str(tmp_path / 'gcs-b1')
    assert write_tiers(layer2, stem, tiers=('bysize',)) == {f'{stem}.bysize.parquet': 9}
    bysize = pd.read_parquet(f'{stem}.bysize.parquet')
    assert [(size_bucket(s), p) for p, s in _rows(bysize, ['path', 'size'])] == [
        (30, '.'), (30, 'b'), (30, 'b/big.bin'),
        (2, 'a'), (2, 'a/x.bin'),
        (0, 'z.bin'),
        (None, 'a/y.bin'), (None, 'e'), (None, 'e/n.bin'),
    ]


def test_row_groups_are_bounded(tmp_path: Path):
    """5000 objects (+ root + `flat`) at 2048 rows per group: `[2048, 2048, 906]`
    — the bound is exact for multiples of DuckDB's vector size, which
    `write_tiers` insists on."""
    n = 5000
    layer2 = _layer2(tmp_path, rows=[(f'flat/f{i:05d}', 1) for i in range(n)])
    stem = str(tmp_path / 'gcs-b1')
    assert write_tiers(layer2, stem, row_group_rows=ROW_GROUP_STEP) == {
        f'{stem}.path.parquet': n + 2,
        f'{stem}.bysize.parquet': n + 2,
    }
    md = pq.read_metadata(f'{stem}.path.parquet')
    assert [md.row_group(i).num_rows for i in range(md.num_row_groups)] == [2048, 2048, 906]
    assert pd.read_parquet(f'{stem}.path.parquet')['path'].tolist() == ['.', 'flat', *(f'flat/f{i:05d}' for i in range(n))]
    md = pq.read_metadata(f'{stem}.bysize.parquet')
    assert [md.row_group(i).num_rows for i in range(md.num_row_groups)] == [2048, 2048, 906]
    # Both 5000-byte rows (bucket 12) lead; the 1-byte objects (bucket 0) follow in path order.
    assert pd.read_parquet(f'{stem}.bysize.parquet')['path'].tolist() == ['.', 'flat', *(f'flat/f{i:05d}' for i in range(n))]


def test_sort_variants_over_label_slices(tmp_path: Path):
    """With label slices the sort closes on the label columns; a variant leads
    with them, on both tiers."""
    labels = tmp_path / 'labels.parquet'
    pd.DataFrame({'prefix': ['d0', 'd1', 'd3', 'd5'], 'usr': ['c', 'a', 'b', 'a']}).to_parquet(labels)
    layer2 = _layer2(tmp_path, labels=str(labels))
    stem = str(tmp_path / 'gcs-b1')
    written = write_tiers(layer2, stem, sort_variants=(('usr',),))
    assert list(written) == [
        f'{stem}.path.parquet', f'{stem}.path-by-usr.parquet',
        f'{stem}.bysize.parquet', f'{stem}.bysize-by-usr.parquet',
    ]
    assert {os.path.basename(p): n for p, n in written.items()} == {
        'gcs-b1.path.parquet': 40, 'gcs-b1.path-by-usr.parquet': 40,
        'gcs-b1.bysize.parquet': 40, 'gcs-b1.bysize-by-usr.parquet': 40,
    }
    assert {t: _kv(f'{stem}.{t}.parquet')['sort'] for t in ('path', 'path-by-usr', 'bysize', 'bysize-by-usr')} == {
        'path': 'depth,path,usr',
        'path-by-usr': 'usr,depth,path',
        'bysize': 'size_bucket desc,path,usr',
        'bysize-by-usr': 'usr,size_bucket desc,path',
    }
    path = pd.read_parquet(f'{stem}.path.parquet')
    assert list(path.columns[:3]) == ['path', 'usr', 'size']
    dirs = path[path.kind == 'dir']
    assert _rows(dirs, ['path', 'usr', 'size']) == [
        ('.', None, 120 * _UNIT), ('.', 'a', 120 * _UNIT), ('.', 'b', 60 * _UNIT), ('.', 'c', 15 * _UNIT),
        ('d0', 'c', 15 * _UNIT), ('d1', 'a', 30 * _UNIT), ('d2', None, 45 * _UNIT),
        ('d3', 'b', 60 * _UNIT), ('d4', None, 75 * _UNIT), ('d5', 'a', 90 * _UNIT),
    ]
    by_usr = pd.read_parquet(f'{stem}.path-by-usr.parquet')
    assert _rows(by_usr, ['usr', 'path'])[:12] == [
        (None, '.'), (None, 'd2'), (None, 'd4'),
        (None, 'd2/f0.bin'), (None, 'd2/f1.bin'), (None, 'd2/f2.bin'), (None, 'd2/f3.bin'), (None, 'd2/f4.bin'),
        (None, 'd4/f0.bin'), (None, 'd4/f1.bin'), (None, 'd4/f2.bin'), (None, 'd4/f3.bin'),
    ]
    assert _rows(by_usr[by_usr.kind == 'dir'], ['usr', 'path']) == [
        (None, '.'), (None, 'd2'), (None, 'd4'),
        ('a', '.'), ('a', 'd1'), ('a', 'd5'),
        ('b', '.'), ('b', 'd3'),
        ('c', '.'), ('c', 'd0'),
    ]
    # `bysize`: bucket desc, then path, then the slice — the root's `a` slice
    # (120 MiB) sorts beside the unlabeled root row, its `c` slice (15 MiB)
    # three buckets down, beside `d0`.
    bysize = pd.read_parquet(f'{stem}.bysize.parquet')
    assert [(size_bucket(s), u, p) for u, p, s in _rows(bysize, ['usr', 'path', 'size'])][:8] == [
        (26, None, '.'), (26, 'a', '.'), (26, None, 'd4'), (26, 'a', 'd5'),
        (25, 'b', '.'), (25, None, 'd2'), (25, 'b', 'd3'),
        (24, 'a', 'd1'),
    ]
    pd.testing.assert_frame_equal(
        bysize,
        pd.read_parquet(layer2).assign(_b=lambda d: d['size'].map(size_bucket).fillna(-1).astype(int))
        .sort_values(['_b', 'path', 'usr'], ascending=[False, True, True], na_position='first')
        .drop(columns='_b').reset_index(drop=True),
    )
    bysize_by_usr = pd.read_parquet(f'{stem}.bysize-by-usr.parquet')
    assert [(u, size_bucket(s), p) for u, p, s in _rows(bysize_by_usr[bysize_by_usr.kind == 'dir'], ['usr', 'path', 'size'])] == [
        (None, 26, '.'), (None, 26, 'd4'), (None, 25, 'd2'),
        ('a', 26, '.'), ('a', 26, 'd5'), ('a', 24, 'd1'),
        ('b', 25, '.'), ('b', 25, 'd3'),
        ('c', 23, '.'), ('c', 23, 'd0'),
    ]


def test_write_tiers_validation(tmp_path: Path):
    layer2 = _layer2(tmp_path)
    stem = str(tmp_path / 'x')
    with pytest.raises(ValueError, match=r"sort variant \('nope',\): column\(s\) \['nope'\] not in"):
        write_tiers(layer2, stem, sort_variants=(('nope',),))
    with pytest.raises(ValueError, match="row_group_rows must be a positive multiple of 2048; got 3000"):
        write_tiers(layer2, stem, row_group_rows=3000)
    with pytest.raises(ValueError, match="unknown tier 'coarse'"):
        write_tiers(layer2, stem, tiers=('coarse',))
    not_layer2 = tmp_path / 'other.parquet'
    pd.DataFrame({'a': [1]}).to_parquet(not_layer2)
    with pytest.raises(ValueError, match=r"not a layer-2 parquet \(columns \['a'\]\)"):
        write_tiers(str(not_layer2), stem)


def test_cli_import_writes_tiers(tmp_path: Path):
    listing = _listing(tmp_path, _LISTING, 'listing.parquet')
    root = tmp_path / 'dt-root'
    tiers_dir = tmp_path / 'tiers'
    env = {**os.environ, 'DISK_TREE_ROOT': str(root)}
    # Bare `-i` cuts both sorts.
    r = subprocess.run(
        [sys.executable, '-m', 'disk_tree.cli.main', 'import',
         '-e', 'duckdb', '-l', str(listing), '-b', 'b1', '-t', TS.isoformat(),
         '-i', '-O', str(tiers_dir), '-r', '2048'],
        env=env, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert sorted(p.name for p in tiers_dir.iterdir()) == ['gcs-b1.bysize.parquet', 'gcs-b1.path.parquet']
    assert pd.read_parquet(tiers_dir / 'gcs-b1.bysize.parquet')['path'].tolist()[:6] == ['.', 'd4', 'd5', 'd2', 'd3', 'd1']
    assert _kv(str(tiers_dir / 'gcs-b1.path.parquet'))['sort'] == 'depth,path'
    conn = sqlite3.connect(root / 'disk-tree.db')
    rows = conn.execute("SELECT path, size, n_children, n_desc FROM scan").fetchall()
    conn.close()
    assert rows == [('gcs://b1', sum(s for _, s in _LISTING), 6, 37)]


def test_cli_tiers_need_a_dir_and_a_disk_engine(tmp_path: Path):
    from disk_tree.cli.import_listing import TierOpts, import_bucket
    with pytest.raises(ValueError, match="--tiers needs a blob on disk: use the duckdb or stream engine"):
        import_bucket(
            db=None, storage=None, con=None, engine='pandas', listings=('x',), bucket='b1',
            scheme='gcs', snap_time=TS, tier_opts=TierOpts(tiers=('path',), out_dir=str(tmp_path)),
        )
    listing = _listing(tmp_path, [('a', 1)], 'listing.parquet')
    r = subprocess.run(
        [sys.executable, '-m', 'disk_tree.cli.main', 'import',
         '-e', 'duckdb', '-l', str(listing), '-b', 'b1', '-i', 'path'],
        env={**os.environ, 'DISK_TREE_ROOT': str(tmp_path / 'root')}, capture_output=True, text=True, check=False,
    )
    assert r.returncode != 0
    assert r.stderr.rstrip().split('\n')[-1] == 'ValueError: --tiers needs --tiers-dir (or --out-dir)'


def test_connect_is_bounded(tmp_path: Path):
    """A cut without a caller's connection runs on `connect()`: DuckDB's
    memory limit, threads and spill directory are the ones asked for (an
    unbounded connection takes 80 % of RAM — 28 GB on a 30 GB Batch task)."""
    con = connect(mem='512MiB', threads=2, tmp_dir=str(tmp_path / 'spill'))  # DuckDB reads `MB` as 10^6
    setting = lambda k: con.execute(f"SELECT current_setting('{k}')").fetchone()[0]
    assert (setting('memory_limit'), setting('threads'), setting('temp_directory')) == ('512.0 MiB', 2, str(tmp_path / 'spill'))
    assert (tmp_path / 'spill').is_dir()


def test_write_tiers_default_spill_dir_is_removed(tmp_path: Path):
    layer2 = _layer2(tmp_path)
    stem = str(tmp_path / 'out' / 'b1')
    (tmp_path / 'out').mkdir()
    written = write_tiers(layer2, stem, tiers=('path',), mem='512MB')
    assert list(written) == [f'{stem}.path.parquet']
    assert sorted(p.name for p in (tmp_path / 'out').iterdir()) == ['b1.path.parquet']

