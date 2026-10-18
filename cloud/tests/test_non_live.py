"""`dt-cloud non-live` (`dt_cloud.non_live`, specs/non-live-bytes.md phase 1): per bucket and class, the bytes the
listing of current objects misses, from fake provider clients (the calls each class makes, paged as the APIs page)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from dt_cloud import non_live as nl


def T(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


class Paged:
    """`client.get_paginator(op).paginate(**kw)`: the pages given per op (keyed by op, then by `Key` for `list_parts`)."""

    def __init__(self, pages: dict):
        self.pages, self.calls = pages, []

    def get_paginator(self, op):
        def paginate(**kw):
            self.calls.append((op, kw))
            p = self.pages[op]
            return p[kw["Key"]] if op == "list_parts" else p
        return SimpleNamespace(paginate=paginate)


def test_s3_versions_split_noncurrent_and_markers_across_pages():
    """Noncurrent versions' bytes, each since its successor's write (a key whose versions span two pages too); delete
    markers counted at 0 bytes; current versions not counted."""
    pages = [
        {"Versions": [{"Key": "a", "LastModified": T(10), "Size": 5, "IsLatest": True},
                      {"Key": "a", "LastModified": T(8), "Size": 7, "IsLatest": False},
                      {"Key": "b", "LastModified": T(9), "Size": 11, "IsLatest": False}],
         "DeleteMarkers": [{"Key": "b", "LastModified": T(12), "IsLatest": True}]},
        {"Versions": [{"Key": "b", "LastModified": T(3), "Size": 13, "IsLatest": False}]},
    ]
    nc, dm = nl.s3_versions(Paged({"list_object_versions": pages}), "bk", "p/")
    assert (nc.row(), dm.row()) == (
        {"bucket": "bk", "class": "noncurrent", "prefix": "", "bytes": 31, "objects": 3, "oldest": T(9), "newest": T(12),
         "expires_min": None, "expires_max": None},
        {"bucket": "bk", "class": "delete-marker", "prefix": "", "bytes": 0, "objects": 1, "oldest": T(12), "newest": T(12),
         "expires_min": None, "expires_max": None},
    )


def test_mpus_sum_each_uploads_parts():
    pages = {"list_multipart_uploads": [{"Uploads": [{"Key": "x", "UploadId": "u1", "Initiated": T(5)},
                                                     {"Key": "y", "UploadId": "u2", "Initiated": T(7)}]}],
             "list_parts": {"x": [{"Parts": [{"Size": 100}, {"Size": 50}]}, {"Parts": [{"Size": 25}]}], "y": [{}]}}
    assert nl.mpus(Paged(pages), "bk", "").row() == {
        "bucket": "bk", "class": "mpu", "prefix": "", "bytes": 175, "objects": 2, "oldest": T(5), "newest": T(7),
        "expires_min": None, "expires_max": None}


def test_gcs_soft_deleted_carries_hard_delete_as_expiry():
    blobs = [SimpleNamespace(size=10, soft_delete_time=T(15), hard_delete_time=T(22)),
             SimpleNamespace(size=30, soft_delete_time=T(18), hard_delete_time=T(25))]
    client = SimpleNamespace(list_blobs=lambda bucket, **kw: blobs if (bucket, kw["soft_deleted"]) == ("bk", True) else [])
    assert nl.gcs_soft_deleted("bk", "", client).row() == {
        "bucket": "bk", "class": "soft-deleted", "prefix": "", "bytes": 40, "objects": 2, "oldest": T(15), "newest": T(18),
        "expires_min": T(22), "expires_max": T(25)}


@pytest.mark.parametrize("uri,classes,msg", [
    ("r2://bk", ["soft-deleted"], "r2://bk: r2 has no soft-deleted (it has mpu)"),
    ("gs://bk/p", ["noncurrent", "delete-marker"], "gs://bk/p: gs has no noncurrent, delete-marker (it has soft-deleted, mpu)"),
    ("hf://open-athena", ["mpu"], "hf://open-athena: want gs://, s3:// or r2:// with a bucket"),
])
def test_a_class_the_provider_cant_have_is_refused(uri, classes, msg):
    with pytest.raises(SystemExit) as e:
        nl.measure(uri, classes)
    assert str(e.value) == msg


def test_cli_writes_one_row_per_class_with_the_prefix(monkeypatch, tmp_path):
    """`-o`: the totals as parquet, `prefix` the URI's (an empty class a row of zeros: measured, none)."""
    monkeypatch.setattr(nl, "gcs_soft_deleted", lambda bucket, prefix: nl.Total(bucket, "soft-deleted", bytes=40, objects=2, expires_max=T(25)))
    monkeypatch.setattr(nl, "s3_client", lambda scheme, bucket, endpoint: Paged({"list_multipart_uploads": [{}]}))
    out = tmp_path / "nl.parquet"
    res = CliRunner().invoke(nl.non_live_cmd, ["-c", "soft-deleted,mpu", "-o", str(out), "gs://bk/a/b"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert pq.read_table(out).to_pylist() == [
        {"bucket": "bk", "class": "soft-deleted", "prefix": "a/b/", "bytes": 40, "objects": 2, "oldest": None, "newest": None,
         "expires_min": None, "expires_max": T(25)},
        {"bucket": "bk", "class": "mpu", "prefix": "a/b/", "bytes": 0, "objects": 0, "oldest": None, "newest": None,
         "expires_min": None, "expires_max": None},
    ]


def test_json_merges_each_buckets_rows_replacing_its_own(tmp_path):
    """`-j`: the scan's `non-live.json` holds every bucket measured; a bucket measured again replaces only its rows."""
    path = tmp_path / "snap" / "non-live.json"
    nl.merge_json(path, [nl.Total("b2", "mpu", bytes=1, objects=1)])
    nl.merge_json(path, [nl.Total("a1", "soft-deleted", bytes=40, objects=2, oldest=T(15), newest=T(18, 3), expires_min=T(22), expires_max=T(25))])
    nl.merge_json(path, [nl.Total("b2", "mpu", bytes=7, objects=2)])
    row = lambda b, c, n, o, **t: {"bucket": b, "class": c, "prefix": "", "bytes": n, "objects": o,  # noqa: E731
                                   **{k: None for k in ("oldest", "newest", "expires_min", "expires_max")}, **t}
    assert json.loads(path.read_text()) == {"rows": [
        row("a1", "soft-deleted", 40, 2, oldest="2026-10-15T00:00:00Z", newest="2026-10-18T03:00:00Z",
            expires_min="2026-10-22T00:00:00Z", expires_max="2026-10-25T00:00:00Z"),
        row("b2", "mpu", 7, 2),
    ]}
