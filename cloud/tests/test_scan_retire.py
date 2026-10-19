"""`dt-cloud scan retire` (`dt_cloud.scan_retire`): which scans it retires (the keep window, both stores' manifests, a
parity marker or a verify run now), what it deletes and keeps per class, the D1 rows it removes (exactly those, against
SQLite with the branch's D1 schema), the backup it takes first, and a dry run changing nothing."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_interval_local import DATES, local, world  # noqa: F401 (fixtures)

from dt_cloud import index_footer as F
from dt_cloud import scan_retire as sr
from dt_cloud.cli import main
from dt_cloud.d1_schema import replay, schema_file

LAYOUT = "idx/{id}/index/{gen}/path-index.parquet"
LAYER2 = "idx/{id}/*.parquet"
#: Every scan's index dir `g1`: the `path` sort (the store's input, a real parquet) and its siblings, sized by name.
SIBLINGS = {"path-index-bysize.parquet": 50, "path-index.rows.parquet": 30, "path-index.trigrams.parquet": 20,
            "age-pyramid-1h.parquet": 7, "age-pyramid-1h.groups.parquet": 3, "attr.tsv": 5, "meta.json": 2}
#: Scans whose dir also holds footers (unparseable stand-ins: only scans that are never verified get them).
FOOTERS = {"path-index.groups.json": 11, "path-index.groups.parquet": 13}
#: An older generation (a v1 index: its `path` sort and a coarse tier), on the first scan.
OLD_GEN = {"path-index.parquet": 9, "path-index-coarse16.parquet": 17, "path-index-coarse16.groups.json": 4}
STATIC_GEN = "s1"


class D1:
    """Every statement runs on SQLite (as D1 runs it)."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def __call__(self, sql, acct, tok, db_id=None):
        cur = self.con.execute(sql)
        rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()] if cur.description else []
        self.con.commit()
        return rows

    def state(self) -> list[tuple]:
        return [
            *(("index_schema", *r) for r in self.con.execute("SELECT date, variant, gen FROM index_schema ORDER BY 1, 2")),
            *(("index_row_groups", *r) for r in self.con.execute("SELECT date, variant, gen, COUNT(*) FROM index_row_groups GROUP BY 1, 2, 3 ORDER BY 1, 2, 3")),
            *(("footer_cache", *r) for r in self.con.execute("SELECT date, variant, gen FROM footer_cache ORDER BY 1, 2")),
        ]


#: Per scan: `(variant, gen, row groups)`; every one has a pointer, and `path` a footer-LRU entry.
SEEDED = {d: [("path", "g1", 3), ("bysize", "g1", 2), ("age-pyramid-1h", "g1", 1)] for d in DATES}
SEEDED[DATES[0]] += [("coarse16", "g0", 2), ("over-time", "g1", 1)]


@pytest.fixture
def d1(monkeypatch) -> D1:
    db = D1(replay([schema_file().read_text()]))
    for d, vs in SEEDED.items():
        for v, g, n in vs:
            db.con.execute("INSERT INTO index_schema (date, variant, version, schema_json, gen, dir) VALUES (?, ?, 2, '{}', ?, ?)", (d, v, g, f"idx/{d}/index/{g}"))
            db.con.executemany("INSERT INTO index_row_groups (date, variant, gen, rg, d_min, d_max, p_min, p_max, b_max, row_start, row_end, rg_json) "
                               "VALUES (?, ?, ?, ?, 1, 1, 'a', 'b', 1, 0, 1, '[1]')", [(d, v, g, i) for i in range(n)])
        db.con.execute("INSERT INTO footer_cache (date, variant, gen, state, started_ts) VALUES (?, 'path', 'g1', 'hot', 0)", (d,))
    db.con.commit()
    monkeypatch.setattr(F, "_d1_query", db)
    monkeypatch.setattr(F, "_creds", lambda: ("tok", "acct"))
    return db


@pytest.fixture
def fx(world, local, tmp_path) -> dict:  # noqa: F811
    """A copy of the built store (base: the first four scans, then four runs), a scans root holding every scan's index
    dir (+ a layer-2 on two scans), a static index listing every scan but 07-31, and parity markers: 07-30, 08-04 and
    08-04T1236 pass, 08-02 fails, 08-03 has none."""
    store = tmp_path / "store"
    shutil.copytree(local["store"], store)
    root = tmp_path / "scans"
    by_id = {s["id"]: s for s in world["scans"]}
    for d in DATES:
        g1 = root / "idx" / d / "index" / "g1"
        g1.mkdir(parents=True)
        shutil.copyfile(world["root"] / by_id[d]["src"], g1 / "path-index.parquet")
        files = {**SIBLINGS, **(FOOTERS if d != "2026-08-03" else {})}
        for name, n in files.items():
            (g1 / name).write_bytes(b"x" * n)
    g0 = root / "idx" / DATES[0] / "index" / "g0"
    g0.mkdir()
    for name, n in OLD_GEN.items():
        (g0 / name).write_bytes(b"x" * n)
    for d in (DATES[0], "2026-08-04T1236"):
        (root / "idx" / d / "b1.parquet").write_bytes(b"x" * 100)
    sgen = store / "static-names" / STATIC_GEN
    (sgen / "manifests").mkdir(parents=True)
    (sgen / "scans.json").write_text(json.dumps({"scans": [{"id": d} for d in DATES[:4] if d != "2026-07-31"]}))
    (sgen / "manifests" / f"{DATES[-1]}.json").write_text(json.dumps({"scans": [d for d in DATES if d != "2026-07-31"]}))
    verify = store / "interval-store" / "g" / "verify"
    verify.mkdir(parents=True)
    for d, ok in (("2026-07-30", True), ("2026-08-02", False), ("2026-08-04", True), ("2026-08-04T1236", True)):
        (verify / f"{d}.json").write_text(json.dumps({"scan": d, "gen": "g", "pass": ok, "views": 9, "view_eq": 9 if ok else 7, "diffs": 0, "diff_eq": 0}))
    return {"store": store, "root": root, "path_sizes": {d: (root / "idx" / d / "index" / "g1" / "path-index.parquet").stat().st_size for d in DATES}}


def _run(fx, *extra: str) -> dict:
    args = ["scan", "retire", "-j", "-G", STATIC_GEN, "-L", LAYOUT, "-l", LAYER2, "-o", str(fx["store"]), "-s", str(fx["root"]), *extra]
    res = CliRunner().invoke(main, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def _files(root: Path) -> dict[str, int]:
    return {p.relative_to(root).as_posix(): p.stat().st_size for p in sorted(root.rglob("*")) if p.is_file()}


def _statuses(report: dict) -> list[tuple]:
    return [(r["id"], r["status"], r["reason"], r["marker"]) for r in report["scans"]]


def _deleted(fx, d: str) -> dict[str, list[int]]:
    """What retiring scan `d` deletes, by class."""
    p = fx["path_sizes"][d]
    out = {"footer": [2, 24], "search": [2, 50], "sort": [2, p + 50]}
    if d == "2026-08-03":
        out["footer"] = None
    if d == DATES[0]:
        out["coarse"] = [2, 21]
        out["sort"] = [3, p + 59]
    if d in (DATES[0], "2026-08-04T1236"):
        out["layer2"] = [1, 100]
    return {k: v for k, v in sorted(out.items()) if v}


KEPT = {"age": [2, 10], "extras": [1, 5], "snapshot": [1, 2]}


def test_dry_run_reports_and_deletes_nothing(fx, d1):
    before, db_before = _files(fx["root"]), d1.state()
    store_before = _files(fx["store"])
    rep = _run(fx)
    assert _statuses(rep) == [
        ("2026-07-30", "retire", None, "pass"),
        ("2026-07-31", "skip", "not in the static index's s1", None),
        ("2026-08-02", "skip", "verify failed (7/9 views, 0/0 diffs equal)", "fail"),
        ("2026-08-03", "needs-verify", None, "missing"),
        ("2026-08-04", "retire", None, "pass"),
        ("2026-08-04T1236", "retire", None, "pass"),
        ("2026-08-05", "keep", "one of the newest 2", None),
        ("2026-08-07", "keep", "one of the newest 2", None),
    ]
    retired = ["2026-07-30", "2026-08-04", "2026-08-04T1236"]
    assert {r["id"]: r["delete"] for r in rep["scans"] if r["delete"]} == {d: _deleted(fx, d) for d in retired}
    assert {r["id"]: r["keep"] for r in rep["scans"] if r["id"] in retired} == {d: KEPT for d in retired}
    assert rep["totals"]["pending"] == _deleted(fx, "2026-08-03")
    coarse = lambda t, d: [("coarse16", "g0", 2 if t == "index_row_groups" else 1)] if d == DATES[0] else []  # noqa: E731
    assert rep["d1"] == [
        {"table": t, "date": d, "variant": v, "gen": g, "rows": n}
        for t, rows in (("footer_cache", lambda d: [("path", "g1", 1)]),
                        ("index_row_groups", lambda d: [("bysize", "g1", 2), *coarse("index_row_groups", d), ("path", "g1", 3)]),
                        ("index_schema", lambda d: [("bysize", "g1", 1), *coarse("index_schema", d)]))
        for d in retired
        for v, g, n in rows(d)
    ]
    assert rep["d1_rows"] == {"footer_cache": 3, "index_row_groups": 17, "index_schema": 4}
    assert (_files(fx["root"]), _files(fx["store"]), d1.state()) == (before, store_before, db_before)


def test_markers_only_and_no_d1(fx, d1):
    rep = _run(fx, "-m", "-R")
    assert [(r["id"], r["status"], r["reason"]) for r in rep["scans"] if r["id"] == "2026-08-03"] == [("2026-08-03", "skip", "no verify marker")]
    assert rep["d1"] is None


def test_no_d1_reports_the_rows_its_scans_still_have(fx, d1):
    """`-R` (a second copy) leaves D1 alone; a retired scan whose pointer rows still exist (its first copy was never
    retired with D1, e.g. a scan only this copy holds) is listed in `d1_left`, so its rows don't outlive its files."""
    plan = _run(fx, "-m")["d1"]
    rep = _run(fx, "-m", "-R")
    assert (rep["d1"], rep["d1_left"], sorted({d["date"] for d in plan})) == (None, plan, ["2026-07-30", "2026-08-04", "2026-08-04T1236"])
    # `-D` (the scan step's copies after its D1 pass): that pass covers the rows, so nothing is reported left
    assert _run(fx, "-m", "-R", "-D")["d1_left"] is None


@pytest.mark.parametrize("rows", [F.D1_DELETE_ROWS, 1])
def test_execute_backs_up_verifies_and_deletes(fx, d1, tmp_path, monkeypatch, rows):
    """With D1 deletes of any batch size (`rows` a statement, until one deletes none): the same rows go."""
    monkeypatch.setattr(F, "D1_DELETE_ROWS", rows)
    before = _files(fx["root"])
    backup = tmp_path / "backup"
    rep = _run(fx, "-x", "-b", str(backup), "-w", str(tmp_path / "work"))
    retired = ["2026-07-30", "2026-08-03", "2026-08-04", "2026-08-04T1236"]
    assert [(r["id"], r["status"], r["marker"]) for r in rep["scans"] if r["status"] == "retire"] == [
        ("2026-07-30", "retire", "pass"), ("2026-08-03", "retire", "verified"), ("2026-08-04", "retire", "pass"), ("2026-08-04T1236", "retire", "pass")]
    marker = json.loads((fx["store"] / "interval-store" / "g" / "verify" / "2026-08-03.json").read_text())
    assert (marker["scan"], marker["gen"], marker["pass"], marker["views"] == marker["view_eq"] > 0) == ("2026-08-03", "g", True, True)
    gone = {k: n for k, n in before.items() if any(k.startswith(f"idx/{d}/") for d in retired) and sr.classify(k.rsplit("/", 1)[-1]) in sr.DELETE
            or k in ("idx/2026-07-30/b1.parquet", "idx/2026-08-04T1236/b1.parquet")}
    assert _files(fx["root"]) == {k: n for k, n in before.items() if k not in gone}
    assert _files(backup) == gone
    assert rep["backup"] == {"objects": len(gone), "bytes": sum(gone.values()), "server_side": False, "present": 0}
    assert rep["deleted"] == {"objects": len(gone), "bytes": sum(gone.values())}
    seeded = [(d, v, g, n) for d, vs in SEEDED.items() for v, g, n in vs]
    assert d1.state() == [
        # Pointers: the sorts' go, but `path` (the site's scan list), the age pyramid's and over-time's stay.
        *sorted(("index_schema", d, v, g) for d, v, g, _ in seeded if not (d in retired and v in ("bysize", "coarse16"))),
        # Row groups: every per-scan sort's go, `path` too.
        *sorted(("index_row_groups", d, v, g, n) for d, v, g, n in seeded if not (d in retired and v in ("path", "bysize", "coarse16"))),
        *(("footer_cache", d, "path", "g1") for d in DATES if d not in retired),
    ]
    assert rep["d1_deleted"] == sum(r["rows"] for r in rep["d1"])


def test_drop_path_pointer(fx, d1):
    _run(fx, "-x", "-m", "-P")
    assert [r for r in d1.state() if r[0] == "index_schema" and r[2] == "path"] == [
        ("index_schema", d, "path", "g1") for d in DATES if d not in ("2026-07-30", "2026-08-04", "2026-08-04T1236")]


LAYER1 = "listing/{id}/*/shard-*.parquet"


def _shard(path: Path, generation: list[int | None]) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    n = len(generation)
    pq.write_table(pa.table({"bucket": ["b"] * n, "name": [f"o{i}" for i in range(n)], "size_bytes": [1] * n,
                             "generation": pa.array(generation, pa.int64())}), path)
    return path.stat().st_size


def test_layer1_goes_unless_it_carries_generation_no_marker_vouches_for(fx, d1):
    """Raw listing shards (`-1`) retire with their scan. One whose shards carry a non-null `generation` keeps them unless
    its marker compared every object's generation equal; an all-null column (S3, an older GCS listing) is no
    generation. Keys the template doesn't match (a sibling cache) stay."""
    root, verify = fx["root"], fx["store"] / "interval-store" / "g" / "verify"
    sizes = {
        "2026-07-30": _shard(root / "listing/2026-07-30/b/shard-000.parquet", [None, None]),
        "2026-08-04": _shard(root / "listing/2026-08-04/b/shard-000.parquet", [None, 7]),
        "2026-08-04T1236": _shard(root / "listing/2026-08-04T1236/b/shard-000.parquet", [5, 6]),
    }
    _shard(root / "listing/2026-07-30/dir-cache/age-days.parquet", [1])
    m = verify / "2026-08-04T1236.json"
    m.write_text(json.dumps({**json.loads(m.read_text()), "generation": {"objects": 2, "equal": 2}}))
    rep = _run(fx, "-m", "-1", LAYER1)
    held = "layer1: its listing has `generation`, which its marker didn't compare equal"
    assert {r["id"]: (r["delete"].get("layer1"), r["keep"].get("layer1"), r.get("held")) for r in rep["scans"] if r["status"] == "retire"} == {
        "2026-07-30": ([1, sizes["2026-07-30"]], None, None),
        "2026-08-04": (None, [1, sizes["2026-08-04"]], held),
        "2026-08-04T1236": ([1, sizes["2026-08-04T1236"]], None, None),
    }
    before = _files(root)
    _run(fx, "-x", "-m", "-1", LAYER1)
    assert sorted(k for k in before if k.startswith("listing/") and k not in _files(root)) == [
        "listing/2026-07-30/b/shard-000.parquet", "listing/2026-08-04T1236/b/shard-000.parquet"]


def test_backup_within_one_store_copies_server_side(monkeypatch, tmp_path):
    """Two buckets of one filesystem: the copy never comes through the host (`get`/`put` refused here), sizes checked."""
    from dt_cloud.blobstore import Store

    src, dst = Store("memory://retire-src"), Store("memory://retire-bk")
    src.write_bytes("a/x.parquet", b"x" * 10)
    src.write_bytes("a/y.parquet", b"y" * 20)

    def refuse(*a, **k):
        raise AssertionError("copied through the host")
    monkeypatch.setattr(Store, "get", refuse)
    monkeypatch.setattr(Store, "put", refuse)
    objs = {"a/x.parquet": 10, "a/y.parquet": 20}
    rep = sr.backup(src, dst, objs, tmp_path)
    assert (rep, dst.listing("a")) == ({"objects": 2, "bytes": 30, "server_side": True, "present": 0}, objs)
    # A rerun (its first run stopped after the backup, e.g. on a D1 failure) copies only what isn't there at its size.
    dst.write_bytes("a/y.parquet", b"y" * 5)
    monkeypatch.setattr(src.fs, "copy", lambda a, b: (_ for _ in ()).throw(AssertionError(f"copied {a}")) if a.endswith("x.parquet") else
                        dst.write_bytes("a/y.parquet", b"y" * 20))
    assert (sr.backup(src, dst, objs, tmp_path), dst.listing("a")) == ({"objects": 2, "bytes": 30, "server_side": True, "present": 1}, objs)


@pytest.mark.parametrize("name,cls", [
    ("path-index.parquet", "sort"), ("path-index-bysize-by-user.parquet", "sort"), ("path-index.v1.parquet", "sort"),
    ("path-index-bysize.groups.parquet", "footer"), ("path-index.groups.json", "footer"),
    ("path-index-coarse20.parquet", "coarse"), ("path-index-coarse20.groups.json", "coarse"),
    ("path-index.rows-search.parquet", "search"), ("path-index.rows.groups.parquet", "search"),
    ("age-pyramid-8d.parquet", "age"), ("age-pyramid-8d.groups.json", "age"), ("age-index.parquet", "age"),
    ("attr.tsv", "extras"), ("attr.tsv.gz", "extras"), ("ck.txt", "extras"), ("meta.json", "snapshot"),
    ("verification.json", "snapshot"), ("over-time.parquet", "over-time"), ("shard-000.parquet", "other"), ("path-index.txt", "other"),
])
def test_classify(name, cls):
    assert sr.classify(name) == cls
