"""Churn on the interval store's path versions (specs/diff-progressive.md §3): `churn_cum`, a path's cumulative churn.

A file's churn at scan t is |size_t − size_t⁻| against the scan before (its full size where it appears or goes away); a
directory's is the sum over the files under it. Churn is a group, additive up the tree and across scans, so one prefix
sum per path is enough: each folded path version (`pvl` piece) carries `churn_cum`, the path's churn over every scan up
to the piece's `vf`, and a range's churn is `churn_cum`(D2) − `churn_cum`(D1). A closed piece also carries `churn_end`,
its path's cumulative churn at its `vt` (that scan's churn included: the files under it going away, or changing as the
next piece opens); NULL while open. Where a path is not live at D, its cumulative churn is its last piece's `churn_end`,
which a reappearing path's next piece continues from. A piece splits where its
path's churn moves inside it (a same-size swap under a directory whose other columns hold); otherwise every churned
path already opens a piece there. `pv` (the path versions) and the owner slices don't carry it.

Churn starts at the generation's churn base (`churn_from`): the last scan that first lists objects after scans that
listed directories only (a v1 index's successor), else its first scan. Pairs at or before it are not counted: before
it there are no file rows, and at it every object that already existed appears because it is now listed, not because
it changed. A range starting before the base has no churn (the reader's diff falls back to the as-of walk).

A generation carries the column when its `scans.json` says so (`"churn": true`); the base follows from its scans (their
`version`s), so an append at the switch counts nothing there either (`counts_at`). The per-path sums are a
cross-range rollup (a directory's files sit in other key ranges): `churn_gen` computes them over every range's pieces
(each file's events, rolled up to its ancestors at depths ≥ 1) and then sets the column range by range. The root's
churn is the sum of the depth-1 paths', as its totals are.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from time import monotonic

import pyarrow as pa
import pyarrow.parquet as pq
from click import IntRange, command, option

from . import static_names as sn
from .static_names import OPEN, err, q

CHURN, CHURN_END = "churn_cum", "churn_end"
CHURN_FIELDS = [pa.field(CHURN, pa.int64(), nullable=False), pa.field(CHURN_END, pa.int64())]
#: In an append's open state and deltas: `churn_cum` NULL on the versions it opened, `churn_end` on those it closed, until
#: `apply_run` sets them.
CHURN_PENDING = [pa.field(CHURN, pa.int64()), pa.field(CHURN_END, pa.int64())]
#: `scans.json`'s flag: the folded path versions carry `churn_cum`.
CHURN_FLAG = "churn"
#: Under an append's `state/<D>/`: each range's churn at D (`scan_churn`), its reappearing paths' earlier cumulative churn,
#: and the versions the apply opened (`apply_run`), which the next append's open state takes in place of its paths'.
SCAN_CHURN, PRIOR, FIX = "churn", "prior", "pvl-fix"
#: `backfill-churn`'s record in the new generation.
BACKFILL = "backfill-churn.json"


def churn_from(scans: list[dict]) -> dict:
    """The scan churn is counted after: the last that first lists objects after scans that listed directories only
    (`version` 1 → ≥ 2), else the first."""
    switch = [s for p, s in zip(scans, scans[1:]) if p.get("version", 1) == 1 and s.get("version", 1) >= 2]
    return switch[-1] if switch else scans[0]


def counts_at(prev: dict, scan: dict) -> bool:
    """Whether churn is counted at `scan` after `prev` (`scans.json` entries): not where objects are first listed."""
    return not (prev.get("version", 1) == 1 and scan.get("version", 1) >= 2)


def has_churn(scans: dict) -> bool:
    """Whether a generation (its `scans.json`) carries `churn_cum` on its folded path versions."""
    return bool(scans.get(CHURN_FLAG))


def record_churn(scans: dict) -> dict:
    """`scans` (a `scans.json`) flagged as carrying churn, the flag before `scans` (where a compaction's copy of the
    base's keys puts it, so both write the same bytes)."""
    out = {}
    for k, v in scans.items():
        if k == CHURN_FLAG:
            continue
        if k == "scans":
            out[CHURN_FLAG] = True
        out[k] = v
    return out


def with_churn(schema: pa.Schema, churn: bool, pending: bool = False) -> pa.Schema:
    """`schema` with `churn_cum` and `churn_end` appended when `churn` (both nullable with `pending`: an append's state and
    deltas)."""
    if not churn:
        return schema
    for f in CHURN_PENDING if pending else CHURN_FIELDS:
        schema = schema.append(f)
    return schema


#: Columns a folded path version carries after `churn_cum` (`interval_store.with_generation`'s, which come last).
AFTER = ("generation", "version")


def pq_schema_with_churn(schema: pa.Schema) -> pa.Schema:
    """A `pvl` file's schema with `churn_cum` and `churn_end` (once), before the object-version column that ends it when
    present."""
    fields = [f for f in schema if f.name not in (CHURN, CHURN_END)]
    at = next((i for i, f in enumerate(fields) if f.name in AFTER), len(fields))
    return pa.schema([*fields[:at], *CHURN_FIELDS, *fields[at:]])


def events_sql(pieces: str, base: int) -> str:
    """The file churn events in `pieces` (folded path versions: `depth, path, vf, vt, kind, size`): `(depth, path, t, c)`
    per file and scan after `base` where its size moves (|Δ|), it appears (its size) or it goes away (its last size),
    `c > 0` only. A path's pieces never overlap; consecutive ones meet (`vt` = the next's `vf`) or leave a gap where the
    path was absent."""
    return f"""WITH v AS (SELECT depth, path, vf, vt, size, lag(vt) OVER w AS pvt, lag(size) OVER w AS psize, lead(vf) OVER w AS nvf
            FROM ({pieces}) WHERE kind = 'file' WINDOW w AS (PARTITION BY depth, path ORDER BY vf))
        SELECT depth, path, t, sum(c)::BIGINT AS c FROM (
            SELECT depth, path, vf AS t, CASE WHEN pvt = vf THEN abs(size - psize) ELSE size END AS c FROM v WHERE vf > {base}
            UNION ALL
            SELECT depth, path, vt AS t, size AS c FROM v WHERE vt <> {OPEN} AND vt > {base} AND nvf IS DISTINCT FROM vt)
        WHERE c > 0 GROUP BY ALL"""


def ancestors_sql(events: str) -> str:
    """`events` (`depth, path, t, c`) rolled up to each path's ancestors at depths 1 … depth − 1: `(depth, path, t, c)`
    summed per ancestor and scan."""
    return f"""SELECT k::UTINYINT AS depth, array_to_string(list_slice(string_split(path, '/'), 1, k), '/') AS path, t, sum(c)::BIGINT AS c
        FROM ({events}), unnest(range(1, depth)) AS r(k) GROUP BY ALL"""


def apply_sql(pieces: str, events: str, cols: list[str]) -> str:
    """`pieces` (folded path versions, columns `cols`, no churn) with `churn_cum` and `churn_end`, split where their path's
    churn moves inside a piece: `events` (`depth, path, t, c`, one row per path and scan) are the path's churn per
    scan. A piece's `churn_cum` is the sum of its path's events at or before its `vf`; a closed one's `churn_end` those at
    or before its `vt`."""
    rest = ", ".join(f"p.{c}" for c in cols if c not in ("depth", "path", "vf", "vt"))
    return f"""WITH e AS (SELECT depth, path, t, sum(c) OVER (PARTITION BY depth, path ORDER BY t) AS cum FROM ({events})),
        p AS (SELECT * FROM ({pieces})),
        cut AS (SELECT p.depth, p.path, p.vf AS pvf, e.t FROM p JOIN e ON e.depth = p.depth AND e.path = p.path AND p.vf < e.t AND e.t < p.vt),
        b AS (SELECT depth, path, vf AS pvf, vf AS lo FROM p UNION ALL SELECT depth, path, pvf, t FROM cut),
        s AS (SELECT depth, path, pvf, lo, coalesce(lead(lo) OVER (PARTITION BY depth, path, pvf ORDER BY lo), p_vt) AS hi
              FROM (SELECT b.*, p.vt AS p_vt FROM b JOIN p ON p.depth = b.depth AND p.path = b.path AND p.vf = b.pvf))
        SELECT s.depth, s.path, s.lo AS vf, s.hi AS vt, {rest}, coalesce(e.cum, 0)::BIGINT AS {CHURN},
            CASE WHEN s.hi <> {OPEN} THEN coalesce(x.cum, 0) END::BIGINT AS {CHURN_END}
        FROM s JOIN p ON p.depth = s.depth AND p.path = s.path AND p.vf = s.pvf
        ASOF LEFT JOIN e ON e.depth = s.depth AND e.path = s.path AND e.t <= s.lo
        ASOF LEFT JOIN e x ON x.depth = s.depth AND x.path = s.path AND x.t <= s.hi"""


def churn_gen(con, gen_dir: Path, k: int, base: int, work: Path, *, jobs: int = 1) -> dict:
    """Set `churn_cum` on every range's `pvl/r####.parquet` under `gen_dir` (rewritten in place, `cols` + `churn_cum`),
    counting churn after `base`: each range's file events and their ancestors' sums (`work/churn/anc/`), then per range
    its pieces with their files' events and its directories' summed ancestor events (`apply_sql`). Returns counts."""
    from pyrmts.intervals import key_range_pieces

    from .interval_store import RANGE_RG

    t0 = monotonic()
    ranges = json.loads((gen_dir / "ranges.json").read_text())["ranges"]
    anc = work / "churn" / "anc"
    anc.mkdir(parents=True, exist_ok=True)
    rf = lambda i: gen_dir / "pvl" / f"r{i:04d}.parquet"  # noqa: E731

    def roll(cur, i: int) -> int:
        cur.execute(f"""COPY ({ancestors_sql(events_sql(f"SELECT * FROM read_parquet({q(str(rf(i)))})", base))})
            TO {q(str(anc / f"r{i:04d}.parquet"))} (FORMAT parquet)""")
        return cur.execute(f"SELECT count(*) FROM read_parquet({q(str(anc / f'r{i:04d}.parquet'))})").fetchone()[0]

    n_anc = sum(sn.each(con, roll, list(range(k)), jobs))
    t1 = monotonic()
    all_anc = f"read_parquet({q(str(anc / 'r*.parquet'))})"

    def apply(cur, i: int) -> dict:
        src = rf(i)
        cols = [c for c in pq.read_schema(src).names if c not in (CHURN, CHURN_END)]
        preds = key_range_pieces(sn.RANGE_COLS, ranges[i]["lo"], ranges[i]["hi"])
        dirs = " UNION ALL ".join(f"SELECT * FROM {all_anc} WHERE {p}" for p in preds)
        pieces = f"SELECT * FROM read_parquet({q(str(src))})"
        events = f"""SELECT depth, path, t, sum(c) AS c FROM ({events_sql(pieces, base)} UNION ALL {dirs}) GROUP BY ALL"""
        out_schema = pq_schema_with_churn(pq.read_schema(src))
        cur.execute(f"CREATE OR REPLACE TEMP TABLE ch AS {apply_sql(pieces, events, cols)}")
        tmp = src.with_suffix(".churn.parquet")
        n = sn.write_sorted(sn._batches(cur, f"SELECT {', '.join(out_schema.names)} FROM ch ORDER BY depth, path, vf"), tmp, out_schema,
                            RANGE_RG, dictionary=[c for c in ("kind", "us") if c in out_schema.names])
        n_in = cur.execute(f"SELECT count(*) FROM read_parquet({q(str(src))})").fetchone()[0]
        cur.execute("DROP TABLE ch")
        tmp.replace(src)
        return {"pieces": n_in, "out": n}

    got = list(sn.each(con, apply, list(range(k)), jobs))
    doc = {"anc_rows": n_anc, "pieces": sum(g["pieces"] for g in got), "split": sum(g["out"] - g["pieces"] for g in got),
           "roll_s": round(t1 - t0, 1), "apply_s": round(monotonic() - t1, 1)}
    err(f"churn: {doc['anc_rows']:,} ancestor rows, {doc['pieces']:,} pieces (+{doc['split']:,} split) in {doc['roll_s']}+{doc['apply_s']}s")
    return doc


# ── Per scan: an append's churn ────────────────────────────────────────────


def scan_churn(con, pvl: str, D: int, roots: list[str], name: str, out: Path, count: bool = True) -> dict:
    """One key range's churn at scan `D`, from table `pvl` (its folded path versions after `D`'s append): its files' events
    at D (a close record and an opening at D, |Δsize|; either alone, its size), with their ancestors' sums
    (`out/churn/<name>.parquet`: `depth, path, t, c`); and its paths opened at D that weren't open before (`out/prior/
    <name>.parquet`: `depth, path, prior`) with their cumulative churn when they went away — the last earlier piece's
    `churn_end`, over the store's `roots` (the base's range files, the runs' deltas: one row per piece, the
    smallest `vt`). `count` false (the scan where objects are first listed, `counts_at`): no events. Returns counts."""
    events = f"""SELECT coalesce(n.depth, o.depth) AS depth, coalesce(n.path, o.path) AS path, {D}::BIGINT AS t,
            abs(coalesce(n.size, 0) - coalesce(o.size, 0))::BIGINT AS c
        FROM (SELECT depth, path, size FROM {pvl} WHERE vt = {D} AND kind = 'file') o
        FULL JOIN (SELECT depth, path, size FROM {pvl} WHERE vf = {D} AND kind = 'file') n ON n.depth = o.depth AND n.path = o.path"""
    ev = f"SELECT * FROM ({events}) WHERE c > 0 AND {str(count).lower()}"
    for sub in (SCAN_CHURN, PRIOR):
        (out / sub).mkdir(parents=True, exist_ok=True)
    churn_f, prior_f = out / SCAN_CHURN / f"{name}.parquet", out / PRIOR / f"{name}.parquet"
    con.execute(f"COPY (SELECT * FROM ({ev}) UNION ALL {ancestors_sql(ev)}) TO {q(str(churn_f))} (FORMAT parquet)")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE __back AS SELECT depth, path FROM (SELECT depth, path FROM {pvl} WHERE vf = {D}) n
        ANTI JOIN (SELECT depth, path FROM {pvl} WHERE vt = {D}) c USING (depth, path)""")
    files = [f"{r}/pvl/{name}.parquet" for r in roots if Path(f"{r}/pvl/{name}.parquet").exists()]
    hist = " UNION ALL ".join(f"SELECT depth, path, vf, vt, {CHURN_END} FROM read_parquet({q(f)}) SEMI JOIN __back USING (depth, path)" for f in files)
    prior = (f"""SELECT depth, path, arg_max({CHURN_END}, vf)::BIGINT AS prior FROM (
            SELECT * FROM ({hist}) QUALIFY row_number() OVER (PARTITION BY depth, path, vf ORDER BY vt) = 1)
        WHERE vt <= {D} GROUP BY depth, path""" if files else "SELECT NULL::UTINYINT AS depth, NULL::VARCHAR AS path, NULL::BIGINT AS prior LIMIT 0")
    con.execute(f"COPY ({prior}) TO {q(str(prior_f))} (FORMAT parquet)")
    n_back = con.execute("SELECT count(*) FROM __back").fetchone()[0]
    con.execute("DROP TABLE __back")
    return {"rows": con.execute(f"SELECT count(*) FROM read_parquet({q(str(churn_f))})").fetchone()[0], "opened": n_back,
            "prior": con.execute(f"SELECT count(*) FROM read_parquet({q(str(prior_f))})").fetchone()[0]}


def apply_run(con, deltas: Path, state: Path, ranges: dict, D: int, out: Path, open_rows=None) -> dict:
    """Scan `D`'s run: its `pvl` deltas (`deltas/pvl/r####.parquet`) with `churn_cum` set from every range's churn at D
    (`state/churn/`) and the reappearing paths' earlier churn (`state/prior/`), written to `out/pvl/` (may be `deltas`),
    and the versions opened at D to `state/pvl-fix/` (the next append's open state takes them). A version closed at D gets
    `churn_end` = its `churn_cum` plus its path's churn at D; an opened version's `churn_cum` is its path's at D⁻ (the
    version it closes, else its `prior`, else 0) plus its churn at D. A path churned at
    D without an opened version (a same-size swap) splits: its open version (from `open_rows(name)`, a relation of the
    range's open versions after D; read only then) closes at D and reopens with its new `churn_cum`. A range whose opened
    and closed versions all have theirs is done (a rerun). Returns counts."""
    from pyrmts.intervals import key_range_pieces

    from .interval_store import RANGE_RG

    all_c = f"read_parquet({q(str(state / SCAN_CHURN / '*.parquet'))})"
    (out / "pvl").mkdir(parents=True, exist_ok=True)
    (state / FIX).mkdir(parents=True, exist_ok=True)
    tot = {"opened": 0, "split": 0, "done": 0}
    for r in ranges["ranges"]:
        name = f"r{r['i']:04d}"
        src = deltas / "pvl" / f"{name}.parquet"
        d = f"read_parquet({q(str(src))})"
        schema = pq.read_schema(src)
        fix_schema = pa.schema([f for f in schema if f.name != "op"])
        dict_cols = [c for c in ("kind", "us") if c in schema.names]
        n1, nulls = con.execute(f"""SELECT count(*), count(*) FILTER (WHERE (op = 1 AND {CHURN} IS NULL) OR (op = -1 AND {CHURN_END} IS NULL))
            FROM {d}""").fetchone()
        if n1 and not nulls:
            tot["done"] += 1
            if out != deltas:
                shutil.copyfile(src, out / "pvl" / f"{name}.parquet")
            sn.write_sorted(sn._batches(con, f"SELECT {', '.join(fix_schema.names)} FROM {d} WHERE op = 1 ORDER BY depth, path"),
                            state / FIX / f"{name}.parquet", fix_schema, RANGE_RG, dictionary=dict_cols)
            continue
        preds = key_range_pieces(sn.RANGE_COLS, r["lo"], r["hi"])
        con.execute(f"""CREATE OR REPLACE TEMP TABLE __c AS SELECT depth, path, sum(c)::BIGINT AS c FROM (
            {" UNION ALL ".join(f"SELECT * FROM {all_c} WHERE {p}" for p in preds)}) GROUP BY depth, path""")
        prior = state / PRIOR / f"{name}.parquet"
        cols = [c for c in schema.names if c not in (CHURN, CHURN_END, "op")]
        sel = ", ".join(f"o.{c}" for c in cols)
        con.execute(f"""CREATE OR REPLACE TEMP TABLE __d AS
            SELECT {sel}, o.{CHURN}, (o.{CHURN} + coalesce(c.c, 0))::BIGINT AS {CHURN_END}, o.op FROM {d} o
                LEFT JOIN __c c ON c.depth = o.depth AND c.path = o.path WHERE o.op = -1
            UNION ALL BY NAME
            SELECT {sel}, (coalesce(cl.{CHURN}, pr.prior, 0) + coalesce(c.c, 0))::BIGINT AS {CHURN}, NULL::BIGINT AS {CHURN_END}, o.op FROM {d} o
                LEFT JOIN (SELECT depth, path, {CHURN} FROM {d} WHERE op = -1) cl ON cl.depth = o.depth AND cl.path = o.path
                LEFT JOIN read_parquet({q(str(prior))}) pr ON pr.depth = o.depth AND pr.path = o.path
                LEFT JOIN __c c ON c.depth = o.depth AND c.path = o.path
                WHERE o.op = 1""")
        con.execute(f"""CREATE OR REPLACE TEMP TABLE __f AS SELECT depth, path, c FROM __c
            ANTI JOIN (SELECT depth, path FROM __d) x USING (depth, path)""")
        n_split = con.execute("SELECT count(*) FROM __f").fetchone()[0]
        if n_split:
            rows = open_rows(name)
            rest = ", ".join(f"s.{c}" for c in cols if c not in ("vf", "vt"))
            con.execute(f"""INSERT INTO __d BY NAME
                SELECT s.vf, {D}::BIGINT AS vt, {rest}, s.{CHURN}, (s.{CHURN} + f.c)::BIGINT AS {CHURN_END}, -1::TINYINT AS op
                    FROM ({rows}) s JOIN __f f USING (depth, path) WHERE s.vf < {D}
                UNION ALL BY NAME
                SELECT {D}::BIGINT AS vf, s.vt, {rest}, (s.{CHURN} + f.c)::BIGINT AS {CHURN}, NULL::BIGINT AS {CHURN_END}, 1::TINYINT AS op
                    FROM ({rows}) s JOIN __f f USING (depth, path) WHERE s.vf < {D}""")
        order = "depth, path, vf, op"
        tmp = out / "pvl" / f"{name}.parquet.tmp"
        sn.write_sorted(sn._batches(con, f"SELECT {', '.join(schema.names)} FROM __d ORDER BY {order}"), tmp, schema, RANGE_RG, dictionary=dict_cols)
        sn.write_sorted(sn._batches(con, f"SELECT {', '.join(fix_schema.names)} FROM __d WHERE op = 1 ORDER BY depth, path"),
                        state / FIX / f"{name}.parquet", fix_schema, RANGE_RG, dictionary=dict_cols)
        tmp.replace(out / "pvl" / f"{name}.parquet")
        tot["opened"] += con.execute("SELECT count(*) FROM __d WHERE op = 1").fetchone()[0]
        tot["split"] += n_split
        con.execute("DROP TABLE __c; DROP TABLE __d; DROP TABLE __f")
    err(f"churn at {D}: {tot['opened']:,} opened versions set, {tot['split']:,} split, {tot['done']} ranges already done")
    return tot


# ── An existing generation: the backfill ───────────────────────────────────


def backfill(store, gen: str, new_gen: str, work: Path, *, ranges_gen: str | None = None, rg_rows: int | None = None,
             threads: int = 4, mem: str = "4GB", jobs: int = 1) -> dict:
    """Generation `gen` of `store` (its base and newest manifest's runs) folded into `new_gen` (`interval_compact.fold_gen`)
    with `churn_cum`/`churn_end` set over the folded path versions (`churn_gen`, from their own pieces: every file version
    boundary's |Δ| rolled up to its ancestors) and `"churn": true` in its `scans.json`; published as `backfill-generation`
    publishes (data first, `scans.json` last; `CURRENT.json` untouched). Refused on a generation that carries it."""
    from . import interval_compact as ic
    from . import interval_store as ist
    from .append_runner import LocalRunStore, latest_key
    from .blobstore import upload_new
    from .interval_backfill import _publish_order, mirror_source
    from .interval_local import publish_tier

    t0 = monotonic()
    prefix, new_prefix = f"{ist.PREFIX}/{gen}", f"{ist.PREFIX}/{new_gen}"
    gen_dir = mirror_source(store, gen, new_gen, work, ranges_gen)
    if has_churn(json.loads((gen_dir / "scans.json").read_text())):
        raise SystemExit(f"{store.root}/{prefix}: already carries `{CHURN}`")
    lstore = LocalRunStore(gen_dir, work / "lease", gen)
    newest = latest_key(lstore.keys("manifests/"))
    runs = lstore.read_json(newest)["runs"] if newest else []
    out = work / "store" / new_prefix
    got: dict = {}

    def before_cut(con, folded: Path, doc: dict) -> None:
        k = json.loads((folded / "ranges.json").read_text())["k"]
        got.update(churn_gen(con, folded, k, churn_from(doc["scans"])["ts"], work / "churn", jobs=jobs))
        new = record_churn(doc)
        doc.clear()
        doc.update(new)

    doc, rows, sorts, stats = ic.fold_gen(gen_dir, out, work, gen=gen, runs=runs, rg_rows=rg_rows or ist.SERVED_RG, threads=threads, mem=mem,
                                          jobs=jobs, before_cut=before_cut)
    rec = {"from": gen, "manifest": newest, "runs": [r["key"] for r in runs], "scans": len(doc["scans"]), "base": churn_from(doc["scans"])["id"], **got}
    (out / BACKFILL).write_text(json.dumps(rec, indent=1) + "\n")
    files = upload_new(store, out, new_prefix, {}, _publish_order, tier=publish_tier)
    s = round(monotonic() - t0, 1)
    err(f"backfill-churn {gen} → {new_gen}: {got['pieces']:,} pieces (+{got['split']:,} split), {stats['versions']:,} versions, {len(files)} files in {s}s")
    return {**rec, "gen": new_gen, "rows": rows, "sorts": sorts, "files": len(files), "s": s}


@command("backfill-churn")
@option("-g", "--gen", help="The source generation (default: the store's current, followed to its newest successor); never rewritten")
@option("-j", "--jobs", default=1, type=IntRange(min=1), help="Key ranges, then served sorts, folded this many at a time, on cursors of one DuckDB database (-p threads and -M memory shared). The output's bytes don't depend on it")
@option("-M", "--mem", default="4GB", help="DuckDB memory limit")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`), the readers' INDEX_R2 root")
@option("-O", "--to-gen", required=True, help="The new generation: `<store>/interval-store/TO_GEN/` (must not exist)")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-R", "--ranges-gen", help="The generation holding GEN's key ranges and base range files, when GEN borrows them (a staged build: its deployment's `ranges_gen`); default GEN")
@option("-r", "--rg-rows", type=IntRange(min=1), help="Rows per served row group (default the store's)")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after); the source generation is mirrored into it")
def backfill_cmd(gen, jobs, mem, store_root, to_gen, threads, ranges_gen, rg_rows, work) -> None:
    """Fold a generation into a new one whose folded path versions carry their churn (`churn_cum`, `churn_end`;
    specs/diff-progressive.md §3), from its own versions: each file's |Δsize| at every version boundary after the churn
    base, rolled up to its ancestors. The source and `CURRENT.json` are left as they are: verify the new generation, then
    `interval-store current -s TO_GEN`. Prints a JSON summary."""
    from . import interval_compact as ic
    from .blobstore import Store
    from .static_local import _check_gen, workdir

    _check_gen(to_gen)
    store = Store(store_root)
    if gen is not None:
        _check_gen(gen)
    else:
        gen, how = ic.resolve(store)
        if gen is None:
            raise SystemExit(f"{store.root}/interval-store: no current generation ({how}): name one with -g")
    gen = ic.chain(store, gen)[-1]
    with workdir(work) as w:
        print(json.dumps(backfill(store, gen, to_gen, w, ranges_gen=ranges_gen, rg_rows=rg_rows, threads=threads, mem=mem, jobs=jobs), indent=1))
