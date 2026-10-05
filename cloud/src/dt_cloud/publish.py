"""Publish a scan's *served* artifacts from the GCS scan store to R2.

The site's read path (`/data/*`, `/api/subtree|diff|series|path-index`, the
`/v1/files` browser) serves derived artifacts — snapshot JSONs, the index tiers
+ their `.groups.json` manifests, the layer-2 parquets. Those are copies already,
so they can live wherever serving is cheapest: colocated with the CF Worker in
R2 (no cross-provider round trips, no egress on reads). An ingest that keeps
building against GCS (cw's GCP Batch job) runs this as its final "publish to
the serving cloud" stage (cw-s3 specs/done/r2-serving-migration.md §3); a deploy
whose ingest already writes to R2 (r2.rbw.sh) never needs it.

The served subset's layout is a deployment parameter: snapshots live under
`snapshots/<SNAPSHOTS_SUBDIR>/<scan>/` (the site's `snapshotsPrefix`), and the
layer-2 dir — canonical parquets, `index/<gen>/` tiers, `.groups.json`, age
pyramids — under `LAYER2_PREFIX` (a `{scan}` template; default the base's
`listing/{scan}/index/`, cw's is `cw-l2/{scan}/`).

Idempotent: an object already in R2 with the same size and md5 is skipped, so a
re-run (or a backfill over every scan) only moves what's missing or changed.
The GCS md5 travels as R2 object metadata (`gcs-md5`) because a multipart
upload's ETag isn't an md5.

R2 is reached through its S3-compatible API; creds come from the env:
  R2_ENDPOINT           https://<account id>.r2.cloudflarestorage.com
  R2_BUCKET             the serving bucket (the `r2_bucket` CF stack output)
  R2_ACCESS_KEY_ID      an R2 API token's id       (`r2_s3_access_key_id` output)
  R2_SECRET_ACCESS_KEY  sha256 of the token value  (`r2_s3_secret_access_key` output)
"""
from __future__ import annotations

import base64
import re
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:

    from mypy_boto3_s3.client import S3Client

err = partial(print, file=sys.stderr)

# The GCS md5 rides along as object metadata: the durable identity check a
# re-run compares against (R2's multipart ETag is not an md5).
MD5_META = "gcs-md5"
# The base's layer-2 dir: the tiers under `listing/<date>/index/<gen>/` (the raw
# listing shards beside them under `listing/<date>/<bucket>/` are NOT served).
# cw keeps its layer 2 under `cw-l2/<scan>/`.
LAYER2_PREFIX = os.environ.get("LAYER2_PREFIX", "listing/{scan}/index/")
SNAPSHOTS_SUBDIR = os.environ.get("SNAPSHOTS_SUBDIR", "")


def snapshots_prefix(scan: str, subdir: str = SNAPSHOTS_SUBDIR) -> str:
    """`snapshots/<subdir>/<scan>/`, or `snapshots/<scan>/` for the default
    (no-subdir) store — mirrors the site's `snapshotsPrefix`."""
    sub = subdir.strip("/")
    return f"snapshots/{sub}/{scan}/" if sub else f"snapshots/{scan}/"


def served_prefixes(scan: str, subdir: str = SNAPSHOTS_SUBDIR, layer2: str = LAYER2_PREFIX) -> list[str]:
    """The key prefixes that make up one scan's served subset: the published
    snapshot JSONs and the layer-2 dir, which holds `index/<gen>/` — every
    tier + `.groups.json` manifest + age pyramid the index reader serves — and,
    on cw, the canonical parquets the file browser opens."""
    return [snapshots_prefix(scan, subdir), layer2.format(scan=scan)]


def md5_hex(b64: str | None) -> str | None:
    """GCS reports `md5_hash` base64; R2/S3 ETags are hex."""
    return base64.b64decode(b64).hex() if b64 else None


@dataclass(frozen=True)
class Obj:
    """A source object: what's needed to decide + copy."""
    key: str
    size: int
    md5: str | None
    content_type: str | None = None


@dataclass(frozen=True)
class Dest:
    """What R2 knows about an existing object."""
    size: int
    md5: str | None


def dest_from_head(head: dict[str, Any] | None) -> Dest | None:
    """A HEAD response → `Dest`; None when the object is absent. The md5 is the
    `gcs-md5` metadata a previous publish stamped, else the ETag when it is a
    plain (single-part) md5, else unknown."""
    if head is None:
        return None
    md5 = (head.get("Metadata") or {}).get(MD5_META)
    if md5 is None:
        etag = (head.get("ETag") or "").strip('"')
        md5 = etag if etag and "-" not in etag else None
    return Dest(size=int(head["ContentLength"]), md5=md5)


def should_copy(src: Obj, dst: Dest | None) -> bool:
    """Copy when the object is missing, a different size, or a different md5
    (when both sides know one). Same size + unknown md5 on either side counts
    as present — the size check is the cheap floor; md5 is the tiebreak."""
    if dst is None or dst.size != src.size:
        return True
    return src.md5 is not None and dst.md5 is not None and src.md5 != dst.md5


def is_listing(key: str, layer2_dir: str) -> bool:
    """A canonical per-bucket listing — a `.parquet` directly under the scan's
    layer-2 dir (`cw-l2/<scan>/<bucket>.parquet`), as opposed to the served
    tiers under `index/<gen>/`. The base's layout (`listing/<scan>/index/`) has
    none. Nothing served reads these (the site reads the tiers; only the
    `/files` viewer opened them), so a deployment may keep them out of R2
    (`publish -L`)."""
    if not key.startswith(layer2_dir):
        return False
    rest = key[len(layer2_dir):]
    return "/" not in rest and rest.endswith(".parquet") and not TIER_STEM.match(rest)


# The index artifacts `write_index` / the over-time build emit: never listings,
# even at a layer-2 dir's top level (the base's pre-generation layout put the
# tiers straight in `listing/<scan>/index/`).
TIER_STEM = re.compile(r"^(path-index|age-index|age-pyramid|over-time)(\.|-)")


# A key inside a generation dir: `<…>/index/<gen>/<file>` → `<…>/index/<gen>`
# (the form D1's `index_schema.dir` stores). Loose files directly under an
# `index/` (the base's pre-generation layout) have no generation.
GEN_KEY = re.compile(r"^(?P<dir>.*/index/[^/]+)/[^/]")


def gen_dir(key: str, layer2_dir: str) -> str | None:
    """The generation dir holding ``key``, when it is under ``layer2_dir``."""
    if not key.startswith(layer2_dir):
        return None
    m = GEN_KEY.match(key)
    return m["dir"] if m and len(m["dir"]) > len(layer2_dir) else None


def served(
    objs: list[Obj],
    layer2_dir: str,
    *,
    listings: bool = True,
    pointed: set[str] | None = None,
) -> tuple[list[Obj], dict[str, tuple[int, int]]]:
    """The objects `publish` copies, and the unpointed generations it leaves
    out (`{gen dir: (objects, bytes)}`). Without ``listings`` the canonical
    per-bucket listings stay behind; with ``pointed`` (D1's pointer dirs),
    every `index/<gen>/` none of them names does too — the site reads
    generations only through their pointers, so a superseded one in R2 is
    dead weight (specs/storage-consolidation.md phase 1). Everything outside
    a generation dir (snapshot JSONs, sidecars beside the layer-2) is kept."""
    keep = {p.rstrip("/") for p in pointed} if pointed is not None else None
    out: list[Obj] = []
    dropped: dict[str, tuple[int, int]] = {}
    for o in objs:
        if not listings and is_listing(o.key, layer2_dir):
            continue
        d = gen_dir(o.key, layer2_dir) if keep is not None else None
        if d is not None and d not in keep:
            n, b = dropped.get(d, (0, 0))
            dropped[d] = (n + 1, b + o.size)
            continue
        out.append(o)
    return out, dropped


@dataclass
class Report:
    copied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    bytes: int = 0

    def summary(self, scan: str, dry_run: bool) -> str:
        verb = "would copy" if dry_run else "copied"
        return f"publish-r2 {scan}: {verb} {len(self.copied)} ({self.bytes:,} B), skipped {len(self.skipped)} up to date"


def r2_client() -> "S3Client":
    """boto3 S3 client for R2 (creds from the env, see the module doc)."""
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("R2_ENDPOINT")
    if not endpoint:
        raise SystemExit("publish-r2: need $R2_ENDPOINT (https://<account id>.r2.cloudflarestorage.com)")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="auto",
        aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY"),
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 10, "mode": "standard"}),
    )


def r2_bucket() -> str:
    b = os.environ.get("R2_BUCKET")
    if not b:
        raise SystemExit("publish-r2: need $R2_BUCKET (the serving bucket)")
    return b


def list_source(src_bucket: str, prefixes: list[str]) -> list[Obj]:
    """Every object under the served prefixes, sorted by key."""
    from google.cloud import storage

    client = storage.Client()
    out: list[Obj] = []
    for prefix in prefixes:
        for blob in client.list_blobs(src_bucket, prefix=prefix):
            if blob.name.endswith("/"):
                continue
            out.append(Obj(key=blob.name, size=int(blob.size or 0), md5=md5_hex(blob.md5_hash), content_type=blob.content_type))
    return sorted(out, key=lambda o: o.key)


def head_dest(s3: "S3Client", bucket: str, key: str) -> Dest | None:
    from botocore.exceptions import ClientError

    try:
        return dest_from_head(s3.head_object(Bucket=bucket, Key=key))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def copy_one(src_bucket: str, s3: "S3Client", bucket: str, obj: Obj) -> None:
    """Stream one object GCS → R2 (no local spool), stamping its md5."""
    from google.cloud import storage

    blob = storage.Client().bucket(src_bucket).blob(obj.key)
    extra: dict[str, Any] = {"Metadata": {MD5_META: obj.md5} if obj.md5 else {}}
    if obj.content_type:
        extra["ContentType"] = obj.content_type
    with blob.open("rb") as f:
        s3.upload_fileobj(f, bucket, obj.key, ExtraArgs=extra)


def publish(
    scan: str,
    *,
    src_bucket: str | None = None,
    prefixes: list[str] | None = None,
    subdir: str = SNAPSHOTS_SUBDIR,
    layer2: str = LAYER2_PREFIX,
    dry_run: bool = False,
    workers: int = 8,
    listings: bool = True,
    pointed: set[str] | None = None,
) -> Report:
    """Copy the scan's served subset to R2, skipping what's already there.
    Dry-run lists the keys it would copy on stdout and touches nothing.
    `listings=False` leaves the canonical per-bucket listings (`is_listing`)
    in GCS only. ``pointed`` (the D1 pointer dirs, `index_footer.pointers`)
    leaves out every `index/<gen>/` dir none of them names (`served`); None
    copies every generation."""
    prefixes = prefixes or served_prefixes(scan, subdir, layer2)
    if src_bucket is None:
        from .deploy import data_bucket
        src_bucket = data_bucket()
    objs, dropped = served(list_source(src_bucket, prefixes), layer2.format(scan=scan), listings=listings, pointed=pointed)
    for d in sorted(dropped):
        n, b = dropped[d]
        err(f"publish-r2 {scan}: left out unpointed generation {d}/ ({n} objects, {b:,} B)")
    if not objs:
        raise SystemExit(f"publish-r2: nothing under {', '.join(prefixes)} in gs://{src_bucket}")
    s3, bucket = r2_client(), r2_bucket()
    report = Report()

    def decide(obj: Obj) -> tuple[Obj, bool]:
        return obj, should_copy(obj, head_dest(s3, bucket, obj.key))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        decisions = list(ex.map(decide, objs))
    todo = [o for o, do in decisions if do]
    report.skipped = [o.key for o, do in decisions if not do]
    if dry_run:
        for o in todo:
            print(o.key)
        report.copied = [o.key for o in todo]
        report.bytes = sum(o.size for o in todo)
        err(report.summary(scan, dry_run=True))
        return report

    def do_copy(obj: Obj) -> Obj:
        copy_one(src_bucket, s3, bucket, obj)
        err(f"  → {obj.key} ({obj.size:,} B)")
        return obj

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for obj in ex.map(do_copy, todo):
            report.copied.append(obj.key)
            report.bytes += obj.size
    err(report.summary(scan, dry_run=False))
    return report
