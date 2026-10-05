"""Tests for the streaming aggregation engine (spec: specs/done/streaming-aggregation.md).

Parity target: byte-identical layer-2 output vs the pandas and DuckDB
engines on the same listing — including the `_IDENTITY_LISTING` edge-case
fixture (which contains `//` keys, the one pattern that perturbs sorted
order and exercises the dirty-key side-merge).
"""

import datetime as dt
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import duckdb
import pandas as pd
from disk_tree.blobfs import read_parquet as read_listing
import pytest

from disk_tree.find.aggregate_duckdb import aggregate_listing_to_parquet
from disk_tree.find.aggregate_stream import aggregate_stream
from disk_tree.find.import_listing import import_listing
from disk_tree.listing import prepare_listing

from test_aggregate_duckdb import _IDENTITY_LISTING, _normalize, TS


def _write_listing(
    path: Path,
    rows: list[tuple[str, int]],
    bucket: str = 'b1',
    sort: bool = True,
    row_group_size: int | None = None,
):
    """Raw-schema listing parquet; sorted by key like real object-store listings.
    `row_group_size` shrinks read batches → more pass-1 checkpoints, so organic
    partition-boundary selection engages on tiny fixtures."""
    if sort:
        rows = sorted(rows)
    kw = {'row_group_size': row_group_size} if row_group_size else {}
    pd.DataFrame({
        'bucket': [bucket] * len(rows),
        'name': [n for n, _ in rows],
        'size_bytes': [s for _, s in rows],
        'created': [TS] * len(rows),
        'storage_class_id': [1] * len(rows),
    }).to_parquet(path, **kw)
    return str(path)


def _stream(listings: tuple[str, ...], tmp_path: Path, bucket: str = 'b1'):
    out = str(tmp_path / 'stream-out.parquet')
    stats = aggregate_stream(listings, bucket=bucket, scheme='gcs', out_parquet=out)
    return _normalize(read_listing(out)), stats


# ---------- Cross-engine identity: all 3 engines, same edge-case fixture ----------

def test_three_engine_identity(tmp_path: Path):
    listing = _write_listing(tmp_path / 'l.parquet', _IDENTITY_LISTING)

    got_pandas = _normalize(import_listing((listing,), bucket='b1', scheme='gcs').df)

    con = duckdb.connect()
    ooc = str(tmp_path / 'ooc.parquet')
    aggregate_listing_to_parquet(
        prepare_listing(con, (listing,)),
        bucket='b1', scheme='gcs', out_parquet=ooc, con=con,
    )
    got_duckdb = _normalize(read_listing(ooc))

    got_stream, stats = _stream((listing,), tmp_path)

    pd.testing.assert_frame_equal(got_pandas, got_duckdb)
    pd.testing.assert_frame_equal(got_pandas, got_stream)

    # Column ORDER is contractual too (file-level diffs, positional set ops
    # like EXCEPT) — `_normalize` reindexes to COLS, hiding order skew, so
    # lock the raw parquet column lists explicitly. Caught on the CW 92.7M
    # acceptance: stream emitted (path,size,mtime,kind,parent,uri,n_*,depth).
    assert list(read_listing(str(tmp_path / 'stream-out.parquet')).columns) == \
        list(read_listing(ooc).columns) == [
        'path', 'size', 'mtime', 'n_desc', 'n_files', 'n_children', 'kind', 'parent', 'uri', 'depth',
    ]

    root = got_stream[got_stream.path == '.'].iloc[0]
    assert stats['root_size'] == int(root['size'])
    assert stats['root_n_desc'] == int(root['n_desc'])
    assert stats['root_n_files'] == int(root['n_files'])
    assert stats['root_n_children'] == int(root['n_children'])
    assert stats['rows'] == len(got_stream)
    assert stats['files'] == len(_IDENTITY_LISTING)


@pytest.mark.parametrize('rows', [
    # Small nested tree with a sibling top-level dir
    [('a.txt', 100), ('sub/b.txt', 200), ('sub/c.txt', 300), ('sub/deep/d.txt', 400), ('other/e.txt', 50)],
    # Single-file bucket
    [('lonely.bin', 42)],
    # All at root
    [('a', 1), ('b', 2), ('c', 3), ('d', 4)],
    # Deep single chain
    [('x/y/z/w/leaf.txt', 999)],
    # A key that is a proper prefix of a dir name (file 'a' + dir 'a/')
    [('a', 7), ('a/b', 8), ('a/b/c', 9)],
])
def test_stream_matches_pandas(rows, tmp_path: Path):
    listing = _write_listing(tmp_path / 'l.parquet', rows)
    got_pandas = _normalize(import_listing((listing,), bucket='b1', scheme='gcs').df)
    got_stream, _ = _stream((listing,), tmp_path)
    pd.testing.assert_frame_equal(got_pandas, got_stream)


# ---------- Multi-shard k-way merge ----------

def test_multi_shard_merge(tmp_path: Path):
    """Rows split across 2 shards by key range must aggregate identically to
    the same rows in one shard."""
    rows = sorted(_IDENTITY_LISTING)
    mid = len(rows) // 2
    shard_dir = tmp_path / 'shards'
    shard_dir.mkdir()
    _write_listing(shard_dir / 'shard-000.parquet', rows[:mid], sort=False)
    _write_listing(shard_dir / 'shard-001.parquet', rows[mid:], sort=False)
    single = _write_listing(tmp_path / 'single.parquet', rows, sort=False)

    got_multi, stats_multi = _stream((str(shard_dir / '*.parquet'),), tmp_path)
    out2 = str(tmp_path / 'single-out.parquet')
    aggregate_stream((single,), bucket='b1', scheme='gcs', out_parquet=out2)
    got_single = _normalize(read_listing(out2))

    pd.testing.assert_frame_equal(got_multi, got_single)
    assert stats_multi['files'] == len(rows)


def test_interleaved_shards(tmp_path: Path):
    """Shards with interleaved (not range-partitioned) keys still merge correctly —
    each shard is sorted, global order comes from the heap merge."""
    rows = sorted(_IDENTITY_LISTING)
    shard_dir = tmp_path / 'shards'
    shard_dir.mkdir()
    _write_listing(shard_dir / 'shard-000.parquet', rows[0::2], sort=False)
    _write_listing(shard_dir / 'shard-001.parquet', rows[1::2], sort=False)

    got_multi, _ = _stream((str(shard_dir / '*.parquet'),), tmp_path)
    got_pandas = _normalize(import_listing((str(shard_dir / '*.parquet'),), bucket='b1', scheme='gcs').df)
    pd.testing.assert_frame_equal(got_pandas, got_multi)


# ---------- Error paths ----------

def test_fractional_second_timestamps(tmp_path: Path):
    """Sub-second `created` values must TRUNCATE to epoch seconds in every
    engine. Bare `epoch(ts)::BIGINT` in DuckDB *rounds* — on the CW 92.7M
    acceptance run that skewed ~50% of mtimes +1s vs the stream engine
    (fractional parts ≥ .5). Whole-second fixtures can't see this."""
    ts_frac = dt.datetime(2026, 7, 28, 12, 0, 0, 700_000, tzinfo=dt.timezone.utc)  # .7s
    listing = tmp_path / 'l.parquet'
    pd.DataFrame({
        'bucket': ['b1', 'b1'],
        'name': ['a.txt', 'sub/b.txt'],
        'size_bytes': [100, 200],
        'created': [ts_frac, ts_frac],
        'storage_class_id': [1, 1],
    }).to_parquet(listing)
    expect = int(ts_frac.timestamp())  # floor: …00, not …01

    got_pandas = _normalize(import_listing((str(listing),), bucket='b1', scheme='gcs').df)

    con = duckdb.connect()
    ooc = str(tmp_path / 'ooc.parquet')
    aggregate_listing_to_parquet(
        prepare_listing(con, (str(listing),)),
        bucket='b1', scheme='gcs', out_parquet=ooc, con=con,
    )
    got_duckdb = _normalize(read_listing(ooc))

    got_stream, _ = _stream((str(listing),), tmp_path)

    assert sorted(got_stream[got_stream.kind == 'file'].mtime.tolist()) == [expect, expect]
    pd.testing.assert_frame_equal(got_pandas, got_duckdb)
    pd.testing.assert_frame_equal(got_pandas, got_stream)


def test_piecewise_sorted_shard(tmp_path: Path):
    """bulk-list bin-packs multiple sorted key ranges into one shard, so shards
    are piecewise sorted: each maximal sorted run becomes its own merge source.
    An out-of-order shard must aggregate identically to the duckdb engine."""
    rows = [('z/a.txt', 1), ('z/b.txt', 2), ('a/x.txt', 3), ('a/y.txt', 4), ('m.txt', 5)]
    listing = _write_listing(tmp_path / 'l.parquet', rows, sort=False)

    got_stream, stats = _stream((listing,), tmp_path)

    con = duckdb.connect()
    ooc = str(tmp_path / 'ooc.parquet')
    aggregate_listing_to_parquet(
        prepare_listing(con, (listing,)),
        bucket='b1', scheme='gcs', out_parquet=ooc, con=con,
    )
    got_duckdb = _normalize(read_listing(ooc))

    pd.testing.assert_frame_equal(got_stream, got_duckdb)
    assert stats['files'] == 5


def test_interleaved_buckets_multi_run_row_groups(tmp_path: Path):
    """Locks the raw-ordinal contract behind row-group skipping: run-start
    ordinals are *raw* (unfiltered) row indices, so a shard interleaving two
    buckets makes raw ≠ bucket-filtered ordinals (a filtered-ordinal reader
    would slice the wrong rows), and `row_group_size=2` forces the reader to
    actually map ordinals onto row-group boundaries and skip non-intersecting
    groups."""
    rows = [
        ('b2', 'zz/1', 9),
        ('b1', 'z/a.txt', 1),
        ('b1', 'z/b.txt', 2),
        ('b2', 'aa/1', 9),
        ('b2', 'ab/2', 9),
        ('b1', 'a/x.txt', 3),
        ('b1', 'a/y.txt', 4),
        ('b2', 'q/1', 9),
        ('b1', 'm.txt', 5),
        ('b1', 'n.txt', 6),
    ]
    listing = str(tmp_path / 'l.parquet')
    pd.DataFrame({
        'bucket': [b for b, _, _ in rows],
        'name': [n for _, n, _ in rows],
        'size_bytes': [s for _, _, s in rows],
        'created': [TS] * len(rows),
        'storage_class_id': [1] * len(rows),
    }).to_parquet(listing, row_group_size=2)

    for bucket, n_files in [('b1', 6), ('b2', 4)]:
        got_stream, stats = _stream((listing,), tmp_path, bucket=bucket)
        con = duckdb.connect()
        ooc = str(tmp_path / f'ooc-{bucket}.parquet')
        aggregate_listing_to_parquet(
            prepare_listing(con, (listing,)),
            bucket=bucket, scheme='gcs', out_parquet=ooc, con=con,
        )
        got_duckdb = _normalize(read_listing(ooc))
        pd.testing.assert_frame_equal(got_stream, got_duckdb)
        assert stats['files'] == n_files


def test_lazy_merge_bounds_open_sources(tmp_path: Path):
    """`heapq.merge` primed every run source up front — at fleet scale
    (thousands of bin-packed runs) that's EMFILE plus a row-group read buffer
    per run. The lazy merge opens a source only when the merge horizon reaches
    its first key and drops it at exhaustion, bounding concurrently-open
    sources by the runs' range-overlap depth. Here: 4 runs across 2 shards,
    pairwise overlapping with depth 2 → high-water must be 2, not 4."""
    a = [(f'{i:03d}.txt', i + 1) for i in [*range(20, 30, 2), *range(0, 10, 2)]]
    b = [(f'{i:03d}.txt', i + 1) for i in [*range(21, 31, 2), *range(1, 11, 2)]]
    la = _write_listing(tmp_path / 'a.parquet', a, sort=False)
    lb = _write_listing(tmp_path / 'b.parquet', b, sort=False)

    got_stream, stats = _stream((f'{tmp_path}/[ab].parquet',), tmp_path)
    assert stats['max_open_sources'] == 2

    con = duckdb.connect()
    ooc = str(tmp_path / 'ooc.parquet')
    aggregate_listing_to_parquet(
        prepare_listing(con, (f'{tmp_path}/[ab].parquet',)),
        bucket='b1', scheme='gcs', out_parquet=ooc, con=con,
    )
    pd.testing.assert_frame_equal(got_stream, _normalize(read_listing(ooc)))


def test_finalize_failure_preserves_parts_and_resumes(tmp_path: Path, capsys, monkeypatch):
    """A finalize-only failure keeps the streamed parts dir (a 63-minute
    stream pass must not re-run), and a rerun with the same output path
    resumes at the merge — skipping the stream pass entirely."""
    from disk_tree.find import aggregate_stream as mod
    listing = _write_listing(tmp_path / 'l.parquet', [('a/x', 1), ('b/y', 2)])
    out = tmp_path / 'out.parquet'

    def boom(*a, **kw):
        raise RuntimeError('injected finalize failure')

    monkeypatch.setattr(mod, '_finalize_parts', boom)
    with pytest.raises(RuntimeError):
        aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=str(out))
    err = capsys.readouterr().err
    assert 'streamed parts preserved at' in err.rsplit('\n', 2)[-2]
    parts_dir = Path(f'{out}.parts')
    assert (parts_dir / 'manifest.json').exists()

    monkeypatch.undo()
    # Same output path → the parts manifest is the resume token.
    stats = aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=str(out))
    assert 'stream pass skipped' in capsys.readouterr().err
    assert not parts_dir.exists()
    got = _normalize(read_listing(out))
    expected = _normalize(import_listing((listing,), bucket='b1', scheme='gcs').df)
    pd.testing.assert_frame_equal(got, expected)
    assert stats['rows'] == 5


def test_finalize_jobs_byte_identical(tmp_path: Path):
    """Parallel finalize (depth-fanned workers + parent re-batching) must
    produce byte-identical output to the serial path — including unsorted
    parts (prefix-sibling inversions), which take the run-merge path."""
    rows = [
        ('store-backup/old.bin', 10),
        ('store-backup/deep/x.bin', 11),
        ('store/a.bin', 12),
        ('store/deep/y.bin', 13),
        ('store2-backup/z.bin', 14),
        ('store2/w.bin', 15),
        ('a.txt', 1),
        ('sub/b.txt', 2),
        ('sub/deep/c.txt', 3),
        ('sub/deep/deeper/d.txt', 4),
    ]
    listing = _write_listing(tmp_path / 'l.parquet', rows)
    outs = []
    for j in (1, 4):
        out = str(tmp_path / f'out-j{j}.parquet')
        aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=out, jobs=j)
        outs.append(out)
    assert open(outs[0], 'rb').read() == open(outs[1], 'rb').read()


def test_boundary_rows_split_across_worker_ranges(tmp_path: Path):
    """Boundary rows (spanning dirs) are the only ones not already range-scoped.
    Splitting them across the workers' `[lo, hi)` is what makes every part
    attributable to one range, which is what lets the finalize fan out by
    (depth, worker) instead of by depth alone — the difference between
    eu-west4's finalize being bound by its 92.2M-row depth 9 or not."""
    from disk_tree.find.aggregate_stream import _write_boundary_parts
    # segs: spanning dirs keyed by path, each [size, mtime, n_desc, n_files,
    # n_children, pivots, mt_wsum]; '' is the root.
    segs = {
        '': [100, 5, 4, 4, 2, [], 0],
        'aaa': [40, 5, 2, 2, 1, [], 0],
        'mmm': [30, 5, 1, 1, 1, [], 0],
        'zzz': [30, 5, 1, 1, 1, [], 0],
    }
    parts, _ = _write_boundary_parts(
        str(tmp_path), segs, 'gs://b1', [], False,
        ranges=[(None, 'n'), ('n', None)],
    )
    got = sorted((p['depth'], p['w'], p['file'], p['rows']) for p in parts)
    assert got == [
        (0, 0, '0000-dir.b.w000.parquet', 1),   # root '.' → first range
        (1, 0, '0001-dir.b.w000.parquet', 2),   # 'aaa', 'mmm' < 'n'
        (1, 1, '0001-dir.b.w001.parquet', 1),   # 'zzz' >= 'n'
    ]
    # Without ranges (jobs=1) it stays one part per depth, still tagged w=0.
    parts1, _ = _write_boundary_parts(str(tmp_path), segs, 'gs://b1', [], False)
    assert sorted((p['depth'], p['w'], p['file']) for p in parts1) == [
        (0, 0, '0000-dir.b.parquet'),
        (1, 0, '0001-dir.b.parquet'),
    ]


def test_depth_stream_coalesces_alternation_slices(tmp_path: Path):
    """The dir↔file merge emits one slice per alternation. Consumers turn each
    into a chunk, so an uncoalesced stream made every flush walk ~10^5 chunks
    per column — 10/10 profiler samples in the flush block, 46 min on one
    eu-west4 depth. `_depth_stream` must hand out ~batch_rows-sized batches,
    not per-alternation slivers."""
    import pyarrow as pa
    import pyarrow.parquet as pq_
    from disk_tree.find.aggregate_stream import _depth_stream, _merge_batches, _part_batches
    n = 300
    dirs = [f'd{i:04d}' for i in range(n)]
    files = [f'd{i:04d}!' for i in range(n)]  # '!' < '/': alternates every row
    for kind, paths in (('dir', dirs), ('file', files)):
        pq_.write_table(
            pa.table({'path': paths, 'size': [1] * len(paths)}),
            str(tmp_path / f'0001-{kind}.parquet'),
        )
    kinds = {
        'dir': [{'file': '0001-dir.parquet', 'sorted': True}],
        'file': [{'file': '0001-file.parquet', 'sorted': True}],
    }
    # The raw merge shreds into ~1 batch per row...
    raw = list(_merge_batches(
        _part_batches(str(tmp_path / '0001-dir.parquet'), True, 1 << 16),
        _part_batches(str(tmp_path / '0001-file.parquet'), True, 1 << 16),
    ))
    assert len(raw) >= 2 * n - 1
    # ...but the depth stream coalesces to batch_rows-sized batches.
    out = list(_depth_stream(str(tmp_path), kinds, batch_rows=256))
    assert [rb.num_rows for rb in out] == [256, 256, 88]
    got = [v for rb in out for v in rb.column('path').to_pylist()]
    assert got == sorted(dirs + files)


def test_part_flush_size_does_not_reach_output(tmp_path: Path, monkeypatch):
    """`_PART_FLUSH_ROWS` is a pure memory knob: a worker holds one buffer per
    open (depth, kind) writer, so it multiplies by ~44 on a deep tree. It must
    not reach the published file — the finalize re-batches at `_FLUSH_ROWS`,
    which is what keeps output bytes a function of row content alone."""
    from disk_tree.find import aggregate_stream as ags
    rows = [(f'sub{i % 5}/deep/f{i:04d}.bin', i) for i in range(400)]
    listing = _write_listing(tmp_path / 'l.parquet', rows)
    outs = []
    for part_flush in (7, 1 << 15):
        monkeypatch.setattr(ags, '_PART_FLUSH_ROWS', part_flush)
        out = str(tmp_path / f'out-{part_flush}.parquet')
        aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=out)
        outs.append(out)
    assert open(outs[0], 'rb').read() == open(outs[1], 'rb').read()


@pytest.mark.parametrize('batch_rows', [1, 2, 3, 1 << 13])
def test_shard_read_is_batch_size_independent(tmp_path: Path, monkeypatch, batch_rows: int):
    """`_shard_rows` bounds its decode buffer via `batch_size` (one live batch
    per open merge source, ~1K open on a real bucket — pyarrow's 65536 default
    cost 13.6GB and an OOM kill). Run-sortedness checks, dirty-key filtering
    and `[lo, hi)` clipping all run per batch, so output must not depend on
    where batch edges land."""
    from disk_tree.find import aggregate_stream as ags
    rows = [
        ('a//b/c.txt', 1),   # dirty key (canonical a/b/c.txt)
        ('a/aa.txt', 2),
        ('a/b/d.txt', 4),
        ('a/z.txt', 8),
        ('sub/deep/e.txt', 16),
        ('sub/f.txt', 32),
    ]
    listing = _write_listing(tmp_path / 'l.parquet', rows, row_group_size=2)
    monkeypatch.setattr(ags, '_SHARD_BATCH_ROWS', batch_rows)
    out = str(tmp_path / f'out-{batch_rows}.parquet')
    aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=out)
    got = _normalize(read_listing(out))
    expected = _normalize(import_listing((listing,), bucket='b1', scheme='gcs').df)
    pd.testing.assert_frame_equal(got, expected)


def test_merge_batches_converts_keys_once_per_batch():
    """`_merge_batches` must convert each batch's key column to numpy exactly
    once. Re-converting the unconsumed tail after every split is quadratic in
    splits per batch, and the dir↔file merge splits once per directory
    (millions of times per depth on a real bucket) — it held the finalize at
    ~37K rows/s vs the stream pass's ~830K."""
    import pyarrow as pa
    from disk_tree.find.aggregate_stream import _merge_batches

    class CountingBatch:
        """Proxy counting key-column accesses (one `to_numpy` each)."""

        def __init__(self, rb, counter):
            self._rb, self._counter = rb, counter

        @property
        def num_rows(self):
            return self._rb.num_rows

        def column(self, name):
            self._counter[0] += 1
            return self._rb.column(name)

        def slice(self, *a):
            # Proxy the slice too, so conversions of a batch's tail after a
            # split are counted (the whole point of the assertion).
            return CountingBatch(self._rb.slice(*a), self._counter)

    dirs = [f'd{i:03d}' for i in range(50)]
    files = [f'd{i:03d}!' for i in range(50)]  # '!' < '/': fully interleaved
    counter = [0]
    batches = [
        CountingBatch(pa.record_batch({'path': pa.array(p)}), counter)
        for p in (dirs, files)
    ]
    out = list(_merge_batches(iter(batches[:1]), iter(batches[1:])))
    conversions = counter[0]
    got = [v for rb in out for v in (rb._rb if isinstance(rb, CountingBatch) else rb).column('path').to_pylist()]
    assert got == sorted(dirs + files)
    # One per input batch — NOT one per split (49 splits happen here).
    assert conversions == 2
    assert len(out) > 49


def test_finalize_worker_coalesces_row_groups(tmp_path: Path, monkeypatch):
    """A depth's dir↔file merge emits one slice per alternation (tens of
    millions on a real bucket). The worker must coalesce them into
    `_FLUSH_ROWS` row groups: a parquet writer holds per-row-group, per-column
    statistics in memory until close, so one row group per merge slice OOM'd a
    61GB node. Row groups must track rows, not slices."""
    import pyarrow as pa
    import pyarrow.parquet as pq_
    from disk_tree.find import aggregate_stream as ags
    # Dir and file parts whose paths fully interleave → the dir↔file merge
    # emits ~1 slice per row.
    dirs = [f'd{i:03d}' for i in range(30)]
    files = [f'd{i:03d}!' for i in range(30)]  # '!' < '/' so they alternate
    for kind, paths in (('dir', dirs), ('file', files)):
        pq_.write_table(
            pa.table({'path': paths, 'size': [1] * len(paths)}),
            str(tmp_path / f'0001-{kind}.parquet'),
        )
    kinds = {
        'dir': [{'file': '0001-dir.parquet', 'sorted': True}],
        'file': [{'file': '0001-file.parquet', 'sorted': True}],
    }
    monkeypatch.setattr(ags, '_FLUSH_ROWS', 16)
    out = str(tmp_path / 'depth.parquet')
    assert ags._finalize_depth_worker(str(tmp_path), kinds, 8, out) == out
    md = pq_.ParquetFile(out).metadata
    assert md.num_rows == 60
    # Ceiling of rows/_FLUSH_ROWS — never one row group per merge slice.
    assert md.num_row_groups == -(-60 // 16) == 4
    got = pq_.read_table(out).column('path').to_pylist()
    assert got == sorted(dirs + files)


def test_unsorted_part_multi_slice_sort(tmp_path: Path):
    """The unsorted-part sort takes in bounded index slices (a >2GiB string
    column overflows 32-bit offsets under `Table.sort_by` — hit on eu-west4's
    51M-row dir parts). batch_rows=2 forces many slices; order must still be
    globally sorted across slice boundaries."""
    import pyarrow as pa
    import pyarrow.parquet as pq_
    from disk_tree.find.aggregate_stream import _part_batches
    paths = ['m', 'z', 'a', 'q', 'b', 'x', 'c']
    part = str(tmp_path / 'part.parquet')
    pq_.write_table(pa.table({'path': paths, 'size': list(range(len(paths)))}), part)
    got = [r for rb in _part_batches(part, part_sorted=False, batch_rows=2) for r in rb.column('path').to_pylist()]
    assert got == sorted(paths)


def test_unsorted_part_many_runs_rechunks(tmp_path: Path):
    """Beyond `_RECHUNK_RUNS` runs the part is re-chunked to merge-budget-sized
    row groups before the run-merge (each run reader buffers one decoded row
    group; at `_FLUSH_ROWS`-sized groups that's ~50MB × runs — OOM'd a 61GB
    node). Rows must come out globally sorted with columns intact, and the
    `.rechunk` temp must be cleaned up."""
    import pyarrow as pa
    import pyarrow.parquet as pq_
    from disk_tree.find.aggregate_stream import _RECHUNK_RUNS, _detect_runs, _part_batches
    # 40 ascending blocks emitted in descending block order → 40 runs of 3.
    paths = [f'r{i:02d}-{j}' for i in reversed(range(40)) for j in range(3)]
    sizes = list(range(len(paths)))
    part = str(tmp_path / 'part.parquet')
    pq_.write_table(pa.table({'path': paths, 'size': sizes}), part, row_group_size=7)
    assert len(_detect_runs(part)) == 40 > _RECHUNK_RUNS
    got = [
        (p, s)
        for rb in _part_batches(part, part_sorted=False, batch_rows=5)
        for p, s in zip(rb.column('path').to_pylist(), rb.column('size').to_pylist())
    ]
    assert got == sorted(zip(paths, sizes))
    assert not (tmp_path / 'part.parquet.rechunk').exists()


def test_prefix_sibling_dir_inversion(tmp_path: Path):
    """Sibling dirs where one name is a proper prefix of the other with next
    char < '/' (`store` vs `store-backup`): `store-backup/`'s subtree sorts
    *before* `store/`, so its dir row pops first — the depth's dir part is
    genuinely unsorted and the finalize must sort it. Identity vs pandas locks
    the repair."""
    rows = [
        ('store-backup/old.bin', 10),
        ('store.old/x.bin', 7),
        ('store/a.bin', 1),
        ('store/b.bin', 2),
    ]
    listing = _write_listing(tmp_path / 'l.parquet', rows)
    got, _ = _stream((listing,), tmp_path)
    expected = _normalize(import_listing((listing,), bucket='b1', scheme='gcs').df)
    pd.testing.assert_frame_equal(got, expected)


def test_deep_chain(tmp_path: Path):
    """Many depths, ~one row each — exercises part-writer family churn and the
    depth-ascending finalize."""
    path = '/'.join(f'd{i:02d}' for i in range(40))
    listing = _write_listing(tmp_path / 'l.parquet', [(f'{path}/leaf.bin', 5), ('top.bin', 3)])
    got, stats = _stream((listing,), tmp_path)
    expected = _normalize(import_listing((listing,), bucket='b1', scheme='gcs').df)
    pd.testing.assert_frame_equal(got, expected)
    assert stats['rows'] == 43  # 2 files + 40 chain dirs + root


def test_same_path_file_and_dir_tiny_batches(tmp_path: Path, monkeypatch):
    """A key that is both a file and a dir name at the same depth is the merge
    tiebreak (dir row first); tiny flush/batch sizes force the boundary-split
    paths in `_merge_two_sorted` instead of whole-batch passthrough."""
    from disk_tree.find import aggregate_stream as mod
    monkeypatch.setattr(mod, '_FLUSH_ROWS', 2)
    rows = [
        ('a/x', 1), ('a/x/1.bin', 2), ('a/x/2.bin', 3),
        ('a/y', 4), ('b/x', 5), ('b/y/z.bin', 6), ('c.bin', 7),
    ]
    listing = _write_listing(tmp_path / 'l.parquet', rows)
    out = str(tmp_path / 'out.parquet')
    mod.aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=out)
    got = read_listing(out)
    expected = import_listing((listing,), bucket='b1', scheme='gcs').df
    # Raw (un-normalized) comparison: row order and column order must match
    # the pandas engine byte-for-byte, including the dir-before-file tiebreak.
    pd.testing.assert_frame_equal(
        got.reset_index(drop=True), expected.reset_index(drop=True), check_dtype=False,
    )


def test_essentially_unsorted_raises(tmp_path: Path, monkeypatch):
    """The run-count guard: input whose runs explode (≈ every row its own run)
    gets the clear duckdb hint instead of a degenerate 1-row-per-source merge."""
    import disk_tree.find.aggregate_stream as mod
    monkeypatch.setattr(mod, '_MAX_RUNS', 2)
    listing = _write_listing(
        tmp_path / 'l.parquet',
        [('z.txt', 1), ('y.txt', 2), ('x.txt', 3), ('w.txt', 4)],  # strictly descending
        sort=False,
    )
    with pytest.raises(ValueError, match=r"sorted runs across listing shards.*use `-e duckdb`"):
        aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=str(tmp_path / 'out.parquet'))


def test_missing_bucket_raises(tmp_path: Path):
    listing = _write_listing(tmp_path / 'l.parquet', [('a.txt', 1)])
    with pytest.raises(ValueError, match="no rows for bucket 'nope'"):
        aggregate_stream((listing,), bucket='nope', scheme='gcs', out_parquet=str(tmp_path / 'out.parquet'))


def test_non_raw_schema_raises(tmp_path: Path):
    """SII-shaped listing (size/timeCreated, no size_bytes) → clear error + duckdb hint."""
    listing = tmp_path / 'sii.parquet'
    pd.DataFrame({
        'bucket': ['b1'],
        'name': ['a.txt'],
        'size': [100],
        'timeCreated': [TS],
        'storageClass': ['STANDARD'],
    }).to_parquet(listing)
    with pytest.raises(ValueError, match=r"lacks required columns.*use `-e duckdb`"):
        aggregate_stream((str(listing),), bucket='b1', scheme='gcs', out_parquet=str(tmp_path / 'out.parquet'))


def test_empty_glob_raises(tmp_path: Path):
    with pytest.raises(ValueError, match='no files match listing glob'):
        aggregate_stream(
            (str(tmp_path / 'nope-*.parquet'),),
            bucket='b1', scheme='gcs', out_parquet=str(tmp_path / 'out.parquet'),
        )


# ---------- Earlier-source-wins across multiple listing globs ----------

def test_earlier_listing_wins(tmp_path: Path):
    """Bucket present in both globs → first glob's rows win (prepare_listing parity)."""
    fresh = _write_listing(tmp_path / 'fresh.parquet', [('new.txt', 111)])
    stale = _write_listing(tmp_path / 'stale.parquet', [('old.txt', 999)])
    got, stats = _stream((fresh, stale), tmp_path)
    assert stats['root_size'] == 111
    assert got[got.kind == 'file'].path.tolist() == ['new.txt']


def test_later_listing_fills_missing_bucket(tmp_path: Path):
    """Bucket only in the second glob → falls through to it."""
    other = _write_listing(tmp_path / 'other.parquet', [('x.txt', 5)], bucket='b_other')
    mine = _write_listing(tmp_path / 'mine.parquet', [('y.txt', 6)], bucket='b1')
    got, stats = _stream((other, mine), tmp_path)
    assert stats['root_size'] == 6
    assert got[got.kind == 'file'].path.tolist() == ['y.txt']


# ---------- The `//` order-perturbation case, isolated ----------

def test_dirty_keys_out_of_raw_order(tmp_path: Path):
    """Raw sort puts `a//b/c` before `a/aa`, but canonical `a/b/c` belongs
    after — without the dirty-key side-merge, dir `a/b` would be closed
    before its second file arrived. Locks the two-pass design."""
    rows = [
        ('a//b/c.txt', 1),   # canonical a/b/c.txt — arrives "early" in raw order
        ('a/aa.txt', 2),
        ('a/b/d.txt', 4),    # same canonical dir as the dirty key's target
        ('a/z.txt', 8),
    ]
    listing = _write_listing(tmp_path / 'l.parquet', rows)  # raw sort: a// first
    got_pandas = _normalize(import_listing((listing,), bucket='b1', scheme='gcs').df)
    got_stream, stats = _stream((listing,), tmp_path)
    pd.testing.assert_frame_equal(got_pandas, got_stream)
    ab = got_stream[got_stream.path == 'a/b'].iloc[0]
    assert int(ab['size']) == 5
    assert int(ab['n_files']) == 2


def test_trailing_slash_key(tmp_path: Path):
    """Keys ending in '/' (placeholder "dirs") canonicalize by stripping —
    they become zero-or-more-byte file rows at the stripped path."""
    rows = [
        ('pre/', 0),
        ('pre/x.txt', 10),
    ]
    listing = _write_listing(tmp_path / 'l.parquet', rows)
    got_pandas = _normalize(import_listing((listing,), bucket='b1', scheme='gcs').df)
    got_stream, _ = _stream((listing,), tmp_path)
    pd.testing.assert_frame_equal(got_pandas, got_stream)


# ---------- Keyspace-partitioned parallel streaming (-j) ----------
# spec: stream-partition-parallel.md — output must be byte-identical for any
# `jobs`, including adversarial boundary placements.

def _md5(p) -> str:
    import hashlib
    return hashlib.md5(Path(p).read_bytes()).hexdigest()


_ROOT_STATS = ['rows', 'files', 'root_size', 'root_n_desc', 'root_n_files', 'root_n_children', 'root_mtime']


def _assert_jobs_identical(tmp_path: Path, listing: str, jobs: int, monkeypatch=None, boundaries=None, **kw):
    """Aggregate with jobs=1 and jobs=N (optionally with forced boundaries);
    assert byte-identical outputs, identical root stats, and frame-identity
    vs the pandas engine."""
    if boundaries is not None:
        from disk_tree.find import aggregate_stream as mod
        monkeypatch.setattr(mod, '_choose_boundaries', lambda *a, **k: boundaries)
    out1 = str(tmp_path / 'j1.parquet')
    outn = str(tmp_path / 'jn.parquet')
    s1 = aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=out1, **kw)
    sn = aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=outn, jobs=jobs, **kw)
    assert _md5(out1) == _md5(outn)
    assert {k: s1[k] for k in _ROOT_STATS} == {k: sn[k] for k in _ROOT_STATS}
    got = _normalize(read_listing(outn))
    expected = _normalize(
        import_listing((listing,), bucket='b1', scheme='gcs',
                       pivot_sums=kw.get('pivot_sums', ()), mean_mtime=kw.get('mean_mtime', False)).df
        [got.columns]
    )
    pd.testing.assert_frame_equal(got, expected)
    return sn


_PARTITION_ROWS = [
    ('data/a/000.bin', 11), ('data/a/001.bin', 12), ('data/a/sub/002.bin', 13),
    ('data/a/sub/003.bin', 14), ('data/b/004.bin', 15), ('data/b/005.bin', 16),
    ('store-backup/old/006.bin', 17), ('store-backup/007.bin', 18),
    ('store.old/008.bin', 19),
    ('store/009.bin', 20), ('store/x/010.bin', 21), ('store/x/y/011.bin', 22),
    ('store/x/y/012.bin', 23), ('store/z/013.bin', 24),
    ('top.bin', 25),
    ('zz/deep/d1/d2/d3/014.bin', 26), ('zz/deep/d1/d2/d3/015.bin', 27), ('zz/016.bin', 28),
]


def test_partitioned_organic_boundaries(tmp_path: Path, capsys, monkeypatch):
    """Real end-to-end `-j 3`: checkpoint-quantile boundary selection (2-row
    pass-1 batches → one checkpoint per 2 rows), spawned workers, monoid
    reduce. The env var reaches spawned pass-1 workers; the attr patch covers
    the already-imported parent module."""
    from disk_tree.find import aggregate_stream as mod
    monkeypatch.setenv('DISK_TREE_SCAN_BATCH_ROWS', '2')
    monkeypatch.setattr(mod, '_SCAN_BATCH_ROWS', 2)
    listing = _write_listing(tmp_path / 'l.parquet', _PARTITION_ROWS, row_group_size=2)
    _assert_jobs_identical(tmp_path, listing, jobs=3)
    assert ', 3 partition(s)' in capsys.readouterr().err


def test_partition_boundary_mid_deep_subtree(tmp_path: Path, monkeypatch):
    """Boundaries deep inside subtrees force multi-level spanning-dir chains
    (every ancestor of the boundary key is assembled in the reduce)."""
    listing = _write_listing(tmp_path / 'l.parquet', _PARTITION_ROWS, row_group_size=2)
    _assert_jobs_identical(
        tmp_path, listing, jobs=3, monkeypatch=monkeypatch,
        boundaries=['data/a/sub/003.bin', 'store/x/y/012.bin', 'zz/deep/d1/d2/d3/015.bin'],
    )


def test_partition_boundary_prefix_sibling(tmp_path: Path, monkeypatch):
    """A boundary between `store-backup/…` and `store/…` splits the
    prefix-sibling inversion across workers: worker parts at the same depth
    overlap in plain path order and the finalize's dir-stream merge must
    interleave them (not just concatenate)."""
    listing = _write_listing(tmp_path / 'l.parquet', _PARTITION_ROWS, row_group_size=2)
    _assert_jobs_identical(
        tmp_path, listing, jobs=2, monkeypatch=monkeypatch,
        boundaries=['store.old/008.bin'],
    )


def test_partition_empty_range(tmp_path: Path, monkeypatch):
    """A boundary below the whole keyspace yields an empty first partition —
    its root-only segment must reduce away (no stat skew)."""
    listing = _write_listing(tmp_path / 'l.parquet', _PARTITION_ROWS, row_group_size=2)
    _assert_jobs_identical(
        tmp_path, listing, jobs=2, monkeypatch=monkeypatch,
        boundaries=['0', 'store/009.bin'],
    )


def test_partitioned_pivot_mean(tmp_path: Path, monkeypatch):
    """Pivot sums + `mtime_mean` across a boundary that splits a multi-class
    dir: the exact Σ mtime·size bigints and per-class byte sums must merge in
    the reduce, and `mean_of` must run on the merged totals."""
    rows = sorted([
        ('data/hot.bin', 1000, 10, 1),
        ('data/warm.bin', 2000, 20, 2),
        ('data/z/cold.bin', 4000, 5, 4),
        ('archive/a.bin', 8000, 1, 4),
        ('archive/b.bin', 8000, 3, 4),
        ('empty/marker', 0, 15, 1),
        ('top.txt', 500, 25, 1),
    ])
    listing = str(tmp_path / 'l.parquet')
    pd.DataFrame({
        'bucket': ['b1'] * len(rows),
        'name': [n for n, *_ in rows],
        'size_bytes': [s for _, s, *_ in rows],
        'created': [dt.datetime(2026, 7, d, tzinfo=dt.timezone.utc) for *_, d, _ in rows],
        'storage_class_id': [c for *_, c in rows],
    }).to_parquet(listing, row_group_size=2)
    _assert_jobs_identical(
        tmp_path, listing, jobs=2, monkeypatch=monkeypatch,
        boundaries=['data/warm.bin'],  # splits dir `data` (classes 1/2/4) mid-subtree
        pivot_sums=('storage_class_id',), mean_mtime=True,
    )


def test_partition_boundary_with_dirty_keys(tmp_path: Path, monkeypatch):
    """Dirty (`//`) keys route to workers by *canonical* position: raw
    `a//b/c.txt` sorts first but canonically belongs to the second partition
    (≥ `a/b`), where dir `a/b` also gets a clean file."""
    rows = [
        ('a//b/c.txt', 1),
        ('a/aa.txt', 2),
        ('a/b/d.txt', 4),
        ('a/z.txt', 8),
    ]
    listing = _write_listing(tmp_path / 'l.parquet', rows, row_group_size=1)
    sn = _assert_jobs_identical(
        tmp_path, listing, jobs=2, monkeypatch=monkeypatch,
        boundaries=['a/b/d.txt'],
    )
    assert sn['files'] == 4


# ---------- CLI --engine stream creates a scan row (subprocess isolation) ----------

def test_cli_engine_stream_creates_scan(tmp_path: Path):
    listing = _write_listing(tmp_path / 'listing.parquet', [('a.txt', 100), ('sub/b.txt', 200)])
    root = tmp_path / 'dt-root'
    env = {**os.environ, 'DISK_TREE_ROOT': str(root)}
    r = subprocess.run(
        [sys.executable, '-m', 'disk_tree.cli.main', 'import',
         '-e', 'stream', '-l', listing, '-b', 'b1', '-t', TS.isoformat()],
        env=env, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    conn = sqlite3.connect(root / 'disk-tree.db')
    rows = conn.execute("SELECT path, size, n_children, n_desc FROM scan").fetchall()
    conn.close()
    assert rows == [('gcs://b1', 300, 2, 4)]


def test_cli_out_dir_makes_resume_reachable(tmp_path: Path, monkeypatch):
    """The CLI's per-invocation temp name hides the `<out>.parts` resume token
    (mgu burn-in: an expensive stream pass re-ran even though its parts had
    survived). `--out-dir` gives the output a deterministic per-bucket name, so
    an interrupted finalize resumes at the merge — with identical output."""
    from disk_tree.cli.import_listing import import_bucket
    from disk_tree.find import aggregate_stream as mod
    from disk_tree.sqla.db import init
    from disk_tree.storage import get_backend, reset_backend

    listing = _write_listing(tmp_path / 'l.parquet', [('a/x', 1), ('b/y', 2)])
    out_dir = tmp_path / 'agg'
    monkeypatch.setenv('DISK_TREE_ROOT', str(tmp_path / 'dt-root'))
    reset_backend()
    db = init()
    db.create_all()

    def boom(*a, **kw):
        raise RuntimeError('injected finalize failure')

    monkeypatch.setattr(mod, '_finalize_parts', boom)
    with pytest.raises(RuntimeError):
        import_bucket(
            db=db, storage=get_backend(), con=None, engine='stream',
            listings=(str(listing),), bucket='b1', scheme='gcs', snap_time=TS,
            out_dir=str(out_dir),
        )
    # Deterministic name → the token is where a rerun will look for it.
    parts_dir = out_dir / 'gcs-b1.parquet.parts'
    assert sorted(p.name for p in out_dir.iterdir()) == ['gcs-b1.parquet.parts']
    assert (parts_dir / 'manifest.json').exists()

    monkeypatch.undo()
    monkeypatch.setenv('DISK_TREE_ROOT', str(tmp_path / 'dt-root'))
    scan = import_bucket(
        db=db, storage=get_backend(), con=None, engine='stream',
        listings=(str(listing),), bucket='b1', scheme='gcs', snap_time=TS,
        out_dir=str(out_dir),
    )
    assert not parts_dir.exists()
    assert (scan.path, scan.size, scan.n_children, scan.n_desc) == ('gcs://b1', 3, 2, 5)
    got = _normalize(get_backend().load(scan.blob))
    expected = _normalize(import_listing((str(listing),), bucket='b1', scheme='gcs').df)
    pd.testing.assert_frame_equal(got, expected)


def test_jobs_gt_1_from_unimportable_main_explains_itself(tmp_path: Path, monkeypatch):
    """A spawn pool whose workers can't re-import `__main__` dies with a bare
    BrokenProcessPool naming neither cause nor cure (mgu lost a resume to it).
    We translate it into an actionable message."""
    from concurrent.futures.process import BrokenProcessPool
    from disk_tree.find import aggregate_stream as mod

    listing = _write_listing(tmp_path / 'l.parquet', [('a/x', 1), ('b/y', 2)])

    class DeadPool:
        def __init__(self, *a, **kw):
            pass

        def map(self, *a, **kw):
            raise BrokenProcessPool('A process in the process pool was terminated abruptly')

        def submit(self, *a, **kw):
            raise BrokenProcessPool('A process in the process pool was terminated abruptly')

        def shutdown(self, *a, **kw):
            pass

    monkeypatch.setattr('concurrent.futures.ProcessPoolExecutor', DeadPool)
    with pytest.raises(RuntimeError) as exc:
        mod.aggregate_stream(
            (listing,), bucket='b1', scheme='gcs',
            out_parquet=str(tmp_path / 'out.parquet'), jobs=4,
        )
    msg = str(exc.value)
    assert msg.startswith('worker pool died immediately (jobs=4).')
    assert "if __name__ == '__main__':" in msg
    assert isinstance(exc.value.__cause__, BrokenProcessPool)


def _finalize_stage_line(err: str) -> str:
    """The single `finalize: …` stage line, minus its timestamp prefix."""
    lines = [ln.split('] ', 1)[1] for ln in err.strip().split('\n') if '] finalize: ' in ln]
    assert len(lines) == 1, err
    return lines[0]


def test_small_finalize_stays_serial_despite_jobs(tmp_path: Path, capsys):
    """Depth-worker spawn + temp round-trip costs more than depth parallelism
    saves on small inputs (10M rows: 3s serial vs 7s parallel). Below the
    threshold the finalize stays serial and says so."""
    listing = _write_listing(tmp_path / 'l.parquet', _PARTITION_ROWS, row_group_size=2)
    out = str(tmp_path / 'out.parquet')
    aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=out, jobs=2)
    line = _finalize_stage_line(capsys.readouterr().err)
    assert line == 'finalize: 7 depth(s) → 7 unit(s), serial (35 rows < 20,000,000 parallel threshold)'


def test_parallel_finalize_engages_above_threshold_with_identical_bytes(tmp_path: Path, capsys, monkeypatch):
    """Above the threshold the depth workers run — and the bytes are the same
    as the serial path's, which is what makes the switch safe to make on size."""
    from disk_tree.find import aggregate_stream as mod

    listing = _write_listing(tmp_path / 'l.parquet', _PARTITION_ROWS, row_group_size=2)
    serial_out = str(tmp_path / 'serial.parquet')
    aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=serial_out, jobs=2)
    capsys.readouterr()

    monkeypatch.setattr(mod, '_PARALLEL_FINALIZE_MIN_ROWS', 0)
    parallel_out = str(tmp_path / 'parallel.parquet')
    aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=parallel_out, jobs=2)
    assert _finalize_stage_line(capsys.readouterr().err) == (
        'finalize: 7 depth(s) → 7 unit(s) across 2 worker(s)'
    )
    assert _md5(serial_out) == _md5(parallel_out)


def test_within_depth_units_are_byte_identical(tmp_path: Path, monkeypatch, capsys):
    """The point of the (depth, keyspace-partition) unit: a depth splits across
    workers without moving a byte. Boundary rows are the delicate part — they
    are the only rows not already range-scoped, so a mis-assignment duplicates
    or drops them, and the manifest row count would not notice either."""
    from disk_tree.find import aggregate_stream as mod

    listing = _write_listing(tmp_path / 'l.parquet', _PARTITION_ROWS, row_group_size=2)
    out1 = str(tmp_path / 'j1.parquet')
    aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=out1)
    capsys.readouterr()

    # Force both the split points and the parallel finalize, on a fixture small
    # enough to reason about. `store/009.bin` splits mid-subtree, so spanning
    # dirs land in the boundary part — the case that matters.
    monkeypatch.setattr(mod, '_choose_boundaries', lambda *a, **k: ['store/009.bin', 'zz/016.bin'])
    monkeypatch.setattr(mod, '_PARALLEL_FINALIZE_MIN_ROWS', 0)
    outn = str(tmp_path / 'jn.parquet')
    stats = aggregate_stream((listing,), bucket='b1', scheme='gcs', out_parquet=outn, jobs=3)
    assert _finalize_stage_line(capsys.readouterr().err) == (
        'finalize: 7 depth(s) → 14 unit(s) across 3 worker(s)'
    )
    assert _md5(out1) == _md5(outn)

    got = _normalize(read_listing(outn))
    expected = _normalize(import_listing((listing,), bucket='b1', scheme='gcs').df[got.columns])
    pd.testing.assert_frame_equal(got, expected)
    assert stats['rows'] == len(got)
