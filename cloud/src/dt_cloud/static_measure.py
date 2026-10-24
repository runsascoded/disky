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

from collections import Counter
from pathlib import Path

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


def base_counts(tier: sd.Tier, kind: str, cands: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """The base tier's root rows strictly under each `(c, dir)` of `cands` (`c` its canonical there): every directory's
    rows counted from those of its topmost ancestor among `cands` (`static_drill.chunk_tops`), read once."""
    want = set(cands)
    tops = [cands[i] for i in sd.chunk_tops(cands)]
    counts: Counter = Counter()
    reads = sd.TierReads([tier], kind)
    try:
        ranges = [(c, d + "/", d + "0", 0) for c, d in tops]
        for a, b in reads.windows(reads.plan(ranges, "rows")):
            for c, lo, hi, o in ranges[a:b]:
                for r in reads.rows(c, lo, hi, only=o):
                    segs = r["path"].split("/")
                    for j in range(1, len(segs)):
                        if (key := (c, "/".join(segs[:j]))) in want:
                            counts[key] += 1
    finally:
        reads.close()
    return {key: counts[key] for key in cands}


def measure_kind(con, tiers: list[sd.Tier], meas: Path, kind: str, out: Path, *, floor: int, members: str | None = None) -> dict:
    """One kind's measurement → `out` (`(q, k, dir, rows)` sorted, as `finish_drill` writes it), from `tiers` (the base's
    drill, then the runs', oldest first), the base's `roots-measure/` `meas`, and (long) the members' `members` parquet
    (column `q`). Returns counts."""
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
    cands = con.execute(f"""SELECT DISTINCT {c0} AS c, d.dir FROM im_d AS d ANTI JOIN im_h USING (q, dir) ANTI JOIN im_b USING (q, dir) {mt}
        ORDER BY c, d.dir""").fetchall()
    bx = base_counts(tiers[0], kind, cands) if cands else {}
    con.execute("CREATE OR REPLACE TEMP TABLE im_bx (c VARCHAR, dir VARCHAR, rows BIGINT)")
    sd.insert_rows(con, "im_bx", [(c, d, n) for (c, d), n in bx.items()])
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
    doc["base_counted"] = len(bx)
    for t in ("im_h", "im_d", "im_b", "im_bx", "im_out", "im_mt", *(f"im_a{i}" for i in range(len(tiers)))):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    return doc


def incremental_measure(con, tiers: list[sd.Tier], meas: Path, members: str, out: Path, *, floor: int, log=err) -> dict:
    """The compacted generation's `roots-measure/` in `out` (`dirs/s0000.parquet`, `short/dirs/s0000.parquet`), from
    `tiers` (the base generation's drill, then its runs', oldest first), the base's `roots-measure/` `meas` and the new
    catalog's `members.parquet` `members`: what `static_local.finish_drill` measures from every root, file for file.
    Every run's `dcount` must count only new roots (`meta.json` `dcount`: `static_drill.DCOUNT_FRESH`); a tier built
    before counts closes too and is refused. Returns per kind its counts."""
    stale = [t.name for t in tiers[1:] if t.meta.get("dcount") != sd.DCOUNT_FRESH]
    if stale:
        raise ValueError(f"runs whose dcount counts closes too (built before `dcount: {sd.DCOUNT_FRESH}`): {', '.join(stale)}")
    doc = {}
    for kind in sd.KINDS:
        doc[kind] = measure_kind(con, tiers, meas, kind, out / SUBS[kind], floor=floor, members=members if kind == "long" else None)
        log(f"measure {kind}: {doc[kind]['rows']:,} directories ({doc[kind]['headers']:,} by run headers, {doc[kind]['dcount']:,} run counts, "
            f"{doc[kind]['base_counted']:,} counted below the base's floor)")
    return doc
