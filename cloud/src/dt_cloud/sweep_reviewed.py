"""Stream a completed DR's exact generation set without a fresh prefix relist."""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from functools import lru_cache, partial
from pathlib import Path
from typing import Any, Iterator

import fsspec
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .staged_plan import load_plan
from .sweep_batch import _parts
from .sweep_exec import PROGRESS_EVERY, _missing_perms, _soft_delete_days, delete_batch
from .sweep_manifest import ManifestProgress
from .sweep_journal import DecisionJournal
from .sweep_xml import XmlDeleter
from .sweep_pacing import BucketPacer, DeleteAttempt

err = partial(print, file=sys.stderr)
COLUMNS = ["name", "size_bytes", "generation", "decision", "dir"]
LOG_SCHEMA = pa.schema([(name, pa.string() if name in ("name", "decision", "dir") else pa.int64()) for name in COLUMNS])
LOG_ROWS = 65_536
CACHE_WORKERS = 8


@dataclass(frozen=True)
class ReviewedObject:
    name: str
    size: int
    generation: int
    directory: str


def pin_path(fs: Any, path: str) -> str:
    protocols = fs.protocol if isinstance(fs.protocol, tuple) else (fs.protocol,)
    if "gcs" in protocols or "gs" in protocols:
        generation = fs.info(path).get("generation")
        if not generation:
            raise ValueError("cannot pin reviewed source artifact")
        return f"{path}#{generation}"
    return path


def reviewed_tables(path: Path) -> Iterator[pa.Table]:
    for batch in pq.ParquetFile(path).iter_batches(batch_size=65_536, columns=COLUMNS):
        table = pa.Table.from_batches([batch])
        yield table.filter(pc.equal(table["decision"], "delete"))


def prepare_reviewed(
    source: str,
    plan_path: str,
    work_dir: str,
    only_buckets: tuple[str, ...] = (),
) -> tuple[dict, dict, dict[str, list[Path]], dict]:
    """Pin/cache/preflight every delete row before the first target mutation."""
    with ManifestProgress(workers=CACHE_WORKERS, window=CACHE_WORKERS) as progress:
        progress.set_phase("reviewed-preflight", source)
        return _prepare_reviewed(source, plan_path, work_dir, only_buckets, progress)


def _prepare_reviewed(
    source: str,
    plan_path: str,
    work_dir: str,
    only_buckets: tuple[str, ...],
    progress: ManifestProgress,
) -> tuple[dict, dict, dict[str, list[Path]], dict]:
    plan = load_plan(plan_path)
    fs, path = fsspec.core.url_to_fs(source, version_aware=True)

    def read_json(name: str) -> dict:
        with fs.open(pin_path(fs, f"{path}/{name}")) as fh:
            return json.load(fh)

    original = read_json("plan-summary.json")
    dry = read_json("would-delete-summary.json")
    approved = sorted(prefix for bucket in plan.buckets for prefix in plan.bands(bucket))
    if any(bucket not in plan.buckets for bucket in only_buckets):
        raise ValueError("requested bucket is outside the staged plan")
    cut_approved = sorted(prefix for bucket in plan.buckets if not only_buckets or bucket in only_buckets for prefix in plan.bands(bucket))
    reviewed_approved = sorted(original.get("approved", []))
    if original.get("plan_id") != plan.plan_id or reviewed_approved not in (approved, cut_approved):
        raise ValueError("reviewed DR does not match the current staged plan")
    expected_buckets = sorted(bucket for bucket, entry in original["buckets"].items() if entry.get("eligible", {}).get("objects", 0))
    if original.get("diagnostic") or dry.get("diagnostic") or dry.get("for_real") is not False or sorted(dry.get("buckets", {})) != expected_buckets:
        raise ValueError("reviewed execution requires a completed, non-diagnostic DR")
    if any(entry.get("interrupted") or entry.get("failed_dirs") for entry in dry["buckets"].values()):
        raise ValueError("reviewed DR is interrupted or failed")
    if any(bucket not in original["buckets"] for bucket in only_buckets):
        raise ValueError("requested bucket is outside the reviewed DR")
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):
        raise ValueError("reviewed execution work directory must be empty")
    files = {}
    artifacts = {}
    for bucket, entry in dry["buckets"].items():
        if only_buckets and bucket not in only_buckets:
            continue
        parts = _parts(fs, path, bucket)
        if not parts and entry.get("decisions", {}).get("delete", 0):
            raise ValueError(f"{bucket}: reviewed decision log is missing")
        files[bucket] = []
        artifacts[bucket] = []
        count = size = all_rows = 0
        prefixes = plan.sweep[bucket]
        pattern = "^(?:" + "|".join(re.escape(prefix) for prefix in prefixes) + ")"
        with progress.lock:
            progress.total += len(parts)

        def read_part(task: tuple[int, str]) -> tuple[Path, str, int, int, int]:
            index, part = task
            started = time.monotonic()
            pinned = pin_path(fs, part)
            local = work / f"{bucket}-{index:05d}.parquet"
            err(f"reviewed preflight: caching {bucket} part {index + 1}/{len(parts)}", flush=True)
            progress.set_phase("reviewed-cache", part)
            fs.get_file(pinned, str(local))
            progress.set_phase("reviewed-validate", part)
            parquet = pq.ParquetFile(local)
            part_count = part_size = 0
            for table in reviewed_tables(local):
                if not len(table):
                    continue
                if table["generation"].null_count or pc.any(pc.less_equal(table["generation"], 0)).as_py():
                    raise ValueError(f"{bucket}: reviewed delete row lacks a positive generation")
                if table["name"].null_count or not pc.all(pc.match_substring_regex(table["name"], pattern)).as_py():
                    raise ValueError(f"{bucket}: reviewed delete row is outside staged prefixes")
                if table["size_bytes"].null_count or pc.any(pc.less(table["size_bytes"], 0)).as_py():
                    raise ValueError(f"{bucket}: reviewed delete row has invalid size")
                if table["dir"].null_count:
                    raise ValueError(f"{bucket}: reviewed delete row lacks directory metadata")
                part_count += len(table)
                part_size += int(pc.sum(table["size_bytes"]).as_py() or 0)
            with progress.lock:
                progress.scanned += 1
                progress.objects += parquet.metadata.num_rows
                progress.eligible += part_count
                progress.scan_seconds += time.monotonic() - started
            return local, pinned, part_count, part_size, parquet.metadata.num_rows

        def cache_part(task: tuple[int, str]) -> tuple[Path, str, int, int, int]:
            part = task[1]
            with progress.lock:
                progress.active[part] = time.monotonic()
            try:
                return read_part(task)
            finally:
                with progress.lock:
                    del progress.active[part]

        with ThreadPoolExecutor(max_workers=CACHE_WORKERS, thread_name_prefix="reviewed-cache") as pool:
            for local, pinned, part_count, part_size, all_part_rows in pool.map(cache_part, enumerate(parts)):
                count += part_count
                size += part_size
                all_rows += all_part_rows
                files[bucket].append(local)
                artifacts[bucket].append({"path": pinned, "rows": all_part_rows})
        if count != entry.get("decisions", {}).get("delete", 0) or size != entry.get("delete_bytes", 0) or all_rows != sum(entry.get("decisions", {}).values()):
            raise ValueError(f"{bucket}: reviewed log totals do not match the completed DR")
        err(f"reviewed preflight: {bucket} verified {count:,} exact-generation deletes", flush=True)
    return original, dry, files, artifacts


def execute_reviewed(
    source: str,
    plan_path: str,
    out: str,
    work_dir: str,
    for_real: bool = False,
    only_buckets: tuple[str, ...] = (),
    backend: str = "xml",
    workers: int = 16,
    max_rate: int = 8000,
    stop: threading.Event | None = None,
    client: Any = None,
    deleter: Any = None,
    on_prepared: Any = None,
    bucket_workers: int = 6,
    initial_rate: int = 1000,
    verified_soft_delete_days: int | None = None,
    exclude_runs: tuple[str, ...] = (),
    on_progress: Any = None,
    pacing: str = "guided",
) -> dict:
    """No re-list/drift audit: delete only generations observed by the DR.

    Full preflight precedes deletion. Buckets share a bounded HTTP worker pool;
    complete local journal parts upload independently under backpressure.
    Stop drains submitted requests and journal uploads. Each bucket has its
    own feedback controller; guided mode retains the GCS cold-ramp envelope.
    """
    from google.cloud import storage

    if backend not in ("xml", "json") or not 1 <= workers <= 64 or max_rate <= 0 or initial_rate <= 0 or not 1 <= bucket_workers <= 6 or pacing not in ("guided", "adaptive"):
        raise ValueError("invalid reviewed deletion backend/workers/rate")
    if pacing == "adaptive" and backend != "xml":
        raise ValueError("adaptive pacing requires the XML backend")
    fs, path = fsspec.core.url_to_fs(out)
    if fs.exists(f"{path}/plan-summary.json") or fs.glob(f"{path}/deleted/**") or fs.glob(f"{path}/would-delete/**"):
        raise ValueError("reviewed execution output directory must be new")
    plan, dry, files, artifacts = prepare_reviewed(source, plan_path, work_dir, only_buckets)
    resume = None
    if exclude_runs:
        from .sweep_resume import subtract_settled

        with ManifestProgress(workers=8, window=8) as progress:
            dry, files, resume = subtract_settled(source, exclude_runs, dry, files, Path(work_dir), progress)
    if (initial_rate > 1000 or verified_soft_delete_days is not None) and for_real:
        from .sweep_delete_benchmark import scratch_target

        # Warm-start benchmarking is confined to the same unique scratch root.
        scratch = source.removesuffix("/reviewed-source")
        bucket, prefix = scratch_target(scratch)
        if sorted(files) != [bucket] or plan["approved"] not in ([f"gs://{bucket}/{prefix}/reviewed/"], [f"gs://{bucket}/{prefix}/reviewed-warm/"]):
            raise ValueError("warm reviewed rate requires the exact synthetic benchmark plan")
    selected = {
        bucket: {
            **plan["buckets"][bucket],
            "eligible": {"objects": dry["buckets"][bucket]["decisions"].get("delete", 0), "bytes": dry["buckets"][bucket].get("delete_bytes", 0)},
        }
        for bucket in files
    }
    plan = {
        **plan,
        "buckets": selected,
        "approved": [prefix for prefix in plan["approved"] if prefix.split("/", 3)[2] in selected],
        "total": {"eligible": {key: sum(entry["eligible"][key] for entry in selected.values()) for key in ("objects", "bytes")}},
    }
    client = client or storage.Client()
    guards = {}
    for bucket in files:
        missing = _missing_perms(client.bucket(bucket))
        if verified_soft_delete_days is None:
            days = _soft_delete_days(client, bucket)
        else:
            days = verified_soft_delete_days
            missing = [permission for permission in missing if permission != "storage.buckets.get"]
        if for_real and (missing or days < 7):
            raise ValueError(f"{bucket}: permissions/soft-delete guard refused reviewed deletion")
        guards[bucket] = {"missing_perms": missing, "soft_delete_days": days}
    mode = "deleted" if for_real else "would-delete"
    summary = {"plan": out, "for_real": for_real, "diagnostic": not for_real, "drift": "reviewed-generations-no-relist", "backend": backend, "pacing": pacing, "bucket_workers": bucket_workers, "reviewed_source": source, "delete_count_semantics": "acknowledged-generation-deletes", "buckets": {}}
    if resume is not None:
        summary["resume_runs"] = list(exclude_runs)
    fs.makedirs(path, exist_ok=True)
    with fs.open(f"{path}/plan-summary.json", "w") as fh:
        json.dump({**plan, "diagnostic": not for_real, "reviewed_source": source, "contract": "reviewed-delete-generations"}, fh)
    with fs.open(f"{path}/reviewed-source.json", "w") as fh:
        json.dump(artifacts, fh)
    if resume is not None:
        with fs.open(f"{path}/resume-source.json", "w") as fh:
            json.dump(resume, fh)
    if on_prepared is not None:
        on_prepared(plan)
    stop = stop or threading.Event()
    slots = threading.BoundedSemaphore(workers * 2)
    batch_size = 1000 if backend == "xml" else 100
    def execute_bucket(bucket: str, parts: list[Path], pool: ThreadPoolExecutor) -> dict:
        expected = dry["buckets"][bucket]["decisions"].get("delete", 0)
        counts = Counter()
        bands = {}
        failures = Counter()
        completed = 0
        deleted_bytes = 0
        started = time.monotonic()
        pacer = BucketPacer(max_rate, min(initial_rate, max_rate), pacing)
        instrumented = backend == "xml" and for_real and deleter is None
        delete = deleter or (XmlDeleter(credentials=client._credentials, before_attempt=lambda count: pacer.wait(count), on_attempt=pacer.observe) if instrumented else delete_batch)
        progress = {"bucket": bucket, "mode": mode, "roots": math.ceil(expected / batch_size), "roots_done": 0, "unit": "batches", "decisions": {}, "delete_bytes": 0, "started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "updated": None, "done": False}
        prefixes = sorted((p for p in plan["approved"] if p.startswith(f"gs://{bucket}/")), key=len, reverse=True)
        @lru_cache(maxsize=8192)
        def band_of(directory: str) -> str:
            uri = f"gs://{bucket}/{directory}/"
            return next(prefix for prefix in prefixes if uri.startswith(prefix))

        lock = threading.Lock()
        reporter_stop = threading.Event()
        journal = DecisionJournal(fs, f"{path}/{mode}/{bucket}", Path(work_dir) / "journals" / bucket, LOG_SCHEMA, stop, rows=LOG_ROWS)
        performance = {"http_worker_seconds": 0.0, "rate_wait_seconds": 0.0, "collect_seconds": 0.0, "batch_read_seconds": 0.0}

        def write_progress(final: bool = False) -> None:
            with lock:
                pacing_metrics = pacer.metrics()
                snapshot = {**progress, "decisions": dict(counts), "bands": {prefix: dict(count) for prefix, count in bands.items()}, "roots_done": completed, "delete_bytes": deleted_bytes, "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "done": final, "pipeline": {**performance, "rate_limit": pacing_metrics["rate"], "pacing": pacing_metrics, "journal": journal.metrics()}}
            from .sweep_exec import log_execution_progress

            log_execution_progress(snapshot)
            err(f"pipeline progress: {bucket} · {json.dumps(snapshot['pipeline'], sort_keys=True)}", flush=True)
            try:
                fs.makedirs(f"{path}/progress", exist_ok=True)
                with fs.open(f"{path}/progress/{bucket}.json", "w") as fh:
                    json.dump(snapshot, fh)
            except Exception as error:
                err(f"WARN: reviewed progress write failed: {error}", flush=True)
            if on_progress is not None:
                try:
                    on_progress(snapshot)
                except Exception as error:
                    err(f"WARN: progress history write failed: {error}", flush=True)

        def reporter() -> None:
            while not reporter_stop.wait(PROGRESS_EVERY):
                write_progress()

        def consume(result: list[tuple[ReviewedObject, str]]) -> None:
            nonlocal completed, deleted_bytes
            rows = []
            with lock:
                for blob, decision in result:
                    rows.append((blob.name, blob.size, blob.generation, decision, blob.directory))
                    counts[decision] += 1
                    prefix = band_of(blob.name.rpartition("/")[0])
                    band = bands.setdefault(prefix, Counter())
                    if decision == "delete":
                        deleted_bytes += blob.size
                        band["bytes"] += blob.size
                        band["objects"] += 1
                    elif decision == "skipped_gone":
                        band["gone"] += 1
                    elif decision == "skipped_overwritten":
                        band["overwritten"] += 1
                    else:
                        failures[blob.directory] += 1
                        band["failed"] += 1
                completed += 1
            journal.append(rows)

        def batches() -> Iterator[list[ReviewedObject]]:
            buffer = []
            for part in parts:
                for table in reviewed_tables(part):
                    reading = time.monotonic()
                    columns = [table[name].to_pylist() for name in ("name", "size_bytes", "generation", "dir")]
                    with lock:
                        performance["batch_read_seconds"] += time.monotonic() - reading
                    for row in zip(*columns):
                        buffer.append(ReviewedObject(*row))
                        if len(buffer) == batch_size:
                            yield buffer
                            buffer = []
            if buffer:
                yield buffer

        thread = threading.Thread(target=reporter, name="reviewed-progress", daemon=True)
        thread.start()
        write_progress()
        pending = {}
        error = None

        def collect(future: Any) -> None:
            nonlocal error
            collecting = time.monotonic()
            batch = pending.pop(future)
            try:
                result = future.result()
            except Exception as caught:
                if error is None:
                    error = caught
                err(f"ERROR: reviewed batch failed: {caught}", flush=True)
                result = [(blob, "delete_failed") for blob in batch]
            consume(result)
            if any(decision == "delete_failed" for _, decision in result) and error is None:
                error = RuntimeError("reviewed batch contains failed deletions; stopping new submissions")
            if error:
                stop.set()
            with lock:
                performance["collect_seconds"] += time.monotonic() - collecting

        def request(batch: list[ReviewedObject]) -> list[tuple[ReviewedObject, str]]:
            requesting = time.monotonic()
            try:
                if not for_real:
                    return [(blob, "delete") for blob in batch]
                if not instrumented:
                    pacer.wait(len(batch))
                result = delete(client, client.bucket(bucket), batch)
                if not instrumented:
                    pacer.observe(DeleteAttempt(len(batch), time.monotonic() - requesting))
                return result
            finally:
                slots.release()
                with lock:
                    performance["http_worker_seconds"] += time.monotonic() - requesting

        def collect_ready() -> None:
            for future in list(pending):
                if future.done():
                    collect(future)

        try:
            for batch in batches():
                collect_ready()
                if stop.is_set():
                    break
                if for_real:
                    waiting = time.monotonic()
                    admitted = pacer.wait(len(batch), "submit", stop, collect_ready)
                    with lock:
                        performance["rate_wait_seconds"] += time.monotonic() - waiting
                    if not admitted:
                        break
                acquired = False
                while not stop.is_set():
                    acquired = slots.acquire(timeout=.1)
                    if acquired:
                        break
                    collect_ready()
                if stop.is_set():
                    if acquired:
                        slots.release()
                    break
                try:
                    future = pool.submit(request, batch)
                except Exception:
                    slots.release()
                    raise
                pending[future] = batch
                if len(pending) >= workers * 2:
                    ready, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in ready:
                        collect(future)
                    if error:
                        break
            while pending:
                ready, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in ready:
                    collect(future)
        except Exception as caught:
            error = caught
            # A fatal answer must not discard successful in-flight undo records.
            for future in list(pending):
                collect(future)
        finally:
            try:
                journal.close()
            except Exception as caught:
                if error is None:
                    error = caught
                stop.set()
                err(f"ERROR: {bucket}: {caught}", flush=True)
            reporter_stop.set()
            thread.join()
            write_progress(final=error is None and sum(counts.values()) == expected)
        interrupted = sum(counts.values()) != expected
        return {
            **guards[bucket], "decisions": dict(counts), "delete_bytes": deleted_bytes,
            "drift_dirs": [], "failed_dirs": [{"dir": directory, "objects": count} for directory, count in sorted(failures.items())],
            "bands": {prefix: dict(count) for prefix, count in bands.items()},
            "performance": {"seconds": round(time.monotonic() - started, 3), "delete_workers": workers, "batch_size": batch_size, **performance, "pacing": pacer.metrics(), "journal": journal.metrics()},
            **({"interrupted": {"roots_skipped": progress["roots"] - completed, "roots": progress["roots"]}} if interrupted else {}),
            **({"error": str(error)} if error else {}),
        }
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="xml-delete") as delete_pool:
        with ThreadPoolExecutor(max_workers=bucket_workers, thread_name_prefix="reviewed-bucket") as buckets_pool:
            futures = {bucket: buckets_pool.submit(execute_bucket, bucket, parts, delete_pool) for bucket, parts in files.items()}
            for bucket, future in futures.items():
                try:
                    summary["buckets"][bucket] = future.result()
                except Exception:
                    stop.set()
                    raise
    with fs.open(f"{path}/{mode}-summary.json", "w") as fh:
        json.dump(summary, fh)
    summary["_plan"] = plan
    return summary
