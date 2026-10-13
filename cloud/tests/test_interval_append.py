"""`dt_cloud.interval_append`: a base generation plus per-scan runs is, version for version, a full rebuild through the
same scan — for the folded path versions (`pvl`), the owner slices (`sv`) and the slices with their path's total
(`svt`) — with each run read alone and with runs merged on the binary counter; every view at every path and scan read
over base + runs is the per-scan reference's; and the comparison catches a broken run (mutations). The deferred carries
(`append_runner`: publish adds a level-0 run, `carry` merges N-way and publishes a revision) end, scan by scan, at the
old inline pairwise carries' stacks, their merged runs byte for byte; a merge interrupted anywhere leaves the store
servable and resumes; one merger at a time (the lease); a publish landing mid-merge is rebased onto. And the chain: order,
stages, the merge stage, jobs."""
from __future__ import annotations

import json
import random
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from test_interval_store import V2_FROM, _scan_rows, _slice_oracle, _universe, _write

from dt_cloud import append_runner as ar
from dt_cloud import interval_append as ia
from dt_cloud import interval_store as ist
from dt_cloud import static_names as sn
from dt_cloud.static_append import push_run, run_key
from dt_cloud.static_profile import Profile, parse_compact_level

#: Four scans in the base (three v1 indexes, one v2 store generation), then four appended (v2, a timed id among them).
DATES = ["2026-07-30", "2026-07-31", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-04T1236", "2026-08-05", "2026-08-07"]
BASE = 4
RANGES = {"k": 3, "ranges": [{"i": 0, "lo": [0, ""], "hi": [2, "b1/gof"]}, {"i": 1, "lo": [2, "b1/gof"], "hi": [3, ""]},
                             {"i": 2, "lo": [3, ""], "hi": None}]}
NAMES = [f"r{i:04d}" for i in range(RANGES["k"])]


def build_full(scans: list[dict], root: Path, out: Path) -> Path:
    """`interval-store build` + `fold` + `build -S` + `fold -S` over `scans`: the range files a generation is cut from."""
    con = duckdb.connect()
    doc = {"bucket": "b", "scans": scans}
    for i in range(RANGES["k"]):
        assert ist.build_range(doc, RANGES, i, out, con, mount=str(root))["eq"]
        ist.fold_range(str(out), i, out, con)
        assert ist.build_slices_range(doc, RANGES, i, out, con, mount=str(root))["eq"]
        ist.slice_totals_range(str(out), i, out, con)
    return out


def append_scan(scan: dict, root: Path, base: Path, prev: Path | None, out: Path) -> list[dict]:
    """Every key range of one scan's run (`runs ranges`): from the base's range files, or the previous run's state."""
    con = duckdb.connect()
    docs = []
    for i, name in enumerate(NAMES):
        state = ia.run_state_sql(str(prev / "state"), name) if prev else ia.base_state_sql(str(base), name)
        docs.append(ia.append_range(con, state, scan, RANGES["ranges"][i], name, out, bucket="b", mount=str(root)))
    return docs


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("iva")
    rng = random.Random(11)
    paths = _universe(rng)
    scans = []
    for j, d in enumerate(DATES):
        v = 2 if j >= V2_FROM else 1
        key = f"listing/{d}/path-index.parquet"
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        _write(_scan_rows(rng, paths, j), root / key, v)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
    base = build_full(scans[:BASE], root, root / "base")
    # A full rebuild through each appended scan: what base + runs must equal.
    full = {s["id"]: build_full(scans[:j + 1], root, root / f"full-{s['id']}") for j, s in enumerate(scans) if j >= BASE}
    runs, prev, docs = {}, None, {}
    for s in scans[BASE:]:
        out = root / "runs" / s["id"]
        docs[s["id"]] = append_scan(s, root, base, prev, out)
        runs[s["id"]] = out
        prev = out
    # The binary counter over the appended scans: after each, the runs a manifest would list (merges made as it says).
    stacks, live = {}, []
    dirs = {f"deltas/{s}": d for s, d in runs.items()}
    for s in scans[BASE:]:
        live, merges = push_run(live, {"key": run_key(s["id"], s["id"]), "first": s["id"], "last": s["id"], "scans": [s["id"]]})
        for ins, m in merges:
            out = root / "merged" / m["key"].replace("/", "_")
            for name in NAMES:
                ia.merge_range([str(dirs[r["key"]]) for r in ins], name, out)
            dirs[m["key"]] = out
        stacks[s["id"]] = [(r, dirs[r["key"]]) for r in live]
    return {"root": root, "scans": scans, "paths": paths, "base": base, "full": full, "runs": runs, "stacks": stacks, "docs": docs}


# ── Base + runs, combined: the versions ────────────────────────────────────


def _rows(d: Path, t: str) -> list[dict]:
    """A dir's `t` rows in the served columns, `usr` '' where no owner is named (the base's NULL, a run's '')."""
    cols = ist.SUB_SCHEMA[t].names
    tb = pa.concat_tables([pq.read_table(p, columns=cols) for p in sorted((d / t).glob("r*.parquet"))])
    rows = tb.to_pylist()
    for r in rows:
        if "usr" in r and r["usr"] is None:
            r["usr"] = ""
    return rows


def combine(tiers: list[list[dict]], t: str) -> list[dict]:
    """Tiers' rows (base first): one per version (`IDENT`), the smallest `vt` (the reader's `interval_read.combine`)."""
    best: dict[tuple, dict] = {}
    for rows in tiers:
        for r in rows:
            k = tuple(r[c] for c in ia.IDENT[t])
            if k not in best or r["vt"] < best[k]["vt"]:
                best[k] = r
    return sorted(best.values(), key=lambda r: tuple(r[c] for c in ia.IDENT[t]))


def versions(base: Path, run_dirs: list[Path], t: str) -> list[dict]:
    return combine([_rows(base, t), *(_rows(d, t) for d in run_dirs)], t)


@pytest.mark.parametrize("t", ia.TABLES)
def test_each_run_alone_plus_the_base_is_a_full_rebuild(world, t):
    """Base + the level-0 runs through scan D (no merges) equals `build`/`fold` over every scan through D, row for
    row (the exact `wts` carried as the build carries it)."""
    appended = [s["id"] for s in world["scans"][BASE:]]
    for j, d in enumerate(appended):
        got = versions(world["base"], [world["runs"][x] for x in appended[:j + 1]], t)
        want = combine([_rows(world["full"][d], t)], t)
        assert got == want, (t, d)


@pytest.mark.parametrize("t", ia.TABLES)
def test_merged_runs_plus_the_base_are_a_full_rebuild(world, t):
    """…and with the runs merged on the binary counter, as each scan's manifest lists them."""
    shapes = {}
    for d, stack in world["stacks"].items():
        shapes[d] = [(r["key"], r["level"]) for r, _ in stack]
        got = versions(world["base"], [p for _, p in stack], t)
        assert got == combine([_rows(world["full"][d], t)], t), (t, d)
    assert shapes == {
        "2026-08-04": [("deltas/2026-08-04", 0)],
        "2026-08-04T1236": [("deltas/2026-08-04_2026-08-04T1236", 1)],
        "2026-08-05": [("deltas/2026-08-04_2026-08-04T1236", 1), ("deltas/2026-08-05", 0)],
        "2026-08-07": [("deltas/2026-08-04_2026-08-07", 2)],
    }


def test_a_merged_run_is_its_inputs_combined(world):
    """A merge keeps one row per version (the smallest `vt`, `op` 1 if any input opened it): a version opened in one
    run and closed in a later one is a single closed row; nothing else changes."""
    stack = world["stacks"]["2026-08-07"]
    (_, merged), = stack
    ins = [world["runs"][s["id"]] for s in world["scans"][BASE:]]
    for t in ia.TABLES:
        cols = ia.DELTA_SCHEMA[t].names
        rows = lambda d: pa.concat_tables([pq.read_table(p) for p in sorted((d / t).glob("r*.parquet"))]).select(cols).to_pylist()  # noqa: E731
        want: dict[tuple, dict] = {}
        for d in ins:
            for r in rows(d):
                k = tuple(r[c] for c in ia.IDENT[t])
                w = want.get(k)
                want[k] = r if w is None else {**w, "vt": min(w["vt"], r["vt"]), "op": max(w["op"], r["op"])}
        assert rows(merged) == [want[k] for k in sorted(want)], t


def test_runs_hold_the_scans_changes_only(world):
    """A run's delta: the versions opened at its scan (`op` 1, `vf` = D, open) and close records (`op` −1, `vt` = D),
    and its counts say so; the open state is the scan's rows (`append_range` checks it, or raises)."""
    for s in world["scans"][BASE:]:
        D = s["ts"]
        for t in ia.TABLES:
            rows = pa.concat_tables([pq.read_table(p) for p in sorted((world["runs"][s["id"]] / t).glob("r*.parquet"))]).to_pylist()
            assert sorted({(r["op"], r["vf"] == D, r["vt"] == D, r["vt"] == sn.OPEN) for r in rows}) == [(-1, False, True, False), (1, True, False, True)]
            doc = [x[t] for x in world["docs"][s["id"]]]
            assert [sum(x["opened"] for x in doc), sum(x["closed"] for x in doc), sum(x["delta"] for x in doc)] == \
                [sum(r["op"] == 1 for r in rows), sum(r["op"] == -1 for r in rows), len(rows)]


def test_slices_live_at_each_scan_are_the_scans_owner_slices(world):
    """Over base + runs, each appended scan's live owner slices are its rows summed per `(depth, path, usr)`, and each
    slice piece carries its path's total then."""
    root = world["root"]
    for s in world["scans"][BASE:]:
        stack = [p for _, p in world["stacks"][s["id"]]]
        D = s["ts"]
        sv = [r for r in versions(world["base"], stack, "sv") if r["vf"] <= D < r["vt"]]
        got = {(r["depth"], r["path"], r["usr"] or None): {c: r[c] for c in ("kind", "size", "n_files", "wb", "c4", "last_read")} for r in sv}
        want = {k: {c: a[c] for c in ("kind", "size", "n_files", "wb", "c4", "last_read")} for k, a in _slice_oracle(root, s).items()}
        assert got == want, s["id"]
        pv = {(r["depth"], r["path"]): r["size"] for r in versions(world["base"], stack, "pvl") if r["vf"] <= D < r["vt"]}
        svt = [r for r in versions(world["base"], stack, "svt") if r["vf"] <= D < r["vt"]]
        assert sorted((r["depth"], r["path"], r["usr"]) for r in svt) == sorted((r["depth"], r["path"], r["usr"]) for r in sv)
        assert [r for r in svt if r["tot"] != pv[(r["depth"], r["path"])]] == []


# ── Mutations: the comparison catches a broken run ─────────────────────────


def _mutate(rows: list[dict], how: str, D: int) -> list[dict]:
    rows = [dict(r) for r in rows]
    if how == "drop-a-close-record":
        i = next(i for i, r in enumerate(rows) if r["vt"] == D)
        return rows[:i] + rows[i + 1:]
    if how == "drop-an-opened-version":
        i = next(i for i, r in enumerate(rows) if r["vf"] == D)
        return rows[:i] + rows[i + 1:]
    if how == "close-a-day-late":
        i = next(i for i, r in enumerate(rows) if r["vt"] == D)
        rows[i]["vt"] = D + 86_400
        return rows
    if how == "wts-of-the-new-scan":
        # The carry rule broken: an opened version's exact `wts` nudged in its last bits, as a fresh sum would be.
        i = next(i for i, r in enumerate(rows) if r["vf"] == D and r["wts"])
        rows[i]["wts"] = rows[i]["wts"] * (1 + 2 ** -50)
        return rows
    raise ValueError(how)


@pytest.mark.parametrize("how", ["drop-a-close-record", "drop-an-opened-version", "close-a-day-late", "wts-of-the-new-scan"])
@pytest.mark.parametrize("t", ia.TABLES)
def test_a_mutated_run_is_caught(world, t, how):
    s = world["scans"][BASE + 1]
    appended = [x["id"] for x in world["scans"][BASE:BASE + 2]]
    good = [_rows(world["runs"][x], t) for x in appended]
    want = combine([_rows(world["full"][s["id"]], t)], t)
    assert combine([_rows(world["base"], t), *good], t) == want
    bad = [good[0], _mutate(good[1], how, s["ts"])]
    assert combine([_rows(world["base"], t), *bad], t) != want


# ── Views over base + runs (the reference reader, as the Worker plans) ─────


def _cut(rel_root: Path, out: Path) -> Path:
    con = duckdb.connect()
    for sort in ia.RUN_SORTS:
        sub, _, _ = ist.SORTS[sort]
        ist.write_served(con, ia.delta_relation(str(rel_root), sub), sort, out / f"{sort}.parquet", ist.SUB_SCHEMA[sub], rg_rows=4)
    return out


@pytest.fixture(scope="module")
def served(world):
    """The base's served sorts (dyadic segments, 4-row groups) and each run's (`cut_run`'s, 4-row groups)."""
    root = world["root"]
    base_scans = world["scans"][:BASE]
    con = duckdb.connect()
    base = root / "served-base"
    for sort in ("path", "bysize"):
        sub, _, _ = ist.SORTS[sort]
        ist.write_served(con, f"read_parquet('{world['base']}/{sub}/r*.parquet')", sort, base / f"{sort}.parquet", ist.SUB_SCHEMA[sub], rg_rows=4,
                         stamps=[s["ts"] for s in base_scans])
    cut = {}
    for d, stack in world["stacks"].items():
        for r, p in stack:
            if r["key"] not in cut:
                cut[r["key"]] = _cut(p, root / "served-runs" / r["key"].replace("/", "_"))
    single = {s: _cut(p, root / "served-single" / s) for s, p in world["runs"].items()}
    return {"base": base, "cut": cut, "single": single}


def _views_equal(world, store_for, paths: list[str]) -> int:
    from dt_cloud.interval_verify import Scan, compare

    root = world["root"]
    checked = 0
    for s in world["scans"]:
        store = store_for(s)
        scan = Scan(duckdb.connect(), str(root / s["src"]), s["version"], s["src"])
        for path in paths:
            for w, h in ((4, 4), (8, 6), (30, 30)):
                for md in (None, 1):
                    want = scan.view(path, w, h, max_depth=md)
                    got = store.view(s["ts"], path, w, h, max_depth=md)
                    assert compare(got["tree"], want["tree"], with_f=True) == [], (s["id"], path, w, h, md)
                    checked += want["tree"] is not None
    return checked


def test_every_view_at_every_path_and_scan_over_base_and_runs(world, served):
    """The newest manifest's stack (one merged run) and the runs read alone: at every scan, base or appended, every
    path's view (three sizes, whole and one level deep) is the per-scan reference's, tile for tile."""
    from dt_cloud import interval_read as ir

    paths = ["", *sorted({p for p in world["paths"] if "." not in p.rsplit("/", 1)[-1]})]
    last = [served["cut"][r["key"]] for r, _ in world["stacks"]["2026-08-07"]]
    singles = [served["single"][s["id"]] for s in world["scans"][BASE:]]
    n1 = _views_equal(world, lambda s: ir.Store(served["base"], last), paths)
    n2 = _views_equal(world, lambda s: ir.Store(served["base"], singles), paths)
    assert n1 == n2 > 0


def test_a_view_over_runs_missing_a_close_record_differs(world, served, tmp_path):
    """The reader's half of the mutation check: a run whose close records are dropped leaves versions live past their
    end, and the views at that scan change."""
    from dt_cloud import interval_read as ir
    from dt_cloud.interval_verify import Scan, compare

    s = world["scans"][BASE]
    bad = tmp_path / "bad"
    for t in ia.TABLES:
        for p in sorted((world["runs"][s["id"]] / t).glob("r*.parquet")):
            tb = pq.read_table(p)
            (bad / t).mkdir(parents=True, exist_ok=True)
            pq.write_table(tb.filter(pa.compute.equal(tb["op"], 1)), bad / t / p.name)
    store = ir.Store(served["base"], [_cut(bad, tmp_path / "served-bad")])
    scan = Scan(duckdb.connect(), str(world["root"] / s["src"]), s["version"], s["src"])
    paths = ["", *sorted({p for p in world["paths"] if "." not in p.rsplit("/", 1)[-1]})]
    differ = [p for p in paths if compare(store.view(s["ts"], p, 30, 30)["tree"], scan.view(p, 30, 30)["tree"], with_f=True)]
    assert differ == ["", "b1", "b1/d", "b1/d/e", "b1/gof", "b1/gof/e", "b2/e", "b2/e/e", "b2/f", "b2/f/c"]


# ── Deferred carries over real runs (`append_runner.merge_pending`, `carry`) ─


RG = 4


@pytest.fixture(scope="module")
def laid(world, tmp_path_factory) -> Path:
    """The appended scans' level-0 runs laid out as the data bucket holds them under `interval-store/<gen>/` (each run's
    deltas, its served sorts cut at 4-row groups, its `meta.json`), the base's `scans.json`; nothing published."""
    gen = tmp_path_factory.mktemp("laid") / "gen"
    gen.mkdir()
    (gen / "scans.json").write_text(json.dumps({"scans": [{"id": s["id"], "ts": s["ts"]} for s in world["scans"][:BASE]]}) + "\n")
    con = duckdb.connect()
    for s in world["scans"][BASE:]:
        run = {"key": run_key(s["id"], s["id"]), "first": s["id"], "last": s["id"], "level": 0, "scans": [s["id"]]}
        d = gen / run["key"]
        for t in ia.TABLES:
            shutil.copytree(world["runs"][s["id"]] / t, d / t)
        docs = ia.cut_run(con, str(d), d / "served", rg_rows=RG)
        (d / "meta.json").write_text(json.dumps(ia.run_meta("g", run, {s["id"]: s["ts"]}, docs), indent=1) + "\n")
    return gen


def _ivstore(laid: Path, tmp_path: Path, cls=ar.LocalRunStore, **kw) -> ar.LocalRunStore:
    """A fresh copy of the laid-out bucket (publishes and merges write into it)."""
    shutil.copytree(laid, tmp_path / "gen")
    return cls(tmp_path / "gen", tmp_path / "scratch", gen="g", **kw)


def _ivmerge(store, tmp_path, build=None, **kw) -> dict:
    c = ia.carry("g", RANGES["k"], threads=2, mem="1GB", rg_rows=RG)
    if build:
        c = ar.Carry(build=build(c.build), missing=c.missing)
    return ar.merge_pending(store, c, store.root, tmp=tmp_path / "work", owner="test", now=lambda: NOW, log=lambda m: None, **kw)


def _ivstack(store, key: str) -> list[tuple[str, int]]:
    return [(r["key"], r["level"]) for r in store.read_json(key)["runs"]]


def _ids(world) -> list[str]:
    return [s["id"] for s in world["scans"][BASE:]]


def _run_files(d: Path) -> dict[str, bytes]:
    """A run's data files, by run-relative path: its deltas and its served sorts and group indexes (not the cut's timing
    reports, nor `meta.json`)."""
    return {f.relative_to(d).as_posix(): f.read_bytes() for f in sorted(d.rglob("*.parquet"))}


def _pairwise_files(world, served, key: str) -> dict[str, bytes]:
    """The old inline pairwise carries' run `key` (`world`'s `push_run` stacks: deltas merged two at a time, then cut)."""
    d = next(p for st in world["stacks"].values() for r, p in st if r["key"] == key)
    deltas = {f.relative_to(d).as_posix(): f.read_bytes() for t in ia.TABLES for f in sorted((d / t).glob("r*.parquet"))}
    cut = served["cut"][key]
    return {**deltas, **{f"served/{f.name}": f.read_bytes() for f in sorted(cut.glob("*.parquet"))}}


def _old_manifest(store, key: str) -> dict:
    """What the old inline publish wrote for the stack manifest `key` lists: `manifest` over its runs with their
    `meta.json`'s stamps, rows and bytes."""
    runs = []
    for r in store.read_json(key)["runs"]:
        meta = store.read_json(f"{r['key']}/meta.json")
        runs.append({**{k: r[k] for k in ("key", "first", "last", "level", "scans")}, "stamps": meta["stamps"], "rows": meta["rows"], "bytes": meta["bytes"]})
    return ia.manifest("g", store.read_json("scans.json"), runs)


def test_deferred_carries_end_at_the_pairwise_stacks_byte_for_byte(world, served, laid, tmp_path):
    """Each scan published (its level-0 run alone), then the merge stage: after every scan the newest manifest (a scan's,
    or a merge's revision) lists the old inline pairwise stack — the L2 one N-way merge of three runs, not an L1 then an L2
    — each merged run byte for byte the pairwise one's (deltas and served sorts), the manifest the old publish's but for
    its revision fields; and every view over the last stack is the per-scan reference's."""
    from dt_cloud import interval_read as ir

    a, b, c, d = _ids(world)
    store = _ivstore(laid, tmp_path)
    newest, merged = {}, []
    for s in (a, b, c, d):
        doc = ia.publish_run(store, s)
        assert (doc["date"], doc["scans"][-1], [r["key"] for r in doc["runs"]][-1]) == (s, s, f"deltas/{s}")
        merged += _ivmerge(store, tmp_path)["merged"]
        key = ar.latest_key(store.keys("manifests/"))
        newest[s] = (key, _ivstack(store, key))
    ab, ad = run_key(a, b), run_key(a, d)
    assert newest == {
        a: (f"manifests/{a}.json", [(f"deltas/{a}", 0)]),
        b: (f"manifests/{b}.m001.json", [(ab, 1)]),
        c: (f"manifests/{c}.json", [(ab, 1), (f"deltas/{c}", 0)]),
        d: (f"manifests/{d}.m001.json", [(ad, 2)]),
    }
    assert {s: st for s, (_, st) in newest.items()} == {s: [(r["key"], r["level"]) for r, _ in st] for s, st in world["stacks"].items()}
    assert [{k: m[k] for k in ("inputs", "output", "level", "scans", "manifest")} for m in merged] == [
        {"inputs": [f"deltas/{a}", f"deltas/{b}"], "output": ab, "level": 1, "scans": 2, "manifest": f"manifests/{b}.m001.json"},
        {"inputs": [ab, f"deltas/{c}", f"deltas/{d}"], "output": ad, "level": 2, "scans": 4, "manifest": f"manifests/{d}.m001.json"},
    ]
    for key in (ab, ad):
        assert _run_files(store.root / key) == _pairwise_files(world, served, key), key
    assert store.read_json(f"{ad}/meta.json")["stamps"] == {s["id"]: s["ts"] for s in world["scans"][BASE:]}
    for s in (b, d):
        rev = store.read_json(f"manifests/{s}.m001.json")
        assert {k: v for k, v in rev.items() if k not in ("rev", "revises")} == _old_manifest(store, f"manifests/{s}.m001.json")
        assert (rev["rev"], rev["revises"], rev["scans"], rev["stamps"]) == (
            1, f"manifests/{s}.json", store.read_json(f"manifests/{s}.json")["scans"], store.read_json(f"manifests/{s}.json")["stamps"])
    # every manifest ever published stays servable: nothing was deleted
    keys = ar.manifest_keys(store.keys("manifests/"))
    assert keys == [f"manifests/{n}" for n in (f"{a}.json", f"{b}.json", f"{b}.m001.json", f"{c}.json", f"{d}.json", f"{d}.m001.json")]
    assert [(k, ia.missing_files(store.read_json(k)["runs"], store.exists)) for k in keys] == [(k, []) for k in keys]
    paths = ["", *sorted({p for p in world["paths"] if "." not in p.rsplit("/", 1)[-1]})]
    runs = [store.root / r["key"] / "served" for r in store.read_json(keys[-1])["runs"]]
    assert _views_equal(world, lambda s: ir.Store(served["base"], runs), paths) > 0
    assert not (tmp_path / "scratch" / "merge.lease.json").exists()


def test_a_backlog_is_one_n_way_merge(world, served, laid, tmp_path):
    """Four scans published with no merge in between: one plan, one merge of the four level-0 runs into the L2 — the
    pairwise L2, byte for byte."""
    ids = _ids(world)
    store = _ivstore(laid, tmp_path)
    for s in ids:
        ia.publish_run(store, s)
    ad = run_key(ids[0], ids[-1])
    assert _ivmerge(store, tmp_path, dry_run=True) == {"manifest": f"manifests/{ids[-1]}.json",
                                                         "plan": [{"inputs": [f"deltas/{s}" for s in ids], "output": ad, "level": 2}]}
    got = _ivmerge(store, tmp_path)
    assert [(m["inputs"], m["output"], m["manifest"]) for m in got["merged"]] == [([f"deltas/{s}" for s in ids], ad, f"manifests/{ids[-1]}.m001.json")]
    assert _ivstack(store, got["manifest"]) == [(ad, 2)]
    assert _run_files(store.root / ad) == _pairwise_files(world, served, ad)


class FlakyStore(ar.LocalRunStore):
    """Uploads `fail_after` files of a tree, then fails (the job killed mid-upload); or fails creating a revision."""

    def __init__(self, *a, fail_after: int | None = None, fail_create: bool = False, **kw):
        super().__init__(*a, **kw)
        self.fail_after, self.fail_create = fail_after, fail_create

    def upload(self, local: Path, prefix: str) -> list[dict]:
        if self.fail_after is None:
            return super().upload(local, prefix)
        files = sorted((p for p in local.rglob("*") if p.is_file()), key=lambda p: (p == local / "meta.json", p))
        for p in files[:self.fail_after]:
            (self.root / prefix / p.relative_to(local)).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, self.root / prefix / p.relative_to(local))
        raise Boom(f"killed after {self.fail_after} files")

    def create(self, key: str, text: str) -> None:
        if self.fail_create and key.startswith("manifests/") and ".m" in key:
            raise Boom(f"killed before {key}")
        super().create(key, text)


class Boom(Exception):
    pass


@pytest.mark.parametrize("where", ["build", "upload", "revision"])
def test_an_interrupted_merge_leaves_the_store_servable_and_resumes(world, served, laid, tmp_path, where):
    """A merge killed while building, midway through uploading, or before its revision: the manifests are as they were,
    the newest one's runs whole, the lease released. A rerun completes it (reusing a merged dir wholly uploaded), byte for
    byte the pairwise run."""
    a, b = _ids(world)[:2]
    store = _ivstore(laid, tmp_path, FlakyStore, fail_after=5 if where == "upload" else None)
    for s in (a, b):
        ia.publish_run(store, s)
    store.fail_create = where == "revision"
    before = store.listing("manifests/")
    builds = []

    def build(inner):
        def b_(dirs, run, outp, **kw):
            builds.append(run["key"])
            if where == "build" and len(builds) == 1:
                raise Boom("killed while building")
            return inner(dirs, run, outp, **kw)
        return b_

    with pytest.raises(Boom):
        _ivmerge(store, tmp_path, build=build)
    assert store.listing("manifests/") == before
    assert ar.latest_key(store.keys("manifests/")) == f"manifests/{b}.json"
    assert ia.missing_files(store.read_json(f"manifests/{b}.json")["runs"], store.exists) == []
    assert not (tmp_path / "scratch" / "merge.lease.json").exists()
    ab = run_key(a, b)
    out = {"key": ab, "first": a, "last": b, "level": 1, "scans": [a, b]}
    c = ia.carry("g", RANGES["k"])
    assert ar.complete(store, c, out) == (where == "revision")
    assert len(store.keys(f"{ab}/")) == {"build": 0, "upload": 5, "revision": len(store.keys(f"{ab}/"))}[where]

    store.fail_after, store.fail_create = None, False
    got = _ivmerge(store, tmp_path, build=build)
    assert [(m["output"], m["manifest"]) for m in got["merged"]] == [(ab, f"manifests/{b}.m001.json")]
    assert builds == [ab, *([] if where == "revision" else [ab])]
    assert _run_files(store.root / ab) == _pairwise_files(world, served, ab)
    assert _ivstack(store, f"manifests/{b}.m001.json") == [(ab, 1)]


def test_a_merge_refuses_a_dir_holding_foreign_keys_and_a_run_lacking_a_file(world, laid, tmp_path):
    a, b = _ids(world)[:2]
    store = _ivstore(laid, tmp_path)
    for s in (a, b):
        ia.publish_run(store, s)
    ab = run_key(a, b)
    store.create(f"{ab}/stray.json", "{}")
    with pytest.raises(RuntimeError) as e:
        _ivmerge(store, tmp_path)
    assert str(e.value) == f"{ab}/ holds 1 objects this merge doesn't write (e.g. {ab}/stray.json): not merging into it"
    (store.root / ab / "stray.json").unlink()

    def build(inner):
        def b_(dirs, run, outp, **kw):
            doc = inner(dirs, run, outp, **kw)
            (outp / "served" / "slices.groups.parquet").unlink()
            return doc
        return b_

    with pytest.raises(RuntimeError) as e:
        _ivmerge(store, tmp_path, build=build)
    assert str(e.value) == f"not publishing manifests/{b}.m001.json: listed runs lack ['{ab}/served/slices.groups.parquet']"
    assert ar.latest_key(store.keys("manifests/")) == f"manifests/{b}.json"


def test_a_publish_landing_mid_merge_is_rebased_onto(world, laid, tmp_path):
    """The third and fourth scans publish while the L1 merge builds: its revision is of the fourth's manifest, then the
    next carry (the L1 and the two L0s, one merge) is planned from it."""
    a, b, c, d = _ids(world)
    store = _ivstore(laid, tmp_path)
    for s in (a, b):
        ia.publish_run(store, s)

    def build(inner):
        def b_(dirs, run, outp, **kw):
            for s in (c, d):
                if not store.exists(f"manifests/{s}.json"):
                    ia.publish_run(store, s)
            return inner(dirs, run, outp, **kw)
        return b_

    got = _ivmerge(store, tmp_path, build=build)
    ab, ad = run_key(a, b), run_key(a, d)
    assert [(m["output"], m["manifest"]) for m in got["merged"]] == [(ab, f"manifests/{d}.m001.json"), (ad, f"manifests/{d}.m002.json")]
    assert [_ivstack(store, f"manifests/{d}{x}.json") for x in ("", ".m001", ".m002")] == [
        [(f"deltas/{s}", 0) for s in (a, b, c, d)], [(ab, 1), (f"deltas/{c}", 0), (f"deltas/{d}", 0)], [(ad, 2)]]
    assert not store.exists(f"manifests/{b}.m001.json")


def test_one_merger_at_a_time(world, laid, tmp_path):
    """A held lease: nothing merges, nothing is written; a stale one (older than a Batch task can run) is taken over."""
    a, b = _ids(world)[:2]
    store = _ivstore(laid, tmp_path)
    for s in (a, b):
        ia.publish_run(store, s)
    held = {"owner": "other", "at": (NOW - timedelta(hours=1)).isoformat()}
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "merge.lease.json").write_text(json.dumps(held))
    listing = store.listing("")
    assert _ivmerge(store, tmp_path) == {"held": held, "merged": [], "manifest": f"manifests/{b}.json"}
    assert store.listing("") == listing
    (tmp_path / "scratch" / "merge.lease.json").write_text(json.dumps({"owner": "other", "at": (NOW - timedelta(seconds=ar.LEASE_S + 1)).isoformat()}))
    assert [m["manifest"] for m in _ivmerge(store, tmp_path)["merged"]] == [f"manifests/{b}.m001.json"]
    assert not (tmp_path / "scratch" / "merge.lease.json").exists()


def test_publish_refuses_a_rewrite_an_old_scan_and_a_run_without_its_files(world, laid, tmp_path):
    a, b, c = _ids(world)[:3]
    store = _ivstore(laid, tmp_path)
    ia.publish_run(store, b)
    with pytest.raises(SystemExit) as e:
        ia.publish_run(store, b)
    assert str(e.value) == f"manifests/{b}.json exists: manifests are never rewritten"
    with pytest.raises(SystemExit) as e:
        ia.publish_run(store, a)
    assert str(e.value) == f"{a} is not past the generation's newest scan {b}"
    (store.root / f"deltas/{c}/served/bysize.parquet").unlink()
    with pytest.raises(SystemExit) as e:
        ia.publish_run(store, c)
    assert str(e.value) == f"not publishing manifests/{c}.json: listed runs lack ['deltas/{c}/served/bysize.parquet']"
    assert ar.manifest_keys(store.keys("manifests/")) == [f"manifests/{b}.json"]


# ── The chain: order, stages, jobs ─────────────────────────────────────────


def test_prune_plan_keeps_the_newest_complete_state_only():
    pre = "interval-store/g"
    objs = [(f"{pre}/state/{s}/{t}/r{i:04d}.parquet", 10) for s in ("2026-08-04", "2026-08-05") for t in ia.TABLES for i in range(2)]
    plan = ia.prune_plan(objs, pre, 2, True, "2026-08-05")
    assert {k: plan[k] for k in ("keep", "delete")} == {"keep": ["2026-08-05"], "delete": [{"scan": "2026-08-04", "objects": 6, "bytes": 60}]}
    assert plan["names"] == sorted(n for n, _ in objs if "/2026-08-04/" in n)
    with pytest.raises(ia.StateIncomplete, match=r"state/2026-08-05 incomplete: 1 of 2 ranges without svt"):
        ia.prune_plan(objs[:-1], pre, 2, True, "2026-08-05")
    with pytest.raises(ia.StateIncomplete, match="no manifest"):
        ia.prune_plan(objs, pre, 2, False, "2026-08-05")


def test_manifest_lists_the_runs_and_their_scans_stamps():
    base = {"scans": [{"id": "2026-08-03"}]}
    runs = [{"key": "deltas/2026-08-04_2026-08-04T1236", "first": "2026-08-04", "last": "2026-08-04T1236", "level": 1,
             "scans": ["2026-08-04", "2026-08-04T1236"], "stamps": {"2026-08-04T1236": 20, "2026-08-04": 10}, "rows": 5, "bytes": 9}]
    assert ia.manifest("g", base, runs) == {
        "gen": "g", "date": "2026-08-04T1236", "base_scans": 1, "scans": ["2026-08-03", "2026-08-04", "2026-08-04T1236"],
        "stamps": {"2026-08-04": 10, "2026-08-04T1236": 20},
        "runs": [{"key": "deltas/2026-08-04_2026-08-04T1236", "first": "2026-08-04", "last": "2026-08-04T1236", "level": 1,
                  "scans": ["2026-08-04", "2026-08-04T1236"], "rows": 5, "bytes": 9}],
    }
    assert ia.missing_files([{"key": "deltas/x"}], lambda k: not k.endswith("slices.groups.parquet")) == ["deltas/x/served/slices.groups.parquet"]


def _cfg(**kw) -> ia.Config:
    p = Profile(name="t", layouts=("listing/{id}/path-index.parquet",), bucket="data", scratch="scr", gen="g1", r2_bucket="r2b",
                r2_secrets={"key_id": "kid", "secret": "sec"}, r2_endpoint="https://r2", project="proj", region="us-east1", image="img",
                sa="sa@x", append_tasks=2, **kw)
    return ia.Config(p, "g0")


G1 = "interval-store/g1"
NOW = datetime(2026, 8, 5, 1, 2, 3, tzinfo=timezone.utc)


def _l0(*ids: str) -> list[dict]:
    return [{"key": f"deltas/{d}", "first": d, "last": d, "level": 0, "scans": [d]} for d in ids]


class Fake:
    """The data bucket as a dict of keys, Batch as a log of submitted jobs whose stages write their outputs: `ranges` its
    range docs, `publish` `manifests/<D>.json` (the newest earlier manifest's runs + D's at level 0), `merge` what
    `carry` writes (each due carry's `meta.json`, then a revision of the newest manifest). `fail`: stages whose job fails."""

    def __init__(self, published: list[str], fail: tuple = (), **cfg):
        self.cfg = cfg
        self.keys: dict[str, dict] = {
            f"{G1}/scans.json": {"scans": [{"id": "2026-08-03"}]},
            "interval-store/g0/ranges.json": {"k": 4},
        }
        self.published, self.fail = published, fail
        self.jobs: list[tuple[str, dict]] = []
        self.log: list[str] = []

    def manifests(self) -> list[str]:
        return ar.manifest_keys([k.removeprefix(f"{G1}/") for k in self.keys if k.startswith(f"{G1}/manifests/")])

    def stack(self, name: str) -> list[tuple[str, int]]:
        return [(r["key"], r["level"]) for r in self.keys[f"{G1}/manifests/{name}"]["runs"]]

    def run_job(self, name: str, spec: dict, wait: float | None = None) -> None:
        self.jobs.append((name, spec))
        stage = spec["labels"]["stage"]
        if stage in self.fail:
            raise RuntimeError(f"Batch job {name}: FAILED")
        words = _runnables(spec)[0][1].split()
        d = words[words.index("-d") + 1] if "-d" in words else None
        if stage == "ranges":
            for i in range(4):
                self.keys[f"{G1}/deltas/{d}/ranges/r{i:04d}.json"] = {}
        elif stage == "publish":
            prev = ar.latest_key(self.manifests(), before=d)
            runs = self.keys[f"{G1}/{prev}"]["runs"] if prev else []
            self.keys[f"{G1}/manifests/{d}.json"] = {"date": d, "runs": [*runs, *_l0(d)]}
        elif stage == "merge":
            level = parse_compact_level("-L", words[words.index("-L") + 1])
            while True:
                key = self.manifests()[-1]
                m = self.keys[f"{G1}/{key}"]
                _, merges = ar.plan_carries(m["runs"], max_level=level)
                if not merges:
                    break
                ins, out = merges[0]
                self.keys[f"{G1}/{out['key']}/meta.json"] = {"scans": out["scans"]}
                scan, rev = ar.parse_manifest(key.removeprefix("manifests/"))
                self.keys[f"{G1}/manifests/{ar.manifest_name(scan, rev + 1)}"] = {**m, "runs": ar.rebase(m["runs"], [r["key"] for r in ins], out)}
            if wait == 0:
                raise ar.StillRunning(f"Batch job {name}: still running after 0s")

    def runner(self, dry_run: bool = False, **kw) -> ia.Runner:
        return ia.Runner(
            cfg=_cfg(**self.cfg), exists=lambda k: k in self.keys,
            count=lambda prefix, suffix: sum(1 for k in self.keys if k.startswith(prefix) and k.endswith(suffix)),
            read_json=lambda k: self.keys[k], list_keys=lambda prefix: [k for k in self.keys if k.startswith(prefix)],
            published=lambda layouts, start: [s for s in self.published if s >= start], run_job=self.run_job,
            prepare=lambda d: self.keys.__setitem__(f"{G1}/deltas/{d}/scans.json", {}),
            prune=lambda d: self.log.append(f"prune {d}"), log=self.log.append, dry_run=dry_run, now=lambda: NOW, **kw,
        )

    def calls(self) -> list[tuple[str, str]]:
        """Each job's stage and its runnables' commands, past the common prefix."""
        return [(spec["labels"]["stage"], " ; ".join(c[1].removeprefix(f"{PREP} ") for c in _runnables(spec))) for _, spec in self.jobs]


COMMON = "-b data -g g1 -R g0 -S scr"


def _ranges(d: str) -> tuple[str, str]:
    return ("ranges", f"ranges -d {d} -n 2 -M 90GB -p 16 {COMMON} -m /gcs/data")


def _publish_r2(d: str) -> tuple[str, str]:
    return ("publish", f"publish -d {d} -M 90GB -p 16 -s {COMMON} -m /gcs/data ; r2 -d {d} {COMMON}")


def _r2(d: str, manifest: str | None = None) -> tuple[str, str]:
    return ("r2", f"r2 -d {d}{f' -m {manifest}' if manifest else ''} {COMMON}")


MERGE = ("merge", f"carry -L 5 -M 90GB -p 16 {COMMON} -m /gcs/data")


def test_the_chain_appends_strictly_in_scan_id_order():
    f = Fake(["2026-08-03", "2026-08-04", "2026-08-04T1236"])
    with pytest.raises(ar.NotNext, match=r"1 earlier published scan\(s\) pending: 2026-08-04"):
        f.runner().run("2026-08-04T1236")
    with pytest.raises(ar.NotNext, match="2026-08-05 is not published"):
        f.runner().run("2026-08-05")
    assert f.runner().run("2026-08-04T1236", catch_up=True) == ["2026-08-04", "2026-08-04T1236"]
    # Each scan publishes its own level-0 run; the merge stage, once after both, submits the carry (not waited on).
    assert f.calls() == [_ranges("2026-08-04"), _publish_r2("2026-08-04"), _ranges("2026-08-04T1236"), _publish_r2("2026-08-04T1236"), MERGE]
    assert [n for n, _ in f.jobs] == ["iv-ranges-2026-08-04-010203", "iv-publish-2026-08-04-010203", "iv-ranges-2026-08-04-1236-010203",
                                      "iv-publish-2026-08-04-1236-010203", "iv-merge-2026-08-04-1236-010203"]
    assert [x for x in f.log if x.startswith("prune")] == ["prune 2026-08-04", "prune 2026-08-04T1236"]
    assert f.manifests() == ["manifests/2026-08-04.json", "manifests/2026-08-04T1236.json", "manifests/2026-08-04T1236.m001.json"]
    assert [f.stack(n.removeprefix("manifests/")) for n in f.manifests()] == [
        _stack(_l0("2026-08-04")), _stack(_l0("2026-08-04", "2026-08-04T1236")), [("deltas/2026-08-04_2026-08-04T1236", 1)]]
    # Done: a rerun only re-copies and prunes, then copies the revision the detached merge published.
    f.jobs.clear()
    assert f.runner().run("2026-08-04T1236") == []
    assert f.calls() == [_r2("2026-08-04T1236"), _r2("2026-08-04T1236", "2026-08-04T1236.m001")]


def _stack(runs: list[dict]) -> list[tuple[str, int]]:
    return [(r["key"], r["level"]) for r in runs]


def test_the_chain_resumes_at_the_first_missing_stage():
    f = Fake(["2026-08-03", "2026-08-04"])
    f.keys[f"{G1}/deltas/2026-08-04/scans.json"] = {}
    for i in range(4):
        f.keys[f"{G1}/deltas/2026-08-04/ranges/r{i:04d}.json"] = {}
    f.runner().run("2026-08-04")
    assert f.calls() == [_publish_r2("2026-08-04")]
    # Published but the copy not known done (a rerun): the copy alone, its own job; no carry due, no merge job.
    f.jobs.clear()
    f.runner().run("2026-08-04")
    assert f.calls() == [_r2("2026-08-04")]


SCANS = ["2026-08-04", "2026-08-04T1236", "2026-08-05", "2026-08-07"]


def test_publish_adds_level_0_runs_and_the_merge_stage_carries_them_later():
    """Four scans with the merge stage off (`-M`): each publish adds only its level-0 run. The stage alone (`interval-store
    merge`, waited on) then submits one merge job — the four runs into one L2, one N-way merge — and the R2 job for the
    revision it published."""
    f = Fake(["2026-08-03", *SCANS])
    for d in SCANS:
        assert f.runner(merge=False).run(d) == [d]
    assert [f.stack(f"{d}.json") for d in SCANS] == [_stack(_l0(*SCANS[:i + 1])) for i in range(4)]
    assert [c for c in f.calls() if c[0] == "merge"] == []
    f.jobs.clear()
    f.runner(merge_wait=None).carries()
    assert f.calls() == [MERGE, _r2(SCANS[-1], f"{SCANS[-1]}.m001")]
    assert f.stack(f"{SCANS[-1]}.m001.json") == [(f"deltas/{SCANS[0]}_{SCANS[-1]}", 2)]


def test_a_waited_merge_reaches_r2_in_the_same_run_and_a_detached_one_with_the_next_scan():
    f = Fake(["2026-08-03", *SCANS[:3]])
    assert f.runner(merge_wait=None).run(SCANS[1], catch_up=True) == SCANS[:2]
    ab = f"deltas/{SCANS[0]}_{SCANS[1]}"
    assert f.calls()[-2:] == [MERGE, _r2(SCANS[1], f"{SCANS[1]}.m001")]
    g = Fake(["2026-08-03", *SCANS[:3]])
    g.runner().run(SCANS[1], catch_up=True)
    assert g.calls()[-1] == MERGE
    assert [m for m in g.log if "still running" in m] == [
        "merge: Batch job iv-merge-2026-08-04-1236-010203: still running after 0s; it publishes its revision on GCS when done, "
        "and R2 gets it with a later run"]
    # The next scan's manifest builds on the revision: its R2 copy carries the merged run.
    g.jobs.clear()
    assert g.runner().run(SCANS[2]) == [SCANS[2]]
    assert g.calls() == [_ranges(SCANS[2]), _publish_r2(SCANS[2])]
    assert g.stack(f"{SCANS[2]}.json") == [(ab, 1), (f"deltas/{SCANS[2]}", 0)]


def test_a_failed_merge_never_fails_the_run():
    f = Fake(["2026-08-03", *SCANS[:2]], fail=("merge",))
    assert f.runner(merge_wait=None).run(SCANS[1], catch_up=True) == SCANS[:2]
    assert f.calls()[-1] == MERGE
    assert [m for m in f.log if m.startswith("merge: failed")] == [
        "merge: failed, not fatal (every listed run is whole; the next run plans again): Batch job iv-merge-2026-08-04-1236-010203: FAILED"]
    assert f.manifests() == [f"manifests/{d}.json" for d in SCANS[:2]]
    # A rerun (already appended) plans the carry again.
    f.jobs.clear()
    f.fail = ()
    assert f.runner(merge_wait=None).run(SCANS[1]) == []
    assert f.calls() == [_r2(SCANS[1]), MERGE, _r2(SCANS[1], f"{SCANS[1]}.m001")]


def test_level_5_is_left_to_a_compaction():
    """Two L4s never carry into an L5: the merge stage submits nothing and says a compaction is due."""
    f = Fake(["2026-08-03"])
    l4 = lambda a, b: {"key": f"deltas/{a}_{b}", "first": a, "last": b, "level": 4, "scans": [a, b]}  # noqa: E731
    f.keys[f"{G1}/manifests/2026-08-07.json"] = {"date": "2026-08-07", "runs": [l4("2026-08-04", "2026-08-05"), l4("2026-08-06", "2026-08-07")]}
    f.runner(merge_wait=None).carries()
    assert (f.jobs, [m for m in f.log if m.startswith("merge")]) == (
        [], ["merge: level 5 is due: compact into a new base generation (carries stop below it)"])


def test_the_cli_merge_and_append_M(monkeypatch):
    """`interval-store merge` exits 1 on a failure (the store as it was) and 0 once merged; `append -M` skips the stage;
    `append` turns `NotNext` into exit 3."""
    from click.testing import CliRunner

    f = Fake(["2026-08-03", *SCANS[:2]])
    f.runner(merge=False).run(SCANS[1], catch_up=True)
    seen, errs = [], []
    monkeypatch.setattr(ia, "ready", lambda profile: _cfg())
    monkeypatch.setattr(ia, "gcs_runner", lambda cfg, **kw: seen.append(kw) or f.runner(**{k: v for k, v in kw.items() if k != "dry_run"}))
    monkeypatch.setattr(ia, "err", errs.append)
    f.fail, f.jobs = ("merge",), []
    r = CliRunner().invoke(ia.merge_cmd, [])
    assert (r.exit_code, errs) == (1, ["interval-store merge: Batch job iv-merge-2026-08-04-1236-010203: FAILED"])
    f.fail, f.jobs = (), []
    r = CliRunner().invoke(ia.merge_cmd, ["-w", "600"])
    assert (r.exit_code, f.calls()) == (0, [MERGE, _r2(SCANS[1], f"{SCANS[1]}.m001")])
    assert json.loads(r.output) == {"gen": "g1", "manifest": f"{G1}/manifests/{SCANS[1]}.m001.json", "dry_run": False}
    f.jobs = []
    r = CliRunner().invoke(ia.append_cmd, ["-M", SCANS[1]])
    assert (r.exit_code, f.calls()) == (0, [_r2(SCANS[1])])
    assert seen == [{"dry_run": False, "merge_wait": None}, {"dry_run": False, "merge_wait": 600.0},
                    {"dry_run": False, "merge": False, "merge_wait": 0}]
    errs.clear()
    r = CliRunner().invoke(ia.append_cmd, ["2026-08-09"])
    assert (r.exit_code, errs) == (3, ["interval-store append 2026-08-09: 2026-08-09 is not published (no path sort under the generation's layouts)"])


def test_jobs_are_the_profiles_and_carry_the_cost_label(monkeypatch):
    monkeypatch.setenv("DISKY_LABELS", "app=disky,deployment=t")
    f = Fake(["2026-08-03", "2026-08-04", "2026-08-04T1236"])
    f.runner().run("2026-08-04")
    f.runner().run("2026-08-04")  # published: the copy's own job
    f.runner().run("2026-08-04T1236")  # then a carry: the merge job
    (_, ranges), (_, publish), (_, r2), _, _, (merge_name, merge) = f.jobs
    want = {"app": "disky", "deployment": "t", "component": "interval-store"}
    for spec in (ranges, publish, r2, merge):
        assert spec["allocationPolicy"]["labels"] == want
        assert {k: spec["labels"][k] for k in ("purpose", "component", "gen")} == {"purpose": "interval-store", "component": "interval-store", "gen": "g1"}
    cmd = lambda s: s["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1]  # noqa: E731
    assert [s["taskGroups"][0]["taskCount"] for s in (ranges, publish, r2, merge)] == [2, 1, 1, 1]
    assert cmd(ranges) == ("set -euo pipefail; mkdir -p /stage/tmp /stage/out && cd /stage && python3 -u -m dt_cloud.interval_append ranges "
                           "-d 2026-08-04 -n 2 -M 90GB -p 16 -b data -g g1 -R g0 -S scr -m /gcs/data")
    assert cmd(r2) == ("set -euo pipefail; mkdir -p /stage/tmp /stage/out && cd /stage && python3 -u -m dt_cloud.interval_append r2 "
                       "-d 2026-08-04 -b data -g g1 -R g0 -S scr")
    assert cmd(merge) == ("set -euo pipefail; mkdir -p /stage/tmp /stage/out && cd /stage && python3 -u -m dt_cloud.interval_append carry "
                          "-L 5 -M 90GB -p 16 -b data -g g1 -R g0 -S scr -m /gcs/data")
    assert r2["taskGroups"][0]["taskSpec"]["environment"] == R2_ENV
    # the merge job: as the stages' account (the R2 account is the same), with the R2 env for its status record on R2,
    # and its own name (the record's holder)
    assert (merge["allocationPolicy"]["serviceAccount"]["email"], merge["allocationPolicy"]["instances"][0]["policy"]["machineType"],
            merge["taskGroups"][0]["taskSpec"]["environment"]) == (
        "sa@x", "n2-highmem-16", {**R2_ENV, "variables": {**R2_ENV["variables"], "DT_JOB_NAME": merge_name}})


def test_the_merge_job_gets_no_r2_env_when_the_r2_account_differs():
    """A job runs as one account: the merge's must write the data bucket, so when the R2 copy's account is another, the
    merge runs without the R2 env (its status record isn't written; /health doesn't see it)."""
    f = Fake(["2026-08-03", "2026-08-04", "2026-08-04T1236"], r2_sa="r2@x")
    f.runner().run("2026-08-04")
    f.runner().run("2026-08-04T1236")
    merge_name, merge = next((n, s) for n, s in f.jobs if s["labels"]["stage"] == "merge")
    assert (merge["allocationPolicy"]["serviceAccount"]["email"], merge["taskGroups"][0]["taskSpec"]["environment"]) == (
        "sa@x", {"variables": {"STATIC_NAMES_BUCKET": "data", "STATIC_NAMES_SCRATCH": "scr", "DT_JOB_NAME": merge_name}})


def test_the_profile_drives_the_append(tmp_path):
    doc = {"bucket": "data", "append": {"gen": "g1", "ranges_gen": "g0", "scratch": "scr", "layouts": ["listing/{id}/path-index.parquet"],
                                        "region": "r", "image": "img", "sa": "sa", "r2_bucket": "r2b"}}
    p = tmp_path / "x.json"
    p.write_text(json.dumps(doc))
    cfg = ia.load_config(str(p), {"INTERVAL_STORE_SRC": "/gcs/data/src/a, /gcs/data/src/b", "INTERVAL_STORE_GEN": "g2"})
    assert [cfg.ranges_gen, cfg.p.gen, cfg.p.bucket, cfg.p.layouts, cfg.p.src] == ["g0", "g2", "data", ("listing/{id}/path-index.parquet",), ("/gcs/data/src/a", "/gcs/data/src/b")]
    p.write_text(json.dumps({"bucket": "data", "append": {k: v for k, v in doc["append"].items() if k != "image"}}))
    with pytest.raises(SystemExit, match=r"no image in the profile's `append` section \(or \$INTERVAL_STORE_IMAGE\)"):
        ia.load_config(str(p), {})
    with pytest.raises(SystemExit, match=r"no image"):  # the gcs profile pins none: the caller passes its job image
        ia.load_config("gcs", {})
    gcs = ia.load_config("gcs", {"INTERVAL_STORE_IMAGE": "img"})
    assert [gcs.p.gen, gcs.ranges_gen, gcs.p.bucket] == ["2026-10-09b", "2026-10-09", "oa-gcs-usage-dvx"]


PREP = "set -euo pipefail; mkdir -p /stage/tmp /stage/out && cd /stage && python3 -u -m dt_cloud.interval_append"
R2_ENV = {
    "variables": {"STATIC_NAMES_BUCKET": "data", "STATIC_NAMES_SCRATCH": "scr", "R2_BUCKET": "r2b", "R2_ENDPOINT": "https://r2"},
    "secretVariables": {"R2_ACCESS_KEY_ID": "projects/proj/secrets/kid/versions/latest", "R2_SECRET_ACCESS_KEY": "projects/proj/secrets/sec/versions/latest"},
}


def _runnables(spec: dict) -> list[str]:
    return [r["container"]["commands"] for r in spec["taskGroups"][0]["taskSpec"]["runnables"]]


def test_publish_and_the_r2_copy_are_one_task_in_order():
    f = Fake(["2026-08-03", "2026-08-04"])
    f.runner().run("2026-08-04")
    assert [n for n, _ in f.jobs] == ["iv-ranges-2026-08-04-010203", "iv-publish-2026-08-04-010203"]
    _, spec = f.jobs[1]
    tg = spec["taskGroups"][0]
    # Runnables run in order and stop at a failure: the publish (its run's cut, its check, the manifest last), then the
    # copy (the runs, its check, the manifest last). A retried task skips the written manifest (`-s`).
    assert _runnables(spec) == [
        ["-c", f"{PREP} publish -d 2026-08-04 -M 90GB -p 16 -s -b data -g g1 -R g0 -S scr -m /gcs/data"],
        ["-c", f"{PREP} r2 -d 2026-08-04 -b data -g g1 -R g0 -S scr"],
    ]
    assert [r["container"]["imageUri"] for r in tg["taskSpec"]["runnables"]] == ["img", "img"]
    assert [tg["taskCount"], tg["taskSpec"]["maxRetryCount"], tg["taskSpec"]["environment"]] == [1, 3, R2_ENV]
    assert [spec["labels"]["stage"], spec["allocationPolicy"]["serviceAccount"]["email"],
            spec["allocationPolicy"]["instances"][0]["policy"]["machineType"]] == ["publish", "sa@x", "n2-highmem-16"]


def test_publish_and_r2_stay_two_jobs_when_the_r2_account_differs():
    f = Fake(["2026-08-03", "2026-08-04"], r2_sa="r2@x")
    f.runner().run("2026-08-04")
    (_, ranges), (_, publish), (_, r2) = f.jobs
    assert [s["labels"]["stage"] for s in (ranges, publish, r2)] == ["ranges", "publish", "r2"]
    assert [s["allocationPolicy"]["serviceAccount"]["email"] for s in (publish, r2)] == ["sa@x", "r2@x"]
    assert _runnables(publish) == [["-c", f"{PREP} publish -d 2026-08-04 -M 90GB -p 16 -b data -g g1 -R g0 -S scr -m /gcs/data"]]
    assert "secretVariables" not in publish["taskGroups"][0]["taskSpec"]["environment"]


def test_ranges_are_assigned_longest_first_to_the_least_loaded_task():
    #           r0 r1 r2 r3 r4 r5 r6 r7
    counts = [8, 1, 1, 7, 2, 2, 3, 6]
    plan = ia.assign_ranges(8, 3, counts)
    assert plan == [[0, 5], [1, 3, 4], [2, 6, 7]]
    assert [sum(counts[i] for i in p) for p in plan] == [10, 10, 10]
    # The measured shape: the big ranges contiguous (r0 and r1 of 8 over 4 tasks). Blocks would put both in task 0.
    counts = [8, 8, 1, 1, 1, 1, 1, 1]
    assert ia.assign_ranges(8, 4, counts) == [[0], [1], [2, 4, 6], [3, 5, 7]]
    # Ties go to the lower range, then the lower task: equal counts are the interleave.
    assert ia.assign_ranges(7, 3, [5] * 7) == [[0, 3, 6], [1, 4], [2, 5]]
    # No counts (the base, a run before them): interleaved.
    assert ia.assign_ranges(10, 4, None) == [[0, 4, 8], [1, 5, 9], [2, 6], [3, 7]]
    # Every range exactly once, more tasks than ranges included.
    assert ia.assign_ranges(2, 3, [1, 2]) == [[1], [0], []]
    with pytest.raises(ValueError, match="3 counts for 4 ranges"):
        ia.assign_ranges(4, 2, [1, 2, 3])


def test_a_ranges_cost_is_its_open_rows_from_the_previous_scans_docs():
    doc = lambda n: {t: {"opened": 0, "closed": 0, "delta": 0, "open": n * (j + 1)} for j, t in enumerate(ia.TABLES)}  # noqa: E731
    docs = {"r0000": doc(1), "r0001": doc(10), "r0002": doc(100)}
    assert ia.open_counts(docs, 3) == [6, 60, 600]
    assert ia.open_counts({k: v for k, v in docs.items() if k != "r0001"}, 3) is None
    assert ia.open_counts({**docs, "r0002": {"pvl": {"open": 1}}}, 3) is None

    class Blob:
        def __init__(self, name):
            self.name = name

        def download_as_bytes(self):
            return json.dumps(docs[Path(self.name).stem]).encode()

    class Gcs:
        listed: list[tuple[str, str]] = []

        def list_blobs(self, bucket, prefix):
            self.listed.append((bucket, prefix))
            return [Blob(f"{prefix}{n}.json") for n in reversed(sorted(docs))]

        def bucket(self, name):
            assert name == "data"
            return type("B", (), {"blob": lambda _, n: Blob(n)})()

    g = Gcs()
    assert ia.prev_open_counts(g, "data", "interval-store/g1", "2026-08-04T1236", 3) == [6, 60, 600]
    assert g.listed == [("data", "interval-store/g1/deltas/2026-08-04T1236/ranges/")]
    assert ia.prev_open_counts(g, "data", "interval-store/g1", "2026-08-04T1236", 4) is None


class PruneGcs:
    """The data bucket's manifests and the scratch bucket's state objects; deletes recorded per bucket. The first
    `barrier` deletes wait for each other: run one at a time, they time out."""

    def __init__(self, objects: list[str], manifests: list[str], barrier: int, gone: set[str] = frozenset(), timeout: float = 5):
        self.objects, self.manifests, self.gone, self.timeout = objects, manifests, set(gone), timeout
        self.deleted: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(barrier)
        self.n = 0

    def list_blobs(self, bucket, prefix):
        assert bucket == "scr"
        return [type("O", (), {"name": n, "size": 10})() for n in self.objects if n.startswith(prefix)]

    def bucket(self, bucket):
        gcs = self

        class Blob:
            def __init__(self, name):
                self.name = name

            def exists(self):
                assert bucket == "data"
                return self.name in gcs.manifests

            def delete(self):
                from google.api_core.exceptions import NotFound

                with gcs.lock:
                    i = gcs.n
                    gcs.n += 1
                if i < gcs.barrier.parties:
                    gcs.barrier.wait(timeout=gcs.timeout)
                if self.name in gcs.gone:
                    raise NotFound(self.name)
                with gcs.lock:
                    gcs.deleted.append((bucket, self.name))

        return type("B", (), {"blob": lambda _, n: Blob(n)})()


def test_prune_deletes_the_earlier_states_in_parallel():
    """Only the scratch bucket's earlier states: the merge lease beside them stays, and a merge's revision of the scan's
    manifest changes nothing (the scan's own manifest says it is published)."""
    pre = "interval-store/g1"
    objs = [f"{pre}/state/{s}/{t}/r{i:04d}.parquet" for s in ("2026-08-03", "2026-08-04", "2026-08-05") for t in ia.TABLES for i in range(4)]
    g = PruneGcs([*objs, f"{pre}/merge.lease.json"], [f"{pre}/manifests/2026-08-05.json", f"{pre}/manifests/2026-08-05.m001.json"], barrier=4,
                 gone={f"{pre}/state/2026-08-03/sv/r0002.parquet"})
    doc = ia.prune_state(g, "g1", "2026-08-05", 4, bucket="data", scratch="scr", workers=4)
    old = [n for n in objs if "/2026-08-05/" not in n]
    assert sorted(g.deleted) == sorted(("scr", n) for n in old if n not in g.gone)
    assert doc == {"scan": "2026-08-05", "keep": ["2026-08-05"], "deleted": 24,
                   "delete": [{"scan": "2026-08-03", "objects": 12, "bytes": 120}, {"scan": "2026-08-04", "objects": 12, "bytes": 120}]}
    # One at a time, the deletes the barrier holds never meet.
    g = PruneGcs(objs, [f"{pre}/manifests/2026-08-05.json"], barrier=2, timeout=0.2)
    with pytest.raises(threading.BrokenBarrierError):
        ia.prune_state(g, "g1", "2026-08-05", 4, bucket="data", scratch="scr", workers=1)
    # Incomplete (no manifest): nothing deleted.
    g = PruneGcs(objs, [], barrier=4)
    with pytest.raises(ia.StateIncomplete, match="no manifest"):
        ia.prune_state(g, "g1", "2026-08-05", 4, bucket="data", scratch="scr", workers=4)
    assert g.deleted == []
