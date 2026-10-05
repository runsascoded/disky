"""O(depth) streaming aggregation over sorted listings (spec: specs/done/streaming-aggregation.md).

Third engine alongside pandas (:func:`disk_tree.find.index.aggregate`) and
DuckDB (:mod:`disk_tree.find.aggregate_duckdb`). Object-store listings are
lexicographically sorted by key, and sorted-by-path is DFS order: every dir's
subtree is a contiguous key interval. So aggregation is the classic ``du``
algorithm — a stack of open ancestor dirs with running accumulators, popped
(and emitted) as soon as the stream leaves their subtree. Working state is
O(max depth); the DuckDB cascade's level re-materialization, long-VARCHAR hash
group-bys, and RSS overshoot all disappear.

Output contract is byte-identical to the other engines: canonical layer-2
rows sorted by ``(depth, path)`` with dir-before-file on equal path. The
stream pass writes per-``(depth, kind)`` parts (already ~sorted — see
:class:`_PartWriters`); the finalize is a depth-ascending ordered merge of
those parts (spec: depth-partitioned-finalize.md) — no external sort.

Parallelism (spec: stream-partition-parallel.md): ``jobs > 1`` partitions the
sorted keyspace into contiguous ranges streamed by worker processes; the few
dirs whose subtree interval spans a partition boundary are exported as partial
accumulator segments and monoid-merged in the parent (:func:`_reduce_partials`).
Output is byte-identical for any ``jobs``.

The one wrinkle: `//` canonicalization (``a//b`` → ``a/b``, same policy as
the other engines) does not preserve lexicographic order — raw ``a//z``
sorts before ``a/b`` but canonical ``a/z`` sorts after, and a dirty key's
canonical position can fall either before or after its raw position. A
single-pass stack over raw order would pop dirs prematurely. Fix: a cheap
names-only pre-scan finds the shards containing dirty keys (rare — a handful
out of 92.7M in marin's production listings); those rows are collected,
canonicalized, sorted, and joined into the k-way merge as one more sorted
source, so the stack only ever sees globally canonical-sorted rows.
"""

from __future__ import annotations

import re
import sys
from bisect import bisect_left
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime
from glob import glob as _glob
from operator import itemgetter
from typing import Iterator

_SLASHES = re.compile(r'/+')


def _stage(msg: str) -> None:
    """Timestamped stderr progress line (same shape as aggregate_duckdb's) —
    long runs need a last-known-stage for post-mortems and phase timing."""
    print(f"[agg {datetime.now().isoformat(timespec='seconds')}] {msg}", file=sys.stderr, flush=True)

# Safety valve: dirty keys are collected in RAM. If a listing is mostly dirty
# keys something is pathological — the pre-scan diversion isn't the right tool.
_DIRTY_MAX = 10_000_000

# Rows per row group in the *final* layer-2 parquet. Contractual: the finalize
# slices at exactly this, which is what makes output bytes independent of `jobs`.
_FLUSH_ROWS = 1 << 18

# Rows buffered per open part writer before flushing a row group. Deliberately
# smaller than `_FLUSH_ROWS`: a worker keeps one writer open per (depth, kind)
# — ~44 on a 22-deep tree — and each buffer holds Python tuples (measured
# 484 B/row vs 292 B/row for the equivalent Arrow), so at `_FLUSH_ROWS` that is
# ~121 MiB per writer, ~5 GB per worker at worst, plus a ~100 MiB transient per
# flush. Part row-group size does not reach the published file (the finalize
# re-batches at `_FLUSH_ROWS`), so this is free to tune for memory; smaller
# groups also shrink the finalize's per-part read buffers.
_PART_FLUSH_ROWS = 1 << 15

# Rows decoded per listing-shard read buffer. The k-way merge holds one live
# batch per open source and ~1K stay open on a real bucket, so this multiplies
# by ~1000 in a worker's RSS — pyarrow's 65536 default cost 13.6GB and an
# OOM kill. Rows leave `_shard_rows` as Python tuples regardless, so shrinking
# this trades no throughput.
_SHARD_BATCH_ROWS = 1 << 13

_COLS = ['path', 'size', 'mtime', 'kind', 'parent', 'uri', 'n_desc', 'n_files', 'n_children', 'depth']


def _canonicalize(name: str) -> str:
    """`a//b` → `a/b`; strip trailing slashes. Same policy as import_listing."""
    return _SLASHES.sub('/', name).rstrip('/')


def _parent_of(p: str) -> str:
    """Everything before the last slash; '' if none. Matches the DuckDB
    engine's `_PARENT_EXPR` (incl. leading-slash keys: parent('/a') = '')."""
    i = p.rfind('/')
    return p[:i] if i > 0 else ('' if i < 0 else '')


def _expand_shards(listing_glob: str) -> list[str]:
    files = sorted(_glob(listing_glob))
    if not files:
        # A non-glob path that exists gets picked up by glob() too, so an
        # empty result means the input genuinely matches nothing.
        raise ValueError(f"no files match listing glob {listing_glob!r}")
    return files


def _check_schema(shard: str, required: frozenset[str]) -> bool:
    """True if the shard carries all `required` columns."""
    import pyarrow.parquet as pq
    names = set(pq.ParquetFile(shard).schema_arrow.names)
    return required <= names


def _epoch_seconds(arr):
    """Timestamp array → int64 epoch seconds, nulls → 0. Sub-second values
    TRUNCATE (floor) — the canonical epoch-seconds semantic; the duckdb and
    pandas engines use `floor(epoch(created))::BIGINT` to match (bare
    `::BIGINT` rounds, which skewed ~50% of real sub-second timestamps +1s
    — caught by the CW 92.7M cross-engine hashsum)."""
    import pyarrow as pa
    import pyarrow.compute as pc
    t = arr.type
    if pa.types.is_timestamp(t):
        per_s = {'s': 1, 'ms': 1_000, 'us': 1_000_000, 'ns': 1_000_000_000}[t.unit]
        ints = pc.fill_null(arr.cast(pa.int64()), 0)
        return pc.divide(ints, per_s) if per_s > 1 else ints
    return pc.fill_null(arr.cast(pa.int64()), 0)


def _dirty_mask(names):
    """Boolean mask of keys whose canonical form differs from the raw key."""
    import pyarrow.compute as pc
    return pc.or_(pc.match_substring(names, '//'), pc.ends_with(names, pattern='/'))


_MAX_RUNS = 100_000

# Pass-1 read-batch size — one checkpoint per batch, so this sets partition
# balance (± one batch) and the intra-run seek granularity. Env-overridable so
# tests can force multi-batch shards on tiny fixtures even in spawned pass-1
# workers (module constants don't survive spawn; the environment does).
import os as _os
_SCAN_BATCH_ROWS = int(_os.environ.get('DISK_TREE_SCAN_BATCH_ROWS', str(1 << 16)))


def _merge_runs(run_srcs: list[tuple[str, Iterator[tuple]]], hw: dict):
    """K-way merge of sorted sources, opening each only when the merge horizon
    reaches its first key (known from pass-1) and dropping it at exhaustion.

    `run_srcs` is [(first_key, row_iter)] sorted by first_key. Sources are
    generators that open their parquet reader on first pull — so concurrently
    open files / read buffers stay O(max range overlap), not O(total runs).
    `heapq.merge` by contrast primes every source up front: a fleet-scale
    bin-packed listing (thousands of runs) blows EMFILE and holds a row-group
    buffer per run (observed: 6 parallel imports all dead at `Too many open
    files`, ~20GB RSS each within minutes).

    First keys may be *lower bounds* (range-clipped sources): a source is then
    just opened earlier than strictly needed — correctness only requires
    claimed-first ≤ actual-first.

    `hw['max_open']` records the concurrently-open high-water mark.
    """
    from heapq import heappush, heappop
    h: list[tuple[str, int, tuple, Iterator[tuple]]] = []
    seq = 0
    i = 0
    n = len(run_srcs)
    n_open = 0
    while i < n or h:
        # Open every pending source whose range may contain the next key.
        while i < n and (not h or run_srcs[i][0] <= h[0][0]):
            src = run_srcs[i][1]
            i += 1
            row = next(src, None)
            if row is not None:
                heappush(h, (row[0], seq, row, src))
                seq += 1
                n_open += 1
                if n_open > hw['max_open']:
                    hw['max_open'] = n_open
        _, _, row, src = heappop(h)
        yield row
        nxt = next(src, None)
        if nxt is None:
            n_open -= 1
        else:
            heappush(h, (nxt[0], seq, nxt, src))
            seq += 1


def _scan_shard(
    shard: str,
    bucket: str,
    pivot_sums: tuple[str, ...] = (),
) -> tuple[int, bool, list[set], tuple[list[int], list[str], list[str]] | None, list[tuple[int, str, int]]]:
    """Pass-1 scan of one shard → (bucket row count, has-dirty-keys, distinct
    non-null value sets per pivot column, run entry (start ordinals, first
    keys, last keys) or None, checkpoints).

    Runs: `bulk-list` bin-packs multiple non-contiguous key ranges into each
    shard (weight balancing), so a shard is *piecewise* sorted — a
    concatenation of internally-sorted runs. Each run becomes its own merge
    source downstream. Ordinals are *raw* (unfiltered) row indices — that's
    what lets `_shard_rows` map them onto row-group boundaries and skip
    non-intersecting row groups entirely. First/last keys let the merge
    detect globally disjoint runs (the bulk-list common case: ranges split
    from one sorted keyspace) and degrade the k-way heap to concatenation.

    Checkpoints are (raw_ordinal, first_key, n_bucket_rows) per read batch —
    one already-needed `.as_py()` each. They double as the partition-boundary
    sample and the intra-run seek index (:func:`_choose_boundaries` /
    :func:`_clip_sources`)."""
    import numpy as np
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    n_rows = 0
    dirty = False
    distinct: list[set] = [set() for _ in pivot_sums]
    pf = pq.ParquetFile(shard)
    raw0 = 0
    prev_last: str | None = None
    starts: list[int] = []
    firsts: list[str] = []
    lasts: list[str] = []
    ckpts: list[tuple[int, str, int]] = []
    for batch in pf.iter_batches(batch_size=_SCAN_BATCH_ROWS, columns=['bucket', 'name', *pivot_sums]):
        nb = batch.num_rows
        mask = pc.fill_null(pc.equal(batch.column('bucket'), bucket), False)
        names = batch.column('name').filter(mask)
        n = len(names)
        if n == 0:
            raw0 += nb
            continue
        n_rows += n
        # Raw (batch-relative) indices of this bucket's rows — run-start
        # ordinals must be raw so the reader can row-group-skip.
        raw_idx = np.nonzero(mask.to_numpy(zero_copy_only=False))[0]
        first_name = names[0].as_py()
        ckpts.append((raw0 + int(raw_idx[0]), first_name, n))
        if not starts:
            starts.append(raw0 + int(raw_idx[0]))
            firsts.append(first_name)
        elif prev_last is not None and first_name < prev_last:
            lasts.append(prev_last)
            starts.append(raw0 + int(raw_idx[0]))
            firsts.append(first_name)
        if n > 1:
            ge = pc.greater_equal(names.slice(1), names.slice(0, n - 1))
            desc = np.nonzero(~ge.to_numpy(zero_copy_only=False))[0]
            if len(desc):
                names_py = names.to_pylist()
                for idx in desc:
                    lasts.append(names_py[int(idx)])
                    starts.append(raw0 + int(raw_idx[int(idx) + 1]))
                    firsts.append(names_py[int(idx) + 1])
        prev_last = names[n - 1].as_py()
        raw0 += nb
        if pc.any(_dirty_mask(names)).as_py():
            dirty = True
        for i, col in enumerate(pivot_sums):
            # NULL is recorded too (as `None`): a pivot is only left implied
            # (== `size`) when its column holds one value and no NULLs.
            vals = pc.unique(batch.column(col).filter(mask))
            distinct[i].update(vals.to_pylist())
    entry = None
    if starts:
        lasts.append(prev_last)
        entry = (starts, firsts, lasts)
    return n_rows, dirty, distinct, entry, ckpts


def _scan_names(
    shards: list[str],
    bucket: str,
    pivot_sums: tuple[str, ...] = (),
    executor=None,
) -> tuple[int, set[str], list[list], dict[str, tuple[list[int], list[str], list[str]]], dict[str, list[tuple[int, str, int]]]]:
    """Pass 1: names scan across shards (in parallel when an executor is
    given). Returns (total rows for bucket, shards with dirty keys, sorted
    distinct values per pivot column — `None` last when the column has
    NULLs — per-shard runs, per-shard checkpoints)."""
    n_rows = 0
    dirty_shards: set[str] = set()
    distinct: list[set] = [set() for _ in pivot_sums]
    runs: dict[str, tuple[list[int], list[str], list[str]]] = {}
    ckpts: dict[str, list[tuple[int, str, int]]] = {}
    total_runs = 0
    if executor is not None:
        from itertools import repeat
        results = executor.map(_scan_shard, shards, repeat(bucket), repeat(pivot_sums))
    else:
        results = (_scan_shard(s, bucket, pivot_sums) for s in shards)
    for shard, (n, dirty, dist, entry, cks) in zip(shards, results):
        n_rows += n
        if dirty:
            dirty_shards.add(shard)
        for i, s in enumerate(dist):
            distinct[i].update(s)
        if entry:
            runs[shard] = entry
            total_runs += len(entry[0])
        if cks:
            ckpts[shard] = cks
        if total_runs > _MAX_RUNS:
            raise ValueError(
                f"more than {_MAX_RUNS:,} sorted runs across listing shards — "
                f"input is essentially unsorted; use `-e duckdb`"
            )
    return n_rows, dirty_shards, [[*sorted(v for v in s if v is not None), *([None] if None in s else [])] for s in distinct], runs, ckpts


def _collect_dirty(
    dirty_shards: list[str],
    bucket: str,
    pivot_sums: tuple[str, ...] = (),
) -> list[tuple]:
    """Pass 1b: re-read only the shards flagged dirty, extracting the dirty
    rows as (canonical_name, size, mtime, *pivot_values), sorted by canonical name."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    out: list[tuple] = []
    for shard in dirty_shards:
        pf = pq.ParquetFile(shard)
        cols = ['bucket', 'name', 'size_bytes', *pivot_sums]
        has_created = 'created' in pf.schema_arrow.names
        if has_created:
            cols.append('created')
        for batch in pf.iter_batches(columns=cols):
            mask = pc.and_(pc.equal(batch.column('bucket'), bucket), _dirty_mask(batch.column('name')))
            if not pc.any(mask).as_py():
                continue
            names = batch.column('name').filter(mask).to_pylist()
            sizes = pc.fill_null(batch.column('size_bytes'), 0).filter(mask).to_pylist()
            if has_created:
                mtimes = _epoch_seconds(batch.column('created')).filter(mask).to_pylist()
            else:
                mtimes = [0] * len(names)
            pivots = [batch.column(c).filter(mask).to_pylist() for c in pivot_sums]
            out.extend(zip(map(_canonicalize, names), map(int, sizes), map(int, mtimes), *pivots))
            if len(out) > _DIRTY_MAX:
                raise ValueError(
                    f"more than {_DIRTY_MAX:,} `//`/trailing-slash keys in listing — "
                    f"pathological input for the stream engine; use `-e duckdb`"
                )
    out.sort(key=itemgetter(0))
    return out


def _shard_rows(
    shard: str,
    bucket: str,
    pivot_sums: tuple[str, ...] = (),
    start: int = 0,
    stop: int | None = None,
    lo: str | None = None,
    hi: str | None = None,
    batch_rows: int = _SHARD_BATCH_ROWS,
) -> Iterator[tuple]:
    """Clean rows of one sorted run of a shard as (name, size, mtime,
    *pivot_values). `[start, stop)` are *raw* row ordinals from
    `_scan_shard`'s run detection — raw so the read can skip every row group
    that doesn't intersect the run (bin-packed shards hold many runs; without
    the skip each run source re-reads the shard from row 0, ~R×/2 read
    amplification). The slice is verified sorted as it goes (a violation
    means the two passes disagreed — a bug, not bad input).

    `[lo, hi)` optionally restricts to a key range (partitioned streaming):
    whole batches below `lo` skip, the read stops at the first batch starting
    ≥ `hi`, and edge batches are mask-filtered exactly.

    `batch_rows` bounds each source's decode buffer. The k-way merge holds one
    live batch per *open* source, and lazy-open still leaves ~1K sources open
    on a real bucket (central2 peaked at 969), so pyarrow's 65536-row default
    put ~10MB × ~1K = 13.6GB in one worker — OOM-killed on eu-west4 at
    `-j 14`. Rows are yielded as Python tuples either way, so this is purely a
    memory knob, not a throughput one."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(shard)
    cols = ['bucket', 'name', 'size_bytes', *pivot_sums]
    has_created = 'created' in pf.schema_arrow.names
    if has_created:
        cols.append('created')
    # Row-group skip: read only groups intersecting [start, stop).
    md = pf.metadata
    rgs: list[int] = []
    off = 0
    ord0 = 0
    for i in range(md.num_row_groups):
        n_rg = md.row_group(i).num_rows
        if off + n_rg > start and (stop is None or off < stop):
            if not rgs:
                ord0 = off
            rgs.append(i)
        off += n_rg
    if not rgs:
        return
    prev: str | None = None
    for batch in pf.iter_batches(batch_size=batch_rows, columns=cols, row_groups=rgs):
        nb = batch.num_rows
        lo_i = max(start - ord0, 0)
        hi_i = nb if stop is None else min(stop - ord0, nb)
        ord0 += nb
        if lo_i >= hi_i:
            if stop is not None and ord0 >= stop:
                break
            continue
        sl = batch.slice(lo_i, hi_i - lo_i)
        mask = pc.fill_null(pc.equal(sl.column('bucket'), bucket), False)
        names_sl = sl.column('name').filter(mask)
        m = len(names_sl)
        if m == 0:
            continue
        # Sortedness within the run: vectorized within the batch slice, plus
        # the batch boundary.
        first = names_sl[0].as_py()
        if (m > 1 and not pc.all(
            pc.greater_equal(names_sl.slice(1), names_sl.slice(0, m - 1))
        ).as_py()) or (prev is not None and first < prev):
            raise RuntimeError(
                f"run [{start}, {stop}) of shard {shard!r} not sorted — "
                f"run detection and row reader disagree (bug)"
            )
        prev = names_sl[m - 1].as_py()

        if lo is not None and prev < lo:
            continue  # whole batch below the partition range
        if hi is not None and first >= hi:
            break  # sorted run: everything from here on is ≥ hi
        keep = pc.invert(_dirty_mask(names_sl))
        if lo is not None:
            keep = pc.and_(keep, pc.greater_equal(names_sl, lo))
        if hi is not None:
            keep = pc.and_(keep, pc.less(names_sl, hi))
        names = names_sl.filter(keep).to_pylist()
        if not names:
            continue
        sizes = pc.fill_null(sl.column('size_bytes'), 0).filter(mask).filter(keep).to_pylist()
        if has_created:
            mtimes = _epoch_seconds(sl.column('created')).filter(mask).filter(keep).to_pylist()
        else:
            mtimes = [0] * len(names)
        pivots = [sl.column(c).filter(mask).filter(keep).to_pylist() for c in pivot_sums]
        yield from zip(names, map(int, sizes), map(int, mtimes), *pivots)


def _choose_boundaries(
    ckpts: dict[str, list[tuple[int, str, int]]],
    n_rows: int,
    jobs: int,
) -> list[str]:
    """Pick `jobs − 1` partition-boundary keys at even row quantiles from the
    pass-1 checkpoints (each weighted by its batch's bucket-row count —
    balance is ±one batch). Boundaries are real keys; ranges are `[lo, hi)`.
    Deduped, so tiny inputs yield fewer (possibly zero) boundaries."""
    items = sorted((key, n) for cks in ckpts.values() for _, key, n in cks)
    if jobs <= 1 or not items or n_rows == 0:
        return []
    bounds: list[str] = []
    cum = 0
    ti = 1
    for key, n in items:
        while ti < jobs and cum >= ti * n_rows / jobs:
            bounds.append(key)
            ti += 1
        cum += n
    out: list[str] = []
    for b in bounds:
        if not out or b > out[-1]:
            out.append(b)
    return out


def _clip_sources(
    run_infos: list[tuple[str, str, str, int, int | None]],
    ckpts: dict[str, list[tuple[int, str, int]]],
    lo: str | None,
    hi: str | None,
) -> list[tuple[str, str, str, int, int | None]]:
    """Restrict run sources to the key range `[lo, hi)`: drop runs wholly
    outside, and tighten each survivor's raw-ordinal window to the enclosing
    checkpoints (last ckpt with key < `lo`, first ckpt with key ≥ `hi`) — so
    a worker re-reads at most one batch per run beyond its range; the exact
    cut happens in `_shard_rows`. Returned first keys are clamped to `lo`
    (a valid lower bound for merge ordering)."""
    out: list[tuple[str, str, str, int, int | None]] = []
    for first, last, shard, start, stop in run_infos:
        if lo is not None and last < lo:
            continue
        if hi is not None and first >= hi:
            continue
        s, e = start, stop
        cks = ckpts.get(shard) or []
        ords = [c[0] for c in cks]
        i0 = bisect_left(ords, start)
        i1 = bisect_left(ords, stop) if stop is not None else len(cks)
        keys = [cks[i][1] for i in range(i0, i1)]
        if lo is not None:
            j = bisect_left(keys, lo)  # rows before ckpt j-1's ordinal are < lo
            if j > 0:
                s = max(s, cks[i0 + j - 1][0])
        if hi is not None:
            j = bisect_left(keys, hi)  # rows from ckpt j's ordinal are ≥ hi
            if j < len(keys):
                e = cks[i0 + j][0] if e is None else min(e, cks[i0 + j][0])
        if e is not None and e <= s:
            continue
        out.append((max(first, lo) if lo is not None else first, last, shard, s, e))
    out.sort(key=itemgetter(0))
    return out


class _Acc:
    """Running accumulators for one open dir on the stack."""
    __slots__ = ('path', 'pfx', 'size', 'mtime', 'n_desc', 'n_files', 'n_children', 'pivot', 'mt_wsum')

    def __init__(self, path: str, n_pivot: int):
        self.path = path
        self.pfx = f'{path}/'  # hoisted: subtree-membership startswith runs per input row
        self.size = 0
        self.mtime = 0
        self.n_desc = 1  # self — matches the other engines' dir-row seeding
        self.n_files = 0
        self.n_children = 0
        self.pivot = [0] * n_pivot
        self.mt_wsum = 0  # exact Σ mtime·size — Python bigint, no overflow


class _Writer:
    """Buffered parquet writer for pre-output rows (files + dirs, unsorted)."""

    def __init__(self, path: str, cols: list[str], mean_mtime: bool):
        import pyarrow as pa
        from .agg_ext import MTIME_MEAN
        fields = []
        for c in cols:
            if c in ('path', 'kind', 'parent', 'uri'):
                fields.append((c, pa.string()))
            elif c == MTIME_MEAN:
                fields.append((c, pa.float64()))
            else:
                fields.append((c, pa.int64()))
        self._cols = cols
        self._schema = pa.schema(fields)
        import pyarrow.parquet as pq
        self._writer = pq.ParquetWriter(path, self._schema)
        self._rows: list[tuple] = []
        self.n_rows = 0

    def write(self, row: tuple) -> None:
        self._rows.append(row)
        self.n_rows += 1
        if len(self._rows) >= _PART_FLUSH_ROWS:
            self._flush()

    def _flush(self) -> None:
        if not self._rows:
            return
        import pyarrow as pa
        cols = list(zip(*self._rows))
        self._writer.write_table(pa.table(dict(zip(self._cols, cols)), schema=self._schema))
        self._rows.clear()

    def close(self) -> None:
        self._flush()
        self._writer.close()


class _PartWriters:
    """Per-(depth, kind) family of buffered parquet part writers.

    The du-stack's emission is already *almost* the output contract: file rows
    at a fixed depth are a subsequence of the globally-sorted key stream (so
    sorted by path), and dir rows at a fixed depth pop in subtree-interval
    order — i.e. sorted by ``path + '/'``, which differs from plain ``path``
    order exactly when a dir name is a proper prefix of a same-depth sibling
    whose next char is < ``'/'`` (``store`` vs ``store-backup``: the sibling's
    subtree sorts *before* ``store/``, so it pops first, inverted). Rather than
    trust that analysis, each part *measures* its own sortedness (one string
    compare per row); the finalize sorts only the parts that actually need it
    and streams the rest — no global external sort anywhere.

    `suffix` disambiguates parallel workers' part files (``.w{idx:03d}``).
    """

    def __init__(self, parts_dir: str, cols: list[str], mean_mtime: bool, suffix: str = '', widx: int = 0):
        import os
        self._dir = parts_dir
        self._cols = cols
        self._mean_mtime = mean_mtime
        self._suffix = suffix
        self._widx = widx
        self._writers: dict[tuple[int, str], _Writer] = {}
        self._last: dict[tuple[int, str], str] = {}
        self._unsorted: set[tuple[int, str]] = set()
        self._join = os.path.join
        self.n_rows = 0

    def write(self, depth: int, kind: str, path: str, row: tuple) -> None:
        key = (depth, kind)
        w = self._writers.get(key)
        if w is None:
            w = self._writers[key] = _Writer(
                self._join(self._dir, f'{depth:04d}-{kind}{self._suffix}.parquet'), self._cols, self._mean_mtime,
            )
        if key not in self._unsorted:
            last = self._last.get(key)
            if last is not None and path < last:
                self._unsorted.add(key)
            self._last[key] = path
        w.write(row)
        self.n_rows += 1

    def close(self) -> list[dict]:
        """Close all writers; return part descriptors for the manifest."""
        parts = []
        for (depth, kind), w in sorted(self._writers.items()):
            w.close()
            parts.append({
                'depth': depth,
                'kind': kind,
                'file': f'{depth:04d}-{kind}{self._suffix}.parquet',
                'rows': w.n_rows,
                'sorted': (depth, kind) not in self._unsorted,
                # Keyspace-partition ordinal. Worker `w` only ever emits keys in
                # its own `[lo, hi)` (spanning dirs leave as partials instead),
                # so parts with the same depth are disjoint and ordered by `w`
                # — which is what lets the finalize split a depth across workers.
                'w': self._widx,
            })
        return parts


def _run_partition(
    widx: int,
    parts_dir: str,
    sources: list[tuple[str, str, str, int, int | None]],
    dirty: list[tuple],
    lo: str | None,
    hi: str | None,
    bucket: str,
    scan_root: str,
    pivot_sums: tuple[str, ...],
    pivot_maps: list[dict],
    pivot_names: list[str],
    mean_mtime: bool,
    suffix: str,
) -> dict:
    """Stream one keyspace partition ``[lo, hi)`` through the du-stack.

    Emits complete rows into per-(depth, kind) parts (suffixed per worker).
    Dirs whose subtree interval may span a partition boundary — detected at
    pop time: still on the stack at range EOF (right-spanning), or
    ``pfx < lo`` (conservatively left-spanning; false positives merge as
    single segments) — export *partial accumulator segments* instead, for the
    parent-process monoid reduce (:func:`_reduce_partials`). A spanning dir's
    ancestors are always spanning too (stack nesting), so all parent-child
    accounting among them happens in the reduce; worker-side, a spanning pop
    retracts the push-time ``n_children`` increment (the reduce adds exactly
    1 per spanning child) and rolls nothing up. Runs in a worker process for
    ``jobs > 1``; called inline (lo = hi = None) otherwise."""
    from .agg_ext import MTIME_MEAN, mean_of

    n_pivot = len(pivot_names)
    out_cols = [*_COLS, *pivot_names, *([MTIME_MEAN] if mean_mtime else [])]
    srcs = [
        _shard_rows(shard, bucket, pivot_sums, start=a, stop=b, lo=lo, hi=hi)
        for _, _, shard, a, b in sources
    ]
    hw = {'max_open': 0}
    # Disjoint check is conservative under clipping (first keys are lower
    # bounds, last keys upper bounds): may miss the concat fast path, never
    # wrongly takes it.
    disjoint = not dirty and all(sources[i][0] > sources[i - 1][1] for i in range(1, len(sources)))
    if disjoint:
        from itertools import chain
        merged = chain.from_iterable(srcs)
    else:
        run_srcs = [(info[0], src) for info, src in zip(sources, srcs)]
        if dirty:
            run_srcs.append((dirty[0][0], iter(dirty)))
            run_srcs.sort(key=itemgetter(0))
        merged = _merge_runs(run_srcs, hw)

    parts = _PartWriters(parts_dir, out_cols, mean_mtime, suffix=suffix, widx=widx)
    partials: list[tuple] = []
    n_files_total = 0
    stack: list[_Acc] = [_Acc('', n_pivot)]

    def dir_extras(acc: _Acc) -> tuple:
        return (
            *acc.pivot,
            *([mean_of(acc.mt_wsum, acc.size)] if mean_mtime else []),
        )

    def pop_emit(at_eof: bool = False) -> None:
        acc = stack.pop()
        parent_acc = stack[-1]
        if (at_eof and hi is not None) or (lo is not None and acc.pfx < lo):
            parent_acc.n_children -= 1
            partials.append((acc.path, acc.size, acc.mtime, acc.n_desc, acc.n_files, acc.n_children, acc.pivot, acc.mt_wsum))
            return
        parent_acc.size += acc.size
        parent_acc.mtime = max(parent_acc.mtime, acc.mtime)
        parent_acc.n_desc += acc.n_desc
        parent_acc.n_files += acc.n_files
        for i, v in enumerate(acc.pivot):
            parent_acc.pivot[i] += v
        parent_acc.mt_wsum += acc.mt_wsum
        raw_parent = _parent_of(acc.path)
        depth = acc.path.count('/') + 1
        parts.write(depth, 'dir', acc.path, (
            acc.path, acc.size, acc.mtime, 'dir',
            raw_parent if raw_parent else '.',
            f'{scan_root}/{acc.path}',
            acc.n_desc, acc.n_files, acc.n_children,
            depth,
            *dir_extras(acc),
        ))

    # Single-pivot-column fast path (the common CLI shape, e.g. just
    # `-p storage_class_id`): skip the per-row zip over pivot_maps.
    pmap0 = pivot_maps[0] if len(pivot_maps) == 1 else None

    for name, size, mtime, *pvals in merged:
        parent = _parent_of(name)
        # Pop everything the new row's parent chain has left behind.
        top = stack[-1]
        while not (top.path == '' or parent == top.path or parent.startswith(top.pfx)):
            pop_emit()
            top = stack[-1]
        # Push the dirs between the surviving top and the row's parent.
        if top.path != parent:
            rel = parent if top.path == '' else parent[len(top.path) + 1:]
            base = top.path
            for comp in rel.split('/'):
                base = comp if base == '' else f'{base}/{comp}'
                top.n_children += 1  # new dir is a direct child of current top
                top = _Acc(base, n_pivot)
                stack.append(top)
        # Fold the file into its parent (subtree totals propagate on pop).
        top.size += size
        top.mtime = max(top.mtime, mtime)
        top.n_desc += 1
        top.n_files += 1
        top.n_children += 1
        if n_pivot:
            file_pivot = [0] * n_pivot
            if pmap0 is not None:
                v = pvals[0]
                if v is not None:
                    idx = pmap0[v]
                    top.pivot[idx] += size
                    file_pivot[idx] = size
            else:
                for pmap, v in zip(pivot_maps, pvals):
                    if v is not None:
                        idx = pmap[v]
                        top.pivot[idx] += size
                        file_pivot[idx] = size
        else:
            file_pivot = ()
        top.mt_wsum += mtime * size
        n_files_total += 1
        if n_files_total % 10_000_000 == 0:
            _stage(f"  [w{widx:02d}] …{n_files_total:,} files streamed")
        depth = name.count('/') + 1
        parts.write(depth, 'file', name, (
            name, size, mtime, 'file', parent, f'{scan_root}/{name}',
            1, 1, 0, depth,
            *file_pivot,
            *([float(mtime)] if mean_mtime else []),
        ))

    # EOF: close out the stack. With a right boundary every remaining dir is
    # spanning (its subtree may continue in the next partition); otherwise
    # normal pops (which still apply the left-spanning test).
    while len(stack) > 1:
        pop_emit(at_eof=True)
    root = stack.pop()
    partials.append(('', root.size, root.mtime, root.n_desc, root.n_files, root.n_children, root.pivot, root.mt_wsum))
    part_list = parts.close()
    return {
        'parts': part_list,
        'partials': partials,
        'rows': parts.n_rows,
        'files': n_files_total,
        'max_open': hw['max_open'],
    }


def _reduce_partials(all_partials: list[list[tuple]]) -> dict[str, list]:
    """Monoid-merge the workers' partial accumulator segments per path:
    Σ size / n_files / pivot / mt_wsum, max mtime, Σ n_children (spanning
    children were retracted worker-side), and n_desc = Σ − (k−1) so exactly
    one self-count survives k segments."""
    segs: dict[str, list] = {}
    counts: dict[str, int] = {}
    for plist in all_partials:
        for path, size, mtime, n_desc, n_files, n_children, pivot, wsum in plist:
            e = segs.get(path)
            if e is None:
                segs[path] = [size, mtime, n_desc, n_files, n_children, list(pivot), wsum]
                counts[path] = 1
            else:
                e[0] += size
                e[1] = max(e[1], mtime)
                e[2] += n_desc
                e[3] += n_files
                e[4] += n_children
                for i, v in enumerate(pivot):
                    e[5][i] += v
                e[6] += wsum
                counts[path] += 1
    for path, e in segs.items():
        e[2] -= counts[path] - 1
    return segs


def _write_boundary_parts(
    parts_dir: str,
    segs: dict[str, list],
    scan_root: str,
    pivot_names: list[str],
    mean_mtime: bool,
    ranges: list[tuple[str | None, str | None]] | None = None,
) -> tuple[list[dict], list]:
    """Roll the merged boundary-spanning dirs up their (also-spanning) parent
    chain, deepest first — the exact `pop_emit` rollup, run once in the parent
    — and write their rows as per-depth path-sorted parts
    (``{depth:04d}-dir.b.parquet``). The root row (depth 0) always lands here.
    Returns (part descriptors, merged root stats).

    With `ranges` (the workers' `[lo, hi)` keyspace partitions), each depth's
    rows are split across them into ``{depth:04d}-dir.b.w{idx:03d}.parquet``.
    These are the only rows not already range-scoped — they come from dirs whose
    subtree spanned a boundary — so splitting them is what makes *every* part
    attributable to one worker range, and hence lets the finalize parallelize
    within a depth rather than only across depths. They are few (single digits
    per depth on the marin fleet), so the split is cheap."""
    import os
    from .agg_ext import MTIME_MEAN, mean_of
    out_cols = [*_COLS, *pivot_names, *([MTIME_MEAN] if mean_mtime else [])]
    rows_by_depth: dict[int, list[tuple[str, tuple]]] = {}
    for path in sorted((p for p in segs if p != ''), key=lambda p: -p.count('/')):
        e = segs[path]
        pe = segs[_parent_of(path)]
        pe[0] += e[0]
        pe[1] = max(pe[1], e[1])
        pe[2] += e[2]
        pe[3] += e[3]
        pe[4] += 1
        for i, v in enumerate(e[5]):
            pe[5][i] += v
        pe[6] += e[6]
        raw_parent = _parent_of(path)
        depth = path.count('/') + 1
        rows_by_depth.setdefault(depth, []).append((path, (
            path, e[0], e[1], 'dir', raw_parent if raw_parent else '.', f'{scan_root}/{path}',
            e[2], e[3], e[4], depth,
            *e[5], *([mean_of(e[6], e[0])] if mean_mtime else []),
        )))
    r = segs['']
    rows_by_depth.setdefault(0, []).append(('.', (
        '.', r[0], r[1], 'dir', '', scan_root, r[2], r[3], r[4], 0,
        *r[5], *([mean_of(r[6], r[0])] if mean_mtime else []),
    )))
    def _widx_of(path: str) -> int:
        """Worker range owning `path`. Ranges are contiguous and ordered, so
        the first whose `hi` exceeds the key owns it (last range has hi=None)."""
        for i, (_, hi) in enumerate(ranges):
            if hi is None or path < hi:
                return i
        return len(ranges) - 1

    parts = []
    for depth in sorted(rows_by_depth):
        rows = sorted(rows_by_depth[depth])
        if not ranges:
            fname = f'{depth:04d}-dir.b.parquet'
            w = _Writer(os.path.join(parts_dir, fname), out_cols, mean_mtime)
            for _, row in rows:
                w.write(row)
            w.close()
            parts.append({
                'depth': depth, 'kind': 'dir', 'file': fname,
                'rows': len(rows), 'sorted': True, 'w': 0,
            })
            continue
        by_w: dict[int, list[tuple]] = {}
        for key, row in rows:
            by_w.setdefault(_widx_of(key), []).append(row)
        for widx in sorted(by_w):
            fname = f'{depth:04d}-dir.b.w{widx:03d}.parquet'
            w = _Writer(os.path.join(parts_dir, fname), out_cols, mean_mtime)
            for row in by_w[widx]:
                w.write(row)
            w.close()
            parts.append({
                'depth': depth, 'kind': 'dir', 'file': fname,
                'rows': len(by_w[widx]), 'sorted': True, 'w': widx,
            })
    return parts, r


# An unsorted part with more runs than this falls back to the in-memory sort
# (pathological — inversions come from prefix-sibling dirs, normally sparse).
_MAX_PART_RUNS = 8192

# Run-merge a part in place only up to this many runs; beyond it, re-chunk the
# part to merge-budget-sized row groups first. Each run reader buffers one
# decoded row group, so at the part writer's `_PART_FLUSH_ROWS`-sized groups
# that is ~6MB × runs — GBs at thousands of runs (at the `_FLUSH_ROWS` groups
# parts used to carry, it OOM-livelocked a 61GB node on the first real-data
# finalize). Re-chunking is one sequential read+write at O(batch) memory and
# caps total reader buffers at roughly the priming budget.
_RECHUNK_RUNS = 16

# Total rows the run-merge may hold primed across all run readers of one part.
# `_finalize_parts` scales this down by `jobs` so concurrent depth workers
# share the same overall budget.
_PRIME_ROWS = 1 << 22

# Below this many rows the finalize stays serial even at `jobs > 1`: the
# depth workers' spawn + temp round-trip costs more than the depth
# parallelism saves. Measured on a 10M-row listing (mixed-radix tree): serial
# 3s vs parallel 7s at both -j 4 and -j 8, while the stream phase kept its
# speedup. The parallel path is a huge win at 300M rows (mgu's serial
# finalize ran >1h53m without completing) — this only skips it where it
# loses. Output bytes are identical either way, so the switch is invisible.
_PARALLEL_FINALIZE_MIN_ROWS = int(_os.environ.get('DISK_TREE_PARALLEL_FINALIZE_MIN_ROWS', str(20_000_000)))


def _detect_runs(path: str, key: str = 'path') -> list[int]:
    """Sorted-run start ordinals of a part parquet's `key` column (cheap
    names-only scan, same vectorized boundary check as pass-1)."""
    import numpy as np
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    starts: list[int] = []
    prev_last: str | None = None
    ord0 = 0
    for batch in pq.ParquetFile(path).iter_batches(columns=[key]):
        col = batch.column(key)
        n = len(col)
        if n == 0:
            continue
        if not starts:
            starts.append(0)
        elif prev_last is not None and col[0].as_py() < prev_last:
            starts.append(ord0)
        if n > 1:
            ge = pc.greater_equal(col.slice(1), col.slice(0, n - 1))
            for idx in np.nonzero(~ge.to_numpy(zero_copy_only=False))[0]:
                starts.append(ord0 + int(idx) + 1)
        prev_last = col[n - 1].as_py()
        ord0 += n
    return starts


def _rows_range(path: str, start: int, stop: int | None, batch_rows: int, metadata=None):
    """Record batches of rows [start, stop) of a parquet file, skipping
    non-intersecting row groups. Pass a shared `metadata` (FileMetaData) when
    opening many readers on the same file — re-parsing a many-row-group footer
    once per run reader is its own CPU/memory multiplier."""
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(path, metadata=metadata)
    md = pf.metadata
    rgs: list[int] = []
    off = 0
    ord0 = 0
    for i in range(md.num_row_groups):
        n_rg = md.row_group(i).num_rows
        if off + n_rg > start and (stop is None or off < stop):
            if not rgs:
                ord0 = off
            rgs.append(i)
        off += n_rg
    if not rgs:
        return
    for batch in pf.iter_batches(batch_size=batch_rows, row_groups=rgs):
        nb = batch.num_rows
        lo = max(start - ord0, 0)
        hi = nb if stop is None else min(stop - ord0, nb)
        ord0 += nb
        if lo >= hi:
            if stop is not None and ord0 >= stop:
                return
            continue
        yield batch.slice(lo, hi - lo)


def _part_batches(path: str, part_sorted: bool, batch_rows: int, prime_rows: int = _PRIME_ROWS):
    """Record batches of one part, in path order.

    A part that measured unsorted at write time (prefix-sibling dir
    inversions) is *nearly* sorted — each inversion starts a new run. Detect
    the runs and merge them as batch streams (pairwise tree of
    :func:`_merge_batches`): O(batch × runs) memory, no whole-part
    materialization. The in-memory sort this replaces died twice on
    eu-west4's 51M-row dir parts: pyarrow `take`/`sort_by` concatenate each
    chunked *input* column into one contiguous Array, and >2GiB `string`
    columns overflow 32-bit offsets (`ArrowInvalid: offset overflow`) no
    matter how the output is sliced. The sort survives only as the
    pathological-run-count fallback, with a large_string round-trip
    legalizing the internal concatenation.

    Beyond `_RECHUNK_RUNS` runs, the part is first re-chunked to
    `rb_rows`-sized row groups so each run reader's decode buffer matches the
    merge budget instead of the part writer's `_PART_FLUSH_ROWS` groups (see
    `_RECHUNK_RUNS`). Internal batch/row-group edges never reach the final
    output (`emit()` re-batches at exactly `_FLUSH_ROWS`), so none of this
    affects the published bytes.
    """
    import os
    import pyarrow as pa
    import pyarrow.parquet as pq
    if part_sorted:
        yield from pq.ParquetFile(path).iter_batches(batch_size=batch_rows)
        return
    starts = _detect_runs(path)
    k = len(starts)
    if k <= _MAX_PART_RUNS:
        # Merge tree primes one batch per run source — scale batch size down
        # so priming stays within `prime_rows` even at the run-count cap.
        rb_rows = max(1024, min(batch_rows, prime_rows // max(1, k)))
        rechunk = None
        if k > _RECHUNK_RUNS:
            rechunk = path + '.rechunk'
            pf = pq.ParquetFile(path)
            with pq.ParquetWriter(rechunk, pf.schema_arrow) as w:
                for rb in pf.iter_batches(batch_size=rb_rows):
                    w.write_batch(rb, row_group_size=rb_rows)
            path = rechunk
        try:
            md = pq.ParquetFile(path).metadata
            bounds: list[int | None] = [*starts[1:], None]
            streams = [
                _rows_range(path, s, e, rb_rows, metadata=md)
                for s, e in zip(starts, bounds)
            ]
            while len(streams) > 1:
                streams = [
                    _merge_batches(streams[i], streams[i + 1]) if i + 1 < len(streams) else streams[i]
                    for i in range(0, len(streams), 2)
                ]
            yield from streams[0]
        finally:
            if rechunk is not None:
                os.remove(rechunk)
        return
    tbl = pq.read_table(path)
    orig = tbl.schema
    big = pa.schema([
        (f.name, pa.large_string() if pa.types.is_string(f.type) else f.type)
        for f in orig
    ])
    for rb in tbl.cast(big).sort_by('path').to_batches(max_chunksize=batch_rows):
        yield rb.cast(orig)


def _merge_batches(sa, sb):
    """Ordered merge of two path-sorted record-batch streams (generator);
    ties → left stream first (callers put dirs on the left: dir-before-file).

    Vectorized boundary merge: whole batches pass through when their key
    ranges don't interleave; otherwise the earlier batch is split at the
    other's boundary key (``searchsorted``) — O(#batches) Python, O(rows) C.

    Each batch's key column is converted to numpy exactly once and then
    consumed through an integer cursor. Re-converting the *remaining tail*
    after every split (the obvious formulation) is quadratic in splits per
    batch, and splits are not rare: the dir↔file merge at one depth alternates
    once per directory — millions of times on a real bucket — which pinned the
    finalize at ~37K rows/s against the stream pass's ~830K.
    """
    import numpy as np

    def cursors(stream):
        """(batch, keys) pairs, skipping empties; one conversion per batch."""
        for rb in stream:
            if rb.num_rows:
                yield rb, rb.column('path').to_numpy(zero_copy_only=False)

    ca, cb = cursors(sa), cursors(sb)
    a = next(ca, None)
    b = next(cb, None)
    ai = bi = 0
    while a is not None and b is not None:
        arb, ak = a
        brb, bk = b
        a_first, a_last = ak[ai], ak[-1]
        b_first, b_last = bk[bi], bk[-1]
        if a_last <= b_first:
            # All left keys ≤ first right key; equal path → left row first, so
            # the whole left remainder goes before the right batch.
            yield arb.slice(ai) if ai else arb
            a = next(ca, None)
            ai = 0
        elif b_last < a_first:
            # Strictly before every left key (a tie would owe the left row first).
            yield brb.slice(bi) if bi else brb
            b = next(cb, None)
            bi = 0
        elif a_first <= b_first:
            # Overlap, left starts first: peel left keys ≤ b_first (ties are
            # left → included, keeping dir-before-file on equal path). The
            # searchsorted runs on a view of the unconsumed tail, and
            # a_first ≤ b_first guarantees it advances by ≥1 row.
            idx = ai + int(np.searchsorted(ak[ai:], b_first, side='right'))
            yield arb.slice(ai, idx - ai)
            ai = idx
            if ai >= len(ak):
                a = next(ca, None)
                ai = 0
        else:
            # Overlap, right starts first: peel right keys strictly < a_first.
            idx = bi + int(np.searchsorted(bk[bi:], a_first, side='left'))
            yield brb.slice(bi, idx - bi)
            bi = idx
            if bi >= len(bk):
                b = next(cb, None)
                bi = 0
    while a is not None:
        arb, _ = a
        yield arb.slice(ai) if ai else arb
        ai = 0
        a = next(ca, None)
    while b is not None:
        brb, _ = b
        yield brb.slice(bi) if bi else brb
        bi = 0
        b = next(cb, None)


def _coalesce(stream, min_rows: int):
    """Concatenate the merge's alternation-sized slices into ~`min_rows` batches.

    `_merge_batches` emits one slice per dir↔file alternation — millions of
    few-row batches per depth. Every one of them becomes a chunk downstream, so
    each `_FLUSH_ROWS` flush was building a table with ~10^5 chunks per column,
    walking them all in `combine_chunks`, then rebuilding them with
    `to_batches`. Profiling the eu-west4 finalize put 10/10 samples in that
    flush block and none in the merge: 46 min on one depth that should take ~2.
    Coalescing here (one copy per row) keeps consumers seeing normal batches.
    """
    import pyarrow as pa
    buf: list = []
    rows = 0
    for rb in stream:
        if rb.num_rows == 0:
            continue
        buf.append(rb)
        rows += rb.num_rows
        if rows >= min_rows:
            yield buf[0] if len(buf) == 1 else pa.concat_batches(buf)
            buf, rows = [], 0
    if buf:
        yield buf[0] if len(buf) == 1 else pa.concat_batches(buf)


def _depth_stream(parts_dir: str, kinds: dict[str, list[dict]], batch_rows: int, prime_rows: int = _PRIME_ROWS):
    """One depth's merged, path-ordered record-batch stream (dirs before files
    on equal path), in consumer-sized batches (see :func:`_coalesce`)."""
    import os

    def kind_stream(parts_list: list[dict]):
        its = [
            _part_batches(os.path.join(parts_dir, p['file']), p['sorted'], batch_rows, prime_rows)
            for p in sorted(parts_list, key=itemgetter('file'))
        ]
        s = its[0]
        for it in its[1:]:
            s = _merge_batches(s, it)
        return s

    if len(kinds) == 1:
        return kind_stream(next(iter(kinds.values())))
    return _coalesce(
        _merge_batches(kind_stream(kinds['dir']), kind_stream(kinds['file'])),
        batch_rows,
    )


def _finalize_depth_worker(
    parts_dir: str,
    kinds: dict[str, list[dict]],
    batch_rows: int,
    tmp_path: str,
    prime_rows: int = _PRIME_ROWS,
) -> str:
    """Parallel-finalize worker: merge one depth's parts → temp parquet.

    The parent streams the temp back through its own row-group batching, so
    worker batch edges never reach the final file (byte-identity for any
    `jobs`).

    Batches are coalesced into `_FLUSH_ROWS` row groups before writing. A
    parquet writer holds one ColumnChunkMetaData — including min/max
    statistics, i.e. two full paths per string column — per row group per
    column in memory until the footer is written at close. One row group per
    incoming batch is therefore not just inefficient encoding: the depth's
    dir↔file merge emits a slice per alternation (tens of millions on a real
    bucket), and the accumulated footer OOM'd a 61GB node in minutes. Row
    groups must scale with rows, never with merge slices.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq
    writer = None
    schema = None
    buf: list = []
    buf_rows = 0
    try:
        for rb in _depth_stream(parts_dir, kinds, batch_rows, prime_rows):
            if rb.num_rows == 0:
                continue
            if writer is None:
                schema = rb.schema
                writer = pq.ParquetWriter(tmp_path, schema)
            buf.append(rb)
            buf_rows += rb.num_rows
            while buf_rows >= _FLUSH_ROWS:
                t = pa.Table.from_batches(buf, schema)
                writer.write_table(t.slice(0, _FLUSH_ROWS).combine_chunks())
                rest = t.slice(_FLUSH_ROWS)
                buf = rest.to_batches()
                buf_rows = rest.num_rows
        if buf_rows:
            writer.write_table(pa.Table.from_batches(buf, schema).combine_chunks())
    finally:
        if writer is not None:
            writer.close()
    return tmp_path if writer is not None else ''


def _finalize_parts(
    parts_dir: str,
    manifest: dict,
    out_parquet: str,
    batch_rows: int = 1 << 16,
    jobs: int = 1,
) -> None:
    """Depth-partitioned, sort-free finalize: parts → canonical layer-2 parquet.

    For each depth ascending: that depth's dir parts (one per worker, plus the
    boundary part) chain through pairwise ordered merges — pass-through when
    ranges don't interleave — then merge against the files stream with the
    dir-before-file tiebreak. O(1) memory and zero spill except for parts that
    measured unsorted (see :func:`_part_batches`). Row groups are sliced at
    exactly `_FLUSH_ROWS`, so the output is byte-identical for any `jobs`.
    """
    import os
    import pyarrow as pa
    import pyarrow.parquet as pq
    from .agg_ext import MTIME_MEAN

    pivot_names = manifest['pivot_names']
    mean_mtime = manifest['mean_mtime']
    # Canonical column order (matches the pandas concat / duckdb tail: extras
    # between `parent` and `uri`) — the published layer-2 must be column-order
    # identical across engines so file-level diffs and positional set ops
    # (EXCEPT) work.
    # The parts carry every column of the v1 shape (`uri` included); the
    # output is v2 (spec `listing-slim.md`): `uri` is not selected, the
    # implied pivots are not selected, the format rides in the metadata.
    from disk_tree import listing_format as lf
    fmt = lf.slim(manifest['scan_root'], [
        'path', 'size', 'mtime', 'n_desc', 'n_files', 'n_children', 'kind', 'parent',
        *manifest['all_pivot_names'], *([MTIME_MEAN] if mean_mtime else []),
        'uri', 'depth',
    ], manifest['implied'])
    canonical_cols = lf.written_columns(fmt)
    assert set(pivot_names) == set(canonical_cols) & set(manifest['all_pivot_names'])
    fields = []
    for c in canonical_cols:
        if c in ('path', 'kind', 'parent', 'uri'):
            fields.append((c, pa.string()))
        elif c == MTIME_MEAN:
            fields.append((c, pa.float64()))
        else:
            fields.append((c, pa.int64()))
    schema = lf.with_kv(pa.schema(fields), fmt)

    by_depth: dict[int, dict[str, list[dict]]] = {}
    for part in manifest['parts']:
        by_depth.setdefault(part['depth'], {}).setdefault(part['kind'], []).append(part)

    # Finer fan-out unit: (depth, keyspace-partition). Splitting only by depth
    # left the finalize Amdahl-bound on the biggest depth — eu-west4 depth 9
    # holds 92.2M of 369M rows and took 38 min while every other depth finished
    # in ~2 and then idled.
    #
    # A worker only emits keys inside its own `[lo, hi)` (spanning dirs leave as
    # partials, and boundary rows are split across the same ranges at write
    # time), so units are disjoint *in subtree order*. They are NOT
    # concatenable, because dir rows sort by plain `path` while the ranges
    # partition `path + '/'`, and the two orders disagree exactly for
    # prefix-siblings: with a boundary between them, `store` lands in a later
    # unit than `store-backup` yet sorts before it. So the parent merges each
    # depth's unit outputs (cheap — `_merge_batches` passes whole batches
    # through wherever ranges don't interleave, which is everywhere except
    # those siblings). Parts predating the `w` tag (a resumed older parts dir)
    # fall back to whole-depth units.
    partitioned = all('w' in p for p in manifest['parts'])
    by_unit: dict[tuple[int, int], dict[str, list[dict]]] = {}
    for part in manifest['parts']:
        unit = (part['depth'], part['w'] if partitioned else 0)
        by_unit.setdefault(unit, {}).setdefault(part['kind'], []).append(part)

    writer = pq.ParquetWriter(out_parquet, schema, **lf.pyarrow_codec())
    buf: list = []
    buf_rows = 0

    def emit(rb) -> None:
        nonlocal buf, buf_rows
        if rb.num_rows == 0:
            return
        buf.append(rb.select(canonical_cols))
        buf_rows += rb.num_rows
        while buf_rows >= _FLUSH_ROWS:
            t = pa.Table.from_batches(buf, schema)
            # combine_chunks: page boundaries inside a column chunk depend on
            # the writer's chunk edges, so a multi-chunk write leaks upstream
            # batch sizes (which vary with `jobs`) into the encoded bytes.
            # One contiguous chunk per row group ⇒ layout is a pure function
            # of row content ⇒ byte-identical output for any `jobs`.
            writer.write_table(t.slice(0, _FLUSH_ROWS).combine_chunks())
            rest = t.slice(_FLUSH_ROWS)
            buf = rest.to_batches()
            buf_rows = rest.num_rows

    def flush() -> None:
        nonlocal buf, buf_rows
        if buf_rows:
            writer.write_table(pa.Table.from_batches(buf, schema).combine_chunks())
        buf = []
        buf_rows = 0

    total_rows = sum(p['rows'] for kinds in by_depth.values() for ps in kinds.values() for p in ps)
    parallel = jobs > 1 and len(by_unit) > 1 and total_rows >= _PARALLEL_FINALIZE_MIN_ROWS
    units = f"{len(by_depth)} depth(s) → {len(by_unit)} unit(s)"
    if parallel:
        _stage(f"finalize: {units} across {jobs} worker(s)")
    elif jobs > 1 and len(by_unit) > 1:
        _stage(
            f"finalize: {units}, serial "
            f"({total_rows:,} rows < {_PARALLEL_FINALIZE_MIN_ROWS:,} parallel threshold)"
        )
    else:
        _stage(f"finalize: {len(by_depth)} depth(s), serial")
    try:
        if parallel:
            # Units are independent — fan them out to worker processes (biggest
            # first to kill the straggler tail), each writing a temp; the parent
            # consumes temps in (depth, w) order through emit(), so row-group
            # edges (and bytes) match jobs=1.
            import multiprocessing as mp
            from concurrent.futures import ProcessPoolExecutor
            tmp_dir = os.path.join(parts_dir, 'finalize-tmp')
            os.makedirs(tmp_dir, exist_ok=True)
            weights = {
                u: sum(p['rows'] for ps in kinds.values() for p in ps)
                for u, kinds in by_unit.items()
            }
            # Unit workers share one priming budget: each unsorted-part
            # run-merge may hold `prime_rows` rows across its run readers, so
            # divide the global budget by the worker count.
            prime_rows = max(1 << 16, _PRIME_ROWS // jobs)
            with ProcessPoolExecutor(max_workers=jobs, mp_context=mp.get_context('spawn')) as ex:
                futs = {
                    u: ex.submit(
                        _finalize_depth_worker, parts_dir, by_unit[u], batch_rows,
                        os.path.join(tmp_dir, f'unit-{u[0]}-{u[1]}.parquet'), prime_rows,
                    )
                    for u in sorted(by_unit, key=lambda u: -weights[u])
                }
                for depth in sorted(by_depth):
                    ws = sorted(w for (d, w) in by_unit if d == depth)
                    temps = [t for t in (futs[(depth, w)].result() for w in ws) if t]
                    if not temps:
                        continue
                    streams = [
                        pq.ParquetFile(t).iter_batches(batch_size=batch_rows)
                        for t in temps
                    ]
                    s = streams[0]
                    for st in streams[1:]:
                        s = _merge_batches(s, st)
                    if len(streams) > 1:
                        s = _coalesce(s, batch_rows)
                    for rb in s:
                        emit(rb)
                    for t in temps:
                        os.remove(t)
            flush()
        else:
            for depth in sorted(by_depth):
                for rb in _depth_stream(parts_dir, by_depth[depth], batch_rows):
                    emit(rb)
            flush()
    finally:
        writer.close()


def _finalize_and_clean(parts_dir: str, manifest: dict, out_parquet: str, jobs: int = 1) -> dict:
    """Run the finalize; preserve the parts dir on failure (resume token),
    remove it on success; return the engine stats dict."""
    import shutil
    try:
        _finalize_parts(parts_dir, manifest, out_parquet, jobs=jobs)
    except BaseException:
        # A finalize-only failure must not cost the (expensive) stream pass:
        # the parts + manifest stay put, and a rerun with the same output path
        # resumes at the merge.
        _stage(f"FAILED — streamed parts preserved at {parts_dir}; rerun resumes at finalize")
        raise
    _stage("finalize done")
    shutil.rmtree(parts_dir, ignore_errors=True)
    return {
        'rows': manifest['rows'],
        'files': manifest['files'],
        'max_open_sources': manifest['max_open_sources'],
        'root_size': manifest['root_size'],
        'root_n_desc': manifest['root_n_desc'],
        'root_n_files': manifest['root_n_files'],
        'root_n_children': manifest['root_n_children'],
        'root_mtime': manifest['root_mtime'],
    }


def aggregate_stream(
    listings: tuple[str, ...],
    bucket: str,
    scheme: str,
    out_parquet: str,
    con: "object | None" = None,
    memory_limit: str | None = None,
    temp_dir: str | None = None,
    max_temp_size: str | None = None,
    pivot_sums: tuple[str, ...] = (),
    mean_mtime: bool = False,
    jobs: int = 1,
) -> dict:
    """Streaming rollup: sorted listing shards → canonical layer-2 parquet.

    Same signature-shape and stats dict as
    :func:`disk_tree.find.aggregate_duckdb.aggregate_listing_to_parquet`.
    `listings` are parquet globs in earlier-source-wins order (the first glob
    containing any rows for `bucket` is used). Requires the raw listing
    schema (``bucket, name, size_bytes[, created]``) with shards *piecewise*
    sorted by key (bulk-list bin-packs sorted ranges into shards; each run
    becomes a merge source) — for anything else, `-e duckdb`.
    `pivot_sums` / `mean_mtime` are the opt-in aggregation extensions — all
    monoid sums, so they stream identically (see :mod:`disk_tree.find.agg_ext`).

    `jobs` partitions the keyspace into that many contiguous ranges streamed
    by parallel worker processes (pass-1 shard scans and the depth finalize
    parallelize through the same pool); 0 = all cores; the default 1 stays
    fully in-process. Output is byte-identical for any value (spec:
    stream-partition-parallel.md).

    **`jobs > 1` requires an import-safe caller.** The pool uses the `spawn`
    start method, so each worker re-imports the caller's `__main__`: a driver
    script that calls this at module top level re-executes itself in every
    worker and dies. Guard the call with `if __name__ == '__main__':` (or call
    from an importable module). Heredoc / `python -c` / stdin scripts cannot
    satisfy this — use `jobs=1` there.

    `con` / `memory_limit` / `temp_dir` / `max_temp_size` are accepted for
    call-site compatibility but unused: the finalize is a depth-partitioned
    ordered merge (see :func:`_finalize_parts`), not a DuckDB external sort,
    so there is nothing to cap or spill.
    """
    import json
    import os
    import shutil
    from .agg_ext import check_pivot_values, pivot_col

    # The parts dir sits next to the output; its manifest doubles as the
    # resume token — a finalize-only failure leaves it in place, and a rerun
    # with the same output path skips straight to the merge.
    if jobs == 0:
        jobs = os.cpu_count() or 1
    jobs = max(1, jobs)

    parts_dir = f'{out_parquet}.parts'
    manifest_path = os.path.join(parts_dir, 'manifest.json')
    if os.path.exists(manifest_path):
        with open(manifest_path) as fh:
            manifest = json.load(fh)
        _stage(f"resuming from streamed parts at {parts_dir} (stream pass skipped)")
        if 'scan_root' not in manifest:
            # Parts streamed before the v2 listing format (spec
            # `listing-slim.md`) still finalize as v2: the root is a function
            # of the call, and every pivot column the parts hold is written
            # (whether one is single-valued was decided by the stream pass
            # that produced them, which this manifest predates).
            from disk_tree.backends.url import canonical
            manifest = {
                **manifest,
                'scan_root': canonical(f'{scheme}://{bucket}'),
                'all_pivot_names': manifest['pivot_names'],
                'implied': {},
            }
            _stage("parts predate the v2 listing format: finalizing as v2 with every pivot column written")
        return _finalize_and_clean(parts_dir, manifest, out_parquet, jobs=jobs)
    ex = None
    if jobs > 1:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor
        ex = ProcessPoolExecutor(max_workers=jobs, mp_context=mp.get_context('spawn'))

    try:
        required = frozenset({'bucket', 'name', 'size_bytes', *pivot_sums})

        # ---- resolve which listing source serves this bucket (earlier wins) ----
        chosen: list[str] | None = None
        dirty_shards: set[str] = set()
        distinct: list[list] = []
        runs: dict[str, tuple[list[int], list[str], list[str]]] = {}
        ckpts: dict[str, list[tuple[int, str, int]]] = {}
        for listing_glob in listings:
            shards = _expand_shards(listing_glob)
            bad = [s for s in shards if not _check_schema(s, required)]
            if bad:
                raise ValueError(
                    f"shard {bad[0]!r} lacks required columns {sorted(required)} — "
                    f"the stream engine only reads raw/bulk-list listings; use `-e duckdb`"
                )
            _stage(f"pass-1 names scan: {len(shards)} shard(s) in {listing_glob!r}")
            n_rows, dirty_shards, distinct, runs, ckpts = _scan_names(shards, bucket, pivot_sums, executor=ex)
            if n_rows > 0:
                chosen = shards
                break
        if chosen is None:
            raise ValueError(f"no rows for bucket {bucket!r}")
        _stage(
            f"pass-1 done: {n_rows:,} rows, {sum(len(starts) for starts, _, _ in runs.values()):,} run(s) "
            f"across {len(runs)} shard(s), {len(dirty_shards)} dirty shard(s)"
        )

        # A pivot holding one value and no NULLs equals `size` on every row:
        # it is left implied (spec `listing-slim.md`) — named in the output's
        # metadata, never computed. The rest pivot as before.
        all_pivot_names: list[str] = []
        implied: dict[str, str] = {}
        kept: list[tuple[str, list]] = []
        for col, seen in zip(pivot_sums, distinct):
            vals = check_pivot_values(col, [v for v in seen if v is not None])
            if len(vals) == 1 and len(seen) == 1:
                all_pivot_names.append(pivot_col(col, vals[0]))
                implied[pivot_col(col, vals[0])] = 'size'
            else:
                all_pivot_names.extend(pivot_col(col, v) for v in vals)
                kept.append((col, vals))
        pivot_sums = tuple(col for col, _ in kept)
        distinct = [vals for _, vals in kept]

        dirty = _collect_dirty(sorted(dirty_shards), bucket, pivot_sums) if dirty_shards else []
        if dirty_shards:
            _stage(f"collected {len(dirty):,} dirty row(s)")

        # Pivot layout: value → index into each _Acc's flat `pivot` vector; column
        # names in (CLI col order) × (sorted value order), matching the other engines.
        pivot_names: list[str] = []
        pivot_maps: list[dict] = []
        for col, vals in zip(pivot_sums, distinct):
            pivot_maps.append({v: len(pivot_names) + i for i, v in enumerate(vals)})
            pivot_names.extend(pivot_col(col, v) for v in vals)

        from disk_tree.backends.url import canonical
        scan_root = canonical(f'{scheme}://{bucket}')  # `file` roots → the bare path
        # One merge source per sorted run (bin-packed shards are piecewise sorted).
        # When a partition's runs are globally disjoint, ordered by first key its
        # merge degrades to concatenation — O(1)/row instead of O(log n_runs)
        # string compares in the heap. Caveat: consecutive in-order ranges within
        # a shard coalesce into one detected run with key *gaps* inside, and other
        # shards' ranges land in those gaps — so multi-shard bulk-list listings
        # usually take the heap fallback; the chain engages for single-run inputs
        # (one shard, or pre-merged listings). Any overlap or a dirty side-stream
        # falls back.
        run_infos: list[tuple[str, str, str, int, int | None]] = []
        for s in chosen:
            entry = runs.get(s)
            if not entry:
                continue
            starts, firsts, lasts = entry
            bounds: list[int | None] = [*starts[1:], None]
            for start, stop, first, last in zip(starts, bounds, firsts, lasts):
                run_infos.append((first, last, s, start, stop))
        run_infos.sort()

        boundaries = _choose_boundaries(ckpts, n_rows, jobs) if jobs > 1 else []
        n_parts_w = len(boundaries) + 1
        ranges = [
            (boundaries[i - 1] if i > 0 else None, boundaries[i] if i < len(boundaries) else None)
            for i in range(n_parts_w)
        ]
        dirty_keys = [d[0] for d in dirty]

        if os.path.isdir(parts_dir):
            # Manifest-less leftovers from a mid-stream crash — unusable.
            shutil.rmtree(parts_dir)
        os.makedirs(parts_dir)

        _stage(f"merge+stream: {len(run_infos)} run source(s), {n_parts_w} partition(s)")
        try:
            common = dict(
                parts_dir=parts_dir, bucket=bucket, scan_root=scan_root,
                pivot_sums=pivot_sums, pivot_maps=pivot_maps, pivot_names=pivot_names,
                mean_mtime=mean_mtime,
            )
            if n_parts_w == 1:
                results = [_run_partition(0, sources=run_infos, dirty=dirty, lo=None, hi=None, suffix='', **common)]
            else:
                futs = []
                for i, (lo, hi) in enumerate(ranges):
                    srcs_i = _clip_sources(run_infos, ckpts, lo, hi)
                    d0 = bisect_left(dirty_keys, lo) if lo is not None else 0
                    d1 = bisect_left(dirty_keys, hi) if hi is not None else len(dirty)
                    futs.append(ex.submit(
                        _run_partition, i, sources=srcs_i, dirty=dirty[d0:d1],
                        lo=lo, hi=hi, suffix=f'.w{i:03d}', **common,
                    ))
                results = [f.result() for f in futs]

            segs = _reduce_partials([r['partials'] for r in results])
            b_parts, root = _write_boundary_parts(
                parts_dir, segs, scan_root, pivot_names, mean_mtime,
                ranges=ranges if n_parts_w > 1 else None,
            )
        except BaseException:
            # Mid-stream failure: partial parts (no manifest) are unusable.
            shutil.rmtree(parts_dir, ignore_errors=True)
            raise

        part_list = sorted(
            [p for r in results for p in r['parts']] + b_parts,
            key=lambda p: (p['depth'], p['kind'], p['file']),
        )
        n_files_total = sum(r['files'] for r in results)
        n_rows_total = sum(r['rows'] for r in results) + sum(p['rows'] for p in b_parts)
        max_open = max(r['max_open'] for r in results)
        manifest = {
            'pivot_names': pivot_names,
            # v2 output (spec `listing-slim.md`); a resumed manifest without
            # these keys is filled in on resume (see `aggregate_stream`).
            'scan_root': scan_root,
            'all_pivot_names': all_pivot_names,
            'implied': implied,
            'mean_mtime': mean_mtime,
            'rows': n_rows_total,
            'files': n_files_total,
            'max_open_sources': max_open,
            'root_size': root[0],
            'root_n_desc': root[2],
            'root_n_files': root[3],
            'root_n_children': root[4],
            'root_mtime': root[1],
            'parts': part_list,
        }
        with open(manifest_path, 'w') as fh:
            json.dump(manifest, fh)
        n_unsorted = sum(1 for p in part_list if not p['sorted'])
        _stage(
            f"streamed {n_rows_total:,} rows ({n_files_total:,} files, "
            f"max {max_open} source(s) open) into {len(part_list)} part(s)"
            f" ({n_unsorted} unsorted); finalize"
        )
    except BrokenProcessPool as e:
        # Spawn workers re-import the caller's `__main__`; a driver that calls
        # us at module top level re-executes itself in every worker and dies
        # before running a task. The raw error names neither cause nor cure.
        raise RuntimeError(
            f"worker pool died immediately (jobs={jobs}). `jobs > 1` uses the spawn start "
            f"method, so every worker re-imports the calling module: guard the call with "
            f"`if __name__ == '__main__':`, or call it from an importable module. Heredoc / "
            f"`python -c` / stdin scripts cannot satisfy that — use jobs=1 there. ({e})"
        ) from e
    finally:
        if ex is not None:
            ex.shutdown()
    return _finalize_and_clean(parts_dir, manifest, out_parquet, jobs=jobs)
