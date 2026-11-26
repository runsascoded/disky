"""Periodic resource evidence that survives a worker's uncatchable SIGKILL."""
import ctypes
import ctypes.util
import sys
from contextvars import ContextVar
from functools import cache, wraps
from json import dumps
from pathlib import Path
from resource import RUSAGE_SELF, getrusage
from threading import Event, Lock, Thread
from time import monotonic

import pyarrow as pa

from .heartbeat import set_detail, set_stage
from .static_names import err


def memory_snapshot(
    status: Path = Path("/proc/self/status"),
    cgroup: Path = Path("/sys/fs/cgroup"),
) -> dict:
    """Linux process/cgroup counters, omitting unavailable files (e.g. macOS)."""
    out = {}
    if status.exists():
        fields = dict(line.split(":", 1) for line in status.read_text().splitlines() if ":" in line)
        for field, key in (("VmRSS", "rss_bytes"), ("VmHWM", "rss_peak_bytes"), ("RssAnon", "rss_anon_bytes"), ("RssFile", "rss_file_bytes")):
            if field in fields:
                out[key] = int(fields[field].split()[0]) * 1024
    for name, key in (("memory.current", "cgroup_current_bytes"), ("memory.max", "cgroup_limit_bytes"), ("memory.peak", "cgroup_peak_bytes")):
        path = cgroup / name
        if path.exists():
            value = path.read_text().strip()
            out[key] = None if value == "max" else int(value)
    events = cgroup / "memory.events"
    if events.exists():
        out["cgroup_events"] = {k: int(v) for k, v in (line.split() for line in events.read_text().splitlines())}
    # Older COS/Docker hosts expose the memory controller as cgroup v1.
    if not (cgroup / "memory.current").exists():
        for name, key in (("memory.usage_in_bytes", "cgroup_current_bytes"), ("memory.limit_in_bytes", "cgroup_limit_bytes"), ("memory.max_usage_in_bytes", "cgroup_peak_bytes"), ("memory.failcnt", "cgroup_limit_hits")):
            path = cgroup / "memory" / name
            if path.exists():
                out[key] = int(path.read_text().strip())
    return out


#: A stage's measured memory, logged on its own line only (`ResourceMonitor.update`), with every `*_bytes` field.
MEMORY_FIELDS = frozenset({"py_blocks", "duckdb_allocator", "duckdb_limit", "decoded_groups", "index_groups", "index_spans", "arrow_pool",
                           "footer_files", "footer_refetches", "footer_evictions", "meta_reparses", "meta_parse_factor", "index_reloads"})


def _memory_field(k: str) -> bool:
    return k.endswith("_bytes") or k in MEMORY_FIELDS


class _Mallinfo2(ctypes.Structure):
    _fields_ = [(name, ctypes.c_size_t) for name in
                ("arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks", "fsmblks", "uordblks", "fordblks", "keepcost")]


@cache
def _mallinfo2():
    """glibc's `mallinfo2` (2.33+), or None (macOS, musl, older glibc)."""
    try:
        fn = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6").mallinfo2
    except (OSError, AttributeError):
        return None
    fn.restype = _Mallinfo2
    return fn


def heap_snapshot() -> dict:
    """What the process's allocators hold, readable from any thread (specs/static-catch-up.md C): glibc's heap in use
    (`mallinfo2` `uordblks` + its mmapped chunks `hblkhd`) and free but kept (`fordblks`), pyarrow's live allocations, and
    CPython's allocated blocks (a count, not bytes: small objects such as the probes' dicts and sets, in pymalloc's own
    arenas, outside glibc's heap). The glibc fields are absent without `mallinfo2`."""
    out = {"arrow_bytes": pa.total_allocated_bytes(), "py_blocks": sys.getallocatedblocks()}
    if (fn := _mallinfo2()) is not None:
        m = fn()
        out |= {"glibc_used_bytes": m.uordblks + m.hblkhd, "glibc_free_bytes": m.fordblks}
    return out


def outside_bytes(anon: int | None, heap: dict, duckdb_jemalloc_bytes: int = 0) -> int | None:
    """Anonymous RSS no allocator here accounts for: `anon` less pyarrow's, glibc's (in use and free) and
    `duckdb_jemalloc_bytes` (DuckDB's buffer-managed bytes when DuckDB allocates through its own jemalloc, outside glibc;
    0 when it allocates through glibc, which already counts them). What's left: jemalloc's retained pages, DuckDB's
    allocations its buffer manager doesn't count, pymalloc's arenas, other mmaps. None without RSS or `mallinfo2`."""
    if not anon or "glibc_used_bytes" not in heap:
        return None
    return anon - heap["arrow_bytes"] - heap["glibc_used_bytes"] - heap["glibc_free_bytes"] - duckdb_jemalloc_bytes


class ResourceMonitor:
    def __init__(self, log=err, interval: float = 15, prefix: str = "DRILL_RESOURCE"):
        self.log, self.interval = log, interval
        self.prefix = prefix
        self.started = monotonic()
        self.lock = Lock()
        self.stop = Event()
        self.state = {"stage": "starting"}
        self.once: dict = {}
        self.thread = Thread(target=self._poll, name="resource-monitor", daemon=True)

    def sample(self) -> None:
        with self.lock:
            usage = getrusage(RUSAGE_SELF)
            snap, heap = memory_snapshot(), heap_snapshot()
            self.log(self.prefix + " " + dumps({
                **self.state, **snap, **heap,
                "elapsed_s": round(monotonic() - self.started, 1),
                "cpu_s": round(usage.ru_utime + usage.ru_stime, 1),
                # DuckDB isn't sampled here (never concurrently with SQL): with its jemalloc, its bytes are in this.
                "outside_glibc_arrow_bytes": outside_bytes(snap.get("rss_anon_bytes"), heap),
                # A stage's own measurement (`update`), as taken: its RSS and heap beside the DuckDB bytes they're read with.
                **self.once,
            }, sort_keys=True))

    def update(self, stage: str, **progress) -> None:
        """A new stage: its line carries `progress` whole; the periodic lines after it keep only its progress, not the
        memory it measured then (`*_bytes`, `MEMORY_FIELDS`), which would read as current beside fresh RSS."""
        with self.lock:
            self.state = {"stage": stage, **{k: v for k, v in progress.items() if not _memory_field(k)}}
            self.once = {k: v for k, v in progress.items() if _memory_field(k)}
        self.sample()
        with self.lock:
            self.once = {}

    def _poll(self) -> None:
        while not self.stop.wait(self.interval):
            self.sample()


_monitor: ContextVar[ResourceMonitor | None] = ContextVar("resource_monitor", default=None)


def resource_stage(stage: str, **progress) -> None:
    """The monitor's stage (its next line), and the process's (`heartbeat.set_stage`: its liveness line), monitored or not."""
    set_stage(stage)
    if shown := {k: v for k, v in progress.items() if not _memory_field(k)}:
        set_detail(" ".join(f"{k}={v}" for k, v in shown.items()))
    if monitor := _monitor.get():
        monitor.update(stage, **progress)


def monitor_resources(fn=None, *, prefix: str = "DRILL_RESOURCE"):
    if fn is None:
        return lambda wrapped: monitor_resources(wrapped, prefix=prefix)
    @wraps(fn)
    def wrapped(*args, **kwargs):
        monitor = ResourceMonitor(prefix=prefix)
        token = _monitor.set(monitor)
        monitor.sample()
        monitor.thread.start()
        try:
            result = fn(*args, **kwargs)
        except BaseException:
            monitor.update("failed")
            raise
        else:
            monitor.update("complete")
            return result
        finally:
            monitor.stop.set()
            monitor.thread.join()
            _monitor.reset(token)
    return wrapped
