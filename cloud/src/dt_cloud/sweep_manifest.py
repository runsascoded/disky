"""Build a staged deletion manifest from sharded per-object listings.

Every bucket and shard is scanned in parallel. Row groups outside the staged
prefixes are pruned using exact ``name`` statistics; only ``size_bytes`` is
read from those groups so the ``outside_bands`` total remains exact.

Results are consumed in shard order under a bounded window. The manifest is
therefore deterministic and memory stays bounded by a small number of shards.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from types import TracebackType
from typing import Iterator

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pyarrow import fs as pafs

err = partial(print, file=sys.stderr)

MANIFEST_SCHEMA = pa.schema([
    ("name", pa.string()),
    ("size_bytes", pa.int64()),
    ("storage_class_id", pa.int8()),
    ("created", pa.timestamp("us", tz="UTC")),
    ("dir", pa.string()),
    ("generation", pa.int64()),
])
_READ = ["name", "size_bytes", "storage_class_id", "created"]
PROGRESS_EVERY = 30.0


class ManifestProgress:
    """Report independently of blocked GCS reads, ordered waits and writes."""

    def __init__(self, workers: int, window: int) -> None:
        self.workers = workers
        self.window = window
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.started = time.monotonic()
        self.phase_started = self.started
        self.phase = "discovering"
        self.current = "-"
        self.total = 0
        self.scanned = 0
        self.written = 0
        self.objects = 0
        self.eligible = 0
        self.scan_seconds = 0.0
        self.write_seconds = 0.0
        self.active: dict[str, float] = {}
        self.thread = threading.Thread(target=self._loop, name="manifest-progress", daemon=True)

    def __enter__(self) -> ManifestProgress:
        self.report()
        self.thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.set_phase("failed" if exc_type else "done")
        self.stop.set()
        self.thread.join()
        self.report()

    def set_phase(self, phase: str, current: str = "-") -> None:
        with self.lock:
            self.phase = phase
            self.current = current
            self.phase_started = time.monotonic()

    def scan(self, task: tuple) -> ShardResult:
        path = task[2]
        started = time.monotonic()
        with self.lock:
            self.active[path] = time.monotonic()
        try:
            result = scan_shard(*task[1:])
            result.scan_seconds = time.monotonic() - started
            with self.lock:
                self.scanned += 1
                self.objects += result.objects
                self.eligible += result.elig_objects
                self.scan_seconds += result.scan_seconds
            return result
        finally:
            with self.lock:
                del self.active[path]

    def consumed(self, write_seconds: float) -> None:
        with self.lock:
            self.written += 1
            self.write_seconds += write_seconds

    def message(self) -> str:
        with self.lock:
            now = time.monotonic()
            oldest = min(self.active, key=self.active.get) if self.active else None
            reading = f"{oldest} ({now - self.active[oldest]:.0f}s)" if oldest else "-"
            return (
                f"manifest progress: {now - self.started:.0f}s elapsed"
                f" · phase={self.phase} ({now - self.phase_started:.0f}s) {self.current}"
                f" · shards scanned={self.scanned}/{self.total}, written={self.written}/{self.total}"
                f" · {self.objects:,} input objects, {self.eligible:,} eligible"
                f" · active readers={len(self.active)}/{self.workers}, window={self.window}"
                f" · oldest reader={reading}"
                f" · scan worker-seconds={self.scan_seconds:.1f}, write-seconds={self.write_seconds:.1f}"
            )

    def report(self) -> None:
        err(self.message(), flush=True)

    def _loop(self) -> None:
        while not self.stop.wait(PROGRESS_EVERY):
            self.report()


def minimal_bands(bands: tuple[str, ...] | list[str]) -> list[str]:
    """Return a sorted, prefix-free set with nested bands removed."""
    out: list[str] = []
    for band in sorted(set(bands)):
        if out and band.startswith(out[-1]):
            continue
        out.append(band)
    return out


def _upper(prefix: str) -> str:
    """The least string greater than every string beginning with ``prefix``."""
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def resolve_fs(url: str) -> tuple[pafs.FileSystem, str]:
    """Resolve a local path or cloud URL to a PyArrow filesystem and path."""
    if "://" not in url:
        return pafs.LocalFileSystem(), os.path.abspath(url)
    return pafs.FileSystem.from_uri(url)


@dataclass
class ShardResult:
    objects: int = 0
    elig_bytes: int = 0
    elig_objects: int = 0
    out_bytes: int = 0
    out_objects: int = 0
    pruned_groups: int = 0
    groups: int = 0
    scan_seconds: float = 0.0
    table: pa.Table | None = None
    dirs: pa.Array | None = None


def _groups_bands(md: pq.FileMetaData, bands: list[str]) -> list[list[str]]:
    """Return the bands each row group's exact ``name`` range can contain."""
    ci = md.schema.names.index("name")
    uppers = [_upper(band) for band in bands]
    out = []
    for i in range(md.num_row_groups):
        stats = md.row_group(i).column(ci).statistics
        if (
            stats is None
            or not stats.has_min_max
            or not getattr(stats, "is_min_exact", True)
            or not getattr(stats, "is_max_exact", True)
        ):
            out.append(list(bands))
            continue
        lo, hi = stats.min, stats.max
        if isinstance(lo, bytes):
            lo, hi = lo.decode(), hi.decode()
        out.append([band for band, upper in zip(bands, uppers) if band <= hi and upper > lo])
    return out


def scan_shard(
    fs: pafs.FileSystem,
    path: str,
    bands: list[str],
    pre_buffer: bool = False,
) -> ShardResult:
    """Scan one listing shard against a bucket's minimal staged bands."""
    result = ShardResult()
    tables: list[pa.Table] = []
    with fs.open_input_file(path) as fh:
        parquet = pq.ParquetFile(fh, pre_buffer=pre_buffer)
        md = parquet.metadata
        result.objects = md.num_rows
        result.groups = md.num_row_groups
        per_group = _groups_bands(md, bands)
        pruned = [i for i, matches in enumerate(per_group) if not matches]
        result.pruned_groups = len(pruned)
        if pruned:
            sizes = parquet.read_row_groups(pruned, columns=["size_bytes"])["size_bytes"]
            result.out_bytes += int(pc.sum(sizes).as_py() or 0)
            result.out_objects += len(sizes)
        for i, matches in enumerate(per_group):
            if not matches:
                continue
            columns = [*_READ, "generation"] if "generation" in parquet.schema.names else _READ
            table = parquet.read_row_group(i, columns=columns)
            names = table["name"]
            mask = pc.starts_with(names, matches[0])
            for band in matches[1:]:
                mask = pc.or_(mask, pc.starts_with(names, band))
            n_in = int(pc.sum(mask).as_py() or 0)
            total_bytes = int(pc.sum(table["size_bytes"]).as_py() or 0)
            if n_in == 0:
                result.out_bytes += total_bytes
                result.out_objects += len(table)
                continue
            if n_in < len(table):
                table = table.filter(mask)
            eligible_bytes = int(pc.sum(table["size_bytes"]).as_py() or 0)
            result.elig_bytes += eligible_bytes
            result.elig_objects += n_in
            result.out_bytes += total_bytes - eligible_bytes
            result.out_objects += len(names) - n_in
            tables.append(table)
    if tables:
        table = pa.concat_tables(tables)
        names = table["name"].cast(pa.string())
        dirs = pc.replace_substring_regex(names, pattern="/[^/]*$", replacement="")
        result.table = pa.table(
            {
                "name": names,
                "size_bytes": table["size_bytes"].cast(pa.int64()),
                "storage_class_id": table["storage_class_id"].cast(pa.int8()),
                "created": table["created"].cast(pa.timestamp("us", tz="UTC")),
                "dir": dirs,
                "generation": (
                    table["generation"].cast(pa.int64())
                    if "generation" in table.column_names
                    else pa.nulls(len(table), type=pa.int64())
                ),
            },
            schema=MANIFEST_SCHEMA,
        )
        result.dirs = pc.unique(dirs)
    return result


@dataclass
class BucketTally:
    objects: int = 0
    elig_bytes: int = 0
    elig_objects: int = 0
    out_bytes: int = 0
    out_objects: int = 0
    groups: int = 0
    pruned_groups: int = 0
    scan_seconds: float = 0.0
    max_scan_seconds: float = 0.0
    write_seconds: float = 0.0
    close_seconds: float = 0.0
    dirs: list[pa.Array] = field(default_factory=list)
    finished: float = 0.0
    _entry: dict | None = None

    def add(self, result: ShardResult) -> None:
        self.objects += result.objects
        self.elig_bytes += result.elig_bytes
        self.elig_objects += result.elig_objects
        self.out_bytes += result.out_bytes
        self.out_objects += result.out_objects
        self.groups += result.groups
        self.pruned_groups += result.pruned_groups
        self.scan_seconds += result.scan_seconds
        self.max_scan_seconds = max(self.max_scan_seconds, result.scan_seconds)
        if result.dirs is not None and len(result.dirs):
            self.dirs.append(result.dirs)

    def n_dirs(self) -> int:
        if not self.dirs:
            return 0
        return len(pc.unique(pa.chunked_array(self.dirs, pa.string())))

    def entry(self) -> dict:
        if self._entry is not None:
            return self._entry
        categories = {
            "eligible": (self.elig_bytes, self.elig_objects),
            "outside_bands": (self.out_bytes, self.out_objects),
        }
        self._entry = {
            "objects": self.objects,
            "dirs": self.n_dirs(),
            **{
                category: {"bytes": size, "objects": objects}
                for category, (size, objects) in categories.items()
                if objects
            },
        }
        self.dirs = []
        return self._entry


def _ordered(
    pool: ThreadPoolExecutor,
    tasks: list[tuple],
    window: int,
    progress: ManifestProgress,
) -> Iterator[tuple[tuple, ShardResult]]:
    """Yield ``scan_shard`` results in task order with bounded concurrency."""
    pending: deque[tuple[tuple, Future]] = deque()
    task_iter = iter(tasks)
    for task in task_iter:
        pending.append((task, pool.submit(progress.scan, task)))
        if len(pending) >= window:
            break
    while pending:
        task, future = pending.popleft()
        progress.set_phase("waiting-for-shard", task[2])
        result = future.result()
        next_task = next(task_iter, None)
        if next_task is not None:
            pending.append((next_task, pool.submit(progress.scan, next_task)))
        yield task, result


def build_manifests(
    root: str,
    date: str,
    bands_by_bucket: dict[str, tuple[str, ...]],
    out: str,
    workers: int | None = None,
    window: int | None = None,
) -> dict[str, dict]:
    """Write one manifest parquet per bucket and return summary entries."""
    workers = workers or min(64, 2 * (os.cpu_count() or 4))
    window = window or 2 * workers
    with ManifestProgress(workers, window) as progress:
        return _build_manifests(root, date, bands_by_bucket, out, workers, window, progress)


def _build_manifests(
    root: str,
    date: str,
    bands_by_bucket: dict[str, tuple[str, ...]],
    out: str,
    workers: int,
    window: int,
    progress: ManifestProgress,
) -> dict[str, dict]:
    fs, rootpath = resolve_fs(root)
    tasks: list[tuple] = []
    for bucket, bands in bands_by_bucket.items():
        progress.set_phase("discovering", bucket)
        selector = pafs.FileSelector(f"{rootpath}/listing/{date}/{bucket}", allow_not_found=True)
        shards = sorted(info.path for info in fs.get_file_info(selector) if info.is_file and info.path.endswith(".parquet"))
        if not shards:
            raise SystemExit(f"no listing shards for {bucket} under {root}/listing/{date}/")
        minimal = minimal_bands(bands)
        tasks.extend((bucket, fs, shard, minimal) for shard in shards)
    with progress.lock:
        progress.total = len(tasks)
    progress.report()
    remaining = {bucket: sum(1 for task in tasks if task[0] == bucket) for bucket in bands_by_bucket}
    tallies = {bucket: BucketTally() for bucket in bands_by_bucket}
    writers: dict[str, pq.ParquetWriter] = {}
    out_fs, out_path = resolve_fs(out)
    progress.set_phase("opening-output", out)
    # Object stores create prefixes by writing objects. Their create_dir APIs
    # can probe or create buckets, requiring permissions artifact writers lack.
    if out_fs.type_name not in ("gcs", "s3"):
        out_fs.create_dir(f"{out_path}/manifest", recursive=True)
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for (bucket, *_), result in _ordered(pool, tasks, window, progress):
                progress.set_phase("writing", bucket)
                tally = tallies[bucket]
                tally.add(result)
                write_started = time.monotonic()
                if result.table is not None:
                    writer = writers.get(bucket)
                    if writer is None:
                        writer = writers[bucket] = pq.ParquetWriter(
                            f"{out_path}/manifest/{bucket}.parquet",
                            MANIFEST_SCHEMA,
                            filesystem=out_fs,
                        )
                    writer.write_table(result.table)
                write_seconds = time.monotonic() - write_started
                tally.write_seconds += write_seconds
                remaining[bucket] -= 1
                progress.consumed(write_seconds)
                if not remaining[bucket]:
                    progress.set_phase("closing-output", bucket)
                    close_started = time.monotonic()
                    writer = writers.pop(bucket, None)
                    if writer is not None:
                        writer.close()
                    tally.close_seconds = time.monotonic() - close_started
                    tally.finished = time.monotonic()
                    progress.set_phase("summarizing", bucket)
                    entry = tally.entry()
                    err(
                        f"  {bucket}: {tally.objects:,} keys, {entry['dirs']:,} dirs — eligible "
                        f"{tally.elig_bytes / 1e12:.2f} TB / {tally.elig_objects:,} objects"
                        f" · {tally.pruned_groups:,}/{tally.groups:,} row groups pruned"
                        f" · {tally.finished - started:.0f}s"
                        f" · scan {tally.scan_seconds:.1f} worker-s (max shard {tally.max_scan_seconds:.1f}s)"
                        f" · write {tally.write_seconds:.1f}s · close {tally.close_seconds:.1f}s"
                    )
    finally:
        progress.set_phase("closing-output")
        for writer in writers.values():
            writer.close()
    return {bucket: tallies[bucket].entry() for bucket in bands_by_bucket}
