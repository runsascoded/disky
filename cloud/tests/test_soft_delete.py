"""`dt-cloud soft-delete`: each bucket's soft-delete retention now, and the day's document the site reads."""
import json

import pytest
from click.testing import CliRunner

from dt_cloud import deploy_cost as dc
from dt_cloud import soft_delete as sd
from dt_cloud.cli import main

NOW = 1_793_862_723.0  # 2026-11-05T07:12:03Z
METAS = {
    "bkt-b": {"softDeletePolicy": {"retentionDurationSeconds": "604800", "effectiveTime": "2026-01-01T00:00:00Z"}},
    "bkt-a": {"softDeletePolicy": {"retentionDurationSeconds": "0"}},
    "bkt-c": {"lifecycle": {"rule": []}},
    "bkt-d": {"softDeletePolicy": {"retentionDurationSeconds": "3600"}},
}
DOC = {
    "version": 1,
    "date": "2026-11-05",
    "as_of": "2026-11-05T07:12:03Z",
    "buckets": {
        "bkt-a": {"retention_seconds": 0},
        "bkt-b": {"retention_seconds": 604800},
        "bkt-c": {"retention_seconds": 0},
        "bkt-d": {"retention_seconds": 3600},
    },
}


@pytest.fixture
def fake_gcs(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(dc, "clock", lambda: NOW)
    monkeypatch.setattr(dc, "fetch_buckets", lambda names: calls.append(list(names)) or {n: METAS[n] for n in names})
    return calls


def test_snapshot_says_each_buckets_retention_and_reads_no_policy_as_off():
    assert sd.snapshot(METAS, "2026-11-05T07:12:03Z") == DOC


def test_prints_each_bucket(fake_gcs):
    r = CliRunner().invoke(main, ["soft-delete", "-b", "bkt-b", "-b", "bkt-a", "-b", "bkt-d", "-b", "bkt-a"])
    assert (r.exit_code, r.output.split("\n"), fake_gcs) == (
        0,
        ["bkt-a  off", "bkt-b  7d", "bkt-d  3600s", ""],
        [["bkt-a", "bkt-b", "bkt-d"]],
    )


def test_json_is_the_document(fake_gcs):
    r = CliRunner().invoke(main, ["soft-delete", "-j", *(a for b in METAS for a in ("-b", b))])
    assert (r.exit_code, json.loads(r.output)) == (0, DOC)


def test_out_writes_the_days_document_under_each_root_and_prints_where(fake_gcs, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    r = CliRunner().invoke(main, ["soft-delete", "-o", str(a), "-o", str(b), *(x for n in METAS for x in ("-b", n))])
    assert (r.exit_code, r.output.split("\n")) == (0, [f"{a}/2026-11-05.json", f"{b}/2026-11-05.json", ""])
    assert [json.loads((d / "2026-11-05.json").read_text()) for d in (a, b)] == [DOC, DOC]


def test_refuses_no_bucket(fake_gcs):
    r = CliRunner().invoke(main, ["soft-delete"])
    assert (r.exit_code, r.output.strip().split("\n")[-1], fake_gcs) == (2, "Error: no bucket: pass -b", [])
