"""The compaction lease (`append_runner.compaction_lease`, `Runner.compact_stage`, `Runner.append`): a running compaction
holds off appends to its generation whoever submitted it (the submitter writes the lease for its job, the job adopts and
renews it), a crashed one's lapses within `COORD_TTL_S`, and `BatchRunner`'s poll rides out a transient connection error."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from dt_cloud import append_runner as ar
from dt_cloud import interval_compact as ic
from dt_cloud import interval_store as ist
from dt_cloud.blobstore import Store

from test_append_runner import Clock
from test_interval_append import G1, NOW, Fake as IvFake
from test_interval_compact import DATES, RG, add, build, make_world


def _record(path) -> dict:
    """The lease record, its random `owner` normalized."""
    return {**json.loads(path.read_text()), "owner": "<owner>"}


def _write(path, holder: str, gen: str, at, ttl_s: int = ar.COORD_TTL_S) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"owner": "o", "holder": holder, "at": at.isoformat(), "ttl_s": ttl_s, "gen": gen}) + "\n")


def _one_scan(f: IvFake) -> None:
    f.keys[f"{G1}/manifests/2026-08-04.json"] = {"date": "2026-08-04", "runs": [
        {"key": "deltas/2026-08-04", "first": "2026-08-04", "last": "2026-08-04", "level": 0, "scans": ["2026-08-04"]}]}


# ── Appends wait while their generation is compacted ──────────────────────


def test_append_refuses_while_its_generation_is_compacted(tmp_path):
    """A live compaction lease naming the generation: `Busy` (exit 4) before any stage; one naming another generation,
    or one past its TTL (a crashed job's), doesn't hold the append."""
    lease = tmp_path / "compacting.lease.json"
    f = IvFake(["2026-08-04"])
    _write(lease, "iv-compact-x", "g1", NOW - timedelta(seconds=ar.COORD_TTL_S - 1))
    with pytest.raises(ar.Busy) as e:
        f.runner(compacting=lambda: ar.LocalLeaseFile(lease)).run("2026-08-04")
    assert (str(e.value), f.calls()) == (
        "generation g1 is being compacted by iv-compact-x (since 2026-08-05T00:47:04+00:00): the append waits for its successor", [])
    _write(lease, "iv-compact-x", "g0", NOW)
    assert f.runner(compacting=lambda: ar.LocalLeaseFile(lease), merge=False).run("2026-08-04") == ["2026-08-04"]

    g = IvFake(["2026-08-04"])
    _write(lease, "iv-compact-x", "g1", NOW - timedelta(seconds=ar.COORD_TTL_S + 1))
    assert g.runner(compacting=lambda: ar.LocalLeaseFile(lease), merge=False).run("2026-08-04") == ["2026-08-04"]


def test_an_appended_scans_rerun_also_waits(tmp_path):
    """A rerun of the newest scan (nothing to append) would still plan carries and a compaction: it waits too."""
    lease = tmp_path / "compacting.lease.json"
    f = IvFake(["2026-08-04"], compact_level=1)
    _one_scan(f)
    _write(lease, "iv-compact-x", "g1", NOW)
    with pytest.raises(ar.Busy):
        f.runner(compacting=lambda: ar.LocalLeaseFile(lease)).run("2026-08-04")
    assert f.calls() == []


# ── The submitter writes the lease for its job ────────────────────────────


def test_compact_stage_writes_the_lease_for_its_job_before_submitting_it(tmp_path):
    """The compaction stage's record names its job (`holder`, what the job adopts) and the generation, alive
    `COMPACT_START_S` while Batch schedules it; it outlives the submitter (`merge_wait` elapsed), so the next run waits."""
    lease = tmp_path / "compacting.lease.json"
    seen = []
    f = IvFake(["2026-08-03"], compact_level=1)
    _one_scan(f)
    run_job = f.run_job

    def submit(name, spec, wait=None):
        seen.append(_record(lease) if lease.exists() else None)
        run_job(name, spec, wait)
        raise ar.StillRunning(f"Batch job {name}: still running after {wait}s")

    f.run_job = submit
    r = f.runner(compacting=lambda: ar.LocalLeaseFile(lease))
    r.compact_now = True
    r.carries()
    name = f.jobs[0][0]
    want = {"owner": "<owner>", "holder": name, "at": NOW.isoformat(), "ttl_s": ar.COMPACT_START_S, "gen": "g1"}
    assert ([s for s, _ in f.calls()], seen, _record(lease)) == (["compact"], [want], want)
    with pytest.raises(ar.Busy):
        IvFake(["2026-08-05"]).runner(compacting=lambda: ar.LocalLeaseFile(lease)).run("2026-08-05")


def test_compact_stage_submits_nothing_while_another_compaction_holds_the_lease(tmp_path):
    lease = tmp_path / "compacting.lease.json"
    f = IvFake(["2026-08-03"], compact_level=1)
    _one_scan(f)
    _write(lease, "iv-compact-other", "g0", NOW)
    r = f.runner(compacting=lambda: ar.LocalLeaseFile(lease), merge_wait=None)
    r.compact_now = True
    r.carries()
    assert (f.calls(), [m for m in f.log if m.startswith("compact:")]) == (
        [], ["compact: the compaction lease is held by iv-compact-other (since 2026-08-05T01:02:03+00:00): not submitting another"])


# ── The job adopts, renews and releases it ────────────────────────────────


def test_the_job_adopts_its_submitters_record_and_refuses_another_live_one(tmp_path):
    lease = tmp_path / "l.json"
    clock = Clock()
    _write(lease, "job-c", "g", clock(), ttl_s=ar.COMPACT_START_S)
    with ar.compaction_lease(ar.LocalLeaseFile(lease), "g", holder="job-c", now=clock) as held:
        assert _record(lease) == {"owner": "<owner>", "holder": "job-c", "at": clock().isoformat(), "ttl_s": ar.COORD_TTL_S, "gen": "g"}
        with pytest.raises(ar.Busy) as e:
            with ar.compaction_lease(ar.LocalLeaseFile(lease), "g", holder="job-d", now=clock):
                pass
        assert str(e.value) == "the compaction lease is held by job-c (since 2026-10-22T12:00:00+00:00)"
        assert held.owner == json.loads(lease.read_text())["owner"]
    assert not lease.exists()
    # a crashed job's record lapses: the next compaction takes it over
    _write(lease, "job-c", "g", clock())
    clock.tick(ar.COORD_TTL_S + 1)
    with ar.compaction_lease(ar.LocalLeaseFile(lease), "g", holder="job-d", now=clock):
        assert _record(lease)["holder"] == "job-d"


def test_a_failed_renewal_is_retried_at_the_next_beat(tmp_path):
    """The compaction lease survives a transient error renewing it (the coordinator lease is lost on one)."""
    class Flaky(ar.LocalLeaseFile):
        fail = 1

        def replace(self, text, token):
            if self.fail:
                self.fail -= 1
                raise ConnectionResetError(54, "Connection reset by peer")
            return super().replace(text, token)

    clock, logs = Clock(), []
    lease = ar.CoordinatorLease(Flaky(tmp_path / "l.json"), holder="job-c", now=clock, extra={"gen": "g"}, tolerate_errors=True,
                                what="compaction lease", log=logs.append)
    lease.take()
    lease.renew()
    clock.tick(ar.HEARTBEAT_S)
    lease.renew()
    lease.check()
    assert (logs, json.loads((tmp_path / "l.json").read_text())["at"]) == (
        ["compaction lease: renewing it failed (retried at the next beat): ConnectionResetError: [Errno 54] Connection reset by peer"],
        "2026-10-22T12:05:00+00:00")


def test_run_compaction_holds_the_lease_through_the_fold(tmp_path, monkeypatch):
    """The `compact` job adopts the submitter's record (its `$DT_JOB_NAME`) before it reads the generation, holds it
    through the fold, and releases it at the end; another live holder's makes it refuse (`Busy`) before reading."""
    world = make_world(tmp_path / "scans", False)
    store_dir = tmp_path / "store"
    build(world, store_dir, tmp_path / "wb", "g", DATES[1])
    add(world, store_dir, tmp_path / "wa", "-t", DATES[2], "-C", "none")
    store = Store(str(store_dir))
    lease = tmp_path / "scratch" / ar.COMPACT_LEASE
    monkeypatch.setenv(ar.JOB_NAME_VAR, "iv-compact-1")
    _write(lease, "iv-compact-1", "g", NOW, ttl_s=ar.COMPACT_START_S)
    c = ic.compactor(RG)
    during = []

    def fold(*a, **kw):
        during.append({k: v for k, v in _record(lease).items() if k != "at"})
        return c.fold(*a, **kw)

    kw = dict(threads=2, mem="1GB", log=lambda m: None, now=0.0, force=True, policy=ar.NEVER)
    doc = ar.run_compaction(store, replace(c, fold=fold), "g", store_dir, tmp_path / "w1", lease=ar.LocalLeaseFile(lease), **kw)
    assert (doc["compact"]["gen"], during, lease.exists()) == (
        "g.c001", [{"owner": "<owner>", "holder": "iv-compact-1", "ttl_s": ar.COORD_TTL_S, "gen": "g"}], False)

    from datetime import datetime, timezone
    _write(lease, "iv-compact-0", "g.c001", datetime.now(timezone.utc))
    with pytest.raises(ar.Busy):
        ar.run_compaction(store, c, "g", store_dir, tmp_path / "w2", lease=ar.LocalLeaseFile(lease), **kw)
    assert ar.current(store, ist.PREFIX) == "g.c001"


# ── BatchRunner: a transient poll error is retried ────────────────────────


class HTTPError(OSError):
    def __init__(self, status: int):
        super().__init__(f"{status} error")
        self.response = type("R", (), {"status_code": status})()


def _poll(monkeypatch, outcomes: list) -> tuple[list, list]:
    from dt_cloud import batch, gcp

    sleeps, logs, it = [], [], iter(outcomes)

    def job(name, project, region):
        o = next(it)
        if isinstance(o, BaseException):
            raise o
        return {"status": {"state": o}}

    monkeypatch.setattr(batch, "submit_job", lambda spec, name, region: None)
    monkeypatch.setattr(gcp, "batch_job", job)
    monkeypatch.setattr(ar.time, "sleep", sleeps.append)
    return sleeps, logs


def test_batch_runner_retries_a_transient_poll_error(monkeypatch):
    sleeps, logs = _poll(monkeypatch, ["RUNNING", ConnectionResetError(54, "Connection reset by peer"), HTTPError(503), "RUNNING", "SUCCEEDED"])
    ar.BatchRunner(ar.Profile(region="r", project="p"), logs.append)("j", {})
    assert (sleeps, logs) == ([10, 10, 20, 20], [
        "submitted j",
        "j RUNNING ",
        "j: polling failed (ConnectionResetError: [Errno 54] Connection reset by peer): retry 1/5 in 10s",
        "j: polling failed (HTTPError: 503 error): retry 2/5 in 20s",
        "j RUNNING ",
        "j SUCCEEDED ",
    ])


def test_batch_runner_raises_a_permanent_or_persistent_poll_error(monkeypatch):
    _poll(monkeypatch, [HTTPError(404)])
    with pytest.raises(HTTPError):
        ar.BatchRunner(ar.Profile(region="r", project="p"), lambda m: None)("j", {})
    sleeps, _ = _poll(monkeypatch, [ConnectionResetError(54, "reset")] * 6)
    with pytest.raises(ConnectionResetError):
        ar.BatchRunner(ar.Profile(region="r", project="p"), lambda m: None)("j", {})
    assert sleeps == [10, 20, 40, 80, 120]
