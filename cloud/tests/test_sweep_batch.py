from __future__ import annotations

import csv
import io
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.sweep_batch import build_batch_manifests, submit_batch_jobs


def _dry_run(tmp_path):
    plan = tmp_path / "20261004-p12"
    log = plan / "would-delete" / "data-us-central2"
    log.mkdir(parents=True)
    table = pa.table({
        "name": ["a/x", "a,comma", "a/gone"],
        "size_bytes": [10, 20, 30],
        "generation": [111, 222, 0],
        "decision": ["delete", "delete", "skipped_gone"],
    })
    pq.write_table(table, log / "part-00000.parquet")
    (plan / "would-delete-summary.json").write_text(json.dumps({
        "for_real": False,
        "buckets": {
            "data-us-central2": {
                "decisions": {"delete": 2, "skipped_gone": 1},
                "delete_bytes": 30,
            },
        },
    }))
    return plan


def test_build_batch_manifest_is_generation_pinned_and_quoted(tmp_path):
    plan = _dry_run(tmp_path)
    summary = build_batch_manifests(str(plan))
    path = plan / "batch-manifest" / "data-us-central2" / "part-00000.csv"
    rows = list(csv.reader(io.StringIO(path.read_text())))
    assert rows == [
        ["bucket", "name", "generation"],
        ["data-us-central2", "a/x", "111"],
        ["data-us-central2", "a,comma", "222"],
    ]
    bucket = summary["buckets"]["data-us-central2"]
    assert {k: bucket[k] for k in ("objects", "bytes", "source_parts", "manifest_location")} == {
        "objects": 2,
        "bytes": 30,
        "source_parts": 1,
        "manifest_location": f"{plan}/batch-manifest/data-us-central2/part-*.csv",
    }
    assert bucket["parts"] == [{
        "path": "part-00000.csv",
        "objects": 2,
        "bytes": 30,
        "sha256": "de295e24651f69db0426bb24253ce2680d9e1d20e0731cdca9db265357463492",
    }]


def test_build_refuses_to_replace_reviewed_manifest(tmp_path):
    plan = _dry_run(tmp_path)
    build_batch_manifests(str(plan))
    with pytest.raises(SystemExit) as exc:
        build_batch_manifests(str(plan))
    assert str(exc.value) == f"{plan}/batch-manifest already exists; refusing to replace the reviewed generation set"


def test_build_refuses_a_delete_without_generation(tmp_path):
    plan = _dry_run(tmp_path)
    part = plan / "would-delete" / "data-us-central2" / "part-00000.parquet"
    table = pq.read_table(part).set_column(2, "generation", pa.array([111, 0, 0], type=pa.int64()))
    pq.write_table(table, part)
    with pytest.raises(SystemExit) as exc:
        build_batch_manifests(str(plan))
    assert str(exc.value) == "data-us-central2: a delete decision has no live generation; refusing an unsafe Batch manifest"


def test_build_skips_buckets_with_no_approved_deletes(tmp_path):
    plan = _dry_run(tmp_path)
    summary_path = plan / "would-delete-summary.json"
    summary = json.loads(summary_path.read_text())
    summary["buckets"]["data-us-east5"] = {"decisions": {}, "delete_bytes": 0}
    summary_path.write_text(json.dumps(summary))
    result = build_batch_manifests(str(plan))
    assert list(result["buckets"]) == ["data-us-central2"]
    assert sorted(str(path.relative_to(plan)) for path in plan.rglob("*")) == [
        "batch-manifest",
        "batch-manifest/data-us-central2",
        "batch-manifest/data-us-central2/part-00000.csv",
        "batch-manifest/summary.json",
        "would-delete",
        "would-delete-summary.json",
        "would-delete/data-us-central2",
        "would-delete/data-us-central2/part-00000.parquet",
    ]


class Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"name": "operations/op-1"}


class Http:
    calls = None

    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response()


def test_submit_is_dry_by_default_and_uses_one_bucket_manifest(tmp_path):
    plan = _dry_run(tmp_path)
    build_batch_manifests(str(plan))
    summary_path = plan / "batch-manifest" / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["buckets"]["data-us-central2"]["manifest_location"] = (
        "gs://my-data/sweep/run/batch-manifest/data-us-central2/part-*.csv"
    )
    summary_path.write_text(json.dumps(summary))
    http = Http()
    result = submit_batch_jobs(str(plan), project="my-project", http=http)
    assert result == {
        "source": str(plan),
        "project": "my-project",
        "dry_run": True,
        "jobs": [{
            "bucket": "data-us-central2",
            "job_id": "gcs-sweep-batch-dry-20261004-p12-data-us-central2",
            "operation": {"name": "operations/op-1"},
        }],
    }
    assert http.calls == [(
        "https://storagebatchoperations.googleapis.com/v1/projects/my-project/locations/global/jobs",
        {
            "params": {"jobId": "gcs-sweep-batch-dry-20261004-p12-data-us-central2"},
            "json": {
                "description": f"disk-tree reviewed sweep: {plan}",
                "bucketList": {"buckets": [{
                    "bucket": "data-us-central2",
                    "manifest": {"manifestLocation": "gs://my-data/sweep/run/batch-manifest/data-us-central2/part-*.csv"},
                }]},
                "deleteObject": {"permanentObjectDeletionEnabled": False},
                "dryRun": True,
                "loggingConfig": {"logActions": ["TRANSFORM"], "logActionStates": ["FAILED"]},
            },
        },
    )]
