"""What a local job's container runs use (specs/vm-pilot.md "Measuring a run"): each run's cgroup v2 counters, sampled by
the job's supervisor on the host while the container runs, and the per-task view of a scan head's run (its Nextflow
trace's tasks, each matched to the samples taken while it ran).

The container's cgroup (`scan.slice/docker-<id>.scope` under Docker's systemd driver) is found from the container's
first process (`/proc/<pid>/cgroup`). Every `interval_s` the sampler reads its `memory.current`, `memory.stat`'s `anon`
and `file`, `memory.peak`, and the parent slice's `memory.current`, appending a row to the job's timeline (`usage.tsv`);
at the end, the run's summary (`usage.jsonl`): wall time, exit, `memory.peak` (monotonic, so its last reading is the
run's high-water mark to within one interval: the cgroup goes when the container exits), the sampled maxima,
`memory.events`' `oom_kill` and `cpu.stat`'s CPU seconds.

`memory.peak` and `memory.current` count page cache, which a cgroup fills up to its limit whenever it reads or writes
more than that (a run that copies 14 GiB through an 8 GiB slice peaks at ~8 GiB), and reclaims before it OOM-kills:
`anon` is the memory a run can't give back.
"""
from __future__ import annotations

import csv
import json
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

#: The timeline's columns (`usage.tsv`, one row per sample).
COLUMNS = ("t", "run", "current", "anon", "file", "peak", "slice_current")
INTERVAL_S = 2.0


def _int(path: Path) -> int | None:
    try:
        return int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _keyed(path: Path) -> dict[str, int]:
    """A cgroup `key value` file (`memory.stat`, `memory.events`, `cpu.stat`)."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        k, _, v = line.partition(" ")
        if v.strip().isdigit():
            out[k] = int(v)
    return out


class Sampler:
    """One container run's cgroup, sampled (module docstring). `find()` returns the cgroup's directory once the container
    is running (None before, and after it has gone); `timeline` gets a row per sample."""

    def __init__(self, run: str, find: Callable[[], Path | None], timeline: Path, interval_s: float = INTERVAL_S,
                 clock: Callable[[], float] = time.time):
        self.run, self.find, self.timeline, self.interval_s, self.clock = run, find, timeline, interval_s, clock
        self.cgroup: Path | None = None
        self.samples = 0
        self.peak: int | None = None
        self.max: dict[str, int] = {}
        self.oom_kill = 0
        self.cpu_s: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample(self) -> bool:
        """One reading (a timeline row), when the cgroup is there; whether it was."""
        if self.cgroup is None:
            self.cgroup = self.find()
            if self.cgroup is None:
                return False
        cg = self.cgroup
        current = _int(cg / "memory.current")
        if current is None:  # the container has exited and its cgroup has gone
            return False
        stat = _keyed(cg / "memory.stat")
        peak = _int(cg / "memory.peak")
        slice_current = _int(cg.parent / "memory.current")
        row = {"t": f"{self.clock():.1f}", "run": self.run, "current": current, "anon": stat.get("anon"),
               "file": stat.get("file"), "peak": peak, "slice_current": slice_current}
        for k in ("current", "anon", "file", "slice_current"):
            if row[k] is not None:
                self.max[k] = max(self.max.get(k, 0), int(row[k]))
        if peak is not None:
            self.peak = max(self.peak or 0, peak)
        self.oom_kill = max(self.oom_kill, _keyed(cg / "memory.events").get("oom_kill", 0))
        if (usec := _keyed(cg / "cpu.stat").get("usage_usec")) is not None:
            self.cpu_s = usec / 1e6
        new = not self.timeline.exists()
        with open(self.timeline, "a", newline="") as f:
            w = csv.DictWriter(f, COLUMNS, delimiter="\t")
            if new:
                w.writeheader()
            w.writerow({k: "" if v is None else v for k, v in row.items()})
        self.samples += 1
        return True

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.sample()
            except Exception:  # noqa: BLE001 — a missed reading never fails the job
                pass

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"usage-{self.run}", daemon=True)
        self._thread.start()

    def stop(self) -> dict:
        """Stop sampling (a last reading first, when the cgroup is still there); the run's summary."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(self.interval_s + 5)
        try:
            self.sample()
        except Exception:  # noqa: BLE001 — as `_loop`
            pass
        return {"cgroup": str(self.cgroup) if self.cgroup else None, "samples": self.samples, "memory_peak": self.peak,
                "max_current": self.max.get("current"), "max_anon": self.max.get("anon"), "max_file": self.max.get("file"),
                "max_slice_current": self.max.get("slice_current"), "oom_kill": self.oom_kill, "cpu_s": self.cpu_s}


def cgroup_of_pid(pid: int, proc: Path = Path("/proc"), root: Path = Path("/sys/fs/cgroup")) -> Path | None:
    """A process's cgroup v2 directory (`0::<path>` in `/proc/<pid>/cgroup`); None when it has gone or isn't on v2."""
    try:
        text = (proc / str(pid) / "cgroup").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            return root / line[3:].lstrip("/")
    return None


# ── a scan head's run, per task ────────────────────────────────────────────

_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_SECONDS = {"ms": 1e-3, "s": 1, "m": 60, "h": 3600, "d": 86400}
_MEMORY = re.compile(r"\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)?\s*")
_BYTES = {None: 1, "B": 1, "KB": 1 << 10, "MB": 1 << 20, "GB": 1 << 30, "TB": 1 << 40}


def duration_s(text: str) -> float | None:
    """A Nextflow trace duration (`2m 22s`, `13.9s`, `850ms`, `1h 2m`) in seconds; None for `-` or empty."""
    parts = _DURATION.findall(text or "")
    return sum(float(n) * _SECONDS[u] for n, u in parts) if parts else None


def memory_bytes(text: str) -> int | None:
    """A Nextflow trace memory (`351.6 MB`, `1.3 GB`: binary units) in bytes; None for `-` or empty."""
    m = _MEMORY.fullmatch(text or "")
    return int(float(m.group(1)) * _BYTES[m.group(2)]) if m else None


def trace_time(text: str) -> float | None:
    """A Nextflow trace timestamp (`2026-11-28 08:40:12.345`, the head JVM's zone: a container's is UTC) as epoch
    seconds; None for `-` or empty."""
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
        except (TypeError, ValueError):
            continue
    return None


def read_timeline(path: Path) -> list[dict[str, float | str | None]]:
    with open(path, newline="") as f:
        return [{k: (v if k == "run" else (float(v) if v else None)) for k, v in row.items()}
                for row in csv.DictReader(f, delimiter="\t")]


def per_task(trace: Iterable[Mapping[str, str]], timeline: list[Mapping[str, float | str | None]]) -> list[dict]:
    """Each traced task with what its own trace says (wall time `realtime`, `peak_rss`, `%cpu`) and the maxima of the
    samples taken between its submit and its end (`submit` + `duration`): exact per task only when tasks ran one at a
    time (the box's `queue_size: 1`), else each window's maxima are its overlapping tasks' together."""
    out = []
    for row in trace:
        start = trace_time(row.get("submit", ""))
        dur = duration_s(row.get("duration", ""))
        end = start + dur if start is not None and dur is not None else None
        window = [s for s in timeline if start is not None and end is not None and start <= float(s["t"] or 0) <= end]

        def top(k: str) -> int | None:
            vals = [int(s[k]) for s in window if s.get(k) is not None]
            return max(vals) if vals else None

        out.append({"task": row.get("name"), "status": row.get("status"), "exit": row.get("exit"),
                    "start": start, "wall_s": duration_s(row.get("realtime", "")), "duration_s": dur,
                    "cpu_pct": row.get("%cpu"), "peak_rss": memory_bytes(row.get("peak_rss", "")),
                    "samples": len(window), "max_current": top("current"), "max_anon": top("anon"),
                    "max_file": top("file"), "max_slice_current": top("slice_current")})
    return out


def gib(n: int | float | None) -> str:
    return "-" if n is None else f"{n / (1 << 30):.2f}"


def table(tasks: list[dict], runs: list[dict], budget: int | None = None) -> str:
    """The per-task report as a TSV table, then the job's runs (wall time, the cgroup's `memory.peak`, OOM kills)."""
    head = ["task", "status", "wall_s", "peak_rss_gib", "max_anon_gib", "max_current_gib", "max_slice_gib", "cpu_pct", "samples"]
    lines = ["\t".join(head)]
    for t in tasks:
        lines.append("\t".join([str(t["task"]), str(t["status"]), "-" if t["wall_s"] is None else f"{t['wall_s']:.0f}",
                                gib(t["peak_rss"]), gib(t["max_anon"]), gib(t["max_current"]), gib(t["max_slice_current"]),
                                str(t["cpu_pct"]), str(t["samples"])]))
    for r in runs:
        cpu = "-" if r.get("cpu_s") is None else f"{r['cpu_s']:.0f}s"
        lines.append(f"# run {r.get('run')}: exit {r.get('exit')}, wall {r.get('wall_s') or 0:.0f}s, memory.peak "
                     f"{gib(r.get('memory_peak'))} GiB, max anon {gib(r.get('max_anon'))} GiB, max slice "
                     f"{gib(r.get('max_slice_current'))} GiB, oom_kill {r.get('oom_kill')}, cpu {cpu}"
                     + (f" (budget {gib(budget)} GiB)" if budget else ""))
    return "\n".join(lines) + "\n"


def read_runs(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
