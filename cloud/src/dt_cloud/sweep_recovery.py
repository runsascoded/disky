"""Bounded read-only samples of soft-deleted generations under a prefix."""

from typing import Any


def recovery_sample(prefix: str, limit: int = 1, client: Any = None) -> dict:
    if not prefix.startswith("gs://") or not prefix.endswith("/"):
        raise ValueError("prefix must be gs://bucket/path/ with a trailing slash")
    bucket, _, path = prefix.removeprefix("gs://").partition("/")
    if not bucket or not path or limit < 1 or limit > 100:
        raise ValueError("a non-root prefix and a limit from 1 to 100 are required")
    if client is None:
        from google.cloud.storage import Client
        client = Client()
    blobs = client.list_blobs(bucket, prefix=path, soft_deleted=True, max_results=limit)
    samples = [{
        "name": blob.name, "generation": int(blob.generation), "size_bytes": blob.size,
        "soft_deleted": blob.soft_delete_time.isoformat() if blob.soft_delete_time else None,
        "hard_delete": blob.hard_delete_time.isoformat() if blob.hard_delete_time else None,
    } for blob in blobs]
    return {"prefix": prefix, "limit": limit, "samples": samples, "complete_inventory": False}
