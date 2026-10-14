"""The hex-run rule end to end (specs/static-hex-runs.md): a generation built under `16,8` over names with long hex ids
(at a name's start, middle and end; mixed case; runs of exactly 15, 16 and 17 digits; digits only; a word glued to a
hash) holds exactly the suffix rows the rule keeps, and every reader — the suffix reader, the catalog (census, cells,
short literals), the drill's roots, base ⊕ runs — answers exactly as brute force under `occurs` on every date. The same
pipeline without a rule answers as plain containment (a generation built before the rule)."""
from __future__ import annotations

import random

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import static_append as sa
from dt_cloud import static_catalog as sc
from dt_cloud import static_names as sn
from dt_cloud import static_roots as sr
from dt_cloud.hex_runs import HexRule, kept_positions, occurs, rule_json

from test_static_names import _coalesced_oracle, _merged, _oracle, _read, _scan_rows, _write

R = HexRule(16, 8)
H64 = "3f9a2b7c1d0e4f5a6b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c"
NAMES = [
    H64, H64[:40] + ".json",                       # a whole hash; one with an extension
    "obj_3f9A2B7c1d0e4f5a6b7c8d9e0f1a2b3cdata.json",  # mixed case, a word glued to the run's end (…2b3cda|ta)
    "x0123456789abcdey", "x0123456789ABCDEFy", "x0123456789abcdef0y",  # 15 / 16 / 17 digits
    "0123456789abcdefcafe.log", "run-cafe0123456789abcdef",  # a run at the start / at the end
    "20261009123456789.ckpt", "cafe.txt", "bed-cafe", "data.json", "gof.txt", "x5418y",
]
DIRS = ["b1", "b2", "b1/a1b2c3d4e5f60718293a4b5c6d7e8f90", "b1/d", "b2/cafe", "b2/f"]
DATES = ["2026-10-07T1801", "2026-10-08T0001", "2026-10-08T0601", "2026-10-08T1202", "2026-10-08T1801"]
LAYOUT = "cw-l2/{id}/index/g/path-index.parquet"
#: Affected (hex throughout, or > 8 leading hex digits) and unaffected literals, inside and outside runs.
TERMS = ["cafe", "1234", "0123", "bcdef", "2b3c", "3f9a", "7c1d", "a1b2", "e8f9", "bed", "2026", "6789", "abcdef0y", "89abcdef0y",
         "data", ".json", "cafe.", "gof", "x01", "ckpt", "run-", "f0y", "c3d4"]
V = 3


@pytest.fixture(scope="module")
def hexfx(tmp_path_factory):
    root = tmp_path_factory.mktemp("hex")
    rng = random.Random(11)
    # `c3d4` lies inside the hex dir's name (opaque) and starts a child's name: a first hit only under the rule
    paths = list(DIRS) + [f"{d}/{n}" for d in DIRS[2:] for n in rng.sample(NAMES, 9)] + [f"{DIRS[2]}/c3d4-notes.txt"]
    scans, merged = [], []
    for j, d in enumerate(DATES):
        v = 2 if j >= 2 else 1
        rows = _scan_rows(rng, paths, j, owners=False)
        key = LAYOUT.format(id=d)
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        _write(rows, root / key, v, owners=False)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
        merged.append((sn.scan_epoch(d), _merged(rows, v)))
    return root, scans, merged


def _gen(root, scans: list[dict], tmp, rule):
    """A generation over `scans` straight to coalesced versions, under `rule`: shards, sidecar, catalog at `V`."""
    doc = {"bucket": "b", "scans": scans, **rule_json(rule)}
    assert sn.gen_rule(doc) == rule
    ranges = sn.plan_ranges(doc, 2, str(root))
    build, mapped, out, cat = tmp / "build", tmp / "map", tmp / "out", tmp / "cat"
    con = sn.connect(2, "1GB", tmp / "tmp")
    for r in ranges["ranges"]:
        sn.build_range(doc, ranges, r["i"], build, mount=str(root), threads=2, mem="1GB", tmp=tmp / "tmp", con=con, coalesced=True, rule=rule)
    plan = sn.plan_shards(sorted((build / "chist").glob("*.parquet")), target_rows=60, tasks=2)
    for r in ranges["ranges"]:
        sn.map_range(str(build / "cintervals" / f"r{r['i']:04d}.parquet"), plan, r["i"], mapped, con, rule)
    rg, sn.SX_RG = sn.SX_RG, 5
    try:
        for t in range(len(plan["tasks"])):
            files = sorted(str(f) for f in (mapped / "sxmap" / f"g{t:03d}").glob("*.parquet"))
            sn.build_shards(files, plan, t, out, threads=2, mem="1GB", tmp=tmp / "tmp")
    finally:
        sn.SX_RG = rg
    nodes = []
    for s in plan["shards"]:
        name = f"s{s['i']:04d}"
        got = sc.census(con, f"read_parquet({sn.q(str(out / 'sx' / f'{name}.parquet'))})", 1).to_pylist()
        nodes += [(x["q"], x["rows"]) for x in got]
        con.execute("CREATE OR REPLACE TABLE mem (q VARCHAR, rows BIGINT)")
        if m := [(x["q"], x["rows"]) for x in got if x["rows"] > V]:
            con.executemany("INSERT INTO mem VALUES (?, ?)", m)
        sc.shard_cells(con, str(out / "sx" / f"{name}.parquet"), "mem", cat / "cells" / f"{name}.parquet", chunk_rows=11, rule=rule)
    for r in ranges["ranges"]:
        sc.range_short(con, str(build / "cintervals" / f"r{r['i']:04d}.parquet"), cat / "short", f"r{r['i']:04d}", rule)
    sc.assemble(con, str(cat / "short/events/*.parquet"), str(cat / "short/vocab/*.parquet"), str(cat / "cells/*.parquet"), cat / "final")
    side = pa.concat_tables([pq.read_table(f) for f in sorted((out / "sidecar").glob("*.parquet"))])
    return {"ranges": ranges, "plan": plan, "build": build, "out": out, "final": cat / "final", "side": side, "con": con, "nodes": nodes}


@pytest.fixture(scope="module", params=["16,8", "off"])
def built(request, hexfx, tmp_path_factory):
    rule = R if request.param == "16,8" else None
    root, scans, merged = hexfx
    return rule, _gen(root, scans, tmp_path_factory.mktemp(f"g-{request.param.replace(',', '-')}"), rule)


def _versions(merged) -> list[tuple]:
    return _coalesced_oracle(_oracle(merged))


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1].lower()


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0].lower() if "/" in path else ""


def _is_root(t: str, path: str, rule) -> bool:
    return occurs(t, _name(path), rule) and not occurs(t, _parent(path), rule)


def _brute(versions, t: str, date: str, rule) -> dict:
    D = sn.scan_epoch(date)
    out: dict[str, list[int]] = {}
    for depth, path, usr, vf, vt, size, n in versions:
        if depth >= 1 and vf <= D < vt and _is_root(t, path, rule):
            b = out.setdefault(path.split("/", 1)[0], [0, 0])
            b[0] += size
            b[1] += n
    return dict(sorted(out.items()))


def _reader(root, side, rule) -> sn.Reader:
    def fetch(file: str, lo: int, hi: int) -> bytes:
        with open(root / file, "rb") as fh:
            fh.seek(lo)
            return fh.read(hi - lo)

    return sn.Reader(fetch, lambda f: (root / f).stat().st_size, side, rule)


def _catalog(d) -> sc.Catalog:
    path = d / "cells.parquet"

    def fetch(lo: int, hi: int) -> bytes:
        with open(path, "rb") as fh:
            fh.seek(lo)
            return fh.read(hi - lo)

    return sc.Catalog(fetch, path.stat().st_size, pq.read_table(d / "index.parquet"))


def _suffix_rows(versions, rule) -> list[tuple]:
    rows = []
    for depth, path, usr, vf, vt, size, n in versions:
        if depth >= 1:
            name = _name(path)
            rows += [(name[p:], depth, path, usr, vf * 1000, vt * 1000, size, n) for p in kept_positions(name, rule)]
    return sorted(rows, key=lambda r: (r[0].encode(), r[2].encode(), r[3].encode(), r[4]))


def test_suffix_rows_are_the_kept_positions(built, hexfx):
    rule, g = built
    want = _suffix_rows(_versions(hexfx[2]), rule)
    got = [(r[0], r[1], r[2], r[3], sn._ms(r[4]), sn._ms(r[5]), r[6], r[7]) for f in sorted((g["out"] / "sx").glob("*.parquet")) for r in _read(f)]
    assert got == want
    hist = {}
    for f in sorted((g["build"] / "chist").glob("*.parquet")):
        for p3, n in _read(f):
            hist[p3] = hist.get(p3, 0) + n
    want_hist = {}
    for r in want:
        want_hist[r[0][:3]] = want_hist.get(r[0][:3], 0) + 1
    assert hist == want_hist
    full = _suffix_rows(_versions(hexfx[2]), None)
    # the rule drops rows here (hash interiors), none without it
    assert (len(want) < len(full)) == (rule is not None)


def test_reader_equals_brute_force(built, hexfx):
    rule, g = built
    versions, dates = _versions(hexfx[2]), [s["id"] for s in hexfx[1]]
    reader = _reader(g["out"], g["side"], rule)
    differs = []
    for t in TERMS:
        assert reader.answer(t, dates)["answers"] == {d: _brute(versions, t, d, rule) for d in dates}, t
        if any(_brute(versions, t, d, rule) != _brute(versions, t, d, None) for d in dates):
            differs.append(t)
    # the literals whose answer the rule changes: those with an occurrence wholly inside a run (`e8f9`: the hex dir's
    # name), or opaque even though it extends past (`89abcdef0y`), and one whose only parent occurrence is opaque (`c3d4`). Not those at a run's start (`a1b2`, `3f9a`, `2026`) or
    # glued past its end (`data`, `abcdef0y`, `f0y`).
    assert differs == (["cafe", "1234", "0123", "bcdef", "2b3c", "7c1d", "e8f9", "6789", "89abcdef0y", "c3d4"] if rule else [])


def test_catalog_equals_brute_force(built, hexfx):
    rule, g = built
    versions, dates = _versions(hexfx[2]), [s["id"] for s in hexfx[1]]
    names = sorted({_name(v[1]) for v in versions if v[0] >= 1})
    # the census: every prefix's range rows, counted over the kept suffix starts
    want_nodes: dict[str, int] = {}
    for v in versions:
        if v[0] >= 1:
            name = _name(v[1])
            for p in kept_positions(name, rule):
                for L in range(3, len(name) - p + 1):
                    want_nodes[name[p:p + L]] = want_nodes.get(name[p:p + L], 0) + 1
    assert sorted(g["nodes"]) == sorted(want_nodes.items())
    cat = _catalog(g["final"])
    shorts = sorted({n[p:p + m] for n in names for m in (1, 2) for p in range(len(n) - m + 1) if occurs(n[p:p + m], n, rule)})
    members = sorted([t for t, n in want_nodes.items() if n > V] + shorts)
    got_members = sorted({r["q"] for r in pq.read_table(g["final"] / "cells.parquet").to_pylist() if r["bucket"] == ""})
    assert got_members == members
    for t in members:
        want = {d: {b: v for b, v in _brute(versions, t, d, rule).items() if v != [0, 0]} for d in dates}
        assert cat.answer(t, dates)["answers"] == want, t


def test_roots_equal_brute_force(built, hexfx):
    rule, g = built
    versions = _versions(hexfx[2])
    con, plan, out = g["con"], g["plan"], g["out"]
    con.execute("DROP TABLE IF EXISTS rt; DROP TABLE IF EXISTS st")
    terms = set()
    for s in plan["shards"]:
        sx = str(out / "sx" / f"s{s['i']:04d}.parquet")
        nodes = [(x["q"], x["rows"]) for x in sc.census(con, f"read_parquet({sn.q(sx)})", 1).to_pylist() if x["rows"] > V]
        terms |= {x for x, _ in nodes}
        con.execute("CREATE OR REPLACE TABLE mem (q VARCHAR, rows BIGINT)")
        if nodes:
            con.executemany("INSERT INTO mem VALUES (?, ?)", nodes)
        for where in sr.chunk_wheres(sx, 7):
            sr.member_roots(con, sr.sx_rows_sql(f"(SELECT * FROM read_parquet({sn.q(sx)}) WHERE {where})"), "mem", "rt", rule=rule)
    for r in g["ranges"]["ranges"]:
        src = str(g["build"] / "cintervals" / f"r{r['i']:04d}.parquet")
        sr.short_roots(con, f"SELECT * FROM read_parquet({sn.q(src)})", "st", pieces=2, rule=rule)
    got = sorted(con.execute("SELECT q, depth, path, usr, vf, vt, size, n_files FROM rt").fetchall())
    assert got == sorted((t, *v) for v in versions if v[0] >= 1 for t in terms if _is_root(t, v[1], rule))
    assert len(got) > 50
    names = {_name(v[1]) for v in versions if v[0] >= 1}
    shorts = {n[p:p + m] for n in names for m in (1, 2) for p in range(len(n) - m + 1)}
    got_short = sorted(con.execute("SELECT * FROM st").fetchall())
    assert got_short == sorted((t, *v) for v in versions if v[0] >= 1 for t in shorts if _is_root(t, v[1], rule))


def test_base_plus_runs_equal_rebuild(hexfx, tmp_path):
    """Two scans appended to a base under the rule: base ⊕ runs hold the rebuild's suffix rows, the tiered reader its
    answers, and the catalog merges to the rebuild's byte for byte; every run's catalog records the rule."""
    root, scans, merged = hexfx
    K = 2
    full = _gen(root, scans, tmp_path / "full", R)
    base = _gen(root, scans[:-K], tmp_path / "base", R)
    con = base["con"]
    shards = sc.BaseShards(str(base["out"]), base["side"])
    ranges = base["ranges"]["ranges"]
    prev = {r["i"]: f"SELECT * FROM read_parquet({sn.q(str(base['build'] / 'cintervals' / f'r{r['i']:04d}.parquet'))})" for r in ranges}
    run_dirs, deltas = [], []
    for scan in scans[-K:]:
        run = tmp_path / "runs" / scan["id"]
        for r in ranges:
            name = f"r{r['i']:04d}"
            sa.append_open(con, prev[r["i"]], scan, r, name, run, bucket="b", mount=str(root), rule=R)
            prev[r["i"]] = f"SELECT * FROM read_parquet({sn.q(str(run / 'copen' / f'{name}.parquet'))})"
        files = sorted(str(f) for f in (run / "cdelta").glob("*.parquet"))
        deltas.append(files)
        sa.delta_shards(con, files, run, target_rows=7, rule=R)
        meta = sa.catalog_delta(con, [base["final"], *(d / "catalog" for d in run_dirs)], shards, deltas, V, run / "catalog",
                                tmp_path / "work" / scan["id"], R)
        assert meta["hex_runs"] == {"min": 16, "tail": 8}
        run_dirs.append(run)
    rows = lambda dirs: sorted(r for d in dirs for f in sorted((d / "sx").glob("*.parquet")) for r in _read(f))  # noqa: E731
    best: dict = {}
    for r in rows([base["out"], *run_dirs]):
        k = (r[0], r[2], r[3], r[4])
        if k not in best or r[5] < best[k][5]:
            best[k] = r
    assert sorted(best.values()) == rows([full["out"]])
    tiered = sa.TieredReader([_reader(base["out"], base["side"], R), *(_reader(d, pq.read_table(d / "sidecar.parquet"), R) for d in run_dirs)], R)
    versions, dates = _versions(merged), [s["id"] for s in scans]
    for t in TERMS:
        assert tiered.answer(t, dates)["answers"] == {d: _brute(versions, t, d, R) for d in dates}, t
        assert tiered.hits(t) == sa.TieredReader([_reader(full["out"], full["side"], R)], R).hits(t), t
    out = tmp_path / "merged-catalog"
    sa.merge_catalogs([base["final"], *(d / "catalog" for d in run_dirs)], out)
    assert (out / "cells.parquet").read_bytes() == (full["final"] / "cells.parquet").read_bytes()
