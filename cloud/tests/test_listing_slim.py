"""Spec `listing-slim.md` phase 1: everything the overlay builds from a layer-2
listing comes out the same from a v1 listing (`uri` + every pivot column,
Snappy, no format metadata — what the engine wrote before) and from the v2
listing of the same input (no `uri`, single-valued pivots implied), under
both `$DISK_TREE_PARQUET_CODEC` values (snappy default, zstd opt-in).

The v1 file is the v2 file's restored v1 view (`blobfs.read_parquet`) written
the way the old writer did (DuckDB COPY, Snappy, 64K-row groups); the engine
suite checks that restored view against the pandas engine's own v1 frame."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow.parquet as pq
import pytest

from disk_tree.blobfs import read_parquet
from disk_tree.find.aggregate_duckdb import aggregate_listing_to_parquet
from disk_tree.listing import prepare_listing
from dt_cloud import cascade_a2a, index as X
from dt_cloud.sweep import Plan, build_expiry_manifest, build_manifest

BUCKET = "bk"
T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
NOW = int((T0 + timedelta(days=30)).timestamp())
#: (name, size, age in hours before T0) — nested dirs, a TTL tree, a root file,
#: a `//` key and a folder placeholder, all in one storage class (2).
OBJECTS = [
    ("marin/ckpt/a.bin", 1000, 1),
    ("marin/ckpt/sub/b.bin", 2000, 30),
    ("marin/ckpt/sub/c.bin", 3000, 300),
    ("marin/data/d.txt", 400, 5000),
    ("marin/data//e.txt", 50, 7),
    ("marin/empty/", 0, 2),
    ("tmp/ttl=1d/old.bin", 70, 24 * 20),
    ("tmp/ttl=14d/new.bin", 80, 24),
    ("top.txt", 9, 12),
]


@pytest.fixture(params=['snappy', 'zstd'])
def codec(request, monkeypatch) -> str:
    """Every check runs under both `$DISK_TREE_PARQUET_CODEC` values; returns
    the parquet footer's codec name."""
    monkeypatch.setenv('DISK_TREE_PARQUET_CODEC', request.param)
    return request.param.upper()


def _listing(path: Path) -> str:
    pd.DataFrame({
        "bucket": [BUCKET] * len(OBJECTS),
        "name": [n for n, _, _ in OBJECTS],
        "size_bytes": [s for _, s, _ in OBJECTS],
        "created": [T0 - timedelta(hours=h) for _, _, h in OBJECTS],
        "storage_class_id": [2] * len(OBJECTS),
    }).to_parquet(path)
    return str(path)


@pytest.fixture
def layer2(tmp_path: Path, codec) -> tuple[str, str]:
    """(v1, v2) layer-2 listings of the same objects, labeled `usr` (the a2a shape)."""
    listing = _listing(tmp_path / "listing.parquet")
    labels = tmp_path / "labels.parquet"
    pd.DataFrame({"prefix": ["marin", "tmp"], "usr": ["alice", None]}).to_parquet(labels)
    con = duckdb.connect()
    v2 = str(tmp_path / "v2.parquet")
    aggregate_listing_to_parquet(
        prepare_listing(con, (listing,)), bucket=BUCKET, scheme="s3", out_parquet=v2, con=con,
        pivot_sums=("storage_class_id",), mean_mtime=True, label=str(labels),
    )
    v1 = str(tmp_path / "v1.parquet")
    v1_df = read_parquet(v2)
    con.execute(f"COPY (SELECT * FROM v1_df) TO '{v1}' (FORMAT PARQUET, ROW_GROUP_SIZE 65536)")
    return v1, v2


def test_the_two_formats(layer2, codec):
    v1, v2 = layer2
    base = ["path", "usr", "size", "mtime", "n_desc", "n_files", "n_children", "kind", "parent"]
    assert pq.read_schema(v1).names == [*base, "sum_storage_class_id_2", "mtime_mean", "uri", "depth"]
    assert pq.read_schema(v2).names == [*base, "mtime_mean", "depth"]
    md = pq.read_metadata(v2)
    assert {k.decode(): v.decode() for k, v in md.metadata.items() if k != b"ARROW:schema"} == {
        "disk_tree.listing_format": "2",
        "disk_tree.scan_root": "s3://bk",
        "disk_tree.implied": '{"sum_storage_class_id_2":"size"}',
        "disk_tree.columns": json.dumps([*base, "sum_storage_class_id_2", "mtime_mean", "uri", "depth"], separators=(",", ":")),
    }
    codecs = {md.row_group(g).column(c).compression for g in range(md.num_row_groups) for c in range(md.num_columns)}
    assert codecs == {codec}
    assert pq.read_metadata(v1).metadata is None
    # Either file reads back as the same v1 frame.
    pd.testing.assert_frame_equal(read_parquet(v1), read_parquet(v2))


def test_indexes_are_byte_identical(layer2, codec, tmp_path: Path):
    """The store's sorts (+ sidecars) and the age pyramid, from either format:
    the v2's implied `sum_storage_class_id_2` is restored as a real column
    (`= size`), so the store carries it either way."""
    v1, v2 = layer2
    s1 = X.write_index([(BUCKET, v1)], tmp_path / "i1", mem="1GB", threads=1)
    s2 = X.write_index([(BUCKET, v2)], tmp_path / "i2", mem="1GB", threads=1)
    files = sorted(p.name for p in (tmp_path / "i1").glob("*.parquet")) + sorted(p.name for p in (tmp_path / "i1").glob("*.json"))
    assert files == sorted(p.name for p in (tmp_path / "i2").glob("*.parquet")) + sorted(p.name for p in (tmp_path / "i2").glob("*.json"))
    assert files == [
        *sorted(["path-index.parquet", "path-index-bysize.parquet", *(f"age-pyramid-{b}.parquet" for b in X.AGE_PYRAMID_BINS)]),
        "path-index-bysize.groups.json", "path-index.groups.json",
    ]
    assert {f: (tmp_path / "i1" / f).read_bytes() == (tmp_path / "i2" / f).read_bytes() for f in files} == {f: True for f in files}
    assert json.dumps(s1).replace("/i1/", "/") == json.dumps(s2).replace("/i2/", "/")
    assert s1["columns"] == [
        "path", "usr", "size", "depth", "kind", "n_files", "n_children", "n_desc", "mtime", "mtime_mean", "created", "last_read",
        "sum_storage_class_id_2",
    ]
    # The single-bin age index (ad-hoc) too.
    for i, src in ((1, v1), (2, v2)):
        con = duckdb.connect()
        store, _ = X.write_store(con, [(BUCKET, src)], tmp_path / f"i{i}")
        X.write_age_index(con, store, tmp_path / f"i{i}")
    assert (tmp_path / "i1" / X.AGE_INDEX).read_bytes() == (tmp_path / "i2" / X.AGE_INDEX).read_bytes()
    # And the index is written in the switch's codec.
    md = pq.read_metadata(tmp_path / "i2" / "path-index.parquet")
    assert {md.row_group(0).column(c).compression for c in range(md.num_columns)} == {codec}


def test_sweep_manifests_are_byte_identical(layer2, tmp_path: Path):
    v1, v2 = layer2
    plan = Plan(name="p", bucket=BUCKET, sweep=["marin/ckpt/"], plan_id=1)
    out = {}
    for tag, src in (("v1", v1), ("v2", v2)):
        s = build_manifest(src, plan, str(tmp_path / f"plan-{tag}"))
        e = build_expiry_manifest(src, str(tmp_path / f"exp-{tag}"), bucket=BUCKET, now_ts=NOW)
        out[tag] = (s, e)
    for kind in ("plan", "exp"):
        m1 = tmp_path / f"{kind}-v1" / "manifest" / f"{BUCKET}.parquet"
        m2 = tmp_path / f"{kind}-v2" / "manifest" / f"{BUCKET}.parquet"
        assert m1.read_bytes() == m2.read_bytes()
    (s1, e1), (s2, e2) = out["v1"], out["v2"]
    assert {**s1, "manifest": None} == {**s2, "manifest": None} == {
        "plan_id": 1, "name": "p", "bucket": BUCKET, "sweep": ["marin/ckpt/"],
        "objects": 3, "bytes": 6000, "manifest": None,
    }
    assert {**e1, "manifest": None} == {**e2, "manifest": None}


def _mgu_index(v1: str, out: Path) -> str:
    """The mgu-side path index `cascade_a2a` compares against, from the v1 rows."""
    duckdb.connect().execute(f"""
        COPY (
            SELECT CASE WHEN path = '.' THEN '{BUCKET}' ELSE '{BUCKET}/' || path END AS path,
                   usr, size AS b, n_files AS o,
                   COALESCE(mtime_mean * size, 0) AS wts, size AS wb,
                   sum_storage_class_id_2 AS c2, NULL::BIGINT AS c3, NULL::BIGINT AS c4
            FROM read_parquet('{v1}')
        ) TO '{out}' (FORMAT PARQUET)
    """)
    return str(out)


def test_cascade_a2a_reads_implied_classes(layer2, tmp_path: Path):
    """A v2 file's implied class pivot is `size`, not "absent → 0": the report
    against the same mgu index is the v1 file's."""
    v1, v2 = layer2
    idx = _mgu_index(v1, tmp_path / "mgu.parquet")
    r1 = cascade_a2a.compare(BUCKET, idx, v1)
    r2 = cascade_a2a.compare(BUCKET, idx, v2)
    assert r1 == r2
    assert (r2["ok"], r2["against_zero"], {m: v["n"] for m, v in r2["mismatch"].items()}) == (
        True, ["c3", "c4"], {"b": 0, "o": 0, "c2": 0, "c3": 0, "c4": 0, "mtime": 0},
    )
