"""`dt_cloud.heartbeat`: a running phase's live note (its command's latest line, the latest runner stage), written every
interval on a thread that stops with the command, never failing it; secrets scrubbed; output teed unchanged."""
from __future__ import annotations

import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from dt_cloud import heartbeat as H
from dt_cloud import scan_steps as S
from dt_cloud.heartbeat import Heartbeat, Tail, fmt_secs, interval_of, run_teed, scrub


class Clock:
    def __init__(self, t: float = 100.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_beats_every_interval_with_the_latest_line_and_stage_then_stops():
    """A fake `wait` advances the clock one interval per beat and feeds the command's next line: each note is the elapsed
    time, the latest stage (a runner's `<stage>: start`) and the latest non-empty line, aged once it is two beats old.
    `stop()` ends the thread: no write after it returns."""
    clock = Clock()
    tail = Tail(clock)
    notes: list[str] = []
    waits: list[float] = []
    blocked = threading.Event()
    lines = ["listing shard 1/4", "   ", "merge 3: drill/name/fold 2/7: start", "\x1b[1mnf-1 RUNNING\x1b[0m SUCCEEDED=3", None, None]

    def wait(s: float) -> bool:
        waits.append(s)
        if len(waits) > len(lines):
            blocked.set()
            return hb._stop.wait()
        clock.t += s
        if (ln := lines[len(waits) - 1]) is not None:
            tail.feed(ln)
        return False

    hb = Heartbeat(notes.append, 60, tail, clock=clock, wait=wait)
    hb.start()
    assert blocked.wait(5)
    hb.stop()
    assert (notes, waits, hb.thread.is_alive(), hb.beats, hb.failures) == ([
        "1m · listing shard 1/4",
        "2m · listing shard 1/4",
        "3m · merge 3: drill/name/fold 2/7: start",
        "4m · merge 3: drill/name/fold 2/7 · nf-1 RUNNING SUCCEEDED=3",
        "5m · merge 3: drill/name/fold 2/7 · nf-1 RUNNING SUCCEEDED=3",
        "6m · merge 3: drill/name/fold 2/7 · nf-1 RUNNING SUCCEEDED=3 (2m ago)",
    ], [60] * 7, False, 6, 0)


def test_a_real_interval_beats_until_stopped():
    tail = Tail()
    notes: list[str] = []
    enough = threading.Event()

    def write(note: str) -> None:
        notes.append(note)
        if len(notes) == 3:
            enough.set()

    tail.feed("one")
    with Heartbeat(write, 0.01, tail) as hb:
        assert enough.wait(5)
    n = len(notes)
    threading.Event().wait(0.1)  # ten more intervals: a live thread would have written again
    assert (hb.thread.is_alive(), notes[:3], len(notes)) == (False, ["0s · one"] * 3, n)


def test_a_failing_write_never_raises_and_warns_once_per_n():
    said: list[str] = []
    clock = Clock()

    def write(note: str) -> None:
        raise RuntimeError("D1 query failed (500): token=abc123")

    hb = Heartbeat(write, 60, None, clock=clock, log=said.append, warn_every=10, label="hb (fg)", scrub=scrub)
    for _ in range(21):
        hb.tick()
    assert (hb.beats, hb.failures, said) == (0, 21, [
        "hb (fg): write failed (1 so far; the work goes on): RuntimeError: D1 query failed (500): token=<redacted>",
        "hb (fg): write failed (11 so far; the work goes on): RuntimeError: D1 query failed (500): token=<redacted>",
        "hb (fg): write failed (21 so far; the work goes on): RuntimeError: D1 query failed (500): token=<redacted>",
    ])


def test_note_clips_long_lines_and_scrubs():
    clock = Clock()
    tail = Tail(clock)
    hb = Heartbeat(lambda n: None, 60, tail, clock=clock, scrub=lambda s: scrub(s, {"SITE_TOKEN": "s3cr3t-value"}))
    clock.t += 3725
    tail.feed(f"{'s' * 70}: start")
    tail.feed("x" * 200 + " s3cr3t-value")
    assert hb.note() == f"1h02m · {'s' * 59}… · {'x' * 119}…"
    tail.feed("using s3cr3t-value")
    assert hb.note() == f"1h02m · {'s' * 59}… · using <SITE_TOKEN>"


def test_scrub():
    text = ("auth Bearer abc.def; url https://u:pw@host/p?token=zzz&x=1 sig X-Amz-Signature=0a1b; "
            "key AKIA1234567890ABCDEFGHIJ1234567890ab; val cf-secret-value; scan 2026-10-22T0700 "
            "path gs://b/static-names-append-20261022-0700/x")
    env = {"CLOUDFLARE_API_TOKEN": "cf-secret-value", "SCAN_ID": "2026-10-22T0700", "R2_KEY": "short"}
    assert scrub(text, env, ["SITE_TOKEN"]) == (
        "auth Bearer <redacted> url https://<redacted>@host/p?token=<redacted>&x=1 sig X-Amz-Signature=<redacted>; "
        "key <redacted>; val <CLOUDFLARE_API_TOKEN>; scan 2026-10-22T0700 path gs://b/static-names-append-20261022-0700/x")


@pytest.mark.parametrize("v, expected", [(None, 60.0), ("", 60.0), ("30", 30.0), ("2m", 120.0), ("0.05", 0.05),
                                         ("0", None), ("off", None), ("soon", 60.0)])
def test_interval_of(v, expected):
    said: list[str] = []
    assert interval_of({} if v is None else {H.HEARTBEAT_ENV: v}, said.append) == expected
    assert said == (["heartbeat: $DT_HEARTBEAT_S='soon' is not a duration (N[s|m|h]); every 60s"] if v == "soon" else [])


def test_fmt_secs():
    assert [fmt_secs(s) for s in (-1, 0, 59, 60, 3599, 3600, 3725)] == ["0s", "0s", "59s", "1m", "59m", "1h00m", "1h02m"]


def test_tail_splits_carriage_returns_and_keeps_the_latest_line(capsys):
    """`run_teed` with a tail: stderr passes through unchanged, each `\\r` / `\\n` line is fed, and captured stdout is
    the command's (never the tail's)."""
    tail = Tail()
    r = run_teed(["sh", "-c", "echo out; printf 'step 1/3\\rstep 3/3\\n\\nlast line\\n' >&2"], stdout=subprocess.PIPE, tail=tail)
    assert (r.returncode, r.stdout, tail.latest()[:2], capsys.readouterr().err) == (
        0, "out\n", ("last line", None), "step 1/3\rstep 3/3\n\nlast line\n")


def test_run_teed_without_a_tail_is_subprocess_run():
    r = run_teed(["sh", "-c", "echo hi; exit 3"], stdout=subprocess.PIPE)
    assert (r.returncode, r.stdout) == (3, "hi\n")


def test_the_heads_poll_loop_beats_once_an_interval_unless_supervised(monkeypatch, tmp_path):
    """`scan head` run as the container's entrypoint (no `supervise -H`) beats its run from its 10 s poll loop: at once,
    then once each `$DT_HEARTBEAT_S`; under `supervise` (`$DT_SUPERVISE`) or with the heartbeat off, not at all."""
    from dt_cloud import scan_head, scan_runs

    beats: list[str] = []
    monkeypatch.setattr(scan_runs.JobRun, "beat", lambda self: beats.append(self.run_id))
    clock = Clock(0)
    beat = scan_head.head_beat({"TMPDIR": str(tmp_path), "DT_HEARTBEAT_S": "60"}, "run-1", clock)
    for t in (0, 10, 50, 60, 70, 119, 125):
        clock.t = t
        beat()
    assert beats == ["run-1"] * 3  # at 0, 60, 125
    assert [scan_head.head_beat(e, "run-1") for e in ({"DT_SUPERVISE": "/tmp/x.json"}, {"DT_HEARTBEAT_S": "off"})] == [None, None]


class Run:
    """A recorder whose heartbeat yields a `Tail` (as `JobRun.heartbeat` does), logging the order of events."""

    def __init__(self):
        self.events: list[tuple] = []
        self.tail = Tail()

    def begin(self, name: str) -> None:
        self.events.append(("begin", name))

    def end(self, name: str, rc: int = 0, **kw) -> int:
        self.events.append(("end", name, rc, self.tail.latest()[0]))
        return rc

    @contextmanager
    def heartbeat(self, phase: str | None, **kw):
        self.events.append(("beat", phase))
        try:
            yield self.tail
        finally:
            self.events.append(("beat stopped", phase))


def test_a_steps_commands_feed_its_phase_heartbeat(tmp_path):
    """`ctx.sh` inside `ctx.phase` runs through `run_teed` with the phase's tail; the heartbeat stops before the phase's
    end is written; a failing command still ends the phase failed (and no tail outlives the phase)."""
    run = Run()
    c = S.Ctx(profile=None, scan="2026-10-22T0700", gen="G", env={"PATH": "/usr/bin:/bin"}, work=Path(tmp_path), run=run)
    with c.phase("static-names"):
        c.sh(["sh", "-c", "echo 'merge 1: drill/a: start' >&2; echo 'nf-2 RUNNING' >&2"])
        assert c.tail is run.tail
    with pytest.raises(S.StepFailed), c.phase("index"):
        c.sh(["sh", "-c", "echo 'index: 3/9 buckets' >&2; exit 4"])
    assert (run.events, c.tail) == ([
        ("begin", "static-names"), ("beat", "static-names"), ("beat stopped", "static-names"), ("end", "static-names", 0, "nf-2 RUNNING"),
        ("begin", "index"), ("beat", "index"), ("beat stopped", "index"), ("end", "index", 4, "index: 3/9 buckets"),
    ], None)


def test_descendants_walk_proc_parents_recursively(tmp_path):
    """A fake `/proc`: 10 → 11, 12; 12 → 13 (its name holding `) ` and spaces); 20 is another tree; `self` and junk ignored."""
    def proc(pid, ppid, comm="python3"):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 1 1 0 -1 0 0 0 0 0 7 3 0 0 20 0 1 0\n")

    for pid, ppid in ((10, 1), (11, 10), (12, 10), (20, 1), (21, 20)):
        proc(pid, ppid)
    proc(13, 12, comm="spawn) main x")
    (tmp_path / "self").mkdir()
    (tmp_path / "99").mkdir()  # no stat: a process gone mid-walk
    assert sorted(H.descendants(10, str(tmp_path))) == [11, 12, 13]
    assert H.descendants(13, str(tmp_path)) == []
    assert H.descendants(10, str(tmp_path / "missing")) == []
