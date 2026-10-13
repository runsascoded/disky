"""Match roots of the heavy name terms (specs/architecture/static-name-search.md, "Drilldown"): for every
catalog member — the literals whose suffix range exceeds V rows, and every one- and two-character literal —
its first-hit rows themselves, so a filtered treemap / table / diff at any path P is computed from the
roots under P.

A **match root** of `q` is a first-hit row: a version (one owner slice of one path, live on `[vf, vt)`) at
depth ≥ 1 whose lowercase name contains `q` and whose lowercase parent path does not. The filtered view
at P on date D is the roots under P live on D, summed by child of P. (If P's own lowercase path contains
`q`, the whole subtree is covered by a root at or above P: the filtered view is the plain one.)

Long members' roots come from the suffix shards, level by level as the catalog's answers
(`static_catalog.member_events`), keeping the rows instead of summing them; short literals' from the
coalesced versions (`static_catalog.short_events`' rule). `measure` / `measure-short` count them:
per member its root rows and distinct root paths, and per directory the root rows under it
(bottom-up, one level at a time: a directory's totals are its children's).
"""
from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep, time
from typing import Callable

import pyarrow as pa
import pyarrow.compute  # noqa: F401  (pa.compute)
import pyarrow.parquet as pq
from click import IntRange, argument, group, option

from .hex_runs import HexRule, grams_sql, occurs_sql
from .static_catalog import CHUNK_ROWS, PARENT, first_hit_sql
from .static_profile import data_bucket, scratch_bucket
from .static_names import (
    _batches, CODEC, NAME, OPEN, PREFIX, _task, connect, err, gen_rule_at, q, read_json,
)

#: The parent of a path `x` (raw case), by string cut: every directory level, newline or not.
def raw_parent(x: str) -> str:
    return f"left({x}, length({x}) - length(string_split({x}, '/')[-1]) - 1)"


ROOT_COLS = "q VARCHAR, depth UTINYINT, path VARCHAR, usr VARCHAR, vf BIGINT, vt BIGINT, size BIGINT, n_files BIGINT"
#: Per (q, root path): its rows (versions × owner slices) and how many are open (live on the newest scan).
RP_COLS = "q VARCHAR, depth UTINYINT, path VARCHAR, n BIGINT, n_open BIGINT"


def sx_rows_sql(src: str) -> str:
    """Suffix-shard rows with the owner slice, epoch seconds."""
    return f"SELECT s, depth, path, usr, epoch(vf)::BIGINT AS vf, epoch(vt)::BIGINT AS vt, size, n_files FROM {src}"


#: Per member: its roots' count and two order-free sums of row hashes (equal sets → equal digests).
DIGEST_COLS = "q VARCHAR, n BIGINT, h1 HUGEINT, h2 HUGEINT"
ROW_HASH = "hash(path, usr, vf, vt, size, n_files)"


def _sink(con, into: str, hit: str, agg: bool | str) -> None:
    if agg == "digest":
        con.execute(f"""INSERT INTO {into} SELECT q, count(*), sum({ROW_HASH})::HUGEINT, sum(hash({ROW_HASH}, 'x'))::HUGEINT
            FROM ({hit}) GROUP BY q""")
    elif agg:
        con.execute(f"""INSERT INTO {into} SELECT q, depth, path, count(*), count(*) FILTER (WHERE vt = {OPEN})
            FROM ({hit}) GROUP BY q, depth, path""")
    else:
        con.execute(f"INSERT INTO {into} SELECT q, depth, path, usr, vf, vt, size, n_files FROM ({hit})")


def member_roots(con, rows_sql: str, members: str, into: str, agg: bool | str = False, rule: HexRule | None = None) -> None:
    """Add `members`' (a table with `q`, ≥ 3 characters) first-hit rows among `rows_sql`'s suffix rows
    `(s, depth, path, usr, vf, vt, size, n_files)` to table `into` (created if absent): `ROOT_COLS`, or with
    `agg` per `(q, depth, path)` `RP_COLS` (or per chunk and member `DIGEST_COLS` partials, `agg="digest"`). The rule and level loop are `static_catalog.member_events`': at
    length L the rows whose prefix of length L is a prefix of a member are carried; a row is a hit of
    `q = left(s, L)` when `q` is a member, `s` starts at `q`'s first occurrence in the name, and the parent
    does not contain `q`. A `(q, path)`'s rows all share one suffix (its first occurrence), so feeding a
    shard in `(s, path)`-cut chunks never splits one: the aggregate is exact per chunk."""
    con.execute(f"CREATE TABLE IF NOT EXISTS {into} ({DIGEST_COLS if agg == 'digest' else RP_COLS if agg else ROOT_COLS})")
    top = con.execute(f"SELECT max(length(q)) FROM {members}").fetchone()[0]
    if not top:
        return
    con.execute("DROP TABLE IF EXISTS rx; DROP TABLE IF EXISTS rpre")
    con.execute(f"""CREATE TABLE rpre AS SELECT DISTINCT left(q, L) AS p, L, max(length(q) = L) OVER (PARTITION BY left(q, L)) AS member
        FROM (SELECT q, unnest(range(3, length(q) + 1)) AS L FROM {members})""")
    con.execute(f"""CREATE TABLE rx AS SELECT s, depth, path, usr, vf, vt, size, n_files, {NAME} AS l, {PARENT} AS par
        FROM ({rows_sql}) WHERE depth >= 1""")
    for L in range(3, top + 1):
        con.execute("DROP TABLE IF EXISTS rl")
        con.execute(f"CREATE TABLE rl AS SELECT p, bool_or(member) AS member FROM rpre WHERE L = {L} GROUP BY p")
        con.execute(f"CREATE OR REPLACE TABLE rx AS SELECT rx.* FROM rx SEMI JOIN rl ON left(rx.s, {L}) = rl.p")
        if con.execute("SELECT count(*) FROM rx").fetchone()[0] == 0:
            break
        hit = f"""SELECT left(s, {L}) AS q, depth, path, usr, vf, vt, size, n_files FROM rx
            SEMI JOIN (SELECT p FROM rl WHERE member) AS mm ON left(rx.s, {L}) = mm.p
            WHERE {first_hit_sql(L, rule)}"""
        _sink(con, into, hit, agg)
        con.execute(f"DELETE FROM rx WHERE length(s) <= {L}")
    con.execute("DROP TABLE IF EXISTS rx; DROP TABLE IF EXISTS rl; DROP TABLE IF EXISTS rpre")


def short_roots(con, versions_sql: str, into: str, agg: bool = False, pieces: int = 1, log: str = "", rule: HexRule | None = None) -> None:
    """Add every one- and two-character literal's first-hit rows among `versions_sql`'s rows `(depth, path,
    usr, vf, vt, size, n_files)` to `into` (as `member_roots`): each distinct character and character pair of
    the lowercase name that the lowercase parent does not contain (`static_catalog.short_events`' rule). In
    `pieces` passes by `hash(path)` (bounding the unnest's memory; a path's rows are in one piece)."""
    if pieces > 1:
        t0 = monotonic()
        for k in range(pieces):
            short_roots(con, f"SELECT * FROM ({versions_sql}) WHERE hash(path) % {pieces} = {k}", into, agg, rule=rule)
            if log:
                err(f"{log}: piece {k + 1}/{pieces} in {monotonic() - t0:.1f}s")
        return
    con.execute(f"CREATE TABLE IF NOT EXISTS {into} ({RP_COLS if agg else ROOT_COLS})")
    hit = f"""SELECT g AS q, depth, path, usr, vf, vt, size, n_files FROM (
            SELECT unnest({grams_sql('l', rule)}) AS g, par, depth, path, usr, vf, vt, size, n_files
            FROM (SELECT {NAME} AS l, {PARENT} AS par, depth, path, usr, vf, vt, size, n_files FROM ({versions_sql}) WHERE depth >= 1)
        ) WHERE NOT {occurs_sql('par', 'g', rule)}"""
    _sink(con, into, hit, agg)


# ── Measuring ──────────────────────────────────────────────────────────────


def q_stats(con, rp: str) -> tuple[pa.Table, pa.Table]:
    """Per member: root rows, distinct root paths, open rows and paths, bucket (depth-1) roots, depth range;
    and per (member, depth) the root paths and rows there."""
    qs = con.execute(f"""SELECT q, sum(n)::BIGINT AS rows, count(*)::BIGINT AS paths, sum(n_open)::BIGINT AS open_rows,
            count(*) FILTER (WHERE n_open > 0)::BIGINT AS open_paths, count(*) FILTER (WHERE depth = 1)::BIGINT AS d1_paths,
            min(depth)::INTEGER AS min_depth, max(depth)::INTEGER AS max_depth
        FROM {rp} GROUP BY q ORDER BY q""").to_arrow_table()
    qd = con.execute(f"""SELECT q, depth::INTEGER AS depth, count(*)::BIGINT AS paths, sum(n)::BIGINT AS rows
        FROM {rp} GROUP BY q, depth ORDER BY q, depth""").to_arrow_table()
    return qs, qd


def dir_stats(con, rp: str, floor: int, partial_top: bool = False) -> tuple[pa.Table, pa.Table, pa.Table | None]:
    """Per (member, directory) the roots strictly under it, bottom-up: a directory at depth k sums its
    children at k + 1 — roots there and directories with roots under them (disjoint: nothing under a root
    is a root). Returns the directories with at least `floor` root rows `(q, k, dir, rows, paths, open_rows,
    children, direct, max_child)` (`children`: its children holding or being roots; `direct`: those that are
    roots; `max_child`: the largest child's rows), the histogram of every directory's rows `(q, k, lg, dirs,
    rows)` (`lg` = ⌊log2 rows⌋), and with `partial_top` the depth-1 directories unfloored and out of the
    histogram (when `rp` is one subtree partition, they are partial sums)."""
    maxd = con.execute(f"SELECT max(depth) FROM {rp}").fetchone()[0] or 0
    con.execute("""CREATE OR REPLACE TABLE dstat (q VARCHAR, k INTEGER, dir VARCHAR, rows BIGINT, paths BIGINT, open_rows BIGINT,
        children BIGINT, direct BIGINT, max_child BIGINT)""")
    con.execute("CREATE OR REPLACE TABLE dhist (q VARCHAR, k INTEGER, lg INTEGER, dirs BIGINT, rows BIGINT)")
    con.execute("CREATE OR REPLACE TABLE dtop (q VARCHAR, k INTEGER, dir VARCHAR, rows BIGINT, paths BIGINT, open_rows BIGINT, children BIGINT, direct BIGINT, max_child BIGINT)")
    con.execute("CREATE OR REPLACE TABLE dcur (q VARCHAR, dir VARCHAR, rows BIGINT, paths BIGINT, open_rows BIGINT)")
    for k in range(maxd - 1, 0, -1):
        con.execute(f"""CREATE OR REPLACE TABLE dnext AS SELECT q, {raw_parent('x')} AS dir, sum(rows)::BIGINT AS rows, sum(paths)::BIGINT AS paths,
                sum(open_rows)::BIGINT AS open_rows, count(*)::BIGINT AS children, sum(is_root)::BIGINT AS direct, max(rows)::BIGINT AS max_child
            FROM (SELECT q, path AS x, n AS rows, 1 AS paths, n_open AS open_rows, 1 AS is_root FROM {rp} WHERE depth = {k + 1}
                  UNION ALL SELECT q, dir, rows, paths, open_rows, 0 FROM dcur)
            GROUP BY ALL""")
        if k == 1 and partial_top:
            con.execute("INSERT INTO dtop SELECT q, 1, dir, rows, paths, open_rows, children, direct, max_child FROM dnext")
        else:
            con.execute(f"INSERT INTO dstat SELECT q, {k}, dir, rows, paths, open_rows, children, direct, max_child FROM dnext WHERE rows >= {floor}")
            con.execute(f"""INSERT INTO dhist SELECT q, {k}, floor(log2(rows))::INTEGER AS lg, count(*), sum(rows)
                FROM dnext GROUP BY q, lg""")
        con.execute("CREATE OR REPLACE TABLE dcur AS SELECT q, dir, rows, paths, open_rows FROM dnext")
    dirs = con.execute("SELECT * FROM dstat ORDER BY q, k, dir").to_arrow_table()
    hist = con.execute("SELECT * FROM dhist ORDER BY q, k, lg").to_arrow_table()
    top = con.execute("SELECT * FROM dtop ORDER BY q, dir").to_arrow_table() if partial_top else None
    for t in ("dstat", "dhist", "dtop", "dcur", "dnext"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    return dirs, hist, top


# ── Shard plumbing ─────────────────────────────────────────────────────────


def chunk_wheres(sx: str, chunk_rows: int = CHUNK_ROWS) -> list[str]:
    """`sx`'s rows in chunks of about `chunk_rows`, cut at row-group starts as `(s, path)` ranges (as
    `static_catalog.shard_cells`): each a DuckDB predicate."""
    pf = pq.ParquetFile(sx)
    cuts, n = [], 0
    for g in range(pf.metadata.num_row_groups):
        if n >= chunk_rows:
            first = pf.read_row_group(g, columns=["s", "path"]).slice(0, 1).to_pylist()[0]
            cuts.append((first["s"], first["path"]))
            n = 0
        n += pf.metadata.row_group(g).num_rows
    bounds = [None, *cuts, None]
    out = []
    for lo, hi in zip(bounds, bounds[1:]):
        conds = []
        if lo is not None:
            conds.append(f"s >= {q(lo[0])} AND (s > {q(lo[0])} OR path >= {q(lo[1])})")
        if hi is not None:
            conds.append(f"s <= {q(hi[0])} AND (s < {q(hi[0])} OR path < {q(hi[1])})")
        out.append(" AND ".join(conds) or "true")
    return out


class Queue:
    """Shards from a shared queue: a task claims one by creating `claims/<kind>/<name>` in the scratch bucket
    (its content: the task index); a claim older than `lease` seconds is taken over (a preempted task's), and so
    is one holding this task's own index (an earlier attempt of this task, which is not running any more).

    `drain` is the whole protocol: a task never exits successfully while an item it is responsible for is
    unfinished. After its pass it waits for the items other tasks hold, taking over any whose lease expires, and
    fails (`QueueTimeout`) when something is still unfinished after `wait` seconds of waiting. (A preempted task's
    fresh claim was once skipped, and the job succeeded without its shards: specs/cw-static-names.md.)"""

    def __init__(self, scratch_bucket, prefix: str, kind: str, task: int, lease: int, *, wait: float | None = None,
                 poll: float = 60, now: Callable[[], float] = time, sleep: Callable[[float], None] = sleep):
        self.b, self.prefix, self.kind, self.t, self.lease = scratch_bucket, prefix, kind, task, lease
        self.wait = 2 * lease if wait is None else wait
        self.poll, self.now, self.sleep = poll, now, sleep

    def claim(self, name: str) -> bool:
        from google.api_core.exceptions import NotFound, PreconditionFailed

        blob = self.b.blob(f"{self.prefix}/claims/{self.kind}/{name}")
        try:
            blob.upload_from_string(str(self.t), if_generation_match=0)
            return True
        except PreconditionFailed:
            pass
        try:
            blob.reload()
            owner = blob.download_as_text(if_generation_match=blob.generation)
        except (NotFound, PreconditionFailed):
            return self.claim(name)
        age = self.now() - blob.updated.timestamp()
        if owner.strip() != str(self.t) and age < self.lease:
            return False
        try:
            blob.upload_from_string(str(self.t), if_generation_match=blob.generation)
            err(f"{self.kind} {name}: taking over a claim {age:.0f}s old (task {owner.strip()}'s)")
            return True
        except PreconditionFailed:
            return False

    def drain(self, names: list[str], done: Callable[[str], bool], work: Callable[[str], None]) -> list[str]:
        """Build every one of `names` (in order) that isn't `done` and that this task can claim; then wait for the
        rest — polling every `poll` seconds, building any whose claim expires — until all are done. Returns the
        names this task built; raises `QueueTimeout` if some are still unfinished after `wait` seconds."""
        built, pending = [], []
        for name in names:
            if done(name):
                continue
            if self.claim(name):
                work(name)
                built.append(name)
            else:
                pending.append(name)
        t0 = self.now()
        while pending:
            left = []
            for name in pending:
                if done(name):
                    continue
                if self.claim(name):
                    work(name)
                    built.append(name)
                else:
                    left.append(name)
            pending = left
            if not pending:
                break
            waited = self.now() - t0
            if waited >= self.wait:
                raise QueueTimeout(f"{self.kind}: task {self.t} waited {waited:.0f}s; still unfinished (claimed by other tasks): {', '.join(pending)}")
            err(f"{self.kind}: task {self.t} waiting for {len(pending)} claimed shards ({', '.join(pending[:8])}{', …' if len(pending) > 8 else ''})")
            self.sleep(self.poll)
        return built


class QueueTimeout(SystemExit):
    """A queue task gave up waiting for other tasks' claims: the task (and so the job) fails."""

    def __init__(self, msg: str):
        err(msg)
        super().__init__(msg)


def _put(b, key: str, t: pa.Table) -> None:
    sink = pa.BufferOutputStream()
    pq.write_table(t, sink, compression=CODEC)
    b.blob(key).upload_from_string(sink.getvalue().to_pybytes())


def _queue_order(plan: dict) -> list[dict]:
    """Every shard, biggest first (so the stragglers start early)."""
    return sorted(plan["shards"], key=lambda s: -s["rows"])


# ── Building: the roots files and the rollups ──────────────────────────────

#: Rows per row group of the roots and rollup files (the decode unit; the Worker reads whole groups).
ROOT_RG = 8192
ROOT_SCHEMA = pa.schema([
    pa.field("q", pa.string(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("usr", pa.string(), nullable=False),
    pa.field("vf", pa.int64(), nullable=False),
    pa.field("vt", pa.int64(), nullable=False),
    pa.field("size", pa.int64(), nullable=False),
    pa.field("n_files", pa.int64(), nullable=False),
])
#: `kind` 0: the (q, dir) header — `child` '', `vf` = the children kept, `b` = root rows under dir, `o` =
#: its children holding or being roots; 1: a kept child's cells; 2: the remainder's cells (every other
#: child, summed; `child` '').
ROLLUP_SCHEMA = pa.schema([
    pa.field("q", pa.string(), nullable=False),
    pa.field("dir", pa.string(), nullable=False),
    pa.field("kind", pa.int8(), nullable=False),
    pa.field("child", pa.string(), nullable=False),
    pa.field("vf", pa.int64(), nullable=False),
    pa.field("b", pa.int64(), nullable=False),
    pa.field("o", pa.int64(), nullable=False),
])
#: Per row group: its exact first and last `(q, key)` (`key` = `path` for roots, `dir` for rollups), byte span,
#: rows, and per column `(data_page_offset, total_compressed_size, dictionary_page_offset or 0)`.
GROUP_INDEX_SCHEMA = pa.schema([
    pa.field("file", pa.string(), nullable=False),
    pa.field("rg", pa.int32(), nullable=False),
    pa.field("q_min", pa.string(), nullable=False),
    pa.field("k_min", pa.string(), nullable=False),
    pa.field("q_max", pa.string(), nullable=False),
    pa.field("k_max", pa.string(), nullable=False),
    pa.field("offset", pa.int64(), nullable=False),
    pa.field("length", pa.int64(), nullable=False),
    pa.field("rows", pa.int32(), nullable=False),
    pa.field("chunks", pa.list_(pa.int64()), nullable=False),
])


def write_indexed(batches, out: Path, schema: pa.Schema, key: str, name: str, dictionary: list[str], rg: int | None = None) -> tuple[int, pa.Table]:
    """Write sorted batches as `rg`-row groups (default `ROOT_RG`) and cut their `GROUP_INDEX_SCHEMA` rows (`file` = `name`)."""
    from .static_names import write_sorted

    stats: list[tuple] = []

    def on_group(g: pa.Table) -> None:
        qc, kc = g.column("q"), g.column(key)
        stats.append((qc[0].as_py(), kc[0].as_py(), qc[g.num_rows - 1].as_py(), kc[g.num_rows - 1].as_py()))

    rows = write_sorted(batches, out, schema, rg or ROOT_RG, on_group=on_group, dictionary=dictionary)
    md = pq.ParquetFile(out).metadata
    if md.num_row_groups != len(stats):
        raise RuntimeError(f"{out}: {md.num_row_groups} row groups, {len(stats)} recorded")
    cols = {k: [] for k in GROUP_INDEX_SCHEMA.names}
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        chunks, starts, ends = [], [], []
        for c in range(rg.num_columns):
            cc = rg.column(c)
            dict_off = cc.dictionary_page_offset or 0
            start = min(dict_off, cc.data_page_offset) if dict_off else cc.data_page_offset
            chunks += [cc.data_page_offset, cc.total_compressed_size, dict_off]
            starts.append(start)
            ends.append(start + cc.total_compressed_size)
        for k, v in zip(GROUP_INDEX_SCHEMA.names, (name, g, *stats[g], min(starts), max(ends) - min(starts), rg.num_rows, chunks)):
            cols[k].append(v)
    return rows, pa.table(cols, schema=GROUP_INDEX_SCHEMA)


def rollup_sql(con, rt: str, R: int, K: int) -> str:
    """Rollup cells (`ROLLUP_SCHEMA`, a query) of `rt`'s (`ROOT_COLS`, every root of its members) heavy
    directories: `(q, dir)` with more than `R` root rows strictly under it. Per heavy `(q, dir)`, per child
    of `dir` (its next path segment), the running Σ size, n_files of the roots at or under the child, one
    cell per change (`+` at `vf`, `−` at `vt`); the `K` children with the largest peak bytes (ties by name)
    are kept by name, the rest summed into the remainder. Heavy directories are closed upwards (their
    ancestors are heavy), so the roots are exploded one depth at a time over a shrinking candidate set."""
    con.execute("DROP TABLE IF EXISTS hrp")
    con.execute(f"CREATE TABLE hrp AS SELECT q, depth, path, count(*)::BIGINT AS n, count(*) FILTER (WHERE vt = {OPEN})::BIGINT AS n_open FROM {rt} GROUP BY q, depth, path")
    dirs, _, _ = dir_stats(con, "hrp", R + 1)
    con.execute("DROP TABLE hrp")
    con.execute("CREATE OR REPLACE TABLE hv (q VARCHAR, k INTEGER, dir VARCHAR, rows BIGINT, children BIGINT)")
    if dirs.num_rows:
        con.register("hv_in", dirs.select(["q", "k", "dir", "rows", "children"]))
        con.execute("INSERT INTO hv SELECT * FROM hv_in")
        con.unregister("hv_in")
    con.execute("CREATE OR REPLACE TABLE rev (q VARCHAR, dir VARCHAR, child VARCHAR, t BIGINT, db HUGEINT, dn HUGEINT)")
    con.execute(f"CREATE OR REPLACE TABLE hc AS SELECT q, string_split(path, '/') AS segs, vf, vt, size, n_files FROM {rt} SEMI JOIN (SELECT DISTINCT q FROM hv) USING (q)")
    top = con.execute("SELECT coalesce(max(k), 0) FROM hv").fetchone()[0]
    for k in range(1, top + 1):
        con.execute(f"""CREATE OR REPLACE TABLE hc AS SELECT hc.* FROM hc SEMI JOIN (SELECT q, dir FROM hv WHERE k = {k}) AS h
            ON hc.q = h.q AND array_to_string(hc.segs[1:{k}], '/') = h.dir""")
        hit = f"SELECT q, array_to_string(segs[1:{k}], '/') AS dir, segs[{k + 1}] AS child, vf, vt, size, n_files FROM hc"
        con.execute(f"""INSERT INTO rev SELECT q, dir, child, t, sum(db), sum(dn) FROM (
                SELECT q, dir, child, vf AS t, size AS db, n_files AS dn FROM ({hit})
                UNION ALL SELECT q, dir, child, vt AS t, -size, -n_files FROM ({hit}) WHERE vt <> {OPEN}
            ) GROUP BY q, dir, child, t""")
    con.execute("DROP TABLE hc")
    run = """sum(db) OVER (PARTITION BY q, dir, child ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::BIGINT AS b,
             sum(dn) OVER (PARTITION BY q, dir, child ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::BIGINT AS o"""
    con.execute(f"""CREATE OR REPLACE TABLE rkeep AS SELECT q, dir, child FROM (
            SELECT q, dir, child, row_number() OVER (PARTITION BY q, dir ORDER BY peak DESC, child) AS r FROM (
                SELECT q, dir, child, max(b) AS peak FROM (SELECT q, dir, child, {run} FROM rev) GROUP BY q, dir, child))
        WHERE r <= {K}""")
    con.execute("""CREATE OR REPLACE TABLE rev2 AS
            SELECT q, dir, 1::TINYINT AS kind, child, t, db, dn FROM rev SEMI JOIN rkeep USING (q, dir, child)
            UNION ALL SELECT q, dir, 2::TINYINT, '', t, sum(db), sum(dn) FROM rev ANTI JOIN rkeep USING (q, dir, child) GROUP BY q, dir, t""")
    con.execute("DROP TABLE rev")
    cells = f"""SELECT q, dir, kind, child, t AS vf,
            sum(db) OVER (PARTITION BY q, dir, kind, child ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::BIGINT AS b,
            sum(dn) OVER (PARTITION BY q, dir, kind, child ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::BIGINT AS o
        FROM (SELECT q, dir, kind, child, t, sum(db) AS db, sum(dn) AS dn FROM rev2 GROUP BY ALL HAVING sum(db) <> 0 OR sum(dn) <> 0)"""
    return f"""SELECT h.q, h.dir, 0::TINYINT AS kind, '' AS child, (SELECT count(*) FROM rkeep AS r WHERE r.q = h.q AND r.dir = h.dir)::BIGINT AS vf,
            h.rows AS b, h.children AS o FROM hv AS h
        UNION ALL {cells}"""


def build_roots(con, rt: str, R: int, K: int, out: Path, name: str) -> dict:
    """`rt`'s roots (every root of its members) → `out/roots/<name>.parquet` (sorted `(q, path, usr, vf)`) and
    `out/rollups/<name>.parquet` (sorted `(q, dir, kind, child, vf)`), with their group indexes
    `out/{roots,rollups}-index/<name>.parquet`; returns the counts."""
    t0 = monotonic()
    rows, idx = write_indexed(_batches(con, f"SELECT q, path, usr, vf, vt, size, n_files FROM {rt} ORDER BY q, path, usr, vf"),
                              out / "roots" / f"{name}.parquet", ROOT_SCHEMA, "path", f"roots/{name}.parquet", ["q", "usr"])
    t1 = monotonic()
    sql = rollup_sql(con, rt, R, K)
    con.execute(f"CREATE OR REPLACE TABLE rcells AS {sql}")
    cells, ridx = write_indexed(_batches(con, "SELECT * FROM rcells ORDER BY q, dir, kind, child, vf"), out / "rollups" / f"{name}.parquet",
                                ROLLUP_SCHEMA, "dir", f"rollups/{name}.parquet", ["q", "dir", "child"])
    heavy = con.execute("SELECT count(*) FROM hv").fetchone()[0]
    for sub, t in (("roots-index", idx), ("rollups-index", ridx)):
        (out / sub).mkdir(parents=True, exist_ok=True)
        pq.write_table(t, out / sub / f"{name}.parquet", compression=CODEC)
    for t in ("rcells", "rev2", "rkeep", "hv"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    return {"roots": rows, "roots_bytes": (out / "roots" / f"{name}.parquet").stat().st_size, "roots_groups": idx.num_rows,
            "heavy_dirs": heavy, "rollup_cells": cells, "rollup_bytes": (out / "rollups" / f"{name}.parquet").stat().st_size,
            "roots_s": round(t1 - t0, 1), "rollups_s": round(monotonic() - t1, 1)}



# ── Reading (the Worker's logic) ───────────────────────────────────────────


#: Entries per row group of a concatenated group index (`drill/<kind>-<sub>-index.parquet`); its top file
#: (`…-index.top.parquet`) has one entry per index row group.
IDX_RG = 1024


def write_index_levels(t: pa.Table, out: Path) -> pa.Table:
    """Write a sorted group index `t` (`GROUP_INDEX_SCHEMA`) as `out` in `IDX_RG`-entry row groups and return
    its top: per index row group `(rg, q_min, k_min, q_max, k_max, offset, length, rows, chunks)` — its first
    entry's first key, its last entry's last key, its byte span, the data rows its entries hold, and per column
    `(data_page_offset, total_compressed_size, dictionary_page_offset or 0)` (`TOP_SCHEMA`)."""
    pq.write_table(t, out, compression=CODEC, row_group_size=IDX_RG)
    if t.num_rows == 0:
        return TOP_SCHEMA.empty_table()
    md = pq.ParquetFile(out).metadata
    cols = {k: [] for k in TOP_SCHEMA.names}
    off = 0
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        part = t.slice(off, rg.num_rows)
        off += rg.num_rows
        chunks, starts, ends = [], [], []
        for c in range(rg.num_columns):
            cc = rg.column(c)
            dict_off = cc.dictionary_page_offset or 0
            start = min(dict_off, cc.data_page_offset) if dict_off else cc.data_page_offset
            chunks += [cc.data_page_offset, cc.total_compressed_size, dict_off]
            starts.append(start)
            ends.append(start + cc.total_compressed_size)
        vals = (g, part.column("q_min")[0].as_py(), part.column("k_min")[0].as_py(), part.column("q_max")[-1].as_py(),
                part.column("k_max")[-1].as_py(), min(starts), max(ends) - min(starts), int(pa.compute.sum(part.column("rows")).as_py()), chunks)
        for k, v in zip(TOP_SCHEMA.names, vals):
            cols[k].append(v)
    if off != t.num_rows:
        raise RuntimeError(f"{out}: {off} of {t.num_rows} entries in row groups")
    return pa.table(cols, schema=TOP_SCHEMA)


TOP_SCHEMA = pa.schema([
    pa.field("rg", pa.int32(), nullable=False),
    pa.field("q_min", pa.string(), nullable=False),
    pa.field("k_min", pa.string(), nullable=False),
    pa.field("q_max", pa.string(), nullable=False),
    pa.field("k_max", pa.string(), nullable=False),
    pa.field("offset", pa.int64(), nullable=False),
    pa.field("length", pa.int64(), nullable=False),
    pa.field("rows", pa.int64(), nullable=False),
    pa.field("chunks", pa.list_(pa.int64()), nullable=False),
])


def _spans_read(Spans, size: int, footer: tuple[int, bytes], start: int, data: bytes, rgs: list[int]) -> list[dict]:
    return pq.ParquetFile(Spans(size, [footer, (start, data)])).read_row_groups(rgs).to_pylist()


class GroupFile:
    """Row groups of one roots or rollups set, through its group index: the groups that can hold keys in
    `[lo, hi)` (contiguous: the index is sorted by `(q_min, k_min)` and the keys are totally ordered) and
    their rows from one ranged read per file. The index is either in memory (`index`) or two-level (`top`,
    the index file's top; its entries fetched by ranged reads of `index_file`), as the Worker reads it."""

    def __init__(self, index: pa.Table | None, fetch, size_of, top: pa.Table | None = None, index_file: str | None = None):
        from .static_names import Spans

        self.fetch, self.size_of, self.Spans = fetch, size_of, Spans
        self.foot: dict[str, tuple[int, bytes]] = {}
        self.index_file = index_file
        if top is not None:
            self.top = top.sort_by("rg").to_pylist()
            self.groups = None
        else:
            self.top = None
            self.groups = index.sort_by([("q_min", "ascending"), ("k_min", "ascending")]).to_pylist()
        self.io = {"index_reads": 0, "index_bytes": 0}

    @staticmethod
    def _meet(entries: list[dict], lo, hi) -> tuple[int, int]:
        """Entries `[a, b)` (sorted, disjoint) whose key range meets `[lo, hi)`."""
        from bisect import bisect_left

        a = bisect_left([(e["q_max"], e["k_max"]) for e in entries], lo)
        b = bisect_left([(e["q_min"], e["k_min"]) for e in entries], hi)
        return a, max(a, b)

    def select(self, lo, hi, cap: int | None = None) -> tuple[list[dict] | None, int]:
        """The groups meeting `[lo, hi)` and their rows' sum. With a two-level index and `cap`, when the index
        row groups strictly inside the range already hold more than `cap` rows, returns `(None, that sum)`
        without reading the index (the range is at least that big)."""
        if self.top is None:
            a, b = self._meet(self.groups, lo, hi)
            sel = self.groups[a:b]
            return sel, sum(g["rows"] for g in sel)
        a, b = self._meet(self.top, lo, hi)
        if a == b:
            return [], 0
        inner = sum(t["rows"] for t in self.top[a + 1:b - 1])
        if cap is not None and inner > cap:
            return None, inner
        sel_top = self.top[a:b]
        start, end = sel_top[0]["offset"], sel_top[-1]["offset"] + sel_top[-1]["length"]
        data = self.fetch(self.index_file, start, end)
        self.io["index_reads"] += 1
        self.io["index_bytes"] += len(data)
        entries = _spans_read(self.Spans, self.size_of(self.index_file), self.footer(self.index_file), start, data, [t["rg"] for t in sel_top])
        a, b = self._meet(entries, lo, hi)
        sel = entries[a:b]
        return sel, sum(g["rows"] for g in sel)

    def footer(self, file: str) -> tuple[int, bytes]:
        if file not in self.foot:
            size = self.size_of(file)
            tail = self.fetch(file, size - 8, size)
            flen = int.from_bytes(tail[:4], "little")
            data = self.fetch(file, max(0, size - max(8 + flen, 1 << 16)), size)  # pyarrow reads up to 64 KB of tail
            self.foot[file] = (size - len(data), data)
        return self.foot[file]

    def read(self, lo, hi, groups: list[dict] | None = None) -> tuple[list[dict], dict]:
        if groups is None:
            groups, _ = self.select(lo, hi)
        rows, io = [], {"groups": len(groups), "bytes": 0, "rows_read": 0}
        by_file: dict[str, list[dict]] = {}
        for g in groups:
            by_file.setdefault(g["file"], []).append(g)
        for file, gs in by_file.items():
            start, end = gs[0]["offset"], gs[-1]["offset"] + gs[-1]["length"]
            data = self.fetch(file, start, end)
            io["bytes"] += len(data)
            for r in _spans_read(self.Spans, self.size_of(file), self.footer(file), start, data, [g["rg"] for g in gs]):
                io["rows_read"] += 1
                key = (r["q"], r["path"] if "path" in r else r["dir"])
                if lo <= key < hi:
                    rows.append(r)
        return rows, io


class Drill:
    """A heavy term's filtered view at a directory `P`: per child of `P`, Σ size, n_files of the roots under
    it live on a date. Dispatch from the indexes alone: if the roots index bounds `[(q, P/), (q, P0))` at
    ≤ `R + 2·rg` rows (`rg` = the files' row-group size), read them; else `(q, P)` is heavy (its true rows >
    R), so its rollup holds the kept children (`answers[date]`) and the remainder (`rest[date]`)."""

    def __init__(self, roots: GroupFile, rollups: GroupFile, R: int, rg: int = ROOT_RG, aliases: dict[str, str] | None = None,
                 rule: HexRule | None = None):
        self.roots, self.rollups, self.R, self.rg, self.aliases, self.rule = roots, rollups, R, rg, aliases or {}, rule

    def view(self, term: str, P: str, dates: list[str]) -> dict:
        from .hex_runs import occurs

        t = term.lower()
        if occurs(t, P.lower(), self.rule):
            return {"q": t, "P": P, "source": "plain", "answers": None}
        c = self.aliases.get(t, t)  # members with identical root sets share their canonical's rows
        lo, hi = (c, P + "/"), (c, P + "0")
        groups, ub = self.roots.select(lo, hi, cap=self.R + 2 * self.rg)
        out: dict = {"q": t, "P": P, "upper": ub}
        if ub <= self.R + 2 * self.rg:
            rows, io = self.roots.read(lo, hi, groups)
            out.update(source="roots", io=io, rows=len(rows), answers=roots_answers(rows, P, dates))
            return out
        rows, io = self.rollups.read((c, P), (c, P + "\x00"))
        out.update(source="rollup", io=io, rows=len(rows))
        if not rows or rows[0]["kind"] != 0:
            raise RuntimeError(f"({t!r}, {P!r}): {ub:,} root rows bound, but no rollup")
        out.update(rollup_view(rows[0], rows[1:], dates))
        return out


def roots_answers(rows: list[dict], P: str, dates: list[str]) -> dict[str, dict[str, list[int]]]:
    """Per date, Σ size, n_files of the roots (`rows`, all under `P/`) live on it, by child of `P` (nonzero only)."""
    from .static_names import scan_epoch

    out = {}
    for d in dates:
        D = scan_epoch(d)
        acc: dict[str, list[int]] = {}
        for r in rows:
            if r["vf"] <= D < r["vt"]:
                c = r["path"][len(P) + 1:].split("/", 1)[0]
                e = acc.setdefault(c, [0, 0])
                e[0] += r["size"]
                e[1] += r["n_files"]
        out[d] = {k: v for k, v in sorted(acc.items()) if v != [0, 0]}
    return out


def rollup_view(header: dict, cells: list[dict], dates: list[str]) -> dict:
    """A rollup's header and cells (kept children `kind` 1, the remainder `kind` 2) → per date each kept child's
    newest cell with `vf ≤ D` (nonzero) and the remainder's."""
    from .static_names import scan_epoch

    out: dict = {"header": {"kept": header["vf"], "rows": header["b"], "children": header["o"]},
                 "kept": sorted({r["child"] for r in cells if r["kind"] == 1}), "answers": {}, "rest": {}}
    cells = sorted(cells, key=lambda r: r["vf"])
    for d in dates:
        D = scan_epoch(d)
        cur: dict[tuple[int, str], tuple[int, int]] = {}
        for r in cells:
            if r["vf"] <= D:
                cur[(r["kind"], r["child"])] = (r["b"], r["o"])
        out["answers"][d] = {c: list(v) for (kd, c), v in sorted(cur.items()) if kd == 1 and v != (0, 0)}
        out["rest"][d] = list(cur.get((2, ""), (0, 0)))
    return out


# ── CLI ────────────────────────────────────────────────────────────────────


@group("roots")
def cli() -> None:
    """Match roots of the catalog members: measurement, the roots files, rollups (specs/architecture/static-name-search.md)."""


MEASURE = "roots-measure"


@cli.command("measure")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-f", "--floor", "floor_rows", default=10_000, type=int, help="Keep directories with at least this many root rows under them")
@option("-g", "--gen", required=True, help="Generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-l", "--lease", default=5400, type=int, help="Seconds after which another task may take over a claimed, unfinished shard")
@option("-m", "--mount", required=True, help="Local mount of the bucket (for `catalog/members.parquet`)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-o", "--only", help="Only these shards (comma-separated indices)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-S", "--scratch", default=scratch_bucket, help="Bucket holding the queue's claims")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir, and where each shard is downloaded")
@option("-w", "--wait", type=float, help="Seconds a task waits, after its pass, for shards other tasks claimed before failing (default: 2 × the lease)")
@option("-W", "--poll", default=60.0, type=float, help="Seconds between checks while waiting for other tasks' claims")
def measure_cmd(bucket, floor_rows, gen, index, lease, mount, mem, only, threads, scratch, tmp, wait, poll) -> None:
    """Long members' roots counted per shard (a shared queue, biggest shard first) → `roots-measure/{q,qdepth,dirs,hist}/s####.parquet`
    (`q_stats`, `dir_stats`); a shard whose `q/` file exists is skipped."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    plan = read_json(f"gs://{bucket}/{prefix}/shards.json")
    t = _task(index)
    client = storage.Client()
    b = client.bucket(bucket)
    queue = Queue(client.bucket(scratch), prefix, "roots-measure", t, lease, wait=wait, poll=poll)
    con = connect(threads, mem, tmp)
    con.execute(f"CREATE TABLE allm AS SELECT q, shard, rows FROM read_parquet({q(f'{mount}/{prefix}/catalog/members.parquet')})")
    t_start, n_done = monotonic(), 0
    keep = {int(x) for x in only.split(",")} if only else None
    shards = {f"s{s['i']:04d}": s for s in _queue_order(plan) if keep is None or s["i"] in keep}

    def work(name: str) -> None:
        nonlocal n_done
        s = shards[name]
        t0 = monotonic()
        con.execute(f"CREATE OR REPLACE TABLE mem AS SELECT q, rows FROM allm WHERE shard = {s['i']}")
        n_members = con.execute("SELECT count(*) FROM mem").fetchone()[0]
        con.execute("DROP TABLE IF EXISTS rp")
        if n_members:
            src = Path(tmp) / f"sx-{name}.parquet"
            b.blob(f"{prefix}/sx/{name}.parquet").download_to_filename(str(src))
            err(f"measure {name}: {s['rows']:,} rows, {n_members:,} members, downloaded in {monotonic() - t0:.1f}s")
            wheres = chunk_wheres(str(src))
            for k, where in enumerate(wheres):
                member_roots(con, sx_rows_sql(f"(SELECT * FROM read_parquet({q(str(src))}) WHERE {where})"), "mem", "rp", agg=True, rule=gen_rule_at(bucket, gen))
                err(f"measure {name}: chunk {k + 1}/{len(wheres)} in {monotonic() - t0:.1f}s")
            src.unlink()
        con.execute(f"CREATE TABLE IF NOT EXISTS rp ({RP_COLS})")
        t1 = monotonic()
        n_rp, n_rows = con.execute("SELECT count(*), coalesce(sum(n), 0) FROM rp").fetchone()
        qs, qd = q_stats(con, "rp")
        dirs, hist, _ = dir_stats(con, "rp", floor_rows)
        for sub, tab in (("qdepth", qd), ("dirs", dirs), ("hist", hist), ("q", qs)):
            _put(b, f"{prefix}/{MEASURE}/{sub}/{name}.parquet", tab)
        n_done += 1
        doc = {"shard": s["i"], "rows": s["rows"], "members": n_members, "root_rows": int(n_rows), "root_paths": n_rp, "dirs": dirs.num_rows,
               "s": round(monotonic() - t0, 1), "stats_s": round(monotonic() - t1, 1)}
        err(f"measure {name}: {n_rows:,} root rows, {n_rp:,} root paths, {dirs.num_rows:,} dirs ≥ {floor_rows:,} in {doc['s']}s "
            f"(stats {doc['stats_s']}s; task {t}: {n_done} shards in {monotonic() - t_start:.0f}s)")
        print(json.dumps(doc), flush=True)
        con.execute("DROP TABLE rp")

    queue.drain(list(shards), lambda name: b.blob(f"{prefix}/{MEASURE}/q/{name}.parquet").exists(), work)


def _partition(parts: int) -> str:
    """A version's subtree partition: by its first two path segments, so every directory at depth ≥ 2 has
    all its descendants in one partition (depth-1 directories are summed across partitions)."""
    return f"(hash(split_part(path, '/', 1) || '/' || split_part(path, '/', 2)) % {parts})"


def download(bucket, keys: list[str], dst: Path, workers: int = 16) -> list[Path]:
    dst.mkdir(parents=True, exist_ok=True)

    def one(key: str) -> Path:
        p = dst / Path(key).name
        if not p.exists():
            bucket.blob(key).download_to_filename(str(p))
        return p

    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, keys))


@cli.command("measure-short")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-f", "--floor", "floor_rows", default=10_000, type=int, help="Keep directories with at least this many root rows under them")
@option("-g", "--gen", required=True, help="Generation (its `cintervals/`)")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX): the subtree partition")
@option("-m", "--mount", required=True, help="Local mount of the bucket (unused: versions are downloaded)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--parts", default=32, type=IntRange(min=1), help="Subtree partitions (= tasks)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-P", "--pieces", default=8, type=IntRange(min=1), help="Passes per partition by hash(path) (memory)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir, and where the versions are downloaded")
def measure_short_cmd(bucket, floor_rows, gen, index, mount, mem, parts, threads, pieces, tmp) -> None:
    """One- and two-character literals' roots counted over one subtree partition of the coalesced versions →
    `roots-measure/short/{q,qdepth,dirs,hist,top}/p###.parquet` (`top`: the partial depth-1 directories)."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    t = _task(index)
    client = storage.Client()
    b = client.bucket(bucket)
    name = f"p{t:03d}"
    if b.blob(f"{prefix}/{MEASURE}/short/q/{name}.parquet").exists():
        err(f"measure-short {name}: done")
        return
    t0 = monotonic()
    keys = sorted(x.name for x in client.list_blobs(bucket, prefix=f"{prefix}/cintervals/") if x.name.endswith(".parquet"))
    local = download(b, keys, Path(tmp) / "cintervals")
    err(f"measure-short {name}: {len(local)} version files downloaded in {monotonic() - t0:.1f}s")
    con = connect(threads, mem, tmp)
    versions = f"SELECT * FROM read_parquet({q(str(Path(tmp) / 'cintervals' / '*.parquet'))}) WHERE {_partition(parts)} = {t}"
    short_roots(con, versions, "rp", agg=True, pieces=pieces, log=f"measure-short {name}", rule=gen_rule_at(bucket, gen))
    n_rp, n_rows = con.execute("SELECT count(*), coalesce(sum(n), 0) FROM rp").fetchone()
    err(f"measure-short {name}: {n_rows:,} root rows, {n_rp:,} root paths in {monotonic() - t0:.1f}s")
    qs, qd = q_stats(con, "rp")
    dirs, hist, top = dir_stats(con, "rp", floor_rows, partial_top=True)
    for sub, tab in (("qdepth", qd), ("dirs", dirs), ("hist", hist), ("top", top), ("q", qs)):
        _put(b, f"{prefix}/{MEASURE}/short/{sub}/{name}.parquet", tab)
    doc = {"part": t, "root_rows": int(n_rows), "root_paths": n_rp, "dirs": dirs.num_rows, "s": round(monotonic() - t0, 1)}
    err(f"measure-short {name}: {dirs.num_rows:,} dirs ≥ {floor_rows:,}, done in {doc['s']}s")
    print(json.dumps(doc), flush=True)
    shutil.rmtree(Path(tmp) / "cintervals")


@cli.command("measure-report")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-R", "--read-rows", "read_rows", default=100_000, type=int, help="A drill's read bound (root rows under the directory)")
@option("-t", "--top", default=50, type=int, help="Members listed by root rows")
def measure_report_cmd(bucket, gen, read_rows, top) -> None:
    """Summarize `roots-measure/` (long members per shard, short literals per subtree partition, depth-1
    partials summed): totals, quantiles, the top members, and the directories whose roots exceed the read
    bound (`-R`) by depth, with their children (rollup sizing). JSON on stdout."""
    import tempfile

    import duckdb
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}/{MEASURE}"
    client = storage.Client()
    with tempfile.TemporaryDirectory() as d:
        n = 0
        for blob in client.list_blobs(bucket, prefix=prefix + "/"):
            p = Path(d) / blob.name.removeprefix(prefix + "/")
            p.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(p))
            n += 1
        con = duckdb.connect()
        rp = lambda sub: f"read_parquet({q(d + '/' + sub + '/*.parquet')})"  # noqa: E731
        has_short = (Path(d) / "short" / "q").exists()
        con.execute(f"CREATE TABLE ql AS SELECT 'long' AS kind, * FROM {rp('q')}")
        con.execute(f"CREATE TABLE dl AS SELECT 'long' AS kind, * FROM {rp('dirs')}")
        con.execute(f"CREATE TABLE hl AS SELECT 'long' AS kind, * FROM {rp('hist')}")
        if has_short:
            con.execute(f"""INSERT INTO ql SELECT 'short', q, sum(rows), sum(paths), sum(open_rows), sum(open_paths), sum(d1_paths), min(min_depth), max(max_depth)
                FROM {rp('short/q')} GROUP BY q""")
            con.execute(f"""CREATE TABLE stop AS SELECT q, k, dir, sum(rows)::BIGINT AS rows, sum(paths)::BIGINT AS paths, sum(open_rows)::BIGINT AS open_rows,
                sum(children)::BIGINT AS children, sum(direct)::BIGINT AS direct, max(max_child)::BIGINT AS max_child FROM {rp('short/top')} GROUP BY q, k, dir""")
            con.execute(f"INSERT INTO dl SELECT 'short', * FROM {rp('short/dirs')}")
            con.execute("INSERT INTO dl SELECT 'short', * FROM stop WHERE rows >= 10000")
            con.execute(f"INSERT INTO hl SELECT 'short', * FROM {rp('short/hist')}")
            con.execute("INSERT INTO hl SELECT 'short', q, 1, floor(log2(rows))::INTEGER AS lg, count(*), sum(rows) FROM stop GROUP BY q, lg")
        out: dict = {"gen": gen, "files": n, "read_rows": read_rows}
        for kind in ("long", "short"):
            r = con.execute(f"""SELECT count(*), sum(rows), sum(paths), sum(open_rows), sum(open_paths),
                    quantile_disc(rows, [0.5, 0.9, 0.99, 0.999]), max(rows), count(*) FILTER (WHERE rows > {read_rows})
                FROM ql WHERE kind = '{kind}'""").fetchone()
            if not r[0]:
                continue
            out[kind] = {"members": r[0], "root_rows": int(r[1]), "root_paths": int(r[2]), "open_rows": int(r[3]), "open_paths": int(r[4]),
                         "rows_q50_q90_q99_q999": [int(x) for x in r[5]], "rows_max": int(r[6]), "members_over_read_rows": r[7],
                         "top": [dict(zip(("q", "rows", "paths", "open_rows", "d1_paths", "max_depth"), x)) for x in con.execute(
                             f"SELECT q, rows, paths, open_rows, d1_paths, max_depth FROM ql WHERE kind = '{kind}' ORDER BY rows DESC, q LIMIT {top}").fetchall()]}
            heavy = con.execute(f"""SELECT k, count(*), count(DISTINCT q), sum(children), sum(least(children, 256)), max(rows), max(children),
                    quantile_disc(children, 0.5), quantile_disc(children, 0.99), sum(rows)
                FROM dl WHERE kind = '{kind}' AND rows > {read_rows} GROUP BY k ORDER BY k""").fetchall()
            out[kind]["heavy_dirs_by_depth"] = [dict(zip(("k", "dirs", "members", "children", "children_capped_256", "max_rows", "max_children",
                                                          "children_q50", "children_q99", "rows"), [int(v) for v in x])) for x in heavy]
            tops = [t["q"] for t in out[kind]["top"][:20]]
            prof = {}
            for t in tops:
                prof[t] = {}
                for k, lg, dirs, rows in con.execute(f"SELECT k, lg, dirs, rows FROM hl WHERE kind = '{kind}' AND q = ? ORDER BY k, lg", [t]).fetchall():
                    prof[t].setdefault(str(k), {})[f"2^{lg}"] = int(dirs)
            out[kind]["dir_rows_histogram_top20"] = prof
        print(json.dumps(out, indent=1, default=str))


DRILL = "drill"


def _upload_dir(b, local: Path, prefix: str) -> None:
    for f in sorted(p for p in local.rglob("*") if p.is_file()):
        blob = b.blob(f"{prefix}/{f.relative_to(local).as_posix()}")
        blob.chunk_size = 64 << 20
        blob.upload_from_filename(str(f))


@cli.command("digest")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-l", "--lease", default=5400, type=int, help="Seconds after which another task may take over a claimed, unfinished shard")
@option("-m", "--mount", required=True, help="Local mount of the bucket (for `catalog/members.parquet`)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-S", "--scratch", default=scratch_bucket, help="Bucket holding the queue's claims")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir, and where each shard is downloaded")
@option("-w", "--wait", type=float, help="Seconds a task waits, after its pass, for shards other tasks claimed before failing (default: 2 × the lease)")
@option("-W", "--poll", default=60.0, type=float, help="Seconds between checks while waiting for other tasks' claims")
def digest_cmd(bucket, gen, index, lease, mount, mem, threads, scratch, tmp, wait, poll) -> None:
    """Long members' root-set digests per shard (a shared queue) → `drill/digest/s####.parquet` `(q, n, h1, h2)`."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    plan = read_json(f"gs://{bucket}/{prefix}/shards.json")
    t = _task(index)
    client = storage.Client()
    b = client.bucket(bucket)
    queue = Queue(client.bucket(scratch), prefix, "drill-digest", t, lease, wait=wait, poll=poll)
    con = connect(threads, mem, tmp)
    con.execute(f"CREATE TABLE allm AS SELECT q, shard, rows FROM read_parquet({q(f'{mount}/{prefix}/catalog/members.parquet')})")
    shards = {f"s{s['i']:04d}": s for s in _queue_order(plan)}

    def work(name: str) -> None:
        s = shards[name]
        t0 = monotonic()
        con.execute(f"CREATE OR REPLACE TABLE mem AS SELECT q, rows FROM allm WHERE shard = {s['i']}")
        con.execute(f"DROP TABLE IF EXISTS dg; CREATE TABLE dg ({DIGEST_COLS})")
        if con.execute("SELECT count(*) FROM mem").fetchone()[0]:
            src = Path(tmp) / f"sx-{name}.parquet"
            b.blob(f"{prefix}/sx/{name}.parquet").download_to_filename(str(src))
            wheres = chunk_wheres(str(src))
            for k, where in enumerate(wheres):
                member_roots(con, sx_rows_sql(f"(SELECT * FROM read_parquet({q(str(src))}) WHERE {where})"), "mem", "dg", agg="digest", rule=gen_rule_at(bucket, gen))
                err(f"digest {name}: chunk {k + 1}/{len(wheres)} in {monotonic() - t0:.1f}s")
            src.unlink()
        tab = con.execute("""SELECT q, sum(n)::BIGINT AS n, (sum(h1) % 18446744073709551616)::UBIGINT AS h1, (sum(h2) % 18446744073709551616)::UBIGINT AS h2
            FROM dg GROUP BY q ORDER BY q""").to_arrow_table()
        _put(b, f"{prefix}/{DRILL}/digest/{name}.parquet", tab)
        err(f"digest {name}: {tab.num_rows:,} members in {monotonic() - t0:.1f}s")

    queue.drain(list(shards), lambda name: b.blob(f"{prefix}/{DRILL}/digest/{name}.parquet").exists(), work)


@cli.command("alias-plan")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
def alias_plan_cmd(bucket, gen) -> None:
    """Members with identical root sets (equal `(n, h1, h2)` digests, across shards) share one copy: the
    group's least `q` (code-point order) is canonical, built by its own shard → `drill/aliases.parquet`
    `(q, canonical, shard, n)` for every long member; prints the summary."""
    import tempfile

    import duckdb
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    client = storage.Client()
    with tempfile.TemporaryDirectory() as d:
        k = 0
        for blob in client.list_blobs(bucket, prefix=f"{prefix}/{DRILL}/digest/"):
            blob.download_to_filename(f"{d}/{Path(blob.name).name}")
            k += 1
        members = Path(d) / "members.parquet"
        client.bucket(bucket).blob(f"{prefix}/catalog/members.parquet").download_to_filename(str(members))
        con = duckdb.connect()
        con.execute(f"""CREATE TABLE a AS SELECT g.q, min(g.q) OVER (PARTITION BY n, h1, h2) AS canonical, m.shard, g.n
            FROM read_parquet({q(d + '/s*.parquet')}) AS g JOIN read_parquet({q(str(members))}) AS m USING (q)""")
        missing = con.execute(f"SELECT count(*) FROM read_parquet({q(str(members))}) ANTI JOIN a USING (q)").fetchone()[0]
        if missing:
            raise SystemExit(f"{missing} members without a digest ({k} digest files)")
        out = Path(d) / "aliases.parquet"
        con.execute(f"COPY (SELECT * FROM a ORDER BY q) TO {q(str(out))} (FORMAT parquet, COMPRESSION zstd)")
        client.bucket(bucket).blob(f"{prefix}/{DRILL}/aliases.parquet").upload_from_filename(str(out))
        summary = dict(zip(("members", "canonical", "rows", "canonical_rows"), con.execute(
            "SELECT count(*), count(*) FILTER (WHERE q = canonical), sum(n), sum(n) FILTER (WHERE q = canonical) FROM a").fetchone()))
    summary = {"gen": gen, **{k: int(v) for k, v in summary.items()}}
    print(json.dumps(summary, indent=1))


@cli.command("build")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-F", "--force", is_flag=True, help="Rebuild (overwrite) shards whose outputs exist (with -o)")
@option("-K", "--keep", "K", default=256, type=int, help="Children kept by name per heavy directory (the rest: the remainder)")
@option("-l", "--lease", default=5400, type=int, help="Seconds after which another task may take over a claimed, unfinished shard")
@option("-m", "--mount", required=True, help="Local mount of the bucket (for `catalog/members.parquet`)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-o", "--only", help="Only these shards (comma-separated indices)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-R", "--read-rows", "R", default=100_000, type=int, help="Directories with more root rows under them get rollups")
@option("-S", "--scratch", default=scratch_bucket, help="Bucket holding the queue's claims")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir, and where each shard is downloaded")
@option("-w", "--wait", type=float, help="Seconds a task waits, after its pass, for shards other tasks claimed before failing (default: 2 × the lease)")
@option("-W", "--poll", default=60.0, type=float, help="Seconds between checks while waiting for other tasks' claims")
def build_cmd(bucket, gen, index, force, K, lease, mount, mem, only, threads, R, scratch, tmp, wait, poll) -> None:
    """Long members' roots and rollups per shard (a shared queue, biggest first) → `drill/long/{roots,rollups,roots-index,rollups-index}/s####.parquet`;
    a shard whose `rollups-index/` file exists is skipped. With `drill/aliases.parquet`, only canonical members
    are built (an alias reads its canonical's rows)."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    plan = read_json(f"gs://{bucket}/{prefix}/shards.json")
    t = _task(index)
    client = storage.Client()
    b = client.bucket(bucket)
    queue = Queue(client.bucket(scratch), prefix, "drill-build", t, lease, wait=wait, poll=poll)
    con = connect(threads, mem, tmp)
    con.execute(f"CREATE TABLE allm AS SELECT q, shard, rows FROM read_parquet({q(f'{mount}/{prefix}/catalog/members.parquet')})")
    if b.blob(f"{prefix}/{DRILL}/aliases.parquet").exists():
        con.execute(f"CREATE TABLE canon AS SELECT DISTINCT canonical FROM read_parquet({q(f'{mount}/{prefix}/{DRILL}/aliases.parquet')})")
    else:
        con.execute("CREATE TABLE canon AS SELECT q AS canonical FROM allm")
    keep = {int(x) for x in only.split(",")} if only else None
    t_start, n_done = monotonic(), 0
    shards = {f"s{s['i']:04d}": s for s in _queue_order(plan) if keep is None or s["i"] in keep}

    def work(name: str) -> None:
        nonlocal n_done
        s = shards[name]
        t0 = monotonic()
        con.execute(f"CREATE OR REPLACE TABLE mem AS SELECT q, rows FROM allm WHERE shard = {s['i']} AND q IN (SELECT canonical FROM canon)")
        n_members = con.execute("SELECT count(*) FROM mem").fetchone()[0]
        con.execute(f"DROP TABLE IF EXISTS rt; CREATE TABLE rt ({ROOT_COLS})")
        if n_members:
            src = Path(tmp) / f"sx-{name}.parquet"
            b.blob(f"{prefix}/sx/{name}.parquet").download_to_filename(str(src))
            err(f"build {name}: {s['rows']:,} rows, {n_members:,} members, downloaded in {monotonic() - t0:.1f}s")
            wheres = chunk_wheres(str(src))
            for k, where in enumerate(wheres):
                member_roots(con, sx_rows_sql(f"(SELECT * FROM read_parquet({q(str(src))}) WHERE {where})"), "mem", "rt", rule=gen_rule_at(bucket, gen))
                err(f"build {name}: chunk {k + 1}/{len(wheres)} in {monotonic() - t0:.1f}s")
            src.unlink()
        out = Path(tmp) / "drill"
        shutil.rmtree(out, ignore_errors=True)
        doc = {"shard": s["i"], "members": n_members, **build_roots(con, "rt", R, K, out, name)}
        _upload_dir(b, out / "roots", f"{prefix}/{DRILL}/long/roots")
        _upload_dir(b, out / "rollups", f"{prefix}/{DRILL}/long/rollups")
        _upload_dir(b, out / "roots-index", f"{prefix}/{DRILL}/long/roots-index")
        _upload_dir(b, out / "rollups-index", f"{prefix}/{DRILL}/long/rollups-index")
        shutil.rmtree(out)
        con.execute("DROP TABLE rt")
        n_done += 1
        doc["s"] = round(monotonic() - t0, 1)
        err(f"build {name}: {doc['roots']:,} roots ({doc['roots_bytes']:,} B), {doc['heavy_dirs']:,} heavy dirs, {doc['rollup_cells']:,} cells "
            f"in {doc['s']}s (task {t}: {n_done} shards in {monotonic() - t_start:.0f}s)")
        print(json.dumps(doc), flush=True)

    if force:
        for name in shards:
            work(name)
    else:
        queue.drain(list(shards), lambda name: b.blob(f"{prefix}/{DRILL}/long/rollups-index/{name}.parquet").exists(), work)


@cli.command("short-plan")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation (its `roots-measure/short/q/`)")
@option("-r", "--target-rows", default=150_000_000, type=int, help="Root rows per q-group")
def short_plan_cmd(bucket, gen, target_rows) -> None:
    """Cut the short literals into q-groups of about `-r` root rows (contiguous in code-point order) from the
    measurement → `drill/short-plan.json` (each group's first literal; printed too)."""
    import tempfile

    import duckdb
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    client = storage.Client()
    with tempfile.TemporaryDirectory() as d:
        for blob in client.list_blobs(bucket, prefix=f"{prefix}/{MEASURE}/short/q/"):
            blob.download_to_filename(f"{d}/{Path(blob.name).name}")
        per_q = duckdb.connect().execute(f"SELECT q, sum(rows)::BIGINT FROM read_parquet({q(d + '/*.parquet')}) GROUP BY q ORDER BY q").fetchall()
    groups, acc = [], 0
    for lit, n in per_q:
        if not groups or acc + n > target_rows:
            groups.append({"lo": lit, "rows": 0})
            acc = 0
        groups[-1]["rows"] += n
        acc += n
    doc = {"gen": gen, "target_rows": target_rows, "literals": len(per_q), "rows": sum(n for _, n in per_q), "groups": groups}
    client.bucket(bucket).blob(f"{prefix}/{DRILL}/short-plan.json").upload_from_string(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(doc, indent=1))


def _group_table(con, plan: dict) -> None:
    con.execute("CREATE OR REPLACE TABLE qg (lo VARCHAR, grp INTEGER)")
    con.executemany("INSERT INTO qg VALUES (?, ?)", [(g["lo"], k) for k, g in enumerate(plan["groups"])])


@cli.command("short-map")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation (its `cintervals/`, `drill/short-plan.json`)")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX): the subtree partition")
@option("-m", "--mount", required=True, help="Local mount of the bucket (unused: versions are downloaded)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--parts", default=32, type=IntRange(min=1), help="Subtree partitions (= tasks)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-P", "--pieces", default=16, type=IntRange(min=1), help="Passes per partition by hash(path), each written on its own (memory, disk)")
@option("-S", "--scratch", default=scratch_bucket, help="Bucket for the shuffle (an intermediate)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir, and where the versions are downloaded")
def short_map_cmd(bucket, gen, index, mount, mem, parts, threads, pieces, scratch, tmp) -> None:
    """One subtree partition's short-literal roots, split by q-group → the scratch bucket's
    `static-names/GEN/drill-short-map/g###/p###.parquet` (a partition's marker: `drill-short-map/done/p###`)."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    t = _task(index)
    client = storage.Client()
    b, sb = client.bucket(bucket), client.bucket(scratch)
    name = f"p{t:03d}"
    if sb.blob(f"{prefix}/drill-short-map/done/{name}").exists():
        err(f"short-map {name}: done")
        return
    plan = read_json(f"gs://{bucket}/{prefix}/{DRILL}/short-plan.json")
    t0 = monotonic()
    keys = sorted(x.name for x in client.list_blobs(bucket, prefix=f"{prefix}/cintervals/") if x.name.endswith(".parquet"))
    download(b, keys, Path(tmp) / "cintervals")
    con = connect(threads, mem, tmp)
    _group_table(con, plan)
    versions = f"SELECT * FROM read_parquet({q(str(Path(tmp) / 'cintervals' / '*.parquet'))}) WHERE {_partition(parts)} = {t}"
    n = 0
    for k in range(pieces):  # each piece written and uploaded on its own: a partition can hold billions of roots
        con.execute("DROP TABLE IF EXISTS rt")
        short_roots(con, f"SELECT * FROM ({versions}) WHERE hash(path) % {pieces} = {k}", "rt", rule=gen_rule_at(bucket, gen))
        part = Path(tmp) / "smap"
        shutil.rmtree(part, ignore_errors=True)
        con.execute(f"COPY (SELECT rt.*, qg.grp FROM rt ASOF JOIN qg ON rt.q >= qg.lo) TO {q(str(part))} (FORMAT parquet, PARTITION_BY (grp), COMPRESSION zstd)")
        n += con.execute("SELECT count(*) FROM rt").fetchone()[0]
        for d in sorted(part.glob("grp=*")):
            g = int(d.name.split("=")[1])
            for m, f in enumerate(sorted(d.glob("*.parquet"))):
                sb.blob(f"{prefix}/drill-short-map/g{g:03d}/{name}-{k}-{m}.parquet").upload_from_filename(str(f))
        shutil.rmtree(part)
        err(f"short-map {name}: piece {k + 1}/{pieces}, {n:,} roots in {monotonic() - t0:.1f}s")
    con.execute("DROP TABLE IF EXISTS rt")
    sb.blob(f"{prefix}/drill-short-map/done/{name}").upload_from_string(json.dumps({"rows": n}))
    shutil.rmtree(Path(tmp) / "cintervals")
    err(f"short-map {name}: {n:,} roots in {monotonic() - t0:.1f}s")


@cli.command("short-reduce")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX): the first q-group")
@option("-K", "--keep", "K", default=256, type=int, help="Children kept by name per heavy directory")
@option("-m", "--mount", required=True, help="Local mount of the bucket (the scratch bucket mounted beside it)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--stride", type=IntRange(min=1), help="Task i builds q-groups i, i + n, i + 2n, … (default: $BATCH_TASK_COUNT, else 1)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-R", "--read-rows", "R", default=100_000, type=int, help="Directories with more root rows under them get rollups")
@option("-S", "--scratch", default=scratch_bucket, help="Bucket holding the shuffle")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def short_reduce_cmd(bucket, gen, index, K, mount, mem, stride, threads, R, scratch, tmp) -> None:
    """Each of the task's q-groups: every partition's roots of its literals → `drill/short/{roots,rollups,roots-index,rollups-index}/g###.parquet`."""
    from google.cloud import storage

    import os

    prefix = f"{PREFIX}/{gen}"
    plan = read_json(f"gs://{bucket}/{prefix}/{DRILL}/short-plan.json")
    t = _task(index)
    stride = stride or int(os.environ.get("BATCH_TASK_COUNT", "1"))
    client = storage.Client()
    b = client.bucket(bucket)
    smount = str(Path(mount).parent / scratch)
    con = connect(threads, mem, tmp)
    for g in range(t, len(plan["groups"]), stride):
        name = f"g{g:03d}"
        if b.blob(f"{prefix}/{DRILL}/short/rollups-index/{name}.parquet").exists():
            err(f"short-reduce {name}: done")
            continue
        t0 = monotonic()
        src = Path(tmp) / "sred"
        shutil.rmtree(src, ignore_errors=True)
        keys = [x.name for x in client.list_blobs(scratch, prefix=f"{prefix}/drill-short-map/{name}/")]
        download(client.bucket(scratch), keys, src)
        con.execute(f"DROP TABLE IF EXISTS rt; CREATE TABLE rt AS SELECT q, depth, path, usr, vf, vt, size, n_files FROM read_parquet({q(str(src / '*.parquet'))})")
        out = Path(tmp) / "drill"
        shutil.rmtree(out, ignore_errors=True)
        doc = {"group": g, "files": len(keys), **build_roots(con, "rt", R, K, out, name)}
        for sub in ("roots", "rollups", "roots-index", "rollups-index"):
            _upload_dir(b, out / sub, f"{prefix}/{DRILL}/short/{sub}")
        shutil.rmtree(out)
        shutil.rmtree(src)
        doc["s"] = round(monotonic() - t0, 1)
        err(f"short-reduce {name}: {doc['roots']:,} roots, {doc['heavy_dirs']:,} heavy dirs, {doc['rollup_cells']:,} cells in {doc['s']}s")
        print(json.dumps(doc), flush=True)


def index_gaps(canonical_shards: set[int], short_groups: int | None, long_have: set[str], short_have: set[str]) -> list[str]:
    """What `index` would be missing: every shard holding a canonical member needs its `long/rollups-index/s####.parquet`
    (written last of a shard's files), and each of the short plan's q-groups its `short/rollups-index/g###.parquet`
    (`short_groups` None: no `short-plan.json`, itself missing). `*_have`: the file names present (`s0001.parquet`)."""
    gaps = [f"long/rollups-index/s{i:04d}.parquet" for i in sorted(canonical_shards) if f"s{i:04d}.parquet" not in long_have]
    if short_groups is None:
        gaps.append("short-plan.json")
    else:
        gaps += [f"short/rollups-index/g{g:03d}.parquet" for g in range(short_groups) if f"g{g:03d}.parquet" not in short_have]
    return gaps


def check_index_inputs(gen: str, gaps: list[str]) -> None:
    """Refuse to index an incomplete drill (a meta.json over part of it would serve silent holes)."""
    if gaps:
        shown = ", ".join(gaps[:20]) + (f", … ({len(gaps)} in all)" if len(gaps) > 20 else "")
        raise SystemExit(f"drill index {gen}: refusing: {len(gaps)} build outputs missing under drill/: {shown}")


def canonical_shards(members: pa.Table, aliases: pa.Table | None) -> set[int]:
    """The shards holding a canonical member (`members`: `q, shard`; without `aliases` every member is canonical)."""
    canon = None if aliases is None else set(aliases.column("canonical").to_pylist())
    return {sh for qq, sh in zip(members.column("q").to_pylist(), members.column("shard").to_pylist()) if canon is None or qq in canon}


@cli.command("index")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-K", "--keep", "K", default=256, type=int, help="The build's -K (recorded)")
@option("-m", "--mount", help="Local mount of the bucket (unused; the Batch driver passes it)")
@option("-R", "--read-rows", "R", default=100_000, type=int, help="The build's -R (recorded)")
@option("-T", "--tmp", default="/stage/tmp", help="Where the index files are written before upload")
def index_cmd(bucket, gen, K, mount, R, tmp) -> None:
    """Concatenate the per-file group indexes into `drill/{long,short}-{roots,rollups}-index.parquet` (`file`
    relative to `drill/`, sorted `(q_min, k_min)`, `IDX_RG`-entry row groups) with its top
    `drill/…-index.top.parquet` (`write_index_levels`), and write `drill/meta.json`; prints it."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}/{DRILL}"
    client = storage.Client()
    b = client.bucket(bucket)

    def table(key: str) -> pa.Table | None:
        blob = b.blob(key)
        return pq.read_table(pa.BufferReader(blob.download_as_bytes())) if blob.exists() else None

    members = table(f"{PREFIX}/{gen}/catalog/members.parquet")
    if members is None:
        raise SystemExit(f"drill index {gen}: no catalog/members.parquet")
    plan = b.blob(f"{prefix}/short-plan.json")
    have = {kind: {Path(x.name).name for x in client.list_blobs(bucket, prefix=f"{prefix}/{kind}/rollups-index/")} for kind in ("long", "short")}
    check_index_inputs(gen, index_gaps(canonical_shards(members, table(f"{prefix}/aliases.parquet")),
                                       len(json.loads(plan.download_as_bytes())["groups"]) if plan.exists() else None, have["long"], have["short"]))
    meta: dict = {"gen": gen, "R": R, "K": K, "rg": ROOT_RG, "idx_rg": IDX_RG, "dispatch_rows": R + 2 * ROOT_RG}
    for kind in ("long", "short"):
        for sub in ("roots", "rollups"):
            tabs = []
            for blob in sorted(client.list_blobs(bucket, prefix=f"{prefix}/{kind}/{sub}-index/"), key=lambda x: x.name):
                t = pq.read_table(pa.BufferReader(blob.download_as_bytes()))
                tabs.append(t.set_column(0, "file", pa.array([f"{kind}/{f}" for f in t.column("file").to_pylist()], pa.string())))
            t = pa.concat_tables(tabs).sort_by([("q_min", "ascending"), ("k_min", "ascending")]) if tabs else None
            if t is None:
                continue
            his = list(zip(t.column("q_max").to_pylist(), t.column("k_max").to_pylist()))
            los = list(zip(t.column("q_min").to_pylist(), t.column("k_min").to_pylist()))
            if any(his[i] > los[i + 1] for i in range(len(los) - 1)):
                raise RuntimeError(f"{kind} {sub}: row groups overlap")
            local = Path(tmp) / f"{kind}-{sub}-index.parquet"
            local.parent.mkdir(parents=True, exist_ok=True)
            top = write_index_levels(t, local)
            top_local = Path(tmp) / f"{kind}-{sub}-index.top.parquet"
            pq.write_table(top, top_local, compression=CODEC)
            for f in (local, top_local):
                blob = b.blob(f"{prefix}/{f.name}")
                blob.chunk_size = 64 << 20
                blob.upload_from_filename(str(f))
            files = {f for f in t.column("file").to_pylist()}
            nbytes = sum(int(x.size) for x in client.list_blobs(bucket, prefix=f"{prefix}/{kind}/{sub}/"))
            meta[f"{kind}_{sub}"] = {"files": len(files), "row_groups": t.num_rows, "rows": int(pa.compute.sum(t.column("rows")).as_py()),
                                     "bytes": nbytes, "index_bytes": local.stat().st_size, "index_row_groups": top.num_rows,
                                     "top_bytes": top_local.stat().st_size}
    b.blob(f"{prefix}/meta.json").upload_from_string(json.dumps(meta, indent=1) + "\n")
    print(json.dumps(meta, indent=1))


# ── Verification ───────────────────────────────────────────────────────────


def brute_view_sql(src: str, version: int, cases: str, rule: HexRule | None = None) -> str:
    """Per (case, child of its P): Σ size, n_files over one scan's rows (`src`, its `path` sort; v1 `b`/`o`)
    strictly under P, at depth ≥ 1, whose lowercase name the term occurs in (`hex_runs`, under `rule`) and whose
    lowercase parent it does not — the first-hit rule straight from the scan. `cases`: a table of `(term, P)`."""
    size, n = ("size", "n_files") if version == 2 else ("b", "o")
    return f"""WITH c AS (SELECT term, P, split_part(P, '/', 1) AS bkt FROM {cases}),
        m AS (SELECT t.term, x.path, x.bkt, x.sz, x.nf
              FROM (SELECT path, split_part(path, '/', 1) AS bkt, {NAME} AS l, {PARENT} AS par, {size} AS sz, {n} AS nf
                    FROM read_parquet({q(src)}) WHERE depth >= 2) AS x, (SELECT DISTINCT term FROM c) AS t
              WHERE {occurs_sql('x.l', 't.term', rule)} AND NOT {occurs_sql('x.par', 't.term', rule)})
        SELECT c.term, c.P, split_part(substring(m.path, length(c.P) + 2), '/', 1) AS child, sum(m.sz)::BIGINT AS b, sum(m.nf)::BIGINT AS o
        FROM m JOIN c ON m.term = c.term AND m.bkt = c.bkt AND starts_with(m.path, c.P || '/') GROUP BY ALL"""


def _cases(path: str) -> list[tuple[str, str]]:
    from .static_names import read_text

    return [(d["q"], d["P"]) for d in map(json.loads, read_text(path).splitlines()) if d]


#: Above this many children a reference case lists only the drill's kept children (and the exact total).
BRUTE_CHILDREN = 200_000


@cli.command("drill-brute")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-c", "--cases", "cases_file", required=True, help="JSON lines `{q, P}` (a path or gs:// URL)")
@option("-d", "--date", "dates", multiple=True, required=True, help="Scan date; repeat (task i answers the i-th)")
@option("-g", "--gen", required=True, help="Generation (its `scans.json`; answers go to `verify/drill-brute/`)")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-k", "--kept", "kept_file", help="`drill-query` answers (a path or gs:// URL): the children each rollup case names")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-O", "--out", "out_key", help="Bucket key to write (default `<gen>/verify/drill-brute/<date>.jsonl`)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-S", "--scans", "scans_json", help="A `scans.json` holding the date (default the generation's; a run's for a date past it)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def drill_brute_cmd(bucket, cases_file, dates, gen, index, kept_file, mount, mem, out_key, threads, scans_json, tmp) -> None:
    """Reference drill views by brute force over one date's scan file → `verify/drill-brute/<date>.jsonl`
    (`{date, q, P, total: [bytes, objects], n: children, children: {child: [bytes, objects]}}`, nonzero
    children; a case with more than `BRUTE_CHILDREN` lists only the children `-k` names for it)."""
    from google.cloud import storage

    from .static_names import read_text

    prefix = f"{PREFIX}/{gen}"
    date = dates[_task(index)]
    scan = next(s for s in read_json(scans_json or f"gs://{bucket}/{prefix}/scans.json")["scans"] if s["id"] == date)
    cases = sorted({(t.lower(), P) for t, P in _cases(cases_file)})
    con = connect(threads, mem, tmp)
    con.execute("CREATE TABLE cases (term VARCHAR, P VARCHAR)")
    con.executemany("INSERT INTO cases VALUES (?, ?)", cases)
    con.execute("CREATE TABLE kept (term VARCHAR, P VARCHAR, child VARCHAR)")
    if kept_file:
        rows = []
        for line in read_text(kept_file).splitlines():
            if line.startswith("{"):
                d = json.loads(line)
                if d["source"] == "rollup":
                    rows += [(d["q"], d["P"], c) for c in sorted({c for a in d["answers"].values() for c in a} | set(d.get("kept") or []))]
        if rows:
            con.executemany("INSERT INTO kept VALUES (?, ?, ?)", rows)
    t0 = monotonic()
    con.execute(f"CREATE TABLE v AS SELECT * FROM ({brute_view_sql(f'{mount}/{scan['src']}', scan['version'], 'cases', gen_rule_at(bucket, gen))}) WHERE b <> 0 OR o <> 0")
    tot = {(t, P): (int(b_), int(o_), int(n)) for t, P, b_, o_, n in con.execute(
        "SELECT term, P, sum(b)::BIGINT, sum(o)::BIGINT, count(*) FROM v GROUP BY term, P").fetchall()}
    got: dict[tuple[str, str], dict] = {c: {} for c in cases}
    for term, P, child, b_, o_ in con.execute(f"""SELECT v.term, v.P, v.child, v.b, v.o FROM v
            JOIN (SELECT term, P, count(*) AS n FROM v GROUP BY term, P) AS c USING (term, P)
            WHERE c.n <= {BRUTE_CHILDREN} OR EXISTS (SELECT 1 FROM kept AS k WHERE k.term = v.term AND k.P = v.P AND k.child = v.child)""").fetchall():
        got[(term, P)][child] = [int(b_), int(o_)]
    body = "".join(json.dumps({"date": date, "q": t, "P": P, "total": list(tot.get((t, P), (0, 0, 0))[:2]), "n": tot.get((t, P), (0, 0, 0))[2],
                               "children": dict(sorted(got[(t, P)].items()))}) + "\n" for t, P in cases)
    storage.Client().bucket(bucket).blob(out_key or f"{prefix}/verify/drill-brute/{date}.jsonl").upload_from_string(body)
    err(f"drill-brute {date}: {len(cases)} cases in {monotonic() - t0:.1f}s")


def gcs_drill(bucket: str, gen: str, kind: str) -> Drill:
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}/{DRILL}"
    b = storage.Client().bucket(bucket)
    meta = json.loads(b.blob(f"{prefix}/meta.json").download_as_bytes())
    blobs: dict[str, object] = {}

    def blob(file: str):
        if file not in blobs:
            blobs[file] = b.get_blob(f"{prefix}/{file}")
        return blobs[file]

    def fetch(file: str, lo: int, hi: int) -> bytes:
        return blob(file).download_as_bytes(start=lo, end=hi - 1)

    def size_of(file: str) -> int:
        return int(blob(file).size)

    top = {sub: pq.read_table(pa.BufferReader(b.blob(f"{prefix}/{kind}-{sub}-index.top.parquet").download_as_bytes())) for sub in ("roots", "rollups")}
    aliases = {}
    if kind == "long" and b.blob(f"{prefix}/aliases.parquet").exists():
        a = pq.read_table(pa.BufferReader(b.blob(f"{prefix}/aliases.parquet").download_as_bytes()), columns=["q", "canonical"])
        aliases = {k: v for k, v in zip(a.column("q").to_pylist(), a.column("canonical").to_pylist()) if k != v}
    files = {sub: GroupFile(None, fetch, size_of, top=top[sub], index_file=f"{kind}-{sub}-index.parquet") for sub in ("roots", "rollups")}
    return Drill(files["roots"], files["rollups"], meta["R"], meta["rg"], aliases, gen_rule_at(bucket, gen))


@cli.command("drill-query")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-c", "--cases", "cases_file", required=True, help="JSON lines `{q, P}`")
@option("-d", "--date", "dates", multiple=True, required=True, help="Scan date; repeat")
@option("-g", "--gen", required=True, help="Generation")
def drill_query_cmd(bucket, cases_file, dates, gen) -> None:
    """Answer drill cases from the roots/rollups (the Worker's logic, over GCS): one JSON line per case."""
    drills: dict[str, Drill] = {}
    for t, P in _cases(cases_file):
        kind = "short" if len(t) <= 2 else "long"
        if kind not in drills:
            drills[kind] = gcs_drill(bucket, gen, kind)
        t0 = monotonic()
        out = drills[kind].view(t, P, list(dates))
        out["s"] = round(monotonic() - t0, 3)
        print(json.dumps(out), flush=True)


@cli.command("drill-verify")
@argument("ref_jsonl")
@argument("answers_jsonl")
def drill_verify_cmd(ref_jsonl, answers_jsonl) -> None:
    """Compare `drill-query` answers with `drill-brute` per (term, P, date): a roots answer must equal the
    reference child for child; a rollup answer's kept children must equal theirs and its remainder the
    reference total less them. JSON report; exit 1 on any difference."""
    ref, totals, partial = {}, {}, set()
    for line in Path(ref_jsonl).read_text().splitlines():
        if line.startswith("{"):
            d = json.loads(line)
            key = (d["q"], d["P"], d["date"])
            ref[key], totals[key] = d["children"], d["total"]
            if d["n"] > BRUTE_CHILDREN:
                partial.add(key)
    pairs, diffs, src_n, io = 0, {}, {}, []
    for line in Path(answers_jsonl).read_text().splitlines():
        if not line.startswith("{"):
            continue
        d = json.loads(line)
        src_n[d["source"]] = src_n.get(d["source"], 0) + 1
        if d["source"] == "plain":
            continue
        io.append({"q": d["q"], "P": d["P"], "source": d["source"], "rows_read": d["io"]["rows_read"], "bytes": d["io"]["bytes"], "s": d["s"]})
        for date, got in d["answers"].items():
            exp = ref.get((d["q"], d["P"], date))
            if exp is None:
                continue
            pairs += 1
            key = (d["q"], d["P"], date)
            if d["source"] == "roots":
                ok = key not in partial and got == exp
            else:  # kept children child for child (absent = zero on the date), remainder = the total less them
                kept = {c: exp.get(c, [0, 0]) for c in d["kept"]}
                rest = [totals[key][i] - sum(v[i] for v in kept.values()) for i in (0, 1)]
                ok = got == {c: v for c, v in kept.items() if v != [0, 0]} and d["rest"][date] == rest
            if not ok:
                diffs[f"{d['q']} {d['P']} {date}"] = {"got": got, "rest": d.get("rest", {}).get(date), "ref": exp}
    report = {"pairs": pairs, "equal": pairs - len(diffs), "by_source": src_n, "max_rows_read": max((x["rows_read"] for x in io), default=0),
              "max_bytes": max((x["bytes"] for x in io), default=0), "diff": dict(list(diffs.items())[:20]), "io": io}
    print(json.dumps(report, indent=1))
    if diffs or not pairs:
        raise SystemExit(1)


@cli.command("drill-cases")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation (its `roots-measure/`)")
@option("-n", "--per-depth", default=2, type=int, help="Directories per (term, depth, heavy|light)")
@option("-R", "--read-rows", "R", default=100_000, type=int, help="Heavy: more root rows under the directory than this")
@option("-s", "--shards", help="Long terms: only members of these shards (comma-separated); default all")
@option("-t", "--terms-file", help="A file of literals, one per line (in addition to TERMS)")
@argument("terms", nargs=-1)
def drill_cases_cmd(bucket, gen, per_depth, R, shards, terms_file, terms) -> None:
    """Verification cases `{q, P}` (JSON lines) for TERMS from the measurement's directory tables: per term and
    depth, the `-n` largest heavy directories (rollup reads) and the `-n` largest light ones with at least 10K
    roots (roots reads), deterministic. Short terms read `roots-measure/short/`."""
    import tempfile

    import duckdb
    from google.cloud import storage

    from .static_names import read_text

    prefix = f"{PREFIX}/{gen}/{MEASURE}"
    client = storage.Client()
    want = {int(x) for x in shards.split(",")} if shards else None
    terms = list(terms) + ([x for x in read_text(terms_file).splitlines() if x.strip()] if terms_file else [])
    with tempfile.TemporaryDirectory() as d:
        for blob in client.list_blobs(bucket, prefix=f"{prefix}/dirs/"):
            if want is None or int(Path(blob.name).stem[1:]) in want:
                blob.download_to_filename(f"{d}/{Path(blob.name).name}")
        (Path(d) / "short").mkdir()
        for sub in ("dirs", "top"):
            for blob in client.list_blobs(bucket, prefix=f"{prefix}/short/{sub}/"):
                blob.download_to_filename(f"{d}/short/{sub}-{Path(blob.name).name}")
        con = duckdb.connect()
        con.execute(f"""CREATE TABLE dl AS SELECT q, k, dir, "rows" FROM read_parquet({q(d + '/s*.parquet')})
            UNION ALL SELECT q, k, dir, "rows" FROM read_parquet({q(d + '/short/dirs-*.parquet')})
            UNION ALL SELECT q, 1, dir, sum("rows")::BIGINT FROM read_parquet({q(d + '/short/top-*.parquet')}) GROUP BY q, dir""")
        con.execute("CREATE TABLE t (q VARCHAR)")
        con.executemany("INSERT INTO t VALUES (?)", [(x.lower(),) for x in terms])
        rows = con.execute(f"""SELECT q, dir FROM (
                SELECT q, dir, row_number() OVER (PARTITION BY q, k, "rows" > {R} ORDER BY "rows" DESC, dir) AS r
                FROM dl SEMI JOIN t USING (q) WHERE "rows" >= 10000) WHERE r <= {per_depth} ORDER BY q, dir""").fetchall()
    for t_, P in rows:
        print(json.dumps({"q": t_, "P": P}))


if __name__ == "__main__":
    cli()
