"""`dt_cloud.static_roots`: every member's roots (from the suffix shards, fed in chunks) and every short
literal's (from the coalesced versions) are exactly its first-hit versions, and the per-directory counts
equal brute force."""
from __future__ import annotations

import re

import duckdb
import pytest

from dt_cloud import static_catalog as sc
from dt_cloud import static_names as sn
from dt_cloud import static_roots as sr

from test_static_catalog import built  # noqa: F401
from test_static_names import _coalesced_oracle, _oracle, fixture, scan_ids  # noqa: F401

V = 4


def _versions(merged) -> list[tuple]:
    return _coalesced_oracle(_oracle(merged))


def _is_root(t: str, path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    parent = path.rsplit("/", 1)[0].lower() if "/" in path else ""
    return t in name and t not in parent


def _brute_roots(versions: list[tuple], terms: set[str]) -> list[tuple]:
    return sorted((t, *v) for v in versions if v[0] >= 1 for t in terms if _is_root(t, v[1]))


def _members(built, v: int = V) -> dict[int, list[tuple[str, int]]]:
    *_, plan, build, out, con, tmp = built
    by_shard = {}
    for s in plan["shards"]:
        sx = str(out / "sx" / f"s{s['i']:04d}.parquet")
        nodes = sc.census(con, f"read_parquet({sn.q(sx)})", 1).to_pylist()
        by_shard[s["i"]] = [(r["q"], r["rows"]) for r in nodes if r["rows"] > v]
    return by_shard


def _member_roots(built, agg, chunk_rows: int, into: str = "rt") -> tuple[list[tuple], set[str]]:
    *_, plan, build, out, con, tmp = built
    members = _members(built)
    con.execute(f"DROP TABLE IF EXISTS {into}")
    for s in plan["shards"]:
        sx = str(out / "sx" / f"s{s['i']:04d}.parquet")
        con.execute("CREATE OR REPLACE TABLE mem (q VARCHAR, rows BIGINT)")
        if members[s["i"]]:
            con.executemany("INSERT INTO mem VALUES (?, ?)", members[s["i"]])
        for where in sr.chunk_wheres(sx, chunk_rows):
            sr.member_roots(con, sr.sx_rows_sql(f"(SELECT * FROM read_parquet({sn.q(sx)}) WHERE {where})"), "mem", into, agg=agg)
    rows = con.execute(f"SELECT * FROM {into} ORDER BY ALL").fetchall()
    return rows, {m for ms in members.values() for m, _ in ms}


@pytest.mark.parametrize("chunk_rows", [7, 10**9])
def test_member_roots_equal_brute_force(built, chunk_rows):  # noqa: F811
    root, scans, merged, *_ = built
    got, terms = _member_roots(built, agg=False, chunk_rows=chunk_rows)
    assert len(terms) > 20
    expected = _brute_roots(_versions(merged), terms)
    assert len(expected) > 100
    assert got == expected


def test_short_roots_equal_brute_force(built):  # noqa: F811
    root, scans, merged, ranges, plan, build, out, con, tmp = built
    con.execute("DROP TABLE IF EXISTS st")
    for r in ranges["ranges"]:
        src = str(build / "cintervals" / f"r{r['i']:04d}.parquet")
        sr.short_roots(con, f"SELECT * FROM read_parquet({sn.q(src)})", "st", pieces=3)
    got = con.execute("SELECT * FROM st ORDER BY ALL").fetchall()
    versions = _versions(merged)
    names = {v[1].rsplit("/", 1)[-1].lower() for v in versions if v[0] >= 1}
    shorts = {n[p:p + L] for n in names for L in (1, 2) for p in range(len(n) - L + 1)}
    assert got == _brute_roots(versions, shorts)


def _brute_dirs(roots: list[tuple]) -> dict[tuple[str, str], tuple[int, int, int]]:
    """Per (q, directory): root rows strictly under it, distinct root paths, distinct children."""
    rows: dict[tuple[str, str], int] = {}
    paths: dict[tuple[str, str], set] = {}
    kids: dict[tuple[str, str], set] = {}
    for t, depth, path, *_ in roots:
        segs = path.split("/")
        for k in range(1, depth):
            d = "/".join(segs[:k])
            rows[(t, d)] = rows.get((t, d), 0) + 1
            paths.setdefault((t, d), set()).add(path)
            kids.setdefault((t, d), set()).add("/".join(segs[:k + 1]))
    return {key: (rows[key], len(paths[key]), len(kids[key])) for key in rows}


@pytest.mark.parametrize("floor", [1, 3])
def test_stats_equal_brute_force(built, floor):  # noqa: F811
    root, scans, merged, ranges, plan, build, out, con, tmp = built
    rows, terms = _member_roots(built, agg=True, chunk_rows=7)
    roots = _brute_roots(_versions(merged), terms)
    qs, qd = sr.q_stats(con, "rt")
    per_q: dict[str, list] = {}
    for t, _depth, path, *_ in roots:
        e = per_q.setdefault(t, [0, set()])
        e[0] += 1
        e[1].add(path)
    assert sorted((r["q"], r["rows"], r["paths"]) for r in qs.to_pylist()) == sorted((t, n, len(p)) for t, (n, p) in per_q.items())
    dirs, hist, _ = sr.dir_stats(con, "rt", floor)
    brute = _brute_dirs(roots)
    assert sorted((r["q"], r["dir"], r["rows"], r["paths"], r["children"]) for r in dirs.to_pylist()) == sorted(
        (t, d, n, p, c) for (t, d), (n, p, c) in brute.items() if n >= floor)
    assert sum(r["dirs"] for r in hist.to_pylist()) == len(brute)


def test_partitioned_dir_stats_sum_to_whole(built):  # noqa: F811
    """`measure-short`'s subtree partitions: directories at depth ≥ 2 are each whole in one partition, and the
    depth-1 partials sum to the whole (children included)."""
    root, scans, merged, ranges, plan, build, out, con, tmp = built
    src = f"SELECT * FROM read_parquet({sn.q(str(build / 'cintervals' / '*.parquet'))})"
    con.execute("DROP TABLE IF EXISTS sw")
    sr.short_roots(con, src, "sw", agg=True)
    whole, _, _ = sr.dir_stats(con, "sw", 1)
    whole = sorted((r["q"], r["k"], r["dir"], r["rows"], r["paths"], r["children"], r["direct"], r["max_child"]) for r in whole.to_pylist())
    deep, top = [], {}
    for t in range(3):
        con.execute("DROP TABLE IF EXISTS sp")
        sr.short_roots(con, f"SELECT * FROM ({src}) WHERE {sr._partition(3)} = {t}", "sp", agg=True)
        d, _, tp = sr.dir_stats(con, "sp", 1, partial_top=True)
        deep += [(r["q"], r["k"], r["dir"], r["rows"], r["paths"], r["children"], r["direct"], r["max_child"]) for r in d.to_pylist()]
        for r in tp.to_pylist():
            e = top.setdefault((r["q"], 1, r["dir"]), [0, 0, 0, 0, 0])
            e[:4] = [e[0] + r["rows"], e[1] + r["paths"], e[2] + r["children"], e[3] + r["direct"]]
            e[4] = max(e[4], r["max_child"])
    assert len(whole) > 50
    assert sorted(deep + [(*k, *v) for k, v in top.items()]) == whole


def _brute_view(versions: list[tuple], t: str, P: str, date: str) -> dict[str, list[int]]:
    D = sn.scan_epoch(date)
    acc: dict[str, list[int]] = {}
    for depth, path, usr, vf, vt, size, n_files in versions:
        if depth >= 1 and vf <= D < vt and path.startswith(P + "/") and _is_root(t, path):
            e = acc.setdefault(path[len(P) + 1:].split("/", 1)[0], [0, 0])
            e[0] += size
            e[1] += n_files
    return {k: v for k, v in sorted(acc.items()) if v != [0, 0]}


def _local(out):
    def fetch(file: str, lo: int, hi: int) -> bytes:
        with open(out / file, "rb") as fh:
            fh.seek(lo)
            return fh.read(hi - lo)

    return fetch, lambda file: (out / file).stat().st_size


def _two_level(out, R: int, rg: int, aliases: dict | None = None, idx_rg: int = 3):
    """A `Drill` over `build_roots`' output in `out`, through two-level indexes (`write_index_levels`), as the Worker reads it."""
    import pyarrow.parquet as pq

    fetch, size_of = _local(out)
    files = {}
    old, sr.IDX_RG = sr.IDX_RG, idx_rg
    try:
        for sub in ("roots", "rollups"):
            t = pq.read_table(out / f"{sub}-index" / "x.parquet")
            top = sr.write_index_levels(t, out / f"{sub}-index.parquet")
            assert top.num_rows == -(-t.num_rows // idx_rg)  # an empty index: an empty top
            files[sub] = sr.GroupFile(None, fetch, size_of, top=top, index_file=f"{sub}-index.parquet")
    finally:
        sr.IDX_RG = old
    return sr.Drill(files["roots"], files["rollups"], R, rg, aliases)


@pytest.mark.parametrize("R,K,rg", [(3, 2, 4), (10, 1, 8), (10**6, 5, 8192)])
def test_drill_equals_brute_force(built, tmp_path, R, K, rg):  # noqa: F811
    root, scans, merged, ranges, plan, build, out, con, tmp = built
    dates = scan_ids(scans)
    rows, terms = _member_roots(built, agg=False, chunk_rows=7)
    con.execute("DROP TABLE IF EXISTS st")
    for r in ranges["ranges"]:
        src = str(build / "cintervals" / f"r{r['i']:04d}.parquet")
        sr.short_roots(con, f"SELECT * FROM read_parquet({sn.q(src)})", "st")
    old, sr.ROOT_RG = sr.ROOT_RG, rg
    try:
        docs = {kind: sr.build_roots(con, table, R, K, tmp_path / kind, "x") for kind, table in (("long", "rt"), ("short", "st"))}
    finally:
        sr.ROOT_RG = old
    versions = _versions(merged)
    shorts = {r[0] for r in con.execute("SELECT DISTINCT q FROM st").fetchall()}
    drills = {}
    for kind in ("long", "short"):
        drills[kind] = _two_level(tmp_path / kind, R, rg)
    dirs = sorted({p.rsplit("/", 1)[0] for _, p, *_ in versions if "/" in p} | {"nope", "a"})
    sources: dict[str, int] = {}
    for t in sorted(terms | shorts):
        drill = drills["short" if len(t) <= 2 else "long"]
        for P in dirs:
            got = drill.view(t, P, dates)
            sources[got["source"]] = sources.get(got["source"], 0) + 1
            if got["source"] == "plain":
                assert t in P.lower()
                continue
            for d in dates:
                exp = _brute_view(versions, t, P, d)
                if got["source"] == "roots":
                    assert got["answers"][d] == exp, (t, P, d)
                else:
                    kept = got["answers"][d]
                    assert kept == {c: v for c, v in exp.items() if c in kept}, (t, P, d)
                    rest = [sum(v[i] for c, v in exp.items() if c not in kept) for i in (0, 1)]
                    assert got["rest"][d] == rest, (t, P, d)
                    assert got["header"]["kept"] <= K
            if got["source"] == "roots":
                assert got["rows"] <= R + 2 * rg
    assert sources.get("roots", 0) > 100
    if R < 100:  # rollups are read only past the dispatch bound R + 2·rg
        assert sources.get("rollup", 0) > (10 if R + 2 * rg < 15 else 0), (sources, docs)


def test_brute_view_sql_equals_oracle(fixture):  # noqa: F811
    """`drill-brute` straight from each scan file equals the versions' first hits under P on that date."""
    root, scans, merged = fixture
    versions = _versions(merged)
    dirs = sorted({p.rsplit("/", 1)[0] for _, p, *_ in versions if "/" in p})
    terms = ["gof", "5418", "pio", "a'b", "é5", "b", "1", "zzz", "/"]
    cases = [(t, P) for t in terms for P in dirs]
    con = sn.connect(2, "1GB", None)
    con.execute("CREATE TABLE cases (term VARCHAR, P VARCHAR)")
    con.executemany("INSERT INTO cases VALUES (?, ?)", cases)
    nonzero = 0
    for s in scans["scans"]:
        got: dict = {c: {} for c in cases}
        for term, P, child, b_, o_ in con.execute(sr.brute_view_sql(str(root / s["src"]), s["version"], "cases")).fetchall():
            if b_ or o_:
                got[(term, P)][child] = [b_, o_]
        assert got == {(t, P): _brute_view(versions, t, P, s["id"]) for t, P in cases}, s["id"]
        nonzero += sum(1 for v in got.values() if v)
    assert nonzero > 20


def test_digests_identify_equal_root_sets(built):  # noqa: F811
    """Chunked digests: equal exactly when two members' root sets are equal; and a build over canonical members
    only, read through the aliases, answers every member as brute force."""
    root, scans, merged, ranges, plan, build, out, con, tmp = built
    dates = scan_ids(scans)
    _member_roots(built, agg="digest", chunk_rows=7, into="dg")
    dig = {r[0]: r[1:] for r in con.execute("SELECT q, sum(n), sum(h1) % 18446744073709551616, sum(h2) % 18446744073709551616 FROM dg GROUP BY q").fetchall()}
    rows, terms = _member_roots(built, agg=False, chunk_rows=10**9)
    sets: dict[str, frozenset] = {}
    for r in rows:
        sets.setdefault(r[0], set()).add(r[1:])
    sets = {k: frozenset(v) for k, v in sets.items()}
    assert set(dig) == set(sets)
    pairs = [(a, b) for a in sets for b in sets if a < b]
    assert [(a, b) for a, b in pairs if dig[a] == dig[b]] == [(a, b) for a, b in pairs if sets[a] == sets[b]]
    canon = {t: min(u for u in sets if dig[u] == dig[t]) for t in sets}
    assert len(set(canon.values())) < len(canon)
    con.execute("CREATE OR REPLACE TABLE rtc AS SELECT * FROM rt WHERE q IN (SELECT unnest(?))", [sorted(set(canon.values()))])
    sr.build_roots(con, "rtc", 3, 2, tmp / "alias", "x")
    drill = _two_level(tmp / "alias", 3, sr.ROOT_RG, {k: v for k, v in canon.items() if k != v})
    versions = _versions(merged)
    dirs = sorted({p.rsplit("/", 1)[0] for _, p, *_ in versions if "/" in p})
    for t in sorted(terms):
        for P in dirs:
            got = drill.view(t, P, dates)
            if got["source"] == "roots":
                assert got["answers"] == {d: _brute_view(versions, t, P, d) for d in dates}, (t, P)


@pytest.mark.parametrize("small", [True, False])
def test_drill_verify(built, tmp_path, small, monkeypatch):  # noqa: F811
    """`drill-verify` over the drill's answers and references written as `drill-brute` writes them (with
    `BRUTE_CHILDREN` = 1 and only the rollup cases, every reference lists only the kept children): all equal;
    and one altered reference fails."""
    import json

    from click.testing import CliRunner
    root, scans, merged, ranges, plan, build, out, con, tmp = built
    dates = scan_ids(scans)
    rows, terms = _member_roots(built, agg=False, chunk_rows=7)
    monkeypatch.setattr(sr, "ROOT_RG", 4)
    sr.build_roots(con, "rt", 3, 2, tmp_path / "long", "x")
    drill = _two_level(tmp_path / "long", 3, 4)
    versions = _versions(merged)
    dirs = sorted({p.rsplit("/", 1)[0] for _, p, *_ in versions if "/" in p})
    cap = 1 if small else 10**6
    monkeypatch.setattr(sr, "BRUTE_CHILDREN", cap)
    answers, refs = [], []
    for t in sorted(terms):
        for P in dirs:
            a = drill.view(t, P, dates)
            if small and a["source"] != "rollup":  # a roots case never has a partial reference (≤ R + 2·rg roots)
                continue
            a["s"] = 0
            answers.append(a)
            for d in dates:
                exp = _brute_view(versions, t, P, d)
                total = [sum(v[i] for v in exp.values()) for i in (0, 1)]
                kept = set(a.get("kept") or [])
                children = exp if len(exp) <= cap else {c: v for c, v in exp.items() if c in kept}
                refs.append({"date": d, "q": t, "P": P, "total": total, "n": len(exp), "children": children})
    (tmp_path / "a.jsonl").write_text("".join(json.dumps(a) + "\n" for a in answers))
    (tmp_path / "r.jsonl").write_text("".join(json.dumps(r) + "\n" for r in refs))
    res = CliRunner().invoke(sr.drill_verify_cmd, [str(tmp_path / "r.jsonl"), str(tmp_path / "a.jsonl")])
    report = json.loads(res.output)
    assert (res.exit_code, report["equal"], report["diff"]) == (0, report["pairs"], {})
    assert report["by_source"]["rollup"] > 10
    assert sum(1 for r in refs if r["n"] > cap) > (0 if small else -1)
    bad = next(r for r in refs if r["total"] != [0, 0] and any(a["q"] == r["q"] and a["P"] == r["P"] and a["source"] == "rollup" for a in answers))
    bad["total"][0] += 1
    (tmp_path / "r.jsonl").write_text("".join(json.dumps(r) + "\n" for r in refs))
    res = CliRunner().invoke(sr.drill_verify_cmd, [str(tmp_path / "r.jsonl"), str(tmp_path / "a.jsonl")])
    assert (res.exit_code, list(json.loads(res.output)["diff"])) == (1, [f"{bad['q']} {bad['P']} {bad['date']}"])


@pytest.mark.parametrize("n_bounds", [1, 3, 100])
def test_ranged_assigns_the_asof_ranges(tmp_path, n_bounds):
    """`ranged`: every row with the range an ASOF join over every row assigns it (the last `lo <= q`), the same rows, at a
    bound count under and over DuckDB's loop-join threshold; and over a given key query (`keys`) holding every `q`."""
    import random

    con = sn.connect(2, "256MB", tmp_path / "spill")
    rnd = random.Random(3)
    lits = sorted({"".join(rnd.choice("abcé'") for _ in range(rnd.randrange(1, 5))) for _ in range(300)})
    con.execute("CREATE TABLE rt (q VARCHAR, x BIGINT)")
    con.executemany("INSERT INTO rt VALUES (?, ?)", [(rnd.choice(lits), i) for i in range(2000)])
    bounds = ["", *sorted(rnd.sample(lits, n_bounds - 1))]
    con.execute("CREATE TABLE qg (lo VARCHAR, grp INTEGER)")
    con.executemany("INSERT INTO qg VALUES (?, ?)", [(lo, g) for g, lo in enumerate(bounds)])
    want = con.execute("SELECT r.*, g.grp FROM rt AS r ASOF JOIN qg AS g ON r.q >= g.lo ORDER BY x").fetchall()
    assert len(want) == 2000
    assert con.execute(f"SELECT * FROM ({sr.ranged('rt', 'qg', 'grp')}) ORDER BY x").fetchall() == want
    con.execute("CREATE TABLE qk AS SELECT DISTINCT q FROM rt")
    assert con.execute(f"SELECT * FROM ({sr.ranged('(SELECT * FROM rt)', 'qg', 'grp', keys='SELECT q FROM qk')}) ORDER BY x").fetchall() == want
    # a row whose `q` the keys miss fails the query (the per-row ASOF join kept it): never a silently smaller spill
    miss = con.execute("SELECT q FROM rt ORDER BY x LIMIT 1").fetchone()[0]
    with pytest.raises(duckdb.InvalidInputException, match=f"^Invalid Input Error: ranged: q '{re.escape(miss)}' has no range$"):
        con.execute(f"SELECT * FROM ({sr.ranged('rt', 'qg', 'grp', keys=f'SELECT q FROM qk WHERE q <> {sn.q(miss)}')})").fetchall()
    con.close()


def test_index_estimates_spread_each_group_over_its_keys(tmp_path):
    """`index_estimates`: a group's rows over the keys in its `[q_min, q_max]`, evenly, the remainder to the first; a group
    of one `q` exact; a group holding no key dropped; a key no group covers absent. Over several index files."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    groups = [("A", "A", 3), ("a", "a", 10), ("a", "c", 7), ("bb", "bc", 4), ("c", "c", 5), ("d", "z", 9)]
    files = []
    for k, part in enumerate((groups[:3], groups[3:])):
        f = tmp_path / f"i{k}.parquet"
        pq.write_table(pa.table({"q_min": [g[0] for g in part], "q_max": [g[1] for g in part], "rows": pa.array([g[2] for g in part], pa.int32())}), f)
        files.append(str(f))
    con = sn.connect(2, "256MB", tmp_path / "spill")
    con.execute("CREATE TABLE ks AS SELECT unnest(['a', 'b', 'c', 'd', 'e', 'f', 'zz', NULL]) AS q")
    sr.index_estimates(con, files, "SELECT q FROM ks", "est")
    assert con.execute("SELECT q, n FROM est ORDER BY q").fetchall() == [("a", 13), ("b", 2), ("c", 7), ("d", 3), ("e", 3), ("f", 3)]
    sr.index_estimates(con, [], "SELECT q FROM ks", "none")
    assert con.execute("SELECT q, n FROM none").fetchall() == []
    con.close()


@pytest.mark.parametrize("parts", [1, 2, 3, 7, 64])
def test_chunk_spill_reads_each_chunk_exactly(tmp_path, parts):
    """`ChunkSpill`: at most `parts` partitions of consecutive chunks, and each chunk's files hold exactly its rows (both
    inputs'), read in order; a partition is removed once the reader passes it."""
    con = sn.connect(2, "256MB", tmp_path / "spill")
    n = 7
    con.execute("CREATE TABLE a AS SELECT (x % 7)::BIGINT AS ch, x, 'a' AS src FROM range(200) AS t(x)")
    con.execute("CREATE TABLE b AS SELECT ((x * 3) % 7)::BIGINT AS ch, x, 'b' AS src FROM range(50) AS t(x)")
    sp = sr.ChunkSpill(con, tmp_path / "s", n, parts)
    sp.write("SELECT * FROM a", "t000")
    sp.write("SELECT * FROM b", "t001")
    p = min(parts, n)
    assert (sp.p, [sp.part(k) for k in range(n)]) == (p, [k * p // n for k in range(n)])
    assert [list(sp.chunks(g)) for g in range(p)] == [[k for k in range(n) if k * p // n == g] for g in range(p)]
    assert sorted(d.name for d in (tmp_path / "s" / "in" / "t000").iterdir()) == sorted(f"sp={g}" for g in range(p))
    got = {}
    for k in range(n):
        files = sp.files(k)
        got[k] = con.execute(f"SELECT src, x FROM read_parquet([{', '.join(sn.q(str(f)) for f in files)}], hive_partitioning = false) ORDER BY src, x").fetchall()
        # the partitions before this chunk's are gone; its own and the later ones are held
        assert sorted({x for f in (tmp_path / "s").rglob("*.parquet") for x in f.parts if x.startswith("sp=")}) == [f"sp={g}" for g in range(sp.part(k), p)]
    want = {k: con.execute(f"SELECT src, x FROM (SELECT * FROM a UNION ALL SELECT * FROM b) WHERE ch = {k} ORDER BY src, x").fetchall() for k in range(n)}
    assert got == want
    sp.close()
    assert not (tmp_path / "s").exists()
    con.close()


@pytest.mark.parametrize("parts", [1, 2, 3, 7])
def test_chunk_spill_prepared_partitions_read_in_any_order(tmp_path, parts):
    """`ChunkSpill.prepare`: a partition's chunks' files, read in any order (here: last chunk first), hold exactly each
    chunk's rows; `release` removes that partition only."""
    con = sn.connect(2, "256MB", tmp_path / "spill")
    n = 7
    con.execute("CREATE TABLE a AS SELECT (x % 7)::BIGINT AS ch, x FROM range(200) AS t(x)")
    sp = sr.ChunkSpill(con, tmp_path / "s", n, parts)
    sp.write("SELECT * FROM a", "t000")
    got = {}
    for g in range(sp.p):
        files = sp.prepare(g)
        assert sorted(files) == list(sp.chunks(g))
        for k in reversed(sorted(files)):
            got[k] = con.execute(f"SELECT x FROM read_parquet([{', '.join(sn.q(str(f)) for f in files[k])}], hive_partitioning = false) ORDER BY x").fetchall()
        sp.release(g)
        assert sorted({x for f in (tmp_path / "s").rglob("*.parquet") for x in f.parts if x.startswith("sp=")}) == [f"sp={h}" for h in range(g + 1, sp.p)]
    assert got == {k: [(x,) for x in range(k, 200, 7)] for k in range(n)}
    sp.close()
    con.close()


def test_chunk_spill_defaults_to_one_partition_per_chunk_up_to_spill_direct(tmp_path):
    """Up to `SPILL_DIRECT` chunks each gets its own partition (no split pass on reading); past it, `SPILL_PARTS`."""
    sizes = [1, sr.SPILL_DIRECT, sr.SPILL_DIRECT + 1, 4858]
    assert [sr.ChunkSpill(None, tmp_path / f"s{n}", n).p for n in sizes] == [1, sr.SPILL_DIRECT, sr.SPILL_PARTS, sr.SPILL_PARTS]
