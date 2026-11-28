"""`spawned.exit_hard`: a spawned pool worker ends at interpreter exit ahead of the teardown registered before it (where
pyarrow's thread pool can hang the worker on Python ≥ 3.13), its results already returned."""
from __future__ import annotations

import atexit
import os
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest

from dt_cloud.spawned import exit_hard


def _init(marker: str, hard: bool) -> None:
    """Teardown registered first (it runs last, `atexit` being LIFO): a marker written at the worker's exit."""
    atexit.register(Path(marker).write_text, f"{os.getpid()}\n")
    if hard:
        exit_hard()


def _double(x: int) -> int:
    return 2 * x


@pytest.mark.parametrize("hard", [True, False])
def test_a_spawned_worker_exits_ahead_of_the_teardown_registered_before_it(tmp_path, hard):
    marker = tmp_path / "marker"
    with ProcessPoolExecutor(1, mp_context=get_context("spawn"), initializer=_init, initargs=(str(marker), hard)) as ex:
        got = ex.submit(_double, 21).result()
    assert (got, marker.exists()) == (42, not hard)
