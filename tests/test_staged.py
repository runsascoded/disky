"""Specs for the staged-delete engine (`disk_tree.staged`) and its CLI.

Engine tests run in-process against a throwaway SQLite session with a fake
delete/size backend (no network, no real deletes). The CLI test drives the real
`disk-tree stage/staged/dispatch` end to end against local temp files."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from disk_tree import staged
from disk_tree.sqla import DeletionBand, DeletionRun, Plan, PlanItem
from disk_tree.sqla.base import Base

A = "s3://bucket/a"
B = "s3://bucket/b"
C = "s3://bucket/c"


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _uris(session, plan):
    return [it.uri for it in staged.items(session, plan)]


def test_stage_is_idempotent_into_one_shared_plan(session):
    p1, added1 = staged.stage(session, [A, B], "ryan")
    p2, added2 = staged.stage(session, [B, C], "ryan")
    assert p1.id == p2.id                      # the same shared open plan
    assert (added1, added2) == ([A, B], [C])   # B already staged the second time
    assert _uris(session, p1) == [A, B, C]


def test_stage_keeps_the_plan_free_of_nesting(session):
    """A staged dir covers everything under it: staging a descendant is a
    no-op, and staging an ancestor absorbs staged descendants — so a plan's
    items never overlap, and its bytes/objects never double-count."""
    child, sibling = f"{A}/x/y.zip", "s3://bucket/ab"
    _, added = staged.stage(session, [child, sibling], "ryan")
    assert added == [child, sibling]
    plan, added = staged.stage(session, [A], "ryan")
    assert added == [A]
    assert sorted(_uris(session, plan)) == [A, sibling]  # `ab` is not under `a`
    _, added = staged.stage(session, [f"{A}/x"], "ryan")
    assert added == []
    assert sorted(_uris(session, plan)) == [A, sibling]


def test_dispatch_skips_items_covered_by_another(session):
    """A plan staged before nesting was collapsed still counts each byte once:
    a covered item is neither sized nor deleted."""
    plan = staged.open_plan(session, "ryan")
    for u in [A, f"{A}/x.zip"]:
        session.add(PlanItem(plan_id=plan.id, uri=u, added_by="ryan", added_ts=staged._now()))
    session.flush()
    deleted: list[str] = []
    run = staged.dispatch(session, plan, "ryan", for_real=True, delete_fn=deleted.append, size_fn=lambda u: (10, 1))
    assert deleted == [A]
    assert (run.deleted_bytes, run.deleted_objects) == (10, 1)
    bands = session.scalars(select(DeletionBand).where(DeletionBand.run_id == run.run_id)).all()
    assert [(b.uri, b.bytes, b.deleted) for b in bands] == [(A, 10, 1)]
    assert plan.state == "closed"


def test_unstage_removes_from_open_plan(session):
    plan, _ = staged.stage(session, [A, B], "ryan")
    assert staged.unstage(session, [A]) == 1
    assert staged.unstage(session, [A]) == 0    # already gone
    assert _uris(session, plan) == [B]


def test_dispatch_dry_records_bands_deletes_nothing_keeps_plan_open(session):
    plan, _ = staged.stage(session, [A, B], "ryan")
    deleted: list[str] = []
    run = staged.dispatch(
        session, plan, "ryan", for_real=False,
        delete_fn=lambda u: deleted.append(u), size_fn=lambda u: (100, 2),
    )
    assert deleted == []                        # dry: nothing deleted
    assert (run.mode, run.deleted_bytes, run.deleted_objects) == ("dry", 0, 0)
    assert plan.state == "open"                 # dry leaves the plan open
    bands = session.scalars(select(DeletionBand).where(DeletionBand.run_id == run.run_id).order_by(DeletionBand.uri)).all()
    assert [(b.uri, b.bytes, b.objects, b.deleted) for b in bands] == [(A, 100, 2, 0), (B, 100, 2, 0)]


def test_dispatch_real_deletes_records_and_closes_plan(session):
    plan, _ = staged.stage(session, [A, B], "ryan")
    deleted: list[str] = []
    run = staged.dispatch(
        session, plan, "ryan", for_real=True,
        delete_fn=lambda u: deleted.append(u), size_fn=lambda u: (100, 2),
    )
    assert deleted == [A, B]                     # both deleted, in URI order
    assert (run.mode, run.deleted_bytes, run.deleted_objects) == ("real", 200, 4)
    assert plan.state == "closed" and plan.closed_ts is not None
    bands = session.scalars(select(DeletionBand).where(DeletionBand.run_id == run.run_id)).all()
    assert all(b.deleted == 1 for b in bands)


def test_dispatch_subset_deletes_only_those_and_closes_once_empty(session):
    plan, _ = staged.stage(session, [A, B, C], "ryan")
    deleted: list[str] = []
    run = staged.dispatch(
        session, plan, "ryan", for_real=True, uris=[B],
        delete_fn=lambda u: deleted.append(u), size_fn=lambda u: (100, 2),
    )
    assert deleted == [B]                        # only the subset
    assert (run.deleted_bytes, run.deleted_objects) == (100, 2)
    assert _uris(session, plan) == [A, C]        # B left the plan…
    assert plan.state == "open"                  # …which stays open while items remain
    bands = session.scalars(select(DeletionBand).where(DeletionBand.run_id == run.run_id)).all()
    assert [(b.uri, b.deleted) for b in bands] == [(B, 1)]

    run2 = staged.dispatch(
        session, plan, "ryan", for_real=True, uris=[C, A],
        delete_fn=lambda u: deleted.append(u), size_fn=lambda u: (100, 2),
    )
    assert deleted == [B, A, C]                  # URI order within the subset
    assert (_uris(session, plan), plan.state) == ([], "closed")
    assert run2.run_id != run.run_id


def test_dispatch_subset_dry_leaves_items_staged(session):
    plan, _ = staged.stage(session, [A, B], "ryan")
    run = staged.dispatch(
        session, plan, "ryan", for_real=False, uris=[A],
        delete_fn=lambda u: (_ for _ in ()).throw(AssertionError(u)), size_fn=lambda u: (7, 1),
    )
    assert (run.mode, _uris(session, plan), plan.state) == ("dry", [A, B], "open")
    bands = session.scalars(select(DeletionBand).where(DeletionBand.run_id == run.run_id)).all()
    assert [(b.uri, b.bytes, b.deleted) for b in bands] == [(A, 7, 0)]


def test_dispatch_subset_rejects_an_unstaged_uri_before_running(session):
    plan, _ = staged.stage(session, [A], "ryan")
    deleted: list[str] = []
    with pytest.raises(KeyError, match="not staged in plan 1: s3://bucket/b"):
        staged.dispatch(
            session, plan, "ryan", for_real=True, uris=[A, B],
            delete_fn=lambda u: deleted.append(u), size_fn=lambda u: (1, 1),
        )
    assert (deleted, _uris(session, plan), plan.state) == ([], [A], "open")


def test_undo_run_restores_deleted_bands_and_sets_full(session):
    plan, _ = staged.stage(session, [A, B], "ryan")
    run = staged.dispatch(session, plan, "ryan", for_real=True, delete_fn=lambda u: None, size_fn=lambda u: (100, 3))
    restored: list[str] = []

    def restore(u):
        restored.append(u)
        return 3

    _, results = staged.undo_run(session, run, restore)
    assert restored == [A, B]                    # every deleted band, in URI order
    assert results == [(A, 3), (B, 3)]
    assert run.undo_state == "full"


def test_undo_run_partial_when_some_restore_nothing(session):
    plan, _ = staged.stage(session, [A, B], "ryan")
    run = staged.dispatch(session, plan, "ryan", for_real=True, delete_fn=lambda u: None, size_fn=lambda u: (1, 1))
    _, results = staged.undo_run(session, run, lambda u: 2 if u == A else 0)
    assert results == [(A, 2), (B, 0)]
    assert run.undo_state == "partial"           # A came back, B didn't


def test_restorable_bands_excludes_a_dry_run(session):
    plan, _ = staged.stage(session, [A], "ryan")
    dry = staged.dispatch(session, plan, "ryan", for_real=False, delete_fn=lambda u: None, size_fn=lambda u: (1, 1))
    assert staged.restorable_bands(session, dry) == []   # dry deleted nothing


def test_plan_by_ref_resolves_id_name_and_default(session):
    plan, _ = staged.stage(session, [A], "ryan")
    assert staged.plan_by_ref(session, None).id == plan.id     # open Staged
    assert staged.plan_by_ref(session, str(plan.id)).id == plan.id
    assert staged.plan_by_ref(session, "Staged").id == plan.id
    assert staged.plan_by_ref(session, "nope") is None


# ---- CLI end-to-end (real local deletes) -----------------------------------


def _run_dt(env_root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DISK_TREE_ROOT": str(env_root)}
    return subprocess.run(
        [sys.executable, "-m", "disk_tree.cli.main", *args],
        env=env, capture_output=True, text=True, check=False,
    )


def test_cli_stage_dry_then_real_delete(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    (data / "a.bin").write_bytes(b"\0" * 1000)
    (data / "sub" / "b.bin").write_bytes(b"\0" * 2000)

    assert _run_dt(root, "index", str(data), "-C", "-q").returncode == 0
    assert _run_dt(root, "stage", str(data)).returncode == 0

    dry = _run_dt(root, "dispatch", "-j")
    assert dry.returncode == 0
    dj = json.loads(dry.stdout)
    assert (dj["mode"], dj["deleted_objects"], dj["plan_state"]) == ("dry", 0, "open")
    assert dj["objects"] > 0                    # sized from the scan
    assert data.exists()                        # dry deleted nothing

    real = _run_dt(root, "dispatch", "-f", "-j")
    assert real.returncode == 0
    rj = json.loads(real.stdout)
    assert (rj["mode"], rj["plan_state"]) == ("real", "closed")
    assert not data.exists()                    # really deleted

    # no open plans left; both runs recorded
    listed = json.loads(_run_dt(root, "staged", "-j").stdout)
    assert listed["plans"] == []
    assert sorted(r["mode"] for r in listed["runs"]) == ["dry", "real"]

    # `undo` (dry) reports the real run's restorable scope without touching the store
    real_run = next(r["run_id"] for r in listed["runs"] if r["mode"] == "real")
    undo = _run_dt(root, "undo", real_run, "-j")
    assert undo.returncode == 0
    uj = json.loads(undo.stdout)
    assert (uj["run_id"], uj["for_real"], uj["paths"]) == (real_run, False, 1)
    assert uj["objects"] > 0
