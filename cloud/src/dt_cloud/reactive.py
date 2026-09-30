"""Per-CSV ingest, driven by a list-based drain (spec: ``specs/reactive-ingest.md``).

One usage CSV under ``usage/`` → one L0 shard of each layer. Output names are a
pure function of the input name, so re-running any file is idempotent: a repeat
rewrites the same objects.

**What decides the work is a set difference**, not a queue: the CSVs present in
``usage/`` minus the L0 shards already written. That replaces the watermark /
ingested-tail / lease / chunk-planning machinery in :mod:`dt_cloud.access`,
all of which exists only because the polled design has to remember what it has
already seen. Here the bucket listings *are* the state, so there is nothing to
lose, corrupt, or contend on, and a crashed run is repaired by the next one
with no bookkeeping.

We evaluated a Pub/Sub push variant and rejected it (§ 7 of the spec): at ~2,700
files/day a 30-minute drain moves ~56 files in ~3 minutes, so the only thing the
queue bought was autoscaling we don't need — against five extra moving parts and
a cross-org IAM exception that OA's ``iam.allowedPolicyMemberDomains`` policy
blocks anyway.

**The feedback loop is still the hazard to respect.** :func:`ingest_one` copies
the processed CSV to ``ingested/`` *in the same bucket*, so :func:`classify`
insists on ``usage/`` and rejects everything else — without that, a drain would
find its own outputs and grind forever.
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from .access import USAGE_LOG_BUCKET, name_ts, upload_blob

err = partial(print, file=sys.stderr)

#: Only objects under this prefix are ingestable. `ingested/` (the sweep
#: destination) deliberately sits outside it — see the module docstring.
USAGE_PREFIX = "usage/"

#: Where swept CSVs go. Must NOT be under USAGE_PREFIX.
INGESTED_PREFIX = "ingested/"

#: Marks a request log. The same prefix also carries daily `_storage_`
#: byte-hour files, which are a different schema entirely.
USAGE_INFIX = "_usage_"


@dataclass(frozen=True)
class Target:
    """A CSV this handler should ingest."""

    name: str      # full object name in the log bucket
    basename: str
    bucket: str    # the source bucket whose traffic this log describes
    ts: str        # log-hour stamp parsed out of the name


@dataclass(frozen=True)
class Skip:
    """A notification to ack and ignore, with why."""

    reason: str


def classify(name: str) -> Target | Skip:
    """Decide whether an object name is an ingestable usage CSV.

    Every rejection path is a :class:`Skip`, never an exception: a notification
    we don't understand must be acked and dropped, not retried forever.
    """
    if not name.startswith(USAGE_PREFIX):
        return Skip(f"not under {USAGE_PREFIX!r}")
    basename = name[len(USAGE_PREFIX):]
    if "/" in basename:
        return Skip("nested under usage/, not a delivered log")
    if USAGE_INFIX not in basename:
        return Skip(f"no {USAGE_INFIX!r} (storage-byte-hours file?)")
    ts = name_ts(basename)
    if not ts:
        return Skip("no parseable timestamp")
    bucket = basename.split(USAGE_INFIX, 1)[0]
    if not bucket:
        return Skip("empty source-bucket prefix")
    return Target(name=name, basename=basename, bucket=bucket, ts=ts)


#: Layers written per CSV. `l0/` marks these as pre-compaction shards; the
#: daily job merges a closed day's L0 into the span-named files the polled
#: ingest produces, then deletes the L0 inputs.
LAYERS = ("raw", "agg", "sizes")


def l0_path(layer: str, bucket: str, basename: str) -> str:
    """Output object name — a pure function of the input, hence idempotent."""
    if layer not in LAYERS:
        raise ValueError(f"unknown layer {layer!r}; expected one of {LAYERS}")
    return f"access/{layer}/{bucket}/l0/{basename}.parquet"


#: Units accepted in a DuckDB ``memory_limit`` string, in MB.
MEM_UNITS = {"B": 1 / (1024 * 1024), "KB": 1 / 1024, "MB": 1.0, "GB": 1024.0, "TB": 1024.0 * 1024}

#: Don't hand a worker less than this, however many of them there are — DuckDB
#: below ~half a gig spills constantly and the drain gets slower, not smaller.
MIN_WORKER_MB = 512


def split_memory(limit: str, workers: int) -> str:
    """Divide a DuckDB ``memory_limit`` across ``workers`` concurrent connections.

    ``memory_limit`` is *per connection*, so handing the whole budget to each of
    N drain workers lets them collectively commit N× the machine's RAM. That is
    exactly the drift that OOM'd the first scheduled ingest, so the division is
    done here rather than left to each caller to remember.
    """
    m = re.fullmatch(r"\s*([\d.]+)\s*([KMGT]?B)\s*", limit, re.I)
    if not m:
        raise ValueError(f"unparseable memory limit {limit!r}")
    per = float(m.group(1)) * MEM_UNITS[m.group(2).upper()] / max(1, workers)
    if per < MIN_WORKER_MB:
        err(f"memory_limit {limit} over {workers} workers = {per:.0f}MB; flooring at {MIN_WORKER_MB}MB")
        per = MIN_WORKER_MB
    return f"{per:.0f}MB"


def group_by_day(basenames: list[str]) -> dict[str, list[str]]:
    """L0 basenames → ``{YYYY_MM_DD: [basename, …]}``, each list name-sorted.

    Grouping key is the *log hour* in the name, not the delivery time: a CSV
    delivered late still belongs to the day it describes, which is what keeps
    compaction output equivalent to the polled ingest's day-spanning shards.
    """
    out: dict[str, list[str]] = {}
    for n in basenames:
        ts = name_ts(n)
        if ts:
            out.setdefault(ts[:10], []).append(n)
    return {d: sorted(v) for d, v in sorted(out.items())}


def closed_days(by_day: dict[str, list[str]], today: str) -> list[str]:
    """Days safe to compact: everything strictly before ``today`` (YYYY_MM_DD).

    Google's delivery lag means a day's last CSVs land ~14h into the next day,
    so "yesterday" is not necessarily closed. Callers pass a ``today`` already
    backed off far enough (see ``--lag-days``); this function only enforces the
    strict-inequality part, which is the bit that's easy to get wrong.
    """
    return [d for d in sorted(by_day) if d < today]


def unprocessed(usage_names: set[str], l0_names: set[str], floor: str) -> list[str]:
    """CSVs present in ``usage/`` with no L0 shard — i.e. the drain's work list.

    Pure set logic, split out from the I/O so it can be pinned by tests. This is
    the whole scheduling decision: there is no queue, no watermark and no lease,
    so every edge of this function is load-bearing.

    ``usage_names`` and ``l0_names`` are basenames; ``floor`` is a name-order
    timestamp (``YYYY_MM_DD_HH_MM_SS``). Files whose log-hour is at or after
    the floor are held back. GCS object creation is atomic, so this is not
    about partial reads — it is a hook for holding off a window deliberately
    (during the cutover, or to leave the newest hour to a different writer).
    Default is no floor at all.

    Names that aren't ingestable at all are excluded rather than flagged: a
    missing timestamp would otherwise compare below any floor and be
    re-dispatched every run forever, since :func:`classify` skips it and it
    therefore never grows an L0 shard.
    """
    out = []
    for n in usage_names:
        if n in l0_names:
            continue
        if isinstance(classify(f"{USAGE_PREFIX}{n}"), Skip):
            continue
        ts = name_ts(n)
        # `not floor` first: an empty floor means "no holdback", but `ts < ""`
        # is false for every name, so testing the comparison alone drains nothing.
        if ts and (not floor or ts < floor):
            out.append(n)
    return sorted(out)


def pending(
    client,
    bucket: str,
    log_bucket: str,
    data_bucket: str,
    floor: str = "",
) -> list[str]:
    """List one source bucket's un-ingested CSVs (see :func:`unprocessed`).

    Two bucket listings and a set difference — the entire state of the pipeline.
    """
    prefix = f"{USAGE_PREFIX}{bucket}{USAGE_INFIX}"
    usage_names = {
        b.name[len(USAGE_PREFIX):]
        for b in client.list_blobs(log_bucket, prefix=prefix)
    }
    l0_prefix = f"access/raw/{bucket}/l0/"
    l0_names = {
        b.name[len(l0_prefix):].removesuffix(".parquet")
        for b in client.list_blobs(data_bucket, prefix=l0_prefix)
    }
    return unprocessed(usage_names, l0_names, floor)


def drain(
    client,
    buckets,
    log_bucket: str,
    data_bucket: str,
    stage_dir: Path,
    floor: str = "",
    memory_limit: str = "8GB",
    workers: int = 4,
    list_workers: int = 8,
    sweep: bool = True,
) -> dict:
    """Ingest every un-ingested CSV across ``buckets``. The primary entrypoint.

    Failures are per-file and non-fatal: one unparseable CSV must not strand the
    other fifty-five in the run, and the next drain retries it for free, because
    the work list is recomputed from the bucket rather than resumed from a
    cursor. The caller decides what a nonzero ``failed`` count means.

    Oldest-first, so a run that is interrupted (preemption, timeout) still
    leaves the backlog contiguous rather than pocked with holes.
    """
    from concurrent.futures import ThreadPoolExecutor

    buckets = list(buckets)
    with ThreadPoolExecutor(max_workers=list_workers) as ex:
        found = list(ex.map(
            lambda b: (b, pending(client, b, log_bucket=log_bucket, data_bucket=data_bucket, floor=floor)),
            buckets,
        ))
    for b, names in found:
        if names:
            err(f"[{b}] {len(names)} CSV(s) to ingest")
    # By log-hour first, so the fleet drains oldest-first globally rather than
    # bucket-by-bucket (basenames sort by bucket name, which is not the point).
    work = [(n, b) for _, n, b in sorted((name_ts(n), n, b) for b, names in found for n in names)]
    if not work:
        err("drain: nothing to do")
        return {"found": 0, "ingested": 0, "failed": 0, "rows": 0, "buckets": {}}

    per_worker = split_memory(memory_limit, min(workers, len(work)))

    def one(item: tuple[str, str]) -> dict | None:
        basename, bucket = item
        try:
            return ingest_one(
                client, classify(f"{USAGE_PREFIX}{basename}"),
                stage_dir=stage_dir, data_bucket=data_bucket, log_bucket=log_bucket,
                memory_limit=per_worker, sweep=sweep,
            )
        except Exception as e:  # noqa: BLE001 — one bad CSV must not strand the rest
            err(f"[{bucket}] FAILED {basename}: {type(e).__name__}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, work))

    ok = [r for r in results if r]
    per_bucket: dict[str, int] = {}
    for r in ok:
        per_bucket[r["bucket"]] = per_bucket.get(r["bucket"], 0) + 1
    out = {
        "found": len(work),
        "ingested": len(ok),
        "failed": len(results) - len(ok),
        "rows": sum(r["rows"] for r in ok),
        "buckets": per_bucket,
    }
    err(
        f"drain: {out['ingested']}/{out['found']} CSVs, {out['rows']:,} rows"
        + (f", {out['failed']} FAILED" if out["failed"] else "")
    )
    return out


def ingest_one(
    client,
    target: Target,
    stage_dir: Path,
    data_bucket: str,
    log_bucket: str = USAGE_LOG_BUCKET,
    memory_limit: str = "8GB",
    sweep: bool = True,
) -> dict:
    """Download one CSV, write its three L0 shards, then sweep the CSV.

    Ordering matters for crash-safety: the sweep is last, so a crash anywhere
    earlier leaves the CSV in ``usage/`` for the next drain to pick up, and
    the re-run lands on the same object names. A crash between the sweep's copy
    and its delete just re-copies.
    """
    import duckdb

    from disk_tree.access.aggregate import aggregate_access
    from disk_tree.access.parsers import parser_for
    from disk_tree.access.parsers.gcs import DEDUPE_WARN_FRACTION, dropped_fraction
    from disk_tree.access.read_sizes import aggregate_read_sizes

    work = stage_dir / target.basename
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    log_bkt = client.bucket(log_bucket)
    data_bkt = client.bucket(data_bucket)
    try:
        csv_local = work / "usage.csv"
        blob = log_bkt.blob(target.name)
        blob.download_to_filename(str(csv_local))

        con = duckdb.connect()
        con.execute(f"SET memory_limit = '{memory_limit}'")
        con.execute("SET preserve_insertion_order = false")
        tmp = work / ".duckdb-tmp"
        tmp.mkdir(exist_ok=True)
        con.execute(f"SET temp_directory = '{tmp}'")

        locals_ = {layer: work / f"{layer}.parquet" for layer in LAYERS}
        rel = parser_for("gcs")(str(csv_local), store="gcs", con=con)
        rel.write_parquet(str(locals_["raw"]), compression="zstd")
        csv_local.unlink()
        raw_rel = f"(SELECT * FROM read_parquet('{locals_['raw']}'))"
        n_rows = con.execute(f"SELECT COUNT(*) FROM {raw_rel}").fetchone()[0]
        # Rows-in vs rows-out. 1a should be marginally smaller than its source
        # CSV, never substantially — the absence of this check is what let a
        # 41%-loss dedupe bug run in production for ten days.
        n_in, dropped = dropped_fraction(con, n_rows)
        if dropped > DEDUPE_WARN_FRACTION:
            err(
                f"[{target.bucket}] WARNING {target.basename}: dedupe dropped "
                f"{n_in - n_rows:,}/{n_in:,} rows ({dropped:.1%}) — expected <"
                f"{DEDUPE_WARN_FRACTION:.0%}; suspect the dedupe key, not the data"
            )
        agg = aggregate_access(con, raw_rel, str(locals_["agg"]))
        sizes = aggregate_read_sizes(con, raw_rel, str(locals_["sizes"]))
        con.close()

        for layer in LAYERS:
            upload_blob(data_bkt.blob(l0_path(layer, target.bucket, target.basename)), locals_[layer])

        if sweep:
            log_bkt.copy_blob(blob, log_bkt, f"{INGESTED_PREFIX}{target.basename}")
            blob.delete()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    err(
        f"[{target.bucket}] {target.basename}: {n_rows:,} rows → "
        f"{agg['paths_out']:,} paths, {sizes['rows']:,} read-size rows"
    )
    return {
        "bucket": target.bucket,
        "basename": target.basename,
        "rows": n_rows,
        "rows_in": n_in,
        "dropped": dropped,
        "paths": agg["paths_out"],
        "size_rows": sizes["rows"],
        "swept": sweep,
    }


def compact_day(
    client,
    bucket: str,
    day: str,
    basenames: list[str],
    stage_dir: Path,
    data_bucket: str,
    memory_limit: str = "8GB",
    delete_l0: bool = True,
) -> dict:
    """Merge one day's L0 shards into the span-named layout the polled ingest
    produced, then drop the L0 inputs.

    Layer-2a and 2b are **re-derived from the concatenated 1a rows**, not
    concatenated themselves: two L0 aggs for the same day hold different
    partial counts for the same ``(bucket, path, day, op)`` key, and stacking
    them would leave duplicate keys that every consumer would have to re-group.
    Re-aggregating is correct by construction and reuses the same functions.
    """
    import duckdb

    from disk_tree.access.aggregate import aggregate_access
    from disk_tree.access.read_sizes import aggregate_read_sizes

    if not basenames:
        return {"bucket": bucket, "day": day, "inputs": 0}
    span = f"part-{name_ts(basenames[0])}--{name_ts(basenames[-1])}"
    work = stage_dir / f"compact-{bucket}-{day}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    data_bkt = client.bucket(data_bucket)
    try:
        raw_dir = work / "raw"
        raw_dir.mkdir()
        for n in basenames:
            data_bkt.blob(l0_path("raw", bucket, n)).download_to_filename(str(raw_dir / f"{n}.parquet"))

        con = duckdb.connect()
        con.execute(f"SET memory_limit = '{memory_limit}'")
        con.execute("SET preserve_insertion_order = false")
        tmp = work / ".duckdb-tmp"
        tmp.mkdir(exist_ok=True)
        con.execute(f"SET temp_directory = '{tmp}'")

        merged = {layer: work / f"{layer}.parquet" for layer in LAYERS}
        src = f"read_parquet('{raw_dir}/*.parquet')"
        con.execute(f"COPY (SELECT * FROM {src}) TO '{merged['raw']}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        raw_rel = f"(SELECT * FROM read_parquet('{merged['raw']}'))"
        n_rows = con.execute(f"SELECT COUNT(*) FROM {raw_rel}").fetchone()[0]
        agg = aggregate_access(con, raw_rel, str(merged["agg"]))
        sizes = aggregate_read_sizes(con, raw_rel, str(merged["sizes"]))
        con.close()
        shutil.rmtree(raw_dir)

        for layer in LAYERS:
            upload_blob(data_bkt.blob(f"access/{layer}/{bucket}/{span}.parquet"), merged[layer])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # Only after all three promoted shards are durable.
    if delete_l0:
        for n in basenames:
            for layer in LAYERS:
                data_bkt.blob(l0_path(layer, bucket, n)).delete()

    err(f"[{bucket}] compacted {day}: {len(basenames)} L0 → {span} ({n_rows:,} rows)")
    return {
        "bucket": bucket,
        "day": day,
        "inputs": len(basenames),
        "span": span,
        "rows": n_rows,
        "paths": agg["paths_out"],
        "size_rows": sizes["rows"],
    }
