"""`test_spawned`'s worker side. A spawned worker imports this module to unpickle its initializer, before the initializer
runs; with `$SPAWNED_MARKER` set, that import registers an `atexit` handler writing the worker's pid there, ahead of
anything the initializer registers (as `multiprocessing`'s own exit handler is)."""
from __future__ import annotations

import atexit
import os
from pathlib import Path

if marker := os.environ.get("SPAWNED_MARKER"):
    atexit.register(Path(marker).write_text, f"{os.getpid()}\n")


def init() -> None:
    """The pool's initializer: importing this module is its point."""


def double(x: int) -> int:
    return 2 * x
