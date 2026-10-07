"""Direct DR execution preserves scope, generations and durable decisions."""

import json
import threading
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.sweep_reviewed import execute_reviewed, prepare_reviewed
from test_sweep_exec import FakeClient, _decisions


@pytest.fixture
def source(tmp_path):
    directory = tmp_path / "source"
    (directory / "would-delete").mkdir(parents=True)
    rows = [dict(name=f"a/d{i // 6}/o{i}", size_bytes=1, generation=i + 1, decision="delete", dir=f"a/d{i // 6}") for i in range(1005)]
    rows.append(dict(name="a/gone", size_bytes=1, generation=0, decision="skipped_gone", dir="a"))
    pq.write_table(pa.Table.from_pylist(rows), directory / "would-delete/b1.parquet")
    original = {"plan_id": 1, "date": "2026-10-03", "approved": ["gs://b1/a/"], "buckets": {"b1": {"eligible": {"objects": 1006, "bytes": 1006}}}, "total": {"eligible": {"objects": 1006, "bytes": 1006}}}
    dry = {"for_real": False, "buckets": {"b1": {"decisions": {"delete": 1005, "skipped_gone": 1}, "delete_bytes": 1005}}}
    (directory / "plan-summary.json").write_text(json.dumps(original))
    (directory / "would-delete-summary.json").write_text(json.dumps(dry))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"plan_id": 1, "sweep": ["gs://b1/a/"]}))
    return directory, plan, rows, original, dry


def test_bulk_execution_combines_small_dirs_and_never_relists(source, tmp_path):
    directory, plan, rows, _, _ = source
    calls = []
    client = FakeClient(blobs={})

    def delete(client, bucket, batch):
        calls.append([(blob.name, blob.generation) for blob in batch])
        return [(blob, "delete") for blob in batch]

    out = tmp_path / "run"
    result = execute_reviewed(str(directory), str(plan), str(out), str(tmp_path / "work"), for_real=True, workers=2, client=client, deleter=delete)
    assert sorted(calls, key=len, reverse=True) == [[(row["name"], row["generation"]) for row in rows[:1000]], [(row["name"], row["generation"]) for row in rows[1000:1005]]]
    assert client.listed == []
    assert result["buckets"]["b1"]["decisions"] == {"delete": 1005}
    assert result["buckets"]["b1"]["bands"] == {"gs://b1/a/": {"bytes": 1005, "objects": 1005}}
    assert result["buckets"]["b1"]["delete_bytes"] == 1005
    assert json.loads((out / "plan-summary.json").read_text())["total"] == {"eligible": {"objects": 1005, "bytes": 1005}}
    assert _decisions(out, "deleted") == sorted((row["name"], "delete", row["generation"]) for row in rows[:1005])
    progress = json.loads((out / "progress/b1.json").read_text())
    assert {key: progress[key] for key in ("unit", "roots", "roots_done", "decisions", "done")} == {"unit": "batches", "roots": 2, "roots_done": 2, "decisions": {"delete": 1005}, "done": True}


def test_dry_only_and_stop_never_delete(source, tmp_path):
    directory, plan, rows, _, _ = source
    client = FakeClient(blobs={})

    def delete(*args):
        raise AssertionError("delete must not be called")

    out = tmp_path / "dry"
    result = execute_reviewed(str(directory), str(plan), str(out), str(tmp_path / "work"), client=client, deleter=delete)
    assert result["for_real"] is False
    assert result["diagnostic"] is True
    assert json.loads((out / "plan-summary.json").read_text())["diagnostic"] is True
    assert _decisions(out, "would-delete") == sorted((row["name"], "delete", row["generation"]) for row in rows[:1005])
    stop = threading.Event()
    stop.set()
    result = execute_reviewed(str(directory), str(plan), str(tmp_path / "stop"), str(tmp_path / "stop-work"), for_real=True, client=client, deleter=delete, stop=stop)
    assert result["buckets"]["b1"]["decisions"] == {}
    assert result["buckets"]["b1"]["interrupted"] == {"roots_skipped": 2, "roots": 2}


@pytest.mark.parametrize("mutation,message", [
    ({"name": "other/x"}, "b1: reviewed delete row is outside staged prefixes"),
    ({"generation": 0}, "b1: reviewed delete row lacks a positive generation"),
    ({"generation": None}, "b1: reviewed delete row lacks a positive generation"),
    ({"size_bytes": -1}, "b1: reviewed delete row has invalid size"),
])
def test_entire_log_preflight_precedes_first_delete(source, tmp_path, mutation, message):
    directory, plan, rows, _, _ = source
    # A bad row at the very end must block deletion of the valid earlier rows.
    rows[1004].update(mutation)
    pq.write_table(pa.Table.from_pylist(rows), directory / "would-delete/b1.parquet")
    calls = []
    with pytest.raises(ValueError) as error:
        execute_reviewed(str(directory), str(plan), str(tmp_path / "out"), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=lambda *args: calls.append(args))
    assert str(error.value) == message
    assert calls == []
    assert (tmp_path / "out").exists() is False


def test_refuses_changed_plan_incomplete_dr_and_totals(source, tmp_path):
    directory, plan, _, original, dry = source
    plan.write_text(json.dumps({"plan_id": 1, "sweep": ["gs://b1/expanded/"]}))
    with pytest.raises(ValueError, match="^reviewed DR does not match the current staged plan$"):
        prepare_reviewed(str(directory), str(plan), str(tmp_path / "mismatch"))
    plan.write_text(json.dumps({"plan_id": 1, "sweep": ["gs://b1/a/"]}))
    (directory / "would-delete-summary.json").write_text(json.dumps({**dry, "for_real": True}))
    with pytest.raises(ValueError, match="^reviewed execution requires a completed, non-diagnostic DR$"):
        prepare_reviewed(str(directory), str(plan), str(tmp_path / "real"))
    dry["buckets"]["b1"]["delete_bytes"] = 0
    (directory / "would-delete-summary.json").write_text(json.dumps(dry))
    with pytest.raises(ValueError, match="^b1: reviewed log totals do not match the completed DR$"):
        prepare_reviewed(str(directory), str(plan), str(tmp_path / "totals"))


def test_partial_failure_preserves_successful_undo_rows(source, tmp_path):
    directory, plan, rows, _, _ = source
    client = FakeClient(blobs={})
    submitted = threading.Barrier(2)

    def delete(client, bucket, batch):
        # Both batches must be in flight before the first failure is observed.
        submitted.wait(timeout=5)
        return [(blob, "delete_failed" if blob.name == rows[0]["name"] else "delete") for blob in batch]

    out = tmp_path / "run"
    result = execute_reviewed(str(directory), str(plan), str(out), str(tmp_path / "work"), for_real=True, workers=2, client=client, deleter=delete)
    assert result["buckets"]["b1"]["decisions"] == {"delete_failed": 1, "delete": 1004}
    assert result["buckets"]["b1"]["failed_dirs"] == [{"dir": "a/d0", "objects": 1}]
    assert result["buckets"]["b1"]["error"] == "reviewed batch contains failed deletions; stopping new submissions"
    assert _decisions(out, "deleted") == sorted((row["name"], "delete_failed" if i == 0 else "delete", row["generation"]) for i, row in enumerate(rows[:1005]))


def test_stop_during_delete_drains_submitted_batch_and_leaves_rest(source, tmp_path):
    directory, plan, rows, _, _ = source
    stop = threading.Event()
    calls = []

    def delete(client, bucket, batch):
        calls.append(len(batch))
        stop.set()
        return [(blob, "delete") for blob in batch]

    out = tmp_path / "run"
    result = execute_reviewed(str(directory), str(plan), str(out), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=delete, stop=stop)
    assert calls == [1000]
    assert result["buckets"]["b1"]["decisions"] == {"delete": 1000}
    assert result["buckets"]["b1"]["interrupted"] == {"roots_skipped": 1, "roots": 2}
    assert _decisions(out, "deleted") == sorted((row["name"], "delete", row["generation"]) for row in rows[:1000])
