"""`dt_cloud.static_compact` through the shared orchestrator (`append_runner.compact_store`): a generation's base and its
runs folded into `<gen>.c<NNN>` is `static-names build` through the same scans over the base's key ranges (`build -r`),
every file md5-equal (but `catalog/meta.json`'s seconds), for every fixture flavor: two level-0 runs (compacted by `add`
when level 1 is due), one merged run (`static-names compact`), and again after a scan appended to the compacted
generation (`add -g <old>` follows `successor.json`; `add -K`)."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from dt_cloud import append_runner as ar
from dt_cloud import static_compact as sk
from dt_cloud import static_drill as sd
from dt_cloud import static_local as sl
from dt_cloud import static_measure as sme
from dt_cloud import static_names as sn
from dt_cloud import static_roots as sr

from test_static_local import _build, _layout, _small_groups
from test_static_names import fixture, scan_ids  # noqa: F401


def _add(root: Path, scans: dict, store: Path, work: Path, through: str, *extra: str) -> dict:
    res = CliRunner().invoke(sl.add_cmd, ["-g", "g", "-L", _layout(scans), "-s", str(root), "-o", str(store), "-w", str(work), "-p", "2", "-M", "1GB",
                                          "-t", through, *extra], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def _compact(store: Path, work: Path, *extra: str) -> dict:
    res = CliRunner().invoke(sk.compact_cmd, ["-g", "g", "-o", str(store), "-w", str(work), "-p", "2", "-M", "1GB", *extra], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


#: What a compacted generation has that a build doesn't (its record, its manifests, the runs appended to it since, its
#: successor record), and the catalog's `meta.json`
#: (its `s`: seconds, which no two builds share either), compared apart.
OWN = ("compaction.json", "manifests/", "catalog/meta.json", "deltas/", ar.SUCCESSOR)


def _md5s(d: Path) -> dict[str, str]:
    return {k: hashlib.md5(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*")) if p.is_file() and not (k := p.relative_to(d).as_posix()).startswith(OWN)}


def _meta(d: Path) -> dict:
    return {k: v for k, v in json.loads((d / "catalog" / "meta.json").read_text()).items() if k != "s"}


@pytest.fixture(scope="module")
def world(fixture, tmp_path_factory):  # noqa: F811
    root, scans, _ = fixture
    ids = scan_ids(scans)
    tmp = tmp_path_factory.mktemp("compact")
    out: dict = {"root": root, "scans": scans, "ids": ids, "tmp": tmp}
    with _small_groups():
        # A: two level-0 runs (level 1: never a carry; due once two runs are at level 0) → `add` compacts into g.c001;
        # then a scan appended to g.c001 (`-g g` follows the successor) and `-K` → g.c002.
        a = tmp / "a"
        _build(root, scans, a, tmp / "wa0", through=ids[1], extra=("-u", "static-names/primary-CURRENT.json"))
        out["a"] = [_add(root, scans, a, tmp / "wa1", ids[2], "-C", "1"), _add(root, scans, a, tmp / "wa2", ids[3], "-C", "1"),
                    _add(root, scans, a, tmp / "wa3", ids[4], "-C", "1", "-K", "-u", "static-names/primary-CURRENT.json")]
        # B: one merged run (level 5: the two carry into an L1) → `static-names compact`.
        b = tmp / "b"
        _build(root, scans, b, tmp / "wb0", through=ids[1])
        out["b_add"] = _add(root, scans, b, tmp / "wb1", ids[3])
        out["b"] = _compact(b, tmp / "wb2")
        # C: B again, its drill folded in chunks of 3 roots (many tuple chunks, runs spilled per chunk, q ranges): the same bytes
        c = tmp / "c"
        _build(root, scans, c, tmp / "wc0", through=ids[1])
        _add(root, scans, c, tmp / "wc1", ids[3])
        out["c"] = _compact(c, tmp / "wc2", "-c", "3")
        # D, E: C again with the runs' chunk spill in 2 partitions, then 1 (each split by chunk on reading); F, H: C and E
        # with 4 chunks folded at a time (`-j 4`): the same bytes
        for name, parts, jobs in (("d", 2, 1), ("e", 1, 1), ("f", None, 4), ("h", 1, 4)):
            d = tmp / name
            _build(root, scans, d, tmp / f"w{name}0", through=ids[1])
            _add(root, scans, d, tmp / f"w{name}1", ids[3])
            old = sr.SPILL_PARTS, sr.SPILL_DIRECT
            if parts is not None:
                sr.SPILL_PARTS, sr.SPILL_DIRECT = parts, 0
            try:
                out[name] = _compact(d, tmp / f"w{name}2", "-c", "3", "-j", str(jobs))
            finally:
                sr.SPILL_PARTS, sr.SPILL_DIRECT = old
            out[f"{name}_store"] = d / sn.PREFIX
        # I: two level-0 runs (`add -N`: no carry), folded 4 chunks (and both runs' spills) at a time: the same bytes
        i = tmp / "i"
        _build(root, scans, i, tmp / "wi0", through=ids[1])
        _add(root, scans, i, tmp / "wi1", ids[3], "-N")
        out["i_runs"] = [r["key"] for r in ar.newest_runs(i / sn.PREFIX / "g")[1]]
        out["i"] = _compact(i, tmp / "wi2", "-c", "3", "-j", "4")
        out["i_store"] = i / sn.PREFIX
        # J: C with the drill's roots staged on the work disk (`-s on`: copied from the store, the generation's mirror
        # notwithstanding) and 4 chunks at a time: the same bytes
        j = tmp / "j"
        _build(root, scans, j, tmp / "wj0", through=ids[1])
        _add(root, scans, j, tmp / "wj1", ids[3])
        out["j_runs"] = [r["key"] for r in ar.newest_runs(j / sn.PREFIX / "g")[1]]
        out["j"] = _compact(j, tmp / "wj2", "-c", "3", "-j", "4", "-s", "on")
        out["j_store"] = j / sn.PREFIX
        ranges = str(a / sn.PREFIX / "g" / "ranges.json")
        for gen, through in (("g.c001", ids[3]), ("g.c002", ids[4])):
            _build(root, scans, tmp / f"ref-{gen}", tmp / f"wr-{gen}", gen=gen, through=through, extra=("-r", ranges))
    out.update(a_store=a / sn.PREFIX, b_store=b / sn.PREFIX, c_store=c / sn.PREFIX)
    return out


def test_add_compacts_when_due_then_appends_to_the_successor(world):
    ids = world["ids"]
    first, second, third = world["a"]
    assert [(o["gen"], [x["scan"] for x in o["added"]], o["compact"] and (o["compact"]["gen"], o["compact"]["why"], o["compact"]["runs"]))
            for o in (first, second, third)] == [
        ("g", [ids[2]], None),
        ("g", [ids[3]], ("g.c001", f"level 1: two runs at level 0 ({ar.run_key(ids[2], ids[2])}, {ar.run_key(ids[3], ids[3])})",
                         [ar.run_key(ids[2], ids[2]), ar.run_key(ids[3], ids[3])])),
        ("g.c001", [ids[4]], ("g.c002", "forced (-K)", [ar.run_key(ids[4], ids[4])])),
    ]
    root = world["a_store"]
    assert [json.loads((root / g / ar.SUCCESSOR).read_text()) for g in ("g", "g.c001")] == [{"gen": "g.c001"}, {"gen": "g.c002"}]
    assert json.loads((root / ar.CURRENT).read_text()) == {"gen": "g.c002"}
    # the old generations stay (readers pin STATIC_GEN; `store-gc -k` collects them)
    assert [(root / g / "scans.json").is_file() for g in ("g", "g.c001")] == [True, True]


@pytest.mark.parametrize("store, gen, ref", [("a_store", "g.c001", "g.c001"), ("a_store", "g.c002", "g.c002"), ("b_store", "g.c001", "g.c001"),
                                             ("c_store", "g.c001", "g.c001"), ("d_store", "g.c001", "g.c001"), ("e_store", "g.c001", "g.c001"),
                                             ("f_store", "g.c001", "g.c001"), ("h_store", "g.c001", "g.c001"), ("i_store", "g.c001", "g.c001"),
                                             ("j_store", "g.c001", "g.c001")])
def test_compacted_generation_is_the_rebuild(world, store, gen, ref):
    got, want = world[store] / gen, world["tmp"] / f"ref-{ref}" / sn.PREFIX / ref
    g, w = _md5s(got), _md5s(want)
    assert sorted(g) == sorted(w)
    assert g == w
    assert _meta(got) == _meta(want)
    assert len(w) > 40


@pytest.mark.parametrize("store, gen", [("a_store", "g.c001"), ("a_store", "g.c002"), ("b_store", "g.c001"), ("i_store", "g.c001")])
def test_incremental_measure_is_the_rebuilds(world, tmp_path, store, gen):
    """specs/static-incremental-compaction.md step 2: a compaction's `roots-measure/` from its base's measurement and the
    runs' counts of new roots (headers, `dcount`, the floor crossers' base counts: `static_measure.incremental_measure`)
    is the rebuild's, md5 for md5: two level-0 runs (A, I), one over a compacted base (A's `g.c002`), one merged run (B)."""
    root = world[store]
    rec = json.loads((root / gen / ar.COMPACTION).read_text())
    base = root / rec["from"]
    tiers = [sd.Tier(base / "drill", "base"), *(sd.Tier(base / k / "drill", k) for k in rec["runs"])]
    con = sn.connect(2, "1GB", tmp_path / "duckdb")
    sme.incremental_measure(con, tiers, base / "roots-measure", str(root / gen / "catalog" / "members.parquet"), tmp_path / "rm",
                            floor=tiers[0].meta.get("floor", sl.FLOOR), log=lambda s: None)
    got, want = _md5s(tmp_path / "rm"), _md5s(world["tmp"] / f"ref-{gen}" / sn.PREFIX / gen / "roots-measure")
    assert sorted(want) == ["dirs/s0000.parquet", "short/dirs/s0000.parquet"]
    assert got == want


def test_compactions_measure_and_roll_up_incrementally(world):
    """specs/static-incremental-compaction.md step 4: every fixture run counts only new roots (`dcount: fresh`), so each
    compaction above (whose bytes `test_compacted_generation_is_the_rebuild` checks) took its measurement and rollups from
    base ⊕ runs, each range's roots only sorted."""
    assert [world[k]["drill"]["measure"] for k in ("b", "c", "d", "e", "f", "h", "i")] == ["incremental"] * 7
    assert [o["compact"]["drill"]["measure"] for o in world["a"][1:]] == ["incremental"] * 2


def test_a_run_counting_closes_measures_every_root_to_the_same_bytes(world, tmp_path):
    """C's fold with its runs' `dcount: fresh` marks removed (drills built before it): the fold measures and rolls up
    every root instead, and writes the incremental fold's files."""
    gen_dir = tmp_path / "g"
    shutil.copytree(world["c_store"] / "g", gen_dir)
    _, runs = ar.newest_runs(gen_dir)
    for r in runs:
        f = gen_dir / r["key"] / "drill" / "meta.json"
        f.write_text(json.dumps({k: v for k, v in json.loads(f.read_text()).items() if k != "dcount"}))
    with _small_groups():
        doc = sk.fold_gen(gen_dir, tmp_path / "out", tmp_path / "w", gen="g", new_gen="g.c001", runs=runs, threads=2, mem="1GB", chunk_rows=3)
    assert doc["drill"]["measure"] == "roots"
    got, want = _md5s(tmp_path / "out"), _md5s(world["c_store"] / "g.c001")
    assert sorted(got) == sorted(want)
    assert got == want


def test_compactions_find_their_classes_from_digests(world):
    """specs/static-incremental-compaction.md step 3: every fixture base has its members' digests and every run its
    classes' changes, so each compaction above (whose bytes `test_compacted_generation_is_the_rebuild` checks) took its
    classes from them, and no tuple's roots disagreed with its count."""
    assert [(world[k]["drill"]["classes"], world[k]["drill"]["fixed"]) for k in ("b", "c", "d", "e", "f", "h", "i", "j")] == [("digests", 0)] * 8
    assert [(o["compact"]["drill"]["classes"], o["compact"]["drill"]["fixed"]) for o in world["a"][1:]] == [("digests", 0)] * 2


def _tiers(root: Path, gen: str) -> list[sd.Tier]:
    """The tiers compaction `gen` (in store `root`) folded: its base's drill, then its runs'."""
    rec = json.loads((root / gen / ar.COMPACTION).read_text())
    base = root / rec["from"]
    return [sd.Tier(base / "drill", "base"), *(sd.Tier(base / k / "drill", k) for k in rec["runs"])]


@pytest.mark.parametrize("store, gen", [("a_store", "g.c001"), ("a_store", "g.c002"), ("b_store", "g.c001"), ("c_store", "g.c001"),
                                        ("i_store", "g.c001")])
def test_digests_are_the_digest_passes(world, tmp_path, store, gen):
    """Each member's digest from its base's digests plus the runs' changes (`member_digests`) is the rebuild's digest pass,
    member for member, and so are the classes they give (`min(q)` per digest: the rebuild's `aliases.parquet`): two
    level-0 runs (A, I), a run over a compacted base (A's `g.c002`), one merged run (B, C)."""
    root, ref = world[store], world["tmp"] / f"ref-{gen}" / sn.PREFIX / gen / "drill"
    con = sn.connect(2, "1GB", tmp_path / "duckdb")
    sk.member_classes(con, _tiers(root, gen), str(root / gen / "catalog" / "members.parquet"))
    sk.member_digests(con, _tiers(root, gen), "mdig")
    want = [tuple(r.values()) for r in pq.read_table(ref / sd.DIGESTS).to_pylist()]
    assert con.execute("SELECT q, n, h1, h2 FROM mdig ORDER BY q").fetchall() == want
    assert len(want) > 10
    aliases = [(r["q"], r["canonical"]) for r in pq.read_table(ref / "aliases.parquet", columns=["q", "canonical"]).to_pylist()]
    assert con.execute("SELECT q, min(q) OVER (PARTITION BY n, h1, h2) FROM mdig ORDER BY q").fetchall() == aliases


@pytest.mark.parametrize("drop", ["run marks", "base digests"])
def test_a_tier_without_digests_finds_classes_from_every_root_to_the_same_bytes(world, tmp_path, drop):
    """C's fold with its runs' `digests: delta` marks removed (drills built before them), or its base's digests gone (a
    generation built before them): the fold digests every root instead, and writes the same files (its own digests
    among them)."""
    gen_dir = tmp_path / "g"
    shutil.copytree(world["c_store"] / "g", gen_dir)
    _, runs = ar.newest_runs(gen_dir)
    if drop == "run marks":
        for r in runs:
            f = gen_dir / r["key"] / "drill" / "meta.json"
            f.write_text(json.dumps({k: v for k, v in json.loads(f.read_text()).items() if k != "digests"}))
    else:
        (gen_dir / "drill" / sd.DIGESTS).unlink()
    with _small_groups():
        doc = sk.fold_gen(gen_dir, tmp_path / "out", tmp_path / "w", gen="g", new_gen="g.c001", runs=runs, threads=2, mem="1GB", chunk_rows=3)
    assert (doc["drill"]["classes"], doc["drill"]["fixed"], doc["drill"]["measure"]) == ("roots", 0, "incremental")
    got, want = _md5s(tmp_path / "out"), _md5s(world["c_store"] / "g.c001")
    assert sorted(got) == sorted(want)
    assert got == want


def test_a_wrong_change_is_corrected_from_the_roots(world, tmp_path):
    """C's run with one class's change off by one root (a change that wasn't exact): every tuple holding that class there
    fails its count check, takes its digest from its roots (the result's `fixed`: those members), and the fold writes
    the same files."""
    gen_dir = tmp_path / "g"
    shutil.copytree(world["c_store"] / "g", gen_dir)
    _, runs = ar.newest_runs(gen_dir)
    drill = gen_dir / runs[-1]["key"] / "drill"
    delta = pq.read_table(drill / sd.DIGESTS).to_pylist()
    bad = delta[0]["q"]
    pq.write_table(pa.Table.from_pylist([{**r, "dn": r["dn"] + (r["q"] == bad)} for r in delta], schema=sd.DIGEST_DELTA_SCHEMA), drill / sd.DIGESTS)
    canon = {r["q"]: r["canonical"] for r in pq.read_table(drill / "aliases.parquet").to_pylist()}
    members = pq.read_table(world["c_store"] / "g.c001" / "catalog" / "members.parquet", columns=["q"]).column("q").to_pylist()
    with _small_groups():
        doc = sk.fold_gen(gen_dir, tmp_path / "out", tmp_path / "w", gen="g", new_gen="g.c001", runs=runs, threads=2, mem="1GB", chunk_rows=3)
    assert (doc["drill"]["classes"], doc["drill"]["fixed"]) == ("digests", sum(canon.get(m) == bad for m in members))
    assert doc["drill"]["fixed"] > 0
    assert _md5s(tmp_path / "out") == _md5s(world["c_store"] / "g.c001")


def _digest(con, rows: list[tuple]) -> tuple[int, int, int]:
    """Rows `(path, usr, vf, vt, size, n_files)`' digest, as the build's digest pass sums it."""
    con.register("dig_in", pa.table({k: pa.array([r[i] for r in rows], t) for i, (k, t) in enumerate(
        (("path", pa.string()), ("usr", pa.string()), ("vf", pa.int64()), ("vt", pa.int64()), ("size", pa.int64()), ("n_files", pa.int64())))}))
    try:
        return con.execute(f"""SELECT count(*), coalesce(sum({sr.ROW_HASH}), 0) % {sn.U64}, coalesce(sum(hash({sr.ROW_HASH}, 'x')), 0) % {sn.U64}
            FROM dig_in""").fetchone()
    finally:
        con.unregister("dig_in")


def test_a_runs_change_is_its_classes_after_less_before(tmp_path):
    """`day_digests`: an existing member's open adds its root; its close record turns the open root it restates into the
    closed one (the count unchanged); a new member's every root is new, closed history included. Each class's change is
    its digest after the scan less its digest before, mod 2⁶⁴."""
    import duckdb

    D, D0, D1, O = 2000, 1000, 1500, sn.OPEN
    con = duckdb.connect()
    con.execute(f"CREATE TABLE dr ({sr.ROOT_COLS}); CREATE TABLE newq (q VARCHAR)")
    con.execute(f"""INSERT INTO dr VALUES ('abc', 2, 'a/abc', 'u', {D}, {O}, 5, 1), ('abc', 2, 'b/abc', 'u', {D0}, {D}, 7, 2),
        ('xyz', 1, 'xyz', 'v', {D0}, {D1}, 3, 1), ('xyz', 1, 'xyz', 'v', {D1}, {O}, 4, 1)""")
    con.execute("INSERT INTO newq VALUES ('xyz')")
    assert sd.day_digests(con, tmp_path, D) is None
    before = {"abc": [("b/abc", "u", D0, O, 7, 2)], "xyz": []}
    after = {"abc": [("a/abc", "u", D, O, 5, 1), ("b/abc", "u", D0, D, 7, 2)], "xyz": [("xyz", "v", D0, D1, 3, 1), ("xyz", "v", D1, O, 4, 1)]}
    want = []
    for c in ("abc", "xyz"):
        (na, a1, a2), (nb, b1, b2) = _digest(con, after[c]), _digest(con, before[c])
        want.append({"q": c, "dn": na - nb, "d1": (a1 - b1) % sn.U64, "d2": (a2 - b2) % sn.U64})
    assert pq.read_table(tmp_path / sd.DIGESTS).to_pylist() == want
    # an existing member's open root not opened at the scan: no rule places it, so no change is written
    (tmp_path / sd.DIGESTS).unlink()
    con.execute(f"INSERT INTO dr VALUES ('abc', 2, 'c/abc', 'u', {D1}, {O}, 1, 1)")
    assert sd.day_digests(con, tmp_path, D) == "1 delta roots of existing members neither opened at the scan nor closed"
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_incremental_measure_refuses_runs_counting_closes(world, tmp_path):
    """A run whose drill predates `dcount: fresh` (its `dcount` counts closes too) is refused, not summed."""
    root = world["b_store"]
    run = ar.newest_runs(root / "g")[1][0]["key"]
    shutil.copytree(root / "g" / run / "drill", tmp_path / "drill")
    meta = json.loads((tmp_path / "drill" / "meta.json").read_text())
    (tmp_path / "drill" / "meta.json").write_text(json.dumps({k: v for k, v in meta.items() if k != "dcount"}))
    tiers = [sd.Tier(root / "g" / "drill", "base"), sd.Tier(tmp_path / "drill", run)]
    with pytest.raises(ValueError) as e:
        sme.incremental_measure(sn.connect(1, "1GB", None), tiers, root / "g" / "roots-measure", "", tmp_path / "rm", floor=2)
    assert str(e.value) == f"runs whose dcount counts closes too (built before `dcount: fresh`): {run}"


@pytest.mark.parametrize("store, gen", [("a_store", "g.c001"), ("a_store", "g.c002"), ("b_store", "g.c001")])
def test_compaction_record(world, store, gen):
    """`compaction.json` (uploaded last): the runs folded, the rule, and every file's md5; the runless manifest."""
    d = world[store] / gen
    rec = json.loads((d / ar.COMPACTION).read_text())
    ids = world["ids"]
    n = 4 if gen == "g.c001" else 5
    # what the compaction wrote: all but its record, and what later appends added (runs, their manifests, a successor)
    later = lambda k: k.startswith(("deltas/", ar.SUCCESSOR)) or (k.startswith("manifests/") and k != f"manifests/{ids[n - 1]}.json")  # noqa: E731
    files = {k: hashlib.md5(p.read_bytes()).hexdigest() for p in sorted(d.rglob("*"))
             if p.is_file() and p.name != ar.COMPACTION and not later(k := p.relative_to(d).as_posix())}
    assert rec["files"] == files
    assert {k: v for k, v in rec.items() if k != "files"} == {
        "from": {"g.c001": "g", "g.c002": "g.c001"}[gen], "manifest": {("a_store", "g.c001"): f"manifests/{ids[3]}.json",
                                                                      ("a_store", "g.c002"): f"manifests/{ids[4]}.json",
                                                                      ("b_store", "g.c001"): f"manifests/{ids[3]}.m001.json"}[(store, gen)],
        "runs": {("a_store", "g.c001"): [ar.run_key(ids[2], ids[2]), ar.run_key(ids[3], ids[3])], ("a_store", "g.c002"): [ar.run_key(ids[4], ids[4])],
                 ("b_store", "g.c001"): [ar.run_key(ids[2], ids[3])]}[(store, gen)],
        "base_scans": {"g.c001": 2, "g.c002": 4}[gen], "scans": n,
        **({"level": 1} if (store, gen) == ("a_store", "g.c001") else
           {"level": 1, "rule": "level:1", "why": "forced (-K)"} if gen == "g.c002" else {"level": None, "rule": "none", "why": "forced (compact)"}),
    }
    assert json.loads((d / "manifests" / f"{ids[n - 1]}.json").read_text()) == {"gen": gen, "date": ids[n - 1], "base_scans": n, "scans": ids[:n], "runs": []}


def test_store_gc_keeps_the_pointer_and_forwarding_records(world, tmp_path):
    """`store-gc -t static-names`: the pointer's generation is live without `-k`; a collected compacted generation keeps
    its `successor.json`, so `add -g` still follows it."""
    import shutil

    from dt_cloud import store_gen_gc as gc
    from dt_cloud.blobstore import Store

    shutil.copytree(world["a_store"].parent, tmp_path / "a")
    store = Store(str(tmp_path / "a"))
    recs = gc.run(store, "static-names", ["g"], grace_s=0, now=4e9, execute=True)
    assert [(r["gen"], r["action"], r["reason"]) for r in recs] == [("g", "keep", "kept (-k)"), ("g.c001", "delete", None), ("g.c002", "keep", "current")]
    root = tmp_path / "a" / sn.PREFIX
    assert sorted(p.relative_to(root).as_posix() for p in (root / "g.c001").rglob("*") if p.is_file()) == [f"g.c001/{ar.SUCCESSOR}"]
    assert ar.chain(store, sn.PREFIX, "g") == ["g", "g.c001", "g.c002"]


def test_independent_static_pointers_and_gc_follow_both_lineages(world, tmp_path):
    import shutil

    from dt_cloud import store_gen_gc as gc
    from dt_cloud.blobstore import Store

    shutil.copytree(world["a_store"].parent, tmp_path / "a")
    store = Store(str(tmp_path / "a"))
    store.write_text("static-names/meta/scans.json", "{}\n")
    store.write_text("static-names/meta/successor.json", '{"gen":"meta.c001"}\n')
    store.write_text("static-names/meta.c001/scans.json", "{}\n")
    sl.publish_current(store, "meta", "static-names/meta-CURRENT.json")
    assert [store.read_json(k) for k in (
        "static-names/CURRENT.json", "static-names/primary-CURRENT.json", "static-names/meta-CURRENT.json",
    )] == [{"gen": "g.c002"}, {"gen": "g.c002"}, {"gen": "meta.c001"}]
    plan = gc.plan(store, "static-names", ["g", "meta"], lineage_only=False, grace_s=0, now=4e9)
    assert [(r["gen"], r["action"]) for r in plan] == [
        ("g", "keep"), ("g.c001", "delete"), ("g.c002", "keep"), ("meta", "keep"), ("meta.c001", "keep"),
    ]


def test_two_runs_folded_concurrently(world):
    """World I folds two level-0 runs (both spilled at once at `-j 4`)."""
    ids = world["ids"]
    two = [ar.run_key(ids[2], ids[2]), ar.run_key(ids[3], ids[3])]
    assert (world["i_runs"], world["i"]["runs"]) == (two, two)


def _roots_files(gen_dir: Path, runs: list[str]) -> list[Path]:
    """The drill's roots files of a generation's base and `runs`: what a fold stages."""
    return [f for t in ["drill", *(f"{r}/drill" for r in runs)] for k in ("long", "short") for f in sorted((gen_dir / t / k / "roots").glob("*.parquet"))]


def test_staged_fold_copied_every_roots_file(world):
    """World J's fold staged every roots file of the base and its runs (`-s on`), byte for byte their sizes."""
    files = _roots_files(world["j_store"] / "g", world["j_runs"])
    staged = world["j"]["staged"]
    assert {**staged, "s": None} == {"files": len(files), "bytes": sum(f.stat().st_size for f in files), "s": None, "kinds": ["long", "short"],
                                     "failed": []}
    assert len(files) == 4  # the base and its one run, long and short


def test_stage_roots_modes(world, tmp_path, monkeypatch):
    """`stage_roots`: `off` and a generation on the work disk read in place; off the work disk, `auto` stages when the
    disk has room for the roots × (1 + `STAGE_HEADROOM`) and reads in place when it hasn't; `on` stages regardless. A
    staged copy is the file, byte for byte; dropping a kind deletes its copies, the last kind the stage dir."""
    gen_dir = world["c_store"] / "g"
    _, runs = ar.newest_runs(gen_dir)
    tiers = ["drill", *(f"{r['key']}/drill" for r in runs)]
    files = _roots_files(gen_dir, [r["key"] for r in runs])
    need = sum(f.stat().st_size for f in files)
    logs: list[str] = []

    def stage(mode: str, free: int | None = None, same: bool = False) -> sk.Staged | None:
        monkeypatch.setattr(sk, "_same_disk", lambda a, b: same)
        monkeypatch.setattr(sk, "_free", lambda p: free if free is not None else need * 100)
        return sk.stage_roots(gen_dir, tiers, ("long", "short"), tmp_path / "w" / "stage", mode=mode, log=logs.append)

    gib = lambda n: f"{n / 2**30:,.1f} GiB"  # noqa: E731
    assert [stage("off"), stage("auto", same=True), stage("auto", free=5 * need - 1)] == [None, None, None]
    assert logs == [f"stage: off; roots read in place from {gen_dir}",
                    f"stage: skipped, {gen_dir} is on the work disk already",
                    f"stage: skipped, the work disk has {gib(5 * need - 1)} free, under {gib(need)} of long+short roots × 5; roots read in place from {gen_dir}"]
    assert not (tmp_path / "w" / "stage").exists()
    for mode, free in (("auto", 5 * need), ("on", 0)):
        st = stage(mode, free=free)
        assert st is not None
        for k in ("long", "short"):
            mine = [f for f in files if f.parent.parent.name == k]
            copies = [tmp_path / "w" / "stage" / f.relative_to(gen_dir) for f in mine]
            assert st.files(k, [str(f) for f in mine]) == [str(c) for c in copies]
            assert [c.read_bytes() == f.read_bytes() for c, f in zip(copies, mine)] == [True] * len(mine)
        assert (st.doc["files"], st.doc["bytes"], st.doc["kinds"]) == (len(files), need, ["long", "short"])
        st.drop("long")
        assert sorted(p.relative_to(tmp_path / "w" / "stage") for p in (tmp_path / "w" / "stage").rglob("*.parquet")) == \
            sorted(f.relative_to(gen_dir) for f in files if f.parent.parent.name == "short")
        st.drop("short")
        assert not (tmp_path / "w" / "stage").exists()


@pytest.mark.parametrize("stage", ["off", "on", "auto", "on-fail-one", "on-fail-all"])
def test_compact_probe_folds_read_only_to_the_published_bytes(world, tmp_path, monkeypatch, stage):
    """`static_merge compact-probe` over C's store as the mount (4 chunks at a time): the published `g.c001`'s files, md5
    for md5, in its report (stdout without the files, the report beside OUT and at `-u`); the mount is untouched. The
    drill's roots read in place (`-s off`), staged on the work disk (`-s on`, from the mount: no `-b`), or (`-s auto`,
    the mount taken for another disk) not staged for lack of room. Staging is a cache: a file whose copy fails
    (`on-fail-one`: the base's long roots) is read in place, as is everything when the copy itself breaks
    (`on-fail-all`), and the bytes are the same."""
    from dt_cloud import blobstore as bs
    from dt_cloud import static_merge as sm

    mount = world["c_store"].parent
    gen_dir = mount / sn.PREFIX / "g"
    files = _roots_files(gen_dir, [r["key"] for r in ar.newest_runs(gen_dir)[1]])
    need = sum(f.stat().st_size for f in files)
    broken = files[0].relative_to(gen_dir).as_posix()
    before = _md5s(mount)
    if stage == "auto":
        monkeypatch.setattr(sk, "_same_disk", lambda a, b: False)
        monkeypatch.setattr(sk, "_free", lambda p: 1)
    if stage == "on-fail-one":
        get = bs.Store.get

        def flaky(self, key: str, dst: Path) -> None:
            if key == broken:
                raise OSError("injected")
            get(self, key, dst)
        monkeypatch.setattr(bs.Store, "get", flaky)
    if stage == "on-fail-all":
        def broke(*a, **kw):
            raise RuntimeError("injected")
        monkeypatch.setattr(sk, "stage_files", broke)
    monkeypatch.delenv("DATA_BUCKET", raising=False)
    with _small_groups():
        res = CliRunner().invoke(sm.cli, ["compact-probe", "-m", str(mount), "-g", "g", "-o", str(tmp_path / "out"), "-T", str(tmp_path / "w"),
                                          "-c", "3", "-j", "4", "-M", "1GB", "-p", "2", "-s", stage.partition("-")[0],
                                          "-u", str(tmp_path / "reports" / "p.json")], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    summary = json.loads((tmp_path / "out.probe.json").read_text())["summary"]
    # the copy's progress lines (`<label> k/n files, …`) apart from the decisions (`stage: …`)
    lines = [ln for ln in summary["stage_log"] if ln.startswith("stage:")]
    fin = [re.sub(r" in \d+s \([\d,]+ MB/s\)", " in <s> (<rate>)", ln) for ln in summary["stage_log"] if ln.endswith("(done)")]
    stage_dir = tmp_path / "w" / "stage"
    if stage == "on":
        assert [ln.partition(" in ")[0] for ln in lines] == [
            f"stage: {len(files)} files, {need / 2**30:,.1f} GiB of long+short roots from {gen_dir} → {stage_dir}"]
        assert fin == [f"stage long+short roots {len(files)}/{len(files)} files, {need / 2**30:,.2f} of {need / 2**30:,.2f} GiB in <s> (<rate>) (done)"]
    elif stage == "on-fail-one":
        rest = need - (gen_dir / broken).stat().st_size
        assert [ln.partition(" in ")[0] for ln in lines] == [
            f"stage: {len(files) - 1} files, {rest / 2**30:,.1f} GiB of long+short roots from {gen_dir} → {stage_dir}"]
        assert [ln for ln in summary["stage_log"] if " not staged " in ln] == [
            f"stage long+short roots: {broken} not staged (OSError: injected); read in place"]
        assert summary["staged"]["failed"] == [broken]
    else:
        assert lines == [{"off": f"stage: off; roots read in place from {gen_dir}",
                          "auto": f"stage: skipped, the work disk has 0.0 GiB free, under {need / 2**30:,.1f} GiB of long+short roots × 5; "
                                  f"roots read in place from {gen_dir}",
                          "on-fail-all": f"stage: failed (RuntimeError: injected); roots read in place from {gen_dir}"}[stage]]
    assert not stage_dir.exists()
    head = json.loads(res.stdout)
    doc = json.loads((tmp_path / "out.probe.json").read_text())
    assert json.loads((tmp_path / "reports" / "p.json").read_text()) == doc
    assert (head["new_gen"], head["jobs"], head["n_files"], head["runs"]) == ("g.c001", 4, len(doc["files"]), doc["runs"])
    got = {k: v["md5"] for k, v in doc["files"].items() if not k.startswith(OWN)}
    assert got == _md5s(world["c_store"] / "g.c001")
    assert (doc["summary"]["staged"] or {}).get("files") == {"on": len(files), "on-fail-one": len(files) - 1}.get(stage)
    assert {k: v["size"] for k, v in doc["files"].items()} == {p.relative_to(tmp_path / "out").as_posix(): p.stat().st_size
                                                              for p in (tmp_path / "out").rglob("*") if p.is_file()}
    assert _md5s(mount) == before


class _Stop(Exception):
    pass


#: The fold's checkpoint steps, in order (`fold_gen`'s parts, the drill's own within it).
STEPS = ["intervals", "shards", "catalog", "fold/long-roots-tables", "fold/long-roots", "fold/spilled-tables", "fold/spilled", "drill", "anchors"]


@pytest.mark.parametrize("jobs", [1, 4])
@pytest.mark.parametrize("stop", ["intervals", "shards", "catalog", "fold/long-roots", "fold/spilled", "drill"])
def test_a_fold_stopped_after_a_saved_step_resumes_there_to_the_same_bytes(world, tmp_path, stop, jobs):
    """C's fold (chunks of 3 roots), stopped right after a step is saved: the rerun restores what was saved, saves only
    the steps after it, and writes the uninterrupted fold's files. A part's save runs beside the next part, so a failed
    save surfaces once that part ends, and no later step is saved. The stopped fold runs `jobs` (1 or 4) at a time, the
    rerun the other count: a checkpoint doesn't depend on it."""
    gen_dir = world["c_store"] / "g"
    _, runs = ar.newest_runs(gen_dir)
    ck_root = str(tmp_path / "scratch")
    saved: list[list[str]] = [[], []]

    def fold(i: int, out: Path) -> dict:
        def on_save(name: str) -> None:
            saved[i].append(name)
            if i == 0 and name == stop:
                raise _Stop(name)
        ck = sk.compact_checkpoint(ck_root, "g", "g.c001", runs, on_save=on_save, work=tmp_path / f"ck{i}")
        try:
            with _small_groups():
                return sk.fold_gen(gen_dir, out, tmp_path / f"w{i}", gen="g", new_gen="g.c001", runs=runs, threads=2, mem="1GB", chunk_rows=3,
                                   jobs=jobs if i == 0 else 5 - jobs, ck=ck, stage="on" if i == 1 else "auto")
        finally:
            ck.close()

    with pytest.raises(_Stop):
        fold(0, tmp_path / "out0")
    fold(1, tmp_path / "out1")
    k = STEPS.index(stop) + 1
    assert saved == [STEPS[:k], STEPS[k:]]
    got, want = _md5s(tmp_path / "out1"), _md5s(world["c_store"] / "g.c001")
    assert sorted(got) == sorted(want)
    assert got == want
    assert _meta(tmp_path / "out1") == _meta(world["c_store"] / "g.c001")
    # once every root is spilled, the long roots' steps are dropped (the spills hold those roots again)
    scratch = tmp_path / "scratch"
    rels = [p.relative_to(scratch).parts for p in scratch.rglob("*") if p.is_file()]
    assert sorted({r[5] for r in rels if r[4] == "fold"}) == ["spilled", "spilled-tables"]
    # cleared once published: nothing of the new generation's folds is left
    assert sk.clear_checkpoints(ck_root, "g.c001") > 0
    assert sorted(p.relative_to(tmp_path / "scratch").as_posix() for p in (tmp_path / "scratch").rglob("*") if p.is_file()) == []


def test_progress_counts_items_done_across_pool_calls():
    """The long roots run one `_pool` per batch of spill parts on one `Progress`: its lines count the items done over
    all of them (gcs's live compaction printed each call's own count: 45, 61, 1, 34 … of 4,881)."""
    import duckdb

    lines: list[str] = []
    prog = sn.Progress("drill/fold/long-roots", 5, lines.append, every_s=0)
    con = duckdb.connect()
    for batch in ([0, 1, 2], [3, 4]):
        sk._pool(con, lambda cur, i: (10, i), batch, 2, prog)
    assert [line.split(":")[0] for line in lines] == [f"drill/fold/long-roots {k}/5" for k in range(1, 6)]
    assert prog.rows == 50


def test_measure_probe_measures_a_saved_spill_read_only_at_any_pool_width(world, tmp_path, monkeypatch):
    """C's fold stopped once its spills are saved; `static_merge measure-probe` over that checkpoint, in turn (2 threads,
    `MEASURE_THREADS` 4) and in 2 processes (`$DT_MEASURE_THREADS` 1): the same measured tables and sorted roots, md5 for
    md5, and the checkpoint untouched."""
    from dt_cloud import static_merge as sm

    gen_dir = world["c_store"] / "g"
    _, runs = ar.newest_runs(gen_dir)
    ck_root = str(tmp_path / "scratch")

    def on_save(name: str) -> None:
        if name == "fold/spilled":
            raise _Stop(name)

    ck = sk.compact_checkpoint(ck_root, "g", "g.c001", runs, on_save=on_save, work=tmp_path / "ck")
    with pytest.raises(_Stop), _small_groups():
        sk.fold_gen(gen_dir, tmp_path / "fold-out", tmp_path / "fw", gen="g", new_gen="g.c001", runs=runs, threads=2, mem="1GB", chunk_rows=3, ck=ck)
    ck.close()
    [done] = [p for p in (tmp_path / "scratch").rglob("done.json") if p.parent.name == "spilled"]
    fold_ck = str(done.parent.parent)
    before = _md5s(tmp_path / "scratch")
    docs = []
    for i, width in enumerate([None, "1"]):
        if width:
            monkeypatch.setenv(sl.MEASURE_THREADS_ENV, width)
        res = CliRunner().invoke(sm.cli, ["measure-probe", "-c", fold_ck, "-m", str(world["c_store"].parent), "-g", "g", "-o", str(tmp_path / f"m{i}"),
                                          "-T", str(tmp_path / f"t{i}"), "-M", "1GB", "-p", "2"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        docs.append(json.loads((tmp_path / f"m{i}.probe.json").read_text()))
    assert [(d["kinds"]["long"]["jobs"], d["measure_threads"]) for d in docs] == [(1, None), (2, "1")]
    assert docs[0]["files"] == docs[1]["files"]
    assert sorted(docs[0]["files"])[:2] == ["long-dirs.parquet", "long-rup.parquet"]
    # a subset (the heaviest range and a sample of 2 others per kind): only theirs fetched and measured, each to the
    # whole's bytes
    monkeypatch.delenv(sl.MEASURE_THREADS_ENV)
    res = CliRunner().invoke(sm.cli, ["measure-probe", "-c", fold_ck, "-m", str(world["c_store"].parent), "-g", "g", "-o", str(tmp_path / "m2"),
                                      "-T", str(tmp_path / "t2"), "-M", "1GB", "-p", "2", "-H", "1", "-s", "2:3"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    sub = json.loads((tmp_path / "m2.probe.json").read_text())
    chosen = {k: sub["kinds"][k]["chosen"] for k in ("long", "short")}
    assert {k: (len(v), sorted(map(int, sub["kinds"][k]["ranges"]))) for k, v in chosen.items()} == {k: (min(3, docs[0]["kinds"][k]["groups"]), v) for k, v in chosen.items()}
    sorted_files = {k: v for k, v in sub["files"].items() if "-sorted/" in k}
    assert sorted(sorted_files) == sorted(f"{k}-sorted/g{g:06d}.parquet" for k, gs in chosen.items() for g in gs)
    assert sorted_files == {k: docs[0]["files"][k] for k in sorted_files}
    # every range dealt to 2 tasks (each fetching only its own), their reports merged: the whole run's sorted roots
    for i in range(2):
        res = CliRunner().invoke(sm.cli, ["measure-probe", "-c", fold_ck, "-m", str(world["c_store"].parent), "-g", "g", "-o", str(tmp_path / f"s{i}"),
                                          "-T", str(tmp_path / f"ts{i}"), "-M", "1GB", "-p", "2", "-S", f"{i}/2", "-u", str(tmp_path / "shards")],
                                 catch_exceptions=False)
        assert res.exit_code == 0, res.output
    assert sorted(p.name for p in (tmp_path / "shards").iterdir()) == ["0-of-2.json", "1-of-2.json"]
    res = CliRunner().invoke(sm.cli, ["measure-probe-merge", "-o", str(tmp_path / "merged.json"), str(tmp_path / "shards")], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    merged = json.loads((tmp_path / "merged.json").read_text())
    whole = {k: v for k, v in docs[0]["files"].items() if "-sorted/" in k}
    assert {k: v for k, v in merged["files"].items() if "-sorted/" in k} == whole
    assert {k: (merged["kinds"][k]["groups"], len(merged["kinds"][k]["ranges"])) for k in ("long", "short")} == {
        k: (docs[0]["kinds"][k]["groups"], docs[0]["kinds"][k]["groups"]) for k in ("long", "short")}
    assert _md5s(tmp_path / "scratch") == before


def test_deal_balances_ranges_heaviest_first_to_the_least_loaded_task():
    from dt_cloud.static_merge import deal

    assert deal({"a": 10, "b": 9, "c": 5, "d": 4, "e": 1}, 2) == [["a", "d", "e"], ["b", "c"]]
    assert deal({"a": 1}, 3) == [["a"], [], []]


def _fanned_fold(world, tmp_path, submit, retries: int = 2, root: str = "scratch", out: str = "out", log=lambda m: None):
    """C's fold with its drill measurement over 3 tasks (`static_fanout.Fanout`, logging to `log`), `submit` running the
    helper jobs; its checkpoint under `tmp_path / root`, the generation at `tmp_path / out`."""
    from dt_cloud.checkpoint import code_version
    from dt_cloud.static_fanout import Fanout

    gen_dir = world["c_store"] / "g"
    _, runs = ar.newest_runs(gen_dir)
    ck = sk.compact_checkpoint(str(tmp_path / root), "g", "g.c001", runs, work=tmp_path / f"ck-{root}")
    try:
        with _small_groups():
            sk.fold_gen(gen_dir, tmp_path / out, tmp_path / f"w-{root}", gen="g", new_gen="g.c001", runs=runs, threads=2, mem="1GB", chunk_rows=3, ck=ck,
                        fanout=lambda c, scan: Fanout(c, 3, submit, code=code_version(), scan=scan, poll_s=0.01, retries=retries, log=log))
    finally:
        ck.close()


def _helpers(tmp_path, calls: list, skip=lambda attempt, i: False):
    """A fake helper job: each task's `measure-shard` in turn, in-process (`skip(attempt, i)`: a task that never runs)."""
    from dt_cloud import static_merge as sm

    def submit(url: str, plan: str, n: int, code: str, scan: str) -> None:
        calls.append((plan, n))
        for i in range(n):
            if skip(len(calls), i):
                continue
            # the command's body, not `CliRunner`: it runs on the fold's helper thread, and the runner swaps the process's
            # stdout/stderr under the fold's own logging
            sm.measure_shard_cmd.callback(checkpoint=url, index=i, mem="1GB", threads=2, plan=plan, tmp=str(tmp_path / f"h{len(calls)}-{i}"),
                                          code=code, workers=4)
    return submit


def test_a_fanned_out_measurement_writes_the_single_vm_fold(world, tmp_path):
    """The fold measures its share, 2 helper tasks theirs (from the saved spills, uploaded per range): `g.c001`'s
    files, md5 for md5."""
    calls: list = []
    _fanned_fold(world, tmp_path, _helpers(tmp_path, calls))
    assert calls == [("measured/plan-1.json", 2)]
    assert _md5s(tmp_path / "out") == _md5s(world["c_store"] / "g.c001")


def test_a_race_seeded_from_a_fanned_out_fold_measures_nothing_again(world, tmp_path):
    """The fold saves its own share's ranges as the helpers save theirs, so a race seeded from its checkpoint through
    `fold/spilled` fetches every range: no helper job, no range measured, the same bytes."""
    calls: list = []
    _fanned_fold(world, tmp_path, _helpers(tmp_path, calls))
    _, runs = ar.newest_runs(world["c_store"] / "g")
    seeded = sk.seed_checkpoint(str(tmp_path / "scratch"), str(tmp_path / "race"), "g", "g.c001", runs, "fold/spilled", log=lambda m: None)
    again, lines = [], []
    _fanned_fold(world, tmp_path, _helpers(tmp_path, again), root="race", out="out-race", log=lines.append)
    plans = [m.groups() for m in map(re.compile(r"measure fan-out: (\d+) ranges here \([\d,]+ B; \d+ spill files, [\d,]+ B to fetch\), (\d+) on 2 helper tasks "
                                                r"\((\d+) to measure\)").fullmatch, lines) if m]
    assert (again, seeded["steps"][-1], len(plans), plans[0][0], plans[0][2]) == ([], "fold/spilled", 1, "0", "0")
    assert _md5s(tmp_path / "out-race") == _md5s(world["c_store"] / "g.c001")


def test_ranges_a_helper_job_left_are_dealt_again(world, tmp_path):
    """The first helper job's second task never runs: its ranges go to a second job (over the 2 helper slots), and the
    fold's bytes are the same."""
    calls: list = []
    _fanned_fold(world, tmp_path, _helpers(tmp_path, calls, skip=lambda attempt, i: attempt == 1 and i == 1))
    assert [p for p, _ in calls] == ["measured/plan-1.json", "measured/plan-2.json"]
    assert _md5s(tmp_path / "out") == _md5s(world["c_store"] / "g.c001")


def test_ranges_never_measured_fail_the_fold_by_name(world, tmp_path):
    """Helper jobs that measure nothing: after the first and its one retry, the fold fails naming the missing ranges."""
    calls: list = []
    with pytest.raises(RuntimeError, match=r"^measure fan-out: (long|short) ranges \[[0-9, ]+\] unmeasured after 2 helper jobs$"):
        _fanned_fold(world, tmp_path, _helpers(tmp_path, calls, skip=lambda attempt, i: True), retries=1)
    assert [p for p, _ in calls] == ["measured/plan-1.json", "measured/plan-2.json"]


def test_a_fanned_out_measurement_resumed_after_the_spills_fetches_only_its_share(world, tmp_path, monkeypatch):
    """C's fold stopped once its spills are saved, an orphan spill file beside them (an aborted attempt's: under
    `spilled/files/`, not in the marker's list); resumed with its measurement over 3 tasks: the `spilled` step's tables
    restored, not its files (`_restore` never called for it), this VM fetching only its share's spill files, the
    helpers theirs, the orphan read by none: the single-VM fold's bytes."""
    from dt_cloud import static_merge as sm
    from dt_cloud.checkpoint import Checkpoint

    _, runs, root = _stopped_fold(world, tmp_path)
    prefix = sk.checkpoint_prefix("g", "g.c001", runs)
    probe = Checkpoint(root, f"{prefix}/fold", work=tmp_path / "ck-probe")
    sizes, by = sm._spilled(probe)
    probe.close()
    files = tmp_path / "scratch" / prefix / "fold" / "spilled" / "files"
    first = sorted(sizes)[0]
    kind, _, rest = first.partition("/")
    orphan = files / kind / "orphan" / rest.split("/")[1] / "x.parquet"
    orphan.parent.mkdir(parents=True)
    shutil.copyfile(files / first, orphan)
    share0 = sm.deal({u: sum(sizes[r] for r in rels) for u, rels in by.items()}, 3)[0]
    restored: list[str] = []
    real = sk._restore
    monkeypatch.setattr(sk, "_restore", lambda con, ck, step, work: restored.append(step) or real(con, ck, step, work))
    lines: list[str] = []
    calls: list = []
    _fanned_fold(world, tmp_path, _helpers(tmp_path, calls), log=lines.append)
    fetch = re.compile(r"measure fan-out: \d+ ranges here \([\d,]+ B; (\d+) spill files, [\d,]+ B to fetch\), .*")
    fetched = [int(m[1]) for m in map(fetch.fullmatch, lines) if m]
    assert (restored, calls, fetched) == ([], [("measured/plan-1.json", 2)], [sum(len(by[u]) for u in share0)])
    assert _md5s(tmp_path / "out") == _md5s(world["c_store"] / "g.c001")


def _anchors_fanned_fold(world, tmp_path, submit, retries: int = 2):
    """C's fold with its base anchors' rollups over 3 tasks (`static_fanout.AnchorFanout`), `submit` running the helper
    jobs."""
    from dt_cloud.checkpoint import code_version
    from dt_cloud.static_fanout import AnchorFanout

    gen_dir = world["c_store"] / "g"
    _, runs = ar.newest_runs(gen_dir)
    ck = sk.compact_checkpoint(str(tmp_path / "scratch"), "g", "g.c001", runs, work=tmp_path / "ck")
    try:
        with _small_groups():
            sk.fold_gen(gen_dir, tmp_path / "out", tmp_path / "w", gen="g", new_gen="g.c001", runs=runs, threads=2, mem="1GB", chunk_rows=3, ck=ck,
                        anchor_fanout=lambda c, scan: AnchorFanout(c, 3, submit, code=code_version(), scan=scan, poll_s=0.01, retries=retries,
                                                                   log=lambda m: None))
    finally:
        ck.close()


def _anchor_helpers(tmp_path, calls: list, skip=lambda attempt, i: False):
    """A fake anchors helper job: each task's `anchor-shard` in turn, in-process (`skip(attempt, i)`: a task that never
    runs). `calls`: each job's plan, task count and the scan it's named by."""
    from dt_cloud import static_merge as sm

    def submit(url: str, plan: str, n: int, code: str, scan: str) -> None:
        calls.append((plan, n, scan))
        for i in range(n):
            if skip(len(calls), i):
                continue
            sm.anchor_shard_cmd.callback(checkpoint=url, index=i, mem="1GB", threads=2, plan=plan, tmp=str(tmp_path / f"a{len(calls)}-{i}"),
                                         code=code, workers=4)
    return submit


def test_fanned_out_anchors_write_the_single_vm_fold(world, tmp_path):
    """The fold builds its share of the anchors' shard tasks, 2 helper tasks theirs (from the shards uploaded under
    `anchors-in/`, their cells under `anchored/`): `g.c001`'s files (built at `anchor_tasks` 1), md5 for md5. The helper
    job is named by the newest scan folded, never by the plan."""
    calls: list = []
    _anchors_fanned_fold(world, tmp_path, _anchor_helpers(tmp_path, calls))
    assert calls == [("anchored/plan-1.json", 2, world["ids"][3])]
    assert _md5s(tmp_path / "out") == _md5s(world["c_store"] / "g.c001")


def test_anchor_tasks_a_helper_job_left_are_dealt_again(world, tmp_path):
    """The first anchors helper job's second task never runs: its shard tasks go to a second job, and the fold's bytes
    are the same."""
    calls: list = []
    _anchors_fanned_fold(world, tmp_path, _anchor_helpers(tmp_path, calls, skip=lambda attempt, i: attempt == 1 and i == 1))
    assert [p for p, _, _ in calls] == ["anchored/plan-1.json", "anchored/plan-2.json"]
    assert _md5s(tmp_path / "out") == _md5s(world["c_store"] / "g.c001")


def test_anchor_tasks_never_built_fail_the_fold_by_name(world, tmp_path):
    """Anchors helper jobs that build nothing: after the first and its one retry, the fold fails naming the missing
    shard tasks."""
    calls: list = []
    with pytest.raises(RuntimeError, match=r"^anchors fan-out: shard tasks \[('(end|exact|start)/s\d{5}'(, )?)+\] not built after 2 helper jobs$"):
        _anchors_fanned_fold(world, tmp_path, _anchor_helpers(tmp_path, calls, skip=lambda attempt, i: True), retries=1)
    assert [p for p, _, _ in calls] == ["anchored/plan-1.json", "anchored/plan-2.json"]


def _stopped_fold(world, tmp_path, stop: str = "fold/spilled") -> tuple[Path, list[dict], str]:
    """C's fold checkpointed under `tmp_path/scratch`, stopped right after saving `stop`; its source dir, runs, root."""
    gen_dir = world["c_store"] / "g"
    _, runs = ar.newest_runs(gen_dir)
    root = str(tmp_path / "scratch")

    def on_save(name: str) -> None:
        if name == stop:
            raise _Stop(name)

    ck = sk.compact_checkpoint(root, "g", "g.c001", runs, on_save=on_save, work=tmp_path / "ck0")
    with pytest.raises(_Stop), _small_groups():
        sk.fold_gen(gen_dir, tmp_path / "out0", tmp_path / "w0", gen="g", new_gen="g.c001", runs=runs, threads=2, mem="1GB", chunk_rows=3, ck=ck)
    ck.close()
    return gen_dir, runs, root


def test_a_race_resumes_another_codes_saved_steps_into_its_own_checkpoint(world, tmp_path, monkeypatch):
    """C's fold stopped after `fold/spilled` by "other code" (its `code.json` says so: a resume there would clear it);
    `compact-probe -R <its root> -t fold/spilled -K <race root>`: the steps through `fold/spilled` seeded into the race's
    root and claimed for this code (the intervals not folded again), the rest folded: `g.c001`'s files, md5 for md5; the
    source checkpoint untouched."""
    from dt_cloud import static_merge as sm

    gen_dir, runs, root = _stopped_fold(world, tmp_path)
    prefix = sk.checkpoint_prefix("g", "g.c001", runs)
    (tmp_path / "scratch" / prefix / "code.json").write_text(json.dumps({"code": "older"}) + "\n")
    before = _md5s(tmp_path / "scratch")
    monkeypatch.setattr(sk, "fold_ranges", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the intervals were folded again")))
    with _small_groups():
        res = CliRunner().invoke(sm.cli, ["compact-probe", "-m", str(world["c_store"].parent), "-g", "g", "-o", str(tmp_path / "out"), "-T", str(tmp_path / "w"),
                                          "-c", "3", "-M", "1GB", "-p", "2", "-R", root, "-t", "fold/spilled", "-K", str(tmp_path / "race")],
                                 catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert {k: v for k, v in _md5s(tmp_path / "out").items() if not k.startswith(OWN)} == _md5s(world["c_store"] / "g.c001")
    assert _md5s(tmp_path / "scratch") == before
    from dt_cloud.checkpoint import code_version

    assert json.loads((tmp_path / "race" / prefix / "code.json").read_text()) == {"code": code_version()}


def test_a_prebuilt_fold_is_published_as_the_compaction(world, tmp_path):
    """`compact-probe -U URL` of C's base and runs, then a compaction of a copy of C's store with its own successor
    removed, adopting it (`compactor(prebuilt=URL)`): `g.c001` published, md5 for md5 C's; a report for other runs is
    refused."""
    from dt_cloud import static_merge as sm
    from dt_cloud.blobstore import Store

    with _small_groups():
        res = CliRunner().invoke(sm.cli, ["compact-probe", "-m", str(world["c_store"].parent), "-g", "g", "-o", str(tmp_path / "out"), "-T", str(tmp_path / "w"),
                                          "-c", "3", "-M", "1GB", "-p", "2", "-U", str(tmp_path / "race")], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    store = tmp_path / "store"
    shutil.copytree(world["c_store"].parent, store)
    shutil.rmtree(store / sn.PREFIX / "g.c001")
    for f in (store / sn.PREFIX / "g" / ar.SUCCESSOR, store / sn.PREFIX / ar.CURRENT):
        f.unlink(missing_ok=True)
    doc = ar.run_compaction(Store(str(store)), sk.compactor(prebuilt=str(tmp_path / "race")), "g", store, tmp_path / "wp", policy=1, force=True)
    assert (doc["compact"]["gen"], doc["compact"]["prebuilt"]) == ("g.c001", str(tmp_path / "race"))
    assert _md5s(store / sn.PREFIX / "g.c001") == _md5s(world["c_store"] / "g.c001")
    report = json.loads((tmp_path / "race" / "probe.json").read_text())
    (tmp_path / "race" / "probe.json").write_text(json.dumps({**report, "runs": report["runs"][:-1]}))
    shutil.rmtree(store / sn.PREFIX / "g.c001")
    (store / sn.PREFIX / "g" / ar.SUCCESSOR).unlink()
    with pytest.raises(SystemExit, match=r"folds g → g\.c001 over"):
        ar.run_compaction(Store(str(store)), sk.compactor(prebuilt=str(tmp_path / "race")), "g", store, tmp_path / "wp2", policy=1, force=True)


def test_seed_keys_join_a_bucket_root_and_a_prefix():
    """`same_bucket`: a bucket's own root is an empty prefix (gcs's race from `gs://<scratch>`), a path one ends in `/`;
    other buckets, schemes or a local dir: None (copied through the client)."""
    assert sk.same_bucket("gs://scratch", "gs://scratch/static-names-race/c001-a") == ("gs://scratch", "", "static-names-race/c001-a/")
    assert sk.same_bucket("gs://scratch/x/", "gs://scratch") == ("gs://scratch", "x/", "")
    assert [sk.same_bucket(a, b) for a, b in (("gs://a", "gs://b/x"), ("gs://a", "r2://a"), ("/tmp/a", "/tmp/b"))] == [None, None, None]


def test_a_seed_through_the_spills_carries_the_finished_measured_ranges(tmp_path):
    """`seed_checkpoint` through `fold/spilled` also copies `fold/measured/`'s finished ranges (a unit dir with a
    `done.json`), not unfinished ones, plans or task reports, so the race resumed there deals only the rest; through
    `drill`, the anchors' finished shard tasks and their uploaded inputs instead."""
    from dt_cloud.blobstore import Store

    runs = [{"key": "runs/2026-10-23T0702"}]
    pre = sk.checkpoint_prefix("g", "g.c001", runs)
    src = Store(str(tmp_path / "src"))
    for rel in ("fold/spilled/done.json", "fold/spilled/long/x.parquet", "fold/measured/plan-1.json", "fold/measured/tasks/plan-1-0.json",
                "fold/measured/long/g000001/sorted.parquet", "fold/measured/long/g000001/done.json",
                "fold/measured/long/g000002/sorted.parquet", "drill/done.json", "drill/d.parquet",
                "anchored/end/s00000/cells.parquet", "anchored/end/s00000/done.json", "anchored/exact/s00001/cells.parquet",
                "anchors-in/sx/s00000.parquet"):
        src.write_text(f"{pre}/{rel}", rel)

    def seeded(through: str, dst: str) -> list[str]:
        sk.seed_checkpoint(str(tmp_path / "src"), str(tmp_path / dst), "g", "g.c001", runs, through, log=lambda m: None)
        return sorted(k[len(pre) + 1:] for k in Store(str(tmp_path / dst)).keys(f"{pre}/"))

    assert seeded("fold/spilled", "a") == ["code.json", "fold/measured/long/g000001/done.json", "fold/measured/long/g000001/sorted.parquet",
                                           "fold/spilled/done.json", "fold/spilled/long/x.parquet"]
    assert seeded("drill", "b") == ["anchored/end/s00000/cells.parquet", "anchored/end/s00000/done.json", "anchors-in/sx/s00000.parquet",
                                    "code.json", "drill/d.parquet", "drill/done.json", "fold/spilled/done.json", "fold/spilled/long/x.parquet"]


def test_a_compactions_record_phases_its_fold_drill_and_anchors(world, tmp_path, monkeypatch):
    """A static compaction's run on /scans: the fold's record phases `fold` (intervals, shards, catalog), `drill` and
    `anchors` (the fold's `done` hook moves it on), each ended done (`fold`'s and `drill`'s notes naming their parts'
    seconds), before `upload`."""
    from dt_cloud.blobstore import Store
    from test_maint_runs import FakeRecorder

    monkeypatch.setattr(ar, "NOTE_S", 1e12)  # no throttled notes: a deterministic call list
    store = tmp_path / "store"
    shutil.copytree(world["c_store"].parent, store)
    shutil.rmtree(store / sn.PREFIX / "g.c001")
    for f in (store / sn.PREFIX / "g" / ar.SUCCESSOR, store / sn.PREFIX / ar.CURRENT):
        f.unlink(missing_ok=True)
    rec = FakeRecorder()
    with _small_groups():
        ar.run_compaction(Store(str(store)), sk.compactor(), "g", store, tmp_path / "w", policy=1, force=True, log=lambda m: None, rec=rec)
    begun = [c[1] for c in rec.calls if c[0] == "begin"]
    fold = begun[begun.index("fold"):begun.index("upload") + 1]
    ended = [c for c in rec.calls if c[0] == "end" and c[1] in ("fold", "drill", "anchors")]
    # the last of them ends with the fold's summary (`compact_store`), the others with their parts' seconds
    parts = {p: [x for x in re.findall(r"(?:^|; )([a-z/-]+) [\d.]+s", rec.notes[p]) if "/" not in x] for p in ("fold", "drill")}
    assert (fold, ended, parts) == (["fold", "drill", "anchors", "upload"], [("end", p, "done") for p in ("fold", "drill", "anchors")],
                                    {"fold": ["intervals", "shards", "catalog"], "drill": ["drill"]})


def test_heavy_chunks_combine_in_parts_to_the_same_generation(world, tmp_path, monkeypatch):
    """A long-roots chunk past `COMBINE_PART_ROWS` (2 here: every chunk of more than two roots) combines in parts by
    `hash(path)`: chunks split, and `g.c001`'s files md5 for md5."""
    monkeypatch.setattr(sk, "COMBINE_PART_ROWS", 2)
    gen_dir = world["c_store"] / "g"
    _, runs = ar.newest_runs(gen_dir)
    lines: list[str] = []
    with _small_groups():
        sk.fold_gen(gen_dir, tmp_path / "out", tmp_path / "w", gen="g", new_gen="g.c001", runs=runs, threads=2, mem="1GB", chunk_rows=3,
                    log=lines.append)
    split = [int(m.group(1)) for m in map(re.compile(r".*: drill/fold/long-roots in .*, split (\d+)\)$").fullmatch, lines) if m]
    assert (len(split), split[0] > 0) == (1, True)
    assert _md5s(tmp_path / "out") == _md5s(world["c_store"] / "g.c001")
