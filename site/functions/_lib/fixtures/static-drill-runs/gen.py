"""The heavy-term drilldown runs fixture (`staticDrillRuns.test.ts`): a base generation's `drill/` plus two per-scan
runs' `deltas/<scan>/drill/`, built by `dt_cloud.static_drill` itself (`build_drill`, `build_day`) over the scans of
`cloud/tests/test_static_drill.py` (`_files`), so the Worker's tiered reader is checked against the Python builder's
layout, the Python reader (`TieredDrill`) and brute force from the versions.

- Scans: `2026-08-01` … `2026-08-03` (the base), then `2026-08-04T0600` and `2026-08-04T1800` (two runs on one day).
  The runs hit each hard case (asserted below, as `test_the_runs_hit_every_case`): a class split (`.js` leaves
  `.json`'s), a child entering the kept set (an existing one and a new one) and one leaving (a full header), a
  directory becoming heavy, a literal becoming a member (`zebra`), closes, resurrection, multi-owner paths.
- `R` = 3 root rows, `K` = 2 kept children, 4-row base and 2-row run groups (so the dispatch sums several tiers'
  slack), and the generation's keys as on R2:
  `scans.json` (the base's scans), `catalog/` (the base catalog, its `meta.json` written here), `drill/` (the base drill), `deltas/<scan>/drill/`
  (`state/` left out: the builder's, not served) and `deltas/<scan>/catalog/` (the catalog deltas: the fleet root's
  cells), `manifests/<last scan>.json` (both runs).
- `expected.json`: `dates`, `paths` (`''`, every directory, `nope`), `long`/`short` (the members through
  the last run) and `members1` (the long members through the first run); `views[t][P][d]`, brute force from the
  versions: per child of `P` holding a live match root under it `[bytes, objects]`, only the non-empty ones (a
  view `P` itself matches is the plain one);
  `py[stack][t][P]`, the Python `TieredDrill` over the base and the first run (`run1`) or both (`run2`), for every
  `P` but the fleet root (it reads the catalog): `[source, upper]` and for a rollup `[kept, rows, children]`.

Regenerate from the repo root, with a `dt_cloud` that has `static_drill` (branch `drill-append`) first on the path:
`PYTHONPATH=<drill-append>/cloud/src:<drill-append>/cloud/tests:<drill-append>/src .venv/bin/python site/functions/_lib/fixtures/static-drill-runs/gen.py`
"""
import json
import shutil
import tempfile
from pathlib import Path

import pyarrow.parquet as pq

from dt_cloud import static_append as sa
from dt_cloud import static_catalog as sc
from dt_cloud import static_drill as sd
from dt_cloud import static_names as sn
from dt_cloud import static_roots as sr
from test_static_catalog import _gen
from test_static_drill import V, _drill, _files, _members, _reader, _rows, _sx_files
from test_static_names import _coalesced_oracle, _merged, _oracle, _write

HERE = Path(__file__).parent
DAYS = ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04T0600", "2026-08-04T1800"]
BASE = 3
CONFIG = {"R": 3, "K": 2, "floor": 2, "rg": 4, "run_rg": 2}
NONMEMBERS = ["nomatch", "qq"]


def build(root: Path, cfg: dict) -> dict:
    """`test_static_drill.world`, over `DAYS`: the base drill, each run's drill and catalog delta."""
    R, K, floor = cfg["R"], cfg["K"], cfg["floor"]
    scans, merged = [], []
    for j, d in enumerate(DAYS):
        rows = _rows(_files(j))
        key = f"listing/{d}/path-index.parquet"
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        _write(rows, root / key, 2)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": 2})
        merged.append((sn.scan_epoch(d), _merged(rows, 2)))
    gens = {j: _gen(root, {"bucket": "b", "scans": scans[:j + 1]}, root / f"gen{j}", V) for j in (BASE - 1, len(DAYS) - 1)}
    base = gens[BASE - 1]
    pq.write_table(base["side"], base["out"] / "sidecar.parquet")
    con = base["con"]
    old = sr.ROOT_RG, sr.IDX_RG
    sr.ROOT_RG, sr.IDX_RG = cfg["rg"], 3
    try:
        meas = _drill(con, base, root / "base" / "drill", R, K, floor)
        _drill(con, gens[len(DAYS) - 1], root / "rebuild" / "drill", R, K, floor)
        shards = sc.BaseShards(str(base["out"]), base["side"])
        prev = {r["i"]: f"SELECT * FROM read_parquet({sn.q(str(base['build'] / 'cintervals' / f'r{r['i']:04d}.parquet'))})" for r in base["ranges"]["ranges"]}
        runs, deltas, prior, metas = [], [], [sd.Tier(root / "base" / "drill", "base")], []
        cintervals = [str(p) for p in sorted((base["build"] / "cintervals").glob("*.parquet"))]
        for j in range(BASE, len(DAYS)):
            scan = scans[j]
            run = root / "runs" / scan["id"]
            for r in base["ranges"]["ranges"]:
                name = f"r{r['i']:04d}"
                sa.append_open(con, prev[r["i"]], scan, r, name, run, bucket="b", mount=str(root))
                prev[r["i"]] = f"SELECT * FROM read_parquet({sn.q(str(run / 'copen' / f'{name}.parquet'))})"
            cdelta = [str(p) for p in sorted((run / "cdelta").glob("*.parquet"))]
            sa.delta_shards(con, cdelta, run, target_rows=7)
            sa.catalog_delta(con, [base["final"], *(d / "catalog" for d in runs)], shards, [*deltas, cdelta], V, run / "catalog", root / "work" / scan["id"])
            con.execute(f"CREATE OR REPLACE TABLE pnew AS {sd.pnew_sql(cdelta, cintervals + [f for d in deltas for f in d])}")
            heads = {r["q"] for r in pq.read_table(run / "catalog" / "cells.parquet").to_pylist() if r["bucket"] == "" and len(r["q"]) >= 3}
            new = sorted(heads - set(prior[-1].alias_table().column("q").to_pylist()))
            history = sd.history_rows([_reader(base["out"]), *(_reader(d) for d in runs), _reader(run)], new)
            metas.append(sd.build_day(con, prior, run / "drill", D=scan["ts"], R=R, K=K, floor=floor, sx_files=_sx_files(run), cdelta_files=cdelta,
                                      new_members=new, history=history, meas={k: sd.meas_sql(k, [v]) for k, v in meas.items()},
                                      tier={"first": scan["id"], "last": scan["id"], "level": 0, "scans": [scan["id"]]}, rg=cfg["run_rg"],
                                      log=lambda *_: None))
            runs.append(run)
            deltas.append(cdelta)
            prior.append(sd.Tier(run / "drill", scan["id"]))
    finally:
        sr.ROOT_RG, sr.IDX_RG = old
    return {"base": base, "runs": runs, "tiers": prior, "metas": metas, "merged": merged,
            "rebuild": sd.Tier(root / "rebuild" / "drill", "rebuild")}


def hits_every_case(metas: list[dict]) -> None:
    long = [m["long_day"] for m in metas]
    short = [m["short_day"] for m in metas]
    cls = [m["classes"] for m in metas]
    assert any(c["classes"] > c["classes_before"] for c in cls), "a class split"
    assert sum(c["members_new"] for c in cls) >= 1, "a literal became a member"
    assert sum(x["restated"] for x in long + short) >= 1, "an existing child entered or left the kept set"
    assert sum(x["heavy_new"] for x in long + short) >= 1, "a directory became heavy"
    assert sum(x["probes_existed"] for x in long + short) >= 1, "a probe finding an earlier root"
    assert sum(x["probes"] - x["probes_existed"] for x in long + short) >= 1, "a probe finding none"


def brute(versions: list[tuple], t: str, P: str, date: str) -> dict | None:
    """Per child of `P` holding a live match root under it: `[bytes, objects]`; None when `P` itself matches."""
    if t in P.lower():
        return None
    D = sn.scan_epoch(date)
    acc: dict[str, list[int]] = {}
    for depth, path, _usr, vf, vt, size, n_files in versions:
        name = path.rsplit("/", 1)[-1].lower()
        parent = path.rsplit("/", 1)[0].lower() if "/" in path else ""
        if depth >= 1 and vf <= D < vt and (P == "" or path.startswith(P + "/")) and t in name and t not in parent:
            e = acc.setdefault((path if P == "" else path[len(P) + 1:]).split("/", 1)[0], [0, 0])
            e[0] += size
            e[1] += n_files
    return {k: v for k, v in sorted(acc.items()) if v != [0, 0]}


def untimed(doc):
    """`doc` without its timings (`s`), so a rerun writes the same bytes."""
    if isinstance(doc, dict):
        return {k: untimed(v) for k, v in doc.items() if k != "s"}
    return doc


def copy_tree(src: Path, dst: Path, skip: tuple[str, ...] = ()) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*skip))
    for m in dst.rglob("meta.json"):
        m.write_text(json.dumps(untimed(json.loads(m.read_text())), indent=1) + "\n")


def main() -> None:
    cfg = CONFIG
    out = HERE
    for k in ("catalog", "drill", "deltas", "manifests", "scans.json", "expected.json"):
        shutil.rmtree(out / k, ignore_errors=True) if (out / k).is_dir() else (out / k).unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        w = build(Path(tmp), cfg)
        hits_every_case(w["metas"])
        copy_tree(w["base"]["final"], out / "catalog")
        md = pq.ParquetFile(out / "catalog" / "cells.parquet").metadata
        cat = {"gen": "fixture", "cells_rows": md.num_rows, "row_groups": md.num_row_groups, "bytes": (out / "catalog" / "cells.parquet").stat().st_size,
               "cell_rg": md.row_group(0).num_rows, "membership": {"max_rows": V}}
        (out / "catalog" / "meta.json").write_text(json.dumps(cat, indent=1) + "\n")
        copy_tree(w["tiers"][0].root, out / "drill")
        keys = []
        for scan, run in zip(DAYS[BASE:], w["runs"]):
            key = f"deltas/{scan}"
            copy_tree(run / "drill", out / key / "drill", ("state",))
            copy_tree(run / "catalog", out / key / "catalog")
            keys.append({"key": key, "first": scan, "last": scan, "level": 0, "scans": [scan]})
        (out / "scans.json").write_text(json.dumps({"bucket": "b", "scans": [{"id": d} for d in DAYS[:BASE]]}, indent=1) + "\n")
        (out / "manifests").mkdir()
        (out / "manifests" / f"{DAYS[-1]}.json").write_text(json.dumps({"gen": "fixture", "date": DAYS[-1], "base_scans": BASE, "scans": DAYS, "runs": keys}, indent=1) + "\n")
        versions = _coalesced_oracle(_oracle(w["merged"]))
        rebuilt = w["rebuild"]
        long, short = _members(rebuilt, "long"), _members(rebuilt, "short")
        assert _members(w["tiers"][-1], "long") == long
        dirs = sorted({p.rsplit("/", 1)[0] for _, p, *_ in versions if "/" in p})
        paths = ["", *dirs, "nope"]
        terms = [*long, *short, *NONMEMBERS]
        views = {t: {P: v for P in paths if t not in P.lower() for v in [{d: x for d in DAYS for x in [brute(versions, t, P, d)] if x}] if v}
                 for t in terms}
        py = {}
        for stack, tiers in (("run1", w["tiers"][:2]), ("run2", w["tiers"])):
            drills = {kind: sd.TieredDrill(tiers, kind) for kind in sd.KINDS}
            doc = {}
            for t in terms:
                kind = "short" if len(t) <= 2 else "long"
                doc[t] = {}
                for P in paths[1:]:
                    a = drills[kind].view(t, P, DAYS)
                    doc[t][P] = ([a["source"]] if a["source"] == "plain" else [a["source"], a["upper"]]
                                 + ([[a["header"][k] for k in ("kept", "rows", "children")]] if a["source"] == "rollup" else []))
            py[stack] = doc
        expected = {"dates": DAYS, "paths": paths, "long": long, "short": short, "members1": _members(w["tiers"][1], "long"),
                    "R": cfg["R"], "K": cfg["K"], "views": views, "py": py}
        (out / "expected.json").write_text(json.dumps(expected, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
