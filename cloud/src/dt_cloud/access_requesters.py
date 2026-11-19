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
import os
import re
import sys
from functools import partial

# gcsfs ≥ 2026.8.1's adaptive prefetcher leaves a read handle that blocks interpreter exit (blobfs.py's note): a
# rollup that wrote its day never exits. Off before gcsfs is imported.
os.environ.setdefault("USE_EXPERIMENTAL_ADAPTIVE_PREFETCHING", "false")

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


def rollup_sql(files: list[str], day: dt.date, depth: int, hourly: bool = False) -> str:
    """`day`'s rows grouped per bucket × op × client × requester × prefix (`depth` segments); `hourly`: per UTC hour
    too (an `hour` column), for joining requesters to what ran on each host at the time."""
    lo = dt.datetime.combine(day, dt.time(), dt.timezone.utc)
    hi = lo + dt.timedelta(days=1)
    return f"""
        SELECT DATE '{day.isoformat()}' AS day, {"date_trunc('hour', ts) AS hour, " if hourly else ''}bucket, op, op_raw,
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


#: A rollup's parquet key-value metadata: the raw parts it was cut from (`part_key`s, sorted, JSON), so a later run
#: can tell a day the access ingest has since added to (its parts changed) from one already complete.
PARTS_KEY = "disky.parts"


def part_key(name: str) -> str:
    """A raw part's name relative to `raw/`: `<bucket>/part-….parquet`."""
    return "/".join(name.rstrip("/").split("/")[-2:])


def raw_parts(root: str) -> list[str]:
    """Every raw part under `<root>/raw/*/` (a dir or `gs://` URL), as paths DuckDB reads."""
    if root.startswith("gs://"):
        import gcsfs

        return sorted(f"gs://{p}" for p in gcsfs.GCSFileSystem().glob(f"{root.removeprefix('gs://').rstrip('/')}/raw/*/part-*.parquet"))
    from pathlib import Path

    return [str(p) for p in sorted(Path(root, "raw").glob("*/part-*.parquet"))]


def written_parts(path: str) -> list[str] | None:
    """The raw parts a written rollup was cut from (`PARTS_KEY`); None for a rollup that doesn't record them (written
    before it did) or no rollup at all."""
    import json

    import pyarrow.parquet as pq

    if path.startswith("gs://"):
        import gcsfs

        fs = gcsfs.GCSFileSystem()
        if not fs.exists(path):
            return None
        with fs.open(path, "rb") as f:
            md = pq.read_metadata(f).metadata or {}
    else:
        from pathlib import Path

        if not Path(path).exists():
            return None
        md = pq.read_metadata(path).metadata or {}
    raw = md.get(PARTS_KEY.encode())
    return None if raw is None else json.loads(raw)


def rollup_day(root: str, day: dt.date, out: str, depth: int = 2, mem: str | None = None, threads: int | None = None,
               names: list[str] | None = None, hourly: bool = False) -> int:
    """Write `day`'s rollup of `<root>/raw/*/part-*.parquet` (a dir or `gs://` URL; `names`: that listing, when the
    caller has it) to `out` (a path or `gs://` URL), recording its parts (`PARTS_KEY`); returns its row count. No part
    meets the day: nothing is written, 0. `hourly`: per UTC hour too (`rollup_sql`)."""
    import json

    import duckdb

    con = duckdb.connect()
    con.sql("SET TimeZone = 'UTC'")  # an `hour` is a UTC hour, whatever the host's zone
    if mem:
        con.sql(f"SET memory_limit = '{mem}'")
    if threads:
        con.sql(f"SET threads = {int(threads)}")
    if root.startswith("gs://") or out.startswith("gs://"):
        import gcsfs

        fs = gcsfs.GCSFileSystem()
        con.register_filesystem(fs)
    files = day_parts(raw_parts(root) if names is None else names, day)
    if not files:
        err(f"requesters {day}: no raw parts")
        return 0
    err(f"requesters {day}: {len(files)} raw parts")
    con.sql(f"CREATE TABLE r AS {rollup_sql(files, day, depth, hourly)}")
    n = con.sql("SELECT count(*) FROM r").fetchone()[0]
    parts = json.dumps(sorted(part_key(f) for f in files)).replace("'", "''")
    copy = f"(FORMAT parquet, COMPRESSION zstd, KV_METADATA {{'{PARTS_KEY}': '{parts}'}})"
    if out.startswith("gs://"):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            local = f"{tmp}/{day.isoformat()}.parquet"
            con.sql(f"COPY r TO '{local}' {copy}")
            fs.put(local, out)
    else:
        con.sql(f"COPY r TO '{out}' {copy}")
    err(f"requesters {day}: {n:,} rows → {out}")
    return n


#: List prices per million requests (GCS Standard, regional/autoclass): Class A writes and listings, Class B reads.
PRICE_PER_M = {"A": 5.0, "B": 0.40}

#: A request's operation class: listings and writes are A, object reads and metadata B, deletes free, and so is the
#: bucket's own lifecycle engine (`GCS Lifecycle Management`: TTL deletes, and Autoclass's moves to a colder class,
#: which its management fee covers). Autoclass's moves back to a warmer class are Class A but aren't told apart in the
#: logs; `autoclass/transition_operation_count` (Cloud Monitoring) counts them by direction.
OP_CLASS_SQL = """CASE
  WHEN user_agent = 'GCS Lifecycle Management' THEN 'free'
  WHEN op = 'DELETE' OR op_raw IN ('DELETE_Object', 'storage.objects.delete', 'POST_Nested_Delete_Multi_Objects') THEN 'free'
  WHEN op = 'LIST' OR op_raw IN ('GET_Bucket', 'storage.objects.list') OR (op = 'GET' AND prefix = '') THEN 'A'
  WHEN op IN ('GET', 'HEAD') THEN 'B'
  ELSE 'A' END"""


def readers(files: list[str], by: tuple[str, ...] = ("client", "bucket", "prefix"), top: int = 15) -> dict:
    """Who made the requests over the rollups `files` (`<day>.parquet`s): per `by` group, requests per day and each
    class's share, priced at `PRICE_PER_M`; the top `top` groups by estimated cost, then the rest as one row."""
    import duckdb

    if not files:
        return {"days": 0, "rows": [], "total": {}}
    con = duckdb.connect()
    if any(f.startswith("gs://") for f in files):
        import gcsfs

        con.register_filesystem(gcsfs.GCSFileSystem())
    con.sql(f"CREATE VIEW r AS SELECT *, {OP_CLASS_SQL} AS cls FROM read_parquet({files!r})")
    days = con.sql("SELECT count(DISTINCT day) FROM r").fetchone()[0]
    keys = ", ".join(f"coalesce(CAST({k} AS VARCHAR), '') AS {k}" for k in by)
    groups = con.sql(f"""
        SELECT {keys}, sum(n) FILTER (cls = 'A') AS a, sum(n) FILTER (cls = 'B') AS b, sum(n) AS n
        FROM r GROUP BY ALL""").fetchall()
    rows = []
    for g in groups:
        a, b, n = (g[len(by)] or 0), (g[len(by) + 1] or 0), g[len(by) + 2]
        usd = (a * PRICE_PER_M["A"] + b * PRICE_PER_M["B"]) / 1e6 / days
        rows.append({**dict(zip(by, g[:len(by)])), "a_per_day": a / days, "b_per_day": b / days, "per_day": n / days, "usd_per_day": usd})
    rows.sort(key=lambda r: -r["usd_per_day"])
    head, rest = rows[:top], rows[top:]
    if rest:
        head.append({**{k: f"({len(rest)} others)" for k in by[:1]}, **{k: "" for k in by[1:]},
                     **{f: sum(r[f] for r in rest) for f in ("a_per_day", "b_per_day", "per_day", "usd_per_day")}})
    total = {f: sum(r[f] for r in rows) for f in ("a_per_day", "b_per_day", "per_day", "usd_per_day")}
    return {"days": days, "by": list(by), "rows": head, "total": total}
