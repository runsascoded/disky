"""A waited-on child job's state line (`JobWatch`), for the process waiting on it: its liveness line's detail
(`heartbeat.set_wait`) and its poll log line.

`<job> <STATE> for <time in it>[, <done>/<total> tasks done[, <n> failed]][: <its latest log line>[ (unchanged <age>)]]`,
or once it ends, `<job> <STATE> after <time waited>`. The time in a state counts from the first poll that saw it. The log
line is the job's newest output line that isn't its own `[alive]` line (`heartbeat.LIVE_TAG`), read through the job
backend's `logs` (Batch: one Cloud Logging `entries.list`) at most every `LOG_EVERY_S`, only while the job runs, never
failing the wait: after `LOG_FAILURES` failed reads in a row (no Logging viewer permission, say) it stops reading.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence

from .heartbeat import LIVE_TAG, clean_line, clip, fmt_secs, scrub

#: Seconds between a running job's log reads. Cloud Logging's `entries.list` is quota'd per project and shared with
#: every other reader (`dt-cloud job logs`, the console), and a stage can wait on several jobs at once.
LOG_EVERY_S = 120.0
#: The lines asked for per read: enough to find one that isn't an `[alive]` line.
LOG_LINES = 10
#: Failed log reads in a row after which a watch stops reading.
LOG_FAILURES = 3
#: A log line in a state line past this many characters is cut (`…`).
LOG_CHARS = 100
#: The states a job doesn't leave.
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "DELETION_IN_PROGRESS"})
#: A task's states that count it done.
DONE_TASKS = ("SUCCEEDED", "FAILED")


def task_counts(groups: Mapping) -> dict[str, int]:
    """A Batch job's `status.taskGroups` (`{group: {"counts": {state: n}}}`, the counts int64s as JSON strings) summed
    over its groups: `{state: n}`."""
    out: dict[str, int] = {}
    for g in (groups or {}).values():
        for k, v in (g.get("counts") or {}).items():
            out[k] = out.get(k, 0) + int(v)
    return out


def tasks_text(counts: Mapping[str, int]) -> str | None:
    """`<done>/<total> tasks done[, <n> failed]` for a job of several tasks; None for one task (its state says it all)
    or no counts."""
    total = sum(counts.values())
    if total <= 1:
        return None
    done = sum(counts.get(k, 0) for k in DONE_TASKS)
    failed = counts.get("FAILED", 0)
    return f"{done}/{total} tasks done" + (f", {failed} failed" if failed else "")


def last_line(lines: Sequence[str]) -> str | None:
    """The newest of `lines` (oldest first) that has text and isn't a liveness line."""
    for ln in reversed(lines):
        if (c := clean_line(ln)) and not c.startswith(LIVE_TAG):
            return c
    return None


class JobWatch:
    """One child job's state line, over its polls (`observe`). `logs()`: the job's newest output lines, oldest first
    (`JobBackend.logs`); None reads none. `clock` times the states and reads."""

    def __init__(self, name: str, *, logs: Callable[[], Sequence[str]] | None = None, log_every: float = LOG_EVERY_S,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] | None = None,
                 env: Mapping[str, str] | None = None):
        self.name, self.logs, self.log_every, self.clock, self.log = name, logs, log_every, clock, log
        self.env = os.environ if env is None else env
        self.started = self.read_at = clock()
        self.state: str | None = None
        self.since = self.started
        self.line: str | None = None
        self.line_at = self.started
        self.failures = 0

    def _read(self, now: float) -> None:
        self.read_at = now
        try:
            line = last_line(self.logs())
        except Exception as e:  # noqa: BLE001 — a log read never fails the wait
            self.failures += 1
            if self.failures == 1 and self.log is not None:
                self.log(f"{self.name}: reading its log failed ({type(e).__name__}: {e}); its state line goes on without it")
            if self.failures >= LOG_FAILURES:
                self.logs = None
            return
        self.failures = 0
        if line is not None and line != self.line:
            self.line, self.line_at = scrub(line, self.env), now

    def observe(self, state: str, counts: Mapping[str, int] | None = None, detail: str = "") -> str:
        """The line after a poll that read `state` (and the task `counts`; `detail`: a backend's note, shown when there
        are no counts)."""
        now = self.clock()
        if state != self.state:
            self.state, self.since = state, now
        if state in TERMINAL:
            text = f"{self.name} {state} after {fmt_secs(now - self.started)}"
            return text + (f" ({detail})" if detail and not counts else "")
        if state == "RUNNING" and self.logs is not None and now - self.read_at >= self.log_every:
            self._read(now)
        text = f"{self.name} {state} for {fmt_secs(now - self.since)}"
        if counts and (tasks := tasks_text(counts)):
            text += f", {tasks}"
        elif detail and not counts:
            text += f" ({detail})"
        if self.line is not None:
            age = now - self.line_at
            text += f": {clip(self.line, LOG_CHARS)}" + (f" (unchanged {fmt_secs(age)})" if age > 0 and age >= self.log_every else "")
        return text
