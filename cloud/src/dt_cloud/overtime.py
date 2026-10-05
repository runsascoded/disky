"""The cross-scan **over-time index** — one path's `(b, o)` at every scan, laid
out so the size-over-time chart reads a whole line in one contiguous range
instead of one point read per scan generation (specs/obs-axis-indexing.md
Phase 1).

The SCD-2 interval consolidation is **pyrmts' generic multiscan kernel**
(`pyrmts_engine.multiscan_duckdb.consolidate_parquet_duckdb` — vectorized
gaps-and-islands over `read_parquet`, out-of-core). cw supplies only the glue:
roll each scan's `path` sort to `(depth, path)` totals of its dir rows (owner
slices summed away — over-time is `(path)`-granular; object rows, present since
the path store, are not series) plus a depth-0 fleet-root row, and hand
the per-scan shards to pyrmts keyed as a `Pyramid(binCol='depth', dims=[path],
metrics=count(b,o))` — which yields exactly `(depth, path, b, o, __scan_lo,
__scan_hi)`. Interval bounds index the ordered scan list; storage is ~static
(real cw churn ~0.02 %/scan) so the whole history is ~one path-index's size, and
a path's line is 1–2 rows — the diff-index (Phase 2) reads the same runs.

Rows sorted `(depth, path, __scan_lo)` — a path's intervals contiguous, RG-pruned
by `path`. The ordered scan list rides an `over-time.scans.json` sidecar (the
D1-footer read path rebuilds row-group stats but not KV metadata). A depth-0
fleet-root row (`path=''`) makes the whole-store series a point lookup.
"""
from __future__ import annotations

import json
import sys
from functools import partial
from pathlib import Path

import duckdb
from pyrmts.types import Dim, Metric, Pyramid
from pyrmts_engine.multiscan_duckdb import consolidate_parquet_duckdb

from .index import ROW_GROUP_SIZE, duckdb_codec

err = partial(print, file=sys.stderr)

OVER_TIME_VARIANT = "over-time"
OVER_TIME_FILE = "over-time.parquet"
#: Scans per sealed multi-scan group (capped so each build is bounded-memory +
#: parallelizable, and a path's line reads ⌈N/K⌉ groups rather than one giant
#: monolith; the ≤K-scan tip is served by `/api/series`'s per-scan fallback).
#: 16 ≈ 4 days at the 6h scan cadence (8 at the earlier 12h) (specs/obs-axis-indexing.md Phase 1).
OVER_TIME_GROUP_SIZE = 16
#: The ordered scan list rides a sidecar JSON the CFW reader fetches — the
#: D1-footer read path rebuilds row-group stats but not KV metadata, so the
#: reader needs this to map `__scan_lo`/`__scan_hi` back to scan ids.
OVER_TIME_SCANS = "over-time.scans.json"
SCAN_LO = "__scan_lo"
SCAN_HI = "__scan_hi"


class _NoStore:
    """The multiscan kernel never touches storage; a stub satisfies `Pyramid`."""

    def head(self, k): return None
    def get(self, k): return None
    def put(self, k, d): pass
    def delete(self, k): pass
    def list(self, p): return []


def over_time_pyramid() -> Pyramid:
    """cw's over-time shape as a pyrmts `Pyramid`: key `(depth, path)`, state
    `(b, o)`. `binCol` is just the first key column here (no event-time axis);
    the `count` monoid gives a single state column named after the metric, so a
    scan shard is exactly `(depth, path, b, o)`."""
    return Pyramid(
        storage=_NoStore(),
        keyTemplate="",
        binCol="depth",
        dims=[Dim("path", "string")],
        metrics=[Metric("b", "count"), Metric("o", "count")],
        tiers=[],
    )


def _roll_sql(con: "duckdb.DuckDBPyConnection", path_index: str) -> str:
    """One scan's `path` sort → `(depth, path, b, o)` totals + a depth-0
    fleet-root row (sum of the buckets at depth 1). A store generation
    (`kind` on every row; specs/path-store.md §4.5) contributes its dir rows'
    `size` / `n_files`; a pre-store index (dir rows only, wire names) its
    `b` / `o` — the same rows in, the same intervals out, across the switch."""
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path_index}') LIMIT 0").fetchall()}
    if "kind" in cols:
        b, o, where = "size", "n_files", " WHERE kind = 'dir'"
    else:
        b, o, where = "b", "o", ""
    return (
        f"WITH t AS (SELECT depth, path, sum({b})::BIGINT AS b, sum({o})::BIGINT AS o "
        f"FROM read_parquet('{path_index}'){where} GROUP BY depth, path) "
        f"SELECT * FROM t UNION ALL SELECT 0, '', sum(b)::BIGINT, sum(o)::BIGINT FROM t WHERE depth = 1"
    )


def write_over_time_index(
    scans: list[tuple[str, str | Path]],
    out_dir: str | Path,
    *,
    mem: str = "8GB",
    threads: int = 8,
    tmp_dir: str | Path | None = None,
    con: "duckdb.DuckDBPyConnection | None" = None,
) -> dict:
    """Write `over-time.parquet` under ``out_dir`` from ``scans`` — an ordered
    ``(scan_id, path_index_parquet)`` list, **oldest first**. Rolls each scan to
    a narrow shard, then consolidates via pyrmts' out-of-core DuckDB kernel.

    ``con``: pass a connection with a gcs secret to read `gs://` path-indices
    directly (out-of-core, no download); else a local connection is used (for
    mounted/local shards). Returns ``{rows, paths, scans, intervals_per_path,
    file, scans_file}``."""
    if not scans:
        raise ValueError("write_over_time_index: no scans")
    ids = [s for s, _ in scans]
    if len(set(ids)) != len(ids):
        raise ValueError(f"write_over_time_index: duplicate scan id in {ids}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    out_path = out / OVER_TIME_FILE
    shards = out / ".shards"
    shards.mkdir(exist_ok=True)
    own = con is None
    if own:
        con = duckdb.connect()
        con.execute(f"SET memory_limit='{mem}'; SET threads={threads}")
        con.execute(f"SET temp_directory='{tmp_dir or out / '.duckdb-tmp'}'")
    try:
        scan_files: list[tuple[str, str]] = []
        for i, (sid, pi) in enumerate(scans):
            shard = shards / f"{i:04d}.parquet"
            con.execute(f"COPY ({_roll_sql(con, str(pi))}) TO '{shard}' (FORMAT parquet)")
            scan_files.append((sid, str(shard)))
        ms = consolidate_parquet_duckdb(scan_files, over_time_pyramid(), con=con)
        con.register("ms_over_time", ms.table)
        con.execute(
            f"COPY (SELECT * FROM ms_over_time ORDER BY depth, path, {SCAN_LO}) "
            f"TO '{out_path}' (FORMAT parquet, {duckdb_codec()}, ROW_GROUP_SIZE {ROW_GROUP_SIZE})"
        )
        con.unregister("ms_over_time")
        rows = con.execute(f"SELECT count(*) FROM read_parquet('{out_path}')").fetchone()[0]
        paths = con.execute(f"SELECT count(DISTINCT (depth, path)) FROM read_parquet('{out_path}')").fetchone()[0]
    finally:
        for sh in shards.glob("*.parquet"):
            sh.unlink()
        try:
            shards.rmdir()
        except OSError:
            pass
        if own:
            con.close()
    (out / OVER_TIME_SCANS).write_text(json.dumps(ids) + "\n")
    err(f"over-time: {rows:,} intervals over {len(ids)} scans / {paths:,} paths → {out_path}")
    return {
        "rows": int(rows),
        "paths": int(paths),
        "scans": ids,
        "intervals_per_path": (rows / paths) if paths else 0.0,
        "file": str(out_path),
        "scans_file": str(out / OVER_TIME_SCANS),
    }


def write_over_time_groups(
    scans: list[tuple[str, str | Path]],
    out_dir: str | Path,
    *,
    group_size: int = OVER_TIME_GROUP_SIZE,
    mem: str = "8GB",
    threads: int = 8,
    tmp_dir: str | Path | None = None,
    con: "duckdb.DuckDBPyConnection | None" = None,
) -> dict:
    """Partition ``scans`` (ordered, oldest first) into fixed groups of
    ``group_size`` and build one self-contained over-time MS per group under
    ``out_dir/<group's last scan id>/`` — each an independent, bounded-memory
    build (interval bounds are indices into *that group's* scan list). Each group
    is synced to D1 as its own `(last-scan-date, over-time)` pointer; the reader
    reads the groups covering a query window and stitches their series, with the
    unsealed ≤``group_size``-scan tip served by the per-scan fallback.

    Returns ``{group_size, groups: [{group, first, last, n, file, scans_file,
    rows, paths}, ...]}`` (a group whose scans are all sealed here)."""
    if group_size < 1:
        raise ValueError(f"write_over_time_groups: group_size must be >= 1, got {group_size}")
    out = Path(out_dir)
    own = con is None
    if own:
        con = duckdb.connect()
        con.execute(f"SET memory_limit='{mem}'; SET threads={threads}")
        con.execute(f"SET temp_directory='{tmp_dir or out / '.duckdb-tmp'}'")
    groups: list[dict] = []
    try:
        for i in range(0, len(scans), group_size):
            grp = scans[i : i + group_size]
            gid = grp[-1][0]  # the group's last scan id names its dir + D1 pointer
            summ = write_over_time_index(grp, out / gid, con=con)
            groups.append({
                "group": gid,
                "first": grp[0][0],
                "last": grp[-1][0],
                "n": len(grp),
                "file": summ["file"],
                "scans_file": summ["scans_file"],
                "rows": summ["rows"],
                "paths": summ["paths"],
            })
            err(f"over-time group {gid}: {len(grp)} scans, {summ['rows']:,} intervals")
    finally:
        if own:
            con.close()
    return {"group_size": group_size, "groups": groups}


# ── Sealed groups → the D1 routing manifest ────────────────────────────────
# The reader (`site/functions/_lib/overTime.ts`) lists `pyramid_multiscans`
# rows for dataset `over-time` and, per row, opens `(key, 'over-time')` from
# `index_schema` — so a group's manifest `key` is the same scan id its footer
# was synced under (`index-sync -v over-time … <gid>`): the group's LAST scan.
# pyrmts owns the table's DDL + row shape (`pyrmts_engine.multiscan_index`);
# the consumer writes rows over its own D1 HTTP path (`index_footer`).
MULTISCAN_DATASET = OVER_TIME_VARIANT
MULTISCAN_TIER = "over-time"
MULTISCAN_ENCODER = "interval"


def multiscan_dataset(store: str | None = None) -> str:
    """The manifest `dataset` of ``store``'s groups: ``over-time`` for the
    primary, ``<store>:over-time`` for a secondary store (specs/multi-store.md;
    the site's `overTimeDataset`). pyrmts owns `pyramid_multiscans`, whose
    ``(dataset, key)`` PK already keeps stores apart — no store column."""
    from .index_footer import PRIMARY_STORE, check_store

    s = check_store(store or PRIMARY_STORE)
    return MULTISCAN_DATASET if s == PRIMARY_STORE else f"{s}:{MULTISCAN_DATASET}"


def scan_ms(scan: str) -> int:
    """A scan id (`YYYY-MM-DD` or `YYYY-MM-DDTHHMM`, UTC) as epoch milliseconds —
    the manifest's period axis, which orders groups for the reader."""
    from datetime import datetime, timezone

    fmt = "%Y-%m-%dT%H%M" if "T" in scan else "%Y-%m-%d"
    return int(datetime.strptime(scan, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)


def sealed_groups(dates: list[str], group_size: int = OVER_TIME_GROUP_SIZE) -> list[list[str]]:
    """The full ``group_size`` runs of consecutive scans, oldest first; the
    ``< group_size`` tail is never a group (the per-scan fallback serves it).
    Fixed partitioning from the oldest scan, so a group's membership never
    changes once sealed — re-running only appends new groups."""
    if group_size < 1:
        raise ValueError(f"sealed_groups: group_size must be >= 1, got {group_size}")
    ds = sorted(set(dates))
    return [ds[i : i + group_size] for i in range(0, len(ds) - group_size + 1, group_size)]


def multiscan_row(scans: list[str], *, written_at_ms: int, store: str | None = None) -> dict:
    """The `pyramid_multiscans` row for one sealed group (mirrors
    `pyrmts_engine.multiscan_index.multiscan_d1_row`): `key` = the group's last
    scan id, `shard_dur` = the group size, period = first..last scan."""
    if not scans:
        raise ValueError("multiscan_row: no scans")
    return {
        "dataset": multiscan_dataset(store),
        "tier": MULTISCAN_TIER,
        "shard_dur": f"{len(scans)}scans",
        "period_start": scan_ms(scans[0]),
        "period_end": scan_ms(scans[-1]),
        "key": scans[-1],
        "scans": json.dumps(scans),
        "encoder": MULTISCAN_ENCODER,
        "digests": None,
        "written_at": written_at_ms,
    }


def manifest_sql(row: dict) -> str:
    """`INSERT OR REPLACE` for one manifest row — idempotent on the
    `(dataset, key)` primary key."""
    from .index_footer import _q

    cols = ["dataset", "tier", "shard_dur", "period_start", "period_end", "key", "scans", "encoder", "digests", "written_at"]
    vals = [str(row[c]) if isinstance(row[c], int) else _q(row[c]) for c in cols]
    return f"INSERT OR REPLACE INTO pyramid_multiscans ({', '.join(cols)}) VALUES ({', '.join(vals)});"


def synced_groups(db_id: str | None = None, store: str | None = None) -> set[str]:
    """The group keys already in the manifest (a complete group: footer synced,
    row written last)."""
    from .index_footer import D1_DB_ID, _creds, _d1_query

    tok, acct = _creds()
    rows = _d1_query(f"SELECT key FROM pyramid_multiscans WHERE dataset = '{multiscan_dataset(store)}';", acct, tok, db_id or D1_DB_ID)
    return {r["key"] for r in rows}


def sync_manifest(row: dict, db_id: str | None = None) -> None:
    """Write one group's manifest row (the table is pyrmts' DDL, created if
    absent). Called AFTER the group's footer is in `index_schema`: the row is
    what makes the reader open the group, so it is the last thing to land."""
    from pyrmts_engine.multiscan_index import multiscan_d1_ddl

    from .index_footer import D1_DB_ID, _creds, _d1_query

    tok, acct = _creds()
    _d1_query(multiscan_d1_ddl(), acct, tok, db_id or D1_DB_ID)
    _d1_query(manifest_sql(row), acct, tok, db_id or D1_DB_ID)
