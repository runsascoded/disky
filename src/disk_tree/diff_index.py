"""Diff index: the complete diff of one scan pair, computed once (vectorized,
per depth) and persisted as parquet, so any `/compare` request for that pair
is a slice instead of a walk (spec: diff-index.md).

Layout of `<DIFFS_DIR>/<scan_a>-<scan_b>.parquet`, sorted `(depth, path)` in
64K-row groups (same read-side pushdown as scan blobs):

    path, parent, depth, kind, status, size_a, size_b, n_desc_a, n_desc_b,
    mtime_a, mtime_b, context

`status` ∈ added | removed | changed | touched | unchanged. Rows under an
added/removed dir are not stored (that dir's row is the whole story). The only
`unchanged` rows stored are `context=True`: unchanged children of a dir that
has at least one non-unchanged child — what the treemap draws as labeled grey
next to the changes (the walk's `unchanged_top`/`unchanged_rest`).
"""
from __future__ import annotations

from dataclasses import dataclass
from os import makedirs
from os.path import exists, join

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

import sqlite3
import time as _time
from datetime import datetime

from . import blobfs, config as _config
from .diff import resolve_blob
from .storage.base import BLOB_ROW_GROUP_SIZE

COLS = ['path', 'parent', 'depth', 'kind', 'size', 'n_desc', 'n_children', 'mtime']
STATUSES = ('added', 'removed', 'changed', 'touched', 'unchanged')


def diffs_dir() -> str:
    return join(_config.ROOT_DIR, 'diffs')


def index_path(scan_a: int, scan_b: int) -> str:
    return join(diffs_dir(), f'{scan_a}-{scan_b}.parquet')


# ---------------------------------------------------------------------------
# Loading a scan as one Arrow table (hybrid chunks expanded, paths rebased)

def load_scan_table(blob_ref: str) -> pa.Table:
    """Whole scan as an Arrow table with `COLS`, chunk blobs expanded in place
    (pointer rows kept — they carry the dir's aggregate — chunk root rows
    dropped). Arrow, not pandas: ~7M-row home scans are ~1 GB here vs ~4 GB
    as object-dtype frames."""
    path = resolve_blob(blob_ref)
    schema = blobfs.read_schema(path)
    have = [c for c in COLS if c in schema.names]
    extra = ['child_scan_id'] if 'child_scan_id' in schema.names else []
    tbl = blobfs.read_table(path, columns=have + extra)
    tbl = _normalize(tbl)
    if extra:
        ptr = tbl.filter(pc.is_valid(tbl['child_scan_id']))
        parts = [tbl.drop_columns(['child_scan_id'])]
        for parent_path, child_ref in zip(ptr['path'].to_pylist(), ptr['child_scan_id'].to_pylist()):
            if blobfs.exists(resolve_blob(child_ref)):
                child = load_scan_table(child_ref)
                parts.append(_rebase(child, parent_path))
        tbl = pa.concat_tables(parts, promote_options='default')
    return tbl


def _normalize(tbl: pa.Table) -> pa.Table:
    """Fill missing `COLS` with nulls, cast, `depth` from path when absent,
    and root-level files' parent `''` → `'.'` (see find/index.py)."""
    n = tbl.num_rows
    cols = {}
    for c in COLS:
        if c in tbl.column_names:
            cols[c] = tbl[c]
        elif c == 'depth':
            cols[c] = pc.if_else(
                pc.equal(tbl['path'], '.'),
                pa.scalar(0, pa.int32()),
                pc.cast(pc.add(pc.count_substring(tbl['path'], '/'), 1), pa.int32()),
            )
        else:
            typ = pa.string() if c in ('parent', 'kind') else pa.float64()
            cols[c] = pa.nulls(n, typ)
    parent = pc.if_else(
        pc.and_(pc.equal(cols['parent'], ''), pc.not_equal(cols['path'], '.')),
        pa.scalar('.'),
        cols['parent'],
    )
    cols['parent'] = parent
    cols['depth'] = pc.cast(cols['depth'], pa.int32())
    for c in ('size', 'n_desc', 'n_children', 'mtime'):
        cols[c] = pc.cast(cols[c], pa.float64())
    out = pa.table(cols)
    if 'child_scan_id' in tbl.column_names:
        out = out.append_column('child_scan_id', tbl['child_scan_id'])
    return out


def _rebase(child: pa.Table, prefix: str) -> pa.Table:
    """Chunk coordinates → parent coordinates: `.` is the pointer row (drop),
    `x` → `prefix/x`, parent `.` → `prefix`."""
    child = child.filter(pc.not_equal(child['path'], '.'))
    # Scalars typed like the column: a pandas-3-written blob holds `large_string`
    # paths, and the join kernel has no mixed `(large_string, string)` form.
    t = child['path'].type
    pfx, sep = pa.scalar(prefix, type=t), pa.scalar('/', type=t)
    path = pc.binary_join_element_wise(pfx, child['path'], sep)
    parent = pc.if_else(
        pc.equal(child['parent'], '.'),
        pfx,
        pc.binary_join_element_wise(pfx, pc.cast(child['parent'], t), sep),
    )
    depth = pc.add(child['depth'], pa.scalar(prefix.count('/') + 1, pa.int32()))
    return child.set_column(0, 'path', path).set_column(1, 'parent', parent).set_column(2, 'depth', pc.cast(depth, pa.int32()))


# ---------------------------------------------------------------------------
# The diff

@dataclass
class DiffIndexStats:
    n_rows: int
    n_added: int
    n_removed: int
    n_changed: int
    n_touched: int
    n_context: int


def iter_diff_depths(ta: pa.Table, tb: pa.Table, stats: DiffIndexStats):
    """Yield one index table per depth, ascending — the diff, streamed.

    Full outer join per depth (rows at depth d only ever match rows at depth
    d), status per row, prune under added/removed dirs, keep unchanged
    *context* rows (siblings of changes). Depths are yielded in order and each
    is sorted by path, so the concatenation is already sorted `(depth, path)`
    — the layout the read path's pushdown wants. `stats` is updated in place.
    """
    max_depth = max(_max(ta['depth']), _max(tb['depth']))
    blocked: set[str] = set()      # added/removed dirs and everything under them
    counts = dict.fromkeys(STATUSES, 0)
    for d in range(0, max_depth + 1):
        a = ta.filter(pc.equal(ta['depth'], d))
        b = tb.filter(pc.equal(tb['depth'], d))
        j = a.join(b, keys='path', join_type='full outer', left_suffix='_a', right_suffix='_b')
        if j.num_rows == 0:
            continue
        in_a = pc.is_valid(j['kind_a']).to_numpy(zero_copy_only=False)
        in_b = pc.is_valid(j['kind_b']).to_numpy(zero_copy_only=False)
        size_a = j['size_a'].to_numpy(zero_copy_only=False)
        size_b = j['size_b'].to_numpy(zero_copy_only=False)
        nd_a = j['n_desc_a'].to_numpy(zero_copy_only=False)
        nd_b = j['n_desc_b'].to_numpy(zero_copy_only=False)
        mt_a = j['mtime_a'].to_numpy(zero_copy_only=False)
        mt_b = j['mtime_b'].to_numpy(zero_copy_only=False)
        kind_a = j['kind_a']
        kind_b = j['kind_b']
        kind = pc.if_else(pc.is_valid(kind_b), kind_b, kind_a)
        parent = pc.if_else(pc.is_valid(j['parent_b']), j['parent_b'], j['parent_a'])

        both = in_a & in_b
        stat_diff = _ne(size_a, size_b) | _ne(nd_a, nd_b)
        kind_diff = pc.not_equal(kind_a, kind_b).fill_null(False).to_numpy(zero_copy_only=False)
        status = np.full(j.num_rows, 'unchanged', dtype=object)
        status[~in_a] = 'added'
        status[~in_b] = 'removed'
        status[both & (stat_diff | kind_diff)] = 'changed'
        status[both & ~(stat_diff | kind_diff) & _ne(mt_a, mt_b)] = 'touched'

        # Prune: anything whose parent is blocked is under an added/removed dir.
        # (Arrow-side membership: materializing millions of parent strings
        # per depth as Python objects was most of the build's peak memory.)
        if blocked:
            under = pc.is_in(parent, value_set=pa.array(list(blocked))).to_numpy(zero_copy_only=False)
        else:
            under = np.zeros(j.num_rows, dtype=bool)
        is_dir = pc.equal(kind, 'dir').fill_null(False).to_numpy(zero_copy_only=False)
        newly_blocked = is_dir & (under | (status == 'added') | (status == 'removed'))
        if newly_blocked.any():
            blocked.update(j['path'].filter(pa.array(newly_blocked)).to_pylist())

        keep = (status != 'unchanged') & ~under
        unchanged = (status == 'unchanged') & ~under
        cols = {
            'path': j['path'],
            'parent': parent,
            'depth': pa.array(np.full(j.num_rows, d, dtype=np.int32)),
            'kind': kind,
            'status': pa.array(status, pa.string()),
            'size_a': j['size_a'], 'size_b': j['size_b'],
            'n_desc_a': j['n_desc_a'], 'n_desc_b': j['n_desc_b'],
            'mtime_a': j['mtime_a'], 'mtime_b': j['mtime_b'],
        }
        rows = pa.table(cols)
        kept = rows.filter(pa.array(keep))
        for st in STATUSES[:-1]:
            counts[st] += int(np.count_nonzero(status[keep] == st))
        at_depth = [kept.append_column('context', pa.array(np.zeros(kept.num_rows, dtype=bool)))]

        # Context: this depth's unchanged rows whose parent also has a kept
        # child — the grey siblings drawn beside the change.
        if kept.num_rows and unchanged.any():
            unch = rows.filter(pa.array(unchanged))
            ctx = unch.filter(pc.is_in(unch['parent'], value_set=pc.unique(kept['parent'])))
            if ctx.num_rows:
                stats.n_context += ctx.num_rows
                at_depth.append(ctx.append_column('context', pa.array(np.ones(ctx.num_rows, dtype=bool))))

        tbl = pa.concat_tables(at_depth) if len(at_depth) > 1 else at_depth[0]
        if tbl.num_rows:
            stats.n_rows += tbl.num_rows
            yield tbl.sort_by([('path', 'ascending')])

    stats.n_added = counts['added']
    stats.n_removed = counts['removed']
    stats.n_changed = counts['changed']
    stats.n_touched = counts['touched']


def build_diff_table(ta: pa.Table, tb: pa.Table) -> tuple[pa.Table, DiffIndexStats]:
    """The whole index in memory (tests, small inputs) — `iter_diff_depths`
    concatenated."""
    stats = DiffIndexStats(0, 0, 0, 0, 0, 0)
    parts = list(iter_diff_depths(ta, tb, stats))
    return (pa.concat_tables(parts) if parts else _empty_index()), stats


def _empty_index() -> pa.Table:
    return pa.table({
        'path': pa.array([], pa.string()), 'parent': pa.array([], pa.string()),
        'depth': pa.array([], pa.int32()), 'kind': pa.array([], pa.string()),
        'status': pa.array([], pa.string()),
        'size_a': pa.array([], pa.float64()), 'size_b': pa.array([], pa.float64()),
        'n_desc_a': pa.array([], pa.float64()), 'n_desc_b': pa.array([], pa.float64()),
        'mtime_a': pa.array([], pa.float64()), 'mtime_b': pa.array([], pa.float64()),
        'context': pa.array([], pa.bool_()),
    })


def _max(col: pa.ChunkedArray) -> int:
    v = pc.max(col).as_py()
    return 0 if v is None else int(v)


def _ne(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Element-wise `!=`, NaN-safe (both-NaN is equal, one-NaN is not)."""
    xn, yn = np.isnan(x), np.isnan(y)
    return np.where(xn | yn, xn != yn, x != y)


def build_diff_index(scan_a_id: int, blob_a: str, scan_b_id: int, blob_b: str) -> tuple[str, DiffIndexStats]:
    """Compute and persist the index for a scan pair; returns (path, stats).

    Depths stream to parquet as they finish rather than accumulating: the
    join outputs dominate peak RSS (two home-dir scans held every depth's
    result at once, ~7 GB), and each depth is independent once written.
    """
    import os
    ta = load_scan_table(blob_a)
    tb = load_scan_table(blob_b)
    makedirs(diffs_dir(), exist_ok=True)
    out = index_path(scan_a_id, scan_b_id)
    tmp = out + '.tmp'
    stats = DiffIndexStats(0, 0, 0, 0, 0, 0)
    writer = None
    try:
        for tbl in iter_diff_depths(ta, tb, stats):
            if writer is None:
                writer = pq.ParquetWriter(tmp, tbl.schema)
            writer.write_table(tbl, row_group_size=BLOB_ROW_GROUP_SIZE)
        if writer is None:  # no differing rows at any depth
            writer = pq.ParquetWriter(tmp, _empty_index().schema)
            writer.write_table(_empty_index())
    finally:
        if writer is not None:
            writer.close()
    del ta, tb
    os.replace(tmp, out)
    return out, stats


# ---------------------------------------------------------------------------
# Serving a slice

def load_index_slice(path: str, rel_prefix: str) -> pd.DataFrame:
    """Rows at/under `rel_prefix` ('' = whole scan), rebased so paths are
    relative to the prefix (its own row is dropped)."""
    if rel_prefix:
        lo, hi = rel_prefix + '/', rel_prefix + '0'
        df = pd.read_parquet(path, filters=[('path', '>=', lo), ('path', '<', hi)])
        cut = len(rel_prefix) + 1
        df['path'] = df['path'].str[cut:]
        df['parent'] = np.where(df['parent'] == rel_prefix, '.', df['parent'].str[cut:])
        df['depth'] = df['depth'] - (rel_prefix.count('/') + 1)
    else:
        df = pd.read_parquet(path)
        df = df[df['path'] != '.']
    return df.reset_index(drop=True)


def _with_ancestors(chg: pd.DataFrame, keep: set[str]) -> pd.DataFrame:
    """`keep` plus every ancestor of a kept row that the frame holds — a cell
    can't render without its spine."""
    have = set(chg['path'])
    for p in list(keep):
        q = p
        while '/' in q:
            q = q.rsplit('/', 1)[0]
            if q in keep:
                break
            if q in have:
                keep.add(q)
    return chg[chg['path'].isin(keep)]


def serve_slice(
    df: pd.DataFrame,
    max_rows: int = 20_000,
    unchanged_top: int = 8,
    min_frac: float = 0.0,
    total: int = 0,
    max_depth: int | None = None,
) -> dict:
    """The recursive-compare response body (`rows`, `unchanged`, counts) from
    an index slice.

    Trimming, in order — each keeps the ancestor spines of what it keeps, and
    marks dirs whose descendants were dropped `pruned` (the client drills into
    one to get a fresh slice, where the same relative thresholds resolve much
    finer detail):

    - `max_depth`: levels below the compared uri.
    - `min_frac`: drop rows whose own bytes *and* |Δ| are under
      `min_frac × total` — a cell that small can't be drawn at any sane
      canvas size, and the index holds ~600K of them for a home dir.
    - `max_rows`: backstop, best-first by |Δ|.
    """
    for c in ('size_a', 'size_b', 'n_desc_a', 'n_desc_b'):
        df[c] = df[c].fillna(0).astype('int64')
    chg = df[~df['context']]
    ctx = df[df['context']]
    truncated = False
    if max_depth is not None:
        deeper = chg['depth'] > max_depth
        if deeper.any():
            truncated = True
            chg = chg[~deeper]
    if min_frac > 0 and total > 0:
        thresh = min_frac * total
        big = (chg[['size_a', 'size_b']].max(axis=1) >= thresh) | ((chg['size_b'] - chg['size_a']).abs() >= thresh)
        if not big.all():
            truncated = True
            keep = set(chg.loc[big, 'path'])
            chg = _with_ancestors(chg, keep)
        # Grey context cells face the same floor — an undrawable unchanged
        # sibling is just bytes on the wire. Their parent's filler absorbs
        # them (its gap grows), so the map still tiles truthfully.
        ctx = ctx[ctx['size_b'].fillna(0) >= thresh]
    if len(chg) > max_rows:
        truncated = True
        absd = (chg['size_b'] - chg['size_a']).abs()
        top = chg.loc[absd.sort_values(ascending=False).index[:max_rows]]
        keep = set(top['path'])
        kept = _with_ancestors(chg, keep)
        omitted_parents = set(chg.loc[~chg['path'].isin(set(kept['path'])), 'parent'])
    else:
        kept = chg
        omitted_parents = set()
    # Rows dropped by depth/size trimming: their parents are `pruned` too.
    dropped = df[~df['context'] & ~df['path'].isin(set(kept['path']))]
    omitted_parents |= set(dropped['parent'])

    kept_paths = set(kept['path'])
    child_parents = set(kept['parent'])
    is_dir = kept['kind'] == 'dir'
    expanded = is_dir & kept['path'].isin(child_parents)
    pruned = is_dir & kept['path'].isin(omitted_parents)

    rows = [
        {
            'path': r.path, 'depth': int(r.depth), 'kind': r.kind, 'status': r.status,
            'size_a': int(r.size_a), 'size_b': int(r.size_b),
            'n_desc_a': int(r.n_desc_a), 'n_desc_b': int(r.n_desc_b),
            'expanded': bool(e), 'pruned': bool(p),
        }
        for r, e, p in zip(kept.itertuples(index=False), expanded, pruned)
    ]
    rows.sort(key=lambda r: (-abs(r['size_b'] - r['size_a']), r['path']))

    # Unchanged context beside kept rows (parents present as rows or as the
    # compared root itself, `'.'`).
    ctx = ctx[ctx['parent'].isin(kept_paths | child_parents | {'.'})]
    top_rows: list[dict] = []
    rest: dict[str, dict] = {}
    if len(ctx):
        ctx = ctx.sort_values(['parent', 'size_b', 'path'], ascending=[True, False, True])
        rank = ctx.groupby('parent').cumcount()
        head = ctx[rank < unchanged_top]
        tail = ctx[rank >= unchanged_top]
        top_rows = [
            {
                'path': r.path, 'depth': int(r.depth), 'kind': r.kind, 'status': 'unchanged',
                'size_a': int(r.size_a), 'size_b': int(r.size_b),
                'n_desc_a': int(r.n_desc_a), 'n_desc_b': int(r.n_desc_b),
                'expanded': False, 'pruned': False,
            }
            for r in head.itertuples(index=False)
        ]
        if len(tail):
            agg = tail.groupby('parent').agg(count=('path', 'size'), size=('size_b', 'sum'), n_desc=('n_desc_b', 'sum'))
            rest = {
                ('' if p == '.' else p): {'count': int(c), 'size': int(s), 'n_desc': int(n)}
                for p, c, s, n in zip(agg.index, agg['count'], agg['size'], agg['n_desc'])
            }
    return {
        'rows': rows,
        'unchanged': {'top': top_rows, 'rest': rest},
        'expansions': int(expanded.sum()),
        'truncated': truncated,
    }


# ---------------------------------------------------------------------------
# Bookkeeping (`diff` table in the SQLite metadata DB)

DIFF_TABLE_SQL = '''
    CREATE TABLE IF NOT EXISTS diff (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_a INTEGER NOT NULL,
        scan_b INTEGER NOT NULL,
        time DATETIME NOT NULL,
        blob TEXT,
        status TEXT NOT NULL,
        n_rows INTEGER,
        n_added INTEGER,
        n_removed INTEGER,
        n_changed INTEGER,
        n_touched INTEGER,
        seconds REAL,
        error TEXT,
        UNIQUE (scan_a, scan_b)
    )
'''


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_config.SQLITE_PATH)
    con.row_factory = sqlite3.Row
    con.execute(DIFF_TABLE_SQL)
    return con


def get_index(scan_a: int, scan_b: int) -> dict | None:
    """The `diff` row for a pair (status ∈ building | done | failed), or None.
    A `done` row whose parquet is gone reads as None (rebuildable)."""
    with _connect() as con:
        row = con.execute('SELECT * FROM diff WHERE scan_a = ? AND scan_b = ?', (scan_a, scan_b)).fetchone()
    if row is None:
        return None
    d = dict(row)
    if d['status'] == 'done' and not (d['blob'] and exists(d['blob'])):
        return None
    return d


def build_and_record(scan_a: int, blob_a: str, scan_b: int, blob_b: str) -> dict:
    """Build the index for a pair, recording `building` → `done`/`failed` in
    the `diff` table. Returns the final row."""
    now = datetime.now().astimezone().isoformat()
    with _connect() as con:
        con.execute(
            'INSERT INTO diff (scan_a, scan_b, time, status) VALUES (?, ?, ?, ?) '
            'ON CONFLICT (scan_a, scan_b) DO UPDATE SET time = excluded.time, status = excluded.status, error = NULL',
            (scan_a, scan_b, now, 'building'),
        )
    t0 = _time.time()
    try:
        path, stats = build_diff_index(scan_a, blob_a, scan_b, blob_b)
    except Exception as e:
        with _connect() as con:
            con.execute('UPDATE diff SET status = ?, error = ?, seconds = ? WHERE scan_a = ? AND scan_b = ?',
                        ('failed', f'{type(e).__name__}: {e}', _time.time() - t0, scan_a, scan_b))
        raise
    with _connect() as con:
        con.execute(
            'UPDATE diff SET status = ?, blob = ?, n_rows = ?, n_added = ?, n_removed = ?, n_changed = ?, n_touched = ?, seconds = ?, error = NULL '
            'WHERE scan_a = ? AND scan_b = ?',
            ('done', path, stats.n_rows, stats.n_added, stats.n_removed, stats.n_changed, stats.n_touched, _time.time() - t0, scan_a, scan_b),
        )
    return get_index(scan_a, scan_b)


def previous_scan(con: sqlite3.Connection, scan_id: int) -> sqlite3.Row | None:
    """The most recent earlier scan of the same path whose blob is *reachable*
    (the pair `sync`/`index` build by default). Skipping unreachable blobs (a
    prior scan whose parquet is on an unmounted volume) keeps the post-scan diff
    step from crashing on a blob it can't read — see spec `r2-scan-target.md`."""
    from disk_tree.config import blob_reachable
    s = con.execute('SELECT * FROM scan WHERE id = ?', (scan_id,)).fetchone()
    if s is None:
        return None
    rows = con.execute(
        'SELECT * FROM scan WHERE path = ? AND time < ? ORDER BY time DESC',
        (s['path'], s['time']),
    ).fetchall()
    return next((r for r in rows if blob_reachable(r['blob'])), None)
