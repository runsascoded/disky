"""Periodic resource evidence that survives a worker's uncatchable SIGKILL."""
from contextvars import ContextVar
from functools import wraps
from json import dumps
from pathlib import Path
from resource import RUSAGE_SELF, getrusage
from threading import Event, Lock, Thread
from time import monotonic

import pyarrow as pa

from .static_names import err


def memory_snapshot(
    status: Path = Path("/proc/self/status"),
    cgroup: Path = Path("/sys/fs/cgroup"),
) -> dict:
    """Linux process/cgroup counters, omitting unavailable files (e.g. macOS)."""
    out = {}
    if status.exists():
        fields = dict(line.split(":", 1) for line in status.read_text().splitlines() if ":" in line)
        for field, key in (("VmRSS", "rss_bytes"), ("VmHWM", "rss_peak_bytes")):
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


class ResourceMonitor:
    def __init__(self, log=err, interval: float = 15, prefix: str = "DRILL_RESOURCE"):
        self.log, self.interval = log, interval
        self.prefix = prefix
        self.started = monotonic()
        self.lock = Lock()
        self.stop = Event()
        self.state = {"stage": "starting"}
        self.thread = Thread(target=self._poll, name="resource-monitor", daemon=True)

    def sample(self) -> None:
        with self.lock:
            usage = getrusage(RUSAGE_SELF)
            self.log(self.prefix + " " + dumps({
                **self.state, **memory_snapshot(),
                "elapsed_s": round(monotonic() - self.started, 1),
                "cpu_s": round(usage.ru_utime + usage.ru_stime, 1),
                "arrow_bytes": pa.total_allocated_bytes(),
            }, sort_keys=True))

    def update(self, stage: str, **progress) -> None:
        with self.lock:
            self.state = {"stage": stage, **progress}
        self.sample()

    def _poll(self) -> None:
        while not self.stop.wait(self.interval):
            self.sample()


_monitor: ContextVar[ResourceMonitor | None] = ContextVar("resource_monitor", default=None)


def resource_stage(stage: str, **progress) -> None:
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
