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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

import pyarrow as pa
import pyarrow.parquet as pq

from . import static_drill as sd
from .static_names import CODEC, err, q

#: `roots-measure/`'s per-kind subdirectories (`static_local.finish_drill`).
SUBS = {"long": "dirs", "short": "short/dirs"}
MEASURE_COLS = "q VARCHAR, k INTEGER, dir VARCHAR, rows BIGINT"
#: Input rows (base measurement, run headers and counts) per `q` range `measure_kind` joins and writes at a time: cw's
#: 2026-11-22..25 compactions, joining every range at once over `gov_reports`' growth, ran DuckDB out of 251 GiB.
MEASURE_CHUNK_ROWS = 50_000_000
#: `$DT_MEASURE_VERIFY=N`: N ranges (evenly spaced) of a cut measurement also joined the uncut way, over the whole
#: tables, and compared row for row (a mismatch raises): the cut checked on real data, at ~one uncut range's cost apiece.
MEASURE_VERIFY_ENV = "DT_MEASURE_VERIFY"
#: About how many sampled `q` values the ranges are cut from
RANGE_SAMPLE_ROWS = 100_000
#: DuckDB's Arrow batch (`to_arrow_table`'s, `to_arrow_reader`'s default) and pyarrow's default row group: `write_sorted`
#: re-cuts a stream to both, so it writes the file one `pq.write_table` of the whole result writes, byte for byte.
ARROW_BATCH_ROWS = 1_000_000
ROW_GROUP_ROWS = 1024 * 1024


def q_ranges(con, tables: list[str], target: int) -> list[tuple[str | None, str | None]]:
    """`[lo, hi)` ranges of the `q` column over `tables` (tables, not views: their `rowid`s), of about `target` rows each
    (None: open), or one unbounded range when they hold fewer. Cut at sampled values: every n / `RANGE_SAMPLE_ROWS`-th
    row of each table by `rowid` (a filter every thread applies, reading `q` alone; a seeded reservoir sample ran on
    one thread, ~5 min over cw's ~10B rows). The cuts only chunk the work: `measure_kind` writes the same bytes at any."""
    n = sum(con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables)
    parts = -(-n // target)
    if parts <= 1:
        return [(None, None)]
    every = max(1, n // RANGE_SAMPLE_ROWS)
    union = " UNION ALL ".join(f"SELECT q FROM {t} WHERE rowid % {every} = 0" for t in tables)
    sample = [r[0] for r in con.execute(f"SELECT q FROM ({union}) ORDER BY q").fetchall()]
    cuts = sorted({sample[len(sample) * i // parts] for i in range(1, parts)})
    bounds = [None, *cuts, None]
    return list(zip(bounds[:-1], bounds[1:]))


def range_index_sql(col: str, cuts: list[str]) -> str:
    """The index of the `q_ranges` range `col` falls in, given their inner bounds `cuts` (sorted): the number of cuts
    ≤ `col`, so range `i` is `[cuts[i-1], cuts[i])` as `q_range_sql` reads it. A balanced tree of comparisons (⌈log2⌉ of
    them per row), not one per cut."""
    def go(lo: int, hi: int) -> str:
        if lo == hi:
            return str(lo)
        mid = (lo + hi) // 2
        return f"(CASE WHEN {col} < {q(cuts[mid])} THEN {go(lo, mid)} ELSE {go(mid + 1, hi)} END)"
    return go(0, len(cuts))


def q_range_sql(col: str, lo: str | None, hi: str | None) -> str:
    """`col` in `[lo, hi)` (None: unbounded)."""
    return " AND ".join([*([f"{col} >= {q(lo)}"] if lo is not None else []), *([f"{col} < {q(hi)}"] if hi is not None else [])]) or "true"


def _rebatched(batches, rows: int):
    """`batches` re-cut into batches of exactly `rows` (the last may be shorter)."""
    buf, n = [], 0
    for b in batches:
        off = 0
        while off < len(b):
            take = min(rows - n, len(b) - off)
            buf.append(b.slice(off, take))
            n, off = n + take, off + take
            if n == rows:
                yield pa.concat_batches(buf)
                buf, n = [], 0
    if n:
        yield pa.concat_batches(buf)


def write_sorted(batches, empty: pa.Table, path: Path) -> int:
    """Write `batches` (Arrow record batches, in order; `empty`: their schema, written when there are none) to `path` as
    `pq.write_table` writes their concatenation as one DuckDB result: DuckDB's 1M-row batches, pyarrow's 1Mi-row groups.
    Holds at most a row group. Returns the rows written."""
    writer, pending, n, total = None, [], 0, 0

    def flush(table: pa.Table) -> None:
        nonlocal writer
        if writer is None:
            writer = pq.ParquetWriter(path, table.schema, compression=CODEC)
        writer.write_table(table, row_group_size=ROW_GROUP_ROWS)

    for b in _rebatched(batches, ARROW_BATCH_ROWS):
        pending.append(b)
        n += len(b)
        total += len(b)
        if n >= ROW_GROUP_ROWS:
            t = pa.Table.from_batches(pending)
            flush(t.slice(0, ROW_GROUP_ROWS))
            rest = t.slice(ROW_GROUP_ROWS)
            pending, n = rest.to_batches(), len(rest)
    if pending and n:
        flush(pa.Table.from_batches(pending))
    if writer is None:
        pq.write_table(empty, path, compression=CODEC)
    else:
        writer.close()
    return total


def _files(fs: list[str]) -> str:
    return "[" + ", ".join(q(f) for f in fs) + "]"


def _union(parts: list[str], empty: str) -> str:
    return " UNION ALL ".join(parts) if parts else f"SELECT * FROM ({empty}) WHERE false"


#: The runs' counts (`im_d`) and the floor crossers (`im_c`) are built in hash parts of about this many input rows
#: (`measure_kind`; their files' row counts), each aggregated alone in memory, at most `AGG_PARTS_MAX` of them: one
#: aggregate over cw's 8.6B counts (2026-11-27) spilled and finished on ~6 of 48 CPUs for 16 of its 23 min.
AGG_PART_ROWS = 200_000_000
AGG_PARTS_MAX = 256
#: At least this many floor crossers are counted on worker processes (`base_counts_parallel`); fewer, here.
PARALLEL_MIN = 200_000
#: `base_counts_parallel`'s shares: at least this many crossers each (and at most 8 per process).
SHARE_ROWS_MIN = 10_000
#: Decoded groups each worker keeps (`CachedGroupFile.cap_groups`): its share is sorted, so neighbors share groups and a few
#: suffice; the default (`static_drill.GROUP_CACHE`, 2048) on 48 workers took a highmem-48 past its RAM (cw, 2026-11-07).
WORKER_GROUPS = 16


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
    if (gf := tier.group_file(kind, "roots")) is not None:
        gf.cap_groups = WORKER_GROUPS
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


#: Bytes free on the work disk `local_tier` leaves after its copy, per byte copied: else the tier is read in place.
LOCAL_TIER_HEADROOM = 1.5


def same_disk(a: Path, b: Path) -> bool:
    """Whether `a` and `b` are on one filesystem (`local_tier` copies only across one)."""
    return os.stat(a).st_dev == os.stat(b).st_dev


def local_tier(tier: sd.Tier, kind: str, dst: Path, *, threads: int = 16) -> sd.Tier:
    """`tier`, with what `kind`'s roots group file reads (`meta.json`, `aliases.parquet`, the roots index and its top, the
    roots files) copied to `dst` on the work disk, as a `Tier` there; `tier` itself when it has no mount (`GcsTier`), is on
    the work disk already, or the disk lacks room (`LOCAL_TIER_HEADROOM`, logged). The compaction's base tier is its generation's mount (gcsfuse):
    `base_counts_parallel`'s workers decoding its groups in place there sat at ~10 of 48 CPUs for hours (cw 2026-11-27's
    level-5 fold, 2.0B floor crossers); a sequential copy is minutes."""
    from concurrent.futures import ThreadPoolExecutor

    from .heartbeat import Progress

    dst.mkdir(parents=True, exist_ok=True)
    if isinstance(tier, sd.GcsTier) or same_disk(tier.root, dst):
        return tier
    rels = [r for r in ("meta.json", "aliases.parquet", f"{kind}-roots-index.parquet", f"{kind}-roots-index.top.parquet") if tier.exists(r)]
    rels += tier.listdir(f"{kind}/roots") if tier.exists(f"{kind}/roots") else []
    sizes = {r: (tier.root / r).stat().st_size for r in rels}
    need, free = sum(sizes.values()), shutil.disk_usage(dst).free
    if need * (1 + LOCAL_TIER_HEADROOM) > free:
        err(f"local tier {tier.name} {kind}: {need / 2**30:.1f} GiB won't fit {free / 2**30:.1f} GiB free; read in place from {tier.root}")
        return tier
    prog = Progress(f"local tier {tier.name} {kind}", len(rels), total_bytes=need, unit="files")

    def copy(rel: str) -> None:
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tier.root / rel, dst / rel)
        prog.tick(nbytes=sizes[rel])
    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(copy, rels))
    prog.done()
    return sd.Tier(dst, tier.name)


def base_counts_parallel(con, tier: sd.Tier, kind: str, src: str, into: str, n: int, *, jobs: int, work: Path) -> None:
    """`base_counts` of DuckDB table `src`'s `(c, dir)` rows (`n` of them) into table `into` `(c, dir, rows)`, on `jobs` worker
    processes: `src` cut in sorted shares (parquet under `work`), each counted against the tier's own group files, copied to
    the work disk first (`local_tier`). Nothing of `src` is held in Python here: a compaction's crossers ran to 191M (cw,
    2026-11-06), 2.0B (2026-11-27). Logs its progress by share (`heartbeat.Progress`: done, crossers counted, ETA)."""
    from concurrent.futures import as_completed
    from disk_tree.spawned import spawn_pool

    from .heartbeat import Progress, set_stage

    parts = max(1, min(jobs * 8, n // SHARE_ROWS_MIN))
    cut = work / "cands"
    # every crosser sorted (9 min of cw's 2026-11-27 fold, 2.0B): `(c, dir)` ranges cut at sampled keys cost as much in a
    # synthetic trial (60M rows: their comparisons 6.5 s, the whole sorted cut 6.1 s)
    set_stage(f"base counts {kind}: {n:,} floor crossers sorted into {parts} shares")
    con.execute(f"""COPY (SELECT c, dir, ((row_number() OVER (ORDER BY c, dir) - 1) * {parts} // {n})::INTEGER AS part FROM {src})
        TO {q(str(cut))} (FORMAT parquet, PARTITION_BY (part))""")
    shares = sorted((int(d.name.split("=", 1)[1]), sorted(str(f) for f in d.glob("*.parquet"))) for d in cut.glob("part=*"))
    err(f"base counts {kind}: {n:,} floor crossers in {len(shares)} shares on {jobs} processes")
    tier = local_tier(tier, kind, work / "tier")
    prog = Progress(f"base counts {kind}", len(shares), total_rows=n, unit="shares")
    with spawn_pool(jobs, _bc_init, (str(tier.root), tier.name, kind)) as pool:
        for f in as_completed([pool.submit(_bc_part, fs, str(work / f"out-{i:05d}.parquet")) for i, fs in shares]):
            prog.tick(rows=f.result())
    prog.done()
    con.execute(f"INSERT INTO {into} SELECT c, dir, rows FROM read_parquet({q(str(work / 'out-*.parquet'))})")


@contextmanager
def open_partitions(con, n: int) -> Iterator[None]:
    """DuckDB keeps a file per partition open at once while a `COPY … PARTITION_BY` of `n` partitions writes (its default
    100 would close and reopen them, cutting each into many files)."""
    opened = con.execute("SELECT current_setting('partitioned_write_max_open_files')").fetchone()[0]
    con.execute(f"SET partitioned_write_max_open_files = {max(int(opened), n + 1)}")
    try:
        yield
    finally:
        con.execute(f"SET partitioned_write_max_open_files = {opened}")


def measure_kind(con, tiers: list[sd.Tier], meas: Path, kind: str, out: Path, *, floor: int, members: str | None = None,
                 jobs: int | None = None) -> dict:
    """One kind's measurement → `out` (`(q, k, dir, rows)` sorted, as `finish_drill` writes it), from `tiers` (the base's
    drill, then the runs', oldest first), the base's `roots-measure/` `meas`, and (long) the members' `members` parquet
    (column `q`). The floor crossers' base rows are counted on `jobs` processes (default: every CPU) when there are at least
    `PARALLEL_MIN` of them. Returns counts."""
    from .heartbeat import Progress, fmt_secs, set_stage

    jobs = jobs or os.cpu_count() or 1
    runs = list(enumerate(tiers))[1:]
    long = kind == "long"

    def build(table: str, what: str, sql: str) -> None:
        """`CREATE OR REPLACE TEMP TABLE table AS sql`, named on the liveness line while it runs and logged after (rows,
        seconds): each is one statement over the whole corpus, minutes long on cw's."""
        set_stage(f"measure {kind}: {what} ({table})")
        t = monotonic()
        con.execute(f"CREATE OR REPLACE TEMP TABLE {table} AS {sql}")
        err(f"measure {kind}: {what} ({table}): {con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]:,} rows in {fmt_secs(monotonic() - t)}")
    if long:
        cols = ", ".join(f"coalesce(a{i}.canonical, m.q) AS c{i}" for i in range(len(tiers)))
        joins = " ".join(f"LEFT JOIN im_a{i} AS a{i} USING (q)" for i in range(len(tiers)))
        for i, t in enumerate(tiers):
            con.register(f"im_a{i}_in", t.alias_table())
            con.execute(f"CREATE OR REPLACE TEMP TABLE im_a{i} AS SELECT q, canonical FROM im_a{i}_in")
            con.unregister(f"im_a{i}_in")
        # each member's canonical per tier; `base`: the base knows it (else it has no base roots)
        build("im_mt", "members' canonicals", f"""SELECT m.q, {cols}, a0.q IS NOT NULL AS base
            FROM (SELECT DISTINCT q FROM read_parquet({q(members)})) AS m {joins}""")

    def keyed(i: int, sql: str) -> str:
        """`sql`'s rows (`q`: tier `i`'s canonical) per member (long) or literal (short)."""
        return f"SELECT mt.q, s.* EXCLUDE (q) FROM ({sql}) AS s JOIN im_mt AS mt ON mt.c{i} = s.q" if long else sql

    heads = [keyed(i, f"""SELECT q, dir, b AS rows, {i} AS tier FROM read_parquet({_files(fs)})
        WHERE kind IN ({sd.KIND_FULL}, {sd.KIND_DELTA})""") for i, t in runs if (fs := t.files(kind, "rollups"))]
    dfiles = [(i, f) for i, t in runs if (f := t.dcount(kind))]
    # each count keyed by its member (long) or literal (short), with what its floor crossing is counted under (`c0`: the
    # member's base canonical; the literal itself)
    dcs = [(f"SELECT mt.q, mt.c0, s.dir, s.rows FROM read_parquet({q(f)}) AS s JOIN im_mt AS mt ON mt.c{i} = s.q" if long else
            f"SELECT q, q AS c0, dir, rows FROM read_parquet({q(f)})") for i, f in dfiles]
    build("im_h", f"the {len(heads)} runs' rollup heads", f"""SELECT q, dir, arg_max(rows, tier) AS rows
        FROM ({_union(heads, "SELECT NULL::VARCHAR AS q, NULL::VARCHAR AS dir, NULL::BIGINT AS rows, NULL::INTEGER AS tier")}) GROUP BY q, dir""")
    top = sorted(str(p) for p in (meas / "short" / "top").glob("*.parquet")) if not long else []
    base = sd.meas_sql(kind, sorted(str(p) for p in (meas / SUBS[kind]).glob("*.parquet")), top)
    build("im_b", "the base measurement", f"SELECT b.q, b.dir, b.rows FROM ({base}) AS b" + (" SEMI JOIN im_mt USING (q)" if long else ""))
    counts = _union(dcs, "SELECT NULL::VARCHAR AS q, NULL::VARCHAR AS c0, NULL::VARCHAR AS dir, NULL::BIGINT AS rows")
    # touched below the base's floor, no header: the base's rows there are counted (per base canonical)
    c0, mt = ("mt.c0", "JOIN im_mt AS mt USING (q) WHERE mt.base") if long else ("d.q", "")
    crossers = f"SELECT DISTINCT {c0} AS c, d.dir FROM {{d}} AS d ANTI JOIN im_h USING (q, dir) ANTI JOIN im_b USING (q, dir) {mt}"
    rows_in = con.execute(f"SELECT coalesce(sum(num_rows), 0) FROM parquet_file_metadata({_files([f for _, f in dfiles])})").fetchone()[0] if dfiles else 0
    nparts = min(AGG_PARTS_MAX, max(1, -(-rows_in // AGG_PART_ROWS)))
    if nparts == 1:
        build("im_d", f"the {len(dcs)} runs' counts", f"SELECT q, dir, sum(rows)::BIGINT AS rows FROM ({counts}) GROUP BY q, dir")
        build("im_c", "floor crossers", crossers.format(d="im_d"))
    else:
        # In hash parts by `c0` (each `(q, dir)` key's rows, and each crosser `(c0, dir)`, fall in exactly one): one streaming
        # pass writes them, then each part is summed and its crossers found alone, in memory, on every core
        out.parent.mkdir(parents=True, exist_ok=True)
        split_d = Path(tempfile.mkdtemp(prefix=f"counts-{kind}-", dir=out.parent))
        try:
            set_stage(f"measure {kind}: the {len(dcs)} runs' counts ({rows_in:,} rows) cut into {nparts} hash parts")
            t = monotonic()
            with open_partitions(con, nparts):
                con.execute(f"COPY (SELECT q, dir, rows, hash(c0) % {nparts} AS h FROM ({counts})) TO {q(str(split_d))} (FORMAT parquet, PARTITION_BY (h))")
            cut_s = monotonic() - t
            con.execute("CREATE OR REPLACE TEMP TABLE im_d (q VARCHAR, dir VARCHAR, rows BIGINT)")
            con.execute("CREATE OR REPLACE TEMP TABLE im_c (c VARCHAR, dir VARCHAR)")
            prog = Progress(f"measure {kind}: counts and crossers", nparts, unit="hash parts")
            for h in range(nparts):
                set_stage(f"measure {kind}: counts and crossers, hash part {h + 1}/{nparts}")
                if files := sorted(str(f) for f in (split_d / f"h={h}").glob("*.parquet")):
                    con.execute(f"""CREATE OR REPLACE TEMP TABLE im_dh AS SELECT q, dir, sum(rows)::BIGINT AS rows
                        FROM read_parquet({_files(files)}, hive_partitioning = false) GROUP BY q, dir""")
                    con.execute("INSERT INTO im_d SELECT * FROM im_dh")
                    con.execute(f"INSERT INTO im_c {crossers.format(d='im_dh')}")
                prog.tick()
            prog.done()
            con.execute("DROP TABLE IF EXISTS im_dh")
        finally:
            shutil.rmtree(split_d, ignore_errors=True)
        nd, nc = (con.execute(f"SELECT count(*) FROM {x}").fetchone()[0] for x in ("im_d", "im_c"))
        err(f"measure {kind}: the {len(dcs)} runs' counts (im_d) and floor crossers (im_c) in {nparts} hash parts: {nd:,} and {nc:,} rows in "
            f"{fmt_secs(monotonic() - t)} (the cut {fmt_secs(cut_s)})")
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
    # One `q` range at a time (each member's rows lie in one): the ranges' joins, sorted and written in range order, are
    # the whole set's, sorted, without holding it. A short literal's base count (`bx.c`) is its own `q`; a member's is its
    # base canonical's (`im_mt.c0`), so a long range reads the counts of its members' canonicals.
    ranges = q_ranges(con, ["im_h", "im_b", "im_d"], MEASURE_CHUNK_ROWS)
    out.mkdir(parents=True, exist_ok=True)
    split = Path(tempfile.mkdtemp(prefix=f"measure-{kind}-", dir=out.parent)) if len(ranges) > 1 else None
    if split is not None:
        # Each table cut by range once, in one parallel pass apiece (`PARTITION_BY` the range's index): a range's join
        # then reads its own files, never the whole tables (cw's 2026-11-27 fold rescanned ~5B run counts and 2.0B base
        # counts for each of ~150 ranges, 5.5 min apiece; sorting them instead took 2h17m and its join ran out of disk).
        # The base counts go to every range holding a member whose canonical they are.
        bounds = [lo for lo, _ in ranges[1:]]
        rix = range_index_sql("q", bounds)
        cuts = {"im_h": f"SELECT *, {rix} AS r FROM im_h", "im_b": f"SELECT *, {rix} AS r FROM im_b",
                "im_d": f"SELECT *, {rix} AS r FROM im_d"}
        if long:
            cuts["im_mt"] = f"SELECT *, {rix} AS r FROM im_mt"
            cuts["im_bx"] = f"SELECT bx.*, cr.r FROM im_bx AS bx JOIN (SELECT DISTINCT c0 AS c, {rix} AS r FROM im_mt) AS cr USING (c)"
        else:
            cuts["im_bx"] = f"SELECT *, {range_index_sql('c', bounds)} AS r FROM im_bx"
        with open_partitions(con, len(ranges)):
            for t, sql in cuts.items():
                set_stage(f"measure {kind}: {t} cut into {len(ranges)} q ranges")
                t1 = monotonic()
                con.execute(f"COPY ({sql}) TO {q(str(split / t))} (FORMAT parquet, PARTITION_BY (r))")
                err(f"measure {kind}: {t} cut into {len(ranges)} q ranges in {fmt_secs(monotonic() - t1)}")

    def part(i: int, lo: str | None, hi: str | None, uncut: bool = False) -> str:
        def of(t: str, col: str = "q") -> str:
            if split is None or uncut:
                return f"(SELECT * FROM {t} WHERE {q_range_sql(col, lo, hi)})"
            files = sorted(str(f) for f in (split / t / f"r={i}").glob("*.parquet"))
            return f"(SELECT * FROM read_parquet({_files(files)}, hive_partitioning = false))" if files else f"(SELECT * FROM {t} LIMIT 0)"
        bx = (of("im_bx") if split is not None and not uncut else
              f"(SELECT * FROM im_bx SEMI JOIN (SELECT c0 AS c FROM im_mt WHERE {q_range_sql('q', lo, hi)}) USING (c))" if long
              else of("im_bx", "c"))
        return f"""SELECT q::VARCHAR AS q, len(string_split(dir, '/'))::INTEGER AS k, dir::VARCHAR AS dir, rows::BIGINT AS rows FROM (
                SELECT q, dir, rows FROM {of("im_h")}
                UNION ALL SELECT x.q, x.dir, coalesce(b.rows, bx.rows, 0) + coalesce(d.rows, 0) AS rows
                FROM (SELECT q, dir FROM {of("im_b")} UNION SELECT q, dir FROM {of("im_d")}) AS x ANTI JOIN {of("im_h")} AS h USING (q, dir)
                LEFT JOIN {of("im_b")} AS b USING (q, dir) LEFT JOIN {of("im_d")} AS d USING (q, dir) {f"JOIN {of('im_mt')} AS mt USING (q)" if long else ""}
                LEFT JOIN {bx} AS bx ON bx.c = {bc} AND bx.dir = x.dir)
            WHERE rows >= {floor} ORDER BY q, k, dir"""

    n_verify = int(os.environ.get(MEASURE_VERIFY_ENV) or 0) if split is not None else 0
    verify = {round(j * (len(ranges) - 1) / max(1, n_verify - 1)) for j in range(n_verify)} if n_verify > 1 else ({len(ranges) // 2} if n_verify else set())

    def batches():
        prog = Progress(f"measure {kind}", len(ranges), unit="q ranges")
        joining = f"measure {kind}: joining {len(ranges)} q ranges"
        set_stage(joining)
        for i, (lo, hi) in enumerate(ranges):
            if i in verify:
                cut = con.execute(part(i, lo, hi)).to_arrow_table()
                set_stage(f"measure {kind}: checking range {i} against the uncut join")
                t1 = monotonic()
                whole = con.execute(part(i, lo, hi, uncut=True)).to_arrow_table()
                if not cut.equals(whole):
                    raise RuntimeError(f"measure {kind}: range {i} [{lo}, {hi}) cut ({cut.num_rows:,} rows) differs from uncut ({whole.num_rows:,})")
                err(f"measure {kind}: range {i} [{lo}, {hi}): cut = uncut, {cut.num_rows:,} rows (uncut in {fmt_secs(monotonic() - t1)})")
                set_stage(joining)
                yield from cut.to_batches(ARROW_BATCH_ROWS)
            else:
                yield from con.execute(part(i, lo, hi)).to_arrow_reader(ARROW_BATCH_ROWS)
            prog.tick()
        prog.done()

    con.execute(f"CREATE OR REPLACE TEMP TABLE im_out ({MEASURE_COLS})")
    empty = con.execute("SELECT * FROM im_out").to_arrow_table()
    try:
        written = write_sorted(batches(), empty, out / "s0000.parquet")
    finally:
        if split is not None:
            shutil.rmtree(split, ignore_errors=True)
    doc = {k: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for k, t in (("headers", "im_h"), ("dcount", "im_d"), ("base", "im_b"))}
    doc["rows"] = written
    doc["ranges"] = len(ranges)
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
