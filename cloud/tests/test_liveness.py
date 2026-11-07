"""`dt_cloud.heartbeat`'s process liveness line (a Batch task's `[alive] …` every interval: stage, RSS, work disk, CPU)
and `Progress` (a long loop's rate-limited progress lines), on fake clocks."""
from __future__ import annotations

import pyarrow as pa
import pytest

from dt_cloud import heartbeat as H
from dt_cloud.heartbeat import Liveness, Progress, Tail

GiB = 1 << 30


class Clock:
    def __init__(self, t: float = 100.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def fresh_stage():
    H.set_stage(None)
    yield
    H.set_stage(None)


def test_liveness_beats_every_interval_with_the_stage_rss_disk_and_cpu():
    """Each beat (a fake `wait` advancing the clock one interval) prints the elapsed time, the current stage with its
    detail and time in it, RSS (current, peak), the work disk's used bytes and the interval's CPU%."""
    clock = Clock()
    cpu = Clock(0.0)
    lines: list[str] = []
    script = [
        # (CPU seconds this interval, what the process does meanwhile)
        (30.0, lambda: H.set_stage("drill/fold/short-roots", clock)),
        (480.0, lambda: H.set_detail("3/40")),
        (60.0, lambda: H.set_stage("drill/write/long/roots", clock)),
    ]
    beats = iter(script)

    def wait(s: float) -> bool:
        try:
            used, act = next(beats)
        except StopIteration:
            return True
        act()
        clock.t += s
        cpu.t += used
        return False

    live = Liveness(lines.append, 60.0, disk_path="/stage", clock=clock, wait=wait, cpu=cpu,
                    rss=lambda: (int(41.5 * GiB), 52 * GiB), disk=lambda: 310 * GiB)
    live.started = clock()
    live._last = (clock(), cpu())
    live._run()
    assert lines == [
        "[alive] 1m · drill/fold/short-roots (1m) · RSS 41.50 GiB (peak 52.00 GiB) · /stage 310.00 GiB used · CPU 50%",
        "[alive] 2m · drill/fold/short-roots 3/40 (2m) · RSS 41.50 GiB (peak 52.00 GiB) · /stage 310.00 GiB used · CPU 800%",
        "[alive] 3m · drill/write/long/roots (1m) · RSS 41.50 GiB (peak 52.00 GiB) · /stage 310.00 GiB used · CPU 100%",
    ]


def test_liveness_without_a_stage_or_current_rss_or_disk():
    clock = Clock()
    lines: list[str] = []
    live = Liveness(lines.append, 60.0, disk_path="/nowhere", clock=clock, cpu=lambda: 0.0, rss=lambda: (None, 2 * GiB), disk=lambda: None)
    live.started = clock()
    live._last = (clock(), 0.0)
    clock.t += 90
    live.tick()
    assert lines == ["[alive] 1m · - · RSS peak 2.00 GiB · CPU 0%"]


def test_tail_skips_liveness_lines():
    """A phase's note keeps its command's own latest line: the liveness lines it also prints are not it."""
    tail = Tail(Clock())
    tail.feed("drill/fold/short-roots 3/40: 1,000 rows in 2m")
    tail.feed("[alive] 3m · drill/fold/short-roots 3/40 (2m) · RSS peak 1.00 GiB · CPU 100%")
    assert tail.latest() == ("drill/fold/short-roots 3/40: 1,000 rows in 2m", None, 100.0)


@pytest.mark.parametrize("env, starts", [
    ({}, False),
    ({"BATCH_TASK_INDEX": "0"}, True),
    ({"BATCH_TASK_INDEX": "0", "DT_LIVENESS": "0"}, False),
    ({"BATCH_TASK_INDEX": "0", "DT_HEARTBEAT_S": "off"}, False),
    ({"BATCH_TASK_INDEX": "0", "DT_LIVENESS_PID": "1"}, False),
    ({"DT_LIVENESS": "1"}, True),
])
def test_start_liveness_on_batch_tasks_once_per_process_tree(monkeypatch, env, starts):
    started: list[Liveness] = []
    monkeypatch.setattr(H, "_liveness", None)
    monkeypatch.setattr(Liveness, "start", lambda self: started.append(self) or self)
    monkeypatch.setenv(H.LIVENESS_PID_ENV, "")  # undone after the test: `start_liveness` sets it for its children
    monkeypatch.delenv(H.LIVENESS_PID_ENV)
    got = H.start_liveness({**env, "DT_WORK_DISK": "/stage"})
    assert (got is not None, len(started)) == (starts, int(starts))
    if starts:
        assert (got.interval, got.disk_path) == (60.0, "/stage")
        assert H.start_liveness({"BATCH_TASK_INDEX": "0"}) is got
        assert len(started) == 1


def test_progress_rate_limits_to_one_line_per_interval_then_the_final():
    """Ticks inside an interval log nothing; the first tick past it logs `k/n`, bytes and the ETA from the bytes'
    fraction; `done()` always logs. The process's stage follows the loop."""
    clock = Clock(0.0)
    lines: list[str] = []
    p = Progress("upload index/g1", 4, total_bytes=4 * GiB, unit="files", log=lines.append, every_s=60.0, clock=clock)
    assert H.current_stage() == ("upload index/g1", "0/4", 0.0)
    for t in (10, 50, 70, 100):
        clock.t = t
        p.tick(nbytes=GiB)
    assert lines == ["upload index/g1 3/4 files, 3.00 GiB of 4.00 GiB in 1m (≈23s left)"]
    assert H.current_stage() == ("upload index/g1", "4/4", 0.0)
    p.done()
    assert lines[1:] == ["upload index/g1 4/4 files, 4.00 GiB of 4.00 GiB in 1m (done)"]
    assert H.current_stage() == ("after upload index/g1", None, 100.0)


def test_progress_counts_rows_through_batches_against_an_estimate():
    clock = Clock(0.0)
    lines: list[str] = []
    p = Progress("cut path", total_rows=1000, log=lines.append, every_s=60.0, clock=clock)
    batches = [pa.table({"x": list(range(n))}) for n in (100, 300, 100)]

    def timed():
        for b, t in zip(batches, (30, 61, 90)):
            clock.t = t
            yield b
    assert [b.num_rows for b in p.batches(timed())] == [100, 300, 100]
    assert lines == ["cut path, 400 of ≈1,000 rows in 1m (≈1m left)"]
    assert H.current_stage() == ("cut path", "500 rows", 0.0)
    p.done()
    assert lines[1:] == ["cut path, 500 of ≈1,000 rows in 1m (done)"]


def test_progress_of_a_short_loop_logs_nothing():
    clock = Clock(0.0)
    lines: list[str] = []
    p = Progress("mirror gs://b/g1", 2, total_bytes=10, unit="files", log=lines.append, every_s=60.0, clock=clock)
    for t in (5, 20):
        clock.t = t
        p.tick(nbytes=5)
    p.done()
    assert (lines, H.current_stage()) == ([], ("after mirror gs://b/g1", None, 20.0))


def test_fmt_bytes():
    assert [H.fmt_bytes(n) for n in (0, 512, 3 << 20, int(1.25 * GiB), int(2.1 * (1 << 40)))] == [
        "0 B", "512 B", "3.0 MiB", "1.25 GiB", "2.10 TiB",
    ]


def test_liveness_peak_is_the_trees_and_cpu_never_negative():
    """The tree's RSS past the process's own peak (a child's: cw's 28 GiB import under a 173.5 MiB wrapper) is the peak
    from then on; a CPU sum falling (a child gone between samples) reads 0%, and the next interval counts from the
    higher mark."""
    clock = Clock()
    lines: list[str] = []
    samples = iter([(20 * GiB, 100.0), (28 * GiB, 40.0), (10 * GiB, 160.0)])
    cur = {}

    def wait(s: float) -> bool:
        try:
            cur["rss"], cur["cpu"] = next(samples)
        except StopIteration:
            return True
        clock.t += s
        return False

    live = Liveness(lines.append, 60.0, disk_path="/stage", clock=clock, wait=wait, cpu=lambda: cur.get("cpu", 0.0),
                    rss=lambda: (cur.get("rss"), 200 << 20), disk=lambda: None)
    live.started = clock()
    live._last = (clock(), 0.0)
    live._run()
    assert lines == [
        "[alive] 1m · - · RSS 20.00 GiB (peak 20.00 GiB) · CPU 167%",
        "[alive] 2m · - · RSS 28.00 GiB (peak 28.00 GiB) · CPU 0%",
        "[alive] 3m · - · RSS 10.00 GiB (peak 28.00 GiB) · CPU 100%",
    ]
