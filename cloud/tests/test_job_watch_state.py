"""`dt_cloud.job_watch`: a waited-on child job's state line (state and time in it, tasks done, its latest log line, read
at most every `log_every` s and never failing the wait), and `BatchRunner` / `batch.wait_jobs` making it the waiting
stage's liveness detail, on fake clocks and fake backends."""
from __future__ import annotations

import pytest

from dt_cloud import append_runner as ar
from dt_cloud import heartbeat as H
from dt_cloud.job_backend import JobStatus
from dt_cloud.job_watch import JobWatch, last_line, task_counts, tasks_text


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def fresh_stage(monkeypatch):
    monkeypatch.setattr(H, "_waits", {})
    H.set_stage(None)
    yield
    H.set_stage(None)


def test_task_counts_sum_a_jobs_groups_and_read_its_int64_strings():
    groups = {"g0": {"counts": {"RUNNING": "2", "SUCCEEDED": "5"}}, "g1": {"counts": {"SUCCEEDED": "1", "FAILED": "1"}}, "g2": {}}
    assert task_counts(groups) == {"RUNNING": 2, "SUCCEEDED": 6, "FAILED": 1}


@pytest.mark.parametrize("counts, text", [
    ({}, None),
    ({"RUNNING": 1}, None),
    ({"PENDING": 2, "RUNNING": 3, "SUCCEEDED": 3}, "3/8 tasks done"),
    ({"RUNNING": 1, "SUCCEEDED": 2, "FAILED": 1}, "3/4 tasks done, 1 failed"),
])
def test_tasks_text(counts, text):
    assert tasks_text(counts) == text


def test_last_line_skips_liveness_and_blank_lines():
    assert last_line(["fold 3/40 in 20m", "[alive] 9m · fold (9m) · CPU 800%", "   "]) == "fold 3/40 in 20m"
    assert last_line(["[alive] 1m · - · CPU 0%"]) is None


def test_a_watch_reads_the_log_only_while_running_and_at_most_every_interval():
    """The time in a state counts from the first poll that saw it; the log is read while the job runs, once `log_every`
    s have passed since the watch began or its last read; a line read again unchanged says for how long; the end says
    how long the wait took."""
    clock = Clock()
    reads: list[float] = []
    out = iter([
        ["2026-11-26T0603 drill: start", "[alive] 1m · drill (1m)"],
        ["drill/fold 3/40 in 4m (≈49m left)"],
        ["drill/fold 3/40 in 4m (≈49m left)", "[alive] 9m · drill/fold 3/40 (9m)"],
    ])

    def logs() -> list[str]:
        reads.append(clock.t)
        return next(out)

    w = JobWatch("sn-drill-2026-11-26-0603-061200", logs=logs, log_every=120, clock=clock, env={})
    seen = []
    for t, state, counts in [
        (1000, "QUEUED", {"PENDING": 4}),
        (1060, "SCHEDULED", {"PENDING": 4}),
        (1150, "RUNNING", {"RUNNING": 4}),
        (1180, "RUNNING", {"RUNNING": 4}),
        (1290, "RUNNING", {"RUNNING": 3, "SUCCEEDED": 1}),
        (1430, "RUNNING", {"RUNNING": 3, "SUCCEEDED": 1}),
        (2000, "SUCCEEDED", {"SUCCEEDED": 4}),
    ]:
        clock.t = t
        seen.append(w.observe(state, counts))
    assert (seen, reads) == ([
        "sn-drill-2026-11-26-0603-061200 QUEUED for 0s, 0/4 tasks done",
        "sn-drill-2026-11-26-0603-061200 SCHEDULED for 0s, 0/4 tasks done",
        "sn-drill-2026-11-26-0603-061200 RUNNING for 0s, 0/4 tasks done: 2026-11-26T0603 drill: start",
        "sn-drill-2026-11-26-0603-061200 RUNNING for 30s, 0/4 tasks done: 2026-11-26T0603 drill: start",
        "sn-drill-2026-11-26-0603-061200 RUNNING for 2m, 1/4 tasks done: drill/fold 3/40 in 4m (≈49m left)",
        "sn-drill-2026-11-26-0603-061200 RUNNING for 4m, 1/4 tasks done: drill/fold 3/40 in 4m (≈49m left) (unchanged 2m)",
        "sn-drill-2026-11-26-0603-061200 SUCCEEDED after 16m",
    ], [1150, 1290, 1430])


def test_a_failing_log_read_is_noted_once_and_given_up_after_three():
    clock = Clock()
    notes: list[str] = []
    calls: list[float] = []

    def logs() -> list[str]:
        calls.append(clock.t)
        raise PermissionError("403 logging.entries.list denied")

    w = JobWatch("j", logs=logs, log_every=60, clock=clock, log=notes.append, env={})
    lines = []
    for t in (1060, 1120, 1180, 1240, 1300):
        clock.t = t
        lines.append(w.observe("RUNNING"))
    assert (lines, calls, notes, w.logs) == (
        ["j RUNNING for 0s", "j RUNNING for 1m", "j RUNNING for 2m", "j RUNNING for 3m", "j RUNNING for 4m"],
        [1060, 1120, 1180],
        ["j: reading its log failed (PermissionError: 403 logging.entries.list denied); its state line goes on without it"],
        None,
    )


def test_a_backends_note_shows_where_it_counts_no_tasks():
    clock = Clock()
    w = JobWatch("j", clock=clock, env={})
    assert w.observe("RUNNING", None, "pid 12") == "j RUNNING for 0s (pid 12)"
    clock.t += 90
    assert w.observe("FAILED", None, "task 0: disky-j-0-0 exited 3") == "j FAILED after 1m (task 0: disky-j-0-0 exited 3)"


def test_a_log_line_is_scrubbed_and_clipped():
    w = JobWatch("j", logs=lambda: ["GET https://x/?token=abc123 " + "y" * 200], log_every=0, clock=Clock(), env={})
    assert w.observe("RUNNING") == "j RUNNING for 0s: GET https://x/?token=<redacted> " + "y" * 67 + "…"


class Backend:
    """A job backend whose `status` reads the next state; each poll's sleep records the stage's liveness detail."""
    what = "Batch job"

    def __init__(self, clock: Clock, states: list[tuple[str, dict]], logs: list[str]):
        self.clock, self.states, self._logs, self.submitted = clock, iter(states), logs, []

    def submit(self, spec, name):
        self.submitted.append(name)

    def status(self, name):
        state, tasks = next(self.states)
        return JobStatus(state, " ".join(f"{k}={v}" for k, v in sorted(tasks.items())), tasks)

    def logs(self, name, limit):
        return self._logs


def test_batch_runner_puts_the_waited_jobs_state_in_the_stage_detail(monkeypatch):
    """Each poll's line is logged and is the stage's detail (`waiting on …`) while the runner waits; the detail is gone
    once the job ends."""
    clock = Clock()
    details: list[str | None] = []

    def sleep(s: float) -> None:
        details.append(H.current_stage()[1])
        clock.t += 60

    monkeypatch.setattr(ar.time, "sleep", sleep)
    backend = Backend(clock, [("QUEUED", {"PENDING": 2}), ("RUNNING", {"RUNNING": 2}), ("RUNNING", {"RUNNING": 1, "SUCCEEDED": 1}),
                              ("SUCCEEDED", {"SUCCEEDED": 2})], ["anchors/rollup 5/9"])
    logs: list[str] = []
    H.set_stage("2026-11-26T0603 anchors")
    ar.BatchRunner(ar.Profile(region="r", project="p"), logs.append, backend=backend, log_every=120, clock=clock)("sn-anchors-x", {})
    assert (logs, details, H.current_stage()[:2]) == ([
        "submitted sn-anchors-x",
        "sn-anchors-x QUEUED for 0s, 0/2 tasks done",
        "sn-anchors-x RUNNING for 0s, 0/2 tasks done",
        "sn-anchors-x RUNNING for 1m, 1/2 tasks done: anchors/rollup 5/9",
        "sn-anchors-x SUCCEEDED after 3m",
    ], [
        "waiting on sn-anchors-x QUEUED for 0s, 0/2 tasks done",
        "waiting on sn-anchors-x RUNNING for 0s, 0/2 tasks done",
        "waiting on sn-anchors-x RUNNING for 1m, 1/2 tasks done: anchors/rollup 5/9",
    ], ("2026-11-26T0603 anchors", None))


def test_set_wait_joins_the_jobs_a_stage_waits_on_at_once():
    H.set_stage("2026-11-26T0603 drill (long ∥ short) ∥ anchors")
    H.set_wait("a", "sn-drill-long RUNNING for 20m")
    H.set_wait("b", "sn-anchors RUNNING for 19m, 3/8 tasks done")
    assert H.current_stage()[1] == "waiting on sn-drill-long RUNNING for 20m ∥ sn-anchors RUNNING for 19m, 3/8 tasks done"
    H.set_wait("a", None)
    assert H.current_stage()[1] == "waiting on sn-anchors RUNNING for 19m, 3/8 tasks done"
    H.set_wait("b", None)
    assert H.current_stage()[1] is None


def test_wait_jobs_logs_each_poll_and_clears_the_detail(monkeypatch):
    from dt_cloud import batch

    jobs = iter([{"status": {"state": "RUNNING", "taskGroups": {"g": {"counts": {"RUNNING": "1"}}}}},
                 {"status": {"state": "SUCCEEDED", "taskGroups": {"g": {"counts": {"SUCCEEDED": "1"}}}}}])
    monkeypatch.setattr(batch, "batch_job", lambda name, region: next(jobs))
    details: list[str | None] = []
    monkeypatch.setattr(batch.time, "sleep", lambda s: details.append(H.current_stage()[1]))
    logs: list[str] = []
    assert batch.wait_jobs([("scan-task-x", "us-east1")], interval=15, log=logs.append, clock=lambda: 0.0) == {"scan-task-x": "SUCCEEDED"}
    assert (logs, details, H.current_stage()[1]) == (
        ["[us-east1] scan-task-x RUNNING for 0s", "[us-east1] scan-task-x SUCCEEDED after 0s"],
        ["waiting on scan-task-x RUNNING for 0s"],
        None,
    )
