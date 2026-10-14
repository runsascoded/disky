"""`dt_cloud.static_local`: the single-machine `static-names build` writes the generation the staged path's stages write
(byte for byte: coalesced versions, suffix shards, sidecar, catalog; the drill as `static_drill.build_drill` over the
staged roots), and `static-names add` appends scans as runs that read, under the combine rule, as the generation rebuilt
over every scan (suffix rows, catalog, first hits and answers on every date)."""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from dt_cloud import static_append as sa
from dt_cloud import static_local as sl
from dt_cloud import static_names as sn

from test_static_append import TERMS, _catalog, _combine, _reader, _sx
from test_static_catalog import _gen
from test_static_drill import _drill
from test_static_names import _brute_answer, _oracle, fixture, scan_ids  # noqa: F401

V = 3
K = 2  # scans appended after the base
R, KEEP, FLOOR = 3, 2, 2


def _layout(scans: dict) -> str:
    s = scans["scans"][0]
    return s["src"].replace(s["id"], "{id}")


def _build(root: Path, scans: dict, store: Path | str, work: Path, gen: str = "g", through: str | None = None):
    args = ["-g", gen, "-L", _layout(scans), "-s", str(root), "-o", str(store), "-x", "off", "-w", str(work), "-k", "2", "-n", "60",
            "-V", str(V), "-R", str(R), "-K", str(KEEP), "-f", str(FLOOR), "-p", "2", "-M", "1GB", *(["-t", through] if through else [])]
    res = CliRunner().invoke(sl.build_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def _add(root: Path, scans: dict, store: Path, work: Path, gen: str = "g"):
    res = CliRunner().invoke(sl.add_cmd, ["-g", gen, "-L", _layout(scans), "-s", str(root), "-o", str(store), "-w", str(work), "-p", "2", "-M", "1GB"],
                             catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def _files(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def built(fixture, tmp_path_factory):  # noqa: F811
    root, scans, merged = fixture
    tmp = tmp_path_factory.mktemp("local")
    rg, sn.SX_RG = sn.SX_RG, 5  # several row groups per shard, as `_gen`
    try:
        summary = _build(root, scans, tmp / "store", tmp / "work")
        staged = _gen(root, scans, tmp / "staged", V)
    finally:
        sn.SX_RG = rg
    return {"root": root, "scans": scans, "merged": merged, "summary": summary, "gen": tmp / "store" / sn.PREFIX / "g", "staged": staged, "tmp": tmp}


def test_build_writes_the_staged_generation(built):
    gen, staged = built["gen"], built["staged"]
    assert json.loads((gen / "ranges.json").read_text()) == staged["ranges"]
    for sub, src in (("cintervals", staged["build"]), ("chist", staged["build"]), ("sx", staged["out"]), ("sidecar", staged["out"])):
        assert _files(gen / sub) == _files(src / sub), sub
    assert pq.read_table(gen / "sidecar.parquet").equals(staged["side"])
    assert json.loads((gen / "shards.json").read_text())["shards"] == sn.plan_shards(sorted((staged["build"] / "chist").glob("*.parquet")), 60, 2)["shards"]
    for f in ("cells.parquet", "index.parquet"):
        assert (gen / "catalog" / f).read_bytes() == (staged["final"] / f).read_bytes(), f


def test_build_scans_json(built):
    root, scans = built["root"], built["scans"]
    doc = json.loads((built["gen"] / "scans.json").read_text())
    assert doc == {"bucket": str(root), "layouts": [_layout(scans)],
                   "scans": [{**s, "size": (root / s["src"]).stat().st_size} for s in scans["scans"]]}


def test_build_drill_is_the_reference_drill(built):
    """The drill equals `build_drill` over the staged generation's roots (the test reference: aliases by exact root set,
    where the build aliases by root-set digest); `meta.json` adds the measurement floor."""
    gen, staged, tmp = built["gen"], built["staged"], built["tmp"]
    ref = tmp / "ref" / "drill"
    meas = _drill(staged["con"], staged, ref, R, KEEP, FLOOR)
    got, want = _files(gen / "drill"), _files(ref)
    assert sorted(got) == sorted(want)
    assert {k: v for k, v in got.items() if k != "meta.json"} == {k: v for k, v in want.items() if k != "meta.json"}
    assert json.loads(got["meta.json"]) == {**json.loads(want["meta.json"]), "floor": FLOOR}
    for sub, kind in (("dirs", "long"), ("short/dirs", "short")):
        got_meas = pq.read_table(gen / "roots-measure" / sub / "s0000.parquet").to_pylist()
        assert got_meas == sorted(pq.read_table(meas[kind]).to_pylist(), key=lambda r: (r["q"], r["k"], r["dir"])), kind


def test_build_refuses_an_existing_generation(built, tmp_path):
    res = CliRunner().invoke(sl.build_cmd, ["-g", "g", "-L", _layout(built["scans"]), "-s", str(built["root"]), "-o", str(built["tmp"] / "store"),
                                            "-x", "off", "-w", str(tmp_path)])
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (1, f"{built['tmp'] / 'store'}/{sn.PREFIX}/g/ exists: a generation is never rewritten")


@pytest.fixture(scope="module")
def appended(fixture, tmp_path_factory):  # noqa: F811
    root, scans, merged = fixture
    tmp = tmp_path_factory.mktemp("local-add")
    n = len(scans["scans"])
    _build(root, scans, tmp / "store", tmp / "work", through=scans["scans"][n - K - 1]["id"])
    base_files = _files(tmp / "store" / sn.PREFIX / "g")
    out = _add(root, scans, tmp / "store", tmp / "work-add")
    _build(root, scans, tmp / "full", tmp / "work-full")
    return {"root": root, "scans": scans, "merged": merged, "out": out, "gen": tmp / "store" / sn.PREFIX / "g", "full": tmp / "full" / sn.PREFIX / "g",
            "base_files": base_files, "tmp": tmp}


def test_add_publishes_each_scan_then_the_carry(appended):
    ids = scan_ids(appended["scans"])
    a, b = ids[-2], ids[-1]
    gen = appended["gen"]
    assert sorted(p.name for p in (gen / "manifests").iterdir()) == [f"{a}.json", f"{b}.json", f"{b}.m001.json"]
    newest = json.loads((gen / "manifests" / f"{b}.m001.json").read_text())
    assert [(r["key"], r["level"], r["scans"]) for r in newest["runs"]] == [(sa.run_key(a, b), 1, [a, b])]
    assert newest["scans"] == ids
    assert [x["scan"] for x in appended["out"]["added"]] == [a, b]
    # the base's files are never rewritten
    assert {k: v for k, v in _files(gen).items() if k in appended["base_files"]} == appended["base_files"]


def test_add_reads_as_the_rebuild(appended):
    """Base ⊕ the level-0 runs, and base ⊕ the merged run: the rebuild's suffix rows, catalog and answers."""
    ids = scan_ids(appended["scans"])
    gen, full = appended["gen"], appended["full"]
    level0 = [gen / sa.run_key(d, d) for d in ids[-K:]]
    merged = gen / sa.run_key(ids[-K], ids[-1])
    for tiers in (level0, [merged]):
        assert _combine(_sx([gen, *tiers])) == _sx([full])
        out = appended["tmp"] / "check" / tiers[0].name
        sa.merge_catalogs([gen / "catalog", *(t / "catalog" for t in tiers)], out)
        for f in ("cells.parquet", "index.parquet"):
            assert (out / f).read_bytes() == (full / "catalog" / f).read_bytes(), f
        tiered = sa.TieredReader([_reader(gen, pq.read_table(gen / "sidecar.parquet")), *(_reader(t, pq.read_table(t / "sidecar.parquet")) for t in tiers)])
        rebuilt = sa.TieredReader([_reader(full, pq.read_table(full / "sidecar.parquet"))])
        oracle = _oracle(appended["merged"])
        for term in TERMS:
            if len(term) >= 3:
                assert tiered.hits(term) == rebuilt.hits(term), term
                assert tiered.answer(term, ids)["answers"] == {d: _brute_answer(oracle, term, d) for d in ids}, term
        tiered_cat = sa.TieredCatalog([_catalog(gen / "catalog"), *(_catalog(t / "catalog") for t in tiers)])
        for term in TERMS:
            assert tiered_cat.answer(term, ids) == _catalog(full / "catalog").answer(term, ids), term


def test_add_drills_each_run(appended):
    ids = scan_ids(appended["scans"])
    gen = appended["gen"]
    for d in ids[-K:]:
        meta = json.loads((gen / sa.run_key(d, d) / "drill" / "meta.json").read_text())
        assert (meta["first"], meta["last"], meta["scans"]) == (d, d, [d])
    assert json.loads((gen / sa.run_key(ids[-K], ids[-1]) / "drill" / "meta.json").read_text())["scans"] == ids[-K:]


def test_add_with_nothing_pending_is_a_nop(appended):
    before = _files(appended["gen"])
    out = _add(appended["root"], appended["scans"], appended["tmp"] / "store", appended["tmp"] / "work-nop")
    assert out == {"gen": "g", "after": scan_ids(appended["scans"])[-1], "added": [], "merge": None, "files": 0}
    assert _files(appended["gen"]) == before


def test_url_store_matches_a_local_one(built, tmp_path):
    """A URL store (fsspec `memory://`) gets the same keys and bytes as a local one (but the catalog's timing)."""
    import fsspec

    rg, sn.SX_RG = sn.SX_RG, 5
    try:
        _build(built["root"], built["scans"], f"memory://{tmp_path.name}/store", tmp_path / "work")
    finally:
        sn.SX_RG = rg
    fs = fsspec.filesystem("memory")
    got = {k.split("/store/", 1)[1]: fs.cat_file(k) for k in sorted(fs.find(f"/{tmp_path.name}/store"))}
    want = {f"{sn.PREFIX}/g/{k}": v for k, v in _files(built["gen"]).items()}
    timing = f"{sn.PREFIX}/g/catalog/meta.json"  # its `s` (seconds) differs run to run
    untimed = lambda files: {k: ({**json.loads(v), "s": None} if k == timing else v) for k, v in files.items()}  # noqa: E731
    assert untimed(got) == untimed(want)


def test_publish_order():
    keys = ["manifests/a.json", "deltas/a/drill/meta.json", "deltas/a/meta.json", "deltas/a/sx/s0000.parquet", "deltas/a/catalog/meta.json",
            "deltas/a/drill/long-roots-index.parquet", "sidecar.parquet"]
    assert sl.publish_order(keys) == ["deltas/a/drill/long-roots-index.parquet", "deltas/a/sx/s0000.parquet", "sidecar.parquet",
                                      "deltas/a/catalog/meta.json", "deltas/a/meta.json", "deltas/a/drill/meta.json", "manifests/a.json"]

