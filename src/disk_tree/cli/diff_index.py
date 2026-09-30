"""`disk-tree diff-index`: build persisted scan-pair diff indexes (spec: diff-index.md)."""
import sqlite3

from click import argument, option
from utz import err

from disk_tree.cli.base import cli
from disk_tree.config import SQLITE_PATH
from disk_tree.diff_index import DIFF_TABLE_SQL


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(SQLITE_PATH)
    con.row_factory = sqlite3.Row
    return con


def build_pair(scan_a: int, scan_b: int, force: bool = False) -> dict | None:
    """Build (or reuse) the index for two scan ids; logs to stderr."""
    from disk_tree.diff_index import build_and_record, get_index
    if not force:
        existing = get_index(scan_a, scan_b)
        if existing and existing['status'] == 'done':
            err(f"diff {scan_a}→{scan_b}: already indexed ({existing['n_rows']:,} rows, {existing['blob']})")
            return existing
    with _connect() as con:
        a = con.execute('SELECT * FROM scan WHERE id = ?', (scan_a,)).fetchone()
        b = con.execute('SELECT * FROM scan WHERE id = ?', (scan_b,)).fetchone()
    if a is None or b is None:
        raise ValueError(f"scan id not found: {scan_a if a is None else scan_b}")
    err(f"diff {scan_a}→{scan_b}: indexing {a['path']} ({a['time']} → {b['time']})")
    row = build_and_record(scan_a, a['blob'], scan_b, b['blob'])
    err(f"diff {scan_a}→{scan_b}: {row['n_rows']:,} rows (+{row['n_added']:,} −{row['n_removed']:,} ~{row['n_changed']:,} touched {row['n_touched']:,}) in {row['seconds']:.1f}s → {row['blob']}")
    return row


def build_previous(scan_id: int, force: bool = False) -> dict | None:
    """Index `scan_id` against the previous scan of the same path. Diffing is
    best-effort prep: a failure here must never turn a persisted scan into a
    nonzero exit (a scheduled run would read as failed), so errors are logged
    and swallowed — see spec `r2-scan-target.md`."""
    from disk_tree.diff_index import previous_scan
    with _connect() as con:
        prev = previous_scan(con, scan_id)
    if prev is None:
        err(f"scan {scan_id}: no earlier scan with a reachable blob, nothing to diff")
        return None
    try:
        return build_pair(prev['id'], scan_id, force=force)
    except Exception as e:
        err(f"scan {scan_id}: diff vs scan {prev['id']} failed, skipping ({type(e).__name__}: {e})")
        return None


def build_latest(path: str, force: bool = False) -> dict | None:
    """Index the two most recent scans of `path` (no-op with fewer than two)."""
    with _connect() as con:
        rows = con.execute('SELECT id FROM scan WHERE path = ? ORDER BY time DESC LIMIT 2', (path,)).fetchall()
    if len(rows) < 2:
        err(f"{path}: fewer than two scans, nothing to diff")
        return None
    return build_pair(rows[1]['id'], rows[0]['id'], force=force)


def gc_indexes(dry_run: bool = False) -> int:
    """Drop diff indexes whose scans are gone (and orphan parquets in
    `diffs/`). Returns the number removed."""
    from glob import glob
    from os import remove
    from os.path import basename, exists, join

    from disk_tree.diff_index import diffs_dir

    n = 0
    with _connect() as con:
        con.execute(DIFF_TABLE_SQL)
        rows = con.execute('SELECT * FROM diff').fetchall()
        live = {r['id'] for r in con.execute('SELECT id FROM scan')}
        keep_blobs = set()
        for r in rows:
            if r['scan_a'] in live and r['scan_b'] in live:
                if r['blob']:
                    keep_blobs.add(r['blob'])
                continue
            err(f"diff {r['scan_a']}→{r['scan_b']}: scan(s) gone, dropping {r['blob'] or '(no blob)'}")
            n += 1
            if not dry_run:
                if r['blob'] and exists(r['blob']):
                    remove(r['blob'])
                con.execute('DELETE FROM diff WHERE id = ?', (r['id'],))
    # Parquets with no `diff` row at all (interrupted builds, manual deletes)
    for path in sorted(glob(join(diffs_dir(), '*.parquet'))):
        if path in keep_blobs:
            continue
        err(f"{basename(path)}: no diff row, removing")
        n += 1
        if not dry_run:
            remove(path)
    return n


@cli.command('diff-index')
@option('-a', '--all', 'all_paths', is_flag=True, help='Every scanned path: index its two most recent scans')
@option('-f', '--force', is_flag=True, help='Rebuild even if an index exists')
@option('-g', '--gc', 'gc', is_flag=True, help='Drop indexes whose scans are gone (and orphan parquets), then exit')
@option('-n', '--dry-run', is_flag=True, help='With --gc: report what would be removed')
@argument('args', nargs=-1)
def diff_index(all_paths: bool, force: bool, gc: bool, dry_run: bool, args: tuple[str, ...]):
    """Build the persisted diff index for a scan pair.

    ARGS: two scan ids (`A B`), or one or more paths (each path's two most
    recent scans). With -a, every scanned path. `disk-tree index` and
    `disk-tree sync` build the index against the previous scan automatically.
    """
    if gc:
        n = gc_indexes(dry_run=dry_run)
        err(f"{n} index(es) {'would be ' if dry_run else ''}removed")
        return
    if all_paths:
        with _connect() as con:
            paths = [r['path'] for r in con.execute('SELECT DISTINCT path FROM scan ORDER BY path')]
        for p in paths:
            build_latest(p, force=force)
        return
    if len(args) == 2 and all(a.isdigit() for a in args):
        build_pair(int(args[0]), int(args[1]), force=force)
        return
    if not args:
        raise SystemExit('expected two scan ids, one or more paths, or -a')
    for p in args:
        build_latest(p.rstrip('/') or '/', force=force)
