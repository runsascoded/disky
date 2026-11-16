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


def _raising(exc):
    def list_blobs(bucket, **kw):
        raise exc
        yield
    return SimpleNamespace(list_blobs=list_blobs)


def test_gcs_soft_deleted_on_a_bucket_with_soft_delete_off_is_zeros():
    """GCS refuses a soft-deleted listing of a bucket whose retention is 0 (400); it holds none: measured, zero."""
    from google.api_core.exceptions import BadRequest
    client = _raising(BadRequest("GET https://storage.googleapis.com/storage/v1/b/bk/o: Soft delete policy is required to list soft-deleted versions"))
    assert nl.gcs_soft_deleted("bk", "", client).row() == {
        "bucket": "bk", "class": "soft-deleted", "prefix": "", "bytes": 0, "objects": 0, "oldest": None, "newest": None,
        "expires_min": None, "expires_max": None}


def test_gcs_soft_deleted_raises_any_other_bad_request():
    from google.api_core.exceptions import BadRequest
    with pytest.raises(BadRequest, match="^400 Invalid argument$"):
        nl.gcs_soft_deleted("bk", "", _raising(BadRequest("Invalid argument")))


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


def test_cli_measures_several_buckets_into_one_json_and_a_parquet_each(monkeypatch, tmp_path):
    """Several URIs: each bucket measured (`-P` at once), its own parquet through `-o …/{bucket}.parquet`, and the
    scan's JSON merged once with every bucket's rows; `-o` without `{bucket}` is refused for several."""
    sizes = {"a": 40, "b": 7, "c": 0}
    monkeypatch.setattr(nl, "gcs_soft_deleted", lambda bucket, prefix: nl.Total(bucket, "soft-deleted", bytes=sizes[bucket], objects=sizes[bucket] // 7))
    snap = tmp_path / "snap" / "non-live.json"
    res = CliRunner().invoke(nl.non_live_cmd, ["-c", "soft-deleted", "-P", "3", "-j", str(snap), "-o", str(tmp_path / "l2" / "{bucket}.parquet"),
                                               "gs://a", "gs://b", "gs://c"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert {b: pq.read_table(tmp_path / "l2" / f"{b}.parquet").column("bytes").to_pylist() for b in "abc"} == {"a": [40], "b": [7], "c": [0]}
    assert [(r["bucket"], r["bytes"], r["objects"]) for r in json.loads(snap.read_text())["rows"]] == [("a", 40, 5), ("b", 7, 1), ("c", 0, 0)]
    res = CliRunner().invoke(nl.non_live_cmd, ["-c", "soft-deleted", "-o", str(tmp_path / "x.parquet"), "gs://a", "gs://b"])
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (1, "-o: several URIs need `{bucket}` in the path")


# --- `-s metric`: Cloud Monitoring's storage metrics -----------------------------------------------------------------

def ts(bucket: str, typ: str, cls: str, points: list[tuple[str, float]]) -> dict:
    """One `timeSeries` as the API gives it: points (`endTime`, value) newest first."""
    return {"resource": {"labels": {"bucket_name": bucket}}, "metric": {"labels": {"type": typ, "storage_class": cls}},
            "points": [{"interval": {"endTime": e}, "value": {"doubleValue": v}} for e, v in points]}


#: 2026-11-15T08:00:00Z
NOW = 1794729600.0


def fake_metric(monkeypatch, latest: dict[str, list[dict]], daily: list[dict] | None = None):
    """`fetch_metric` answering from `latest` (by metric) and `daily`; the calls it got."""
    calls = []

    def fetch(metric, project, buckets, start, end, daily_=False, **kw):
        daily_ = kw.get("daily", daily_)
        calls.append((metric.rsplit("/", 1)[1], project, buckets, nl._rfc3339(start), nl._rfc3339(end), daily_))
        return [{"timeSeries": daily or []}] if daily_ else [{"timeSeries": latest.get(metric, [])}]
    monkeypatch.setattr(nl, "fetch_metric", fetch)
    monkeypatch.setattr(nl, "bucket_projects", lambda buckets: {b: "748" for b in buckets})
    monkeypatch.setattr(nl, "clock", lambda: NOW)
    return calls


def test_metric_totals_sum_storage_classes_newest_sample(monkeypatch, tmp_path):
    """Each bucket's classes now: every series' newest sample (bytes from `total_bytes`, objects from `total_count`),
    summed over storage classes; a class with no series is zeros. One query per metric for all the buckets of a
    project, over the last hour; no history without -S."""
    latest = {
        nl.METRIC_BYTES: [ts("a", "soft-deleted-object", "REGIONAL", [("t2", 30), ("t1", 99)]),
                          ts("a", "soft-deleted-object", "COLDLINE", [("t2", 12)]),
                          ts("a", "live-object", "REGIONAL", [("t2", 1000)]),
                          ts("b", "multipart-upload", "REGIONAL", [("t2", 7)])],
        nl.METRIC_COUNT: [ts("a", "soft-deleted-object", "REGIONAL", [("t2", 3)]),
                          ts("a", "soft-deleted-object", "COLDLINE", [("t2", 1)]),
                          ts("b", "multipart-upload", "REGIONAL", [("t2", 2)])],
    }
    calls = fake_metric(monkeypatch, latest)
    snap = tmp_path / "non-live.json"
    res = CliRunner().invoke(nl.non_live_cmd, ["-s", "metric", "-c", "soft-deleted,mpu", "-j", str(snap), "gs://a", "gs://b"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert calls == [("total_bytes", "748", ["a", "b"], "2026-11-15T07:00:00Z", "2026-11-15T08:00:00Z", False),
                     ("total_count", "748", ["a", "b"], "2026-11-15T07:00:00Z", "2026-11-15T08:00:00Z", False)]
    assert [(r["bucket"], r["class"], r["bytes"], r["objects"], r["expires_max"]) for r in json.loads(snap.read_text())["rows"]] == [
        ("a", "soft-deleted", 42, 4, None), ("a", "mpu", 0, 0, None),
        ("b", "soft-deleted", 0, 0, None), ("b", "mpu", 7, 2, None),
    ]


@pytest.mark.parametrize("args, msg", [
    (["-s", "metric", "-c", "delete-marker", "gs://a"], "-s metric: no delete-marker (it has soft-deleted, noncurrent, mpu)"),
    (["-s", "metric", "-c", "mpu", "gs://a/p"], "-s metric: whole GCS buckets only (no prefix): gs://a/p"),
    (["-s", "metric", "-c", "mpu", "s3://a"], "-s metric: whole GCS buckets only (no prefix): s3://a"),
    (["-c", "mpu", "-S", "x.json", "gs://a"], "-S: the history comes from the metric (-s metric)"),
])
def test_metric_refuses_what_it_cant_measure(monkeypatch, args, msg):
    fake_metric(monkeypatch, {})
    res = CliRunner().invoke(nl.non_live_cmd, args)
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (1, msg)


def test_metric_history_is_one_total_per_day_and_class(monkeypatch, tmp_path):
    """-S: daily means over `-H` days before today, summed over storage classes, labelled by the day each aligned
    point closes; a day with no `live` point is null in every class, a non-live class missing that day is 0, and a class
    0 every day is left out."""
    daily = [
        ts("a", "live-object", "REGIONAL", [("2026-11-15T00:00:00Z", 100), ("2026-11-14T00:00:00Z", 90)]),
        ts("a", "live-object", "COLDLINE", [("2026-11-15T00:00:00Z", 10)]),
        ts("a", "soft-deleted-object", "REGIONAL", [("2026-11-15T00:00:00Z", 5.6)]),
        ts("a", "noncurrent-object", "REGIONAL", [("2026-11-15T00:00:00Z", 0)]),
    ]
    calls = fake_metric(monkeypatch, {}, daily)
    out = tmp_path / "series.json"
    res = CliRunner().invoke(nl.non_live_cmd, ["-s", "metric", "-c", "soft-deleted", "-H", "3", "-S", str(out), "gs://a"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert calls[-1] == ("total_bytes", "748", ["a"], "2026-11-12T00:00:00Z", "2026-11-15T00:00:00Z", True)
    assert json.loads(out.read_text()) == {
        "as_of": "2026-11-15T08:00:00Z", "metric": nl.METRIC_BYTES, "dates": ["2026-11-12", "2026-11-13", "2026-11-14"],
        "buckets": {"a": {"live": [None, 90, 110], "soft-deleted": [None, 0, 6]}},
    }
