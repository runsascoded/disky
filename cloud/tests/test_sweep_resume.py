"""A handoff excludes only drained, reconciled identities from the same DR."""

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.sweep_reviewed import execute_reviewed
from test_sweep_exec import FakeClient, _decisions
from test_sweep_reviewed import source


def prior_run(tmp_path, directory, rows):
    prior = tmp_path / "prior"
    (prior / "deleted/b1").mkdir(parents=True)
    settled = [dict(row) for row in rows[:500]]
    settled[498]["decision"] = "skipped_gone"
    settled[499]["decision"] = "skipped_overwritten"
    settled.append({**rows[500], "decision": "delete_failed"})
    pq.write_table(pa.Table.from_pylist(settled), prior / "deleted/b1/part-00000.parquet")
    summary = {"plan": str(prior), "for_real": True, "reviewed_source": str(directory), "buckets": {"b1": {"decisions": {"delete": 498, "skipped_gone": 1, "skipped_overwritten": 1, "delete_failed": 1}, "delete_bytes": 498}}}
    (prior / "deleted-summary.json").write_text(json.dumps(summary))
    return prior, settled, summary


def test_resume_subtracts_successes_and_skips_but_retries_failed_rows(source, tmp_path):
    directory, plan, rows, _, _ = source
    prior, _, _ = prior_run(tmp_path, directory, rows)
    out = tmp_path / "run"
    calls = []

    def delete(client, bucket, batch):
        calls.extend((blob.name, blob.generation) for blob in batch)
        return [(blob, "delete") for blob in batch]

    result = execute_reviewed(str(directory), str(plan), str(out), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=delete, exclude_runs=(str(prior),))
    assert sorted(calls) == sorted((row["name"], row["generation"]) for row in rows[500:1005])
    assert result["buckets"]["b1"]["decisions"] == {"delete": 505}
    assert result["resume_runs"] == [str(prior)]
    assert result["_plan"]["total"] == {"eligible": {"objects": 505, "bytes": 505}}
    assert _decisions(out, "deleted") == sorted((row["name"], "delete", row["generation"]) for row in rows[500:1005])
    assert json.loads((out / "resume-source.json").read_text())["buckets"]["b1"] == {"parts": [str(prior / "deleted/b1/part-00000.parquet")], "excluded_objects": 500, "excluded_bytes": 500}


@pytest.mark.parametrize("mutation,message", [
    ({"generation": 999999}, "b1: exclusion identity is outside the reviewed delete set"),
    ({"size_bytes": 2}, "b1: drained decision log does not match its final summary"),
    ({"dir": "different"}, "b1: exclusion identity is outside the reviewed delete set"),
])
def test_tampered_exclusion_blocks_every_delete(source, tmp_path, mutation, message):
    directory, plan, rows, _, _ = source
    prior, settled, _ = prior_run(tmp_path, directory, rows)
    settled[0].update(mutation)
    pq.write_table(pa.Table.from_pylist(settled), prior / "deleted/b1/part-00000.parquet")
    calls = []
    with pytest.raises(ValueError) as error:
        execute_reviewed(str(directory), str(plan), str(tmp_path / "out"), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=lambda *args: calls.append(args), exclude_runs=(str(prior),))
    assert str(error.value) == message
    assert calls == []
    assert (tmp_path / "out").exists() is False


def test_missing_earlier_resume_chain_is_refused(source, tmp_path):
    directory, plan, rows, _, _ = source
    prior, _, summary = prior_run(tmp_path, directory, rows)
    summary["resume_runs"] = ["gs://data/earlier-run"]
    (prior / "deleted-summary.json").write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="^resume requires every earlier run in the exclusion chain$"):
        execute_reviewed(str(directory), str(plan), str(tmp_path / "out"), str(tmp_path / "work"), client=FakeClient(blobs={}), exclude_runs=(str(prior),))
