"""`dt_cloud.interval_store`: the path versions reconstruct every scan's per-path rows exactly (the
reader's `merge` over a path's rows, folded into one), the read versions every scan's `last_read`, and
the build's per-scan digests say so."""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import interval_store as ist
from dt_cloud import static_names as sn

#: The store's class columns, as its rows name them (the oracles key them `c2`/`c3`/`c4`).
C2, C3, C4 = (ist.CLASS_COLS[k] for k in (2, 3, 4))

DATES = ["2026-07-30", "2026-07-31", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"]
V2_FROM = 3  # scans from here on are store generations (v2); earlier ones v1 indexes


def _universe(rng: random.Random) -> list[str]:
    dirs = ["b1", "b2", "b1/gof", "b1/d", "b2/e", "b2/f"]
    paths = list(dirs)
    for d in dirs[2:]:
        for n in rng.sample(["a.txt", "b.pq", "c", "d.json", "e", "f.bin"], 4):
            paths.append(f"{d}/{n}")
    return paths


def _scan_rows(rng: random.Random, paths: list[str], j: int) -> list[dict]:
    """One scan's rows: paths missing at random, values drifting (mostly not), owner slices on
    directories (some of zero bytes), last-read days that move, and (v1) a duplicated unattributed row."""
    rows = []
    for p in paths:
        if rng.random() < 0.12:
            continue
        depth = p.count("/") + 1
        is_file = "." in p.rsplit("/", 1)[-1]
        # An object is one row (attributed to its directory's owner, `viz.write_path_index`); a directory
        # is one row per owner slice.
        users = rng.choice([[None], ["alice"]] if is_file else [[None], [None], [None], ["alice"], [None, "alice"], ["alice", "bob"]])
        for usr in users:
            size = rng.choice([10, 10, 10, 10, 20, 0])
            rows.append({"path": p, "depth": depth, "usr": usr, "size": size, "n_files": rng.choice([1, 1, 1, 2]),
                         "mtime_mean": rng.choice([1_700_000_000.25, 1_700_000_000.25, 1_700_000_100.75]),
                         "last_read": rng.choice([None, 5, 5, 6]), "kind": "file" if is_file else "dir"})
            if j < V2_FROM and usr is None and rng.random() < 0.1:
                rows.append({**rows[-1]})
    return rows


def _write(rows: list[dict], path: Path, v: int) -> None:
    rows = sorted(rows, key=lambda r: (r["depth"], r["path"], r["usr"] or ""))
    if v == 2:
        t = pa.table({
            "path": [r["path"] for r in rows], "usr": [r["usr"] for r in rows], "size": [r["size"] for r in rows],
            "depth": pa.array([r["depth"] for r in rows], pa.int32()), "kind": [r["kind"] for r in rows],
            "n_files": [r["n_files"] for r in rows], "n_children": [1 for _ in rows], "n_desc": [2 for _ in rows],
            "mtime": [7 for _ in rows], "mtime_mean": [r["mtime_mean"] for r in rows], "created": [0 for _ in rows],
            "last_read": pa.array([r["last_read"] for r in rows], pa.int32()),
            "sum_storage_class_id_2": [0 for _ in rows], "sum_storage_class_id_3": [None for _ in rows], "sum_storage_class_id_4": [r["size"] // 10 for r in rows],
        })
    else:
        t = pa.table({
            "path": [r["path"] for r in rows], "depth": [r["depth"] for r in rows], "usr": [r["usr"] for r in rows],
            "b": [r["size"] for r in rows], "o": [r["n_files"] for r in rows],
            "wts": [r["mtime_mean"] * r["size"] for r in rows], "wb": [r["size"] for r in rows],
            "c2": [0 for _ in rows], "c3": [0 for _ in rows], "c4": [0 for _ in rows], "a": pa.array([r["last_read"] for r in rows], pa.int32()),
        })
    pq.write_table(t, path, row_group_size=7)


def _oracle(rows: list[dict], v: int) -> dict[tuple[int, str], dict]:
    """The reader's per-path aggregate (`view.ts` `merge` over a path's rows, `index.ts` `toRowV1/V2`)."""
    out: dict[tuple[int, str], dict] = {}
    for r in rows:
        a = out.setdefault((r["depth"], r["path"]), {"dir": False, "size": 0, "n_files": 0, "n_children": -1, "n_desc": -1, "mtime": -1,
                                                     "wts": 0.0, "wb": 0, "last_read": -1, "c2": 0, "c3": 0, "c4": 0, "ub": {}, "rows": 0})
        a["rows"] += 1
        a["dir"] |= v == 1 or r["kind"] != "file"
        a["size"] += r["size"]
        a["n_files"] += r["n_files"]
        if v == 2:
            a["n_children"], a["n_desc"], a["mtime"] = 1, 2, 7
            a["c4"] += r["size"] // 10
            mw, wt = r["size"], r["mtime_mean"] * r["size"]
        else:
            wb = r["size"]
            mw, wt = wb, ((r["mtime_mean"] * r["size"]) / wb) * wb if wb > 0 else 0.0
        a["wts"] += wt
        a["wb"] += mw
        if r["last_read"] is not None:
            a["last_read"] = max(a["last_read"], r["last_read"])
        if r["usr"]:
            a["ub"][r["usr"]] = a["ub"].get(r["usr"], 0) + r["size"]
    res = {}
    for k, a in out.items():
        named = sorted(a["ub"].items())
        if not named:
            us = ""
        elif a["rows"] == 1:
            us = named[0][0]
        else:
            us = json.dumps(named, separators=(",", ":"))
        res[k] = {"kind": "dir" if a["dir"] else "file", "size": a["size"], "n_files": a["n_files"], "n_children": a["n_children"],
                  "n_desc": a["n_desc"], "mtime": a["mtime"], "dr": round(a["wts"] / a["wb"]) if a["wb"] > 0 else 0, "wb": a["wb"],
                  "c2": a["c2"], "c3": a["c3"], "c4": a["c4"], "us": us, "last_read": a["last_read"]}
    return res


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("ist")
    rng = random.Random(11)
    paths = _universe(rng)
    scans, oracle = [], []
    for j, d in enumerate(DATES):
        v = 2 if j >= V2_FROM else 1
        rows = _scan_rows(rng, paths, j)
        key = f"listing/{d}/path-index.parquet"
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        _write(rows, root / key, v)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
        oracle.append(_oracle(rows, v))
    scans_doc = {"bucket": "b", "scans": scans}
    ranges = {"k": 3, "ranges": [{"i": 0, "lo": [0, ""], "hi": [2, "b1/gof"]}, {"i": 1, "lo": [2, "b1/gof"], "hi": [3, ""]},
                                 {"i": 2, "lo": [3, ""], "hi": None}]}
    out = root / "out"
    con = duckdb.connect(str(root / "b.duckdb"))
    docs = [ist.build_range(scans_doc, ranges, i, out, con, mount=str(root)) for i in range(3)]
    for i in range(3):
        ist.fold_range(str(out), i, out, con)
    for i in range(3):
        ist.build_slices_range(scans_doc, ranges, i, out, con, mount=str(root))
    for i in range(3):
        ist.slice_totals_range(str(out), i, out, con)
    return out, scans_doc, oracle, docs


def _live(out: Path, sub: str, ts: int) -> list[dict]:
    t = pa.concat_tables([pq.read_table(p) for p in sorted((out / sub).glob("r*.parquet"))])
    return [r for r in t.to_pylist() if r["vf"] <= ts < r["vt"]]


def _slice_oracle(root: Path, scan: dict) -> dict[tuple[int, str, str | None], dict]:
    """A scan's rows summed per `(depth, path, usr)` as the reader's `merge` would (`_oracle` per slice)."""
    t = pq.read_table(root / scan["src"]).to_pylist()
    v = scan["version"]
    rows = [{"path": r["path"], "depth": r["depth"], "usr": r["usr"], "size": r["size"] if v == 2 else r["b"],
             "n_files": r["n_files"] if v == 2 else r["o"], "kind": r.get("kind", "dir"),
             "mtime_mean": r["mtime_mean"] if v == 2 else (r["wts"] / r["wb"] if r["wb"] else 0.0),
             "last_read": r["last_read"] if v == 2 else r["a"]} for r in t]
    out = {}
    for usr in {r["usr"] for r in rows}:
        for k, a in _oracle([r for r in rows if r["usr"] == usr], v).items():
            out[(*k, usr)] = {c: x for c, x in a.items() if c != "us"}
    return out


def test_slice_versions_reconstruct_every_scan(built):
    out, scans, _, _ = built
    root = out.parent
    n = 0
    for s in scans["scans"]:
        got = {}
        for r in _live(out, "sv", s["ts"]):
            got[(r["depth"], r["path"], r["usr"])] = {"kind": r["kind"], "size": r["size"], "n_files": r["n_files"], "n_children": r["n_children"],
                                                      "n_desc": r["n_desc"], "mtime": r["mtime"], "dr": round(r["wts"] / r["wb"]) if r["wb"] > 0 else 0,
                                                      "wb": r["wb"], "c2": r[C2], "c3": r[C3], "c4": r[C4], "last_read": r["last_read"]}
        want = _slice_oracle(root, s)
        assert got == want, s["id"]
        n += len(want)
    assert n == sum(d["rows"] for d in json.loads((out / "sv-digest" / "r0000.json").read_text())["scans"]) + sum(
        d["rows"] for i in (1, 2) for d in json.loads((out / "sv-digest" / f"r{i:04d}.json").read_text())["scans"])
    assert [json.loads((out / "sv-digest" / f"r{i:04d}.json").read_text())["eq"] for i in range(3)] == [True, True, True]


def test_slice_totals_carry_each_scans_path_total(built):
    """Every slice live at a scan carries its path's total at that scan (Σ of its live slices)."""
    out, scans, _, _ = built
    for s in scans["scans"]:
        live = _live(out, "svt", s["ts"])
        tot: dict[tuple, int] = {}
        for r in live:
            tot[(r["depth"], r["path"])] = tot.get((r["depth"], r["path"]), 0) + r["size"]
        assert [(r["path"], r["usr"], r["tot"]) for r in live] == [(r["path"], r["usr"], tot[(r["depth"], r["path"])]) for r in live], s["id"]
        key = lambda r: (r["depth"], r["path"], r["usr"] or "")
        assert sorted(map(key, live)) == sorted(map(key, _live(out, "sv", s["ts"])))


def test_path_versions_reconstruct_every_scan(built):
    out, scans, oracle, _ = built
    for s, want in zip(scans["scans"], oracle):
        got = {}
        for r in _live(out, "pv", s["ts"]):
            got[(r["depth"], r["path"])] = {"kind": r["kind"], "size": r["size"], "n_files": r["n_files"], "n_children": r["n_children"],
                                            "n_desc": r["n_desc"], "mtime": r["mtime"], "dr": round(r["wts"] / r["wb"]) if r["wb"] > 0 else 0,
                                            "wb": r["wb"], "c2": r[C2], "c3": r[C3], "c4": r[C4], "us": r["us"]}
        assert got == {k: {c: v for c, v in a.items() if c != "last_read"} for k, a in want.items()}, s["id"]


def test_read_versions_reconstruct_every_scan(built):
    out, scans, oracle, _ = built
    for s, want in zip(scans["scans"], oracle):
        got = {(r["depth"], r["path"]): r["last_read"] for r in _live(out, "rd", s["ts"])}
        assert got == {k: a["last_read"] for k, a in want.items() if a["last_read"] >= 0}, s["id"]


def test_fold_equals_versioning_with_read_days(built):
    """The folded versions reconstruct every scan's per-path rows *with* their read day, and no two
    adjacent ones are equal: the versions a build with `last_read` as one more change column makes."""
    out, scans, oracle, _ = built
    for s, want in zip(scans["scans"], oracle):
        got = {}
        for r in _live(out, "pvl", s["ts"]):
            got[(r["depth"], r["path"])] = {"kind": r["kind"], "size": r["size"], "n_files": r["n_files"], "n_children": r["n_children"],
                                            "n_desc": r["n_desc"], "mtime": r["mtime"], "dr": round(r["wts"] / r["wb"]) if r["wb"] > 0 else 0,
                                            "wb": r["wb"], "c2": r[C2], "c3": r[C3], "c4": r[C4], "us": r["us"], "last_read": r["last_read"]}
        assert got == want, s["id"]
    t = pa.concat_tables([pq.read_table(p) for p in sorted((out / "pvl").glob("r*.parquet"))]).to_pylist()
    key = lambda r: tuple(r[c] for c in ("kind", "size", "n_files", "n_children", "n_desc", "mtime", "wb", C2, C3, C4, "us", "last_read")) + (round(r["wts"] / r["wb"]) if r["wb"] > 0 else 0,)
    assert [(a["path"], a["vt"]) for a, b in zip(t, t[1:]) if (a["depth"], a["path"]) == (b["depth"], b["path"]) and a["vt"] == b["vf"] and key(a) == key(b)] == []
    assert (len(t), sum(1 for p in sorted((out / "pv").glob("r*.parquet")) for _ in pq.read_table(p).to_pylist())) == (116, 113)


def test_versions_are_runs_of_change(built):
    """No two adjacent versions of a path hold equal change columns (they'd be one version)."""
    out = built[0]
    t = pa.concat_tables([pq.read_table(p) for p in sorted((out / "pv").glob("r*.parquet"))]).to_pylist()
    key = lambda r: (r["kind"], r["size"], r["n_files"], r["n_children"], r["n_desc"], r["mtime"],
                     round(r["wts"] / r["wb"]) if r["wb"] > 0 else 0, r["wb"], r[C2], r[C3], r[C4], r["us"])
    adjacent_equal = [(a["path"], a["vt"]) for a, b in zip(t, t[1:]) if (a["depth"], a["path"]) == (b["depth"], b["path"]) and a["vt"] == b["vf"] and key(a) == key(b)]
    assert adjacent_equal == []


def test_digests_say_every_scan_reconstructs(built):
    _, scans, oracle, docs = built
    assert [d["eq"] for d in docs] == [True, True, True]
    rows = [sum(d["scans"][j]["rows"] for d in docs) for j in range(len(scans["scans"]))]
    assert rows == [len(o) for o in oracle]
    assert [sum(d["scans"][j]["live"] for d in docs) for j in range(len(scans["scans"]))] == rows


@pytest.fixture(scope="module")
def served(built, tmp_path_factory):
    """The fixture's served sorts, in tiny row groups so group planning has something to prune."""
    out, scans, _, _ = built
    dst = tmp_path_factory.mktemp("served")
    con = duckdb.connect()
    for sort, (sub, _, _) in ((s, ist.SORTS[s]) for s in ist.served_sorts(True)):
        schema = ist.SUB_SCHEMA[sub]
        ist.write_served(con, f"read_parquet('{out}/{sub}/r*.parquet')", sort, dst / f"{sort}.parquet", schema, page_rows=ist.served_page_rows(sort), rg_rows=4,
                         stamps=[x["ts"] for x in scans["scans"]])
    return dst


def test_served_pages_are_page_rows_aligned_with_a_page_index(built, tmp_path):
    """Pages change the layout only: the same rows in the same groups as one page a group, the planning columns' pages
    start every `page_rows` rows (a group's last short), and each chunk's page-index ranges are in the sidecar's tuple."""
    from dt_cloud.parquet_pages import chunk_indexes, page_locations

    out, scans, _, _ = built
    con = duckdb.connect()
    sub = ist.SORTS["path"][0]
    src = f"read_parquet('{out}/{sub}/r*.parquet')"
    stamps = [x["ts"] for x in scans["scans"]]
    paged, whole = tmp_path / "paged" / "path.parquet", tmp_path / "whole" / "path.parquet"
    ist.write_served(con, src, "path", paged, ist.SUB_SCHEMA[sub], rg_rows=8, stamps=stamps, page_rows=3)
    ist.write_served(con, src, "path", whole, ist.SUB_SCHEMA[sub], rg_rows=8, stamps=stamps, page_rows=8)
    pm, wm = pq.read_metadata(paged), pq.read_metadata(whole)
    assert pq.read_table(paged).to_pylist() == pq.read_table(whole).to_pylist()
    sizes = [pm.row_group(g).num_rows for g in range(pm.num_row_groups)]
    assert sizes == [wm.row_group(g).num_rows for g in range(wm.num_row_groups)]
    ixs = chunk_indexes(paged)
    names = pm.schema.names
    plan = ("depth", "path", "vf", "vt")
    starts = [{n: [p.first_row_index for p in page_locations(paged, ix)] for n, ix in zip(names, ixs[g])} for g in range(pm.num_row_groups)]
    # The columns a reader plans pages by start a page every 3 rows; any other column's pages start on those rows too (an
    # all-null column never reaches the 1-byte target: one page a group).
    assert [{n: v for n, v in st.items() if n in plan} for st in starts] == [{n: list(range(0, k, 3)) for n in plan} for k in sizes]
    assert [n for st, k in zip(starts, sizes) for n, v in st.items() if not set(v) <= set(range(0, k, 3))] == []
    assert [[[p.first_row_index for p in page_locations(whole, ix)] for ix in g] for g in chunk_indexes(whole)] == [[[0]] * wm.num_columns for _ in sizes]
    tuples = [json.loads(r["rg_json"])[2] for r in pq.read_table(paged.with_name("path.groups.parquet")).to_pylist()]
    assert [[c[3:] for c in t] for t in tuples] == [[list(ix) for ix in g] for g in ixs]
    assert [[(n, bool(ix.column_index_length), bool(ix.offset_index_length)) for n, ix in zip(names, g) if n in plan] for g in ixs] == \
        [[(n, True, True) for n in names if n in plan]] * len(ixs)
    assert all(ix.offset_index_length for g in ixs for ix in g)


def _scans(built):
    from dt_cloud.interval_verify import Scan

    out, scans, _, _ = built
    root = out.parent
    con = duckdb.connect()
    return [(s, Scan(con, str(root / s["src"]), s["version"], s["src"])) for s in scans["scans"]]


def test_seg_sql_places_a_version_in_the_smallest_dyadic_block_of_its_scans():
    con = duckdb.connect()
    stamps = [10, 20, 30, 40, 50, 60]
    # Live at scans (by index) 0..0 → block [0] (level 0); 1..2 → [0..3] (level 2: 1 and 2 split at 2);
    # 2..3 → [2..3] (level 1, block 1); 3..4 and 0..4 → [0..7] (level 3); open → segment 0.
    rows = [(10, 20), (20, 40), (30, 50), (40, 60), (10, 60), (30, sn.OPEN)]
    got = [con.execute(f"SELECT {ist.seg_sql(stamps)} FROM (SELECT {vf}::BIGINT AS vf, {vt}::BIGINT AS vt)").fetchone()[0] for vf, vt in rows]
    L = ist.SEG_LEVEL
    assert got == [1, 1 + 2 * L, 1 + 1 * L + 1, 1 + 3 * L, 1 + 3 * L, 0]
    assert con.execute(f"SELECT {ist.seg_sql(None)} FROM (SELECT 10::BIGINT AS vf, 20::BIGINT AS vt)").fetchone()[0] == 1


def test_served_groups_split_open_from_closed_by_dyadic_block(built, served):
    _, scans, _, _ = built
    idx = {s["ts"]: i for i, s in enumerate(scans["scans"])}
    t = pq.read_table(served / "path.parquet").to_pylist()
    g = pq.read_table(served / "path.groups.parquet").to_pylist()
    segs = [x["seg"] for x in g]
    assert segs == sorted(segs) and segs[0] == 0 and segs[-1] > 1
    assert all(x["vt_min"] == x["vt_max"] == sn.OPEN for x in g if x["seg"] == 0)
    # Every closed row lies inside its group's block: live only at that block's scans.
    outside = []
    for x in g:
        if x["seg"] == 0:
            continue
        k, block = divmod(x["seg"] - 1, ist.SEG_LEVEL)
        lo, hi = block << k, ((block + 1) << k) - 1
        for r in t[x["row_start"]:x["row_end"]]:
            i, j = idx[r["vf"]], idx[r["vt"]] - 1
            if not (lo <= i <= j <= hi):
                outside.append((r["path"], i, j, k, block))
    assert outside == []
    # Each segment in the sort's order.
    for s in set(segs):
        rows = [r for x in g if x["seg"] == s for r in t[x["row_start"]:x["row_end"]]]
        keys = [(r["depth"], r["path"], r["vf"]) for r in rows]
        assert keys == sorted(keys)
    b = pq.read_table(served / "bysize.parquet")
    bg = pq.read_table(served / "bysize.groups.parquet").to_pylist()
    sizes = b["size"].to_pylist()
    for s in {x["seg"] for x in bg}:
        bk = [(sz.bit_length() - 1 if sz > 0 else -1) for x in bg if x["seg"] == s for sz in sizes[x["row_start"]:x["row_end"]]]
        assert bk == sorted(bk, key=lambda v: (v < 0, -v))


@pytest.mark.parametrize("wh", [(4, 4), (8, 6), (30, 30)])
def test_views_equal_the_per_scan_reference(built, served, wh):
    from dt_cloud import interval_read as ir
    from dt_cloud.interval_verify import compare

    store = ir.Store(served)
    w, h = wh
    checked = 0
    for s, scan in _scans(built):
        for path in ["", "b1", "b2", "b1/gof", "b2/e", "b2/f"]:
            for md in (None, 1):
                want = scan.view(path, w, h, max_depth=md)
                got = store.view(s["ts"], path, w, h, max_depth=md)
                assert compare(got["tree"], want["tree"], with_f=True) == [], (s["id"], path, md)
                checked += want["tree"] is not None
    assert checked == 50  # of 6 scans × 6 paths × 2 depths: the rest are paths absent from a scan


def test_diffs_equal_the_per_scan_reference(built, served):
    from dt_cloud import interval_read as ir

    store = ir.Store(served)
    sc = _scans(built)
    n_changed = folds = 0
    for (sa, a), (sb, b) in zip(sc, sc[1:]):
        for path in ["", "b1", "b2/e"]:
            ra, rb = a.root_b(path), b.root_b(path)
            if ra <= 0 or rb <= 0:
                continue
            thr = max(ra, rb) * ir.MIN_AREA / (8 * 6)
            # Across the v1 → v2 cutover (08-02 → 08-03) the objects fold: per-scan from each source's format, the
            # store from its rows (`Store.view`'s `objects`).
            fold = a.v != b.v
            want = ir.diff(a.view(path, 8, 6, threshold=thr), b.view(path, 8, 6, threshold=thr), a.lookup, b.lookup, fold=fold)
            cost = ir.Cost()
            ga, gb = store.view(sa["ts"], path, 8, 6, threshold=thr), store.view(sb["ts"], path, 8, 6, threshold=thr)
            got = ir.diff(ga, gb, lambda p: store.lookup(sa["ts"], p, cost), lambda p: store.lookup(sb["ts"], p, cost),
                          fold=ga["objects"] != gb["objects"])
            assert (got, ga["objects"] != gb["objects"]) == (want, fold), (sa["id"], sb["id"], path)
            n_changed += sum(1 for r in want if r[2] != "unchanged")
            folds += fold
    assert n_changed > 0
    assert folds > 0


def test_a_scans_rows_say_its_format(built, served):
    """A v1 scan's versions carry no structural counts (`n_children` / `n_desc` -1), a store generation's always do:
    what `buildDiff` (and `Store.view`'s `objects`) tells a dir-only scan by."""
    from dt_cloud import interval_read as ir

    store = ir.Store(served)
    paths = ["", "b1", "b2", "b1/gof", "b1/d", "b2/e", "b2/f"]
    sc = _scans(built)
    # Every view each scan has (the fixture's paths come and go; a scan without P has no view of it).
    got = [(s["id"], sorted({v["objects"] for v in (store.view(s["ts"], p, 30, 30) for p in paths) if v["tree"] is not None})) for s, _ in sc]
    assert got == [(s["id"], [scan.v == 2]) for s, scan in sc]


class _Spy:
    """A connection, its statements kept."""

    def __init__(self, con):
        self.con, self.sql = con, []

    def execute(self, sql: str):
        self.sql.append(sql)
        return self.con.execute(sql)


def _global_stats(con, root: str, scans: dict) -> dict:
    """`churn_stats` as one global `count(DISTINCT …)` over every path (what it was, what a machine of a billion paths
    couldn't hold): the reference its per-range counts must equal, byte for byte."""
    ids = {s["ts"]: s["id"] for s in scans["scans"]}
    pv, rd = f"read_parquet('{root}/pv/r*.parquet')", f"read_parquet('{root}/rd/r*.parquet')"
    per = {i: {"id": i, "opened": 0, "closed": 0, "reads_opened": 0, "reads_closed": 0} for i in ids.values()}
    for col, key, src in (("vf", "opened", pv), ("vt", "closed", pv), ("vf", "reads_opened", rd), ("vt", "reads_closed", rd)):
        for t, n in con.execute(f"SELECT {col}, count(*) FROM {src} WHERE {col} <> {ist.OPEN} GROUP BY {col}").fetchall():
            per[ids[t]][key] = n
    kinds = {k: {"versions": n, "open": o, "paths": p} for k, n, o, p in con.execute(
        f"SELECT kind, count(*), count(*) FILTER (WHERE vt = {ist.OPEN}), count(DISTINCT path) FROM {pv} GROUP BY kind ORDER BY kind").fetchall()}
    tot = con.execute(f"SELECT count(*), count(*) FILTER (WHERE vt = {ist.OPEN}), count(DISTINCT (depth, path)) FROM {pv}").fetchone()
    rtot = con.execute(f"SELECT count(*), count(*) FILTER (WHERE vt = {ist.OPEN}) FROM {rd}").fetchone()
    return {"versions": tot[0], "open": tot[1], "paths": tot[2], "reads": rtot[0], "reads_open": rtot[1], "kinds": kinds,
            "scans": [per[s["id"]] for s in scans["scans"]]}


def _stats_json(stats: dict) -> str:
    return json.dumps(stats, indent=1) + "\n"


def test_churn_stats_counts_a_range_at_a_time_as_the_global_query_did(tmp_path):
    """Boundaries cut through what the prefix inversion puts on both sides of one (`a/b` sorts below `a/b-c`, its object
    `a/b/c.txt` a depth on), a path's versions and a kind that flips, each row placed by the real `range_preds`: no key
    is in two files, the distinct-path counts are the global query's, and each counts one file."""
    t1, t2 = 100, 200
    OPEN = ist.OPEN
    rows = [
        (0, "", "dir", t1, t2), (0, "", "dir", t2, OPEN),
        (1, "a", "dir", t1, OPEN),
        (2, "a/b", "dir", t1, t2), (2, "a/b", "dir", t2, OPEN),
        (2, "a/b-c", "file", t2, OPEN),
        (2, "a/x", "file", t1, t2), (2, "a/x", "dir", t2, OPEN),
        (3, "a/b/c.txt", "file", t1, OPEN),
        (3, "a/b/d", "file", t1, t2), (3, "a/b/d", "file", t2, OPEN),
    ]
    ranges = [{"lo": [0, ""], "hi": [2, "a/b-c"]}, {"lo": [2, "a/b-c"], "hi": [3, "a/b/d"]}, {"lo": [3, "a/b/d"], "hi": [3, "zz"]}, {"lo": [3, "zz"], "hi": None}]
    con = duckdb.connect()
    t = pa.table(dict(zip(("depth", "path", "kind", "vf", "vt"), zip(*rows))))
    t = t.cast(pa.schema([("depth", pa.uint8()), ("path", pa.string()), ("kind", pa.string()), ("vf", pa.int64()), ("vt", pa.int64())]))
    con.register("t", t)
    (tmp_path / "pv").mkdir()
    (tmp_path / "rd").mkdir()
    held = []
    for i, r in enumerate(ranges):
        sql = " UNION ALL ".join(f"SELECT * FROM t WHERE {p}" for p in sn.range_preds(r))
        part = con.execute(f"{sql} ORDER BY depth, path, vf").to_arrow_table()
        pq.write_table(part, tmp_path / "pv" / f"r{i:04d}.parquet")
        pq.write_table(pa.table({"vf": pa.array([t1], pa.int64()), "vt": pa.array([OPEN], pa.int64())}), tmp_path / "rd" / f"r{i:04d}.parquet")
        held.append(sorted(set(zip(part["depth"].to_pylist(), part["path"].to_pylist()))))
    assert held == [
        [(0, ""), (1, "a"), (2, "a/b")],
        [(2, "a/b-c"), (2, "a/x"), (3, "a/b/c.txt")],
        [(3, "a/b/d")],
        [],
    ]
    scans = {"scans": [{"id": "s1", "ts": t1}, {"id": "s2", "ts": t2}]}
    spy = _Spy(con)
    got = ist.churn_stats(spy, str(tmp_path), scans)
    assert got["kinds"] == {
        "dir": {"versions": 6, "open": 4, "paths": 4},
        "file": {"versions": 5, "open": 3, "paths": 4},
    }
    assert (got["paths"], got["versions"], got["open"]) == (7, 11, 7)
    assert _stats_json(got) == _stats_json(_global_stats(con, str(tmp_path), scans))
    # One file a statement, all of them, in order; no other statement holds a distinct path.
    assert [re.findall(r"read_parquet\('([^']*)'\)", s) for s in spy.sql if "DISTINCT" in s] == [[str(tmp_path / "pv" / f"r{i:04d}.parquet")] for i in range(4)]


def test_churn_stats_on_built_ranges_is_the_global_querys(built):
    """The same over a real build's range files and bounds (`b1/gof`'s prefix-siblings around the cut)."""
    out, scans, *_ = built
    con = duckdb.connect()
    assert _stats_json(ist.churn_stats(con, str(out), scans)) == _stats_json(_global_stats(con, str(out), scans))
