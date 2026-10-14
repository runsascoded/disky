"""`dt_cloud.static_anchors`: anchored name search (`^q`, `q$`, `^q$`) over a base generation and per-scan runs —
every key's view at every directory on every scan, through the reader's dispatch (light, scoped roots, rollups,
stacked over the tiers; each run apart and merged), equals brute force from the versions; the name index holds
exactly one row per version; the scan-file brute force SQL equals the version brute force."""
from __future__ import annotations

import json
import random
from pathlib import Path

import fsspec
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import static_anchors as an
from dt_cloud import static_append as sa
from dt_cloud import static_catalog as sc
from dt_cloud import static_names as sn
from dt_cloud import static_roots as sr

from test_static_catalog import _gen
from test_static_names import _coalesced_oracle, _merged, _oracle, _scan_rows, _write

V = 3
R = 3
K = 2
IDS = ["2026-07-30", "2026-08-01", "2026-08-15", "2026-09-01", "2026-09-15", "2026-10-01"]
BASE = 4


def _paths() -> list[str]:
    """Names that exercise the anchors: a starts-with root below a contains root (`big-tomato/tomato.txt` for
    `^tomat`), a matching directory covering matches (`tomat/tomato.json`, `sub.json/in.json`, `config.json/config.json`),
    suffixes that are not the key (`.jsonl`, `.json.gz`), case, buckets that match, and enough `.json` files at
    several depths for heavy directories at R = 3."""
    p = ["b1", "b2", "tomato-bkt",
         "b1/big-tomato", "b1/big-tomato/tomato.txt", "b1/big-tomato/tomatoes.json",
         "b1/tomat", "b1/tomat/tomato.json", "b1/tomat/x",
         "b1/data", "b1/data/c.jsonl", "b1/data/x.json.gz", "b1/data/E.JSON", "b1/data/sub.json", "b1/data/sub.json/in.json",
         "b1/data/deep", "b1/data/deep/er",
         "b2/cfg", "b2/cfg/config.json", "b2/config.json", "b2/config.json/config.json", "b2/x", "b2/x/config.json", "b2/x/Config.JSON",
         "b2/train", "b2/train/train-0.bin", "b2/train-1.bin", "b2/xtrain", "b2/xtrain/train-2.bin", "b2/Train-3.BIN",
         "tomato-bkt/a.json", "tomato-bkt/tomato.json",
         # a parent holding the key inside a segment that doesn't match it: a contains-style test would drop these
         "b1/a.jsonx", "b1/a.jsonx/k.json", "b2/config.json.bak", "b2/config.json.bak/config.json"]
    p += [f"b1/data/f{i}.json" for i in range(6)]
    p += [f"b1/data/deep/g{i}.json" for i in range(4)]
    p += [f"b1/data/deep/er/h{i}.json" for i in range(4)]
    p += [f"b2/json/j{i}.json" for i in range(3)]
    p += ["b2/json"]
    p += [f"b2/cfg/c{i}/config.json" for i in range(4)] + [f"b2/cfg/c{i}" for i in range(4)]
    return p


KEYS = [an.term_key(t, m) for t, m in [
    (".json", "end"), ("json", "end"), ("son", "end"), (".bin", "end"), ("bin", "end"), ("ato", "end"), ("-bkt", "end"), ("zzz", "end"),
    ("tomat", "start"), ("train", "start"), ("conf", "start"), ("tr", "start"), ("b1", "start"), ("zz", "start"),
    ("config.json", "exact"), ("x", "exact"), ("train", "exact"), ("tomato.json", "exact"), ("b2", "exact"), ("nope", "exact"),
]]


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return build_world(tmp_path_factory.mktemp("anchors"))


def build_world(tmp: Path) -> dict:
    """The scans (`_paths` over `IDS`), the base generation (the first `BASE` scans) with its name index and
    anchors, a run per later scan (`names/`, `anchors/`, catalog), and the runs merged (also the TS fixture's,
    `site/functions/_lib/fixtures/static-anchors/gen.py`)."""
    root = tmp / "data"
    rng = random.Random(11)
    paths = _paths()
    scans, merged = [], []
    for j, d in enumerate(IDS):
        v = 2 if j >= 2 else 1
        rows = _scan_rows(rng, paths, j)
        key = f"listing/{d}/path-index.parquet"
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        _write(rows, root / key, v)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
        merged.append((sn.scan_epoch(d), _merged(rows, v)))
    base = _gen(root, {"bucket": "b", "scans": scans[:BASE]}, tmp / "base", V)
    con = base["con"]
    bdir = base["out"]
    plan = sn.plan_shards(sorted((base["build"] / "chist").glob("*.parquet")), target_rows=60, tasks=2)  # `_gen`'s plan
    (bdir / "shards.json").write_text(json.dumps(plan) + "\n")
    pq.write_table(base["side"], bdir / "sidecar.parquet")
    cint = sorted(str(f) for f in (base["build"] / "cintervals").glob("*.parquet"))
    rg = (sa.SX_RG, an.SX_RG, an.ROLLUP_RG, sr.IDX_RG)
    # several row groups per shard (the dispatch slack is the larger of the two), per rollup file and per index file
    sa.SX_RG, an.SX_RG, an.ROLLUP_RG, sr.IDX_RG = 4, 5, 4, 3
    try:
        an.write_names(con, an.files_sql(cint, "depth, path, usr, vf, vt, size, n_files"), bdir / an.NAMES, target_rows=11)
        an.build_tier_local(con, an.Tier(bdir).files("end"), an.Tier(bdir).files("exact"), bdir / an.ANCHORS, R, K)
        shards = sc.BaseShards(str(bdir), base["side"])
        ranges = base["ranges"]["ranges"]
        prev = {r["i"]: f"SELECT * FROM read_parquet({sn.q(str(base['build'] / 'cintervals' / f'r{r['i']:04d}.parquet'))})" for r in ranges}
        runs: list[an.Tier] = []
        deltas = []
        for j in range(BASE, len(IDS)):
            scan = scans[j]
            run = tmp / "runs" / scan["id"]
            for r in ranges:
                name = f"r{r['i']:04d}"
                sa.append_open(con, prev[r["i"]], scan, r, name, run, bucket="b", mount=str(root))
                prev[r["i"]] = f"SELECT * FROM read_parquet({sn.q(str(run / 'copen' / f'{name}.parquet'))})"
            files = sorted(str(f) for f in (run / "cdelta").glob("*.parquet"))
            deltas.append(files)
            sa.delta_shards(con, files, run, target_rows=7)
            sa.catalog_delta(con, [base["final"], *(t.root / "catalog" for t in runs)], shards, deltas, V, run / "catalog", tmp / "work" / scan["id"])
            an.build_run_local(con, [an.Tier(bdir), *runs], an.Tier(run), scan["ts"], R, K, [scan["id"]],
                               versions_sql=an.files_sql(files, "depth, path, usr, vf, vt, size, n_files"), names_rows=7)
            runs.append(an.Tier(run))
        merged_dir = tmp / "runs" / "merged"
        sa.merge_shards([t.root for t in runs], merged_dir, target_rows=9)
        an.merge_run_local(con, runs, an.Tier(merged_dir), R, K, IDS[BASE:], names_rows=9)
    finally:
        sa.SX_RG, an.SX_RG, an.ROLLUP_RG, sr.IDX_RG = rg
    oracle = _oracle(merged)
    versions = [(path, usr, vf * 1000, vt * 1000, size, n) for depth, path, usr, vf, vt, _k, size, n, *_ in oracle if depth >= 1]
    return {"root": root, "scans": scans, "base": bdir, "runs": [t.root for t in runs], "merged": merged_dir, "oracle": oracle,
            "versions": versions, "paths": paths, "con": con, "cint": cint, "catalog": base["final"]}


def _reader(dirs: list[Path], **kw) -> an.AnchoredReader:
    fs = fsspec.filesystem("file")
    return an.AnchoredReader([an.reader_tier(fs, str(d)) for d in dirs], **kw)


def _dirs(paths: list[str]) -> list[str]:
    out = {""}
    for p in paths:
        parts = p.split("/")
        for k in range(1, len(parts)):
            out.add("/".join(parts[:k]))
        out.add(p)
    return sorted(out) + ["nope"]


def _views(world, stack: list[Path], dates: list[str], keys=KEYS, **kw) -> tuple[list, list, dict]:
    reader = _reader(stack, **kw)
    got, want, sources = [], [], {}
    for key in keys:
        for P in _dirs(world["paths"]):
            v = reader.view(key, P, dates)
            sources[v["source"]] = sources.get(v["source"], 0) + 1
            if v["source"] == "plain":
                assert an.term_in_path(key, P)
                continue
            if v["source"] == "declined":
                continue
            for d in dates:
                D = sn.scan_epoch(d) * 1000
                b = an.brute_view(world["versions"], key, P, D)
                if v["source"] in ("rollup", "catalog"):
                    kept = v["answers"][d]
                    rest = [sum(x[0] for c, x in b.items() if c not in kept), sum(x[1] for c, x in b.items() if c not in kept)]
                    got.append((key, P, d, kept, v["rest"][d]))
                    want.append((key, P, d, {c: b[c] for c in kept if c in b}, rest))
                else:
                    got.append((key, P, d, v["answers"][d]))
                    want.append((key, P, d, b))
    return got, want, sources


def test_name_index_is_one_row_per_version(world):
    rows = sorted((r["s"], r["path"], r["usr"], r["vf"], r["vt"]) for f in sorted((world["base"] / an.NAMES / "sx").glob("*.parquet"))
                  for r in pq.read_table(f).to_pylist())
    end = sn.scan_epoch(IDS[BASE - 1])
    want = sorted(("/" + path.rsplit("/", 1)[-1].lower(), path, usr, vf, vt if vt <= end else sn.OPEN)
                  for depth, path, usr, vf, vt, *_ in _coalesced_oracle(world["oracle"]) if depth >= 1 and vf <= end)
    as_s = [(s, p, u, int(vf.timestamp()), int(vt.timestamp())) for s, p, u, vf, vt in rows]
    assert as_s == want


def test_base_views_equal_brute_force(world):
    got, want, sources = _views(world, [world["base"]], IDS[:BASE])
    assert got == want
    assert {k for k, n in sources.items() if n} == {"plain", "light"}  # at the fixture's scale every range is light


def test_base_views_at_a_small_light_bound(world):
    """With the whole-range bounds at 0 (and 8) rows the `q$` / `^q$` views with rows are scoped (roots or a rollup); the
    `^q` ones are the starts-with catalog at the fleet root, scoped reads below it, declined past the bound."""
    got, want, sources = _views(world, [world["base"]], IDS[:BASE], max_rows=0, start_max_rows=0)
    assert got == want
    # light: only the keys with no rows; `^q` below the root: declined unless no group meets the path
    assert min(sources[k] for k in ("rollup", "roots", "catalog", "declined")) > 0
    got, want, sources = _views(world, [world["base"]], IDS[:BASE], max_rows=8, start_max_rows=8)
    assert got == want
    assert min(sources[k] for k in ("light", "roots", "rollup", "catalog", "scoped", "declined", "plain")) > 0


@pytest.mark.parametrize("stack", ["run1", "run2", "merged"])
def test_tiered_views_equal_brute_force(world, stack):
    tiers = {"run1": [world["base"], world["runs"][0]], "run2": [world["base"], *world["runs"]], "merged": [world["base"], world["merged"]]}[stack]
    dates = IDS[:BASE + (1 if stack == "run1" else 2)]
    for mr in (an.MAX_ROWS, 8, 0):
        got, want, sources = _views(world, tiers, dates, max_rows=mr, start_max_rows=mr if mr != an.MAX_ROWS else None)
        assert got == want
        if mr != an.MAX_ROWS:
            assert sources["rollup"] > 0 and sources["roots"] > 0 and sources["catalog"] > 0


def _prefixes(paths: list[str]) -> list[str]:
    """Every `^q` key (q ≥ 2 characters) of the fixture's lowercase names, plus two absent ones."""
    out = {an.term_key(n[:L], "start") for p in paths for n in [p.rsplit("/", 1)[-1].lower()] for L in range(2, len(n) + 1)}
    return sorted(out | {"/zz", "/qq"})


def _start_rows(tiers: list[Path]) -> dict[str, int]:
    """Per `^q` key, its stored rows over the tiers' name indexes (the reader's whole-range count, row exact)."""
    n: dict[str, int] = {}
    for t in tiers:
        for f in sorted((t / an.NAMES / "sx").glob("*.parquet")):
            for s in pq.read_table(f, columns=["s"]).column("s").to_pylist():
                for L in range(3, len(s) + 1):
                    n[s[:L]] = n.get(s[:L], 0) + 1
    return n


@pytest.mark.parametrize("stack", ["base", "run1", "run2", "merged"])
def test_start_catalog_root_equals_brute_force(world, stack):
    """The fleet root of every `^q` prefix of every name, from the starts-with catalog alone (bounds 0): its members are
    exactly the prefixes with more than R rows over the tiers, and each member's per-bucket answer on every scan equals
    brute force (kept buckets and the remainder)."""
    tiers = {"base": [world["base"]], "run1": [world["base"], world["runs"][0]], "run2": [world["base"], *world["runs"]],
             "merged": [world["base"], world["merged"]]}[stack]
    dates = IDS[:BASE + {"base": 0, "run1": 1}.get(stack, 2)]
    keys = _prefixes(world["paths"])
    rows = _start_rows(tiers)
    got, want, sources = _views(world, tiers, dates, keys=keys, max_rows=0, start_max_rows=0)
    assert got == want
    reader = _reader(tiers, max_rows=0, start_max_rows=0)
    members = sorted(k for k in keys if reader.view(k, "", dates)["source"] == "catalog")
    heavy = sorted(k for k in keys if rows.get(k, 0) > R)
    # Heaviness is sticky: a merged run stores fewer rows than its inputs did (a version's open and close records are
    # one), so its stack may keep members whose rows dropped back to R.
    assert (members == heavy) if stack != "merged" else set(heavy) <= set(members)
    assert len(heavy) > 10


def test_start_scoped_reads_and_the_decline_boundary(world):
    """Below the root a `^q` past its whole-range bound reads the groups of its range whose path bounds meet the view
    path: exact at a bound equal to their rows (`upper`), declined one row under it."""
    tiers = [world["base"], *world["runs"]]
    probe = _reader(tiers)
    seen = 0
    for key in ["/tr", "/train", "/conf", "/b1", "/tomat", "/da", "/c", "/f", "/g"]:
        whole = sum(t.names.select(key, False)[2] for t in probe.anch)
        for P in _dirs(world["paths"]):
            if P == "" or an.term_in_path(key, P):
                continue
            upper = sum(t.names.select(key, False, P)[2] for t in probe.anch)
            if not 0 < upper < whole:
                continue
            v = _reader(tiers, start_max_rows=upper).view(key, P, IDS)
            assert (v["source"], v["upper"]) == ("scoped", upper)
            assert v["answers"] == {d: an.brute_view(world["versions"], key, P, sn.scan_epoch(d) * 1000) for d in IDS}
            assert _reader(tiers, start_max_rows=upper - 1).view(key, P, IDS) == {"key": key, "P": P, "source": "declined", "upper": upper}
            seen += 1
    assert seen > 20


def test_start_hit_cap(world):
    """Past `start_max_hits` first hits a whole `^q` read is heavy (the root: the catalog) and a scoped one declined."""
    tiers = [world["base"], *world["runs"]]
    D = sn.scan_epoch(IDS[-1]) * 1000
    capped = _reader(tiers, start_max_hits=1)
    root = capped.view("/tr", "", IDS)
    assert (root["source"], root["answers"][IDS[-1]]) == ("catalog", an.brute_view(world["versions"], "/tr", "", D))
    assert capped.view("/tr", "b2", IDS)["source"] == "declined"
    roomy = _reader(tiers, start_max_hits=1000)
    assert [(v["source"], v["answers"][IDS[-1]]) for v in (roomy.view("/tr", "", IDS), roomy.view("/tr", "b2", IDS))] == [
        ("light", an.brute_view(world["versions"], "/tr", "", D)), ("light", an.brute_view(world["versions"], "/tr", "b2", D))]


def test_start_bound_is_raised():
    """`^q` reads up to 400K rows (+ two groups) whole or scoped; `q$` / `^q$` keep V + two groups."""
    assert (an.START_MAX_ROWS, an.START_MAX_HITS, an.MAX_ROWS) == (400_000 + 2 * 8192, 150_000, 100_000 + 2 * 8192)
    assert (an.AnchoredReader([]).start_max_rows, an.AnchoredReader([]).start_max_hits) == (an.START_MAX_ROWS, an.START_MAX_HITS)


def test_start_catalog_mutations_fail(world, monkeypatch, tmp_path):
    """The catalog's root fails brute force when the reader ignores the runs' cells, and when the builder's first-hit
    test is the contains literal's (the lowercase parent holds the prefix anywhere, not at a segment's start)."""
    import shutil

    keys = _prefixes(world["paths"])

    def differs(tiers: list[Path], dates: list[str]) -> bool:
        got, want, _ = _views(world, tiers, dates, keys=keys, max_rows=0, start_max_rows=0)
        return got != want

    assert not differs([world["base"], *world["runs"]], IDS)
    with monkeypatch.context() as m:
        m.setattr(an, "stack", lambda parts: (lambda ps: (ps[0][0], ps[0][1:]) if ps else None)([p for p in parts if p]))
        assert differs([world["base"], *world["runs"]], IDS)
    mutant = tmp_path / "mutant"
    shutil.copytree(world["base"], mutant)
    shutil.rmtree(mutant / an.START_DIR)
    orig = an.first_hit_sql
    with monkeypatch.context() as m:
        m.setattr(an, "first_hit_sql", lambda mode, key="s", par="par": f"NOT contains({par}, substring({key}, 2))" if mode == "start" else orig(mode, key, par))
        con, files = world["con"], an.Tier(mutant).files("exact")
        con.execute("DROP TABLE IF EXISTS cells")
        an.start_base(con, files, an.start_heads_sql(an.prefix_counts_sql(files), R), R, K, "cells")
        an.write_start(con, "cells", mutant / an.START_DIR, R, K)
    assert differs([mutant], IDS[:BASE])


def test_runs_make_directories_heavy_and_write_deltas(world):
    """The fixture's runs hit both run cases: a delta header on a heavy directory and a directory made heavy."""
    kinds = []
    for d in world["runs"]:
        for f in sorted((d / an.ANCHORS / "rollups").glob("*.parquet")):
            kinds += [r["kind"] for r in pq.read_table(f).to_pylist()]
    assert {-1, 0} <= set(kinds)


def test_brute_sql_equals_brute_force(world):
    con = world["con"]
    cases = [(key, *an.parse_key(key), P) for key in KEYS for P in ["", "b1", "b1/data", "b2", "b2/cfg"]]
    con.execute("CREATE OR REPLACE TABLE cases (key VARCHAR, k VARCHAR, m VARCHAR, P VARCHAR)")
    con.executemany("INSERT INTO cases VALUES (?, ?, ?, ?)", cases)
    for s in world["scans"]:
        got: dict = {(key, P): {} for key, _, _, P in cases}
        for key, P, child, b, o in con.execute(an.brute_sql(str(world["root"] / s["src"]), s["version"], "cases")).fetchall():
            if b or o:
                got[(key, P)][child] = [b, o]
        D = sn.scan_epoch(s["id"]) * 1000
        want = {(key, P): an.brute_view(world["versions"], key, P, D) for key, _, _, P in cases}
        # the plain views (P holds the key) are the reader's business, not brute force's: compare the rest
        assert {c: v for c, v in got.items() if not an.term_in_path(c[0], c[1])} == {c: v for c, v in want.items() if not an.term_in_path(c[0], c[1])}


def test_a_mutated_reader_fails(world, monkeypatch):
    """Mutation checks: a contains-style parent test, a stack that ignores the runs' cells and a largest-`vt` combine
    each make some view differ from brute force."""
    tiers = [world["base"], *world["runs"]]
    dates = IDS[:BASE + 2]

    def differs() -> bool:
        return any(got != want for got, want, _ in (_views(world, tiers, dates, max_rows=mr) for mr in (an.MAX_ROWS, 0)))

    assert not differs()
    with monkeypatch.context() as m:
        orig = an.fold

        def contains_parent(rows, key, under=None, rule=None):
            """The contains literal's parent test (the lowercase parent holds the literal) on top of the per-segment one."""
            text = an.parse_key(key)[0]
            return [r for r in orig(rows, key, under, rule) if text not in (r["path"].rsplit("/", 1)[0].lower() if "/" in r["path"] else "")]
        m.setattr(an, "fold", contains_parent)
        assert differs()
    with monkeypatch.context() as m:
        m.setattr(an, "stack", lambda parts: (lambda ps: (ps[0][0], ps[0][1:]) if ps else None)([p for p in parts if p]))
        assert differs()
    with monkeypatch.context() as m:
        def worst(parts):
            best: dict = {}
            for p in parts:
                for h in p:
                    k = (h["path"], h["usr"], h["vf"])
                    if k not in best or h["vt"] > best[k]["vt"]:
                        best[k] = h
            return list(best.values())
        m.setattr(an, "combine", worst)
        assert differs()
