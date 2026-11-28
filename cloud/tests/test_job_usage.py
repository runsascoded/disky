"""`job_usage`: a local job's cgroup samples, and a scan head's run read back per task."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from dt_cloud import job_usage as U

GIB = 1 << 30


@pytest.mark.parametrize("text, seconds", [
    ("2m 22s", 142.0), ("13.9s", 13.9), ("850ms", 0.85), ("1h 2m", 3720.0), ("1d 1s", 86401.0), ("-", None), ("", None),
])
def test_duration_s(text, seconds):
    assert U.duration_s(text) == (pytest.approx(seconds) if seconds is not None else None)


@pytest.mark.parametrize("text, n", [
    ("351.6 MB", int(351.6 * (1 << 20))), ("1.3 GB", int(1.3 * GIB)), ("12 KB", 12 << 10), ("0", 0), ("-", None), ("", None),
])
def test_memory_bytes(text, n):
    assert U.memory_bytes(text) == n


def test_trace_time_is_utc():
    at = datetime(2026, 11, 28, 8, 40, 12, 345000, tzinfo=timezone.utc).timestamp()
    assert (U.trace_time("2026-11-28 08:40:12.345"), U.trace_time("2026-11-28 08:40:12"), U.trace_time("-")) == (
        pytest.approx(at), pytest.approx(at - 0.345), None)


def test_cgroup_of_pid(tmp_path):
    (tmp_path / "proc/42").mkdir(parents=True)
    (tmp_path / "proc/42/cgroup").write_text("0::/scan.slice/docker-abc.scope\n")
    assert (U.cgroup_of_pid(42, tmp_path / "proc", Path("/sys/fs/cgroup")), U.cgroup_of_pid(7, tmp_path / "proc")) == (
        Path("/sys/fs/cgroup/scan.slice/docker-abc.scope"), None)


def test_per_task_takes_each_tasks_window_of_samples():
    t0 = U.trace_time("2026-11-28 08:00:00.000")
    trace = [
        {"name": "list_source (r2://a)", "status": "COMPLETED", "exit": "0", "submit": "2026-11-28 08:00:00.000",
         "duration": "1m", "realtime": "50s", "%cpu": "190.0%", "peak_rss": "3 GB"},
        {"name": "index", "status": "COMPLETED", "exit": "0", "submit": "2026-11-28 08:01:10.000",
         "duration": "20s", "realtime": "15s", "%cpu": "300.0%", "peak_rss": "2 GB"},
        {"name": "post", "status": "CACHED", "exit": "0", "submit": "-", "duration": "-", "realtime": "-", "%cpu": "-",
         "peak_rss": "-"},
    ]

    def sample(dt: float, current: int, anon: int) -> dict:
        return {"t": t0 + dt, "run": "h", "current": current, "anon": anon, "file": current - anon, "peak": None,
                "slice_current": current + 1}

    timeline = [sample(10, 5, 3), sample(59, 7, 4), sample(65, 9, 1), sample(75, 6, 2), sample(80, 8, 5)]
    assert U.per_task(trace, timeline) == [
        {"task": "list_source (r2://a)", "status": "COMPLETED", "exit": "0", "start": t0, "wall_s": 50.0, "duration_s": 60.0,
         "cpu_pct": "190.0%", "peak_rss": 3 * GIB, "samples": 2, "max_current": 7, "max_anon": 4, "max_file": 3,
         "max_slice_current": 8},
        {"task": "index", "status": "COMPLETED", "exit": "0", "start": t0 + 70, "wall_s": 15.0, "duration_s": 20.0,
         "cpu_pct": "300.0%", "peak_rss": 2 * GIB, "samples": 2, "max_current": 8, "max_anon": 5, "max_file": 4,
         "max_slice_current": 9},
        {"task": "post", "status": "CACHED", "exit": "0", "start": None, "wall_s": None, "duration_s": None, "cpu_pct": "-",
         "peak_rss": None, "samples": 0, "max_current": None, "max_anon": None, "max_file": None, "max_slice_current": None},
    ]


def test_table():
    tasks = [{"task": "index", "status": "COMPLETED", "wall_s": 15.0, "peak_rss": 2 * GIB, "max_anon": GIB, "max_current": 3 * GIB,
              "max_slice_current": 4 * GIB, "cpu_pct": "300.0%", "samples": 2}]
    runs = [{"run": "disky-j-0-0", "exit": 0, "wall_s": 61.2, "memory_peak": 7 * GIB, "max_anon": 5 * GIB,
             "max_slice_current": 7.5 * GIB, "oom_kill": 0, "cpu_s": 120.4}]
    assert U.table(tasks, runs, 8 * GIB).splitlines() == [
        "task\tstatus\twall_s\tpeak_rss_gib\tmax_anon_gib\tmax_current_gib\tmax_slice_gib\tcpu_pct\tsamples",
        "index\tCOMPLETED\t15\t2.00\t1.00\t3.00\t4.00\t300.0%\t2",
        "# run disky-j-0-0: exit 0, wall 61s, memory.peak 7.00 GiB, max anon 5.00 GiB, max slice 7.50 GiB, oom_kill 0, "
        "cpu 120s (budget 8.00 GiB)",
    ]


def test_the_sampler_reads_until_the_cgroup_goes(tmp_path):
    cg = tmp_path / "scan.slice/docker-x.scope"
    cg.mkdir(parents=True)
    found: list[Path | None] = [None, cg]
    s = U.Sampler("r", lambda: found.pop(0) if found else cg, tmp_path / "usage.tsv", clock=lambda: 5.0)
    (cg / "memory.current").write_text("10\n")
    (cg / "memory.stat").write_text("anon 4\nfile 6\n")
    (cg / "memory.peak").write_text("12\n")
    (tmp_path / "scan.slice/memory.current").write_text("30\n")
    reads = [s.sample(), s.sample()]
    (cg / "memory.current").unlink()  # the container exited
    reads.append(s.sample())
    assert (reads, s.stop()) == ([False, True, False], {
        "cgroup": str(cg), "samples": 1, "memory_peak": 12, "max_current": 10, "max_anon": 4, "max_file": 6,
        "max_slice_current": 30, "oom_kill": 0, "cpu_s": None})
