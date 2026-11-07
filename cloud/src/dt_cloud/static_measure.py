"""A compaction's drill measurement (`roots-measure/`: per member `q`, the directories with at least `floor` root rows
strictly under them, `(q, k, dir, rows)`) from the base's measurement and the runs' counts of the roots new to the stack,
instead of measuring every root again (specs/static-incremental-compaction.md, step 2).

Root rows per `(q, dir)` are additive in new keys: the compacted generation's roots are the base's ∪ the runs'
(`static_compact.fold_drill`: combined on `(path, usr, vf)`), and a run adds exactly its `fresh` roots (an existing
member's opens, every root of a new member: `static_drill.day_rollups`). What a run keeps of them, per class canonical
`c` at the run and directory:

- a heavy directory's header (rollups `kind` 0 or −1): `b`, its root rows through the run's last scan (a delta header's
  `h.hb + t.opens`, a full one's count of the combined roots), absolute. Once a directory has a header, every later run
  touching it writes one, so the newest header's `b` is the total;
- any other touched directory's `fresh` rows in `state/dcount-<kind>.parquet` (summed by a merged tier; dropped where
  it has a header).

So per member `m` (each tier's canonical `c_i(m)`: its `aliases.parquet`, a member it doesn't know being its own) and
directory: the newest run header's `b`; else the base's rows plus the runs' `dcount`s. The base's rows are its
measurement's where it has them (≥ `floor`); a directory below the floor there that a run touched (a floor crosser,
possibly) is counted from the base drill's roots (fewer than `floor` per directory, each nested one counted from its
topmost touched ancestor's rows). The short literals are their own classes."""
from __future__ import annotations

import os
import shutil
import tempfile
from bisect import bisect_left
from pathlib import Path
from time import monotonic

import pyarrow as pa
import pyarrow.parquet as pq

from . import static_drill as sd
from .static_names import CODEC, err, q

#: `roots-measure/`'s per-kind subdirectories (`static_local.finish_drill`).
SUBS = {"long": "dirs", "short": "short/dirs"}
MEASURE_COLS = "q VARCHAR, k INTEGER, dir VARCHAR, rows BIGINT"


def _files(fs: list[str]) -> str:
    return "[" + ", ".join(q(f) for f in fs) + "]"


def _union(parts: list[str], empty: str) -> str:
    return " UNION ALL ".join(parts) if parts else f"SELECT * FROM ({empty}) WHERE false"


#: At least this many floor crossers are counted on worker processes (`base_counts_parallel`); fewer, here.
PARALLEL_MIN = 200_000


def count_under(gf, ci: str, d: str) -> int:
    """The roots of `ci` strictly under directory `d` (`d/ ≤ path < d0`) in one tier's roots group file, from its index: a
    group wholly inside counts its `rows` undecoded, a boundary group is decoded once (cached) and its keys bisected. Each
    `(q, path, usr, vf)` is one row in a tier's file (its build combines them), so this is the rows `TierReads.rows`
    returns for the range, counted without reading them."""
    lo, hi = d + "/", d + "0"
    groups, _ = gf.select((ci, lo), (ci, hi))
    n = 0
    for g in groups:
        if g["q_min"] == ci == g["q_max"] and lo <= g["k_min"] and g["k_max"] < hi:
            n += g["rows"]
            continue
        t, runs = gf.group_rows(g)
        run = runs.get(ci)
        if run is None:
            continue
        s, e, keys = run
        if keys is None:
            keys = run[2] = t.column(gf.key).slice(s, e - s).to_pylist()
        n += bisect_left(keys, hi) - bisect_left(keys, lo)
    return n


def base_counts(tier: sd.Tier, kind: str, cands: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """The base tier's root rows strictly under each `(c, dir)` of `cands` (`c` its canonical there; `count_under`)."""
    gf = tier.group_file(kind, "roots")
    return {(c, d): count_under(gf, tier.canon(c, kind), d) if gf is not None else 0 for c, d in cands}


_BC: tuple | None = None


def _bc_init(root: str, name: str, kind: str) -> None:
    global _BC
    tier = sd.Tier(root, name)
    _BC = (tier, kind)


def _bc_part(srcs: list[str], out: str) -> int:
    """One share of `base_counts_parallel`: `srcs`' `(c, dir)` rows, sorted (so neighbors share groups), counted → `out`."""
    tier, kind = _BC
    tab = pq.read_table(srcs, columns=["c", "dir"]).sort_by([("c", "ascending"), ("dir", "ascending")])
    cands = list(zip(tab.column("c").to_pylist(), tab.column("dir").to_pylist()))
    counts = base_counts(tier, kind, cands)
    pq.write_table(pa.table({"c": [c for c, _ in cands], "dir": [d for _, d in cands],
                             "rows": pa.array([counts[k] for k in cands], pa.int64())}), out)
    return len(cands)


def base_counts_parallel(con, tier: sd.Tier, kind: str, src: str, into: str, n: int, *, jobs: int, work: Path) -> None:
    """`base_counts` of DuckDB table `src`'s `(c, dir)` rows (`n` of them) into table `into` `(c, dir, rows)`, on `jobs` worker
    processes: `src` cut in sorted shares (parquet under `work`), each counted against the tier's own group files. Nothing
    of `src` is held in Python here: a compaction's crossers ran to 191M (cw, 2026-11-06)."""
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    parts = max(1, min(jobs * 8, n // 10_000))
    cut = work / "cands"
    con.execute(f"""COPY (SELECT c, dir, ((row_number() OVER (ORDER BY c, dir) - 1) * {parts} // {n})::INTEGER AS part FROM {src})
        TO {q(str(cut))} (FORMAT parquet, PARTITION_BY (part))""")
    shares = sorted((int(d.name.split("=", 1)[1]), sorted(str(f) for f in d.glob("*.parquet"))) for d in cut.glob("part=*"))
    with ProcessPoolExecutor(jobs, mp_context=get_context("spawn"), initializer=_bc_init,
                             initargs=(str(tier.root), tier.name, kind)) as pool:
        for f in [pool.submit(_bc_part, fs, str(work / f"out-{i:05d}.parquet")) for i, fs in shares]:
            f.result()
    con.execute(f"INSERT INTO {into} SELECT c, dir, rows FROM read_parquet({q(str(work / 'out-*.parquet'))})")


def measure_kind(con, tiers: list[sd.Tier], meas: Path, kind: str, out: Path, *, floor: int, members: str | None = None,
                 jobs: int | None = None) -> dict:
    """One kind's measurement → `out` (`(q, k, dir, rows)` sorted, as `finish_drill` writes it), from `tiers` (the base's
    drill, then the runs', oldest first), the base's `roots-measure/` `meas`, and (long) the members' `members` parquet
    (column `q`). The floor crossers' base rows are counted on `jobs` processes (default: every CPU) when there are at least
    `PARALLEL_MIN` of them. Returns counts."""
    jobs = jobs or os.cpu_count() or 1
    runs = list(enumerate(tiers))[1:]
    long = kind == "long"
    if long:
        cols = ", ".join(f"coalesce(a{i}.canonical, m.q) AS c{i}" for i in range(len(tiers)))
        joins = " ".join(f"LEFT JOIN im_a{i} AS a{i} USING (q)" for i in range(len(tiers)))
        for i, t in enumerate(tiers):
            con.register(f"im_a{i}_in", t.alias_table())
            con.execute(f"CREATE OR REPLACE TEMP TABLE im_a{i} AS SELECT q, canonical FROM im_a{i}_in")
            con.unregister(f"im_a{i}_in")
        # each member's canonical per tier; `base`: the base knows it (else it has no base roots)
        con.execute(f"""CREATE OR REPLACE TEMP TABLE im_mt AS SELECT m.q, {cols}, a0.q IS NOT NULL AS base
            FROM (SELECT DISTINCT q FROM read_parquet({q(members)})) AS m {joins}""")

    def keyed(i: int, sql: str) -> str:
        """`sql`'s rows (`q`: tier `i`'s canonical) per member (long) or literal (short)."""
        return f"SELECT mt.q, s.* EXCLUDE (q) FROM ({sql}) AS s JOIN im_mt AS mt ON mt.c{i} = s.q" if long else sql

    heads = [keyed(i, f"""SELECT q, dir, b AS rows, {i} AS tier FROM read_parquet({_files(fs)})
        WHERE kind IN ({sd.KIND_FULL}, {sd.KIND_DELTA})""") for i, t in runs if (fs := t.files(kind, "rollups"))]
    dcs = [keyed(i, f"SELECT q, dir, rows FROM read_parquet({q(f)})") for i, t in runs if (f := t.dcount(kind))]
    con.execute(f"""CREATE OR REPLACE TEMP TABLE im_h AS SELECT q, dir, arg_max(rows, tier) AS rows
        FROM ({_union(heads, "SELECT NULL::VARCHAR AS q, NULL::VARCHAR AS dir, NULL::BIGINT AS rows, NULL::INTEGER AS tier")}) GROUP BY q, dir""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE im_d AS SELECT q, dir, sum(rows)::BIGINT AS rows
        FROM ({_union(dcs, "SELECT NULL::VARCHAR AS q, NULL::VARCHAR AS dir, NULL::BIGINT AS rows")}) GROUP BY q, dir""")
    top = sorted(str(p) for p in (meas / "short" / "top").glob("*.parquet")) if not long else []
    base = sd.meas_sql(kind, sorted(str(p) for p in (meas / SUBS[kind]).glob("*.parquet")), top)
    con.execute(f"CREATE OR REPLACE TEMP TABLE im_b AS SELECT b.q, b.dir, b.rows FROM ({base}) AS b"
                + (" SEMI JOIN im_mt USING (q)" if long else ""))
    # touched below the base's floor, no header: the base's rows there are counted (per base canonical)
    c0, mt = ("mt.c0", "JOIN im_mt AS mt USING (q) WHERE mt.base") if long else ("d.q", "")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE im_c AS SELECT DISTINCT {c0} AS c, d.dir
        FROM im_d AS d ANTI JOIN im_h USING (q, dir) ANTI JOIN im_b USING (q, dir) {mt}""")
    n = con.execute("SELECT count(*) FROM im_c").fetchone()[0]
    con.execute("CREATE OR REPLACE TEMP TABLE im_bx (c VARCHAR, dir VARCHAR, rows BIGINT)")
    t0 = monotonic()
    if n >= PARALLEL_MIN and jobs > 1 and tiers[0].ranged is None:
        out.parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f"base-counts-{kind}-", dir=out.parent))
        try:
            base_counts_parallel(con, tiers[0], kind, "im_c", "im_bx", n, jobs=jobs, work=work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    elif n:
        cands = con.execute("SELECT c, dir FROM im_c ORDER BY c, dir").fetchall()
        sd.insert_rows(con, "im_bx", [(c, d, x) for (c, d), x in base_counts(tiers[0], kind, cands).items()])
    base_s = monotonic() - t0
    bc = "mt.c0" if long else "x.q"
    con.execute(f"CREATE OR REPLACE TEMP TABLE im_out ({MEASURE_COLS})")
    con.execute(f"""INSERT INTO im_out SELECT q, len(string_split(dir, '/')), dir, rows FROM (
            SELECT q, dir, rows FROM im_h
            UNION ALL SELECT x.q, x.dir, coalesce(b.rows, bx.rows, 0) + coalesce(d.rows, 0) AS rows
            FROM (SELECT q, dir FROM im_b UNION SELECT q, dir FROM im_d) AS x ANTI JOIN im_h USING (q, dir)
            LEFT JOIN im_b AS b USING (q, dir) LEFT JOIN im_d AS d USING (q, dir) {"JOIN im_mt AS mt USING (q)" if long else ""}
            LEFT JOIN im_bx AS bx ON bx.c = {bc} AND bx.dir = x.dir)
        WHERE rows >= {floor}""")
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(con.execute("SELECT * FROM im_out ORDER BY q, k, dir").to_arrow_table(), out / "s0000.parquet", compression=CODEC)
    doc = {k: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for k, t in (("headers", "im_h"), ("dcount", "im_d"), ("base", "im_b"),
                                                                                  ("rows", "im_out"))}
    doc["base_counted"] = n
    doc["base_count_s"] = round(base_s, 1)
    for t in ("im_h", "im_d", "im_b", "im_c", "im_bx", "im_out", "im_mt", *(f"im_a{i}" for i in range(len(tiers)))):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    return doc


def incremental_measure(con, tiers: list[sd.Tier], meas: Path, members: str, out: Path, *, floor: int, log=err,
                        jobs: int | None = None) -> dict:
    """The compacted generation's `roots-measure/` in `out` (`dirs/s0000.parquet`, `short/dirs/s0000.parquet`), from
    `tiers` (the base generation's drill, then its runs', oldest first), the base's `roots-measure/` `meas` and the new
    catalog's `members.parquet` `members`: what `static_local.finish_drill` measures from every root, file for file.
    Every run's `dcount` must count only new roots (`meta.json` `dcount`: `static_drill.DCOUNT_FRESH`); a tier built
    before counts closes too and is refused. `jobs`: `measure_kind`'s. Returns per kind its counts."""
    stale = [t.name for t in tiers[1:] if t.meta.get("dcount") != sd.DCOUNT_FRESH]
    if stale:
        raise ValueError(f"runs whose dcount counts closes too (built before `dcount: {sd.DCOUNT_FRESH}`): {', '.join(stale)}")
    doc = {}
    for kind in sd.KINDS:
        doc[kind] = measure_kind(con, tiers, meas, kind, out / SUBS[kind], floor=floor, members=members if kind == "long" else None,
                                 jobs=jobs)
        log(f"measure {kind}: {doc[kind]['rows']:,} directories ({doc[kind]['headers']:,} by run headers, {doc[kind]['dcount']:,} run counts, "
            f"{doc[kind]['base_counted']:,} counted below the base's floor, in {doc[kind]['base_count_s']}s)")
    return doc


def blocker(tiers: list[sd.Tier], meas: Path, *, floor: int, R: int, K: int | None = None) -> str | None:
    """Why a compaction over `tiers` (the base's drill, then the runs') can't measure and roll up from base ⊕ runs
    (`incremental_measure`, `incremental_rollups`), or `None`: a run whose `dcount` counts closes too, a base without its
    measurement `meas`, a floor above `R + 1` (the measurement would miss heavy directories), or a tier whose kept
    children per heavy directory aren't `K` (its rollups kept another set)."""
    if K is not None and (other := sorted({t.meta["K"] for t in tiers if t.meta["K"] != K})):
        return f"K {', '.join(map(str, other))} → {K}"
    if stale := [t.name for t in tiers[1:] if t.meta.get("dcount") != sd.DCOUNT_FRESH]:
        return f"runs without `dcount: {sd.DCOUNT_FRESH}`: {', '.join(stale)}"
    if not (meas / SUBS["long"]).is_dir():
        return f"no base measurement at {meas}"
    if floor > R + 1:
        return f"floor {floor:,} > R + 1 ({R + 1:,})"
    return None


def incremental_rollups(con, tiers: list[sd.Tier], kind: str, meas: Path, into: str, *, R: int, aliases=None, log=err) -> dict:
    """A compaction's rollup rows for one kind (`ROLLUP_COLS`, inserted into table `into`) from `tiers` (the base's drill,
    then the runs', oldest first) and the compaction's measurement `meas` (its `roots-measure/`, `incremental_measure`'s):
    what `static_local.measure_rollups` rolls up from every root, without reading a root (specs/static-incremental-compaction.md,
    step 4).

    Per `(c, dir)` (`c`: a canonical of `aliases` `(q, canonical)`, the compaction's classes, re-keyed per tier as
    `incremental_measure` does; short: the literal), `static_drill.merge_tiers`' rule over base ⊕ runs: the cells of the
    newest tier with a full header and every later one (the base's header is full), collapsed into the rebuild's form: one
    full header (`kind` 0; `vf` the kept children and `o` the children, the newest header's; `b` the measured root rows),
    the kept children's and the remainder's cells. Heavy is the measurement's: a directory with more than `R` root rows (the
    measurement holds every one: its floor is at most `R + 1`); a header whose directory isn't is dropped, a heavy
    directory with no header in any tier is an error. Returns counts."""
    long = kind == "long"
    if long:
        con.register("ir_al_in", aliases)
        con.execute("CREATE OR REPLACE TEMP TABLE ir_c AS SELECT DISTINCT canonical AS c FROM ir_al_in")
        con.unregister("ir_al_in")
        con.execute("CREATE OR REPLACE TEMP TABLE ir_m (c VARCHAR, tier INTEGER, ci VARCHAR)")
        for i, t in enumerate(tiers):
            con.register("ir_a_in", t.alias_table())
            con.execute(f"INSERT INTO ir_m SELECT c.c, {i}, coalesce(a.canonical, c.c) FROM ir_c AS c LEFT JOIN ir_a_in AS a ON a.q = c.c")
            con.unregister("ir_a_in")

    def keyed(i: int, files: list[str]) -> str:
        """Tier `i`'s rollup rows (`files`), `q` the compaction's canonical (long) or the literal (short)."""
        if long:
            return f"""SELECT {i} AS tier, m.c AS q, r.dir, r.kind, r.child, r.vf, r.b, r.o FROM read_parquet({_files(files)}) AS r
                JOIN ir_m AS m ON m.tier = {i} AND m.ci = r.q"""
        return f"SELECT {i} AS tier, q, dir, kind, child, vf, b, o FROM read_parquet({_files(files)})"

    empty = ("SELECT 0 AS tier, NULL::VARCHAR AS q, NULL::VARCHAR AS dir, NULL::TINYINT AS kind, NULL::VARCHAR AS child, NULL::BIGINT AS vf, "
             "NULL::BIGINT AS b, NULL::BIGINT AS o")
    runs = [keyed(i, fs) for i, t in list(enumerate(tiers))[1:] if (fs := t.files(kind, "rollups"))]
    con.execute(f"CREATE OR REPLACE TEMP TABLE ir_r AS {_union(runs, empty)}")
    # per directory a run touched: the newest full header's tier (else the base's), the newest header
    hdr = f"FILTER (WHERE kind IN ({sd.KIND_FULL}, {sd.KIND_DELTA}))"
    con.execute(f"""CREATE OR REPLACE TEMP TABLE ir_f AS SELECT q, dir, coalesce(max(tier) FILTER (WHERE kind = {sd.KIND_FULL}), 0) AS ft,
            arg_max(vf, tier) {hdr} AS vf, arg_max(o, tier) {hdr} AS o, arg_max(b, tier) {hdr} AS b
        FROM ir_r GROUP BY q, dir""")
    headless = con.execute("SELECT count(*) FROM ir_f WHERE vf IS NULL").fetchone()[0]
    if headless:
        raise RuntimeError(f"{kind}: {headless} directories with run cells but no run header")
    base = tiers[0].files(kind, "rollups")
    con.execute(f"CREATE OR REPLACE TEMP VIEW ir_b AS {keyed(0, base) if base else f'SELECT * FROM ({empty}) WHERE false'}")
    files = sorted(str(p) for p in (meas / SUBS[kind]).glob("*.parquet"))
    heavy = (f"SELECT q, dir, rows FROM read_parquet({_files(files)}) WHERE rows > {R}" + (" AND q IN (SELECT c FROM ir_c)" if long else "")
             if files else "SELECT NULL::VARCHAR AS q, NULL::VARCHAR AS dir, NULL::BIGINT AS rows WHERE false")
    con.execute(f"CREATE OR REPLACE TEMP TABLE ir_h AS {heavy}")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE ir_hd AS SELECT h.q, h.dir, h.rows, coalesce(f.ft, 0) AS ft, coalesce(f.vf, b.vf) AS vf,
            coalesce(f.o, b.o) AS o, coalesce(f.b, b.b) AS hb, f.q IS NOT NULL AS touched
        FROM ir_h AS h LEFT JOIN ir_f AS f USING (q, dir) LEFT JOIN (SELECT q, dir, vf, b, o FROM ir_b WHERE kind = {sd.KIND_FULL}) AS b USING (q, dir)""")
    missing = con.execute("SELECT count(*) FROM ir_hd WHERE vf IS NULL").fetchone()[0]
    if missing:
        ex = con.execute("SELECT q, dir, rows FROM ir_hd WHERE vf IS NULL ORDER BY q, dir LIMIT 3").fetchall()
        raise RuntimeError(f"{kind}: {missing} heavy directories (> {R} root rows) without a header in any tier, e.g. {ex}")
    n0 = con.execute(f"SELECT count(*) FROM {into}").fetchone()[0]
    con.execute(f"""INSERT INTO {into}
        SELECT q, dir, {sd.KIND_FULL}::TINYINT, '', vf, rows, o FROM ir_hd
        UNION ALL SELECT b.q, b.dir, b.kind, b.child, b.vf, b.b, b.o FROM ir_b AS b SEMI JOIN (SELECT q, dir FROM ir_hd WHERE ft = 0) AS h USING (q, dir)
            WHERE b.kind IN ({sd.KIND_KEPT}, {sd.KIND_REST})
        UNION ALL SELECT r.q, r.dir, r.kind, r.child, r.vf, r.b, r.o FROM ir_r AS r JOIN ir_hd AS h USING (q, dir)
            WHERE r.tier >= h.ft AND r.kind IN ({sd.KIND_KEPT}, {sd.KIND_REST})""")
    one = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    doc = {
        "headers": one("SELECT count(*) FROM ir_hd"),
        "touched": one("SELECT count(*) FROM ir_hd WHERE touched"),
        "restated": one("SELECT count(*) FROM ir_hd WHERE ft > 0"),
        "dropped": one(f"SELECT count(*) FROM (SELECT q, dir FROM ir_f UNION SELECT q, dir FROM ir_b WHERE kind = {sd.KIND_FULL}) ANTI JOIN ir_h USING (q, dir)"),
        "b_differs": one("SELECT count(*) FROM ir_hd WHERE hb <> rows"),
        "rows": one(f"SELECT count(*) FROM {into}") - n0,
    }
    con.execute("DROP VIEW ir_b")
    for t in ("ir_r", "ir_f", "ir_h", "ir_hd", "ir_m", "ir_c"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    log(f"rollups {kind}: {doc['rows']:,} rows, {doc['headers']:,} heavy directories ({doc['touched']:,} touched by the runs, "
        f"{doc['restated']:,} restated in full there), {doc['dropped']:,} headers dropped (≤ {R:,} root rows), "
        f"{doc['b_differs']:,} headers whose rows differ from the measurement's")
    return doc
