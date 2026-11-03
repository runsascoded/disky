"""The parallel served-sort cut (`interval_store.write_served`, specs/diff-progressive.md §3b): parts written by
processes and joined (`parquet_pages.concat`) are the bytes one serial writer leaves, sidecar included."""
from __future__ import annotations

import random
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import interval_store as ist
from dt_cloud import static_names as sn
from dt_cloud.parquet_pages import concat, decode, encode, footer

STAMPS = [1000 * (i + 1) for i in range(6)]
RG, PAGE = 32, 8


def serial_cut(con, src: str, sort: str, out: Path, schema: pa.Schema, *, rg_rows: int, stamps: list[int], page_rows: int) -> dict:
    """The cut as one writer: the rows streamed in sort order into one `ParquetWriter`, a group closed every `rg_rows`
    rows and at each segment's end (`write_served` before it went parallel), the reference the parallel cut must equal."""
    _, order, size_col = ist.SORTS[sort]
    bounds: list[dict] = []
    out.parent.mkdir(parents=True, exist_ok=True)
    sql = f"SELECT {ist.qcols(schema.names)}, {ist.seg_sql(stamps)} AS __seg FROM {src} ORDER BY __seg, {order}"
    t = pa.Table.from_batches(list(sn._batches(con, sql)))
    segs = t.column("__seg").to_pylist()
    t = t.drop_columns(["__seg"]).cast(schema)
    with ist._served_writer(out, schema, page_rows) as w:
        a = 0
        while a < t.num_rows:
            z = a
            while z < t.num_rows and z - a < rg_rows and segs[z] == segs[a]:
                z += 1
            g = t.slice(a, z - a)
            w.write_table(g, row_group_size=rg_rows)
            bounds.append(ist._bounds(g, segs[a], size_col))
            a = z
        w.add_key_value_metadata({"store": "interval", "version": str(ist.STORE_VERSION), "sort": sort, "order": order,
                                  "segments": "open,dyadic", "open": str(ist.OPEN)})
    return ist.write_groups(out, bounds, sort, lambda rows: ist._seg_summary(rows, True))


@pytest.fixture(scope="module")
def src(tmp_path_factory) -> str:
    """Path versions over 6 scans: ~700 paths, each live over a random run of scans split into versions, so the rows
    fall in the open segment and several dyadic ones."""
    rnd = random.Random(7)
    schema = ist.PVL_SCHEMA
    rows = []
    for i in range(700):
        depth = 1 + i % 4
        path = "/".join(f"d{(i >> (2 * k)) % 4}" for k in range(depth - 1)) + ("/" if depth > 1 else "") + f"f{i:04d}"
        a = rnd.randrange(len(STAMPS))
        b = rnd.randrange(a, len(STAMPS))
        cuts = sorted(rnd.sample(range(a + 1, b + 1), rnd.randrange(0, b - a + 1)))
        for lo, hi in zip([a, *cuts], [*cuts, b + 1]):
            vt = sn.OPEN if hi == len(STAMPS) else STAMPS[hi]
            row = {}
            for f in schema:
                if f.name == "depth":
                    row[f.name] = depth
                elif f.name == "path":
                    row[f.name] = path
                elif f.name == "vf":
                    row[f.name] = STAMPS[lo]
                elif f.name == "vt":
                    row[f.name] = vt
                elif f.name == "kind":
                    row[f.name] = rnd.choice("fd")
                elif f.name == "us":
                    row[f.name] = rnd.choice(["", "ann:5", "bob:7"])
                elif pa.types.is_floating(f.type):
                    row[f.name] = rnd.random() * 1e6
                else:
                    row[f.name] = rnd.randrange(0, 1 << rnd.choice([3, 20, 40 if f.type.bit_width > 32 else 30]))
            rows.append(row)
    p = tmp_path_factory.mktemp("pvl") / "r0000.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), p)
    return f"read_parquet('{p}')"


def test_cut_plan_splits_on_group_boundaries():
    """Groups never cross a segment (a segment's last is short); parts are contiguous runs of whole groups of about
    equal rows, one starting inside a segment."""
    assert ist.cut_plan([(0, 10), (5, 7)], 4, 3) == [
        [(0, 0, 4), (0, 4, 4)],
        [(0, 8, 2), (5, 10, 4)],
        [(5, 14, 3)],
    ]
    assert ist.cut_plan([(0, 3)], 4, 8) == [[(0, 0, 3)]]
    assert ist.cut_plan([], 4, 8) == [[]]


@pytest.mark.parametrize("sort", ["path", "bysize"])
def test_parallel_cut_is_the_serial_cut(src, sort, tmp_path):
    """Over a fixture of ~60 groups in several segments, the cut in 1 and 4 processes is the serial writer's file and
    sidecar, byte for byte: a part starts inside a segment, and a part ends on a segment's short last group."""
    con = duckdb.connect()
    schema = ist.served_schema(sort)
    ref = serial_cut(con, src, sort, tmp_path / "ref" / f"{sort}.parquet", schema, rg_rows=RG, stamps=STAMPS, page_rows=PAGE)
    segs = con.execute(f"SELECT {ist.seg_sql(STAMPS)} AS s, count(*) FROM {src} GROUP BY s ORDER BY s").fetchall()
    plan = ist.cut_plan(segs, RG, 4)
    seg_starts, at = set(), 0
    for _, n in segs:
        seg_starts.add(at)
        at += n
    assert len(plan) == 4 and len(segs) >= 4 and ref["groups"] >= 40
    assert [p[0][1] in seg_starts for p in plan[1:]].count(False) >= 1
    assert [p[-1][2] < RG for p in plan[:-1]].count(True) >= 1
    assert plan[-1][-1][2] < RG
    for procs in (1, 4):
        out = tmp_path / f"p{procs}" / f"{sort}.parquet"
        doc = ist.write_served(con, src, sort, out, schema, rg_rows=RG, stamps=STAMPS, page_rows=PAGE, procs=procs, part_rows=0)
        assert doc == ref
        assert out.read_bytes() == (tmp_path / "ref" / f"{sort}.parquet").read_bytes()
        assert out.with_name(f"{sort}.groups.parquet").read_bytes() == (tmp_path / "ref" / f"{sort}.groups.parquet").read_bytes()
        assert sorted(x.name for x in out.parent.iterdir()) == [f"{sort}.groups.parquet", f"{sort}.parquet"]
    assert pq.read_table(out).equals(pq.read_table(tmp_path / "ref" / f"{sort}.parquet"))


def test_concat_is_one_writer(tmp_path):
    """Parts of any sizes, an empty one among them, joined: one writer's bytes; and an unedited footer re-encodes to
    itself."""
    n = 1000
    t = pa.table({"depth": pa.array([i % 7 for i in range(n)], pa.uint8()), "path": [f"a/b/{i:05d}" for i in range(n)],
                  "kind": ["f" if i % 3 else "d" for i in range(n)], "usr": pa.array([None] * n, pa.string()), "vf": list(range(n))})
    groups = [t.slice(o, min(100, n - o)) for o in range(0, n, 100)] + [t.slice(0, 37)]

    def write(p: Path, gs: list) -> Path:
        with ist._served_writer(p, t.schema, 16) as w:
            for g in gs:
                w.write_table(g, row_group_size=100)
            w.add_key_value_metadata({"k": "v"})
        return p

    one = write(tmp_path / "one.parquet", groups)
    assert encode(decode(footer(one))) == footer(one)
    cuts = [0, 3, 4, 4, 9, 11]
    parts = [write(tmp_path / f"p{i}.parquet", groups[a:z]) for i, (a, z) in enumerate(zip(cuts, cuts[1:]))]
    assert concat(parts, tmp_path / "cat.parquet", drop=True) == {"rows": 1037, "groups": 11, "bytes": one.stat().st_size}
    assert (tmp_path / "cat.parquet").read_bytes() == one.read_bytes()
    assert [p.exists() for p in parts] == [False] * 5
