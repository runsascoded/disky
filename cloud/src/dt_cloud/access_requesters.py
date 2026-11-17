"""Who makes a deployment's storage requests: layer-1a's request rows (`access/raw/<bucket>/part-*.parquet`, see
`access.py`) rolled up per UTC day × bucket × operation × client × requester × path prefix, one parquet per day
(`access/requesters/<day>.parquet`). It is the per-requester table the read lens lacks: what reads an object store
(GCS bills reads as Class B operations, listings and writes as Class A), from where, through what.

A row's `client` is its user agent's first product token (`python-gcsfs`, `gcloud-python`, `Go-http-client`, …),
`user_agent` the whole string without the front end's `,gzip(gfe)` suffix, `prefix` the object path's first `depth`
segments. Requests are counted, not deduplicated: a retried read is two reads, as the bill counts it.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from functools import partial

err = partial(print, file=sys.stderr)

PART = re.compile(r"part-(\d{4})_(\d\d)_(\d\d)_(\d\d)_\d\d_\d\d--(\d{4})_(\d\d)_(\d\d)_(\d\d)_\d\d_\d\d\.parquet$")


def part_window(name: str) -> tuple[dt.datetime, dt.datetime] | None:
    """A raw part's `[first hour, last hour + 1h)` in UTC, from its name; None for another file."""
    m = PART.search(name)
    if not m:
        return None
    a = dt.datetime(*map(int, m.group(1, 2, 3, 4)), tzinfo=dt.timezone.utc)
    b = dt.datetime(*map(int, m.group(5, 6, 7, 8)), tzinfo=dt.timezone.utc)
    return a, b + dt.timedelta(hours=1)


def day_parts(names: list[str], day: dt.date) -> list[str]:
    """The parts whose window meets `day` (UTC)."""
    lo = dt.datetime.combine(day, dt.time(), dt.timezone.utc)
    hi = lo + dt.timedelta(days=1)
    out = []
    for n in names:
        w = part_window(n)
        if w and w[0] < hi and w[1] > lo:
            out.append(n)
    return sorted(out)


def rollup_sql(files: list[str], day: dt.date, depth: int) -> str:
    lo = dt.datetime.combine(day, dt.time(), dt.timezone.utc)
    hi = lo + dt.timedelta(days=1)
    return f"""
        SELECT DATE '{day.isoformat()}' AS day, bucket, op, op_raw,
               nullif(regexp_extract(coalesce(user_agent, ''), '^([^/ ,;()]+)', 1), '') AS client,
               nullif(regexp_replace(coalesce(user_agent, ''), ',gzip\\(gfe\\)$', ''), '') AS user_agent,
               nullif(requester, '') AS requester,
               array_to_string(list_slice(string_split(coalesce(path, ''), '/'), 1, {depth}), '/') AS prefix,
               count(*) AS n, sum(coalesce(bytes_out, 0)) AS bytes_out, sum(coalesce(bytes_in, 0)) AS bytes_in
        FROM read_parquet({files!r})
        WHERE ts >= TIMESTAMPTZ '{lo.isoformat()}' AND ts < TIMESTAMPTZ '{hi.isoformat()}'
        GROUP BY ALL
        ORDER BY bucket, op, n DESC
    """


def rollup_day(root: str, day: dt.date, out: str, depth: int = 2, mem: str | None = None, threads: int | None = None) -> int:
    """Write `day`'s rollup of `<root>/raw/*/part-*.parquet` (a dir or `gs://` URL) to `out` (a path or `gs://` URL);
    returns its row count. No part meets the day: nothing is written, 0."""
    import duckdb

    con = duckdb.connect()
    if mem:
        con.sql(f"SET memory_limit = '{mem}'")
    if threads:
        con.sql(f"SET threads = {int(threads)}")
    if root.startswith("gs://") or out.startswith("gs://"):
        import gcsfs

        fs = gcsfs.GCSFileSystem()
        con.register_filesystem(fs)
        names = [f"gs://{p}" for p in fs.glob(f"{root.removeprefix('gs://').rstrip('/')}/raw/*/part-*.parquet")]
    else:
        from pathlib import Path

        names = [str(p) for p in sorted(Path(root, "raw").glob("*/part-*.parquet"))]
    files = day_parts(names, day)
    if not files:
        err(f"requesters {day}: no raw parts")
        return 0
    err(f"requesters {day}: {len(files)} raw parts")
    con.sql(f"CREATE TABLE r AS {rollup_sql(files, day, depth)}")
    n = con.sql("SELECT count(*) FROM r").fetchone()[0]
    if out.startswith("gs://"):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            local = f"{tmp}/{day.isoformat()}.parquet"
            con.sql(f"COPY r TO '{local}' (FORMAT parquet, COMPRESSION zstd)")
            fs.put(local, out)
    else:
        con.sql(f"COPY r TO '{out}' (FORMAT parquet, COMPRESSION zstd)")
    err(f"requesters {day}: {n:,} rows → {out}")
    return n
