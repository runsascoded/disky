"""Maintenance runs (a store's compaction, its merges) recorded as `scan_runs` rows, so `/scans` follows them beside the
scans: the same tables, sink and upserts as `scan-run` (`scan_runs.record_sql` over `D1Sink`), written in-process by
the Batch job itself.

- **Kinds** (`MAINT_KINDS`): `compact` (`append_runner.run_compaction`, either store) and `merge` (the deferred carries,
  `append_runner.merge_pending`). Store `primary`, no parent (each is its own row on `/scans`, not folded into the
  scan run that submitted it).
- **Run id**: the job's `$BATCH_JOB_UID` (as a scan's: `/scans`' logs link queries `labels.job_uid` by it). None (not
  a Batch job), or `$DT_RECORD=off`: nothing is recorded.
- **`scan`**: the newest scan the run covers (a compaction's: the newest scan it folds, or would fold; a merge's: the
  newest scan of the manifest it carries), so the row sorts beside the scan it follows. **`gen`**: `<store>/<gen>`
  (e.g. `interval-store/2026-10-09b.g2`), and a compaction's finish sets `<store>/<gen> → <new gen>`.
- **Status**: `running` from `start`, then `succeeded`, `failed` (its `error`, `failed_phase`) or `nop` (nothing due).
- **Phases**: each `begin` / `note` / `end` writes its row (`note` updates a running phase's: progress); a
  heartbeat thread rewrites the run row every `HEARTBEAT_S` (its `updated_ts`), so a running row that stops moving
  reads as stale on `/scans`.
- **Job facts**: `$DT_JOB_NAME`, `$DT_JOB_REGION`, `$DT_JOB_MACHINE`, `$DT_JOB_SPOT`, `$DT_JOB_IMAGE`, which the
  submitter sets in the spec (`append_runner.Runner.record_env`); the cost estimate from `$SCAN_RUNS_PROFILE`'s prices.

Non-fatal throughout: a failed write is logged and the job goes on; after `MAX_FAILURES` in a row the recorder stops
trying (a dead D1 never costs a multi-hour fold more than a few HTTP timeouts)."""
from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Protocol

from .heartbeat import set_stage

err = partial(print, file=sys.stderr)

MAINT_KINDS = ("compact", "merge")
HEARTBEAT_S = 60.0
#: Consecutive failed writes after which the recorder gives up (logged once).
MAX_FAILURES = 5
#: The job facts the submitter puts in the spec's env (`append_runner.Runner.record_env`) → the run row's columns.
FACT_ENV = {"job_name": "DT_JOB_NAME", "region": "DT_JOB_REGION", "machine": "DT_JOB_MACHINE", "spot": "DT_JOB_SPOT", "image": "DT_JOB_IMAGE",
            "parent": "DT_PARENT_RUN"}
#: A fan-out helper task's record (`task_recorder`): the run it's a child of on `/scans` is `DT_PARENT_RUN`'s (the
#: compaction's job), under the scan and generation its submitter names.
RECORD_SCAN_ENV, RECORD_GEN_ENV = "DT_RECORD_SCAN", "DT_RECORD_GEN"
#: What a recording job needs from its submitter's env, passed through as plain variables (ids, not secrets; the
#: token comes from Secret Manager: the profile's `record_secrets`).
PASS_ENV = ("D1_DB_ID", "CLOUDFLARE_ACCOUNT_ID", "SCAN_RUNS_PROFILE")
#: Env vars whose values never reach a record (an error's text).
SECRET_ENV = ("CLOUDFLARE_API_TOKEN", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


class Recorder(Protocol):
    """A maintenance run's record (`MaintRun`; `NullRecorder` records nothing; tests pass a fake)."""

    def start(self, scan: str, gen: str) -> None: ...

    def begin(self, phase: str) -> None: ...

    def note(self, phase: str, note: str) -> None: ...

    def end(self, phase: str, note: str | None = None, failed: bool = False) -> None: ...

    def finish(self, status: str, error: str | None = None, gen: str | None = None) -> None: ...


class NullRecorder:
    def start(self, scan: str, gen: str) -> None:
        pass

    def begin(self, phase: str) -> None:
        pass

    def note(self, phase: str, note: str) -> None:
        pass

    def end(self, phase: str, note: str | None = None, failed: bool = False) -> None:
        pass

    def finish(self, status: str, error: str | None = None, gen: str | None = None) -> None:
        pass


NULL = NullRecorder()


class Prefixed:
    """`rec` with every phase name prefixed (`<prefix><phase>`): a step's sub-phases (a merge's tiers under its `carry L<n>`)."""

    def __init__(self, rec: Recorder, prefix: str):
        self.rec, self.prefix = rec, prefix

    def start(self, scan: str, gen: str) -> None:
        self.rec.start(scan, gen)

    def begin(self, phase: str) -> None:
        self.rec.begin(self.prefix + phase)

    def note(self, phase: str, note: str) -> None:
        self.rec.note(self.prefix + phase, note)

    def end(self, phase: str, note: str | None = None, failed: bool = False) -> None:
        self.rec.end(self.prefix + phase, note, failed)

    def finish(self, status: str, error: str | None = None, gen: str | None = None) -> None:
        self.rec.finish(status, error, gen)


@dataclass
class Slot:
    """`phase()`'s handle: the note its phase ends with (set by the body)."""
    note: str | None = None


@contextmanager
def phase(rec: Recorder, name: str) -> Iterator[Slot]:
    """Phase `name` of `rec`'s run around the body: begun, then ended with the body's `slot.note`, or failed (its note
    the exception) when the body raises (re-raised)."""
    rec.begin(name)
    set_stage(name)
    slot = Slot()
    try:
        yield slot
    except BaseException as e:
        rec.end(name, f"failed: {type(e).__name__}: {e}" + (f"; {slot.note}" if slot.note else ""), failed=True)
        raise
    rec.end(name, slot.note)


class PhaseHook:
    """A long step's progress hook on `rec`'s run (a fold's `progress`): `hook(note)` notes the running phase (at most
    every `note_s`), `hook(note, next_phase)` ends it (its note + `suffix()`) and begins `next_phase`; `phase` is the
    running one, begun with `begin()`."""

    def __init__(self, rec: Recorder, first: str, *, note_s: float, suffix: Callable[[], str] = lambda: ""):
        self.rec, self.phase, self.note_s, self.suffix, self.noted = rec, first, note_s, suffix, 0.0

    def begin(self) -> None:
        self.rec.begin(self.phase)

    def __call__(self, note: str, next_phase: str | None = None) -> None:
        if next_phase is None:
            if time.monotonic() - self.noted >= self.note_s:
                self.noted = time.monotonic()
                self.rec.note(self.phase, note)
            return
        self.rec.end(self.phase, note + self.suffix())
        self.phase = next_phase
        self.rec.begin(next_phase)

    def end(self, note: str | None = None, *, failed: bool = False) -> None:
        self.rec.end(self.phase, note, failed=failed)


def gib(n: float | None) -> str:
    return "?" if n is None else f"{n / 2**30:.2f} GiB"


@dataclass
class MaintRun:
    """One maintenance run's record, written through `sink` (`scan_runs.Sink`) at `clock()` (epoch s)."""
    sink: object
    run_id: str
    kind: str
    store: str = "primary"
    facts: Mapping[str, object] = field(default_factory=dict)
    prices: object = None
    clock: Callable[[], float] = time.time
    heartbeat_s: float | None = HEARTBEAT_S
    log: Callable[[str], None] = err
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        from .scan_runs import Phase

        self._phases: dict[str, Phase] = {}
        self._scan: str | None = None
        self._gen: str | None = None
        self._started: int | None = None
        self._failed_phase: str | None = None
        self._failures = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._beat: threading.Thread | None = None

    def _now(self) -> int:
        return int(self.clock())

    def _run(self, status: str = "running", **kw):
        from .scan_runs import Run

        facts = {k: (int(v) if k == "spot" else v) for k, v in self.facts.items() if v is not None}
        return Run(run_id=self.run_id, scan=self._scan, kind=self.kind, status=status, store=self.store, source="job",
                   started_ts=self._started, gen=self._gen, **facts, **kw)

    def _write(self, run, phases=()) -> None:
        from .scan_runs import Record, record_sql

        with self._lock:
            if self._failures >= MAX_FAILURES or self._scan is None:
                return
            try:
                self.sink.execute(record_sql(Record(run, tuple(phases)), self._now()))
                self._failures = 0
            except Exception as e:  # noqa: BLE001 — a record never fails the job
                self._failures += 1
                self.log(f"{self.kind} run {self.run_id}: record failed ({type(e).__name__}: {e})"
                         + ("; not recording any more" if self._failures >= MAX_FAILURES else ""))

    def _phase(self, name: str, *, started: int | None = None, finished: int | None = None, status: str = "running", note: str | None = None):
        from .scan_runs import Phase

        old = self._phases.get(name)
        p = Phase(name, old.seq if old else len(self._phases), started if started is not None else (old.started_ts if old else self._now()),
                  finished, status, note if note is not None else (old.note if old else None))
        self._phases[name] = p
        return p

    def start(self, scan: str, gen: str) -> None:
        from .scan_id import check_scan_id

        self._scan, self._gen, self._started = check_scan_id(scan), gen, self._now()
        self._write(self._run())
        if self.heartbeat_s:
            self._beat = threading.Thread(target=self._heartbeat, daemon=True)
            self._beat.start()

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.heartbeat_s):
            self._write(self._run())

    def begin(self, phase: str) -> None:
        self._write(self._run(), [self._phase(phase, started=self._now())])

    def note(self, phase: str, note: str) -> None:
        self._write(self._run(), [self._phase(phase, note=note)])

    def end(self, phase: str, note: str | None = None, failed: bool = False) -> None:
        if failed:
            self._failed_phase = self._failed_phase or phase
        self._write(self._run(), [self._phase(phase, finished=self._now(), status="failed" if failed else "done", note=note)])

    def finish(self, status: str, error: str | None = None, gen: str | None = None) -> None:
        from .scan_runs import redact

        self._stop.set()
        if self._beat is not None:
            self._beat.join()
        now = self._now()
        if gen is not None:
            self._gen = gen
        # a phase left running (a raise between `begin` and `end` outside `phase()`) ends with the run
        left = [self._phase(n, finished=now, status="failed" if status == "failed" else "done", note=p.note)
                for n, p in list(self._phases.items()) if p.finished_ts is None]
        kw: dict = {"finished_ts": now, "cost_usd": self._cost(now)}
        if status == "failed":
            kw.update(error=redact(error or "failed", SECRET_ENV, self.env)[:2000],
                      failed_phase=self._failed_phase or (left[0].phase if len(left) == 1 else None))
        self._write(self._run(status, **kw), left)

    def _cost(self, now: int) -> float | None:
        from .scan_runs import cost

        machine = self.facts.get("machine")
        if self.prices is None or not machine or self._started is None:
            return None
        return cost(self.prices, str(machine), bool(int(self.facts.get("spot") or 0)), 1, now - self._started)


def task_recorder(kind: str, env: Mapping[str, str] | None = None, *, log: Callable[[str], None] = err) -> Recorder:
    """A fan-out helper task's recorder (`from_env`), started: one row per task (`<job uid>-<task index>`), a child of
    the compaction's run (`$DT_PARENT_RUN`), under `$DT_RECORD_SCAN` / `$DT_RECORD_GEN`; `NULL` when its submitter named
    no scan (or recording is off)."""
    env = dict(os.environ if env is None else env)
    if not (env.get(RECORD_SCAN_ENV) or "").strip():
        log(f"{kind}: not recorded on /scans (its submitter named no ${RECORD_SCAN_ENV})")
        return NULL
    if (uid := (env.get("BATCH_JOB_UID") or "").strip()):
        env["BATCH_JOB_UID"] = f"{uid}-{env.get('BATCH_TASK_INDEX', '0')}"
    rec = from_env(kind, env, log=log)
    rec.start(env[RECORD_SCAN_ENV].strip(), env.get(RECORD_GEN_ENV, "").strip())
    return rec


def from_env(kind: str, env: Mapping[str, str] | None = None, *, log: Callable[[str], None] = err) -> Recorder:
    """This job's recorder for a `kind` run: a `MaintRun` into the deployment's D1 (`scan_runs.D1Sink`: `$D1_DB_ID`,
    `$CLOUDFLARE_API_TOKEN`, `$CLOUDFLARE_ACCOUNT_ID`) under `$BATCH_JOB_UID`; `NULL` (logged) when any is missing or
    `$DT_RECORD` is off. Never raises."""
    from .scan_runs import RECORD_ENV, D1Sink, _off, load_profile

    env = os.environ if env is None else env
    run_id = (env.get("BATCH_JOB_UID") or "").strip()
    if not run_id or _off(env.get(RECORD_ENV)):
        log(f"{kind}: not recorded on /scans ({'$' + RECORD_ENV + ' off' if run_id else 'not a Batch job: no $BATCH_JOB_UID'})")
        return NULL
    missing = [v for v in ("D1_DB_ID", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID") if not (env.get(v) or "").strip()]
    if missing:
        log(f"{kind}: not recorded on /scans (no {', '.join('$' + v for v in missing)}: the submitter passes them, `record_secrets` the token)")
        return NULL
    try:
        sink = D1Sink(env["D1_DB_ID"].strip())
    except (Exception, SystemExit) as e:  # noqa: BLE001
        log(f"{kind}: not recorded on /scans ({type(e).__name__}: {e})")
        return NULL
    prices = None
    if (env.get("SCAN_RUNS_PROFILE") or "").strip():
        try:
            prices = load_profile(env["SCAN_RUNS_PROFILE"].strip())
        except (Exception, SystemExit) as e:  # noqa: BLE001 — the cost is a nicety
            log(f"{kind}: no cost estimate ({type(e).__name__}: {e})")
    facts = {k: v for k, var in FACT_ENV.items() if (v := (env.get(var) or "").strip())}
    log(f"{kind}: recording run {run_id} on /scans")
    return MaintRun(sink, run_id, kind, store=prices.store if prices is not None else "primary", facts=facts, prices=prices, log=log, env=dict(env))
