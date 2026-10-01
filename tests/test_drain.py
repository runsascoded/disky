"""Specs for the CP4 drainer (`disk_tree.drain`) and D1 client (`disk_tree.d1`).

`drain_once` runs against a fake in-memory D1 (`FakeD1`, sqlite-backed, same
`?`-placeholder SQL the real client sends) with injected size/delete fns — no
network, no real deletes. `D1Client` is checked against a stubbed `urlopen`."""
from __future__ import annotations

import json
import sqlite3

import pytest

from disk_tree import drain
from disk_tree.d1 import D1Client, D1Error

SCHEMA = """
CREATE TABLE plan_items (plan_id INTEGER, uri TEXT, note TEXT, added_by TEXT, added_ts INTEGER,
    PRIMARY KEY (plan_id, uri));
CREATE TABLE deletion_runs (run_id TEXT PRIMARY KEY, plan_id INTEGER, mode TEXT, actor TEXT,
    started_ts INTEGER, finished_ts INTEGER, deleted_bytes INTEGER DEFAULT 0,
    deleted_objects INTEGER DEFAULT 0, skipped_gone INTEGER DEFAULT 0,
    undo_state TEXT DEFAULT 'none', undo_deadline INTEGER, batch_job TEXT);
CREATE TABLE deletion_bands (run_id TEXT, uri TEXT, bytes INTEGER, objects INTEGER,
    deleted INTEGER, gone INTEGER, PRIMARY KEY (run_id, uri));
"""


class FakeD1:
    """Duck-types `D1Client.query` over in-memory sqlite (`?` placeholders)."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def query(self, sql, params=None):
        cur = self.conn.execute(sql, params or [])
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        self.conn.commit()
        return rows


def _enqueue(db, run_id, plan_id, uris, actor="ryan"):
    db.query(
        "INSERT INTO deletion_runs (run_id, plan_id, mode, actor, started_ts) VALUES (?, ?, 'real', ?, 100)",
        [run_id, plan_id, actor],
    )
    for i, uri in enumerate(uris):
        db.query("INSERT INTO plan_items (plan_id, uri, added_by, added_ts) VALUES (?, ?, ?, 100)", [plan_id, uri, actor])


@pytest.fixture
def db():
    return FakeD1()


def test_drain_executes_enqueued_run_records_bands_and_finishes(db):
    _enqueue(db, "1-x", 1, ["r2://b/a", "r2://b/c"])
    deleted, announced = [], []
    out = drain.drain_once(
        db, size_fn=lambda u: (100, 2), delete_fn=lambda u: deleted.append(u),
        announce=announced.append, now=lambda: 200,
    )
    assert deleted == ["r2://b/a", "r2://b/c"]
    assert out == [{
        "run_id": "1-x", "plan_id": 1, "actor": "ryan", "mode": "real", "items": 2, "deleted_paths": 2,
        "deleted_bytes": 200, "deleted_objects": 4, "errors": [], "finished_ts": 200, "trashed": False,
    }]
    assert announced == out
    run = db.query("SELECT finished_ts, deleted_bytes, deleted_objects, undo_state FROM deletion_runs WHERE run_id = '1-x'")
    assert run == [{"finished_ts": 200, "deleted_bytes": 200, "deleted_objects": 4, "undo_state": "none"}]
    bands = db.query("SELECT uri, bytes, objects, deleted FROM deletion_bands WHERE run_id = '1-x' ORDER BY uri")
    assert bands == [
        {"uri": "r2://b/a", "bytes": 100, "objects": 2, "deleted": 1},
        {"uri": "r2://b/c", "bytes": 100, "objects": 2, "deleted": 1},
    ]


def test_drain_records_a_failed_uri_without_stranding_the_run(db):
    _enqueue(db, "2-y", 2, ["r2://b/ok", "r2://b/bad"])

    def delete_fn(uri):
        if uri.endswith("bad"):
            raise RuntimeError("access denied")

    out = drain.drain_once(db, size_fn=lambda u: (50, 1), delete_fn=delete_fn, now=lambda: 300)
    assert out[0]["deleted_objects"] == 1 and out[0]["deleted_bytes"] == 50
    assert out[0]["errors"] == [("r2://b/bad", "access denied")]
    bands = db.query("SELECT uri, deleted FROM deletion_bands WHERE run_id = '2-y' ORDER BY uri")
    assert bands == [{"uri": "r2://b/bad", "deleted": 0}, {"uri": "r2://b/ok", "deleted": 1}]


def test_drain_undo_state_only_when_something_deleted(db):
    _enqueue(db, "3-z", 3, ["r2://b/x"])
    drain.drain_once(db, size_fn=lambda u: (10, 1), delete_fn=lambda u: None, undo_state="versioning", now=lambda: 400)
    assert db.query("SELECT undo_state FROM deletion_runs WHERE run_id = '3-z'") == [{"undo_state": "versioning"}]


def test_drain_skips_already_finished_runs(db):
    _enqueue(db, "4-a", 4, ["r2://b/x"])
    drain.drain_once(db, size_fn=lambda u: (1, 1), delete_fn=lambda u: None, now=lambda: 500)
    # second pass: the run is finished, so nothing is pending
    again = drain.drain_once(db, size_fn=lambda u: (1, 1), delete_fn=lambda u: None, now=lambda: 600)
    assert again == []


# ---- D1 client -------------------------------------------------------------


def test_d1_query_posts_and_parses(monkeypatch):
    seen = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"success": True, "result": [{"results": [{"run_id": "1-x"}], "success": True}]}).encode()

    def fake_urlopen(req):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        seen["auth"] = req.headers["Authorization"]
        return FakeResp()

    monkeypatch.setattr("disk_tree.d1.urlopen", fake_urlopen)
    d1 = D1Client("acct", "dbid", "tok")
    rows = d1.query("SELECT * FROM deletion_runs WHERE finished_ts IS NULL", [])
    assert rows == [{"run_id": "1-x"}]
    assert seen["url"] == "https://api.cloudflare.com/client/v4/accounts/acct/d1/database/dbid/query"
    assert seen["body"] == {"sql": "SELECT * FROM deletion_runs WHERE finished_ts IS NULL", "params": []}
    assert seen["auth"] == "Bearer tok"


def test_announce_off_returns_no_callback():
    from disk_tree.notify.announce import make_announcer

    assert make_announcer(None) is None
    assert make_announcer({"chat": "none"}) is None


def test_announce_formats_a_run():
    from disk_tree.notify.announce import format_run

    ok = format_run({"run_id": "1-x", "actor": "ryan", "items": 2, "deleted_bytes": 200,
                     "deleted_objects": 4, "errors": []})
    assert ok == ":wastebasket: run `1-x` by ryan: deleted 200 Bytes (4 object(s)) across 2 path(s)"
    with_err = format_run({"run_id": "2-y", "actor": None, "items": 1, "deleted_bytes": 0,
                           "deleted_objects": 0, "errors": [("r2://b/bad", "denied")]})
    assert with_err.split("\n") == [
        ":wastebasket: run `2-y`: deleted 0 Bytes (0 object(s)) across 1 path(s)",
        ":warning: 1 failed:",
        "  • r2://b/bad: denied",
    ]


def test_d1_from_env_reports_missing_vars(monkeypatch):
    for v in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "DISK_TREE_D1_DATABASE_ID"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(D1Error) as e:
        D1Client.from_env()
    assert str(e.value) == "missing env for D1 access: CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, DISK_TREE_D1_DATABASE_ID"
