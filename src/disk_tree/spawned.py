"""Process pools of spawned workers, each of which exits past the C runtime's teardown (`exit_hard`)."""
from __future__ import annotations

import atexit
import os
import sys
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context


def exit_hard() -> None:
    """For a spawned pool worker: at interpreter exit, end the process with `os._exit(0)` once the streams are flushed,
    ahead of the `atexit` handlers registered before this call and of the C runtime's exit handlers. pyarrow's global
    thread pool joins its threads in a static destructor there; on Python ≥ 3.13 a thread of it running a leftover
    dataset-scan task (`pq.read_table` over a Python buffer) frees that buffer, blocks forever taking the finalized
    interpreter's GIL (`PyThread_hang_thread`), and the worker never exits: the pool's `shutdown` waits on it (CI,
    2026-11-28: a `gdb` backtrace of `base_counts_parallel`'s worker). A pool worker's results are sent before it exits,
    its exit code isn't read (the pool watches its sentinel), and it writes no `multiprocessing` queue of its own (whose
    feeder `multiprocessing`'s own exit handler, skipped here, would flush)."""
    def bye() -> None:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    atexit.register(bye)


def _init(initializer: Callable[..., object] | None, initargs: tuple) -> None:
    exit_hard()
    if initializer is not None:
        initializer(*initargs)


def spawn_pool(workers: int, initializer: Callable[..., object] | None = None, initargs: tuple = ()) -> ProcessPoolExecutor:
    """A `ProcessPoolExecutor` of `workers` spawned processes, each `exit_hard` first, then `initializer(*initargs)`. Every
    process pool goes through here (`tests/test_spawned.py` reads the tree for any other)."""
    return ProcessPoolExecutor(workers, mp_context=get_context("spawn"), initializer=_init, initargs=(initializer, initargs))
