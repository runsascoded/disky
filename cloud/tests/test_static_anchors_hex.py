"""Anchored search (`^q`, `q$`, `^q$`) on a generation built under the hex-run rule (specs/static-hex-runs.md): the
anchors built under the generation's rule (base and runs, rollups at a small R) answer every key at every directory
on every date exactly as brute force under `segment_occurs`; anchors built under another rule are declined (`q$` still
reads the light index); the scan-file brute force SQL equals the version brute force under the rule."""
from __future__ import annotations

import json

import fsspec
import pyarrow.parquet as pq
import pytest

from dt_cloud import static_anchors as an
from dt_cloud import static_append as sa
from dt_cloud import static_names as sn
from dt_cloud import static_roots as sr
from dt_cloud.hex_runs import segment_occurs

from test_static_hex import DATES, H64, R, _gen, _versions, hexfx  # noqa: F401

RR = 2
K = 2
BASE = 3
KEYS = [an.term_key(t, m) for t, m in [
    # `q$`: proper suffixes of trailing runs (dropped under the rule), a whole run, plain endings
    ("2b3c", "end"), ("a2b3c", "end"), ("abcdef", "end"), ("cdef", "end"), (H64[-20:], "end"), ("cafe0123456789abcdef", "end"),
    (".json", "end"), ("json", "end"), ("ckpt", "end"), ("cafe", "end"), ("f0y", "end"),
    ("3f9a", "start"), ("cafe", "start"), ("x01", "start"), ("run", "start"), ("obj_", "start"),
    ("cafe.txt", "exact"), (H64, "exact"), ("data.json", "exact"), ("b2", "exact"),
]]


def _brute(versions, key: str, P: str, D_ms: int, rule) -> dict:
    return an.brute_view([(p, u, vf * 1000, vt * 1000, s, n) for d, p, u, vf, vt, s, n in versions if d >= 1], key, P, D_ms, rule)


def _dirs(versions) -> list[str]:
    out = {""}
    for _, p, *_ in versions:
        parts = p.split("/")
        out.update("/".join(parts[:k]) for k in range(1, len(parts) + 1))
    return sorted(out)


@pytest.fixture(scope="module", params=["16,8", "mismatch"])
def world(request, hexfx, tmp_path_factory):  # noqa: F811
    """A base generation (the first `BASE` scans) under `16,8` with its name index and anchors — built under the rule,
    or (`mismatch`) without it — and one level-0 run per later scan."""
    root, scans, merged = hexfx
    tmp = tmp_path_factory.mktemp(f"anchors-hex-{request.param}")
    arule = R if request.param == "16,8" else None
    base = _gen(root, scans[:BASE], tmp / "base", R)
    con, bdir = base["con"], base["out"]
    (bdir / "shards.json").write_text(json.dumps(base["plan"]) + "\n")
    pq.write_table(base["side"], bdir / "sidecar.parquet")
    cint = sorted(str(f) for f in (base["build"] / "cintervals").glob("*.parquet"))
    rg = (sa.SX_RG, an.SX_RG, an.ROLLUP_RG, sr.IDX_RG)
    sa.SX_RG, an.SX_RG, an.ROLLUP_RG, sr.IDX_RG = 4, 5, 4, 3
    try:
        an.write_names(con, an.files_sql(cint, "depth, path, usr, vf, vt, size, n_files"), bdir / an.NAMES, target_rows=11)
        an.build_tier_local(con, an.Tier(bdir).files("end"), an.Tier(bdir).files("exact"), bdir / an.ANCHORS, RR, K, rule=arule)
        ranges = base["ranges"]["ranges"]
        prev = {r["i"]: f"SELECT * FROM read_parquet({sn.q(str(base['build'] / 'cintervals' / f'r{r['i']:04d}.parquet'))})" for r in ranges}
        runs: list[an.Tier] = []
        for scan in scans[BASE:]:
            run = tmp / "runs" / scan["id"]
            for r in ranges:
                name = f"r{r['i']:04d}"
                sa.append_open(con, prev[r["i"]], scan, r, name, run, bucket="b", mount=str(root), rule=R)
                prev[r["i"]] = f"SELECT * FROM read_parquet({sn.q(str(run / 'copen' / f'{name}.parquet'))})"
            files = sorted(str(f) for f in (run / "cdelta").glob("*.parquet"))
            sa.delta_shards(con, files, run, target_rows=7, rule=R)
            an.build_run_local(con, [an.Tier(bdir), *runs], an.Tier(run), scan["ts"], RR, K, [scan["id"]],
                               versions_sql=an.files_sql(files, "depth, path, usr, vf, vt, size, n_files"), names_rows=7, rule=arule)
            runs.append(an.Tier(run))
    finally:
        sa.SX_RG, an.SX_RG, an.ROLLUP_RG, sr.IDX_RG = rg
    return {"param": request.param, "root": root, "scans": scans, "dirs": [bdir, *(t.root for t in runs)], "versions": _versions(merged)}


def _reader(dirs, rule, **kw) -> an.AnchoredReader:
    fs = fsspec.filesystem("file")
    return an.AnchoredReader([an.reader_tier(fs, str(d)) for d in dirs], rule=rule, **kw)


def test_meta_records_the_rule(world):
    metas = [json.loads((d / an.ANCHORS / "meta.json").read_text()).get("hex_runs") for d in world["dirs"]]
    assert metas == [{"min": 16, "tail": 8} if world["param"] == "16,8" else None] * len(world["dirs"])


def test_views_equal_brute_force_under_the_rule(world):
    """Every key at every directory on every date of the base and of base ⊕ runs: the reader's view equals brute force
    under `segment_occurs` (a rollup: its kept children, and the rest as the others' sum), or it declines — only when
    the anchors were built under another rule, and then only what needs them."""
    versions = world["versions"]
    got, want, sources = [], [], {}
    # whole-range bounds: the default (light), and none (scoped roots, rollups; `^q` scoped or declined)
    for n, dates, kw in [(n, ds, kw) for n, ds in ((1, DATES[:BASE]), (len(world["dirs"]), DATES))
                         for kw in ({}, {"max_rows": 0, "start_max_rows": 0})]:
        reader = _reader(world["dirs"][:n], R, **kw)
        for key in KEYS:
            for P in _dirs(versions):
                v = reader.view(key, P, dates)
                sources[v["source"]] = sources.get(v["source"], 0) + 1
                if v["source"] == "plain":
                    assert an.term_in_path(key, P, R)
                    continue
                if v["source"] == "declined":
                    got.append((key, P, "declined"))
                    # a `^q` past a zero bound is too common to read (`term-too-common`), as on any generation
                    ok = world["param"] == "mismatch" or (kw and an.parse_key(key)[1] == "start")
                    want.append((key, P, "declined" if ok else "answered"))
                    continue
                for d in dates:
                    b = _brute(versions, key, P, sn.scan_epoch(d) * 1000, R)
                    if v["source"] in ("rollup", "catalog"):
                        kept = v["answers"][d]
                        rest = [sum(x[0] for c, x in b.items() if c not in kept), sum(x[1] for c, x in b.items() if c not in kept)]
                        got.append((key, P, d, kept, v["rest"][d]))
                        want.append((key, P, d, {c: b[c] for c in kept if c in b}, rest))
                    else:
                        got.append((key, P, d, v["answers"][d]))
                        want.append((key, P, d, b))
    assert got == want
    if world["param"] == "16,8":
        assert {"light", "roots", "rollup"} <= set(sources), sources
    else:
        # only `q$` answers, from the light index; its heavy views and every `^q` / `^q$` decline
        assert set(sources) == {"plain", "light", "declined"}, sources


def test_the_rule_changes_end_keys_only(world):
    """The keys whose brute-force answer the rule changes: proper hex suffixes of 16+ digit trailing runs, never `^q`."""
    versions = world["versions"]
    D = sn.scan_epoch(DATES[-1]) * 1000
    differs = [k for k in KEYS if any(_brute(versions, k, P, D, R) != _brute(versions, k, P, D, None) for P in _dirs(versions))]
    assert differs == [an.term_key(t, "end") for t in ("2b3c", "a2b3c", "abcdef", "cdef", H64[-20:])]


def test_brute_sql_equals_brute_view_under_the_rule(world):
    if world["param"] != "16,8":
        pytest.skip("one rule is enough")
    versions = world["versions"]
    con = sn.connect(2, "1GB", None)
    cases = [(k, *an.parse_key(k), P) for k in KEYS for P in _dirs(versions) if not an.term_in_path(k, P, R)]
    con.execute("CREATE TABLE cases (key VARCHAR, k VARCHAR, m VARCHAR, P VARCHAR)")
    con.executemany("INSERT INTO cases VALUES (?, ?, ?, ?)", cases)
    for scan in world["scans"]:
        got: dict = {(k, P): {} for k, _, _, P in cases}
        for key, P, child, b, o in con.execute(an.brute_sql(f"{world['root']}/{scan['src']}", scan["version"], "cases", R)).fetchall():
            if b or o:
                got[(key, P)][child] = [int(b), int(o)]
        D = scan["ts"] * 1000
        assert got == {(k, P): _brute(versions, k, P, D, R) for k, _, _, P in cases}, scan["id"]


def test_segment_occurs_anchors():
    """`^q` / `^q$` are untouched by the rule; `q$` loses exactly the occurrences `dropped` drops."""
    seg = "run-cafe0123456789abcdef"
    assert [segment_occurs(seg, q, m, R) for q, m in [
        ("run", "start"), (seg, "exact"), ("abcdef", "end"), ("cafe0123456789abcdef", "end"), ("-cafe0123456789abcdef", "end"), ("0123", None),
    ]] == [True, True, False, True, True, False]
    assert [segment_occurs(seg, q, m, None) for q, m in [("abcdef", "end"), ("0123", None)]] == [True, True]


def test_first_hit_sql_end_equals_segment_occurs(world):
    """The rollup builders' `q$` first-hit test in SQL (`first_hit_sql`, its row and its ancestors) equals the Python
    definition under the rule, on every `q$` suffix row of the fixture's paths (rows the rule drops included)."""
    if world["param"] != "16,8":
        pytest.skip("one rule is enough")
    paths = sorted({p for d, p, *_ in world["versions"] if d >= 1} | {f"b1/{H64}/x{H64[-6:]}", f"b2/a{H64[-20:]}/{H64}"})
    ends = [an.parse_key(k)[0] for k in KEYS if an.parse_key(k)[1] == "end"] + ["e8f9", "f0y"]
    rows = [(k, p) for p in paths for k in ends if p.rsplit("/", 1)[-1].lower().endswith(k)]
    con = sn.connect(2, "1GB", None)
    con.execute("CREATE TABLE r (k VARCHAR, path VARCHAR)")
    con.executemany("INSERT INTO r VALUES (?, ?)", rows)
    from dt_cloud.static_catalog import PARENT

    got = sorted(con.execute(f"SELECT k, path FROM (SELECT k, path, {PARENT} AS par FROM r) WHERE {an.first_hit_sql('end', 'k', rule=R)}").fetchall())

    def first(k: str, p: str) -> bool:
        segs = p.lower().split("/")
        return segment_occurs(segs[-1], k, "end", R) and not any(segment_occurs(s, k, "end", R) for s in segs[:-1])

    assert got == sorted((k, p) for k, p in rows if first(k, p))
    # the rule matters here: plain containment keeps more
    assert len(got) < sum(1 for k, p in rows if not any(s.endswith(k) for s in p.lower().split("/")[:-1]))
