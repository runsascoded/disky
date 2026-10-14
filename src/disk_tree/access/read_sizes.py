"""Layer-2b: read-size distribution per shallow prefix per time bin.

A *shallow* rollup, deliberately separate from the layer-2a tree
(:mod:`disk_tree.access.aggregate`) rather than extra columns on it:

- 2a is blob-grain across every depth; multiplying it by a size-bucket
  dimension would be enormous. This rollup stops at ``max_depth`` prefixes,
  where the distribution is the interesting object anyway.
- Being shallow, it can afford a finer *time* grain than 2a's day, and
  sparsity makes finer grains far cheaper than their nominal ratio
  (see :data:`DEFAULT_GRAIN`).

Why a histogram and not mean/variance: observed read sizes are sharply
multimodal — one 18h eu-west4 shard had 532,377 reads at exactly 8388608 bytes
(8 MiB) sitting alongside modes at 8196, 1087, 415 and 102 bytes. A mean of
2.81 MB describes none of that, and a stddev would only say "the modes are far
apart", not where they are.

Bucket key is ``round(log2(bytes) * LG2_SCALE)`` — 1/50 of an octave, ~1.4%
wide. Fine enough that adjacent constants separate (at 1/10 octave, the 1087-
and 1117-byte modes collide in one bucket), and nearly free because a mode
that is a single exact value occupies exactly one bucket at any resolution:
going from 1/10 to 1/50 octave cost 37% more rows on real data. Rounding
rather than flooring puts an exact power of two dead centre in its bucket
instead of on a boundary.

Zero-byte responses (HEADs, 304s, errors) have no logarithm and key to
:data:`ZERO_BYTES_BUCKET` instead of being dropped — they are a real and
distinct access mode.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..duck import one

if TYPE_CHECKING:
    import duckdb


#: Log-bucket resolution: buckets per octave. See module docstring.
LG2_SCALE = 50

#: Sentinel bucket for zero-byte responses (log2(0) is undefined).
ZERO_BYTES_BUCKET = -9999

#: Ops that constitute a *read of the data*. A bucket LIST is not one, and it
#: attaches to a prefix rather than an object key.
READ_OPS = ('GET', 'HEAD')

#: Prefix depth this rollup stops at. Beyond ~4 the prefix count explodes
#: (measured: 1,159 prefixes at depth 4, 102,467 at depth 5) while the
#: distribution stops being a summary of anything.
DEFAULT_MAX_DEPTH = 4

#: Time grain. Sparsity saturates fast, so finer grains cost far less than
#: their nominal ratio — measured rows on one real shard at depth 4:
#: 1d 6,274 · 1h 20,385 · 15m 49,921 · 5m 83,687 · 1m 100,257. So 1-minute is
#: ~4.9x hour, not 60x. Parameterized because you can always roll a fine grain
#: up and never a coarse one down, and because pyrmts-style tier ladders want
#: the finest base that's affordable.
DEFAULT_GRAIN = '1 hour'


def lg2_bucket(n: int) -> int:
    """Python mirror of the SQL bucket key — for tests and for reading back."""
    if n <= 0:
        return ZERO_BYTES_BUCKET
    from math import log2
    # round-half-even in Python vs round-half-away in DuckDB: only differs on
    # exact .5 keys, which need a value whose log2 is an exact odd/100th.
    return int(round(log2(n) * LG2_SCALE))


def bucket_bounds(k: int) -> tuple[float, float]:
    """Byte range a bucket covers, as ``[lo, hi)``. Inverse of :func:`lg2_bucket`."""
    if k == ZERO_BYTES_BUCKET:
        return (0.0, 0.0)
    return (2 ** ((k - 0.5) / LG2_SCALE), 2 ** ((k + 0.5) / LG2_SCALE))


def aggregate_read_sizes(
    con: "duckdb.DuckDBPyConnection",
    raw_sql: str,
    out_parquet: str,
    max_depth: int = DEFAULT_MAX_DEPTH,
    grain: str = DEFAULT_GRAIN,
    ops: tuple[str, ...] = READ_OPS,
) -> dict:
    """Aggregate canonical layer-1a access rows → layer-2b read-size parquet.

    ``raw_sql`` is a parenthesized SELECT over the canonical row shape (same
    contract as :func:`~disk_tree.access.aggregate.aggregate_access`). Must run
    against **layer-1a**, not 2a: 2a has already summed ``bytes_out`` across
    requests, and the per-request distribution is unrecoverable from a sum.

    Output columns::

        bucket, prefix, bin, lg2, n_ops, bytes_out
    """
    con.execute("SET TimeZone = 'UTC'")
    ops_sql = ', '.join(f"'{o}'" for o in ops)
    # Same `//` collapse + trailing-slash strip as layer-2a, so prefixes here
    # are join-compatible with tree paths there.
    canon = "rtrim(regexp_replace(path, '/+', '/', 'g'), '/')"
    prefix = (
        f"array_to_string(list_slice(string_split({canon}, '/'), 1, {max_depth}), '/')"
    )
    con.execute(f"""
        CREATE OR REPLACE TABLE read_sizes AS
        SELECT
            bucket,
            {prefix} AS prefix,
            time_bucket(INTERVAL '{grain}', ts) AS bin,
            CASE WHEN bytes_out <= 0 THEN {ZERO_BYTES_BUCKET}
                 ELSE CAST(round(log2(bytes_out) * {LG2_SCALE}) AS INTEGER) END AS lg2,
            COUNT(*)::BIGINT AS n_ops,
            SUM(bytes_out)::BIGINT AS bytes_out
        FROM {raw_sql}
        WHERE op IN ({ops_sql})
        GROUP BY 1, 2, 3, 4
    """)
    con.execute(f"""
        COPY (SELECT bucket, prefix, bin, lg2, n_ops, bytes_out
              FROM read_sizes ORDER BY bucket, prefix, bin, lg2)
        TO '{out_parquet}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    rows, prefixes, bins, n_ops, total = one(con, """
        SELECT COUNT(*), COUNT(DISTINCT (bucket, prefix)), COUNT(DISTINCT bin),
               COALESCE(SUM(n_ops), 0), COALESCE(SUM(bytes_out), 0)
        FROM read_sizes
    """)
    return {
        'rows': int(rows),
        'prefixes': int(prefixes),
        'bins': int(bins),
        'n_ops': int(n_ops),
        'bytes_out': int(total),
    }
