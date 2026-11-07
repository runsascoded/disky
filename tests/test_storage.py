"""Tests for storage backends."""
import os
import tempfile

import pandas as pd
import pytest

from disk_tree.storage.base import PathStats
from disk_tree.storage.parquet import ParquetBackend
from disk_tree.storage.hybrid import HybridBackend


@pytest.fixture
def sample_df():
    """Create a sample scan DataFrame.

    n_desc = number of descendants (not including self)
    Files have n_desc=0, directories count their descendants.
    """
    return pd.DataFrame([
        {'path': '.', 'size': 1000, 'mtime': 1000.0, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 5, 'n_children': 2, 'depth': 0},
        {'path': 'foo', 'size': 400, 'mtime': 1001.0, 'kind': 'dir', 'parent': '.', 'uri': '/test/foo', 'n_desc': 2, 'n_children': 2, 'depth': 1},
        {'path': 'bar', 'size': 600, 'mtime': 1002.0, 'kind': 'dir', 'parent': '.', 'uri': '/test/bar', 'n_desc': 1, 'n_children': 1, 'depth': 1},
        {'path': 'foo/a.txt', 'size': 100, 'mtime': 1003.0, 'kind': 'file', 'parent': 'foo', 'uri': '/test/foo/a.txt', 'n_desc': 0, 'n_children': 0, 'depth': 2},
        {'path': 'foo/b.txt', 'size': 300, 'mtime': 1004.0, 'kind': 'file', 'parent': 'foo', 'uri': '/test/foo/b.txt', 'n_desc': 0, 'n_children': 0, 'depth': 2},
        {'path': 'bar/c.txt', 'size': 600, 'mtime': 1005.0, 'kind': 'file', 'parent': 'bar', 'uri': '/test/bar/c.txt', 'n_desc': 0, 'n_children': 0, 'depth': 2},
    ])


class TestParquetBackend:
    def test_save_and_load(self, sample_df):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = ParquetBackend(scans_dir=tmpdir)
            blob_ref = backend.save(sample_df, '/test')

            loaded = backend.load(blob_ref)
            assert len(loaded) == 6
            assert set(loaded['path'].tolist()) == {'.', 'foo', 'bar', 'foo/a.txt', 'foo/b.txt', 'bar/c.txt'}

    def test_depth_filtering(self, sample_df):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = ParquetBackend(scans_dir=tmpdir)
            blob_ref = backend.save(sample_df, '/test')

            # Load only depth 1
            loaded = backend.load(blob_ref, max_depth=1, min_depth=1)
            assert len(loaded) == 2
            assert set(loaded['path'].tolist()) == {'foo', 'bar'}

    def test_get_path_stats(self, sample_df):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = ParquetBackend(scans_dir=tmpdir)
            blob_ref = backend.save(sample_df, '/test')

            stats = backend.get_path_stats(blob_ref, 'foo')
            assert stats is not None
            assert stats.size == 400
            assert stats.n_desc == 2

    def test_does_not_support_updates(self, sample_df):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = ParquetBackend(scans_dir=tmpdir)
            assert backend.supports_updates is False
            blob_ref = backend.save(sample_df, '/test')
            result = backend.delete_path(blob_ref, 'foo/a.txt')
            assert result is None  # Not supported


class TestHybridBackend:
    def test_save_and_load_small(self, sample_df):
        """Small scans should not be chunked."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = HybridBackend(scans_dir=tmpdir, chunk_threshold=1000)
            blob_ref = backend.save(sample_df, '/test')

            loaded = backend.load(blob_ref)
            assert len(loaded) == 6
            assert set(loaded['path'].tolist()) == {'.', 'foo', 'bar', 'foo/a.txt', 'foo/b.txt', 'bar/c.txt'}

    def test_depth_filtering(self, sample_df):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = HybridBackend(scans_dir=tmpdir)
            blob_ref = backend.save(sample_df, '/test')

            loaded = backend.load(blob_ref, max_depth=1, min_depth=1)
            assert len(loaded) == 2
            assert set(loaded['path'].tolist()) == {'foo', 'bar'}

    def test_get_path_stats(self, sample_df):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = HybridBackend(scans_dir=tmpdir)
            blob_ref = backend.save(sample_df, '/test')

            stats = backend.get_path_stats(blob_ref, 'foo')
            assert stats is not None
            assert stats.size == 400
            assert stats.n_desc == 2

    def test_chunking_large_subtrees(self):
        """Large subtrees should be split into separate parquets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a DataFrame with a large subtree
            rows = [
                {'path': '.', 'size': 10000, 'mtime': 1000.0, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 105, 'n_children': 2, 'depth': 0},
                {'path': 'small', 'size': 100, 'mtime': 1001.0, 'kind': 'dir', 'parent': '.', 'uri': '/test/small', 'n_desc': 2, 'n_children': 1, 'depth': 1},
                {'path': 'small/a.txt', 'size': 100, 'mtime': 1002.0, 'kind': 'file', 'parent': 'small', 'uri': '/test/small/a.txt', 'n_desc': 1, 'n_children': 0, 'depth': 2},
                {'path': 'large', 'size': 9900, 'mtime': 1003.0, 'kind': 'dir', 'parent': '.', 'uri': '/test/large', 'n_desc': 102, 'n_children': 100, 'depth': 1},
            ]
            # Add 100 files under 'large'
            for i in range(100):
                rows.append({
                    'path': f'large/f{i}.txt',
                    'size': 99,
                    'mtime': 1004.0 + i,
                    'kind': 'file',
                    'parent': 'large',
                    'uri': f'/test/large/f{i}.txt',
                    'n_desc': 1,
                    'n_children': 0,
                    'depth': 2,
                })
            df = pd.DataFrame(rows)

            # Use low threshold to trigger chunking
            backend = HybridBackend(scans_dir=tmpdir, chunk_threshold=50)
            blob_ref = backend.save(df, '/test')

            # Check that chunking occurred
            chunk_stats = backend.get_chunk_stats(blob_ref)
            assert chunk_stats['total_chunks'] == 1
            assert chunk_stats['chunks'][0]['path'] == 'large'

            # Verify we can still access all data
            stats = backend.get_path_stats(blob_ref, 'large/f50.txt')
            assert stats is not None
            assert stats.size == 99

            # Loading without follow_refs should only get summary
            summary = backend.load(blob_ref, follow_refs=False)
            assert 'large/f0.txt' not in summary['path'].tolist()
            assert 'large' in summary['path'].tolist()

    def test_delete_in_chunked_subtree(self):
        """Deleting a path inside a chunked subtree should work."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # n_desc = number of descendants (not including self)
            # files have n_desc=0, big has 50 descendants, root has 51 (big + 50 files)
            rows = [
                {'path': '.', 'size': 1000, 'mtime': 1000.0, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 51, 'n_children': 1, 'depth': 0},
                {'path': 'big', 'size': 1000, 'mtime': 1001.0, 'kind': 'dir', 'parent': '.', 'uri': '/test/big', 'n_desc': 50, 'n_children': 50, 'depth': 1},
            ]
            for i in range(50):
                rows.append({
                    'path': f'big/f{i}.txt',
                    'size': 20,
                    'mtime': 1002.0 + i,
                    'kind': 'file',
                    'parent': 'big',
                    'uri': f'/test/big/f{i}.txt',
                    'n_desc': 0,  # files have no descendants
                    'n_children': 0,
                    'depth': 2,
                })
            df = pd.DataFrame(rows)

            backend = HybridBackend(scans_dir=tmpdir, chunk_threshold=10)
            assert backend.supports_updates is True
            blob_ref = backend.save(df, '/test')

            # Delete a file inside the chunked subtree
            stats = backend.delete_path(blob_ref, 'big/f0.txt')
            assert stats is not None
            assert stats.size == 20

            # Verify it's deleted
            assert backend.get_path_stats(blob_ref, 'big/f0.txt') is None

            # Verify parent stats were updated
            big_stats = backend.get_path_stats(blob_ref, 'big')
            assert big_stats.size == 980  # 1000 - 20
            assert big_stats.n_desc == 49  # 50 - 1

            # Verify root was updated
            root_stats = backend.get_path_stats(blob_ref, '.')
            assert root_stats.size == 980
            assert root_stats.n_desc == 50  # 51 - 1


def _mk_backend(kind: str, tmpdir: str):
    if kind == 'parquet':
        return ParquetBackend(scans_dir=tmpdir)
    if kind == 'hybrid':
        return HybridBackend(scans_dir=tmpdir)
    raise ValueError(kind)


@pytest.fixture
def prefix_adversarial_df():
    """Scan with siblings whose names are string-prefixes of `foo`+`<sep>`:
    `foo!x` sorts *before* `foo/…` (0x21 < 0x2f) and `foo0`/`foobar` sort
    *after* (0x30, 0x62 > 0x2f). A naive `[pfx, pfx+'0')` range would leak
    `foo!x` into the result; the exact semantics must not.
    """
    return pd.DataFrame([
        {'path': '.', 'size': 1500, 'mtime': 1000.0, 'kind': 'dir', 'parent': '', 'uri': '/t', 'n_desc': 8, 'n_children': 5, 'depth': 0},
        {'path': 'foo', 'size': 400, 'mtime': 1001.0, 'kind': 'dir', 'parent': '.', 'uri': '/t/foo', 'n_desc': 2, 'n_children': 2, 'depth': 1},
        {'path': 'foo!x', 'size': 10, 'mtime': 1002.0, 'kind': 'file', 'parent': '', 'uri': '/t/foo!x', 'n_desc': 0, 'n_children': 0, 'depth': 1},
        {'path': 'foo.bak', 'size': 20, 'mtime': 1003.0, 'kind': 'file', 'parent': '', 'uri': '/t/foo.bak', 'n_desc': 0, 'n_children': 0, 'depth': 1},
        {'path': 'foo0', 'size': 30, 'mtime': 1004.0, 'kind': 'file', 'parent': '', 'uri': '/t/foo0', 'n_desc': 0, 'n_children': 0, 'depth': 1},
        {'path': 'foobar', 'size': 940, 'mtime': 1005.0, 'kind': 'dir', 'parent': '.', 'uri': '/t/foobar', 'n_desc': 1, 'n_children': 1, 'depth': 1},
        {'path': 'foo/a.txt', 'size': 100, 'mtime': 1006.0, 'kind': 'file', 'parent': 'foo', 'uri': '/t/foo/a.txt', 'n_desc': 0, 'n_children': 0, 'depth': 2},
        {'path': 'foo/b.txt', 'size': 300, 'mtime': 1007.0, 'kind': 'file', 'parent': 'foo', 'uri': '/t/foo/b.txt', 'n_desc': 0, 'n_children': 0, 'depth': 2},
        {'path': 'foobar/z.txt', 'size': 940, 'mtime': 1008.0, 'kind': 'file', 'parent': 'foobar', 'uri': '/t/foobar/z.txt', 'n_desc': 0, 'n_children': 0, 'depth': 2},
    ])


BACKENDS = ['parquet', 'hybrid']


@pytest.mark.parametrize('kind', BACKENDS)
class TestPathPrefixFiltering:
    def test_prefix_excludes_prefix_named_siblings(self, kind, prefix_adversarial_df):
        """`path_prefix='foo'` = the `foo` row + descendants — never `foo!x`,
        `foo.bak`, `foo0`, or `foobar/…`."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _mk_backend(kind, tmpdir)
            blob_ref = backend.save(prefix_adversarial_df, '/t')
            loaded = backend.load(blob_ref, path_prefix='foo')
            assert sorted(loaded['path'].tolist()) == ['foo', 'foo/a.txt', 'foo/b.txt']

    def test_prefix_composes_with_depth(self, kind, prefix_adversarial_df):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _mk_backend(kind, tmpdir)
            blob_ref = backend.save(prefix_adversarial_df, '/t')
            loaded = backend.load(blob_ref, min_depth=2, max_depth=2, path_prefix='foo')
            assert sorted(loaded['path'].tolist()) == ['foo/a.txt', 'foo/b.txt']

    def test_prefix_matches_full_frame_mask(self, kind, prefix_adversarial_df):
        """Filtered load == unfiltered load + pandas mask, column-for-column."""
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _mk_backend(kind, tmpdir)
            blob_ref = backend.save(prefix_adversarial_df, '/t')
            full = backend.load(blob_ref)
            expected = full[(full['path'] == 'foobar') | full['path'].str.startswith('foobar/')]
            loaded = backend.load(blob_ref, path_prefix='foobar')
            pd.testing.assert_frame_equal(
                loaded.sort_values('path').reset_index(drop=True)[expected.columns],
                expected.sort_values('path').reset_index(drop=True),
            )

    def test_missing_prefix_returns_empty(self, kind, prefix_adversarial_df):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _mk_backend(kind, tmpdir)
            blob_ref = backend.save(prefix_adversarial_df, '/t')
            loaded = backend.load(blob_ref, path_prefix='nope')
            assert loaded['path'].tolist() == []


def test_hybrid_follow_refs_prefix_inside_chunk():
    """With `follow_refs=True` the prefix can't be pushed down (the chunk
    placeholder ancestor wouldn't survive a range filter); it must be applied
    as an exact mask *after* expansion."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = HybridBackend(scans_dir=tmpdir, chunk_threshold=10)
        rows = [
            {'path': '.', 'size': 1000, 'mtime': 1.0, 'kind': 'dir', 'parent': '', 'uri': '/t', 'n_desc': 21, 'n_children': 1, 'depth': 0},
            {'path': 'big', 'size': 1000, 'mtime': 1.0, 'kind': 'dir', 'parent': '.', 'uri': '/t/big', 'n_desc': 20, 'n_children': 20, 'depth': 1},
        ] + [
            {'path': f'big/f{i:02d}.txt', 'size': 50, 'mtime': 1.0, 'kind': 'file', 'parent': 'big', 'uri': f'/t/big/f{i:02d}.txt', 'n_desc': 0, 'n_children': 0, 'depth': 2}
            for i in range(20)
        ]
        blob_ref = backend.save(pd.DataFrame(rows), '/t')
        # Confirm `big` actually got chunked, or this test exercises nothing.
        summary = backend.load(blob_ref, follow_refs=False)
        assert summary.loc[summary['path'] == 'big', 'child_scan_id'].notna().all()
        loaded = backend.load(blob_ref, follow_refs=True, path_prefix='big')
        assert sorted(loaded['path'].tolist()) == ['big'] + [f'big/f{i:02d}.txt' for i in range(20)]


def test_hybrid_follow_refs_recomputes_depth():
    """Chunk expansion re-roots paths; a stale chunk-relative `depth` column
    made depth-trusting consumers (filter, ScanSource) mis-level every
    expanded row."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = HybridBackend(scans_dir=tmpdir, chunk_threshold=5)
        rows = [
            {'path': '.', 'size': 500, 'mtime': 1.0, 'kind': 'dir', 'parent': '', 'uri': '/t', 'n_desc': 11, 'n_children': 1, 'depth': 0},
            {'path': 'big', 'size': 500, 'mtime': 1.0, 'kind': 'dir', 'parent': '.', 'uri': '/t/big', 'n_desc': 10, 'n_children': 1, 'depth': 1},
            {'path': 'big/sub', 'size': 500, 'mtime': 1.0, 'kind': 'dir', 'parent': 'big', 'uri': '/t/big/sub', 'n_desc': 9, 'n_children': 9, 'depth': 2},
        ] + [
            {'path': f'big/sub/f{i}.txt', 'size': 50, 'mtime': 1.0, 'kind': 'file', 'parent': 'big/sub', 'uri': f'/t/big/sub/f{i}.txt', 'n_desc': 0, 'n_children': 0, 'depth': 3}
            for i in range(9)
        ]
        blob_ref = backend.save(pd.DataFrame(rows), '/t')
        loaded = backend.load(blob_ref, follow_refs=True)
        expected = loaded['path'].map(lambda p: 0 if p == '.' else p.count('/') + 1)
        assert loaded['depth'].tolist() == expected.tolist()
