"""Bucket requests attributed to the jobs that made them (specs/access-job-attribution.md phase 3), one UTC day at a
time, under an access store's root (`<data>/access`):

- `requesters-hourly/<day>.parquet` (`access requesters -H`): requests per hour × bucket × op × client × requester IP
  × path prefix;
- `hosts/<day>/<HH>.parquet` (`access hosts`, hourly): each VM's internal and external IPs while it lived;
- `jobs/<day>/{workers,tasks}.parquet` (`access jobs`, the deployment's scheduler tables): each hour's workers by
  internal address, and the tasks on each worker with their `owner`, `job` and `n_samples`.

A requester IP at an hour maps to the VM holding that external IP in the snapshot nearest the hour (within
`HOST_SLACK`, and never a VM created after the hour ended, so a reused ephemeral IP isn't misattributed); the VM's
internal IP to that hour's workers; a worker's requests split between its tasks by their `n_samples` that hour.
Written to `attributed/<day>.parquet`: the hourly rollup's columns, `n` / `bytes_out` / `bytes_in` scaled by the share
(so they still sum to the day's), plus `vm`, `owner`, `job` and `via`: `task` (attributed), `vm` (a VM no task ran on
that hour), `ip` (no VM holds the IP). `access readers -r <data>/access/attributed -b owner,job` reads it."""

from __future__ import annotations

import datetime as dt
import json
import sys
from functools import partial

err = partial(print, file=sys.stderr)

#: How far a host snapshot may be from the request's hour (a missed hourly snapshot falls back to its neighbors).
HOST_SLACK = dt.timedelta(hours=3)
#: A written day's parquet key-value metadata: its inputs (`{path relative to the root: size}`, JSON), so a later run
#: re-attributes a day whose inputs changed (a re-rolled hour, jobs written later) and skips one that didn't.
INPUTS_KEY = "disky.inputs"


class Root:
    """An access store root (a dir or `gs://` URL): listing, sizes, reads and puts."""

    def __init__(self, root: str):
        self.root = root.rstrip("/")
        self.gs = self.root.startswith("gs://")
        if self.gs:
            import gcsfs

            self.fs = gcsfs.GCSFileSystem()

    def sizes(self, pattern: str) -> dict[str, int]:
        """`{relative path: size}` of the files matching `pattern` (a glob relative to the root)."""
        if self.gs:
            hits = self.fs.glob(f"{self.root.removeprefix('gs://')}/{pattern}", detail=True)
            base = self.root.removeprefix("gs://") + "/"
            return {k.removeprefix(base): int(v["size"]) for k, v in hits.items() if v.get("type") != "directory"}
        from pathlib import Path

        return {str(p.relative_to(self.root)): p.stat().st_size for p in Path(self.root).glob(pattern) if p.is_file()}

    def url(self, rel: str) -> str:
        return f"{self.root}/{rel}"

    def metadata(self, rel: str) -> dict[bytes, bytes] | None:
        import pyarrow.parquet as pq

        if self.gs:
            if not self.fs.exists(self.url(rel)):
                return None
            with self.fs.open(self.url(rel), "rb") as f:
                return pq.read_metadata(f).metadata or {}
        from pathlib import Path

        return (pq.read_metadata(self.url(rel)).metadata or {}) if Path(self.url(rel)).exists() else None


def inputs(root: Root, day: dt.date) -> dict[str, int]:
    """`day`'s inputs and their sizes: its hourly rollup, the host snapshots of the day and its neighbors' (for the
    slack at the day's edges), and its job tables."""
    out: dict[str, int] = {}
    out.update(root.sizes(f"requesters-hourly/{day}.parquet"))
    for d in (day - dt.timedelta(days=1), day, day + dt.timedelta(days=1)):
        out.update(root.sizes(f"hosts/{d}/*.parquet"))
    for name in ("workers", "tasks"):
        out.update(root.sizes(f"jobs/{day}/{name}.parquet"))
    return dict(sorted(out.items()))


def attribute_sql(req: str, hosts: list[str], workers: str | None, tasks: str | None) -> list[str]:
    """The statements building table `att` from the day's inputs (URLs or paths)."""
    slack = int(HOST_SLACK.total_seconds())
    stmts = [
        f"CREATE TABLE req AS SELECT * FROM read_parquet('{req}')",
        "CREATE TABLE hosts AS SELECT ts, external_ip, internal_ip, name AS vm, try_cast(created AS TIMESTAMPTZ) AS created "
        + (f"FROM read_parquet({hosts!r}) WHERE external_ip IS NOT NULL" if hosts else
           "FROM (SELECT NULL::TIMESTAMPTZ AS ts, NULL::VARCHAR AS external_ip, NULL::VARCHAR AS internal_ip, NULL::VARCHAR AS name, NULL::VARCHAR AS created) WHERE false"),
        # each (requester, hour): the host holding that IP in the snapshot nearest the hour's middle, within the slack,
        # not created after the hour ended
        f"""CREATE TABLE ipmap AS SELECT k.requester, k.hour, h.internal_ip, h.vm FROM (SELECT DISTINCT requester, hour FROM req
            WHERE requester IS NOT NULL) k JOIN LATERAL (SELECT internal_ip, vm FROM hosts h WHERE h.external_ip = k.requester
              AND (h.created IS NULL OR h.created < k.hour + INTERVAL 1 HOUR)
              AND abs(epoch(h.ts) - epoch(k.hour + INTERVAL 30 MINUTE)) <= {slack}
            ORDER BY abs(epoch(h.ts) - epoch(k.hour + INTERVAL 30 MINUTE)), h.vm LIMIT 1) h ON TRUE""",
    ]
    if workers and tasks:
        # each (hour, internal IP): its tasks' shares of the hour's samples (a VM with several workers or tasks splits)
        stmts.append(f"""CREATE TABLE shares AS SELECT w.hour, w.internal_ip, t.owner, t.job,
              t.n_samples / sum(t.n_samples) OVER (PARTITION BY w.hour, w.internal_ip) AS share
            FROM (SELECT DISTINCT hour::TIMESTAMPTZ AS hour, worker_id, split_part(address, ':', 1) AS internal_ip
                  FROM read_parquet('{workers}')) w
            JOIN (SELECT hour::TIMESTAMPTZ AS hour, worker_id, owner, job, sum(n_samples) AS n_samples
                  FROM read_parquet('{tasks}') GROUP BY ALL) t ON t.hour = w.hour AND t.worker_id = w.worker_id""")
    else:
        stmts.append("CREATE TABLE shares AS SELECT NULL::TIMESTAMPTZ AS hour, NULL::VARCHAR AS internal_ip, NULL::VARCHAR AS owner, NULL::VARCHAR AS job, NULL::DOUBLE AS share WHERE false")
    stmts.append("""CREATE TABLE att AS SELECT r.* EXCLUDE (n, bytes_out, bytes_in), m.vm, s.owner, s.job,
          CASE WHEN s.share IS NOT NULL THEN 'task' WHEN m.vm IS NOT NULL THEN 'vm' ELSE 'ip' END AS via,
          r.n * coalesce(s.share, 1) AS n, r.bytes_out * coalesce(s.share, 1) AS bytes_out, r.bytes_in * coalesce(s.share, 1) AS bytes_in
        FROM req r
        LEFT JOIN ipmap m ON m.requester = r.requester AND m.hour = r.hour
        LEFT JOIN shares s ON s.hour = r.hour AND s.internal_ip = m.internal_ip""")
    return stmts


def attribute_day(root: str, day: dt.date, *, skip_unchanged: bool = False) -> dict:
    """Write `<root>/attributed/<day>.parquet` (see the module doc). `skip_unchanged`: a day already written from the
    same inputs (`INPUTS_KEY`) is left. Returns `{day, status: written | unchanged | no-rollup, rows?, share?}`, `share`
    being each `via`'s fraction of the day's requests."""
    import duckdb

    r = Root(root)
    ins = inputs(r, day)
    rel = f"attributed/{day}.parquet"
    if f"requesters-hourly/{day}.parquet" not in ins:
        return {"day": str(day), "status": "no-rollup"}
    if skip_unchanged and (md := r.metadata(rel)) is not None and md.get(INPUTS_KEY.encode()) == json.dumps(ins).encode():
        return {"day": str(day), "status": "unchanged"}
    con = duckdb.connect()
    con.sql("SET TimeZone = 'UTC'")
    if r.gs:
        con.register_filesystem(r.fs)
    hosts = [r.url(k) for k in ins if k.startswith("hosts/")]
    jobs = {k.rsplit("/", 1)[-1].removesuffix(".parquet"): r.url(k) for k in ins if k.startswith("jobs/")}
    for s in attribute_sql(r.url(f"requesters-hourly/{day}.parquet"), hosts, jobs.get("workers"), jobs.get("tasks")):
        con.sql(s)
    rows = con.sql("SELECT count(*) FROM att").fetchone()[0]
    share = {via: round(f, 4) for via, f in con.sql("SELECT via, sum(n) / (SELECT sum(n) FROM att) FROM att GROUP BY 1 ORDER BY 1").fetchall()}
    meta = json.dumps(ins).replace("'", "''")
    copy = f"(FORMAT parquet, COMPRESSION zstd, KV_METADATA {{'{INPUTS_KEY}': '{meta}'}})"
    if r.gs:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            con.sql(f"COPY (SELECT * FROM att ORDER BY hour, bucket, n DESC) TO '{tmp}/a.parquet' {copy}")
            r.fs.put(f"{tmp}/a.parquet", r.url(rel))
    else:
        from pathlib import Path

        Path(r.url(rel)).parent.mkdir(parents=True, exist_ok=True)
        con.sql(f"COPY (SELECT * FROM att ORDER BY hour, bucket, n DESC) TO '{r.url(rel)}' {copy}")
    err(f"attributed {day}: {rows:,} rows, " + ", ".join(f"{k} {v:.1%}" for k, v in share.items()) + f" → {r.url(rel)}")
    return {"day": str(day), "status": "written", "rows": rows, "share": share}
