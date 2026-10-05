"""Access-log ingest + aggregation, using canned GCS usage-log fixtures.

Real CSVs land later — these tests pin the schema, parser semantics, and the
aggregation output shape so a landed real-data smoke has something to
compare against. The GCS fixture matches Google's documented usage-log
format field-for-field (header + a handful of representative rows).
"""

from __future__ import annotations

import csv
from pathlib import Path

import duckdb
import pandas as pd
import pytest


# ---------- GCS parser ----------

# Google's GCS-usage-log header, verbatim (subset — parser tolerates extras
# via union_by_name).
_GCS_HEADER = [
    'time_micros', 'c_ip', 'c_ip_type', 'c_ip_region',
    'cs_method', 'cs_uri', 'sc_status', 'cs_bytes', 'sc_bytes',
    'time_taken_micros', 'cs_host', 'cs_referer', 'cs_user_agent',
    's_request_id', 'cs_operation', 'cs_bucket', 'cs_object',
]


def _write_gcs_csv(path: Path, rows: list[tuple]) -> None:
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(_GCS_HEADER)
        for r in rows:
            w.writerow(r)


def _gcs_row(
    ts_micros: int,
    c_ip: str,
    cs_method: str,
    cs_operation: str,
    bucket: str,
    obj: str,
    sc_bytes: int = 0,
    cs_bytes: int = 0,
    status: int = 200,
    req_id: str = '',
    user_agent: str = 'ua',
) -> tuple:
    return (
        ts_micros, c_ip, 'RESIDENTIAL', 'us-central1',
        cs_method, f'/{bucket}/{obj}', status, cs_bytes, sc_bytes,
        1000, f'{bucket}.storage.googleapis.com', '', user_agent,
        req_id, cs_operation, bucket, obj,
    )


def test_gcs_parse_shape(tmp_path: Path):
    """A handful of representative rows parse to the canonical shape."""
    from disk_tree.access.parsers.gcs import parse

    csv_path = tmp_path / 'usage.csv'
    _write_gcs_csv(csv_path, [
        _gcs_row(1755300000_000_000, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'foo/a.txt',
                 sc_bytes=1024, req_id='r1'),
        _gcs_row(1755300010_000_000, '10.0.0.2', 'GET', 'LIST_Bucket', 'b1', '',
                 sc_bytes=200, req_id='r2'),
        _gcs_row(1755300020_000_000, '10.0.0.1', 'PUT', 'PUT_Object', 'b1', 'foo/b.txt',
                 cs_bytes=500, status=200, req_id='r3'),
        _gcs_row(1755300030_000_000, '10.0.0.3', 'DELETE', 'DELETE_Object', 'b1', 'foo/c.txt',
                 status=204, req_id='r4'),
    ])

    con = duckdb.connect()
    rel = parse(str(csv_path), con=con)
    df = rel.df().sort_values('ts').reset_index(drop=True)

    # Canonical columns present in expected order
    assert list(df.columns) == [
        'ts', 'store', 'bucket', 'path',
        'op', 'op_raw', 'status',
        'bytes_out', 'bytes_in',
        'requester', 'user_agent', 'request_id',
    ]
    # Row-level assertions (parsed → canonical)
    assert df['store'].tolist() == ['gcs'] * 4
    assert df['bucket'].tolist() == ['b1'] * 4
    assert df['path'].tolist() == ['foo/a.txt', '', 'foo/b.txt', 'foo/c.txt']
    assert df['op'].tolist() == ['GET', 'LIST', 'PUT', 'DELETE']
    assert df['op_raw'].tolist() == ['GET_Object', 'LIST_Bucket', 'PUT_Object', 'DELETE_Object']
    assert df['status'].tolist() == [200, 200, 200, 204]
    assert df['bytes_out'].tolist() == [1024, 200, 0, 0]
    assert df['bytes_in'].tolist() == [0, 0, 500, 0]
    assert df['requester'].tolist() == ['10.0.0.1', '10.0.0.2', '10.0.0.1', '10.0.0.3']
    assert df['request_id'].tolist() == ['r1', 'r2', 'r3', 'r4']


def test_gcs_parse_deduplicates_identical_lines(tmp_path: Path):
    """Google documents rare duplicate log lines — byte-identical lines collapse."""
    from disk_tree.access.parsers.gcs import parse

    csv_path = tmp_path / 'dup.csv'
    _write_gcs_csv(csv_path, [
        _gcs_row(1_000_000, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'x', sc_bytes=100, req_id='same'),
        _gcs_row(1_000_000, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'x', sc_bytes=100, req_id='same'),
        _gcs_row(2_000_000, '10.0.0.2', 'GET', 'GET_Object', 'b1', 'y', sc_bytes=50,  req_id='other'),
    ])
    con = duckdb.connect()  # keep alive; DuckDB relations hold conn refs
    df = parse(str(csv_path), con=con).df()
    assert len(df) == 2
    assert sorted(df['request_id'].tolist()) == ['other', 'same']


def test_dropped_fraction_reports_rows_in_and_loss(tmp_path: Path):
    """The invariant whose absence let the batch-dedupe bug ship: 1a must come
    out only marginally smaller than its source CSV."""
    from disk_tree.access.parsers.gcs import dropped_fraction, parse

    csv_path = tmp_path / 'mixed.csv'
    _write_gcs_csv(csv_path, [
        _gcs_row(1_000_000, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'x', sc_bytes=100, req_id='dup'),
        _gcs_row(1_000_000, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'x', sc_bytes=100, req_id='dup'),
        _gcs_row(2_000_000, '10.0.0.2', 'GET', 'GET_Object', 'b1', 'y', sc_bytes=50, req_id='r2'),
        _gcs_row(3_000_000, '10.0.0.2', 'GET', 'GET_Object', 'b1', 'z', sc_bytes=50, req_id='r3'),
    ])
    con = duckdb.connect()
    n_out = parse(str(csv_path), con=con).aggregate('COUNT(*)').fetchone()[0]
    assert (n_out, dropped_fraction(con, n_out)) == (3, (4, 0.25))


def test_dropped_fraction_is_zero_when_nothing_is_deduped(tmp_path: Path):
    from disk_tree.access.parsers.gcs import dropped_fraction, parse

    csv_path = tmp_path / 'clean.csv'
    _write_gcs_csv(csv_path, [
        _gcs_row(1_000_000, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'x', sc_bytes=1, req_id='r1'),
        _gcs_row(2_000_000, '10.0.0.2', 'GET', 'GET_Object', 'b1', 'y', sc_bytes=2, req_id='r2'),
    ])
    con = duckdb.connect()
    n_out = parse(str(csv_path), con=con).aggregate('COUNT(*)').fetchone()[0]
    assert (n_out, dropped_fraction(con, n_out)) == (2, (2, 0.0))


def test_batch_request_stays_under_the_dedupe_warning_threshold(tmp_path: Path):
    """The regression check with teeth: parse a batch-heavy CSV and assert the
    loss rate is inside the alerting threshold. Under the old `request_id`-only
    key this CSV lost 75% of its rows and would trip the warning."""
    from disk_tree.access.parsers.gcs import DEDUPE_WARN_FRACTION, dropped_fraction, parse

    csv_path = tmp_path / 'batchy.csv'
    _write_gcs_csv(csv_path, [
        _gcs_row(3_000_000, '10.0.0.9', 'POST', '', 'b1', '', req_id='batch'),
        *[
            _gcs_row(3_000_000, '10.0.0.9', 'PUT', 'PUT_Object', 'b1', f'p/{i}.gz',
                     cs_bytes=i, req_id='batch')
            for i in range(50)
        ],
    ])
    from disk_tree.access.parsers.gcs import RAW_VIEW

    con = duckdb.connect()
    n_out = parse(str(csv_path), con=con).aggregate('COUNT(*)').fetchone()[0]
    n_in, dropped = dropped_fraction(con, n_out)
    assert (n_in, n_out, dropped) == (51, 51, 0.0)
    assert dropped <= DEDUPE_WARN_FRACTION

    # And demonstrate — not merely assert in prose — that the retired key loses
    # the batch and that the guard would have caught it. `parse` leaves the
    # pre-dedupe view on the connection, so the old key can be run against the
    # very same input.
    old_out = con.execute(
        f'SELECT COUNT(*) FROM (SELECT DISTINCT ON (s_request_id) * FROM {RAW_VIEW})'
    ).fetchone()[0]
    _, old_dropped = dropped_fraction(con, old_out)
    assert (old_out, round(old_dropped, 4)) == (1, 0.9804)
    assert old_dropped > DEDUPE_WARN_FRACTION


def test_batch_records_and_duplicate_records_are_told_apart(tmp_path: Path):
    """The two phenomena that share an `s_request_id`, in one file.

    Google: "Occasionally, a single record may appear twice... you can use the
    s_request_id field to detect duplicates." *Detect*, not key on — a batched
    write is one request emitting many distinct records. Keeping the batch and
    dropping the repeat is the entire contract of this parser's dedupe.

    The batch here mirrors the measured shape: records differing only by object
    name. The leading no-operation record is included because such rows do
    occur in real logs, not because every batch carries one.
    """
    from disk_tree.access.parsers.gcs import parse

    csv_path = tmp_path / 'both.csv'
    _write_gcs_csv(csv_path, [
        # A batch: one envelope + three objects, all under req_id='batch'.
        _gcs_row(3_000_000, '10.0.0.9', 'POST', '', 'b1', '', req_id='batch'),
        _gcs_row(3_000_000, '10.0.0.9', 'PUT', 'PUT_Object', 'b1', 'p/a.gz', cs_bytes=11, req_id='batch'),
        _gcs_row(3_000_000, '10.0.0.9', 'PUT', 'PUT_Object', 'b1', 'p/b.gz', cs_bytes=22, req_id='batch'),
        _gcs_row(3_000_000, '10.0.0.9', 'PUT', 'PUT_Object', 'b1', 'p/c.gz', cs_bytes=33, req_id='batch'),
        # A duplicate: the same record delivered twice, under its own id.
        _gcs_row(4_000_000, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'q/x', sc_bytes=7, req_id='twice'),
        _gcs_row(4_000_000, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'q/x', sc_bytes=7, req_id='twice'),
    ])
    con = duckdb.connect()
    df = parse(str(csv_path), con=con).df().sort_values(['request_id', 'path']).reset_index(drop=True)
    assert list(zip(df['request_id'], df['op'], df['path'], df['bytes_in'], df['bytes_out'])) == [
        ('batch', 'OTHER', '', 0, 0),
        ('batch', 'PUT', 'p/a.gz', 11, 0),
        ('batch', 'PUT', 'p/b.gz', 22, 0),
        ('batch', 'PUT', 'p/c.gz', 33, 0),
        ('twice', 'GET', 'q/x', 0, 7),
    ]


def test_gcs_parse_keeps_every_object_of_a_batch_request(tmp_path: Path):
    """A batched write logs one envelope line plus one line per object, all
    sharing an `s_request_id`. Deduping on the id alone discards every object
    but one — measured at 2,408 lost PUTs in a 7h window on marin-us-east1,
    the fleet's *smallest* bucket.
    """
    from disk_tree.access.parsers.gcs import parse

    csv_path = tmp_path / 'batch.csv'
    _write_gcs_csv(csv_path, [
        _gcs_row(3_000_000, '10.0.0.9', 'POST', '', 'b1', '', req_id='batch'),
        _gcs_row(3_000_000, '10.0.0.9', 'PUT', 'PUT_Object', 'b1', 'p/a.gz', cs_bytes=11, req_id='batch'),
        _gcs_row(3_000_000, '10.0.0.9', 'PUT', 'PUT_Object', 'b1', 'p/b.gz', cs_bytes=22, req_id='batch'),
        _gcs_row(3_000_000, '10.0.0.9', 'PUT', 'PUT_Object', 'b1', 'p/c.gz', cs_bytes=33, req_id='batch'),
    ])
    con = duckdb.connect()
    df = parse(str(csv_path), con=con).df().sort_values('path').reset_index(drop=True)
    assert df['path'].tolist() == ['', 'p/a.gz', 'p/b.gz', 'p/c.gz']
    assert df['op'].tolist() == ['OTHER', 'PUT', 'PUT', 'PUT']
    assert df['bytes_in'].tolist() == [0, 11, 22, 33]
    assert df['request_id'].tolist() == ['batch'] * 4


# ---------- End-to-end: parse → agg → top ----------

def _write_multiday_fixture(csv_path: Path) -> None:
    """Two days, three top-level prefixes, mixed ops. Exercises rollup + tree
    synthesis simultaneously."""
    day1 = 1755302400_000_000  # 2025-08-16T00:00:00Z (arbitrary)
    day2 = day1 + 86_400_000_000  # +1 day
    _write_gcs_csv(csv_path, [
        # day1, hot: tokenized/finemath — 3 GETs, 3 KB egress each from 2 distinct IPs
        _gcs_row(day1 + 100, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'tokenized/finemath/a.bin',
                 sc_bytes=3000, req_id='d1r1'),
        _gcs_row(day1 + 200, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'tokenized/finemath/b.bin',
                 sc_bytes=3000, req_id='d1r2'),
        _gcs_row(day1 + 300, '10.0.0.2', 'GET', 'GET_Object', 'b1', 'tokenized/finemath/a.bin',
                 sc_bytes=3000, req_id='d1r3'),
        # day1, other prefixes
        _gcs_row(day1 + 400, '10.0.0.3', 'GET', 'GET_Object', 'b1', 'logs/2026-01-01.log',
                 sc_bytes=100, req_id='d1r4'),
        _gcs_row(day1 + 500, '10.0.0.1', 'LIST', 'LIST_Bucket', 'b1', '',
                 sc_bytes=50, req_id='d1r5'),
        # day2, mostly PUTs to a new prefix
        _gcs_row(day2 + 100, '10.0.0.1', 'PUT', 'PUT_Object', 'b1', 'uploads/x.dat',
                 cs_bytes=5000, req_id='d2r1'),
        _gcs_row(day2 + 200, '10.0.0.1', 'PUT', 'PUT_Object', 'b1', 'uploads/y.dat',
                 cs_bytes=5000, req_id='d2r2'),
    ])


def test_agg_produces_layer2a_tree(tmp_path: Path):
    """Aggregation output is a proper tree — every leaf path has all its
    ancestors synthesized, with correct bytes_out / n_ops rollup."""
    from disk_tree.access.aggregate import aggregate_access
    from disk_tree.access.parsers.gcs import parse

    csv_path = tmp_path / 'usage.csv'
    _write_multiday_fixture(csv_path)

    con = duckdb.connect()
    raw = str(tmp_path / 'raw.parquet')
    parse(str(csv_path), con=con).write_parquet(raw)

    out = str(tmp_path / 'agg.parquet')
    stats = aggregate_access(con, f"(SELECT * FROM read_parquet('{raw}'))", out)

    assert stats == {'rows_in': 7, 'paths_out': 10, 'hours': 2, 'total_bytes_out': 9150}

    df = pd.read_parquet(out)
    # Hour grain (spec specs/done/mgu-scale-unification.md D.1), UTC, naive TIMESTAMP.
    assert df['hour'].dtype == 'datetime64[us]'
    assert sorted(df['hour'].unique()) == [
        pd.Timestamp('2025-08-16 00:00:00'), pd.Timestamp('2025-08-17 00:00:00'),
    ]
    day1, day2 = sorted(df['hour'].unique())

    # Bucket is a first-class key on every row.
    assert set(df['bucket'].unique()) == {'b1'}

    # Every path present at every day/op it participates in.
    # tokenized/finemath: day1 GET → 3 ops, 9000 bytes_out.
    tf = df[(df.path == 'tokenized/finemath') & (df.op == 'GET')].iloc[0]
    assert int(tf['n_ops']) == 3
    assert int(tf['bytes_out']) == 9000
    # tokenized (parent of finemath): same 3 ops rolled up.
    tok = df[(df.path == 'tokenized') & (df.op == 'GET')].iloc[0]
    assert int(tok['n_ops']) == 3
    assert int(tok['bytes_out']) == 9000
    # last_ts (atime) = the most recent request under the path: the fixture's
    # three finemath GETs land at +100/+200/+300µs past day1 — MAX propagates
    # to the file rows' synthesized ancestors unchanged.
    assert tf['last_ts'] == tok['last_ts']
    assert int(tf['last_ts'].timestamp() * 1e6) == 1755302400_000_000 + 300
    # Root (`.`): day1 GET total = 3 (tokenized) + 1 (logs) = 4; bytes_out 9100.
    root_get_d1 = df[(df.path == '.') & (df.op == 'GET') & (df.hour == day1)]
    assert len(root_get_d1) == 1
    assert int(root_get_d1.iloc[0]['n_ops']) == 4
    assert int(root_get_d1.iloc[0]['bytes_out']) == 9100
    # Root's kind is 'dir' (not 'file' — even though a bucket-level LIST op
    # targeted path='').
    assert root_get_d1.iloc[0]['kind'] == 'dir'
    # day2 uploads: 2 PUTs, 10000 bytes_in.
    ul = df[(df.path == 'uploads') & (df.op == 'PUT') & (df.hour == day2)].iloc[0]
    assert int(ul['n_ops']) == 2
    assert int(ul['bytes_in']) == 10000


def test_agg_hour_grain_splits_a_day(tmp_path: Path):
    """Requests an hour apart land in separate rows; `last_ts` stays exact."""
    from disk_tree.access.aggregate import aggregate_access
    from disk_tree.access.parsers.gcs import parse

    t0 = 1755302400_000_000  # 2025-08-16T00:00:00Z
    h = 3_600_000_000
    csv_path = tmp_path / 'usage.csv'
    _write_gcs_csv(csv_path, [
        _gcs_row(t0 + 5, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'p/a', sc_bytes=10, req_id='r1'),
        _gcs_row(t0 + h - 1, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'p/a', sc_bytes=20, req_id='r2'),
        _gcs_row(t0 + h, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'p/b', sc_bytes=30, req_id='r3'),
        _gcs_row(t0 + 2 * h + 7, '10.0.0.2', 'GET', 'GET_Object', 'b1', 'p/a', sc_bytes=40, req_id='r4'),
    ])
    con = duckdb.connect()
    raw = str(tmp_path / 'raw.parquet')
    parse(str(csv_path), con=con).write_parquet(raw)
    out = str(tmp_path / 'agg.parquet')
    stats = aggregate_access(con, f"(SELECT * FROM read_parquet('{raw}'))", out)
    assert stats == {'rows_in': 4, 'paths_out': 4, 'hours': 3, 'total_bytes_out': 100}
    df = pd.read_parquet(out)
    rows = [
        (r.path, r.hour.isoformat(), r.n_ops, r.bytes_out, int(r.last_ts.timestamp() * 1e6) - t0)
        for r in df[df.path.isin(['p', 'p/a'])].itertuples()
    ]
    assert rows == [
        ('p', '2025-08-16T00:00:00', 2, 30, h - 1),
        ('p', '2025-08-16T01:00:00', 1, 30, h),
        ('p', '2025-08-16T02:00:00', 1, 40, 2 * h + 7),
        ('p/a', '2025-08-16T00:00:00', 2, 30, h - 1),
        ('p/a', '2025-08-16T02:00:00', 1, 40, 2 * h + 7),
    ]
    import pyarrow.parquet as pq
    assert {k: v for k, v in pq.read_metadata(out).metadata.items() if k != b'ARROW:schema'} == {b'grain': b'hour'}


def test_agg_as_of_cuts_at_the_instant(tmp_path: Path):
    """`as_of` drops requests at/after the instant — exact on the raw rows, so a
    mid-hour cut keeps only the earlier part of that hour (spec D.2)."""
    from disk_tree.access.aggregate import aggregate_access
    from disk_tree.access.parsers.gcs import parse

    t0 = 1755302400_000_000  # 2025-08-16T00:00:00Z
    m = 60_000_000
    csv_path = tmp_path / 'usage.csv'
    _write_gcs_csv(csv_path, [
        _gcs_row(t0 + 10 * m, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'p/a', sc_bytes=1, req_id='r1'),
        _gcs_row(t0 + 30 * m, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'p/a', sc_bytes=2, req_id='r2'),
        _gcs_row(t0 + 30 * m, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'p/b', sc_bytes=4, req_id='r3'),
        _gcs_row(t0 + 90 * m, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'p/a', sc_bytes=8, req_id='r4'),
    ])
    con = duckdb.connect()
    raw = str(tmp_path / 'raw.parquet')
    parse(str(csv_path), con=con).write_parquet(raw)

    def root_at(as_of, name):
        out = str(tmp_path / f'{name}.parquet')
        stats = aggregate_access(con, f"(SELECT * FROM read_parquet('{raw}'))", out, as_of=as_of)
        df = pd.read_parquet(out)
        import pyarrow.parquet as pq
        md = {k.decode(): v.decode() for k, v in pq.read_metadata(out).metadata.items() if k != b'ARROW:schema'}
        return stats, [(r.hour.isoformat(), r.n_ops, r.bytes_out) for r in df[df.path == '.'].itertuples()], md

    # Mid-hour: the 00:30 requests are excluded, 00:10 stays; the row is still the 00:00 hour.
    assert root_at('2025-08-16T00:30:00Z', 'a') == (
        {'rows_in': 1, 'paths_out': 3, 'hours': 1, 'total_bytes_out': 1},
        [('2025-08-16T00:00:00', 1, 1)],
        {'grain': 'hour', 'as_of': '2025-08-16T00:30:00+00:00'},
    )
    # One microsecond later: the two 00:30 requests are in.
    assert root_at('2025-08-16T00:30:00.000001Z', 'b')[1] == [('2025-08-16T00:00:00', 3, 7)]
    # Hour-aligned: exactly the first hour. A naive instant is taken as UTC.
    assert root_at('2025-08-16T01:00:00', 'c')[1] == [('2025-08-16T00:00:00', 3, 7)]
    # Everything.
    assert root_at('2025-08-17T00:00:00Z', 'd')[1] == [('2025-08-16T00:00:00', 3, 7), ('2025-08-16T01:00:00', 1, 8)]
    with pytest.raises(ValueError, match="no access rows in input before 2025-08-16T00:00:00\\+00:00"):
        root_at('2025-08-16T00:00:00Z', 'e')


def test_top_hot_prefixes(tmp_path: Path):
    """`dt access top` surfaces hottest prefix at the requested depth."""
    from disk_tree.access.aggregate import aggregate_access
    from disk_tree.access.parsers.gcs import parse

    csv_path = tmp_path / 'usage.csv'
    _write_multiday_fixture(csv_path)
    con = duckdb.connect()
    raw = str(tmp_path / 'raw.parquet')
    parse(str(csv_path), con=con).write_parquet(raw)
    agg = str(tmp_path / 'agg.parquet')
    aggregate_access(con, f"(SELECT * FROM read_parquet('{raw}'))", agg)

    # depth=1 by bytes_out: tokenized wins (9000 across day1 GET)
    rows = con.execute(f"""
        SELECT path, op, SUM(n_ops)::BIGINT AS n_ops, SUM(bytes_out)::BIGINT AS bytes_out
        FROM read_parquet('{agg}')
        WHERE depth = 1
        GROUP BY path, op
        ORDER BY bytes_out DESC
        LIMIT 3
    """).fetchall()
    # Expect: (tokenized, GET, 3, 9000), (uploads, PUT, 2, 0), (logs, GET, 1, 100)
    # Order-sensitive on bytes_out desc
    assert rows[0][0] == 'tokenized'
    assert rows[0][1] == 'GET'
    assert rows[0][3] == 9000


def test_agg_keeps_buckets_separate(tmp_path: Path):
    """Identically-named prefixes in different buckets must NOT merge — the
    layer-2a key is (bucket, path), and each bucket gets its own `.` root."""
    from disk_tree.access.aggregate import aggregate_access
    from disk_tree.access.parsers.gcs import parse

    csv_path = tmp_path / 'usage.csv'
    _write_gcs_csv(csv_path, [
        _gcs_row(1_000_000, '10.0.0.1', 'GET', 'GET_Object', 'b1', 'shared/x.bin',
                 sc_bytes=100, req_id='a1'),
        _gcs_row(2_000_000, '10.0.0.1', 'GET', 'GET_Object', 'b2', 'shared/x.bin',
                 sc_bytes=700, req_id='a2'),
    ])
    con = duckdb.connect()
    raw = str(tmp_path / 'raw.parquet')
    parse(str(csv_path), con=con).write_parquet(raw)
    out = str(tmp_path / 'agg.parquet')
    aggregate_access(con, f"(SELECT * FROM read_parquet('{raw}'))", out)

    df = pd.read_parquet(out)
    shared = df[df.path == 'shared'].sort_values('bucket').reset_index(drop=True)
    assert shared['bucket'].tolist() == ['b1', 'b2']
    assert shared['bytes_out'].tolist() == [100, 700]
    roots = df[df.path == '.'].sort_values('bucket').reset_index(drop=True)
    assert roots['bucket'].tolist() == ['b1', 'b2']
    assert roots['bytes_out'].tolist() == [100, 700]


# ---------- Unknown stores surface a clear error ----------

def test_parser_for_unknown_store_raises():
    from disk_tree.access.parsers import parser_for
    with pytest.raises(ValueError, match="no access-log parser"):
        parser_for('azure')
