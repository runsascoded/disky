"""`dt_cloud.interval_churn`: every folded path version's `churn_cum` is its path's cumulative churn (an oracle over the
scans' files), so a range's churn is additive up the tree (a directory's is its children's sum) and bounds its net change;
the switch scan (where objects are first listed) counts none; a same-size swap under a directory splits its piece."""
from __future__ import annotations

import json
import random
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest
from test_interval_store import _write

from dt_cloud import interval_churn as ich
from dt_cloud import interval_store as ist
from dt_cloud import static_names as sn
from dt_cloud.static_names import OPEN

DATES = ["2026-07-30", "2026-07-31", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"]
V2_FROM = 2
RANGES = {"k": 3, "ranges": [{"i": 0, "lo": [0, ""], "hi": [2, "b1/x"]}, {"i": 1, "lo": [2, "b1/x"], "hi": [3, ""]},
                             {"i": 2, "lo": [3, ""], "hi": None}]}
FILES = ["b1/a.bin", "b1/x/b.bin", "b1/x/c.bin", "b1/x/y/d.bin", "b1/x/y/e.bin", "b2/f.bin", "b2/z/g.bin", "b2/z/h.bin"]
#: The swap: at scan `SWAP`, `b2/z`'s files trade sizes (its size, count and stamps hold).
SWAP = 5


def _dirs(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:k]) for k in range(1, len(parts))]


def _files(rng: random.Random, j: int) -> dict[str, int]:
    """Scan `j`'s files and sizes: some missing, some resized, `b2/z`'s two trading sizes at `SWAP`."""
    out = {}
    for f in FILES:
        if f.startswith("b2/z/"):
            out[f] = {"b2/z/g.bin": 10, "b2/z/h.bin": 30}[f] if j < SWAP else {"b2/z/g.bin": 30, "b2/z/h.bin": 10}[f]
        elif rng.random() < 0.8:
            out[f] = rng.choice([5, 10, 10, 20, 40])
    return out


def _rows(files: dict[str, int], v: int) -> list[dict]:
    """A scan's rows: its directories (sizes the sums of their files), and with v2 its files. Stamps and read days
    hold, so a directory's piece moves only where its size or file count does."""
    sizes: dict[str, list[int]] = {}
    for f, b in files.items():
        for d in _dirs(f):
            sizes.setdefault(d, []).append(b)
    rows = [{"path": d, "depth": d.count("/") + 1, "usr": None, "size": sum(bs), "n_files": len(bs), "mtime_mean": 1_700_000_000.0,
             "last_read": None, "kind": "dir"} for d, bs in sizes.items()]
    if v == 2:
        rows += [{"path": f, "depth": f.count("/") + 1, "usr": None, "size": b, "n_files": 1, "mtime_mean": 1_700_000_000.0,
                  "last_read": None, "kind": "file"} for f, b in files.items()]
    return rows


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("churn")
    rng = random.Random(5)
    scans, by_scan = [], []
    for j, d in enumerate(DATES):
        v = 2 if j >= V2_FROM else 1
        files = _files(rng, j)
        key = f"listing/{d}/path-index.parquet"
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        _write(_rows(files, v), root / key, v)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
        by_scan.append(files)
    out = root / "gen"
    out.mkdir()
    (out / "ranges.json").write_text(json.dumps(RANGES))
    con = duckdb.connect()
    doc = {"bucket": "b", "scans": scans}
    for i in range(RANGES["k"]):
        assert ist.build_range(doc, RANGES, i, out, con, mount=str(root))["eq"]
        ist.fold_range(str(out), i, out, con)
    before = {i: pq.read_table(out / "pvl" / f"r{i:04d}.parquet").num_rows for i in range(RANGES["k"])}
    base = ich.churn_from(scans)
    stats = ich.churn_gen(con, out, RANGES["k"], base["ts"], root / "work")
    pieces = sorted((r for i in range(RANGES["k"]) for r in pq.read_table(out / "pvl" / f"r{i:04d}.parquet").to_pylist()),
                    key=lambda r: (r["depth"], r["path"], r["vf"]))
    return {"scans": scans, "files": by_scan, "base": base, "pieces": pieces, "stats": stats, "before": before}


def _oracle(world) -> dict[str, list[int]]:
    """Each path's cumulative churn at every scan: per scan after the base, each file's |Δsize| (its size where it
    appears or goes away), summed onto the file and its directories."""
    scans, files = world["scans"], world["files"]
    b = next(j for j, s in enumerate(scans) if s["id"] == world["base"]["id"])
    paths = sorted({p for fs in files for f in fs for p in [f, *_dirs(f)]})
    cum = {p: [0] * len(scans) for p in paths}
    for j in range(1, len(scans)):
        step = {p: 0 for p in paths}
        if j > b:
            for f in set(files[j - 1]) | set(files[j]):
                c = abs(files[j].get(f, 0) - files[j - 1].get(f, 0))
                for p in [f, *_dirs(f)]:
                    step[p] += c
        for p in paths:
            cum[p][j] = cum[p][j - 1] + step[p]
    return cum


def _cum_at(world, path: str, ts: int) -> int | None:
    """The cumulative churn at `ts` as a reader takes it: the live piece's `churn_cum`, else the last piece's `churn_end`;
    None before the path's first piece."""
    mine = [r for r in world["pieces"] if r["path"] == path]
    live = [r for r in mine if r["vf"] <= ts < r["vt"]]
    if live:
        return live[0][ich.CHURN]
    past = [r for r in mine if r["vt"] <= ts]
    if not past:
        return None
    return max(past, key=lambda r: r["vf"])[ich.CHURN_END]


def test_churn_cum_is_each_paths_cumulative_churn(world):
    cum = _oracle(world)
    got = {p: [_cum_at(world, p, s["ts"]) for s in world["scans"]] for p in cum}
    # Before a path's first piece there is none; from it on, the oracle's.
    want = {p: [None if got[p][j] is None else c for j, c in enumerate(cs)] for p, cs in cum.items()}
    assert got == want
    assert sum(v is not None for vs in got.values() for v in vs) > 2 * len(cum)


def test_the_switch_scan_counts_no_churn(world):
    """At the base (the first scan listing objects, after dirs-only scans) every live piece's `churn_cum` is 0, though
    every file appears there."""
    assert world["base"]["id"] == DATES[V2_FROM]
    t = world["base"]["ts"]
    live = [r for r in world["pieces"] if r["vf"] <= t < r["vt"]]
    assert {r[ich.CHURN] for r in live} == {0}
    assert sum(r["kind"] == "file" for r in live) > 0


def _children(paths: set[str], d: str) -> list[str]:
    return [p for p in paths if p.rsplit("/", 1)[0] == d and p != d]


def test_range_churn_is_additive_up_the_tree_and_bounds_net(world):
    """For every pair of scans from the base on and every directory: its range churn is the sum of its children's, and
    at least its net change."""
    scans = world["scans"]
    b = next(j for j, s in enumerate(scans) if s["id"] == world["base"]["id"])
    paths = {r["path"] for r in world["pieces"]}
    dirs = sorted(p for p in paths if any(q.startswith(p + "/") for q in paths))
    sizes = lambda j, p: sum(b_ for f, b_ in world["files"][j].items() if f == p or f.startswith(p + "/"))  # noqa: E731
    checked = 0
    for j1 in range(b, len(scans)):
        for j2 in range(j1 + 1, len(scans)):
            t1, t2 = scans[j1]["ts"], scans[j2]["ts"]
            rc = {p: (_cum_at(world, p, t2) or 0) - (_cum_at(world, p, t1) or 0) for p in paths}
            for d in dirs:
                assert (d, j1, j2, rc[d]) == (d, j1, j2, sum(rc[c] for c in _children(paths, d)))
                assert rc[d] >= abs(sizes(j2, d) - sizes(j1, d))
                checked += 1
            assert all(rc[p] >= abs(sizes(j2, p) - sizes(j1, p)) for p in paths)
    assert checked == 5 * 10  # 5 directories × the 10 pairs of the 5 scans from the base on


def test_a_same_size_swap_splits_the_directorys_piece(world):
    """`b2/z`'s files trade sizes at `SWAP`: its own row holds (size, count, stamps), so only its churn moves there, and
    its piece splits at that scan, its other columns equal on both sides; the one split `churn_gen` made."""
    t = world["scans"][SWAP]["ts"]
    z = [r for r in world["pieces"] if r["path"] == "b2/z"]
    rest = lambda r: {k: v for k, v in r.items() if k not in ("vf", "vt", ich.CHURN, ich.CHURN_END)}  # noqa: E731
    assert [(r["vf"], r["vt"], r[ich.CHURN], r[ich.CHURN_END]) for r in z[-2:]] == [(z[-2]["vf"], t, 0, 40), (t, OPEN, 40, None)]
    assert rest(z[-2]) == rest(z[-1])
    assert world["stats"]["split"] == 1


def test_churn_gen_writes_the_columns_once_before_the_object_version():
    import pyarrow as pa

    s = pa.schema([("depth", pa.uint8()), ("path", pa.string()), ("version", pa.string())])
    assert ich.pq_schema_with_churn(s).names == ["depth", "path", ich.CHURN, ich.CHURN_END, "version"]
    assert ich.pq_schema_with_churn(ich.pq_schema_with_churn(s)).names == ["depth", "path", ich.CHURN, ich.CHURN_END, "version"]


def test_churn_from_is_the_last_switch_to_objects_else_the_first_scan():
    v = lambda *vs: [{"id": str(i), "version": x} for i, x in enumerate(vs)]  # noqa: E731
    assert ich.churn_from(v(1, 1, 2, 2))["id"] == "2"
    assert ich.churn_from(v(2, 2))["id"] == "0"
    assert ich.churn_from(v(1, 2, 1, 2))["id"] == "3"


# ── backfill-churn: a generation from before churn ─────────────────────────


@pytest.fixture(scope="module")
def churnless(tmp_path_factory):
    """`test_interval_local`'s world: a base built as before churn (its `churn_gen` a no-op, no flag), the other scans
    appended to it (which then write no churn either), and a fresh build over every scan, with churn."""
    from test_interval_local import DATES, LAYOUT, _add, _build
    from test_interval_store import V2_FROM as V2, _scan_rows, _universe

    from dt_cloud import interval_local as il

    tmp = tmp_path_factory.mktemp("churn-backfill")
    root = tmp / "scans"
    rng = random.Random(11)
    paths = _universe(rng)
    scans = []
    for j, d in enumerate(DATES):
        key = LAYOUT.replace("{id}", d)
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        _write(_scan_rows(rng, paths, j), root / key, 2 if j >= V2 else 1)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": 2 if j >= V2 else 1})
    world = {"root": root, "scans": scans, "paths": paths}
    store = tmp / "store"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(il.ich, "churn_gen", lambda *a, **kw: {})
        mp.setattr(il.ich, "record_churn", lambda doc: doc)
        _build(world, store, tmp / "work-build")
    _add(world, store, tmp / "work-add")
    fresh = tmp / "fresh"
    args = ["-g", "f", "-L", LAYOUT, "-s", str(world["root"]), "-o", str(fresh), "-w", str(tmp / "work-fresh"),
            "-k", "3", "-r", "4", "-p", "2", "-M", "1GB", "-t", DATES[-1]]
    from click.testing import CliRunner

    res = CliRunner().invoke(il.build_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return {"tmp": tmp, "store": store, "fresh": fresh / ist.PREFIX / "f"}


def test_a_backfilled_generation_is_a_fresh_build(churnless):
    """`backfill-churn` over the churnless base + its runs: the served sorts and `scans.json` of a fresh build over every
    scan, byte for byte, and its folded path versions row for row; the source untouched."""
    from click.testing import CliRunner

    store, tmp = churnless["store"], churnless["tmp"]
    src = store / ist.PREFIX / "g"
    assert "churn" not in json.loads((src / "scans.json").read_text())
    assert ich.CHURN not in pq.read_schema(src / "served" / "path.parquet").names
    before = {p.relative_to(src).as_posix(): p.read_bytes() for p in sorted(src.rglob("*")) if p.is_file()}
    res = CliRunner().invoke(ich.backfill_cmd, ["-g", "g", "-O", "g.churn", "-o", str(store), "-w", str(tmp / "work-bf"), "-r", "4", "-p", "2", "-M", "1GB"],
                             catch_exceptions=False)
    assert res.exit_code == 0, res.output
    got, want = store / ist.PREFIX / "g.churn", churnless["fresh"]
    served = lambda g: {p.relative_to(g).as_posix(): p.read_bytes() for p in sorted((g / "served").glob("*.parquet"))}  # noqa: E731
    assert served(got) == served(want)
    assert (got / "scans.json").read_bytes() == (want / "scans.json").read_bytes()
    rows = lambda g: [r for i in range(3) for r in pq.read_table(g / "pvl" / f"r{i:04d}.parquet").to_pylist()]  # noqa: E731
    key = lambda r: (r["depth"], r["path"], r["vf"])  # noqa: E731
    assert sorted(rows(got), key=key) == sorted(rows(want), key=key)
    rec = json.loads((got / ich.BACKFILL).read_text())
    assert (rec["from"], rec["scans"], rec["base"]) == ("g", 8, "2026-08-03")
    assert {p.relative_to(src).as_posix(): p.read_bytes() for p in sorted(src.rglob("*")) if p.is_file()} == before
    res = CliRunner().invoke(ich.backfill_cmd, ["-g", "g.churn", "-O", "g.again", "-o", str(store), "-w", str(tmp / "work-bf2")])
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (1, f"{store}/{ist.PREFIX}/g.churn: already carries `{ich.CHURN}`")
