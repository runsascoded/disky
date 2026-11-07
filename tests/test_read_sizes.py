"""Layer-2b read-size rollup (`disk_tree.access.read_sizes`).

The resolution choice (1/50 octave) is load-bearing, not cosmetic — coarser
buckets provably merge distinct modes seen in real data. `test_resolution_*`
pins that.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from disk_tree.access.read_sizes import (
    LG2_SCALE,
    ZERO_BYTES_BUCKET,
    aggregate_read_sizes,
    bucket_bounds,
    lg2_bucket,
)


# ---------- Bucket key ----------

def test_lg2_bucket_exact_powers_of_two():
    """A power of two keys to an exact multiple of the scale (dead centre)."""
    assert [lg2_bucket(n) for n in (1, 2, 1024, 8388608)] == [0, 50, 500, 1150]


def test_lg2_bucket_zero_and_negative_use_sentinel():
    assert [lg2_bucket(n) for n in (0, -1)] == [ZERO_BYTES_BUCKET, ZERO_BYTES_BUCKET]


def test_resolution_separates_adjacent_real_modes():
    """1087 and 1117 are distinct modes in real GCS traffic (57,080 and 35,232
    requests in one shard). At 1/10 octave they collide; at 1/50 they don't."""
    assert LG2_SCALE == 50
    assert lg2_bucket(1087) == 504
    assert lg2_bucket(1117) == 506


def test_bucket_bounds_bracket_their_inputs():
    for n in (1, 100, 1087, 1117, 8388608, 1754161833):
        lo, hi = bucket_bounds(lg2_bucket(n))
        assert lo <= n < hi, (n, lo, hi)


def test_bucket_bounds_width_is_one_scale_step():
    lo, hi = bucket_bounds(1150)
    assert hi / lo == pytest.approx(2 ** (1 / LG2_SCALE))


def test_bucket_bounds_zero_sentinel_is_empty_range():
    assert bucket_bounds(ZERO_BYTES_BUCKET) == (0.0, 0.0)


# ---------- Aggregation ----------

ROWS = [
    # (ts, bucket, path, op, bytes_out)
    ("2026-08-22 10:15:00", "b1", "x/y/z/w/deep/file", "GET", 8388608),
    ("2026-08-22 10:45:00", "b1", "x/y/z/w/other/file", "GET", 8388608),
    ("2026-08-22 11:00:00", "b1", "x/y/z/w/deep/file", "GET", 8388608),
    ("2026-08-22 10:20:00", "b1", "x/y/z/w/deep/file", "HEAD", 0),
    ("2026-08-22 10:20:00", "b1", "x/y/z/w/deep/file", "GET", 1087),
    ("2026-08-22 10:20:00", "b1", "x/y/z/w/deep/file", "GET", 1117),
    ("2026-08-22 10:20:00", "b1", "shallow", "GET", 1024),
    ("2026-08-22 10:20:00", "b2", "x/y/z/w/deep/file", "GET", 1024),
    # Excluded: not reads of the data
    ("2026-08-22 10:20:00", "b1", "x/y/z/w/deep/file", "LIST", 999),
    ("2026-08-22 10:20:00", "b1", "x/y/z/w/deep/file", "PUT", 999),
    ("2026-08-22 10:20:00", "b1", "x/y/z/w/deep/file", "DELETE", 999),
]


def _relation() -> str:
    vals = ", ".join(
        f"(TIMESTAMP '{ts}', '{b}', '{p}', '{op}', {n})" for ts, b, p, op, n in ROWS
    )
    return (
        f"(SELECT * FROM (VALUES {vals}) AS t(ts, bucket, path, op, bytes_out))"
    )


def _agg(tmp_path: Path, **kw) -> tuple[list[tuple], dict]:
    con = duckdb.connect()
    out = str(tmp_path / "sizes.parquet")
    stats = aggregate_read_sizes(con, _relation(), out, **kw)
    rows = con.execute(
        f"SELECT bucket, prefix, bin, lg2, n_ops, bytes_out "
        f"FROM read_parquet('{out}') ORDER BY bucket, prefix, bin, lg2"
    ).fetchall()
    return [(b, p, h.strftime("%Y-%m-%d %H:%M"), k, n, o) for b, p, h, k, n, o in rows], stats


def test_rollup_rows_exact(tmp_path: Path):
    rows, _ = _agg(tmp_path)
    assert rows == [
        ("b1", "shallow",   "2026-08-22 10:00", 500,  1, 1024),
        ("b1", "x/y/z/w",   "2026-08-22 10:00", ZERO_BYTES_BUCKET, 1, 0),
        ("b1", "x/y/z/w",   "2026-08-22 10:00", 504,  1, 1087),
        ("b1", "x/y/z/w",   "2026-08-22 10:00", 506,  1, 1117),
        ("b1", "x/y/z/w",   "2026-08-22 10:00", 1150, 2, 16777216),
        ("b1", "x/y/z/w",   "2026-08-22 11:00", 1150, 1, 8388608),
        ("b2", "x/y/z/w",   "2026-08-22 10:00", 500,  1, 1024),
    ]


def test_stats_exact(tmp_path: Path):
    _, stats = _agg(tmp_path)
    assert stats == {
        "rows": 7,
        "prefixes": 3,      # (b1,shallow), (b1,x/y/z/w), (b2,x/y/z/w)
        "bins": 2,
        "n_ops": 8,         # 11 rows minus LIST/PUT/DELETE
        "bytes_out": 25170076,  # 3*8388608 + 1087 + 1117 + 1024 + 1024 + 0
    }


def test_write_and_list_ops_excluded(tmp_path: Path):
    """LIST/PUT/DELETE carry bytes but are not reads; none of their 999s appear."""
    rows, stats = _agg(tmp_path)
    assert [r for r in rows if r[5] == 999] == []
    assert stats["n_ops"] == len([r for r in ROWS if r[3] in ("GET", "HEAD")])


def test_zero_byte_reads_bucketed_not_dropped(tmp_path: Path):
    rows, _ = _agg(tmp_path)
    assert [(r[0], r[1], r[4]) for r in rows if r[3] == ZERO_BYTES_BUCKET] == [
        ("b1", "x/y/z/w", 1),
    ]


def test_hour_grain_splits_the_same_prefix_and_bucket(tmp_path: Path):
    """The three 8 MiB reads span two hours → two rows, 2 + 1, not one row of 3."""
    rows, _ = _agg(tmp_path)
    assert [(r[2], r[4]) for r in rows if r[3] == 1150] == [
        ("2026-08-22 10:00", 2),
        ("2026-08-22 11:00", 1),
    ]


def test_grain_controls_time_binning(tmp_path: Path):
    """Same reads, three grains: a day merges what an hour splits, and a minute
    splits what an hour merges (10:15 and 10:45 are one hour, two 30-min bins)."""
    day, _ = _agg(tmp_path, grain="1 day")
    assert [(r[2], r[4]) for r in day if r[3] == 1150] == [("2026-08-22 00:00", 3)]

    half, _ = _agg(tmp_path, grain="30 minutes")
    assert [(r[2], r[4]) for r in half if r[3] == 1150] == [
        ("2026-08-22 10:00", 1),
        ("2026-08-22 10:30", 1),
        ("2026-08-22 11:00", 1),
    ]


def test_max_depth_controls_prefix_truncation(tmp_path: Path):
    rows2, _ = _agg(tmp_path, max_depth=2)
    assert sorted({(r[0], r[1]) for r in rows2}) == [
        ("b1", "shallow"), ("b1", "x/y"), ("b2", "x/y"),
    ]
    rows6, _ = _agg(tmp_path, max_depth=6)
    assert sorted({(r[0], r[1]) for r in rows6}) == [
        ("b1", "shallow"),
        ("b1", "x/y/z/w/deep/file"),
        ("b1", "x/y/z/w/other/file"),
        ("b2", "x/y/z/w/deep/file"),
    ]


def test_sql_and_python_bucket_keys_agree(tmp_path: Path):
    """The SQL key and `lg2_bucket` must not drift — consumers use both."""
    rows, _ = _agg(tmp_path)
    by_bytes = {o // n: k for _, _, _, k, n, o in rows if k != ZERO_BYTES_BUCKET}
    assert by_bytes == {n: lg2_bucket(n) for n in by_bytes}
