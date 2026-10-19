"""An empty bucket in a scan of several (cw's `marin-us-east-08a`, emptied 2026-10-19): `bulk-list` writes its listing as
one empty shard in the listing schema (`write_empty_shard`) beside `_SUCCESS.json` (0 objects). Through the
listings-direct path (gcs, the r2 demo): `path-index` writes it as a 0-byte bucket root, as `import` does on cw;
`index-verify` passes it; the interval store and the static name index take it in a base build and in `add` — a bucket
that held objects the scan before closes them (its root reads 0, its objects gone), and one refilled reopens."""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import duckdb
import pandas as pd
import pytest
from click.testing import CliRunner

from disk_tree.find.bulk import SUCCESS_MARKER, write_empty_shard
from dt_cloud import interval_local as il
from dt_cloud import interval_read as ir
from dt_cloud import interval_store as ist
from dt_cloud import static_append as sa
from dt_cloud import static_local as sl
from dt_cloud import static_names as sn
from dt_cloud.cli import main
from dt_cloud.index_verify import verify_index
from dt_cloud.interval_verify import Scan, compare
from dt_cloud.viz import write_path_index

from test_static_append import _combine, _sx

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 1, tzinfo=UTC)
#: Per scan, per bucket: its objects (`None`: an empty bucket's listing). bk2 empties, stays empty, then refills.
SCANS = {
    "2026-10-16": {"bk1": {"aa/xx.bin": 10, "yy.bin": 20}, "bk2": {"dd/zz.bin": 5, "ww.bin": 7}},
    "2026-10-17": {"bk1": {"aa/xx.bin": 10, "yy.bin": 20}, "bk2": None},
    "2026-10-18": {"bk1": {"aa/xx.bin": 10, "yy.bin": 20, "aa/vv.bin": 4}, "bk2": None},
    "2026-10-19": {"bk1": {"aa/xx.bin": 10, "yy.bin": 20, "aa/vv.bin": 4}, "bk2": {"qq.bin": 3}},
}
IDS = list(SCANS)
LAYOUT = "index/{id}/path-index.parquet"


def _listing(d: Path, bucket: str, objects: dict[str, int] | None) -> str:
    """A bucket's listing dir as `bulk-list` leaves it: shards + `_SUCCESS.json`; returns the shard glob."""
    if objects is None:
        write_empty_shard(str(d), "shard-00-0000")
    else:
        d.mkdir(parents=True)
        pd.DataFrame({
            "bucket": [bucket] * len(objects), "name": list(objects), "size_bytes": list(objects.values()),
            "created": [T0] * len(objects), "storage_class_id": [1] * len(objects), "generation": range(1, len(objects) + 1),
        }).to_parquet(d / "shard-00-0000.parquet")
    (d / SUCCESS_MARKER).write_text(json.dumps({"bucket": bucket, "prefix": "", "objects": len(objects or {}),
                                                 "started": "2026-10-16T06:00:00Z", "finished": "2026-10-16T06:01:00Z"}))
    return f"{d}/*.parquet"


def _globs(root: Path, scan: str) -> list[str]:
    return [_listing(root / "listing" / scan / b, b, objs) for b, objs in SCANS[scan].items()]


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> dict:
    """Every scan's listings and `path-index -P` output (the `path` sort at `LAYOUT`, the snapshot JSONs)."""
    root = tmp_path_factory.mktemp("empty-bucket")
    metas = {}
    for scan in IDS:
        metas[scan] = write_path_index(tuple(_globs(root, scan)), root / "snap" / scan, scan, path_index=root / LAYOUT.replace("{id}", scan))
    return {"root": root, "metas": metas, "scans": [{"id": s, "src": LAYOUT.replace("{id}", s), "ts": sn.scan_epoch(s), "version": 2} for s in IDS]}


def _sort(world: dict, scan: str) -> list[tuple]:
    df = pd.read_parquet(world["root"] / LAYOUT.replace("{id}", scan))
    return [(r.path, r.kind, r.size, r.n_files, r.n_children, r.n_desc) for r in df.itertuples()]


def test_path_index_writes_an_empty_bucket_as_a_zero_byte_root(world):
    """The empty bucket is its root alone, 0 bytes, no objects, no children; the totals are the other bucket's."""
    assert _sort(world, "2026-10-17") == [
        ("bk1", "dir", 30, 2, 2, 3), ("bk2", "dir", 0, 0, 0, 0),
        ("bk1/aa", "dir", 10, 1, 1, 1), ("bk1/yy.bin", "file", 20, 1, 0, 0),
        ("bk1/aa/xx.bin", "file", 10, 1, 0, 0),
    ]
    meta = world["metas"]["2026-10-17"]
    assert (meta["total_bytes"], meta["total_objects"], meta["class_bytes"], meta["index"]["rows"]) == (30, 2, {1: 30}, 5)
    bysize = pd.read_parquet(world["root"] / "index" / "2026-10-17" / "path-index-bysize.parquet")
    assert sorted(bysize.path) == ["bk1", "bk1/aa", "bk1/aa/xx.bin", "bk1/yy.bin", "bk2"]


def test_path_index_over_only_empty_buckets(tmp_path):
    """Every bucket empty: each one's 0-byte root, and nothing else."""
    globs = (_listing(tmp_path / "l" / "bk1", "bk1", None), _listing(tmp_path / "l" / "bk2", "bk2", None))
    pidx = tmp_path / "index" / "path-index.parquet"
    meta = write_path_index(globs, tmp_path / "snap", "2026-10-17", path_index=pidx)
    assert (meta["total_bytes"], meta["total_objects"], meta["index"]["rows"]) == (0, 0, 2)
    df = pd.read_parquet(pidx)
    assert [(r.path, r.kind, r.size, r.n_files, r.n_desc) for r in df.itertuples()] == [("bk1", "dir", 0, 0, 0), ("bk2", "dir", 0, 0, 0)]


def test_index_verify_passes_an_empty_bucket(world, tmp_path):
    """`age-pyramid -c` and `index-verify` (the scan step's checks) over the scan with the empty bucket: bk2 listed with
    nothing, its 0-byte root in the sort."""
    root, scan = world["root"], "2026-10-17"
    pi = tmp_path / "pi"
    shutil.copytree(root / "index" / scan, pi)
    pyr = CliRunner().invoke(main, ["age-pyramid", "-c", "-t", "2", "-o", str(pi), str(pi / "path-index.parquet")])
    assert pyr.exit_code == 0, pyr.output
    report = verify_index(pi / "path-index.parquet", _globs(tmp_path / "relist", scan), root / "snap" / scan, ["bk1", "bk2"], scan=scan)
    assert report == {"scan": scan, "buckets": {"bk1": {"bytes": 30, "objects": 2}, "bk2": {"bytes": 0, "objects": 0}},
                      "totals": {"bytes": 30, "objects": 2}}


def _depth1(store: ir.Store, ts: int) -> list[tuple]:
    return [(r["path"], r["size"], r["n_files"], r["n_desc"]) for r in store.point_depth(ts, 1, ir.Cost())]


def _newest_runs(gen: Path) -> list[Path]:
    """The newest manifest's runs (`manifests/<id>[.mNNN].json`, the last in scan-id order)."""
    newest = sorted((gen / "manifests").iterdir())[-1]
    return [gen / r["key"] / "served" for r in json.loads(newest.read_text())["runs"]]


@pytest.mark.parametrize("through", IDS[:2], ids=["base-full", "base-empty"])
def test_interval_store_closes_and_reopens_an_emptied_bucket(world, tmp_path, through):
    """The interval store's base build through `through`, then `add` of the rest: at every scan the depth-1 rows are
    the scan's own (bk2 at 0 while empty, its objects gone), and every view at the root and each bucket is the per-scan
    sort's, tile for tile."""
    root = world["root"]
    common = ["-g", "g", "-L", LAYOUT, "-s", str(root), "-o", str(tmp_path / "store"), "-r", "4", "-p", "2", "-M", "1GB"]
    res = CliRunner().invoke(il.build_cmd, [*common, "-w", str(tmp_path / "work-build"), "-k", "2", "-t", through], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    res = CliRunner().invoke(il.add_cmd, [*common, "-w", str(tmp_path / "work-add")], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    gen = tmp_path / "store" / ist.PREFIX / "g"
    store = ir.Store(gen / "served", _newest_runs(gen))
    ts = {s["id"]: s["ts"] for s in world["scans"]}
    assert {scan: _depth1(store, ts[scan]) for scan in IDS} == {
        "2026-10-16": [("bk1", 30, 2, 3), ("bk2", 12, 2, 3)],
        "2026-10-17": [("bk1", 30, 2, 3), ("bk2", 0, 0, 0)],
        "2026-10-18": [("bk1", 34, 3, 4), ("bk2", 0, 0, 0)],
        "2026-10-19": [("bk1", 34, 3, 4), ("bk2", 3, 1, 1)],
    }
    for s in world["scans"]:
        ref = Scan(duckdb.connect(), str(root / s["src"]), s["version"], s["src"])
        for path in ("", "bk1", "bk2"):
            for md in (None, 1):
                want, got = ref.view(path, 30, 30, max_depth=md), store.view(s["ts"], path, 30, 30, max_depth=md)
                assert compare(got["tree"], want["tree"], with_f=True) == [], (s["id"], path, md)


@pytest.mark.parametrize("through", IDS[:2], ids=["base-full", "base-empty"])
def test_static_names_add_takes_an_emptied_bucket(world, tmp_path, through):
    """The static name index's base build through `through`, then `add` of the rest, reads as the generation rebuilt
    over every scan (suffix rows): bk2's names close while it's empty and its new one opens on the refill."""
    root = world["root"]

    def build(store: Path, work: Path, *extra: str) -> None:
        res = CliRunner().invoke(sl.build_cmd, ["-g", "g", "-L", LAYOUT, "-s", str(root), "-o", str(store), "-x", "off", "-w", str(work),
                                                "-k", "2", "-p", "2", "-M", "1GB", *extra], catch_exceptions=False)
        assert res.exit_code == 0, res.output

    build(tmp_path / "store", tmp_path / "work", "-t", through)
    res = CliRunner().invoke(sl.add_cmd, ["-g", "g", "-L", LAYOUT, "-s", str(root), "-o", str(tmp_path / "store"), "-w", str(tmp_path / "work-add"),
                                          "-p", "2", "-M", "1GB", "-N"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    added = [x["scan"] for x in json.loads(res.stdout)["added"]]
    assert added == IDS[IDS.index(through) + 1:]
    build(tmp_path / "full", tmp_path / "work-full")
    gen, full = tmp_path / "store" / sn.PREFIX / "g", tmp_path / "full" / sn.PREFIX / "g"
    assert _combine(_sx([gen, *(gen / sa.run_key(d, d) for d in added)])) == _sx([full])
