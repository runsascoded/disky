"""Maintenance runs on `/scans` (`maint_runs`): a compaction's record from `append_runner.run_compaction` (phases and
statuses when due, not due, failed), the recorder's rows in the D1 tables (`scan_runs`, `scan_run_phases`), its
`from_env` gate, and the env the runners give a merge / compaction job to record with (`Runner.record_env`)."""
from __future__ import annotations

import copy
import re
from dataclasses import replace

import pytest

from dt_cloud import append_runner as ar
from dt_cloud import interval_compact as ic
from dt_cloud import interval_store as ist
from dt_cloud import maint_runs as mr
from dt_cloud import scan_runs as sr
from dt_cloud.blobstore import Store

from test_interval_append import Fake as IvFake
from test_interval_compact import DATES, RG, add, build, make_world


class FakeRecorder:
    """Every call, in order: `(op, phase-or-scan, …)`; notes kept apart (`notes`), as they carry sizes and seconds."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.notes: dict[str, str | None] = {}

    def start(self, scan: str, gen: str) -> None:
        self.calls.append(("start", scan, gen))

    def begin(self, phase: str) -> None:
        self.calls.append(("begin", phase))

    def note(self, phase: str, note: str) -> None:
        self.calls.append(("note", phase))

    def end(self, phase: str, note: str | None = None, failed: bool = False) -> None:
        self.calls.append(("end", phase, "failed" if failed else "done"))
        self.notes[phase] = note

    def finish(self, status: str, error: str | None = None, gen: str | None = None) -> None:
        self.calls.append(("finish", status, error, gen))


def _phases(*names: str) -> list[tuple]:
    return [c for n in names for c in (("begin", n), ("end", n, "done"))]


@pytest.fixture
def interval_store(tmp_path):
    """An interval store dir (its own mount): generation `g` through DATES[1], two scans appended as runs."""
    world = make_world(tmp_path / "scans", False)
    store_dir = tmp_path / "store"
    build(world, store_dir, tmp_path / "wb", "g", DATES[1])
    for d in DATES[2:4]:
        add(world, store_dir, tmp_path / "wa", "-t", d, "-C", "none")
    return store_dir


def _norm(note: str | None, store_dir) -> str | None:
    """A note with its sizes, seconds and the store's tmp path abstracted."""
    if note is None:
        return None
    note = note.replace(str(store_dir), "<store>")
    note = re.sub(r"\d+\.\d+ GiB", "<GiB>", note)
    note = re.sub(r"(?<= in )\d+(?:\.\d+)?s\b", "<s>", note)
    return re.sub(r"\d[\d,]* (files|rows|versions|objects)", r"<n> \1", note)


def test_compaction_record_due(interval_store, tmp_path, monkeypatch):
    """Due: started on the generation's newest scan, then `read`, `due`, `fold` → `sorts` (the interval fold's hook moves
    it on), `upload`, `pointer`, `prune`; finished `succeeded`, its gen naming the new generation."""
    monkeypatch.setattr(ar, "NOTE_S", 1e12)  # no throttled progress notes: a deterministic call list
    rec = FakeRecorder()
    doc = ar.run_compaction(Store(str(interval_store)), ic.compactor(RG), "g", interval_store, tmp_path / "w",
                            policy=ar.parse_policy("x", "every:2s"), threads=2, mem="1GB", log=lambda m: None, rec=rec)
    assert doc["compact"]["gen"] == "g.c001"
    assert rec.calls == [
        ("start", DATES[3], "interval-store/g"),
        *_phases("read", "due", "fold", "sorts", "upload", "pointer", "prune"),
        ("finish", "succeeded", None, "interval-store/g → g.c001"),
    ]
    assert {k: _norm(v, interval_store) for k, v in rec.notes.items()} == {
        "read": "<n> files, <GiB> from <store> (mount) (in place)",
        "due": "every 2s: 2 scans past the base (1 runs: L1)",
        "fold": "3 ranges, <n> rows in <s>; work disk peak <GiB>, RSS peak <GiB>",
        "sorts": "4 scans; <n> rows; <n> versions; 3 sorts; work disk peak <GiB>, RSS peak <GiB>",
        "upload": "<n> files, <GiB> to <store>/interval-store/g.c001/ in <s>",
        "pointer": "g/successor.json → g.c001; CURRENT.json → g.c001",
        "prune": "g: <n> objects, <GiB> kept (0h old)",
    }


def test_compaction_record_superseded(interval_store, tmp_path, monkeypatch):
    """A scan appended while the generation was folded (`compact_store`'s guard): nothing published, so the run
    finishes `nop` (not `succeeded`, not `failed`), its gen naming the manifest that landed; no `prune`."""
    monkeypatch.setattr(ar, "NOTE_S", 1e12)
    world = make_world(tmp_path / "scans-race", False)
    ar.set_current(Store(str(interval_store)), ist.PREFIX, "g")
    c = ic.compactor(RG)

    def fold(*a, **kw):
        out = c.fold(*a, **kw)
        add(world, interval_store, tmp_path / "wr", "-t", DATES[4], "-C", "none")
        return out

    rec = FakeRecorder()
    doc = ar.run_compaction(Store(str(interval_store)), replace(c, fold=fold), "g", interval_store, tmp_path / "w",
                            policy=ar.parse_policy("x", "every:2s"), threads=2, mem="1GB", log=lambda m: None, rec=rec)
    sup = doc["compact"]["superseded"]
    assert (sup.startswith(f"manifests/{DATES[4]}"), doc["predecessors"]) == (True, [])
    assert rec.calls == [
        ("start", DATES[3], "interval-store/g"),
        *_phases("read", "due", "fold", "sorts", "upload", "pointer"),
        ("finish", "nop", None, f"interval-store/g (superseded: {sup}, not published)"),
    ]
    assert rec.notes["pointer"] == f"superseded: {sup} landed on g during the fold; g.c001 deleted, not published"


def test_compaction_record_not_due(interval_store, tmp_path):
    """Not due: `read` and `due` (its note says under what), then `nop`."""
    rec = FakeRecorder()
    doc = ar.run_compaction(Store(str(interval_store)), ic.compactor(RG), "g", interval_store, tmp_path / "w",
                            policy=ar.parse_policy("x", "every:3s"), threads=2, mem="1GB", log=lambda m: None, now=0.0, rec=rec)
    assert doc == {"gen": "g", "why": None, "compact": None, "predecessors": []}
    assert rec.calls == [("start", DATES[3], "interval-store/g"), *_phases("read", "due"), ("finish", "nop", None, None)]
    assert rec.notes["due"] == "not due under every:3s (L1)"


def test_compaction_record_failed(interval_store, tmp_path):
    """A fold that raises: its phase failed (the note the error), the run `failed` with it, the error re-raised."""
    c = ic.compactor(RG)

    def fold(*a, **kw):
        raise RuntimeError("disk full")

    rec = FakeRecorder()
    with pytest.raises(RuntimeError, match="^disk full$"):
        ar.run_compaction(Store(str(interval_store)), replace(c, fold=fold), "g", interval_store, tmp_path / "w",
                          policy=ar.NEVER, force=True, threads=2, mem="1GB", log=lambda m: None, now=0.0, rec=rec)
    assert rec.calls == [
        ("start", DATES[3], "interval-store/g"), *_phases("read", "due"),
        ("begin", "fold"), ("end", "fold", "failed"),
        ("finish", "failed", "RuntimeError: disk full", None),
    ]
    assert rec.notes["fold"] == "failed: RuntimeError: disk full"


# ── The recorder's rows ────────────────────────────────────────────────────


def _rows(sink: sr.SqliteSink) -> tuple[list[dict], list[dict]]:
    run_cols = "run_id, store, scan, kind, status, failed_phase, error, started_ts, finished_ts, job_name, region, machine, spot, image, cost_usd, gen, source, updated_ts"
    return (sink.query(f"SELECT {run_cols} FROM scan_runs"),
            sink.query("SELECT phase, seq, started_ts, finished_ts, status, note FROM scan_run_phases ORDER BY seq"))


def _run(tmp_path, t: list[int], **kw) -> tuple[mr.MaintRun, sr.SqliteSink]:
    """A recorder into a local D1 stand-in, its clock `t[0]`."""
    sink = sr.SqliteSink(tmp_path / "d1.sqlite")
    prices = sr.parse_profile({"prices": {"n2-highmem-32": 2.0}, "spot_factor": 0.5})
    return mr.MaintRun(sink, "uid-1", "compact", facts={"job_name": "iv-compact-x", "region": "us-east1", "machine": "n2-highmem-32", "spot": "0"},
                       prices=prices, clock=lambda: t[0], heartbeat_s=None, log=lambda m: None, **kw), sink


def test_maint_run_rows(tmp_path):
    """The run row (running, then succeeded with its cost over its wall time) and each phase's (running with its
    progress note, then done with its end note), written through the `scan-run` SQL."""
    t = [100]
    rec, sink = _run(tmp_path, t)
    rec.start("2026-10-20T0700", "interval-store/g")
    t[0] = 110
    rec.begin("fold")
    t[0] = 150
    rec.note("fold", "3/8 ranges")
    assert _rows(sink)[1] == [{"phase": "fold", "seq": 0, "started_ts": 110, "finished_ts": None, "status": "running", "note": "3/8 ranges"}]
    t[0] = 160
    rec.end("fold", "8 ranges")
    t[0] = 3700
    rec.finish("succeeded", gen="interval-store/g → g.c001")
    assert _rows(sink) == ([{
        "run_id": "uid-1", "store": "primary", "scan": "2026-10-20T0700", "kind": "compact", "status": "succeeded", "failed_phase": None,
        "error": None, "started_ts": 100, "finished_ts": 3700, "job_name": "iv-compact-x", "region": "us-east1", "machine": "n2-highmem-32",
        "spot": 0, "image": None, "cost_usd": 2.0, "gen": "interval-store/g → g.c001", "source": "job", "updated_ts": 3700,
    }], [{"phase": "fold", "seq": 0, "started_ts": 110, "finished_ts": 160, "status": "done", "note": "8 ranges"}])


def test_maint_run_failed_rows(tmp_path):
    """A failed phase names the run's `failed_phase`; a phase left running ends with the run; secrets are redacted."""
    t = [100]
    rec, sink = _run(tmp_path, t, env={"CLOUDFLARE_API_TOKEN": "tok123"})
    rec.start("2026-10-20T0700", "static-names/g")
    t[0] = 110
    rec.begin("upload")
    t[0] = 120
    rec.end("upload", "failed: OSError: no space", failed=True)
    t[0] = 130
    rec.begin("pointer")
    rec.finish("failed", error="OSError: bad token tok123")
    runs, phases = _rows(sink)
    assert [(r["status"], r["failed_phase"], r["error"], r["finished_ts"]) for r in runs] == [("failed", "upload", "OSError: bad token <CLOUDFLARE_API_TOKEN>", 130)]
    assert phases == [
        {"phase": "upload", "seq": 0, "started_ts": 110, "finished_ts": 120, "status": "failed", "note": "failed: OSError: no space"},
        {"phase": "pointer", "seq": 1, "started_ts": 130, "finished_ts": 130, "status": "failed", "note": None},
    ]


def test_maint_run_never_raises(tmp_path):
    """A sink that fails: each write logged, none raised; after `MAX_FAILURES` in a row the recorder stops writing."""
    class Dead:
        n = 0

        def execute(self, statements):
            Dead.n += 1
            raise OSError("D1 unreachable")

    logs: list[str] = []
    rec = mr.MaintRun(Dead(), "uid-1", "merge", clock=lambda: 100, heartbeat_s=None, log=logs.append)
    rec.start("2026-10-20T0700", "static-names/g")
    for i in range(8):
        rec.begin(f"carry L{i}")
    rec.finish("succeeded")
    assert (Dead.n, logs[-1]) == (mr.MAX_FAILURES, "merge run uid-1: record failed (OSError: D1 unreachable); not recording any more")


def test_heartbeat_moves_updated_ts(tmp_path):
    """The heartbeat rewrites the run row while it runs (its `updated_ts`: what `/scans` reads as alive)."""
    import threading

    sink = sr.SqliteSink(tmp_path / "d1.sqlite")
    sink.con = sr.sqlite3.connect(str(tmp_path / "d1.sqlite"), check_same_thread=False)
    sink.con.row_factory = sr.sqlite3.Row
    t = [100]
    beat = threading.Event()
    rec = mr.MaintRun(sink, "uid-1", "compact", clock=lambda: t[0], heartbeat_s=0.01, log=lambda m: None)
    orig = rec._write

    def write(run, phases=()):
        orig(run, phases)
        if t[0] == 200:
            beat.set()
    rec._write = write
    rec.start("2026-10-20T0700", "interval-store/g")
    t[0] = 200
    assert beat.wait(5)
    rec.finish("succeeded")
    assert sink.query("SELECT status, started_ts, updated_ts FROM scan_runs") == [{"status": "succeeded", "started_ts": 100, "updated_ts": 200}]


# ── `from_env`: recording only with the job's identity and the D1 credentials ──


@pytest.mark.parametrize("env, why", [
    ({}, "not a Batch job: no $BATCH_JOB_UID"),
    ({"BATCH_JOB_UID": "u", "DT_RECORD": "off"}, "$DT_RECORD off"),
    ({"BATCH_JOB_UID": "u", "D1_DB_ID": "db"}, "no $CLOUDFLARE_API_TOKEN, $CLOUDFLARE_ACCOUNT_ID: the submitter passes them, `record_secrets` the token"),
])
def test_from_env_unrecorded(env, why):
    logs: list[str] = []
    assert mr.from_env("compact", env, log=logs.append) is mr.NULL
    assert logs == [f"compact: not recorded on /scans ({why})"]


def test_from_env_records(monkeypatch):
    monkeypatch.setattr(sr, "D1Sink", lambda db: ("d1", db))
    logs: list[str] = []
    rec = mr.from_env("merge", {"BATCH_JOB_UID": "u", "D1_DB_ID": "db", "CLOUDFLARE_API_TOKEN": "t", "CLOUDFLARE_ACCOUNT_ID": "a",
                                "DT_JOB_NAME": "sn-merge-x", "DT_JOB_MACHINE": "n2-highmem-16", "DT_JOB_SPOT": "1"}, log=logs.append)
    assert (rec.sink, rec.run_id, rec.kind, rec.store, rec.facts, logs) == (
        ("d1", "db"), "u", "merge", "primary", {"job_name": "sn-merge-x", "machine": "n2-highmem-16", "spot": "1"}, ["merge: recording run u on /scans"])


# ── The submitter: a merge / compaction job's record env ───────────────────


def test_record_env():
    """The compaction job gets its job facts; with the profile's `record_secrets` and the submitter's D1 ids, those
    (and the scan-runs profile, for the cost) plus the token from Secret Manager."""
    f = IvFake(["2026-08-03"], compact_level=1, record_secrets={"CLOUDFLARE_API_TOKEN": "cf-tok"})
    r = f.runner(merge_wait=None, environ={"D1_DB_ID": "db", "CLOUDFLARE_ACCOUNT_ID": "acct", "SCAN_RUNS_PROFILE": "/app/job/scan-runs.json",
                                           "CLOUDFLARE_API_TOKEN": "never-copied"})
    name, spec = r.compact_jobs("2026-08-04")[0]
    before = copy.deepcopy(spec["taskGroups"][0]["taskSpec"]["environment"])
    got = r.record_env(name, spec)["taskGroups"][0]["taskSpec"]["environment"]
    assert got == {
        **before,
        "variables": {**before["variables"], "DT_JOB_NAME": name, "DT_JOB_REGION": "us-east1", "DT_JOB_MACHINE": "n2-highmem-16",
                      "DT_JOB_SPOT": "0", "DT_JOB_IMAGE": "img", "D1_DB_ID": "db", "CLOUDFLARE_ACCOUNT_ID": "acct",
                      "SCAN_RUNS_PROFILE": "/app/job/scan-runs.json"},
        "secretVariables": {**before.get("secretVariables", {}), "CLOUDFLARE_API_TOKEN": "projects/proj/secrets/cf-tok/versions/latest"},
    }


def test_record_env_without_secrets():
    """No `record_secrets`: the job facts only (the job logs that it runs unrecorded)."""
    f = IvFake(["2026-08-03"], compact_level=1)
    r = f.runner(merge_wait=None, environ={"D1_DB_ID": "db", "CLOUDFLARE_ACCOUNT_ID": "acct"})
    name, spec = r.compact_jobs("2026-08-04")[0]
    before = copy.deepcopy(spec["taskGroups"][0]["taskSpec"]["environment"])
    assert r.record_env(name, spec)["taskGroups"][0]["taskSpec"]["environment"] == {
        **before, "variables": {**before["variables"], "DT_JOB_NAME": name, "DT_JOB_REGION": "us-east1", "DT_JOB_MACHINE": "n2-highmem-16",
                                "DT_JOB_SPOT": "0", "DT_JOB_IMAGE": "img"}}


# ── A merge job's record (`merge_pending` + `finish_merge`) ────────────────


def test_merge_record(tmp_path):
    """A phase per merged run (`carry L<level>`, a repeat level numbered), then `prune`; `succeeded`. A second merge
    with nothing due: `prune` only, `nop`."""
    import json

    from test_compact_level import EIGHT, _stub_build

    store = ar.LocalRunStore(tmp_path / "gen", tmp_path / "scratch", gen="g")
    store.create("manifests/2026-01-08.json", json.dumps({"date": "2026-01-08", "runs": EIGHT}) + "\n")
    carry = ar.Carry(build=_stub_build, missing=lambda store, runs: [])
    pruned = {"runs": [{"key": "deltas/2026-01-01"}], "objects": 3, "bytes": 2**30, "deleted": 3}
    for calls in ([
        ("start", "2026-01-08", "g"), *_phases("carry L2", "carry L2 #2", "prune"), ("finish", "succeeded", None, None),
    ], [
        ("start", "2026-01-08", "g"), *_phases("prune"), ("finish", "nop", None, None),
    ]):
        rec = FakeRecorder()
        doc = ar.merge_pending(store, carry, tmp_path / "mount", tmp=tmp_path / "tmp", max_level=3, log=lambda m: None, rec=rec)
        assert ar.finish_merge(rec, doc, lambda: pruned) == pruned
        assert rec.calls == calls
    assert rec.notes == {"prune": "1 folded runs, 3 objects, 1.00 GiB deleted"}


def test_a_fan_out_helper_task_records_a_child_row_of_its_own(monkeypatch):
    """`task_recorder`: one row per task (`<job uid>-<task index>`), started under the scan and generation its submitter
    named; nothing recorded (`NULL`) when it named no scan."""
    got: list = []

    def fake_from_env(kind, env, *, log):
        got.append((kind, env["BATCH_JOB_UID"], env.get("DT_PARENT_RUN")))
        return rec

    rec = FakeRecorder()
    monkeypatch.setattr(mr, "from_env", fake_from_env)
    env = {"BATCH_JOB_UID": "job-u1", "BATCH_TASK_INDEX": "2", "DT_PARENT_RUN": "job-c0", mr.RECORD_SCAN_ENV: "2026-10-23T0702",
           mr.RECORD_GEN_ENV: "static-names/g"}
    assert mr.task_recorder("measure-shard", env, log=lambda m: None) is rec
    assert (got, rec.calls) == ([("measure-shard", "job-u1-2", "job-c0")], [("start", "2026-10-23T0702", "static-names/g")])
    assert mr.task_recorder("measure-shard", {"BATCH_JOB_UID": "job-u1"}, log=lambda m: None) is mr.NULL
