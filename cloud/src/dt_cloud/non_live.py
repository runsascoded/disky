"""Non-live bytes, phase 1 (specs/non-live-bytes.md): what a bucket holds, and usually bills, that the scan's listing
of current objects misses, totaled per bucket and class: `dt-cloud non-live`.

Read-only: list calls only. Each class a deployment asks for (`-c`) is measured where its provider has it:

| class           | provider          | from                                                            | expires              |
|-----------------|-------------------|-----------------------------------------------------------------|----------------------|
| `soft-deleted`  | GCS               | `list_blobs(soft_deleted=True)`: `size`, `softDeleteTime`        | `hardDeleteTime`     |
| `noncurrent`    | S3, CAIOS         | `ListObjectVersions` `Versions[]`, `IsLatest = false`            | (lifecycle: phase 2) |
| `delete-marker` | S3, CAIOS         | `ListObjectVersions` `DeleteMarkers[]` (0 bytes: counted)        | (lifecycle: phase 2) |
| `mpu`           | GCS, S3, CAIOS, R2 | `ListMultipartUploads` + `ListParts` (part sizes)               | (lifecycle: phase 2) |

`-s metric` (GCS) reads Cloud Monitoring's `storage/v2/total_bytes` / `total_count` instead: `soft-deleted`, `noncurrent`
and `mpu`, whole buckets, no clocks or expiries, one query for every bucket, and (`-S`) their daily history (`live` too).
It also counts what a soft-deleted listing can't reach: GCS refuses that listing once a bucket's policy is off, while it
still holds and bills what was deleted under the old policy (gcs 2026-11-14: ~900 TB the listing reported as 0 B).

A class the provider can't have is refused, never reported as zero (a deployment asks only for its own). The output is
one row per `(bucket, class)` (`prefix` = `""` in phase 1): `bytes`, `objects`, the class's own clock (`oldest`,
`newest`: soft-delete time, noncurrent-since, initiated) and `expires_min`/`expires_max` where the provider says (null:
no expiry known). A class with nothing in it is a row of zeros, so "measured, none" reads apart from "not measured".
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.parquet as pq
from click import Choice, IntRange, argument, command, option

from .static_names import err

CLASSES = ("soft-deleted", "noncurrent", "delete-marker", "mpu")
#: The classes each provider can have.
PROVIDER_CLASSES = {"gs": ("soft-deleted", "mpu"), "s3": ("noncurrent", "delete-marker", "mpu"), "r2": ("mpu",)}

SCHEMA = pa.schema([
    ("bucket", pa.string()), ("class", pa.string()), ("prefix", pa.string()),
    ("bytes", pa.int64()), ("objects", pa.int64()),
    ("oldest", pa.timestamp("s", "UTC")), ("newest", pa.timestamp("s", "UTC")),
    ("expires_min", pa.timestamp("s", "UTC")), ("expires_max", pa.timestamp("s", "UTC")),
])


@dataclass
class Total:
    """One class's total in one bucket (a `SCHEMA` row)."""
    bucket: str
    cls: str
    prefix: str = ""
    bytes: int = 0
    objects: int = 0
    oldest: datetime | None = None
    newest: datetime | None = None
    expires_min: datetime | None = None
    expires_max: datetime | None = None

    def add(self, size: int, at: datetime | None, expires: datetime | None = None) -> None:
        self.bytes += int(size or 0)
        self.objects += 1
        if at is not None:
            self.oldest = at if self.oldest is None else min(self.oldest, at)
            self.newest = at if self.newest is None else max(self.newest, at)
        if expires is not None:
            self.expires_min = expires if self.expires_min is None else min(self.expires_min, expires)
            self.expires_max = expires if self.expires_max is None else max(self.expires_max, expires)

    def row(self) -> dict:
        d = asdict(self)
        d["class"] = d.pop("cls")
        return d


def split_uri(uri: str) -> tuple[str, str, str]:
    """`gs://b/p` → `("gs", "b", "p/")`: the provider (`gcs://` is `gs`), bucket, and prefix (`""` or `/`-ended)."""
    u = urlparse(uri)
    scheme = {"gcs": "gs"}.get(u.scheme, u.scheme)
    if scheme not in PROVIDER_CLASSES or not u.netloc:
        raise SystemExit(f"{uri}: want gs://, s3:// or r2:// with a bucket")
    p = u.path.strip("/")
    return scheme, u.netloc, f"{p}/" if p else ""


#: GCS's answer to a soft-deleted listing of a bucket whose soft delete is off (retention 0).
NO_SOFT_DELETE = "Soft delete policy is required"


def gcs_soft_deleted(bucket: str, prefix: str, client=None) -> Total:
    """GCS soft-deleted objects under `prefix`: sizes, soft-delete times, hard-delete (expiry) times. A bucket with
    soft delete off holds none (its deletes are final): a row of zeros, not an error."""
    from google.api_core.exceptions import BadRequest

    from disk_tree.find.gcs_client import storage_client
    client = client or storage_client("non-live")
    t = Total(bucket, "soft-deleted")
    try:
        for b in client.list_blobs(bucket, prefix=prefix or None, soft_deleted=True,
                                   fields="items(name,size,softDeleteTime,hardDeleteTime),nextPageToken"):
            t.add(b.size, b.soft_delete_time, b.hard_delete_time)
    except BadRequest as e:
        if NO_SOFT_DELETE not in str(e):
            raise
        err(f"non-live gs://{bucket}: soft delete is off; nothing soft-deleted")
        return Total(bucket, "soft-deleted")
    return t


def s3_versions(client, bucket: str, prefix: str) -> tuple[Total, Total]:
    """S3 noncurrent versions (bytes; noncurrent since the next newer version's `LastModified`) and delete markers
    (counted) under `prefix`. Versions come key by key, newest first, so a noncurrent one's successor precedes it."""
    nc, dm = Total(bucket, "noncurrent"), Total(bucket, "delete-marker")
    prev_key, prev_mod = None, None  # across pages: a key's versions may span two
    for page in client.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix):
        # Each key's versions and markers, newest first (one sequence: a marker can supersede a version).
        entries = sorted([*((v["Key"], v["LastModified"], v) for v in page.get("Versions", [])),
                          *((m["Key"], m["LastModified"], {**m, "Size": None}) for m in page.get("DeleteMarkers", []))],
                         key=lambda e: (e[0], -e[1].timestamp()))
        for key, mod, e in entries:
            if e["Size"] is None:
                dm.add(0, mod)
            elif not e.get("IsLatest"):
                nc.add(e["Size"], prev_mod if key == prev_key else None)
            prev_key, prev_mod = key, mod
    return nc, dm


def mpus(client, bucket: str, prefix: str) -> Total:
    """Incomplete multipart uploads under `prefix`: their parts' bytes, by initiation time."""
    t = Total(bucket, "mpu")
    for page in client.get_paginator("list_multipart_uploads").paginate(Bucket=bucket, Prefix=prefix):
        for u in page.get("Uploads", []):
            size = sum(p["Size"] for pp in client.get_paginator("list_parts").paginate(Bucket=bucket, Key=u["Key"], UploadId=u["UploadId"])
                       for p in pp.get("Parts", []))
            t.add(size, u["Initiated"])
    return t


def s3_client(scheme: str, bucket: str, endpoint: str | None):
    """The S3 API client the lister uses for this bucket (`bulk_s3.S3BulkLister`: adaptive retries, addressing style,
    the bucket's credential profile); GCS's XML API for its multipart uploads (`storage.googleapis.com`, HMAC
    credentials in the environment)."""
    from disk_tree.blobfs import bucket_profile
    from disk_tree.find.bulk_s3 import S3BulkLister

    if scheme == "gs":
        endpoint = endpoint or "https://storage.googleapis.com"
    return S3BulkLister(scheme=scheme, endpoint_url=endpoint, profile=bucket_profile(bucket))._client()


def measure(uri: str, classes: list[str], endpoint: str | None = None) -> list[Total]:
    """`classes` of the bucket (and prefix) at `uri`, each one `Total`; a class its provider can't have is refused."""
    scheme, bucket, prefix = split_uri(uri)
    if bad := [c for c in classes if c not in PROVIDER_CLASSES[scheme]]:
        raise SystemExit(f"{uri}: {scheme} has no {', '.join(bad)} (it has {', '.join(PROVIDER_CLASSES[scheme])})")
    out: list[Total] = []
    if "soft-deleted" in classes:
        out.append(gcs_soft_deleted(bucket, prefix))
    if {"noncurrent", "delete-marker"} & set(classes):
        nc, dm = s3_versions(s3_client(scheme, bucket, endpoint), bucket, prefix)
        out += [t for t in (nc, dm) if t.cls in classes]
    if "mpu" in classes:
        out.append(mpus(s3_client(scheme, bucket, endpoint), bucket, prefix))
    for t in out:
        t.prefix = prefix
    return out


# --- GCS: Cloud Monitoring's storage metrics (`-s metric`) ---------------------------------------------------------

#: Each bucket's bytes and objects by `type` and `storage_class`, sampled every 5 min; daily means are kept ~2 years.
#: Unlike a soft-deleted listing (refused once a bucket's policy is off, while GCS still holds and bills what was
#: deleted under it), the metric counts every class GCS holds. Bucket totals only: no prefixes, clocks or expiries.
METRIC_BYTES = "storage.googleapis.com/storage/v2/total_bytes"
METRIC_COUNT = "storage.googleapis.com/storage/v2/total_count"
#: The metric's `type` → our class; `live` (the listing's own bytes) appears only in the history series.
METRIC_TYPES = {"live-object": "live", "soft-deleted-object": "soft-deleted", "noncurrent-object": "noncurrent", "multipart-upload": "mpu"}
#: The classes the metric measures, per provider.
METRIC_CLASSES = {"gs": ("soft-deleted", "noncurrent", "mpu")}
#: How far back a newest sample may be and still count as "now" (samples come every 5 min).
METRIC_FRESH = 3600
DAY = 86400


def clock() -> float:
    return time.time()


def _rfc3339(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bucket_projects(buckets: list[str]) -> dict[str, str]:
    """Each bucket's project number (`buckets.get`), which its metrics live under."""
    from .gcp import _get

    return {b: _get(f"https://storage.googleapis.com/storage/v1/b/{b}", fields="projectNumber")["projectNumber"] for b in buckets}


def fetch_metric(metric: str, project: str, buckets: list[str], start: float, end: float, daily: bool = False) -> list[dict]:
    """`metric`'s `timeSeries.list` pages for `buckets` over [start, end]: raw samples, or (`daily`) one mean per day."""
    from .gcp import pages

    names = ", ".join(json.dumps(b) for b in buckets)
    params = {"filter": f'metric.type="{metric}" resource.labels.bucket_name=one_of({names})',
              "interval.startTime": _rfc3339(start), "interval.endTime": _rfc3339(end)}
    if daily:
        params |= {"aggregation.alignmentPeriod": f"{DAY}s", "aggregation.perSeriesAligner": "ALIGN_MEAN"}
    return pages(f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries", **params)


def _series(pages: list[dict]):
    """`(bucket, class, points)` per series (storage classes not summed yet); points newest first, as the API gives."""
    for p in pages:
        for ts in p.get("timeSeries", []):
            yield ts["resource"]["labels"]["bucket_name"], METRIC_TYPES.get(ts["metric"]["labels"].get("type")), ts.get("points", [])


def _value(point: dict) -> float:
    v = point["value"]
    return float(v.get("doubleValue", v.get("int64Value", 0)))


def latest_totals(bytes_pages: list[dict], count_pages: list[dict], buckets: list[str], classes: list[str]) -> list[Total]:
    """Each bucket's `classes` now: every series' newest sample, summed over storage classes. A class with no series
    (GCS reports a type only once a bucket has held some) is a row of zeros: measured, none."""
    tot = {(b, c): Total(b, c) for b in buckets for c in classes}
    for pages, field in ((bytes_pages, "bytes"), (count_pages, "objects")):
        for b, c, pts in _series(pages):
            if (b, c) in tot and pts:
                setattr(tot[b, c], field, getattr(tot[b, c], field) + round(_value(pts[0])))
    return list(tot.values())


def daily_series(pages: list[dict], buckets: list[str], start_day: str, end_day: str) -> dict:
    """The history (`-S`): per bucket and class (`live` too), one byte total per UTC day in [start_day, end_day): each
    series' daily mean, summed over storage classes. A day the bucket has no `live` point is null in every class (the
    metric has nothing for it); a non-live class missing that day is 0 (GCS reports a type only while it holds some). A
    class that is 0 every day is left out."""
    from datetime import date, timedelta

    d0, d1 = date.fromisoformat(start_day), date.fromisoformat(end_day)
    dates = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days)]
    idx = {d: i for i, d in enumerate(dates)}
    acc: dict[str, dict[str, list[float | None]]] = {b: {} for b in buckets}
    for b, c, pts in _series(pages):
        if b not in acc or c is None:
            continue
        row = acc[b].setdefault(c, [None] * len(dates))
        for p in pts:
            # an aligned point's `endTime` closes its day: the day it covers is the one before
            day = (datetime.fromisoformat(p["interval"]["endTime"].replace("Z", "+00:00")) - timedelta(seconds=1)).date().isoformat()
            if (i := idx.get(day)) is not None:
                row[i] = (row[i] or 0) + _value(p)
    out: dict[str, dict[str, list[int | None]]] = {}
    for b, cls in acc.items():
        live = cls.get("live", [None] * len(dates))
        series = {}
        for c in ("live", *CLASSES):
            if c not in cls:
                continue
            vals = [None if live[i] is None else round(v or 0) for i, v in enumerate(cls[c])]
            if c == "live" or any(vals):
                series[c] = vals
        out[b] = series
    return {"metric": METRIC_BYTES, "dates": dates, "buckets": out}


def measure_metric(uris: list[str], classes: list[str], project: str | None, now: float, history_days: int | None) -> tuple[list[list[Total]], dict | None]:
    """`classes` of each `gs://` bucket now, from the metric (per URI, as `measure` gives), and (`history_days`) the
    daily history series over that many days before today. A prefix or a class the metric lacks is refused."""
    split = [split_uri(u) for u in uris]
    if bad := [u for u, (s, _, p) in zip(uris, split) if s not in METRIC_CLASSES or p]:
        raise SystemExit(f"-s metric: whole GCS buckets only (no prefix): {', '.join(bad)}")
    if bad := [c for c in classes if c not in METRIC_CLASSES["gs"]]:
        raise SystemExit(f"-s metric: no {', '.join(bad)} (it has {', '.join(METRIC_CLASSES['gs'])})")
    buckets = [b for _, b, _ in split]
    by_project: dict[str, list[str]] = {}
    for b, pr in ({b: project for b in buckets} if project else bucket_projects(buckets)).items():
        by_project.setdefault(str(pr), []).append(b)
    totals: dict[str, Total] = {}
    history_pages: list[dict] = []
    midnight = now - now % DAY
    for pr, bs in by_project.items():
        bp, cp = (fetch_metric(m, pr, bs, now - METRIC_FRESH, now) for m in (METRIC_BYTES, METRIC_COUNT))
        totals |= {f"{t.bucket}\0{t.cls}": t for t in latest_totals(bp, cp, bs, classes)}
        if history_days:
            history_pages += fetch_metric(METRIC_BYTES, pr, bs, midnight - history_days * DAY, midnight, daily=True)
    per = [[totals[f"{b}\0{c}"] for c in classes] for b in buckets]
    series = None
    if history_days:
        fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).date().isoformat()  # noqa: E731
        series = {"as_of": _rfc3339(now), **daily_series(history_pages, buckets, fmt(midnight - history_days * DAY), fmt(midnight))}
    return per, series


def json_row(t: Total) -> dict:
    """A row as the site reads it (`non-live.json`): times as ISO 8601 UTC seconds."""
    return {k: (v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(v, datetime) else v) for k, v in t.row().items()}


def merge_json(path: Path, totals: list[Total]) -> None:
    """`path`'s rows (`{"rows": [...]}`, every bucket measured this scan) with these totals' bucket(s) replaced, sorted by
    bucket then class; written whole (a temp file, then a rename)."""
    buckets = {t.bucket for t in totals}
    rows = [r for r in (json.loads(path.read_text())["rows"] if path.exists() else []) if r["bucket"] not in buckets]
    rows = sorted([*rows, *(json_row(t) for t in totals)], key=lambda r: (r["bucket"], CLASSES.index(r["class"])))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"rows": rows}, indent=1) + "\n")
    tmp.replace(path)


def table(totals: list[Total]) -> pa.Table:
    return pa.Table.from_pylist([t.row() for t in totals], schema=SCHEMA)


#: `-H`'s default: about the daily history Cloud Monitoring keeps for a bucket (24 months).
HISTORY_DAYS = 730


def write_series(series: dict, dest: str) -> str:
    """The history JSON at `dest` (a local path or a URL: `blobstore.Store` on its parent), replacing what's there."""
    from .blobstore import Store

    root, _, name = dest.rstrip("/").rpartition("/")
    store = Store(root or ".")
    store.write_text(name, json.dumps(series, separators=(",", ":")) + "\n")
    err(f"non-live: history ({len(series['dates'])} days, {len(series['buckets'])} buckets) → {store.uri(name)}")
    return store.uri(name)


#: Buckets measured at once (`non-live -P`): each a provider listing, I/O-bound (gcs's soft-deleted listing of ~11M
#: objects took ~45 min of a scan's `pre` stage bucket by bucket).
PROCS = 8


@command("non-live")
@option("-c", "--classes", required=True, help=f"Comma-separated classes to measure ({', '.join(CLASSES)}); each must be one the source's provider has")
@option("-E", "--endpoint", help="S3 API endpoint (CAIOS, R2; GCS's XML API by default for its multipart uploads)")
@option("-j", "--json", "json_out", help="Merge the totals into this JSON file (a scan's `non-live.json`: every bucket's rows, these buckets' replaced)")
@option("-o", "--out", help="Write each bucket's totals here as parquet (a local path; `{bucket}` in it with several URIs); else (and no -j) print them as JSON lines")
@option("-H", "--history-days", default=HISTORY_DAYS, type=IntRange(min=1), help=f"With -S: days of history before today (default {HISTORY_DAYS}: about what Monitoring keeps)")
@option("-p", "--project", help="With -s metric: the buckets' project (default: each bucket's own, from `buckets.get`)")
@option("-P", "--procs", default=PROCS, type=IntRange(min=1), help="Buckets measured at once")
@option("-s", "--source", type=Choice(["listing", "metric"]), default="listing", help="listing: the provider's list calls (any class; prefixes; clocks and expiries). metric: Cloud Monitoring's storage metrics (GCS: soft-deleted, noncurrent, mpu; whole buckets; also what a soft-deleted listing can't see once a bucket's policy is off)")
@option("-S", "--series", "series_out", multiple=True, help="With -s metric: write the daily history (every bucket's live and non-live bytes per day) as JSON here (a path or URL; repeatable)")
@argument("uris", nargs=-1, required=True)
def non_live_cmd(classes, endpoint, json_out, out, history_days, project, procs, source, series_out, uris) -> None:
    """Total the non-live bytes of each bucket (URIS: `gs://`, `s3://`, `r2://`, optionally with a prefix): per class, its
    bytes, objects, oldest/newest and expiry where known (specs/non-live-bytes.md, phase 1), `-P` buckets at once.
    Read-only. The JSON is merged once, after every bucket. `-s metric` reads the buckets' Cloud Monitoring totals
    instead of listing (one query for them all; `monitoring.timeSeries.list` on their project), and `-S` writes their
    daily history beside."""
    from concurrent.futures import ThreadPoolExecutor

    want = [c.strip() for c in classes.split(",") if c.strip()]
    if bad := [c for c in want if c not in CLASSES]:
        raise SystemExit(f"-c: unknown class(es) {', '.join(bad)} (classes: {', '.join(CLASSES)})")
    if out and len(uris) > 1 and "{bucket}" not in out:
        raise SystemExit("-o: several URIs need `{bucket}` in the path")
    if series_out and source != "metric":
        raise SystemExit("-S: the history comes from the metric (-s metric)")
    if source == "metric":
        per, series = measure_metric(list(uris), want, project, clock(), history_days if series_out else None)
        for dest in series_out:
            write_series(series, dest)
    else:
        with ThreadPoolExecutor(min(procs, len(uris))) as ex:
            per = list(ex.map(lambda u: measure(u, want, endpoint), uris))
    totals = [t for ts in per for t in ts]
    for t in totals:
        err(f"non-live {t.bucket} {t.cls}: {t.bytes:,} B in {t.objects:,}" + (f", expiring by {t.expires_max:%Y-%m-%d}" if t.expires_max else ""))
    if json_out:
        merge_json(Path(json_out), totals)
    if out is None:
        if not json_out:
            for t in totals:
                print(json.dumps(t.row(), default=str))
        return
    for uri, ts in zip(uris, per):
        path = Path(out.format(bucket=split_uri(uri)[1]))
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table(ts), path)
