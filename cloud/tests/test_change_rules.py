"""`dt_cloud.change_rules` (specs/done/change-metrics.md §1.2): the added / deleted / modified classification over hand-made
path versions: the identity rule (token over size over mtime), the mtime rule (`write` | `coarse`), touches, a file → dir
flip, and a run's deltas opened by `op`."""
from __future__ import annotations

import duckdb
import pytest

from dt_cloud import change_rules as cr
from dt_cloud.static_names import OPEN

#: Path versions over stamps 0..4, as a generation's pieces:
#: - a/x: 10 B throughout, its version split at 2 (a read-day change: a touch);
#: - a/y: 20 B, 25 B at 1 (a mod by size), its token first known at 2 with the same size and mtime (a touch), a new
#:   token at 3 (a mod by token), its mtime moved at 4 with the token unchanged (not a mod: the token decides);
#: - a/w: added at 1, deleted at 3;
#: - b/z: deleted at 1, re-created at 2;
#: - b/m: its mtime moved at 2, size and token unknown and unchanged (a mod by mtime where mtimes are write times);
#: - b/f: a file until 3, a directory from then (the file deleted).
ROWS = [
    (2, "a/x", 0, 2, "file", 10, 1, None), (2, "a/x", 2, OPEN, "file", 10, 1, None),
    (2, "a/y", 0, 1, "file", 20, 1, None), (2, "a/y", 1, 2, "file", 25, 2, None), (2, "a/y", 2, 3, "file", 25, 2, "t1"),
    (2, "a/y", 3, 4, "file", 25, 3, "t2"), (2, "a/y", 4, OPEN, "file", 25, 4, "t2"),
    (2, "a/w", 1, 3, "file", 5, 1, None),
    (2, "b/z", 0, 1, "file", 7, 1, None), (2, "b/z", 2, OPEN, "file", 8, 2, "t3"),
    (2, "b/m", 0, 2, "file", 3, 1, None), (2, "b/m", 2, OPEN, "file", 3, 5, None),
    (2, "b/f", 0, 3, "file", 4, 1, None), (2, "b/f", 3, OPEN, "dir", 4, 1, None),
    (1, "a", 0, OPEN, "dir", 0, 0, None), (1, "b", 0, OPEN, "dir", 0, 0, None),
]
TOKEN = cr.token_sql("version")
NO_TOKEN = cr.token_sql(None)


def _pieces(con) -> str:
    con.execute("CREATE TABLE p (depth UTINYINT, path VARCHAR, vf BIGINT, vt BIGINT, kind VARCHAR, size BIGINT, mtime BIGINT, version VARCHAR)")
    con.executemany("INSERT INTO p VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ROWS)
    return "p"


def events(con, src: str, opened: str, *, tok: str, mtime: str, at: tuple[int, ...] | None = None) -> list[tuple]:
    """`(t, path, event, basis, size, psize)` rows, sorted, at the stamps `at` (all when None)."""
    rows = con.execute(f"SELECT * FROM ({cr.events_sql(src, opened, tok=tok, mtime=mtime)}) ORDER BY t, path").fetchall()
    return [r for r in rows if at is None or r[0] in at]


def test_every_event():
    con = duckdb.connect()
    assert events(con, _pieces(con), "true", tok=TOKEN, mtime="write") == [
        (0, "a/x", "added", None, 10, None),
        (0, "a/y", "added", None, 20, None),
        (0, "b/f", "added", None, 4, None),
        (0, "b/m", "added", None, 3, None),
        (0, "b/z", "added", None, 7, None),
        (1, "a/w", "added", None, 5, None),
        (1, "a/y", "modified", "size", 25, 20),
        (1, "b/z", "deleted", None, None, 7),
        (2, "a/x", None, "mtime", 10, 10),  # a split version: a touch
        (2, "a/y", None, "mtime", 25, 25),  # NULL → token, same size and mtime: a touch
        (2, "b/m", "modified", "mtime", 3, 3),
        (2, "b/z", "added", None, 8, None),
        (3, "a/w", "deleted", None, None, 5),
        (3, "a/y", "modified", "token", 25, 25),
        (3, "b/f", "deleted", None, None, 4),  # a file → dir flip: the file deleted
        (4, "a/y", None, "token", 25, 25),  # the mtime moved, the token didn't: a touch
    ]


def test_coarse_mtimes_never_decide_a_mod():
    # b/m's moved mtime is the only change at 2 that an mtime decides: with `coarse` mtimes it's a touch
    con = duckdb.connect()
    assert events(con, _pieces(con), "true", tok=TOKEN, mtime="coarse", at=(2,)) == [
        (2, "a/x", None, "mtime", 10, 10),
        (2, "a/y", None, "mtime", 25, 25),
        (2, "b/m", None, "mtime", 3, 3),
        (2, "b/z", "added", None, 8, None),
    ]


def test_without_tokens_size_then_mtime_decide():
    # versions carrying no token: a/y's token change at 3 isn't seen, but its mtime moved at 3 (2 → 3) and at 4 (3 → 4)
    con = duckdb.connect()
    assert events(con, _pieces(con), "true", tok=NO_TOKEN, mtime="write", at=(3, 4)) == [
        (3, "a/w", "deleted", None, None, 5),
        (3, "a/y", "modified", "mtime", 25, 25),
        (3, "b/f", "deleted", None, None, 4),
        (4, "a/y", "modified", "mtime", 25, 25),
    ]


def test_deltas_open_by_op():
    # a run's deltas at 3: the versions it opened (`op = 1`, open) and the close records (`op = -1`, `vt` = 3)
    con = duckdb.connect()
    con.execute(f"""CREATE TABLE d AS SELECT * FROM (VALUES
        (2::UTINYINT, 'a/y', 3::BIGINT, {OPEN}::BIGINT, 'file', 25::BIGINT, 3::BIGINT, 't2', 1::TINYINT),
        (2::UTINYINT, 'a/y', 2::BIGINT, 3::BIGINT, 'file', 25::BIGINT, 2::BIGINT, 't1', -1::TINYINT),
        (2::UTINYINT, 'a/w', 1::BIGINT, 3::BIGINT, 'file', 5::BIGINT, 1::BIGINT, NULL, -1::TINYINT),
        (2::UTINYINT, 'a/n', 3::BIGINT, {OPEN}::BIGINT, 'file', 6::BIGINT, 3::BIGINT, 't4', 1::TINYINT)
        ) t(depth, path, vf, vt, kind, size, mtime, version, op)""")
    assert events(con, "d", "op = 1", tok=TOKEN, mtime="write") == [
        (3, "a/n", "added", None, 6, None),
        (3, "a/w", "deleted", None, None, 5),
        (3, "a/y", "modified", "token", 25, 25),
    ]


def test_mtime_semantics_are_checked():
    with pytest.raises(ValueError, match=r"^mtime: one of \('write', 'coarse'\), not 'exact'$"):
        cr.events_sql("p", "true", tok=TOKEN, mtime="exact")


def test_changes_compare_object_scans_only():
    # directory-only scans (version 1), the switch to object listings, then object scans: only object → object pairs
    scans = [{"id": f"s{t}", "version": v} for t, v in enumerate([1, 1, 2, 2, 2])]
    assert [cr.comparable(p, s) for p, s in zip(scans, scans[1:])] == [False, False, True, True]
    assert cr.comparable({}, {"version": 2}) is False
