"""`dt-cloud soft-delete`: each bucket's soft-delete retention now, asked of its store.

A sweep's undo restores from what the bucket keeps after a delete. On GCS that is soft delete, and with the policy off GCS
refuses to restore (or list) a soft-deleted version, whatever the bucket retained when the object was deleted. An S3-style
store has no such setting: its retention is versioning plus a noncurrent-version lifecycle rule (`s3_retention_seconds`).
The site's own account can't read bucket metadata, so the job's `soft-delete` step (`scan_steps`) writes what its account
reads, one document a day under `snapshots/soft-delete/`:

    {"version": 1, "date": "2026-11-05", "as_of": "2026-11-05T07:12:03Z",
     "buckets": {"<bucket>": {"retention_seconds": 604800}, …}}        # 0 = off; null = kept until purged (S3 only)

The site reads the newest (`site/functions/_lib/softDeleteNow.ts`): a recorded undo on a bucket it shows off reads
"undo unavailable", and a bucket it doesn't name is unknown there. `sweep undo` doesn't read it: it asks GCS itself
(`sweep_exec.undo_run`), and `plan-sweep undo` reports per key what the bucket kept (`sweep.undo_run`). Read-only, except
`-o`'s document.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from click import UsageError, command, option

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

#: The `-o` document's shape; the site reads only a version it knows.
SNAPSHOT_VERSION = 1

#: A run's recorded `soft_delete_days` for a bucket that keeps deleted data until purged (`deletion_runs` has no other way
#: to say it: NULL there means not recorded). `site/src/softDelete.ts` `NO_EXPIRY_DAYS`: keep identical.
NO_EXPIRY_DAYS = 36500


def s3_retention_seconds(versioning: Mapping, rules: Sequence[Mapping]) -> int | None:
    """An S3-style bucket's retention, from `GetBucketVersioning` and its lifecycle rules. 0: a new delete there isn't
    recoverable (versioning `Suspended` or never enabled, whatever older versions it still holds); None: kept until purged
    (`Enabled`, no noncurrent rule); else the smallest enabled `NoncurrentDays`, since a rule may cover only some prefixes."""
    if versioning.get("Status") != "Enabled":
        return 0
    days = [
        r["NoncurrentVersionExpiration"]["NoncurrentDays"]
        for r in rules
        if r.get("Status") == "Enabled" and "NoncurrentDays" in r.get("NoncurrentVersionExpiration", {})
    ]
    return min(days) * 86400 if days else None


def read_s3_bucket(client: "S3Client", bucket: str) -> dict:
    """What `s3_retention_seconds` needs of `bucket`. A bucket with no lifecycle configuration has no rules."""
    from botocore.exceptions import ClientError

    versioning = client.get_bucket_versioning(Bucket=bucket)
    if versioning.get("Status") != "Enabled":
        return {"Versioning": versioning, "Rules": []}
    try:
        rules = client.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchLifecycleConfiguration":
            raise
        rules = []
    return {"Versioning": versioning, "Rules": rules}


def retention_days(seconds: int | None) -> int:
    """A retention as a run records it: whole days, `NO_EXPIRY_DAYS` for kept until purged."""
    return NO_EXPIRY_DAYS if seconds is None else seconds // 86400


def retention_seconds(meta: Mapping) -> int | None:
    """A bucket's retention, from `read_s3_bucket`'s answer or a Storage JSON API bucket resource (0: off; a GCS bucket with
    no policy reads the same)."""
    if "Versioning" in meta:
        return s3_retention_seconds(meta["Versioning"], meta["Rules"])
    return int(meta.get("softDeletePolicy", {}).get("retentionDurationSeconds", 0))


def snapshot(metas: Mapping[str, Mapping], as_of: str) -> dict:
    """`metas` (bucket → its JSON API resource) as the day's document; `as_of`: when they were read (RFC 3339 UTC)."""
    return {
        "version": SNAPSHOT_VERSION,
        "date": as_of[:10],
        "as_of": as_of,
        "buckets": {name: {"retention_seconds": retention_seconds(meta)} for name, meta in sorted(metas.items())},
    }


def render(snap: Mapping) -> str:
    """One line per bucket: `<bucket>  off`, `<bucket>  7d` (`3600s` for a retention that isn't whole days) or `<bucket>  no
    expiry` (kept until purged)."""
    def held(seconds: int | None) -> str:
        return "no expiry" if seconds is None else "off" if seconds == 0 else f"{seconds // 86400}d" if seconds % 86400 == 0 else f"{seconds}s"

    width = max(len(name) for name in snap["buckets"])
    return "".join(f"{name:<{width}}  {held(b['retention_seconds'])}\n" for name, b in snap["buckets"].items())


@command("soft-delete")
@option("-b", "--bucket", "buckets", multiple=True, help="A bucket to read (repeatable): `NAME` on GCS, `s3://NAME` on an S3-style store")
@option("-E", "--endpoint", default=None, help="The `s3://` buckets' endpoint (default $SWEEP_S3_ENDPOINT)")
@option("-j", "--json", "as_json", is_flag=True, help="The document as JSON")
@option("-o", "--out", "outs", multiple=True, help="Write the day's document, `<OUT>/<date>.json` (a dir or URL; repeatable), and print its locations instead")
def soft_delete_cmd(buckets: Sequence[str], endpoint: str | None, as_json: bool, outs: Sequence[str]) -> None:
    """Each `-b` bucket's soft-delete retention now, read from GCS (`storage.buckets.get`) or an S3-style store (versioning
    + lifecycle): what a sweep's undo has to work with. Read-only, except `-o`'s daily document (the job's `soft-delete` step; the site's "undo unavailable")."""
    from .deploy_cost import _rfc3339, clock, fetch_buckets, write_snapshot

    if not buckets:
        raise UsageError("no bucket: pass -b")
    s3 = sorted({b.removeprefix("s3://") for b in buckets if b.startswith("s3://")})
    gcs = sorted({b for b in buckets if not b.startswith("s3://")})
    metas = dict(fetch_buckets(gcs)) if gcs else {}
    if s3:
        from .sweep import s3_client

        client = s3_client(endpoint)
        metas.update({name: read_s3_bucket(client, name) for name in s3})
    snap = snapshot(metas, _rfc3339(clock()))
    if outs:
        for written in write_snapshot(snap, outs):
            print(written)
        return
    print(json.dumps(snap, indent=2) if as_json else render(snap), end="\n" if as_json else "")
