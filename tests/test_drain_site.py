"""Specs for the drainer over `site/`'s D1 schema (specs/m3-site.md Phase 3):
`plan_items.prefix`, no `batch_job`, bands without `deleted`, the
`undo_deadline` / `purge_state` hold, the `agents` heartbeat — plus dry runs,
trashing, and the post-run hook, which are schema-independent."""
from __future__ import annotations

import sqlite3

import pytest

from disk_tree import drain
from disk_tree.drain import Schema

# The columns `site/migrations/cw/0001_init.sql` (+ 0006_agents) give these tables.
SITE_SCHEMA = """
CREATE TABLE plans (id INTEGER PRIMARY KEY, name TEXT, state TEXT DEFAULT 'open', created_by TEXT, created_ts INTEGER);
CREATE TABLE plan_items (plan_id INTEGER, prefix TEXT, note TEXT, added_by TEXT, added_ts INTEGER, batch_id INTEGER,
    PRIMARY KEY (plan_id, prefix));
CREATE TABLE deletion_runs (run_id TEXT PRIMARY KEY, plan_id INTEGER, manifest TEXT, scan TEXT, head INTEGER, exec_head INTEGER,
    actor TEXT, mode TEXT, started_ts INTEGER, finished_ts INTEGER, deleted_bytes INTEGER DEFAULT 0,
    deleted_objects INTEGER DEFAULT 0, skipped_gone INTEGER DEFAULT 0, skipped_overwritten INTEGER DEFAULT 0,
    drift_dirs INTEGER DEFAULT 0, ledger_drift_dirs INTEGER DEFAULT 0, buckets TEXT, undo_deadline INTEGER,
    undo_state TEXT DEFAULT 'none', purge_state TEXT DEFAULT 'none', log_dir TEXT, plan_digest TEXT);
CREATE TABLE deletion_bands (run_id TEXT, prefix TEXT, bytes INTEGER, objects INTEGER, gone INTEGER DEFAULT 0,
    overwritten INTEGER DEFAULT 0, drift_new_objects INTEGER DEFAULT 0, undone_objects INTEGER DEFAULT 0,
    PRIMARY KEY (run_id, prefix));
CREATE TABLE agents (name TEXT PRIMARY KEY, seen_ts INTEGER NOT NULL, host TEXT);
"""


class FakeD1:
    def __init__(self, schema: str):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(schema)

    def query(self, sql, params=None):
        cur = self.conn.execute(sql, params or [])
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        self.conn.commit()
        return rows


A = "file:///Users/ryan/c/old-repo/"
B = "file:///Users/ryan/Downloads/big.iso/"


def _enqueue(db, run_id, plan_id, prefixes, mode="real"):
    db.query("INSERT INTO plans (id, name, created_by, created_ts) VALUES (?, 'Staged', 'ryan', 1)", [plan_id])
    db.query(
        "INSERT INTO deletion_runs (run_id, plan_id, manifest, scan, head, exec_head, actor, mode, started_ts, log_dir, plan_digest) "
        "VALUES (?, ?, 'laptop', '2026-09-29', 0, 0, 'ryan', ?, 100, 'laptop', 'd')",
        [run_id, plan_id, mode],
    )
    for p in prefixes:
        db.query("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (?, ?, 'ryan', 100)", [plan_id, p])


@pytest.fixture
def db():
    return FakeD1(SITE_SCHEMA)


def test_schema_detection_site_vs_ui(db):
    from test_drain import FakeD1 as UiD1

    assert Schema.detect(db) == Schema(item_col="prefix", batch_job=False, bands_deleted=False, hold=True, agents=True)
    assert Schema.detect(UiD1()) == Schema(item_col="uri", batch_job=True, bands_deleted=True, hold=False, agents=False)


def test_drain_checks_in_even_with_nothing_pending(db):
    assert drain.drain_once(db, size_fn=lambda u: (0, 0), delete_fn=lambda u: None, now=lambda: 500, host="m3") == []
    assert db.query("SELECT name, seen_ts, host FROM agents") == [{"name": "drainer", "seen_ts": 500, "host": "m3"}]
    drain.drain_once(db, size_fn=lambda u: (0, 0), delete_fn=lambda u: None, now=lambda: 560, host="m3")
    assert db.query("SELECT seen_ts FROM agents") == [{"seen_ts": 560}]


def test_dry_run_sizes_without_deleting_and_reports_would_delete(db):
    _enqueue(db, "laptop-dry-1", 1, [A, B], mode="dry")
    deleted = []
    out = drain.drain_once(db, size_fn=lambda u: (100, 2), delete_fn=deleted.append, trash_fn=lambda u, r: deleted.append(u), now=lambda: 200)
    assert deleted == []
    assert out == [{
        "run_id": "laptop-dry-1", "plan_id": 1, "actor": "ryan", "mode": "dry", "items": 2, "deleted_paths": 0,
        "deleted_bytes": 200, "deleted_objects": 4, "errors": [], "finished_ts": 200, "trashed": False,
    }]
    assert db.query("SELECT finished_ts, deleted_bytes, deleted_objects, undo_state, undo_deadline, purge_state FROM deletion_runs") == [
        {"finished_ts": 200, "deleted_bytes": 200, "deleted_objects": 4, "undo_state": "none", "undo_deadline": None, "purge_state": "none"},
    ]
    # items run in prefix order: `D` sorts before `c`
    assert db.query("SELECT prefix, bytes, objects, gone FROM deletion_bands ORDER BY prefix") == [
        {"prefix": B, "bytes": 100, "objects": 2, "gone": 0},
        {"prefix": A, "bytes": 100, "objects": 2, "gone": 0},
    ]


def test_real_run_trashes_and_opens_the_hold(db):
    _enqueue(db, "laptop-real-1", 1, [A, B])
    trashed, after = [], []
    out = drain.drain_once(
        db, size_fn=lambda u: (100, 2), delete_fn=lambda u: (_ for _ in ()).throw(AssertionError("rm used")),
        trash_fn=lambda u, r: trashed.append((u, r)) or f"/t/{r}{u}", hold_s=7 * 86400, after=after.append, now=lambda: 300,
    )
    assert trashed == [(B, "laptop-real-1"), (A, "laptop-real-1")]
    assert out[0]["trashed"] is True and out[0]["deleted_objects"] == 4
    assert after == out
    assert db.query("SELECT finished_ts, deleted_bytes, deleted_objects, undo_deadline, purge_state FROM deletion_runs") == [
        {"finished_ts": 300, "deleted_bytes": 200, "deleted_objects": 4, "undo_deadline": 300 + 7 * 86400, "purge_state": "pending"},
    ]


def test_real_run_without_trash_deletes_and_leaves_no_hold(db):
    _enqueue(db, "laptop-real-2", 2, [A])
    deleted, after = [], []
    drain.drain_once(db, size_fn=lambda u: (10, 1), delete_fn=deleted.append, after=after.append, now=lambda: 400)
    assert deleted == [A]
    assert [s["run_id"] for s in after] == ["laptop-real-2"]
    assert db.query("SELECT undo_deadline, purge_state FROM deletion_runs") == [{"undo_deadline": None, "purge_state": "none"}]


def test_after_hook_skips_dry_runs_and_runs_that_deleted_nothing(db):
    _enqueue(db, "laptop-dry-3", 3, [A], mode="dry")
    _enqueue(db, "laptop-real-4", 4, [])
    after = []
    drain.drain_once(db, size_fn=lambda u: (0, 0), delete_fn=lambda u: None, trash_fn=lambda u, r: "", after=after.append, now=lambda: 500)
    assert after == []


def test_an_unscanned_path_still_opens_the_hold_and_the_after_hook(db):
    # the scan sizes it as 0 (staged since the last scan), but it was trashed
    _enqueue(db, "laptop-real-6", 6, [A])
    after = []
    out = drain.drain_once(db, size_fn=lambda u: (0, 0), delete_fn=lambda u: None, trash_fn=lambda u, r: "/t", hold_s=100, after=after.append, now=lambda: 700)
    assert (out[0]["deleted_paths"], out[0]["deleted_objects"]) == (1, 0)
    assert [s["run_id"] for s in after] == ["laptop-real-6"]
    assert db.query("SELECT undo_deadline, purge_state FROM deletion_runs") == [{"undo_deadline": 800, "purge_state": "pending"}]


def test_a_failed_path_is_recorded_not_fatal(db):
    _enqueue(db, "laptop-real-5", 5, [A, B])

    def trash_fn(uri, run_id):
        if uri == B:
            raise OSError("not on the trash root's volume")
        return "/t"

    out = drain.drain_once(db, size_fn=lambda u: (50, 1), delete_fn=lambda u: None, trash_fn=trash_fn, now=lambda: 600)
    assert (out[0]["deleted_objects"], out[0]["deleted_bytes"], out[0]["errors"]) == (1, 50, [(B, "not on the trash root's volume")])
    assert db.query("SELECT prefix FROM deletion_bands ORDER BY prefix") == [{"prefix": B}, {"prefix": A}]


FREED_SCHEMA = SITE_SCHEMA + "ALTER TABLE deletion_runs ADD COLUMN freed_bytes INTEGER;\n"


def test_dry_run_records_what_the_set_actually_frees():
    """A dry run on a schema with `freed_bytes` measures the staged set as a
    whole (`reclaim_fn`, the extent intersection: clone/hardlink-shared bytes
    don't count) beside the per-path would-delete sizes."""
    db = FakeD1(FREED_SCHEMA)
    assert Schema.detect(db).freed
    _enqueue(db, "laptop-dry-2", 1, [A, B], mode="dry")
    measured = []
    out = drain.drain_once(db, size_fn=lambda u: (100, 2), delete_fn=lambda u: None, reclaim_fn=lambda us: measured.append(us) or 30, now=lambda: 200)
    assert measured == [[B, A]]
    assert (out[0]["deleted_bytes"], out[0]["freed_bytes"]) == (200, 30)
    assert db.query("SELECT deleted_bytes, freed_bytes FROM deletion_runs") == [{"deleted_bytes": 200, "freed_bytes": 30}]


def test_freed_bytes_only_for_dry_runs_on_a_schema_that_has_it(db):
    """No column → no measurement; a real run is never re-measured (its trash
    rename frees nothing until emptied)."""
    _enqueue(db, "laptop-dry-3", 1, [A], mode="dry")
    calls = []
    out = drain.drain_once(db, size_fn=lambda u: (100, 2), delete_fn=lambda u: None, reclaim_fn=lambda us: calls.append(us) or 1, now=lambda: 200)
    assert (calls, "freed_bytes" in out[0]) == ([], False)
    db2 = FakeD1(FREED_SCHEMA)
    _enqueue(db2, "laptop-real-3", 1, [A])
    drain.drain_once(db2, size_fn=lambda u: (100, 2), delete_fn=lambda u: None, trash_fn=lambda u, r: None, reclaim_fn=lambda us: calls.append(us) or 1, now=lambda: 200)
    assert (calls, db2.query("SELECT freed_bytes FROM deletion_runs")) == ([], [{"freed_bytes": None}])


def test_a_failed_measurement_leaves_freed_bytes_unset():
    db = FakeD1(FREED_SCHEMA)
    _enqueue(db, "laptop-dry-4", 1, [A], mode="dry")

    def boom(us):
        raise OSError("F_LOG2PHYS_EXT unsupported")

    out = drain.drain_once(db, size_fn=lambda u: (100, 2), delete_fn=lambda u: None, reclaim_fn=boom, now=lambda: 200)
    assert ("freed_bytes" in out[0], db.query("SELECT finished_ts, freed_bytes FROM deletion_runs")) == (False, [{"finished_ts": 200, "freed_bytes": None}])
