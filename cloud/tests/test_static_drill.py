"""`dt_cloud.static_drill`: a base drill plus per-scan runs (each separately, and merged as the binary counter merges them),
read as the Worker reads them, equals the drill rebuilt over every scan through D — every member's roots, every heavy
directory's rollup — and every filtered view at every directory and date equals brute force. The scans are designed so the
runs hit each hard case: classes splitting, a child entering the kept set (an existing one and a new one), a directory
becoming heavy, a literal becoming a member, probes either way, closes, resurrection and multi-owner paths."""
from __future__ import annotations

import random
import re
import shutil
from itertools import groupby
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import static_append as sa
from dt_cloud import static_catalog as sc
from dt_cloud import static_drill as sd
from dt_cloud import static_names as sn
from dt_cloud import static_roots as sr

from test_static_catalog import _gen
from test_static_names import _coalesced_oracle, _merged, _oracle, _write

DAYS = ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
BASE = 3  # scans in the base generation; the rest are runs
V = 2
CONFIGS = [{"R": 3, "K": 2, "floor": 2, "rg": 4, "run_rg": 2}, {"R": 5, "K": 1, "floor": 3, "rg": 3, "run_rg": 3}]
NOISE = ["gof.txt", "x5418y", "nk080", "48.parquet", "a'b", "é5418", "sh", "ab", "ckpt_old.bin", "data.jsonl"]


def _files(j: int) -> dict[str, list[tuple[str | None, int]]]:
    """Scan `j`'s files `{path: [(owner, bytes)]}`; each a case for the runs (scans BASE … BASE + 3)."""
    rng = random.Random(100 + j)
    f: dict[str, list[tuple[str | None, int]]] = {}

    def add(p: str, size: int, usr: str | None = None) -> None:
        f.setdefault(p, []).append((usr, size))

    for r, (n, s) in {"run1": (3, 100), "run2": (2, 80), "run3": (1, 10), "run4": (1, 5), "run5": (1, 3)}.items():
        for i in range(n):
            add(f"b1/runs/{r}/ckpt-{i}.bin", s)
    if j >= 3:  # an existing directory child outside the kept set grows past it
        for i in range(1, 4):
            add(f"b1/runs/run4/ckpt-{i}.bin", 100)
    if j >= 4:  # a new directory child with more than any kept child: one leaves
        for i in range(2):
            if not (i == 1 and j == 5):  # closed at 5, back at 6
                add(f"b1/runs/run6/ckpt-{i}.bin", 500)
    if j >= 5:  # another newcomer past the kept set, after the counter's first merge
        for i in range(3):
            add(f"b1/runs/run7/ckpt-{i}.bin", 700 + j)
    add("b1/runs/runx/notes.txt", 7)
    if j >= 3:  # a directory child with no earlier `ckpt` root: probed, new
        add("b1/runs/runx/ckpt-0.bin", 1)
    for i in range(6):
        if (i == 3 and j == 2) or (i == 1 and j == 4):  # f3 gone for a scan and back (resurrection); f1 deleted (closes)
            continue
        add(f"b1/data/f{i}.json", 1000 if (i == 5 and j >= 3) else 10 + i)  # f5 re-versioned past the kept set
    if j >= 3:
        add("b1/data/x.js", 4)  # `.js` leaves `.json`'s class
    add("b2/new/ckpt-a.bin", 2)
    if j >= 3:  # b2/new becomes heavy
        for c in "bcdef":
            add(f"b2/new/ckpt-{c}.bin", 2)
    if j >= 3:  # `zebra` becomes a member
        for i in range(4):
            add(f"b2/zoo/zebra{i}.txt", 1)
    if j >= 4:
        add("b2/zoo/zebra9.txt", 1)
    if j >= 6:  # b4/deep becomes heavy in a run built over a merged run
        for c in "abcdefg":
            add(f"b4/deep/ckpt-{c}.bin", 3)
        add("b2/zoo/zebra10.txt", 2)
    add("b2/mix/ckpt-m.bin", 30)
    add("b2/mix/ckpt-m.bin", 20 if j < 3 else 25, "alice")  # one owner's slice changes
    for d in ("b3/p", "b3/q", "b3/p/r"):
        for n in NOISE:
            if rng.random() < 0.6:
                add(f"{d}/{n}", rng.choice([1, 2, 3, 50]))
    return f


def _rows(files: dict) -> list[dict]:
    """A scan's rows: the files and every directory (per owner: Σ bytes and files under it), as a layer-2 rolls them up."""
    acc: dict[tuple[str, str | None], list] = {}
    for p, slices in files.items():
        parts = p.split("/")
        for usr, size in slices:
            for k in range(1, len(parts) + 1):
                e = acc.setdefault(("/".join(parts[:k]), usr), [0, 0, k == len(parts)])
                e[0] += size
                e[1] += 1
    return [{"path": p, "depth": p.count("/") + 1, "usr": usr, "size": s, "n_files": n, "mtime_mean": 1.0, "last_read": 5,
             "kind": "file" if leaf else "dir"} for (p, usr), (s, n, leaf) in sorted(acc.items(), key=lambda x: (x[0][0], x[0][1] or ""))]


def _sx_files(d: Path) -> list[str]:
    return [str(p) for p in sorted((d / "sx").glob("*.parquet"))]


def _reader(d: Path) -> sn.Reader:
    def fetch(file: str, lo: int, hi: int) -> bytes:
        with open(d / file, "rb") as fh:
            fh.seek(lo)
            return fh.read(hi - lo)

    return sn.Reader(fetch, lambda f: (d / f).stat().st_size, pq.read_table(d / "sidecar.parquet"))


def _drill(con, g: dict, out: Path, R: int, K: int, floor: int) -> dict[str, str]:
    """A generation's whole drill (`build_drill`, aliases by exact root set) in `out`, and its measurement files
    `{kind: path}` (`(q, k, dir, rows)` at ≥ `floor` rows, as `roots measure`)."""
    members = sorted({r["q"] for r in pq.read_table(g["final"] / "cells.parquet").to_pylist() if r["bucket"] == "" and len(r["q"]) >= 3})
    con.execute("CREATE OR REPLACE TABLE mem (q VARCHAR)")
    con.executemany("INSERT INTO mem VALUES (?)", [(m,) for m in members])
    con.execute("DROP TABLE IF EXISTS lall; DROP TABLE IF EXISTS sall")
    sx = "[" + ", ".join(sn.q(f) for f in _sx_files(g["out"])) + "]"
    sr.member_roots(con, sr.sx_rows_sql(f"read_parquet({sx})"), "mem", "lall")
    acc: dict[str, list] = {m: [] for m in members}
    for qq, *row in con.execute("SELECT q, path, usr, vf, vt, size, n_files FROM lall ORDER BY ALL").fetchall():
        acc[qq].append(tuple(row))
    sets = {m: tuple(rows) for m, rows in acc.items()}
    canon: dict[tuple, str] = {}
    for m in members:
        canon.setdefault(sets[m], m)
    aliases = pa.table({"q": members, "canonical": [canon[sets[m]] for m in members]})
    con.execute("CREATE OR REPLACE TABLE canon (q VARCHAR)")
    con.executemany("INSERT INTO canon VALUES (?)", [(c,) for c in sorted(set(canon.values()))])
    con.execute("CREATE OR REPLACE TABLE lcan AS SELECT * FROM lall SEMI JOIN canon USING (q)")
    ci = "[" + ", ".join(sn.q(str(p)) for p in sorted((g["build"] / "cintervals").glob("*.parquet"))) + "]"
    sr.short_roots(con, f"SELECT * FROM read_parquet({ci})", "sall")
    sd.build_drill(con, out, long_roots="lcan", short_roots="sall", aliases=aliases, R=R, K=K)
    meas = {}
    for kind, table in (("long", "lall"), ("short", "sall")):
        con.execute(f"""CREATE OR REPLACE TABLE rp AS SELECT q, depth, path, count(*) AS n, count(*) FILTER (WHERE vt = {sn.OPEN}) AS n_open
            FROM {table} GROUP BY q, depth, path""")
        dirs, _, _ = sr.dir_stats(con, "rp", floor)
        meas[kind] = str(out.parent / f"meas-{kind}.parquet")
        pq.write_table(dirs.select(["q", "k", "dir", "rows"]), meas[kind])
    return meas


@pytest.fixture(scope="module", params=range(len(CONFIGS)), ids=lambda i: "R{R}-K{K}".format(**CONFIGS[i]))
def world(request, tmp_path_factory):
    cfg = CONFIGS[request.param]
    R, K, floor = cfg["R"], cfg["K"], cfg["floor"]
    root = tmp_path_factory.mktemp("drill")
    scans, merged = [], []
    for j, d in enumerate(DAYS):
        rows = _rows(_files(j))
        key = f"listing/{d}/path-index.parquet"
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        _write(rows, root / key, 2)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": 2})
        merged.append((sn.scan_epoch(d), _merged(rows, 2)))
    gens = {j: _gen(root, {"bucket": "b", "scans": scans[:j + 1]}, root / f"gen{j}", V) for j in range(BASE - 1, len(DAYS))}
    base = gens[BASE - 1]
    pq.write_table(base["side"], base["out"] / "sidecar.parquet")
    con = base["con"]
    old_rg, sr.ROOT_RG = sr.ROOT_RG, cfg["rg"]
    old_idx, sr.IDX_RG = sr.IDX_RG, 3
    try:
        drills, meas = {}, None
        for j, g in gens.items():
            m = _drill(con, g, root / f"drill{j}" / "drill", R, K, floor)
            drills[j] = sd.Tier(root / f"drill{j}" / "drill", f"rebuild-{j}")
            meas = m if j == BASE - 1 else meas
        # the light runs (static_append), then each run's drill
        shards = sc.BaseShards(str(base["out"]), base["side"])
        prev = {r["i"]: f"SELECT * FROM read_parquet({sn.q(str(base['build'] / 'cintervals' / f'r{r['i']:04d}.parquet'))})" for r in base["ranges"]["ranges"]}
        runs, deltas, prior, metas = [], [], [sd.Tier(root / f"drill{BASE - 1}" / "drill", "base")], []
        cintervals = [str(p) for p in sorted((base["build"] / "cintervals").glob("*.parquet"))]
        # the binary counter's tiers (as `publish` merges them: `push_run`, `merge_tiers`), `(run, tier)` oldest first,
        # each scan's drill built over them; and the level-0 tiers it built (for the heaviness bound)
        counter: list[tuple[dict, sd.Tier]] = []
        counter_built: list[sd.Tier] = []
        counter_stacks: dict[int, list[sd.Tier]] = {}

        def build(scan: dict, run: Path, cdelta: list[str], over: list[sd.Tier], out: Path, runs: list[Path], deltas: list[list[str]], **kw) -> dict:
            con.execute(f"CREATE OR REPLACE TABLE pnew AS {sd.pnew_sql(cdelta, cintervals + [f for d in deltas for f in d])}")
            heads = {r["q"] for r in pq.read_table(run / "catalog" / "cells.parquet").to_pylist() if r["bucket"] == "" and len(r["q"]) >= 3}
            new = sorted(heads - set(over[-1].alias_table().column("q").to_pylist()))
            history = sd.history_rows([_reader(base["out"]), *(_reader(d) for d in runs), _reader(run)], new)
            return sd.build_day(con, over, out, D=scan["ts"], R=R, K=K, floor=floor, sx_files=_sx_files(run), cdelta_files=cdelta,
                                new_members=new, history=history, meas={k: sd.meas_sql(k, [v]) for k, v in meas.items()},
                                tier={"first": scan["id"], "last": scan["id"], "level": 0, "scans": [scan["id"]]}, rg=cfg["run_rg"],
                                log=lambda *_: None, **kw)

        rebuilds = []  # each scan's run build, again (`rebuild(out, **build_day kwargs)`), over the separate runs before it

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
            metas.append(build(scan, run, cdelta, prior, run / "drill", runs, deltas))
            rebuilds.append(lambda out, _a=(scan, run, cdelta), _p=list(prior), _r=list(runs), _d=list(deltas), **kw:
                            build(*_a, [sd.Tier(t.root, t.name) for t in _p], out, _r, _d, **kw))  # fresh tiers: their read counts from 0
            over = [prior[0], *(t for _, t in counter)]
            if [t.root for t in over] == [t.root for t in prior]:  # the counter's tiers are the separate runs: the same build
                tier = sd.Tier(run / "drill", scan["id"])
            else:
                build(scan, run, cdelta, over, root / "counter" / scan["id"] / "drill", runs, deltas)
                tier = sd.Tier(root / "counter" / scan["id"] / "drill", f"counter-{scan['id']}")
            counter_built.append(tier)
            key = sa.run_key(scan["id"], scan["id"])
            after, merges = sa.push_run([r for r, _ in counter], {"key": key, "first": scan["id"], "last": scan["id"], "scans": [scan["id"]]},
                                        {r["key"] for r, _ in counter} | {key})
            tiers = {**{r["key"]: t for r, t in counter}, key: tier}
            for ins, m in merges:
                out = root / "counter" / m["key"] / "drill"
                sd.merge_tiers(con, [tiers[r["key"]] for r in ins], out, tier={k: m[k] for k in ("key", "first", "last", "level", "scans")})
                tiers[m["key"]] = sd.Tier(out, m["key"])
            counter = [(r, tiers[r["key"]]) for r in after]
            counter_stacks[j] = [prior[0], *(t for _, t in counter)]
            runs.append(run)
            deltas.append(cdelta)
            prior.append(sd.Tier(run / "drill", scan["id"]))
        # a merged run's drill backfilled from its scans' level-0 drills, each built over the separate runs before it
        # (`static_merge.backfill_tier`: cw's `deltas/2026-10-09T1801_2026-10-10T0001`, merged before drill was on)
        a, b = scans[BASE]["id"], scans[BASE + 1]["id"]
        bf = root / "backfill" / sa.run_key(a, b) / "drill"
        sd.merge_tiers(con, prior[1:3], bf, tier={"key": sa.run_key(a, b), "first": a, "last": b, "level": 1, "scans": [a, b]})
        backfill = sd.Tier(bf, sa.run_key(a, b))
    finally:
        sr.ROOT_RG, sr.IDX_RG = old_rg, old_idx
    return {"cfg": cfg, "base_out": base["out"], "run_dirs": runs, "scans": scans, "merged": merged, "drills": drills, "base": prior[0], "runs": prior[1:],
            "counter": counter_stacks, "counter_built": counter_built, "metas": metas, "con": con, "backfill": backfill, "rebuilds": rebuilds}


# (name, last scan index, tiers, the level-0 tiers they were built from): every run apart, and the binary counter's
# tiers after each scan (`2+1`: a level-1 run of two scans, then a level-0 one built over it)
STACKS = [("run1", BASE, lambda w: [w["base"], w["runs"][0]], lambda w: w["runs"][:1]),
          ("runs1-2", BASE + 1, lambda w: [w["base"], *w["runs"][:2]], lambda w: w["runs"][:2]),
          ("runs1-3", BASE + 2, lambda w: [w["base"], *w["runs"][:3]], lambda w: w["runs"][:3]),
          ("runs1-4", BASE + 3, lambda w: [w["base"], *w["runs"]], lambda w: w["runs"]),
          ("counter-2", BASE + 1, lambda w: w["counter"][BASE + 1], lambda w: w["counter_built"][:2]),
          ("counter-2+1", BASE + 2, lambda w: w["counter"][BASE + 2], lambda w: w["counter_built"][:3]),
          ("counter-4", BASE + 3, lambda w: w["counter"][BASE + 3], lambda w: w["counter_built"]),
          ("backfill-2+1+1", BASE + 3, lambda w: [w["base"], w["backfill"], *w["runs"][2:]], lambda w: w["runs"])]


def _members(tier: sd.Tier, kind: str) -> list[str]:
    if kind == "long":
        return sorted(tier.alias_table().column("q").to_pylist())
    return sorted({r for f in tier.files("short", "roots") for r in pq.read_table(f, columns=["q"]).column("q").to_pylist()})


def _rollup(tiers: list[sd.Tier], kind: str, t: str, d: str) -> tuple | None:
    """The reader's rollup of `(t, d)` over `tiers`: `(header (kept, rows, children), sorted cells)`, or None."""
    header, cells = None, []
    for tier in reversed(tiers):
        gf = tier.group_file(kind, "rollups")
        c = tier.canon(t, kind)
        rows = gf.read((c, d), (c, d + "\x00"))[0] if gf is not None else []
        if not rows:
            continue
        header = header or rows[0]
        cells += rows[1:]
        if rows[0]["kind"] == sd.KIND_FULL:
            break
    if header is None:
        return None
    return ((header["vf"], header["b"], header["o"]), sorted((r["kind"], r["child"], r["vf"], r["b"], r["o"]) for r in cells))


def _heavy(tiers: list[sd.Tier], kind: str, members: list[str]) -> set[tuple[str, str]]:
    """Every `(member, dir)` with a rollup header in some tier."""
    out = set()
    for tier in tiers:
        heads = {(r["q"], r["dir"]) for f in tier.files(kind, "rollups") for r in pq.read_table(f).to_pylist() if r["kind"] in (sd.KIND_FULL, sd.KIND_DELTA)}
        by_canon: dict[str, list[str]] = {}
        for m in members:
            by_canon.setdefault(tier.canon(m, kind), []).append(m)
        out |= {(m, d) for c, d in heads for m in by_canon.get(c, [])}
    return out


@pytest.mark.parametrize("stack", STACKS, ids=[s[0] for s in STACKS])
@pytest.mark.parametrize("kind", sd.KINDS)
def test_roots_equal_rebuild(world, stack, kind):
    """Every member's roots over base ⊕ runs (each tier's own canonical, combined: smallest `vt`) are the rebuild's."""
    _, j, tiers_of, _ = stack
    tiers, rebuilt = tiers_of(world), world["drills"][j]
    members = _members(rebuilt, kind)
    if kind == "long":
        assert _members(tiers[-1], kind) == members
    got, want = sd.TierReads(tiers, kind), sd.TierReads([rebuilt], kind)
    nonempty = 0
    for t in members:
        a = sorted(tuple(r[k] for k in ("path", "usr", "vf", "vt", "size", "n_files")) for r in got.rows(t, "", "\U0010ffff"))
        assert a == sorted(tuple(r[k] for k in ("path", "usr", "vf", "vt", "size", "n_files")) for r in want.rows(t, "", "\U0010ffff")), t
        nonempty += bool(a)
    assert nonempty > 20


@pytest.mark.parametrize("stack", STACKS, ids=[s[0] for s in STACKS])
@pytest.mark.parametrize("kind", sd.KINDS)
def test_rollups_equal_rebuild(world, stack, kind):
    """Every rollup the rebuild holds, the tiers hold too, equal (header, kept children's cells, remainder's). A rollup only the
    tiers hold is a directory whose stored rows over the tiers (close records counted per tier) exceed R while its roots don't."""
    _, j, tiers_of, built_of = stack
    tiers, rebuilt = tiers_of(world), world["drills"][j]
    R = world["cfg"]["R"]
    members = _members(rebuilt, kind)
    want_heavy, got_heavy = _heavy([rebuilt], kind, members), _heavy(tiers, kind, members)
    assert want_heavy <= got_heavy
    for t, d in sorted(want_heavy):
        assert _rollup(tiers, kind, t, d) == _rollup([rebuilt], kind, t, d), (t, d)
    built = [world["base"], *built_of(world)]  # every level-0 tier: ≥ the rows each build counted (a merge only collapses rows)
    for t, d in sorted(got_heavy - want_heavy):
        stored = sum(len(sd.TierReads([tier], kind).rows(t, d + "/", d + "0")) for tier in built)
        assert len(sd.TierReads([rebuilt], kind).rows(t, d + "/", d + "0")) <= R < stored, (t, d)
    assert len(want_heavy) > 20


def _matching(versions: list[tuple], t: str) -> list[tuple]:
    """The versions whose name holds `t` and whose parent doesn't: a member's roots, every date."""
    out = []
    for v in versions:
        name = v[1].rsplit("/", 1)[-1].lower()
        parent = v[1].rsplit("/", 1)[0].lower() if "/" in v[1] else ""
        if v[0] >= 1 and t in name and t not in parent:
            out.append(v)
    return out


def _brute_view(matching: list[tuple], P: str, date: str) -> dict[str, list[int]]:
    """Brute force: per child of `P`, Σ (bytes, objects) of the matching versions live on `date` under it."""
    D = sn.scan_epoch(date)
    acc: dict[str, list[int]] = {}
    for depth, path, usr, vf, vt, size, n_files in matching:
        if vf <= D < vt and path.startswith(P + "/"):
            e = acc.setdefault(path[len(P) + 1:].split("/", 1)[0], [0, 0])
            e[0] += size
            e[1] += n_files
    return {k: v for k, v in sorted(acc.items()) if v != [0, 0]}


VIEW_STACKS = [s for s in STACKS if s[0] not in ("runs1-2", "runs1-3")]


@pytest.mark.parametrize("stack", VIEW_STACKS, ids=[s[0] for s in VIEW_STACKS])
def test_views_equal_brute_force(world, stack):
    """Every member's filtered view at every directory on every date, read over base ⊕ runs as the Worker reads it: a roots
    answer is brute force child for child; a rollup's kept children are brute force and its remainder the rest. Where the
    rebuild dispatches the same way, its answer is the same."""
    _, j, tiers_of, _ = stack
    tiers, rebuilt = tiers_of(world), world["drills"][j]
    versions = _coalesced_oracle(_oracle(world["merged"][:j + 1]))
    dates = DAYS[:j + 1]
    dirs = sorted({p.rsplit("/", 1)[0] for _, p, *_ in versions if "/" in p} | {"nope"})
    K = world["cfg"]["K"]
    sources = {"roots": 0, "rollup": 0, "plain": 0, "same": 0}
    for kind in sd.KINDS:
        got, want = sd.TieredDrill(tiers, kind), sd.TieredDrill([rebuilt], kind)
        for t in _members(rebuilt, kind):
            matching = _matching(versions, t)
            for P in dirs:
                a = got.view(t, P, dates)
                sources[a["source"]] += 1
                if a["source"] == "plain":
                    continue
                for d in dates:
                    exp = _brute_view(matching, P, d)
                    if a["source"] == "roots":
                        assert a["answers"][d] == exp, (t, P, d)
                    else:
                        kept = a["answers"][d]
                        assert kept == {c: v for c, v in exp.items() if c in kept}, (t, P, d)
                        assert a["rest"][d] == [sum(v[i] for c, v in exp.items() if c not in kept) for i in (0, 1)], (t, P, d)
                        assert a["header"]["kept"] <= K
                b = want.view(t, P, dates)
                if b["source"] == a["source"]:
                    sources["same"] += 1
                    assert {k: a[k] for k in ("answers", "rest", "header", "kept") if k in a} == {k: b[k] for k in ("answers", "rest", "header", "kept") if k in b}, (t, P)
                if a["source"] == "roots":
                    assert a["rows"] <= got.thr
    assert sources["roots"] > 300 and sources["rollup"] > 20 and sources["same"] > 300, sources


def _as_children(v: dict, d: str) -> tuple[dict[str, list[int]], list[int]]:
    """A view's answer on `d` as `(named children, the rest)`: a roots answer names every child (rest zero)."""
    if v["source"] == "roots":
        return v["answers"][d], [0, 0]
    return v["answers"][d], v["rest"][d]


@pytest.mark.parametrize("j", range(BASE + 1, len(DAYS)), ids=lambda j: f"scan{j - BASE + 1}")
def test_counter_reads_as_separate_runs(world, j):
    """`publish`'s merges change no answer: base ⊕ the binary counter's runs (merged by `merge_tiers`, later scans built
    over the merged ones) answers every member's view at every directory on every date as base ⊕ every run apart. Where
    both dispatch alike the views are equal (header and kept set too); where the dispatch bound differs (it grows with
    the tier count) a rollup's kept children are the roots answer's and its remainder the rest of it."""
    sep, ctr = [world["base"], *world["runs"][:j - BASE + 1]], world["counter"][j]
    assert len(ctr) < len(sep)
    versions = _coalesced_oracle(_oracle(world["merged"][:j + 1]))
    dates = DAYS[:j + 1]
    dirs = sorted({p.rsplit("/", 1)[0] for _, p, *_ in versions if "/" in p} | {"nope"})
    seen = {"same": 0, "crossed": 0, "plain": 0}
    for kind in sd.KINDS:
        a_of, b_of = sd.TieredDrill(ctr, kind), sd.TieredDrill(sep, kind)
        for t in _members(world["drills"][j], kind):
            for P in dirs:
                a, b = a_of.view(t, P, dates), b_of.view(t, P, dates)
                if a["source"] == "plain" or b["source"] == "plain":
                    assert a["source"] == b["source"] == "plain", (t, P)
                    seen["plain"] += 1
                elif a["source"] == b["source"]:
                    seen["same"] += 1
                    keys = ("source", "answers", "rest", "header", "kept")
                    assert {k: a[k] for k in keys if k in a} == {k: b[k] for k in keys if k in b}, (t, P)
                else:
                    seen["crossed"] += 1
                    for d in dates:
                        (ak, ar), (bk, br) = _as_children(a, d), _as_children(b, d)
                        small, big, rest = (ak, bk, ar) if a["source"] == "rollup" else (bk, ak, br)
                        assert small == {c: v for c, v in big.items() if c in small}, (t, P, d)
                        assert rest == [sum(v[i] for c, v in big.items() if c not in small) for i in (0, 1)], (t, P, d)
    assert seen["same"] > 300, seen


def test_the_runs_hit_every_case(world):
    """The fixture exercises each hard case at least once across the two runs."""
    long = [m["long_day"] for m in world["metas"]]
    short = [m["short_day"] for m in world["metas"]]
    cls = [m["classes"] for m in world["metas"]]
    assert any(c["classes"] > c["classes_before"] for c in cls)  # a class split
    assert sum(c["members_new"] for c in cls) >= 1  # a literal became a member
    assert sum(x["restated"] for x in long + short) >= 1  # an existing child entered or left the kept set
    assert sum(x["heavy_new"] for x in long + short) >= 1  # a directory became heavy
    assert sum(x["probes_existed"] for x in long + short) >= 1  # a probe finding an earlier root under the child
    assert sum(x["probes"] - x["probes_existed"] for x in long + short) >= 1  # and one finding none: a new child
    assert sum(x["candidates"] for x in long + short) >= 1
    assert sum(x["base_counted"] for x in long + short) >= 1  # a base count below the measurement's floor


def test_run_roots_are_deltas(world):
    """A run's roots are only rows opened or closed at its scan, except a new member's whole history."""
    for tier, meta in zip(world["runs"], world["metas"]):
        D = meta["D"]
        new = set()
        for kind in sd.KINDS:
            for f in tier.files(kind, "roots"):
                for r in pq.read_table(f).to_pylist():
                    if not (r["vf"] == D or r["vt"] == D):
                        new.add(r["q"])
        assert new <= {q for q in tier.alias_table().column("q").to_pylist() if q not in set(world["base"].alias_table().column("q").to_pylist())}


def test_history_table_equals_rows(world, tmp_path):
    """The build's DuckDB read of new members' tiered suffix rows equals the reference reader's, row for row."""
    con = world["con"]
    base_out = world["base_out"]
    run = world["run_dirs"][0]
    terms = world["metas"][0]["new_members_list"]
    assert terms
    want = sd.history_rows([_reader(base_out), _reader(run)], terms)
    got = sd.history_table(con, [str(base_out), str(run)], terms)
    key = lambda r: (r["s"], r["path"], r["usr"], r["vf"])  # noqa: E731
    assert sorted(got.to_pylist(), key=key) == sorted(want.to_pylist(), key=key)


def _tree(d: Path, drop: set[str] = frozenset({"s"})) -> dict[str, object]:
    """A drill dir's files: bytes, but `meta*.json` parsed, its build times (`s`) dropped."""
    def strip(x):
        return {k: strip(v) for k, v in x.items() if k not in drop} if isinstance(x, dict) else x

    import json

    return {str(f.relative_to(d)): strip(json.loads(f.read_text())) if f.name.startswith("meta") else f.read_bytes()
            for f in sorted(d.rglob("*")) if f.is_file()}


@pytest.fixture
def run_rg(world, monkeypatch):
    """The world's row-group sizes, as its builds wrote them (`IDX_RG` is read when writing the index levels)."""
    monkeypatch.setattr(sr, "ROOT_RG", world["cfg"]["rg"])
    monkeypatch.setattr(sr, "IDX_RG", 3)


@pytest.mark.parametrize("i", range(len(DAYS) - BASE), ids=lambda i: f"run{i + 1}")
def test_prefetch_writes_the_same_bytes(world, run_rg, tmp_path, i):
    """A run's drill is the same files, byte for byte, and the same `meta.json` (but build times) without prefetch (each miss
    fetched in the loops), with it, and with it under a byte cache too small for one window (every item its own window, the
    cache evicting: LRU misses fetched in the loops)."""
    rebuild = world["rebuilds"][i]
    rebuild(tmp_path / "serial", threads=0)
    rebuild(tmp_path / "prefetch", threads=8)
    rebuild(tmp_path / "tiny", threads=3, cache_bytes=64)
    want = _tree(tmp_path / "serial")
    assert len(want) == len(sd.tier_files(world["metas"][i]))
    assert _tree(tmp_path / "prefetch") == want
    assert _tree(tmp_path / "tiny") == want
    # and the fixture's own build (prefetching by default), but its `reads` counts: its tiers' caches were warmed by earlier builds
    assert _tree(world["runs"][i].root, drop={"s", "reads"}) == _tree(tmp_path / "serial", drop={"s", "reads"})


def test_a_run_holds_no_bulk_read_groups(world, run_rg, tmp_path, monkeypatch):
    """The newly heavy directories' prior roots (a bulk read: each group read once) leave no decoded group nor prefetched
    group bytes behind, and each kind's reads of the prior tiers are released when it is done, so what the reads hold
    doesn't grow with those roots nor carry into the next kind (m3's 2026-10-15T2210 add: 19.2M prior roots, OOM at 8 GiB)."""
    seen, released = [], []
    newly_heavy, build_day = sd.newly_heavy_rollups, sd.build_day

    def held(reads: sd.TierReads) -> tuple[int, int]:
        """Decoded groups, and prefetched bytes of data groups (not index row groups), the reads' group files hold."""
        gfs = [gf for _, gf in reads.items if gf is not None]
        raw = sum(len(v) for (gf, f, _), v in reads.raw._d.items() if f != gf.index_file) if reads.raw else 0
        return sum(len(gf._groups) for gf in gfs), raw

    def nh(con, reads, kind, K, **kw):
        before, groups = held(reads), sum(gf.io["groups_read"] for _, gf in reads.items if gf is not None)
        n = newly_heavy(con, reads, kind, K, **kw)
        seen.append((n, sum(gf.io["groups_read"] for _, gf in reads.items if gf is not None) - groups, before, held(reads)))
        return n

    def bd(con, prior, *args, **kw):
        meta = build_day(con, prior, *args, **kw)
        released.append([sorted(t._gf) for t in prior])
        return meta

    monkeypatch.setattr(sd, "newly_heavy_rollups", nh)
    monkeypatch.setattr(sd, "build_day", bd)
    for i, rebuild in enumerate(world["rebuilds"]):
        rebuild(tmp_path / f"run{i}")
    assert [after for _, _, before, after in seen] == [before for _, _, before, _ in seen]
    assert any(n and read for n, read, _, _ in seen)  # some newly heavy directory read prior roots, decoding groups
    assert released == [[[] for _ in r] for r in released]
    assert len(released) == len(world["rebuilds"])


@pytest.mark.parametrize("i", range(len(DAYS) - BASE), ids=lambda i: f"run{i + 1}")
def test_prefetch_plan_covers_the_reads(world, run_rg, tmp_path, monkeypatch, i):
    """Each loop's plan holds every group it decodes, so after the prefetch nothing is fetched in the loops (index row
    groups and footers included). The `rows` plans (settling, base counts, newly heavy directories) are exactly the groups
    read; a probe's plan (`plan_any`) also holds the later tiers' groups when a tier's decoded rows answer it."""
    stages: list[dict] = []
    windows, group_rows = sd.TierReads.windows, sd.CachedGroupFile.group_rows

    def rec_windows(self, plans):
        stage = {"plan": {(id(gf), g["file"], g["rg"]) for p in plans for gf, g in p}, "read": set(), "any": self.stats["any"]}
        stages.append(stage)
        for a, b in windows(self, plans):
            yield a, b
        stage["any"] = self.stats["any"] - stage["any"]

    def rec_group_rows(self, g, keep=True):
        if stages:
            stages[-1]["read"].add((id(self), g["file"], g["rg"]))
        return group_rows(self, g, keep)

    monkeypatch.setattr(sd.TierReads, "windows", rec_windows)
    monkeypatch.setattr(sd.CachedGroupFile, "group_rows", rec_group_rows)
    seen: list[sd.TierReads] = []
    init = sd.TierReads.__init__

    def rec_init(self, *a, **kw):
        init(self, *a, **kw)
        seen.append(self)

    monkeypatch.setattr(sd.TierReads, "__init__", rec_init)
    world["rebuilds"][i](tmp_path / "drill", threads=4)
    assert [r.requests().get("miss_fetches", 0) for r in seen] == [0, 0]
    assert all(r.requests()["prefetch_fetches"] > 0 for r in seen)
    read = 0
    for st in stages:
        assert st["read"] <= st["plan"]
        if not st["any"]:
            assert st["read"] == st["plan"]
        read += len(st["read"])
    assert read > 0


def _today_by_ranges(con, cands: set[tuple]) -> dict[tuple, list]:
    """`static_drill._today_rows` as it was: a join on the candidates' ranges themselves (`q` equal, `lo ≤ path < hi`)."""
    ranges = sorted({(c, *sd._child_range(d, x, root)) for c, d, x in cands for root in (True, False)})
    today: dict[tuple, list] = {r: [] for r in ranges}
    con.execute("CREATE OR REPLACE TABLE sr_ (q VARCHAR, lo VARCHAR, hi VARCHAR)")
    sd.insert_rows(con, "sr_", ranges)
    for c, lo, hi, *row in con.execute("""SELECT r.q, r.lo, r.hi, d.path, d.usr, d.vf, d.vt, d.size, d.n_files
            FROM sr_ AS r JOIN dr AS d ON d.q = r.q AND d.path >= r.lo AND d.path < r.hi""").fetchall():
        today[(c, lo, hi)].append(tuple(row))
    con.execute("DROP TABLE sr_")
    return {k: sorted(v) for k, v in today.items()}


def test_today_rows_equal_the_range_join():
    """Settling's rows of today under each candidate child, matched on `(q, dir, child)` at the candidates' depths, are the
    range join's: the child's path itself vs below it, siblings sharing a prefix (`x0`, `xy`, `x.bin`), nested candidates (a
    root under both), an empty segment, the directory itself, other members and other directories."""
    import duckdb

    con = duckdb.connect()
    con.execute(f"CREATE TABLE dr ({sr.ROOT_COLS})")
    paths = {"ckpt": ["a/b", "a/b/x", "a/b/x/f1", "a/b/x/y/f2", "a/b/x0/f", "a/b/xy", "a/b/x.bin", "a/bb/x/f", "a/b//f", "c/b/x/f"],
             "zz": ["a/b/x/f"]}
    sd.insert_rows(con, "dr", [(qq, p.count("/") + 1, p, "u", 10, sn.OPEN, len(p), 1) for qq, ps in paths.items() for p in ps])
    cands = {("ckpt", "a/b", "x"), ("ckpt", "a", "b"), ("ckpt", "a/b", ""), ("ckpt", "a/b", "xy"), ("ckpt", "c/b", "z")}

    def row(p):
        return (p, "u", 10, sn.OPEN, len(p), 1)

    want = {
        ("ckpt", "a/b/x", "a/b/x\x00"): [row("a/b/x")],
        ("ckpt", "a/b/x/", "a/b/x0"): [row("a/b/x/f1"), row("a/b/x/y/f2")],
        ("ckpt", "a/b", "a/b\x00"): [row("a/b")],
        ("ckpt", "a/b/", "a/b0"): sorted(row(p) for p in ["a/b/x", "a/b/x/f1", "a/b/x/y/f2", "a/b/x0/f", "a/b/xy", "a/b/x.bin", "a/b//f"]),
        ("ckpt", "a/b/", "a/b/\x00"): [],
        ("ckpt", "a/b//", "a/b/0"): [row("a/b//f")],
        ("ckpt", "a/b/xy", "a/b/xy\x00"): [row("a/b/xy")],
        ("ckpt", "a/b/xy/", "a/b/xy0"): [],
        ("ckpt", "c/b/z", "c/b/z\x00"): [],
        ("ckpt", "c/b/z/", "c/b/z0"): [],
    }
    assert _today_by_ranges(con, cands) == want
    assert sd._today_rows(con, cands) == want
    assert sd._today_rows(con, set()) == {}


#: Per config (`R`) and run, each kind's settling (long, short): `(candidates, today's roots under them)`, so the comparison
#: below covers matched rows in every run but one.
SETTLE_CALLS = {
    3: [[(4, 8), (34, 82)], [(0, 0), (0, 0)], [(3, 3), (16, 17)], [(1, 2), (13, 20)]],
    5: [[(2, 6), (28, 85)], [(1, 1), (14, 18)], [(1, 4), (6, 18)], [(29, 32), (14, 26)]],
}


@pytest.mark.parametrize("i", range(len(DAYS) - BASE), ids=lambda i: f"run{i + 1}")
def test_settling_by_ranges_writes_the_same_bytes(world, run_rg, tmp_path, monkeypatch, i):
    """A run's drill is the same files, byte for byte, with settling's rows of today read by the former range join (and
    without prefetch); the two read the same rows."""
    rebuild = world["rebuilds"][i]
    calls: list[tuple[int, int]] = []
    today_rows = sd._today_rows

    def both(con, cands):
        new = today_rows(con, cands)
        old = _today_by_ranges(con, cands)
        assert new == old
        calls.append((len(cands), sum(map(len, new.values()))))
        return old

    rebuild(tmp_path / "new")
    monkeypatch.setattr(sd, "_today_rows", both)
    rebuild(tmp_path / "old", threads=0)
    assert _tree(tmp_path / "old", drop={"s", "reads"}) == _tree(tmp_path / "new", drop={"s", "reads"})
    assert calls == SETTLE_CALLS[world["cfg"]["R"]][i]


# ── Checkpoints: a reclaimed task's retry resumes (specs/drill-preemption.md) ──


class Interrupted(Exception):
    """A spot reclaim, right after a checkpoint's save."""


def _ck(root: Path, on_save=None) -> sd.Checkpoint:
    return sd.Checkpoint(str(root), "drill/g/scan/long-short", every_s=0, log=lambda *_: None, on_save=on_save)


def _steps(long_settle: int, long_heavy: int, short_settle: int, short_heavy: int) -> list[tuple[str, int | None]]:
    """A checkpointed build's saves in order: each step's (None), or a loop's number of parts (its items, at `every_s=0`)."""
    return [("long/digests", None), ("long/roots", None), ("long/probes", None), ("long/settle", long_settle), ("long/counts", None),
            ("long/heavy", long_heavy), ("long/written", None), ("short/roots", None), ("short/probes", None), ("short/settle", short_settle),
            ("short/counts", None), ("short/heavy", short_heavy)]


#: Per config (`R`) and run, the checkpointed build's saves (`_steps`: settled and newly heavy directories per kind).
SAVES = {
    3: [_steps(4, 36, 34, 160), _steps(10, 63, 30, 41), _steps(33, 9, 35, 32), _steps(8, 63, 26, 72)],
    5: [_steps(2, 19, 27, 113), _steps(4, 12, 31, 59), _steps(4, 59, 23, 25), _steps(29, 26, 14, 90)],
}


def _tables(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}


@pytest.mark.parametrize("i", range(len(DAYS) - BASE), ids=lambda i: f"run{i + 1}")
def test_an_interrupted_build_resumes_to_the_same_bytes(world, run_rg, tmp_path, i):
    """A checkpointed build (every loop item's part saved) writes the files of an uncheckpointed one, byte for byte;
    interrupted right after each of its saves (its connection's new tables and its output dir lost, as a reclaimed task's
    are), a rerun with the same checkpoint saves exactly the steps after that one and writes the same files again; then
    the checkpoint clears to nothing."""
    rebuild, con = world["rebuilds"][i], world["con"]
    rebuild(tmp_path / "plain")
    want = _tree(tmp_path / "plain", drop={"s", "reads"})
    saves: list[str] = []
    rebuild(tmp_path / "ck", checkpoint=_ck(tmp_path / "ck-store", saves.append))
    assert _tree(tmp_path / "ck", drop={"s", "reads"}) == want
    # the steps in save order, a loop's parts counted (one per item: `every_s=0`)
    steps = [(s, None if g == [s] else len(g)) for s, g in ((s, list(g)) for s, g in groupby(saves, key=lambda s: s.split("#")[0]))]
    assert saves == [s if n is None else f"{s}#{j}" for s, n in steps for j in range(n or 1)]
    assert steps == SAVES[world["cfg"]["R"]][i]
    # interrupted after each step, and after a loop's first, middle and last parts
    at = sorted({k for k, s in enumerate(saves) if "#" not in s} | {saves.index(f"{s}#{j}") for s, n in steps if n for j in (0, n // 2, n - 1)})
    for k in at:
        name = saves[k]
        store, out = tmp_path / f"store-{k}", tmp_path / f"out-{k}"

        def interrupt(n, _k=k):
            if n == saves[_k]:
                raise Interrupted(n)

        before = _tables(con)
        with pytest.raises(Interrupted):
            rebuild(out, checkpoint=_ck(store, interrupt))
        for t in _tables(con) - before:
            con.execute(f"DROP TABLE {t}")
        shutil.rmtree(out)
        resumed: list[str] = []
        ck = _ck(store, resumed.append)
        rebuild(out, checkpoint=ck)
        assert (name, resumed) == (name, saves[k + 1:])
        assert (name, _tree(out, drop={"s", "reads"})) == (name, want)
        assert ck.clear() > 0
        assert ck.store.listing(ck.prefix) == {}


def test_a_checkpoint_restores_tables_exactly(tmp_path):
    """A step's tables come back with their DuckDB types and rows (a `HUGEINT` digest beyond a double's precision, an
    unsigned byte, nulls, booleans), and its doc; a part series comes back in order with the items it covers."""
    import duckdb

    con = duckdb.connect()
    con.execute(f"CREATE TABLE dg ({sr.DIGEST_COLS})")
    con.execute("INSERT INTO dg VALUES ('ckpt', 3, 170141183460469231731687303715884105727, -12345678901234567890123), ('zz', 0, 0, NULL)")
    con.execute("CREATE TABLE pr (q VARCHAR, depth UTINYINT, existed BOOLEAN)")
    con.execute("INSERT INTO pr VALUES ('a', 255, true), ('b', 0, NULL)")
    want = {t: con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in ("dg", "pr")}
    types = {t: con.execute(f"SELECT column_name, column_type FROM (DESCRIBE {t})").fetchall() for t in ("dg", "pr")}
    ck = sd.Checkpoint(str(tmp_path / "store"), "x/y", every_s=0, log=lambda *_: None)
    assert not ck.done("s")
    ck.save(con, "s", ("dg", "pr"), {"n": [1, 2]})
    other = duckdb.connect()
    assert ck.done("s")
    assert ck.load(other, "s") == {"n": [1, 2]}
    assert {t: other.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in ("dg", "pr")} == want
    assert {t: other.execute(f"SELECT column_name, column_type FROM (DESCRIBE {t})").fetchall() for t in ("dg", "pr")} == types
    parts = sd.Parts(ck, "loop", sd.ROLLUP_ARROW)
    assert (parts.start, parts.loaded) == (0, None)
    parts.add([("c", "d", 1, "x", 5, 10, 1)], 1)
    parts.add([], 2)
    parts.add([("c", "e", 0, "", 2, 3, 4), ("c", "e", 1, "y", 5, 6, 7)], 3)
    again = sd.Parts(ck, "loop", sd.ROLLUP_ARROW)
    assert (again.start, again.n_parts) == (3, 3)
    assert again.loaded.to_pylist() == [
        {"q": "c", "dir": "d", "kind": 1, "child": "x", "vf": 5, "b": 10, "o": 1},
        {"q": "c", "dir": "e", "kind": 0, "child": "", "vf": 2, "b": 3, "o": 4},
        {"q": "c", "dir": "e", "kind": 1, "child": "y", "vf": 5, "b": 6, "o": 7},
    ]
    assert sorted(ck.store.listing("x/y")) == ["x/y/loop/part-00000.parquet", "x/y/loop/part-00001.parquet", "x/y/loop/part-00002.parquet",
                                               "x/y/loop/progress.json", "x/y/s/dg.parquet", "x/y/s/done.json", "x/y/s/pr.parquet"]
    assert ck.clear() == 7
    assert ck.store.listing("x/y") == {}


def test_a_checkpoint_other_code_saved_is_not_resumed(tmp_path):
    """A checkpoint is keyed by the code that saved it (`code.json`): reopened by the same code it resumes; by other code
    (a rebuilt image after a bug fix) or with no version recorded, it is cleared, so no step a buggy build saved reaches
    the fixed build's output. `close` logs the checkpoint's total cost."""
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE t (x INTEGER)")
    root, logs = str(tmp_path / "store"), []

    def open_(code):
        return sd.Checkpoint(root, "d/g/s/long", every_s=0, log=logs.append, code=code)

    a = open_("aaaa")
    a.save(con, "inputs", ("t",))
    assert sorted(a.store.listing("d/g/s/long")) == ["d/g/s/long/code.json", "d/g/s/long/inputs/done.json", "d/g/s/long/inputs/t.parquet"]
    assert open_("aaaa").done("inputs")
    b = open_("bbbb")
    assert not b.done("inputs")
    assert sorted(b.store.listing("d/g/s/long")) == ["d/g/s/long/code.json"]
    assert b.store.read_json("d/g/s/long/code.json") == {"code": "bbbb"}
    sd.Checkpoint(root, "d/g/s/long", log=logs.append).save(con, "inputs", ("t",))
    sd.Checkpoint(root, "d/g/s/long", log=logs.append).store.delete({"d/g/s/long/code.json": 0})
    c = open_("cccc")
    assert not c.done("inputs")
    c.save(con, "inputs", ("t",))
    c.load(con, "inputs")
    logs.clear()
    c.close()
    assert [re.sub(r"\d+s\b", "<n>s", m).replace(root, "<root>") for m in logs] == ["checkpoint <root>/d/g/s/long: total 1 saves in <n>s, 1 resumes in <n>s"]
    assert len(sd.code_version()) == 16


def test_the_build_inputs_checkpoint_round_trips(world, tmp_path):
    """`build -S`'s first step (`save_inputs`): table `pnew`, the new members and their history come back as saved, under
    `drill/<gen>/<scan>/<kinds>/inputs/`."""
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE pnew (depth UTINYINT, path VARCHAR)")
    con.execute("INSERT INTO pnew VALUES (2, 'b1/x'), (3, 'b2/zoo/zebra9.txt')")
    history = sd.history_rows([_reader(world["base_out"]), _reader(world["run_dirs"][0])], ["zebra"])
    assert history.num_rows > 0
    ck = sd.drill_checkpoint(str(tmp_path / "store"), "g1", "2026-08-04", ("long",), log=lambda *_: None)
    sd.save_inputs(con, ck, ["zebra"], history)
    assert sorted(ck.store.listing(ck.prefix)) == ["drill/g1/2026-08-04/long/code.json"] + [f"drill/g1/2026-08-04/long/inputs/{f}" for f in ("done.json", "nmh.parquet", "pnew.parquet")]
    other = duckdb.connect()
    assert ck.load(other, "inputs") == {"new_members": ["zebra"]}
    assert other.execute("SELECT * FROM pnew ORDER BY ALL").fetchall() == [(2, "b1/x"), (3, "b2/zoo/zebra9.txt")]
    got = other.execute("SELECT * FROM nmh").to_arrow_table()
    assert (got.schema, got.to_pylist()) == (history.schema, history.to_pylist())
