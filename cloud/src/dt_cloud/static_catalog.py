"""The static name catalog (specs/architecture/static-name-search.md, "Static catalog"): precomputed
per-bucket first-hit answers, on every scan of a generation, for exactly the literals the static suffix
reader should not read itself, so no `/names` query needs the query box.

**Membership.** A literal `q` is a member iff

- it has one or two characters (every such literal present in any name, ever: the suffix shards hold
  suffixes of three or more characters only), or
- its range in the suffix shards — the suffix rows starting with `q`, i.e. Σ over versions of `q`'s
  occurrences in the version's lowercase name — holds more than `V` rows.

Ranges only grow as scans are appended (a version is never removed; a closure only sets its `vt`), so
membership is monotone: once a member, always one. A member's every prefix and every substring of three
or more characters is a member too (their ranges contain its range's rows, one for one), so the members
of a shard are found in one pass of its sorted suffixes, level by level (`census_sql`): the prefixes of
length `L` with more than `V` rows, then, among their rows only, those of length `L + 1`. Any `V` is
allowed; `census` keeps every prefix down to a lower floor so `V` can be chosen afterwards.

**Answers.** For a member and each scan date `D`, per bucket: Σ `size`, `n_files` over its first hits live
on `D` — the version is live (`vf ≤ D < vt`), at depth ≥ 1, its lowercase name contains `q` and its
lowercase parent path does not (`static_names.Reader.answer`). A version's rows in
`q`'s range are its occurrences of `q`; the first occurrence's row (`instr(name, q)` = the row's position)
carries it, so no dedup state is needed. Each first hit is two events — `+(size, n_files)` at `vf`,
`−(size, n_files)` at `vt` (none while open) — summed per `(q, bucket, t)` and accumulated in time order:
the cells are `(q, bucket, vf, b, o)`, one per change. A
date's answer for a bucket is its newest cell at or before the date (none: zero).

**Layout** (`gs://<data bucket>/static-names/<gen>/catalog/`, copied to R2): `cells.parquet`, sorted
`(q, bucket, vf)` in code-point order, `CELL_RG`-row groups, every member led by a header row `(q, '',
0, rows, n)` (`rows` = its range rows, −1 for a one- or two-character literal; `n` = its cells), so a
member with no first hits is still found; `index.parquet`, per row group `(rg, q_min, q_max, offset,
length, rows)` (the row group's exact first and last `q`, its byte span); `meta.json`.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from time import monotonic, time

import pyarrow as pa
import pyarrow.parquet as pq
from click import IntRange, argument, group, option

from .hex_runs import HexRule, first_sql, grams_sql, kept_sql, occurs_sql, rule_json
from .static_profile import data_bucket, scratch_bucket
from .static_names import (
    CINTERVAL_SCHEMA, CODEC, NAME, OPEN, PREFIX, SX_SCHEMA, _batches, _download, _task, connect, err, gen_rule, q,
    read_json, scan_epoch, upload_tree, write_sorted,
)

CELL_RG = 4096
NODE_SCHEMA = pa.schema([
    pa.field("q", pa.string(), nullable=False),
    pa.field("rows", pa.int64(), nullable=False),
    pa.field("ubytes", pa.int64(), nullable=False),
])
CELL_SCHEMA = pa.schema([
    pa.field("q", pa.string(), nullable=False),
    pa.field("bucket", pa.string(), nullable=False),
    pa.field("vf", pa.int64(), nullable=False),
    pa.field("b", pa.int64(), nullable=False),
    pa.field("o", pa.int64(), nullable=False),
])
EVENT_SCHEMA = pa.schema([
    pa.field("q", pa.string(), nullable=False),
    pa.field("bucket", pa.string(), nullable=False),
    pa.field("t", pa.int64(), nullable=False),
    pa.field("db", pa.int64(), nullable=False),
    pa.field("dn", pa.int64(), nullable=False),
])
INDEX_SCHEMA = pa.schema([
    pa.field("rg", pa.int32(), nullable=False),
    pa.field("q_min", pa.string(), nullable=False),
    pa.field("q_max", pa.string(), nullable=False),
    pa.field("offset", pa.int64(), nullable=False),
    pa.field("length", pa.int64(), nullable=False),
    pa.field("rows", pa.int32(), nullable=False),
    pa.field("chunks", pa.list_(pa.int64()), nullable=False),
])
#: A suffix row's decoded size, roughly: its strings plus the fixed-width columns (the decode cost the Worker pays).
UBYTES = "(strlen(s) + strlen(path) + strlen(usr) + 41)"
#: The lowercase parent (everything before the last `/`; `''` without one). A string cut, ~8× faster than the
#: regex `^(.*)/[^/]*$` it equals, which stays only for paths with a newline: RE2's `.` doesn't match one, so the
#: regex yields `''` there, and those rows keep that answer.
PARENT = ("lower(CASE WHEN contains(path, chr(10)) THEN regexp_extract(path, '^(.*)/[^/]*$', 1) WHEN NOT contains(path, '/') THEN '' "
          "ELSE left(path, length(path) - length(string_split(path, '/')[-1]) - 1) END)")
BUCKET = "split_part(path, '/', 1)"


# ── Census: every prefix's range rows ──────────────────────────────────────


def census(con, src: str, floor_rows: int, floor_bytes: int | None = None) -> pa.Table:
    """Every prefix (≥ 3 characters) of `src`'s suffixes (a table or `read_parquet` of sx rows) whose range
    holds at least `floor_rows` rows (or `floor_bytes` decoded bytes): `NODE_SCHEMA`, sorted by `q`. Level
    by level: a prefix of length `L + 1` can reach the floor only inside a prefix of length `L` that does."""
    keep = f"n >= {floor_rows}" + (f" OR ub >= {floor_bytes}" if floor_bytes else "")
    con.execute("DROP TABLE IF EXISTS cx; DROP TABLE IF EXISTS cnodes")
    con.execute(f"CREATE TABLE cx AS SELECT s, {UBYTES}::BIGINT AS ub FROM {src}")
    con.execute("CREATE TABLE cnodes (q VARCHAR, rows BIGINT, ubytes BIGINT)")
    L = 3
    while True:
        con.execute("DROP TABLE IF EXISTS lvl")
        con.execute(f"""CREATE TABLE lvl AS SELECT left(s, {L}) AS q, count(*)::BIGINT AS n, sum(ub)::BIGINT AS ub
            FROM cx WHERE length(s) >= {L} GROUP BY q HAVING {keep}""")
        if con.execute("SELECT count(*) FROM lvl").fetchone()[0] == 0:
            break
        con.execute("INSERT INTO cnodes SELECT q, n, ub FROM lvl")
        con.execute(f"CREATE OR REPLACE TABLE cx AS SELECT s, ub FROM cx SEMI JOIN lvl ON left(cx.s, {L}) = lvl.q WHERE length(s) > {L}")
        L += 1
    out = con.execute("SELECT q, rows, ubytes FROM cnodes ORDER BY q").to_arrow_table().cast(NODE_SCHEMA)
    con.execute("DROP TABLE IF EXISTS cx; DROP TABLE IF EXISTS cnodes; DROP TABLE IF EXISTS lvl")
    return out


# ── Answers: first-hit events per member ───────────────────────────────────


def first_hit_sql(L: int, rule: HexRule | None) -> str:
    """A suffix row `(s, l, par)` is the first hit of `left(s, L)`: it starts at the literal's first occurrence in the
    name `l` (under `rule`: the first one the rule keeps) and the literal does not occur in the parent `par`."""
    x = f"left(s, {L})"
    return f"{first_sql('l', x, rule)} = length(l) - length(s) + 1 AND NOT {occurs_sql('par', x, rule)}"


def member_events(con, rows_sql: str, members: str, fresh: bool = True, rule: HexRule | None = None) -> str:
    """Events `(q, bucket, t, db, dn)` of `members`' (a table with `q`, ≥ 3 characters) first hits among
    `rows_sql`'s suffix rows `(s, depth, path, t0, t1, size, n_files, sign)`: `sign·(size, n_files)` at `t0`,
    and its negation at `t1` unless `t1` is NULL or OPEN (a delta's close record is a row with `t0` = the
    scan, `sign` −1 and `t1` NULL). Level by level over lengths, carrying only the rows whose prefix of the
    level's length is a prefix of some member. Adds the summed events to table `ev` (created afresh unless
    `fresh` is false: events are additive, so a shard can be fed in chunks); returns its name."""
    con.execute("DROP TABLE IF EXISTS mx; DROP TABLE IF EXISTS mpre")
    if fresh:
        con.execute("DROP TABLE IF EXISTS ev")
    con.execute("CREATE TABLE IF NOT EXISTS ev (q VARCHAR, bucket VARCHAR, t BIGINT, db HUGEINT, dn HUGEINT)")
    top = con.execute(f"SELECT max(length(q)) FROM {members}").fetchone()[0]
    if not top:
        return "ev"
    con.execute(f"""CREATE TABLE mpre AS SELECT DISTINCT left(q, L) AS p, L, max(length(q) = L) OVER (PARTITION BY left(q, L)) AS member
        FROM (SELECT q, unnest(range(3, length(q) + 1)) AS L FROM {members})""")
    con.execute(f"""CREATE TABLE mx AS SELECT s, depth, path, t0, t1, size, n_files, sign, {NAME} AS l, {PARENT} AS par
        FROM ({rows_sql}) WHERE depth >= 1""")
    for L in range(3, top + 1):
        con.execute("DROP TABLE IF EXISTS ml")
        con.execute(f"CREATE TABLE ml AS SELECT p, bool_or(member) AS member FROM mpre WHERE L = {L} GROUP BY p")
        con.execute(f"CREATE OR REPLACE TABLE mx AS SELECT mx.* FROM mx SEMI JOIN ml ON left(mx.s, {L}) = ml.p")
        if con.execute("SELECT count(*) FROM mx").fetchone()[0] == 0:
            break
        hit = f"""SELECT left(s, {L}) AS q, {BUCKET} AS bucket, t0, t1, sign * size AS sz, sign * n_files AS nf FROM mx
            SEMI JOIN (SELECT p FROM ml WHERE member) AS mm ON left(mx.s, {L}) = mm.p
            WHERE {first_hit_sql(L, rule)}"""
        con.execute(f"""INSERT INTO ev SELECT q, bucket, t, sum(db), sum(dn) FROM (
                SELECT q, bucket, t0 AS t, sz AS db, nf AS dn FROM ({hit})
                UNION ALL SELECT q, bucket, t1 AS t, -sz, -nf FROM ({hit}) WHERE t1 IS NOT NULL AND t1 <> {OPEN}
            ) GROUP BY q, bucket, t""")
        con.execute(f"DELETE FROM mx WHERE length(s) <= {L}")
    con.execute("DROP TABLE IF EXISTS mx; DROP TABLE IF EXISTS ml; DROP TABLE IF EXISTS mpre")
    return "ev"


def short_events(con, versions_sql: str, rule: HexRule | None = None) -> str:
    """Events of every one- and two-character literal's first hits among `versions_sql`'s rows `(depth, path,
    t0, t1, size, n_files, sign)` (as `member_events`), into table `sev`: each distinct character and
    character pair of the lowercase name that the lowercase parent does not contain."""
    con.execute("DROP TABLE IF EXISTS sev")
    hit = f"""SELECT g AS q, bucket, t0, t1, sz, nf FROM (
            SELECT unnest({grams_sql('l', rule)}) AS g, par, bucket, t0, t1, sz, nf
            FROM (SELECT {NAME} AS l, {PARENT} AS par, {BUCKET} AS bucket, t0, t1, sign * size AS sz, sign * n_files AS nf
                  FROM ({versions_sql}) WHERE depth >= 1)
        ) WHERE NOT {occurs_sql('par', 'g', rule)}"""
    con.execute(f"""CREATE TABLE sev AS SELECT q, bucket, t, sum(db)::HUGEINT AS db, sum(dn)::HUGEINT AS dn FROM (
            SELECT q, bucket, t0 AS t, sz AS db, nf AS dn FROM ({hit})
            UNION ALL SELECT q, bucket, t1 AS t, -sz, -nf FROM ({hit}) WHERE t1 IS NOT NULL AND t1 <> {OPEN}
        ) GROUP BY q, bucket, t""")
    return "sev"


def short_vocab_sql(versions_sql: str, rule: HexRule | None = None) -> str:
    """Every one- and two-character literal occurring (under `rule`) in the versions' lowercase names (depth ≥ 1), with
    how many versions it occurs in: the short members, including those whose every occurrence is under a match."""
    return f"""SELECT g AS q, count(*)::BIGINT AS n FROM (
            SELECT unnest({grams_sql('l', rule)}) AS g
            FROM (SELECT {NAME} AS l FROM ({versions_sql}) WHERE depth >= 1)) GROUP BY g"""


def cells_sql(events: str, members: str) -> str:
    """Cells from summed events: per `(q, bucket)` the running totals at each event time where they change,
    each member preceded by its header row `(q, '', 0, rows, n)` (`members`: `q, rows`)."""
    return f"""WITH e AS (SELECT q, bucket, t, sum(db) AS db, sum(dn) AS dn FROM {events} GROUP BY q, bucket, t HAVING sum(db) <> 0 OR sum(dn) <> 0),
        c AS (SELECT q, bucket, t AS vf,
                sum(db) OVER (PARTITION BY q, bucket ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::BIGINT AS b,
                sum(dn) OVER (PARTITION BY q, bucket ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::BIGINT AS o FROM e),
        n AS (SELECT q, count(*) AS n FROM c GROUP BY q)
        SELECT m.q, '' AS bucket, 0::BIGINT AS vf, m.rows::BIGINT AS b, coalesce(n.n, 0)::BIGINT AS o FROM {members} AS m LEFT JOIN n USING (q)
        UNION ALL SELECT c.q, c.bucket, c.vf, c.b, c.o FROM c SEMI JOIN {members} AS m USING (q)"""


def sx_rows_sql(src: str) -> str:
    """Suffix-shard rows as `member_events` input: intervals, epoch seconds."""
    return f"""SELECT s, depth, path, epoch(vf)::BIGINT AS t0, epoch(vt)::BIGINT AS t1, size, n_files, 1::BIGINT AS sign FROM {src}"""


#: Suffix rows fed to `member_events` at a time (events are additive across chunks).
CHUNK_ROWS = 1 << 24


def shard_cells(con, sx: str, members: str, dst: Path, chunk_rows: int = CHUNK_ROWS, log: str = "",
                rule: HexRule | None = None) -> tuple[int, dict]:
    """One shard's members' cells (`members`: `q, rows`, all within the shard) → `dst`, sorted; returns the
    cell count and per-phase seconds. `sx` is a local file (sorted `s, path, …`), read in chunks of about
    `chunk_rows` rows cut at row-group boundaries as `(s, path)` ranges (not `s` alone: one suffix can fill a
    shard, e.g. `ccess` from `_SUCCESS` markers), with `s` bounds DuckDB prunes the other row groups by, so
    memory is bounded by the chunk; events are additive, so any partition of the rows gives the same cells.
    `log` prefixes a progress line per chunk."""
    pf = pq.ParquetFile(sx)
    cuts, n = [], 0
    for g in range(pf.metadata.num_row_groups):
        if n >= chunk_rows:
            first = pf.read_row_group(g, columns=["s", "path"]).slice(0, 1).to_pylist()[0]
            cuts.append((first["s"], first["path"]))
            n = 0
        n += pf.metadata.row_group(g).num_rows
    bounds = [None, *cuts, None]
    con.execute("DROP TABLE IF EXISTS ev")
    con.execute("CREATE TABLE ev (q VARCHAR, bucket VARCHAR, t BIGINT, db HUGEINT, dn HUGEINT)")
    t0 = monotonic()
    for k, (lo, hi) in enumerate(zip(bounds, bounds[1:])):
        conds = []
        if lo is not None:
            conds.append(f"s >= {q(lo[0])} AND (s > {q(lo[0])} OR path >= {q(lo[1])})")
        if hi is not None:
            conds.append(f"s <= {q(hi[0])} AND (s < {q(hi[0])} OR path < {q(hi[1])})")
        where = " AND ".join(conds) or "true"
        member_events(con, sx_rows_sql(f"(SELECT * FROM read_parquet({q(sx)}) WHERE {where})"), members, fresh=False, rule=rule)
        if log:
            err(f"{log}: chunk {k + 1}/{len(bounds) - 1} in {monotonic() - t0:.1f}s")
    t1 = monotonic()
    rows = write_sorted(_batches(con, f"SELECT * FROM ({cells_sql('ev', members)}) ORDER BY q, bucket, vf"), dst, CELL_SCHEMA, 1 << 16,
                        dictionary=["bucket"])
    return rows, {"events": round(t1 - t0, 1), "cells": round(monotonic() - t1, 1)}


def range_short(con, versions: str, dst: Path, name: str, rule: HexRule | None = None) -> None:
    """One key range's short-literal events and vocabulary (`versions`: an intervals or cintervals file)
    → `dst/{events,vocab}/<name>.parquet`."""
    vs = f"SELECT depth, path, vf AS t0, vt AS t1, size, n_files, 1::BIGINT AS sign FROM read_parquet({q(versions)})"
    short_events(con, vs, rule)
    (dst / "events").mkdir(parents=True, exist_ok=True)
    (dst / "vocab").mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY (SELECT q, bucket, t, db::BIGINT AS db, dn::BIGINT AS dn FROM sev ORDER BY q, bucket, t) TO {q(str(dst / 'events' / f'{name}.parquet'))} (FORMAT parquet, COMPRESSION zstd)")
    con.execute(f"COPY ({short_vocab_sql(vs, rule)} ORDER BY q) TO {q(str(dst / 'vocab' / f'{name}.parquet'))} (FORMAT parquet, COMPRESSION zstd)")


def assemble(con, short_events_glob: str, short_vocab_glob: str, shard_cells_glob: str, out: Path) -> dict:
    """The short literals' cells merged with every shard's into `out/cells.parquet` (sorted, `CELL_RG`-row
    groups) and `out/index.parquet`; returns the counts."""
    con.execute("DROP TABLE IF EXISTS sev; DROP TABLE IF EXISTS smem")
    con.execute(f"""CREATE TABLE sev AS SELECT q, bucket, t, sum(db)::HUGEINT AS db, sum(dn)::HUGEINT AS dn
        FROM read_parquet({q(short_events_glob)}) GROUP BY q, bucket, t""")
    con.execute(f"CREATE TABLE smem AS SELECT q, -1::BIGINT AS rows FROM read_parquet({q(short_vocab_glob)}) GROUP BY q")
    out.mkdir(parents=True, exist_ok=True)
    shard = f"read_parquet({q(shard_cells_glob)})"
    sql = f"SELECT * FROM (SELECT * FROM ({cells_sql('sev', 'smem')}) UNION ALL SELECT * FROM {shard}) ORDER BY q, bucket, vf"
    rows, index = write_cells(_batches(con, sql), out / "cells.parquet")
    pq.write_table(index, out / "index.parquet", compression=CODEC)
    n_short, n_long, top = con.execute(f"""SELECT count(*) FILTER (WHERE length(q) <= 2), count(*) FILTER (WHERE length(q) > 2), max(length(q))
        FROM (SELECT q FROM smem UNION ALL SELECT q FROM {shard} WHERE bucket = '')""").fetchone()
    return {"cells_rows": rows, "row_groups": index.num_rows, "members_short": n_short, "members_long": n_long, "max_len": top,
            "bytes": (out / "cells.parquet").stat().st_size, "index_bytes": (out / "index.parquet").stat().st_size, "cell_rg": CELL_RG}


# ── Appending a scan ───────────────────────────────────────────────────────


def expand_sql(versions_sql: str, rule: HexRule | None = None) -> str:
    """Suffix rows (≥ 3 characters, depth ≥ 1; under `rule`, the positions it keeps) of `versions_sql`'s rows `(depth,
    path, t0, t1, size, n_files, sign)`, as `member_events` input."""
    return f"""SELECT substring(l, p) AS s, depth, path, t0, t1, size, n_files, sign FROM (
            SELECT depth, path, t0, t1, size, n_files, sign, l, unnest(generate_series(1, length(l) - 2)) AS p
            FROM (SELECT *, {NAME} AS l FROM ({versions_sql}) WHERE depth >= 1) WHERE length(l) >= 3) WHERE {kept_sql('l', 'p', rule)}"""


def delta_versions_sql(files: list[str], point: bool) -> str:
    """Coalesced delta rows (`cdelta/<date>/*.parquet`) as versions: an opened one `+` at its `vf`, a closed
    one `−` at its `vt` (both point events: `t1` NULL) when `point`; else just the opened ones as intervals
    `[vf, vt)` (their rows, for counting)."""
    lst = "[" + ", ".join(q(f) for f in files) + "]"
    if point:
        return f"""SELECT depth, path, CASE WHEN op = 1 THEN vf ELSE vt END AS t0, NULL::BIGINT AS t1, size, n_files, op::BIGINT AS sign
            FROM read_parquet({lst})"""
    return f"SELECT depth, path, vf AS t0, vt AS t1, size, n_files, 1::BIGINT AS sign FROM read_parquet({lst}) WHERE op = 1"


class BaseShards:
    """A base generation's suffix shards, read through its sidecar: upper bounds and exact counts of a
    prefix's range rows, and the range's rows themselves (whole row groups)."""

    def __init__(self, root: str, sidecar: pa.Table):
        side = sidecar.sort_by([("file", "ascending"), ("rg", "ascending")]).to_pylist()
        self.root, self.groups = root, side
        self.mins = [g["s_min"] for g in side]
        self.maxs = [g["s_max"] for g in side]
        acc, cum = 0, [0]
        for g in side:
            acc += g["rows"]
            cum.append(acc)
        self.cum = cum
        self.files: dict[str, pq.ParquetFile] = {}

    def span(self, key: str) -> tuple[int, int]:
        from bisect import bisect_left

        a = bisect_left(self.maxs, key)
        return a, max(a, bisect_left(self.mins, key + "\U0010ffff"))

    def upper(self, key: str) -> int:
        """Rows of the row groups that can hold suffixes starting with `key` (≥ its range's rows)."""
        a, b = self.span(key)
        return self.cum[b] - self.cum[a]

    def _pf(self, file: str) -> pq.ParquetFile:
        if file not in self.files:
            self.files[file] = pq.ParquetFile(f"{self.root}/{file}")
        return self.files[file]

    def exact(self, key: str) -> int:
        """The range's rows: the groups strictly inside it whole, the two edge groups decoded (`s` only)."""
        a, b = self.span(key)
        if a == b:
            return 0
        n = self.cum[b - 1] - self.cum[a + 1] if b - a > 2 else 0
        for g in sorted({a, b - 1}):
            info = self.groups[g]
            col = self._pf(info["file"]).read_row_group(info["rg"], columns=["s"]).column("s").to_pylist()
            n += sum(1 for x in col if x.startswith(key))
        return n

    def rows(self, keys: list[str]) -> pa.Table:
        """Every row of the row groups spanning any of `keys`' ranges (each group once), sx columns."""
        want: set[int] = set()
        for k in keys:
            a, b = self.span(k)
            want.update(range(a, b))
        tables = []
        for g in sorted(want):
            info = self.groups[g]
            tables.append(self._pf(info["file"]).read_row_group(info["rg"], columns=["s", "depth", "path", "vf", "vt", "size", "n_files"]))
        return pa.concat_tables(tables) if tables else pa.table({f.name: pa.array([], f.type) for f in SX_SCHEMA if f.name != "usr"})


def _insert(con, table: str, rows: list[tuple]) -> None:
    if rows:
        con.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(rows[0]))})", rows)


def append(con, *, prev: Path, base: BaseShards, deltas: list[list[str]], V: int, out: Path, rule: HexRule | None = None) -> dict:
    """The catalog of `base` plus the scans of `deltas` (each a scan's coalesced delta files, oldest first), from
    `prev` (`cells.parquet`: the catalog of `base` plus all but the last delta) — equal to rebuilding it.

    Membership: a literal of three or more characters is a member iff its range rows — `base`'s plus every
    delta's opened versions' — exceed `V`. Every prefix whose rows could (`base.upper` + delta rows > `V`) is
    found level by level over the deltas' opened suffix rows; for those not already members, `base.exact`
    decides. A new member's cells are built from its whole history (`base`'s rows in its range as intervals,
    every delta's opens and closes as point events); an existing one's get the last scan's events added;
    every one- and two-character literal gets the last scan's events, and those first seen there join."""
    t0 = monotonic()
    last = deltas[-1]
    # previous state: members (header rows) and their cells as events
    con.execute("DROP TABLE IF EXISTS pc")
    con.execute(f"CREATE TABLE pc AS SELECT * FROM read_parquet({q(str(prev / 'cells.parquet'))})")
    con.execute("CREATE OR REPLACE TABLE pmem AS SELECT q, b AS rows FROM pc WHERE bucket = ''")
    con.execute("""CREATE OR REPLACE TABLE pev AS SELECT q, bucket, vf AS t, (b - coalesce(lag(b) OVER w, 0))::HUGEINT AS db,
            (o - coalesce(lag(o) OVER w, 0))::HUGEINT AS dn FROM pc WHERE bucket <> '' WINDOW w AS (PARTITION BY q, bucket ORDER BY vf)""")
    # every delta's opened suffix rows: who could cross V
    all_files = [f for d in deltas for f in d]
    con.execute(f"CREATE OR REPLACE TABLE xo AS SELECT s, t0 FROM ({expand_sql(delta_versions_sql(all_files, point=False), rule)})")
    D = con.execute(f"SELECT max(CASE WHEN op = 1 THEN vf ELSE vt END) FROM read_parquet([{', '.join(q(f) for f in last)}])").fetchone()[0]
    cand: list[tuple[str, int, int]] = []  # (q, rows over all deltas, rows of the last)
    L = 3
    while True:
        lvl = con.execute(f"""SELECT left(s, {L}) AS q, count(*), count(*) FILTER (WHERE t0 = {D}) FROM xo WHERE length(s) >= {L}
            GROUP BY q""").fetchall()
        keep = [(k, n, nl) for k, n, nl in lvl if base.upper(k) + n > V]
        if not keep:
            break
        cand += keep
        con.execute("CREATE OR REPLACE TABLE ck (q VARCHAR)")
        con.executemany("INSERT INTO ck VALUES (?)", [(k,) for k, _, _ in keep])
        con.execute(f"CREATE OR REPLACE TABLE xo AS SELECT s, t0 FROM xo SEMI JOIN ck ON left(xo.s, {L}) = ck.q WHERE length(s) > {L}")
        L += 1
    prev_rows = dict(con.execute("SELECT q, rows FROM pmem").fetchall())
    new_members: list[tuple[str, int]] = []
    rows_now: dict[str, int] = {}
    for k, n, nl in cand:
        if k in prev_rows:
            rows_now[k] = prev_rows[k] + nl
        else:
            r = base.exact(k) + n
            if r > V:
                new_members.append((k, r))
    con.execute("CREATE OR REPLACE TABLE nmem (q VARCHAR, rows BIGINT)")
    _insert(con, "nmem", new_members)
    con.execute("CREATE OR REPLACE TABLE lmem (q VARCHAR, rows BIGINT)")
    _insert(con, "lmem", [(k, r) for k, r in prev_rows.items() if r >= 0 and k not in rows_now and len(k) >= 3])
    _insert(con, "lmem", list(rows_now.items()))
    # the last scan's events for existing long members
    point_last = delta_versions_sql(last, point=True)
    member_events(con, expand_sql(point_last, rule), "lmem", rule=rule)
    con.execute("CREATE OR REPLACE TABLE lev AS SELECT * FROM ev")
    # new long members: their whole history
    if new_members:
        con.register("base_rows", base.rows([k for k, _ in new_members]))
        hist = f"""{sx_rows_sql('base_rows')} UNION ALL {expand_sql(delta_versions_sql(all_files, point=True), rule)}"""
        member_events(con, hist, "nmem", rule=rule)
        con.unregister("base_rows")
    else:
        con.execute("DELETE FROM ev")
    con.execute("CREATE OR REPLACE TABLE nev AS SELECT * FROM ev")
    # short literals: the last scan's events, and any first seen
    short_events(con, point_last, rule)
    con.execute(f"""CREATE OR REPLACE TABLE amem AS SELECT q, rows FROM lmem UNION ALL SELECT q, rows FROM nmem
        UNION ALL SELECT q, -1::BIGINT FROM (SELECT q FROM pmem WHERE rows = -1 UNION SELECT q FROM ({short_vocab_sql(delta_versions_sql(last, point=False), rule)}))""")
    con.execute("""CREATE OR REPLACE TABLE aev AS SELECT * FROM pev UNION ALL SELECT * FROM lev UNION ALL SELECT * FROM nev
        UNION ALL SELECT q, bucket, t, db, dn FROM sev""")
    out.mkdir(parents=True, exist_ok=True)
    rows, index = write_cells(_batches(con, f"SELECT * FROM ({cells_sql('aev', 'amem')}) ORDER BY q, bucket, vf"), out / "cells.parquet")
    pq.write_table(index, out / "index.parquet", compression=CODEC)
    doc = {"scan": int(D), "cells_rows": rows, "candidates": len(cand), "new_members": len(new_members),
           "members": con.execute("SELECT count(*) FROM amem").fetchone()[0], "s": round(monotonic() - t0, 1)}
    for t in ("pc", "pmem", "pev", "xo", "ck", "nmem", "lmem", "lev", "nev", "amem", "aev", "ev", "sev"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    return doc


# ── Reading the catalog (the Worker's logic) ───────────────────────────────


class Catalog:
    """Membership and answers from `cells.parquet` + `index.parquet`: the row groups whose `[q_min, q_max]`
    holds `q` (contiguous) are one ranged read; `q` is a member iff a header row `(q, '')` is there."""

    def __init__(self, fetch, size: int, index: pa.Table):
        from .static_names import Spans

        self.fetch, self.size, self.Spans = fetch, size, Spans
        idx = index.sort_by("rg").to_pylist()
        self.groups = idx
        self.mins = [g["q_min"] for g in idx]
        self.maxs = [g["q_max"] for g in idx]
        self.foot: bytes | None = None

    def footer(self) -> tuple[int, bytes]:
        if self.foot is None:
            tail = self.fetch(self.size - 8, self.size)
            flen = int.from_bytes(tail[:4], "little")
            self.foot = self.fetch(max(0, self.size - max(8 + flen, 1 << 16)), self.size)
        return self.size - len(self.foot), self.foot

    def rows(self, key: str) -> list[dict] | None:
        """`key`'s cells (header first), or None when it is not a member."""
        from bisect import bisect_left, bisect_right

        a = bisect_left(self.maxs, key)
        b = bisect_right(self.mins, key)
        if a >= b:
            return None
        sel = self.groups[a:b]
        lo, hi = sel[0]["offset"], sel[-1]["offset"] + sel[-1]["length"]
        data = self.fetch(lo, hi)
        start, foot = self.footer()
        pf = pq.ParquetFile(self.Spans(self.size, [(start, foot), (lo, data)]))
        tab = pf.read_row_groups([g["rg"] for g in sel]).to_pylist()
        mine = [r for r in tab if r["q"] == key]
        if not mine or mine[0]["bucket"] != "":
            return None
        return mine

    def answer(self, term: str, dates: list[str]) -> dict | None:
        """Per date `{bucket: [bytes, objects]}` (nonzero buckets), or None for a non-member."""
        key = term.lower()
        cells = self.rows(key)
        if cells is None:
            return None
        head, body = cells[0], cells[1:]
        out = {}
        for d in dates:
            D = scan_epoch(d)
            cur: dict[str, tuple[int, int]] = {}
            for c in body:
                if c["vf"] <= D:
                    cur[c["bucket"]] = (c["b"], c["o"])
            out[d] = {k: list(v) for k, v in sorted(cur.items()) if v != (0, 0)}
        return {"q": key, "rows": head["b"], "cells": head["o"], "answers": out}


def gcs_catalog(bucket: str, prefix: str) -> Catalog:
    from google.cloud import storage

    b = storage.Client().bucket(bucket)
    index = pq.read_table(_download(b, f"{prefix}/catalog/index.parquet"))
    blob = b.get_blob(f"{prefix}/catalog/cells.parquet")

    def fetch(lo: int, hi: int) -> bytes:
        return blob.download_as_bytes(start=lo, end=hi - 1)

    return Catalog(fetch, int(blob.size), index)


def write_cells(batches, out: Path) -> tuple[int, pa.Table]:
    """Write sorted cell batches as `CELL_RG`-row groups and cut their index: per row group its exact first and
    last `q`, byte span, rows, and per column `(data_page_offset, total_compressed_size, dictionary_page_offset
    or 0)` — what a reader needs to decode a group without the footer."""
    stats: list[tuple[str, str]] = []

    def on_group(g: pa.Table) -> None:
        col = g.column("q")
        stats.append((col[0].as_py(), col[g.num_rows - 1].as_py()))

    rows = write_sorted(batches, out, CELL_SCHEMA, CELL_RG, on_group=on_group, dictionary=["bucket"])
    md = pq.ParquetFile(out).metadata
    if md.num_row_groups != len(stats):
        raise RuntimeError(f"{out}: {md.num_row_groups} row groups, {len(stats)} recorded")
    cols = {k: [] for k in INDEX_SCHEMA.names}
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
        for k, v in zip(INDEX_SCHEMA.names, (g, stats[g][0], stats[g][1], min(starts), max(ends) - min(starts), rg.num_rows, chunks)):
            cols[k].append(v)
    return rows, pa.table(cols, schema=INDEX_SCHEMA)


# ── CLI ────────────────────────────────────────────────────────────────────


@group("catalog")
def cli() -> None:
    """The static name catalog: census, members, answers and assembly (specs/architecture/static-name-search.md)."""


def _shards_for_task(plan: dict, t: int) -> list[dict]:
    return [plan["shards"][i] for i in plan["tasks"][t]["shards"]]


@cli.command("census")
@option("-B", "--floor-bytes", type=int, help="Also keep prefixes with at least this many decoded bytes")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-f", "--floor", "floor_rows", default=50_000, type=int, help="Keep prefixes with at least this many range rows")
@option("-g", "--gen", required=True, help="Generation (its `shards.json` and `sx/`)")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX): the plan's task group")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def census_cmd(floor_bytes, bucket, floor_rows, gen, index, mount, mem, threads, tmp) -> None:
    """Every suffix prefix at or above the floor, per shard of one task group → `catalog/census/s####.parquet`."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    plan = read_json(f"gs://{bucket}/{prefix}/shards.json")
    t = _task(index)
    b = storage.Client().bucket(bucket)
    con = connect(threads, mem, tmp)
    for s in _shards_for_task(plan, t):
        name = f"s{s['i']:04d}"
        key = f"{prefix}/catalog/census/{name}.parquet"
        if b.blob(key).exists():
            err(f"census {name}: done")
            continue
        t0 = monotonic()
        nodes = census(con, f"read_parquet({q(f'{mount}/{prefix}/sx/{name}.parquet')})", floor_rows, floor_bytes)
        sink = pa.BufferOutputStream()
        pq.write_table(nodes, sink, compression=CODEC)
        b.blob(key).upload_from_string(sink.getvalue().to_pybytes())
        doc = {"shard": s["i"], "nodes": nodes.num_rows, "s": round(monotonic() - t0, 1)}
        err(f"census {name}: {nodes.num_rows:,} prefixes in {doc['s']}s")
        print(json.dumps(doc), flush=True)


@cli.command("members")
@option("-B", "--max-bytes", type=int, help="Also admit prefixes with more than this many decoded bytes")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-V", "--max-rows", required=True, type=int, help="Admit prefixes with more than this many range rows")
def members_cmd(max_bytes, bucket, gen, max_rows) -> None:
    """Cut the members (≥ 3 characters) from the census → `catalog/members.parquet` `(q, shard, rows, ubytes)`,
    and print the membership summary (JSON)."""
    import tempfile

    import duckdb
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    client = storage.Client()
    with tempfile.TemporaryDirectory() as d:
        n = 0
        for blob in client.list_blobs(bucket, prefix=f"{prefix}/catalog/census/"):
            blob.download_to_filename(f"{d}/{Path(blob.name).name}")
            n += 1
        con = duckdb.connect()
        cond = f"rows > {max_rows}" + (f" OR ubytes > {max_bytes}" if max_bytes else "")
        con.execute(f"""CREATE TABLE m AS SELECT q, regexp_extract(filename, 's(\\d+)\\.parquet', 1)::INTEGER AS shard, rows, ubytes
            FROM read_parquet({q(d + '/*.parquet')}, filename = true) WHERE {cond} ORDER BY q""")
        floor = con.execute(f"SELECT min(rows) FROM read_parquet({q(d + '/*.parquet')})").fetchone()[0]
        summary = {"gen": gen, "max_rows": max_rows, "max_bytes": max_bytes, "census_shards": n, "census_floor_seen": floor,
                   "members": con.execute("SELECT count(*) FROM m").fetchone()[0],
                   "by_length": dict(con.execute("SELECT length(q), count(*) FROM m GROUP BY 1 ORDER BY 1").fetchall()),
                   "rows": con.execute("SELECT sum(rows) FROM m").fetchone()[0]}
        out = Path(d) / "members.parquet"
        con.execute(f"COPY m TO {q(str(out))} (FORMAT parquet, COMPRESSION zstd)")
        client.bucket(bucket).blob(f"{prefix}/catalog/members.parquet").upload_from_filename(str(out))
    summary["by_length"] = {str(k): v for k, v in summary["by_length"].items()}
    client.bucket(bucket).blob(f"{prefix}/catalog/members.json").upload_from_string(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))


@cli.command("answers")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX); without -Q, the plan's task group")
@option("-l", "--lease", default=1800, type=int, help="With -Q: seconds after which another task may take over a claimed, unfinished shard")
@option("-m", "--mount", required=True, help="Local mount of the bucket (for `catalog/members.parquet`; shards are downloaded to -T)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-Q", "--queue", is_flag=True, help="Take shards from a shared queue (claims in the scratch bucket) instead of the plan's task group")
@option("-S", "--scratch", default=scratch_bucket, help="With -Q: bucket holding the claims")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir, and where each shard is downloaded")
def answers_cmd(bucket, gen, index, lease, mount, mem, threads, queue, scratch, tmp) -> None:
    """Members' cells, per shard → `catalog/cells/s####.parquet` (sorted). A shard whose output exists is
    skipped, so a rerun (or a second job) resumes. With -Q, every task walks all shards, latest-in-plan first
    (so a queue job racing a plan-ordered one meets it in the middle), claiming each by creating
    `claims/answers/s####` in the scratch bucket; a claim older than -l is taken over (a preempted task's)."""
    from google.api_core.exceptions import NotFound, PreconditionFailed
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    plan = read_json(f"gs://{bucket}/{prefix}/shards.json")
    rule = gen_rule(read_json(f"gs://{bucket}/{prefix}/scans.json"))
    t = _task(index)
    client = storage.Client()
    b = client.bucket(bucket)
    sb = client.bucket(scratch)
    con = connect(threads, mem, tmp)
    con.execute(f"CREATE TABLE allm AS SELECT q, shard, rows FROM read_parquet({q(f'{mount}/{prefix}/catalog/members.parquet')})")
    out = Path(tmp) / "cells"
    out.mkdir(parents=True, exist_ok=True)
    if queue:
        pos = {i: k for g in plan["tasks"] for k, i in enumerate(g["shards"])}
        todo = sorted(plan["shards"], key=lambda s: (-pos[s["i"]], -s["rows"]))
    else:
        todo = _shards_for_task(plan, t)

    def claim(name: str) -> bool:
        blob = sb.blob(f"{prefix}/claims/answers/{name}")
        try:
            blob.upload_from_string(str(t), if_generation_match=0)
            return True
        except PreconditionFailed:
            pass
        try:
            blob.reload()
        except NotFound:
            return claim(name)
        if time() - blob.updated.timestamp() < lease:
            return False
        try:
            blob.upload_from_string(str(t), if_generation_match=blob.generation)
            err(f"answers {name}: taking over a claim {time() - blob.updated.timestamp():.0f}s old")
            return True
        except PreconditionFailed:
            return False

    t_start = monotonic()
    n_done = 0
    for s in todo:
        name = f"s{s['i']:04d}"
        key = f"{prefix}/catalog/cells/{name}.parquet"
        if b.blob(key).exists():
            continue
        if queue and not claim(name):
            continue
        t0 = monotonic()
        src = Path(tmp) / f"sx-{name}.parquet"
        b.blob(f"{prefix}/sx/{name}.parquet").download_to_filename(str(src))
        t_dl = monotonic() - t0
        con.execute(f"CREATE OR REPLACE TABLE mem AS SELECT q, rows FROM allm WHERE shard = {s['i']}")
        n_members = con.execute("SELECT count(*) FROM mem").fetchone()[0]
        err(f"answers {name}: {s['rows']:,} rows, {n_members:,} members, downloaded in {t_dl:.1f}s")
        dst = out / f"{name}.parquet"
        rows, phases = shard_cells(con, str(src), "mem", dst, log=f"answers {name}", rule=rule)
        src.unlink()
        t1 = monotonic()
        b.blob(key).upload_from_filename(str(dst))
        dst.unlink()
        n_done += 1
        doc = {"shard": s["i"], "rows": s["rows"], "members": n_members, "cells": rows, "s": round(monotonic() - t0, 1),
               "phases": {"download": round(t_dl, 1), **phases, "upload": round(monotonic() - t1, 1)}}
        err(f"answers {name}: {n_members:,} members, {rows:,} cells in {doc['s']}s {doc['phases']} "
            f"(task {t}: {n_done} shards in {monotonic() - t_start:.0f}s)")
        print(json.dumps(doc), flush=True)


@cli.command("short")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation (its versions: `cintervals/`, else `intervals/` of -I)")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-I", "--intervals-gen", help="Read `intervals/` of this generation instead of GEN's `cintervals/`")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-S", "--scratch", default=scratch_bucket, help="Bucket for the per-range events (an intermediate)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def short_cmd(bucket, gen, index, intervals_gen, mount, mem, per_task, threads, scratch, tmp) -> None:
    """One- and two-character literals' first-hit events and vocabulary per key range → the scratch bucket's
    `static-names/GEN/catalog-short/{events,vocab}/r####.parquet`."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    rule = gen_rule(read_json(f"gs://{bucket}/{prefix}/scans.json"))
    ranges = read_json(f"gs://{bucket}/{PREFIX}/{intervals_gen or gen}/ranges.json")
    t = _task(index)
    todo = list(range(t * per_task, min((t + 1) * per_task, ranges["k"])))
    sb = storage.Client().bucket(scratch)
    con = connect(threads, mem, tmp)
    for i in todo:
        name = f"r{i:04d}"
        if sb.blob(f"{prefix}/catalog-short/vocab/{name}.parquet").exists():
            err(f"short {name}: done")
            continue
        t0 = monotonic()
        # -I: that generation's coalesced versions when it has them (a derived generation's source), else its intervals.
        ig = f"{mount}/{PREFIX}/{intervals_gen}"
        src = ((f"{ig}/cintervals/{name}.parquet" if Path(f"{ig}/cintervals/{name}.parquet").exists() else f"{ig}/intervals/{name}.parquet")
               if intervals_gen else f"{mount}/{prefix}/cintervals/{name}.parquet")
        outp = Path(tmp) / f"short-{i}"
        range_short(con, src, outp, name, rule)
        sb.blob(f"{prefix}/catalog-short/events/{name}.parquet").upload_from_filename(str(outp / "events" / f"{name}.parquet"))
        sb.blob(f"{prefix}/catalog-short/vocab/{name}.parquet").upload_from_filename(str(outp / "vocab" / f"{name}.parquet"))
        shutil.rmtree(outp)
        err(f"short {name}: {monotonic() - t0:.1f}s")


@cli.command("assemble")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-S", "--scratch", default=scratch_bucket, help="Bucket holding the short literals' per-range events")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def assemble_cmd(bucket, gen, mount, mem, threads, scratch, tmp) -> None:
    """Merge the short literals' events into cells and every shard's cells into `catalog/cells.parquet`
    (sorted, `CELL_RG`-row groups), cut `catalog/index.parquet`, write `catalog/meta.json`."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    con = connect(threads, mem, tmp)
    smount = str(Path(mount).parent / scratch)
    out = Path(tmp) / "catalog"
    t0 = monotonic()
    meta = {"gen": gen, **assemble(con, f"{smount}/{prefix}/catalog-short/events/*.parquet", f"{smount}/{prefix}/catalog-short/vocab/*.parquet",
                                   f"{mount}/{prefix}/catalog/cells/*.parquet", out), "s": 0}
    meta["s"] = round(monotonic() - t0, 1)
    members = read_json(f"gs://{bucket}/{prefix}/catalog/members.json") if storage.Client().bucket(bucket).blob(f"{prefix}/catalog/members.json").exists() else None
    if members:
        meta["membership"] = {k: members[k] for k in ("max_rows", "max_bytes") if k in members}
    # The generation's hex-run rule, which the readers follow (every tier's `catalog/meta.json` carries it).
    meta.update(rule_json(gen_rule(read_json(f"gs://{bucket}/{prefix}/scans.json"))))
    (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    upload_tree(out, bucket, f"{prefix}/catalog")
    shutil.rmtree(out)
    print(json.dumps(meta, indent=1))


def brute_sql(src: str, version: int, terms: str, rule: HexRule | None = None) -> str:
    """Per (term, bucket): Σ size, n_files over one scan's rows (`src`, its `path` sort; v1 `b`/`o`) at depth ≥ 1
    whose lowercase name the term occurs in (`hex_runs`, under `rule`) and whose lowercase parent it does not — the
    first-hit rule straight from the scan, no versions, no index. `terms`: a table of `term`."""
    size, n = ("size", "n_files") if version == 2 else ("b", "o")
    return f"""SELECT t.term, split_part(x.path, '/', 1) AS bucket, sum(x.sz)::BIGINT AS b, sum(x.nf)::BIGINT AS o
        FROM (SELECT path, {NAME} AS l, {PARENT} AS par, {size} AS sz, {n} AS nf FROM read_parquet({q(src)}) WHERE depth >= 1) AS x,
            {terms} AS t
        WHERE {occurs_sql('x.l', 't.term', rule)} AND NOT {occurs_sql('x.par', 't.term', rule)} GROUP BY ALL"""


@cli.command("brute")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-d", "--date", "dates", multiple=True, required=True, help="Scan date; repeat (task i answers the i-th)")
@option("-g", "--gen", required=True, help="Generation (its `scans.json` names each date's source; answers go to its `verify/brute/`)")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-t", "--terms-file", required=True, help="Literals, one per line (a path or gs:// URL)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def brute_cmd(bucket, dates, gen, index, mount, mem, threads, terms_file, tmp) -> None:
    """Reference answers by brute force over one date's scan file → `verify/brute/<date>.jsonl` (one line per
    term: `{date, q, buckets: {bucket: [bytes, objects]}}`, nonzero buckets, as `job/static-names.sh ch-answers`)."""
    from google.cloud import storage

    from .static_names import read_text

    prefix = f"{PREFIX}/{gen}"
    date = dates[_task(index)]
    scans = read_json(f"gs://{bucket}/{prefix}/scans.json")
    scan = next(s for s in scans["scans"] if s["id"] == date)
    terms = sorted({x.lower() for x in read_text(terms_file).splitlines() if x.strip()})
    con = connect(threads, mem, tmp)
    con.execute("CREATE TABLE terms (term VARCHAR)")
    con.executemany("INSERT INTO terms VALUES (?)", [(t,) for t in terms])
    t0 = monotonic()
    rows = con.execute(brute_sql(f"{mount}/{scan['src']}", scan["version"], "terms", gen_rule(scans))).fetchall()
    got: dict[str, dict] = {t: {} for t in terms}
    for term, bkt, b_, o_ in rows:
        if b_ or o_:
            got[term][bkt] = [int(b_), int(o_)]
    body = "".join(json.dumps({"date": date, "q": t, "buckets": dict(sorted(got[t].items()))}) + "\n" for t in terms)
    storage.Client().bucket(bucket).blob(f"{prefix}/verify/brute/{date}.jsonl").upload_from_string(body)
    err(f"brute {date}: {len(terms)} terms in {monotonic() - t0:.1f}s")


@cli.command("query")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-d", "--date", "dates", multiple=True, required=True, help="Scan date; repeat")
@option("-g", "--gen", required=True, help="Generation")
@option("-t", "--terms-file", help="A file of literals, one per line (in addition to TERMS)")
@option("-x", "--static", "also_static", is_flag=True, help="Answer non-members from the suffix shards (the Worker's dispatch)")
@argument("terms", nargs=-1)
def query_cmd(bucket, dates, gen, terms_file, also_static, terms) -> None:
    """Answer literals from the catalog (one JSON line each: `source` catalog | static | none, per-date
    `{bucket: [bytes, objects]}`); with -x a non-member is answered by the suffix reader."""
    from .static_names import gcs_reader, read_text

    lits = list(terms) + ([x for x in read_text(terms_file).splitlines() if x.strip()] if terms_file else [])
    cat = gcs_catalog(bucket, f"{PREFIX}/{gen}")
    reader = gcs_reader(bucket, f"{PREFIX}/{gen}", gen_rule(read_json(f"gs://{bucket}/{PREFIX}/{gen}/scans.json"))) if also_static else None
    for t in lits:
        t0 = monotonic()
        out = cat.answer(t, list(dates))
        if out is not None:
            out["source"] = "catalog"
        elif reader is not None and len(t) >= 3:
            r = reader.answer(t, list(dates))
            out = {"q": r["q"], "source": "static", "io": r["io"], "rows_matching_range": r["rows_matching"], "answers": r["answers"]}
        else:
            out = {"q": t.lower(), "source": "none" if len(t) >= 3 else "absent-short",
                   "answers": {d: {} for d in dates} if len(t) < 3 else None}
        out["s"] = round(monotonic() - t0, 3)
        print(json.dumps(out), flush=True)


@cli.command("verify")
@option("-V", "--max-rows", required=True, type=int, help="The catalog's V: a statically answered literal's range must hold at most this many rows")
@argument("ref_jsonl")
@argument("answers_jsonl")
def verify_cmd(max_rows, ref_jsonl, answers_jsonl) -> None:
    """Compare `query -x` answers with reference answers (`brute` or `ch-answers` JSON lines): per (term, date)
    the nonzero buckets' bytes and objects must be equal; every term answered statically must have a range of at
    most V rows, and every term of one or two characters must come from the catalog (or be absent from every
    name). Prints a JSON report; exit 1 on any failure."""
    ref: dict[tuple[str, str], dict] = {}
    for line in Path(ref_jsonl).read_text().splitlines():
        if line.startswith("{"):
            d = json.loads(line)
            ref[(d["q"], d["date"])] = {k: list(v) for k, v in d["buckets"].items() if v[0] or v[1]}
    pairs, diffs, terms, bound = 0, {}, {}, []
    for line in Path(answers_jsonl).read_text().splitlines():
        if not line.startswith("{"):
            continue
        d = json.loads(line)
        src = d["source"]
        terms[d["q"]] = {"source": src, **({"rows": d["rows"], "cells": d["cells"]} if src == "catalog" else {}),
                         **({"rows_read": d["io"]["rows_read"], "groups": d["io"]["groups"], "bytes": d["io"]["bytes"]} if src == "static" else {}),
                         "s": d.get("s")}
        if src == "static":
            n = d["rows_matching_range"]
            terms[d["q"]]["range_rows"] = n
            if n > max_rows:
                bound.append(d["q"])
        if src not in ("catalog", "static", "absent-short"):
            bound.append(d["q"])
        for date, buckets in (d["answers"] or {}).items():
            if (d["q"], date) not in ref:
                continue
            pairs += 1
            mine = {k: v for k, v in buckets.items() if v[0] or v[1]}
            if mine != ref[(d["q"], date)]:
                diffs[f"{d['q']} {date}"] = {"answer": mine, "ref": ref[(d["q"], date)]}
    missing = sorted(f"{t} {dt}" for t, dt in ref if t not in terms)
    by_source: dict[str, int] = {}
    for v in terms.values():
        by_source[v["source"]] = by_source.get(v["source"], 0) + 1
    report = {"pairs": pairs, "equal": pairs - len(diffs), "terms": len(terms), "by_source": by_source, "max_rows": max_rows,
              "static_max_range_rows": max((v.get("range_rows", 0) for v in terms.values()), default=0), "bound_violations": bound,
              "missing": missing, "diff": diffs, "per_term": terms}
    print(json.dumps(report, indent=1))
    if diffs or missing or bound or not pairs:
        raise SystemExit(1)


@cli.command("census-check")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-f", "--floor", "floor_rows", default=50_000, type=int, help="The census floor")
@option("-g", "--gen", required=True, help="Generation (its `catalog/census/` and `chist/`)")
def census_check_cmd(bucket, floor_rows, gen) -> None:
    """The census's three-character prefixes (counted from the sorted shards) against the coalesce stage's
    histograms (counted from the versions, independently): the same prefixes at or above the floor, the same
    rows. Prints a JSON report; exit 1 on any difference."""
    import tempfile

    import duckdb
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    client = storage.Client()
    with tempfile.TemporaryDirectory() as d:
        for sub in ("catalog/census", "chist"):
            (Path(d) / sub).mkdir(parents=True)
            for blob in client.list_blobs(bucket, prefix=f"{prefix}/{sub}/"):
                blob.download_to_filename(str(Path(d) / sub / Path(blob.name).name))
        con = duckdb.connect()
        census_rows = dict(con.execute(f"SELECT q, rows FROM read_parquet({q(d + '/catalog/census/*.parquet')}) WHERE length(q) = 3").fetchall())
        hist = dict(con.execute(f"SELECT p3, sum(n)::BIGINT FROM read_parquet({q(d + '/chist/*.parquet')}) GROUP BY p3 HAVING sum(n) >= {floor_rows}").fetchall())
        nodes = con.execute(f"SELECT count(*), max(length(q)), sum(rows) FILTER (WHERE length(q) = 3) FROM read_parquet({q(d + '/catalog/census/*.parquet')})").fetchone()
    diff = {k: [census_rows.get(k), hist.get(k)] for k in sorted(set(census_rows) | set(hist)) if census_rows.get(k) != hist.get(k)}
    report = {"gen": gen, "floor": floor_rows, "prefixes3": len(hist), "nodes": nodes[0], "max_len": nodes[1], "rows3": nodes[2],
              "equal": not diff, "diff": dict(list(diff.items())[:50])}
    print(json.dumps(report, indent=1))
    if diff:
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
