"""`dt_cloud.static_names`: intervals vs a sequential ingest oracle, append vs rebuild, suffix
shards vs brute force, and the reader's per-bucket first-hit answers vs brute force over every date."""
from __future__ import annotations

import json
import random
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import static_names as sn
from dt_cloud.static_profile_examples import GCS

DATES = ["2026-07-30", "2026-07-31", "2026-08-02", "2026-08-03", "2026-08-04"]
V2_FROM = 3  # scans from here on are store generations (v2); earlier ones v1 indexes
#: The deployments' scan shapes, each test run over each: gcs (a scan a day, owner slices, `listing/<date>/…`), gcs
#: after it moves to scan ids (`gcs-sub`), and cw
#: (a scan every 6 h, ids to the minute, no owners: its v2 sorts carry no `usr` column, `cw-l2/<scan>/index/<gen>/…`).
FLAVORS = {
    "gcs": {"ids": DATES, "layout": "listing/{id}/path-index.parquet", "owners": True, "per_dir": 5},
    # gcs moving to sub-daily scan ids (specs/scan-ids-not-dates.md): date ids, then two scans on one day
    "gcs-sub": {"ids": ["2026-07-30", "2026-07-31", "2026-08-02", "2026-08-03T0001", "2026-08-03T1200"],
                "layout": "listing/{id}/path-index.parquet", "owners": True, "per_dir": 5},
    # the appends (`test_static_append`: the last two scans) are two scans of one day
    "cw": {"ids": ["2026-10-07T1801", "2026-10-08T0001", "2026-10-08T0601", "2026-10-08T1202", "2026-10-08T1801"],
           "layout": "cw-l2/{id}/index/20261009T000000Z/path-index.parquet", "owners": False, "per_dir": 8},  # more names: as many rows without owner slices
}


def scan_ids(scans: dict) -> list[str]:
    return [s["id"] for s in scans["scans"]]


def _universe(rng: random.Random, per_dir: int = 5) -> list[str]:
    names = ["Gof.txt", "x5418y", "nk080", "48.parquet", "48.parquet.crc", "aGOFb", "pio", "a'b", "é5418", "sh", "ab"]
    dirs = ["b1", "b2", "b1/gof", "b1/d", "b2/e5418", "b2/f"]
    paths = list(dirs)
    for d in dirs[2:]:
        for n in rng.sample(names, per_dir):
            paths.append(f"{d}/{n}")
    return paths


def _scan_rows(rng: random.Random, paths: list[str], j: int, owners: bool = True) -> list[dict]:
    """One scan's rows: a few paths missing (absence gaps), values drifting, owner slices (`owners`), and
    (v1) a duplicated unattributed row."""
    rows = []
    for p in paths:
        if rng.random() < 0.15:
            continue
        depth = p.count("/") + 1
        usrs = [None, "alice"] if rng.random() < 0.3 else [None]
        for usr in (usrs if owners else [None]):
            size = rng.choice([10, 10, 10, 20, 0])
            n = rng.choice([1, 1, 2])
            mean = rng.choice([1_700_000_000.4, 1_700_000_000.5, 1_700_000_001.5, 1_700_000_000.2])
            rows.append({"path": p, "depth": depth, "usr": usr, "size": size, "n_files": n, "mtime_mean": mean,
                         "last_read": rng.choice([5, 5, 6]), "kind": "file" if "." in p.rsplit("/", 1)[-1] and depth > 2 else "dir"})
            if j < V2_FROM and usr is None and rng.random() < 0.1:
                rows.append({**rows[-1]})
    return rows


def _write(rows: list[dict], path: Path, v: int, owners: bool = True) -> None:
    """A scan's `path` sort: v2 (a store generation; without owners, no `usr` column, as cw's) or v1."""
    rows = sorted(rows, key=lambda r: (r["depth"], r["path"], r["usr"] or ""))
    if v == 2:
        t = pa.table({
            "path": [r["path"] for r in rows], "usr": [r["usr"] for r in rows], "size": [r["size"] for r in rows],
            "depth": pa.array([r["depth"] for r in rows], pa.int32()), "kind": [r["kind"] for r in rows],
            "n_files": [r["n_files"] for r in rows], "n_children": [1 for _ in rows], "n_desc": [2 for _ in rows],
            "mtime": [7 for _ in rows], "mtime_mean": [r["mtime_mean"] for r in rows], "created": [0 for _ in rows],
            "last_read": pa.array([r["last_read"] for r in rows], pa.int32()),
            "sum_storage_class_id_2": [0 for _ in rows], "sum_storage_class_id_3": [None for _ in rows], "sum_storage_class_id_4": [0 for _ in rows],
        })
        if not owners:
            t = t.drop_columns(["usr"])
    else:
        t = pa.table({
            "path": [r["path"] for r in rows], "depth": [r["depth"] for r in rows], "usr": [r["usr"] for r in rows],
            "b": [r["size"] for r in rows], "o": [r["n_files"] for r in rows],
            "wts": [r["mtime_mean"] * r["size"] for r in rows], "wb": [r["size"] for r in rows],
            "c2": [0 for _ in rows], "c3": [0 for _ in rows], "c4": [0 for _ in rows], "a": pa.array([r["last_read"] for r in rows], pa.int32()),
        })
    pq.write_table(t, path, row_group_size=7)


def _merged(rows: list[dict], v: int) -> dict:
    """The per-key merge (`static_names.MERGED`), values compared with the stamp rounded."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["depth"], r["path"], r["usr"] or ""), []).append(r)
    out = {}
    for k, rs in groups.items():
        if v == 2:
            vals = [("file" if r["kind"] == "file" else "dir", r["size"], r["n_files"], 1, 2, 7, r["mtime_mean"], r["size"], r["last_read"], 0, 0, 0) for r in rs]
        else:
            vals = [("dir", r["size"], r["n_files"], -1, -1, -1, (r["mtime_mean"] * r["size"] / r["size"]) if r["size"] > 0 else 0.0, r["size"], r["last_read"], 0, 0, 0) for r in rs]
        if len(vals) == 1:
            mean = vals[0][6]
        else:
            mean = sum(x[6] * x[7] for x in vals) / max(sum(x[7] for x in vals), 1)
        out[k] = (vals[0][0], sum(x[1] for x in vals), sum(x[2] for x in vals), max(x[3] for x in vals), max(x[4] for x in vals),
                  max(x[5] for x in vals), float(round(mean)), sum(x[7] for x in vals), max(x[8] for x in vals), 0, 0, 0)
    return out


def _oracle(scans: list[tuple[int, dict]]) -> list[tuple]:
    """Sequential ingest: open versions vs each scan, closing gone/changed keys and opening new/changed ones."""
    open_: dict[tuple, tuple[int, tuple]] = {}
    done: list[tuple] = []
    for ts, rows in scans:
        for k in list(open_):
            if k not in rows or rows[k] != open_[k][1]:
                vf, vals = open_.pop(k)
                done.append((*k, vf, ts, *vals))
        for k, vals in rows.items():
            if k not in open_:
                open_[k] = (ts, vals)
    done += [(*k, vf, sn.OPEN, *vals) for k, (vf, vals) in open_.items()]
    return sorted(done)


@pytest.fixture(scope="module", params=sorted(FLAVORS))
def fixture(request, tmp_path_factory):
    flavor = FLAVORS[request.param]
    root = tmp_path_factory.mktemp(f"sn-{request.param}")
    rng = random.Random(7)
    paths = _universe(rng, flavor["per_dir"])
    scans, merged = [], []
    for j, d in enumerate(flavor["ids"]):
        v = 2 if j >= V2_FROM else 1
        rows = _scan_rows(rng, paths, j, flavor["owners"])
        key = flavor["layout"].format(id=d)
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        _write(rows, root / key, v, flavor["owners"])
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
        merged.append((sn.scan_epoch(d), _merged(rows, v)))
    return root, {"bucket": "b", "scans": scans}, merged


def _read(path: Path) -> list[tuple]:
    t = pq.read_table(path)
    return [tuple(r.values()) for r in t.to_pylist()]


def _build(root: Path, scans: dict, ranges: dict, out: Path) -> list[tuple]:
    rows = []
    for r in ranges["ranges"]:
        sn.build_range(scans, ranges, r["i"], out, mount=str(root), threads=2, mem="1GB", tmp=out / "tmp")
        rows += _read(out / "intervals" / f"r{r['i']:04d}.parquet")
    return rows


@pytest.mark.parametrize("k", [1, 4])
def test_intervals_equal_sequential_ingest(fixture, tmp_path, k):
    root, scans, merged = fixture
    ranges = sn.plan_ranges(scans, k, str(root))
    rows = _build(root, scans, ranges, tmp_path)
    assert rows == sorted(rows)
    assert rows == _oracle(merged)
    # every range's file is sorted and ranges are disjoint, ordered
    assert len({(r[0], r[1], r[2], r[3]) for r in rows}) == len(rows)


def test_append_equals_rebuild(fixture, tmp_path):
    root, scans, merged = fixture
    ranges = sn.plan_ranges(scans, 3, str(root))
    head = {"bucket": "b", "scans": scans["scans"][:-1]}
    full = tmp_path / "full"
    _build(root, scans, ranges, full)
    prev = tmp_path / "prev"
    _build(root, head, ranges, prev)
    app = tmp_path / "app"
    for r in ranges["ranges"]:
        name = f"r{r['i']:04d}"
        doc = sn.append_range(prev / "intervals" / f"{name}.parquet", scans["scans"][-1], ranges, r["i"], app,
                              bucket="b", mount=str(root), threads=2, mem="1GB", tmp=app / "tmp")
        assert (app / "intervals" / f"{name}.parquet").read_bytes() == (full / "intervals" / f"{name}.parquet").read_bytes()
        assert (app / "hist" / f"{name}.parquet").read_bytes() == (full / "hist" / f"{name}.parquet").read_bytes()
        full_doc = json.loads((full / "digest" / f"{name}.json").read_text())
        assert doc["digests"] == full_doc["digests"]
        delta = pq.read_table(app / "delta" / scan_ids(scans)[-1] / f"{name}.parquet").to_pylist()
        D = sn.scan_epoch(scan_ids(scans)[-1])
        assert sorted((d["op"], d["vf"] == D, d["vt"] == D) for d in delta) == sorted(
            [(1, True, False)] * doc["opened"] + [(-1, False, True)] * doc["closed"])


def test_digests_match_oracle(fixture, tmp_path):
    root, scans, merged = fixture
    ranges = sn.plan_ranges(scans, 2, str(root))
    _build(root, scans, ranges, tmp_path)
    import duckdb

    con = duckdb.connect()
    opened: dict[int, int] = {}
    closed: dict[int, int] = {}
    for depth, path, usr, vf, vt, _kind, size, n_files, *_ in _oracle(merged):
        opened[vf] = opened.get(vf, 0) + 1
        if vt != sn.OPEN:
            closed[vt] = closed.get(vt, 0) + 1
    got_open: dict[int, int] = {}
    got_close: dict[int, int] = {}
    for f in sorted((tmp_path / "digest").glob("*.json")):
        for ts, kinds in json.loads(f.read_text())["digests"].items():
            if "opened" in kinds:
                got_open[int(ts)] = got_open.get(int(ts), 0) + kinds["opened"][0]
            if "closed" in kinds:
                got_close[int(ts)] = got_close.get(int(ts), 0) + kinds["closed"][0]
    assert (got_open, got_close) == (opened, closed)
    # the digest string hashes as the md5's first 8 bytes, little-endian
    import hashlib

    s = "2|b1/gof|alice|1785369600|10|1"
    assert con.execute(f"SELECT md5_number_upper({sn.q(s)})").fetchone()[0] == int.from_bytes(hashlib.md5(s.encode()).digest()[:8], "little")


def _brute_answer(oracle: list[tuple], term: str, date: str) -> dict:
    D = sn.scan_epoch(date)
    totals: dict[str, list[int]] = {}
    for depth, path, usr, vf, vt, _kind, size, n_files, *_ in oracle:
        if not (vf <= D < vt) or depth < 1:
            continue
        name = path.rsplit("/", 1)[-1].lower()
        parent = path.rsplit("/", 1)[0].lower() if "/" in path else ""
        if term in name and term not in parent:
            b = totals.setdefault(path.split("/", 1)[0], [0, 0])
            b[0] += size
            b[1] += n_files
    return dict(sorted(totals.items()))


def test_shards_and_reader(fixture, tmp_path):
    root, scans, merged = fixture
    ranges = sn.plan_ranges(scans, 3, str(root))
    build = tmp_path / "build"
    _build(root, scans, ranges, build)
    plan = sn.plan_shards(sorted((build / "hist").glob("*.parquet")), target_rows=40, tasks=3)
    assert [s["lo"] for s in plan["shards"]] == sorted(s["lo"] for s in plan["shards"])
    mapped = tmp_path / "map"
    con = sn.connect(2, "1GB", tmp_path / "tmp")
    for r in ranges["ranges"]:
        sn.map_range(str(build / "intervals" / f"r{r['i']:04d}.parquet"), plan, r["i"], mapped, con)
    out = tmp_path / "out"
    for t in range(len(plan["tasks"])):
        files = sorted(str(f) for f in (mapped / "sxmap" / f"g{t:03d}").glob("*.parquet"))
        sn.build_shards(files, plan, t, out, threads=2, mem="1GB", tmp=tmp_path / "tmp")
    # every suffix row, by brute force over the oracle's versions
    expected = []
    for depth, path, usr, vf, vt, _kind, size, n_files, *_ in _oracle(merged):
        name = path.rsplit("/", 1)[-1].lower()
        if depth >= 1:
            expected += [(name[p:], depth, path, usr, vf * 1000, vt * 1000, size, n_files) for p in range(len(name) - 2)]
    got = []
    for f in sorted((out / "sx").glob("*.parquet")):
        for r in pq.read_table(f).to_pylist():
            got.append((r["s"], r["depth"], r["path"], r["usr"], sn._ms(r["vf"]), sn._ms(r["vt"]), r["size"], r["n_files"]))
    assert got == sorted(expected, key=lambda r: (r[0].encode(), r[2].encode(), r[3].encode(), r[4]))
    side = pa.concat_tables([pq.read_table(f) for f in sorted((out / "sidecar").glob("*.parquet"))])
    assert sum(side.column("rows").to_pylist()) == len(expected)

    def fetch(file: str, lo: int, hi: int) -> bytes:
        with open(out / file, "rb") as fh:
            fh.seek(lo)
            return fh.read(hi - lo)

    reader = sn.Reader(fetch, lambda f: (out / f).stat().st_size, side)
    oracle = _oracle(merged)
    for term in ["gof", "5418", "nk080", "48.parquet", "48.parquet.crc", "pio", "a'b", "é54", "zzz", "par"]:
        body = reader.answer(term, scan_ids(scans))
        assert body["answers"] == {d: _brute_answer(oracle, term, d) for d in scan_ids(scans)}, term


def _coalesced_oracle(oracle: list[tuple]) -> list[tuple]:
    """Adjacent versions of a key (`vt` = the next `vf`) with equal `size` and `n_files` merged."""
    out: list[list] = []
    for depth, path, usr, vf, vt, _kind, size, n_files, *_ in sorted(oracle):
        last = out[-1] if out else None
        if last and last[:3] == [depth, path, usr] and last[4] == vf and last[5:] == [size, n_files]:
            last[4] = vt
        else:
            out.append([depth, path, usr, vf, vt, size, n_files])
    return [tuple(r) for r in out]


def test_coalesce_equals_oracle(fixture, tmp_path):
    root, scans, merged = fixture
    ranges = sn.plan_ranges(scans, 3, str(root))
    build = tmp_path / "build"
    rows = _build(root, scans, ranges, build)
    con = sn.connect(2, "1GB", tmp_path / "tmp")
    got = []
    terms = ["gof", "5418", "aaa"]
    stats = []
    for r in ranges["ranges"]:
        name = f"r{r['i']:04d}"
        stats.append(sn.coalesce_range(str(build / "intervals" / f"{name}.parquet"), r["i"], build, con, terms))
        got += _read(build / "cintervals" / f"{name}.parquet")
    expected = _coalesced_oracle(_oracle(merged))
    assert got == expected
    assert len(expected) < len(rows)
    assert sum(s["versions"][0] for s in stats) == len(rows)
    assert sum(s["versions"][1] for s in stats) == len(expected)

    def term_rows(versions, t):
        return sum(sum(1 for p in range(len(n) - len(t) + 1) if n[p:p + len(t)] == t)
                   for n in (v[1].rsplit("/", 1)[-1].lower() for v in versions if v[0] >= 1))

    full = [(r[0], r[1]) for r in rows]
    co = [(r[0], r[1]) for r in expected]
    for t in terms:
        assert [sum(s["terms"][t][k] for s in stats) for k in (0, 1)] == [term_rows(full, t), term_rows(co, t)], t
    assert [sum(s["suffix_rows"][k] for s in stats) for k in (0, 1)] == [
        sum(max(len(p.rsplit("/", 1)[-1]) - 2, 0) for d, p in vs if d >= 1) for vs in (full, co)]


def test_coalesced_from_scans_equals_two_step(fixture, tmp_path):
    """Runs keyed on `size`/`n_files` alone, straight from the scans, are the coalesced versions: the
    `coalesced` build's files are byte-identical to `coalesce_range` over the full intervals, and equal to
    keying on every value with the others carried (`first`) then projected."""
    from pyrmts.intervals import islands_sql, long_sql, stamped_sql

    root, scans, merged = fixture
    ranges = sn.plan_ranges(scans, 3, str(root))
    two, one = tmp_path / "two", tmp_path / "one"
    _build(root, scans, ranges, two)
    con = sn.connect(2, "1GB", tmp_path / "tmp")
    rest = [c for c in sn.VALUE_COLS if c not in sn.ANSWER_COLS]
    cols = ", ".join(sn.CINTERVAL_SCHEMA.names)
    for r in ranges["ranges"]:
        name = f"r{r['i']:04d}"
        sn.coalesce_range(str(two / "intervals" / f"{name}.parquet"), r["i"], two, con)
        doc = sn.build_range(scans, ranges, r["i"], one, mount=str(root), threads=2, mem="1GB", tmp=one / "tmp", coalesced=True)
        assert (one / "cintervals" / f"{name}.parquet").read_bytes() == (two / "cintervals" / f"{name}.parquet").read_bytes()
        assert (one / "chist" / f"{name}.parquet").read_bytes() == (two / "chist" / f"{name}.parquet").read_bytes()
        assert sorted(p.name for p in one.iterdir()) == ["chist", "cintervals", "tmp"]
        srcs = [sn.scan_sql(con, str(root / s["src"]), sn.range_preds(r), s["version"]) for s in scans["scans"]]
        state = [*sn.ANSWER_COLS, *rest]
        runs = islands_sql(long_sql(srcs), sn.KEY_COLS, state, carried={c: "first" for c in rest})
        carried = con.execute(f"SELECT {cols} FROM ({stamped_sql(runs, sn.KEY_COLS, state, [s['ts'] for s in scans['scans']], sn.OPEN)}) "
                              f"ORDER BY depth, path, usr, vf").fetchall()
        assert [tuple(x) for x in carried] == _read(one / "cintervals" / f"{name}.parquet")
        assert doc["rows"] == len(carried)
        assert doc["suffix_rows"] == json.loads((two / "cstats" / f"{name}.json").read_text())["suffix_rows"][1]


def test_coalesce_append_equals_rebuild(fixture, tmp_path):
    root, scans, merged = fixture
    ranges = sn.plan_ranges(scans, 3, str(root))
    head = {"bucket": "b", "scans": scans["scans"][:-1]}
    full, prev, app = tmp_path / "full", tmp_path / "prev", tmp_path / "app"
    _build(root, scans, ranges, full)
    _build(root, head, ranges, prev)
    con = sn.connect(2, "1GB", tmp_path / "tmp")
    last = scans["scans"][-1]
    D = last["ts"]
    for r in ranges["ranges"]:
        name = f"r{r['i']:04d}"
        sn.coalesce_range(str(full / "intervals" / f"{name}.parquet"), r["i"], full, con)
        sn.coalesce_range(str(prev / "intervals" / f"{name}.parquet"), r["i"], prev, con)
        sn.append_range(prev / "intervals" / f"{name}.parquet", last, ranges, r["i"], app,
                        bucket="b", mount=str(root), threads=2, mem="1GB", tmp=app / "tmp")
        doc = sn.coalesce_append(prev / "cintervals" / f"{name}.parquet", app / "delta" / last["id"] / f"{name}.parquet", last, app, r["i"], con)
        assert (app / "cintervals" / f"{name}.parquet").read_bytes() == (full / "cintervals" / f"{name}.parquet").read_bytes()
        assert (app / "chist" / f"{name}.parquet").read_bytes() == (full / "chist" / f"{name}.parquet").read_bytes()
        before, after = set(_read(prev / "cintervals" / f"{name}.parquet")), set(_read(full / "cintervals" / f"{name}.parquet"))
        delta = pq.read_table(app / "cdelta" / last["id"] / f"{name}.parquet").to_pylist()
        opened = sorted(tuple(d[k] for k in sn.CINTERVAL_SCHEMA.names) for d in delta if d["op"] == 1)
        closed = sorted(tuple(d[k] for k in sn.CINTERVAL_SCHEMA.names) for d in delta if d["op"] == -1)
        assert opened == sorted(v for v in after if v[3] == D)
        assert closed == sorted(v for v in after if v[4] == D)
        assert (doc["opened"], doc["closed"]) == (len(opened), len(closed))
        # what changed between the two coalesced builds is exactly the delta: new versions, and closes of open ones
        assert sorted(after - before) == sorted(opened + closed)
        assert sorted(before - after) == sorted((*c[:4], sn.OPEN, *c[5:]) for c in closed)


def test_coalesced_shards_answer_exactly(fixture, tmp_path):
    """Suffix shards over the coalesced versions give the same answers as over every version."""
    root, scans, merged = fixture
    ranges = sn.plan_ranges(scans, 2, str(root))
    build = tmp_path / "build"
    _build(root, scans, ranges, build)
    con = sn.connect(2, "1GB", tmp_path / "tmp")
    for r in ranges["ranges"]:
        sn.coalesce_range(str(build / "intervals" / f"r{r['i']:04d}.parquet"), r["i"], build, con)
    plan = sn.plan_shards(sorted((build / "chist").glob("*.parquet")), target_rows=40, tasks=2)
    mapped, out = tmp_path / "map", tmp_path / "out"
    for r in ranges["ranges"]:
        sn.map_range(str(build / "cintervals" / f"r{r['i']:04d}.parquet"), plan, r["i"], mapped, con)
    for t in range(len(plan["tasks"])):
        files = sorted(str(f) for f in (mapped / "sxmap" / f"g{t:03d}").glob("*.parquet"))
        sn.build_shards(files, plan, t, out, threads=2, mem="1GB", tmp=tmp_path / "tmp")
    side = pa.concat_tables([pq.read_table(f) for f in sorted((out / "sidecar").glob("*.parquet"))])

    def fetch(file: str, lo: int, hi: int) -> bytes:
        with open(out / file, "rb") as fh:
            fh.seek(lo)
            return fh.read(hi - lo)

    reader = sn.Reader(fetch, lambda f: (out / f).stat().st_size, side)
    oracle = _oracle(merged)
    for term in ["gof", "5418", "nk080", "48.parquet", "pio", "a'b", "é54", "zzz", "par"]:
        assert reader.answer(term, scan_ids(scans))["answers"] == {d: _brute_answer(oracle, term, d) for d in scan_ids(scans)}, term


CW_LAYOUTS = ["cw-l2/{id}/index/{gen}/path-index.parquet"]


def _pins(n: int) -> dict:
    return {"generation": n, "size": n}


def test_pick_scans_gcs_layouts():
    """gcs: a date's newest generation's sort wins over the pre-generation file."""
    objects = [
        ("listing/2026-07-30/path-index.parquet", _pins(1)),
        ("listing/2026-07-31/path-index.parquet", _pins(2)),
        ("listing/2026-07-31/index/20260801T000000Z/path-index.parquet", _pins(3)),
        ("listing/2026-07-31/index/20260731T000000Z/path-index.parquet", _pins(4)),
    ]
    assert sn.pick_scans(objects, GCS.layouts) == [
        {"id": "2026-07-30", "src": "listing/2026-07-30/path-index.parquet", "generation": 1, "size": 1, "ts": sn.scan_epoch("2026-07-30")},
        {"id": "2026-07-31", "src": "listing/2026-07-31/index/20260801T000000Z/path-index.parquet", "generation": 3, "size": 3,
         "ts": sn.scan_epoch("2026-07-31")},
    ]


def test_pick_scans_cw_layout():
    """cw: several scans a day, ids to the minute, in time order; the newest index generation per scan; a dir that is not a
    scan (`over-time-dev`) and keys outside the layout are skipped; `start`/`through` bound the ids."""
    objects = [
        ("cw-l2/2026-10-08T1801/index/20261008T184207Z/path-index.parquet", _pins(1)),
        ("cw-l2/2026-10-09T0001/index/20261009T005124Z/path-index.parquet", _pins(2)),
        ("cw-l2/2026-10-08T0601/index/20260921T145538Z/path-index.parquet", _pins(3)),
        ("cw-l2/2026-10-08T0601/index/20260920T212315Z/path-index.parquet", _pins(4)),
        ("cw-l2/2026-10-08T0601/index/20260921T145538Z/age-index.parquet", _pins(5)),
        ("cw-l2/over-time-dev/index/20261001T000000Z/path-index.parquet", _pins(6)),
        ("listing/2026-10-08/path-index.parquet", _pins(7)),
    ]
    got = sn.pick_scans(objects, CW_LAYOUTS)
    assert [(s["id"], s["src"], s["ts"]) for s in got] == [
        ("2026-10-08T0601", "cw-l2/2026-10-08T0601/index/20260921T145538Z/path-index.parquet", 1791439260),
        ("2026-10-08T1801", "cw-l2/2026-10-08T1801/index/20261008T184207Z/path-index.parquet", 1791482460),
        ("2026-10-09T0001", "cw-l2/2026-10-09T0001/index/20261009T005124Z/path-index.parquet", 1791504060),
    ]
    assert [s["id"] for s in sn.pick_scans(objects, CW_LAYOUTS, start="2026-10-08T1801", through="2026-10-08T2359")] == ["2026-10-08T1801"]


def test_pick_scans_refuses_two_ids_at_one_instant():
    """A date and a minute id at midnight are the same stamp: a version's `vf` could not say which scan opened it."""
    objects = [("x/2026-10-08/p", _pins(1)), ("x/2026-10-08T0000/p", _pins(2))]
    with pytest.raises(ValueError) as e:
        sn.pick_scans(objects, ["x/{id}/p"])
    assert str(e.value) == "scans 2026-10-08 and 2026-10-08T0000: ids out of time order (stamps 1791417600, 1791417600)"


@pytest.mark.parametrize("layout, msg", [
    ("cw-l2/index/path-index.parquet", "layout 'cw-l2/index/path-index.parquet': needs one {id} and at most one {gen}"),
    ("a/{id}/{gen}/{gen}/p", "layout 'a/{id}/{gen}/{gen}/p': needs one {id} and at most one {gen}"),
])
def test_layout_glob_rejects_bad_templates(layout, msg):
    with pytest.raises(ValueError) as e:
        sn.layout_glob(layout)
    assert str(e.value) == msg


def test_layout_glob():
    assert sn.layout_glob(CW_LAYOUTS[0])[0] == "cw-l2/*/index/*/path-index.parquet"


def test_scan_labels_round_trip():
    for sid in ["2026-10-08", "2026-10-08T0601", "2026-10-09T0001"]:
        assert sn.scan_label(sn.scan_epoch(sid)) == sid
    with pytest.raises(ValueError) as e:
        sn.scan_epoch("2026-10-08T06")
    assert str(e.value) == "not a scan id: '2026-10-08T06' (want YYYY-MM-DD or YYYY-MM-DDTHHMM)"
