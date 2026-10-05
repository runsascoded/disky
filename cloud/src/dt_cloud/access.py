"""Incremental GCS usage-log ingest (gcs:specs/access-logs-and-cost.md § Productionize).

Google delivers hourly usage CSVs *flat* under ``usage/`` in the log bucket,
with the source bucket as a filename prefix (not a directory):

    gs://<log-bucket>/usage/<bucket>_usage_<YYYY_MM_DD_HH_MM_SS>_<id>_v0

Each run, per source bucket:

1. list names past the watermark (names embed the log hour and sort
   lexicographically per bucket, so a name watermark is a time watermark);
2. chunk new CSVs into ≤ ``max_chunk_gb`` groups;
3. per chunk: stage to local disk → parse (``disk_tree.access.parsers.gcs``)
   → lossless layer-1a parquet (zstd) → layer-2a agg shard
   (``disk_tree.access.aggregate``) → upload both → advance the watermark.

Chunks are the unit of atomicity: a crash reprocesses at most one chunk, and
re-uploads land on the same object names (idempotent). Google may deliver an
hour's file late; the state keeps an ``ingested_tail`` of recently-ingested
names and each run re-lists from ``LAG_HOURS`` before the watermark, so
stragglers inside the lag window are picked up instead of skipped.

Data-bucket layout::

    access/state/<bucket>.json          watermark + ingested tail
    access/raw/<bucket>/part-<first>--<last>.parquet    layer-1a (lossless)
    access/agg/<bucket>/part-<first>--<last>.parquet    layer-2a (tree shards)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path

err = partial(print, file=sys.stderr)



def access_log_bucket() -> str:
    """The bucket the source buckets deliver their usage logs to (`$ACCESS_LOG_BUCKET`)."""
    from .deploy import require
    return require("ACCESS_LOG_BUCKET", what="the bucket the usage logs are delivered to")


def access_buckets() -> list[str]:
    """The buckets whose usage logs are ingested (`$ACCESS_BUCKETS`, space-separated)."""
    from .deploy import words
    return words("ACCESS_BUCKETS", what="the buckets whose usage logs to ingest (or pass -b)")

# Hours of name-time overlap re-listed each run to catch late-delivered files.
LAG_HOURS = 6

_TS_RE = re.compile(r"_usage_(\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2})_")


def name_ts(basename: str) -> str | None:
    """``…_usage_2026_08_13_19_00_00_<id>_v0`` → ``2026_08_13_19_00_00``."""
    m = _TS_RE.search(basename)
    return m.group(1) if m else None


def _ts_minus_hours(ts: str, hours: int) -> str:
    t = dt.datetime.strptime(ts, "%Y_%m_%d_%H_%M_%S")
    return (t - dt.timedelta(hours=hours)).strftime("%Y_%m_%d_%H_%M_%S")


@dataclass
class Chunk:
    names: list[str]  # blob names (full paths in the log bucket)
    bytes: int


def plan_chunks(names_sizes: list[tuple[str, int]], max_chunk_bytes: int) -> list[Chunk]:
    """Greedy fixed-order packing — order is the delivery (name) order, so a
    chunk is a contiguous time span and the watermark can advance per chunk."""
    chunks: list[Chunk] = []
    cur: list[str] = []
    cur_b = 0
    for name, size in names_sizes:
        if cur and cur_b + size > max_chunk_bytes:
            chunks.append(Chunk(cur, cur_b))
            cur, cur_b = [], 0
        cur.append(name)
        cur_b += size
    if cur:
        chunks.append(Chunk(cur, cur_b))
    return chunks


LEASE_TTL_HOURS = 8


def acquire_lease(client, data_bucket: str, owner: str) -> bool:
    """Single-writer lease over the whole ingest (atomic create-if-absent).

    Two concurrent ingests would race the per-bucket watermarks and cut
    different chunk spans over the same CSVs → double-counted rows under
    different part names. The scheduled /6h job and any manual/backfill run
    both take this lease; a crashed holder's lease is stolen after its TTL.
    """
    from google.api_core.exceptions import NotFound, PreconditionFailed

    blob = client.bucket(data_bucket).blob("access/state/.lease")
    body = json.dumps(
        {
            "owner": owner,
            "expires": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=LEASE_TTL_HOURS)).isoformat(
                timespec="seconds"
            ),
        }
    )
    for _ in range(2):
        try:
            blob.upload_from_string(body, content_type="application/json", if_generation_match=0)
            return True
        except PreconditionFailed:
            blob.reload()
            cur = json.loads(blob.download_as_bytes())
            if dt.datetime.fromisoformat(cur["expires"]) > dt.datetime.now(dt.timezone.utc):
                err(f"ingest lease held by {cur.get('owner')} until {cur['expires']} — skipping")
                return False
            try:  # expired: steal (generation-matched delete, then retry the create)
                blob.delete(if_generation_match=blob.generation)
            except (PreconditionFailed, NotFound):
                pass
    return False


def release_lease(client, data_bucket: str) -> None:
    from google.api_core.exceptions import NotFound

    try:
        client.bucket(data_bucket).blob("access/state/.lease").delete()
    except NotFound:
        pass


def _state_blob(client, data_bucket: str, bucket: str):
    return client.bucket(data_bucket).blob(f"access/state/{bucket}.json")


def load_state(client, data_bucket: str, bucket: str) -> dict:
    b = _state_blob(client, data_bucket, bucket)
    if not b.exists():
        return {"watermark": None, "ingested_tail": []}
    return json.loads(b.download_as_bytes())


def save_state(client, data_bucket: str, bucket: str, watermark: str, tail: list[str]) -> None:
    _state_blob(client, data_bucket, bucket).upload_from_string(
        json.dumps(
            {
                "watermark": watermark,
                "ingested_tail": sorted(tail),
                "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        )
        + "\n",
        content_type="application/json",
    )


def list_new(client, log_bucket: str, bucket: str, state: dict) -> list[tuple[str, int]]:
    """Names (with sizes) needing ingest: everything past the watermark, plus
    any straggler inside the lag window not in the ingested tail."""
    prefix = f"usage/{bucket}_usage_"
    wm = state.get("watermark")
    start = None
    if wm:
        wm_ts = name_ts(wm)
        if wm_ts:
            start = prefix + _ts_minus_hours(wm_ts, LAG_HOURS)
    done = set(state.get("ingested_tail") or [])
    out = []
    for b in client.list_blobs(log_bucket, prefix=prefix, start_offset=start):
        base = b.name.rsplit("/", 1)[-1]
        if base in done:
            continue
        # Remaining names are either past the watermark (new) or inside the
        # lag window but missing from the tail (a late-delivered straggler);
        # both get ingested.
        out.append((b.name, int(b.size or 0)))
    out.sort()
    return out


def sweep_ingested(
    client,
    log_bucket: str,
    bucket: str,
    watermark: str | None,
    workers: int = 16,
    lag_hours: int = LAG_HOURS,
) -> int:
    """Move fully-ingested CSVs out of ``usage/`` into ``ingested/``, where a
    7d ``matchesPrefix`` lifecycle rule deletes them (the copy resets the
    object's age clock, so that's 7 days *after conversion*, not delivery).

    Sweeps every name strictly below the lag-window floor (watermark −
    ``lag_hours``) — exactly the set ``list_new`` will never re-list, so sweep
    and ingest can never disagree about a file. A file delivered *ultra*-late
    (name-ts below the floor by the time it lands) is already permanently
    skipped by ingest; sweeping it just moves its deletion from the bucket-wide
    30d backstop to the 7d TTL. Self-healing: a crash mid-sweep leaves files in
    ``usage/`` for the next run; a crash between copy and delete just re-copies.

    ``lag_hours=0`` sweeps everything through the watermark inclusive, which is
    the cutover step to the list-based drain: the drain's work list is "in
    ``usage/`` and not yet an L0 shard", so any CSV the polled path ingested has
    to leave ``usage/`` before the drain starts, or it gets ingested a second
    time. Only safe once the polled ingest has stopped for good.
    """
    from concurrent.futures import ThreadPoolExecutor

    wm_ts = name_ts(watermark or "")
    if not wm_ts:
        return 0
    prefix = f"usage/{bucket}_usage_"
    # end_offset is exclusive, so lag_hours=0 needs a nudge past the watermark
    # itself; '`' is the byte just above '_', the separator that follows a stamp.
    floor = prefix + (f"{wm_ts}`" if lag_hours <= 0 else _ts_minus_hours(wm_ts, lag_hours))
    blobs = list(client.list_blobs(log_bucket, prefix=prefix, end_offset=floor))
    if not blobs:
        return 0
    bkt = client.bucket(log_bucket)

    def move(blob) -> None:
        base = blob.name.rsplit("/", 1)[-1]
        bkt.copy_blob(blob, bkt, f"ingested/{base}")
        blob.delete()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(move, blobs))
    err(f"[{bucket}] swept {len(blobs)} ingested CSVs → ingested/ (7d TTL)")
    return len(blobs)


# Above this, upload in parallel chunks instead of one stream. The job's
# compute is us-central1 while the data bucket is us-east1, and a single
# stream over that RTT measured ~61 MB/s (3.3 GB of layer-1a took 53s, ~37%
# of total ingest wall time). Chunked upload is the standard remedy for a
# latency-bound transfer; it also helps once the buckets are colocated.
PARALLEL_UPLOAD_MIN_BYTES = 64 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 32 * 1024 * 1024


def upload_blob(blob, local: Path, workers: int = 16) -> None:
    """Upload ``local`` to ``blob``, in parallel chunks when it's big enough.

    Falls back to a single stream on any failure of the chunked path: XML
    multipart upload needs ``storage.multipartUploads.*`` and a compatible
    library version, and a slow upload beats a failed ingest.
    """
    from google.cloud.storage import transfer_manager

    size = local.stat().st_size
    if size >= PARALLEL_UPLOAD_MIN_BYTES:
        try:
            # Threads, not the default process pool: the work is socket writes
            # (concurrency over RTT is the whole point), and process workers
            # re-import __main__ in each child — fragile under a container
            # entrypoint, and it hard-fails outright when __main__ isn't a file.
            transfer_manager.upload_chunks_concurrently(
                str(local), blob, chunk_size=UPLOAD_CHUNK_BYTES, max_workers=workers,
                worker_type=transfer_manager.THREAD,
            )
            return
        except Exception as e:
            err(f"chunked upload of {blob.name} failed ({type(e).__name__}: {e}); retrying single-stream")
    blob.upload_from_filename(str(local), timeout=600)


def _span_name(first: str, last: str) -> str:
    a = name_ts(first.rsplit("/", 1)[-1]) or "unknown"
    b = name_ts(last.rsplit("/", 1)[-1]) or "unknown"
    return f"part-{a}--{b}"


def ingest_bucket(
    client,
    bucket: str,
    log_bucket: str,
    data_bucket: str,
    stage_dir: Path,
    memory_limit: str,
    max_chunk_bytes: int,
    workers: int = 16,
    max_chunks: int | None = None,
) -> dict:
    """Ingest all new usage CSVs for one source bucket. Returns stats."""
    import duckdb
    from google.cloud.storage import transfer_manager

    from disk_tree.access.aggregate import aggregate_access
    from disk_tree.access.parsers import parser_for
    from disk_tree.access.parsers.gcs import DEDUPE_WARN_FRACTION, dropped_fraction
    from disk_tree.access.read_sizes import aggregate_read_sizes

    state = load_state(client, data_bucket, bucket)
    todo = list_new(client, log_bucket, bucket, state)
    if not todo:
        err(f"[{bucket}] up to date (watermark {state.get('watermark')})")
        sweep_ingested(client, log_bucket, bucket, state.get("watermark"), workers=workers)
        return {"bucket": bucket, "files": 0, "bytes": 0, "chunks": 0}

    chunks = plan_chunks(todo, max_chunk_bytes)
    if max_chunks is not None:
        chunks = chunks[:max_chunks]
    total_b = sum(c.bytes for c in chunks)
    err(f"[{bucket}] {sum(len(c.names) for c in chunks)} files / {total_b / 1e9:.1f} GB in {len(chunks)} chunk(s)")

    log_bkt = client.bucket(log_bucket)
    data_bkt = client.bucket(data_bucket)
    tail = set(state.get("ingested_tail") or [])
    watermark = state.get("watermark")
    n_rows_total = 0

    for ci, chunk in enumerate(chunks):
        csv_dir = stage_dir / bucket / "csv"
        if csv_dir.exists():
            shutil.rmtree(csv_dir)
        csv_dir.mkdir(parents=True, exist_ok=True)
        err(f"[{bucket}] chunk {ci + 1}/{len(chunks)}: {len(chunk.names)} files, {chunk.bytes / 1e9:.1f} GB — staging")
        results = transfer_manager.download_many_to_path(
            log_bkt,
            chunk.names,
            destination_directory=str(csv_dir),
            max_workers=workers,
        )
        failed = [(n, r) for n, r in zip(chunk.names, results) if isinstance(r, Exception)]
        if failed:
            for n, r in failed[:5]:
                err(f"[{bucket}] stage FAILED {n}: {r}")
            raise RuntimeError(f"{len(failed)}/{len(chunk.names)} downloads failed for {bucket}")

        span = _span_name(chunk.names[0], chunk.names[-1])
        raw_local = stage_dir / bucket / f"{span}.raw.parquet"
        agg_local = stage_dir / bucket / f"{span}.agg.parquet"
        sizes_local = stage_dir / bucket / f"{span}.sizes.parquet"
        con = duckdb.connect()
        con.execute(f"SET memory_limit = '{memory_limit}'")
        con.execute("SET preserve_insertion_order = false")
        # Half the cores: the parquet writer keeps per-thread row-group
        # buffers in flight, and 16 threads' worth blew a 24GB cap on a
        # row-heavy chunk (2026-08-23) that byte-similar chunks survived.
        con.execute(f"SET threads = {max(4, (os.cpu_count() or 8) // 2)}")
        tmp = stage_dir / bucket / ".duckdb-tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = '{tmp}'")

        rel = parser_for("gcs")(f"{csv_dir}/usage/*", store="gcs", con=con)
        rel.write_parquet(str(raw_local), compression="zstd")
        n_rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{raw_local}')").fetchone()[0]
        n_rows_total += n_rows
        # Rows-in vs rows-out — see disk_tree.access.parsers.gcs.dropped_fraction.
        # This chunk-wide parse is the *worse* case for a bad dedupe key, since
        # duplicates are hunted across ~16 CSVs rather than within one.
        n_in, dropped = dropped_fraction(con, n_rows)
        if dropped > DEDUPE_WARN_FRACTION:
            err(
                f"[{bucket}] WARNING chunk {ci + 1}: dedupe dropped "
                f"{n_in - n_rows:,}/{n_in:,} rows ({dropped:.1%}) — expected <"
                f"{DEDUPE_WARN_FRACTION:.0%}; suspect the dedupe key, not the data"
            )
        raw_rel = f"(SELECT * FROM read_parquet('{raw_local}'))"
        stats = aggregate_access(con, raw_rel, str(agg_local))
        # Layer-2b must read 1a, not 2a: 2a has already summed bytes_out across
        # requests, and the per-request size distribution can't be recovered.
        sizes = aggregate_read_sizes(con, raw_rel, str(sizes_local))
        con.close()
        err(
            f"[{bucket}] chunk {ci + 1}: {n_rows:,} rows → {raw_local.stat().st_size / 1e9:.2f} GB raw, "
            f"{stats['paths_out']:,} paths / {stats['hours']} hour(s) agg, "
            f"{sizes['rows']:,} read-size rows / {sizes['prefixes']:,} prefixes"
        )

        # Upload both, then (and only then) advance the watermark — a crash
        # in between reprocesses this chunk onto the same object names.
        for local, kind in ((raw_local, "raw"), (agg_local, "agg"), (sizes_local, "sizes")):
            blob = data_bkt.blob(f"access/{kind}/{bucket}/{span}.parquet")
            upload_blob(blob, local, workers=workers)
            local.unlink()
        shutil.rmtree(csv_dir)

        bases = [n.rsplit("/", 1)[-1] for n in chunk.names]
        watermark = max(watermark or "", *bases)
        wm_ts = name_ts(watermark)
        floor = _ts_minus_hours(wm_ts, LAG_HOURS) if wm_ts else ""
        tail |= set(bases)
        tail = {t for t in tail if (name_ts(t) or "") >= floor}
        save_state(client, data_bucket, bucket, watermark, sorted(tail))

    sweep_ingested(client, log_bucket, bucket, watermark, workers=workers)

    return {
        "bucket": bucket,
        "files": sum(len(c.names) for c in chunks),
        "bytes": total_b,
        "chunks": len(chunks),
        "rows": n_rows_total,
    }


def ingest(
    buckets: tuple[str, ...],
    log_bucket: str,
    data_bucket: str,
    stage_dir: Path,
    memory_limit: str,
    max_chunk_gb: float,
    workers: int,
    max_chunks: int | None = None,
) -> list[dict]:
    import os
    import socket

    from google.cloud import storage

    client = storage.Client()
    owner = os.environ.get("BATCH_JOB_UID") or f"{socket.gethostname()}:{os.getpid()}"
    if not acquire_lease(client, data_bucket, owner):
        return []
    out = []
    try:
        for b in buckets:
            out.append(
                ingest_bucket(
                    client,
                    b,
                    log_bucket=log_bucket,
                    data_bucket=data_bucket,
                    stage_dir=stage_dir,
                    memory_limit=memory_limit,
                    max_chunk_bytes=int(max_chunk_gb * 1e9),
                    workers=workers,
                    max_chunks=max_chunks,
                )
            )
    finally:
        release_lease(client, data_bucket)
    done = [r for r in out if r["files"]]
    err(
        "access ingest done: "
        + (", ".join(f"{r['bucket']}: {r['files']}f/{r['bytes'] / 1e9:.0f}GB" for r in done) if done else "all up to date")
    )
    return out
