"""The scan's sharded `index` (`options.index.parts`): `scan step index <part>` per part, then `scan step index`
merging them. On GCS: each part stages only its bucket's listing, aggregates its range and uploads its dir beside the
generation; the merge stages the parts, merges, uploads the generation as the one pass does, then drops the parts. On
a local data dir, run for real: the generation and the snapshot are the one-pass step's, byte for byte."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import disk_tree
import dt_cloud
from dt_cloud import scan_steps as S
from dt_cloud.scan_profile import ProfileError, parse

from test_index_parts import _listing, _objects
from test_scan_steps import Runner

SCAN = "2026-10-16T1200"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("SCAN_RUNS_PROFILE", raising=False)
    (tmp_path / "t").mkdir()
    return {"SNAP_ID": SCAN, "GEN": "G1", "DT_JOB_START": "1", "TMPDIR": str(tmp_path / "t"), "HOME": str(tmp_path / "home")}


def _profile(data: str, index: dict, executor: dict | None = None):
    return parse({"deployment": "fix", "executor": executor or {"kind": "local"}, "work": f"{data}/nf", "data": data,
                  "layout": {"layer2": "listing/{scan}/", "snapshots": "", "listing": "listing/{scan}/"},
                  "sources": ["gcs://east", "gcs://big", "gcs://eu"],
                  "options": {"index": {"mode": "path-index", **index}, "static-names": {"profile": "sn.json"},
                              "digest": {"template": "t", "config": "d.yml"}}})


def test_parts_config_checks(tmp_path):
    with pytest.raises(ProfileError) as e:
        _profile(str(tmp_path), {"parts": {"nope": [], "big": ["m", "c"], "eu": "m"}})
    assert [p for p in e.value.problems if "parts" in p] == [
        "options.index.parts.nope: not one of the sources' buckets",
        "options.index.parts.big: big: split bounds must increase strictly (got ['m', 'c'])",
        "options.index.parts.eu: a list of split bounds (first key segments)",
    ]
    p = _profile(str(tmp_path), {"parts": {"big": ["d3", "m"]}})
    assert [(pid, part.asdict()) for pid, part in S.index_parts(p)] == [
        ("east", {"bucket": "east", "lo": None, "hi": None}),
        ("big~0", {"bucket": "big", "lo": None, "hi": "d3"}),
        ("big~1", {"bucket": "big", "lo": "d3", "hi": "m"}),
        ("big~2", {"bucket": "big", "lo": "m", "hi": None}),
        ("eu", {"bucket": "eu", "lo": None, "hi": None}),
    ]
    assert S.index_parts(_profile(str(tmp_path), {})) == []


def test_gcs_parts_then_merge_commands(tmp_path, env, monkeypatch):
    """GCS data: a part stages its own bucket's listing (+ attribution, access), aggregates with `-p`/`-K` and the
    index's `args` but `-S`, its rollup cache per part, and uploads `part.json` last; the merge stages every part, runs
    `path-index-merge -S`, uploads the generation (`path-index.parquet` last), publishes, then drops the parts."""
    monkeypatch.setattr(S.Ctx, "nproc", property(lambda self: 8))
    mnt = tmp_path / "mnt"
    for b in ("east", "big", "eu"):
        (mnt / f"dvx/listing/{SCAN}/{b}").mkdir(parents=True)
        (mnt / f"dvx/listing/{SCAN}/{b}/_SUCCESS.json").write_text("{}")
    (mnt / "dvx/access/agg/x").mkdir(parents=True)
    (mnt / "dvx/access/agg/x/a.parquet").write_text("")
    uploads, dropped = [], []
    monkeypatch.setattr(S, "upload", lambda files, dest: uploads.append(([f.name for f in files], dest)))
    monkeypatch.setattr(S, "drop_prefix", lambda ctx, key: dropped.append(key))
    p = _profile("gs://dvx", {"parts": {"big": ["m"]}, "attribution": ["attr/a.parquet"], "access": "access/agg/*/*.parquet",
                              "args": ["-u", "bysize", "-S"]},
                 executor={"kind": "google-batch", "project": "p", "region": "r", "image": "i"})
    c = S.make_ctx(p, {**env, "DT_DATA_MOUNT": str(mnt)})
    runner = Runner(tmp_path)

    def run(argv, env, stdout=None, text=True):
        r = runner(argv, env, stdout, text)
        if argv[:2] == ["dt-cloud", "path-index"]:
            d = Path(argv[argv.index("-P") + 1]).parent
            for n in ("path-index.parquet", "path-index-bysize.parquet", "part.json"):
                (d / n).write_text(n)
        if argv[:2] == ["dt-cloud", "path-index-merge"]:
            pi = Path(argv[argv.index("-P") + 1]).parent
            for n in ("path-index.parquet", "path-index-bysize.parquet", "attr.tsv"):
                (pi / n).write_text(n)
            out = Path(argv[argv.index("-o") + 1])
            out.mkdir(parents=True)
            (out / "meta.json").write_text("{}")
        return r

    c.sh_impl = run
    w = f"<tmp>/t/dt-scan/{SCAN}"
    S.index(c, "big~1")
    assert runner.calls == [
        f"dt-cloud stage -o {w}/stage /gcs/dvx/listing/{SCAN}/big/*.parquet /gcs/dvx/listing/{SCAN}/big/_SUCCESS.json "
        "/gcs/dvx/attr/a.parquet /gcs/dvx/access/agg/*/*.parquet",
        f"dt-cloud path-index -d {SCAN} -l {w}/stage/dvx/listing/{SCAN}/big/*.parquet -a {w}/stage/dvx/attr/a.parquet "
        f"-x {w}/stage/dvx/access/agg/*/*.parquet -p big -K m: -c <tmp>/mnt/dvx/listing/{SCAN}/dir-cache/big~1 "
        f"-P {w}/parts/big~1/path-index.parquet -u bysize",
    ]
    assert uploads == [(["path-index-bysize.parquet", "path-index.parquet", "part.json"], f"gs://dvx/listing/{SCAN}/index-parts/G1/big~1")]

    runner.calls.clear()
    uploads.clear()
    with pytest.raises(S.StepFailed, match="missing input .*index-parts/G1/east/part.json"):
        S.index(c, None)
    for pid in ("east", "big~0", "big~1", "eu"):
        (mnt / f"dvx/listing/{SCAN}/index-parts/G1/{pid}").mkdir(parents=True)
        (mnt / f"dvx/listing/{SCAN}/index-parts/G1/{pid}/part.json").write_text("{}")
    S.index(c, None)
    st = f"{w}/stage/dvx/listing/{SCAN}/index-parts/G1"
    assert runner.calls == [
        f"dt-cloud stage -o {w}/stage /gcs/dvx/listing/{SCAN}/index-parts/G1/*",
        f"dt-cloud path-index-merge -o {w}/snap -P {w}/path-index/path-index.parquet -S {st}/east {st}/big~0 {st}/big~1 {st}/eu",
    ]
    assert uploads == [(["attr.tsv", "path-index-bysize.parquet", "path-index.parquet"], f"gs://dvx/listing/{SCAN}/index/G1")]
    assert dropped == [f"listing/{SCAN}/index-parts/G1"]
    # a part rerun after the merge skips: the generation is there
    runner.calls.clear()
    S.index(c, "east")
    assert runner.calls == []


def _cli(tmp: Path):
    """A `subprocess.run` running `dt-cloud` as this checkout's module (the venv's entry point may be another tree's)."""
    pypath = os.pathsep.join(str(Path(m.__file__).parents[1]) for m in (dt_cloud, disk_tree))

    def run(argv, env, stdout=None, text=True):
        argv = [sys.executable, "-m", "dt_cloud.cli", *argv[1:]] if argv[0] == "dt-cloud" else list(argv)
        return subprocess.run(argv, env={**env, "PYTHONPATH": pypath, "PATH": os.environ["PATH"]}, stdout=stdout, text=text)
    return run


def test_local_sharded_step_is_the_one_pass_step(tmp_path, env):
    """A local data dir, run for real: the parts' steps then the merge step leave the generation dir and the snapshot
    the one-pass step leaves (every file byte for byte; `meta.json` but for its clock stamps); the parts are dropped."""
    import random

    rng = random.Random(3)
    objs = {"east": _objects(rng, 400, ["a", "b"]), "big": _objects(rng, 3000, ["d0", "d3", "m", "mz", "z"]), "eu": None}
    out = {}
    for name, index in (("one", {"args": ["-r", "2048"]}), ("parts", {"args": ["-r", "2048"], "parts": {"big": ["d3", "m"]}})):
        data = tmp_path / name
        for b, o in objs.items():
            _listing(data / f"listing/{SCAN}/{b}", b, o)
        c = S.make_ctx(_profile(str(data), index), env)
        c.sh_impl = _cli(tmp_path)
        for pid, _ in S.index_parts(c.profile):
            S.index(c, pid)
        S.index(c, None)
        gen = data / f"listing/{SCAN}/index/G1"
        out[name] = ({f.name: f.read_bytes() for f in sorted(gen.iterdir())},
                     (data / c.snapshots / SCAN / "age.json").read_bytes(), json.loads((data / c.snapshots / SCAN / "meta.json").read_text()))
        assert not (data / f"listing/{SCAN}/index-parts/G1").exists()
    (g1, a1, m1), (g2, a2, m2) = out["one"], out["parts"]
    assert sorted(g1) == ["path-index-bysize.groups.json", "path-index-bysize.groups.parquet", "path-index-bysize.parquet",
                          "path-index.groups.json", "path-index.groups.parquet", "path-index.parquet"]
    assert sorted(g2) == sorted(g1) and [n for n in g1 if g1[n] != g2[n]] == []
    assert a1 == a2
    stamps = ("generated", "published")
    assert {k: v for k, v in m1.items() if k not in stamps} == {k: v for k, v in m2.items() if k not in stamps}
