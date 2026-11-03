"""A running phase's heartbeat: while its command runs, at most every `$DT_HEARTBEAT_S` (default 60 s) its run record
is touched — the run's `updated_ts` bumped, the phase's `note` set to a live progress line — so `/scans` tells a long
phase (static-names, path-index: an hour or two between phase boundaries) from a dead job.

- `Tail`: the latest non-empty line of the phase's command output (stdout unless captured, and stderr; forwarded to
  ours unchanged), and the latest stage line (`append_runner.Runner.stage`'s `<stage>: start`), so a nested runner's
  `merge …: drill/<kind>/fold k/n` shows while its Batch job polls.
- `Heartbeat`: a daemon thread calling `write(note)` every `interval` s until stopped; the note is
  `<elapsed> · [<stage> · ]<latest line>[ (<age> ago): two beats, and a minute, old]`, secrets scrubbed (`scrub`). Best-effort: a failed write is
  counted, logged once per `warn_every` failures, and never reaches the step.
- `run_teed`: `subprocess.run`'s shape (the scan steps' `sh_impl`), its output also fed to a `Tail`.
- `Liveness` / `start_liveness`: a Batch task's own `[alive] …` stderr line every interval (its stage, RSS, work disk,
  CPU), and `Progress`: a long loop's rate-limited progress lines (the section at the end).

The record writes themselves (`scan_runs.phase_beat_sql`, `JobRun.heartbeat`) live with the recorder; the run-level
beat between phases is `scan-run supervise -H` (`JobRun.beat`)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from functools import partial
from typing import IO

err = partial(print, file=sys.stderr)

HEARTBEAT_ENV = "DT_HEARTBEAT_S"
#: Seconds between a running phase's record touches (one small D1 request each).
HEARTBEAT_S = 60.0
#: A note's latest line past this many characters is cut (`…`); a stage name past `STAGE_CHARS`.
LINE_CHARS = 120
STAGE_CHARS = 60
#: One warning per this many failed writes (the first, then every N-th).
WARN_EVERY = 10
#: A stage line: `append_runner.Runner.stage`'s start marker.
STAGE_RE = re.compile(r"^(?P<stage>.+): start$")
#: How long a finished command's output readers are waited on (a grandchild may hold the pipe open past its exit).
PUMP_JOIN_S = 10.0
#: The pending partial line a reader keeps (a `\r` progress bar without a newline must not grow without bound).
PENDING_BYTES = 4096

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
#: Env vars whose names say they hold a secret: their values never reach a note.
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|WEBHOOK|(?:^|_)KEY(?:_ID)?$", re.IGNORECASE)
_SECRET_TEXT = (
    # `Authorization: Bearer …`, `bearer …`
    (re.compile(r"\b(bearer|basic)\s+\S+", re.IGNORECASE), r"\1 <redacted>"),
    # `token=…`, `api_key: …`, `X-Amz-Signature=…`
    (re.compile(r"\b([\w.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|access[_-]?key|signature|credentials?))(\s*[=:]\s*)[^\s&,;]+",
                re.IGNORECASE), r"\1\2<redacted>"),
    # `scheme://user:pass@host`
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://<redacted>@"),
    # a long opaque run of letters *and* digits (a key, a token, a signed URL's signature); paths and job names, split
    # by `/` and `-`, never match
    (re.compile(r"(?=[A-Za-z0-9+_=]*\d)(?=[A-Za-z0-9+_=]*[A-Za-z])[A-Za-z0-9+_=]{32,}"), "<redacted>"),
)


def interval_of(env: Mapping[str, str], log: Callable[[str], None] = err) -> float | None:
    """`$DT_HEARTBEAT_S` (N[s|m|h], default `HEARTBEAT_S`); None when `0` / `off` (no heartbeat). A malformed value
    is logged and the default used: a setting never fails the job."""
    v = (env.get(HEARTBEAT_ENV) or "").strip()
    if not v:
        return HEARTBEAT_S
    if v.lower() in ("0", "off", "false", "no"):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smh]?)", v)
    if not m or not float(m[1]):
        log(f"heartbeat: ${HEARTBEAT_ENV}={v!r} is not a duration (N[s|m|h]); every {HEARTBEAT_S:g}s")
        return HEARTBEAT_S
    return float(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[m[2]]


def scrub(text: str, env: Mapping[str, str] | None = None, names: Iterable[str] = ()) -> str:
    """`text` without secrets: each value of `names` and of every env var whose name says it holds one (`SECRET_NAME`)
    replaced by `<NAME>` (longest first; values under 6 characters are too common to replace), then anything shaped
    like a credential (`_SECRET_TEXT`) by `<redacted>`."""
    env = env or {}
    named = set(names) | {n for n in env if SECRET_NAME.search(n)}
    vals = sorted({(v, n) for n in named if len(v := (env.get(n) or "").strip()) >= 6}, key=lambda p: -len(p[0]))
    for v, n in vals:
        text = text.replace(v, f"<{n}>")
    for pat, rep in _SECRET_TEXT:
        text = pat.sub(rep, text)
    return text


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def fmt_secs(s: float) -> str:
    """`45s`, `12m`, `1h05m`."""
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def clean_line(text: str) -> str:
    """One output line as a note shows it: ANSI escapes and control characters gone, whitespace collapsed."""
    return " ".join(_CTRL.sub(" ", _ANSI.sub("", text)).split())


class Tail:
    """The latest non-empty line fed (and when, on `clock`), and the latest stage line's stage. Thread-safe: each
    piped stream's reader feeds it."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._line: str | None = None
        self._stage: str | None = None
        self._at: float | None = None

    def feed(self, text: str) -> None:
        line = clean_line(text)
        if not line or line.startswith(LIVE_TAG):
            return
        with self._lock:
            self._line, self._at = line, self.clock()
            if m := STAGE_RE.match(line):
                self._stage = m["stage"]

    def latest(self) -> tuple[str | None, str | None, float | None]:
        """(the latest line, the latest stage, when the line came)."""
        with self._lock:
            return self._line, self._stage, self._at

    def pump(self, src: IO[bytes], dst: IO | None) -> threading.Thread:
        """A daemon thread copying `src` to `dst` (bytes as they come: a `\\r` progress bar stays live) and feeding
        each line here, until EOF."""
        def run() -> None:
            pending = b""
            try:
                while chunk := (src.read1(65536) if hasattr(src, "read1") else src.read(65536)):
                    _write(dst, chunk)
                    *lines, pending = re.split(rb"[\r\n]", pending + chunk)
                    pending = pending[-PENDING_BYTES:]
                    for ln in lines:
                        self.feed(ln.decode(errors="replace"))
                self.feed(pending.decode(errors="replace"))
            except (OSError, ValueError):  # the pipe closed under us: the command is gone
                pass
            finally:
                try:
                    src.close()
                except OSError:
                    pass

        t = threading.Thread(target=run, daemon=True, name="heartbeat-tail")
        t.start()
        return t


def _write(dst: IO | None, chunk: bytes) -> None:
    if dst is None:
        return
    try:
        buf = getattr(dst, "buffer", None)
        if buf is not None:
            buf.write(chunk)
            buf.flush()
        else:
            dst.write(chunk.decode(errors="replace"))
            dst.flush()
    except (OSError, ValueError):
        pass


class Heartbeat:
    """`write(note)` every `interval` s on a daemon thread, from `start()` until `stop()` (which joins it; the thread
    never writes after `stop()` returns). `wait(s)` is the pause (default: the stop event's; a test passes a fake), and
    `clock` times the note's elapsed and line ages (from `start()`)."""

    def __init__(
        self,
        write: Callable[[str], None],
        interval: float,
        tail: Tail | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
        scrub: Callable[[str], str] = lambda s: s,
        log: Callable[[str], None] | None = None,
        warn_every: int = WARN_EVERY,
        label: str = "heartbeat",
    ):
        self.write, self.interval, self.tail = write, interval, tail
        self.clock, self.scrub, self.log, self.warn_every, self.label = clock, scrub, log, warn_every, label
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait
        self.started = clock()
        self.beats = 0
        self.failures = 0
        self.thread: threading.Thread | None = None

    def note(self) -> str:
        now = self.clock()
        parts = [fmt_secs(now - self.started)]
        line, stage, at = self.tail.latest() if self.tail else (None, None, None)
        if stage and line != f"{stage}: start":
            parts.append(clip(stage, STAGE_CHARS))
        if line:
            age = now - at if at is not None else 0
            parts.append(clip(line, LINE_CHARS) + (f" ({fmt_secs(age)} ago)" if age >= max(2 * self.interval, 60) else ""))
        return self.scrub(" · ".join(parts))

    def tick(self) -> None:
        """One beat: never raises."""
        try:
            self.write(self.note())
            self.beats += 1
        except (Exception, SystemExit) as e:  # noqa: BLE001 — a heartbeat never fails the step
            self.failures += 1
            if self.failures % self.warn_every == 1 or self.warn_every == 1:
                (self.log or err)(self.scrub(f"{self.label}: write failed ({self.failures} so far; the work goes on): {type(e).__name__}: {e}"))

    def _run(self) -> None:
        while not self._wait(self.interval):
            if self._stop.is_set():
                break
            self.tick()

    def start(self) -> Heartbeat:
        self.started = self.clock()
        self.thread = threading.Thread(target=self._run, daemon=True, name=self.label)
        self.thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self.thread is not None:
            self.thread.join()

    def __enter__(self) -> Heartbeat:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def run_teed(argv: Sequence[str], env: Mapping[str, str] | None = None, stdout: int | None = None, text: bool = True,
             tail: Tail | None = None) -> subprocess.CompletedProcess:
    """`subprocess.run(argv, env=env, stdout=stdout, text=text)`, and with a `tail`, the command's stderr (and its
    stdout unless captured: `stdout=PIPE`) piped through it on the way to ours."""
    if tail is None:
        return subprocess.run(list(argv), env=None if env is None else dict(env), stdout=stdout, text=text)
    capture = stdout == subprocess.PIPE
    p = subprocess.Popen(list(argv), env=None if env is None else dict(env), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pumps = [tail.pump(p.stderr, sys.stderr)]
    if not capture:
        pumps.append(tail.pump(p.stdout, sys.stdout))
    out = p.stdout.read() if capture else None
    rc = p.wait()
    for t in pumps:
        t.join(PUMP_JOIN_S)
    if capture and p.stdout is not None:
        p.stdout.close()
    return subprocess.CompletedProcess(list(argv), rc, out.decode(errors="replace") if out is not None and text else out, None)


def pipe_kwargs(tail: Tail | None) -> dict:
    """`Popen` kwargs piping a command's output for `tail` (none without one)."""
    return {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE} if tail is not None else {}


def pump_all(p: subprocess.Popen, tail: Tail | None) -> list[threading.Thread]:
    """`tail`'s readers of a `pipe_kwargs` command's stdout and stderr (none without a tail)."""
    if tail is None:
        return []
    return [tail.pump(p.stdout, sys.stdout), tail.pump(p.stderr, sys.stderr)]


def join_all(pumps: Sequence[threading.Thread]) -> None:
    for t in pumps:
        t.join(PUMP_JOIN_S)


# ── A process's liveness line and its loops' progress ──────────────────────
#
# A dt-cloud process running as a Batch task (`$BATCH_TASK_INDEX` set; `$DT_LIVENESS` `1` forces it, `0` disables it)
# prints `LIVE_TAG …` to stderr every `$DT_HEARTBEAT_S` (`Liveness`, started on import by `dt_cloud/__init__.py`): the
# current stage (`set_stage`: `append_runner.Runner.stage`, the builds' and folds' `step`/`done` markers, every
# `Progress`), how long it has been in it, RSS (and peak), the work disk's used bytes and CPU%. Stderr only: no output
# byte changes. A long loop's `Progress` logs `label k/n …` at most every `PROGRESS_S` (and a final line).

#: A liveness line's first token; a `Tail` skips such lines, so a phase's note keeps the command's own latest line.
LIVE_TAG = "[alive]"
LIVENESS_ENV = "DT_LIVENESS"
#: Set in the environment by the process that started a `Liveness`: its children (a spawn pool's workers, a nested
#: `dt-cloud`) don't start their own, so a pool doesn't print one line per worker per interval.
LIVENESS_PID_ENV = "DT_LIVENESS_PID"
#: The work disk whose used bytes the liveness line shows: `$DT_WORK_DISK`, else Batch's local SSD (`/stage`, where
#: `append_runner.job_spec` mounts it) when there is one, else the temp dir.
WORK_DISK_ENV = "DT_WORK_DISK"
BATCH_STAGE_DIR = "/stage"
#: Seconds between a loop's progress lines (`Progress`).
PROGRESS_S = 60.0
GiB = 1 << 30

_stage_lock = threading.Lock()
_stage: dict = {"label": None, "detail": None, "since": time.monotonic()}


def set_stage(label: str | None, clock: Callable[[], float] = time.monotonic) -> None:
    """The process's current stage (what its liveness line names), from now; clears the detail."""
    with _stage_lock:
        _stage.update(label=label, detail=None, since=clock())


def after(name: str, clock: Callable[[], float] = time.monotonic) -> None:
    """The stage once step `name` ended (a build's `step`/`done` markers, which log a step at its end): `after <name>`,
    so a silent stretch reads as the time since the last step logged."""
    set_stage(f"after {name}", clock)


def set_detail(detail: str | None) -> None:
    """The current stage's detail (a loop's `k/n`), its start time kept."""
    with _stage_lock:
        _stage["detail"] = detail


def current_stage() -> tuple[str | None, str | None, float]:
    """(label, detail, since)."""
    with _stage_lock:
        return _stage["label"], _stage["detail"], _stage["since"]


def fmt_bytes(n: float) -> str:
    """`512 B`, `3.0 MiB`, `1.25 GiB`, `2.10 TiB`."""
    for unit, k in (("TiB", 1 << 40), ("GiB", GiB), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if n >= k:
            return f"{n / k:.2f} {unit}" if unit in ("TiB", "GiB") else f"{n / k:.1f} {unit}"
    return f"{int(n)} B"


def rss_bytes() -> int | None:
    """This process's current RSS (Linux `/proc/self/statm`; None elsewhere)."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def descendants(pid: int, proc: str = "/proc") -> list[int]:
    """`pid`'s live descendant processes (Linux: each `/proc/<p>/stat`'s parent; `[]` elsewhere)."""
    try:
        entries = [e for e in os.listdir(proc) if e.isdigit()]
    except OSError:
        return []
    kids: dict[int, list[int]] = {}
    for e in entries:
        try:
            with open(f"{proc}/{e}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(e))
    out, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


def tree_cpu_seconds(proc: str = "/proc") -> float:
    """CPU seconds of this process, its exited (waited-for) children and its live descendants, each with the children it
    waited for (`cutime`/`cstime`: a worker's exited processes stay counted once their parent reaped them; without them
    the sum fell as they exited, cw's import read -1711%): a spawn pool's workers' included (`time.process_time` alone
    read 0% through a compaction's `drill/measure`, 12 processes busy)."""
    import resource

    total = time.process_time() + sum(getattr(resource.getrusage(resource.RUSAGE_CHILDREN), f) for f in ("ru_utime", "ru_stime"))
    tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    for c in descendants(os.getpid(), proc):
        try:
            with open(f"{proc}/{c}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            total += sum(int(f) for f in fields[11:15]) / tick
        except (OSError, ValueError, IndexError):
            continue
    return total


def tree_rss_bytes(proc: str = "/proc") -> int | None:
    """This process's current RSS plus its live descendants' (Linux; None elsewhere)."""
    own = rss_bytes()
    if own is None:
        return None
    page = os.sysconf("SC_PAGE_SIZE")
    for c in descendants(os.getpid(), proc):
        try:
            with open(f"{proc}/{c}/statm") as f:
                own += int(f.read().split()[1]) * page
        except (OSError, ValueError, IndexError):
            continue
    return own


def rss_peak_bytes() -> int:
    """This process's peak RSS so far (`ru_maxrss`: bytes on macOS, KiB on Linux)."""
    import resource

    m = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return m if sys.platform == "darwin" else m << 10


def work_disk(env: Mapping[str, str]) -> str:
    if v := (env.get(WORK_DISK_ENV) or "").strip():
        return v
    if os.path.isdir(BATCH_STAGE_DIR):
        return BATCH_STAGE_DIR
    import tempfile

    return tempfile.gettempdir()


def disk_used(path: str) -> int | None:
    try:
        return shutil.disk_usage(path).used
    except OSError:
        return None


class Liveness(Heartbeat):
    """`Heartbeat`'s thread writing `LIVE_TAG <elapsed> · <stage>[ <detail>] (<in stage>) · RSS … · <disk> … · CPU …%`
    (`-` for the stage, with no time, before any is set)
    (`write`: a stderr line). The probes (`rss() -> (current | None, peak)`, `disk() -> used | None`, `cpu()`: the
    CPU seconds of the process and its children, all threads: `tree_cpu_seconds`) and `stage()` are parameters, so a test
    fakes them; CPU% is over the interval since the previous line (past 100% on a busy multi-core sort or a pool). RSS
    is the process's and its live descendants' (`tree_rss_bytes`); the peak is the process's own."""

    def __init__(self, write: Callable[[str], None], interval: float, *, disk_path: str, clock: Callable[[], float] = time.monotonic,
                 wait: Callable[[float], bool] | None = None,
                 stage: Callable[[], tuple[str | None, str | None, float]] = current_stage,
                 rss: Callable[[], tuple[int | None, int]] = lambda: (tree_rss_bytes(), rss_peak_bytes()),
                 disk: Callable[[], int | None] | None = None,
                 cpu: Callable[[], float] = tree_cpu_seconds):
        super().__init__(write, interval, clock=clock, wait=wait, log=err, label="liveness")
        self.disk_path, self.stage, self.rss, self.cpu = disk_path, stage, rss, cpu
        self.disk = disk or (lambda: disk_used(disk_path))
        self._last = (clock(), cpu())
        self.peak = 0

    def start(self) -> Liveness:
        self._last = (self.clock(), self.cpu())
        super().start()
        return self

    def note(self) -> str:
        now, cpu = self.clock(), self.cpu()
        t0, c0 = self._last
        # a child exiting unreaped between two samples can still drop out of the tree's sum: never a negative interval
        cpu = max(cpu, c0)
        self._last = (now, cpu)
        label, detail, since = self.stage()
        # No stage set yet: `-`, with no time in it (the elapsed already says how long the process has run).
        stage = "-" if label is None else clip(label, STAGE_CHARS) + (f" {detail}" if detail else "") + f" ({fmt_secs(now - since)})"
        parts = [LIVE_TAG, fmt_secs(now - self.started), "·", stage]
        cur, peak = self.rss()
        # the tree's peak as sampled (the process's own `ru_maxrss` misses its children's: cw's 28 GiB import read 173.5 MiB)
        self.peak = max(self.peak, peak, cur or 0)
        parts += ["·", f"RSS {fmt_bytes(cur)} (peak {fmt_bytes(self.peak)})" if cur is not None else f"RSS peak {fmt_bytes(self.peak)}"]
        used = self.disk()
        if used is not None:
            parts += ["·", f"{self.disk_path} {fmt_bytes(used)} used"]
        parts += ["·", f"CPU {100 * (cpu - c0) / (now - t0):.0f}%" if now > t0 else "CPU -"]
        return " ".join(parts)


_liveness: Liveness | None = None


def start_liveness(env: Mapping[str, str] | None = None) -> Liveness | None:
    """This process's `Liveness`, started once: when it runs as a Batch task (`$BATCH_TASK_INDEX`) or `$DT_LIVENESS` is
    on, unless `$DT_LIVENESS` is off, the heartbeat is off (`$DT_HEARTBEAT_S=0`), or a parent process already beats
    (`$DT_LIVENESS_PID`). Never raises: liveness never fails a job."""
    global _liveness
    try:
        env = os.environ if env is None else env
        if _liveness is not None:
            return _liveness
        flag = (env.get(LIVENESS_ENV) or "").strip().lower()
        if flag in ("0", "off", "false", "no") or not (flag in ("1", "on", "true", "yes") or env.get("BATCH_TASK_INDEX") is not None):
            return None
        if (pid := env.get(LIVENESS_PID_ENV)) and pid != str(os.getpid()):
            return None
        interval = interval_of(env)
        if interval is None:
            return None
        os.environ[LIVENESS_PID_ENV] = str(os.getpid())
        _liveness = Liveness(lambda s: print(s, file=sys.stderr, flush=True), interval, disk_path=work_disk(env)).start()
        return _liveness
    except Exception as e:  # noqa: BLE001 — liveness never fails a job
        err(f"liveness: not started: {type(e).__name__}: {e}")
        return None


class Progress:
    """A long loop's progress lines: `<label> k/n[ <unit>][, rows][, bytes of total] in <elapsed> (≈<eta> left)`, at
    most one per `every_s` (`PROGRESS_S`), then the last (`done()`, once the loop has run that long). `total` counts items (`tick(n)`),
    `total_bytes` bytes (`tick(nbytes=)`), `total_rows` rows (`tick(rows=)`, an estimate): the ETA comes from the first
    known fraction of bytes, items, rows. Thread-safe (a pool's workers tick it). Sets the process's stage to `label`
    and its detail to `k/n` (or the rows so far): what the liveness line names."""

    def __init__(self, label: str, total: int | None = None, *, total_bytes: int | None = None, total_rows: int | None = None,
                 unit: str = "", log: Callable[[str], None] = err, every_s: float = PROGRESS_S,
                 clock: Callable[[], float] = time.monotonic, stage: bool = True):
        self.label, self.total, self.total_bytes, self.total_rows, self.unit = label, total, total_bytes, total_rows, unit
        self.log, self.every_s, self.clock, self.stage = log, every_s, clock, stage
        self.t0 = self.last = clock()
        self.n = self.rows = self.nbytes = 0
        self._lock = threading.Lock()
        if stage:
            set_stage(label, clock)
            set_detail(self._detail())

    def _detail(self) -> str | None:
        return f"{self.n}/{self.total}" if self.total is not None else f"{self.rows:,} rows" if self.rows else None

    def _frac(self) -> float | None:
        for done, tot in ((self.nbytes, self.total_bytes), (self.n, self.total), (self.rows, self.total_rows)):
            if tot:
                return done / tot
        return None

    def line(self, now: float, final: bool = False) -> str:
        s = now - self.t0
        head = f"{self.label} {self.n}/{self.total}" if self.total is not None else self.label
        bits = [head + (f" {self.unit}" if self.unit and self.total is not None else "")]
        if self.rows or self.total_rows:
            bits.append(f"{self.rows:,}" + (f" of ≈{self.total_rows:,}" if self.total_rows else "") + " rows")
        if self.nbytes or self.total_bytes:
            bits.append(fmt_bytes(self.nbytes) + (f" of {fmt_bytes(self.total_bytes)}" if self.total_bytes else ""))
        out = ", ".join(bits) + f" in {fmt_secs(s)}"
        if final:
            return out + " (done)"
        f = self._frac()
        return out + (f" (≈{fmt_secs(s / f * (1 - f))} left)" if f and f < 1 else "")

    def tick(self, n: int = 1, *, rows: int = 0, nbytes: int = 0) -> None:
        with self._lock:
            self.n, self.rows, self.nbytes = self.n + n, self.rows + rows, self.nbytes + nbytes
            if self.stage:
                set_detail(self._detail())
            now = self.clock()
            if now - self.last < self.every_s:
                return
            self.last = now
            msg = self.line(now)
        self.log(msg)

    def batches(self, it: Iterable) -> Iterator:
        """`it`'s items (Arrow batches / tables) passed through, each ticking its rows."""
        for b in it:
            self.tick(0, rows=b.num_rows)
            yield b

    def done(self) -> None:
        """The final line, when the loop ran for `every_s` or more (a short one stays quiet: it needed no progress)."""
        with self._lock:
            now = self.clock()
            msg = self.line(now, final=True) if now - self.t0 >= self.every_s else None
        if msg is not None:
            self.log(msg)
        if self.stage:
            after(self.label, self.clock)
