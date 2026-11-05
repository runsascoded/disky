"""What a run's row says after `dt-cloud sweep undo`: `deletion_runs.undo_state` and `deletion_bands.undone_objects` hold
only what the undo has seen restored, written as it goes. An undo refused up front, or dead before its first restore, leaves
the row as it was; one that dies part way leaves `partial` and the counts it had reported."""

from __future__ import annotations

import sqlite3
from collections import Counter

import pytest
from click.testing import CliRunner

from dt_cloud import cli, index_footer, sweep_exec
from test_sweep_exec import _real_run_dir, _UndoClient, _UndoHandle

RUN = "2026-09-01-p12/20260901T000000Z"
A, B = "gs://b1/a/", "gs://b1/b/"

#: `deletion_runs` / `deletion_bands` as the undo reads and writes them, `undo_state`'s CHECK as in the schema.
TABLES = """
CREATE TABLE deletion_runs (
  run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, started_ts INTEGER NOT NULL, finished_ts INTEGER, deleted_objects INTEGER NOT NULL DEFAULT 0,
  undo_deadline INTEGER, undo_state TEXT NOT NULL DEFAULT 'none' CHECK (undo_state IN ('none', 'partial', 'full', 'expired')),
  log_dir TEXT NOT NULL, min_soft_delete_days INTEGER, soft_delete_days INTEGER
);
CREATE TABLE deletion_bands (
  run_id TEXT NOT NULL REFERENCES deletion_runs(run_id), prefix TEXT NOT NULL, objects INTEGER NOT NULL, undone_objects INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, prefix)
);
"""


class _Killed(_UndoHandle):
    """…whose job dies at its `die_at`-th restore call: that call and every later one restore nothing (as a killed job
    does: no `except Exception` sees it)."""

    def __init__(self, die_at: int, **kw):
        super().__init__(**kw)
        self.die_at = die_at
        #: The names that did come back.
        self.restored: list[str] = []

    def restore_blob(self, name, generation=None, if_generation_match=None):
        if len(self.calls) + 1 >= self.die_at:
            self.calls.append((name, generation, if_generation_match))
            raise KeyboardInterrupt
        blob = super().restore_blob(name, generation=generation, if_generation_match=if_generation_match)
        self.restored.append(name)
        return blob


@pytest.fixture
def undo(tmp_path, monkeypatch):
    """`sweep undo RUN` against a finished real run of 4 deleted objects (bands `a/` 3, `b/` 1) and its D1 row; returns
    `(result, restore calls, (undo_state, {band: undone_objects}))`."""
    log_dir = _real_run_dir(tmp_path)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(TABLES)
    db.execute("INSERT INTO deletion_runs (run_id, mode, started_ts, finished_ts, deleted_objects, undo_deadline, log_dir) VALUES (?, 'real', 1, 2, 4, NULL, ?)", (RUN, str(log_dir)))
    db.executemany("INSERT INTO deletion_bands (run_id, prefix, objects) VALUES (?, ?, ?)", [(RUN, A, 3), (RUN, B, 1)])
    sent: list[str] = []

    def query(sql, *_, **__):
        sent.append(sql)
        if sql.lstrip().upper().startswith("SELECT"):
            return [dict(r) for r in db.execute(sql)]
        db.executescript(sql)
        return []

    monkeypatch.setattr(index_footer, "_d1_query", query)
    monkeypatch.setattr(index_footer, "_creds", lambda: ("token", "account"))
    monkeypatch.setattr(cli, "_hard_exit", lambda: None)

    def run(handle: _UndoHandle, *flags: str, state: str = "none", off: tuple[str, ...] = ()):
        db.execute("UPDATE deletion_runs SET undo_state = ?", (state,))
        monkeypatch.setattr("google.cloud.storage.Client", lambda: _UndoClient(handle, off=off))
        result = CliRunner().invoke(cli.main, ["sweep", "undo", "-w", "1", *flags, RUN])
        row = (db.execute("SELECT undo_state FROM deletion_runs").fetchone()[0], dict(db.execute("SELECT prefix, undone_objects FROM deletion_bands ORDER BY prefix").fetchall()))
        return result, handle.calls, row

    run.sent = sent
    return run


def test_a_refused_undo_leaves_the_row_as_it_was(undo):
    """Soft delete off now, asked of GCS: refused by name before anything is restored, and nothing is written."""
    result, calls, row = undo(_UndoHandle(), off=("b1",))
    assert (result.exit_code, str(result.exception), calls, row) == (
        1,
        "undo unavailable: soft delete is off on b1 (GCS, now): it restores nothing from a bucket with no soft-delete policy",
        [],
        ("none", {A: 0, B: 0}),
    )
    assert [sql.split(" ", 1)[0] for sql in undo.sent] == ["SELECT"]


def test_an_undo_killed_as_its_first_restore_goes_out_leaves_the_row_as_it_was(undo):
    handle = _Killed(1)
    result, _, row = undo(handle)
    assert (result.exit_code, handle.restored, row) == (1, [], ("none", {A: 0, B: 0}))
    assert [sql.split(" ", 1)[0] for sql in undo.sent] == ["SELECT"]


def test_an_undo_killed_part_way_reads_partly_undone_with_what_it_had_reported(undo, monkeypatch):
    """Reports go out every `UNDO_PROGRESS_ROWS` restores: two settle and are reported, then it dies."""
    monkeypatch.setattr(sweep_exec, "UNDO_PROGRESS_ROWS", 2)
    handle = _Killed(3)
    result, _, row = undo(handle)
    assert (result.exit_code, handle.restored, row) == (1, ["a/x", "a/y"], ("partial", {A: 2, B: 0}))


def test_an_undo_that_brought_back_some_reads_partly_undone_with_their_count(undo):
    """`a/x` restored, `a/y` live already, `a/z` gone for good, `b/w` failed: not everything is live, one was restored."""
    result, _, row = undo(_UndoHandle(live={"a/y"}, expired={"a/z"}, broken={"b/w"}))
    assert (result.exit_code, result.exception, row) == (0, None, ("partial", {A: 1, B: 0}))


def test_an_undo_that_brought_back_everything_reads_undone(undo):
    result, _, row = undo(_UndoHandle())
    assert (result.exit_code, result.exception, row) == (0, None, ("full", {A: 3, B: 1}))


def test_an_undo_that_found_nothing_live_again_leaves_the_row_as_it_was(undo):
    """Every soft-deleted copy gone: the undo ran to its end and nothing is back, so the run is not "partly undone"."""
    result, calls, row = undo(_UndoHandle(expired={"a/x", "a/y", "a/z", "b/w"}))
    assert (result.exit_code, result.exception, len(calls), row) == (0, None, 4, ("none", {A: 0, B: 0}))
    assert [sql.split(" ", 1)[0] for sql in undo.sent] == ["SELECT"]


def test_an_undo_of_part_of_an_undone_run_leaves_it_undone(undo):
    """`-p` covers less than the run, so it can't say everything is live: it never takes `full` back to `partial`."""
    result, calls, row = undo(_UndoHandle(live={"b/w"}), "-p", "b1/b/", state="full")
    assert (result.exit_code, result.exception, calls, row) == (0, None, [("b/w", 14, 0)], ("full", {A: 0, B: 0}))


def test_undo_record_writes_each_report_once_and_a_failed_write_with_the_next(monkeypatch):
    sent: list[str] = []
    down = [True]

    def query(sql, acct, tok):
        if down[0]:
            raise RuntimeError("D1 query failed (500)")
        sent.append(sql)

    said: list[str] = []
    monkeypatch.setattr(index_footer, "_creds", lambda: ("tok", "acct"))
    monkeypatch.setattr(index_footer, "_d1_query", query)
    monkeypatch.setattr(sweep_exec, "err", lambda text, **_: said.append(text))
    record = sweep_exec.UndoRecord("r")
    record({A: Counter(restored=2, unrestorable=1)})  # D1 down: kept
    down[0] = False
    record({A: Counter(bulk_restored=1), B: Counter(failed=1)})
    record({B: Counter(already_live=1)})  # nothing new to write: the state is marked, no restore to count
    assert (record.finish({}, 5), record.finish({"log_partial": True}, 4), sweep_exec.UndoRecord("q").finish({}, 4)) == ("partial", "partial", None)
    record({B: Counter(restored=1)})
    assert record.finish({}, 5) == "full"
    assert sent == [
        "UPDATE deletion_runs SET undo_state = 'partial' WHERE run_id = 'r' AND undo_state != 'full'; "
        "UPDATE deletion_bands SET undone_objects = undone_objects + 3 WHERE run_id = 'r' AND prefix = 'gs://b1/a/'",
        "UPDATE deletion_runs SET undo_state = 'partial' WHERE run_id = 'r' AND undo_state != 'full'",
        "UPDATE deletion_runs SET undo_state = 'partial' WHERE run_id = 'r' AND undo_state != 'full'",
        "UPDATE deletion_bands SET undone_objects = undone_objects + 1 WHERE run_id = 'r' AND prefix = 'gs://b1/b/'",
        "UPDATE deletion_runs SET undo_state = 'full' WHERE run_id = 'r'",
    ]
    assert said == ["WARN: undo progress not recorded (D1 query failed (500)); it goes with the next report"]
