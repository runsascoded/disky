from __future__ import annotations

import hashlib

import pytest
from botocore.exceptions import ClientError

from dt_cloud.sweep_canary import cleanup, conditional_delete_canary

BUCKET = "b"


class _Pages:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages

    def paginate(self, **_kw) -> list[dict]:
        return self.pages


class _Store:
    """A versioned S3 store; `enforce_single` / `enforce_batch` decide whether a stale ETag is refused."""

    def __init__(self, enforce_single: bool, enforce_batch: bool) -> None:
        self.enforce_single, self.enforce_batch = enforce_single, enforce_batch
        self.versions: list[tuple[str, str, str | None]] = []  # (key, version id, etag or None for a delete marker)

    def _live(self, key: str) -> str | None:
        mine = [v for v in self.versions if v[0] == key]
        return mine[-1][2] if mine else None

    def _mark(self, key: str) -> None:
        self.versions.append((key, f"v{len(self.versions)}", None))

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> dict:  # noqa: N803
        etag = f'"{hashlib.md5(Body).hexdigest()}"'
        self.versions.append((Key, f"v{len(self.versions)}", etag))
        return {"ETag": etag}

    def create_multipart_upload(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        self.uploads = {**getattr(self, "uploads", {}), "u": []}
        return {"UploadId": "u"}

    def upload_part(self, Bucket: str, Key: str, UploadId: str, PartNumber: int, Body: bytes) -> dict:  # noqa: N803
        self.uploads[UploadId].append(hashlib.md5(Body).digest())
        return {"ETag": f'"{hashlib.md5(Body).hexdigest()}"'}

    def complete_multipart_upload(self, Bucket: str, Key: str, UploadId: str, MultipartUpload: dict) -> dict:  # noqa: N803
        parts = self.uploads.pop(UploadId)
        etag = f'"{hashlib.md5(b"".join(parts)).hexdigest()}-{len(parts)}"'
        self.versions.append((Key, f"v{len(self.versions)}", etag))
        return {"ETag": etag}

    def abort_multipart_upload(self, Bucket: str, Key: str, UploadId: str) -> dict:  # noqa: N803
        self.uploads.pop(UploadId, None)
        return {}

    def head_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        etag = self._live(Key)
        if etag is None:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ETag": etag}

    def delete_object(self, Bucket: str, Key: str, IfMatch: str) -> dict:  # noqa: N803
        if self.enforce_single and IfMatch != self._live(Key):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "DeleteObject")
        self._mark(Key)
        return {}

    def delete_objects(self, Bucket: str, Delete: dict) -> dict:  # noqa: N803
        deleted, errors = [], []
        for o in Delete["Objects"]:
            if "VersionId" in o:
                self.versions = [v for v in self.versions if (v[0], v[1]) != (o["Key"], o["VersionId"])]
                deleted.append({"Key": o["Key"]})
            elif self.enforce_batch and o.get("ETag") != self._live(o["Key"]):
                errors.append({"Key": o["Key"], "Code": "PreconditionFailed"})
            else:
                self._mark(o["Key"])
                deleted.append({"Key": o["Key"]})
        return {"Deleted": deleted, "Errors": errors}

    def get_paginator(self, name: str) -> _Pages:
        if name == "list_object_versions":
            return _Pages([{
                "Versions": [{"Key": k, "VersionId": v} for k, v, e in self.versions if e is not None],
                "DeleteMarkers": [{"Key": k, "VersionId": v} for k, v, e in self.versions if e is None],
            }])
        return _Pages([{"Contents": [{"Key": k} for k in dict.fromkeys(k for k, _, _ in self.versions) if self._live(k)]}])


def _summary(store: _Store) -> list[dict]:
    s = conditional_delete_canary(store, BUCKET)
    assert (store.versions, s["left"]) == ([], 0)
    return [{k: p[k] for k in ("kind", "stale", "survived_stale", "current", "removed_by_current", "enforced")} for p in s["probes"]]


@pytest.mark.parametrize("single,batch", [(True, True), (True, False), (False, True), (False, False)])
def test_canary_reports_what_the_store_enforces_and_leaves_nothing(single: bool, batch: bool) -> None:
    expect = []
    for kind, on in (("single", single), ("batch", batch), ("multipart", batch), ("mixed", batch)):
        expect.append({
            "kind": kind, "stale": "PreconditionFailed" if on else "ok", "survived_stale": on,
            "current": "ok", "removed_by_current": True, "enforced": on,
        })
    assert _summary(_Store(single, batch)) == expect


def test_cleanup_refuses_a_prefix_not_the_runs_own() -> None:
    with pytest.raises(ValueError):
        cleanup(_Store(True, True), BUCKET, "disky-canary/")
