"""Tests for the DuckDB out-of-core aggregation path (spec Item B).

Parity target: byte-identical output to `disk_tree.find.index.aggregate`
(pandas) on the same input, and to `import_listing` on the same listing.
"""

import datetime as dt
import os
import subprocess
import sqlite3
import sys
import tempfile
from os.path import dirname
from pathlib import Path

import duckdb
import pandas as pd
from disk_tree.blobfs import read_parquet as read_listing
import pytest

from disk_tree.find import aggregate as aggregate_pandas
from disk_tree.find.aggregate_duckdb import aggregate_duckdb, aggregate_listing_to_parquet
from disk_tree.find.import_listing import import_listing
from disk_tree.listing import prepare_listing


TS = dt.datetime(2026, 7, 28, tzinfo=dt.timezone.utc)
NUMERIC = ['size', 'mtime', 'n_desc', 'n_files', 'n_children', 'depth']
COLS = ['path', 'size', 'mtime', 'kind', 'parent', 'uri', 'n_desc', 'n_files', 'n_children', 'depth']


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce to matching dtypes + row order for equality assertion."""
    d = df[COLS].astype({c: 'int64' for c in NUMERIC}).copy()
    return d.sort_values(['depth', 'path']).reset_index(drop=True)


def _build_inputs(rows: list[tuple[str, int]], scan_root: str) -> pd.DataFrame:
    """Compose the (files + synthesized dirs) input frame the way `import_listing` does."""
    files = pd.DataFrame({
        'path': [n for n, _ in rows],
        'size': [s for _, s in rows],
        'mtime': [int(TS.timestamp())] * len(rows),
        'kind': 'file',
        'parent': [dirname(n) for n, _ in rows],
        'uri': [f'{scan_root}/{n}' for n, _ in rows],
    })
    dir_paths = {''}
    for p in files['parent']:
        while p and p not in dir_paths:
            dir_paths.add(p)
            p = dirname(p)
    dir_names = sorted(dir_paths)
    dirs = pd.DataFrame({
        'path': dir_names,
        'size': 0,
        'mtime': 0,
        'kind': 'dir',
        'parent': [dirname(p) for p in dir_names],
        'uri': [f'{scan_root}/{p}' for p in dir_names],
    })
    return pd.concat([files, dirs], ignore_index=True)


# ---------- Byte-parity: pandas aggregate() vs. aggregate_duckdb() ----------

@pytest.mark.parametrize('rows', [
    # Small nested tree with a sibling top-level dir
    [('a.txt', 100), ('sub/b.txt', 200), ('sub/c.txt', 300), ('sub/deep/d.txt', 400), ('other/e.txt', 50)],
    # Single-file bucket
    [('lonely.bin', 42)],
    # All at root
    [('a', 1), ('b', 2), ('c', 3), ('d', 4)],
    # Deep single chain
    [('x/y/z/w/leaf.txt', 999)],
])
def test_parity_pandas_vs_duckdb(rows):
    scan_root = 'gcs://b1'
    inputs = _build_inputs(rows, scan_root)
    got_pandas = _normalize(aggregate_pandas(inputs.copy(), scan_root=scan_root))
    got_duckdb = _normalize(aggregate_duckdb(inputs.copy(), scan_root=scan_root))
    pd.testing.assert_frame_equal(got_pandas, got_duckdb)


# ---------- Standing cross-engine identity check (mgu's ask post-a2a) ----------
#
# The two engines' contract is byte-identical layer-2 output. mgu's 2026-08-14
# west4 a2a caught TWO drifts (`n_desc` semantics, `//` path components) that
# looked like production-vs-DT disagreements but actually would have surfaced
# as engine-vs-engine drift with the right fixture. This fixture is that
# fixture — every edge pattern that showed up in real listings, checked in so
# regressions get caught in CI, not on the next 588M-row a2a.
#
# When adding a new edge case: add it here first. If both engines still agree,
# ship. If they disagree, treat as a bug in whichever one drifted from spec.

_IDENTITY_LISTING = [
    # Normal case: nested files with siblings
    ('data/train/a.txt', 1024),
    ('data/train/b.txt', 2048),
    ('data/val/c.txt',   512),
    ('data/test/d.bin',  8192),
    # Sibling top-level dirs
    ('logs/2026-01-01.log', 4096),
    ('logs/2026-01-02.log', 4096),
    # Root-level file (parent = '')
    ('README.md', 300),
    ('LICENSE',   100),
    # Deep single-child chain
    ('deep/very/nested/leaf.txt', 42),
    # `//` empty-component path — real marin regression
    # (tokenized/finemath_3_plus-a26b0f//.artifact.json in production)
    ('tokenized/finemath//artifact.json', 4),
    ('tokenized/starcoder//artifact.json', 4),
    ('tokenized/normal/regular.txt', 1000),
    # Unicode paths (bucket keys can be arbitrary UTF-8)
    ('café/résumé.pdf', 500),
    ('日本語/データ.csv', 700),
    # Files with dots and no extension
    ('.hidden', 10),
    ('.git/config', 20),
    # Big single dir with many children (nudges the fold + groupby edges)
    *[(f'many/f{i:03d}.dat', i) for i in range(20)],
    # Zero-size file (real: placeholder objects)
    ('placeholder/marker', 0),
    # Single-file top-level dir (leaf-like)
    ('single/only.txt', 999),
]


def test_cross_engine_identity_on_real_edge_cases(tmp_path: Path):
    """Real-world edge patterns must produce byte-identical output from both
    engines. Standing identity check — any drift here is a bug."""
    listing = tmp_path / 'identity-listing.parquet'
    pd.DataFrame({
        'bucket': ['b1'] * len(_IDENTITY_LISTING),
        'name': [n for n, _ in _IDENTITY_LISTING],
        'size_bytes': [s for _, s in _IDENTITY_LISTING],
        'created': [TS] * len(_IDENTITY_LISTING),
        'storage_class_id': [1] * len(_IDENTITY_LISTING),
    }).to_parquet(listing)

    # Pandas engine: import_listing (in-memory).
    got_pandas = _normalize(import_listing((str(listing),), bucket='b1', scheme='gcs').df)

    # DuckDB engine: aggregate_listing_to_parquet (out-of-core).
    con = duckdb.connect()
    out = str(tmp_path / 'ooc.parquet')
    aggregate_listing_to_parquet(
        prepare_listing(con, (str(listing),)),
        bucket='b1', scheme='gcs', out_parquet=out, con=con,
    )
    got_duckdb = _normalize(read_listing(out))

    pd.testing.assert_frame_equal(got_pandas, got_duckdb)

    # Additional invariants that would be silently masked by identity alone:
    root_p = got_pandas[got_pandas.path == '.'].iloc[0]
    total_size = sum(s for _, s in _IDENTITY_LISTING)
    assert int(root_p['size']) == total_size, \
        f"root size drift: got {int(root_p['size'])}, expected {total_size}"
    assert int(root_p['n_files']) == len(_IDENTITY_LISTING), \
        f"root n_files drift: got {int(root_p['n_files'])}, expected {len(_IDENTITY_LISTING)}"
    # `//` file's bytes stay in `tokenized` (regression-locked)
    tok = got_pandas[got_pandas.path == 'tokenized'].iloc[0]
    assert int(tok['size']) == 1008  # 4 + 4 + 1000
    assert int(tok['n_files']) == 3


# ---------- True out-of-core path (parquet → parquet) matches import_listing ----------

@pytest.fixture
def listing_parquet(tmp_path: Path):
    listing = tmp_path / 'l.parquet'
    pd.DataFrame({
        'bucket': ['b1'] * 5,
        'name': ['a.txt', 'sub/b.txt', 'sub/c.txt', 'sub/deep/d.txt', 'other/e.txt'],
        'size_bytes': [100, 200, 300, 400, 50],
        'created': [TS] * 5,
        'storage_class_id': [1] * 5,
    }).to_parquet(listing)
    return str(listing)


def test_out_of_core_matches_import_listing(listing_parquet, tmp_path: Path):
    """aggregate_listing_to_parquet output equals import_listing (pandas) output."""
    con = duckdb.connect()
    out = str(tmp_path / 'out.parquet')
    stats = aggregate_listing_to_parquet(
        prepare_listing(con, (listing_parquet,)),
        bucket='b1', scheme='gcs', out_parquet=out, con=con,
    )
    got_ooc = _normalize(read_listing(out))
    got_ref = _normalize(import_listing((listing_parquet,), bucket='b1', scheme='gcs').df)
    pd.testing.assert_frame_equal(got_ooc, got_ref)
    # Root stats returned by aggregate_listing_to_parquet match the frame's root row
    root = got_ref[got_ref['path'] == '.'].iloc[0]
    assert stats['root_size'] == int(root['size'])
    assert stats['root_n_desc'] == int(root['n_desc'])
    assert stats['root_n_files'] == int(root['n_files'])
    assert stats['root_n_children'] == int(root['n_children'])
    assert stats['rows'] == len(got_ref)
    assert stats['files'] == 5


def test_out_of_core_missing_bucket_raises(listing_parquet, tmp_path: Path):
    con = duckdb.connect()
    with pytest.raises(ValueError, match="no rows for bucket 'nope'"):
        aggregate_listing_to_parquet(
            prepare_listing(con, (listing_parquet,)),
            bucket='nope', scheme='gcs',
            out_parquet=str(tmp_path / 'out.parquet'), con=con,
        )


def test_out_of_core_collapses_double_slashes(tmp_path: Path):
    """DuckDB parity for the `//` fix (see test_import.test_import_listing_collapses_double_slashes).
    Real marin regression: `regexp_extract` fails to match trailing-`/` paths →
    the intermediate dir gets parent='' → its bytes hop to the tree root."""
    listing = tmp_path / 'l.parquet'
    pd.DataFrame({
        'bucket': ['b1'] * 3,
        'name': ['tokenized/a.txt', 'tokenized/sub//x.txt', 'other/b.txt'],
        'size_bytes': [100, 4, 50],
        'created': [TS] * 3,
        'storage_class_id': [1] * 3,
    }).to_parquet(listing)
    con = duckdb.connect()
    stats = aggregate_listing_to_parquet(
        prepare_listing(con, (str(listing),)),
        bucket='b1', scheme='gcs',
        out_parquet=str(tmp_path / 'out.parquet'), con=con,
    )
    df = read_listing(tmp_path / 'out.parquet')

    # Bytes conserved.
    assert stats['root_size'] == 154

    # `//` file's bytes stay in tokenized (not hoisted to root).
    tok = df[df.path == 'tokenized'].iloc[0]
    assert int(tok['size']) == 104

    other = df[df.path == 'other'].iloc[0]
    assert int(other['size']) == 50

    # Canonical single-slash path in the output.
    assert 'tokenized/sub//x.txt' not in df.path.tolist()
    assert 'tokenized/sub/x.txt' in df.path.tolist()

    # And a proper `tokenized/sub` dir row (parent = tokenized, not empty).
    sub = df[df.path == 'tokenized/sub'].iloc[0]
    assert int(sub['size']) == 4
    assert sub['parent'] == 'tokenized'


# ---------- Scale smoke: 50k rows through the OOC path, tight memory cap ----------

def test_out_of_core_scale(tmp_path: Path):
    """50k rows aggregate under a 512MB duckdb cap (would spill if needed)."""
    listing = tmp_path / 'l.parquet'
    # 200 top-level dirs × 250 files each, varying depth 1-3
    names = []
    for d in range(200):
        for f in range(250):
            depth = (d + f) % 3
            if depth == 0:
                names.append(f'top{d}/file{f}.bin')
            elif depth == 1:
                names.append(f'top{d}/mid{f % 10}/file{f}.bin')
            else:
                names.append(f'top{d}/mid{f % 10}/deep{f % 5}/file{f}.bin')
    pd.DataFrame({
        'bucket': ['b1'] * len(names),
        'name': names,
        'size_bytes': [i % 10000 for i in range(len(names))],
        'created': [TS] * len(names),
        'storage_class_id': [1] * len(names),
    }).to_parquet(listing)

    con = duckdb.connect()
    temp_dir = tmp_path / 'ddb-tmp'
    temp_dir.mkdir()
    stats = aggregate_listing_to_parquet(
        prepare_listing(con, (str(listing),)),
        bucket='b1', scheme='gcs',
        out_parquet=str(tmp_path / 'out.parquet'),
        con=con, memory_limit='512MB', temp_dir=str(temp_dir),
    )
    assert stats['files'] == len(names)
    # root n_desc = 1 (self) + all files + all synthesized dir rows
    df = read_listing(tmp_path / 'out.parquet')
    n_dirs = int((df['kind'] == 'dir').sum())
    assert stats['root_n_desc'] == n_dirs + stats['files']  # includes self via n_dirs count


# ---------- CLI --engine duckdb creates a scan row (subprocess isolation) ----------

def test_cli_engine_duckdb_creates_scan(tmp_path: Path):
    listing = tmp_path / 'listing.parquet'
    pd.DataFrame({
        'bucket': ['b1', 'b1'],
        'name': ['a.txt', 'sub/b.txt'],
        'size_bytes': [100, 200],
        'created': [TS, TS],
        'storage_class_id': [1, 1],
    }).to_parquet(listing)
    root = tmp_path / 'dt-root'
    env = {**os.environ, 'DISK_TREE_ROOT': str(root)}
    r = subprocess.run(
        [sys.executable, '-m', 'disk_tree.cli.main', 'import',
         '-e', 'duckdb', '-l', str(listing), '-b', 'b1', '-t', TS.isoformat()],
        env=env, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    conn = sqlite3.connect(root / 'disk-tree.db')
    rows = conn.execute("SELECT path, size, n_children, n_desc FROM scan").fetchall()
    conn.close()
    # Matches the pandas-engine test's expected shape (byte-parity is enforced elsewhere)
    assert rows == [('gcs://b1', 300, 2, 4)]


# ---------- Fleet-scale cascade (spec specs/done/mgu-scale-unification.md, item A) ----------
#
# Both knobs are pure memory levers: output must stay byte-identical to the
# single in-memory cascade for every `partition_depth` and `db` setting. The
# identity listing is the fixture — its `//` keys are exactly the rows a
# name-range pushdown would miss without the dirty-key side table.

def _identity_listing(tmp_path: Path) -> str:
    listing = tmp_path / 'identity-listing.parquet'
    pd.DataFrame({
        'bucket': ['b1'] * len(_IDENTITY_LISTING),
        'name': [n for n, _ in _IDENTITY_LISTING],
        'size_bytes': [s for _, s in _IDENTITY_LISTING],
        'created': [TS] * len(_IDENTITY_LISTING),
        'storage_class_id': [1] * len(_IDENTITY_LISTING),
    }).to_parquet(listing)
    return str(listing)


def _run_ooc(listing: str, out: Path, **kw) -> tuple[pd.DataFrame, dict]:
    con = duckdb.connect()
    stats = aggregate_listing_to_parquet(
        prepare_listing(con, (listing,)),
        bucket='b1', scheme='gcs', out_parquet=str(out), con=con, **kw,
    )
    return _normalize(read_listing(out)), stats


_IDENTITY_STATS = {
    'rows': 57,
    'files': 38,
    'root_size': 23837,
    'root_n_desc': 57,
    'root_n_files': 38,
    'root_n_children': 13,
    'root_mtime': int(TS.timestamp()),
}


@pytest.mark.parametrize('depth, keys', [
    # k=1: every top-level *dir* is a key (10); the 3 root-level files are top-cascade rows
    (1, 10),
    # k=2: the depth-2 dirs that hold files — `many/f*.dat` are files at depth 2, not keys
    (2, 7),
    # k=3: only `deep/very/nested` reaches depth 3 with a file beneath it
    (3, 1),
    # deeper than the tree: everything is "shallow" → the top cascade alone
    (6, 0),
])
def test_partitioned_cascade_is_byte_identical(tmp_path: Path, depth: int, keys: int):
    listing = _identity_listing(tmp_path)
    base, base_stats = _run_ooc(listing, tmp_path / 'base.parquet')
    # `partition_files=0`: one cascade per key, so every stub-merge path is exercised.
    got, stats = _run_ooc(listing, tmp_path / f'k{depth}.parquet', partition_depth=depth, partition_files=0)
    pd.testing.assert_frame_equal(base, got)
    stats.pop('max_rss_mb')
    base_stats.pop('max_rss_mb')
    assert base_stats == {**_IDENTITY_STATS, 'partitions': 0, 'partition_keys': 0, 'partition_splits': 0, 'sort_ranges': 0}
    # ≥ 2 cascades → the final sort runs per range: one per cascade + the
    # rows before the first key (`_write_ranged`); a single cascade sorts globally.
    assert stats == {
        **_IDENTITY_STATS, 'partitions': keys, 'partition_keys': keys, 'partition_splits': 0,
        'sort_ranges': keys + 1 if keys >= 2 else 0,
    }


def test_partitions_batch_into_bounded_cascades(tmp_path: Path):
    """`partition_files` packs consecutive keys into cascades of ≤ N files: at
    k=1 the keys (in order) hold .git 1, café 1, data 4, deep 1, logs 2, many
    20, placeholder 1, single 1, tokenized 3, 日本語 1 files → at N=5 the
    greedy packing is [.git café] [data deep] [logs] [many] [placeholder single
    tokenized] [日本語]: 6 cascades for 10 keys, and `many` (20 > N) stands
    alone. Output stays byte-identical."""
    listing = _identity_listing(tmp_path)
    base, _ = _run_ooc(listing, tmp_path / 'base.parquet')
    got, stats = _run_ooc(listing, tmp_path / 'k1n5.parquet', partition_depth=1, partition_files=5)
    pd.testing.assert_frame_equal(base, got)
    assert (stats['partitions'], stats['partition_keys']) == (6, 10)
    # The default budget swallows the whole fixture: one cascade, same rows.
    got, stats = _run_ooc(listing, tmp_path / 'k1.parquet', partition_depth=1)
    pd.testing.assert_frame_equal(base, got)
    assert (stats['partitions'], stats['partition_keys']) == (1, 10)


@pytest.mark.parametrize('partition_files', [
    0,    # one key per cascade: `[a/, a0)` is exact — never lost rows
    4,    # pairs `[A _x] [a a-v1] [a-v2 a.bak]`: `[a/, a-v10)` lost every `a/…` row
    100,  # one batch `[A/, a.bak0)`: `a/…` sorts past `a.bak0`, lost
])
def test_batch_range_covers_prefix_keys(tmp_path: Path, partition_files: int):
    """Keys sorted as bare strings put `a` before `a-v1`, but `a/…` rows sort
    *after* `a-v1/…` (`/` is 0x2F, `-` is 0x2D), so a batch range built from
    the bare order `[a/, a-v20)` misses every row under `a` — mgu's round-2
    fleet gate dropped three whole subtrees this way (spec `specs/done/mgu-scale-a3-gate.md`
    ask 6). Batches must be contiguous in `key/` order, at every budget."""
    listing = tmp_path / 'prefix-keys.parquet'
    names = [
        'A/f', 'A/d/f',
        '_x/f', '_x/d/f',
        'a/f', 'a/d/f',
        'a-v1/f', 'a-v1/d/f',
        'a-v2/f', 'a-v2/d/f',
        'a.bak/f',
    ]
    pd.DataFrame({
        'bucket': ['b1'] * len(names),
        'name': names,
        'size_bytes': list(range(1, len(names) + 1)),
        'created': [TS] * len(names),
        'storage_class_id': [1] * len(names),
    }).to_parquet(listing)
    base, base_stats = _run_ooc(str(listing), tmp_path / 'base.parquet')
    assert (base_stats['files'], base_stats['root_size'], base_stats['root_n_files']) == (11, 66, 11)
    got, stats = _run_ooc(str(listing), tmp_path / 'k1.parquet', partition_depth=1, partition_files=partition_files)
    pd.testing.assert_frame_equal(base, got)
    assert stats['partition_keys'] == 6
    assert (stats['files'], stats['root_size'], stats['root_n_files']) == (11, 66, 11)


@pytest.mark.parametrize('partition_files, expect', [
    # `big` (9 files) > 5 → its depth-2 dirs `d1` (3) and `d2` (4) become keys; its
    # 2 direct files join the top cascade. Frontier in `key/` order: big/d1, big/d2,
    # small → packed [big/d1] [big/d2 small].
    (5, dict(partitions=2, partition_keys=3, partition_splits=1)),
    # 4: every key stands alone.
    (4, dict(partitions=3, partition_keys=3, partition_splits=1)),
    # 3: `big/d2` (4) splits again into `big/d2/x` (4), which is a flat dir of 4 files
    # — nothing to split into, so it stands alone over budget.
    (3, dict(partitions=3, partition_keys=3, partition_splits=2)),
])
def test_oversized_keys_split_recursively(tmp_path: Path, partition_files: int, expect: dict):
    """A key over `partition_files` is replaced by its sub-directories until
    every key fits or is flat (spec `specs/done/mgu-scale-a3-gate.md` ask 7); the frontier
    is then keys at mixed depths, and the output is still byte-identical."""
    listing = tmp_path / 'split.parquet'
    names = [
        'big/d1/f1', 'big/d1/f2', 'big/d1/f3',
        'big/d2/x/f1', 'big/d2/x/f2', 'big/d2/x/f3', 'big/d2/x/f4',
        'big/direct1', 'big/direct2',
        'small/f',
        'top.txt',
    ]
    pd.DataFrame({
        'bucket': ['b1'] * len(names),
        'name': names,
        'size_bytes': list(range(1, len(names) + 1)),
        'created': [TS] * len(names),
        'storage_class_id': [1] * len(names),
    }).to_parquet(listing)
    base, _ = _run_ooc(str(listing), tmp_path / 'base.parquet')
    got, stats = _run_ooc(str(listing), tmp_path / 'k1.parquet', partition_depth=1, partition_files=partition_files)
    pd.testing.assert_frame_equal(base, got)
    assert {k: stats[k] for k in expect} == expect
    big = got.set_index('path').loc['big']
    assert (big['size'], big['n_files'], big['n_children'], big['n_desc']) == (45, 9, 4, 13)


def test_batch_partitions_packing():
    from disk_tree.find.aggregate_duckdb import _batch_partitions
    parts = [('a', 1), ('b', 1), ('c', 4), ('d', 1), ('e', 2), ('f', 20), ('g', 1)]
    assert _batch_partitions(parts, 5) == [['a', 'b'], ['c', 'd'], ['e'], ['f'], ['g']]
    assert _batch_partitions(parts, 0) == [[k] for k, _ in parts]
    assert _batch_partitions(parts, 100) == [[k for k, _ in parts]]
    assert _batch_partitions([], 5) == []


@pytest.mark.parametrize('kw', [dict(), dict(partition_depth=1, partition_files=0), dict(partition_depth=2)])
def test_folder_placeholders_are_objects_at_their_dir(tmp_path: Path, kw: dict):
    """A listing name ending in `/` (a folder placeholder) is an object at the
    directory it names: the dir row carries it (`n_files`, its size/mtime), it
    is never a file child, and it never duplicates a synthesized dir row
    (spec specs/done/mgu-scale-a3-gate.md ask 2). Same rows under every partitioning."""
    listing = tmp_path / 'ph.parquet'
    names = ['profile/', 'profile/1/', 'profile/1/x.bin', 'profile/2/']
    pd.DataFrame({
        'bucket': ['b1'] * 4,
        'name': names,
        'size_bytes': [0, 0, 5, 3],
        'created': [TS] * 4,
        'storage_class_id': [1] * 4,
    }).to_parquet(listing)
    df, stats = _run_ooc(str(listing), tmp_path / 'out.parquet', **kw)
    # `n_desc` counts the row itself (this engine's convention: root 5 = itself
    # + 3 dirs + 1 file); `n_files` counts objects — the 3 placeholders + x.bin.
    cols = ['path', 'kind', 'size', 'n_desc', 'n_files', 'n_children', 'parent', 'depth']
    assert df[cols].values.tolist() == [
        ['.', 'dir', 8, 5, 4, 1, '', 0],
        ['profile', 'dir', 8, 4, 4, 2, '.', 1],
        ['profile/1', 'dir', 5, 2, 2, 1, 'profile', 2],
        ['profile/2', 'dir', 3, 1, 1, 0, 'profile', 2],
        ['profile/1/x.bin', 'file', 5, 1, 1, 0, 'profile/1', 3],
    ]
    assert df['mtime'].tolist() == [int(TS.timestamp())] * 5
    assert (stats['files'], stats['root_n_files'], stats['root_n_desc'], stats['root_n_children']) == (4, 4, 5, 1)


def test_partition_depth_negative_raises(tmp_path: Path):
    listing = _identity_listing(tmp_path)
    with pytest.raises(ValueError, match="partition_depth must be >= 0; got -1"):
        _run_ooc(listing, tmp_path / 'x.parquet', partition_depth=-1)


def test_file_backed_db_in_directory_is_temporary(tmp_path: Path):
    """`db=<dir>`: the cascade runs in a fresh database file under it, removed on success."""
    listing = _identity_listing(tmp_path)
    base, _ = _run_ooc(listing, tmp_path / 'base.parquet')
    db_dir = tmp_path / 'spill-disk'
    db_dir.mkdir()
    got, stats = _run_ooc(listing, tmp_path / 'db.parquet', db=str(db_dir), partition_depth=2, partition_files=0)
    pd.testing.assert_frame_equal(base, got)
    assert (stats['partitions'], stats['partition_keys']) == (7, 7)
    assert sorted(p.name for p in db_dir.iterdir()) == []


def test_file_backed_db_path_is_kept_and_left_empty(tmp_path: Path):
    """`db=<file>`: the database is the user's (post-mortems); every working
    table is dropped by the end, so a clean run leaves it empty."""
    listing = _identity_listing(tmp_path)
    base, _ = _run_ooc(listing, tmp_path / 'base.parquet')
    db_file = tmp_path / 'cascade.duckdb'
    got, _ = _run_ooc(listing, tmp_path / 'db.parquet', db=str(db_file))
    pd.testing.assert_frame_equal(base, got)
    assert db_file.exists()
    con = duckdb.connect(str(db_file))
    assert con.execute("SELECT table_name FROM duckdb_tables() ORDER BY 1").fetchall() == []
    assert con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal ORDER BY 1").fetchall() == []
    con.close()


def test_cli_engine_duckdb_partitioned_file_backed_creates_scan(tmp_path: Path):
    listing = tmp_path / 'listing.parquet'
    pd.DataFrame({
        'bucket': ['b1', 'b1', 'b1'],
        'name': ['a.txt', 'sub/b.txt', 'sub/deep/c.txt'],
        'size_bytes': [100, 200, 300],
        'created': [TS, TS, TS],
        'storage_class_id': [1, 1, 1],
    }).to_parquet(listing)
    root = tmp_path / 'dt-root'
    db_dir = tmp_path / 'db'
    db_dir.mkdir()
    env = {**os.environ, 'DISK_TREE_ROOT': str(root)}
    r = subprocess.run(
        [sys.executable, '-m', 'disk_tree.cli.main', 'import',
         '-e', 'duckdb', '-l', str(listing), '-b', 'b1', '-t', TS.isoformat(),
         '-k', '1', '-P', '1', '-d', str(db_dir)],
        env=env, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    conn = sqlite3.connect(root / 'disk-tree.db')
    rows = conn.execute("SELECT path, size, n_children, n_desc FROM scan").fetchall()
    conn.close()
    assert rows == [('gcs://b1', 600, 2, 6)]
    assert sorted(p.name for p in db_dir.iterdir()) == []


# ---------- Seeded random listings: every partition config is byte-identical ----------

#: Segments chosen so keys are string-prefixes of siblings with the next byte
#: on either side of `/` (0x2F): `a` vs `a-v1` (`-` < `/`), `a` vs `a0`/`ab`
#: (> `/`) — the ordering that dropped subtrees in the batched cascade (spec
#: `specs/done/mgu-scale-a3-gate.md` ask 6) and that the ranged final sort's bounds rest
#: on; plus space, unicode, a `gs:` segment (`gs://` inside names, ask 9).
_RANDOM_SEGMENTS = ['a', 'a-v1', 'a.bak', 'a0', 'ab', '_x', 'A', ' ', 'b', 'z', '日本', 'gs:']


def _random_listing(tmp_path: Path, seed: int, n: int = 60) -> tuple[str, str, str]:
    """A listing of `n` distinct names 1–6 segments deep off `_RANDOM_SEGMENTS`,
    ~5% with a `//` inside (dirty: canonical path ≠ listing name), ~5% folder
    placeholders (trailing `/`), zero sizes and spread `created`; a label
    table over random 1–3-segment prefixes (some with trailing `/`, some
    unlabeled); a side table over random canonical paths (root included).
    Returns `(listing, labels, side)` parquet paths."""
    import random
    rng = random.Random(seed)
    names: set[str] = set()
    while len(names) < n:
        segs = [rng.choice(_RANDOM_SEGMENTS) for _ in range(rng.randint(1, 6))]
        name = '/'.join(segs)
        r = rng.random()
        if r < 0.05 and len(segs) > 1:
            i = rng.randrange(1, len(segs))
            name = '/'.join(segs[:i]) + '//' + '/'.join(segs[i:])
        elif r < 0.10:
            name += '/'
        names.add(name)
    rows = sorted(names)
    listing = tmp_path / f'random-{seed}.parquet'
    pd.DataFrame({
        'bucket': ['b1'] * n,
        'name': rows,
        'size_bytes': [rng.choice([0, rng.randint(1, 10_000)]) for _ in rows],
        'created': [TS + dt.timedelta(seconds=rng.randint(0, 10**7)) for _ in rows],
        'storage_class_id': [rng.randint(1, 3) for _ in rows],
    }).to_parquet(listing)

    def canonical(name: str) -> str:
        import re
        return re.sub('/+', '/', name).rstrip('/')

    prefixes = sorted({'/'.join(canonical(nm).split('/')[:k]) for nm in rows for k in (1, 2, 3)} - {''})
    chosen = rng.sample(prefixes, k=min(len(prefixes), 12))
    labels = tmp_path / f'labels-{seed}.parquet'
    pd.DataFrame({
        'prefix': [p + ('/' if rng.random() < 0.3 else '') for p in chosen],
        'usr': [rng.choice(['u1', 'u2', None]) for _ in chosen],
    }).to_parquet(labels)
    paths = sorted({canonical(nm) for nm in rows} | set(prefixes)) + ['.']
    picked = rng.sample(paths, k=min(len(paths), 20))
    side = tmp_path / f'side-{seed}.parquet'
    pd.DataFrame({
        'path': picked,
        'last_ts': [TS + dt.timedelta(seconds=rng.randint(0, 10**7)) for _ in picked],
    }).to_parquet(side)
    return str(listing), str(labels), str(side)


@pytest.mark.parametrize('seed', [1, 2])
def test_random_listings_are_byte_identical_across_partition_configs(tmp_path: Path, seed: int):
    """The one-cascade output is the reference; every `(k, partition_files)`
    config — including one cascade per key and budgets that pack a few keys
    per cascade, hence a ranged final sort — must reproduce it row for row,
    with labels, pivots, mean mtime, size histogram and a side MAX column all
    on. Guards the whole partition/sort machinery against the input shapes
    the hand-written fixtures happen not to contain."""
    listing, labels, side = _random_listing(tmp_path, seed)
    ext = dict(
        pivot_sums=('storage_class_id',), mean_mtime=True, size_hist=True,
        label=labels, label_cols=('usr',), side=side, max_cols=('last_ts',),
    )
    base, base_stats = _run_ooc(listing, tmp_path / 'base.parquet', **ext)
    assert base_stats['sort_ranges'] == 0
    ranged = 0
    for k in (1, 2, 3):
        for partition_files in (0, 5, 10**6):
            got, stats = _run_ooc(listing, tmp_path / f'k{k}-p{partition_files}.parquet',
                                  partition_depth=k, partition_files=partition_files, **ext)
            pd.testing.assert_frame_equal(base, got)
            expect = stats['partitions'] + 1 if stats['partitions'] >= 2 else 0
            assert stats['sort_ranges'] == expect, (k, partition_files, stats)
            ranged += stats['sort_ranges'] > 0
    assert ranged >= 4
