"""The builder's read source (`read_from: gcs | r2`, specs/static-compaction.md): the profiles' option and env, the
merge and compaction jobs' `-F` (refused when the jobs can't hold the R2 credentials), and reads from another copy of
the store — local dirs standing in for the data bucket and the R2 copy — give the same generation as reads from the
bucket, are logged with their bytes and seconds per source, and are refused when the copy lacks a file."""
from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud import append_runner as ar
from dt_cloud import interval_compact as ic
from dt_cloud import interval_local as il
from dt_cloud import interval_store as ist
from dt_cloud.blobstore import Store
from dt_cloud.static_profile import from_mapping, load_profile

from test_compact_level import _static_runner
from test_interval_append import COMMON, G1, Fake as IvFake
from test_interval_compact import DATES, LAYOUT, RG, build, make_world


def _calls_merge(f: IvFake) -> list[tuple[str, str]]:
    return [c for c in f.calls() if c[0] == "merge"]


def test_profile_read_from():
    assert [from_mapping({}).read_from, from_mapping({"read_from": "r2"}).read_from,
            load_profile({"STATIC_NAMES_PROFILE": "gcs", "STATIC_NAMES_READ_FROM": "r2"}).read_from] == ["gcs", "r2", "r2"]
    with pytest.raises(SystemExit) as e:
        from_mapping({"read_from": "s3"})
    assert str(e.value) == "read_from must be gcs or r2, not 's3'"


def test_jobs_pass_read_from():
    """`r2`: the merge and compaction jobs get `-F r2` (the interval fake's stages run as the R2 account)."""
    f = IvFake(["2026-08-03"], read_from="r2")
    runs = [{"key": f"deltas/{a}_{b}", "first": a, "last": b, "level": 2, "scans": [a, b]} for a, b in (("2026-08-04", "2026-08-05"), ("2026-08-06", "2026-08-07"))]
    f.keys[f"{G1}/manifests/2026-08-07.json"] = {"date": "2026-08-07", "runs": runs}
    f.runner(merge_wait=None).carries()
    assert _calls_merge(f) == [("merge", f"carry -L 5 -F r2 -M 90GB -p 16 {COMMON} -m /gcs/data")]
    r = f.runner()
    assert r.compact_jobs("2026-08-07")[0][1]["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1].split(
        "dt_cloud.interval_append ")[1] == f"compact -C level:5 -F r2 -r -j 1 -M 90GB -p 16 {COMMON} -m /gcs/data"


def test_read_from_r2_needs_the_r2_account():
    """The static example's stages run as another account than the R2 copy's: reading R2 in their jobs is refused."""
    r = _static_runner(5)
    r.cfg = replace(r.cfg, read_from="r2")
    with pytest.raises(SystemExit) as e:
        r.merge_job("2026-10-10")
    assert str(e.value) == ("read_from r2: the merge and compaction jobs run as build@proj, not the R2 account copy@proj: they can't read the R2 copy "
                            "(read from gcs, or make the R2 account the stages')")


# ── Reads from another copy: local dirs for both stores ────────────────────


@pytest.fixture(scope="module")
def stores(tmp_path_factory):
    """A base and two scans appended (carried into one run), the store copied (the R2
    stand-in) before any compaction."""
    tmp = tmp_path_factory.mktemp("rf")
    world = make_world(tmp / "scans", False)
    store = tmp / "gcs"
    build(world, store, tmp / "wb", "g", DATES[1])
    for d in DATES[2:4]:
        res = CliRunner().invoke(il.add_cmd, ["-L", LAYOUT, "-s", str(world["root"]), "-o", str(store), "-w", str(tmp / "wa"), "-r", str(RG), "-p", "2",
                                              "-M", "1GB", "-C", "3", "-t", d], catch_exceptions=False)
        assert res.exit_code == 0, res.output
    return {"tmp": tmp, "world": world, "store": store}


def _copy(stores: dict, dst: Path) -> Path:
    shutil.copytree(stores["store"], dst)
    return dst


def _gen_files(d: Path) -> dict[str, bytes]:
    """A generation's files but its compaction record, each served sort's record without its seconds (`s`, a wall time
    that rounds to 0.1 on a loaded machine)."""
    out = {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file() and p.name != ar.COMPACTION}
    for k in [k for k in out if k.startswith("served/") and k.endswith(".json") and k != "served/stats.json"]:
        out[k] = json.dumps({**json.loads(out[k]), "s": None}, sort_keys=True).encode()
    return out


def test_compaction_reads_the_copy(stores, tmp_path):
    """`run_compaction` with `read_from="r2"` (the copy a `Store` over a dir): the generation the in-place read
    makes, the read logged from the copy; a copy lacking a file the bucket has is refused before anything is written."""
    gcs_a, gcs_b, r2 = _copy(stores, tmp_path / "a"), _copy(stores, tmp_path / "b"), _copy(stores, tmp_path / "r2")
    kw = dict(policy=ar.NEVER, force=True, threads=2, mem="1GB", log=lambda m: None, now=0.0)
    a = ar.run_compaction(Store(str(gcs_a)), ic.compactor(RG), "g", gcs_a, tmp_path / "wa", **kw)
    b = ar.run_compaction(Store(str(gcs_b)), ic.compactor(RG), "g", Path("/nonexistent"), tmp_path / "wb", read_from="r2", source=Store(str(r2)), **kw)
    reads = b["compact"]["reads"]
    want = {k: n for k, n in Store(str(r2)).listing(f"{ist.PREFIX}/g").items() if not k.split("/", 2)[2].startswith(il.UNREAD)}
    assert (reads["source"], reads["files"], reads["bytes"], reads["fetched"], type(reads["s"])) == (str(r2), len(want), sum(want.values()),
                                                                                                      sum(want.values()), float)
    assert a["compact"]["reads"]["source"] == f"{gcs_a} (mount)"
    assert _gen_files(gcs_a / ist.PREFIX / "g.c001") == _gen_files(gcs_b / ist.PREFIX / "g.c001")
    # the copy lacks a range file the bucket has: refused, nothing written
    gcs_c, r2_c = _copy(stores, tmp_path / "c"), _copy(stores, tmp_path / "r2c")
    gone = sorted((r2_c / ist.PREFIX / "g" / "pvl").glob("*.parquet"))[0]
    gone.unlink()
    with pytest.raises(SystemExit) as e:
        ar.run_compaction(Store(str(gcs_c)), ic.compactor(RG), "g", Path("/nonexistent"), tmp_path / "wc", read_from="r2", source=Store(str(r2_c)), **kw)
    key = gone.relative_to(r2_c).as_posix()
    assert str(e.value) == (f"compact g: read: {r2_c} lacks 1 of the data bucket's files under {ist.PREFIX}/g/ (e.g. {key}): "
                            "it holds the readers' files only; read from gcs")
    assert (gcs_c / ist.PREFIX / "g.c001").exists() is False


def test_merge_reads_the_copy(stores, tmp_path):
    """A merge's input runs mirrored from the copy (`read_runs`): byte for byte the bucket's, with the read's bytes."""
    gcs, r2 = stores["store"], _copy(stores, tmp_path / "r2")
    gen = gcs / ist.PREFIX / "g"
    store = ar.LocalRunStore(gen, tmp_path / "lease", "g")
    keys = [ar.run_key(d, d) for d in DATES[2:4]]
    root, doc = ar.read_runs(store, keys, gen, tmp_path / "read", source=(Store(str(r2)), f"{ist.PREFIX}/g"), log=lambda m: None)
    want = {k: n for k in keys for k, n in [(f"{k}/{x.removeprefix(k + '/')}", n) for x, n in store.listing(f"{k}/").items()]}
    assert (root, doc["source"], doc["files"], doc["bytes"]) == (tmp_path / "read", str(r2), len(want), sum(want.values()))
    assert {k: (root / k).read_bytes() for k in want} == {k: (gen / k).read_bytes() for k in want}
    place, doc = ar.read_runs(store, keys, gen, tmp_path / "unused", log=lambda m: None)
    assert (place, doc["files"], doc["bytes"], doc["s"]) == (gen, len(want), sum(want.values()), None)


def test_add_reads_the_copy(stores, tmp_path):
    """`interval-store add -F <copy>`: the same run as reading `-o`, written to `-o` only; refused when the copy lacks a
    file."""
    world = stores["world"]
    a, b, r2 = _copy(stores, tmp_path / "a"), _copy(stores, tmp_path / "b"), _copy(stores, tmp_path / "r2")
    args = ["-L", LAYOUT, "-s", str(world["root"]), "-r", str(RG), "-p", "2", "-M", "1GB", "-C", "3", "-t", DATES[4]]
    for store, extra in ((a, []), (b, ["-F", str(r2)])):
        res = CliRunner().invoke(il.add_cmd, [*args, "-o", str(store), "-w", str(tmp_path / f"w-{store.name}"), *extra], catch_exceptions=False)
        assert res.exit_code == 0, res.output
    run = ist.PREFIX + "/g/" + ar.run_key(DATES[4], DATES[4])
    # Every served file, a sort's `<sort>.json` without its cut's wall-clock seconds (`s`)
    served = lambda d: {p.relative_to(d).as_posix(): {k: v for k, v in json.loads(p.read_text()).items() if k != "s"} if p.suffix == ".json" else p.read_bytes()  # noqa: E731
                        for p in sorted((d / run / "served").rglob("*")) if p.is_file()}
    assert served(a) == served(b)
    assert (r2 / run).exists() is False
    (r2 / ist.PREFIX / "g" / "scans.json").unlink()
    res = CliRunner().invoke(il.add_cmd, [*args, "-o", str(_copy(stores, tmp_path / "c")), "-w", str(tmp_path / "wc"), "-F", str(r2)])
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (
        1, f"add g: read: {r2} lacks 1 of the data bucket's files under {ist.PREFIX}/g/ (e.g. {ist.PREFIX}/g/scans.json): "
           "it holds the readers' files only; read from gcs")
