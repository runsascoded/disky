"""Does the sweep's S3 store enforce a delete's ETag precondition? (specs/delete-preconditions.md, "verify actual
`If-Match` enforcement with an owned canary").

Only objects the canary writes itself, under a fresh `<prefix>/<run>/` it never reuses, are read, overwritten or deleted.
Each probe writes a key twice (ETags A, then B), then deletes with the stale A:

- `single`: `DeleteObject(IfMatch=A)`. Enforced: refused (412) and the object is still B.
- `batch`: `DeleteObjects` naming `{Key, ETag: A}`. Enforced: the item comes back in `Errors` and the object is still B.

Each then deletes with the current B, which must succeed (else the store refuses conditional deletes outright).
Finally every version and delete marker under the run's prefix is removed (`VersionId` deletes), so nothing of the
canary's stays, on a versioned bucket or not.
"""
from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


def _code(e: Exception) -> str:
    return str(getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__))


def _etag_now(client: "S3Client", bucket: str, key: str) -> str | None:
    """The key's current ETag, None when it isn't live (404 or a delete marker)."""
    from botocore.exceptions import ClientError

    try:
        return client.head_object(Bucket=bucket, Key=key)["ETag"]
    except ClientError as e:
        if _code(e) in ("404", "NoSuchKey", "NotFound", "405", "MethodNotAllowed"):
            return None
        raise


def _two_versions(client: "S3Client", bucket: str, key: str) -> tuple[str, str]:
    a = client.put_object(Bucket=bucket, Key=key, Body=b"canary A\n")["ETag"]
    b = client.put_object(Bucket=bucket, Key=key, Body=b"canary B, a different body\n")["ETag"]
    if a == b:
        raise RuntimeError(f"{key}: two different bodies got one ETag {a}")
    return a, b


def _single(client: "S3Client", bucket: str, key: str, etag: str) -> str:
    """`DeleteObject(IfMatch=etag)`: `ok` or the error code."""
    from botocore.exceptions import ClientError

    try:
        client.delete_object(Bucket=bucket, Key=key, IfMatch=etag)
    except ClientError as e:
        return _code(e)
    return "ok"


def _batch(client: "S3Client", bucket: str, key: str, etag: str) -> str:
    """`DeleteObjects` of one `{Key, ETag}` item: `ok` (in `Deleted`), the item's error code, or the request's."""
    from botocore.exceptions import ClientError

    try:
        resp = client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key, "ETag": etag}], "Quiet": False})
    except ClientError as e:
        return f"request:{_code(e)}"
    errors = [x for x in resp.get("Errors", []) if x.get("Key") == key]
    if errors:
        return str(errors[0].get("Code", "error"))
    return "ok" if any(x.get("Key") == key for x in resp.get("Deleted", [])) else "unanswered"


def probe(client: "S3Client", bucket: str, key: str, kind: str) -> dict[str, Any]:
    """One probe (`single` | `batch`) on its own key: the stale delete's answer and whether the object survived it,
    then the current delete's answer and whether that removed it."""
    delete = _single if kind == "single" else _batch
    a, b = _two_versions(client, bucket, key)
    stale = delete(client, bucket, key, a)
    after_stale = _etag_now(client, bucket, key)
    if after_stale is None:  # not enforced: the stale delete removed it; put it back for the current-ETag delete
        b = client.put_object(Bucket=bucket, Key=key, Body=b"canary B, a different body\n")["ETag"]
    current = delete(client, bucket, key, b)
    after_current = _etag_now(client, bucket, key)
    return {
        "kind": kind,
        "stale": stale,
        "survived_stale": after_stale == b,
        "current": current,
        "removed_by_current": after_current is None,
        "enforced": stale != "ok" and after_stale == b and current == "ok" and after_current is None,
    }


def cleanup(client: "S3Client", bucket: str, prefix: str) -> int:
    """Remove every version and delete marker under `prefix` (the run's own). Returns how many."""
    if not prefix.endswith("/") or prefix.count("/") < 2:
        raise ValueError(f"refusing to clean {prefix!r}: want the run's own `<prefix>/<run>/`")
    ids: list[dict[str, str]] = []
    try:
        for page in client.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix):
            ids += [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
    except Exception:  # a store without versioning support: plain keys
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            ids += [{"Key": o["Key"]} for o in page.get("Contents", [])]
    if ids:
        client.delete_objects(Bucket=bucket, Delete={"Objects": ids, "Quiet": True})
    return len(ids)


def conditional_delete_canary(client: "S3Client", bucket: str, prefix: str = "disky-canary/conditional-delete") -> dict[str, Any]:
    """Both probes under a fresh run prefix, then its cleanup. The summary says, per probe, whether the store enforced
    a stale ETag."""
    run = f"{prefix.strip('/')}/{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}/"
    out: dict[str, Any] = {"bucket": bucket, "prefix": run, "probes": []}
    try:
        for kind in ("single", "batch"):
            out["probes"].append(probe(client, bucket, f"{run}{kind}", kind))
    finally:
        out["cleaned"] = cleanup(client, bucket, run)
        out["left"] = sum(len(p.get("Contents", [])) for p in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=run))
    return out
