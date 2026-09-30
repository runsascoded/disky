"""Specs for the store writer (`dt_cloud.index`, specs/path-store.md §4.2): a
synthetic layer-2 parquet (disk-tree's names, objects and dirs) → the union
bucket-prefixed, every row, cut into the `path` and `bysize` sorts under
their served names, footer sidecars beside them, the age pyramid from the
same union."""
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import index as X

BUCKET = "marin-us-east-02a"
GIB = 1024**3
BINS = ("1h", "3h", "6h", "12h", "1d", "2d", "4d", "8d")
L2_COLS = ["path", "depth", "kind", "size", "n_files", "n_children", "n_desc", "mtime", "mtime_mean"]


def _write_l2(path: Path, rows: list[tuple], extra: dict[str, list] | None = None) -> None:
    """(path, depth, kind, size, n_files, n_children, n_desc, mtime, mtime_mean|None)."""
    cols = {
        "path": [r[0] for r in rows],
        "depth": pa.array([r[1] for r in rows], pa.int32()),
        "kind": [r[2] for r in rows],
        "size": pa.array([r[3] for r in rows], pa.int64()),
        "n_files": pa.array([r[4] for r in rows], pa.int64()),
        "n_children": pa.array([r[5] for r in rows], pa.int64()),
        "n_desc": pa.array([r[6] for r in rows], pa.int64()),
        "mtime": pa.array([r[7] for r in rows], pa.int64()),
        "mtime_mean": pa.array([r[8] for r in rows], pa.float64()),
        **(extra or {}),
    }
    pq.write_table(pa.table(cols), path)


# root 40 GiB: marin 30 (a 20 = one object; b 10 = two objects), tmp 10 (one
# object), `empty` holds nothing (size 0: the `bysize` tail). The root's
# mtime_mean is NULL (no weighted time → wts/wb 0).
T17, T16, T15 = 1_700_000_000, 1_600_000_000, 1_500_000_000
L2 = [
    (".", 0, "dir", 40 * GIB, 4, 3, 9, T17, None),
    ("empty", 1, "dir", 0, 0, 0, 0, 0, None),
    ("marin", 1, "dir", 30 * GIB, 3, 2, 5, T17, float(T17)),
    ("marin/a", 2, "dir", 20 * GIB, 1, 1, 1, T17, float(T17)),
    ("marin/a/x.bin", 3, "file", 20 * GIB, 1, 0, 0, T17, float(T17)),
    ("marin/b", 2, "dir", 10 * GIB, 2, 2, 2, T16, float(T16)),
    ("marin/b/y.bin", 3, "file", 6 * GIB, 1, 0, 0, T16, float(T16)),
    ("marin/b/z.bin", 3, "file", 4 * GIB, 1, 0, 0, T16, float(T16)),
    ("tmp", 1, "dir", 10 * GIB, 1, 1, 1, T15, float(T15)),
    ("tmp/t.bin", 2, "file", 10 * GIB, 1, 0, 0, T15, float(T15)),
]
STORE_COLS = [
    "path", "size", "depth", "kind", "n_files", "n_children", "n_desc", "mtime", "mtime_mean", "created", "last_read",
]


def _read(path: Path, cols: str = "*") -> list[tuple]:
    """Rows in file order (never re-sorted: the sort is what is under test)."""
    return duckdb.connect().execute(f"SELECT {cols} FROM read_parquet('{path}')").fetchall()


def _kv(path: Path) -> dict[str, str]:
    return {k.decode(): v.decode() for k, v in pq.read_metadata(path).metadata.items() if k != b"ARROW:schema"}


def _pyr_files(out: Path) -> dict[str, str]:
    return {b: str(out / f"age-pyramid-{b}.parquet") for b in BINS}


def test_write_index(tmp_path: Path):
    l2 = tmp_path / "l2.parquet"
    _write_l2(l2, L2)
    out = tmp_path / "index"
    s = X.write_index([(BUCKET, str(l2))], out, mem="1GB", threads=2)
    # fleet 40 GiB → the age floor F_24 = 2^(35 − 24) = 2 KiB: every prefix kept;
    # four objects at three stamps ~3 years apart → 3 bins × their ancestor
    # chains (root + bucket + marin + a | b; root + bucket + tmp) = 11 rows per tier.
    assert s == {
        "rows": 10,
        "buckets": [BUCKET],
        "columns": STORE_COLS,
        "sorts": {"path": {"rows": 10, "groups": 1}, "bysize": {"rows": 10, "groups": 1}},
        "pyramid": {"floor": 2**11, "bins": {b: {"rows": 11, "file": _pyr_files(out)[b]} for b in BINS}},
        "files": {
            "path": str(out / "path-index.parquet"),
            "bysize": str(out / "path-index-bysize.parquet"),
            **{f"age-pyramid-{b}": _pyr_files(out)[b] for b in BINS},
        },
    }
    # Only the served files remain: the union itself is gone, no coarse tiers.
    assert sorted(p.name for p in out.iterdir() if p.name != ".duckdb-tmp") == sorted([
        "path-index.parquet", "path-index.groups.json",
        "path-index-bysize.parquet", "path-index-bysize.groups.json",
        *(f"age-pyramid-{b}.parquet" for b in BINS),
    ])

    # `path`: every row — objects included, `kind` on each — bucket-prefixed,
    # depth + 1, sorted (depth, path), the layer-2's names then the wire aliases.
    path = out / "path-index.parquet"
    assert pq.read_schema(path).names == STORE_COLS
    assert _kv(path) == {"tier": "path", "sort": "depth,path"}
    assert _read(path, "path, depth, kind, size, n_files, n_children, n_desc, mtime, mtime_mean, created, last_read") == [
        (BUCKET, 1, "dir", 40 * GIB, 4, 3, 9, T17, None, None, None),
        (f"{BUCKET}/empty", 2, "dir", 0, 0, 0, 0, 0, None, None, None),
        (f"{BUCKET}/marin", 2, "dir", 30 * GIB, 3, 2, 5, T17, float(T17), None, None),
        (f"{BUCKET}/tmp", 2, "dir", 10 * GIB, 1, 1, 1, T15, float(T15), None, None),
        (f"{BUCKET}/marin/a", 3, "dir", 20 * GIB, 1, 1, 1, T17, float(T17), None, None),
        (f"{BUCKET}/marin/b", 3, "dir", 10 * GIB, 2, 2, 2, T16, float(T16), None, None),
        (f"{BUCKET}/tmp/t.bin", 3, "file", 10 * GIB, 1, 0, 0, T15, float(T15), None, None),
        (f"{BUCKET}/marin/a/x.bin", 4, "file", 20 * GIB, 1, 0, 0, T17, float(T17), None, None),
        (f"{BUCKET}/marin/b/y.bin", 4, "file", 6 * GIB, 1, 0, 0, T16, float(T16), None, None),
        (f"{BUCKET}/marin/b/z.bin", 4, "file", 4 * GIB, 1, 0, 0, T16, float(T16), None, None),
    ]

    # `bysize`: the same rows by size bucket desc (40 GiB → 2^35; 30 and 20 GiB
    # → 2^34; 10 GiB → 2^33; 6 and 4 GiB → 2^32), path order within a bucket,
    # dirs and objects interleaved by size alone; the empty dir closes the file.
    bysize = out / "path-index-bysize.parquet"
    assert pq.read_schema(bysize).names == STORE_COLS
    assert _kv(bysize) == {"tier": "bysize", "sort": "size_bucket desc,path", "bucket": "log2"}
    assert _read(bysize, "path, kind, size") == [
        (BUCKET, "dir", 40 * GIB),
        (f"{BUCKET}/marin", "dir", 30 * GIB),
        (f"{BUCKET}/marin/a", "dir", 20 * GIB),
        (f"{BUCKET}/marin/a/x.bin", "file", 20 * GIB),
        (f"{BUCKET}/marin/b", "dir", 10 * GIB),
        (f"{BUCKET}/tmp", "dir", 10 * GIB),
        (f"{BUCKET}/tmp/t.bin", "file", 10 * GIB),
        (f"{BUCKET}/marin/b/y.bin", "file", 6 * GIB),
        (f"{BUCKET}/marin/b/z.bin", "file", 4 * GIB),
        (f"{BUCKET}/empty", "dir", 0),
    ]
    assert pq.read_metadata(path).row_group(0).num_rows == 10

    # The footer sidecars: one group each, `b_min`/`b_max` the group's size range
    # (the 12-field engine layout; D1's row shape is the first 11).
    for f in (path, bysize):
        doc = json.loads(f.with_suffix(".json").with_name(f.stem + ".groups.json").read_text())
        assert (doc["v"], doc["version"], doc["floor_bytes"]) == (1, 1, None)
        assert [e["name"] for e in doc["schema"][1:]] == STORE_COLS
        (g,) = doc["groups"]
        assert (g[0], g[1], g[2], g[5], g[8], g[9], g[11]) == (0, 1, 4, 40 * GIB, 0, 10, 0)
    # Path stats bound each sort's rows: `path` runs the bucket to its deepest
    # object; `bysize` the same set (one group holds every row).
    for f in (path, bysize):
        g = json.loads(f.with_name(f.stem + ".groups.json").read_text())["groups"][0]
        assert (g[3], g[4]) == (BUCKET, f"{BUCKET}/tmp/t.bin")


def test_age_pyramid_from_the_store(tmp_path: Path):
    """The pyramid reads the union's `kind = 'file'` rows: the same strata the
    per-bucket layer-2 explode produced — root (depth 0) and bucket rows summed
    once per object — floored per path."""
    l2 = tmp_path / "l2.parquet"
    _write_l2(l2, L2)
    out = tmp_path / "index"
    X.write_index([(BUCKET, str(l2))], out, mem="1GB", threads=2)
    h = lambda t: (t // 3600) * 3600 * 1000  # noqa: E731
    assert duckdb.connect().execute(
        f"SELECT path, depth, binstart, b, o FROM read_parquet('{out / 'age-pyramid-1h.parquet'}') ORDER BY depth, path, binstart"
    ).fetchall() == [
        ("", 0, h(T15), 10 * GIB, 1), ("", 0, h(T16), 10 * GIB, 2), ("", 0, h(T17), 20 * GIB, 1),
        (BUCKET, 1, h(T15), 10 * GIB, 1), (BUCKET, 1, h(T16), 10 * GIB, 2), (BUCKET, 1, h(T17), 20 * GIB, 1),
        (f"{BUCKET}/marin", 2, h(T16), 10 * GIB, 2), (f"{BUCKET}/marin", 2, h(T17), 20 * GIB, 1),
        (f"{BUCKET}/tmp", 2, h(T15), 10 * GIB, 1),
        (f"{BUCKET}/marin/a", 3, h(T17), 20 * GIB, 1),
        (f"{BUCKET}/marin/b", 3, h(T16), 10 * GIB, 2),
    ]


def test_write_index_two_buckets(tmp_path: Path):
    """A second bucket's rows join the same sorts under its own depth-1 row;
    `path` interleaves the buckets by name at each depth, `bysize` by size."""
    HERO = [
        (".", 0, "dir", 24 * GIB, 1, 1, 3, 1_710_000_000, 1_710_000_000.0),
        ("tmp", 1, "dir", 24 * GIB, 1, 1, 2, 1_710_000_000, 1_710_000_000.0),
        ("tmp/ttl=14d", 2, "dir", 24 * GIB, 1, 1, 1, 1_710_000_000, 1_710_000_000.0),
        ("tmp/ttl=14d/c.bin", 3, "file", 24 * GIB, 1, 0, 0, 1_710_000_000, 1_710_000_000.0),
    ]
    a, b = tmp_path / "a.parquet", tmp_path / "b.parquet"
    _write_l2(a, L2)
    _write_l2(b, HERO)
    out = tmp_path / "index"
    s = X.write_index([(BUCKET, str(a)), ("hero-checkpoints", str(b))], out, mem="1GB", threads=2)
    assert (s["rows"], s["buckets"], s["sorts"]) == (
        14, [BUCKET, "hero-checkpoints"],
        {"path": {"rows": 14, "groups": 1}, "bysize": {"rows": 14, "groups": 1}},
    )
    assert _read(out / "path-index.parquet", "path, depth, size")[:8] == [
        ("hero-checkpoints", 1, 24 * GIB),
        (BUCKET, 1, 40 * GIB),
        ("hero-checkpoints/tmp", 2, 24 * GIB),
        (f"{BUCKET}/empty", 2, 0),
        (f"{BUCKET}/marin", 2, 30 * GIB),
        (f"{BUCKET}/tmp", 2, 10 * GIB),
        ("hero-checkpoints/tmp/ttl=14d", 3, 24 * GIB),
        (f"{BUCKET}/marin/a", 3, 20 * GIB),
    ]
    # 24 GiB → bucket 2^34, beside marin (30) and marin/a (20), in path order.
    assert _read(out / "path-index-bysize.parquet", "path")[:6] == [
        (BUCKET,),
        ("hero-checkpoints",), ("hero-checkpoints/tmp",), ("hero-checkpoints/tmp/ttl=14d",), ("hero-checkpoints/tmp/ttl=14d/c.bin",),
        (f"{BUCKET}/marin",),
    ]


def test_store_columns_and_pivots(tmp_path: Path):
    """A labeled source puts `usr` right after `path` (the engine's label
    block); pivot columns present in any source — or implied by a v2
    listing's metadata — are in every row (0 where a source has neither);
    the columns are the layer-2's and nothing else."""
    assert X.store_columns([
        (["path", "usr", "size", "kind", "sum_storage_class_id_3", "depth"], {"sum_storage_class_id_1": "size"}),
        (["path", "size", "kind", "depth"], {}),
    ]) == [
        "path", "usr", "size", "depth", "kind", "n_files", "n_children", "n_desc", "mtime", "mtime_mean", "created", "last_read",
        "sum_storage_class_id_1", "sum_storage_class_id_3",
    ]
    assert X.store_columns([(["path", "size", "kind", "depth"], {})]) == [
        "path", "size", "depth", "kind", "n_files", "n_children", "n_desc", "mtime", "mtime_mean", "created", "last_read",
    ]
    rows = [(".", 0, "dir", 30, 2, 2, 2, T17, float(T17)), ("x.bin", 1, "file", 10, 1, 0, 0, T17, float(T17)), ("y.bin", 1, "file", 20, 1, 0, 0, T17, float(T17))]
    a, b = tmp_path / "a.parquet", tmp_path / "b.parquet"
    _write_l2(a, rows, extra={"usr": ["u", "u", None], "sum_storage_class_id_2": pa.array([10, 10, 0], pa.int64())})
    _write_l2(b, rows)
    out = tmp_path / "index"
    s = X.write_index([("A", str(a)), ("B", str(b))], out, mem="1GB", threads=2)
    assert s["columns"] == [
        "path", "usr", "size", "depth", "kind", "n_files", "n_children", "n_desc", "mtime", "mtime_mean", "created", "last_read",
        "sum_storage_class_id_2",
    ]
    assert _kv(out / "path-index.parquet")["sort"] == "depth,path,usr"
    assert _read(out / "path-index.parquet", "path, usr, size, sum_storage_class_id_2") == [
        ("A", "u", 30, 10), ("B", None, 30, 0),
        ("A/x.bin", "u", 10, 10), ("A/y.bin", None, 20, 0), ("B/x.bin", None, 10, 0), ("B/y.bin", None, 20, 0),
    ]


def test_write_index_rejects_bad_sources(tmp_path: Path):
    with pytest.raises(ValueError, match=r"^write_store: no \(bucket, layer-2\) sources$"):
        X.write_index([], tmp_path / "index")
    with pytest.raises(ValueError, match=r"^write_store: duplicate bucket in \['b', 'b'\]$"):
        X.write_index([("b", "x"), ("b", "y")], tmp_path / "index")
    thin = tmp_path / "thin.parquet"
    pq.write_table(pa.table({"path": ["."], "size": [0], "depth": [0], "kind": ["dir"]}), thin)
    with pytest.raises(ValueError, match=r"not a layer-2 parquet — missing \['n_files', 'n_children', 'n_desc', 'mtime'\]"):
        X.write_index([("b", str(thin))], tmp_path / "index")


def test_coarse_floor():
    assert X.coarse_floor(0, 24) == 1
    assert X.coarse_floor(923_009_082_966_172, 24) == 2**26  # ~840 TiB → 2^50 → 64 MiB
    assert X.coarse_floor(2**40, 16) == 2**24


def test_write_index_age_only(tmp_path: Path):
    """`age_only=True` writes only the age pyramid — no sorts, no union left
    behind — and the summary omits their rows (index-sync -A syncs just the
    age variants; the sort pointers keep their generation)."""
    l2 = tmp_path / "l2.parquet"
    _write_l2(l2, L2)
    out = tmp_path / "index"
    s = X.write_index([(BUCKET, str(l2))], out, mem="1GB", threads=2, age_only=True)
    assert s == {
        "buckets": [BUCKET],
        "pyramid": {"floor": 2**11, "bins": {b: {"rows": 11, "file": _pyr_files(out)[b]} for b in BINS}},
        "files": {f"age-pyramid-{b}": _pyr_files(out)[b] for b in BINS},
    }
    assert sorted(p.name for p in out.iterdir() if p.name != ".duckdb-tmp") == sorted(f"age-pyramid-{b}.parquet" for b in BINS)


def test_write_index_row_group_rows(tmp_path: Path):
    """`row_group_rows` sets the sorts' group size — the range-read unit and the
    footer's row count per sort (gcs cuts 32K groups; specs/path-store.md §1.6)."""
    rows = [(".", 0, "dir", 4100, 4100, 4100, 4100, T17, float(T17))] + [
        (f"f{i:04d}.bin", 1, "file", 1, 1, 0, 0, T17, float(T17)) for i in range(4100)
    ]
    l2 = tmp_path / "l2.parquet"
    _write_l2(l2, rows)
    out = tmp_path / "index"
    s = X.write_index([(BUCKET, str(l2))], out, mem="1GB", threads=2, row_group_rows=2048)
    assert s["sorts"] == {"path": {"rows": 4101, "groups": 3}, "bysize": {"rows": 4101, "groups": 3}}

