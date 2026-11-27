"""`S3LeaseFile` (an R2 / S3 lease record by ETag preconditions) over a fake S3 client, and `CoordinatorLease` on it."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
from itertools import count
from pathlib import Path

import pytest

from dt_cloud.append_runner import Busy, CoordinatorLease, LocalLeaseFile, S3LeaseFile, lease_file


class ClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """One bucket's objects with S3's conditional `PutObject` / `DeleteObject` semantics; ETags count up."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.etags = count(1)

    def put_object(self, Bucket, Key, Body, ContentType, IfNoneMatch=None, IfMatch=None):
        cur = self.objects.get(Key)
        if IfNoneMatch == "*" and cur is not None:
            raise ClientError("PreconditionFailed")
        if IfMatch is not None and (cur is None or cur[1] != IfMatch):
            raise ClientError("PreconditionFailed" if cur else "NoSuchKey")
        etag = f'"{next(self.etags)}"'
        self.objects[Key] = (Body, etag)
        return {"ETag": etag}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError("NoSuchKey")
        body, etag = self.objects[Key]
        return {"Body": BytesIO(body), "ETag": etag}

    def delete_object(self, Bucket, Key, IfMatch=None):
        cur = self.objects.get(Key)
        if IfMatch is not None and (cur is None or cur[1] != IfMatch):
            raise ClientError("PreconditionFailed")
        self.objects.pop(Key, None)
        return {}


def test_create_read_replace_delete():
    s3 = FakeS3()
    f = S3LeaseFile("b", "d/2026-11.lease.json", s3)
    assert f.read() is None
    assert f.create('{"owner": "a"}') is True
    assert f.create('{"owner": "b"}') is False
    rec, tok = f.read()
    assert (rec, tok) == ({"owner": "a"}, '"1"')
    new = f.replace('{"owner": "a", "n": 2}', tok)
    assert new == '"2"'
    assert f.replace('{"owner": "c"}', tok) is None
    assert f.read() == ({"owner": "a", "n": 2}, '"2"')
    assert f.delete(tok) is False
    assert f.delete(new) is True
    assert f.read() is None
    assert f.replace("{}", new) is None


def test_coordinator_lease_on_s3():
    s3 = FakeS3()
    t0 = datetime(2026, 11, 27, tzinfo=timezone.utc)
    now = [t0]
    a = CoordinatorLease(S3LeaseFile("b", "k", s3), holder="job-a", now=lambda: now[0], ttl_s=60)
    a.take()
    b = CoordinatorLease(S3LeaseFile("b", "k", s3), holder="job-b", now=lambda: now[0], ttl_s=60)
    with pytest.raises(Busy):
        b.take()
    now[0] = t0 + timedelta(seconds=30)
    a.renew()
    a.check()
    now[0] = t0 + timedelta(seconds=120)
    b.take()
    a.renew()
    assert a.lost == "it is now job-b's"
    b.release()
    assert s3.objects == {}


def test_coordinator_lease_holder_ended():
    s3 = FakeS3()
    now = datetime(2026, 11, 27, tzinfo=timezone.utc)
    CoordinatorLease(S3LeaseFile("b", "k", s3), holder="job-a", now=lambda: now, region="us-west1").take()
    logs: list[str] = []
    b = CoordinatorLease(S3LeaseFile("b", "k", s3), holder="job-b", now=lambda: now, log=logs.append,
                         ended=lambda job, region: f"{job} SUCCEEDED" if (job, region) == ("job-a", "us-west1") else None)
    b.take()
    assert logs == ["coordinator lease: its holder ended (job-a SUCCEEDED): taking it"]


def test_lease_file_by_scheme(tmp_path: Path, monkeypatch):
    assert isinstance(lease_file(str(tmp_path / "x.lease.json")), LocalLeaseFile)
    monkeypatch.setenv("DISKY_R2_ENDPOINT_URL", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    f = lease_file("r2://idx/digest/r2/C1/2026-11.lease.json")
    assert isinstance(f, S3LeaseFile)
    assert (f.bucket, f.key, f.client.meta.endpoint_url) == ("idx", "digest/r2/C1/2026-11.lease.json", "https://acct.r2.cloudflarestorage.com")
    with pytest.raises(ValueError):
        lease_file("hf://ns/x")
