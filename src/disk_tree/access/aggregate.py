"""Layer-2a: per-bucket per-path per-hour per-op rollups from canonical access rows.

Same bottom-up parent-synthesis machinery as size scans
(:mod:`disk_tree.find.aggregate_duckdb`), but the accumulators are
``n_ops`` / ``bytes_out`` / ``n_requesters`` per (bucket, path, hour, op)
instead of ``size`` / ``n_desc``.

Structural upshot: the widgets (treemap, series, diff) work over ops and
egress *the same way* they work over bytes-at-rest — a treemap sized by
``bytes_out`` shows hot-egress paths; a time series over ``n_ops`` shows
request-rate shifts.

Output schema (canonical layer-2a parquet):

::

    bucket                                 -- source bucket (tree per bucket)
    path, parent, depth, kind              -- tree mechanics (same as scans)
    hour                                   -- UTC hour (TIMESTAMP, floor of ts)
    op                                     -- GET|PUT|LIST|HEAD|DELETE|OTHER
    n_ops                                  -- request count under this path/hour/op
    bytes_out, bytes_in                    -- summed byte totals
    n_requesters                           -- DISTINCT COUNT(requester)
    last_ts                                -- MAX(request ts) — read-recency ("atime")

``bucket`` is a first-class key at every level (identically-named prefixes in
different buckets used to merge silently); each bucket gets its own tree with
its own ``.`` root row. ``last_ts`` propagates as MAX up the parent levels, so
any prefix's atime is the most recent request anywhere under it.

Hour grain (spec specs/done/mgu-scale-unification.md D.1): a scan's as-of instant has
to cut the access history, and a day-grained row can't be split — hours keep
the cut within an hour of the instant (``last_ts`` stays exact). Row count
grows only for paths read across many hours. ``as_of`` (D.2) applies the cut
here, on the raw rows (``ts < as_of``, exact), so every emitted hour row
satisfies ``hour < as_of``; re-aggregating retained raw shards with the same
``as_of`` makes history follow the same rule.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb


# Parent-of expression identical to find/aggregate_duckdb.py — trailing
# slashes already collapsed by ingest (see find/import_listing._canonicalize).
_PARENT_EXPR = (
    "CASE WHEN position('/' IN {col}) > 0 "
    "THEN regexp_extract({col}, '^(.*)/[^/]+$', 1) "
    "ELSE '' END"
)

GRAIN = 'hour'


def as_of_utc(as_of: "str | datetime") -> datetime:
    """Normalize an as-of instant to an aware UTC datetime (naive input is
    taken as UTC — the convention `import -t` uses)."""
    if isinstance(as_of, str):
        as_of = datetime.fromisoformat(as_of.replace('Z', '+00:00'))
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    return as_of.astimezone(timezone.utc)


def as_of_literal(as_of: "str | datetime") -> str:
    """SQL literal (TIMESTAMPTZ, UTC) for an as-of instant."""
    return f"TIMESTAMPTZ '{as_of_utc(as_of).isoformat()}'"


def aggregate_access(
    con: "duckdb.DuckDBPyConnection",
    raw_sql: str,
    out_parquet: str,
    as_of: "str | datetime | None" = None,
) -> dict:
    """Aggregate canonical access rows → layer-2a parquet.

    ``raw_sql`` is a parenthesized SELECT exposing the canonical row shape
    (typically from a parser: ``(SELECT * FROM parser_relation)``). Handles
    unlimited row counts out-of-core; intermediate tables spill under the
    connection's memory limit.

    ``as_of`` drops every request at or after that instant before
    aggregating; the output records it in its parquet metadata.

    Returns a small stats dict (rows_in / paths_out / hours / total_bytes_out).
    """
    # `hour` must mean *UTC* hour regardless of host TZ — date_trunc over
    # TIMESTAMPTZ truncates in the session timezone (a laptop run in EDT would
    # shard hours differently than the UTC batch job).
    con.execute("SET TimeZone = 'UTC'")
    cut = f" WHERE ts < {as_of_literal(as_of)}" if as_of is not None else ''
    # Canonicalize input into a `access_in` table so subsequent SQL can spill.
    con.execute(f"""
        CREATE OR REPLACE TABLE access_in AS
        SELECT
            ts,
            date_trunc('{GRAIN}', ts)::TIMESTAMP AS hour,
            store, bucket,
            -- Reuse the same `//` collapse policy as size-scan ingest so
            -- access and scan trees line up path-for-path.
            rtrim(regexp_replace(path, '/+', '/', 'g'), '/') AS path,
            op, bytes_out, bytes_in, requester
        FROM {raw_sql}{cut}
    """)

    n_rows = con.execute("SELECT COUNT(*) FROM access_in").fetchone()[0]
    if n_rows == 0:
        raise ValueError("no access rows in input" + (f" before {as_of_utc(as_of).isoformat()}" if as_of is not None else ''))

    # Leaf rollup: per (bucket, path, hour, op). This is the "level 0" grain.
    con.execute("""
        CREATE OR REPLACE TABLE access_leaf AS
        SELECT
            bucket, path, hour, op,
            COUNT(*)::BIGINT AS n_ops,
            SUM(bytes_out)::BIGINT AS bytes_out,
            SUM(bytes_in)::BIGINT AS bytes_in,
            COUNT(DISTINCT requester)::BIGINT AS n_requesters,
            MAX(ts) AS last_ts
        FROM access_in
        GROUP BY bucket, path, hour, op
    """)

    # Bottom-up parent synthesis. Group current level's rows by parent path
    # (+ bucket + hour + op) to produce the next level, until nothing left.
    con.execute("CREATE OR REPLACE TABLE level_cur AS SELECT * FROM access_leaf")
    level = 0
    level_tables: list[str] = []
    parent_expr = _PARENT_EXPR.format(col='path')
    while True:
        next_tbl = f'access_level_{level}'
        con.execute(f"""
            CREATE OR REPLACE TABLE {next_tbl} AS
            SELECT
                bucket,
                {parent_expr} AS path,
                hour, op,
                SUM(n_ops)::BIGINT AS n_ops,
                SUM(bytes_out)::BIGINT AS bytes_out,
                SUM(bytes_in)::BIGINT AS bytes_in,
                -- n_requesters at parent level is a bounded overestimate: we
                -- can't recover exact distinct-count across children without
                -- rebuilding from raw. Sum bounds it above; a HyperLogLog
                -- sketch is the fix if this ever needs to be exact.
                SUM(n_requesters)::BIGINT AS n_requesters,
                MAX(last_ts) AS last_ts
            FROM level_cur
            WHERE path != ''
            GROUP BY 1, 2, 3, 4
        """)
        cnt = con.execute(f"SELECT COUNT(*) FROM {next_tbl}").fetchone()[0]
        if cnt == 0:
            break
        level_tables.append(next_tbl)
        con.execute(f"CREATE OR REPLACE TABLE level_cur AS SELECT * FROM {next_tbl}")
        level += 1

    # Union all levels + attach kind/parent/depth for tree mechanics.
    unions = " UNION ALL ".join(
        f"SELECT bucket, path, hour, op, n_ops, bytes_out, bytes_in, n_requesters, last_ts FROM {t}"
        for t in [*level_tables, 'access_leaf']
    )
    con.execute(f"""
        CREATE OR REPLACE TABLE access_agg AS
        WITH all_levels AS ({unions})
        SELECT
            bucket,
            CASE WHEN path = '' THEN '.' ELSE path END AS path,
            hour, op,
            SUM(n_ops)::BIGINT AS n_ops,
            SUM(bytes_out)::BIGINT AS bytes_out,
            SUM(bytes_in)::BIGINT AS bytes_in,
            SUM(n_requesters)::BIGINT AS n_requesters,
            MAX(last_ts) AS last_ts,
            -- 'dir' for the tree root and any synthesized parent; 'file' for
            -- leaf paths present in access_leaf (i.e. object keys that
            -- actually appeared in the raw log).
            CASE
                WHEN path = '' THEN 'dir'
                WHEN (bucket, path) IN (SELECT DISTINCT (bucket, path) FROM access_leaf) THEN 'file'
                ELSE 'dir'
            END AS kind,
            CASE
                WHEN path = '' THEN ''
                ELSE {_PARENT_EXPR.format(col='CASE WHEN path IS NULL THEN NULL ELSE path END')}
            END AS parent,
            CASE WHEN path = '' THEN 0 ELSE (length(path) - length(replace(path, '/', ''))) + 1 END AS depth
        FROM all_levels
        GROUP BY bucket, path, hour, op
    """)

    # Root-level normalization: rows with parent='' + path!='' → parent='.'
    # (matches size-scan convention).
    con.execute("""
        UPDATE access_agg SET parent = '.' WHERE parent = '' AND path != '.'
    """)

    kv = {'grain': GRAIN}
    if as_of is not None:
        kv['as_of'] = as_of_utc(as_of).isoformat()
    kv_sql = ', '.join(f"'{k}': '{v}'" for k, v in kv.items())
    con.execute(f"""
        COPY (SELECT bucket, path, parent, depth, kind, hour, op,
                     n_ops, bytes_out, bytes_in, n_requesters, last_ts
              FROM access_agg
              ORDER BY bucket, depth, path, hour, op)
        TO '{out_parquet}' (FORMAT PARQUET, COMPRESSION ZSTD, KV_METADATA {{{kv_sql}}})
    """)

    n_paths = con.execute("SELECT COUNT(DISTINCT (bucket, path)) FROM access_agg").fetchone()[0]
    n_hours = con.execute("SELECT COUNT(DISTINCT hour) FROM access_agg").fetchone()[0]
    total_out = con.execute(
        "SELECT COALESCE(SUM(bytes_out), 0) FROM access_agg WHERE path = '.'"
    ).fetchone()[0]
    return {
        'rows_in': int(n_rows),
        'paths_out': int(n_paths),
        'hours': int(n_hours),
        'total_bytes_out': int(total_out),
    }
