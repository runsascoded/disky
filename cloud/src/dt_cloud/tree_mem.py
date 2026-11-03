"""One memory budget for a whole process tree (`-M`): a compaction's DuckDB limit is what the budget leaves once
everything else the tree holds is counted — this process's own pages, each spawned worker's, and the Arrow/Python
side's buffers, each bounded by construction (a stated number of row groups or batches × the bytes a row) — so `-M`
sizes the box, not just DuckDB:

    -M = base + procs × proc_base + arrow + duck

- `base`: this process's resident bytes when the budget is made (`own_bytes`: the interpreter, its imported libraries,
  whatever it already holds).
- `proc_base`: a spawned worker's own bytes before its task (`spawned_bytes`: one spawned as the cut's are — running this
  process's `__main__`, importing the cut's modules — reports its private pages; the shared libraries' pages are counted
  once, in `base`). `procs`: the workers alive at once.
- `arrow`: the phase's Arrow/Python buffers (`fold_arrow`, `cut_plan`): row groups and batches × `rb`, the inputs'
  largest estimated in-memory bytes a row (`interval_compact.row_bytes`: uncompressed bytes + `STRING_T` a string, at
  least what Arrow holds: a 4-byte offset and the bytes, a dictionary's values ≤ 12 bytes).
- `duck`: DuckDB's `memory_limit`, set per phase (`SET memory_limit`), at least `DUCK_MIN`; a budget that can't leave it
  that much is refused (`TooSmall`, naming the smallest `-M` that fits).

Not counted, because not a function of the settings but of the sort's size: the served sort's footer. The cut's join
(`parquet_pages.concat`) and its groups sidecar (`interval_store.write_groups`) hold a part's footer bytes (~120 B a
column chunk; pyarrow's parsed footer ~50 B) and decode it one row group at a time (`row_group_stream`), the sidecar
`GROUPS_RG` groups at a time: ~0.3 GiB at a billion rows (2.3M column chunks), where the decoded footer was ~40× that.

Freed Arrow memory goes back to the OS at once (`arrow_returns_freed`: jemalloc's decay off, in this process and every
worker), so a phase's peak is what it holds, not what its predecessors held."""
from __future__ import annotations

import resource
import sys
from dataclasses import dataclass
from functools import cache

import pyarrow as pa

MIB = 1 << 20
#: The least DuckDB limit a phase runs under: its sorts and folds spill beyond it, but each needs some memory of its own.
DUCK_MIN = 128 * MIB

# ── The fold (`interval_compact.compact_range`, per job) ───────────────────
#: Writers a range's fold keeps open at once (`pvl`, `pv`, `rd`; the owner and attribution passes open one each).
FOLD_WRITERS = 3
#: Row groups an idle open `static_names.SortedWriter` pins: its pending rows (< a group) are slices of at most one
#: fetched batch (≤ a group: `_batches(…, rg_rows)`).
WRITER_IDLE = 1
#: Row groups the writer being fed holds: pending slices pinning ≤ 2 fetched batches (the one a remainder was cut from,
#: the one being read), the group's combined copy, and its encoding.
WRITER_ACTIVE = 4
#: A fold job's row groups: one writer fed, the others idle.
FOLD_GROUPS = WRITER_ACTIVE + (FOLD_WRITERS - 1) * WRITER_IDLE

# ── The served sorts' cut (`interval_store.write_served`) ──────────────────
#: Batches a run's sort holds in this process (`_sort_run`): the fetched batch, and the spool writer's encoding of it.
SORT_COPIES = 2
#: `merge_rows` multiples a merging worker holds (`merge_seg` over `pyrmts.runs.merge_sorted`): across its inputs
#: (`merge_rows` rows a batch each, summed), each input's compressed spool group, its decoded batch and its buffered
#: rows; the frontier's sorted copy (≤ every buffer); the output spool's encoding of it.
MERGE_COPIES = 5
#: Spool groups a writing worker holds (`cut_part`): the compressed group being read and its decoded rows.
PART_SPOOL = 2
#: Served groups a writing worker holds: pending rows (≤ a group + a spool batch), the group's combined copy, its encoding.
PART_GROUPS = 3
#: The buffer a joining worker copies through (`parquet_pages._copy_range`, without `copy_file_range`).
CONCAT_BUF = 1 << 24
#: The share of `-M` past `base` the cut's workers (their `proc_base` and buffers) and its sorts' batches may take;
#: DuckDB's sorts get the rest. Workers only add speed: fewer of them, or smaller merge batches, still cut the same bytes.
WORKER_SHARE = 1 / 2
#: The merge batches tried (`merge_rows`): `interval_store.MERGE_ROWS` halved down to this floor (a 16-input pass at
#: `MERGE_MIN_BATCH` rows an input).
MERGE_FLOOR = 1 << 16


class TooSmall(SystemExit):
    """A budget that can't leave DuckDB `DUCK_MIN` under the phase's settings."""


def _status(field: str) -> int | None:
    """A `/proc/self/status`-style `kB` field's bytes (Linux), else None."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith(field + ":"):
                    return int(line.split()[1]) << 10
    except FileNotFoundError:  # legitimate: no procfs (macOS); callers fall back to `ru_maxrss`
        return None
    return None


def peak_bytes() -> int:
    """This process's peak resident bytes (`ru_maxrss`: bytes on macOS, KiB elsewhere)."""
    m = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return m if sys.platform == "darwin" else m << 10


def own_bytes() -> int:
    """This process's resident bytes now (Linux `VmRSS`; elsewhere its peak, an upper bound)."""
    got = _status("VmRSS")
    return got if got is not None else peak_bytes()


def private_bytes() -> int:
    """This process's private resident bytes now (Linux `smaps_rollup`'s `Private_Clean` + `Private_Dirty`: what a
    process with the same imports adds to a tree; elsewhere `own_bytes`, an upper bound)."""
    try:
        with open("/proc/self/smaps_rollup") as f:
            fields = {p[0]: int(p[1]) << 10 for p in (line.split() for line in f) if len(p) == 3 and p[0].endswith(":") and p[2] == "kB"}
    except FileNotFoundError:  # legitimate: no procfs (macOS)
        return own_bytes()
    return fields["Private_Clean:"] + fields["Private_Dirty:"]


def arrow_returns_freed() -> None:
    """Freed Arrow memory back to the OS at once: jemalloc (pyarrow's default pool on Linux) otherwise keeps freed pages
    for its decay time, so a phase's RSS would carry its predecessors' buffers. Other pools (mimalloc, the system's)
    have no such knob (`NotImplementedError`); theirs is left as is."""
    try:
        pa.jemalloc_set_decay_ms(0)
    except NotImplementedError:  # legitimate: pyarrow's pool isn't jemalloc here
        pass


def worker_init() -> None:
    """A spawned cut worker's initializer: one Arrow thread (the cores are split between the workers), freed memory
    returned (`arrow_returns_freed`)."""
    pa.set_cpu_count(1)
    arrow_returns_freed()


def _spawned_private() -> int:
    import pyrmts.runs  # noqa: F401 — the merge's
    from . import interval_store, parquet_pages  # noqa: F401 — the cut's tasks'

    return private_bytes()


@cache
def spawned_bytes() -> int:
    """A spawned cut worker's private bytes once its modules are imported (`worker_init`'d, as `write_served`'s pool
    spawns them), measured once per process in a worker of its own."""
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    with ProcessPoolExecutor(1, mp_context=get_context("spawn"), initializer=worker_init) as ex:
        return ex.submit(_spawned_private).result()


def fold_arrow(jobs: int, rg_rows: int, rb: int) -> int:
    """The fold's Arrow bytes: `jobs` ranges at once, each `FOLD_GROUPS` row groups of `rg_rows` rows of `rb` bytes."""
    return max(1, jobs) * FOLD_GROUPS * rg_rows * rb


def merge_bytes(merge_rows: int, rb: int) -> int:
    """A merging worker's bytes (`MERGE_COPIES` × `merge_rows` rows)."""
    return MERGE_COPIES * merge_rows * rb


def part_bytes(spool_rows: int, merge_rows: int, rg_rows: int, rb: int) -> int:
    """A writing worker's bytes: `PART_SPOOL` spool groups (run spools of a merged sort are cut in groups of up to
    `merge_rows`; the others in `spool_rows`) and `PART_GROUPS` served groups."""
    return (PART_SPOOL * max(spool_rows, merge_rows) + PART_GROUPS * rg_rows) * rb


def worker_bytes(spool_rows: int, merge_rows: int, rg_rows: int, rb: int) -> int:
    """A cut worker's bytes: the most of its three tasks' (merge, write, join)."""
    return max(merge_bytes(merge_rows, rb), part_bytes(spool_rows, merge_rows, rg_rows, rb), CONCAT_BUF)


def sort_bytes(procs: int, spool_rows: int, merge_rows: int, rb: int) -> int:
    """One sort's run batches in this process: its `procs` runs at once, a merged sort's `merge_rows` rows across them
    (each `merge_rows // runs`), another's `spool_rows` each; `SORT_COPIES` each."""
    return SORT_COPIES * max(merge_rows, procs * spool_rows) * rb


@dataclass(frozen=True)
class CutPlan:
    """The served sorts' cut under a budget: `at_once` sorts, each over `procs` worker processes, merging `merge_rows`
    rows a pass and spooling `spool_rows`-row groups; `arrow` the Arrow bytes and `duck` DuckDB's limit that leaves."""

    at_once: int
    procs: int
    merge_rows: int
    spool_rows: int
    arrow: int
    duck: int


@dataclass(frozen=True)
class Budget:
    """`-M` (`total`) over the process tree, with this process's own bytes (`base`) and a worker's (`proc_base`) as
    measured when it was made (`Budget.measure`)."""

    total: int
    base: int
    proc_base: int

    @classmethod
    def measure(cls, total: int) -> Budget:
        arrow_returns_freed()
        return cls(total, own_bytes(), spawned_bytes())

    def duck(self, arrow: int, procs: int = 0, what: str = "") -> int:
        """DuckDB's limit: `total − base − procs × proc_base − arrow`; `TooSmall` below `DUCK_MIN`."""
        d = self.total - self.base - procs * self.proc_base - arrow
        if d < DUCK_MIN:
            need = self.total - d + DUCK_MIN
            raise TooSmall(f"memory budget {self.total / MIB:,.0f} MiB too small{f' for {what}' if what else ''}: this process "
                           f"{self.base / MIB:,.0f} MiB, {procs} workers × {self.proc_base / MIB:,.0f} MiB, Arrow buffers "
                           f"{arrow / MIB:,.0f} MiB leave DuckDB {d / MIB:,.0f} MiB (< {DUCK_MIN / MIB:,.0f}); needs ≥ {need / MIB:,.0f} MiB")
        return d

    def fold(self, jobs: int, rg_rows: int, rb: int, rows: int | None = None) -> tuple[int, int]:
        """The fold's `(arrow, duck)`: `fold_arrow(jobs, min(rg_rows, rows), rb)` (`rows`: the inputs', which no group
        exceeds), DuckDB the rest (no workers)."""
        a = fold_arrow(jobs, min(rg_rows, rows or rg_rows), rb)
        return a, self.duck(a, 0, f"{jobs} fold jobs")

    def cut(self, at_once: int, cores: int, rg_rows: int, rb: int, spool_rows: int, merge_max: int, rows: int | None = None) -> CutPlan:
        """The cut's plan: the most workers (≤ `cores // at_once` a sort), then the largest merge batch (`merge_max`
        halved down to `MERGE_FLOOR`), whose bytes — `at_once × (procs × (proc_base + worker_bytes) + sort_bytes)`, each
        row count charged at most `rows` (the sorts' rows: no batch, group or merge holds more) — fit `WORKER_SHARE` of
        `total − base`; none fitting: one worker at the floor. DuckDB gets the rest."""
        cap = (lambda r: min(r, rows)) if rows else (lambda r: r)  # noqa: E731
        room = (self.total - self.base) * WORKER_SHARE
        top = max(1, cores // max(1, at_once))
        sizes = [merge_max >> i for i in range(64) if merge_max >> i >= MERGE_FLOOR] or [merge_max]

        def cost(procs: int, mr: int) -> tuple[int, int]:
            sort = SORT_COPIES * cap(max(mr, procs * spool_rows)) * rb  # `sort_bytes`, capped
            arrow = at_once * (procs * worker_bytes(cap(spool_rows), cap(mr), cap(rg_rows), rb) + sort)
            return arrow, arrow + at_once * procs * self.proc_base

        procs, mr = 1, sizes[-1]
        for p in range(top, 0, -1):
            fit = next((r for r in sizes if cost(p, r)[1] <= room), None)
            if fit is not None:
                procs, mr = p, fit
                break
        arrow, _ = cost(procs, mr)
        return CutPlan(at_once, procs, mr, spool_rows, arrow, self.duck(arrow, at_once * procs, f"the cut ({at_once} sorts × {procs} workers)"))


def duck_sql(limit: int) -> str:
    """DuckDB's `SET memory_limit` to `limit` bytes (whole MiB, rounded down)."""
    return f"SET memory_limit='{limit // MIB}MiB'"
