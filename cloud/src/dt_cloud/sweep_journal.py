"""Bounded local decision spooling with independent remote upload workers."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


class DecisionJournal:
    """Keep complete local parts; apply backpressure before memory/disk grows.

    Remote writes never hold the progress lock. A failed upload stops new
    deletes, but all submitted delete results are still spooled and drained.
    Success is reported only after every part has been uploaded successfully.
    """

    def __init__(
        self,
        fs: Any,
        remote: str,
        local: Path,
        schema: pa.Schema,
        stop: threading.Event,
        rows: int = 65_536,
        workers: int = 2,
        window: int = 4,
    ) -> None:
        if rows <= 0 or workers <= 0 or window < workers:
            raise ValueError("invalid decision journal rows/workers/window")
        self.fs = fs
        self.remote = remote
        self.local = local
        self.schema = schema
        self.stop = stop
        self.rows = rows
        self.buffer: list[tuple] = []
        self.number = 0
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(window)
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="decision-upload")
        self.error: Exception | None = None
        self.stats = {"parts_spooled": 0, "parts_uploaded": 0, "rows_uploaded": 0, "spool_seconds": 0.0, "upload_worker_seconds": 0.0, "backpressure_seconds": 0.0}
        local.mkdir(parents=True, exist_ok=False)
        fs.makedirs(remote, exist_ok=True)

    def metrics(self) -> dict:
        with self.lock:
            return {**self.stats, "local_spool": str(self.local)}

    def _upload(self, local: Path, rows: int) -> None:
        started = time.monotonic()
        try:
            self.fs.put_file(str(local), f"{self.remote}/{local.name}")
            with self.lock:
                self.stats["parts_uploaded"] += 1
                self.stats["rows_uploaded"] += rows
        except Exception as error:
            with self.lock:
                if self.error is None:
                    self.error = error
            self.stop.set()
        finally:
            with self.lock:
                self.stats["upload_worker_seconds"] += time.monotonic() - started
            self.slots.release()

    def append(self, rows: list[tuple], final: bool = False) -> None:
        self.buffer.extend(rows)
        while len(self.buffer) >= self.rows or (final and self.buffer):
            # Reserve a bounded upload slot before creating another disk part.
            waiting = time.monotonic()
            self.slots.acquire()
            with self.lock:
                self.stats["backpressure_seconds"] += time.monotonic() - waiting
            started = time.monotonic()
            chunk = self.buffer[:self.rows]
            local = self.local / f"part-{self.number:05d}.parquet"
            try:
                with local.open("wb") as fh:
                    pq.write_table(pa.table(dict(zip(self.schema.names, zip(*chunk))), schema=self.schema), fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                self.pool.submit(self._upload, local, len(chunk))
            except Exception:
                self.slots.release()
                self.stop.set()
                raise
            self.buffer = self.buffer[len(chunk):]
            self.number += 1
            with self.lock:
                self.stats["parts_spooled"] += 1
                self.stats["spool_seconds"] += time.monotonic() - started

    def close(self) -> None:
        try:
            self.append([], final=True)
        finally:
            self.pool.shutdown(wait=True)
        if self.error is not None:
            raise RuntimeError(f"decision upload failed; complete local parts retained at {self.local}") from self.error
