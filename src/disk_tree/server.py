import json
import mimetypes
import re
from os import listdir, makedirs, remove, stat
from os.path import abspath, basename, dirname, exists, isabs, isdir, isfile, join
import shutil
import sqlite3
import subprocess
import os
import threading
import time
import uuid
from datetime import datetime

import pandas as pd
from flask import Flask, jsonify, redirect, request, g, Response, send_file, send_from_directory, url_for
from flask_cors import CORS

from disk_tree import config as _config
from disk_tree import library
from disk_tree.config import SQLITE_PATH
from disk_tree import blobfs
from disk_tree.diff import ScanSource, recursive_diff, resolve_blob, resolve_chunk_for_path
from disk_tree.diff_index import DIFF_TABLE_SQL, build_and_record, get_index, load_index_slice, serve_slice
from disk_tree.filter import DEFAULT_DISPLAY_DEPTH, filter_scan, rebase_frame
from disk_tree.listing_format import write_listing
from disk_tree.registry import freshest_scan_covering
from disk_tree.storage import get_backend
from disk_tree.storage.base import BLOB_ROW_GROUP_SIZE

app = Flask(__name__)
CORS(app)

DB_PATH = abspath(SQLITE_PATH)

# Default max_rows for treemap performance
DEFAULT_MAX_ROWS = 2000


def init_db():
    """Initialize the database with required tables if they don't exist."""
    from os.path import dirname
    makedirs(dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                time DATETIME NOT NULL,
                blob TEXT NOT NULL,
                error_count INTEGER,
                error_paths TEXT,
                size INTEGER,
                n_children INTEGER,
                n_desc INTEGER,
                mtime INTEGER
            )
        ''')
        # Best-effort migration: pre-existing DBs may be missing the mtime column.
        try:
            conn.execute('ALTER TABLE scan ADD COLUMN mtime INTEGER')
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.execute('''
            CREATE INDEX IF NOT EXISTS ix_scan_path_time ON scan (path, time)
        ''')
        conn.execute(DIFF_TABLE_SQL)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scan_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                pid INTEGER NOT NULL,
                started DATETIME NOT NULL,
                items_found INTEGER NOT NULL DEFAULT 0,
                items_per_sec INTEGER,
                error_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running'
            )
        ''')
        conn.commit()
    finally:
        conn.close()


# Initialize database on module load
init_db()


# Static file serving for bundled UI
# Check multiple locations: PyInstaller bundle, packaged static/, dev ui/dist/
import sys as _sys
_this_dir = dirname(abspath(__file__))
_static_candidates = [
    join(_this_dir, 'static'),           # Packaged: disk_tree/static/
    join(_this_dir, '..', '..', 'ui', 'dist'),  # Dev: ../../ui/dist from src/disk_tree/
]
# Frozen (PyInstaller): a frozen `__file__` isn't a real dir; data is staged
# under sys._MEIPASS. We add-data the UI as `disk_tree/static` (see the spec).
if getattr(_sys, 'frozen', False) and hasattr(_sys, '_MEIPASS'):
    _static_candidates.insert(0, join(_sys._MEIPASS, 'disk_tree', 'static'))
# Several may exist in a dev checkout (a stale packaged `static/` from the last
# wheel build beside a fresh `ui/dist`): serve whichever `index.html` is newest.
_static_found = [c for c in _static_candidates if exists(join(c, 'index.html'))]
STATIC_DIR = abspath(max(_static_found, key=lambda c: os.path.getmtime(join(c, 'index.html')))) if _static_found else None

# Track in-progress scans: {job_id: {path, status, started, output, error}}
running_scans: dict[str, dict] = {}

# Simple TTL cache for expensive operations
_cache: dict[str, tuple[float, any]] = {}
CACHE_TTL = 60  # seconds

def load_scan_data(
    blob_ref: str,
    max_depth: int | None = None,
    min_depth: int | None = None,
    follow_refs: bool = False,
    path_prefix: str | None = None,
) -> pd.DataFrame:
    """Load scan data via the storage backend with optional depth/path-prefix filtering."""
    backend = get_backend()
    return backend.load(blob_ref, max_depth=max_depth, min_depth=min_depth, follow_refs=follow_refs, path_prefix=path_prefix)


def cached(key: str, ttl: int = CACHE_TTL):
    """Decorator for caching expensive function results with TTL."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            now = time.time()
            if key in _cache:
                cached_time, cached_result = _cache[key]
                if now - cached_time < ttl:
                    return cached_result
            result = fn(*args, **kwargs)
            _cache[key] = (now, result)
            return result
        return wrapper
    return decorator


def invalidate_cache(key: str):
    """Remove a key from the cache."""
    _cache.pop(key, None)


def clear_cache():
    """Clear all cached data."""
    _cache.clear()


def _on_root_change():
    """Re-point raw-sqlite `DB_PATH`, ensure its schema, drop caches — on a library swap.

    A freshly-opened library has no DB yet; `init_db` creates the tables so the
    raw-sqlite reads (`/api/scans` et al.) return empty rather than 500 on a
    missing `scan` table.
    """
    global DB_PATH
    DB_PATH = abspath(_config.SQLITE_PATH)
    init_db()
    clear_cache()


_config.on_root_change(_on_root_change)


def get_db():
    """Get database connection for current request."""
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error):
    """Close database connection at end of request."""
    db = g.pop('db', None)
    if db is not None:
        db.close()


def _fetch_scans_data():
    """Fetch all scans with stats from SQLite (denormalized columns)."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        # Get most recent scan for each path with denormalized stats
        cursor = db.execute('''
            SELECT s.*
            FROM scan s
            INNER JOIN (
                SELECT path, MAX(time) as max_time
                FROM scan
                GROUP BY path
            ) latest ON s.path = latest.path AND s.time = latest.max_time
            ORDER BY s.time DESC
        ''')
        result = []
        for row in cursor:
            result.append({
                'id': row['id'],
                'path': row['path'],
                'time': row['time'],
                'blob': row['blob'],
                'error_count': row['error_count'],
                'error_paths': row['error_paths'],
                'size': row['size'],
                'n_children': row['n_children'],
                'n_desc': row['n_desc'],
            })
        return result
    finally:
        db.close()


@app.route('/api/scans')
def get_scans():
    """Return list of most recent scan per path, with root stats."""
    # Check cache first (60 second TTL)
    cache_key = 'scans_list'
    now = time.time()
    if cache_key in _cache:
        cached_time, cached_result = _cache[cache_key]
        if now - cached_time < CACHE_TTL:
            return jsonify(cached_result)

    result = _fetch_scans_data()
    _cache[cache_key] = (now, result)
    return jsonify(result)


def _fetch_s3_buckets_data():
    """Fetch S3 buckets with scan stats (expensive, should be cached)."""
    result = subprocess.run(
        ['aws', 's3', 'ls'],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or 'Failed to list buckets')

    buckets = []
    for line in result.stdout.strip().split('\n'):
        if not line:
            continue
        # Format: "2024-01-01 12:00:00 bucket-name"
        parts = line.split(None, 2)
        if len(parts) >= 3:
            buckets.append({
                'name': parts[2],
                'created': f'{parts[0]} {parts[1]}',
            })

    # Get scan info and stats for each bucket (use denormalized columns)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        for bucket in buckets:
            cursor = db.execute(
                'SELECT time, size, n_children, n_desc FROM scan WHERE path = ? OR path LIKE ? ORDER BY time DESC LIMIT 1',
                (f's3://{bucket["name"]}', f's3://{bucket["name"]}/%')
            )
            row = cursor.fetchone()
            bucket['last_scanned'] = row['time'] if row else None
            bucket['size'] = row['size'] if row else None
            bucket['n_children'] = row['n_children'] if row else None
            bucket['n_desc'] = row['n_desc'] if row else None
    finally:
        db.close()

    return buckets


@app.route('/api/s3/buckets')
def list_s3_buckets():
    """Return list of S3 buckets accessible to the current AWS profile."""
    # Check cache first (60 second TTL)
    cache_key = 's3_buckets'
    now = time.time()
    if cache_key in _cache:
        cached_time, cached_result = _cache[cache_key]
        if now - cached_time < CACHE_TTL:
            return jsonify(cached_result)

    try:
        buckets = _fetch_s3_buckets_data()
        _cache[cache_key] = (now, buckets)
        return jsonify(buckets)
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Timeout listing buckets'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def list_fs_children(path: str) -> list[dict]:
    """List filesystem children of a directory with basic stats."""
    if not isdir(path):
        return []

    children = []
    try:
        for name in listdir(path):
            child_path = join(path, name)
            try:
                st = stat(child_path)
                children.append({
                    'path': name,
                    'uri': child_path,
                    'size': st.st_size if isfile(child_path) else None,
                    'mtime': int(st.st_mtime),
                    'kind': 'file' if isfile(child_path) else 'dir',
                    'n_children': None,
                    'n_desc': None,
                    'scanned': False,
                })
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError):
        pass

    return children


def list_s3_children(s3_uri: str) -> list[dict]:
    """List S3 children of a prefix (non-recursive, immediate children only)."""
    # aws s3 ls s3://bucket/prefix/ returns immediate children
    # Format: "PRE dirname/" for dirs, "2024-01-01 12:00:00 size filename" for files
    try:
        # Ensure trailing slash for prefix listing
        uri = s3_uri.rstrip('/') + '/'
        result = subprocess.run(
            ['aws', 's3', 'ls', uri],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []

        children = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            if line.startswith('PRE '):
                # Directory: "PRE dirname/"
                name = line[4:].rstrip('/')
                children.append({
                    'path': name,
                    'uri': f'{s3_uri.rstrip("/")}/{name}',
                    'size': None,
                    'mtime': None,
                    'kind': 'dir',
                    'n_children': None,
                    'n_desc': None,
                    'scanned': False,
                })
            else:
                # File: "2024-01-01 12:00:00 size filename"
                parts = line.split(None, 3)
                if len(parts) >= 4:
                    from dateutil.parser import parse
                    mtime_str = f'{parts[0]} {parts[1]}'
                    try:
                        mtime = int(parse(mtime_str).timestamp())
                    except Exception:
                        mtime = None
                    size = int(parts[2]) if parts[2].isdigit() else None
                    name = parts[3]
                    children.append({
                        'path': name,
                        'uri': f'{s3_uri.rstrip("/")}/{name}',
                        'size': size,
                        'mtime': mtime,
                        'kind': 'file',
                        'n_children': None,
                        'n_desc': None,
                        'scanned': False,
                    })
        return children
    except Exception:
        return []


def get_scanned_paths(db) -> dict[str, dict]:
    """Get a mapping of scanned paths to their most recent scan info with denormalized stats."""
    cursor = db.execute('''
        SELECT s.path, s.time, s.blob, s.size, s.n_children, s.n_desc, s.mtime
        FROM scan s
        INNER JOIN (
            SELECT path, MAX(time) as max_time
            FROM scan
            GROUP BY path
        ) latest ON s.path = latest.path AND s.time = latest.max_time
    ''')
    return {row['path']: dict(row) for row in cursor}


@app.route('/api/scan')
def get_scan():
    """Return scan details for a given URI.

    Query params:
        uri: The path or s3:// URI to look up
        scan_id: Optional specific scan ID to use (for time-travel)
        depth: Max depth of children to return (default 2)
        max_rows: Max rows to return for treemap (default DEFAULT_MAX_ROWS, 0 for unlimited)
        expand_single: Auto-expand single-child directories (default true)
    """
    uri = request.args.get('uri', '/')
    scan_id = request.args.get('scan_id')
    depth = int(request.args.get('depth', 2))
    max_rows = int(request.args.get('max_rows', DEFAULT_MAX_ROWS))
    expand_single = request.args.get('expand_single', 'true').lower() != 'false'

    # Normalize URI
    uri = uri.rstrip('/')
    if not uri:
        uri = '/'

    # Find the best matching scan (exact match or ancestor)
    if '://' in uri:
        search_path = uri  # s3://, ssh://, etc.
    else:
        search_path = uri if uri.startswith('/') else f'/{uri}'

    db = get_db()
    scan = None
    candidate_scans = []

    # If scan_id provided, use that specific scan
    if scan_id:
        cursor = db.execute('SELECT * FROM scan WHERE id = ?', (scan_id,))
        row = cursor.fetchone()
        if row:
            scan = dict(row)
            # Verify the scan covers the requested URI
            scan_path = scan['path']
            if not (search_path == scan_path or search_path.startswith(scan_path + '/')):
                return jsonify({'error': f"Scan {scan_id} does not cover path {uri}"}), 400
    else:
        # Find the most recent scan covering this path (exact or ancestor)
        # Collect all candidate scans, then pick the freshest
        from disk_tree.backends import url_parent
        candidate_scans = []
        test_path = search_path
        while test_path:
            cursor = db.execute(
                'SELECT * FROM scan WHERE path = ? ORDER BY time DESC LIMIT 1',
                (test_path,)
            )
            row = cursor.fetchone()
            if row:
                candidate_scans.append(dict(row))
            parent = url_parent(test_path)
            if parent is None or parent == test_path:
                break
            test_path = parent

        # Sort candidates by recency (most recent first); we'll try them
        # in order and fall back if one doesn't contain the requested URI
        # (e.g. `/` on macOS won't contain `/Users` due to APFS firmlinks)
        if candidate_scans:
            candidate_scans.sort(key=lambda s: s['time'], reverse=True)
            scan = candidate_scans[0]

    if not scan:
        # No ancestor scan - build from filesystem + any child scans
        if '://' not in search_path:
            # Local path - list filesystem and merge with scan data
            fs_children = list_fs_children(search_path)

            # Get all scanned paths to check which children have scans
            scanned_paths = get_scanned_paths(db)

            # Find child scans under this path
            prefix = search_path.rstrip('/') + '/'
            scan_data_by_name = {}  # immediate child name -> scan stats

            for scanned_path, scan_info in scanned_paths.items():
                if scanned_path.startswith(prefix):
                    rel = scanned_path[len(prefix):]
                    immediate = rel.split('/')[0]
                    if immediate not in scan_data_by_name:
                        # Use denormalized stats from SQLite - no parquet load needed
                        if scan_info.get('size') is not None:
                            scan_data_by_name[immediate] = {
                                'size': scan_info['size'] or 0,
                                'mtime': scan_info.get('mtime') or 0,
                                'n_children': scan_info.get('n_children') or 0,
                                'n_desc': scan_info.get('n_desc') or 0,
                                'scan_time': scan_info['time'],
                            }

            # Merge filesystem with scan data
            children_by_name = {}
            for child in fs_children:
                name = child['path']
                child_uri = child['uri']

                # Check if this child has a direct scan
                if child_uri in scanned_paths:
                    scan_info = scanned_paths[child_uri]
                    # Use denormalized stats from SQLite - no parquet load needed
                    if scan_info.get('size') is not None:
                        child['size'] = scan_info['size'] or 0
                        child['mtime'] = 0  # mtime not denormalized
                        child['n_children'] = scan_info.get('n_children') or 0
                        child['n_desc'] = scan_info.get('n_desc') or 0
                        child['scanned'] = True
                        child['scan_time'] = scan_info['time']
                elif name in scan_data_by_name:
                    # Has a descendant scan - use that data
                    data = scan_data_by_name[name]
                    child['size'] = data['size']
                    child['mtime'] = data['mtime']
                    child['n_children'] = data['n_children']
                    child['n_desc'] = data['n_desc']
                    child['scanned'] = 'partial'  # Has child scans but not itself
                    child['scan_time'] = data['scan_time']

                children_by_name[name] = child

            children = list(children_by_name.values())
            # Sort: scanned first (by size desc), then unscanned (by name)
            children.sort(key=lambda x: (
                0 if x.get('scanned') else 1,
                -(x.get('size') or 0),
                x['path'],
            ))

            # Compute virtual root stats from scanned children only
            scanned_children = [c for c in children if c.get('scanned')]
            total_size = sum(c.get('size') or 0 for c in scanned_children)
            max_mtime = max((c.get('mtime') or 0 for c in scanned_children), default=0)
            total_desc = sum(c.get('n_desc') or 0 for c in scanned_children)

            return jsonify({
                'root': {
                    'path': '.',
                    'uri': search_path,
                    'size': total_size if scanned_children else None,
                    'mtime': max_mtime if scanned_children else None,
                    'kind': 'dir',
                    'n_children': len(children),
                    'n_desc': total_desc if scanned_children else None,
                    'parent': None,
                    'scanned': False,
                },
                'children': children,
                'rows': [c for c in children if c.get('scanned')],
                'time': None,
                'scan_path': None,
                'scan_status': 'partial' if scanned_children else 'none',
            })

        # S3 path - list bucket/prefix contents
        if search_path.startswith('s3://'):
            s3_children = list_s3_children(search_path)

            # Get all scanned paths to check which children have scans
            scanned_paths = get_scanned_paths(db)

            # Find child scans under this path
            prefix = search_path.rstrip('/') + '/'
            scan_data_by_name = {}  # immediate child name -> scan stats

            for scanned_path, scan_info in scanned_paths.items():
                if scanned_path.startswith(prefix):
                    rel = scanned_path[len(prefix):]
                    immediate = rel.split('/')[0]
                    if immediate not in scan_data_by_name:
                        # Use denormalized stats from SQLite - no parquet load needed
                        if scan_info.get('size') is not None:
                            scan_data_by_name[immediate] = {
                                'size': scan_info['size'] or 0,
                                'mtime': scan_info.get('mtime') or 0,
                                'n_children': scan_info.get('n_children') or 0,
                                'n_desc': scan_info.get('n_desc') or 0,
                                'scan_time': scan_info['time'],
                            }

            # Merge S3 listing with scan data
            children_by_name = {}
            for child in s3_children:
                name = child['path']
                child_uri = child['uri']

                # Check if this child has a direct scan
                if child_uri in scanned_paths:
                    scan_info = scanned_paths[child_uri]
                    # Use denormalized stats from SQLite - no parquet load needed
                    if scan_info.get('size') is not None:
                        child['size'] = scan_info['size'] or 0
                        child['mtime'] = 0  # mtime not denormalized
                        child['n_children'] = scan_info.get('n_children') or 0
                        child['n_desc'] = scan_info.get('n_desc') or 0
                        child['scanned'] = True
                        child['scan_time'] = scan_info['time']
                elif name in scan_data_by_name:
                    # Has a descendant scan - use that data
                    data = scan_data_by_name[name]
                    child['size'] = data['size']
                    child['mtime'] = data['mtime']
                    child['n_children'] = data['n_children']
                    child['n_desc'] = data['n_desc']
                    child['scanned'] = 'partial'
                    child['scan_time'] = data['scan_time']

                children_by_name[name] = child

            children = list(children_by_name.values())
            # Sort: scanned first (by size desc), then unscanned (by name)
            children.sort(key=lambda x: (
                0 if x.get('scanned') else 1,
                -(x.get('size') or 0),
                x['path'],
            ))

            # Compute virtual root stats from scanned children only
            scanned_children = [c for c in children if c.get('scanned')]
            total_size = sum(c.get('size') or 0 for c in scanned_children)
            max_mtime = max((c.get('mtime') or 0 for c in scanned_children), default=0)
            total_desc = sum(c.get('n_desc') or 0 for c in scanned_children)

            return jsonify({
                'root': {
                    'path': '.',
                    'uri': search_path,
                    'size': total_size if scanned_children else None,
                    'mtime': max_mtime if scanned_children else None,
                    'kind': 'dir',
                    'n_children': len(children),
                    'n_desc': total_desc if scanned_children else None,
                    'parent': None,
                    'scanned': False,
                },
                'children': children,
                'rows': [c for c in children if c.get('scanned')],
                'time': None,
                'scan_path': None,
                'scan_status': 'partial' if scanned_children else 'none',
            })

        return jsonify({'error': 'No scan found for path', 'uri': uri}), 404

    # Compute relative path from scan root to requested URI
    if scan['path'] == uri:
        relative_path = '.'
    else:
        scan_prefix = scan['path'].rstrip('/') + '/'
        relative_path = uri[len(scan_prefix):] if uri.startswith(scan_prefix) else ''

    # Resolve chunk: if the relative path is inside a chunked subtree, get the chunk blob
    effective_blob, rebased_path = resolve_chunk_for_path(scan['blob'], relative_path)
    is_chunked = effective_blob != scan['blob']

    # Compute max depth for predicate pushdown
    # viewed_path_depth is depth of rebased path (0 for '.', 1 for 'foo', etc.)
    viewed_path_depth = 0 if rebased_path == '.' else rebased_path.count('/') + 1
    max_depth = viewed_path_depth + depth

    # Load parquet with depth filter (only loads rows up to max_depth) and,
    # when viewing a subdir, a path-prefix filter (only that subtree's rows —
    # row-group pruning via min/max stats on the sorted path column).
    prefix_filter = rebased_path if rebased_path not in ('.', '') else None
    df = load_scan_data(effective_blob, max_depth, path_prefix=prefix_filter)

    # Filter to requested URI prefix
    prefix = uri.rstrip('/') + '/'

    # When viewing from a chunk, paths are already rebased relative to chunk root
    if is_chunked or rebased_path == '.':
        # Paths in df are relative to chunk/scan root
        # rebased_path is '.' for root, or 'subdir' for subdir within chunk
        if rebased_path == '.':
            root_mask = df['path'] == '.'
            children_mask = (df['parent'] == '.') | ((df['parent'] == '') & (df['path'] != '.'))
        else:
            root_mask = df['path'] == rebased_path
            children_prefix = rebased_path + '/'
            children_mask = df['path'].str.startswith(children_prefix)

            # Recompute relative paths for the viewed subdir within chunk
            df = df.copy()
            def make_relative(row):
                if row['path'] == rebased_path:
                    return '.'
                elif row['path'].startswith(children_prefix):
                    return row['path'][len(children_prefix):]
                return row['path']

            def make_relative_parent(row):
                orig_parent = row['parent']
                if orig_parent == rebased_path:
                    return '.'
                parent_prefix = rebased_path + '/'
                if orig_parent.startswith(parent_prefix):
                    return orig_parent[len(parent_prefix):]
                return orig_parent

            df['rel_path'] = df.apply(make_relative, axis=1)
            df['rel_parent'] = df.apply(make_relative_parent, axis=1)
    elif scan['path'] == uri:
        # Exact match - use '.' as root
        root_mask = df['path'] == '.'
        # Direct children have parent='.' (dirs) or parent='' (files)
        children_mask = (df['parent'] == '.') | ((df['parent'] == '') & (df['path'] != '.'))
    else:
        # Scan is an ancestor (non-chunked) - filter by URI
        root_mask = df['uri'] == uri
        children_mask = df['uri'].str.startswith(prefix)

        # Recompute relative paths and parents
        # The viewed dir path relative to scan root (e.g., '.dvc' when viewing s3://bucket/.dvc)
        root_row_for_rel = df[root_mask]
        viewed_dir_path = root_row_for_rel.iloc[0]['path'] if not root_row_for_rel.empty else ''

        def make_relative(row):
            if row['uri'] == uri:
                return '.'
            elif row['uri'].startswith(prefix):
                return row['uri'][len(prefix):]
            return row['path']

        def make_relative_parent(row):
            orig_parent = row['parent']
            # If parent is the viewed dir, it becomes '.'
            if orig_parent == viewed_dir_path:
                return '.'
            # If parent starts with viewed dir + '/', strip prefix
            parent_prefix = viewed_dir_path + '/'
            if orig_parent.startswith(parent_prefix):
                return orig_parent[len(parent_prefix):]
            # Fallback (shouldn't happen for rows under prefix)
            return orig_parent

        df = df.copy()
        df['rel_path'] = df.apply(make_relative, axis=1)
        df['rel_parent'] = df.apply(make_relative_parent, axis=1)
    root_row = df[root_mask]
    if root_row.empty:
        # URI not found in this scan — try next candidate (e.g. `/` scan
        # on macOS won't contain `/Users` due to APFS firmlinks)
        if candidate_scans:
            failed_id = scan['id']
            candidate_scans = [s for s in candidate_scans if s['id'] != failed_id]
            if candidate_scans:
                scan = candidate_scans[0]
                return redirect(url_for('get_scan', uri=uri, scan_id=scan['id'], depth=depth, max_rows=max_rows, expand_single=expand_single))
        return jsonify({'error': 'URI not found in scan', 'uri': uri, 'scan_path': scan['path']}), 404

    root = root_row.iloc[0].to_dict()
    for k, v in root.items():
        if hasattr(v, 'item'):
            v = root[k] = v.item()
        if isinstance(v, float) and v != v:
            root[k] = None
    root['path'] = '.'
    root['parent'] = None

    # Get children up to requested depth
    # When at chunk root or exact scan match, paths in df are already correct (path column)
    # Otherwise, use rel_path column
    if scan['path'] == uri or (is_chunked and rebased_path == '.'):
        # Filter by path depth - paths are already relative to current root
        def get_depth(path):
            if path == '.':
                return 0
            return path.count('/') + 1

        df['depth'] = df['path'].apply(get_depth)
        children_df = df[(df['depth'] > 0) & (df['depth'] <= depth)]
        # Direct children have parent='.' (dirs) or parent='' (files)
        direct_children_df = df[(df['parent'] == '.') | ((df['parent'] == '') & (df['path'] != '.'))]
    else:
        # Filter by relative path depth (subdir within scan or chunk)
        def get_rel_depth(rel_path):
            if rel_path == '.':
                return 0
            return rel_path.count('/') + 1

        df['depth'] = df['rel_path'].apply(get_rel_depth)
        children_df = df[children_mask & (df['depth'] > 0) & (df['depth'] <= depth)]
        direct_children_df = df[children_mask & (df['depth'] == 1)]

    # Convert to list of dicts, handling numpy types
    # use_rel_path: if True, use 'rel_path' column as 'path' (when viewing subdir within scan/chunk)
    # At chunk root (rebased_path == '.'), paths are already correct, no transformation needed
    use_rel_path = 'rel_path' in df.columns

    def row_to_dict(row):
        d = row.to_dict()
        # Convert numpy types to Python types; NaN → None (NaN is not valid
        # JSON — e.g. `mtime_mean` is NULL for zero-size dirs).
        for k, v in d.items():
            if hasattr(v, 'item'):
                v = d[k] = v.item()
            if isinstance(v, float) and v != v:
                d[k] = None
        # Use rel_path and rel_parent when viewing a subdir of a scan
        if use_rel_path:
            if 'rel_path' in d:
                d['path'] = d['rel_path']
            if 'rel_parent' in d:
                d['parent'] = d['rel_parent']
        return d

    children = [row_to_dict(row) for _, row in direct_children_df.iterrows()]

    # Each chunked direct child's own direct children (its chunk's depth-1
    # rows, depth 2 here) so the treemap shows every chunk's top level. From
    # the root blob's shallow sidecar when it has one, else a filtered +
    # projected read of the chunk, cached per process — never the whole chunk
    # blob (spec `scan-page-r2-latency.md`).
    if 'child_scan_id' in df.columns:
        from disk_tree.shallow import chunk_top_rows
        root_blob_path = resolve_blob(effective_blob)
        top_cols = [c for c in df.columns if c not in ('rel_path', 'rel_parent')]
        child_scan_dfs = []
        for _, row in direct_children_df.iterrows():
            child_scan = row.get('child_scan_id')
            if pd.isna(child_scan):
                continue
            try:
                child_df = chunk_top_rows(root_blob_path, child_scan, resolve_blob, top_cols)
            except Exception as e:
                print(f"Error loading child scan {child_scan}: {e}")
                continue
            if child_df is None or len(child_df) == 0:
                continue
            # Prefix paths with the parent directory name; all become its children
            parent_path = row['path'] if not use_rel_path else row.get('rel_path', row['path'])
            child_df = child_df.copy()
            child_df['path'] = parent_path + '/' + child_df['path']
            child_df['parent'] = parent_path
            child_df['depth'] = 2
            child_scan_dfs.append(child_df)
        if child_scan_dfs:
            children_df = pd.concat([children_df] + child_scan_dfs, ignore_index=True)

    # Limit rows for treemap performance
    if max_rows > 0 and len(children_df) > max_rows:
        # Sort by size descending and take top N
        sorted_df = children_df.sort_values('size', ascending=False).head(max_rows)
        # Ensure parent chain is included for valid treemap structure
        included_paths = set(sorted_df['path'].tolist())
        parents_to_add = []
        for _, row in sorted_df.iterrows():
            parent = row.get('parent') if use_rel_path else row.get('parent')
            while parent and parent != '.' and parent not in included_paths:
                parent_row = children_df[children_df['path'] == parent]
                if not parent_row.empty:
                    parents_to_add.append(parent_row.iloc[0])
                    included_paths.add(parent)
                    parent = parent_row.iloc[0].get('parent')
                else:
                    break
        if parents_to_add:
            sorted_df = pd.concat([sorted_df, pd.DataFrame(parents_to_add)], ignore_index=True)
        rows = [row_to_dict(row) for _, row in sorted_df.iterrows()]
    else:
        rows = [row_to_dict(row) for _, row in children_df.iterrows()]

    # Mark all children as scanned
    for c in children:
        c['scanned'] = True
        c['scan_time'] = scan['time']

    # Auto-expand single-child directories
    collapsed_rows = []  # List of row dicts for collapsed parent directories
    if expand_single:
        # Keep expanding while there's exactly 1 child that's a directory
        while len(children) == 1 and children[0].get('kind') == 'dir':
            single_child = children[0]
            single_child_path = single_child['path']

            # Find grandchildren (children of the single child)
            # They have parent == single_child_path
            if use_rel_path:
                grandchildren_df = children_df[children_df['rel_parent'] == single_child_path]
            else:
                grandchildren_df = children_df[children_df['parent'] == single_child_path]

            if grandchildren_df.empty:
                # No grandchildren data loaded (need deeper depth), stop expanding
                break

            # Convert grandchildren to list
            grandchildren = [row_to_dict(row) for _, row in grandchildren_df.iterrows()]
            for gc in grandchildren:
                gc['scanned'] = True
                gc['scan_time'] = scan['time']

            # Save full row data for the collapsed parent (before we modify it)
            # Include the original path for URI construction
            collapsed_row = dict(single_child)
            collapsed_row['original_path'] = single_child_path
            collapsed_rows.append(collapsed_row)

            # Update paths: strip the single_child_path prefix from grandchildren paths
            prefix_to_strip = single_child_path + '/'
            for gc in grandchildren:
                if gc['path'].startswith(prefix_to_strip):
                    gc['path'] = gc['path'][len(prefix_to_strip):]
                if gc.get('parent') == single_child_path:
                    gc['parent'] = '.'
                elif gc.get('parent', '').startswith(prefix_to_strip):
                    gc['parent'] = gc['parent'][len(prefix_to_strip):]

            # Update root to be the single child
            root = single_child
            root['path'] = '.'
            root['parent'] = None

            # Grandchildren become the new children
            children = grandchildren

            # Also update rows for treemap - strip prefix and filter out the collapsed path
            # (which becomes root, added separately by frontend)
            new_rows = []
            for r in rows:
                if r['path'] == single_child_path:
                    # Skip - this becomes root, which is added separately
                    continue
                if r['path'].startswith(prefix_to_strip):
                    r['path'] = r['path'][len(prefix_to_strip):]
                if r.get('parent') == single_child_path:
                    r['parent'] = '.'
                elif r.get('parent', '').startswith(prefix_to_strip):
                    r['parent'] = r['parent'][len(prefix_to_strip):]
                new_rows.append(r)
            rows = new_rows

    # Patch in fresher child scans: find direct child paths with newer scans
    scan_time = scan['time']
    child_uri_prefix = uri.rstrip('/') + '/'
    db = get_db()
    # Query for scans of direct children that are newer than parent scan
    # Direct children: path starts with parent prefix, no additional slashes
    fresher_scans = db.execute('''
        SELECT s1.*
        FROM scan s1
        INNER JOIN (
            SELECT path, MAX(time) as max_time
            FROM scan
            WHERE path LIKE ? ESCAPE '\\'
              AND path NOT LIKE ? ESCAPE '\\'
              AND time > ?
            GROUP BY path
        ) s2 ON s1.path = s2.path AND s1.time = s2.max_time
    ''', (
        child_uri_prefix.replace('%', '\\%').replace('_', '\\_') + '%',  # children
        child_uri_prefix.replace('%', '\\%').replace('_', '\\_') + '%/%',  # exclude grandchildren+
        scan_time,
    )).fetchall()

    # Build a map of child path -> fresher scan stats (using denormalized SQLite columns)
    fresher_stats = {}
    for child_scan in fresher_scans:
        # Use denormalized stats from SQLite - no need to load parquet
        if child_scan['size'] is not None:
            fresher_stats[child_scan['path']] = {
                'size': child_scan['size'],
                'mtime': child_scan['mtime'],
                'n_desc': child_scan['n_desc'],
                'n_children': child_scan['n_children'],
                'scan_time': child_scan['time'],
            }

    # Patch fresher stats into children
    for c in children:
        child_path = c.get('uri', '')
        if child_path in fresher_stats:
            stats = fresher_stats[child_path]
            c['size'] = stats['size']
            c['mtime'] = stats['mtime']
            c['n_desc'] = stats['n_desc']
            c['n_children'] = stats['n_children']
            c['scan_time'] = stats['scan_time']
            c['patched'] = True  # Mark that this was patched from a fresher scan

    # Add expand_preview for child directories that would auto-expand
    # This shows users what path they'll land on when clicking (e.g., "vms" shows "vms / 0")
    if expand_single:
        for c in children:
            if c.get('kind') != 'dir' or c.get('n_children') != 1:
                continue
            # Build the expand preview chain
            preview_parts = []
            current_path = c['path']
            while True:
                # Find children of current_path
                if use_rel_path:
                    sub_df = children_df[children_df['rel_parent'] == current_path]
                else:
                    sub_df = children_df[children_df['parent'] == current_path]
                if len(sub_df) != 1:
                    break
                sub_row = sub_df.iloc[0]
                if sub_row.get('kind') != 'dir':
                    break
                # Add to preview
                sub_path = sub_row['rel_path'] if use_rel_path else sub_row['path']
                sub_name = sub_path.split('/')[-1]
                preview_parts.append(sub_name)
                # Check if this also has exactly 1 child
                if sub_row.get('n_children') != 1:
                    break
                current_path = sub_path
            if preview_parts:
                c['expand_preview'] = '/'.join(preview_parts)

    return jsonify({
        'root': root,
        'children': sorted(children, key=lambda x: -x.get('size', 0)),
        'rows': rows,
        'time': scan['time'],
        'scan_path': scan['path'],
        'scan_status': 'full',
        'error_count': scan.get('error_count'),
        'error_paths': scan.get('error_paths'),
        'collapsed_rows': collapsed_rows if collapsed_rows else None,
    })


@app.route('/api/scans/history')
def get_scan_history():
    """Return all scans for a given path, including ancestor scans.

    Query params:
        uri: The path to look up

    Returns scans of the exact path AND scans of ancestor paths that contain
    data for the requested URI. Each scan includes 'scan_path' to indicate
    which path was actually scanned.
    """
    uri = request.args.get('uri', '/')
    uri = uri.rstrip('/')
    if not uri:
        uri = '/'

    db = get_db()

    # Build list of paths to check: the exact path and all ancestors
    paths_to_check = [uri]
    test_path = uri
    while test_path and test_path != '/':
        parent = dirname(test_path)
        if parent == test_path:
            break
        if parent:
            paths_to_check.append(parent)
        test_path = parent
    # Include root for local paths
    if not uri.startswith('s3://') and '/' not in paths_to_check:
        paths_to_check.append('/')

    # Query for scans of any of these paths
    placeholders = ','.join('?' * len(paths_to_check))
    scans = db.execute(
        f'SELECT id, path, time, size, n_children, n_desc FROM scan WHERE path IN ({placeholders}) ORDER BY time DESC',
        paths_to_check
    ).fetchall()

    # For ancestor scans, we need to get the stats for the specific subpath from the parquet
    results = []
    for s in scans:
        scan_dict = dict(s)
        scan_path = scan_dict['path']

        if scan_path == uri:
            # Exact match - use denormalized stats directly
            scan_dict['scan_path'] = scan_path
            results.append(scan_dict)
        else:
            # Ancestor scan - need to extract stats for the subpath from parquet
            # Look up the blob path
            blob_row = db.execute('SELECT blob FROM scan WHERE id = ?', (scan_dict['id'],)).fetchone()
            if blob_row:
                try:
                    # The path in parquet is relative to scan root
                    rel_path = uri[len(scan_path):].lstrip('/')
                    # Calculate depth of target path (e.g., 'Library' = depth 1, 'Library/Caches' = depth 2)
                    target_depth = rel_path.count('/') + 1
                    # Use depth filtering to only load rows up to target depth (much faster)
                    df = load_scan_data(blob_row['blob'], target_depth)
                    row = df[df['path'] == rel_path]
                    if not row.empty:
                        r = row.iloc[0]
                        scan_dict['size'] = int(r['size']) if pd.notna(r['size']) else None
                        scan_dict['n_children'] = int(r['n_children']) if pd.notna(r.get('n_children')) else None
                        scan_dict['n_desc'] = int(r['n_desc']) if pd.notna(r.get('n_desc')) else None
                        scan_dict['scan_path'] = scan_path
                        results.append(scan_dict)
                except Exception:
                    # Skip this scan if we can't load it
                    pass

    return jsonify(results)


@app.route('/api/histogram')
def get_histogram():
    """Byte-weighted mtime histograms for each child of a URI (spec: viz-widgets.md §4).

    Query params:
        uri: path or s3:// URI to break down
        bins: number of mtime bins (default 24)
        limit: max children returned, biggest-first; 0 = all (default 20)
        scan_id: specific scan to use (time-travel)

    Unlike `/api/scan`, this needs *every* descendant file row (a distribution
    can't be rolled up from per-dir means), so there is no depth pushdown to
    lean on — responses are cached for `CACHE_TTL` and the UI only requests
    them when the histogram view is open.
    """
    uri = request.args.get('uri', '/').rstrip('/') or '/'
    scan_id = request.args.get('scan_id')
    try:
        bins = int(request.args.get('bins', 24))
        limit_arg = request.args.get('limit', '20')
        limit = None if limit_arg in ('0', 'none', '') else int(limit_arg)
    except ValueError as e:
        return jsonify({'error': f'invalid parameter: {e}'}), 400
    if bins < 1:
        return jsonify({'error': f'bins must be >= 1; got {bins}'}), 400

    cache_key = f"histogram:{uri}:{scan_id}:{bins}:{limit}"
    now = time.time()
    if cache_key in _cache:
        cached_time, cached_result = _cache[cache_key]
        if now - cached_time < CACHE_TTL:
            return jsonify(cached_result)

    search_path = uri if ('://' in uri or uri.startswith('/')) else f'/{uri}'
    scan = freshest_scan_covering(get_db(), search_path, scan_id)
    if not scan:
        return jsonify({'error': 'No scan found for path', 'uri': uri}), 404

    if scan['path'] == uri:
        relative_path = '.'
    else:
        scan_prefix = scan['path'].rstrip('/') + '/'
        relative_path = uri[len(scan_prefix):] if uri.startswith(scan_prefix) else '.'

    effective_blob, rebased_path = resolve_chunk_for_path(scan['blob'], relative_path)
    # No *depth* pushdown possible (needs every descendant file row), but the
    # path-prefix filter still prunes sibling subtrees when viewing a subdir.
    df = load_scan_data(effective_blob, path_prefix=rebased_path if rebased_path != '.' else None)

    from disk_tree.histogram import age_histograms
    hist = age_histograms(df, rel_path=rebased_path, bins=bins, limit=limit)

    response = {
        'uri': uri,
        'scan_path': scan['path'],
        'time': scan['time'],
        **hist.to_dict(),
    }
    _cache[cache_key] = (now, response)
    return jsonify(response)


@app.route('/api/filter')
def filter_route():
    """Recursive filter: matched rows *re-aggregated* to ancestors (spec:
    diff-and-search.md §4 v1) — unlike the UI's v0 display-only dimming, a
    dir's size here counts only matched bytes. Matches are outermost-only
    (a match inside a matched dir is already in its aggregate), so totals
    never double-count.

    Query params:
        uri: Root to filter under
        q: Query — `/…/flags` regex, else substring; case-insensitive default
        depth: Display depth of the returned slice (default 4); totals always
            cover every depth
        scan_id: Optional specific scan
        case: 1/true for case-sensitive matching
    """
    uri = request.args.get('uri', '/').rstrip('/') or '/'
    q = request.args.get('q', '')
    scan_id = request.args.get('scan_id')
    case_sensitive = request.args.get('case', '').lower() in ('1', 'true')
    try:
        display_depth = int(request.args.get('depth', DEFAULT_DISPLAY_DEPTH))
    except ValueError as e:
        return jsonify({'error': f'invalid parameter: {e}'}), 400

    cache_key = f"filter:{uri}:{scan_id}:{q}:{display_depth}:{case_sensitive}"
    now = time.time()
    if cache_key in _cache:
        cached_time, cached_result = _cache[cache_key]
        if now - cached_time < CACHE_TTL:
            return jsonify(cached_result)

    search_path = uri if ('://' in uri or uri.startswith('/')) else f'/{uri}'
    scan = freshest_scan_covering(get_db(), search_path, scan_id)
    if not scan:
        return jsonify({'error': 'No scan found for path', 'uri': uri}), 404

    if scan['path'] == uri:
        relative_path = '.'
    else:
        scan_prefix = scan['path'].rstrip('/') + '/'
        relative_path = uri[len(scan_prefix):] if uri.startswith(scan_prefix) else '.'

    effective_blob, rebased_path = resolve_chunk_for_path(scan['blob'], relative_path)
    df, effective_q, indexed = _filter_inputs(effective_blob, rebased_path, q, case_sensitive)
    result = filter_scan(df, effective_q, display_depth=display_depth, case_sensitive=case_sensitive)

    response = {**_filter_payload(uri, scan, q, result), 'indexed': indexed}
    _cache[cache_key] = (now, response)
    return jsonify(response)


def _load_filter_frame(effective_blob: str, rebased_path: str) -> pd.DataFrame:
    # follow_refs: a search must not silently miss chunked subtrees. No depth
    # pushdown (matches live at any depth); the prefix prunes sibling subtrees.
    df = load_scan_data(
        effective_blob,
        follow_refs=True,
        path_prefix=rebased_path if rebased_path != '.' else None,
    )
    return rebase_frame(df, rebased_path)


def _filter_inputs(effective_blob: str, rebased_path: str, q: str, case_sensitive: bool):
    """Pick the filter's input frame: the vocab-sidecar fast path when a fresh
    sidecar covers this blob and the query is segment-local, else the brute
    full-frame load. Returns `(frame, effective_query, indexed)` — the indexed
    frame holds *matched rows only*, so the effective query is `''` (matches
    everything) and the shared dedup/rollup machinery runs unchanged."""
    if not effective_blob.startswith(('ddb:', 'sqlite:')):
        from disk_tree.sidecar import indexed_filter_frame
        matched = indexed_filter_frame(
            resolve_blob(effective_blob), q,
            case_sensitive=case_sensitive, rel_path=rebased_path,
        )
        if matched is not None:
            return matched, '', True
    return _load_filter_frame(effective_blob, rebased_path), q, False


def _filter_payload(uri: str, scan: dict, q: str, result) -> dict:
    return {
        'uri': uri,
        'scan_path': scan['path'],
        'time': scan['time'],
        'query': q,
        'total_size': result.total_size,
        'n_matches': result.n_matches,
        'max_depth_scanned': result.max_depth_scanned,
        'rows': [
            {
                'path': n.path,
                'uri': uri.rstrip('/') + '/' + n.path,
                'depth': n.depth,
                'kind': n.kind,
                'size': n.size,
                'n_matches': n.n_matches,
                'matched': n.matched,
            }
            for n in result.nodes
        ],
    }


@app.route('/api/filter/stream')
def filter_stream():
    """SSE variant of `/api/filter`: one cumulative snapshot per depth.

    Depth-major layout makes shallow-first iterative deepening plain iteration
    order — the display-depth slice arrives for a few % of the scan cost and
    deepens as scanning proceeds. Events are JSON: `{phase: 'loading'}` first,
    then `/api/filter`-shaped payloads plus `depth`/`done`; the final event has
    `done: true` and equals the plain endpoint's response.
    """
    uri = request.args.get('uri', '/').rstrip('/') or '/'
    q = request.args.get('q', '')
    scan_id = request.args.get('scan_id')
    case_sensitive = request.args.get('case', '').lower() in ('1', 'true')
    try:
        display_depth = int(request.args.get('depth', DEFAULT_DISPLAY_DEPTH))
    except ValueError as e:
        return jsonify({'error': f'invalid parameter: {e}'}), 400

    search_path = uri if ('://' in uri or uri.startswith('/')) else f'/{uri}'
    scan = freshest_scan_covering(get_db(), search_path, scan_id)
    if not scan:
        return jsonify({'error': 'No scan found for path', 'uri': uri}), 404

    if scan['path'] == uri:
        relative_path = '.'
    else:
        scan_prefix = scan['path'].rstrip('/') + '/'
        relative_path = uri[len(scan_prefix):] if uri.startswith(scan_prefix) else '.'
    # Resolve before entering the generator — get_db() is request-scoped.
    effective_blob, rebased_path = resolve_chunk_for_path(scan['blob'], relative_path)

    def generate():
        yield f"data: {json.dumps({'phase': 'loading'})}\n\n"
        df, effective_q, indexed = _filter_inputs(effective_blob, rebased_path, q, case_sensitive)
        from disk_tree.filter import iter_filter_scan
        last = None
        for d, snap in iter_filter_scan(df, effective_q, display_depth=display_depth, case_sensitive=case_sensitive):
            last = snap
            payload = {**_filter_payload(uri, scan, q, snap), 'depth': d, 'done': False, 'indexed': indexed}
            yield f"data: {json.dumps(payload)}\n\n"
        if last is None:
            from disk_tree.filter import FilterResult
            last = FilterResult(nodes=[], total_size=0, n_matches=0, max_depth_scanned=0)
        yield f"data: {json.dumps({**_filter_payload(uri, scan, q, last), 'done': True, 'indexed': indexed})}\n\n"

    return Response(generate(), mimetype='text/event-stream')


# Diff-index builds: one at a time (two full scans in memory), started by the
# first recursive compare of a pair (spec diff-index.md). Keyed (scan_a, scan_b).
_index_lock = threading.Lock()
_index_building: set[tuple[int, int]] = set()
# `DISK_TREE_AUTO_DIFF_INDEX=0` disables the first-request background build
# (the CLI's `diff-index` / `index` still build explicitly).
AUTO_INDEX = os.environ.get('DISK_TREE_AUTO_DIFF_INDEX', '1') != '0'


def _index_status(scan_a: int, scan_b: int) -> dict:
    """`{'status': none|building|done|failed, ...}` for a pair."""
    with _index_lock:
        if (scan_a, scan_b) in _index_building:
            return {'status': 'building'}
    row = get_index(scan_a, scan_b)
    if row is None:
        return {'status': 'none'}
    out = {'status': row['status'], 'time': row['time']}
    if row['status'] == 'done':
        out.update({k: row[k] for k in ('n_rows', 'n_added', 'n_removed', 'n_changed', 'n_touched', 'seconds')})
    elif row['status'] == 'failed':
        out['error'] = row['error']
    return out


def _ensure_index_build(scan1, scan2) -> None:
    """Kick off a background build for the pair unless one exists/is running."""
    if not AUTO_INDEX:
        return
    key = (int(scan1['id']), int(scan2['id']))
    with _index_lock:
        if key in _index_building:
            return
        row = get_index(*key)
        if row is not None and row['status'] in ('done', 'building'):
            # `building` from another process (CLI) — or a crashed one; the
            # CLI's own run will flip it, and a stale row can be forced via
            # `disk-tree diff-index -f`.
            return
        _index_building.add(key)

    def run():
        try:
            build_and_record(key[0], scan1['blob'], key[1], scan2['blob'])
        except Exception as e:  # recorded as `failed` by build_and_record
            print(f"diff index {key}: {type(e).__name__}: {e}")
        finally:
            with _index_lock:
                _index_building.discard(key)

    threading.Thread(target=run, name=f'diff-index-{key[0]}-{key[1]}', daemon=True).start()


@app.route('/api/diff/status')
def diff_index_status():
    """Build state of the persisted diff index for a scan pair."""
    try:
        scan_a = int(request.args['scan1'])
        scan_b = int(request.args['scan2'])
    except (KeyError, ValueError):
        return jsonify({'error': 'scan1 and scan2 (ints) required'}), 400
    return jsonify(_index_status(scan_a, scan_b))


# Single-flight for /api/compare: identical concurrent requests (React's
# StrictMode double-fires effects in dev; two tabs; a double-click) share one
# computation instead of each walking the scans.
_inflight_lock = threading.Lock()
_inflight: dict[str, threading.Event] = {}


def _mtime_differs(a, b) -> bool:
    """NaN-safe: both-missing counts as equal."""
    a_na, b_na = pd.isna(a), pd.isna(b)
    if a_na and b_na:
        return False
    return bool(a_na or b_na or a != b)


@app.route('/api/compare')
def compare_scans():
    """Compare two scans of the same path.

    Query params:
        uri: The path to compare
        scan1: ID of first (older) scan
        scan2: ID of second (newer) scan
        depth: Max depth of children to compare (default 1)
        recursive: If truthy, walk changed spines best-first (|Δsize| priority)
            and return the delta *frontier* across depths instead of one level.
            `added`/`removed` dirs are not descended (their row is the whole
            story); stats-equal dirs are pruned. Rows gain `depth`, `expanded`,
            `pruned`; summary gains `expansions`, `truncated`.
        budget: recursive only — max directory expansions (default 100)
        max_depth: recursive only — deepest level to descend to
        min_frac: index-served only — drop rows whose bytes and |Δ| are both
            under this fraction of the compared subtree (default 2e-5: ~4K
            rows instead of ~590K on a home dir, all of them drawable).
            `0` serves everything the row budget allows.

    Scans can be of the exact URI or ancestor paths. When comparing ancestor
    scans, we extract the relevant subtree for the requested URI.
    """
    uri = request.args.get('uri', '/')
    scan1_id = request.args.get('scan1')
    scan2_id = request.args.get('scan2')
    depth = int(request.args.get('depth', 1))
    recursive = request.args.get('recursive', '').lower() in ('1', 'true')
    budget = int(request.args.get('budget', 100))
    rec_max_depth_arg = request.args.get('max_depth')
    rec_max_depth = int(rec_max_depth_arg) if rec_max_depth_arg else None
    min_frac = float(request.args.get('min_frac', 2e-5))

    if not scan1_id or not scan2_id:
        return jsonify({'error': 'scan1 and scan2 parameters required'}), 400

    # Check response cache. No TTL: a compare is keyed by two scan *ids*, whose
    # blobs are immutable — the result can only change via a delete rewriting
    # parquets, and the delete path clears `_cache` wholesale. (The recursive
    # walk can take tens of seconds on a big hybrid scan; expiring it every
    # CACHE_TTL forced that recompute on nearly every page load.)
    index_status = _index_status(int(scan1_id), int(scan2_id)) if recursive and scan1_id.isdigit() and scan2_id.isdigit() else None
    indexed = bool(index_status and index_status['status'] == 'done')
    cache_key = f"compare:{uri}:{scan1_id}:{scan2_id}:{depth}:{recursive}:{budget}:{rec_max_depth}:{min_frac}:{'idx' if indexed else 'walk'}"
    now = time.time()
    if cache_key in _cache:
        _, cached_result = _cache[cache_key]
        return jsonify(cached_result)
    with _inflight_lock:
        waiter = _inflight.get(cache_key)
        if waiter is None:
            _inflight[cache_key] = threading.Event()
    if waiter is not None:
        waiter.wait()
        if cache_key in _cache:
            return jsonify(_cache[cache_key][1])
        # the leader failed; fall through and compute ourselves
        with _inflight_lock:
            _inflight.setdefault(cache_key, threading.Event())
    try:
        return _compare_scans(uri, scan1_id, scan2_id, depth, recursive, budget, rec_max_depth, cache_key, now, index_status, min_frac)
    finally:
        with _inflight_lock:
            ev = _inflight.pop(cache_key, None)
        if ev is not None:
            ev.set()


def _compare_scans(uri, scan1_id, scan2_id, depth, recursive, budget, rec_max_depth, cache_key, now, index_status=None, min_frac=0.0):

    db = get_db()

    # Load both scans
    scan1 = db.execute('SELECT * FROM scan WHERE id = ?', (scan1_id,)).fetchone()
    scan2 = db.execute('SELECT * FROM scan WHERE id = ?', (scan2_id,)).fetchone()

    if not scan1 or not scan2:
        return jsonify({'error': 'Scan not found'}), 404

    # Filter to direct children of the requested URI
    uri = uri.rstrip('/')
    if not uri:
        uri = '/'

    # Helper to convert numpy types to native Python types
    def to_native(v):
        if v is None:
            return None
        if hasattr(v, 'item'):
            return v.item()
        return v

    def get_subtree_stats(scan):
        """Stats of the compared uri within a scan (rebasing into ancestor scans)."""
        scan_path = scan['path']
        if scan_path == uri:
            return {
                'size': to_native(scan['size']),
                'n_desc': to_native(scan['n_desc']) if 'n_desc' in scan.keys() else None,
            }
        # Load the parquet with depth + path-prefix filtering and find the row
        # for the target URI (the prefix filter narrows to exactly that row).
        # Hybrid scans keep big subtrees in chunk blobs: resolve first, so a
        # uri inside a chunk reads that chunk (its root row is `.` at depth 0).
        rel_path = uri[len(scan_path):].lstrip('/')
        blob, rel_path = resolve_chunk_for_path(scan['blob'], rel_path)
        if rel_path == '.':
            target_depth = 0
        else:
            target_depth = rel_path.count('/') + 1
        df = load_scan_data(blob, max_depth=target_depth, min_depth=target_depth, path_prefix=None if rel_path == '.' else rel_path)
        row = df[df['path'] == rel_path]
        if not row.empty:
            r = row.iloc[0]
            return {
                'size': to_native(r['size']),
                'n_desc': to_native(r.get('n_desc')),
            }
        return {'size': None, 'n_desc': None}

    if recursive:
        # Best-first pruned walk down changed spines (spec diff-and-search.md §3a).
        # Rows span depths; totals come from the subtree stats, not row sums
        # (a frontier row's Δ is already included in its ancestors').
        served = None
        same_root = scan1['path'] == scan2['path']
        if index_status and index_status['status'] == 'done' and same_root:
            # The persisted full diff: a parquet slice instead of a walk.
            row = get_index(int(scan1_id), int(scan2_id))
            if row is not None:
                rel_prefix = '' if scan1['path'] == uri else uri[len(scan1['path']):].lstrip('/')
                # Thresholds are relative to the *compared* subtree, so a
                # drill into a pruned dir resolves detail the parent view
                # couldn't draw.
                sub1, sub2 = get_subtree_stats(scan1), get_subtree_stats(scan2)
                served = serve_slice(
                    load_index_slice(row['blob'], rel_prefix),
                    min_frac=min_frac,
                    total=max(sub1['size'] or 0, sub2['size'] or 0),
                    max_depth=rec_max_depth,
                )
        if served is None:
            src1 = ScanSource(scan1['blob'], scan1['path'], uri, load_scan_data, resolve=resolve_chunk_for_path)
            src2 = ScanSource(scan2['blob'], scan2['path'], uri, load_scan_data, resolve=resolve_chunk_for_path)
            result = recursive_diff(src1, src2, budget=budget, max_depth=rec_max_depth)
            if same_root and index_status is not None and index_status['status'] in ('none', 'failed'):
                _ensure_index_build(scan1, scan2)
        stats1 = get_subtree_stats(scan1)
        stats2 = get_subtree_stats(scan2)
        uri_prefix = uri.rstrip('/') + '/'
        def rec_row(r):
            g = r.get if isinstance(r, dict) else (lambda k: getattr(r, k))
            return {
                'path': g('path'),
                'uri': uri_prefix + g('path'),
                'depth': g('depth'),
                'kind': g('kind'),
                'status': g('status'),
                'size_a': g('size_a'),
                'size_b': g('size_b'),
                'size_delta': g('size_b') - g('size_a'),
                'size_old': g('size_a'),
                'n_desc_a': g('n_desc_a'),
                'n_desc_b': g('n_desc_b'),
                'n_desc_delta': g('n_desc_b') - g('n_desc_a'),
                'n_desc_old': g('n_desc_a'),
                'expanded': g('expanded'),
                'pruned': g('pruned'),
            }
        if served is not None:
            rows = [rec_row(r) for r in served['rows']]
            unchanged_top = [rec_row(r) for r in served['unchanged']['top']]
            unchanged_rest = served['unchanged']['rest']
            expansions, truncated = served['expansions'], served['truncated']
        else:
            rows = [rec_row(r) for r in result.rows]
            unchanged_top = [rec_row(r) for r in result.unchanged_top]
            unchanged_rest = {
                parent: {'count': u.count, 'size': u.size, 'n_desc': u.n_desc}
                for parent, u in result.unchanged_rest.items()
            }
            expansions, truncated = result.expansions, result.truncated
        response = {
            'uri': uri,
            'recursive': True,
            'scan1': {
                'id': to_native(scan1['id']),
                'time': scan1['time'],
                'size': stats1['size'],
                'n_desc': stats1['n_desc'],
                'scan_path': scan1['path'],
            },
            'scan2': {
                'id': to_native(scan2['id']),
                'time': scan2['time'],
                'size': stats2['size'],
                'n_desc': stats2['n_desc'],
                'scan_path': scan2['path'],
            },
            'rows': rows,
            # Labeled grey context: the biggest unchanged siblings met while
            # expanding each dir, plus the aggregate of the rest (keyed by
            # parent path, '' = uri) — so the treemap can name its biggest
            # grey cells and count what its filler stands for.
            'unchanged': {'top': unchanged_top, 'rest': unchanged_rest},
            # Persisted full-diff state for the pair: `done` means this
            # response came from the index (complete, any subpath instant);
            # `building` means the walk answered and the client should
            # refetch when the index lands.
            'index': _index_status(int(scan1_id), int(scan2_id)) if same_root else {'status': 'none'},
            'summary': {
                'added': sum(1 for r in rows if r['status'] == 'added'),
                'removed': sum(1 for r in rows if r['status'] == 'removed'),
                'changed': sum(1 for r in rows if r['status'] == 'changed'),
                'touched': sum(1 for r in rows if r['status'] == 'touched'),
                'unchanged': sum(1 for r in rows if r['status'] == 'unchanged'),
                'total_delta': (stats2['size'] or 0) - (stats1['size'] or 0),
                'expansions': expansions,
                'truncated': truncated,
            },
        }
        _cache[cache_key] = (time.time(), response)
        return jsonify(response)

    def get_children(scan, depth_limit):
        """Get direct children of the target URI from a scan.

        Handles both exact match scans and ancestor scans.
        """
        scan_path = scan['path']

        # Rebase into the scan (it may be of an ancestor path), then across a
        # hybrid chunk boundary — a uri inside a chunk blob reads that blob.
        rel_prefix = '' if scan_path == uri else uri[len(scan_path):].lstrip('/')
        blob, rebased = resolve_chunk_for_path(scan['blob'], rel_prefix or '.')
        at_root = rebased in ('', '.')
        depth_offset = 0 if at_root else rebased.count('/') + 1

        # Load with EXACT depth filter - we only need rows at depth_offset +
        # depth_limit. This avoids loading millions of rows at other depths;
        # the path-prefix filter also skips every sibling subtree. Rows at
        # exactly that depth under that prefix *are* the descendants wanted
        # (no parent-column mask: root-level files carry parent='' while dirs
        # carry '.', see find/index.py).
        target_depth = depth_offset + depth_limit
        df = load_scan_data(blob, max_depth=target_depth, min_depth=target_depth, path_prefix=None if at_root else rebased)
        if df.empty:
            return df
        depths = df['path'].map(lambda p: 0 if p == '.' else p.count('/') + 1)
        mask = depths == target_depth
        if not at_root:
            mask &= df['path'].str.startswith(rebased + '/')
        children = df[mask].copy()
        children['rel_path'] = children['path'] if at_root else children['path'].str[len(rebased) + 1:]
        return children

    children1 = get_children(scan1, depth)
    children2 = get_children(scan2, depth)

    # Build comparison using rel_path (the child name relative to the target URI)
    paths1 = set(children1['rel_path'].tolist()) if 'rel_path' in children1.columns and len(children1) > 0 else set()
    paths2 = set(children2['rel_path'].tolist()) if 'rel_path' in children2.columns and len(children2) > 0 else set()

    added = paths2 - paths1
    removed = paths1 - paths2
    common = paths1 & paths2

    def row_to_dict(row):
        d = row.to_dict()
        for k, v in d.items():
            d[k] = to_native(v)
        return d

    # Build URI prefix for drill-down links
    uri_prefix = uri.rstrip('/') + '/'

    # Index by rel_path for O(1) lookups — a boolean mask per child inside the
    # loops below is O(C) each, i.e. O(C²) total, which a 100k-wide cloud
    # prefix turns into 10^10 comparisons. drop=False keeps rel_path in the
    # row dicts (the response shape predates this).
    by_rel1 = children1.set_index('rel_path', drop=False) if len(children1) else children1
    by_rel2 = children2.set_index('rel_path', drop=False) if len(children2) else children2
    by_rel1 = by_rel1[~by_rel1.index.duplicated()] if len(by_rel1) else by_rel1
    by_rel2 = by_rel2[~by_rel2.index.duplicated()] if len(by_rel2) else by_rel2

    results = []

    # Added rows
    for rel_path in added:
        row = by_rel2.loc[rel_path]
        d = row_to_dict(row)
        d['path'] = rel_path  # Use rel_path as display path
        d['uri'] = uri_prefix + rel_path  # Build full URI for linking
        d['status'] = 'added'
        d['size_delta'] = to_native(d.get('size', 0) or 0)
        results.append(d)

    # Removed rows
    for rel_path in removed:
        row = by_rel1.loc[rel_path]
        d = row_to_dict(row)
        d['path'] = rel_path  # Use rel_path as display path
        d['uri'] = uri_prefix + rel_path  # Build full URI for linking
        d['status'] = 'removed'
        d['size_delta'] = -to_native(d.get('size', 0) or 0)
        results.append(d)

    # Changed rows
    for rel_path in common:
        row1 = by_rel1.loc[rel_path]
        row2 = by_rel2.loc[rel_path]
        d = row_to_dict(row2)
        d['path'] = rel_path  # Use rel_path as display path
        d['uri'] = uri_prefix + rel_path  # Build full URI for linking

        # Compute deltas
        size1 = to_native(row1.get('size', 0) or 0)
        size2 = to_native(row2.get('size', 0) or 0)
        d['size_delta'] = size2 - size1
        d['size_old'] = size1

        n_desc1 = to_native(row1.get('n_desc', 0) or 0)
        n_desc2 = to_native(row2.get('n_desc', 0) or 0)
        d['n_desc_delta'] = n_desc2 - n_desc1
        d['n_desc_old'] = n_desc1

        if d['size_delta'] != 0 or d['n_desc_delta'] != 0:
            d['status'] = 'changed'
        elif _mtime_differs(row1.get('mtime'), row2.get('mtime')):
            d['status'] = 'touched'
        else:
            d['status'] = 'unchanged'

        results.append(d)

    # Sort by absolute size delta
    results.sort(key=lambda x: abs(x.get('size_delta', 0)), reverse=True)

    # Compute totals
    total_delta = sum(r.get('size_delta', 0) for r in results)

    stats1 = get_subtree_stats(scan1)
    stats2 = get_subtree_stats(scan2)

    response = {
        'uri': uri,
        'scan1': {
            'id': to_native(scan1['id']),
            'time': scan1['time'],
            'size': stats1['size'],
            'n_desc': stats1['n_desc'],
            'scan_path': scan1['path'],
        },
        'scan2': {
            'id': to_native(scan2['id']),
            'time': scan2['time'],
            'size': stats2['size'],
            'n_desc': stats2['n_desc'],
            'scan_path': scan2['path'],
        },
        'rows': results,
        'summary': {
            'added': len(added),
            'removed': len(removed),
            'changed': len([r for r in results if r['status'] == 'changed']),
            'touched': len([r for r in results if r['status'] == 'touched']),
            'unchanged': len([r for r in results if r['status'] == 'unchanged']),
            'total_delta': to_native(total_delta),
        }
    }

    # Cache the response
    _cache[cache_key] = (time.time(), response)

    return jsonify(response)


def run_scan_job(job_id: str, path: str, force: bool = True):
    """Run disk-tree index in background and update job status."""
    try:
        running_scans[job_id]['status'] = 'running'
        cmd = ['disk-tree', 'index']
        if force:
            cmd.append('-C')  # Force fresh scan, don't use cache
        cmd.append('-m')  # mtime_mean feeds the UI's age lens
        cmd.append(path)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
        running_scans[job_id]['output'] = result.stdout
        running_scans[job_id]['error'] = result.stderr
        running_scans[job_id]['status'] = 'completed' if result.returncode == 0 else 'failed'
        running_scans[job_id]['finished'] = datetime.now().isoformat()
    except Exception as e:
        running_scans[job_id]['status'] = 'failed'
        running_scans[job_id]['error'] = str(e)
        running_scans[job_id]['finished'] = datetime.now().isoformat()


@app.route('/api/scan/start', methods=['POST'])
def start_scan():
    """Start a new scan for a path.

    JSON body:
        path: The path or s3:// URI to scan
    """
    data = request.get_json() or {}
    path = data.get('path', '/')

    # Check if already scanning this path
    for job_id, job in running_scans.items():
        if job['path'] == path and job['status'] == 'running':
            return jsonify({'error': 'Scan already in progress', 'job_id': job_id}), 409

    job_id = str(uuid.uuid4())[:8]
    running_scans[job_id] = {
        'path': path,
        'status': 'pending',
        'started': datetime.now().isoformat(),
        'output': '',
        'error': '',
    }

    thread = threading.Thread(target=run_scan_job, args=(job_id, path))
    thread.daemon = True
    thread.start()

    return jsonify({'job_id': job_id, 'path': path, 'status': 'pending'})


@app.route('/api/scan/status/<job_id>')
def scan_status(job_id: str):
    """Get status of a scan job."""
    if job_id not in running_scans:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({'job_id': job_id, **running_scans[job_id]})


@app.route('/api/scans/running')
def running_scans_list():
    """Get list of all running/recent scans."""
    return jsonify([
        {'job_id': job_id, **job}
        for job_id, job in running_scans.items()
    ])


@app.route('/api/scans/progress')
def get_scans_progress():
    """Get current progress of all active scans (one-shot, no SSE)."""
    db = get_db()
    try:
        cursor = db.execute('SELECT * FROM scan_progress')
        result = []
        for row in cursor:
            result.append({
                'id': row['id'],
                'path': row['path'],
                'pid': row['pid'],
                'started': row['started'],
                'items_found': row['items_found'],
                'items_per_sec': row['items_per_sec'],
                'error_count': row['error_count'],
                'status': row['status'],
            })
        return jsonify(result)
    except sqlite3.OperationalError:
        # Table might not exist yet
        return jsonify([])


@app.route('/api/scans/progress/stream')
def stream_scans_progress():
    """Stream scan progress updates via Server-Sent Events."""
    def generate():
        last_data = None
        while True:
            # Create a new connection for each poll (can't share across threads)
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.execute('SELECT * FROM scan_progress')
                rows = []
                for row in cursor:
                    rows.append({
                        'id': row['id'],
                        'path': row['path'],
                        'pid': row['pid'],
                        'started': row['started'],
                        'items_found': row['items_found'],
                        'items_per_sec': row['items_per_sec'],
                        'error_count': row['error_count'],
                        'status': row['status'],
                    })
                data = json.dumps(rows)
                # Only send if data changed
                if data != last_data:
                    yield f"data: {data}\n\n"
                    last_data = data
            except sqlite3.OperationalError:
                # Table might not exist yet
                yield f"data: []\n\n"
            finally:
                conn.close()

            time.sleep(1)  # Poll every second

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',  # Disable nginx buffering
        },
    )


def hexdump(data: bytes, offset: int = 0) -> str:
    """Generate hexdump -C style output."""
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part = ' '.join(f'{b:02x}' for b in chunk[:8])
        if len(chunk) > 8:
            hex_part += '  ' + ' '.join(f'{b:02x}' for b in chunk[8:])
        # Pad hex part to fixed width
        hex_part = hex_part.ljust(49)
        # ASCII part: printable chars or '.'
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'{offset + i:08x}  {hex_part} |{ascii_part}|')
    return '\n'.join(lines)


# Preview limits
TEXT_PREVIEW_MAX = 64 * 1024      # 64KB for text
HEX_PREVIEW_MAX = 2 * 1024        # 2KB for hex (128 lines)
PREVIEW_ABSOLUTE_MAX = 1024 * 1024  # 1MB hard cap


@app.route('/api/file/preview')
def file_preview():
    """Return preview of a file's contents.

    Query params:
        path: Absolute path to the file
        max_size: Max bytes to read for text (default 64KB, max 1MB)
        hex_size: Max bytes for hex dump (default 2KB, max 16KB)
    """
    path = request.args.get('path')
    max_size = min(int(request.args.get('max_size', TEXT_PREVIEW_MAX)), PREVIEW_ABSOLUTE_MAX)
    hex_size = min(int(request.args.get('hex_size', HEX_PREVIEW_MAX)), 16 * 1024)

    if not path:
        return jsonify({'error': 'Path is required'}), 400

    if not path.startswith('/'):
        return jsonify({'error': 'Path must be absolute'}), 400

    if not isfile(path):
        return jsonify({'error': 'File not found'}), 404

    try:
        file_size = stat(path).st_size

        # Check if file is likely text
        text_extensions = {'.txt', '.log', '.json', '.xml', '.html', '.css', '.js', '.ts',
                          '.py', '.md', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf',
                          '.sh', '.bash', '.zsh', '.csv', '.tsv', '.sql', '.env', '.gitignore'}
        ext = '.' + path.rsplit('.', 1)[-1].lower() if '.' in path else ''

        # Read enough to determine type and provide preview
        # For text detection, read up to max_size; for hex we'll truncate later
        read_size = max(max_size, hex_size)
        with open(path, 'rb') as f:
            raw = f.read(read_size)

        # Try to decode as text
        try:
            content = raw.decode('utf-8')
            is_text = True
        except UnicodeDecodeError:
            try:
                content = raw.decode('latin-1')
                is_text = True
            except:
                is_text = False
                content = None

        # Check for binary content (null bytes, etc.)
        if is_text and '\x00' in content:
            is_text = False
            content = None

        # Apply appropriate limits
        if is_text:
            text_truncated = len(content) > max_size or file_size > max_size
            content = content[:max_size] if content else None
            hex_content = None
            hex_truncated = False
        else:
            content = None
            text_truncated = False
            # Generate hex dump with smaller limit
            hex_raw = raw[:hex_size]
            hex_content = hexdump(hex_raw)
            hex_truncated = file_size > hex_size

        return jsonify({
            'path': path,
            'size': file_size,
            'truncated': text_truncated,
            'hex_truncated': hex_truncated,
            'preview_bytes': hex_size if not is_text else max_size,
            'is_text': is_text,
            'content': content,
            'hex': hex_content,
            'extension': ext,
        })

    except PermissionError:
        return jsonify({'error': 'Permission denied'}), 403
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/files/get')
def files_get():
    """Raw file bytes for `<FileTree>`'s `Store.get` — with HTTP Range support.

    Backs the parquet/csv/sqlite viewers, which read byte ranges (unlike the
    size-capped `/api/file/preview`). Local absolute paths only; `s3://`/`gcs://`
    objects return 501 until presign/proxy lands (Half A follow-up).

    A `Range: bytes=<start>-<end>` request gets a 206 with `Content-Range`; a
    plain GET streams the whole file. `Accept-Ranges: bytes` is always set so
    the client knows ranges are available.
    """
    path = request.args.get('path')
    if not path:
        return jsonify({'error': 'path is required'}), 400
    if '://' in path:
        return jsonify({'error': 'cloud object fetch not implemented (local paths only)'}), 501
    if not path.startswith('/'):
        return jsonify({'error': 'path must be absolute'}), 400
    if not isfile(path):
        return jsonify({'error': 'file not found'}), 404

    file_size = stat(path).st_size
    ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    range_header = request.headers.get('Range')

    if range_header:
        m = re.match(r'bytes=(\d*)-(\d*)\s*$', range_header.strip())
        if not m or (not m.group(1) and not m.group(2)):
            return jsonify({'error': f'unsatisfiable range: {range_header!r}'}), 416
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else file_size - 1
        else:
            # suffix range: bytes=-N → last N bytes
            start = max(0, file_size - int(m.group(2)))
            end = file_size - 1
        end = min(end, file_size - 1)
        if start > end or start >= file_size:
            resp = jsonify({'error': 'range not satisfiable'})
            resp.status_code = 416
            resp.headers['Content-Range'] = f'bytes */{file_size}'
            return resp
        try:
            with open(path, 'rb') as f:
                f.seek(start)
                data = f.read(end - start + 1)
        except PermissionError:
            return jsonify({'error': 'permission denied'}), 403
        resp = Response(data, status=206, mimetype=ctype)
        resp.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        resp.headers['Accept-Ranges'] = 'bytes'
        resp.headers['Content-Length'] = str(len(data))
        return resp

    # Plain GET: stream the whole file (send_file, not read() into memory — a
    # multi-GB blob would otherwise spike RAM). It sets Content-Length and,
    # with conditional=True, honors a Range itself as a fallback. Set
    # Accept-Ranges explicitly — send_file only emits it on some Werkzeug
    # versions, and the client keys off it to know ranges are available.
    try:
        resp = send_file(path, mimetype=ctype, conditional=True)
    except PermissionError:
        return jsonify({'error': 'permission denied'}), 403
    resp.headers['Accept-Ranges'] = 'bytes'
    return resp


@app.route('/api/reveal', methods=['POST'])
def reveal_path():
    """Open a path in Finder.

    Directories: opens the folder. Files: reveals (selects) in parent folder.

    JSON body:
        path: The absolute path to reveal
    """
    data = request.get_json() or {}
    path = data.get('path')
    if not path:
        return jsonify({'error': 'path is required'}), 400
    from disk_tree.backends import backend_for
    if not backend_for(path).is_local:
        return jsonify({'error': 'Reveal is only supported for local paths'}), 400
    if not exists(path):
        return jsonify({'error': f'Path not found: {path}'}), 404
    if isdir(path):
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['open', '-R', path])
    return jsonify({'ok': True})


@app.route('/api/delete', methods=['POST'])
def delete_path():
    """Delete a file or directory.

    JSON body:
        path: The absolute path to delete
    """
    data = request.get_json() or {}
    path = data.get('path')

    if not path:
        return jsonify({'error': 'Path is required'}), 400

    from disk_tree.backends import backend_for, url_parent
    source = backend_for(path)

    # Reject bare-relative local paths (URLs with schemes are OK)
    if source.is_local and not path.startswith('/'):
        return jsonify({'error': 'Path must be absolute'}), 400

    path_exists = source.exists(path)

    # Fast-path size stat for local files only; non-local skips this and
    # falls back to scan-derived size below.
    deleted_size = None
    if source.is_local and isfile(path):
        try:
            deleted_size = stat(path).st_size
        except (OSError, PermissionError):
            pass

    if path_exists:
        try:
            source.delete(path)
        except Exception as e:
            return jsonify({'error': f'Failed to delete: {e}'}), 500

    # Update the most recent scan that covers this path
    deleted_n_desc = None
    backend = get_backend()
    db = get_db()

    # Find all scans covering this path, then pick the most recent
    candidate_scans = []
    test_path: str | None = path
    while test_path:
        cursor = db.execute(
            'SELECT id, path, blob, time FROM scan WHERE path = ? ORDER BY time DESC LIMIT 1',
            (test_path,)
        )
        row = cursor.fetchone()
        if row:
            candidate_scans.append(dict(row))
        test_path = url_parent(test_path)

    # Pick the most recent scan
    covering_scan = max(candidate_scans, key=lambda s: s['time']) if candidate_scans else None

    if covering_scan:
        scan_path = covering_scan['path']
        blob_ref = covering_scan['blob']
        rel_path = path[len(scan_path):].lstrip('/') if path != scan_path else '.'

        if backend.supports_updates:
            # DuckDB/SQLite: efficient in-place update
            stats = backend.delete_path(blob_ref, rel_path)
            if stats:
                deleted_size = stats.size
                deleted_n_desc = stats.n_desc
                # Update denormalized stats in SQLite scan metadata
                root_stats = backend.get_path_stats(blob_ref, '.')
                if root_stats:
                    db.execute('''
                        UPDATE scan SET size = ?, n_children = ?, n_desc = ?
                        WHERE id = ?
                    ''', (root_stats.size, root_stats.n_children, root_stats.n_desc, covering_scan['id']))
                    db.commit()
        else:
            # Parquet: need to rewrite the file (expensive but only for most recent scan)
            try:
                df = backend.load(blob_ref)
                deleted_mask = (df['path'] == rel_path) | df['path'].str.startswith(rel_path + '/')
                if deleted_mask.any():
                    deleted_rows = df[deleted_mask]
                    target_row = deleted_rows[deleted_rows['path'] == rel_path]
                    if not target_row.empty:
                        deleted_size = int(target_row.iloc[0]['size'])
                        deleted_n_desc = int(target_row.iloc[0].get('n_desc', 1) or 1)

                    # Remove deleted rows and update ancestors
                    df = df[~deleted_mask].copy()

                    # Update ancestor stats
                    parts = rel_path.split('/') if rel_path != '.' else []
                    ancestors = ['.']
                    for i in range(1, len(parts)):
                        ancestors.append('/'.join(parts[:i]))

                    for ancestor in ancestors:
                        mask = df['path'] == ancestor
                        if mask.any():
                            df.loc[mask, 'size'] = df.loc[mask, 'size'] - (deleted_size or 0)
                            df.loc[mask, 'n_desc'] = df.loc[mask, 'n_desc'] - (deleted_n_desc or 0)
                            # n_children only for direct parent
                            if ancestor == ('/'.join(parts[:-1]) if len(parts) > 1 else '.'):
                                df.loc[mask, 'n_children'] = df.loc[mask, 'n_children'] - 1

                    # Rewrite parquet (this is the expensive part)
                    write_listing(df, resolve_blob(blob_ref), BLOB_ROW_GROUP_SIZE)

                    # Update denormalized stats in SQLite scan metadata
                    root_row = df[df['path'] == '.']
                    if not root_row.empty:
                        r = root_row.iloc[0]
                        db.execute('''
                            UPDATE scan SET size = ?, n_children = ?, n_desc = ?
                            WHERE id = ?
                        ''', (
                            int(r['size']) if pd.notna(r['size']) else None,
                            int(r['n_children']) if pd.notna(r.get('n_children')) else None,
                            int(r['n_desc']) if pd.notna(r.get('n_desc')) else None,
                            covering_scan['id'],
                        ))
                        db.commit()
            except Exception as e:
                print(f"Warning: Failed to update scan after delete: {e}")

    # Invalidate caches so next request gets fresh data
    _cache.clear()
    if hasattr(backend, 'clear_cache'):
        backend.clear_cache()

    return jsonify({
        'success': True,
        'path': path,
        'deleted_size': deleted_size,
        'deleted_n_desc': deleted_n_desc,
        'already_deleted': not path_exists,
    })


# ---- staged delete (spec `specs/staged-delete.md`) ------------------------
# The HTTP surface behind the `/staged` UI, over the CP1 engine. The Cloudflare
# edge (`ui/cfn/stagedRoutes.ts`) implements the same shapes but *enqueues* a run
# (it can't reach arbitrary buckets); the Flask peer deletes inline (local-confirm).

def _staged_who() -> str:
    """Actor for a staged action on the local (ungated) server."""
    return os.environ.get('USER') or 'local'


def _staged_epoch(dt) -> int | None:
    """A model `datetime` as epoch seconds, matching the edge's D1 shape."""
    return int(dt.timestamp()) if dt is not None else None


def _staged_uris(data):
    uris = data.get('uris')
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
        return None
    return uris


@app.route('/api/staged')
def api_staged():
    """Open plans (staged sets) with their URIs — each sized from its freshest
    covering scan (`bytes`/`objects`, what a dispatch would report, plus its
    `kind`) — plus the recent runs feed."""
    from sqlalchemy import select
    from disk_tree.sqla import DeletionRun, Plan
    from disk_tree.staged import items
    from disk_tree.staged_backend import describe, session

    def item(uri: str) -> dict:
        return {'uri': uri, **describe(uri)}

    s = session()
    plans = list(s.scalars(select(Plan).where(Plan.state == 'open').order_by(Plan.id)))
    runs = list(s.scalars(select(DeletionRun).order_by(DeletionRun.started_ts.desc()).limit(20)))
    return jsonify({
        'plans': [
            {
                'id': p.id, 'name': p.name, 'state': p.state, 'created_by': p.created_by,
                'created_ts': _staged_epoch(p.created_ts), 'items': [item(it.uri) for it in items(s, p)],
            }
            for p in plans
        ],
        'runs': [
            {
                'run_id': r.run_id, 'plan_id': r.plan_id, 'mode': r.mode, 'actor': r.actor,
                'started_ts': _staged_epoch(r.started_ts), 'finished_ts': _staged_epoch(r.finished_ts),
                'deleted_bytes': r.deleted_bytes, 'deleted_objects': r.deleted_objects,
            }
            for r in runs
        ],
    })


@app.route('/api/plans/stage', methods=['POST'])
def api_stage():
    """Stage URIs into the shared open plan."""
    from disk_tree.staged import stage
    from disk_tree.staged_backend import session

    uris = _staged_uris(request.get_json() or {})
    if uris is None:
        return jsonify({'error': '`uris` must be a non-empty array of strings'}), 400
    note = (request.get_json() or {}).get('note')
    s = session()
    plan, added = stage(s, uris, _staged_who(), note)
    s.commit()
    return jsonify({'plan_id': plan.id, 'added': added})


@app.route('/api/plans/unstage', methods=['POST'])
def api_unstage():
    """Remove URIs from every open plan."""
    from disk_tree.staged import unstage
    from disk_tree.staged_backend import session

    uris = _staged_uris(request.get_json() or {})
    if uris is None:
        return jsonify({'error': '`uris` must be a non-empty array of strings'}), 400
    s = session()
    removed = unstage(s, uris)
    s.commit()
    return jsonify({'removed': removed})


@app.route('/api/dispatch', methods=['POST'])
def api_dispatch():
    """Dispatch a plan. The local server deletes inline (`for_real`, default
    true); pass `for_real=false` for a dry report. `uris` (optional) dispatches
    just those staged items — they leave the plan, which stays open while
    anything remains staged (spec `staged-page-ux.md` §2)."""
    from sqlalchemy import select
    from disk_tree.staged import dispatch, items, plan_by_ref
    from disk_tree.staged_backend import delete_fn, session, size_fn

    data = request.get_json(silent=True) or {}
    ref = data.get('plan')
    for_real = bool(data.get('for_real', True))
    uris = None
    if 'uris' in data:
        uris = _staged_uris(data)
        if uris is None:
            return jsonify({'error': '`uris` must be a non-empty array of strings'}), 400
    s = session()
    plan = plan_by_ref(s, ref)
    if plan is None:
        return jsonify({'error': f'no plan {ref or "(open Staged)"}'}), 404
    if plan.state != 'open':
        return jsonify({'error': f'plan {plan.id} is already {plan.state}'}), 409
    its = items(s, plan)
    if not its:
        return jsonify({'error': f'plan {plan.id} has no staged items'}), 400
    try:
        run = dispatch(s, plan, _staged_who(), for_real=for_real, delete_fn=delete_fn, size_fn=size_fn, uris=uris)
    except KeyError as e:
        return jsonify({'error': str(e.args[0])}), 400
    s.commit()
    if for_real:
        # Deleted objects — drop cached scan slices so the next read is fresh.
        _cache.clear()
    # A dry run deletes nothing, so its scope lives on the bands: `bytes`/`objects`
    # are what the run covered (dry or real); `deleted_*` what it actually removed.
    from sqlalchemy import func
    from disk_tree.sqla import DeletionBand
    tot_bytes, tot_objs = s.execute(
        select(func.coalesce(func.sum(DeletionBand.bytes), 0), func.coalesce(func.sum(DeletionBand.objects), 0))
        .where(DeletionBand.run_id == run.run_id)
    ).one()
    return jsonify({
        'run_id': run.run_id, 'plan_id': plan.id, 'mode': run.mode, 'items': len(uris) if uris else len(its),
        'bytes': tot_bytes, 'objects': tot_objs,
        'deleted_bytes': run.deleted_bytes, 'deleted_objects': run.deleted_objects,
        'state': 'done' if for_real else 'dry',
    })


#: What this server can do — the live Flask peer can do everything. The static
#: Cloudflare Pages deployment (`ui/functions/api/capabilities.ts`) answers the
#: same shape with most of these off, and the UI hides those affordances. Keep
#: in sync with `ui/src/api.ts` `Capabilities`.
CAPABILITIES = {
    'static': False,
    'scan': True,
    'delete': True,
    'reveal': True,
    'histogram': True,
    'filter': True,
    'preview': True,
    'compare': True,
    'progress': True,
    'library': True,
    'backend': True,
    's3': True,
    'stageDelete': True,
    'deleteApproval': 'sync',
}


def _delete_approval() -> str:
    """The deployment's delete-approval policy (spec `staged-delete.md` CP6):
    `DISK_TREE_DELETE_APPROVAL`, else buckets.yml `delete.approval`, else `sync`
    (a credentialed local server just deletes)."""
    v = os.environ.get('DISK_TREE_DELETE_APPROVAL')
    if v in ('sync', 'staged', 'user-choice'):
        return v
    try:
        import yaml
        from disk_tree.config import ROOT_DIR
        p = os.path.join(ROOT_DIR, 'buckets.yml')
        if os.path.exists(p):
            with open(p) as f:
                raw = yaml.safe_load(f) or {}
            a = (raw.get('delete') or {}).get('approval')
            if a in ('sync', 'staged', 'user-choice'):
                return a
    except Exception:
        pass
    return 'sync'


@app.route('/api/capabilities')
def get_capabilities():
    return jsonify({**CAPABILITIES, 'deleteApproval': _delete_approval()})


@app.route('/api/backend', methods=['GET'])
def get_backend_info():
    """Get information about the current storage backend.

    Returns:
        name: Backend name (parquet, duckdb, sqlite, hybrid)
        supports_updates: Whether in-place updates are supported
        stats: Backend-specific statistics (if available)
    """
    backend = get_backend()
    result = {
        'name': backend.name,
        'supports_updates': backend.supports_updates,
    }

    # Add backend-specific stats if available
    if hasattr(backend, 'get_stats'):
        try:
            result['stats'] = backend.get_stats()
        except Exception:
            pass

    return jsonify(result)


@app.route('/api/backend/available', methods=['GET'])
def get_available_backends():
    """List available storage backends.

    Returns list of backends with their capabilities.
    Note: Switching backends requires restarting with DISK_TREE_BACKEND env var.
    """
    backends = [
        {
            'name': 'hybrid',
            'description': 'Chunked parquet files (default)',
            'supports_updates': True,
        },
        {
            'name': 'duckdb',
            'description': 'Single DuckDB database',
            'supports_updates': True,
        },
        {
            'name': 'sqlite',
            'description': 'Single SQLite database',
            'supports_updates': True,
        },
        {
            'name': 'parquet',
            'description': 'Individual parquet files (legacy)',
            'supports_updates': False,
        },
    ]

    current = get_backend().name
    for b in backends:
        b['current'] = b['name'] == current

    return jsonify({
        'backends': backends,
        'current': current,
        'switch_instructions': 'Set DISK_TREE_BACKEND env var and restart server',
    })


def _label(path: str) -> str:
    return basename(path.rstrip('/')) or path


def _scan_count(db_path: str) -> int:
    if not exists(db_path):
        return 0
    con = sqlite3.connect(db_path)
    try:
        # A freshly-created root has a DB file but no `scan` table yet — a
        # legitimate "empty library" state, not an error.
        try:
            return con.execute('SELECT COUNT(*) FROM scan').fetchone()[0]
        except sqlite3.OperationalError:
            return 0
    finally:
        con.close()


def _library_payload() -> dict:
    """Active library + recents, each decorated with a display label and scan count."""
    state = library.list_libraries()
    cur = state['current']
    return {
        'current': {
            'path': cur,
            'label': _label(cur),
            'exists': exists(join(cur, 'disk-tree.db')),
            'scans': _scan_count(join(cur, 'disk-tree.db')),
        },
        'recents': [
            {'path': r['path'], 'label': _label(r['path']), 'opened_at': r.get('opened_at')}
            for r in state.get('recents', [])
        ],
    }


@app.route('/api/library', methods=['GET'])
def get_library():
    """The open scans library plus recently-opened ones."""
    return jsonify(_library_payload())


@app.route('/api/library/open', methods=['POST'])
def open_library_route():
    """Switch the active library to `path`, rebinding DB / backend / caches."""
    data = request.get_json(silent=True) or {}
    path = (data.get('path') or '').strip()
    if not path:
        return jsonify({'error': 'path required'}), 400
    try:
        library.open_library(path)
    except (ValueError, RuntimeError) as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(_library_payload())


@app.route('/api/library/pick', methods=['POST'])
def pick_library_dir():
    """Native folder picker — only when running inside the pywebview app window.

    The browser has no way to hand back an absolute directory path, so it gets a
    501 and falls back to a path input.
    """
    try:
        import webview
    except ImportError:
        return jsonify({'error': 'no native picker'}), 501
    wins = getattr(webview, 'windows', None) or []
    if not wins:
        return jsonify({'error': 'no window'}), 501
    result = wins[0].create_file_dialog(webview.FOLDER_DIALOG)
    return jsonify({'picked': result[0] if result else None})


# Static file serving routes (if UI is bundled)
if STATIC_DIR:
    @app.route('/')
    def serve_index():
        """Serve the SPA index.html."""
        return send_from_directory(STATIC_DIR, 'index.html')

    @app.route('/assets/<path:filename>')
    def serve_assets(filename):
        """Serve static assets (JS, CSS, fonts, images)."""
        return send_from_directory(join(STATIC_DIR, 'assets'), filename)

    @app.route('/<path:path>')
    def serve_spa_routes(path):
        """SPA catch-all: serve index.html for client-side routes.

        Non-API routes that don't match a static file get index.html,
        allowing React Router to handle them.
        """
        # Check if it's a static file that exists
        static_path = join(STATIC_DIR, path)
        if exists(static_path) and isfile(static_path):
            return send_from_directory(STATIC_DIR, path)
        # Otherwise serve index.html for SPA routing
        return send_from_directory(STATIC_DIR, 'index.html')


def main():
    import os
    debug = os.environ.get('FLASK_DEBUG', '0').lower() in ('1', 'true', 'yes')
    port = int(os.environ.get('PORT', '5001'))
    if STATIC_DIR:
        print(f"Serving UI from: {STATIC_DIR}")
    else:
        print("No UI found. Run 'cd ui && pnpm build' to build the UI.")
    app.run(debug=debug, port=port)


if __name__ == '__main__':
    main()
