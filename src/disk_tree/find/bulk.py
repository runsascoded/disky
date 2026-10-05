"""Sharded live listing of cloud buckets → canonical layer-1 listing parquet.

Ports marin-gcs-usage's `bucket_list.py` (measured at 32 vCPU × 24 procs × 10 threads on GCS: ~1M objects/min per proc)
into `disk-tree` with a scheme-generic backbone and per-scheme streaming
plugins.

Two levels of parallelism, both sourced from marin's measured tuning:
    - depth-2 prefixes are bin-packed by weight (from a prior listing, if
      given) across N worker *processes* (page parsing is pure-Python — one
      thread pool alone is GIL-bound at ~1M/min);
    - each process streams several prefixes concurrently on threads.

Hot prefixes (a single depth-2 dir with >>ideal object count) are split into
byte-range shards via DuckDB reservoir-quantile boundaries fed to the
scheme's `start_offset` / `end_offset` cursor.

Layer split (see `~/c/disk-tree/specs/gcs-backend-and-snapshot-diff.md`):
    - **This module produces layer-1** (raw per-object listing parquet with
      canonical columns `bucket, name, size_bytes, created, storage_class_id`).
    - Layer-2 aggregation (per-path scan parquet) is `disk-tree import`
      (`find/import_listing.py` + `find/aggregate_duckdb.py`), consuming this
      module's output.

Backends plug in a `BulkLister` implementation (see :class:`BulkLister`) —
the shared helpers below don't touch any cloud SDK.
"""

from __future__ import annotations

import heapq
import json
import math
import sys
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from multiprocessing import get_context
from queue import Queue
from typing import TYPE_CHECKING, Callable, Iterable, Optional, Protocol, Union

import pandas as pd

from disk_tree.listing import SII_CLASS_IDS

if TYPE_CHECKING:
    import fsspec  # noqa: F401


err = partial(print, file=sys.stderr)

ROWS_PER_SHARD = 1_000_000
BATCH_ROWS = 200_000
SAMPLE_ROWS = 500_000
SUCCESS_MARKER = "_SUCCESS.json"

# A stream item is either a whole prefix or (prefix, start, end) — hot
# prefixes are split into name-range shards so a single 100M-object flat dir
# doesn't serialize a whole worker.
StreamItem = Union[str, "tuple[str, Optional[str], Optional[str]]"]


# --- scheme plug-in interface ------------------------------------------------

@dataclass(frozen=True)
class BlobRow:
    """One object as returned by a backend's stream method.

    ``storage_class`` is the raw string from the API (e.g. ``"STANDARD"``);
    :func:`entries_to_frame` maps it to :data:`SII_CLASS_IDS`.
    """
    name: str
    size: int
    created: Optional[str]  # ISO-8601 with 'Z' suffix if UTC
    storage_class: Optional[str]


class BulkLister(Protocol):
    """Per-scheme streaming primitive.

    Implementations are expected to be pickle-safe (they're re-instantiated
    inside worker processes via `ProcessPoolExecutor`). Prefer classmethod /
    staticmethod bodies that build their SDK client inside `stream_prefix` so
    no non-pickleable state is captured at construction.
    """

    scheme: str  # 'gcs' | 's3' | 'r2'

    def stream_prefix(
        self,
        bucket: str,
        prefix: str,
        start: Optional[str],
        end: Optional[str],
    ) -> Iterable[BlobRow]:
        """Yield every object under ``bucket/prefix`` in name-order.

        ``start`` and ``end`` bound the name-range: ``start`` is inclusive,
        ``end`` is exclusive (matches GCS ``start_offset`` / ``end_offset``
        and S3 ``StartAfter`` / a bounded pagination loop). ``None`` means
        no bound on that side.
        """
        ...

    def discover_prefixes(
        self,
        fs: "fsspec.AbstractFileSystem",
        root: str,
    ) -> "tuple[list[tuple[str, int, Optional[str], Optional[str]]], list[str], list[str]]":
        """Walk ``root`` two levels: (shallow_files, depth2_prefixes, self_dirs).

        Default implementation (below) is fsspec-generic; GCS overrides only
        to handle its placeholder-object quirk. See :func:`generic_discover`.
        """
        ...

    def placeholder_rows(
        self,
        bucket: str,
        self_dirs: list[str],
    ) -> "list[tuple[str, int, Optional[str], Optional[str]]]":
        """Return the placeholder objects behind ``self_dirs``.

        Only GCS surfaces zero-byte ``<name>/`` blobs as directories in an
        `ls` listing — for other schemes, the default (below) returns [].
        """
        ...


# --- scheme-generic helpers --------------------------------------------------

def generic_discover(
    fs: "fsspec.AbstractFileSystem",
    root: str,
) -> "tuple[list[tuple[str, int, Optional[str], Optional[str]]], list[str], list[str]]":
    """Two-level walk via fsspec, extracting shallow files, depth-2 prefixes,
    self-dir placeholders.

    Listings can contain the listed path *itself* as a directory entry when a
    placeholder object backs it (GCS quirk). Recursing into those re-lists the
    parent, so skip them at both levels; their names are returned so the
    caller can fetch the placeholder *objects* separately.
    """
    shallow: "list[tuple[str, int, Optional[str], Optional[str]]]" = []
    stream_prefixes: list[str] = []
    self_dirs: list[str] = []
    for e1 in fs.ls(root, detail=True):
        if e1["name"].rstrip("/") == root.rstrip("/"):
            err(f"WARN: skipping self entry {e1['name']!r} in {root!r} listing")
            self_dirs.append("/")
            continue
        for e2 in [e1] if e1["type"] == "file" else fs.ls(e1["name"], detail=True):
            rel = e2["name"].split("/", 1)
            if e2["name"].rstrip("/") == e1["name"].rstrip("/") and e2["type"] != "file":
                err(f"WARN: skipping self entry {e2['name']!r} in {e1['name']!r} listing")
                self_dirs.append(rel[1] + "/" if len(rel) > 1 else "/")
                continue
            if len(rel) < 2 or not rel[1]:
                err(f"WARN: skipping unparseable listing entry {e2['name']!r} under {e1['name']!r}")
                continue
            if e2["type"] == "file":
                shallow.append((rel[1], e2["size"], e2.get("timeCreated"), e2.get("storageClass")))
            else:
                stream_prefixes.append(rel[1] + "/")
    return shallow, stream_prefixes, self_dirs


def pack_chunks(items: list, weights: dict, n: int) -> "list[list]":
    """Greedy bin-pack ``items`` into ``n`` chunks by ``weights``.

    Round-robin by count leaves one worker with the mega-prefixes (marin's
    first balanced run spent half its wall clock on a single straggler
    chunk); a longest-processing-time heap keeps chunk-count within
    ~ideal±max(w).
    """
    bins: "list[tuple[int, int, list]]" = [(0, i, []) for i in range(n)]
    heapq.heapify(bins)
    for p in sorted(items, key=lambda p: (-weights.get(p, 1), str(p))):
        w, i, chunk = heapq.heappop(bins)
        chunk.append(p)
        heapq.heappush(bins, (w + weights.get(p, 1), i, chunk))
    return [chunk for _, _, chunk in sorted(bins, key=lambda b: b[1])]


def split_hot_prefixes(
    prefixes: list[str],
    weights: dict,
    weights_glob: str,
    n_streams: int,
) -> "tuple[list, dict]":
    """Split prefixes heavier than the per-stream ideal into name ranges.

    Range boundaries are name-quantiles from the weights parquet (a prior
    listing or SII inventory — either has every object name), consumed by
    the backend as ``start`` inclusive / ``end`` exclusive, so quantile
    boundaries produce no gaps or dups even when stale. Returns
    ``(items, weights)`` with ranges replacing their prefix.
    """
    import duckdb

    total = sum(weights.get(p, 1) for p in prefixes)
    ideal = max(total // max(n_streams, 1), 1)
    items: list = []
    out_w: dict = dict(weights)
    con = None
    for p in sorted(prefixes):
        w = weights.get(p, 1)
        k = min(math.ceil(w / ideal), n_streams)
        if k < 2:
            items.append(p)
            continue
        if con is None:
            con = duckdb.connect()
            con.execute("SET memory_limit='4GB'")
        fracs = [i / k for i in range(1, k)]
        # Reservoir-sample the prefix's names before quantiling: exact
        # quantile_disc buffers every matching name (30M+ under one prefix →
        # OOM). Boundaries only partition [start, end) ranges, so sample
        # noise costs nothing — a 500k sample lands within ~0.3% of exact.
        [(bounds,)] = con.execute(
            f"SELECT quantile_disc(name, {fracs}) FROM ("
            f"SELECT name FROM read_parquet('{weights_glob}') WHERE starts_with(name, ?)"
            f" USING SAMPLE reservoir({SAMPLE_ROWS} ROWS))",
            [p],
        ).fetchall()
        if not bounds or len(set(bounds)) != len(bounds):
            items.append(p)  # degenerate name distribution — keep whole
            continue
        edges = [None, *bounds, None]
        err(f"splitting hot prefix {p!r} ({w:,} weighted objects) into {k} ranges")
        for start, end in zip(edges[:-1], edges[1:]):
            rng = (p, start, end)
            items.append(rng)
            out_w[rng] = w // k
    return items, out_w


def prefix_weights(weights_glob: str) -> "dict[str, int]":
    """Object counts per depth-1 and depth-2 prefix from a canonical listing."""
    import duckdb

    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    weights: "dict[str, int]" = {}
    for expr in (
        "split_part(name, '/', 1) || '/'",
        "split_part(name, '/', 1) || '/' || split_part(name, '/', 2) || '/'",
    ):
        for prefix, count in con.execute(
            f"SELECT {expr} AS p, count(*) FROM read_parquet('{weights_glob}') WHERE name LIKE '%/%' GROUP BY 1"
        ).fetchall():
            weights[prefix] = int(count)
    return weights


def dedupe_prefixes(prefixes: list[str]) -> "tuple[list[str], list[tuple[str, str]]]":
    """Sorted, de-duplicated prefixes plus the (descendant, ancestor) pairs dropped.

    Placeholder "folder" objects (zero-byte ``dir/`` blobs) make ``ls`` return
    a directory inside its own listing, so the depth-2 enumeration can emit
    both ``scratch/`` and ``scratch/<user>/`` — streaming those subtrees
    twice. Keeping only ancestors makes double-listing structurally
    impossible.
    """
    kept: list[str] = []
    dropped: "list[tuple[str, str]]" = []
    for p in sorted(set(prefixes)):
        if kept and p.startswith(kept[-1]):
            dropped.append((p, kept[-1]))
            continue
        kept.append(p)
    return kept, dropped


def entries_to_frame(
    bucket: str,
    rows: "list[tuple[str, int, Optional[str], Optional[str]]]",
) -> pd.DataFrame:
    """`(name, size, created, storage_class)` tuples → canonical listing columns."""
    names, sizes, created, classes = zip(*rows) if rows else ((), (), (), ())
    return pd.DataFrame(
        {
            "bucket": bucket,
            "name": list(names),
            "size_bytes": [int(s) for s in sizes],
            # format='ISO8601': GCS mixes timestamp precisions (whole-second
            # `…T00:44:00Z` next to microsecond `…T19:41:35.383000Z`); without
            # it pandas ≥2 locks onto the first row's format and raises on the
            # other, killing the worker mid-listing.
            "created": pd.to_datetime(list(created), utc=True, format='ISO8601'),
            "storage_class_id": [SII_CLASS_IDS.get(c or "", 0) for c in classes],
        }
    )


# A shard holds one sorted run per listing range it collected, and layer-2's
# merge opens a source per run. `_shard_rows` skips row groups that miss a
# run's ordinals — but pyarrow's default writes the whole frame as ONE group,
# so the skip is inert and every source decodes the full ~900K-row group.
# At 3 runs/shard that is merely wasteful; at 153 (a 58K-range adaptive
# listing of `marin-us-east-02a`) it OOM-killed the merge at both -j 8 and
# -j 14 on a 61 GB node. Bounding the group makes the skip real: a ~6K-row
# run touches one 64K group instead of all 900K rows.
_ROW_GROUP_ROWS = 1 << 16


def utc_now() -> str:
    """ISO-8601 UTC instant for the listing marker — a scan's as-of bounds
    (`started` ≤ every object's observation ≤ `finished`)."""
    return datetime.now(timezone.utc).isoformat()


def _write_shard(out_dir: str, name: str, frame: pd.DataFrame) -> None:
    import fsspec

    out_fs, out_root = fsspec.core.url_to_fs(out_dir)
    out_fs.makedirs(out_root, exist_ok=True)
    frame.to_parquet(
        f"{out_root}/{name}.parquet",
        index=False,
        filesystem=out_fs,
        row_group_size=_ROW_GROUP_ROWS,
    )


def resolve_existing(out_fs, out_root: str, exists: str) -> Optional[dict]:
    """Apply the exists-policy to a target dir; returns the completed run's
    ``_SUCCESS`` payload when it should be reused, else None (proceed to list).

    - ``error``: refuse if any shards are present
    - ``reuse``: short-circuit iff a completed run's marker is present;
      clear partial output (shards without a marker) and proceed
    - ``clear``: always delete existing shards and proceed
    """
    shards = out_fs.glob(f"{out_root}/*.parquet")
    marker = f"{out_root}/{SUCCESS_MARKER}"
    complete = out_fs.exists(marker)
    if not shards and not complete:
        return None
    if exists == "error":
        raise ValueError(
            f"{out_root} already has {len(shards)} shards"
            f"{' and a completion marker' if complete else ' (no completion marker — partial run?)'};"
            " pass --exists clear|reuse or choose another --out"
        )
    if exists == "reuse" and complete:
        payload = json.loads(out_fs.cat(marker))
        err(f"{out_root}: reusing completed listing ({payload.get('objects', '?'):,} objects)")
        return payload
    # 'clear', or 'reuse' over a partial run
    err(f"{out_root}: clearing {len(shards)} existing shard(s){' (partial run)' if exists == 'reuse' else ''}")
    if shards:
        out_fs.rm(shards)
    if complete:
        out_fs.rm(marker)
    return None


# --- worker + top-level ------------------------------------------------------

def _stream_prefixes_worker(
    lister: BulkLister,
    bucket: str,
    prefixes: "list[StreamItem]",
    out_dir: str,
    ns: int,
    threads: int,
) -> int:
    """Worker-process body: stream items to shards named ``shard-<ns>-*``.

    Bounded ``Queue`` between producer threads and the single-writer
    consumer keeps memory O(threads * BATCH_ROWS), not O(prefix contents).
    """
    q: Queue = Queue(maxsize=threads)
    done = object()
    produce_error: "list[BaseException]" = []

    def one(item: StreamItem) -> None:
        pfx, start, end = item if isinstance(item, tuple) else (item, None, None)
        rows: "list[tuple[str, int, Optional[str], Optional[str]]]" = []
        for blob in lister.stream_prefix(bucket, pfx, start, end):
            rows.append((blob.name, blob.size, blob.created, blob.storage_class))
            if len(rows) >= BATCH_ROWS:
                q.put(entries_to_frame(bucket, rows))
                rows = []
        if rows:
            q.put(entries_to_frame(bucket, rows))

    def produce() -> None:
        try:
            with ThreadPoolExecutor(threads) as ex:
                for _ in ex.map(one, prefixes):
                    pass
        except BaseException as e:  # surfaced in the consumer loop below
            produce_error.append(e)
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
    if produce_error:
        raise produce_error[0]
    flush()
    err(f"  [w{ns}] done: {total:,} objects, {n_out} shards")
    return total


def list_bucket_to_parquet(
    lister: BulkLister,
    bucket: str,
    out_dir: str,
    fs: "fsspec.AbstractFileSystem",
    procs: int = 6,
    threads: int = 8,
    prefix: Optional[str] = None,
    exists: str = "error",
    weights_from: Optional[str] = None,
    discover: Optional[Callable] = None,
) -> int:
    """List ``bucket`` (optionally under ``prefix``) to parquet shards in ``out_dir``.

    ``out_dir`` may be local or any fsspec-supported URL. Returns total
    object count. A ``_SUCCESS.json`` marker (with counts) is written on
    completion; ``exists`` governs behavior when the target already has
    output (see :func:`resolve_existing`).

    ``discover`` overrides the two-level walk (default:
    :func:`generic_discover`). GCS backends pass their own to handle
    placeholder rows.
    """
    import fsspec

    out_fs, out_root = fsspec.core.url_to_fs(out_dir)
    out_fs.makedirs(out_root, exist_ok=True)
    reused = resolve_existing(out_fs, out_root, exists)
    if reused is not None:
        return int(reused["objects"])
    started = utc_now()

    root = f"{bucket}/{prefix.strip('/')}" if prefix else bucket
    _discover = discover or generic_discover
    shallow, stream_prefixes, self_dirs = _discover(fs, root)
    if self_dirs:
        shallow.extend(lister.placeholder_rows(bucket, self_dirs))
    stream_prefixes, nested = dedupe_prefixes(stream_prefixes)
    for p, parent in nested:
        err(f"WARN: dropping nested prefix {p!r} (inside {parent!r})")
    # De-nested depth-1 parents stream their whole subtree, which includes
    # depth-2 files that also landed in `shallow` — keep them stream-side only.
    parents = tuple({parent for _, parent in nested})
    if parents:
        before = len(shallow)
        shallow = [row for row in shallow if not row[0].startswith(parents)]
        err(f"{root}: dropped {before - len(shallow)} shallow objects covered by de-nested parents")
    err(
        f"{root}: {len(stream_prefixes)} depth-2 prefixes, {len(shallow)} shallow objects;"
        f" {procs} procs × {threads} threads"
    )

    _write_shard(out_dir, "shard-shallow", entries_to_frame(bucket, shallow))
    total = len(shallow)

    weights = prefix_weights(weights_from) if weights_from else {}
    stream_items: "list[StreamItem]" = list(stream_prefixes)
    if weights:
        err(f"chunk balancing from {weights_from} ({len(weights)} weighted prefixes)")
        stream_items, weights = split_hot_prefixes(stream_prefixes, weights, weights_from, procs * threads)
    chunks = pack_chunks(stream_items, weights, procs)
    with ProcessPoolExecutor(procs, mp_context=get_context("spawn")) as ex:
        futures = [
            ex.submit(_stream_prefixes_worker, lister, bucket, chunk, out_dir, ns, threads)
            for ns, chunk in enumerate(chunks)
            if chunk
        ]
        for f in futures:
            total += f.result()
    out_fs.pipe(
        f"{out_root}/{SUCCESS_MARKER}",
        json.dumps({
            "bucket": bucket, "prefix": prefix, "objects": total,
            "started": started, "finished": utc_now(),
        }).encode(),
    )
    err(f"{root}: {total:,} objects listed → {out_dir}")
    return total
