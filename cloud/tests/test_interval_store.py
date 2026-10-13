"""`dt_cloud.interval_store`: the path versions reconstruct every scan's per-path rows exactly (the
reader's `merge` over a path's rows, folded into one), the read versions every scan's `last_read`, and
the build's per-scan digests say so."""
from __future__ import annotations

import json
import random
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import interval_store as ist
from dt_cloud import static_names as sn

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
                                                      "wb": r["wb"], "c2": r["c2"], "c3": r["c3"], "c4": r["c4"], "last_read": r["last_read"]}
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
                                            "wb": r["wb"], "c2": r["c2"], "c3": r["c3"], "c4": r["c4"], "us": r["us"]}
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
                                            "wb": r["wb"], "c2": r["c2"], "c3": r["c3"], "c4": r["c4"], "us": r["us"], "last_read": r["last_read"]}
        assert got == want, s["id"]
    t = pa.concat_tables([pq.read_table(p) for p in sorted((out / "pvl").glob("r*.parquet"))]).to_pylist()
    key = lambda r: tuple(r[c] for c in ("kind", "size", "n_files", "n_children", "n_desc", "mtime", "wb", "c2", "c3", "c4", "us", "last_read")) + (round(r["wts"] / r["wb"]) if r["wb"] > 0 else 0,)
    assert [(a["path"], a["vt"]) for a, b in zip(t, t[1:]) if (a["depth"], a["path"]) == (b["depth"], b["path"]) and a["vt"] == b["vf"] and key(a) == key(b)] == []
    assert (len(t), sum(1 for p in sorted((out / "pv").glob("r*.parquet")) for _ in pq.read_table(p).to_pylist())) == (116, 113)


def test_versions_are_runs_of_change(built):
    """No two adjacent versions of a path hold equal change columns (they'd be one version)."""
    out = built[0]
    t = pa.concat_tables([pq.read_table(p) for p in sorted((out / "pv").glob("r*.parquet"))]).to_pylist()
    key = lambda r: (r["kind"], r["size"], r["n_files"], r["n_children"], r["n_desc"], r["mtime"],
                     round(r["wts"] / r["wb"]) if r["wb"] > 0 else 0, r["wb"], r["c2"], r["c3"], r["c4"], r["us"])
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
    for sort, (sub, _, _) in ist.SORTS.items():
        schema = ist.SUB_SCHEMA[sub]
        ist.write_served(con, f"read_parquet('{out}/{sub}/r*.parquet')", sort, dst / f"{sort}.parquet", schema, rg_rows=4,
                         stamps=[x["ts"] for x in scans["scans"]])
    return dst


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
    n_changed = 0
    for (sa, a), (sb, b) in zip(sc, sc[1:]):
        for path in ["", "b1", "b2/e"]:
            ra, rb = a.root_b(path), b.root_b(path)
            if ra <= 0 or rb <= 0:
                continue
            thr = max(ra, rb) * ir.MIN_AREA / (8 * 6)
            want = ir.diff(a.view(path, 8, 6, threshold=thr), b.view(path, 8, 6, threshold=thr), a.lookup, b.lookup)
            cost = ir.Cost()
            got = ir.diff(store.view(sa["ts"], path, 8, 6, threshold=thr), store.view(sb["ts"], path, 8, 6, threshold=thr),
                          lambda p: store.lookup(sa["ts"], p, cost), lambda p: store.lookup(sb["ts"], p, cost))
            assert got == want, (sa["id"], sb["id"], path)
            n_changed += sum(1 for r in want if r[2] != "unchanged")
    assert n_changed > 0
