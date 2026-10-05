"""Adaptive range-splitting bulk listing (spec: specs/done/adaptive-listing.md).

The pre-planned sharding in :mod:`disk_tree.find.bulk` needs either prior
weights (``-W``) or directory-ish fanout at depth ≤ 2 — a first scan of a
bucket whose keys bury their fanout deep (marin's ``datakit/`` holds 78% of
92.7M keys) degenerates to one serial pagination chain: ~1000 keys/page ×
1 RTT/page, hours of wall clock regardless of worker count.

Here workers own key **ranges** ``[start, end)`` instead of prefixes, and the
splitting is *self-donation* rather than coordinated stealing: every worker
that is ≥2 pages into its range and sees idle peers (a shared counter)
bisects its own **remaining** range ``(last_key_seen, end)`` at a
lexicographically interpolated midpoint and enqueues the upper half. Ramp-up
from a single ``["", ∞)`` seed doubles the busy-worker count roughly every
page — ~log2(W) pages to saturate — and hot keyspace regions keep splitting
for as long as anyone is idle, with no prior run, no weights, and no reliance
on directory structure.

No gaps, no dups: a donation happens only after a page ending at
``last_key_seen`` was emitted, and ``last < mid < end`` is guaranteed by
:func:`key_midpoint` — the donor keeps ``(last, mid)`` (it continues its
pagination chain and stops at the shrunken end), the recipient starts at
``mid`` (inclusive: GCS ``start_offset`` natively; S3 via a HEAD-compensation
for the exclusive ``StartAfter``).

Output is the same shard-parquet + ``_SUCCESS.json`` contract as the planned
path, with one addition: the marker records every finished range's
``[start, end, rows]`` — the warm start for the next run (``--warm-from``),
which seeds the queue with converged boundaries instead of re-discovering
them. Shards interleave ranges, so they are *piecewise* sorted — exactly the
shape ``import -e stream``'s run-splitting merge consumes.

Interpolated midpoints operate on Unicode code points (skipping surrogates):
Python ``str`` comparison is code-point order, which equals UTF-8 byte order
— the order GCS and S3 list keys in — so ``a < m < b`` locally is
``a < m < b`` server-side too.
"""

from __future__ import annotations

import json
import sys
import threading
from functools import partial
from queue import Empty, Queue
from typing import TYPE_CHECKING, Iterable, Optional, Protocol

if TYPE_CHECKING:
    import pandas as pd  # noqa: F401

from disk_tree.find.bulk import (
    BATCH_ROWS,
    ROWS_PER_SHARD,
    SUCCESS_MARKER,
    utc_now,
    BlobRow,
    entries_to_frame,
    _write_shard,
    resolve_existing,
)

err = partial(print, file=sys.stderr)

_MAXCP = 0x10FFFF
# A worker must be at least this many pages into its range before donating —
# a range this small is cheaper to finish than to split.
_MIN_PAGES_BEFORE_SPLIT = 2
# Blocked-get poll interval; also bounds termination-detection latency.
_IDLE_POLL_S = 0.25


class PagedLister(Protocol):
    """Page-granular streaming — the primitive adaptive listing needs.

    (:class:`~disk_tree.find.bulk.BulkLister`'s ``stream_prefix`` hides
    pagination, but donation decisions happen *between* pages.)
    """

    scheme: str

    def stream_pages(
        self,
        bucket: str,
        prefix: Optional[str],
        start: Optional[str],
        end_hint: Optional[str],
    ) -> "Iterable[list[BlobRow]]":
        """Yield pages of objects with name ≥ ``start`` (inclusive), in name
        order. ``end_hint`` MAY be honored server-side (GCS ``end_offset``);
        the caller enforces the (possibly shrunken) end client-side either
        way. Pages continue until the keyspace (or hint) is exhausted; the
        caller closes the generator early when its range completes."""
        ...


# --- lexicographic midpoints --------------------------------------------------

def _mid_cp(lo: int, hi: int) -> Optional[int]:
    """Code point strictly between lo and hi (surrogates excluded), or None.
    ``lo`` may be -1 ("before every character"); ``hi`` may be _MAXCP+1."""
    if hi - lo < 2:
        return None
    mid = (lo + hi) // 2
    if 0xD800 <= mid <= 0xDFFF:
        mid = 0xD7FF if lo < 0xD7FF else 0xE000
        if not (lo < mid < hi):
            return None
    return mid


def key_midpoint(a: str, b: Optional[str]) -> Optional[str]:
    """A key ``m`` with ``a < m < b`` (``b=None`` = unbounded), or None.

    Bisects at the first position where the bounds diverge; when their code
    points are adjacent, descends into ``a``'s branch (where the upper bound
    becomes unbounded — any extension of ``a[:i+1]`` sorts below ``b``).
    Descent positions prefer the character class of ``a``'s char there
    (digits stay digits, lowercase stays lowercase): a generic bound puts the
    midpoint in the astral plane, past any real key, and the recipient of
    such a split gets an empty range. Any return value is *correct* — ranges
    partition the keyspace wherever the split lands; caps only affect
    balance. A None return just means "don't split here"."""
    i = 0
    while b is not None and i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    ca = ord(a[i]) if i < len(a) else -1
    cb = ord(b[i]) if (b is not None and i < len(b)) else _MAXCP + 1
    mid = _mid_cp(ca, cb)
    if mid is not None:
        return a[:i] + chr(mid)
    if ca == -1:
        # a == b[:i] and b continues with chr(0)-ish: nothing fits between.
        return None
    # Adjacent code points: descend into a's branch; upper bound is now open.
    j = i + 1
    while j <= len(a) + 32:
        cj = ord(a[j]) if j < len(a) else -1
        hi_j = _class_hi(cj) if cj >= 0 else None
        if hi_j is not None:
            mid = _mid_cp(cj, hi_j)
            if mid is not None:
                return a[:j] + chr(mid)
            j += 1  # at the class edge ('9'/'z'/'Z') — descend deeper
            continue
        mid = _mid_cp(cj, _MAXCP + 1)
        if mid is not None:
            return a[:j] + chr(mid)
        j += 1
    return None


def _class_hi(cp: int) -> Optional[int]:
    """Exclusive upper bound of ``cp``'s character class (digits / lowercase /
    uppercase), or None for anything else."""
    if 0x30 <= cp <= 0x39:
        return 0x3A
    if 0x61 <= cp <= 0x7A:
        return 0x7B
    if 0x41 <= cp <= 0x5A:
        return 0x5B
    return None


def open_split(first: str, last: str) -> Optional[str]:
    """Split point for an *unbounded* range ``[…, ∞)`` given the keys observed
    so far (``first`` = first key seen in the range, ``last`` = latest).

    First-divergence bisection against a generic upper bound fails here: with
    a hot shared prefix (``datakit/…`` = 78% of marin's CW keys), bisecting at
    position 0 lands past the entire real keyspace and the recipient gets an
    empty range. The observed keys say where variation actually happens —
    ``first`` and ``last`` diverge at position ``c``, and in sorted order the
    remaining keys diverge from ``last`` at position ≤ ``c`` — so bisect
    *there*, capped by the character class at that position (digits stay
    digits, lowercase stays lowercase). A skewed guess self-corrects: both
    halves re-split while anyone is idle."""
    c = 0
    n = min(len(first), len(last))
    while c < n and first[c] == last[c]:
        c += 1
    if c >= len(last):
        c = len(last) - 1  # degenerate (last is a prefix of first): bump the tail
    lo = ord(last[c])
    hi = _class_hi(lo)
    mid = _mid_cp(lo, hi) if hi is not None else None
    if mid is None:
        return None  # at the class edge — try again next page (last will move)
    return last[:c] + chr(mid)


def next_prefix(p: str) -> Optional[str]:
    """Smallest key that sorts after every key with prefix ``p`` (or None =
    unbounded, when ``p`` is empty / all U+10FFFF)."""
    for i in reversed(range(len(p))):
        c = ord(p[i])
        if c < _MAXCP:
            nc = c + 1
            if 0xD800 <= nc <= 0xDFFF:
                nc = 0xE000
            return p[:i] + chr(nc)
    return None


# --- worker bodies -------------------------------------------------------------

def _orphan_guard():
    """Return a callable that is True once this process's parent has died.

    `pkill` of the CLI does not touch spawn-context children — their cmdline is
    `spawn_main`, not the CLI name — so a killed run leaves workers listing and
    *writing shards* for minutes. A relaunch into the same out_dir then
    interleaves two runs' output: the 2026-08-18b scan collected 9.37M
    duplicate rows (34%) exactly this way, and the run looked healthy (exit 0,
    correct distinct-key count). Death of the parent is detected as a ppid
    change (reparenting), which also works under subreapers where the new
    parent isn't pid 1.
    """
    import os
    ppid0 = os.getppid()
    return lambda: os.getppid() != ppid0


def _range_loop(
    lister: PagedLister,
    bucket: str,
    prefix: Optional[str],
    range_q,
    idle,
    outstanding,
    emit,
    record,
    orphaned=None,
) -> None:
    """One worker: pull ranges, stream them, donate the upper half of the
    remainder whenever peers are idle. Exits when no range is outstanding
    anywhere (every queued range is counted in ``outstanding`` before it is
    enqueued, so 0 means globally done)."""
    while True:
        with idle.get_lock():
            idle.value += 1
        try:
            rng = range_q.get(timeout=_IDLE_POLL_S)
        except Empty:
            with idle.get_lock():
                idle.value -= 1
            if orphaned is not None and orphaned():
                import os
                os._exit(1)  # parent is gone; die before writing anything else
            with outstanding.get_lock():
                if outstanding.value == 0:
                    return
            continue
        with idle.get_lock():
            idle.value -= 1

        start, end = rng
        n_rows = 0
        pages_since_split = 0
        first: Optional[str] = None
        last: Optional[str] = None
        buf: "list[tuple]" = []

        def flush_rows() -> None:
            if buf:
                emit(buf)
                buf.clear()

        try:
            gen = lister.stream_pages(bucket, prefix, start, end)
            try:
                for page in gen:
                    if orphaned is not None and orphaned():
                        import os
                        os._exit(1)
                    pages_since_split += 1
                    if not page:
                        continue
                    orig_len = len(page)
                    if end is not None:
                        page = [r for r in page if r.name < end]
                    if page:
                        buf.extend((r.name, r.size, r.created, r.storage_class) for r in page)
                        if len(buf) >= BATCH_ROWS:
                            flush_rows()
                        if first is None:
                            first = page[0].name
                        last = page[-1].name
                        n_rows += len(page)
                    if end is not None and len(page) < orig_len:
                        break  # crossed the (possibly shrunken) end
                    # Donation gate re-arms after each split (pages *since*
                    # the last one) — without that, one worker floods the
                    # queue with slivers every page while any peer is idle.
                    if pages_since_split >= _MIN_PAGES_BEFORE_SPLIT and last is not None and idle.value > 0:
                        # Bounded range: real interpolation between real keys.
                        # Unbounded: bisect at the observed divergence position
                        # (see open_split — generic bisection lands past the
                        # keyspace when keys share a hot prefix).
                        mid = key_midpoint(last, end) if end is not None else open_split(first, last)
                        if mid is not None and last < mid and (end is None or mid < end):
                            with outstanding.get_lock():
                                outstanding.value += 1
                            range_q.put((mid, end))
                            end = mid
                            pages_since_split = 0
            finally:
                if hasattr(gen, 'close'):
                    gen.close()
            flush_rows()
            record((start, end, n_rows))
        finally:
            # Unconditional: a range that dies mid-stream must still release
            # its outstanding slot, or every other worker spins forever.
            with outstanding.get_lock():
                outstanding.value -= 1


def _adaptive_proc(
    lister: PagedLister,
    bucket: str,
    prefix: Optional[str],
    out_dir: str,
    ns: int,
    threads: int,
    range_q,
    idle,
    outstanding,
    results_q,
    watch_parent: bool = True,
) -> None:
    """Process body: ``threads`` range-loop workers → single shard writer
    (same bounded-queue / single-writer shape as the planned path)."""
    import pandas as pd

    q: Queue = Queue(maxsize=threads)
    done = object()
    records: list = []
    errors: list = []

    def emit(rows: "list[tuple]") -> None:
        q.put(entries_to_frame(bucket, list(rows)))

    # Only armed in spawned children: with procs<=1 this body runs in the CLI
    # process, where a ppid change is legitimate (nohup/disown reparenting).
    orphaned = _orphan_guard() if watch_parent else None

    def one_worker() -> None:
        try:
            _range_loop(lister, bucket, prefix, range_q, idle, outstanding, emit, records.append, orphaned=orphaned)
        except BaseException as e:  # re-raised by the writer loop below
            errors.append(e)

    def produce() -> None:
        try:
            workers = [threading.Thread(target=one_worker, daemon=True) for _ in range(threads)]
            for w in workers:
                w.start()
            for w in workers:
                w.join()
        except BaseException as e:
            errors.append(e)
        finally:
            q.put(done)

    threading.Thread(target=produce, daemon=True).start()

    total = 0
    n_out = 0
    buffer: "list[pd.DataFrame]" = []
    buffered = 0

    def flush() -> None:
        nonlocal n_out, buffered
        if not buffered:
            return
        _write_shard(out_dir, f"shard-{ns:02d}-{n_out:04d}", pd.concat(buffer, ignore_index=True))
        n_out += 1
        buffer.clear()
        buffered = 0

    while (item := q.get()) is not done:
        buffer.append(item)
        buffered += len(item)
        total += len(item)
        if buffered >= ROWS_PER_SHARD:
            flush()
            err(f"  [w{ns}] {total:,} objects, {n_out} shards")
    flush()
    if errors:
        raise errors[0]
    err(f"  [w{ns}] done: {total:,} objects, {n_out} shards, {len(records)} ranges")
    results_q.put((total, records))


# --- top level -----------------------------------------------------------------

def list_bucket_adaptive(
    lister: PagedLister,
    bucket: str,
    out_dir: str,
    procs: int = 6,
    threads: int = 8,
    prefix: Optional[str] = None,
    exists: str = "error",
    warm_ranges: "Optional[list[tuple[Optional[str], Optional[str]]]]" = None,
) -> int:
    """Adaptively list ``bucket`` to parquet shards at ``out_dir``.

    No discovery walk, no weights: the whole (prefix-bounded) keyspace starts
    as one range and splits itself to saturation. ``warm_ranges`` (typically
    a prior run's recorded boundaries, via the CLI's ``--warm-from``) seeds
    the queue pre-converged instead.
    """
    import fsspec
    from multiprocessing import get_context

    out_fs, out_root = fsspec.core.url_to_fs(out_dir)
    out_fs.makedirs(out_root, exist_ok=True)
    reused = resolve_existing(out_fs, out_root, exists)
    if reused is not None:
        return int(reused["objects"])
    started = utc_now()

    pfx = prefix.strip('/') + '/' if prefix and prefix.strip('/') else None
    if warm_ranges:
        seeds = list(warm_ranges)
    elif pfx:
        seeds = [(pfx, next_prefix(pfx))]
    else:
        seeds = [("", None)]

    ctx = get_context("spawn")
    range_q = ctx.Queue()
    results_q = ctx.Queue()
    idle = ctx.Value('i', 0)
    outstanding = ctx.Value('i', len(seeds))
    for s in seeds:
        range_q.put(tuple(s))

    err(
        f"{bucket}{'/' + pfx if pfx else ''}: adaptive listing, "
        f"{procs} procs × {threads} threads, {len(seeds)} seed range(s)"
    )

    def proc_args(ns: int) -> tuple:
        return (lister, bucket, pfx, out_dir, ns, threads, range_q, idle, outstanding, results_q)

    total = 0
    ranges: list = []
    if procs <= 1:
        _adaptive_proc(*proc_args(0), watch_parent=False)
        t, records = results_q.get()
        total += t
        ranges.extend(records)
    else:
        workers = [ctx.Process(target=_adaptive_proc, args=proc_args(ns)) for ns in range(procs)]
        for w in workers:
            w.start()
        # Drain results BEFORE joining: a child blocks on its queue feeder
        # thread at exit if the parent isn't reading (the classic mp.Queue
        # join deadlock). Each child puts exactly one result.
        collected = 0
        while collected < len(workers):
            try:
                t, records = results_q.get(timeout=5)
            except Exception:
                dead = [w.exitcode for w in workers if w.exitcode not in (None, 0)]
                if dead:
                    for w in workers:
                        w.terminate()
                    raise RuntimeError(f"adaptive worker process(es) failed (exit codes {dead})")
                continue
            total += t
            ranges.extend(records)
            collected += 1
        for w in workers:
            w.join()
    ranges.sort(key=lambda r: (r[0] or ''))

    _assert_no_duplicate_keys(out_dir)

    out_fs.pipe(
        f"{out_root}/{SUCCESS_MARKER}",
        json.dumps({
            "bucket": bucket,
            "prefix": prefix,
            "objects": total,
            "mode": "adaptive",
            "ranges": [[s, e, n] for s, e, n in ranges],
            "started": started,
            "finished": utc_now(),
        }).encode(),
    )
    err(f"{bucket}: {total:,} objects listed adaptively ({len(ranges)} final ranges) → {out_dir}")
    return total


class DuplicateKeysError(Exception):
    """A listing emitted the same object key more than once."""


def _assert_no_duplicate_keys(out_dir: str) -> None:
    """Fail the run if any key landed in the shard set twice.

    Donation shrinks a worker's ``end`` to ``mid`` and only *then* records the
    range, so ``_SUCCESS.json``'s ranges are disjoint by construction and prove
    nothing about what was actually emitted. A 2026-08-18 run at 16 procs wrote
    9,372,828 duplicate rows (34% of the listing) while reporting a correct
    object count — ``objects`` counts distinct keys — and exited 0. Nothing
    downstream noticed until the layer-2 rollup produced 1,156 TiB against a
    910 TiB quota.

    Deliberately does *not* delete the shards: the duplicates are the only
    evidence of the race that produced them, and a listing this expensive is
    not worth re-running blind. The missing ``_SUCCESS.json`` is what keeps a
    corrupt listing from being consumed (`import -l` globs shards, but every
    caller that resumes or warm-starts checks the marker).
    """
    import duckdb

    glob = f"{out_dir.rstrip('/')}/shard-*.parquet"
    con = duckdb.connect()
    con.execute("SET threads TO 8")
    rows, distinct = con.execute(
        "SELECT count(*), count(DISTINCT name) FROM read_parquet(?)", [glob],
    ).fetchone()
    if rows == distinct:
        return
    sample = con.execute(
        "SELECT name, count(*) n FROM read_parquet(?) GROUP BY name "
        "HAVING count(*) > 1 ORDER BY n DESC, name LIMIT 5", [glob],
    ).fetchall()
    raise DuplicateKeysError(
        f"{rows - distinct:,} duplicate rows ({100 * (rows - distinct) / rows:.1f}% of "
        f"{rows:,}) in {glob} — shards left in place for debugging; no "
        f"{SUCCESS_MARKER} written. Worst offenders: "
        + ", ".join(f"{n}x {k}" for k, n in sample)
    )


def load_warm_ranges(warm_from: str) -> "list[tuple[Optional[str], Optional[str]]]":
    """Read a prior adaptive run's ``_SUCCESS.json`` boundaries as seeds."""
    import fsspec

    fs, root = fsspec.core.url_to_fs(warm_from)
    marker = f"{root.rstrip('/')}/{SUCCESS_MARKER}"
    payload = json.loads(fs.cat(marker))
    ranges = payload.get("ranges")
    if not ranges:
        raise ValueError(
            f"{marker} has no recorded ranges — --warm-from needs a prior *adaptive* listing"
        )
    return [(s, e) for s, e, *_ in ranges]
