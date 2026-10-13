"""The interval store fixture (specs/interval-store.md) for `intervalStore.test.ts`.

Six synthetic scans (three v1 indexes, then three v2 store generations: owner slices, a duplicated v1
row, paths that come and go, read days that move) from `cloud/tests/test_interval_store.py`'s
generator, built into one generation by `dt_cloud.interval_store` (`build_range` over three key
ranges, `write_served` at 4 rows per group) under `interval-store/<GEN>/` here, plus `expected.json`:
per `(date, path, w, h, depth)` the view computed from that scan's own rows by the per-scan reference
(`interval_verify.Scan.view`), flattened (`interval_read.flatten`). The generator asserts the Python
interval reader returns the same trees.

    cloud/.venv-or-root/bin/python site/functions/_lib/fixtures/iv/gen.py [g2 | tiered]

`tiered`: the base + per-scan runs generations (`interval_append`): `g3` / `g4` / `g6` over `g1`'s scans (runs unmerged /
merged pairwise as the old inline publish did / merged by the deferred carries, a revision manifest listing the merged
run), `g5` over `g2`'s.
"""
from __future__ import annotations

import importlib.util
import json
import random
import shutil
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
ROOT = HERE.parents[4]
GEN = "g1"
PATHS = ["", "b1", "b2", "b1/gof", "b2/e"]
SIZES = [(8, 6), (30, 30)]


def main() -> None:
    from dt_cloud import interval_read as ir
    from dt_cloud import interval_store as ist
    from dt_cloud import static_names as sn
    from dt_cloud.interval_verify import Scan

    spec = importlib.util.spec_from_file_location("tis", ROOT / "cloud/tests/test_interval_store.py")
    t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t)

    work = HERE / "work"
    shutil.rmtree(work, ignore_errors=True)
    rng = random.Random(11)
    paths = t._universe(rng)
    scans = []
    for j, d in enumerate(t.DATES):
        v = 2 if j >= t.V2_FROM else 1
        key = f"listing/{d}/path-index.parquet"
        (work / key).parent.mkdir(parents=True, exist_ok=True)
        t._write(t._scan_rows(rng, paths, j), work / key, v)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
    doc = {"bucket": "b", "scans": scans}
    ranges = {"k": 3, "ranges": [{"i": 0, "lo": [0, ""], "hi": [2, "b1/gof"]}, {"i": 1, "lo": [2, "b1/gof"], "hi": [3, ""]}, {"i": 2, "lo": [3, ""], "hi": None}]}
    con = duckdb.connect()
    for i in range(3):
        ist.build_range(doc, ranges, i, work / "out", con, mount=str(work))
    for i in range(3):
        ist.fold_range(str(work / "out"), i, work / "out", con)
    out = HERE / "interval-store" / GEN
    shutil.rmtree(out, ignore_errors=True)
    for sort, (sub, _, _) in ist.SORTS.items():
        schema = ist.SUB_SCHEMA[sub]
        ist.write_served(con, f"read_parquet('{work}/out/{sub}/r*.parquet')", sort, out / "served" / f"{sort}.parquet", schema, rg_rows=4,
                         stamps=[x["ts"] for x in scans])
    (out / "scans.json").write_text(json.dumps({"scans": [{"id": s["id"], "ts": s["ts"]} for s in scans]}, indent=1) + "\n")
    store = ir.Store(out / "served")
    expected = []
    for s in scans:
        scan = Scan(duckdb.connect(), str(work / s["src"]), s["version"], s["src"])
        for p in PATHS:
            for w, h in SIZES:
                for md in (None, 1):
                    want = scan.view(p, w, h, max_depth=md)
                    got = store.view(s["ts"], p, w, h, max_depth=md)
                    from dt_cloud.interval_verify import compare

                    assert compare(got["tree"], want["tree"], with_f=True) == [], (s["id"], p, w, h, md)
                    if want["tree"] is None:
                        continue
                    expected.append({"date": s["id"], "path": p, "w": w, "h": h, "depth": md, "v": s["version"], "tiles": ir.flatten(want["tree"])})
    (HERE / "expected.json").write_text(json.dumps(expected, indent=1, sort_keys=True) + "\n")
    shutil.rmtree(work)
    print(f"{len(expected)} views; files: {sorted(p.name for p in (out / 'served').iterdir())}", file=sys.stderr)
    slices_gen()


#: Base + per-scan runs (`interval_append`) over `g1`'s scans: the base holds the first four, each later scan is a
#: run. `g3`: the newest manifest lists the runs unmerged (each its own tier); `g4`: merged into one, as the old inline
#: publish's binary counter merged two level-0 runs; `g6`: the deferred carries' (`append_runner`): each scan's manifest
#: lists its level-0 run, and a merge's revision `2026-08-05.m001.json` the merged run, byte for byte `g4`'s. All must
#: serve exactly `g1`'s answers (`expected.json`).
TIERED = {"g3": "unmerged", "g4": "pairwise", "g6": "deferred"}


def tiered_gens(mount: Path, work: Path, scans: list[dict], n_base: int, ranges: dict, *, rg_rows: int, gens: dict[str, str] = TIERED) -> None:
    """`gens` (name → `unmerged` | `pairwise` | `deferred`) built over the scans under `mount`, intermediates in `work`."""
    from dt_cloud import append_runner as ar
    from dt_cloud import interval_append as ia
    from dt_cloud import interval_store as ist
    from dt_cloud.static_append import push_run, run_key

    doc = {"bucket": "b", "scans": scans[:n_base]}
    base = work / "base-out"
    con = duckdb.connect()
    for i in range(ranges["k"]):
        ist.build_range(doc, ranges, i, base, con, mount=str(mount))
        ist.fold_range(str(base), i, base, con)
        ist.build_slices_range(doc, ranges, i, base, con, mount=str(mount))
        ist.slice_totals_range(str(base), i, base, con)
    names = [f"r{i:04d}" for i in range(ranges["k"])]
    runs, prev = {}, None
    for s in scans[n_base:]:
        d = work / "runs" / s["id"]
        for i, name in enumerate(names):
            state = ia.run_state_sql(str(prev / "state"), name) if prev else ia.base_state_sql(str(base), name)
            ia.append_range(con, state, s, ranges["ranges"][i], name, d, bucket="b", mount=str(mount))
        runs[s["id"]] = d
        prev = d
    for gen, mode in gens.items():
        out = HERE / "interval-store" / gen
        shutil.rmtree(out, ignore_errors=True)
        for sort in ia.RUN_SORTS:
            sub, _, _ = ist.SORTS[sort]
            ist.write_served(con, f"read_parquet('{base}/{sub}/r*.parquet')", sort, out / "served" / f"{sort}.parquet", ist.SUB_SCHEMA[sub],
                             rg_rows=rg_rows, stamps=[x["ts"] for x in scans[:n_base]])
        (out / "scans.json").write_text(json.dumps({"scans": [{"id": x["id"], "ts": x["ts"]} for x in scans[:n_base]]}, indent=1) + "\n")
        if mode == "deferred":
            # The bucket as the append writes it: each scan's run (deltas, cut, `meta.json`), published, then the merge stage.
            root = work / "deferred" / gen
            root.mkdir(parents=True)
            shutil.copy(out / "scans.json", root / "scans.json")
            store = ar.LocalRunStore(root, work / "deferred-lease" / gen, gen=gen)
            for s in scans[n_base:]:
                run = {"key": run_key(s["id"], s["id"]), "first": s["id"], "last": s["id"], "level": 0, "scans": [s["id"]]}
                for t in ia.TABLES:
                    shutil.copytree(runs[s["id"]] / t, root / run["key"] / t)
                docs = ia.cut_run(con, str(root / run["key"]), root / run["key"] / "served", rg_rows=rg_rows)
                (root / run["key"] / "meta.json").write_text(json.dumps(ia.run_meta(gen, run, {s["id"]: s["ts"]}, docs), indent=1) + "\n")
                ia.publish_run(store, s["id"])
                ar.merge_pending(store, ia.carry(gen, ranges["k"], threads=1, mem="1GB", rg_rows=rg_rows), root, tmp=work / "deferred-tmp",
                                 owner="gen", log=lambda m: None)
            keys = ar.manifest_keys(store.keys("manifests/"))
            for k in keys:
                (out / k).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(root / k, out / k)
            for key in sorted({r["key"] for k in keys for r in store.read_json(k)["runs"]}):
                for f in sorted((root / key / "served").glob("*.parquet")):
                    (out / key / "served").mkdir(parents=True, exist_ok=True)
                    shutil.copy(f, out / key / "served" / f.name)
            g4 = HERE / "interval-store" / "g4"
            for f in sorted(g4.rglob("deltas/*_*/served/*.parquet")):
                assert (out / f.relative_to(g4)).read_bytes() == f.read_bytes(), f.relative_to(g4)
            print(f"{gen}: manifests {[k.removeprefix('manifests/') for k in keys]}", file=sys.stderr)
            continue
        live, dirs, stamps = [], {}, {}
        for s in scans[n_base:]:
            new = {"key": run_key(s["id"], s["id"]), "first": s["id"], "last": s["id"], "scans": [s["id"]]}
            dirs[new["key"]], stamps[new["key"]] = runs[s["id"]], {s["id"]: s["ts"]}
            if mode == "pairwise":
                live, merges = push_run(live, new)
                for ins, m in merges:
                    md = work / "merged" / gen / m["key"]
                    for name in names:
                        ia.merge_range([str(dirs[r["key"]]) for r in ins], name, md)
                    dirs[m["key"]] = md
                    stamps[m["key"]] = {k: v for r in ins for k, v in stamps[r["key"]].items()}
            else:
                live = [*live, {**new, "level": 0}]
            for r in live:
                if not (out / r["key"] / "served").exists():
                    ia.cut_run(con, str(dirs[r["key"]]), out / r["key"] / "served", rg_rows=rg_rows)
            m = ia.manifest(gen, {"scans": scans[:n_base]}, [{**r, "stamps": stamps[r["key"]]} for r in live])
            (out / "manifests").mkdir(parents=True, exist_ok=True)
            (out / "manifests" / f"{s['id']}.json").write_text(json.dumps(m, indent=1) + "\n")
        # The cut's per-sort reports aren't read by the Worker.
        for f in out.rglob("*.json"):
            if f.parent.name == "served":
                f.unlink()
        print(f"{gen}: runs {[r['key'] for r in live]}", file=sys.stderr)


#: `g2`: the per-scan store fixtures `v2-slices` (multi-owner slices) and `v2-lens` as three scans —
#: slices, lens, slices again (every version closes and reopens) — so `intervalStore.test.ts` compares
#: sliced reads (lens, owner pools) of the interval store with the per-scan reader over the same files.
G2 = "g2"
G2_SCANS = [("2026-09-30T0007", "v2-slices"), ("2026-09-30T0008", "v2-lens"), ("2026-09-30T0009", "v2-slices")]


def slices_gen() -> None:
    from dt_cloud import interval_store as ist
    from dt_cloud import static_names as sn

    fx = HERE.parent
    work = HERE / "work2"
    shutil.rmtree(work, ignore_errors=True)
    scans = [{"id": d, "src": f"{name}/path-index.parquet", "ts": sn.scan_epoch(d), "version": 2} for d, name in G2_SCANS]
    doc = {"bucket": "b", "scans": scans}
    ranges = {"k": 1, "ranges": [{"i": 0, "lo": [0, ""], "hi": None}]}
    con = duckdb.connect()
    d = ist.build_range(doc, ranges, 0, work / "out", con, mount=str(fx))
    ist.fold_range(str(work / "out"), 0, work / "out", con)
    ds = ist.build_slices_range(doc, ranges, 0, work / "out", con, mount=str(fx))
    ist.slice_totals_range(str(work / "out"), 0, work / "out", con)
    assert d["eq"] and ds["eq"], (d["eq"], ds["eq"])
    out = HERE / "interval-store" / G2
    shutil.rmtree(out, ignore_errors=True)
    for sort, (sub, _, _) in ist.SORTS.items():
        if sort == "reads":
            continue
        ist.write_served(con, f"read_parquet('{work}/out/{sub}/r*.parquet')", sort, out / "served" / f"{sort}.parquet", ist.SUB_SCHEMA[sub],
                         rg_rows=256, stamps=[x["ts"] for x in scans])
    (out / "scans.json").write_text(json.dumps({"scans": [{"id": x["id"], "ts": x["ts"]} for x in scans]}, indent=1) + "\n")
    shutil.rmtree(work)
    print(f"{G2}: {sorted(p.name for p in (out / 'served').iterdir())}", file=sys.stderr)


def tiered() -> None:
    """`g3`, `g4` over `g1`'s six scans (the same generator, seed and ranges as `main`), and `g5` over `g2`'s three."""
    from dt_cloud import static_names as sn

    spec = importlib.util.spec_from_file_location("tis", ROOT / "cloud/tests/test_interval_store.py")
    t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t)
    work = HERE / "work"
    shutil.rmtree(work, ignore_errors=True)
    rng = random.Random(11)
    paths = t._universe(rng)
    scans = []
    for j, d in enumerate(t.DATES):
        v = 2 if j >= t.V2_FROM else 1
        key = f"listing/{d}/path-index.parquet"
        (work / key).parent.mkdir(parents=True, exist_ok=True)
        t._write(t._scan_rows(rng, paths, j), work / key, v)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
    ranges = {"k": 3, "ranges": [{"i": 0, "lo": [0, ""], "hi": [2, "b1/gof"]}, {"i": 1, "lo": [2, "b1/gof"], "hi": [3, ""]}, {"i": 2, "lo": [3, ""], "hi": None}]}
    tiered_gens(work, work / "tiered", scans, 4, ranges, rg_rows=4)
    g2 = [{"id": d, "src": f"{name}/path-index.parquet", "ts": sn.scan_epoch(d), "version": 2} for d, name in G2_SCANS]
    tiered_gens(HERE.parent, work / "tiered-g5", g2, 2, {"k": 1, "ranges": [{"i": 0, "lo": [0, ""], "hi": None}]}, rg_rows=256, gens={"g5": "unmerged"})
    shutil.rmtree(work)


if __name__ == "__main__":
    {"g2": slices_gen, "tiered": tiered}.get(sys.argv[1] if sys.argv[1:] else "", main)()
