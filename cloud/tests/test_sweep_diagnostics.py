"""Bounded diagnostics exercise real read/merge/log code, never deletions."""

import json
import pstats
import random
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from dt_cloud.sweep_exec import execute_plan
from dt_cloud.sweep_manifest_benchmark import benchmark_manifest_io
from dt_cloud.sweep_profile import RangeClient, profile_executor
from test_sweep_exec import FakeClient, _client, _decisions, _plan_dir
from test_sweep_manifest import B2, DATE, PLAN, _write


def test_manifest_io_modes_produce_exact_rows(tmp_path: Path) -> None:
    root = tmp_path / "input"
    directory = root / "listing" / DATE / B2
    directory.mkdir(parents=True)
    _write(directory / "a.parquet", ["x/a", "x/b", "y/c"], random.Random(0))
    _write(directory / "b.parquet", ["x/d", "y/e"], random.Random(1))
    _write(directory / "outside.parquet", [f"z/{i}" for i in range(100)], random.Random(2))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(PLAN) + "\n")
    out = tmp_path / "output"
    result = benchmark_manifest_io(str(root), DATE, str(plan), B2, str(out), str(tmp_path / "work"), shards=2, workers=2)
    assert result["contract"] == "diagnostic-sample-not-a-deletion-plan"
    assert [Path(shard["path"]).name for shard in result["shards"]] == ["a.parquet", "b.parquet"]
    assert [{k: trial[k] for k in ("mode", "objects", "eligible_objects")} for trial in result["trials"]] == [
        {"mode": "ranges", "objects": 5, "eligible_objects": 3},
        {"mode": "prebuffer", "objects": 5, "eligible_objects": 3},
        {"mode": "local", "objects": 5, "eligible_objects": 3},
    ]
    expected = pq.read_table(out / "ranges.parquet").to_pylist()
    assert sorted(row["name"] for row in expected) == ["x/a", "x/b", "x/d"]
    assert pq.read_table(out / "prebuffer.parquet").to_pylist() == expected
    assert pq.read_table(out / "local.parquet").to_pylist() == expected
    assert json.loads((out / "benchmark.json").read_text()) == result
    with pytest.raises(ValueError, match="^benchmark artifact directory must be empty$"):
        benchmark_manifest_io(str(root), DATE, str(plan), B2, str(out), str(tmp_path / "other"))


class BoundedClient(FakeClient):
    def list_blobs(self, bucket: str, prefix: str = "", start_offset: str = "", end_offset: str = "", max_results: int = 0) -> list:
        self.listed.append((bucket, prefix, start_offset, end_offset, max_results))
        return sorted((b for b in self.blobs.get(bucket, []) if b.name.startswith(prefix) and start_offset <= b.name < end_offset), key=lambda b: b.name)[:max_results]


def test_range_client_intersects_unicode_prefix_and_bounds_every_request() -> None:
    client = BoundedClient(blobs={})
    bounded = RangeClient(client, [("a/a", "a/z"), ("é/a", "é/z")], 10)
    assert list(bounded.list_blobs("b1", "a/")) == []
    assert list(bounded.list_blobs("b1", "é/")) == []
    assert client.listed == [("b1", "a/", "a/a", "a/z", 10), ("b1", "é/", "é/a", "é/z", 10)]


def test_executor_profile_writes_remote_style_artifacts_and_no_deletes(tmp_path: Path) -> None:
    source = _plan_dir(tmp_path)
    original = _client()
    client = BoundedClient(blobs=original.blobs)
    work = tmp_path / "work"
    out = tmp_path / "diagnostic"
    result = profile_executor(str(source), "b1", str(work), groups=1, rows=3, workers=2, client=client, artifact_out=str(out))
    assert {k: result[k] for k in ("contract", "sample_objects", "ranges", "workers")} == {
        "contract": "diagnostic-sample-not-a-reviewed-dry-run", "sample_objects": 3,
        "ranges": [("a/x", "a/{")], "workers": 2,
    }
    assert client.listed == [("b1", "a/", "a/x", "a/{", 6)]
    assert client.handle.deletes == []
    assert _decisions(out, "would-delete") == [("a/x", "delete", 111), ("a/y", "skipped_gone", 0), ("a/z", "skipped_overwritten", 333)]
    summary = json.loads((out / "plan-summary.json").read_text())
    assert {k: summary[k] for k in ("diagnostic", "buckets", "total")} == {
        "diagnostic": True, "buckets": {"b1": {"eligible": {"bytes": 60, "objects": 3}}},
        "total": {"eligible": {"bytes": 60, "objects": 3}},
    }
    profiles = sorted((work / "profiles").glob("*.pstats"))
    assert len(profiles) == 1
    assert pstats.Stats(str(profiles[0])).total_calls > 0
    assert (out / profiles[0].name).read_bytes() == profiles[0].read_bytes()
    assert (out / "profile.json").read_bytes() == (work / "profile.json").read_bytes()
    with pytest.raises(ValueError, match="^diagnostic manifests cannot be used for real deletion$"):
        execute_plan(str(out), for_real=True, client=client)
    assert client.handle.deletes == []


def test_executor_profiler_refuses_real_even_before_opening_input() -> None:
    with pytest.raises(ValueError, match="^executor profiling is dry-only$"):
        execute_plan("nonexistent", for_real=True, profile_dir="nonexistent")
