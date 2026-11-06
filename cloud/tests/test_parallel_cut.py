"""The bounded, parallel served-sort cut (`interval_store.write_served`, specs/diff-progressive.md §3b): runs sorted
alone, concatenated or merged, and parts written by processes and joined (`parquet_pages.concat`) are the bytes one
`ORDER BY` streamed into one serial writer leaves, sidecar included."""
from __future__ import annotations

import json
import random
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import interval_compact as ic
from dt_cloud import interval_level as ilv
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
    fall in the open segment and several dyadic ones. The versions' values draw from their own stream, so the segments
    (what the cut's parts are planned from) don't move with a deployment's `CLASS_COLS`."""
    rnd, values = random.Random(7), random.Random(8)
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
                    row[f.name] = values.choice("fd")
                elif f.name == "us":
                    row[f.name] = values.choice(["", "ann:5", "bob:7"])
                elif pa.types.is_floating(f.type):
                    row[f.name] = values.random() * 1e6
                else:
                    row[f.name] = values.randrange(0, 1 << values.choice([3, 20, 40 if f.type.bit_width > 32 else 30]))
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


@pytest.mark.parametrize("sort", [s for s in ist.SORTS if s != ist.LEVEL])
def test_bounded_cut_is_one_order_by(gen, sort, tmp_path):
    """Every served sort, cut from a generation's range files as runs (each range whole, or in key sub-ranges under a
    tiny budget), sorted alone and concatenated (a sort led by `(depth, path)`) or cut per segment in key-range pieces
    sorted alone (the others: one piece a segment, or many under a tiny piece share — splitters between rows equal on
    the key's leading columns, spans cut inside spool groups), then written in 1 or several processes: the file and
    sidecar one `ORDER BY` of the whole generation streamed into one writer leaves, byte for byte."""
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
    # `(procs, budget, piece rows, spool rows)`: a piece share of 7 or 50 rows cuts a segment in many pieces (fences
    # every row, or every few), 8-row spool groups put a piece's span ends inside groups.
    for procs, budget, piece, spool in ((1, None, None, None), (4, None, None, None), (1, 1, None, None), (4, third, None, None),
                                        (4, None, 7, 8), (1, third, 50, 8), (4, 1, 50, None)):
        out = tmp_path / f"p{procs}-b{budget}-r{piece}-s{spool}" / f"{sort}.parquet"
        doc = ist.write_served(con, files, sort, out, schema, rg_rows=RG, stamps=STAMPS, page_rows=PAGE, procs=procs, part_rows=0,
                               budget=budget, piece_rows=piece, **({"spool_rows": spool} if spool else {}))
        assert doc == ref
        assert out.read_bytes() == (tmp_path / "ref" / f"{sort}.parquet").read_bytes()
        assert out.with_name(f"{sort}.groups.parquet").read_bytes() == (tmp_path / "ref" / f"{sort}.groups.parquet").read_bytes()
        assert sorted(x.name for x in out.parent.iterdir()) == [f"{sort}.groups.parquet", f"{sort}.parquet"]


@pytest.mark.parametrize("seed", range(40))
def test_piece_plan_is_exact(seed):
    """Random runs of sorted keys, fenced every `stride` rows: unique keys at `write_served`'s stride, or few distinct
    keys at any stride (equal keys repeat within and across runs and straddle fences). The pieces tile the key space in
    order, a run's rows of a piece lie in its span, and a piece holds ≤ `share` rows — always over unique keys; over
    repeated ones unless no fence key lies strictly between its `lo` and `hi` (its bound already over `share`)."""
    rnd = random.Random(seed)
    unique = seed % 2 == 1
    n_runs = rnd.randrange(1, 6)
    if unique:
        pool = rnd.sample(range(10_000), rnd.randrange(1, 400))
        cuts = sorted(rnd.choices(range(len(pool) + 1), k=n_runs - 1))
        runs = [sorted((k // 100, f"{k % 100:02d}") for k in pool[a:z]) for a, z in zip([0, *cuts], [*cuts, len(pool)])]
    else:
        runs = [sorted((rnd.randrange(4), rnd.choice("ab")) for _ in range(rnd.randrange(0, 60))) for _ in range(n_runs)]
    share = rnd.randrange(1, 40)
    stride = max(1, share // (n_runs + 1)) if unique else rnd.randrange(1, 9)
    # Each run's segment starts some rows into its spool.
    offs = [rnd.randrange(0, 5) for _ in runs]
    spec = [(o, o + len(r), [(r[i], o + i) for i in range(0, len(r), stride)]) for o, r in zip(offs, runs)]
    cands = sorted({k for _, _, f in spec for k, _ in f})
    pieces = ist.piece_plan(spec, share)
    assert pieces[0][0] is None and pieces[-1][1] is None
    assert [p[1] for p in pieces[:-1]] == [p[0] for p in pieces[1:]]
    total = 0
    for lo, hi, spans in pieces:
        got = []
        for j, (o, r) in enumerate(zip(offs, runs)):
            rows = [o + i for i, k in enumerate(r) if (lo is None or k >= lo) and (hi is None or k < hi)]
            if rows:
                (_, a, z), = [s for s in spans if s[0] == j]
                assert a <= rows[0] and rows[-1] < z
            got += [r[i - o] for i in rows]
        total += len(got)
        forced = not any((lo is None or lo < c) and (hi is None or c < hi) for c in cands)
        assert len(got) <= share or (forced and not unique)
    assert total == sum(map(len, runs))


def test_merge_keys_are_the_orders():
    """Every sort not led by the range key `(depth, path)` has merge keys, and those sorts' `MERGE_KEYS` are exactly the
    others: the level sort's `(depth, path)` are its rows' parents', so it merges too."""
    assert sorted([*(s for s, (_, o, _) in ist.SORTS.items() if not o.startswith("depth, path")), ist.LEVEL]) == sorted(ist.MERGE_KEYS)
    assert {s: ist.merge_key(s) for s in ist.MERGE_KEYS} == {
        "bysize": ["__b", "path", "vf"],
        "slices-bytotal": ["__b", "path", "__un", "__u", "vf"],
        "slices-bysize-user": ["__un", "__u", "__b", "path", "vf"],
        ist.ATTRIBUTION: ["path", "vf"],
        ist.LEVEL: ["depth", "path", "ts", "name"],
    }


def test_the_level_cut_is_the_same_bytes_at_any_parallelism(gen, tmp_path):
    """The level sort (`interval_level`), segmented by the generation's key ranges over its parent key, cut as runs (each
    range whole, or in key sub-ranges), per segment in key-range pieces (one, or many under a tiny piece share), and
    written by 1 or several processes: the same bytes every time, and its rows the transitions of every path version, in
    key order."""
    con = duckdb.connect()
    files = []
    (tmp_path / "pvl").mkdir()
    for f in sorted((gen / "pvl").glob("r*.parquet")):
        dst = tmp_path / "pvl" / f.name
        con.execute(f"""COPY (SELECT *, (hash(path, vf) % 1000)::BIGINT AS churn_cum,
            CASE WHEN vt <> {sn.OPEN} THEN (hash(path, vt) % 1000)::BIGINT END AS churn_end FROM read_parquet('{f}')) TO '{dst}' (FORMAT parquet)""")
        files.append(str(dst))
    bounds = [[0, ""], *(list(con.execute(f"SELECT depth, path FROM read_parquet('{f}') LIMIT 1").fetchone()) for f in files[1:])]
    schema = ist.served_schema(ist.LEVEL)
    third = max(ic.sub_ranges([f], None)[0][2] for f in files) // 3
    ref = None
    for procs, budget, piece, spool in ((1, None, None, None), (4, None, None, None), (1, 1, None, None), (4, third, None, None),
                                        (4, None, 7, 8), (1, third, 50, 8), (4, 1, 50, None)):
        out = tmp_path / f"p{procs}-b{budget}-r{piece}-s{spool}" / "level.parquet"
        doc = ist.write_served(con, files, ist.LEVEL, out, schema, rg_rows=RG, page_rows=PAGE, procs=procs, part_rows=0, budget=budget, key_bounds=bounds,
                               piece_rows=piece, **({"spool_rows": spool} if spool else {}))
        got = (doc, out.read_bytes(), out.with_name("level.groups.parquet").read_bytes())
        ref = ref or got
        assert got == ref
    assert len(ref[0]["segments"]) >= 2 or ref[0]["groups"] >= 20
    want = con.execute(f"SELECT * FROM ({ilv.rows_sql(f"read_parquet('{tmp_path}/pvl/r*.parquet')")}) ORDER BY depth, path, ts, name").fetchall()
    assert want and con.execute(f"SELECT * FROM read_parquet('{out}')").fetchall() == want


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


def test_empty_sort_sidecar_bytes(tmp_path) -> None:
    """A sort with no rows: `write_groups` writes one empty row group, the sidecar a fresh build has always had. The
    reference is that writer's whole sequence over the same empty sort, so a change to the empty case changes bytes."""
    from disk_tree.find.groups import schema_json

    schema = ist.PVL_SCHEMA
    out = tmp_path / "reads.parquet"
    with ist._served_writer(out, schema, None) as w:
        w.add_key_value_metadata({"store": "interval", "version": str(ist.STORE_VERSION), "sort": "reads"})
    doc = ist.write_groups(out, [], "reads", lambda rows: ist._seg_summary(rows, True))
    got = tmp_path / "reads.groups.parquet"
    assert (doc["rows"], doc["groups"], pq.read_metadata(got).num_row_groups, pq.read_metadata(got).num_rows) == (0, 0, 1, 0)

    md = pq.read_metadata(out)
    ref = tmp_path / "ref.groups.parquet"
    kv = {"groups_v": "1", "version": str(ist.STORE_VERSION), "schema": json.dumps(schema_json(md)["schema"], separators=(",", ":")), "sort": "reads",
          "rows": "0", "segments": json.dumps({k: v["groups"] for k, v in ist._seg_summary([], True).items()})}
    t = pa.table({c: [] for c in ist.GROUPS_SCHEMA.names}, schema=ist.GROUPS_SCHEMA)
    with pq.ParquetWriter(ref, ist.GROUPS_SCHEMA, compression="zstd", write_statistics=ist.GROUPS_STAT_COLS, store_schema=False) as w:
        w.write_table(t, row_group_size=512)
        w.add_key_value_metadata(kv)
    assert got.read_bytes() == ref.read_bytes()
