"""Scan-registry queries shared by the server and the CLI (no Flask import)."""

from __future__ import annotations


def freshest_scan_covering(db, uri: str, scan_id: str | None = None) -> dict | None:
    """Newest scan whose path is `uri` or an ancestor of it (or `scan_id`,
    verified), whose blob is *reachable* — a scan whose parquet lives only on an
    unmounted volume is skipped so `du`/`filter`/etc. fall back to an older
    reachable scan instead of crashing on the missing blob (spec
    `r2-scan-target.md`). An explicit `scan_id` is honored even if unreachable."""
    from disk_tree.backends import url_parent
    from disk_tree.config import blob_reachable
    if scan_id:
        row = db.execute('SELECT * FROM scan WHERE id = ?', (scan_id,)).fetchone()
        if not row:
            return None
        scan = dict(row)
        if uri == scan['path'] or uri.startswith(scan['path'].rstrip('/') + '/'):
            return scan
        return None
    # Every scan of `uri` or an ancestor (not just the newest per path), so a
    # newest-but-unreachable scan can be skipped in favor of an older reachable
    # one rather than being the sole candidate and crashing at load.
    candidates = []
    test_path = uri
    while test_path:
        rows = db.execute(
            'SELECT * FROM scan WHERE path = ? ORDER BY time DESC', (test_path,)
        ).fetchall()
        candidates.extend(dict(r) for r in rows)
        parent = url_parent(test_path)
        if parent is None or parent == test_path:
            break
        test_path = parent
    if not candidates:
        return None
    candidates.sort(key=lambda s: s['time'], reverse=True)
    return next((s for s in candidates if blob_reachable(s['blob'])), None)
