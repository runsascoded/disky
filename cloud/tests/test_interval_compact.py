"""`dt_cloud.interval_compact`: `interval-store add` compacts the base and every run into a new generation once the
compaction level is due, and that generation is what a fresh `interval-store build` over the same scans writes — served
sorts and `scans.json` byte for byte, every range file's versions row for row. The pointer (`CURRENT.json`) and the
successor records follow it, so an unpinned `add` (and the site) find it with no config change; a compacted generation
leaves the store after the grace, but for its forwarding record."""
from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner
from test_interval_append import _rows
from test_interval_store import V2_FROM, _scan_rows, _universe, _write

from dt_cloud import append_runner as ar
from dt_cloud import interval_compact as ic
from dt_cloud import interval_local as il
from dt_cloud import interval_store as ist
from dt_cloud import static_local as sl
from dt_cloud import static_names as sn

#: Two scans in the base, then eight appended one `add` at a time at compaction level 2: two runs reach level 1 every four
#: scans, so the 4th and 8th appended scans compact (`g` → `g.c001` → `g.c002`).
DATES = ["2026-07-30", "2026-07-31", "2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-04T1236", "2026-08-05", "2026-08-06",
         "2026-08-07"]
BASE = 2
LEVEL = 2
LAYOUT = "listing/{id}/path-index.parquet"
RG = 4
#: The generation each appended scan's `add` appends to, and the one it compacts into.
EXPECT = [("g", None), ("g", None), ("g", None), ("g", "g.c001"), ("g.c001", None), ("g.c001", None), ("g.c001", None), ("g.c001", "g.c002")]


def make_world(root: Path, owned: bool) -> dict:
    """Ten scans (v1 indexes, then v2 store generations); `owned` False: no owner named anywhere (r2.rbw.sh, cw-s3)."""
    rng = random.Random(23)
    paths = _universe(rng)
    scans = []
    for j, d in enumerate(DATES):
        v = 2 if j >= V2_FROM else 1
        key = LAYOUT.replace("{id}", d)
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        rows = _scan_rows(rng, paths, j)
        _write(rows if owned else [{**r, "usr": None} for r in rows], root / key, v)
        if v == 2 and not owned:
            pq.write_table(pq.read_table(root / key).drop_columns(["usr"]), root / key, row_group_size=7)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
    return {"root": root, "scans": scans, "paths": paths}


def build(world, store: Path, work: Path, gen: str, through: str) -> dict:
    args = ["-g", gen, "-L", LAYOUT, "-s", str(world["root"]), "-o", str(store), "-w", str(work), "-k", "3", "-r", str(RG), "-p", "2", "-M", "1GB",
            "-t", through]
    res = CliRunner().invoke(il.build_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def add(world, store: Path, work: Path, *extra: str) -> dict:
    args = ["-L", LAYOUT, "-s", str(world["root"]), "-o", str(store), "-w", str(work), "-r", str(RG), "-p", "2", "-M", "1GB", "-C", str(LEVEL), *extra]
    res = CliRunner().invoke(il.add_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def files(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def served(gen: Path) -> dict[str, bytes]:
    """What the reader serves of a generation: its sorts and their group indexes, and `scans.json`."""
    return {k: v for k, v in files(gen).items() if (k.startswith("served/") and k.endswith(".parquet")) or k == "scans.json"}


@pytest.fixture(scope="module", params=[True, False], ids=["owned", "unowned"])
def run(request, tmp_path_factory):
    """The base over the first two scans, then each later scan appended by its own unpinned `add`, a snapshot of the
    store kept before the first compaction."""
    tmp = tmp_path_factory.mktemp("ivc")
    world = make_world(tmp / "scans", request.param)
    store = tmp / "store"
    build(world, store, tmp / "work-build", "g", DATES[BASE - 1])
    outs = []
    for j, d in enumerate(DATES[BASE:]):
        if j == 3:
            shutil.copytree(store, tmp / "before-c001")
        outs.append(add(world, store, tmp / "work", "-t", d))
    return {"tmp": tmp, "world": world, "store": store, "outs": outs, "owned": request.param}


def test_add_compacts_every_two_to_the_level_scans(run):
    assert [(o["gen"], o["compact"] and o["compact"]["gen"]) for o in run["outs"]] == EXPECT
    c1, c2 = run["outs"][3]["compact"], run["outs"][7]["compact"]
    assert [(c["from"], c["scans"], c["runs"], c["current"]) for c in (c1, c2)] == [
        ("g", 6, [ar.run_key(DATES[2], DATES[3]), ar.run_key(DATES[4], DATES[5])], True),
        ("g.c001", 10, [ar.run_key(DATES[6], DATES[7]), ar.run_key(DATES[8], DATES[9])], True),
    ]
    # Its cost, for sizing the machine: seconds, peak work-disk and RSS (GiB), as `add` reports its own
    assert [(type(c["s"]), sorted(c["peak"]), all(v >= 0 for v in c["peak"].values())) for c in (c1, c2)] == [(float, ["disk_gb", "rss_gb"], True)] * 2
    root = run["store"] / ist.PREFIX
    assert json.loads((root / "CURRENT.json").read_text()) == {"gen": "g.c002"}
    assert [json.loads((root / g / "successor.json").read_text()) for g in ("g", "g.c001")] == [{"gen": "g.c001"}, {"gen": "g.c002"}]
    assert (root / "g.c002" / "successor.json").exists() is False
    assert json.loads((root / "g.c002" / "compaction.json").read_text()) == {
        "from": "g.c001", "manifest": f"manifests/{DATES[9]}.m001.json", "runs": [ar.run_key(DATES[6], DATES[7]), ar.run_key(DATES[8], DATES[9])], "base_scans": 6, "scans": 10,
        "level": LEVEL}
    # The compacted generation is a base: no runs, no manifests.
    assert sorted(p.name for p in (root / "g.c002").iterdir()) == sorted(
        ["compaction.json", "pv", "pvl", "ranges.json", "rd", "scans.json", "served", *(["sv", "svt"] if run["owned"] else [])])


@pytest.mark.parametrize("gen,through", [("g.c001", DATES[5]), ("g.c002", DATES[9])])
def test_a_compacted_generation_is_a_fresh_build(run, tmp_path, gen, through):
    """Served sorts, their group indexes and `scans.json`: a fresh `build` over the same scans' bytes. Every range file
    (`pv`, `rd`, `pvl`, and the slices'): its versions row for row (the fresh build plans its own key ranges)."""
    fresh = tmp_path / "store"
    build(run["world"], fresh, tmp_path / "work", "f", through)
    got, want = run["store"] / ist.PREFIX / gen, fresh / ist.PREFIX / "f"
    assert sorted(served(got)) == sorted(served(want))
    assert served(got) == served(want)
    subs = ["pv", "rd", "pvl", *(["sv", "svt"] if run["owned"] else [])]
    for t in subs:
        key = ["depth", "path", *(["usr"] if t in ("sv", "svt") else []), "vf"]
        order = lambda rows: sorted(rows, key=lambda r: tuple(r[c] for c in key))  # noqa: E731
        assert order(_rows(got, t)) == order(_rows(want, t)), t
    assert sorted(p.name for p in (got / "served").iterdir()) == sorted(p.name for p in (want / "served").iterdir())


def test_the_newest_generation_serves_every_scan_as_the_per_scan_reader(run):
    """Read as the Worker opens it (no runs yet): every directory's view at every scan, base or once appended, is the
    per-scan reference's."""
    from test_interval_append import _views_equal

    from dt_cloud import interval_read as ir

    gen = run["store"] / ist.PREFIX / "g.c002"
    paths = ["", *sorted({p for p in run["world"]["paths"] if "." not in p.rsplit("/", 1)[-1]})]
    assert _views_equal(run["world"], lambda s: ir.Store(gen / "served", []), paths) > 0


def test_a_pinned_add_follows_the_successors(run, tmp_path):
    """`add -g g` (a pin from before the compactions) appends to the newest generation; with nothing pending it changes
    nothing."""
    before = files(run["store"])
    out = add(run["world"], run["store"], tmp_path / "work", "-g", "g")
    assert (out["gen"], out["after"], out["added"], out["compact"]) == ("g.c002", DATES[-1], [], None)
    assert files(run["store"]) == before


def test_compacted_generations_leave_after_the_grace(run, tmp_path, monkeypatch):
    """Within the grace both compacted generations stay; past it (the prune's clock moved) an `add` deletes every file of
    each but its `successor.json`, and the pointer and the newest generation are untouched."""
    store = tmp_path / "store"
    shutil.copytree(run["store"], store)
    root = store / ist.PREFIX
    newest = files(root / "g.c002")
    out = add(run["world"], store, tmp_path / "work")
    assert [(p["gen"], p["deleted"]) for p in out["predecessors"]] == [("g.c001", 0), ("g", 0)]
    t = max(p.stat().st_mtime for p in root.rglob("compaction.json"))
    monkeypatch.setattr(sl, "now", lambda: t + ar.GRACE_S)
    out = add(run["world"], store, tmp_path / "work")
    assert [(p["gen"], p["objects"] == p["deleted"] > 0) for p in out["predecessors"]] == [("g.c001", True), ("g", True)]
    assert [sorted(files(root / g)) for g in ("g", "g.c001")] == [["successor.json"], ["successor.json"]]
    assert files(root / "g.c002") == newest
    assert json.loads((root / "CURRENT.json").read_text()) == {"gen": "g.c002"}
    assert add(run["world"], store, tmp_path / "work")["predecessors"] == []


def test_an_interrupted_compaction_is_redone(run, tmp_path):
    """An earlier `add` died mid-compaction (part of `g.c001` uploaded, no successor record, the pointer unmoved): the
    next `add` replaces the attempt's keys and publishes the generation a clean compaction does."""
    store = tmp_path / "store"
    shutil.copytree(run["tmp"] / "before-c001", store)
    torn = store / ist.PREFIX / "g.c001"
    (torn / "served").mkdir(parents=True)
    (torn / "served" / "path.parquet").write_bytes(b"torn")
    (torn / "junk.txt").write_text("an earlier attempt's\n")
    out = add(run["world"], store, tmp_path / "work", "-t", DATES[5])
    assert (out["gen"], out["compact"]["gen"]) == ("g", "g.c001")
    assert served(torn) == served(run["store"] / ist.PREFIX / "g.c001")
    assert (torn / "junk.txt").exists() is False


def test_an_unbounded_level_never_compacts(run, tmp_path):
    """`-C none`: the binary counter carries at every level and nothing compacts."""
    store = tmp_path / "store"
    shutil.copytree(run["tmp"] / "before-c001", store)
    out = add(run["world"], store, tmp_path / "work", "-t", DATES[5], "-C", "none")
    newest = json.loads(sorted((store / ist.PREFIX / "g" / "manifests").iterdir(), key=lambda p: ar.parse_manifest(p.name))[-1].read_text())
    assert (out["gen"], out["compact"], [(r["level"], r["scans"]) for r in newest["runs"]]) == ("g", None, [(2, DATES[BASE:BASE + 4])])
    assert json.loads((store / ist.PREFIX / "CURRENT.json").read_text()) == {"gen": "g"}


# ── The pointer ────────────────────────────────────────────────────────────


def test_build_points_an_empty_store_at_its_generation_and_no_other(run, tmp_path):
    store = tmp_path / "store"
    assert build(run["world"], store, tmp_path / "w1", "a", DATES[0])["current"] is True
    assert "current" not in build(run["world"], store, tmp_path / "w2", "b", DATES[0])
    assert json.loads((store / ist.PREFIX / "CURRENT.json").read_text()) == {"gen": "a"}


def test_an_unpinned_add_adopts_a_sole_generation_and_refuses_several(run, tmp_path):
    """A store from before the pointer: its only generation is adopted (`CURRENT.json` written); with two and no pointer,
    `add` refuses without `-g`."""
    store = tmp_path / "store"
    build(run["world"], store, tmp_path / "w", "a", DATES[0])
    (store / ist.PREFIX / "CURRENT.json").unlink()
    out = add(run["world"], store, tmp_path / "work", "-t", DATES[1])
    assert (out["gen"], json.loads((store / ist.PREFIX / "CURRENT.json").read_text())) == ("a", {"gen": "a"})
    build(run["world"], store, tmp_path / "w2", "b", DATES[0])
    (store / ist.PREFIX / "CURRENT.json").unlink()
    args = ["-L", LAYOUT, "-s", str(run["world"]["root"]), "-o", str(store), "-w", str(tmp_path / "work2")]
    res = CliRunner().invoke(il.add_cmd, args)
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (1, f"{store}/{ist.PREFIX}: no current generation (ambiguous: name one with -g)")


def test_the_current_command(run, tmp_path):
    store = tmp_path / "store"
    cmd = lambda *a: CliRunner().invoke(il.current_cmd, ["-o", str(store), *a])  # noqa: E731
    r = cmd()
    assert (r.exit_code, r.stdout) == (0, "")
    build(run["world"], store, tmp_path / "w", "a", DATES[0])
    build(run["world"], store, tmp_path / "w2", "b", DATES[0])
    assert [(r.exit_code, r.stdout) for r in (cmd(), cmd("-s", "b"), cmd())] == [(0, "a\n"), (0, "b\n"), (0, "b\n")]
    r = cmd("-s", "nope")
    assert (r.exit_code, r.output.strip()) == (1, f"{store}/{ist.PREFIX}/nope/scans.json: no such generation")
    (store / ist.PREFIX / "CURRENT.json").unlink()
    r = cmd()
    assert (r.exit_code, r.output.strip()) == (1, f"{store}/{ist.PREFIX}: several generations and no {ist.PREFIX}/CURRENT.json: name one with `-s`")


def test_a_pointer_left_on_a_compacted_generation_moves_to_its_successor(run, tmp_path):
    """A compaction stopped between its successor record and the pointer: the next `add` follows the record and moves the
    pointer."""
    store = tmp_path / "store"
    shutil.copytree(run["store"], store)
    ic.set_current(ic.Store(str(store)), "g")
    out = add(run["world"], store, tmp_path / "work")
    assert (out["gen"], json.loads((store / ist.PREFIX / "CURRENT.json").read_text())) == ("g.c002", {"gen": "g.c002"})


def test_a_url_store_compacts_as_a_local_one(run, tmp_path, monkeypatch):
    """A URL store (fsspec `memory://`), four scans caught up by one `add`: the compacted generation is the local store's
    (served bytes), the pointer moved; past the grace the old generation leaves but for its `successor.json`."""
    import time

    import fsspec

    name = f"{tmp_path.name}-{run['owned']}"
    url = f"memory://{name}/store"
    world = run["world"]
    args = ["-g", "g", "-L", LAYOUT, "-s", str(world["root"]), "-o", url, "-w", str(tmp_path / "wb"), "-k", "3", "-r", str(RG), "-p", "2", "-M", "1GB",
            "-t", DATES[BASE - 1]]
    assert CliRunner().invoke(il.build_cmd, args, catch_exceptions=False).exit_code == 0
    out = add(world, url, tmp_path / "work", "-t", DATES[5])
    assert ([a["scan"] for a in out["added"]], out["compact"]["gen"]) == (DATES[BASE:6], "g.c001")
    fs = fsspec.filesystem("memory")
    root = f"/{name}/store/{ist.PREFIX}"
    got = {k.split("/g.c001/", 1)[1]: fs.cat_file(k) for k in fs.find(f"{root}/g.c001")}
    assert {k: v for k, v in got.items() if (k.startswith("served/") and k.endswith(".parquet")) or k == "scans.json"} == served(
        run["store"] / ist.PREFIX / "g.c001")
    assert json.loads(fs.cat_file(f"{root}/CURRENT.json")) == {"gen": "g.c001"}
    monkeypatch.setattr(sl, "now", lambda: time.time() + ar.GRACE_S)
    out = add(world, url, tmp_path / "work", "-t", DATES[5])
    assert [(p["gen"], p["objects"] == p["deleted"] > 0) for p in out["predecessors"]] == [("g", True)]
    assert sorted(fs.find(f"{root}/g")) == [f"{root}/g/successor.json"]


def test_next_gen():
    assert [ic.next_gen(g) for g in ("2026-10-14", "2026-10-14.c001", "g.c009", "g.c999", "a.cb")] == [
        "2026-10-14.c001", "2026-10-14.c002", "g.c010", "g.c1000", "a.cb.c001"]


def test_compaction_due():
    run = lambda first, level: {"key": f"deltas/{first}", "first": first, "last": first, "level": level, "scans": [first]}  # noqa: E731
    l0 = [run(f"2026-08-0{i}", 0) for i in range(1, 5)]
    assert [ar.compaction_due(l0[:n], 2) for n in range(1, 5)] == [False, False, False, True]
    assert [ar.compaction_due(l0, lv) for lv in (1, 2, 3, None)] == [True, True, False, False]
    assert [ar.compaction_due([run("2026-08-01", 1), run("2026-08-03", 1)], lv) for lv in (2, 3)] == [True, False]
