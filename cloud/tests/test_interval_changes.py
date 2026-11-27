"""`dt_cloud.interval_changes` (specs/change-metrics.md §3.1): each scan's added / deleted / modified objects at the root
and depth 1. The classifier over hand-made path versions (the identity rule, the mtime rule, touches, a file → dir flip);
then over a real store, three ways that must agree with the scans' files and with each other: appended scans' counters
(each run's `meta.json`, carried in the manifest through its carries), a fresh build's (`changes.json`), a compaction's
(carried from the base and the runs), and `backfill-changes`' over a generation without them."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner
from test_interval_compact import BASE, DATES, LAYOUT, add, build, make_world

from dt_cloud import interval_changes as ichg
from dt_cloud import interval_store as ist
from dt_cloud.append_runner import latest_key
from dt_cloud.static_names import OPEN


# ── The classifier ─────────────────────────────────────────────────────────


def _pieces(con, rows: list[tuple]) -> str:
    con.execute("CREATE OR REPLACE TABLE p (depth UTINYINT, path VARCHAR, vf BIGINT, vt BIGINT, kind VARCHAR, size BIGINT, mtime BIGINT, version VARCHAR)")
    con.executemany("INSERT INTO p VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return "p"


#: Path versions over stamps 0..4 (scan `s<t>`), as a generation's pieces:
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
WANT = {1: ("s1", "s0"), 2: ("s2", "s1"), 3: ("s3", "s2"), 4: ("s4", "s3")}


def c(added=0, deleted=0, modified=0, added_bytes=0, deleted_bytes=0, modified_bytes=0, token=0, size=0, mtime=0) -> dict:
    return {"added": added, "deleted": deleted, "modified": modified, "added_bytes": added_bytes, "deleted_bytes": deleted_bytes,
            "modified_bytes": modified_bytes, "mods_by": {"token": token, "size": size, "mtime": mtime}}


def test_counters_classify_every_event():
    con = duckdb.connect()
    got = ichg.counters(con, [(_pieces(con, ROWS), "true")], WANT, identity=ist.OBJECT_VERSION_FIELD, mtime="write")
    assert got == {
        "s1": {"since": "s0", "mtime": "write", "paths": {
            "": c(added=1, deleted=1, modified=1, added_bytes=5, deleted_bytes=7, modified_bytes=25, size=1),
            "a": c(added=1, modified=1, added_bytes=5, modified_bytes=25, size=1),
            "b": c(deleted=1, deleted_bytes=7),
        }},
        "s2": {"since": "s1", "mtime": "write", "paths": {
            "": c(added=1, modified=1, added_bytes=8, modified_bytes=3, mtime=1),
            "b": c(added=1, modified=1, added_bytes=8, modified_bytes=3, mtime=1),
        }},
        "s3": {"since": "s2", "mtime": "write", "paths": {
            "": c(deleted=2, modified=1, deleted_bytes=9, modified_bytes=25, token=1),
            "a": c(deleted=1, modified=1, deleted_bytes=5, modified_bytes=25, token=1),
            "b": c(deleted=1, deleted_bytes=4),
        }},
        "s4": {"since": "s3", "mtime": "write", "paths": {"": c()}},
    }


def test_coarse_mtimes_never_decide_a_mod():
    # b/m's moved mtime is the only change at 2 that an mtime decides: with `coarse` mtimes it's a touch
    con = duckdb.connect()
    got = ichg.counters(con, [(_pieces(con, ROWS), "true")], {2: ("s2", "s1")}, identity=ist.OBJECT_VERSION_FIELD, mtime="coarse")
    assert got == {"s2": {"since": "s1", "mtime": "coarse", "paths": {"": c(added=1, added_bytes=8), "b": c(added=1, added_bytes=8)}}}


def test_without_tokens_size_then_mtime_decide():
    # a generation carrying no version token: a/y's token change at 3 isn't seen (its size and mtime moved only at 1)…
    con = duckdb.connect()
    got = ichg.counters(con, [(_pieces(con, ROWS), "true")], {3: ("s3", "s2"), 4: ("s4", "s3")}, identity=False, mtime="write")
    # … but at 3 its mtime moved (2 → 3) and at 4 (3 → 4): mods by mtime, each
    assert {s: e["paths"][""] for s, e in got.items()} == {
        "s3": c(deleted=2, modified=1, deleted_bytes=9, modified_bytes=25, mtime=1),
        "s4": c(modified=1, modified_bytes=25, mtime=1),
    }


def test_deltas_open_by_op():
    # a run's deltas at 3: the versions it opened (`op = 1`, open) and the close records (`op = -1`, `vt` = 3)
    con = duckdb.connect()
    con.execute(f"""CREATE TABLE d AS SELECT * FROM (VALUES
        (2::UTINYINT, 'a/y', 3::BIGINT, {OPEN}::BIGINT, 'file', 25::BIGINT, 3::BIGINT, 't2', 1::TINYINT),
        (2::UTINYINT, 'a/y', 2::BIGINT, 3::BIGINT, 'file', 25::BIGINT, 2::BIGINT, 't1', -1::TINYINT),
        (2::UTINYINT, 'a/w', 1::BIGINT, 3::BIGINT, 'file', 5::BIGINT, 1::BIGINT, NULL, -1::TINYINT),
        (2::UTINYINT, 'a/n', 3::BIGINT, {OPEN}::BIGINT, 'file', 6::BIGINT, 3::BIGINT, 't4', 1::TINYINT)
        ) t(depth, path, vf, vt, kind, size, mtime, version, op)""")
    got = ichg.counters(con, [("d", "op = 1")], {3: ("s3", "s2")}, identity=ist.OBJECT_VERSION_FIELD, mtime="write")
    assert got == {"s3": {"since": "s2", "mtime": "write", "paths": {
        "": c(added=1, deleted=1, modified=1, added_bytes=6, deleted_bytes=5, modified_bytes=25, token=1),
        "a": c(added=1, deleted=1, modified=1, added_bytes=6, deleted_bytes=5, modified_bytes=25, token=1),
    }}}


def test_counting_starts_after_the_churn_base():
    # directory-only scans (version 1), the switch to object listings, then object scans: only object → object pairs
    scans = [{"id": f"s{t}", "ts": t, "version": v} for t, v in enumerate([1, 1, 2, 2, 2])]
    assert ichg.wanted(scans) == {3: ("s3", "s2"), 4: ("s4", "s3")}
    assert ichg.wanted(scans[2:]) == {3: ("s3", "s2"), 4: ("s4", "s3")}
    assert ichg.wanted(scans[:3]) == {}


# ── Over a store: appends, a fresh build, compaction and the backfill agree with the scans' files ──


def _files(world: dict, j: int) -> dict[str, int]:
    """Scan `j`'s objects: path → size."""
    t = pq.read_table(world["root"] / world["scans"][j]["src"], columns=["path", "kind", "size"]).to_pylist()
    return {r["path"]: r["size"] for r in t if r["kind"] == "file"}


def _oracle(world: dict, through: int) -> dict[str, dict]:
    """The counters from the scans' files alone: every object scan after an object scan, up to `through` (an index)."""
    out = {}
    for j in range(1, through + 1):
        p, s = world["scans"][j - 1], world["scans"][j]
        if p["version"] < 2 or s["version"] < 2:
            continue
        a, b = _files(world, j - 1), _files(world, j)
        paths: dict[str, dict] = {"": c()}

        def bump(path: str, **kw):
            for k in ("", path.split("/", 1)[0]):
                e = paths.setdefault(k, c())
                for f, v in kw.items():
                    if f == "size":
                        e["mods_by"]["size"] += v
                    else:
                        e[f] += v
        for f in b.keys() - a.keys():
            bump(f, added=1, added_bytes=b[f])
        for f in a.keys() - b.keys():
            bump(f, deleted=1, deleted_bytes=a[f])
        for f in a.keys() & b.keys():
            if a[f] != b[f]:
                bump(f, modified=1, modified_bytes=b[f], size=1)
        out[s["id"]] = {"since": p["id"], "mtime": "write", "paths": paths}
    return out


def _doc(gen: Path) -> dict[str, dict]:
    return json.loads((gen / ichg.DOC).read_text())["scans"] if (gen / ichg.DOC).is_file() else {}


def _newest(gen: Path) -> dict:
    return json.loads((gen / latest_key([p.relative_to(gen).as_posix() for p in (gen / "manifests").iterdir()])).read_text())


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ivchg")
    return make_world(tmp / "scans", True)


@pytest.fixture(scope="module")
def appended(world, tmp_path_factory):
    """The base over the first two scans, then each later scan appended by its own `add`, never compacted (its carries
    merge runs)."""
    tmp = tmp_path_factory.mktemp("ivchg-add")
    store = tmp / "store"
    build(world, store, tmp / "work-build", "g", DATES[BASE - 1])
    for d in DATES[BASE:]:
        add(world, store, tmp / "work", "-t", d, "-C", "none")
    return store


def test_the_scans_files_say_what_changed(world):
    # the fixture has every kind of event at the root, at both depth-1 dirs
    want = _oracle(world, len(DATES) - 1)
    assert sorted(want) == DATES[4:]
    assert all(want[s]["paths"][""]["added"] + want[s]["paths"][""]["deleted"] + want[s]["paths"][""]["modified"] > 0 for s in want)


def test_appends_count_each_scan_from_its_deltas(world, appended):
    """Each appended scan's counters (its run's `meta.json`, carried in the newest manifest through the carries): the
    scans' files' changes exactly; none for the directory-only scan nor the switch to object listings."""
    gen = appended / ist.PREFIX / "g"
    m = _newest(gen)
    assert m[ichg.KEY] == _oracle(world, len(DATES) - 1)
    assert _doc(gen) == {}  # the base's scans are directory-only: nothing to count
    # a merged run's `meta.json` holds its inputs' counters
    for r in m["runs"]:
        meta = json.loads((gen / r["key"] / "meta.json").read_text())
        assert meta.get(ichg.KEY, {}) == {s: e for s, e in m[ichg.KEY].items() if s in r["scans"]}


def test_a_fresh_build_counts_the_same(world, appended, tmp_path):
    """A fresh build over every scan counts from its folded pieces what the appends counted from their deltas."""
    build(world, tmp_path / "store", tmp_path / "work", "f", DATES[-1])
    assert _doc(tmp_path / "store" / ist.PREFIX / "f") == _newest(appended / ist.PREFIX / "g")[ichg.KEY]


@pytest.mark.parametrize("gen,through", [("g.c001", 5), ("g.c002", 9)])
def test_compaction_carries_the_counters(world, tmp_path, gen, through):
    """Compacted at level 2 (every four appended scans): each new generation's `changes.json` holds the base's and the
    runs' counters, a fresh build's."""
    store = tmp_path / "store"
    build(world, store, tmp_path / "work-build", "g", DATES[BASE - 1])
    for d in DATES[BASE:through + 1]:
        add(world, store, tmp_path / "work", "-t", d)
    got = _doc(store / ist.PREFIX / gen)
    assert got == _oracle(world, through)
    build(world, tmp_path / "fresh", tmp_path / "work-fresh", "f", DATES[through])
    assert (store / ist.PREFIX / gen / ichg.DOC).read_text() == (tmp_path / "fresh" / ist.PREFIX / "f" / ichg.DOC).read_text()


def test_backfill_writes_what_appends_never_did(world, appended, tmp_path):
    """A generation whose base and runs carry no counters (a copy with the manifests' `changes` and the runs' stripped):
    `backfill-changes` writes `changes.json` with every counted scan, from the base's pieces and the runs' deltas, the
    appends' counters exactly, and touches nothing else; a second backfill is refused."""
    store = tmp_path / "store"
    shutil.copytree(appended, store)
    gen = store / ist.PREFIX / "g"
    for p in [*(gen / "manifests").iterdir(), *gen.glob("deltas/*/meta.json")]:
        d = json.loads(p.read_text())
        p.write_text(json.dumps({k: v for k, v in d.items() if k != ichg.KEY}, indent=1) + "\n")
    before = {p.relative_to(store).as_posix(): p.read_bytes() for p in sorted(store.rglob("*")) if p.is_file()}
    args = ["-g", "g", "-o", str(store), "-w", str(tmp_path / "work"), "-p", "2", "-M", "1GB"]
    res = CliRunner().invoke(ichg.backfill_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    after = {p.relative_to(store).as_posix(): p.read_bytes() for p in sorted(store.rglob("*")) if p.is_file()}
    assert sorted(after.keys() - before.keys()) == [f"{ist.PREFIX}/g/{ichg.DOC}"]
    assert {k: v for k, v in after.items() if k in before} == before
    assert _doc(gen) == _oracle(world, len(DATES) - 1)
    again = CliRunner().invoke(ichg.backfill_cmd, args)
    assert (again.exit_code, str(again.exception)) == (1, f"{store}/{ist.PREFIX}/g/{ichg.DOC} exists: a generation's counters are written once")


def test_the_identity_holds(world):
    """Every scan's added − deleted is its change in objects, at the root and each depth-1 path (the conservation law)."""
    want = _oracle(world, len(DATES) - 1)
    for j, s in enumerate(world["scans"]):
        if s["id"] not in want:
            continue
        a, b = _files(world, j - 1), _files(world, j)
        for p, e in want[s["id"]]["paths"].items():
            n = lambda fs: sum(1 for f in fs if p == "" or f.split("/", 1)[0] == p)  # noqa: E731
            assert e["added"] - e["deleted"] == n(b) - n(a), (s["id"], p)
