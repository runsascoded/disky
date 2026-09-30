"""Migration commands for disk-tree database."""
from os import makedirs, rename, replace
from os.path import basename, isabs, isfile, join

import pandas as pd
from click import argument, command, option
from utz import err

from disk_tree.blobfs import read_parquet  # v2 listings come back in the v1 shape (spec `listing-slim.md`)
from disk_tree.cli.base import cli
from disk_tree.config import SCANS_DIR, SQLITE_PATH as DB_PATH
from disk_tree.listing_format import write_listing  # …and every rewrite goes back out as v2
from disk_tree.storage.base import BLOB_ROW_GROUP_SIZE


@cli.command('migrate')
def migrate():
    """Run database migrations: add every column the models have that the DB
    lacks (any table), then backfill `scan` stats from the parquet blobs."""
    import sqlite3
    from sqlalchemy import create_engine
    from disk_tree.sqla.migrate import add_missing_columns

    if not isfile(DB_PATH):
        err(f"Database not found: {DB_PATH}")
        return

    # Generic schema catch-up (also runs on every server/CLI DB init): the
    # explicit `scan` list below predates it and stays for the backfill.
    added = add_missing_columns(create_engine(f"sqlite:///{DB_PATH}"))
    for table, col in added:
        err(f"Added column: {table}.{col}")
    if not added:
        err("Schema up to date (no missing columns)")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Check existing columns
    cursor.execute("PRAGMA table_info(scan)")
    columns = {row['name'] for row in cursor.fetchall()}

    # Add new columns if they don't exist
    new_columns = [
        ('size', 'INTEGER'),
        ('n_children', 'INTEGER'),
        ('n_desc', 'INTEGER'),
        ('mtime', 'INTEGER'),
    ]
    for col_name, col_type in new_columns:
        if col_name not in columns:
            err(f"Adding column: {col_name}")
            cursor.execute(f"ALTER TABLE scan ADD COLUMN {col_name} {col_type}")
            conn.commit()
        else:
            err(f"Column already exists: {col_name}")

    # Backfill stats from parquet files
    cursor.execute("SELECT id, path, blob, size, mtime FROM scan")
    rows = cursor.fetchall()
    err(f"Backfilling stats for {len(rows)} scans...")

    updated = 0
    skipped = 0
    errors = 0
    for row in rows:
        scan_id = row['id']
        blob_path = row['blob']
        existing_size = row['size']
        existing_mtime = row['mtime']

        # Skip if already fully migrated (has both size and mtime)
        if existing_size is not None and existing_mtime is not None:
            skipped += 1
            continue

        if not isfile(blob_path):
            err(f"  Parquet not found: {blob_path}")
            errors += 1
            continue

        try:
            df = read_parquet(blob_path)
            # Try 'parent == ""' first (local scans), fallback to 'path == "."' (S3 scans)
            root_rows = df[df['parent'] == '']
            if root_rows.empty:
                root_rows = df[df['path'] == '.']
            if root_rows.empty:
                err(f"  No root row found: {blob_path}")
                errors += 1
                continue

            root = root_rows.iloc[0]
            size = int(root['size']) if pd.notna(root['size']) else None
            n_children = int(root['n_children']) if pd.notna(root.get('n_children')) else None
            n_desc = int(root['n_desc']) if pd.notna(root.get('n_desc')) else None
            mtime = int(root['mtime']) if pd.notna(root.get('mtime')) else None

            cursor.execute(
                "UPDATE scan SET size = ?, n_children = ?, n_desc = ?, mtime = ? WHERE id = ?",
                (size, n_children, n_desc, mtime, scan_id),
            )
            conn.commit()
            updated += 1
        except Exception as e:
            err(f"  Error processing {blob_path}: {e}")
            errors += 1

    conn.close()
    err(f"Migration complete: {updated} updated, {skipped} skipped, {errors} errors")


@cli.command('migrate-depth')
def migrate_depth():
    """Add depth column to existing parquet files for predicate pushdown."""
    import sqlite3
    import pyarrow.parquet as pq

    if not isfile(DB_PATH):
        err(f"Database not found: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT id, path, blob FROM scan")
    rows = cursor.fetchall()
    err(f"Checking {len(rows)} parquet files for depth column...")

    updated = 0
    skipped = 0
    errors = 0
    for row in rows:
        blob_path = row['blob']

        if not isfile(blob_path):
            err(f"  Parquet not found: {blob_path}")
            errors += 1
            continue

        try:
            schema = pq.read_schema(blob_path)
            has_depth = 'depth' in schema.names

            df = read_parquet(blob_path)

            # Add depth column if missing
            if not has_depth:
                df['depth'] = df['path'].apply(lambda p: 0 if p == '.' else p.count('/') + 1)

            # Always re-sort by depth for efficient parquet filtering (breadth-first order)
            df = df.sort_values(['depth', 'path']).reset_index(drop=True)
            write_listing(df, blob_path, BLOB_ROW_GROUP_SIZE)
            updated += 1
        except Exception as e:
            err(f"  Error processing {blob_path}: {e}")
            errors += 1

    conn.close()
    err(f"Depth migration complete: {updated} updated, {errors} errors")


@cli.command('migrate-hybrid')
@option('-n', '--dry-run', is_flag=True, help="Show what would be done without making changes")
def migrate_hybrid(dry_run: bool):
    """Migrate existing parquet scans to hybrid chunked format.

    Re-saves each scan using HybridBackend, which auto-chunks large subtrees.
    Original parquets are moved to a backup directory.
    """
    import sqlite3
    from disk_tree.storage.hybrid import HybridBackend

    if not isfile(DB_PATH):
        err(f"Database not found: {DB_PATH}")
        return

    # Create backup directory
    backup_dir = join(SCANS_DIR, 'backup_pre_hybrid')
    if not dry_run:
        makedirs(backup_dir, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT id, path, blob FROM scan ORDER BY time")
    rows = cursor.fetchall()
    err(f"Migrating {len(rows)} scans to hybrid format...")

    backend = HybridBackend()

    migrated = 0
    chunked = 0
    skipped = 0
    errors = 0

    for row in rows:
        scan_id = row['id']
        scan_path = row['path']
        old_blob = row['blob']

        if not isfile(old_blob):
            err(f"  Parquet not found: {old_blob}")
            errors += 1
            continue

        try:
            df = read_parquet(old_blob)
            n_rows = len(df)

            # Check if already has child_scan_id (already hybrid)
            if 'child_scan_id' in df.columns and df['child_scan_id'].notna().any():
                err(f"  Already hybrid: {scan_path} ({n_rows} rows)")
                skipped += 1
                continue

            if dry_run:
                # Estimate if chunking will happen
                if 'n_desc' in df.columns and 'depth' in df.columns:
                    depth1_dirs = df[(df['depth'] == 1) & (df['kind'] == 'dir')]
                    large = depth1_dirs[depth1_dirs['n_desc'] >= backend.chunk_threshold]
                    if not large.empty:
                        err(f"  Would chunk: {scan_path} ({n_rows} rows, {len(large)} large subtrees)")
                        chunked += 1
                    else:
                        err(f"  Would migrate: {scan_path} ({n_rows} rows, no chunking needed)")
                else:
                    err(f"  Would migrate: {scan_path} ({n_rows} rows)")
                migrated += 1
                continue

            # Save using hybrid backend
            new_blob = backend.save(df, scan_path)

            # Check if chunking occurred
            chunk_stats = backend.get_chunk_stats(new_blob)
            num_chunks = chunk_stats.get('total_chunks', 0)

            # Update database
            cursor.execute("UPDATE scan SET blob = ? WHERE id = ?", (new_blob, scan_id))
            conn.commit()

            # Move original to backup
            backup_path = join(backup_dir, basename(old_blob))
            rename(old_blob, backup_path)

            if num_chunks > 0:
                err(f"  Chunked: {scan_path} ({n_rows} rows → {num_chunks} chunks)")
                chunked += 1
            else:
                err(f"  Migrated: {scan_path} ({n_rows} rows)")
            migrated += 1

        except Exception as e:
            err(f"  Error processing {scan_path}: {e}")
            errors += 1

    conn.close()

    if dry_run:
        err(f"Dry run complete: {migrated} would migrate ({chunked} would chunk), {skipped} skipped, {errors} errors")
    else:
        err(f"Migration complete: {migrated} migrated ({chunked} chunked), {skipped} skipped, {errors} errors")
        err(f"Originals backed up to: {backup_dir}")


@cli.command('migrate-blobs')
@option('-n', '--dry-run', is_flag=True, help="Show what would be done without making changes")
def migrate_blobs(dry_run: bool):
    """Convert absolute parquet blob refs to basenames (relative to SCANS_DIR).

    Rewrites:
    - scan.blob column in SQLite
    - child_scan_id column inside hybrid parquet files
    """
    import sqlite3

    if not isfile(DB_PATH):
        err(f"Database not found: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT id, blob FROM scan")
    rows = cursor.fetchall()
    db_updated = 0
    db_skipped = 0
    for row in rows:
        blob = row['blob']
        if not isabs(blob):
            db_skipped += 1
            continue
        new_blob = basename(blob)
        if dry_run:
            err(f"  scan.id={row['id']}: {blob} -> {new_blob}")
        else:
            cursor.execute("UPDATE scan SET blob = ? WHERE id = ?", (new_blob, row['id']))
        db_updated += 1
    if not dry_run:
        conn.commit()
    err(f"SQLite scan rows: {db_updated} updated, {db_skipped} already relative")

    # Rewrite child_scan_id inside each parquet
    cursor.execute("SELECT id, blob FROM scan")
    rows = cursor.fetchall()
    counts = {'updated': 0, 'skipped': 0, 'errors': 0}
    for row in rows:
        blob = row['blob']
        blob_path = blob if isabs(blob) else join(SCANS_DIR, blob)
        _normalize_parquet_chunks(blob_path, dry_run, counts)

    conn.close()
    err(f"Parquet files: {counts['updated']} rewritten, {counts['skipped']} unchanged, {counts['errors']} errors")


def _normalize_parquet_chunks(blob_path: str, dry_run: bool, counts: dict) -> None:
    """Rewrite child_scan_id column to basenames; recurse into referenced chunks."""
    if not isfile(blob_path):
        err(f"  Parquet not found: {blob_path}")
        counts['errors'] += 1
        return
    try:
        df = read_parquet(blob_path)
    except Exception as e:
        err(f"  Error reading {blob_path}: {e}")
        counts['errors'] += 1
        return

    if 'child_scan_id' not in df.columns:
        counts['skipped'] += 1
        return

    refs = df['child_scan_id'].dropna()
    abs_refs = refs[refs.apply(isabs)]
    if abs_refs.empty:
        counts['skipped'] += 1
    else:
        if dry_run:
            err(f"  Would rewrite {len(abs_refs)} refs in {basename(blob_path)}")
        else:
            df['child_scan_id'] = df['child_scan_id'].apply(
                lambda v: basename(v) if isinstance(v, str) and isabs(v) else v
            )
            write_listing(df, blob_path, BLOB_ROW_GROUP_SIZE)
        counts['updated'] += 1

    # Recurse into chunk parquets (resolve via basename in case still abs-on-disk)
    for child_ref in df['child_scan_id'].dropna():
        child_path = child_ref if isabs(child_ref) else join(SCANS_DIR, basename(child_ref))
        _normalize_parquet_chunks(child_path, dry_run, counts)


@cli.command('migrate-row-groups')
@option('-n', '--dry-run', is_flag=True, help='Only report blobs that would be rewritten')
@option('-r', '--rows', default=BLOB_ROW_GROUP_SIZE, help=f'Target rows per row group (default {BLOB_ROW_GROUP_SIZE})')
@argument('scans_dir', required=False)
def migrate_row_groups(dry_run: bool, rows: int, scans_dir: str | None):
    """Rewrite the blobs in SCANS_DIR (a dir or URL; default the write dir)
    whose parquet row groups exceed ROWS rows.

    A directory listing (`load(min_depth=max_depth=d, path_prefix=…)`) decodes
    every row group whose stats overlap, so row-group size is the unit of work
    per browse / diff expansion: ~4 ms at 64K rows vs ~40 ms at 1M — and over
    R2 it is the unit of *fetch*: a `depth ≤ 2` view of a 1M-row group pulls
    ~38 MiB, of a 64K-row group ~2 MiB (spec `scan-page-r2-latency.md`). Older
    blobs were written with 262K–1M-row groups. Rewrites stream and are atomic
    (tmp + move); sidecars are skipped — a vocab sidecar indexes by row group,
    so rebuild it with `disk-tree vocab` (it refuses stale reads).
    """
    from disk_tree import blobfs
    from disk_tree.find.groups import groups_path, write_groups_sidecar
    d = scans_dir or SCANS_DIR
    n = 0
    for name in blobfs.list_parquets(d):
        path = blobfs.join(d, name)
        sizes = blobfs.row_group_sizes(path)
        biggest = max(sizes, default=0)
        if biggest <= rows:
            continue
        n += 1
        err(f"{name}: {sum(sizes):,} rows in {len(sizes)} group(s), max {biggest:,}")
        if dry_run:
            continue
        blobfs.rewrite_row_groups(path, rows)
        # a `.groups.json` (the edge reader's precomputed footer) describes the
        # old groups — regenerate it beside the rewritten blob
        if blobfs.exists(groups_path(path)):
            write_groups_sidecar(path)
    err(f"{n} blob(s) {'would be ' if dry_run else ''}rewritten to ≤{rows:,}-row groups")
