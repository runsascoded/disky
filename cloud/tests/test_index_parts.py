"""Sharded `path-index` (`index_parts`): per-bucket and per-key-range parts (`path-index -p [-K]`) merged by
`path-index-merge` are the single pass's outputs byte for byte — every sort, its `.groups.*` sidecars, `attr.tsv`,
`age.json` — and its `meta.json` but for the two clock stamps.

The fixture interleaves at every depth: bucket names that prefix each other (`bk`, `bk-a`, `bk.z`; `bk/…` sorts after
`bk-a/…`), an empty bucket (one empty shard and a 0-object marker), and `bk-big`, ~9K objects over dirs 0–4 deep
spanning several 2,048-row groups (and two at 8,192), split into key ranges whose bounds are a top-level dir's own name (`d3`), a name
between dirs (`m`) and a range holding nothing (`x`–`y`): its root is rebuilt from its parts'. Attribution exercises
the prefix map's per-part rules (a bucket glob, a path glob, a literal rule on a bucket outside the scan, an
attribution parquet row), access logs the read-recency columns, `-G` the generation column."""
from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from disk_tree.find.bulk import SUCCESS_MARKER, write_empty_shard
from dt_cloud.cli import main
from dt_cloud.index_parts import SPOOL_GROUPS_ENV, Part, check_tiling, merge_path_index, split_parts
from dt_cloud.viz import write_path_index

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 9, 1, tzinfo=UTC)
ASOF = "2026-10-01"
BIG_SPLIT = ["d3", "m", "x", "y"]

IDENTITIES = """\
users:
  ryan-williams:
    aliases: [rw]
prefix_owners:
  - prefix: gs://bk*/shared/
    user: shared-team
  - prefix: gs://bk-big/d1/p_*/
    user: proj-team
  - prefix: gs://bk-big/d1/p_2/
    user: literal-loses
  - prefix: gs://elsewhere/data/
    user: other-team
"""


def _objects(rng: random.Random, n: int, tops: list[str]) -> dict[str, tuple[int, dt.datetime, int]]:
    out = {}
    for i in range(n):
        depth = rng.choice([0, 1, 1, 2, 2, 3, 4])
        segs = [rng.choice(tops)] + [rng.choice(["p_1", "p_2", "q", "shared", "r-s", "r"]) for _ in range(depth - 1)] if depth else []
        name = "/".join([*segs, f"f{i:05d}.bin"])
        out[name] = (rng.choice([0, 1, 7, rng.randint(1, 10**12)]), T0 + dt.timedelta(seconds=rng.randint(0, 10**7)), rng.choice([1, 1, 1, 2, 3, 4]))
    return out


def _listing(d: Path, bucket: str, objects: dict | None) -> str:
    if objects is None:
        write_empty_shard(str(d), "shard-00-0000")
    else:
        d.mkdir(parents=True)
        names = list(objects)
        pd.DataFrame({
            "bucket": [bucket] * len(names), "name": names, "size_bytes": [objects[k][0] for k in names],
            "created": [objects[k][1] for k in names], "storage_class_id": [objects[k][2] for k in names],
            "generation": range(1, len(names) + 1),
        }).to_parquet(d / "shard-00-0000.parquet")
    (d / SUCCESS_MARKER).write_text(json.dumps({"bucket": bucket, "prefix": "", "objects": len(objects or {}),
                                                 "started": f"2026-10-01T0{len(bucket) % 10}:00:00Z", "finished": "2026-10-01T09:00:00Z"}))
    return f"{d}/*.parquet"


@pytest.fixture(scope="module")
def scan(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("parts")
    rng = random.Random(7)
    tops = ["d0", "d1", "d2", "d3", "d3-x", "d3.y", "d4", "m", "ma", "shared", "zz"]
    buckets = {
        "bk-big": _objects(rng, 9000, tops),
        "bk": _objects(rng, 300, ["a", "shared", "b-c"]),
        "bk-a": _objects(rng, 200, ["a", "shared"]),
        "bk.z": _objects(rng, 50, ["a"]),
        "bk-empty": None,
    }
    listings = {b: _listing(root / "listing" / b, b, objs) for b, objs in buckets.items()}
    attribution = root / "attribution.parquet"
    pd.DataFrame({"prefix": ["gs://bk-big/d0/", "gs://bk/a/", "gs://bk-big/d1/p_2/"], "user": ["rw", "alice", "parquet-wins"],
                  "source": ["user-prefix"] * 3, "asof": [dt.date(2026, 9, 30)] * 3}).to_parquet(attribution)
    identities = root / "identities.yaml"
    identities.write_text(IDENTITIES)
    access = root / "access.parquet"
    pd.DataFrame({
        "bucket": ["bk-big", "bk-big", "bk", "bk-a"], "path": ["d1/p_1", "d3", ".", "shared"],
        "day": [dt.date(2026, 9, 20)] * 3 + [dt.date(2026, 10, 1)],
        "op": ["GET", "HEAD", "GET", "GET"],
        "last_ts": [dt.datetime(2026, 9, 20, h, tzinfo=UTC) for h in (3, 5, 7)] + [dt.datetime(2026, 10, 1, 1, tzinfo=UTC)],
        "n_ops": [1, 2, 3, 4], "bytes_out": [10, 20, 30, 40],
    }).to_parquet(access)
    return {"root": root, "listings": listings, "attribution": str(attribution), "identities": str(identities), "access": str(access)}


def _parts(splits: dict[str, list[str]], buckets) -> list[Part]:
    return [p for b in buckets for p in (split_parts(b, splits[b]) if b in splits else [Part(b)])]


def _run(scan: dict, out: Path, parts: list[Part] | None, **kw) -> dict:
    """`path-index` over the whole scan (`parts` None) or each part then the merge; returns the meta."""
    attr = dict(attributions=(scan["attribution"],), identities_path=scan["identities"], access=(scan["access"],))
    listings = scan["listings"]
    if parts is None:
        return write_path_index(tuple(listings.values()), out / "snap", ASOF, **attr, path_index=out / "index" / "path-index.parquet", **kw)
    search = kw.pop("search", False)
    dirs = []
    for i, p in enumerate(parts):
        d = out / "parts" / f"{i:02d}"
        write_path_index((listings[p.bucket],), d, ASOF, **attr, path_index=d / "path-index.parquet", part=p, **kw)
        dirs.append(d)
    return merge_path_index(dirs, out / "snap", out / "index" / "path-index.parquet", search=search)


def _files(d: Path) -> dict[str, bytes]:
    return {f.name: f.read_bytes() for f in sorted(d.iterdir()) if f.is_file()}


def _assert_identical(single: Path, merged: Path, m1: dict, m2: dict) -> None:
    a, b = _files(single / "index"), _files(merged / "index")
    assert sorted(a) == sorted(b)
    assert [n for n in a if a[n] != b[n]] == []
    assert _files(single / "snap")["age.json"] == _files(merged / "snap")["age.json"]
    stamps = ("generated", "published")
    assert {k: v for k, v in m1.items() if k not in stamps} == {k: v for k, v in m2.items() if k not in stamps}
    on_disk = [{k: v for k, v in json.loads((d / "snap" / "meta.json").read_text()).items() if k not in stamps} for d in (single, merged)]
    assert on_disk[0] == on_disk[1]


SPLITS = {"bk-big": BIG_SPLIT}


@pytest.mark.parametrize("kw", [
    pytest.param(dict(row_group_rows=2048, user_sort_tiers=("bysize",), generation=True), id="gcs-shape"),
    pytest.param(dict(row_group_rows=2048), id="both-user-copies"),
    pytest.param(dict(row_group_rows=2048, path_only=True), id="path-only"),
    pytest.param(dict(row_group_rows=8192, search=True), id="8k-search"),
])
def test_parts_merge_is_the_single_pass(scan, tmp_path, kw, monkeypatch):
    # several spool chunks per sort (the ordered COPY reads them as one scan)
    monkeypatch.setenv(SPOOL_GROUPS_ENV, "2")
    m1 = _run(scan, tmp_path / "single", None, **kw)
    m2 = _run(scan, tmp_path / "merged", _parts(SPLITS, scan["listings"]), **kw)
    _assert_identical(tmp_path / "single", tmp_path / "merged", m1, m2)
    md = pq.read_metadata(tmp_path / "single" / "index" / "path-index.parquet")
    rows, rg = kw["row_group_rows"], md.num_row_groups
    # the fixture spans several groups, every one full but the last
    assert rg >= (2 if rows > 2048 else 3) and [md.row_group(i).num_rows for i in range(rg - 1)] == [rows] * (rg - 1)
    assert md.num_rows == m1["index"]["rows"]


def test_per_bucket_parts(scan, tmp_path):
    """No key ranges: one part per bucket, nothing recomputed."""
    kw = dict(row_group_rows=2048, user_sort_tiers=("bysize",))
    m1 = _run(scan, tmp_path / "single", None, **kw)
    m2 = _run(scan, tmp_path / "merged", _parts({}, scan["listings"]), **kw)
    _assert_identical(tmp_path / "single", tmp_path / "merged", m1, m2)


def test_split_root_is_rebuilt(scan, tmp_path):
    """The split bucket's root row in the merged `path` sort: one per owner slice, the whole bucket's (its parts' sum),
    not any part's share."""
    _run(scan, tmp_path / "m", _parts(SPLITS, scan["listings"]), row_group_rows=2048)
    df = pd.read_parquet(tmp_path / "m" / "index" / "path-index.parquet")
    files = df[(df.kind == "file") & df.path.str.startswith("bk-big/")]
    roots = df[df.path == "bk-big"]
    assert (int(roots["size"].sum()), int(roots.n_files.sum())) == (int(files["size"].sum()), len(files))
    top = df[(df.depth == 2) & df.path.str.startswith("bk-big/")]
    assert set(roots.n_children) == {top.path.nunique()}
    parts = [json.loads((tmp_path / "m" / "parts" / f"{i:02d}" / "part.json").read_text()) for i in range(len(BIG_SPLIT) + 1)]
    assert [p["part"] for p in parts] == [{"bucket": "bk-big", "lo": lo, "hi": hi} for lo, hi in
                                          [(None, "d3"), ("d3", "m"), ("m", "x"), ("x", "y"), ("y", None)]]
    # the `x`–`y` range holds nothing: no rows, no root
    assert (parts[3]["partial"]["total_objects"], parts[3]["rows"]["path"]) == (0, 0)


def test_empty_bucket_alone_and_split(tmp_path):
    """`test_empty_bucket`'s cases through parts: an empty bucket beside a full one, and an empty bucket split into
    key ranges (each range writes its 0-byte root; the merge's sum is the single pass's)."""
    objs = {"aa/xx.bin": (10, T0, 1), "yy.bin": (20, T0, 1)}
    listings = {"bk1": _listing(tmp_path / "l" / "bk1", "bk1", objs), "bk2": _listing(tmp_path / "l" / "bk2", "bk2", None)}
    for name, parts in (("alone", [Part("bk1"), Part("bk2")]), ("split", [Part("bk1"), *split_parts("bk2", ["m"])])):
        out = tmp_path / name
        m1 = write_path_index(tuple(listings.values()), out / "single" / "snap", "2026-10-17", path_index=out / "single" / "index" / "path-index.parquet")
        dirs = []
        for i, p in enumerate(parts):
            d = out / "merged" / "parts" / str(i)
            write_path_index((listings[p.bucket],), d, "2026-10-17", path_index=d / "path-index.parquet", part=p)
            dirs.append(d)
        m2 = merge_path_index(dirs, out / "merged" / "snap", out / "merged" / "index" / "path-index.parquet")
        _assert_identical(out / "single", out / "merged", m1, m2)
        df = pd.read_parquet(out / "merged" / "index" / "path-index.parquet")
        assert [(r.path, r.kind, r.size, r.n_files, r.n_children, r.n_desc) for r in df.itertuples()] == [
            ("bk1", "dir", 30, 2, 2, 3), ("bk2", "dir", 0, 0, 0, 0),
            ("bk1/aa", "dir", 10, 1, 1, 1), ("bk1/yy.bin", "file", 20, 1, 0, 0),
            ("bk1/aa/xx.bin", "file", 10, 1, 0, 0),
        ]


def test_cli(scan, tmp_path):
    """`path-index -p [-K]` per part, then `path-index-merge`, as the scan step runs them."""
    single, merged = tmp_path / "single", tmp_path / "merged"
    common = ["-d", ASOF, "-a", scan["attribution"], "-i", scan["identities"], "-x", scan["access"], "-r", "2048", "-u", "bysize"]
    r = CliRunner().invoke(main, ["path-index", *common, *[a for g in scan["listings"].values() for a in ("-l", g)],
                                  "-o", str(single / "snap"), "-P", str(single / "index" / "path-index.parquet")])
    assert r.exit_code == 0, r.output
    dirs = []
    for i, p in enumerate(_parts(SPLITS, scan["listings"])):
        d = merged / "parts" / str(i)
        rng = ["-K", f"{p.lo or ''}:{p.hi or ''}"] if p.split else []
        r = CliRunner().invoke(main, ["path-index", *common, "-l", scan["listings"][p.bucket], "-p", p.bucket, *rng,
                                      "-P", str(d / "path-index.parquet")])
        assert r.exit_code == 0, r.output
        dirs.append(str(d))
    r = CliRunner().invoke(main, ["path-index-merge", "-o", str(merged / "snap"), "-P", str(merged / "index" / "path-index.parquet"), *dirs])
    assert r.exit_code == 0, r.output
    load = lambda d: json.loads((d / "snap" / "meta.json").read_text())  # noqa: E731
    _assert_identical(single, merged, load(single), load(merged))


def test_merge_refuses_a_missing_range(scan, tmp_path):
    parts = split_parts("bk-big", ["d3", "m"])
    check_tiling(parts)
    with pytest.raises(ValueError, match=r"bk-big: parts \[\(None, 'd3'\), \('m', None\)\] don't tile its keys"):
        check_tiling([parts[0], parts[2]])
    with pytest.raises(ValueError, match="bk-big: parts .* don't tile its keys"):
        check_tiling([Part("bk-big"), parts[1]])


def test_split_bounds_validated():
    assert split_parts("b", ["m"]) == [Part("b", None, "m"), Part("b", "m", None)]
    with pytest.raises(ValueError, match="increase strictly"):
        split_parts("b", ["m", "c"])
    with pytest.raises(ValueError, match="without ':' or '/'"):
        split_parts("b", ["a/b"])
