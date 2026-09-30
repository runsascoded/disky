"""Tests for the Flask API server."""
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from disk_tree.server import app


@pytest.fixture
def test_db_dir():
    """Create a temporary directory for test database and parquet files."""
    tmpdir = tempfile.mkdtemp()
    yield tmpdir
    shutil.rmtree(tmpdir)


@pytest.fixture
def test_client(test_db_dir, monkeypatch):
    """Create a test client with isolated database."""
    db_path = os.path.join(test_db_dir, 'disk-tree.db')
    scans_dir = os.path.join(test_db_dir, 'scans')
    os.makedirs(scans_dir)

    # Create database with scan table (including denormalized stats columns)
    conn = sqlite3.connect(db_path)
    conn.execute('''
        CREATE TABLE scan (
            id INTEGER NOT NULL PRIMARY KEY,
            path VARCHAR NOT NULL,
            time DATETIME NOT NULL,
            blob VARCHAR NOT NULL,
            error_count INTEGER,
            error_paths TEXT,
            size INTEGER,
            n_children INTEGER,
            n_desc INTEGER,
            mtime INTEGER
        )
    ''')
    conn.execute('CREATE INDEX ix_scan_path_time ON scan(path, time)')
    conn.commit()
    conn.close()

    # Patch the DB_PATH (server) and the config paths the diff index reads
    monkeypatch.setattr('disk_tree.server.DB_PATH', db_path)
    monkeypatch.setattr('disk_tree.config.SQLITE_PATH', db_path)
    monkeypatch.setattr('disk_tree.config.ROOT_DIR', test_db_dir)
    # no background index builds racing the tmp dir's teardown
    monkeypatch.setattr('disk_tree.server.AUTO_INDEX', False)

    # Clear cache before each test
    from disk_tree.server import clear_cache
    clear_cache()

    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client, db_path, scans_dir


def create_test_parquet(scans_dir: str, name: str, rows: list[dict]) -> str:
    """Create a test parquet file with given rows."""
    df = pd.DataFrame(rows)
    path = os.path.join(scans_dir, f'{name}.parquet')
    df.to_parquet(path)
    return path


class TestGetScans:
    """Tests for GET /api/scans endpoint."""

    def test_empty_db(self, test_client):
        """Returns empty list when no scans exist."""
        client, _, _ = test_client
        response = client.get('/api/scans')
        assert response.status_code == 200
        assert response.json == []

    def test_returns_scans(self, test_client):
        """Returns list of scans."""
        client, db_path, scans_dir = test_client

        # Create a parquet file
        parquet_path = create_test_parquet(scans_dir, 'test', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 1, 'n_children': 0},
        ])

        # Insert scan record
        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob) VALUES (?, ?, ?)',
            ('/test', '2025-01-01T12:00:00', parquet_path),
        )
        conn.commit()
        conn.close()

        response = client.get('/api/scans')
        assert response.status_code == 200
        scans = response.json
        assert len(scans) == 1
        assert scans[0]['path'] == '/test'
        assert scans[0]['time'] == '2025-01-01T12:00:00'

    def test_returns_most_recent_per_path(self, test_client):
        """Returns only the most recent scan for each path."""
        client, db_path, scans_dir = test_client

        parquet1 = create_test_parquet(scans_dir, 'test1', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 1, 'n_children': 0},
        ])
        parquet2 = create_test_parquet(scans_dir, 'test2', [
            {'path': '.', 'size': 2000, 'mtime': 200, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob) VALUES (?, ?, ?)',
            ('/test', '2025-01-01T12:00:00', parquet1),
        )
        conn.execute(
            'INSERT INTO scan (path, time, blob) VALUES (?, ?, ?)',
            ('/test', '2025-01-02T12:00:00', parquet2),
        )
        conn.commit()
        conn.close()

        response = client.get('/api/scans')
        assert response.status_code == 200
        scans = response.json
        assert len(scans) == 1
        assert scans[0]['time'] == '2025-01-02T12:00:00'


class TestGetScan:
    """Tests for GET /api/scan endpoint."""

    def test_scan_not_found(self, test_client):
        """Returns 404 for paths without scans."""
        client, _, _ = test_client
        response = client.get('/api/scan?uri=/nonexistent')
        # Falls through to filesystem listing if no scan, which returns scan_status='none'
        assert response.status_code == 200
        data = response.json
        assert data['scan_status'] == 'none'

    def test_exact_match(self, test_client):
        """Returns scan data for exact path match."""
        client, db_path, scans_dir = test_client

        parquet_path = create_test_parquet(scans_dir, 'test', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test/parent', 'n_desc': 2, 'n_children': 1},
            {'path': 'child', 'size': 500, 'mtime': 50, 'kind': 'dir', 'parent': '.', 'uri': '/test/parent/child', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob) VALUES (?, ?, ?)',
            ('/test/parent', '2025-01-01T12:00:00', parquet_path),
        )
        conn.commit()
        conn.close()

        response = client.get('/api/scan?uri=/test/parent')
        assert response.status_code == 200
        data = response.json
        assert data['scan_status'] == 'full'
        assert data['root']['size'] == 1000
        assert len(data['children']) == 1
        assert data['children'][0]['path'] == 'child'


class TestFresherChildPatching:
    """Tests for fresher child scan patching."""

    def test_patches_fresher_child_scan(self, test_client):
        """When child has newer scan, patches its stats into parent view."""
        client, db_path, scans_dir = test_client

        parent_time = datetime(2025, 1, 1, 12, 0, 0)
        child_time = parent_time + timedelta(hours=1)

        # Parent scan shows child with old stats (size=100)
        parent_parquet = create_test_parquet(scans_dir, 'parent', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test/parent', 'n_desc': 3, 'n_children': 2},
            {'path': 'child1', 'size': 100, 'mtime': 10, 'kind': 'dir', 'parent': '.', 'uri': '/test/parent/child1', 'n_desc': 1, 'n_children': 0},
            {'path': 'child2', 'size': 900, 'mtime': 90, 'kind': 'dir', 'parent': '.', 'uri': '/test/parent/child2', 'n_desc': 1, 'n_children': 0},
        ])

        # Child scan shows updated stats (size=200)
        child_parquet = create_test_parquet(scans_dir, 'child', [
            {'path': '.', 'size': 200, 'mtime': 20, 'kind': 'dir', 'parent': '', 'uri': '/test/parent/child1', 'n_desc': 2, 'n_children': 1},
            {'path': 'subfile.txt', 'size': 200, 'mtime': 20, 'kind': 'file', 'parent': '.', 'uri': '/test/parent/child1/subfile.txt', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test/parent', parent_time.isoformat(), parent_parquet, 1000, 2, 3),
        )
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test/parent/child1', child_time.isoformat(), child_parquet, 200, 1, 2),
        )
        conn.commit()
        conn.close()

        response = client.get('/api/scan?uri=/test/parent')
        assert response.status_code == 200
        data = response.json

        # Find child1 in children
        child1 = next((c for c in data['children'] if c['path'] == 'child1'), None)
        assert child1 is not None
        assert child1['size'] == 200, 'child1 size should be patched to 200'
        assert child1['patched'] is True, 'child1 should be marked as patched'
        assert child1['n_desc'] == 2, 'child1 n_desc should be patched'
        assert child1['n_children'] == 1, 'child1 n_children should be patched'

        # child2 should be unchanged
        child2 = next((c for c in data['children'] if c['path'] == 'child2'), None)
        assert child2 is not None
        assert child2['size'] == 900
        assert child2.get('patched') is not True

    def test_does_not_patch_older_child_scan(self, test_client):
        """When child has older scan, does not patch."""
        client, db_path, scans_dir = test_client

        parent_time = datetime(2025, 1, 2, 12, 0, 0)  # Parent is newer
        child_time = datetime(2025, 1, 1, 12, 0, 0)   # Child is older

        parent_parquet = create_test_parquet(scans_dir, 'parent', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test/parent', 'n_desc': 2, 'n_children': 1},
            {'path': 'child1', 'size': 100, 'mtime': 10, 'kind': 'dir', 'parent': '.', 'uri': '/test/parent/child1', 'n_desc': 1, 'n_children': 0},
        ])

        child_parquet = create_test_parquet(scans_dir, 'child', [
            {'path': '.', 'size': 200, 'mtime': 20, 'kind': 'dir', 'parent': '', 'uri': '/test/parent/child1', 'n_desc': 2, 'n_children': 1},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob) VALUES (?, ?, ?)',
            ('/test/parent', parent_time.isoformat(), parent_parquet),
        )
        conn.execute(
            'INSERT INTO scan (path, time, blob) VALUES (?, ?, ?)',
            ('/test/parent/child1', child_time.isoformat(), child_parquet),
        )
        conn.commit()
        conn.close()

        response = client.get('/api/scan?uri=/test/parent')
        assert response.status_code == 200
        data = response.json

        child1 = next((c for c in data['children'] if c['path'] == 'child1'), None)
        assert child1 is not None
        assert child1['size'] == 100, 'child1 size should NOT be patched (older scan)'
        assert child1.get('patched') is not True

    def test_does_not_patch_grandchild_scans(self, test_client):
        """Grandchild scans should not be patched into parent view (not transitive)."""
        client, db_path, scans_dir = test_client

        parent_time = datetime(2025, 1, 1, 12, 0, 0)
        grandchild_time = parent_time + timedelta(hours=1)

        parent_parquet = create_test_parquet(scans_dir, 'parent', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test/parent', 'n_desc': 3, 'n_children': 1},
            {'path': 'child', 'size': 500, 'mtime': 50, 'kind': 'dir', 'parent': '.', 'uri': '/test/parent/child', 'n_desc': 2, 'n_children': 1},
            {'path': 'child/grandchild', 'size': 100, 'mtime': 10, 'kind': 'dir', 'parent': 'child', 'uri': '/test/parent/child/grandchild', 'n_desc': 1, 'n_children': 0},
        ])

        grandchild_parquet = create_test_parquet(scans_dir, 'grandchild', [
            {'path': '.', 'size': 200, 'mtime': 20, 'kind': 'dir', 'parent': '', 'uri': '/test/parent/child/grandchild', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob) VALUES (?, ?, ?)',
            ('/test/parent', parent_time.isoformat(), parent_parquet),
        )
        conn.execute(
            'INSERT INTO scan (path, time, blob) VALUES (?, ?, ?)',
            ('/test/parent/child/grandchild', grandchild_time.isoformat(), grandchild_parquet),
        )
        conn.commit()
        conn.close()

        # Disable expand_single to test raw patching behavior
        response = client.get('/api/scan?uri=/test/parent&expand_single=false')
        assert response.status_code == 200
        data = response.json

        # child should NOT be patched (grandchild scans don't propagate)
        child = next((c for c in data['children'] if c['path'] == 'child'), None)
        assert child is not None
        assert child['size'] == 500, 'child size should NOT be patched from grandchild'
        assert child.get('patched') is not True


class TestAncestorScanRelativePaths:
    """Tests for viewing subdirectories of scans (ancestor scan case)."""

    def test_children_have_relative_paths(self, test_client):
        """When viewing subdir of a scan, children paths should be relative to viewed dir."""
        client, db_path, scans_dir = test_client

        # Scan at /test with nested structure
        parquet_path = create_test_parquet(scans_dir, 'test', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 5, 'n_children': 2},
            {'path': 'subdir', 'size': 600, 'mtime': 80, 'kind': 'dir', 'parent': '.', 'uri': '/test/subdir', 'n_desc': 3, 'n_children': 2},
            {'path': 'subdir/child1', 'size': 300, 'mtime': 60, 'kind': 'dir', 'parent': 'subdir', 'uri': '/test/subdir/child1', 'n_desc': 1, 'n_children': 0},
            {'path': 'subdir/child2', 'size': 300, 'mtime': 70, 'kind': 'file', 'parent': 'subdir', 'uri': '/test/subdir/child2', 'n_desc': 1, 'n_children': 0},
            {'path': 'other', 'size': 400, 'mtime': 90, 'kind': 'dir', 'parent': '.', 'uri': '/test/other', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob) VALUES (?, ?, ?)',
            ('/test', '2025-01-01T12:00:00', parquet_path),
        )
        conn.commit()
        conn.close()

        # View /test/subdir (a subdir of the scan at /test)
        response = client.get('/api/scan?uri=/test/subdir')
        assert response.status_code == 200
        data = response.json

        # Root should be '.'
        assert data['root']['path'] == '.'
        assert data['root']['size'] == 600

        # Children should have relative paths (child1, child2), NOT (subdir/child1, subdir/child2)
        child_paths = sorted([c['path'] for c in data['children']])
        assert child_paths == ['child1', 'child2'], f'Expected relative paths, got {child_paths}'

    def test_deeply_nested_subdir_paths(self, test_client):
        """Relative paths work correctly for deeply nested directories."""
        client, db_path, scans_dir = test_client

        # Scan at /root with deeply nested structure
        parquet_path = create_test_parquet(scans_dir, 'deep', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/root', 'n_desc': 5, 'n_children': 1},
            {'path': 'a', 'size': 800, 'mtime': 90, 'kind': 'dir', 'parent': '.', 'uri': '/root/a', 'n_desc': 4, 'n_children': 1},
            {'path': 'a/b', 'size': 600, 'mtime': 80, 'kind': 'dir', 'parent': 'a', 'uri': '/root/a/b', 'n_desc': 3, 'n_children': 1},
            {'path': 'a/b/c', 'size': 400, 'mtime': 70, 'kind': 'dir', 'parent': 'a/b', 'uri': '/root/a/b/c', 'n_desc': 2, 'n_children': 1},
            {'path': 'a/b/c/file.txt', 'size': 100, 'mtime': 60, 'kind': 'file', 'parent': 'a/b/c', 'uri': '/root/a/b/c/file.txt', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob) VALUES (?, ?, ?)',
            ('/root', '2025-01-01T12:00:00', parquet_path),
        )
        conn.commit()
        conn.close()

        # View /root/a/b (deeply nested), disable expand_single to test raw root selection
        response = client.get('/api/scan?uri=/root/a/b&expand_single=false')
        assert response.status_code == 200
        data = response.json

        assert data['root']['path'] == '.'
        assert data['root']['size'] == 600

        # Direct child should be 'c', not 'a/b/c'
        assert len(data['children']) == 1
        assert data['children'][0]['path'] == 'c', f'Expected "c", got {data["children"][0]["path"]}'

    def test_s3_subdir_relative_paths(self, test_client):
        """Relative paths work for S3 URIs when viewing subdir of scan."""
        client, db_path, scans_dir = test_client

        # Scan at s3://bucket with nested structure (simulating .dvc case)
        parquet_path = create_test_parquet(scans_dir, 's3bucket', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': 's3://bucket', 'n_desc': 4, 'n_children': 1},
            {'path': '.dvc', 'size': 800, 'mtime': 90, 'kind': 'dir', 'parent': '.', 'uri': 's3://bucket/.dvc', 'n_desc': 3, 'n_children': 2},
            {'path': '.dvc/files', 'size': 500, 'mtime': 80, 'kind': 'dir', 'parent': '.dvc', 'uri': 's3://bucket/.dvc/files', 'n_desc': 1, 'n_children': 0},
            {'path': '.dvc/cache', 'size': 300, 'mtime': 70, 'kind': 'dir', 'parent': '.dvc', 'uri': 's3://bucket/.dvc/cache', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob) VALUES (?, ?, ?)',
            ('s3://bucket', '2025-01-01T12:00:00', parquet_path),
        )
        conn.commit()
        conn.close()

        # View s3://bucket/.dvc (subdir of the scan)
        response = client.get('/api/scan?uri=s3://bucket/.dvc')
        assert response.status_code == 200
        data = response.json

        assert data['root']['path'] == '.'
        assert data['root']['uri'] == 's3://bucket/.dvc'

        # Children should be 'files' and 'cache', NOT '.dvc/files' and '.dvc/cache'
        child_paths = sorted([c['path'] for c in data['children']])
        assert child_paths == ['cache', 'files'], f'Expected relative paths, got {child_paths}'


class TestScanHistoryWithAncestors:
    """Tests for GET /api/scans/history including ancestor scans."""

    def test_returns_exact_match_scans(self, test_client):
        """Returns scans that exactly match the requested path."""
        client, db_path, scans_dir = test_client

        parquet_path = create_test_parquet(scans_dir, 'test', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test/subdir', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test/subdir', '2025-01-01T12:00:00', parquet_path, 1000, 0, 1),
        )
        conn.commit()
        conn.close()

        response = client.get('/api/scans/history?uri=/test/subdir')
        assert response.status_code == 200
        scans = response.json
        assert len(scans) == 1
        assert scans[0]['path'] == '/test/subdir'
        assert scans[0]['scan_path'] == '/test/subdir'

    def test_includes_ancestor_scans(self, test_client):
        """Returns ancestor scans that contain data for the requested path."""
        client, db_path, scans_dir = test_client

        # Parent scan at /test containing subdir
        parent_parquet = create_test_parquet(scans_dir, 'parent', [
            {'path': '.', 'size': 2000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 3, 'n_children': 2},
            {'path': 'subdir', 'size': 1000, 'mtime': 80, 'kind': 'dir', 'parent': '.', 'uri': '/test/subdir', 'n_desc': 1, 'n_children': 0},
            {'path': 'other', 'size': 1000, 'mtime': 90, 'kind': 'dir', 'parent': '.', 'uri': '/test/other', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test', '2025-01-01T12:00:00', parent_parquet, 2000, 2, 3),
        )
        conn.commit()
        conn.close()

        # Request history for /test/subdir - should include the parent scan
        response = client.get('/api/scans/history?uri=/test/subdir')
        assert response.status_code == 200
        scans = response.json
        assert len(scans) == 1
        assert scans[0]['path'] == '/test'  # Path is the scan's path
        assert scans[0]['scan_path'] == '/test'  # scan_path indicates source
        assert scans[0]['size'] == 1000  # Size extracted from parquet for /test/subdir

    def test_combines_exact_and_ancestor_scans(self, test_client):
        """Returns both exact match scans and ancestor scans."""
        client, db_path, scans_dir = test_client

        # Exact scan of /test/subdir
        exact_parquet = create_test_parquet(scans_dir, 'exact', [
            {'path': '.', 'size': 1500, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test/subdir', 'n_desc': 2, 'n_children': 1},
        ])

        # Ancestor scan at /test containing subdir
        ancestor_parquet = create_test_parquet(scans_dir, 'ancestor', [
            {'path': '.', 'size': 3000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 4, 'n_children': 2},
            {'path': 'subdir', 'size': 1000, 'mtime': 80, 'kind': 'dir', 'parent': '.', 'uri': '/test/subdir', 'n_desc': 1, 'n_children': 0},
            {'path': 'other', 'size': 2000, 'mtime': 90, 'kind': 'dir', 'parent': '.', 'uri': '/test/other', 'n_desc': 2, 'n_children': 1},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test/subdir', '2025-01-02T12:00:00', exact_parquet, 1500, 1, 2),
        )
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test', '2025-01-01T12:00:00', ancestor_parquet, 3000, 2, 4),
        )
        conn.commit()
        conn.close()

        response = client.get('/api/scans/history?uri=/test/subdir')
        assert response.status_code == 200
        scans = response.json
        assert len(scans) == 2

        # Sort by time to check both are present
        scan_paths = {s['scan_path'] for s in scans}
        assert scan_paths == {'/test/subdir', '/test'}

        # Verify sizes are correct for each
        exact_scan = next(s for s in scans if s['scan_path'] == '/test/subdir')
        ancestor_scan = next(s for s in scans if s['scan_path'] == '/test')
        assert exact_scan['size'] == 1500  # Direct from denormalized stats
        assert ancestor_scan['size'] == 1000  # Extracted from parquet


class TestCompareWithAncestorScans:
    """Tests for /api/compare endpoint with ancestor scans."""

    def test_compare_exact_match_scans(self, test_client):
        """Compare two scans that exactly match the requested path."""
        client, db_path, scans_dir = test_client

        parquet1 = create_test_parquet(scans_dir, 'scan1', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 2, 'n_children': 1},
            {'path': 'file.txt', 'size': 500, 'mtime': 80, 'kind': 'file', 'parent': '.', 'uri': '/test/file.txt', 'n_desc': 1, 'n_children': 0},
        ])
        parquet2 = create_test_parquet(scans_dir, 'scan2', [
            {'path': '.', 'size': 1500, 'mtime': 110, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 2, 'n_children': 1},
            {'path': 'file.txt', 'size': 1000, 'mtime': 90, 'kind': 'file', 'parent': '.', 'uri': '/test/file.txt', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test', '2025-01-01T12:00:00', parquet1, 1000, 1, 2),
        )
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test', '2025-01-02T12:00:00', parquet2, 1500, 1, 2),
        )
        conn.commit()
        conn.close()

        response = client.get('/api/compare?uri=/test&scan1=1&scan2=2')
        assert response.status_code == 200
        data = response.json

        assert data['uri'] == '/test'
        assert len(data['rows']) == 1
        assert data['rows'][0]['path'] == 'file.txt'
        assert data['rows'][0]['size_delta'] == 500
        assert data['summary']['changed'] == 1

    def test_compare_ancestor_scans_for_subdir(self, test_client):
        """Compare two ancestor scans when viewing a subdirectory."""
        client, db_path, scans_dir = test_client

        # Two scans at /test, we want to compare /test/subdir
        parquet1 = create_test_parquet(scans_dir, 'scan1', [
            {'path': '.', 'size': 2000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 4, 'n_children': 2},
            {'path': 'subdir', 'size': 1000, 'mtime': 80, 'kind': 'dir', 'parent': '.', 'uri': '/test/subdir', 'n_desc': 2, 'n_children': 1},
            {'path': 'subdir/child', 'size': 500, 'mtime': 70, 'kind': 'dir', 'parent': 'subdir', 'uri': '/test/subdir/child', 'n_desc': 1, 'n_children': 0},
            {'path': 'other', 'size': 1000, 'mtime': 90, 'kind': 'dir', 'parent': '.', 'uri': '/test/other', 'n_desc': 1, 'n_children': 0},
        ])
        parquet2 = create_test_parquet(scans_dir, 'scan2', [
            {'path': '.', 'size': 2500, 'mtime': 110, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 4, 'n_children': 2},
            {'path': 'subdir', 'size': 1500, 'mtime': 90, 'kind': 'dir', 'parent': '.', 'uri': '/test/subdir', 'n_desc': 2, 'n_children': 1},
            {'path': 'subdir/child', 'size': 1000, 'mtime': 85, 'kind': 'dir', 'parent': 'subdir', 'uri': '/test/subdir/child', 'n_desc': 1, 'n_children': 0},
            {'path': 'other', 'size': 1000, 'mtime': 90, 'kind': 'dir', 'parent': '.', 'uri': '/test/other', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test', '2025-01-01T12:00:00', parquet1, 2000, 2, 4),
        )
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test', '2025-01-02T12:00:00', parquet2, 2500, 2, 4),
        )
        conn.commit()
        conn.close()

        # Compare /test/subdir using the two ancestor scans
        response = client.get('/api/compare?uri=/test/subdir&scan1=1&scan2=2')
        assert response.status_code == 200
        data = response.json

        assert data['uri'] == '/test/subdir'
        assert data['scan1']['scan_path'] == '/test'
        assert data['scan2']['scan_path'] == '/test'
        assert data['scan1']['size'] == 1000  # Size of /test/subdir in scan1
        assert data['scan2']['size'] == 1500  # Size of /test/subdir in scan2

        # Should show child as changed
        assert len(data['rows']) == 1
        assert data['rows'][0]['path'] == 'child'
        assert data['rows'][0]['size_delta'] == 500
        assert data['rows'][0]['uri'] == '/test/subdir/child'

    def test_compare_detects_added_removed(self, test_client):
        """Compare detects added and removed items in ancestor scans."""
        client, db_path, scans_dir = test_client

        # scan1: subdir has child1
        parquet1 = create_test_parquet(scans_dir, 'scan1', [
            {'path': '.', 'size': 2000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 3, 'n_children': 1},
            {'path': 'subdir', 'size': 1000, 'mtime': 80, 'kind': 'dir', 'parent': '.', 'uri': '/test/subdir', 'n_desc': 2, 'n_children': 1},
            {'path': 'subdir/child1', 'size': 500, 'mtime': 70, 'kind': 'dir', 'parent': 'subdir', 'uri': '/test/subdir/child1', 'n_desc': 1, 'n_children': 0},
        ])
        # scan2: subdir has child2 (child1 removed, child2 added)
        parquet2 = create_test_parquet(scans_dir, 'scan2', [
            {'path': '.', 'size': 2000, 'mtime': 110, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 3, 'n_children': 1},
            {'path': 'subdir', 'size': 800, 'mtime': 90, 'kind': 'dir', 'parent': '.', 'uri': '/test/subdir', 'n_desc': 2, 'n_children': 1},
            {'path': 'subdir/child2', 'size': 300, 'mtime': 85, 'kind': 'file', 'parent': 'subdir', 'uri': '/test/subdir/child2', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test', '2025-01-01T12:00:00', parquet1, 2000, 1, 3),
        )
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test', '2025-01-02T12:00:00', parquet2, 2000, 1, 3),
        )
        conn.commit()
        conn.close()

        response = client.get('/api/compare?uri=/test/subdir&scan1=1&scan2=2')
        assert response.status_code == 200
        data = response.json

        assert data['summary']['added'] == 1
        assert data['summary']['removed'] == 1

        rows_by_status = {r['path']: r['status'] for r in data['rows']}
        assert rows_by_status.get('child1') == 'removed'
        assert rows_by_status.get('child2') == 'added'


class TestDeleteEndpoint:
    """Tests for POST /api/delete endpoint."""

    def test_delete_requires_path(self, test_client):
        """Returns 400 when path is not provided."""
        client, _, _ = test_client
        response = client.post('/api/delete', json={})
        assert response.status_code == 400
        assert 'Path is required' in response.json['error']

    def test_delete_requires_absolute_path(self, test_client):
        """Returns 400 for relative paths."""
        client, _, _ = test_client
        response = client.post('/api/delete', json={'path': 'relative/path'})
        assert response.status_code == 400
        assert 'absolute' in response.json['error'].lower()

    def test_delete_nonexistent_path(self, test_client):
        """Returns 200 for paths that don't exist (idempotent delete for cleaning stale entries)."""
        client, _, _ = test_client
        response = client.post('/api/delete', json={'path': '/nonexistent/path/12345'})
        # Delete is idempotent - returns 200 even for non-existent paths
        # This allows cleaning up stale scan entries when files are deleted externally
        assert response.status_code == 200
        assert response.json.get('already_deleted') is True

    def test_delete_file_success(self, test_client):
        """Successfully deletes a file and returns stats."""
        client, db_path, scans_dir = test_client

        # Create a file to delete
        test_file = os.path.join(scans_dir, 'to_delete.txt')
        with open(test_file, 'w') as f:
            f.write('test content')

        response = client.post('/api/delete', json={'path': test_file})
        assert response.status_code == 200
        data = response.json
        assert data['success'] is True
        assert data['path'] == test_file
        assert data['deleted_size'] > 0
        assert not os.path.exists(test_file)

    def test_delete_clears_cache(self, test_client):
        """Delete clears server caches so next request gets fresh data."""
        client, db_path, scans_dir = test_client

        # Create scan with a file
        parquet_path = create_test_parquet(scans_dir, 'test', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 2, 'n_children': 1},
            {'path': 'file.txt', 'size': 500, 'mtime': 80, 'kind': 'file', 'parent': '.', 'uri': '/test/file.txt', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test', '2025-01-01T12:00:00', parquet_path, 1000, 1, 2),
        )
        conn.commit()
        conn.close()

        # First request to populate cache
        response1 = client.get('/api/scan?uri=/test')
        assert response1.status_code == 200

        # Create file to delete (outside of scan data, just to trigger cache clear)
        test_file = os.path.join(scans_dir, 'deleteme.txt')
        with open(test_file, 'w') as f:
            f.write('x')

        # Delete should clear caches
        from disk_tree.server import _cache

        response = client.post('/api/delete', json={'path': test_file})
        assert response.status_code == 200

        # Server cache should be cleared
        assert len(_cache) == 0


class TestScanIdParameter:
    """Tests for scan_id parameter (time-travel feature)."""

    def test_scan_id_uses_specific_scan(self, test_client):
        """scan_id parameter uses the specified scan instead of latest."""
        client, db_path, scans_dir = test_client

        # Create two scans with different data
        parquet1 = create_test_parquet(scans_dir, 'old', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 1, 'n_children': 0},
        ])
        parquet2 = create_test_parquet(scans_dir, 'new', [
            {'path': '.', 'size': 2000, 'mtime': 200, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test', '2025-01-01T12:00:00', parquet1, 1000, 0, 1),
        )
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test', '2025-01-02T12:00:00', parquet2, 2000, 0, 1),
        )
        conn.commit()
        conn.close()

        # Without scan_id, should get latest (size=2000)
        response = client.get('/api/scan?uri=/test')
        assert response.status_code == 200
        assert response.json['root']['size'] == 2000

        # With scan_id=1, should get old scan (size=1000)
        response = client.get('/api/scan?uri=/test&scan_id=1')
        assert response.status_code == 200
        assert response.json['root']['size'] == 1000

    def test_scan_id_invalid_returns_error(self, test_client):
        """Returns 400 if scan_id doesn't cover the requested path."""
        client, db_path, scans_dir = test_client

        parquet = create_test_parquet(scans_dir, 'test', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/other', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob) VALUES (?, ?, ?)',
            ('/other', '2025-01-01T12:00:00', parquet),
        )
        conn.commit()
        conn.close()

        # Request /test with scan_id=1 which is for /other
        response = client.get('/api/scan?uri=/test&scan_id=1')
        assert response.status_code == 400
        assert 'does not cover' in response.json['error']


class TestDepthFiltering:
    """Tests for depth-based parquet filtering."""

    def test_depth_column_in_parquet(self, test_client):
        """Parquet files with depth column support efficient filtering."""
        client, db_path, scans_dir = test_client

        # Create parquet with depth column
        parquet_path = create_test_parquet(scans_dir, 'deep', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 4, 'n_children': 1, 'depth': 0},
            {'path': 'a', 'size': 800, 'mtime': 90, 'kind': 'dir', 'parent': '.', 'uri': '/test/a', 'n_desc': 3, 'n_children': 1, 'depth': 1},
            {'path': 'a/b', 'size': 600, 'mtime': 80, 'kind': 'dir', 'parent': 'a', 'uri': '/test/a/b', 'n_desc': 2, 'n_children': 1, 'depth': 2},
            {'path': 'a/b/c', 'size': 400, 'mtime': 70, 'kind': 'file', 'parent': 'a/b', 'uri': '/test/a/b/c', 'n_desc': 1, 'n_children': 0, 'depth': 3},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test', '2025-01-01T12:00:00', parquet_path, 1000, 1, 4),
        )
        conn.commit()
        conn.close()

        # Request should work - depth filtering is an implementation detail
        # Disable expand_single to test raw root without auto-expansion
        response = client.get('/api/scan?uri=/test&expand_single=false')
        assert response.status_code == 200
        assert response.json['root']['size'] == 1000
        assert len(response.json['children']) == 1
        assert response.json['children'][0]['path'] == 'a'

    def test_parquet_without_depth_still_works(self, test_client):
        """Parquet files without depth column still work (fallback to full load)."""
        client, db_path, scans_dir = test_client

        # Create parquet WITHOUT depth column
        parquet_path = create_test_parquet(scans_dir, 'nodepth', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 2, 'n_children': 1},
            {'path': 'child', 'size': 500, 'mtime': 80, 'kind': 'dir', 'parent': '.', 'uri': '/test/child', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test', '2025-01-01T12:00:00', parquet_path, 1000, 1, 2),
        )
        conn.commit()
        conn.close()

        response = client.get('/api/scan?uri=/test')
        assert response.status_code == 200
        assert response.json['root']['size'] == 1000


class TestCacheInvalidation:
    """Tests for cache behavior."""

    def test_cache_populated_on_request(self, test_client):
        """Requests populate the cache."""
        client, db_path, scans_dir = test_client

        parquet_path = create_test_parquet(scans_dir, 'test', [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 1, 'n_children': 0},
        ])

        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
            ('/test', '2025-01-01T12:00:00', parquet_path, 1000, 0, 1),
        )
        conn.commit()
        conn.close()

        from disk_tree.server import clear_cache
        clear_cache()

        response = client.get('/api/scan?uri=/test')
        assert response.status_code == 200
        # Request should succeed (caching is now internal to storage backend)


class TestGetHistogram:
    """Tests for GET /api/histogram endpoint (spec: viz-widgets.md §4)."""

    @staticmethod
    def _seed(db_path: str, scans_dir: str, path: str = '/test') -> None:
        """A scan of `path` with two dirs and a loose file at known mtimes."""
        parquet_path = create_test_parquet(scans_dir, 'hist', [
            {'path': '.', 'size': 300, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': path, 'n_desc': 6, 'n_children': 3, 'depth': 0},
            {'path': 'a', 'size': 210, 'mtime': 100, 'kind': 'dir', 'parent': '.', 'uri': f'{path}/a', 'n_desc': 2, 'n_children': 2, 'depth': 1},
            {'path': 'b', 'size': 60, 'mtime': 50, 'kind': 'dir', 'parent': '.', 'uri': f'{path}/b', 'n_desc': 1, 'n_children': 1, 'depth': 1},
            {'path': 'loose.txt', 'size': 30, 'mtime': 75, 'kind': 'file', 'parent': '.', 'uri': f'{path}/loose.txt', 'n_desc': 0, 'n_children': 0, 'depth': 1},
            {'path': 'a/old.bin', 'size': 200, 'mtime': 0, 'kind': 'file', 'parent': 'a', 'uri': f'{path}/a/old.bin', 'n_desc': 0, 'n_children': 0, 'depth': 2},
            {'path': 'a/new.bin', 'size': 10, 'mtime': 100, 'kind': 'file', 'parent': 'a', 'uri': f'{path}/a/new.bin', 'n_desc': 0, 'n_children': 0, 'depth': 2},
            {'path': 'b/mid.bin', 'size': 60, 'mtime': 50, 'kind': 'file', 'parent': 'b', 'uri': f'{path}/b/mid.bin', 'n_desc': 0, 'n_children': 0, 'depth': 2},
        ])
        conn = sqlite3.connect(db_path)
        conn.execute(
            'INSERT INTO scan (path, time, blob, size, n_children, n_desc, mtime) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (path, '2025-01-01T12:00:00', parquet_path, 300, 3, 6, 100),
        )
        conn.commit()
        conn.close()

    def test_bins_bytes_per_child(self, test_client):
        client, db_path, scans_dir = test_client
        self._seed(db_path, scans_dir)

        response = client.get('/api/histogram?uri=/test&bins=4')
        assert response.status_code == 200
        assert response.json == {
            'uri': '/test',
            'scan_path': '/test',
            'time': '2025-01-01T12:00:00',
            'edges': [0, 25, 50, 75, 100],
            'children': [
                {'path': 'a', 'kind': 'dir', 'bytes': [200, 0, 0, 10], 'total_bytes': 210, 'n_files': 2},
                {'path': 'b', 'kind': 'dir', 'bytes': [0, 0, 60, 0], 'total_bytes': 60, 'n_files': 1},
                {'path': 'loose.txt', 'kind': 'file', 'bytes': [0, 0, 0, 30], 'total_bytes': 30, 'n_files': 1},
            ],
            'omitted': 0,
            'omitted_bytes': 0,
        }

    def test_drills_into_subdir_of_an_ancestor_scan(self, test_client):
        client, db_path, scans_dir = test_client
        self._seed(db_path, scans_dir)

        response = client.get('/api/histogram?uri=/test/a&bins=2')
        assert response.status_code == 200
        body = response.json
        assert body['scan_path'] == '/test'
        assert body['edges'] == [0, 50, 100]
        assert body['children'] == [
            {'path': 'old.bin', 'kind': 'file', 'bytes': [200, 0], 'total_bytes': 200, 'n_files': 1},
            {'path': 'new.bin', 'kind': 'file', 'bytes': [0, 10], 'total_bytes': 10, 'n_files': 1},
        ]

    def test_limit_reports_omitted_children(self, test_client):
        client, db_path, scans_dir = test_client
        self._seed(db_path, scans_dir)

        response = client.get('/api/histogram?uri=/test&bins=2&limit=1')
        assert response.status_code == 200
        body = response.json
        assert [c['path'] for c in body['children']] == ['a']
        assert (body['omitted'], body['omitted_bytes']) == (2, 90)

    def test_limit_0_returns_all_children(self, test_client):
        client, db_path, scans_dir = test_client
        self._seed(db_path, scans_dir)

        response = client.get('/api/histogram?uri=/test&bins=2&limit=0')
        assert response.status_code == 200
        assert [c['path'] for c in response.json['children']] == ['a', 'b', 'loose.txt']

    def test_missing_scan_is_404(self, test_client):
        client, _, _ = test_client
        response = client.get('/api/histogram?uri=/nope')
        assert response.status_code == 404
        assert response.json == {'error': 'No scan found for path', 'uri': '/nope'}

    def test_invalid_bins_is_400(self, test_client):
        client, db_path, scans_dir = test_client
        self._seed(db_path, scans_dir)

        response = client.get('/api/histogram?uri=/test&bins=0')
        assert response.status_code == 400
        assert response.json == {'error': 'bins must be >= 1; got 0'}


class TestCompareRecursive:
    """`/api/compare?recursive=1` — best-first frontier across depths."""

    def _seed(self, db_path, scans_dir):
        base = [
            {'path': '.', 'size': 1000, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 4, 'n_children': 2, 'depth': 0},
            {'path': 'a', 'size': 400, 'mtime': 90, 'kind': 'dir', 'parent': '.', 'uri': '/test/a', 'n_desc': 1, 'n_children': 1, 'depth': 1},
            {'path': 'b', 'size': 600, 'mtime': 100, 'kind': 'dir', 'parent': '.', 'uri': '/test/b', 'n_desc': 1, 'n_children': 1, 'depth': 1},
            {'path': 'a/f.txt', 'size': 400, 'mtime': 90, 'kind': 'file', 'parent': 'a', 'uri': '/test/a/f.txt', 'n_desc': 0, 'n_children': 0, 'depth': 2},
            {'path': 'b/g.bin', 'size': 600, 'mtime': 100, 'kind': 'file', 'parent': 'b', 'uri': '/test/b/g.bin', 'n_desc': 0, 'n_children': 0, 'depth': 2},
        ]
        changed = [dict(r) for r in base]
        for r in changed:
            if r['path'] in ('.', 'b', 'b/g.bin'):
                r['size'] += 1000
                r['mtime'] = 110
        p1 = create_test_parquet(scans_dir, 'rec1', base)
        p2 = create_test_parquet(scans_dir, 'rec2', changed)
        conn = sqlite3.connect(db_path)
        conn.execute('INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
                     ('/test', '2025-01-01T12:00:00', p1, 1000, 2, 4))
        conn.execute('INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
                     ('/test', '2025-01-02T12:00:00', p2, 2000, 2, 4))
        conn.commit()
        conn.close()

    def test_recursive_returns_frontier_across_depths(self, test_client):
        client, db_path, scans_dir = test_client
        self._seed(db_path, scans_dir)

        response = client.get('/api/compare?uri=/test&scan1=1&scan2=2&recursive=1')
        assert response.status_code == 200
        data = response.json
        assert data['recursive'] is True
        assert [
            (r['path'], r['depth'], r['status'], r['size_delta'], r['expanded'], r['pruned'])
            for r in data['rows']
        ] == [
            ('b', 1, 'changed', 1000, True, False),
            ('b/g.bin', 2, 'changed', 1000, False, False),
        ]
        assert data['rows'][0]['uri'] == '/test/b'
        assert data['summary'] == {
            'added': 0, 'removed': 0, 'changed': 2, 'touched': 0, 'unchanged': 0,
            'total_delta': 1000, 'expansions': 2, 'truncated': False,
        }
        # no index (auto-build is off in tests): the walk answered
        assert data['index'] == {'status': 'none'}
        # `a` was compared (and pruned) while expanding the root: it ships as
        # labeled grey context rather than vanishing.
        assert data['unchanged'] == {
            'top': [{
                'path': 'a', 'uri': '/test/a', 'depth': 1, 'kind': 'dir', 'status': 'unchanged',
                'size_a': 400, 'size_b': 400, 'size_delta': 0, 'size_old': 400,
                'n_desc_a': 1, 'n_desc_b': 1, 'n_desc_delta': 0, 'n_desc_old': 1,
                'expanded': False, 'pruned': False,
            }],
            'rest': {},
        }

    def test_recursive_budget_prunes(self, test_client):
        client, db_path, scans_dir = test_client
        self._seed(db_path, scans_dir)

        response = client.get('/api/compare?uri=/test&scan1=1&scan2=2&recursive=1&budget=1')
        assert response.status_code == 200
        data = response.json
        assert [
            (r['path'], r['status'], r['expanded'], r['pruned'])
            for r in data['rows']
        ] == [('b', 'changed', False, True)]
        assert data['summary']['truncated'] is True

    def test_recursive_served_from_index_when_built(self, test_client):
        """With a persisted diff index for the pair, the response is a slice
        of it (`index.status == 'done'`): same frontier, plus the unchanged
        sibling as context, no walk budget."""
        client, db_path, scans_dir = test_client
        self._seed(db_path, scans_dir)
        conn = sqlite3.connect(db_path)
        p1, p2 = [r[0] for r in conn.execute('SELECT blob FROM scan ORDER BY id')]
        conn.close()
        from disk_tree.diff_index import build_and_record
        row = build_and_record(1, p1, 2, p2)
        assert row['status'] == 'done'
        # `.`, `b`, `b/g.bin` changed + `a` as context
        assert (row['n_rows'], row['n_added'], row['n_removed'], row['n_changed'], row['n_touched']) == (4, 0, 0, 3, 0)

        status = client.get('/api/diff/status?scan1=1&scan2=2').json
        assert status['status'] == 'done' and status['n_rows'] == 4

        data = client.get('/api/compare?uri=/test&scan1=1&scan2=2&recursive=1').json
        assert data['index']['status'] == 'done'
        assert [
            (r['path'], r['depth'], r['status'], r['size_delta'], r['expanded'], r['pruned'])
            for r in data['rows']
        ] == [
            ('b', 1, 'changed', 1000, True, False),
            ('b/g.bin', 2, 'changed', 1000, False, False),
        ]
        assert [(r['path'], r['status'], r['size_b']) for r in data['unchanged']['top']] == [('a', 'unchanged', 400)]
        assert data['summary'] == {
            'added': 0, 'removed': 0, 'changed': 2, 'touched': 0, 'unchanged': 0,
            'total_delta': 1000, 'expansions': 1, 'truncated': False,
        }

        # a subpath is a slice too
        sub = client.get('/api/compare?uri=/test/b&scan1=1&scan2=2&recursive=1').json
        assert sub['index']['status'] == 'done'
        assert [(r['path'], r['depth'], r['status']) for r in sub['rows']] == [('g.bin', 1, 'changed')]

    def test_non_recursive_shape_unchanged(self, test_client):
        client, db_path, scans_dir = test_client
        self._seed(db_path, scans_dir)

        response = client.get('/api/compare?uri=/test&scan1=1&scan2=2')
        assert response.status_code == 200
        data = response.json
        assert 'recursive' not in data
        assert [(r['path'], r['status'], r['size_delta']) for r in data['rows']] == [
            ('b', 'changed', 1000),
            ('a', 'unchanged', 0),
        ]


class TestFilterEndpoint:
    """`/api/filter` — recursive filter with true re-aggregation."""

    def _seed(self, db_path, scans_dir):
        rows = [
            {'path': '.', 'size': 1450, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': '/test', 'n_desc': 9, 'n_children': 3, 'depth': 0},
            {'path': 'a', 'size': 700, 'mtime': 100, 'kind': 'dir', 'parent': '.', 'uri': '/test/a', 'n_desc': 4, 'n_children': 2, 'depth': 1},
            {'path': 'b', 'size': 730, 'mtime': 100, 'kind': 'dir', 'parent': '.', 'uri': '/test/b', 'n_desc': 3, 'n_children': 1, 'depth': 1},
            {'path': 'other.txt', 'size': 20, 'mtime': 100, 'kind': 'file', 'parent': '', 'uri': '/test/other.txt', 'n_desc': 0, 'n_children': 0, 'depth': 1},
            {'path': 'a/demo', 'size': 500, 'mtime': 100, 'kind': 'dir', 'parent': 'a', 'uri': '/test/a/demo', 'n_desc': 1, 'n_children': 1, 'depth': 2},
            {'path': 'a/noise.txt', 'size': 200, 'mtime': 100, 'kind': 'file', 'parent': 'a', 'uri': '/test/a/noise.txt', 'n_desc': 0, 'n_children': 0, 'depth': 2},
            {'path': 'b/x', 'size': 730, 'mtime': 100, 'kind': 'dir', 'parent': 'b', 'uri': '/test/b/x', 'n_desc': 2, 'n_children': 1, 'depth': 2},
            {'path': 'a/demo/demo.txt', 'size': 500, 'mtime': 100, 'kind': 'file', 'parent': 'a/demo', 'uri': '/test/a/demo/demo.txt', 'n_desc': 0, 'n_children': 0, 'depth': 3},
            {'path': 'b/x/demo.dat', 'size': 730, 'mtime': 100, 'kind': 'file', 'parent': 'b/x', 'uri': '/test/b/x/demo.dat', 'n_desc': 0, 'n_children': 0, 'depth': 3},
        ]
        p = create_test_parquet(scans_dir, 'filt', rows)
        conn = sqlite3.connect(db_path)
        conn.execute('INSERT INTO scan (path, time, blob, size, n_children, n_desc) VALUES (?, ?, ?, ?, ?, ?)',
                     ('/test', '2025-01-01T12:00:00', p, 1450, 3, 9))
        conn.commit()
        conn.close()

    def test_filter_reaggregates_outermost_matches(self, test_client):
        client, db_path, scans_dir = test_client
        self._seed(db_path, scans_dir)

        response = client.get('/api/filter?uri=/test&q=demo')
        assert response.status_code == 200
        data = response.json
        # a/demo (dir) matches outermost — demo.txt inside it must not re-count.
        assert [(r['path'], r['size'], r['n_matches'], r['matched']) for r in data['rows']] == [
            ('a', 500, 1, False),
            ('b', 730, 1, False),
            ('a/demo', 500, 1, True),
            ('b/x', 730, 1, False),
            ('b/x/demo.dat', 730, 1, True),
        ]
        assert data['total_size'] == 1230
        assert data['n_matches'] == 2
        assert data['rows'][0]['uri'] == '/test/a'

    def test_filter_under_subdir_uri(self, test_client):
        client, db_path, scans_dir = test_client
        self._seed(db_path, scans_dir)

        response = client.get('/api/filter?uri=/test/b&q=demo')
        assert response.status_code == 200
        data = response.json
        assert [(r['path'], r['size'], r['matched']) for r in data['rows']] == [
            ('x', 730, False),
            ('x/demo.dat', 730, True),
        ]
        assert data['total_size'] == 730
        assert data['n_matches'] == 1

    def test_filter_regex_and_display_depth(self, test_client):
        client, db_path, scans_dir = test_client
        self._seed(db_path, scans_dir)

        response = client.get('/api/filter?uri=/test&q=' + r'/\.dat$/' + '&depth=1')
        assert response.status_code == 200
        data = response.json
        assert [(r['path'], r['size'], r['matched']) for r in data['rows']] == [
            ('b', 730, False),
        ]
        assert data['total_size'] == 730
        assert data['n_matches'] == 1
        assert data['max_depth_scanned'] == 3

    def test_filter_stream_sse(self, test_client):
        """`/api/filter/stream` — one cumulative snapshot per depth; the final
        `done` event equals the plain endpoint's response."""
        client, db_path, scans_dir = test_client
        self._seed(db_path, scans_dir)

        plain = client.get('/api/filter?uri=/test&q=demo').json

        response = client.get('/api/filter/stream?uri=/test&q=demo')
        assert response.status_code == 200
        assert response.mimetype == 'text/event-stream'
        events = [
            json.loads(line[len('data: '):])
            for line in response.get_data(as_text=True).splitlines()
            if line.startswith('data: ')
        ]
        assert events[0] == {'phase': 'loading'}
        depth_events = events[1:-1]
        assert [(e['depth'], e['n_matches'], e['total_size'], e['done']) for e in depth_events] == [
            (1, 0, 0, False),
            (2, 1, 500, False),      # a/demo lands
            (3, 2, 1230, False),     # b/x/demo.dat lands
        ]
        final = events[-1]
        assert final['done'] is True
        assert {k: v for k, v in final.items() if k != 'done'} == plain

    def test_filter_indexed_parity(self, test_client):
        """With a vocab sidecar next to the blob, `/api/filter` takes the
        indexed path (`indexed: true`) and returns exactly the brute rows."""
        client, db_path, scans_dir = test_client
        self._seed(db_path, scans_dir)

        brute = client.get('/api/filter?uri=/test&q=demo').json
        assert brute['indexed'] is False

        from disk_tree.server import clear_cache
        from disk_tree.sidecar import build_vocab_sidecar
        build_vocab_sidecar(os.path.join(scans_dir, 'filt.parquet'))
        clear_cache()

        indexed = client.get('/api/filter?uri=/test&q=demo').json
        assert indexed['indexed'] is True
        assert indexed['rows'] == brute['rows']
        assert (indexed['total_size'], indexed['n_matches']) == (brute['total_size'], brute['n_matches'])

    def test_filter_indexed_subdir_and_path_mode(self, test_client):
        client, db_path, scans_dir = test_client
        self._seed(db_path, scans_dir)
        from disk_tree.sidecar import build_vocab_sidecar
        build_vocab_sidecar(os.path.join(scans_dir, 'filt.parquet'))

        data = client.get('/api/filter?uri=/test/b&q=demo').json
        assert data['indexed'] is True
        assert [(r['path'], r['size'], r['matched']) for r in data['rows']] == [
            ('x', 730, False),
            ('x/demo.dat', 730, True),
        ]
        assert (data['total_size'], data['n_matches']) == (730, 1)

        # A query containing '/' matches full paths — the name index can't
        # serve it, so the endpoint falls back to the brute scan.
        pm = client.get('/api/filter?uri=/test&q=x/demo').json
        assert pm['indexed'] is False
        assert [(r['path'], r['matched']) for r in pm['rows']] == [
            ('b', False),
            ('b/x', False),
            ('b/x/demo.dat', True),
        ]
        assert (pm['total_size'], pm['n_matches']) == (730, 1)


class TestCapabilities:
    """GET /api/capabilities — the live server can do everything (the static
    Pages deployment answers the same keys mostly off; `ui/src/api.ts`)."""

    def test_all_on(self, test_client):
        client, _, _ = test_client
        response = client.get('/api/capabilities')
        assert response.status_code == 200
        assert response.json == {
            'static': False,
            'scan': True, 'delete': True, 'reveal': True, 'histogram': True, 'filter': True,
            'preview': True, 'compare': True, 'progress': True, 'library': True, 'backend': True, 's3': True,
            'stageDelete': True, 'deleteApproval': 'sync',
        }


class TestStagedApi:
    """`/api/staged` sizes each item (and reports its `kind`); `/api/dispatch`
    takes a `uris` subset (spec `staged-page-ux.md` §2–3)."""

    def test_sizes_items_and_dispatches_a_subset(self, test_client, tmp_path):
        from disk_tree.backends import canonical
        client, _, _ = test_client
        a, b = tmp_path / 'a.bin', tmp_path / 'b.bin'
        a.write_bytes(b'\0' * 10)
        b.write_bytes(b'\0' * 20)
        ca, cb = canonical(str(a)), canonical(str(b))

        r = client.post('/api/plans/stage', json={'uris': [str(a), str(b)]})
        assert (r.status_code, r.get_json()['added']) == (200, [ca, cb])

        # no scan covers tmp_path yet: items size as (0, 0)
        plan = client.get('/api/staged').get_json()['plans'][0]
        assert [(plan['state'], plan['items'])] == [('open', [
            {'uri': ca, 'bytes': 0, 'objects': 0, 'kind': None},
            {'uri': cb, 'bytes': 0, 'objects': 0, 'kind': None},
        ])]

        # a scan of the parent covers both: each item is sized from its row
        _, db_path, scans_dir = test_client
        blob = create_test_parquet(scans_dir, 'cover', [
            {'path': '.', 'size': 30, 'mtime': 100, 'kind': 'dir', 'parent': '', 'uri': str(tmp_path), 'n_desc': 2, 'n_children': 2, 'depth': 0},
            {'path': 'a.bin', 'size': 10, 'mtime': 100, 'kind': 'file', 'parent': '.', 'uri': str(a), 'n_desc': 0, 'n_children': 0, 'depth': 1},
            {'path': 'b.bin', 'size': 20, 'mtime': 100, 'kind': 'file', 'parent': '.', 'uri': str(b), 'n_desc': 0, 'n_children': 0, 'depth': 1},
        ])
        conn = sqlite3.connect(db_path)
        conn.execute('INSERT INTO scan (path, time, blob, size, n_desc, n_children) VALUES (?, ?, ?, ?, ?, ?)',
                     (str(tmp_path), '2026-09-25T00:00:00', blob, 30, 2, 2))
        conn.commit()
        conn.close()
        plan = client.get('/api/staged').get_json()['plans'][0]
        assert plan['items'] == [
            {'uri': ca, 'bytes': 10, 'objects': 1, 'kind': 'file'},
            {'uri': cb, 'bytes': 20, 'objects': 1, 'kind': 'file'},
        ]

        bad = client.post('/api/dispatch', json={'uris': [str(tmp_path / 'nope')]})
        assert (bad.status_code, bad.get_json()) == (400, {'error': f"not staged in plan {plan['id']}: {canonical(str(tmp_path / 'nope'))}"})
        assert a.exists() and b.exists()

        one = client.post('/api/dispatch', json={'uris': [str(a)]})
        assert one.status_code == 200
        j = one.get_json()
        assert (j['plan_id'], j['mode'], j['items'], j['state'], j['bytes'], j['objects']) == (plan['id'], 'real', 1, 'done', 10, 1)
        assert (a.exists(), b.exists()) == (False, True)

        after = client.get('/api/staged').get_json()['plans']
        assert [(p['id'], p['state'], [it['uri'] for it in p['items']]) for p in after] == [(plan['id'], 'open', [cb])]

        rest = client.post('/api/dispatch', json={'uris': [str(b)]})
        assert (rest.status_code, rest.get_json()['state']) == (200, 'done')
        assert not b.exists()
        assert client.get('/api/staged').get_json()['plans'] == []   # closed once empty
