"""gcs's owner-price carry (`owner_carry`): `owner_prefix_aggs` rows copied to a compaction's new generation before anything
points at it, so `CURRENT` moving doesn't send every scoped series back to first-time pricing. A sqlite stand-in for D1
(`d1_db`, the branch's `schema.sql`) behind `index_footer._d1_query`; compactions are real folds of the small store world."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud import append_runner as ar
from dt_cloud import index_footer as fx
from dt_cloud import interval_append as ia
from dt_cloud import interval_compact as ic
from dt_cloud import interval_store as ist
from dt_cloud.blobstore import Store
from dt_cloud.owner_carry import OwnerCarry, tag

from test_compact_stage import _gens
from test_interval_append import Fake as IvFake
from test_interval_compact import DATES, RG, add, build, make_world

OLD, NEW = tag("g"), tag("g.c001")
SCANS = [f"2026-08-{d:02d}" for d in range(1, 29)] + [f"2026-09-{d:02d}" for d in range(1, 13)]


class D1:
    """`_d1_query` over a sqlite: `fail(sql)` may raise; every statement in `sql`, in order."""

    def __init__(self, db: sqlite3.Connection):
        self.db, self.sql, self.fail = db, [], lambda sql: None
        db.row_factory = sqlite3.Row

    def __call__(self, sql: str, acct: str, tok: str, db_id: str = "") -> list[dict]:
        assert (acct, tok) == ("acct", "tok")
        self.sql.append(sql)
        self.fail(sql)
        rows = [dict(r) for r in self.db.execute(sql).fetchall()]
        self.db.commit()
        return rows

    def seed(self, idx: str, scans: list[str], body: str = "{}", ts: int = 1) -> None:
        self.db.executemany("INSERT INTO owner_prefix_aggs (scan, idx, body, updated_ts) VALUES (?, ?, ?, ?)", [(s, idx, f"{body}{s}", ts) for s in scans])
        self.db.commit()

    def rows(self, idx: str) -> list[tuple[str, str, int]]:
        return [tuple(r) for r in self.db.execute("SELECT scan, body, updated_ts FROM owner_prefix_aggs WHERE idx = ? ORDER BY scan", (idx,))]

    def writes(self) -> list[str]:
        return [s for s in self.sql if s.startswith(("INSERT", "DELETE"))]


@pytest.fixture
def d1(d1_db, monkeypatch) -> D1:
    stub = D1(d1_db)
    monkeypatch.setattr(fx, "_d1_query", stub)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    return stub


def carry(now: int = 9) -> OwnerCarry:
    return OwnerCarry(now=lambda: now, log=lambda m: None)


def down(sql: str) -> None:
    raise RuntimeError("D1 query gave no answer after 4 tries")


# ── The tag: what the site's `indexTag` writes (`intervalSlices.test.ts` pins the same strings) ──


def test_tag_is_the_sites():
    assert [tag("2026-10-09b.g3"), tag("2026-10-09b.g3.c001")] == ["pq:iv:2026-10-09b.g3:s", "pq:iv:2026-10-09b.g3.c001:s"]


# ── The copy ──


def test_copy_pages_16_rows_a_statement_and_a_rerun_changes_nothing(d1):
    d1.seed(OLD, SCANS)
    assert carry().copy("g", "g.c001") == {"rows": 40, "changed": 40}
    assert d1.rows(NEW) == [(s, f"{{}}{s}", 9) for s in SCANS]
    assert [re.search(r"scan > '([^']*)' AND scan <= '([^']*)'", s).groups() for s in d1.writes()] == [("", SCANS[15]), (SCANS[15], SCANS[31]), (SCANS[31], SCANS[39])]
    # IDP: a rerun writes no row (each `updated_ts` as the first copy left it).
    assert carry(now=99).copy("g", "g.c001") == {"rows": 40, "changed": 0}
    assert d1.rows(NEW) == [(s, f"{{}}{s}", 9) for s in SCANS]


def test_a_partial_copy_resumes(d1):
    d1.seed(OLD, SCANS)
    d1.fail = lambda sql: down(sql) if sql.startswith("INSERT") and f"scan > '{SCANS[15]}'" in sql else None
    with pytest.raises(RuntimeError) as e:
        carry().copy("g", "g.c001")
    assert str(e.value) == "D1 query gave no answer after 4 tries"
    assert [r[0] for r in d1.rows(NEW)] == SCANS[:16]
    d1.fail = lambda sql: None
    assert carry().copy("g", "g.c001") == {"rows": 40, "changed": 24}
    assert [r[0] for r in d1.rows(NEW)] == SCANS


def test_copy_halves_on_a_cpu_reset_and_stops_at_one_row(d1):
    d1.seed(OLD, SCANS)
    resets = [1]

    def once(sql: str) -> None:
        if sql.startswith("INSERT") and resets:
            resets.pop()
            raise fx.D1CpuLimit("7429")

    d1.fail = once
    assert carry().copy("g", "g.c001") == {"rows": 40, "changed": 40}
    assert [re.search(r"scan <= '([^']*)'", s)[1] for s in d1.writes()] == [SCANS[15], *(SCANS[i] for i in range(7, 40, 8))]

    def always(sql: str) -> None:
        if sql.startswith("INSERT"):
            raise fx.D1CpuLimit("7429")

    d1.fail = always
    d1.seed(tag("h"), SCANS[:2])
    with pytest.raises(fx.D1CpuLimit):
        carry().copy("h", "h.c001")
    assert d1.rows(tag("h.c001")) == []


def test_rows_priced_meanwhile_are_picked_up_only_when_longer(d1):
    d1.seed(OLD, SCANS[:3], body="{}", ts=1)
    d1.seed(NEW, SCANS[:2], body="{}", ts=2)
    d1.db.execute("UPDATE owner_prefix_aggs SET body = body || 'old-grew' WHERE idx = ? AND scan = ?", (OLD, SCANS[0]))
    d1.db.execute("UPDATE owner_prefix_aggs SET body = body || 'new-grew' WHERE idx = ? AND scan = ?", (NEW, SCANS[1]))
    d1.db.commit()
    assert carry().copy("g", "g.c001") == {"rows": 3, "changed": 2}
    assert d1.rows(NEW) == [(SCANS[0], f"{{}}{SCANS[0]}old-grew", 9), (SCANS[1], f"{{}}{SCANS[1]}new-grew", 2), (SCANS[2], f"{{}}{SCANS[2]}", 9)]


def test_copy_leaves_the_old_tag_and_every_other_alone(d1):
    d1.seed(OLD, SCANS)
    d1.seed(tag("other"), SCANS[:2], body="other")
    before = d1.rows(OLD), d1.rows(tag("other"))
    carry().copy("g", "g.c001")
    assert (d1.rows(OLD), d1.rows(tag("other"))) == before


def test_a_new_tags_rows_price_nothing_else(d1):
    """A scan the old tag doesn't hold gets no row: it prices itself (`intervalSlices.test.ts`)."""
    d1.seed(OLD, SCANS[:2])
    carry().copy("g", "g.c001")
    assert [r[0] for r in d1.rows(NEW)] == SCANS[:2]


# ── The check before the fold ──


def test_preflight_counts_the_generations_rows(d1):
    d1.seed(OLD, SCANS[:3])
    d1.seed(tag("other"), SCANS[:1])
    assert carry().preflight("g") == 3
    assert d1.writes() == []


def test_preflight_refuses_an_unreachable_d1_no_rows_and_a_revved_tag(d1):
    d1.fail = down
    with pytest.raises(RuntimeError) as e:
        carry().preflight("g")
    assert str(e.value) == "D1 query gave no answer after 4 tries"
    d1.fail = lambda sql: None
    d1.seed(tag("other"), SCANS[:1])
    with pytest.raises(RuntimeError) as e:
        carry().preflight("g")
    assert str(e.value) == f"owner carry: no owner_prefix_aggs rows under {OLD}: the site's tag isn't what this carries (other tags of g: none)"
    d1.seed("pq:iv:g@2:s", SCANS[:1])
    with pytest.raises(RuntimeError) as e:
        carry().preflight("g")
    assert str(e.value) == f"owner carry: no owner_prefix_aggs rows under {OLD}: the site's tag isn't what this carries (other tags of g: pq:iv:g@2:s)"


# ── The delete at retirement ──


def test_retire_deletes_one_tags_rows_32_a_statement(d1):
    d1.seed(OLD, SCANS)
    d1.seed(NEW, SCANS[:2])
    assert carry().retire("g") == 40
    assert (d1.rows(OLD), [r[0] for r in d1.rows(NEW)]) == ([], SCANS[:2])
    assert [re.search(r"LIMIT (\d+)", s)[1] for s in d1.writes()] == ["32", "32", "32"]
    assert carry().retire("g") == 0


# ── Around a real compaction ──


def _world(tmp_path: Path) -> tuple[Path, Store]:
    """`g` (a base through DATES[1], DATES[2:4] appended), current."""
    world = make_world(tmp_path / "scans", False)
    store_dir = tmp_path / "store"
    build(world, store_dir, tmp_path / "wb", "g", DATES[1])
    for d in DATES[2:4]:
        add(world, store_dir, tmp_path / "wa", "-t", d, "-C", "none")
    store = Store(str(store_dir))
    ar.set_current(store, ist.PREFIX, "g")
    return store_dir, store


KW = dict(threads=2, mem="1GB", log=lambda m: None, now=0.0, force=True, policy=ar.NEVER)


def test_the_copy_lands_before_the_pointer_and_again_after_the_r2_copy(d1, tmp_path, monkeypatch):
    store_dir, store = _world(tmp_path)
    d1.seed(OLD, SCANS[:3])
    events: list[str] = []

    class Spy(Store):
        def write_text(self, key: str, text: str) -> None:
            if key.endswith((ar.SUCCESSOR, ar.CURRENT)):
                events.append(key.rsplit("/", 1)[1])
            super().write_text(key, text)

    class Traced(OwnerCarry):
        def copy(self, old: str, new: str) -> dict:
            events.append(f"copy {old} {new}")
            return super().copy(old, new)

    monkeypatch.setattr(ar, "r2_publish_gen", lambda *a, **kw: (events.append("r2"), {"objects": 1, "copied": 1, "bytes": 1, "s": 0})[1])
    doc = ar.run_compaction(Spy(str(store_dir)), ic.compactor(RG), "g", store_dir, tmp_path / "w", r2_bucket="b", carry=Traced(now=lambda: 9, log=lambda m: None), **KW)
    assert events == ["copy g g.c001", ar.SUCCESSOR, ar.CURRENT, "r2", "copy g g.c001"]
    assert (doc["compact"]["owner_carry"], d1.rows(NEW)) == ({"rows": 3, "changed": 0}, [(s, f"{{}}{s}", 9) for s in SCANS[:3]])
    assert ar.current(store, ist.PREFIX) == "g.c001"


def test_an_unreachable_d1_stops_before_the_fold(d1, tmp_path):
    store_dir, store = _world(tmp_path)
    folds: list[str] = []
    c = ic.compactor(RG)
    spy = replace(c, fold=lambda *a, **kw: (folds.append(kw["gen"]), c.fold(*a, **kw))[1])
    d1.fail = down
    with pytest.raises(RuntimeError) as e:
        ar.run_compaction(store, spy, "g", store_dir, tmp_path / "w", carry=carry(), **KW)
    assert str(e.value) == "D1 query gave no answer after 4 tries"
    assert (folds, _gens(store_dir), ar.current(store, ist.PREFIX), (store_dir / ist.PREFIX / "g" / ar.SUCCESSOR).exists()) == ([], ["g"], "g", False)


def test_a_d1_failure_at_the_pointer_moves_nothing_and_a_rerun_finishes(d1, tmp_path):
    store_dir, store = _world(tmp_path)
    d1.seed(OLD, SCANS)
    d1.fail = lambda sql: down(sql) if sql.startswith("INSERT") and f"scan > '{SCANS[15]}'" in sql else None
    with pytest.raises(RuntimeError):
        ar.run_compaction(store, ic.compactor(RG), "g", store_dir, tmp_path / "w1", carry=carry(), **KW)
    assert (ar.current(store, ist.PREFIX), (store_dir / ist.PREFIX / "g" / ar.SUCCESSOR).exists()) == ("g", False)
    assert [r[0] for r in d1.rows(NEW)] == SCANS[:16]
    d1.fail = lambda sql: None
    doc = ar.run_compaction(store, ic.compactor(RG), "g", store_dir, tmp_path / "w2", carry=carry(), **KW)
    assert (doc["compact"]["gen"], ar.current(store, ist.PREFIX), [r[0] for r in d1.rows(NEW)]) == ("g.c001", "g.c001", SCANS)


def test_a_failed_repeat_after_the_pointer_is_logged_not_fatal(d1, tmp_path, monkeypatch):
    store_dir, store = _world(tmp_path)
    d1.seed(OLD, SCANS[:2])
    monkeypatch.setattr(ar, "r2_publish_gen", lambda *a, **kw: (setattr(d1, "fail", down), {"objects": 1, "copied": 1, "bytes": 1, "s": 0})[1])
    doc = ar.run_compaction(store, ic.compactor(RG), "g", store_dir, tmp_path / "w", r2_bucket="b", carry=carry(), **KW)
    assert (doc["compact"]["owner_carry"], ar.current(store, ist.PREFIX)) == ({"error": "RuntimeError: D1 query gave no answer after 4 tries"}, "g.c001")


def test_a_candidate_touches_no_d1_and_promote_carries_it(d1, tmp_path):
    store_dir, store = _world(tmp_path)
    d1.seed(OLD, SCANS[:2])
    d1.fail = down
    doc = ar.run_compaction(store, ic.compactor(RG), "g", store_dir, tmp_path / "w", promote=False, carry=carry(), **KW)
    assert (doc["compact"]["candidate"], len(d1.sql), "owner_carry" in doc["compact"]) == (True, 0, False)
    log: list[str] = []
    with pytest.raises(RuntimeError):
        ar.promote_gen(store, ist.PREFIX, "g.c001", carry=carry(), log=log.append)
    assert (ar.current(store, ist.PREFIX), (store_dir / ist.PREFIX / "g" / ar.SUCCESSOR).exists(), d1.rows(NEW)) == ("g", False, [])
    d1.fail = lambda sql: None
    assert ar.promote_gen(store, ist.PREFIX, "g.c001", carry=carry(), log=log.append) == {"from": "g", "gen": "g.c001", "current": True}
    assert [r[0] for r in d1.rows(NEW)] == SCANS[:2]
    assert len(d1.rows(OLD)) == 2


def test_a_compaction_superseded_after_the_copy_keeps_the_rows_for_the_next(d1, tmp_path):
    world = make_world(tmp_path / "scans", False)
    store_dir = tmp_path / "store"
    build(world, store_dir, tmp_path / "wb", "g", DATES[1])
    add(world, store_dir, tmp_path / "wa", "-t", DATES[2], "-C", "none")
    ar.set_current(Store(str(store_dir)), ist.PREFIX, "g")
    d1.seed(OLD, SCANS[:2])

    class Racing(Store):
        def write_text(self, key: str, text: str) -> None:
            if key.endswith(f"/g/{ar.SUCCESSOR}"):
                add(world, store_dir, tmp_path / "wr", "-t", DATES[3], "-C", "none")
            super().write_text(key, text)

    doc = ar.run_compaction(Racing(str(store_dir)), ic.compactor(RG), "g", store_dir, tmp_path / "w", carry=carry(), **KW)
    assert ("superseded" in doc["compact"], ar.current(Store(str(store_dir)), ist.PREFIX), [r[0] for r in d1.rows(NEW)], len(d1.rows(OLD))) == (True, "g", SCANS[:2], 2)


def test_retirement_deletes_the_tag_with_the_generation_past_its_grace(d1, tmp_path):
    store_dir, store = _world(tmp_path)
    d1.seed(OLD, SCANS[:2])
    kept = ar.run_compaction(store, ic.compactor(RG), "g", store_dir, tmp_path / "w1", carry=carry(), **KW)
    assert ([(p["gen"], p["deleted"]) for p in kept["predecessors"]], len(d1.rows(OLD)), len(d1.rows(NEW))) == ([("g", 0)], 2, 2)

    # A D1 failure at the delete keeps the generation's files, so the next prune finds it and deletes both.
    d1.fail = lambda sql: down(sql) if sql.startswith("DELETE") else None
    with pytest.raises(RuntimeError):
        ar.prune_predecessors(store, ist.PREFIX, "g.c001", now=1e12, retire=carry().retire, log=lambda m: None)
    assert (_gens(store_dir), len(d1.rows(OLD))) == (["g", "g.c001"], 2)
    d1.fail = lambda sql: None
    [rec] = ar.prune_predecessors(store, ist.PREFIX, "g.c001", now=1e12, retire=carry().retire, log=lambda m: None)
    assert (rec["gen"], rec["owner_rows"], d1.rows(OLD), len(d1.rows(NEW))) == ("g", 2, [], 2)
    assert (store_dir / ist.PREFIX / "g" / ar.SUCCESSOR).exists()


def test_retirement_runs_in_the_compaction_jobs_prune_phase(d1, tmp_path):
    store_dir, store = _world(tmp_path)
    d1.seed(OLD, SCANS[:2])
    doc = ar.run_compaction(store, ic.compactor(RG), "g", store_dir, tmp_path / "w", carry=carry(), **{**KW, "now": 1e12})
    assert ([(p["gen"], p["owner_rows"]) for p in doc["predecessors"]], d1.rows(OLD), len(d1.rows(NEW))) == ([("g", 2)], [], 2)


# ── Opt-out: nothing changes ──


def test_without_the_setting_the_job_and_the_generation_are_what_they_were(d1, tmp_path):
    runner = IvFake(["2026-08-03"], compact_level=1, compact_jobs=4).runner(merge_wait=None)
    [(_, spec)] = runner.compact_jobs("2026-08-04")
    on = IvFake(["2026-08-03"], compact_level=1, compact_jobs=4, owner_carry=True).runner(merge_wait=None)
    [(_, spec_on)] = on.compact_jobs("2026-08-04")
    cmd = spec["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1]
    cmd_on = spec_on["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1]
    assert " -O " not in cmd
    assert cmd_on.replace(" -O ", " ", 1) == cmd

    d1.fail = down
    plain_dir, plain = _world(tmp_path / "plain")
    doc = ar.run_compaction(plain, ic.compactor(RG), "g", plain_dir, tmp_path / "plain" / "w", **KW)
    assert (d1.sql, "owner_carry" in doc["compact"], [("owner_rows" in p) for p in doc["predecessors"]]) == ([], False, [False])

    d1.fail = lambda sql: None
    d1.seed(OLD, SCANS[:2])
    carried_dir, carried = _world(tmp_path / "carried")
    ar.run_compaction(carried, ic.compactor(RG), "g", carried_dir, tmp_path / "carried" / "w", carry=carry(), **KW)
    # A scan's entry names its source's path, under each world's own `tmp_path`.
    gen_of = lambda d: d / ist.PREFIX / "g.c001"  # noqa: E731
    # `served/<sort>.json` and `stats.json` record each cut's seconds; every data file and group index must match.
    bytes_of = lambda d: {k: v for k, v in ar.md5s(gen_of(d)).items() if k != "scans.json" and not re.fullmatch(r"served/[^/]+\.json", k)}  # noqa: E731
    assert bytes_of(carried_dir) == bytes_of(plain_dir)
    assert (gen_of(carried_dir) / "scans.json").read_text().replace("/carried/", "/plain/") == (gen_of(plain_dir) / "scans.json").read_text()


def test_the_flags_build_the_carry_and_only_they_do(monkeypatch):
    got: dict[str, object] = {}
    monkeypatch.setattr(ia, "Store", lambda root: root)
    monkeypatch.setattr(ar, "run_compaction", lambda *a, **kw: (got.__setitem__("compact", kw["carry"]), {"gen": "g"})[1])
    monkeypatch.setattr(ar, "promote_gen", lambda *a, **kw: (got.__setitem__("promote", kw["carry"]), {"gen": "g"})[1])
    compact = ["compact", "-b", "data", "-g", "g", "-R", "g", "-S", "scratch", "-m", "/m", "-T", "/t", "-M", "1GB", "-N"]
    for args, want in (([*compact], None), ([*compact, "-O"], OwnerCarry)):
        got.clear()
        res = CliRunner().invoke(ia.cli, args, catch_exceptions=False)
        assert (res.exit_code, type(got["compact"]) if want else got["compact"]) == (0, want)
    assert json.loads(CliRunner().invoke(ia.cli, ["promote", "-b", "data", "g"], catch_exceptions=False).stdout) == {"gen": "g"}
    assert got["promote"] is None
    CliRunner().invoke(ia.cli, ["promote", "-O", "-b", "data", "g"], catch_exceptions=False)
    assert type(got["promote"]) is OwnerCarry
