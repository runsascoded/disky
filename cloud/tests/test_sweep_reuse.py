"""Reviewed manifest reuse is fail-closed and copies exact parquet bytes."""

import json
from pathlib import Path

import fsspec

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.sweep_reuse import reuse_manifest


@pytest.fixture
def reviewed(tmp_path: Path) -> tuple[Path, Path, dict]:
    source = tmp_path / "dry"
    (source / "manifest").mkdir(parents=True)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"plan_id": 1, "sweep": ["gs://b1/a/", "gs://b2/empty/"]}))
    pq.write_table(pa.table({"name": ["a/x", "a/y"]}), source / "manifest/b1.parquet")
    summary = {
        "date": "2026-10-03", "plan_id": 1,
        "approved": ["gs://b1/a/", "gs://b2/empty/"],
        "buckets": {"b1": {"eligible": {"objects": 2, "bytes": 10}}, "b2": {}},
        "total": {"eligible": {"objects": 2, "bytes": 10}},
    }
    (source / "plan-summary.json").write_text(json.dumps(summary))
    (source / "would-delete-summary.json").write_text(json.dumps({
        "for_real": False, "buckets": {"b1": {"decisions": {"delete": 2}, "delete_bytes": 10}},
    }))
    return source, plan, summary


def test_reuse_exact_bytes_and_provenance(reviewed, tmp_path: Path) -> None:
    source, plan, original = reviewed
    out = tmp_path / "real"
    result = reuse_manifest(str(source), str(plan), "2026-10-03", str(out))
    expected = {**original, "reused_from": str(source)}
    assert result == expected
    assert json.loads((out / "plan-summary.json").read_text()) == expected
    assert sorted(p.name for p in (out / "manifest").iterdir()) == ["b1.parquet"]
    assert (out / "manifest/b1.parquet").read_bytes() == (source / "manifest/b1.parquet").read_bytes()
    assert json.loads((source / "plan-summary.json").read_text()) == original


@pytest.mark.parametrize("mutation,error", [
    ({"date": "2026-10-02"}, "reviewed manifest scan/plan does not match the new dispatch"),
    ({"plan_id": 2}, "reviewed manifest scan/plan does not match the new dispatch"),
    ({"approved": ["gs://b1/other/"]}, "reviewed manifest prefix/bucket set does not match the new dispatch"),
    ({"buckets": {"b1": {"eligible": {"objects": 3}}}}, "reviewed manifest prefix/bucket set does not match the new dispatch"),
])
def test_reuse_refuses_mismatched_inputs(reviewed, tmp_path: Path, mutation: dict, error: str) -> None:
    source, plan, original = reviewed
    (source / "plan-summary.json").write_text(json.dumps({**original, **mutation}))
    out = tmp_path / "real"
    with pytest.raises(SystemExit) as caught:
        reuse_manifest(str(source), str(plan), "2026-10-03", str(out))
    assert str(caught.value) == error
    assert out.exists() is False


@pytest.mark.parametrize("dry,error", [
    ({"for_real": True, "buckets": {"b1": {}}}, "manifest reuse requires a completed dry-run of the same buckets"),
    ({"for_real": False, "buckets": {"b1": {"interrupted": {"roots_skipped": 1}}}}, "cannot reuse an interrupted or failed dry-run"),
])
def test_reuse_requires_complete_dr(reviewed, tmp_path: Path, dry: dict, error: str) -> None:
    source, plan, _ = reviewed
    (source / "would-delete-summary.json").write_text(json.dumps(dry))
    out = tmp_path / "real"
    with pytest.raises(SystemExit) as caught:
        reuse_manifest(str(source), str(plan), "2026-10-03", str(out))
    assert str(caught.value) == error
    assert out.exists() is False


def test_reuse_refuses_incomplete_parquet_and_existing_output(reviewed, tmp_path: Path) -> None:
    source, plan, _ = reviewed
    pq.write_table(pa.table({"name": ["a/x"]}), source / "manifest/b1.parquet")
    out = tmp_path / "real"
    with pytest.raises(SystemExit) as caught:
        reuse_manifest(str(source), str(plan), "2026-10-03", str(out))
    assert str(caught.value) == "b1: reviewed manifest has 1 rows, expected 2"
    assert out.exists() is False
    out.mkdir()
    summary = out / "plan-summary.json"
    summary.write_text("untouched\n")
    with pytest.raises(SystemExit) as caught:
        reuse_manifest(str(source), str(plan), "2026-10-03", str(out))
    assert str(caught.value) == "manifest reuse destination already contains a manifest"
    assert summary.read_text() == "untouched\n"


def test_reuse_pins_gcs_artifact_for_both_validation_and_copy(reviewed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, plan, original = reviewed
    out = tmp_path / "real"
    local = fsspec.filesystem("file")
    calls = []

    class VersionedStore:
        protocol = "gcs"

        def __getattr__(self, name: str):
            return getattr(local, name)

        def open(self, path: str, mode: str = "r"):
            if path.endswith(".parquet#123"):
                calls.append(("validate", path))
                path = path.removesuffix("#123")
            return local.open(path, mode)

        def info(self, path: str) -> dict:
            return {**local.info(path), "generation": "123"}

        def copy(self, src: str, dst: str) -> None:
            calls.append(("copy", src, dst))
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            local.copy(src.removesuffix("#123"), dst)

    store = VersionedStore()
    resolve = fsspec.core.url_to_fs

    def url_to_fs(url: str, **kwargs) -> tuple:
        if url in (str(source), str(out)):
            assert kwargs == {"version_aware": True}
            return store, url
        return resolve(url, **kwargs)

    monkeypatch.setattr(fsspec.core, "url_to_fs", url_to_fs)
    assert reuse_manifest(str(source), str(plan), "2026-10-03", str(out)) == {**original, "reused_from": str(source)}
    pinned = f"{source}/manifest/b1.parquet#123"
    assert calls == [("validate", pinned), ("copy", pinned, f"{out}/manifest/b1.parquet")]
    assert (out / "manifest/b1.parquet").read_bytes() == (source / "manifest/b1.parquet").read_bytes()


def test_reuse_refuses_diagnostic_manifest(reviewed, tmp_path: Path) -> None:
    source, plan, original = reviewed
    (source / "plan-summary.json").write_text(json.dumps({**original, "diagnostic": True}) + "\n")
    with pytest.raises(SystemExit) as caught:
        reuse_manifest(str(source), str(plan), "2026-10-03", str(tmp_path / "real"))
    assert str(caught.value) == "manifest reuse requires a completed dry-run of the same buckets"
