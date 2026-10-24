"""`dt_cloud.static_compact` through the shared orchestrator (`append_runner.compact_store`): a generation's base and its
runs folded into `<gen>.c<NNN>` is `static-names build` through the same scans over the base's key ranges (`build -r`),
every file md5-equal (but `catalog/meta.json`'s seconds), for every fixture flavor: two level-0 runs (compacted by `add`
when level 1 is due), one merged run (`static-names compact`), and again after a scan appended to the compacted
generation (`add -g <old>` follows `successor.json`; `add -K`)."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

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
                                             ("f_store", "g.c001", "g.c001"), ("h_store", "g.c001", "g.c001"), ("i_store", "g.c001", "g.c001")])
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


def test_compact_probe_folds_read_only_to_the_published_bytes(world, tmp_path):
    """`static_merge compact-probe` over C's store as the mount (4 chunks at a time): the published `g.c001`'s files, md5
    for md5, in its report (stdout without the files, the report beside OUT and at `-u`); the mount is untouched."""
    from dt_cloud import static_merge as sm

    mount = world["c_store"].parent
    before = _md5s(mount)
    with _small_groups():
        res = CliRunner().invoke(sm.cli, ["compact-probe", "-m", str(mount), "-g", "g", "-o", str(tmp_path / "out"), "-T", str(tmp_path / "w"),
                                          "-c", "3", "-j", "4", "-M", "1GB", "-p", "2", "-u", str(tmp_path / "reports" / "p.json")], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    head = json.loads(res.stdout)
    doc = json.loads((tmp_path / "out.probe.json").read_text())
    assert json.loads((tmp_path / "reports" / "p.json").read_text()) == doc
    assert (head["new_gen"], head["jobs"], head["n_files"], head["runs"]) == ("g.c001", 4, len(doc["files"]), doc["runs"])
    got = {k: v["md5"] for k, v in doc["files"].items() if not k.startswith(OWN)}
    assert got == _md5s(world["c_store"] / "g.c001")
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
                                   jobs=jobs if i == 0 else 5 - jobs, ck=ck)
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
