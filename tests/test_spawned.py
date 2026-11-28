"""`disk_tree.spawned`: a spawned pool's worker ends at interpreter exit ahead of the teardown registered before its
initializer (where pyarrow's thread pool can hang it on Python ≥ 3.13), and every process pool in the tree is one."""
from __future__ import annotations

import ast
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest

from disk_tree.spawned import spawn_pool

from spawned_marker import double, init

ROOT = Path(__file__).resolve().parents[1]
TREES = (ROOT / "src" / "disk_tree", ROOT / "cloud" / "src" / "dt_cloud")
#: Spawned processes that aren't a pool's: `bulk_adaptive`'s workers send their one result through a `multiprocessing`
#: queue, whose feeder `multiprocessing`'s exit handler flushes (`exit_hard` skips it), and the parent reads their exit
#: codes (`exit_hard`'s is always 0).
NOT_POOLS = [("src/disk_tree/find/bulk_adaptive.py", "get_context")]


@pytest.mark.parametrize("hard", [True, False])
def test_a_spawned_pools_worker_exits_ahead_of_the_teardown_registered_before_its_initializer(tmp_path, monkeypatch, hard):
    marker = tmp_path / "marker"
    monkeypatch.setenv("SPAWNED_MARKER", str(marker))
    pool = spawn_pool(1, init) if hard else ProcessPoolExecutor(1, mp_context=get_context("spawn"), initializer=init)
    with pool as ex:
        got = ex.submit(double, 21).result()
    assert (got, marker.exists()) == (42, not hard)


def constructions() -> list[tuple[str, str]]:
    """Every `ProcessPoolExecutor(…)` and `get_context(…)` call in the engine and `dt_cloud`, `(path, callee)`, but
    `spawn_pool`'s own."""
    found = []
    for tree in TREES:
        for f in sorted(tree.rglob("*.py")):
            rel = f.relative_to(ROOT).as_posix()
            if rel == "src/disk_tree/spawned.py":
                continue
            for node in ast.walk(ast.parse(f.read_text())):
                if isinstance(node, ast.Call):
                    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                    if name in ("ProcessPoolExecutor", "get_context"):
                        found.append((rel, name))
    return found


def test_every_process_pool_is_a_spawn_pool():
    """A pool built any other way skips `exit_hard`: its worker can hang at exit, and the pool's `shutdown` with it."""
    assert constructions() == NOT_POOLS
