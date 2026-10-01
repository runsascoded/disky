"""Phase-0: listing parquet → `import_listing` → a scan."""

import datetime as dt
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from disk_tree.find.import_listing import import_listing, list_buckets

TS_A = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)


def write_listing(path: Path, rows: list[dict]) -> str:
    """Write a raw object-listing parquet (layer-1 schema)."""
    pd.DataFrame(rows).to_parquet(path)
    return str(path)


# ---------- Unit tests: import_listing produces canonical layer-2 rows ----------

def test_import_listing_shape(tmp_path: Path):
    """The layer-2 frame has synthesized dir rows, correct n_desc / n_children / depth."""
    listing = write_listing(tmp_path / "l.parquet", [
        {'bucket': 'b1', 'name': 'a.txt', 'size_bytes': 100, 'created': TS_A, 'storage_class_id': 1},
        {'bucket': 'b1', 'name': 'sub/b.txt', 'size_bytes': 200, 'created': TS_A, 'storage_class_id': 1},
        {'bucket': 'b1', 'name': 'sub/c.txt', 'size_bytes': 300, 'created': TS_A, 'storage_class_id': 1},
        {'bucket': 'b1', 'name': 'sub/deep/d.txt', 'size_bytes': 400, 'created': TS_A, 'storage_class_id': 1},
    ])
    df = import_listing((listing,), bucket='b1', scheme='gcs').df

    # by-path dict for order-insensitive assertion (aggregation ordering is well-defined
    # but the important invariant is per-row)
    got = {r['path']: (r['size'], r['kind'], r['parent'], r['uri'], r['n_desc'], r['n_files'], r['n_children'], r['depth'])
           for _, r in df.iterrows()}
    # n_desc for dirs includes self + all descendant dirs (walk-backend semantics:
    # gfind emits `path='' kind='dir'` for the scan root, so root/sub/sub/deep all
    # count self). `n_files` is objects-only, i.e. the count consumers expect from
    # an S3/GCS bucket.
    assert got == {
        '.':               (1000, 'dir',  '',         'gcs://b1',                7, 4, 2, 0),
        'a.txt':           (100,  'file', '',         'gcs://b1/a.txt',          1, 1, 0, 1),
        'sub':             (900,  'dir',  '.',        'gcs://b1/sub',            5, 3, 3, 1),
        'sub/b.txt':       (200,  'file', 'sub',      'gcs://b1/sub/b.txt',      1, 1, 0, 2),
        'sub/c.txt':       (300,  'file', 'sub',      'gcs://b1/sub/c.txt',      1, 1, 0, 2),
        'sub/deep':        (400,  'dir',  'sub',      'gcs://b1/sub/deep',       2, 1, 1, 2),
        'sub/deep/d.txt':  (400,  'file', 'sub/deep', 'gcs://b1/sub/deep/d.txt', 1, 1, 0, 3),
    }


def test_import_listing_bucket_filter(tmp_path: Path):
    """`bucket=` filters; other buckets don't contribute to the scan."""
    listing = write_listing(tmp_path / "l.parquet", [
        {'bucket': 'b1', 'name': 'x.txt', 'size_bytes': 10, 'created': TS_A, 'storage_class_id': 1},
        {'bucket': 'b2', 'name': 'y.txt', 'size_bytes': 20, 'created': TS_A, 'storage_class_id': 1},
    ])
    df = import_listing((listing,), bucket='b1', scheme='gcs').df
    assert sorted(df['path'].tolist()) == ['.', 'x.txt']
    root = df[df.path == '.'].iloc[0]
    assert root['size'] == 10 and root['n_desc'] == 2  # self (dir) + 1 file


def test_import_listing_missing_bucket_raises(tmp_path: Path):
    listing = write_listing(tmp_path / "l.parquet", [
        {'bucket': 'b1', 'name': 'x.txt', 'size_bytes': 10, 'created': TS_A, 'storage_class_id': 1},
    ])
    with pytest.raises(ValueError, match="no rows for bucket 'nope'"):
        import_listing((listing,), bucket='nope', scheme='gcs')


def test_list_buckets_distinct(tmp_path: Path):
    listing = write_listing(tmp_path / "l.parquet", [
        {'bucket': 'b1', 'name': 'x', 'size_bytes': 1, 'created': TS_A, 'storage_class_id': 1},
        {'bucket': 'b2', 'name': 'y', 'size_bytes': 2, 'created': TS_A, 'storage_class_id': 1},
        {'bucket': 'b1', 'name': 'z', 'size_bytes': 3, 'created': TS_A, 'storage_class_id': 1},
    ])
    assert list_buckets((listing,)) == ['b1', 'b2']


def test_import_listing_collapses_double_slashes(tmp_path: Path):
    """Keys with empty path components (`a//b`) must stay in their real
    subtree — not get hoisted to the tree root by trailing-slash-borked
    parent-walking (real marin regression:
    `tokenized/finemath_3_plus-a26b0f//.artifact.json` moved bytes across
    top-level subtrees; see specs/import-a2a-findings.md item 2)."""
    listing = write_listing(tmp_path / "l.parquet", [
        {'bucket': 'b1', 'name': 'tokenized/a.txt',      'size_bytes': 100, 'created': TS_A, 'storage_class_id': 1},
        {'bucket': 'b1', 'name': 'tokenized/sub//x.txt', 'size_bytes':   4, 'created': TS_A, 'storage_class_id': 1},
        {'bucket': 'b1', 'name': 'other/b.txt',          'size_bytes':  50, 'created': TS_A, 'storage_class_id': 1},
    ])
    df = import_listing((listing,), bucket='b1', scheme='gcs').df

    # Root sum unchanged (bytes-conserving).
    root = df[df.path == '.'].iloc[0]
    assert int(root['size']) == 154

    # The `//` file's bytes stay inside the `tokenized` subtree.
    tok = df[df.path == 'tokenized'].iloc[0]
    assert int(tok['size']) == 104  # 100 + 4

    # `other` is unaffected.
    other = df[df.path == 'other'].iloc[0]
    assert int(other['size']) == 50

    # File row uses the canonicalized (single-slash) path — no `//` survives.
    assert 'tokenized/sub//x.txt' not in df.path.tolist()
    assert 'tokenized/sub/x.txt' in df.path.tolist()

    # And a `tokenized/sub` dir row exists (properly synthesized, not orphaned).
    sub = df[df.path == 'tokenized/sub'].iloc[0]
    assert int(sub['size']) == 4
    assert sub['parent'] == 'tokenized'


# ---------- CLI smoke test (subprocess to sidestep the sqla singleton) ----------

def test_import_cli_creates_scan(tmp_path: Path):
    """`disk-tree import -l <listing>` creates a scan row in an isolated DISK_TREE_ROOT."""
    listing = write_listing(tmp_path / 'listing.parquet', [
        {'bucket': 'b1', 'name': 'a.txt', 'size_bytes': 100, 'created': TS_A, 'storage_class_id': 1},
        {'bucket': 'b1', 'name': 'sub/b.txt', 'size_bytes': 200, 'created': TS_A, 'storage_class_id': 1},
    ])
    root = tmp_path / 'dt-root'
    env = {**os.environ, 'DISK_TREE_ROOT': str(root)}
    r = subprocess.run(
        [sys.executable, '-m', 'disk_tree.cli.main', 'import',
         '-l', listing, '-b', 'b1', '-t', TS_A.isoformat()],
        env=env, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    # Scan row landed
    conn = sqlite3.connect(root / 'disk-tree.db')
    rows = conn.execute("SELECT path, size, n_children, n_desc FROM scan").fetchall()
    conn.close()
    # size=300 (100 + 200), n_children=2 (a.txt + sub dir), n_desc=4 (self + a.txt + sub + b.txt)
    assert rows == [('gcs://b1', 300, 2, 4)]
