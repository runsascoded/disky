"""Spawned pool workers' exit."""
from __future__ import annotations

import atexit
import os
import sys


def exit_hard() -> None:
    """For a spawned pool worker's initializer: at interpreter exit, end the process with `os._exit(0)` once the streams
    are flushed, ahead of the `atexit` handlers registered before this call and of the C runtime's exit handlers.
    pyarrow's global thread pool joins its threads in a static destructor there; on Python ≥ 3.13 a thread of it running a
    leftover dataset-scan task (`pq.read_table` over a Python buffer) frees that buffer, blocks forever taking the
    finalized interpreter's GIL (`PyThread_hang_thread`), and the worker never exits: the pool's `shutdown` waits on it
    (CI, 2026-11-28: a `gdb` backtrace of `base_counts_parallel`'s worker). A worker's results are sent before it exits,
    and its exit code isn't read (the pool watches its sentinel)."""
    def bye() -> None:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    atexit.register(bye)
