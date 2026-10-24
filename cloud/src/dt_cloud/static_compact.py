"""Compaction of the static name index (specs/static-compaction.md): a generation `G` (its base `static-names/<G>/` and
the newest manifest's runs) folded into a new base generation `G'`, equal to `static-names build` through the last
scan over `G`'s key ranges (`build -r G/ranges.json`), file for file. This module is the store-specific fold
(`fold_gen`); publishing, the successor record, the pointer, R2 and the predecessor's grace are the shared
orchestrator's (`append_runner.compact_store`, `compactor()` here).

How each part folds (the build's own code wherever a part is a function of others):

- `scans.json` (`G`'s, then each run scan's `deltas/<id>/scans.json` entry, kept on folded runs by
  `static_merge.FOLDED_RETAIN`), `ranges.json` (`G`'s).
- `cintervals/`, `chist/` per key range (`fold_range`): `G`'s coalesced versions and every scan's `cdelta` (a close record
  sets `vt`, an open adds a version), one row per `(depth, path, usr, vf)`, the smallest `vt`; written as
  `build_range(coalesced=True)` writes them.
- `shards.json`: the build's planner over the folded histograms (`G`'s target rows and task count); `sx/`, `sidecar/`,
  `sidecar.parquet`: `static_append.merge_shards` of `G`'s and the runs' suffix rows, cut to that plan.
- `catalog/cells.parquet`, `index.parquet`: `static_append.merge_catalogs` of `G`'s and the runs'; the members
  (`members.parquet`, `members.json`): the build's census over the new shards; `meta.json` from both.
- `drill/` and `roots-measure/` (`fold_drill`): roots are read, not recomputed from the suffix shards. Each member's
  root set through the last scan is `G`'s rows of its class there ∪ each run's rows of its class there (combined: the
  smallest `vt` per `(path, usr, vf)`); members with equal sets (digest as the build's: count and two hash sums) alias
  to the least; then the build's own measurement, rollups and writer (`static_local.finish_drill`).
- `names/`, `anchors/` (when `G` has `anchors/meta.json`): the build's base builders over `G'` (`build_base_anchors`).
- `manifests/<last scan>.json` (no runs), and `compaction.json` (the orchestrator's completion record).
"""
from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from time import monotonic

import pyarrow as pa
import pyarrow.parquet as pq
from click import IntRange, command, option

from . import append_runner as ar
from . import heartbeat as hb
from . import static_anchors as an
from . import static_append as sa
from . import static_catalog as sc
from . import static_drill as sd
from . import static_local as sl
from . import static_measure as sme
from . import static_names as sn
from . import static_roots as sr
from .checkpoint import Checkpoint, code_version
from .static_names import CINTERVAL_SCHEMA, CODEC, INTERVAL_RG, PREFIX, err, q

U64 = 1 << 64


# ── Light: key ranges ──────────────────────────────────────────────────────


def fold_range(con, base_file: str, cdelta_files: list[str], out: Path, name: str, rule=None) -> int:
    """Key range `name`'s coalesced versions through the last scan: `G`'s (`base_file`) and every scan's `cdelta`
    (oldest first), one row per `(depth, path, usr, vf)` with the smallest `vt` → `out/cintervals/<name>.parquet` and
    `out/chist/<name>.parquet`, as `build_range(coalesced=True)` writes them. Returns the versions. Its table is named for
    the range, so ranges fold at once on cursors of one database (`fold_ranges`)."""
    cols = ", ".join(CINTERVAL_SCHEMA.names)
    parts = [f"SELECT {cols} FROM read_parquet({q(f)})" for f in [base_file, *cdelta_files]]
    civ = f"civ_{name}"
    con.execute(f"""CREATE OR REPLACE TABLE {civ} AS SELECT depth, path, usr, vf, min(vt) AS vt, any_value(size) AS size,
            any_value(n_files) AS n_files FROM ({' UNION ALL '.join(parts)}) GROUP BY depth, path, usr, vf""")
    rows = sn.write_sorted(sn._batches(con, f"SELECT * FROM {civ} ORDER BY depth, path, usr, vf"), out / "cintervals" / f"{name}.parquet",
                           CINTERVAL_SCHEMA, INTERVAL_RG, dictionary=["usr"])
    (out / "chist").mkdir(parents=True, exist_ok=True)
    pq.write_table(con.execute(sn.hist_sql(civ, rule)).to_arrow_table(), out / "chist" / f"{name}.parquet", compression=CODEC)
    con.execute(f"DROP TABLE {civ}")
    return rows


def fold_ranges(con, gen_dir: Path, days: list[str], ranges: dict, out: Path, rule=None, jobs: int = 1) -> int:
    """Every key range of `ranges` folded (`fold_range`: `gen_dir`'s base and the scans `days`' `cdelta`s), `jobs` at a time
    on cursors of `con` (`static_names.each`: one `threads` pool and `memory_limit` across them). Each range's files depend
    only on its own rows, so the bytes don't depend on `jobs`. Returns the versions."""
    def one(cur, r: dict) -> int:
        name = f"r{r['i']:04d}"
        return fold_range(cur, str(gen_dir / "cintervals" / f"{name}.parquet"),
                          [str(gen_dir / ar.run_key(d, d) / "cdelta" / f"{name}.parquet") for d in days], out, name, rule)
    return sum(sn.each(con, one, ranges["ranges"], jobs))


# ── Light: the catalog ─────────────────────────────────────────────────────


def fold_catalog(con, tiers: list[Path], out: Path, work: Path, *, new_gen: str, plan: dict, membership: dict, rule, jobs: int = 1,
                 merged: Callable[[], dict] | None = None) -> dict:
    """`G`'s and the runs' catalogs merged into `out/catalog` (`merge_catalogs`: the rebuild's cells and index), the
    members cut by the build's census over `out`'s new shards (`members.parquet`, `members.json`), and `meta.json` as
    the build's `catalog_meta` (its `s`: this fold's seconds). The census runs `jobs` shards at a time on cursors of
    `con` (each shard's census file depends only on its rows). `merged`: the merge's doc, when it was started earlier
    (it reads only the tiers' catalogs: `fold_gen` runs it beside the intervals and shards); else merged here."""
    t0 = monotonic()
    cat = out / "catalog"
    census = work / "census"
    shutil.rmtree(census, ignore_errors=True)
    census.mkdir(parents=True)

    def one(cur, s: dict) -> None:
        name = f"s{s['i']:04d}"
        pq.write_table(sc.census(cur, f"read_parquet({q(str(out / 'sx' / f'{name}.parquet'))})", membership["max_rows"] + 1, tag=f"_{name}"),
                       census / f"{name}.parquet", compression=CODEC)
    sn.each(con, one, plan["shards"], jobs)
    doc = merged() if merged is not None else sa.merge_catalogs(tiers, cat)
    members = sc.cut_members(census, cat / "members.parquet", gen=new_gen, max_rows=membership["max_rows"])
    (cat / "members.json").write_text(json.dumps(members, indent=1) + "\n")
    shutil.rmtree(census)
    short, long_, top = con.execute(f"""SELECT count(*) FILTER (WHERE length(q) <= 2), count(*) FILTER (WHERE length(q) > 2), max(length(q))
        FROM read_parquet({q(str(cat / 'cells.parquet'))}) WHERE bucket = ''""").fetchone()
    assembled = {"cells_rows": doc["cells_rows"], "row_groups": doc["row_groups"], "members_short": short, "members_long": long_, "max_len": top,
                 "bytes": doc["bytes"], "index_bytes": (cat / "index.parquet").stat().st_size, "cell_rg": sc.CELL_RG}
    meta = sc.catalog_meta(new_gen, assembled, round(monotonic() - t0, 1), members, rule)
    (cat / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    return meta


# ── The drill ──────────────────────────────────────────────────────────────


def _roots_sql(files: list[str]) -> str:
    """Published roots files' rows (`ROOT_SCHEMA`: no `depth`)."""
    if not files:
        return ("SELECT NULL::VARCHAR AS q, NULL::VARCHAR AS path, NULL::VARCHAR AS usr, NULL::BIGINT AS vf, NULL::BIGINT AS vt, "
                "NULL::BIGINT AS size, NULL::BIGINT AS n_files WHERE false")
    return f"SELECT q, path, usr, vf, vt, size, n_files FROM read_parquet([{', '.join(q(f) for f in files)}])"


#: A root's `depth`: its path's segments (as the suffix shards record it).
DEPTH = "len(string_split(path, '/'))::UTINYINT"


def q_spans(files: list[str]) -> list[tuple[str, str | None, str | None]]:
    """Each roots file's `(file, min q, max q)` from its footer's row-group statistics (None, None: no statistics; the
    file is read for every chunk)."""
    out = []
    for f in files:
        md = pq.ParquetFile(f).metadata
        col = md.schema.to_arrow_schema().get_field_index("q")
        lo = hi = None
        for g in range(md.num_row_groups):
            st = md.row_group(g).column(col).statistics
            if st is None or not st.has_min_max:
                lo = hi = None
                break
            lo = st.min if lo is None or st.min < lo else lo
            hi = st.max if hi is None or st.max > hi else hi
        else:
            out.append((f, lo, hi))
            continue
        out.append((f, None, None))
    return out


def overlapping(spans: list[tuple[str, str | None, str | None]], lo: str, hi: str | None) -> list[str]:
    """The files of `spans` that may hold a `q` in `[lo, hi]` (`hi` None: no upper bound)."""
    return [f for f, a, b in spans if a is None or ((hi is None or a <= hi) and b >= lo)]


def fold_drill(con, tiers: list[sd.Tier], out: Path, work: Path, *, R: int, K: int, floor: int, chunk_rows: int = sl.CHUNK_ROWS,
               jobs: int = 1, step=sl._nop_step, log: Callable[[str], None] = err, ck: Checkpoint | None = None,
               fanout: Callable[[Checkpoint], object] | None = None, meas: Path | None = None) -> dict:
    """`G'`'s `drill/` and `roots-measure/` in `out` from the tiers' drills (`G`'s, then each run's, oldest first) and
    `out/catalog/members.parquet`: each member's roots through the last scan, read from the tiers through each one's
    class map (every tier's `aliases.parquet` maps every member it knows to its canonical, which holds that class's rows
    there; a member a tier doesn't know has none there), combined; the classes by root-set digest, as the build's
    (`static_local.build_base_drill`); then `static_local.finish_drill` over the canonical members' roots and the short
    literals'.

    `meas`: `G`'s `roots-measure/`. When every run counts only new roots (`static_measure.blocker` finds nothing in the
    way), `finish_drill` only sorts each range's roots and takes the measurement and rollups from base ⊕ runs
    (specs/static-incremental-compaction.md, step 4); else it measures and rolls up every root. `log` says which, and
    the result's `measure` (`incremental` | `roots`).

    Chunked, so the working set is `chunk_rows` roots, not the corpus (gcs's first compaction ran one aggregate over all
    299 GB of roots and logged nothing for 6 h): members with equal tuples of classes (one per tier) hold equal rows, so
    the tuples (`tid`, ordered by the base's class) are cut into chunks of about `chunk_rows` rows (each tier's rows per
    class, summed, as its roots index estimates them: `static_roots.index_estimates`). The runs' rows are spilled once
    by chunk, into at most `static_roots.SPILL_PARTS` partitions of consecutive chunks (`static_roots.ChunkSpill`); the
    base's are read per chunk by its classes' `q` span (the roots files are sorted by `q`, so a chunk reads the files,
    and row groups, that span overlaps). Each chunk's combined roots and digests are exact (a tuple's rows are all in its chunk);
    the canonical members' roots are then spilled by `q` range chunk by chunk, and the short literals' by `q` range
    (planned from their roots indexes too), as the build's. The chunks and partitions never change the bytes. `log`: a
    progress line per chunk (at most one per 30 s).

    `jobs`: the chunks (long roots, canonical roots, short ranges) folded this many at a time, each on its own cursor of
    `con` (one DuckDB database: its `threads` and `memory_limit` shared, not multiplied), each writing its own files
    (gcs's 10-23 compaction ran its 4,881 chunks one after another at ~9 of 48 cores). It never changes the bytes.

    `ck`: saved after the long roots (`long-roots`: their digests and files) and after every root is spilled (`spilled`:
    the classes and the spills), so a resubmitted job resumes past the last one saved."""
    t = monotonic()
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    if ck is not None and ck.done("spilled"):
        groups = _restore(con, ck, "spilled", work)
    else:
        if ck is not None and ck.done("long-roots"):
            _restore(con, ck, "long-roots", work)
        else:
            t = _long_roots(con, tiers, out, work, chunk_rows=chunk_rows, jobs=jobs, step=step, log=log, t=t)
            _save(con, ck, "long-roots", work, tables=("dmem", "mtid", "tdig"), subs=("troots",))
        t, groups = _spill_roots(con, tiers, work, chunk_rows=chunk_rows, jobs=jobs, step=step, log=log, t=t)
        _save(con, ck, "spilled", work, tables=("al",), subs=("long", "short"), doc=groups)
    if ck is not None:
        # the spills hold every long root again: the chunks' roots are no longer needed to resume (an attempt stopped
        # right after saving the spills left them)
        ck.drop("long-roots-tables")
        ck.drop("long-roots")
    aliases = con.execute("SELECT q, canonical FROM al ORDER BY q").to_arrow_table()
    # the measurement's helper tasks fetch the spills the checkpoint just saved (specs/static-measure-fanout.md)
    fan = fanout(ck) if fanout is not None and ck is not None else None
    why = sme.blocker(tiers, meas, floor=floor, R=R) if meas is not None else "no base measurement given"
    log(f"drill measurement: {'incremental (base ⊕ runs; each range only sorted)' if why is None else f'every root ({why})'}")
    done = sl.finish_drill(con, out, work, aliases=aliases, long_groups=groups["long"], short_groups=groups["short"], R=R, K=K, floor=floor,
                           step=step, t=t, fanout=fan, incremental=(tiers, meas) if why is None else None)
    return {"members": aliases.num_rows, "canonical": len(set(aliases.column("canonical").to_pylist())), "groups": done["groups"],
            "measure": "roots" if why else "incremental"}


def _files(root: Path, *subs: str) -> list[str]:
    """The files (paths relative to `root`) of `root`'s `subs`: each a file or a directory (its files, recursively)."""
    out = []
    for sub in subs:
        p = root / sub
        out += [sub] if p.is_file() else sorted(f.relative_to(root).as_posix() for f in p.rglob("*") if f.is_file())
    return out


def _save(con, ck: Checkpoint | None, step: str, work: Path, *, tables: tuple[str, ...], subs: tuple[str, ...], doc: dict | None = None) -> None:
    """Step `step`: `tables`, then the files of `work`'s `subs` with `doc` (its marker, written last)."""
    if ck is not None:
        ck.save(con, f"{step}-tables", tables)
        ck.save_files(step, work, _files(work, *subs), doc)


def _restore(con, ck: Checkpoint, step: str, work: Path) -> dict | None:
    """`_save`'s tables and files back (into `work`); its doc."""
    ck.load(con, f"{step}-tables")
    return ck.load_files(step, work)


def _long_roots(con, tiers: list[sd.Tier], out: Path, work: Path, *, chunk_rows: int, jobs: int, step, log: Callable[[str], None], t: float) -> float:
    """`fold_drill`'s plan, runs' spill and long roots: tables `dmem`, `mtid (q, tid)`, `tdig` (each tuple's digest) and
    `work/troots/c<chunk>.parquet` (each chunk's combined roots)."""
    n = len(tiers)
    con.execute(f"CREATE OR REPLACE TABLE dmem AS SELECT q FROM read_parquet({q(str(out / 'catalog' / 'members.parquet'))})")
    for i, tr in enumerate(tiers):
        con.register(f"al_in{i}", tr.alias_table())
        con.execute(f"CREATE OR REPLACE TABLE ta{i} AS SELECT q, canonical FROM al_in{i}")
        con.unregister(f"al_in{i}")
    cs = ", ".join(f"ta{i}.canonical AS c{i}" for i in range(n))
    joins = " ".join(f"LEFT JOIN ta{i} USING (q)" for i in range(n))
    # a member's tuple of classes, one per tier: members with equal tuples hold equal rows
    con.execute(f"CREATE OR REPLACE TABLE mt AS SELECT m.q, {cs} FROM dmem AS m {joins}")
    con.execute(f"""CREATE OR REPLACE TABLE tup AS SELECT row_number() OVER (ORDER BY {', '.join(f'c{i} NULLS FIRST' for i in range(n))}) AS tid, *
        FROM (SELECT DISTINCT {', '.join(f'c{i}' for i in range(n))} FROM mt)""")
    on = " AND ".join(f"t.c{i} IS NOT DISTINCT FROM mt.c{i}" for i in range(n))
    con.execute(f"CREATE OR REPLACE TABLE mtid AS SELECT mt.q, t.tid FROM mt JOIN tup AS t ON {on}")
    long_files = [tr.files("long", "roots") for tr in tiers]
    # each tier's rows per class, estimated from its roots index (`static_roots.index_estimates`): chunk sizes only
    for i, tr in enumerate(tiers):
        sr.index_estimates(con, [tr.local("long-roots-index.parquet")] if long_files[i] else [], f"SELECT canonical AS q FROM ta{i}", f"tc{i}")
    est = " + ".join(f"coalesce(tc{i}.n, 0)" for i in range(n))
    cjoin = " ".join(f"LEFT JOIN tc{i} ON tc{i}.q = t.c{i}" for i in range(n))
    con.execute(f"""CREATE OR REPLACE TABLE tch AS SELECT tid, (dense_rank() OVER (ORDER BY ch) - 1)::BIGINT AS ch FROM (
        SELECT tid, ((sum(est) OVER (ORDER BY tid ROWS UNBOUNDED PRECEDING) - est) // {chunk_rows})::BIGINT AS ch
        FROM (SELECT t.tid, {est} AS est FROM tup AS t {cjoin}))""")
    chunks = con.execute("""SELECT ch, min(tid), max(tid), min(t.c0), max(t.c0) FROM tch JOIN tup AS t USING (tid)
        GROUP BY ch ORDER BY ch""").fetchall()
    t = step("drill/fold/plan", t, tuples=sl.count(con, "tup"), chunks=len(chunks))
    runs = sr.ChunkSpill(con, work / "runs", len(chunks))
    spills = [i for i in range(1, n) if long_files[i]]
    k = max(1, min(jobs, len(spills)))
    # a partitioned write keeps per-thread, per-partition buffers outside `memory_limit` (gcs 10-08c: RSS past `-M` here),
    # so the runs written at once split its flush threshold (a connection's own setting) between them
    flush = con.execute("SELECT current_setting('partitioned_write_flush_threshold')").fetchone()[0]

    def spill(cur, i: int) -> tuple[int, None]:
        cur.execute(f"SET partitioned_write_flush_threshold = {max(1, int(flush) // k)}")
        runs.write(f"""SELECT c.ch, t.tid, r.path, r.usr, r.vf, r.vt, r.size, r.n_files FROM ({_roots_sql(long_files[i])}) AS r
            JOIN tup AS t ON t.c{i} = r.q JOIN tch AS c USING (tid)""", f"t{i:03d}", con=cur)
        return 0, None

    _pool(con, spill, spills, k, sn.Progress("drill/fold/runs-spill", len(spills), log))
    t = step("drill/fold/runs-spill", t, runs=n - 1, parts=runs.p)
    spans = q_spans(long_files[0])
    troots = work / "troots"
    troots.mkdir()
    prog = sn.Progress("drill/fold/long-roots", len(chunks), log)
    spilled: dict[int, list[Path]] = {}

    def chunk(cur, c: tuple) -> tuple[int, pa.Table | None]:
        """Chunk `c`'s combined roots → `troots/c<ch>.parquet`; its rows and its tuples' digests (by `tid`)."""
        ch, a, b, lo, hi = c
        parts = []
        base = overlapping(spans, lo, hi) if lo is not None else []
        if base:
            parts.append(f"""SELECT t.tid, r.path, r.usr, r.vf, r.vt, r.size, r.n_files FROM ({_roots_sql(base)}) AS r
                JOIN tup AS t ON t.c0 = r.q WHERE r.q BETWEEN {q(lo)} AND {q(hi)} AND t.tid BETWEEN {a} AND {b}""")
        if spilled[ch]:
            parts.append(f"""SELECT tid, path, usr, vf, vt, size, n_files
                FROM read_parquet([{', '.join(q(str(f)) for f in spilled[ch])}], hive_partitioning = false)""")
        if not parts:
            return 0, None
        cur.execute(f"""CREATE OR REPLACE TEMP TABLE ct AS SELECT tid, path, usr, vf, min(vt) AS vt, any_value(size) AS size,
            any_value(n_files) AS n_files FROM ({' UNION ALL '.join(parts)}) GROUP BY tid, path, usr, vf""")
        dig = cur.execute(f"""SELECT tid, count(*)::BIGINT AS n, (sum({ROW_HASH}) % {U64})::UBIGINT AS h1,
            (sum(hash({ROW_HASH}, 'x')) % {U64})::UBIGINT AS h2 FROM ct GROUP BY tid ORDER BY tid""").to_arrow_table()
        rows = sl.count(cur, "ct")
        if rows:
            cur.execute(f"COPY ct TO {q(str(troots / f'c{ch:06d}.parquet'))} (FORMAT parquet, COMPRESSION zstd)")
        cur.execute("DROP TABLE ct")
        return rows, dig

    # the chunks run `jobs` at a time, whole runs' spill partitions at once (each split by chunk first, removed after)
    digs: list[pa.Table] = []
    for parts in _part_batches(runs, jobs):
        for p in parts:
            spilled.update(runs.prepare(p))
        digs += [d for d in _pool(con, chunk, [chunks[k] for p in parts for k in runs.chunks(p)], jobs, prog) if d is not None]
        for p in parts:
            runs.release(p)
        spilled.clear()
    runs.close()
    con.execute("CREATE OR REPLACE TABLE tdig (tid BIGINT, n BIGINT, h1 UBIGINT, h2 UBIGINT)")
    if digs:
        con.register("tdig_in", pa.concat_tables(digs))
        con.execute("INSERT INTO tdig SELECT tid, n, h1, h2 FROM tdig_in")
        con.unregister("tdig_in")
    return step("drill/fold/long-roots", t, tuples=sl.count(con, "tup"), rows=prog.rows, chunks=len(chunks))


def _part_batches(spill: sr.ChunkSpill, jobs: int) -> list[list[int]]:
    """`spill`'s partitions in order, in consecutive batches of at least `4 × jobs` chunks (the last: what remains), so
    a pool of `jobs` stays busy across partitions of one chunk each and only a batch's partitions are held at once."""
    out: list[list[int]] = [[]]
    for p in range(spill.p):
        if sum(len(spill.chunks(x)) for x in out[-1]) >= 4 * jobs:
            out.append([])
        out[-1].append(p)
    return out


def _pool(con, fn: Callable[..., tuple[int, object]], items: list, jobs: int, prog: sn.Progress) -> list:
    """`fn(cursor, item)` → `(rows, result)` for every item, `jobs` at a time, each on its own cursor of `con`
    (`static_names.each`: one `threads` pool and one `memory_limit` across them, its own temp tables), `prog` ticked as
    each ends; the results in `items`' order. Each item writes its own files, so the outputs never depend on `jobs`."""
    lock = Lock()

    def one(cur, item):
        cur.execute("SET preserve_insertion_order=false")  # a connection's own setting, as `connect` sets it
        rows, res = fn(cur, item)
        with lock:
            prog.finish(rows)  # its count, not this call's: the long roots run one `_pool` per batch of spill parts
        return res

    return sn.each(con, one, items, jobs)


def _spill_roots(con, tiers: list[sd.Tier], work: Path, *, chunk_rows: int, jobs: int, step, log: Callable[[str], None], t: float) -> tuple[float, dict]:
    """`fold_drill`'s classes (table `al (q, canonical)`, by root-set digest) and every root spilled by `q` range: the
    canonical members' long roots (`work/long/`) and the short literals' (`work/short/`); the ranges per kind."""
    n, troots = len(tiers), work / "troots"
    con.execute("CREATE OR REPLACE TABLE dgq AS SELECT m.q, d.n, d.h1, d.h2 FROM mtid AS m JOIN tdig AS d USING (tid)")
    con.execute("CREATE OR REPLACE TABLE al AS SELECT q, min(q) OVER (PARTITION BY n, h1, h2) AS canonical FROM dgq")
    missing = con.execute("SELECT count(*) FROM dmem ANTI JOIN al USING (q)").fetchone()[0]
    if missing:
        raise RuntimeError(f"{missing} members without roots in any tier: the runs' drills don't cover the folded catalog's members")
    aliases = con.execute("SELECT q, canonical FROM al ORDER BY q").to_arrow_table()
    con.execute("CREATE OR REPLACE TABLE canon AS SELECT DISTINCT canonical AS q FROM al")
    con.execute("CREATE OR REPLACE TABLE ctid AS SELECT c.q, m.tid FROM canon AS c JOIN mtid AS m USING (q)")
    t = step("drill/fold/aliases", t, members=aliases.num_rows, canonical=sl.count(con, "canon"))
    long_groups = sl.plan_groups(con, con.execute("SELECT c.q, d.n FROM ctid AS c JOIN tdig AS d USING (tid) ORDER BY c.q").fetchall(), chunk_rows)
    files = sorted(troots.glob("*.parquet"))
    prog = sn.Progress("drill/fold/canonical-roots", len(files), log)

    def canonical(cur, kf: tuple[int, Path]) -> tuple[int, None]:
        k, f = kf
        cur.execute(f"""CREATE OR REPLACE TEMP TABLE rt AS SELECT c.q, {DEPTH} AS depth, r.path, r.usr, r.vf, r.vt, r.size, r.n_files
            FROM ctid AS c JOIN read_parquet({q(str(f))}) AS r USING (tid)""")
        rows = sl.count(cur, "rt")
        if rows:
            sl.spill_roots(cur, "rt", work / "long" / f"fold{k:06d}")
        cur.execute("DROP TABLE rt")
        return rows, None

    _pool(con, canonical, list(enumerate(files)), jobs, prog)
    n_long = prog.rows
    shutil.rmtree(troots)
    # a resumed fold (`long-roots` restored) never made `mt`, `tup`, `tch` or the per-tier tables
    for tb in ("rt", "tdig", "ctid", "mt", "mtid", "tup", "tch", *(f"t{x}{i}" for i in range(n) for x in "ac")):
        con.execute(f"DROP TABLE IF EXISTS {tb}")
    t = step("drill/fold/canonical-roots", t, lcan=n_long, groups=long_groups)
    short_files = [tr.files("short", "roots") for tr in tiers]
    hb.set_stage("drill/fold/short-roots: plan")
    # the q ranges planned from the tiers' roots indexes (the literals: every group's first and last `q`), not counted
    idx = [tr.local("short-roots-index.parquet") for tr, fs in zip(tiers, short_files) if fs]
    keys = " UNION ".join(f"SELECT q_min AS q FROM read_parquet({q(f)}) UNION SELECT q_max FROM read_parquet({q(f)})" for f in idx)
    sr.index_estimates(con, idx, keys, "sest")
    counts = con.execute("SELECT q, n FROM sest ORDER BY q").fetchall()
    con.execute("DROP TABLE sest")
    bounds = sl.q_bounds(counts, chunk_rows)
    short_groups = sl.plan_groups(con, counts, chunk_rows)
    short_spans = [q_spans(fs) for fs in short_files]
    prog = sn.Progress("drill/fold/short-roots", len(bounds), log)

    def short(cur, g: int) -> tuple[int, None]:
        lo = bounds[g]
        hi = bounds[g + 1] if g + 1 < len(bounds) else None
        where = f"q >= {q(lo)}" + (f" AND q < {q(hi)}" if hi is not None else "")
        parts = [f"SELECT * FROM ({_roots_sql(fs)}) WHERE {where}" for sp in short_spans if (fs := overlapping(sp, lo, hi))]
        if not parts:
            return 0, None
        cur.execute(f"""CREATE OR REPLACE TEMP TABLE rt AS SELECT q, {DEPTH} AS depth, path, usr, vf, min(vt) AS vt, any_value(size) AS size,
                any_value(n_files) AS n_files FROM ({' UNION ALL '.join(parts)}) GROUP BY q, path, usr, vf""")
        rows = sl.count(cur, "rt")
        if rows:
            sl.spill_roots(cur, "rt", work / "short" / f"fold{g:06d}")
        cur.execute("DROP TABLE rt")
        return rows, None

    _pool(con, short, list(range(len(bounds))), jobs, prog)
    n_short = prog.rows
    return step("drill/fold/short-roots", t, sall=n_short, groups=short_groups), {"long": long_groups, "short": short_groups}


ROW_HASH = "hash(path, usr, vf, vt, size, n_files)"


# ── The whole fold ─────────────────────────────────────────────────────────


def run_scans(gen_dir: Path, gen: str, runs: list[dict]) -> list[dict]:
    """The `scans.json` entries of every scan the runs hold (`deltas/<id>/scans.json`, kept on folded runs)."""
    out = []
    for r in runs:
        for sid in r["scans"]:
            p = gen_dir / ar.run_key(sid, sid) / "scans.json"
            if not p.is_file():
                raise SystemExit(f"{gen}: {p.relative_to(gen_dir)} missing: scan {sid}'s entry for the compacted `scans.json`")
            out.append(json.loads(p.read_text())["scans"][0])
    return out


def fold_gen(gen_dir: Path, out: Path, work: Path, *, gen: str, new_gen: str, runs: list[dict], threads: int = 4, mem: str = "4GB",
             chunk_rows: int = sl.CHUNK_ROWS, jobs: int = 1, log: Callable[[str], None] = err, progress: Callable[..., None] | None = None,
             ck: Checkpoint | None = None, fanout: Callable[[Checkpoint], object] | None = None) -> dict:
    """Generation `gen` (its base in `gen_dir`, the runs `runs`: the newest manifest's, oldest first) folded into `new_gen`
    in `out` (every part but `compaction.json`); its `manifests/<last>.json` lists no runs. Returns a summary (rows, and
    per part its seconds, peak work-disk GiB and peak RSS GiB). `progress(note)`: the run record's hook
    (`append_runner.compact_store`), the parts done so far as each ends.

    `ck` (`compact_checkpoint`): each part's files in `out` saved as it ends (the drill's also within it, `fold_drill`),
    so a resubmitted job restores the parts saved and folds from the first that wasn't (gcs's 10-08c compaction ran 16 h
    and was killed in its drill's write with nothing kept). Each save runs beside the next part (in part order, one at a
    time; the drill, which saves its own steps, starts once the saves before it ended).

    `jobs`: key ranges (`fold_ranges`) and census shards (`fold_catalog`) at a time, on cursors of one DuckDB database
    (`threads` and `mem` shared). The catalogs' merge needs no other part and runs beside the intervals and shards. The
    shards need every range's histogram (their plan), the census the shards, the drill the members: those stay in order."""
    with ThreadPoolExecutor(2, thread_name_prefix="compact") as bg:
        return _fold_gen(gen_dir, out, work, gen=gen, new_gen=new_gen, runs=runs, threads=threads, mem=mem, chunk_rows=chunk_rows, jobs=jobs,
                         log=log, progress=progress, ck=ck, bg=bg, fanout=fanout)


def _fold_gen(gen_dir: Path, out: Path, work: Path, *, gen: str, new_gen: str, runs: list[dict], threads: int, mem: str, chunk_rows: int, jobs: int,
              log: Callable[[str], None], progress: Callable[..., None] | None, ck: Checkpoint | None, bg: ThreadPoolExecutor,
              fanout: Callable[[Checkpoint], object] | None = None) -> dict:
    """`fold_gen`, its background work (the catalogs' merge, the parts' saves) on `bg` (waited on as `fold_gen` returns or
    fails)."""
    t0, times, peaks = monotonic(), {}, {}
    if not runs:
        raise SystemExit(f"{gen}: no runs to compact")
    base = json.loads((gen_dir / "scans.json").read_text())
    rule = sn.gen_rule(base)
    run_dirs = [gen_dir / r["key"] for r in runs]
    days = [s for r in runs for s in r["scans"]]
    lack = [r["key"] for r in runs if not (gen_dir / r["key"] / "drill" / "meta.json").is_file()]
    if not (gen_dir / "drill" / "meta.json").is_file() or lack:
        raise SystemExit(f"{gen}: compaction needs every tier's drill/ (the base's{' and ' + ', '.join(lack) if lack else ''})")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    work.mkdir(parents=True, exist_ok=True)
    disk = sl.DiskPeak(work)
    steps: dict[str, float] = {}

    def done(part: str, t: float) -> float:
        times[part] = round(monotonic() - t, 1)
        peaks[part] = {"disk_gb": disk.take(), "rss_gb": sl.rss_peak_gb()}
        log(f"compact {gen} → {new_gen}: {part} in {times[part]}s (work disk peak {peaks[part]['disk_gb']} GiB, RSS peak {peaks[part]['rss_gb']} GiB)")
        hb.after(f"compact/{part}")
        if progress is not None:
            progress("; ".join(f"{p} {s}s (disk {peaks[p]['disk_gb']} GiB, RSS {peaks[p]['rss_gb']} GiB)" for p, s in times.items()))
        return monotonic()

    def step(name: str, t: float, **counts: int) -> float:
        steps[name] = round(monotonic() - t, 1)
        n = "".join(f", {k} {v:,}" for k, v in counts.items())
        log(f"compact {gen} → {new_gen}: {name} in {steps[name]}s (work disk peak {disk.take_step()} GiB, RSS peak {sl.rss_peak_gb()} GiB{n})")
        hb.after(name)
        return monotonic()

    # In the background: the catalogs' merge (it reads only the tiers' catalogs) and each part's checkpoint save, one at a
    # time and in part order (a part's save overlaps the next part's work; it ends before the next part's save starts).
    saving: list[Future] = []

    def saved() -> None:
        while saving:
            saving.pop().result()

    def resumed(part: str, subs: tuple[str, ...], run: Callable[[], dict], *, after_saves: bool = False) -> dict:
        """`run()`'s doc, its files (`out`'s `subs`) saved as `part` in the background once the previous part's save
        ended; or, saved by an earlier attempt, restored. `after_saves`: run only once the saves before it ended (a part
        with checkpoints of its own, which would otherwise land before the previous part's marker)."""
        if ck is not None and ck.done(part):
            return ck.load_files(part, out)
        if after_saves:
            saved()
        doc = run()
        saved()
        if ck is not None:
            saving.append(bg.submit(ck.save_files, part, out, _files(out, *subs), doc))
        return doc

    con = sn.connect(threads, mem, work / "duckdb")
    # every input is read about once (a chunk's base row groups, each run's spill): DuckDB's cache of the parquet bytes it
    # read only filled the buffer pool to `-M` (gcs's 10-08c plan step: 174 GiB of a read-only scan)
    con.execute("SET enable_external_file_cache=false")
    doc = dict(base)
    doc["scans"] = [*base["scans"], *run_scans(gen_dir, gen, runs)]
    (out / "scans.json").write_text(json.dumps(doc, indent=1) + "\n")
    shutil.copyfile(gen_dir / "ranges.json", out / "ranges.json")
    ranges = json.loads((out / "ranges.json").read_text())
    merging: Future | None = None
    if ck is None or not ck.done("catalog"):
        def merge() -> dict:
            tm = monotonic()
            m = sa.merge_catalogs([gen_dir / "catalog", *(d / "catalog" for d in run_dirs)], out / "catalog")
            log(f"compact {gen} → {new_gen}: catalog merge in {monotonic() - tm:.1f}s ({m['cells_rows']:,} cells; beside the intervals and shards)")
            return m
        merging = bg.submit(merge)
    t = monotonic()
    versions = resumed("intervals", ("cintervals", "chist"), lambda: {"versions": fold_ranges(con, gen_dir, days, ranges, out, rule, jobs)})["versions"]
    t = done("intervals", t)

    def shards() -> dict:
        old = json.loads((gen_dir / "shards.json").read_text())
        plan = sn.plan_shards(sorted((out / "chist").glob("*.parquet")), old["target_rows"], len(old["tasks"]))
        sa.merge_shards([gen_dir, *run_dirs], out, plan=plan)
        (out / "shards.json").write_text(json.dumps(plan, indent=1) + "\n")
        side = sn.concat_sidecars([pq.read_table(f) for f in sorted((out / "sidecar").glob("*.parquet"))])
        sn.write_sidecar(side, out / "sidecar.parquet")
        return {"suffix_rows": int(sum(side.column("rows").to_pylist()))}
    suffix_rows = resumed("shards", ("sx", "sidecar", "sidecar.parquet", "shards.json"), shards)["suffix_rows"]
    plan = json.loads((out / "shards.json").read_text())
    t = done("shards", t)
    membership = json.loads((gen_dir / "catalog" / "meta.json").read_text())["membership"]
    cat = resumed("catalog", ("catalog",), lambda: fold_catalog(con, [gen_dir / "catalog", *(d / "catalog" for d in run_dirs)], out, work,
                                                                new_gen=new_gen, plan=plan, membership=membership, rule=rule, jobs=jobs,
                                                                merged=merging.result if merging is not None else None))
    t = done("catalog", t)
    dm = json.loads((gen_dir / "drill" / "meta.json").read_text())
    tiers = [sd.Tier(gen_dir / "drill", "base"), *(sd.Tier(d / "drill", r["key"]) for d, r in zip(run_dirs, runs))]
    drill = resumed("drill", ("drill", "roots-measure"), lambda: fold_drill(
        con, tiers, out, work / "drill", R=dm["R"], K=dm["K"], floor=dm.get("floor", sl.FLOOR), chunk_rows=chunk_rows, jobs=jobs, step=step,
        log=lambda s: log(f"compact {gen} → {new_gen}: {s}"), ck=ck.sub("fold") if ck is not None else None, fanout=fanout,
        meas=gen_dir / "roots-measure"), after_saves=True)
    t = done("drill", t)
    anchors = None
    if (gen_dir / an.ANCHORS / "meta.json").is_file():
        am = json.loads((gen_dir / an.ANCHORS / "meta.json").read_text())
        anchors = resumed("anchors", (an.NAMES, an.ANCHORS),
                          lambda: sl.build_base_anchors(con, out, R=am["R"], K=am["K"], scans=[s["id"] for s in doc["scans"]], rule=rule))
        t = done("anchors", t)
    (out / "manifests").mkdir()
    ids = [s["id"] for s in doc["scans"]]
    (out / "manifests" / f"{ids[-1]}.json").write_text(json.dumps(sa.manifest(new_gen, ids, []), indent=1) + "\n")
    saved()
    disk.close()
    return {"from": gen, "gen": new_gen, "runs": [r["key"] for r in runs], "base_scans": len(base["scans"]), "scans": len(ids),
            "versions": versions, "suffix_rows": suffix_rows, "shards": len(plan["shards"]),
            "members": drill["members"], "cells_rows": cat["cells_rows"], "drill": drill, "anchors": anchors, "s": round(monotonic() - t0, 1),
            "stages": times, "steps": steps, "peaks": peaks}


def fold(gen_dir: Path, out: Path, work: Path, *, gen: str, new_gen: str, runs: list[dict], threads: int = 4, mem: str = "4GB",
         chunk_rows: int = sl.CHUNK_ROWS, jobs: int = 1, progress: Callable[..., None] | None = None, checkpoint: str | None = None,
         fanout: Callable[[Checkpoint], object] | None = None) -> dict:
    """`append_runner.Compactor.fold` for the static store (`fold_gen`; `jobs`: key ranges, census shards and drill-fold chunks at a time).
    `checkpoint`: a store root (the scratch bucket) for `compact_checkpoint`; `fanout`: the drill measurement's helper
    tasks, given the fold's checkpoint (`static_fanout.Fanout`; needs `checkpoint`)."""
    ck = compact_checkpoint(checkpoint, gen, new_gen, runs, work=work / "ckpt") if checkpoint else None
    try:
        return fold_gen(gen_dir, out, work, gen=gen, new_gen=new_gen, runs=runs, threads=threads, mem=mem, chunk_rows=chunk_rows, jobs=jobs,
                        progress=progress, ck=ck, fanout=fanout)
    finally:
        if ck is not None:
            ck.close()


#: Under a checkpoint root: the folds' checkpoints, `<new generation>/<inputs>/` (`compact_checkpoint`).
CHECKPOINTS = f"{PREFIX}/compact-checkpoints"


def compact_checkpoint(root: str, gen: str, new_gen: str, runs: list[dict], **kw) -> Checkpoint:
    """The fold of `gen` and `runs` into `new_gen`'s checkpoint under `root`: keyed by its inputs (a later compaction
    into the same `new_gen` folds more runs) and, as every `Checkpoint`, by the code (another build's isn't resumed)."""
    from .blobstore import store_url

    inputs = hashlib.sha256(json.dumps([gen, [r["key"] for r in runs]]).encode()).hexdigest()[:16]
    return Checkpoint(store_url(root), f"{CHECKPOINTS}/{new_gen}/{inputs}", code=code_version(), **kw)


def clear_checkpoints(root: str, new_gen: str) -> int:
    """Every fold checkpoint into `new_gen` under `root` (its compaction published, or superseded); the files deleted."""
    from .blobstore import Store, store_url

    store = Store(store_url(root))
    return store.delete(sorted(store.listing(f"{CHECKPOINTS}/{new_gen}")))


def compactor(chunk_rows: int = sl.CHUNK_ROWS, checkpoint: str | None = None, fanout: Callable[[Checkpoint], object] | None = None) -> ar.Compactor:
    """The static store's plug into the shared compaction (`append_runner.compact_store`): its fold and upload order; its
    predecessors are left to `store-gc -k` (readers pin `STATIC_GEN`, so a compacted generation may still be served).
    `checkpoint`: the folds' checkpoint root (`fold`); `fanout`: its drill measurement's helper tasks (`fold`)."""
    return ar.Compactor(prefix=PREFIX, fold=lambda *a, **kw: fold(*a, **kw, chunk_rows=chunk_rows, checkpoint=checkpoint, fanout=fanout),
                        publish_order=sl.publish_order,
                        last=("scans.json", "catalog/meta.json", "drill/meta.json", f"{an.ANCHORS}/meta.json"), prune=False, md5s=True, keep=reads,
                        publish_tier=sl.publish_tier)


#: What the fold never reads of the base: the anchored search and the measurement (rebuilt), the parity markers.
UNREAD = (f"{an.NAMES}/", "roots-measure/", "verify/")


def reads(rel: str) -> bool:
    """Whether the fold reads generation-relative `rel` (`Compactor.keep`): all but `UNREAD` and the base's `anchors/`
    (whose `meta.json` says the base has anchored search)."""
    return not rel.startswith(UNREAD) and (not rel.startswith(f"{an.ANCHORS}/") or rel == f"{an.ANCHORS}/meta.json")


# ── CLI (one machine) ──────────────────────────────────────────────────────


@command("compact")
@option("-c", "--chunk-rows", default=sl.CHUNK_ROWS, type=IntRange(min=1), help="The drill's chunks: roots per measurement / rollup q range")
@option("-g", "--gen", required=True, help="The generation (`<store>/static-names/GEN/`); one compacted already is followed to its newest successor")
@option("-j", "--jobs", default=1, type=IntRange(min=1), help="Key ranges, census shards and drill-fold chunks folded this many at a time, on cursors of one DuckDB database (-p threads and -M memory shared); the bytes don't depend on it")
@option("-M", "--mem", default="4GB", help="DuckDB memory limit")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`)")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after); the generation is mirrored into it")
def compact_cmd(chunk_rows, gen, jobs, mem, store_root, threads, work) -> None:
    """Fold the generation's base and its newest manifest's runs into a new generation now (`<gen>.c<NNN>`, equal to
    `build -r <gen>/ranges.json` through the last scan), whatever the compaction policy says; published (data, then
    `compaction.json`), then `successor.json` in the old generation and `static-names/CURRENT.json` when it named it (or
    nothing). The old generation stays (`store-gc -k` collects it). Prints a JSON summary."""
    from .blobstore import Store, mirror

    sl._check_gen(gen)
    store = Store(store_root)
    gen = ar.resolve_gen(store, PREFIX, gen)
    prefix = f"{PREFIX}/{gen}"
    with sl.workdir(work) as w:
        gen_dir = w / "store" / prefix
        mirror(store, prefix, gen_dir)
        why = ar.forced(gen_dir, "forced (compact)")
        if why is None:
            raise SystemExit(f"{store.root}/{prefix}: no runs to compact")
        out = ar.compact_store(store, compactor(chunk_rows), gen, gen_dir, w, policy=ar.NEVER, why=why, threads=threads, mem=mem, jobs=jobs)
    print(json.dumps(out, indent=1))
