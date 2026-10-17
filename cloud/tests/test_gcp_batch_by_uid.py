"""`gcp.batch_job_by_uid`: a filtered Batch job list can answer an empty page and a `nextPageToken` (the filter runs
per page); the lookup follows the pages (one task in five missed its job on 2026-10-16 when it read only the first)."""
from __future__ import annotations

from dt_cloud import gcp

URL = "https://batch.googleapis.com/v1/projects/p/locations/r/jobs"


def pages(*answers: dict):
    calls: list[tuple[str, dict]] = []

    def get(url: str, **params: str) -> dict:
        calls.append((url, params))
        return answers[len(calls) - 1]

    return get, calls


def test_follows_empty_pages_to_the_job(monkeypatch):
    get, calls = pages({"nextPageToken": "t1"}, {"nextPageToken": "t2"}, {"jobs": [{"name": "j", "uid": "u1"}]})
    monkeypatch.setattr(gcp, "_get", get)
    assert gcp.batch_job_by_uid("u1", "p", "r") == {"name": "j", "uid": "u1"}
    assert calls == [
        (URL, {"filter": 'uid="u1"'}),
        (URL, {"filter": 'uid="u1"', "pageToken": "t1"}),
        (URL, {"filter": 'uid="u1"', "pageToken": "t2"}),
    ]


def test_none_when_the_pages_end(monkeypatch):
    get, calls = pages({"nextPageToken": "t1"}, {})
    monkeypatch.setattr(gcp, "_get", get)
    assert gcp.batch_job_by_uid("u1", "p", "r") is None
    assert len(calls) == 2
