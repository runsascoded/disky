"""The bounded, parallel served-sort cut (`interval_store.write_served`, specs/diff-progressive.md §3b): runs sorted
alone, concatenated or merged, and parts written by processes and joined (`parquet_pages.concat`) are the bytes one
`ORDER BY` streamed into one serial writer leaves, sidecar included."""
from __future__ import annotations

import random
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import interval_compact as ic
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


#: Name stems whose code-point order isn't their case-folded or locale order, so a sort that compared strings other than
#: by code point (DuckDB's run sorts, pyarrow's and Python's merge) would reorder them.
STEMS = ["f", "F", "z~", "é", "日", "a b"]
#: Owners of a path's slices: NULL (sorts first), the empty one, and named ones.
OWNERS = [None, "", "ann", "Bob", "zé"]
#: Range files a fixture generation's rows are split into, by `(depth, path)`.
N_RANGES = 5


def _value(rnd: random.Random, f: pa.Field):
    if f.name == "kind":
        return rnd.choice("fd")
    if f.name == "us":
        return rnd.choice(["", "ann:5", "bob:7"])
    if pa.types.is_string(f.type):
        return rnd.choice([None, "", "x", "Y", "é"]) if f.nullable else rnd.choice(["", "x"])
    if pa.types.is_floating(f.type):
        return rnd.random() * 1e6
    # Sizes from 0 (no size bucket: last in the bucket orders) to 2^40, so many buckets.
    return rnd.randrange(0, 1 << rnd.choice([3, 20, 40 if f.type.bit_width > 32 else 30]))


@pytest.fixture(scope="module")
def gen(tmp_path_factory) -> Path:
    """A generation's range files of every kind (`pvl`, `rd`, `sv`, `svt`, `attribution`) over 6 scans, each kind's rows
    split by `(depth, path)` into `N_RANGES` key-sorted range files of 16-row groups (so a small budget sub-ranges
    them): ~400 paths of mixed-case and non-ASCII names, each live over a random run of scans split into versions (so
    the rows fall in the open segment and several dyadic ones), a sliced path in 1–3 owners' slices."""
    rnd = random.Random(11)
    root = tmp_path_factory.mktemp("gen")
    paths = []
    for i in range(400):
        depth = 1 + i % 4
        paths.append((depth, "/".join(f"{STEMS[(i >> (2 * k)) % 3]}{k}" for k in range(depth - 1)) + ("/" if depth > 1 else "") + f"{rnd.choice(STEMS)}{i:04d}"))
    paths.sort()
    for sub, schema in ist.SUB_SCHEMA.items():
        if sub == "pv":
            continue
        rows = []
        for depth, path in paths:
            for usr in (rnd.sample(OWNERS, rnd.randrange(1, 4)) if "usr" in schema.names else [None]):
                a = rnd.randrange(len(STAMPS))
                b = rnd.randrange(a, len(STAMPS))
                cuts = sorted(rnd.sample(range(a + 1, b + 1), rnd.randrange(0, b - a + 1)))
                for lo, hi in zip([a, *cuts], [*cuts, b + 1]):
                    fixed = {"depth": depth, "path": path, "usr": usr, "vf": STAMPS[lo], "vt": sn.OPEN if hi == len(STAMPS) else STAMPS[hi]}
                    rows.append({f.name: fixed[f.name] if f.name in fixed else _value(rnd, f) for f in schema})
        rows.sort(key=lambda r: (r["depth"], r["path"], r.get("usr") is not None, r.get("usr") or "", r["vf"]))
        (root / sub).mkdir()
        bounds = [len(paths) * j // N_RANGES for j in range(1, N_RANGES)]
        keys = [paths[j] for j in bounds]
        at = 0
        for j in range(N_RANGES):
            z = at
            while z < len(rows) and (j == N_RANGES - 1 or (rows[z]["depth"], rows[z]["path"]) < keys[j]):
                z += 1
            pq.write_table(pa.Table.from_pylist(rows[at:z], schema=schema), root / sub / f"r{j:04d}.parquet", row_group_size=16)
            at = z
        assert at == len(rows)
    return root


@pytest.mark.parametrize("sort", list(ist.SORTS))
def test_bounded_cut_is_one_order_by(gen, sort, tmp_path, monkeypatch):
    """Every served sort, cut from a generation's range files as runs (each range whole, or in key sub-ranges under a
    tiny budget), sorted alone and concatenated (a sort led by `(depth, path)`) or k-way merged per segment (the others),
    then written in 1 or several processes: the file and sidecar one `ORDER BY` of the whole generation streamed into one
    writer leaves, byte for byte."""
    con = duckdb.connect()
    sub = ist.SORTS[sort][0]
    schema = ist.served_schema(sort)
    files = [str(p) for p in sorted((gen / sub).glob("r*.parquet"))]
    assert len(files) == N_RANGES
    ref = serial_cut(con, f"read_parquet('{gen / sub}/r*.parquet')", sort, tmp_path / "ref" / f"{sort}.parquet", schema,
                     rg_rows=RG, stamps=STAMPS, page_rows=PAGE)
    assert len(ref["segments"]) >= 3 and ref["groups"] >= 20
    # A budget of 1 byte cuts a range file at every row-group start; a third of the largest file's estimate at a few.
    third = max(ic.sub_ranges([f], None)[0][2] for f in files) // 3
    assert len(ist.cut_runs(files, None)) == N_RANGES
    assert len(ist.cut_runs(files, 1)) > len(ist.cut_runs(files, third)) > N_RANGES
    # Fan-in 2 merges a segment's runs in passes of pairs: 5 runs in 3, 40-odd sub-ranges in 6 or 7.
    for procs, budget, fan_in in ((1, None, None), (4, None, None), (1, 1, None), (4, third, None), (1, None, 2), (4, 1, 2)):
        if fan_in:
            monkeypatch.setattr(ist, "MERGE_FAN_IN", fan_in)
        out = tmp_path / f"p{procs}-b{budget}-f{fan_in}" / f"{sort}.parquet"
        doc = ist.write_served(con, files, sort, out, schema, rg_rows=RG, stamps=STAMPS, page_rows=PAGE, procs=procs, part_rows=0,
                               budget=budget)
        assert doc == ref
        assert out.read_bytes() == (tmp_path / "ref" / f"{sort}.parquet").read_bytes()
        assert out.with_name(f"{sort}.groups.parquet").read_bytes() == (tmp_path / "ref" / f"{sort}.groups.parquet").read_bytes()
        assert sorted(x.name for x in out.parent.iterdir()) == [f"{sort}.groups.parquet", f"{sort}.parquet"]


def test_merge_keys_are_the_orders():
    """Every sort not led by `(depth, path)` has merge keys, and those sorts' `MERGE_KEYS` are exactly the others."""
    assert sorted(s for s, (_, o, _) in ist.SORTS.items() if not o.startswith("depth, path")) == sorted(ist.MERGE_KEYS)
    assert {s: ist.merge_key(s) for s in ist.MERGE_KEYS} == {
        "bysize": ["__b", "path", "vf"],
        "slices-bytotal": ["__b", "path", "__un", "__u", "vf"],
        "slices-bysize-user": ["__un", "__u", "__b", "path", "vf"],
        ist.ATTRIBUTION: ["path", "vf"],
    }


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
