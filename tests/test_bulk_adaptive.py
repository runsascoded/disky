"""Tests for adaptive range-splitting bulk listing (spec: adaptive-listing.md).

The engine runs against :class:`disk_tree.find.bulk_fake.FakeLister` — an
in-memory PagedLister with S3 semantics — so the full worker/donation/merge
machinery (including real multiprocessing in one smoke test) is exercised
with zero cloud SDKs. Correctness bar: the union of all shards equals the
key set exactly (any gap or dup breaks multiset equality).
"""

import json
from pathlib import Path

import pandas as pd
from disk_tree.blobfs import read_parquet as read_listing
import pytest

from disk_tree.find.bulk_adaptive import (
    DuplicateKeysError,
    _assert_no_duplicate_keys,
    key_midpoint,
    list_bucket_adaptive,
    load_warm_ranges,
    next_prefix,
)
from disk_tree.find.bulk_fake import FakeLister, _row


# ---------- key_midpoint / next_prefix ----------

@pytest.mark.parametrize('a,b', [
    ('a', 'z'),
    ('a', None),
    ('abc', 'abd'),                # adjacent last char → descend into a's branch
    ('a', 'b'),                    # adjacent first char
    ('datakit/f0001', None),
    ('datakit/f0001', 'datakit/f0002'),
    ('café/x', 'café/z'),          # non-ASCII common prefix
    ('a', 'a\x02'),
    ('', 'b'),
    ('zzz', None),
])
def test_key_midpoint_invariants(a, b):
    m = key_midpoint(a, b)
    assert m is not None, f"expected a split point between {a!r} and {b!r}"
    assert a < m, (a, m)
    if b is not None:
        assert m < b, (m, b)
    m.encode('utf-8')  # no surrogates / invalid code points


def test_key_midpoint_no_gap():
    # Nothing fits strictly between 'a' and 'a\x00'.
    assert key_midpoint('a', 'a\x00') is None


def test_key_midpoint_class_capped_descent():
    """Adjacent-bound descent stays within the character class — the split for
    ['…f0001399', '…f0002') bisects the digits, not the astral plane."""
    m = key_midpoint('datakit/f0001399', 'datakit/f0002')
    assert 'datakit/f0001399' < m < 'datakit/f0002'
    assert all(ord(c) < 0x80 for c in m), m
    # All-'9' tail: every descent position is at the class edge, so the split
    # degrades to a tiny valid sliver (astral tail) rather than None — still
    # correct (a < m < b), just unbalanced.
    m9 = key_midpoint('datakit/f0001999', 'datakit/f0002')
    assert 'datakit/f0001999' < m9 < 'datakit/f0002'


def test_open_split_hot_prefix():
    """Unbounded splits bisect at the *observed* divergence position: for keys
    sharing a hot prefix, the split lands inside the prefix (generic
    first-divergence bisection would land past the entire keyspace)."""
    from disk_tree.find.bulk_adaptive import open_split
    m = open_split('datakit/part000200.bin', 'datakit/part000399.bin')
    assert m is not None
    assert m.startswith('datakit/part000')          # split inside the hot prefix
    assert 'datakit/part000399.bin' < m
    assert all(ord(c) < 0x80 for c in m), m
    # At the class edge ('9'): no split this page — caller retries later.
    assert open_split('datakit/part000600', 'datakit/part000999') is None


def test_next_prefix():
    assert next_prefix('a/') == 'a0'          # '/' + 1 = '0'
    assert next_prefix('ab') == 'ac'
    assert next_prefix('') is None
    p = next_prefix('a\U0010FFFF')
    assert p == 'b'                            # carry past the max code point


# ---------- engine correctness (FakeLister, single process) ----------

_UNIFORM = tuple(f'k{i:04d}.bin' for i in range(300))
_SKEWED = tuple(
    [f'datakit/part{i:05d}.bin' for i in range(400)]        # 78%-style hot prefix
    + [f'logs/{i:03d}.log' for i in range(60)]
    + [f'top{i}.txt' for i in range(40)]
)
_UNICODE = tuple(
    [f'café/résumé{i:02d}.pdf' for i in range(30)]
    + [f'日本語/データ{i:02d}.csv' for i in range(30)]
    + [f'ascii/{i:02d}.txt' for i in range(30)]
)
_PLACEHOLDERS = tuple(['pre/', 'pre/x.txt', 'pre/y.txt', 'q.txt'])


def _run(keys, tmp_path: Path, out='listing', procs=1, threads=4, page_size=3, page_delay_s=0.0, **kw):
    out_dir = str(tmp_path / out)
    lister = FakeLister(keys=keys, page_size=page_size, page_delay_s=page_delay_s)
    total = list_bucket_adaptive(
        lister, bucket='b1', out_dir=out_dir, procs=procs, threads=threads, **kw,
    )
    df = read_listing(f'{out_dir}/*.parquet') if False else pd.concat(
        [read_listing(p) for p in sorted(Path(out_dir).glob('*.parquet'))],
        ignore_index=True,
    )
    success = json.loads((Path(out_dir) / '_SUCCESS.json').read_text())
    return total, df, success


def _expect(keys, prefix=None):
    ks = sorted(k for k in keys if prefix is None or k.startswith(prefix))
    rows = [_row(k) for k in ks]
    return pd.DataFrame({
        'name': [r.name for r in rows],
        'size_bytes': [r.size for r in rows],
    })


@pytest.mark.parametrize('keys', [_UNIFORM, _SKEWED, _UNICODE, _PLACEHOLDERS],
                         ids=['uniform', 'skewed', 'unicode', 'placeholders'])
def test_adaptive_lists_exactly(keys, tmp_path: Path):
    total, df, success = _run(keys, tmp_path)
    assert total == len(keys)
    got = df[['name', 'size_bytes']].sort_values('name').reset_index(drop=True)
    pd.testing.assert_frame_equal(got, _expect(keys))
    assert success['objects'] == len(keys)
    assert success['mode'] == 'adaptive'
    # Ranges partition the keyspace: counts add up.
    assert sum(n for _, _, n in success['ranges']) == len(keys)
    # The as-of window (spec mgu-scale-unification.md C): UTC, ordered, in the past.
    from datetime import datetime, timezone
    assert sorted(success) == ['bucket', 'finished', 'mode', 'objects', 'prefix', 'ranges', 'started']
    started, finished = (datetime.fromisoformat(success[k]) for k in ('started', 'finished'))
    assert started.tzinfo == timezone.utc and finished.tzinfo == timezone.utc
    assert started <= finished <= datetime.now(timezone.utc)


def test_splitting_actually_happens(tmp_path: Path):
    """With idle workers and pages that take time (simulated 2ms RTT), the
    single seed must split — the whole point of the feature."""
    _, _, success = _run(_SKEWED, tmp_path, threads=4, page_size=5, page_delay_s=0.002)
    assert len(success['ranges']) > 1


def test_prefix_mode(tmp_path: Path):
    total, df, success = _run(_SKEWED, tmp_path, prefix='datakit')
    assert total == 400
    got = df[['name', 'size_bytes']].sort_values('name').reset_index(drop=True)
    pd.testing.assert_frame_equal(got, _expect(_SKEWED, prefix='datakit/'))
    # All recorded ranges stay inside the prefix subtree.
    for s, e, _ in success['ranges']:
        assert s >= 'datakit/'
        assert e is None or e <= next_prefix('datakit/')


def test_warm_start(tmp_path: Path):
    _, _, s1 = _run(_SKEWED, tmp_path, out='run1')
    seeds = load_warm_ranges(str(tmp_path / 'run1'))
    assert seeds == [(s, e) for s, e, _ in s1['ranges']]

    total2, df2, s2 = _run(_SKEWED, tmp_path, out='run2', warm_ranges=seeds)
    assert total2 == len(_SKEWED)
    got = df2[['name', 'size_bytes']].sort_values('name').reset_index(drop=True)
    pd.testing.assert_frame_equal(got, _expect(_SKEWED))
    # Warm-started: at least the seeded ranges exist (splits may add more).
    assert len(s2['ranges']) >= len(seeds)


def test_exists_reuse_short_circuits(tmp_path: Path):
    total1, _, _ = _run(_UNIFORM, tmp_path, out='r')
    lister = FakeLister(keys=(), page_size=3)  # would list 0 if actually run
    total2 = list_bucket_adaptive(
        lister, bucket='b1', out_dir=str(tmp_path / 'r'), procs=1, threads=2, exists='reuse',
    )
    assert total2 == total1 == len(_UNIFORM)


def test_multiprocess_smoke(tmp_path: Path):
    """Real spawn-based multiprocessing over the shared queue (2 procs × 2
    threads): exact multiset identity still holds."""
    total, df, success = _run(_SKEWED, tmp_path, procs=2, threads=2, page_size=5)
    assert total == len(_SKEWED)
    got = df[['name', 'size_bytes']].sort_values('name').reset_index(drop=True)
    pd.testing.assert_frame_equal(got, _expect(_SKEWED))
    assert sum(n for _, _, n in success['ranges']) == len(_SKEWED)


# ---------- downstream synergy: adaptive shards → import -e stream ----------

def test_stream_import_over_adaptive_shards(tmp_path: Path):
    """Adaptive shards interleave ranges (piecewise-sorted) — exactly what
    `-e stream`'s run-splitting merge consumes. End-to-end identity vs the
    pandas engine over the same shards."""
    from disk_tree.find.aggregate_stream import aggregate_stream
    from disk_tree.find.import_listing import import_listing
    from test_aggregate_duckdb import _normalize

    _run(_SKEWED, tmp_path, out='listing', threads=4, page_size=5)
    glob = str(tmp_path / 'listing' / '*.parquet')

    out = str(tmp_path / 'l2.parquet')
    aggregate_stream((glob,), bucket='b1', scheme='s3', out_parquet=out)
    got_stream = _normalize(read_listing(out))
    got_pandas = _normalize(import_listing((glob,), bucket='b1', scheme='s3').df)
    pd.testing.assert_frame_equal(got_pandas, got_stream)


def test_duplicate_keys_assert_fires_and_leaves_shards(tmp_path: Path):
    """A shard set where one key appears twice must raise — and must leave the
    shards on disk (they are the only evidence of the race that produced them;
    see the 2026-08-18 -P16 run: 34% duplicates, correct `objects` count,
    exit 0)."""
    frame = pd.DataFrame({
        'name': ['a/1', 'a/2', 'b/1'],
        'size_bytes': [1, 2, 3],
    })
    frame.to_parquet(tmp_path / 'shard-00-0000.parquet', index=False)
    pd.DataFrame({'name': ['b/1', 'c/1'], 'size_bytes': [3, 4]}).to_parquet(
        tmp_path / 'shard-00-0001.parquet', index=False)

    with pytest.raises(DuplicateKeysError) as exc:
        _assert_no_duplicate_keys(str(tmp_path))
    assert str(exc.value) == (
        f'1 duplicate rows (20.0% of 5) in {tmp_path}/shard-*.parquet — shards '
        'left in place for debugging; no _SUCCESS.json written. '
        'Worst offenders: 2x b/1'
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        'shard-00-0000.parquet',
        'shard-00-0001.parquet',
    ]


def test_duplicate_keys_assert_passes_clean(tmp_path: Path):
    pd.DataFrame({'name': ['a/1', 'b/1'], 'size_bytes': [1, 2]}).to_parquet(
        tmp_path / 'shard-00-0000.parquet', index=False)
    _assert_no_duplicate_keys(str(tmp_path))  # no raise
