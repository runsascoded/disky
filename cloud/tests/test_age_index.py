"""The per-path created-day age index (`dt_cloud.index.write_age_index`) and its
footer extraction (`index_footer` with no `usr` column) — specs/age-index.md.
Both read the store's union (`write_store`), not the per-bucket layer-2s."""
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.index import AGE_INDEX, write_age_index, write_store
from dt_cloud.index_footer import extract, groups_blob

DAY = 86400


def _l2(tmp_path: Path, files: list[tuple[str, int, int, str]]) -> Path:
    """A synthetic layer-2 parquet (disk-tree import shape): rows of
    (path, size, mtime, kind); the count columns are a leaf's."""
    n = len(files)
    t = pa.table({
        "path": [f[0] for f in files],
        "size": [f[1] for f in files],
        "mtime": [f[2] for f in files],
        "kind": [f[3] for f in files],
        "depth": [f[0].count("/") + 1 for f in files],
        "n_files": [1 if f[3] == "file" else 0 for f in files],
        "n_children": [0] * n,
        "n_desc": [0] * n,
    })
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "l2.parquet"
    pq.write_table(t, p)
    return p


def _store(con: duckdb.DuckDBPyConnection, sources: list[tuple[str, Path]], out: Path) -> str:
    return write_store(con, [(b, str(l2)) for b, l2 in sources], out)[0]


def _read(path: Path) -> list[tuple]:
    return duckdb.sql(
        f"SELECT path, depth, day, b, o FROM read_parquet('{path}') ORDER BY depth, path, day"
    ).fetchall()


def test_rollup_is_descendant_inclusive(tmp_path: Path):
    l2 = _l2(tmp_path, [
        ("marin/tmp/ttl=14d/a.bin", 100, 100 * DAY, "file"),
        ("marin/tmp/ttl=14d/b.bin", 200, 100 * DAY, "file"),  # same dir+day → summed
        ("marin/tmp/ttl=14d/c.bin", 50, 200 * DAY, "file"),   # same dir, later day
        ("marin/tmp/keep/d.bin", 10, 100 * DAY, "file"),
        ("root-obj.bin", 7, 200 * DAY, "file"),               # bucket-root direct child
        ("marin/tmp/ttl=14d", 0, 0, "dir"),                   # dir rows ignored
    ])
    con = duckdb.connect()
    write_age_index(con, _store(con, [("BKT", l2)], tmp_path), tmp_path)
    assert _read(tmp_path / AGE_INDEX) == [
        # depth 0: the synthetic fleet root (all buckets summed per day)
        ("", 0, 100, 310, 3),
        ("", 0, 200, 57, 2),
        # depth 1: the bucket
        ("BKT", 1, 100, 310, 3),
        ("BKT", 1, 200, 57, 2),
        # depth 2: marin/
        ("BKT/marin", 2, 100, 310, 3),
        ("BKT/marin", 2, 200, 50, 1),
        # depth 3: marin/tmp
        ("BKT/marin/tmp", 3, 100, 310, 3),
        ("BKT/marin/tmp", 3, 200, 50, 1),
        # depth 4: the leaves
        ("BKT/marin/tmp/keep", 4, 100, 10, 1),
        ("BKT/marin/tmp/ttl=14d", 4, 100, 300, 2),
        ("BKT/marin/tmp/ttl=14d", 4, 200, 50, 1),
    ]


def test_floor_drops_small_prefixes_but_keeps_root(tmp_path: Path):
    # One big prefix (2 GiB) and one tiny one (100 B); the floor is the finest
    # coarse tier of the ~2 GiB fleet, so the tiny leaf drops and every big
    # ancestor (incl. the fleet root) stays.
    big = 2 * 1024**3
    l2 = _l2(tmp_path, [
        ("big/x.bin", big, 100 * DAY, "file"),
        ("tiny/y.bin", 100, 100 * DAY, "file"),
    ])
    con = duckdb.connect()
    s = write_age_index(con, _store(con, [("BKT", l2)], tmp_path), tmp_path)
    paths = {r[0] for r in _read(tmp_path / AGE_INDEX)}
    assert s["floor"] > 100
    assert "BKT/tiny" not in paths          # below floor
    assert paths == {"", "BKT", "BKT/big"}  # root + bucket + the big leaf


def test_multi_bucket_root_sums_every_bucket(tmp_path: Path):
    la = _l2(tmp_path / "a", [("m/x.bin", 1000, 100 * DAY, "file")])
    lb = _l2(tmp_path / "b", [("m/y.bin", 2000, 100 * DAY, "file")])
    con = duckdb.connect()
    write_age_index(con, _store(con, [("A", la), ("B", lb)], tmp_path), tmp_path)
    rows = _read(tmp_path / AGE_INDEX)
    root = [r for r in rows if r[0] == ""]
    assert root == [("", 0, 100, 3000, 2)]  # 1000 (A) + 2000 (B), 2 objects


def test_footer_extract_without_usr_column(tmp_path: Path):
    l2 = _l2(tmp_path, [
        ("marin/a.bin", 1_000_000_000, 100 * DAY, "file"),
        ("marin/sub/c.bin", 500_000_000, 100 * DAY, "file"),
    ])
    con = duckdb.connect()
    s = write_age_index(con, _store(con, [("BKT", l2)], tmp_path), tmp_path)
    schema, groups = extract(str(tmp_path / AGE_INDEX))
    assert [e["name"] for e in schema["schema"][1:]] == ["path", "depth", "day", "b", "o"]
    assert schema["version"] == 1  # not a store sort
    assert schema["floor_bytes"] == s["floor"]
    assert len(groups) == 1
    g = groups[0]
    assert (g["u_min"], g["u_max"]) == (None, None)  # no ownership column
    assert g["d_min"] == 0 and g["d_max"] == 3       # depths 0..3 present
    assert g["p_min"] == "" and g["p_max"] == "BKT/marin/sub"
    # the blob round-trips (u_min/u_max NULL slots included)
    assert isinstance(groups_blob(schema, groups), str)


# --- Phase B: multi-scale pyramid ------------------------------------------

def _read_bin(path: Path) -> list[tuple]:
    return duckdb.sql(
        f"SELECT path, depth, binstart, b, o FROM read_parquet('{path}') ORDER BY depth, path, binstart"
    ).fetchall()


def test_age_pyramid_bins_and_cascade(tmp_path: Path):
    from dt_cloud.index import write_age_pyramid
    H = 3600  # one hour of seconds
    # Two objects one hour apart on the same day/month under marin/x, one under
    # marin/y a day later — exercises 1h buckets cascading to 1d and 1mo.
    base_day = 20000 * 86400  # an epoch-day far from boundaries, in seconds
    l2 = _l2(tmp_path, [
        ("marin/x/a.bin", 100, base_day + 0 * H, "file"),   # hour 0
        ("marin/x/b.bin", 200, base_day + 1 * H, "file"),   # hour 1 (same day/month)
        ("marin/y/c.bin", 50, base_day + 25 * H, "file"),   # next day (same month)
    ])
    con = duckdb.connect()
    s = write_age_pyramid(con, _store(con, [("BKT", l2)], tmp_path), tmp_path, bins=("1h", "1d", "1mo"))
    ms = 1000
    h0 = base_day * ms                             # a's hour bucket
    h1 = (base_day + H) * ms                        # b's hour bucket
    hc = (base_day + 25 * H) * ms                   # c's hour bucket (base_day is hour-aligned)
    d0 = (base_day // 86400) * 86400 * ms           # day of a,b
    d1 = ((base_day + 25 * H) // 86400) * 86400 * ms  # next day (c)

    # 1h tier: x split across two hour buckets, descendant-inclusive to root+bucket+marin
    assert _read_bin(tmp_path / "age-pyramid-1h.parquet") == [
        ("", 0, h0, 100, 1), ("", 0, h1, 200, 1), ("", 0, hc, 50, 1),
        ("BKT", 1, h0, 100, 1), ("BKT", 1, h1, 200, 1), ("BKT", 1, hc, 50, 1),
        ("BKT/marin", 2, h0, 100, 1), ("BKT/marin", 2, h1, 200, 1), ("BKT/marin", 2, hc, 50, 1),
        ("BKT/marin/x", 3, h0, 100, 1), ("BKT/marin/x", 3, h1, 200, 1),
        ("BKT/marin/y", 3, hc, 50, 1),
    ]
    # 1d tier: x's two hours collapse into one day bucket (300 B, 2 obj)
    assert _read_bin(tmp_path / "age-pyramid-1d.parquet") == [
        ("", 0, d0, 300, 2), ("", 0, d1, 50, 1),
        ("BKT", 1, d0, 300, 2), ("BKT", 1, d1, 50, 1),
        ("BKT/marin", 2, d0, 300, 2), ("BKT/marin", 2, d1, 50, 1),
        ("BKT/marin/x", 3, d0, 300, 2),
        ("BKT/marin/y", 3, d1, 50, 1),
    ]
    # 1mo tier: everything in one month bucket
    mo = duckdb.sql(f"SELECT epoch_ms(date_trunc('month', to_timestamp({base_day})))").fetchone()[0]
    assert _read_bin(tmp_path / "age-pyramid-1mo.parquet") == [
        ("", 0, mo, 350, 3),
        ("BKT", 1, mo, 350, 3),
        ("BKT/marin", 2, mo, 350, 3),
        ("BKT/marin/x", 3, mo, 300, 2),
        ("BKT/marin/y", 3, mo, 50, 1),
    ]
    assert set(s["bins"]) == {"1h", "1d", "1mo"}
