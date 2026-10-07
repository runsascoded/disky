"""Executor semantics against a stub GCS client (specs/sweep-executor.md §3)."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from dt_cloud.sweep_exec import execute_plan, execution_progress_message

T0 = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
T1 = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)


@dataclass
class FakeBlob:
    name: str
    size: int
    generation: int
    time_created: dt.datetime
    soft_delete_time: dt.datetime | None = None


@dataclass
class FakeBucketHandle:
    deletes: list = field(default_factory=list)
    # What the job identity holds on the bucket (GCS answers testIamPermissions
    # with the granted subset of what was asked).
    perms: set = field(default_factory=lambda: {"storage.buckets.get", "storage.objects.delete", "storage.objects.restore"})
    active_batch: object = None

    def delete_blob(self, name, if_generation_match=None):
        self.deletes.append((name, if_generation_match))
        if self.active_batch is not None:
            self.active_batch.items.append((name, if_generation_match))

    def test_iam_permissions(self, permissions):
        return [p for p in permissions if p in self.perms]


@dataclass
class FakeResponse:
    status_code: int


class FakeBatch:
    """The library's `Batch` as the executor sees it: deletes issued inside the
    block, then `_responses` — one per item — once it closes (or the whole
    request fails). What each attempt answers comes from the client's script."""

    def __init__(self, client, raise_exception):
        self.client = client
        self.raise_exception = raise_exception
        self.items: list = []
        self._responses: list = []

    def __enter__(self):
        self.client.handle.active_batch = self
        return self

    def __exit__(self, et, ev, tb):
        self.client.handle.active_batch = None
        if et is not None:
            return False
        answer = self.client.batch_script.pop(0) if self.client.batch_script else self.client.batch_default
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            answer = [answer] * len(self.items)
        self._responses = [FakeResponse(c) for c in answer]
        return False


@dataclass
class FakeClient:
    blobs: dict  # bucket -> [FakeBlob] (any order; listed sorted by name, recursively, like GCS)
    handle: FakeBucketHandle = field(default_factory=FakeBucketHandle)
    soft_days: int = 7
    listed: list = field(default_factory=list)  # prefixes asked for, in order
    # Scripted batch answers, one per attempt: a status per item, one status
    # for every item, or an exception for the whole request. Empty → default.
    batch_script: list = field(default_factory=list)
    batch_default: object = 204
    fail_prefixes: set = field(default_factory=set)  # listings that blow up (a root that raises)

    soft_deleted: dict = field(default_factory=dict)  # bucket -> [FakeBlob] with soft_delete_time

    def list_blobs(self, bucket, prefix="", soft_deleted=False):
        self.listed.append(prefix)
        if prefix in self.fail_prefixes:
            raise RuntimeError(f"listing {prefix!r} failed")
        src = self.soft_deleted if soft_deleted else self.blobs
        return sorted((b for b in src.get(bucket, []) if b.name.startswith(prefix)), key=lambda b: b.name)

    def bucket(self, name):
        return self.handle

    def get_bucket(self, name):
        # The library's own policy object: the 2026-09-11 real run died on a
        # guard reading an attribute a hand-rolled stand-in had invented.
        from google.cloud.storage.bucket import SoftDeletePolicy

        @dataclass
        class B:
            soft_delete_policy: SoftDeletePolicy
        return B(SoftDeletePolicy(bucket=None, retention_duration_seconds=self.soft_days * 86400))

    def batch(self, raise_exception=True):
        assert raise_exception is False, "the executor must read per-item responses, not let the library raise the last one"
        return FakeBatch(self, raise_exception)


def _plan_dir(tmp_path):
    d = tmp_path / "plan"
    (d / "manifest").mkdir(parents=True)
    (d / "plan-summary.json").write_text(json.dumps({
        "date": "2026-09-01", "plan_id": 12,
        "buckets": {"b1": {"eligible": {"bytes": 60, "objects": 4}}},
    }))
    mf = pd.DataFrame([
        {"name": "a/x", "size_bytes": 10, "storage_class_id": 1, "created": T0, "dir": "a", "owner": "k", "sweepers": "k"},
        {"name": "a/y", "size_bytes": 20, "storage_class_id": 1, "created": T0, "dir": "a", "owner": "k", "sweepers": "k"},
        {"name": "a/z", "size_bytes": 30, "storage_class_id": 1, "created": T0, "dir": "a", "owner": "k", "sweepers": "k"},
        {"name": "b/w", "size_bytes": 40, "storage_class_id": 1, "created": T0, "dir": "b", "owner": "k", "sweepers": "k"},
    ])
    mf.to_parquet(d / "manifest" / "b1.parquet")
    return d


def _client():
    return FakeClient(blobs={"b1": [
        # a/x live+matching; a/y gone; a/z overwritten (created moved)
        FakeBlob("a/x", 10, 111, T0),
        FakeBlob("a/z", 33, 333, T1),
        # b/w live+matching, plus a NEW key → drift
        FakeBlob("b/w", 40, 444, T0),
        FakeBlob("b/new", 5, 555, T1),
    ]})


def _decisions(plan, mode):
    # the log is a directory of part files (each chunk its own parquet)
    df = pq.read_table(f"{plan}/{mode}/b1").to_pandas()
    return sorted(map(tuple, df[["name", "decision", "generation"]].itertuples(index=False)))


def test_log_lands_as_part_files_with_progress(tmp_path):
    # Each flushed chunk is a complete parquet; the run's progress file ends
    # marked done with the same counts the summary carries.
    plan = _plan_dir(tmp_path)
    s = execute_plan(str(plan), client=_client(), workers=1)
    parts = sorted(p.name for p in (plan / "would-delete" / "b1").iterdir())
    assert parts == ["part-00000.parquet"]
    prog = json.loads((plan / "progress" / "b1.json").read_text())
    assert {k: prog[k] for k in ("bucket", "mode", "roots", "roots_done", "decisions", "delete_bytes", "done")} == {
        "bucket": "b1", "mode": "would-delete", "roots": 2, "roots_done": 2,
        "decisions": {"delete": 1, "skipped_gone": 1, "skipped_overwritten": 1}, "delete_bytes": 10, "done": True,
    }
    assert s["buckets"]["b1"]["decisions"] == prog["decisions"]


def test_execution_progress_message() -> None:
    snap = {
        "bucket": "b1", "mode": "deleted", "roots": 1_000, "roots_done": 20,
        "decisions": {"delete": 2_000, "skipped_gone": 3, "skipped_overwritten": 4, "delete_failed": 5},
        "delete_bytes": 2_000_000_000_000, "done": False,
    }
    assert execution_progress_message(snap) == (
        "execute progress: b1 (deleted) · roots=20/1,000 · delete=2,000 (2.00 TB)"
        " · gone=3 · overwritten=4 · unanswered=5 · done=false"
    )


def test_dry_run_decisions_and_drift_skip(tmp_path):
    plan = _plan_dir(tmp_path)
    client = _client()
    s = execute_plan(str(plan), client=client)
    assert client.handle.deletes == []  # dry-run touches nothing
    assert _decisions(plan, "would-delete") == [
        ("a/x", "delete", 111),
        ("a/y", "skipped_gone", 0),
        ("a/z", "skipped_overwritten", 333),
    ]
    b = s["buckets"]["b1"]
    assert b["decisions"] == {"delete": 1, "skipped_gone": 1, "skipped_overwritten": 1}
    assert b["delete_bytes"] == 10
    assert b["drift_dirs"] == [{"dir": "b", "new_objects": 1, "new_bytes": 5, "skipped_deletes": 1}]


def test_dry_run_uses_scan_generation_when_available(tmp_path):
    plan = _plan_dir(tmp_path)
    manifest = plan / "manifest" / "b1.parquet"
    table = pq.read_table(manifest).append_column("generation", pa.array([111, 222, 222, 444], type=pa.int64()))
    pq.write_table(table, manifest)
    client = _client()
    client.blobs["b1"][1].time_created = T0
    execute_plan(str(plan), client=client)
    assert _decisions(plan, "would-delete") == [
        ("a/x", "delete", 111),
        ("a/y", "skipped_gone", 0),
        ("a/z", "skipped_overwritten", 333),
    ]


def test_for_real_deletes_with_generation_match_and_drift_proceed(tmp_path):
    plan = _plan_dir(tmp_path)
    client = _client()
    s = execute_plan(str(plan), for_real=True, drift="proceed", client=client)
    assert sorted(client.handle.deletes) == [("a/x", 111), ("b/w", 444)]
    assert _decisions(plan, "deleted") == [
        ("a/x", "delete", 111),
        ("a/y", "skipped_gone", 0),
        ("a/z", "skipped_overwritten", 333),
        ("b/w", "delete", 444),
    ]
    assert s["buckets"]["b1"]["delete_bytes"] == 50
    assert s["buckets"]["b1"]["drift_dirs"] == [
        {"dir": "b", "new_objects": 1, "new_bytes": 5, "skipped_deletes": 0},
    ]


def test_for_real_refuses_without_soft_delete(tmp_path):
    import pytest
    plan = _plan_dir(tmp_path)
    client = _client()
    client.soft_days = 0
    with pytest.raises(SystemExit) as ei:
        execute_plan(str(plan), for_real=True, client=client)
    assert "soft delete retention 0d < required 7d" in str(ei.value)


def test_delete_batch_settles_each_item_and_retries_transients(monkeypatch):
    from google.api_core.exceptions import ServiceUnavailable
    import dt_cloud.sweep_exec as se
    sleeps = []
    monkeypatch.setattr(se, "_sleep", sleeps.append)
    client = FakeClient(blobs={})
    blobs = [FakeBlob(n, 1, g, T0) for n, g in (("a/1", 11), ("a/2", 22), ("a/3", 33), ("a/4", 44))]
    # attempt 1: the request itself fails (the 2026-09-11 503) — the server may
    # have applied any of them, so a/2's 404 on attempt 2 is our delete;
    # attempt 2: one item still 503; attempt 3: it lands.
    client.batch_script = [ServiceUnavailable("server(s) are not responding"), [204, 404, 412, 503], [204]]
    out = se.delete_batch(client, client.handle, blobs)
    assert [(b.name, d) for b, d in out] == [("a/1", "delete"), ("a/2", "delete"), ("a/3", "skipped_overwritten"), ("a/4", "delete")]
    assert client.handle.deletes == [("a/1", 11), ("a/2", 22), ("a/3", 33), ("a/4", 44)] * 2 + [("a/4", 44)]
    assert len(sleeps) == 2


def test_delete_batch_owns_a_404_after_an_unanswered_attempt(monkeypatch):
    # Run 4 on east5 (2026-09-11): the request failed after the server had
    # applied two deletes; the retry saw 404 — ours, not "already gone".
    from google.api_core.exceptions import ServiceUnavailable
    import dt_cloud.sweep_exec as se
    monkeypatch.setattr(se, "_sleep", lambda _s: None)
    client = FakeClient(blobs={})
    blobs = [FakeBlob("a/1", 1, 11, T0), FakeBlob("a/2", 1, 22, T0)]
    client.batch_script = [ServiceUnavailable("lost reply"), [404, 204]]
    out = se.delete_batch(client, client.handle, blobs)
    assert [(b.name, d) for b, d in out] == [("a/1", "delete"), ("a/2", "delete")]
    # but a 404 after the server answered "not applied" (per-item 503) is someone else's delete
    client.batch_script = [[503], [404]]
    out = se.delete_batch(client, client.handle, [FakeBlob("a/3", 1, 33, T0)])
    assert [(b.name, d) for b, d in out] == [("a/3", "skipped_gone")]


def test_delete_batch_gives_up_as_delete_failed(monkeypatch):
    import dt_cloud.sweep_exec as se
    sleeps = []
    monkeypatch.setattr(se, "_sleep", sleeps.append)
    client = FakeClient(blobs={})
    client.batch_default = 503
    out = se.delete_batch(client, client.handle, [FakeBlob("a/1", 1, 11, T0)])
    assert [(b.name, d) for b, d in out] == [("a/1", "delete_failed")]
    assert len(sleeps) == se.DELETE_ATTEMPTS


def test_delete_batch_raises_on_a_non_transient_item(monkeypatch):
    import pytest
    import dt_cloud.sweep_exec as se
    monkeypatch.setattr(se, "_sleep", lambda _s: None)
    client = FakeClient(blobs={})
    client.batch_script = [[403]]
    with pytest.raises(RuntimeError) as ei:
        se.delete_batch(client, client.handle, [FakeBlob("a/1", 1, 11, T0)])
    assert str(ei.value) == "delete a/1@11: unexpected HTTP 403"


def test_for_real_unanswered_deletes_are_reported_not_fatal(tmp_path, monkeypatch):
    import dt_cloud.sweep_exec as se
    monkeypatch.setattr(se, "_sleep", lambda _s: None)
    plan = _plan_dir(tmp_path)
    client = _client()
    client.batch_default = 503
    s = execute_plan(str(plan), for_real=True, client=client)
    b = s["buckets"]["b1"]
    assert b["decisions"] == {"delete_failed": 1, "skipped_gone": 1, "skipped_overwritten": 1}
    assert b["failed_dirs"] == [{"dir": "a", "objects": 1}]
    assert b["delete_bytes"] == 0
    assert _decisions(plan, "deleted") == [("a/x", "delete_failed", 111), ("a/y", "skipped_gone", 0), ("a/z", "skipped_overwritten", 333)]


def test_log_keeps_finished_roots_when_another_root_raises(tmp_path):
    import pytest
    plan = _plan_dir(tmp_path)
    client = _client()
    client.fail_prefixes = {"b/"}
    with pytest.raises(RuntimeError) as ei:
        execute_plan(str(plan), client=client, workers=1)
    assert str(ei.value) == "listing 'b/' failed"
    assert _decisions(plan, "would-delete") == [("a/x", "delete", 111), ("a/y", "skipped_gone", 0), ("a/z", "skipped_overwritten", 333)]


def test_stop_leaves_unstarted_roots_for_a_rerun(tmp_path):
    # `sweep stop` (or SIGTERM) mid-run: roots already listing finish and log;
    # the rest are skipped and reported, so a re-run picks them up.
    import threading
    plan = _plan_dir(tmp_path)
    client = _client()
    stop = threading.Event()
    listed = client.list_blobs
    def list_then_stop(bucket, prefix="", **kw):
        stop.set()  # asked to stop while the first root is listing
        return listed(bucket, prefix, **kw)
    client.list_blobs = list_then_stop
    s = execute_plan(str(plan), client=client, workers=1, stop=stop)
    assert s["buckets"]["b1"]["interrupted"] == {"roots_skipped": 1, "roots": 2}
    assert _decisions(plan, "would-delete") == [("a/x", "delete", 111), ("a/y", "skipped_gone", 0), ("a/z", "skipped_overwritten", 333)]


def test_dry_run_reads_the_soft_delete_window(tmp_path):
    # The rehearsal reads the bucket's soft-delete policy like a real run
    # would (same call, same parse) and records the window; a short one warns
    # here and refuses there.
    plan = _plan_dir(tmp_path)
    client = _client()
    assert execute_plan(str(plan), client=client)["buckets"]["b1"]["soft_delete_days"] == 7
    client.soft_days = 0
    assert execute_plan(str(plan), client=client)["buckets"]["b1"]["soft_delete_days"] == 0


def test_for_real_refuses_without_real_permissions(tmp_path):
    # The 2026-09-11 real run died on the soft-delete guard's bucket GET: the job
    # identity had objectViewer only. Every real-run permission is checked up
    # front, before any listing, so the refusal names the whole gap at once.
    import pytest
    plan = _plan_dir(tmp_path)
    client = _client()
    client.handle.perms = {"storage.objects.list", "storage.objects.get"}
    with pytest.raises(SystemExit) as ei:
        execute_plan(str(plan), for_real=True, client=client)
    assert str(ei.value) == (
        "b1: the job identity lacks storage.buckets.get, storage.objects.delete, storage.objects.restore"
        " — refusing --for-real (grant roles/storage.objectUser + roles/storage.legacyBucketReader on the bucket)"
    )
    assert client.listed == []
    assert client.handle.deletes == []


def test_dry_run_reports_missing_real_permissions(tmp_path):
    # A dry run is the rehearsal: it lists what a real run would be refused for.
    plan = _plan_dir(tmp_path)
    client = _client()
    client.handle.perms = {"storage.buckets.get", "storage.objects.list"}
    s = execute_plan(str(plan), client=client)
    assert s["buckets"]["b1"]["missing_perms"] == ["storage.objects.delete", "storage.objects.restore"]
    ok = execute_plan(str(plan), client=_client())
    assert ok["buckets"]["b1"]["missing_perms"] == []


def _sized_plan(tmp_path, dirs: dict[str, int], **summary):
    """A plan whose manifest holds `dirs[dn]` objects under each dir."""
    d = tmp_path / "plan"
    (d / "manifest").mkdir(parents=True)
    (d / "plan-summary.json").write_text(json.dumps({"date": "2026-09-01", "plan_id": 1, "buckets": {"b1": {"eligible": {"bytes": 1, "objects": sum(dirs.values())}}}, **summary}))
    rows = [{"name": f"{dn}/o{i}", "size_bytes": 1, "storage_class_id": 1, "created": T0, "dir": dn, "owner": "k", "sweepers": "k"} for dn, n in dirs.items() for i in range(n)]
    pd.DataFrame(rows).to_parquet(d / "manifest" / "b1.parquet")
    return d


def test_roots_run_largest_first(tmp_path):
    # Longest-processing-time first: the pool starts on the big root and the
    # small ones fill the tail (alphabetical order left central2's last hours
    # to a single huge listing).
    plan = _sized_plan(tmp_path, {"a": 1, "b": 3, "c": 2})
    client = FakeClient(blobs={"b1": []})
    execute_plan(str(plan), client=client, workers=1)
    assert client.listed == ["b/", "c/", "a/"]


def test_oversized_root_splits_into_its_children(tmp_path):
    # A root over `max_root_objects` lists per child instead (one thread each);
    # a root with an object directly in it stays whole — splitting would skip
    # that object.
    plan = _sized_plan(tmp_path, {"big/x": 3, "big/y": 3, "flat": 5, "flat/z": 4})
    client = FakeClient(blobs={"b1": []})
    execute_plan(str(plan), client=client, workers=1, max_root_objects=4)
    assert client.listed == ["flat/", "big/x/", "big/y/"]


def test_roots_are_band_children_and_prefix_free():
    from dt_cloud.sweep_exec import list_roots

    dirs = {"ckpt/r1/step-1", "ckpt/r1/step-2", "ckpt/r2", "scratch/k/a/b", "scratch/k", "raw/x/y"}
    # bands: ckpt/ (children r1, r2 become roots), scratch/k/ (eligible itself → one root), raw/x/y uncovered → its top segment
    roots = list_roots(dirs, ("gs://b1/ckpt/", "gs://b1/scratch/k/"), "b1")
    assert roots == ["ckpt/r1", "ckpt/r2", "raw", "scratch/k"]
    assert list_roots({""}, (), "b1") == [""]  # the bucket root itself: list everything
    assert list_roots({"a/b", "a"}, ("gs://b1/a/",), "b1") == ["a"]  # the band itself is eligible → swallows its children


def test_streamed_merge_buffers_nested_dirs_until_the_listing_passes_them(tmp_path):
    """Manifest dirs `a` and `a/c` under one root: `a`'s keys interleave with
    `a/c`'s in the listing; `a/c` gains a new key (drift → skipped) while `a`
    is clean and deletes; gone keys are found wherever the merge passes them."""
    d = tmp_path / "plan"
    (d / "manifest").mkdir(parents=True)
    (d / "plan-summary.json").write_text(json.dumps({
        "date": "2026-09-01", "plan_id": 1, "approved": ["gs://b1/a/"],
        "buckets": {"b1": {"eligible": {"bytes": 1, "objects": 1}}},
    }))
    row = lambda name, dn, size=1: {"name": name, "size_bytes": size, "storage_class_id": 1, "created": T0, "dir": dn, "owner": None, "sweepers": "k"}
    pd.DataFrame([row("a/b.txt", "a"), row("a/c/q", "a/c"), row("a/c/r", "a/c"), row("a/d.txt", "a", 7), row("a/zz", "a")]).to_parquet(d / "manifest" / "b1.parquet")
    client = FakeClient(blobs={"b1": [
        FakeBlob("a/b.txt", 1, 11, T0),
        FakeBlob("a/c/new", 9, 99, T1),  # drift in a/c
        FakeBlob("a/c/q", 1, 12, T0),
        FakeBlob("a/c/r", 1, 13, T0),
        FakeBlob("a/d.txt", 7, 14, T0),
        FakeBlob("a/e/other", 3, 15, T1),  # a dir not in the manifest: ignored
    ]})
    s = execute_plan(str(d), for_real=True, client=client)
    assert client.listed == ["a/"]  # one recursive listing for the band
    assert sorted(client.handle.deletes) == [("a/b.txt", 11), ("a/d.txt", 14)]
    assert _decisions(d, "deleted") == [
        ("a/b.txt", "delete", 11),
        ("a/d.txt", "delete", 14),
        ("a/zz", "skipped_gone", 0),
    ]
    b = s["buckets"]["b1"]
    assert b["decisions"] == {"delete": 2, "skipped_gone": 1}
    assert b["delete_bytes"] == 8
    assert b["drift_dirs"] == [{"dir": "a/c", "new_objects": 1, "new_bytes": 9, "skipped_deletes": 2}]
    assert b["bands"] == {"gs://b1/a/": {"bytes": 8, "objects": 2, "gone": 1, "drift_new_objects": 1}}


# ---- undo: restore what a real run deleted, from its logs ------------------

class _UndoHandle:
    """restore_blob stub: `live` names answer 412 (a live object exists),
    `expired` names 404 (no soft-deleted copy), `broken` names blow up."""

    def __init__(self, live=(), expired=(), broken=()):
        self.live, self.expired, self.broken = set(live), set(expired), set(broken)
        self.calls: list[tuple] = []

    def restore_blob(self, name, generation=None, if_generation_match=None):
        from google.api_core.exceptions import NotFound, PreconditionFailed
        self.calls.append((name, generation, if_generation_match))
        if name in self.live:
            raise PreconditionFailed("live")
        if name in self.expired:
            raise NotFound("gone")
        if name in self.broken:
            raise RuntimeError("boom")
        return FakeBlob(name=name, size=0, generation=generation + 1000, time_created=T1)


@dataclass
class _UndoClient:
    handle: _UndoHandle

    def bucket(self, name):
        return self.handle


def _real_run_dir(tmp_path, for_real=True):
    d = tmp_path / "run"
    (d / "deleted").mkdir(parents=True)
    (d / "plan-summary.json").write_text(json.dumps({"date": "2026-09-01", "plan_id": 12, "approved": ["gs://b1/a/"], "buckets": {}}))
    (d / "deleted-summary.json").write_text(json.dumps({"plan": str(d), "for_real": for_real, "buckets": {"b1": {}}}))
    pd.DataFrame([
        {"name": "a/x", "size_bytes": 10, "generation": 11, "decision": "delete", "dir": "a"},
        {"name": "a/y", "size_bytes": 20, "generation": 12, "decision": "delete", "dir": "a"},
        {"name": "a/z", "size_bytes": 30, "generation": 13, "decision": "delete", "dir": "a"},
        {"name": "b/w", "size_bytes": 40, "generation": 14, "decision": "delete", "dir": "b"},
        {"name": "b/v", "size_bytes": 50, "generation": 0, "decision": "skipped_gone", "dir": "b"},
    ]).to_parquet(d / "deleted" / "b1.parquet")
    return d


def _restored_rows(d):
    files = sorted((d / "restored").glob("b1-*.parquet"))
    assert len(files) == 1
    t = pq.read_table(files[0], columns=["name", "generation", "new_generation", "decision", "error"])
    return [tuple(r.values()) for r in t.to_pylist()]


def test_undo_restores_logged_generations_and_classifies_outcomes(tmp_path):
    """Every `delete` row is restored by its logged generation with
    if_generation_match=0; a live name, an expired copy and an error each get
    their own decision; skipped rows are never touched."""
    from dt_cloud.sweep_exec import undo_run
    d = _real_run_dir(tmp_path)
    h = _UndoHandle(live={"a/y"}, expired={"a/z"}, broken={"b/w"})
    s = undo_run(str(d), client=_UndoClient(h), workers=2, now=1_800_000_000)
    assert sorted(h.calls) == [("a/x", 11, 0), ("a/y", 12, 0), ("a/z", 13, 0), ("b/w", 14, 0)]
    assert _restored_rows(d) == [
        ("a/x", 11, 1011, "restored", None),
        ("a/y", 12, 0, "already_live", None),
        ("a/z", 13, 0, "unrestorable", None),
        ("b/w", 14, 0, "failed", "RuntimeError: boom"),
    ]
    assert s["buckets"]["b1"] == {
        "decisions": {"restored": 1, "already_live": 1, "unrestorable": 1, "failed": 1},
        "restored_bytes": 10,
        "bands": {"gs://b1/a/": {"restored": 1, "already_live": 1, "unrestorable": 1, "bytes": 10}, "gs://b1/b/": {"failed": 1}},
    }
    assert json.loads((d / f"undo-{s['stamp']}-summary.json").read_text())["buckets"] == s["buckets"]


def test_undo_prefix_filter_and_dry_run(tmp_path):
    """`prefixes` narrows to gs://bucket/dir/ (bare form accepted); a dry run
    logs `would_restore` and calls nothing."""
    from dt_cloud.sweep_exec import undo_run
    d = _real_run_dir(tmp_path)
    h = _UndoHandle()
    s = undo_run(str(d), client=_UndoClient(h), prefixes=("b1/b/",), dry_run=True, now=1_800_000_000)
    assert h.calls == []
    assert _restored_rows(d) == [("b/w", 14, 0, "would_restore", None)]
    assert s["buckets"]["b1"]["decisions"] == {"would_restore": 1}


def test_undo_refuses_dry_runs_and_closed_windows(tmp_path):
    import pytest
    from dt_cloud.sweep_exec import undo_run
    d = _real_run_dir(tmp_path, for_real=False)
    with pytest.raises(SystemExit, match="dry run"):
        undo_run(str(d), client=_UndoClient(_UndoHandle()), now=1_800_000_000)
    d = _real_run_dir(tmp_path / "real")
    with pytest.raises(SystemExit, match="undo window closed"):
        undo_run(str(d), client=_UndoClient(_UndoHandle()), deadline=1_700_000_000, now=1_800_000_000)
