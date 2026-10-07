"""The executor overlaps buckets, HTTP requests, and bounded journal uploads."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.sweep_journal import DecisionJournal
from dt_cloud.sweep_reviewed import execute_reviewed
from test_sweep_exec import FakeClient, _decisions


def reviewed_source(tmp_path, buckets: tuple[str, ...], count: int = 1005):
    source = tmp_path / "source"
    (source / "would-delete").mkdir(parents=True)
    approved = [f"gs://{bucket}/a/" for bucket in buckets]
    original = {"plan_id": 1, "date": "scratch", "approved": approved, "buckets": {bucket: {"eligible": {"objects": count, "bytes": count}} for bucket in buckets}, "total": {"eligible": {"objects": count * len(buckets), "bytes": count * len(buckets)}}}
    dry = {"for_real": False, "buckets": {bucket: {"decisions": {"delete": count}, "delete_bytes": count} for bucket in buckets}}
    rows = [dict(name=f"a/o{i:06d}", size_bytes=1, generation=i + 1, decision="delete", dir="a") for i in range(count)]
    for bucket in buckets:
        pq.write_table(pa.Table.from_pylist(rows), source / f"would-delete/{bucket}.parquet")
    (source / "plan-summary.json").write_text(json.dumps(original))
    (source / "would-delete-summary.json").write_text(json.dumps(dry))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"plan_id": 1, "sweep": approved}))
    return source, plan, rows


def test_progress_hook_reports_exact_initial_and_final_samples(tmp_path):
    source, plan, _ = reviewed_source(tmp_path, ("b1",), count=5)
    samples = []
    execute_reviewed(str(source), str(plan), str(tmp_path / "run"), str(tmp_path / "work"), client=FakeClient(blobs={}), on_progress=samples.append)
    assert [(s["bucket"], s["decisions"], s["delete_bytes"], s["done"]) for s in samples] == [
        ("b1", {}, 0, False),
        ("b1", {"delete": 5}, 5, True),
    ]
    assert [s["bands"] for s in samples] == [{}, {"gs://b1/a/": {"bytes": 5, "objects": 5}}]


def test_buckets_overlap_with_shared_delete_worker_pool(tmp_path):
    source, plan, rows = reviewed_source(tmp_path, ("b1", "b2"))
    barrier = threading.Barrier(2)
    seen = []

    def delete(client, bucket, batch):
        if len(batch) == 1000:
            barrier.wait(timeout=5)
        seen.append(bucket.name)
        return [(blob, "delete") for blob in batch]

    out = tmp_path / "run"
    client = FakeClient(blobs={})
    client.bucket = lambda name: SimpleNamespace(name=name, test_iam_permissions=client.handle.test_iam_permissions)
    result = execute_reviewed(str(source), str(plan), str(out), str(tmp_path / "work"), for_real=True, client=client, deleter=delete, workers=2, bucket_workers=2)
    assert sorted(seen) == ["b1", "b1", "b2", "b2"]
    assert {bucket: entry["decisions"] for bucket, entry in result["buckets"].items()} == {"b1": {"delete": 1005}, "b2": {"delete": 1005}}
    for bucket in ("b1", "b2"):
        table = pq.read_table(out / "deleted" / bucket).to_pylist()
        assert sorted(table, key=lambda row: row["name"]) == rows


@pytest.mark.parametrize("restricted_dr", [False, True])
def test_bucket_cut_accepts_matching_full_or_cut_dr_without_widening(tmp_path, restricted_dr):
    source, plan, _ = reviewed_source(tmp_path, ("b1", "b2"), count=5)
    if restricted_dr:
        summary = json.loads((source / "plan-summary.json").read_text())
        summary["approved"] = ["gs://b2/a/"]
        summary["buckets"] = {"b2": summary["buckets"]["b2"]}
        (source / "plan-summary.json").write_text(json.dumps(summary))
        dry = json.loads((source / "would-delete-summary.json").read_text())
        dry["buckets"] = {"b2": dry["buckets"]["b2"]}
        (source / "would-delete-summary.json").write_text(json.dumps(dry))
    seen = []

    def delete(client, bucket, batch):
        seen.append([(blob.name, blob.generation) for blob in batch])
        return [(blob, "delete") for blob in batch]

    result = execute_reviewed(str(source), str(plan), str(tmp_path / "run"), str(tmp_path / "work"), for_real=True, only_buckets=("b2",), client=FakeClient(blobs={}), deleter=delete)
    assert seen == [[(f"a/o{i:06d}", i + 1) for i in range(5)]]
    assert sorted(result["buckets"]) == ["b2"]
    assert result["_plan"]["approved"] == ["gs://b2/a/"]


def test_unknown_bucket_cut_fails_before_any_delete(tmp_path):
    source, plan, _ = reviewed_source(tmp_path, ("b1",), count=5)
    seen = []
    with pytest.raises(ValueError, match="^requested bucket is outside the staged plan$"):
        execute_reviewed(str(source), str(plan), str(tmp_path / "run"), str(tmp_path / "work"), for_real=True, only_buckets=("outside",), client=FakeClient(blobs={}), deleter=lambda *args: seen.append(args))
    assert seen == []


def test_default_starts_all_six_buckets_with_bounded_shared_workers(tmp_path, monkeypatch):
    buckets = tuple(f"b{i}" for i in range(6))
    source, plan, _ = reviewed_source(tmp_path, buckets, count=5)
    barrier = threading.Barrier(6)
    seen = []
    active = peak = 0
    lock = threading.Lock()
    monkeypatch.setattr("dt_cloud.sweep_pacing.BucketPacer.wait", lambda *args, **kwargs: True)

    def delete(client, bucket, batch):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            seen.append(bucket.name)
        barrier.wait(timeout=5)
        with lock:
            active -= 1
        return [(blob, "delete") for blob in batch]

    client = FakeClient(blobs={})
    client.bucket = lambda name: SimpleNamespace(name=name, test_iam_permissions=client.handle.test_iam_permissions)
    result = execute_reviewed(str(source), str(plan), str(tmp_path / "run"), str(tmp_path / "work"), for_real=True, client=client, deleter=delete, workers=6)
    assert [sorted(seen), peak, active] == [list(buckets), 6, 0]
    assert {b: s["decisions"] for b, s in result["buckets"].items()} == {b: {"delete": 5} for b in buckets}
    assert result["bucket_workers"] == 6


def test_real_xml_path_wires_retry_feedback_and_commits_exact_undo_rows(tmp_path, monkeypatch):
    from test_sweep_xml import Http, reply

    source, plan, rows = reviewed_source(tmp_path, ("b1",), count=5)
    xml = '<DeleteResult>' + ''.join(f'<Deleted><Key>{row["name"]}</Key><VersionId>{row["generation"]}</VersionId></Deleted>' for row in rows) + '</DeleteResult>'
    http = Http([reply(429, "limited"), reply(200, xml)])
    admitted = []
    monkeypatch.setattr("dt_cloud.sweep_xml.XmlDeleter.session", lambda self: http)
    monkeypatch.setattr("dt_cloud.sweep_xml._sleep", lambda _: None)
    monkeypatch.setattr("dt_cloud.sweep_pacing.BucketPacer.wait", lambda self, count, lane="http", *args: admitted.append((lane, count)) or True)
    client = FakeClient(blobs={})
    client._credentials = None
    client.bucket = lambda name: SimpleNamespace(name=name, test_iam_permissions=client.handle.test_iam_permissions)
    out = tmp_path / "run"
    result = execute_reviewed(str(source), str(plan), str(out), str(tmp_path / "work"), for_real=True, client=client, pacing="adaptive")
    assert admitted == [("submit", 5), ("http", 5), ("http", 5)]
    pacing = result["buckets"]["b1"]["performance"]["pacing"]
    assert {k: pacing[k] for k in ("mode", "attempts", "attempted_objects", "pressures", "decreases", "rate")} == {"mode": "adaptive", "attempts": 2, "attempted_objects": 10, "pressures": 1, "decreases": 1, "rate": 500}
    assert result["buckets"]["b1"]["decisions"] == {"delete": 5}
    assert pq.read_table(out / "deleted" / "b1").to_pylist() == rows


def test_blocked_upload_does_not_stop_feeding_deletes(tmp_path, monkeypatch):
    source, plan, rows = reviewed_source(tmp_path, ("b1",), count=20_000)
    blocked = threading.Event()
    released = threading.Event()
    continued = threading.Event()
    calls = []
    upload = DecisionJournal._upload

    def blocked_upload(self, local, count):
        blocked.set()
        assert released.wait(timeout=10) is True
        upload(self, local, count)

    def delete(client, bucket, batch):
        calls.append(len(batch))
        if blocked.is_set():
            continued.set()
        return [(blob, "delete") for blob in batch]

    monkeypatch.setattr(DecisionJournal, "_upload", blocked_upload)
    monkeypatch.setattr("dt_cloud.sweep_reviewed.LOG_ROWS", 1000, raising=False)
    monkeypatch.setattr("dt_cloud.sweep_pacing.BucketPacer.wait", lambda *args, **kwargs: True)
    out = tmp_path / "run"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_reviewed, str(source), str(plan), str(out), str(tmp_path / "work"), for_real=True, client=FakeClient(blobs={}), deleter=delete, workers=2)
        try:
            assert blocked.wait(timeout=5) is True
            assert continued.wait(timeout=5) is True
        finally:
            released.set()
        result = future.result(timeout=10)
    assert calls == [1000] * 20
    assert result["buckets"]["b1"]["decisions"] == {"delete": 20_000}
    assert result["buckets"]["b1"]["performance"]["journal"]["parts_uploaded"] == 20
    assert _decisions(out, "deleted") == sorted((row["name"], "delete", row["generation"]) for row in rows)
