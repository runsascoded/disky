"""Per-scan access state (spec mgu-scale-unification.md D.3).

One row per ``(bucket, dir)`` that exists in a scan's listing: when it was
last read, how much, and per-op sums — the access plane folded to the shape
the size cascade can join (D.4: ``import --side <state> --max-col last_ts``).

Built incrementally: the previous state (its rows semi-joined to the live
dirs, so since-deleted dirs drop out) plus the layer-2a shards for the window
``[prev_as_of, as_of)`` (``hour`` rows, so a non-hour-aligned ``as_of`` is
coarse by less than an hour; ``aggregate_access(as_of=)`` is the exact cut).
Cost is O(live dirs) + O(new rows), not O(everything since logging began).
Measured on mgu 2026-09-05: 64.3M dirs ever read, 52.0M (81 %) no longer
exist.

Output (sorted ``(bucket, path)``; ``as_of`` / ``prev_as_of`` / ``grain`` in
the parquet key-value metadata)::

    bucket, path
    last_ts                     -- MAX request ts over GETs ("last read")
    read_ops, read_bytes        -- GET count / egress
    n_ops_<op>, bytes_out_<op>, bytes_in_<op>   -- per op in schema.OPS

"Read" means ``GET`` — the only op that moves object bytes out.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from disk_tree.access.aggregate import GRAIN, as_of_literal, as_of_utc
from disk_tree.access.schema import OPS

if TYPE_CHECKING:
    import duckdb

READ_OP = 'GET'


def _per_op_cols() -> list[str]:
    return [f'{m}_{op.lower()}' for op in OPS for m in ('n_ops', 'bytes_out', 'bytes_in')]


STATE_COLUMNS: tuple[str, ...] = ('bucket', 'path', 'last_ts', 'read_ops', 'read_bytes', *_per_op_cols())


def live_bucket(con: "duckdb.DuckDBPyConnection", live: str) -> str:
    """The bucket a dirs tier / layer-2 blob describes: a v2 listing's scan root
    (key-value metadata), else its root row's `uri`."""
    from disk_tree.listing_format import format_of
    fmt = format_of(live)
    if fmt.version >= 2:
        uri = fmt.scan_root
    else:
        rows = con.execute(f"SELECT uri FROM read_parquet('{live}') WHERE path = '.' LIMIT 1").fetchall()
        if not rows:
            raise ValueError(f"{live}: no root row (path = '.') — not a layer-2 blob or dirs tier")
        uri = rows[0][0]
    if '://' not in uri:
        raise ValueError(f"{live}: root uri {uri!r} has no scheme — a bucket listing is expected")
    return uri.split('://', 1)[1].split('/', 1)[0]


def read_as_of(path: str) -> datetime:
    """The `as_of` a state (or 2a) parquet was built with, from its metadata."""
    import pyarrow.parquet as pq
    md = pq.read_metadata(path).metadata or {}
    if b'as_of' not in md:
        raise ValueError(f"{path}: no `as_of` in its parquet metadata")
    return as_of_utc(md[b'as_of'].decode())


def build_state(
    con: "duckdb.DuckDBPyConnection",
    shards_sql: str,
    out_parquet: str,
    as_of: "str | datetime",
    live: tuple[str, ...],
    prev: str | None = None,
) -> dict:
    """Fold layer-2a rows (``shards_sql``: a parenthesized SELECT over hour-grained
    2a parquet) into a per-scan state at ``out_parquet``.

    ``live``: one dirs tier (or layer-2 blob) per bucket in the scan; the
    state keeps exactly the ``(bucket, path)`` dir rows found there that have
    any read history. ``prev``: the previous state — its ``as_of`` opens the
    window, and its rows carry forward (minus dirs no longer live).
    """
    con.execute("SET TimeZone = 'UTC'")
    if not live:
        raise ValueError("at least one --live dirs tier is required")
    at = as_of_utc(as_of)
    prev_at = read_as_of(prev) if prev is not None else None
    if prev_at is not None and prev_at >= at:
        raise ValueError(f"--as-of {at.isoformat()} is not after the previous state's {prev_at.isoformat()}")

    live_selects = []
    buckets = []
    for tier in live:
        bucket = live_bucket(con, tier)
        if bucket in buckets:
            raise ValueError(f"bucket {bucket!r} given twice in --live")
        buckets.append(bucket)
        live_selects.append(
            f"SELECT '{bucket}'::VARCHAR AS bucket, path FROM read_parquet('{tier}') WHERE kind = 'dir'"
        )
    con.execute(f"CREATE OR REPLACE TABLE live_dirs AS {' UNION ALL '.join(live_selects)}")
    n_live = con.execute("SELECT COUNT(*) FROM live_dirs").fetchone()[0]

    # New rows: the window's hour rows, folded per (bucket, path, op) then pivoted.
    window = f"hour < {as_of_literal(at)}"
    if prev_at is not None:
        window = f"hour >= {as_of_literal(prev_at)} AND {window}"
    per_op = ''.join(
        f", SUM(CASE WHEN op = '{op}' THEN {m} ELSE 0 END)::BIGINT AS {m}_{op.lower()}"
        for op in OPS for m in ('n_ops', 'bytes_out', 'bytes_in')
    )
    con.execute(f"""
        CREATE OR REPLACE TABLE state_new AS
        SELECT
            s.bucket, s.path,
            MAX(CASE WHEN op = '{READ_OP}' THEN last_ts END) AS last_ts,
            SUM(CASE WHEN op = '{READ_OP}' THEN n_ops ELSE 0 END)::BIGINT AS read_ops,
            SUM(CASE WHEN op = '{READ_OP}' THEN bytes_out ELSE 0 END)::BIGINT AS read_bytes{per_op}
        FROM {shards_sql} s
        SEMI JOIN live_dirs l ON s.bucket = l.bucket AND s.path = l.path
        WHERE {window}
        GROUP BY s.bucket, s.path
    """)
    n_new = con.execute("SELECT COUNT(*) FROM state_new").fetchone()[0]

    cols = ', '.join(STATE_COLUMNS)
    if prev is not None:
        con.execute(f"""
            CREATE OR REPLACE TABLE state_prev AS
            SELECT {cols} FROM read_parquet('{prev}') p
            SEMI JOIN live_dirs l ON p.bucket = l.bucket AND p.path = l.path
        """)
        n_prev_all = con.execute(f"SELECT COUNT(*) FROM read_parquet('{prev}')").fetchone()[0]
    else:
        con.execute(f"CREATE OR REPLACE TABLE state_prev AS SELECT {cols} FROM state_new WHERE false")
        n_prev_all = 0
    n_carried = con.execute("SELECT COUNT(*) FROM state_prev").fetchone()[0]

    sums = ''.join(f", SUM({c})::BIGINT AS {c}" for c in STATE_COLUMNS[3:])
    kv = {'as_of': at.isoformat(), 'prev_as_of': prev_at.isoformat() if prev_at else '', 'grain': GRAIN}
    kv_sql = ', '.join(f"'{k}': '{v}'" for k, v in kv.items())
    con.execute(f"""
        COPY (
            SELECT bucket, path, MAX(last_ts) AS last_ts{sums}
            FROM (SELECT {cols} FROM state_prev UNION ALL SELECT {cols} FROM state_new)
            GROUP BY bucket, path
            ORDER BY bucket, path
        ) TO '{out_parquet}' (FORMAT PARQUET, COMPRESSION ZSTD, KV_METADATA {{{kv_sql}}})
    """)
    n_rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out_parquet}')").fetchone()[0]
    for t in ('live_dirs', 'state_new', 'state_prev'):
        con.execute(f"DROP TABLE {t}")
    return {
        'rows': int(n_rows),
        'live_dirs': int(n_live),
        'new': int(n_new),
        'carried': int(n_carried),
        'dropped': int(n_prev_all - n_carried),
        'as_of': at.isoformat(),
        'prev_as_of': prev_at.isoformat() if prev_at else None,
    }
