"""`dt-cloud access requesters`: a day's raw request rows rolled up by bucket × op × client × requester × prefix."""

import datetime as dt

import duckdb

from dt_cloud.access_requesters import day_parts, part_window, rollup_day

UTC = dt.timezone.utc


def test_part_window_and_day_parts():
    assert part_window("x/part-2026_11_13_20_00_00--2026_11_14_01_00_00.parquet") == (
        dt.datetime(2026, 11, 13, 20, tzinfo=UTC), dt.datetime(2026, 11, 14, 2, tzinfo=UTC),
    )
    assert part_window("x/notes.json") is None
    names = [
        "b/part-2026_11_12_14_00_00--2026_11_12_19_00_00.parquet",
        "b/part-2026_11_13_20_00_00--2026_11_14_01_00_00.parquet",
        "b/part-2026_11_14_02_00_00--2026_11_14_07_00_00.parquet",
        "b/part-2026_11_14_20_00_00--2026_11_14_23_00_00.parquet",
        "b/part-2026_11_15_00_00_00--2026_11_15_05_00_00.parquet",
    ]
    assert day_parts(names, dt.date(2026, 11, 14)) == names[1:4]


def _raw(path, rows):
    con = duckdb.connect()
    con.sql("""CREATE TABLE t (ts TIMESTAMPTZ, store VARCHAR, bucket VARCHAR, path VARCHAR, op VARCHAR, op_raw VARCHAR,
               status INTEGER, bytes_out BIGINT, bytes_in BIGINT, requester VARCHAR, user_agent VARCHAR, request_id VARCHAR)""")
    for r in rows:
        con.execute("INSERT INTO t VALUES (?, 'gcs', ?, ?, ?, ?, 200, ?, 0, ?, ?, 'x')", r)
    path.parent.mkdir(parents=True, exist_ok=True)
    con.sql(f"COPY t TO '{path}' (FORMAT parquet)")


def test_rollup_day(tmp_path):
    root = tmp_path / "access"
    gcsfs_ua = "python-gcsfs/2026.1.0,gzip(gfe)"
    _raw(root / "raw/b1/part-2026_11_13_23_00_00--2026_11_14_00_00_00.parquet", [
        # the day before: left out
        ("2026-11-13T23:59:00Z", "b1", "data/tok/a.bin", "GET", "storage.objects.get", 10, "10.0.0.1", gcsfs_ua),
        ("2026-11-14T00:10:00Z", "b1", "data/tok/a.bin", "GET", "storage.objects.get", 10, "10.0.0.1", gcsfs_ua),
        ("2026-11-14T00:20:00Z", "b1", "data/tok/b.bin", "GET", "storage.objects.get", 20, "10.0.0.1", "python-gcsfs/2026.1.0"),
    ])
    _raw(root / "raw/b2/part-2026_11_14_06_00_00--2026_11_14_06_00_00.parquet", [
        ("2026-11-14T06:00:00Z", "b2", "ckpt/run-1/step-5/x", "LIST", "GET_Bucket", 1, "34.1.2.3", "gcloud-python/3.4.0 gl-python/3.13"),
        ("2026-11-14T06:01:00Z", "b2", "top", "GET", "GET", 5, "", ""),
    ])
    out = tmp_path / "out.parquet"
    assert rollup_day(str(root), dt.date(2026, 11, 14), str(out)) == 3
    rows = duckdb.sql(f"SELECT day::VARCHAR, bucket, op, op_raw, client, user_agent, requester, prefix, n, bytes_out FROM '{out}' ORDER BY bucket, op, prefix").fetchall()
    assert rows == [
        ("2026-11-14", "b1", "GET", "storage.objects.get", "python-gcsfs", "python-gcsfs/2026.1.0", "10.0.0.1", "data/tok", 2, 30),
        ("2026-11-14", "b2", "GET", "GET", None, None, None, "top", 1, 5),
        ("2026-11-14", "b2", "LIST", "GET_Bucket", "gcloud-python", "gcloud-python/3.4.0 gl-python/3.13", "34.1.2.3", "ckpt/run-1", 1, 1),
    ]
    assert rollup_day(str(root), dt.date(2026, 11, 20), str(tmp_path / "none.parquet")) == 0


def test_cli_writes_each_day_and_skip_existing_leaves_written_ones(tmp_path):
    from click.testing import CliRunner

    from dt_cloud.cli import main

    root = tmp_path / "access"
    _raw(root / "raw/b1/part-2026_11_14_00_00_00--2026_11_14_00_00_00.parquet", [
        ("2026-11-14T00:10:00Z", "b1", "data/tok/a.bin", "GET", "storage.objects.get", 10, "10.0.0.1", "python-gcsfs/2026.1.0"),
    ])
    out = tmp_path / "req"
    run = lambda *a: CliRunner().invoke(main, ["access", "requesters", "-r", str(root), "-o", str(out), *a])
    first = run("2026-11-14", "2026-11-15")
    again = run("-s", "2026-11-14")
    assert (first.exit_code, first.stdout, again.exit_code, again.stdout, sorted(p.name for p in out.iterdir())) == (
        0, "2026-11-14\t1\n2026-11-15\t0\n", 0, "", ["2026-11-14.parquet"],
    )


def test_readers_prices_each_group_by_operation_class(tmp_path):
    """`readers`: per group, Class A (listings: a bucket-root GET; writes) and Class B (object reads) requests per day,
    priced at list prices; deletes and the bucket's lifecycle engine (Autoclass moves) free; groups past `top` folded
    into one row."""
    from dt_cloud.access_requesters import readers

    def day(name, rows):
        con = duckdb.connect()
        con.sql("CREATE TABLE r (day DATE, bucket VARCHAR, op VARCHAR, op_raw VARCHAR, client VARCHAR, user_agent VARCHAR, requester VARCHAR, prefix VARCHAR, n BIGINT, bytes_out BIGINT, bytes_in BIGINT)")
        for r in rows:
            con.execute("INSERT INTO r VALUES (?, 'b', ?, ?, ?, ?, NULL, ?, ?, 0, 0)", (*r[:4], r[6] if len(r) > 6 else None, *r[4:6]))
        con.sql(f"COPY r TO '{tmp_path / name}' (FORMAT parquet)")
        return str(tmp_path / name)

    files = [
        day("2026-11-14.parquet", [
            ("2026-11-14", "GET", "storage.objects.get", "tensorstore", "cache", 3_000_000),
            ("2026-11-14", "GET", "GET", "python-gcsfs", "", 200_000),
            ("2026-11-14", "DELETE", "DELETE_Object", "python-gcsfs", "tmp", 50),
            ("2026-11-14", "GET", "storage.objects.get", "polars", "tmp", 1_000_000),
            ("2026-11-14", "PUT", "PUT_Object", "GCS", "docs", 5_000_000, "GCS Lifecycle Management"),
        ]),
        day("2026-11-15.parquet", [("2026-11-15", "GET", "storage.objects.get", "tensorstore", "cache", 1_000_000)]),
    ]
    out = readers(files, ("client", "prefix"), top=2)
    rnd = lambda r: {k: round(v, 2) if isinstance(v, float) else v for k, v in r.items()}
    assert (out["days"], [rnd(r) for r in out["rows"]], rnd(out["total"])) == (2, [
        {"client": "tensorstore", "prefix": "cache", "a_per_day": 0.0, "b_per_day": 2_000_000.0, "per_day": 2_000_000.0, "usd_per_day": 0.8},
        {"client": "python-gcsfs", "prefix": "", "a_per_day": 100_000.0, "b_per_day": 0.0, "per_day": 100_000.0, "usd_per_day": 0.5},
        {"client": "(3 others)", "prefix": "", "a_per_day": 0.0, "b_per_day": 500_000.0, "per_day": 3_000_025.0, "usd_per_day": 0.2},
    ], {"a_per_day": 100_000.0, "b_per_day": 2_500_000.0, "per_day": 5_100_025.0, "usd_per_day": 1.5})
