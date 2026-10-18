"""Non-live bytes, phase 1 (specs/non-live-bytes.md): what a bucket holds, and usually bills, that the scan's listing
of current objects misses, totaled per bucket and class: `dt-cloud non-live`.

Read-only: list calls only. Each class a deployment asks for (`-c`) is measured where its provider has it:

| class           | provider          | from                                                            | expires              |
|-----------------|-------------------|-----------------------------------------------------------------|----------------------|
| `soft-deleted`  | GCS               | `list_blobs(soft_deleted=True)`: `size`, `softDeleteTime`        | `hardDeleteTime`     |
| `noncurrent`    | S3, CAIOS         | `ListObjectVersions` `Versions[]`, `IsLatest = false`            | (lifecycle: phase 2) |
| `delete-marker` | S3, CAIOS         | `ListObjectVersions` `DeleteMarkers[]` (0 bytes: counted)        | (lifecycle: phase 2) |
| `mpu`           | GCS, S3, CAIOS, R2 | `ListMultipartUploads` + `ListParts` (part sizes)               | (lifecycle: phase 2) |

A class the provider can't have is refused, never reported as zero (a deployment asks only for its own). The output is
one row per `(bucket, class)` (`prefix` = `""` in phase 1): `bytes`, `objects`, the class's own clock (`oldest`,
`newest`: soft-delete time, noncurrent-since, initiated) and `expires_min`/`expires_max` where the provider says (null:
no expiry known). A class with nothing in it is a row of zeros, so "measured, none" reads apart from "not measured".
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.parquet as pq
from click import argument, command, option

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


def gcs_soft_deleted(bucket: str, prefix: str, client=None) -> Total:
    """GCS soft-deleted objects under `prefix`: sizes, soft-delete times, hard-delete (expiry) times."""
    from google.cloud import storage

    client = client or storage.Client()
    t = Total(bucket, "soft-deleted")
    for b in client.list_blobs(bucket, prefix=prefix or None, soft_deleted=True,
                               fields="items(name,size,softDeleteTime,hardDeleteTime),nextPageToken"):
        t.add(b.size, b.soft_delete_time, b.hard_delete_time)
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


@command("non-live")
@option("-c", "--classes", required=True, help=f"Comma-separated classes to measure ({', '.join(CLASSES)}); each must be one the source's provider has")
@option("-E", "--endpoint", help="S3 API endpoint (CAIOS, R2; GCS's XML API by default for its multipart uploads)")
@option("-j", "--json", "json_out", help="Merge the totals into this JSON file (a scan's `non-live.json`: every bucket's rows, this one's replaced)")
@option("-o", "--out", help="Write the totals here as parquet (a local path); else (and no -j) print them as JSON lines")
@argument("uri")
def non_live_cmd(classes, endpoint, json_out, out, uri) -> None:
    """Total the non-live bytes of one bucket (URI: `gs://`, `s3://`, `r2://`, optionally with a prefix): per class, its
    bytes, objects, oldest/newest and expiry where known (specs/non-live-bytes.md, phase 1). Read-only."""
    want = [c.strip() for c in classes.split(",") if c.strip()]
    if bad := [c for c in want if c not in CLASSES]:
        raise SystemExit(f"-c: unknown class(es) {', '.join(bad)} (classes: {', '.join(CLASSES)})")
    totals = measure(uri, want, endpoint)
    for t in totals:
        err(f"non-live {t.bucket} {t.cls}: {t.bytes:,} B in {t.objects:,}" + (f", expiring by {t.expires_max:%Y-%m-%d}" if t.expires_max else ""))
    if json_out:
        merge_json(Path(json_out), totals)
    if out is None:
        if not json_out:
            for t in totals:
                print(json.dumps(t.row(), default=str))
        return
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table(totals), out)
