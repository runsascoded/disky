"""Candidate compactions (specs/candidate-compaction.md): a candidate's fold holds the compaction lease marked `candidate`
(another compaction waits, appends don't), a whole candidate is never folded again, and `Runner.adopt` catches it up
under its own coordinator lease, then promotes it under the source's, catching up again when a scan lands meanwhile."""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from dt_cloud import append_runner as ar
from dt_cloud.static_profile import Profile

from test_append_runner import Clock
from test_compact_level import _static_runner
from test_static_runner import GEN, _py

P = "p"
OLD, NEW = "g", "g.c001"
S1, S2, S3, S4 = "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"


def _run(d: str) -> dict:
    return {"key": f"deltas/{d}", "first": d, "last": d, "level": 0, "scans": [d]}


class Mini(ar.Runner):
    """`append_runner.Runner` over a dict store: an append (`one`) writes the scan's manifest, the promote job writes the
    source's `successor.json`."""
    store_prefix = P
    job_prefix = "t"

    def one(self, d: str) -> None:
        keys = self.manifests()
        runs = self.read_json(keys[-1])["runs"] if keys else []
        self.events.append(("append", self.p.gen, d))
        self.store[f"{self.root}/manifests/{d}.json"] = {"date": d, "runs": [*runs, _run(d)]}

    def with_gen(self, gen: str) -> Profile:
        return replace(self.cfg, gen=gen)

    def promote_jobs(self, scan: str, old: str, new: str) -> list[tuple[str, dict]]:
        return [(f"t-promote-{new}", {"labels": {"stage": "promote"}, "old": old, "new": new, "scan": scan})]


class World:
    """The store (`keys`: key → JSON), the published scans, and what ran (`events`): appends, lease takes and releases,
    jobs. `busy[prefix]`: how many more takes of that coordinator lease raise `Busy`; `on_busy`: run at each."""

    def __init__(self, base: list[str], old_runs: list[str]):
        self.keys: dict = {f"{P}/{g}/scans.json": {"scans": [{"id": s} for s in base]} for g in (OLD, NEW)}
        self.keys[f"{P}/{NEW}/{ar.CANDIDATE}"] = {"from": OLD}
        self.keys[f"{P}/{NEW}/{ar.COMPACTION}"] = {"from": OLD}
        for i, d in enumerate(old_runs):
            self.keys[f"{P}/{OLD}/manifests/{d}.json"] = {"date": d, "runs": [_run(x) for x in old_runs[:i + 1]]}
        self.published = [*base, *old_runs]
        self.events: list[tuple] = []
        self.busy: dict[str, int] = {}
        self.on_busy = lambda: None
        self.sleeps: list[float] = []

    def land(self, scan: str) -> None:
        """A scan's append to the source (as a scan's own step makes it)."""
        keys = sorted(k for k in self.keys if k.startswith(f"{P}/{OLD}/manifests/"))
        runs = self.keys[keys[-1]]["runs"] if keys else []
        self.keys[f"{P}/{OLD}/manifests/{scan}.json"] = {"date": scan, "runs": [*runs, _run(scan)]}
        self.published.append(scan)

    def runner(self, **kw) -> Mini:
        store = SimpleNamespace(exists=lambda k: k in self.keys, read_json=lambda k: self.keys[k])

        @contextmanager
        def lease():
            prefix = ar.coordinator_prefix(store, P, r.cfg.gen)
            if self.busy.get(prefix, 0) > 0:
                self.busy[prefix] -= 1
                self.events.append(("busy", prefix))
                self.on_busy()
                raise ar.Busy("the coordinator lease is held by scan-x", lease="coordinator")
            self.events.append(("lock", prefix))
            try:
                yield lambda: None
            finally:
                self.events.append(("unlock", prefix))

        def run_job(name: str, spec: dict, wait: float | None = None) -> None:
            self.events.append(("job", name, wait))
            self.keys[f"{P}/{spec['old']}/{ar.SUCCESSOR}"] = {"gen": spec["new"]}

        cfg = Profile(name="t", layouts=("x/{id}",), gen=OLD, project="proj", region="r", image="img", sa="sa", bucket="b", scratch="s", hex_runs="off")
        r = Mini(cfg=cfg, exists=lambda k: k in self.keys, count=lambda p, s: 0, read_json=lambda k: self.keys[k],
                 published=lambda layouts, start: [s for s in self.published if s >= start], run_job=run_job, prepare=lambda d: None,
                 prune=lambda d: None, list_keys=lambda prefix: [k for k in self.keys if k.startswith(prefix)], merge=False, log=lambda m: None,
                 coordination=lease, **kw)
        r.store, r.events = self.keys, self.events
        return r

    def adopt(self, r: Mini, **kw) -> dict | None:
        return r.adopt(sleep=self.sleeps.append, **kw)


def test_adopt_catches_the_candidate_up_then_promotes_under_the_sources_lease():
    """Two scans landed on `g` during the fold: appended to `g.c001` under its own lease, then the promote job under
    `g`'s (the one every scan's append holds), the runner on `g.c001` after."""
    w = World([S1], [S2, S3])
    r = w.runner()
    assert w.adopt(r) == {"from": OLD, "gen": NEW, "rounds": 1, "appended": [S2, S3], "promoted": True}
    assert w.events == [("lock", f"{P}/{NEW}"), ("append", NEW, S2), ("append", NEW, S3), ("unlock", f"{P}/{NEW}"),
                        ("lock", P), ("job", f"t-promote-{NEW}", None), ("unlock", P)]
    assert (w.keys[f"{P}/{OLD}/{ar.SUCCESSOR}"], r.p.gen, r.candidate, r.defer_compaction) == ({"gen": NEW}, NEW, False, False)


def test_a_scan_landing_while_the_source_is_held_is_caught_up_next_round():
    """The source's lease is held by a scan's append (Busy: retried after `poll_s`), which lands `s4`: the next round
    appends it to the candidate before promoting."""
    w = World([S1], [S2])
    w.busy[P] = 1
    w.on_busy = lambda: w.land(S4)
    r = w.runner()
    assert w.adopt(r, poll_s=60) == {"from": OLD, "gen": NEW, "rounds": 2, "appended": [S2, S4], "promoted": True}
    assert w.events == [("lock", f"{P}/{NEW}"), ("append", NEW, S2), ("unlock", f"{P}/{NEW}"), ("busy", P),
                        ("lock", P), ("unlock", P),
                        ("lock", f"{P}/{NEW}"), ("append", NEW, S4), ("unlock", f"{P}/{NEW}"),
                        ("lock", P), ("job", f"t-promote-{NEW}", None), ("unlock", P)]
    assert w.sleeps == [60]


def test_adopt_gives_up_when_the_source_stays_held():
    """`wait_s` spent waiting on the source's lease: nothing promoted, the runner still on `g` (a later adopt resumes)."""
    w = World([S1], [S2])
    w.busy[P] = 99
    r = w.runner()
    assert w.adopt(r, poll_s=300, wait_s=600) == {"from": OLD, "gen": NEW, "rounds": 1, "appended": [S2], "promoted": False}
    assert (w.sleeps, f"{P}/{OLD}/{ar.SUCCESSOR}" in w.keys, r.p.gen) == ([300, 300], False, OLD)


def test_a_caught_up_candidate_is_promoted_without_appends():
    w = World([S1, S2], [])
    assert w.adopt(w.runner()) == {"from": OLD, "gen": NEW, "rounds": 1, "appended": [], "promoted": True}
    assert w.events == [("lock", P), ("job", f"t-promote-{NEW}", None), ("unlock", P)]


@pytest.mark.parametrize("drop, why", [(ar.COMPACTION, "its fold didn't finish"), (ar.CANDIDATE, "not a candidate")])
def test_no_whole_candidate_no_adoption(drop, why):
    """A candidate whose fold died (no `compaction.json`), or a next generation that isn't one: nothing to adopt."""
    w = World([S1], [S2])
    del w.keys[f"{P}/{NEW}/{drop}"]
    r = w.runner()
    assert (r.whole_candidate(), w.adopt(r), w.events) == (None, None, [])


def test_a_whole_candidate_is_never_folded_again():
    """The compaction stage on `g` with `g.c001` whole: nothing submitted (a fold would replace it), however due."""
    w = World([S1], [S2])
    r = w.runner()
    r.compact_jobs = lambda scan: pytest.fail("folded again")
    r.compact_stage(S2)
    assert w.events == []


def test_a_candidates_compaction_lease_holds_off_compactions_not_appends(tmp_path):
    """`compaction_lease(candidate=True)`: its record says so; another compaction is refused, an append's check
    (`compaction_holds(appends=True)`) passes it, and a promoting compaction's lease still holds appends off."""
    file, clock = ar.LocalLeaseFile(tmp_path / "l.json"), Clock()
    with ar.compaction_lease(file, "g", holder="job-c", now=clock, candidate=True):
        assert (json.loads((tmp_path / "l.json").read_text())["candidate"], ar.compaction_holds(file, "g", clock(), appends=True),
                ar.compaction_holds(file, "g", clock())["holder"]) == (True, None, "job-c")
        with pytest.raises(ar.Busy):
            with ar.compaction_lease(file, "g", holder="job-d", now=clock):
                pass
    with ar.compaction_lease(file, "g", holder="job-e", now=clock):
        assert ("candidate" in json.loads((tmp_path / "l.json").read_text()), ar.compaction_holds(file, "g", clock(), appends=True)["holder"]) == (
            False, "job-e")


def test_the_static_promote_jobs():
    """One `promote -r` job as the stages' account when it holds the R2 credentials; else `promote` (GCS), then the R2
    account's `compact-r2` from the source (its chain's pointers)."""
    def commands(r):
        return [(s["labels"]["stage"], s["allocationPolicy"]["serviceAccount"]["email"], s["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1])
                for _, s in r.promote_jobs("2026-10-10", GEN, f"{GEN}.c001")]

    r = _static_runner(1)
    assert commands(r) == [("promote", "build@proj", _py("static_merge", "promote", f"{GEN}.c001", mount=False)),
                           ("compact-r2", "copy@proj", _py("static_merge", "compact-r2", "-g", GEN, mount=False))]
    r.cfg = replace(r.cfg, r2_sa=r.cfg.sa)
    assert commands(r) == [("promote", "build@proj", _py("static_merge", "promote", "-r", f"{GEN}.c001", mount=False))]


def test_another_adopter_holding_the_candidate_stands_this_one_down():
    """The candidate's own lease held (another coordinator catching it up): this adopt returns unpromoted, no error."""
    w = World([S1], [S2])
    w.busy[f"{P}/{NEW}"] = 1
    r = w.runner()
    assert (w.adopt(r), w.events, r.p.gen) == (
        {"from": OLD, "gen": NEW, "rounds": 1, "appended": [], "promoted": False}, [("busy", f"{P}/{NEW}")], OLD)
