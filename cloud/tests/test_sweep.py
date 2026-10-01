"""Plan-driven deletion engine: plan model, versioning guard, manifest builder, executor."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.sweep import (
    Plan,
    SweepError,
    build_manifest,
    eligible,
    execute_plan,
    load_plan,
    normalize_prefix,
    prefix_free,
    purge_run,
    undo_run,
    versioning_enabled,
)

BUCKET = "marin-us-east-02a"


def test_normalize_prefix() -> None:
    assert normalize_prefix(f"s3://{BUCKET}/marin/ckpt/", BUCKET) == "marin/ckpt/"
    assert normalize_prefix(f"s3://{BUCKET}/marin/ckpt", BUCKET) == "marin/ckpt/"
    assert normalize_prefix("marin/ckpt", BUCKET) == "marin/ckpt/"
    assert normalize_prefix("/marin/ckpt/", BUCKET) == "marin/ckpt/"


def test_plan_validate_rejects_bad() -> None:
    with pytest.raises(SweepError):
        Plan(name="empty", bucket=BUCKET, sweep=[]).validate()
    with pytest.raises(SweepError):
        Plan(name="no-slash", bucket=BUCKET, sweep=["marin/ckpt"]).validate()  # normalize adds slash; raw lacks it
    with pytest.raises(SweepError):
        Plan(name="dotdot", bucket=BUCKET, sweep=["../etc/"]).validate()


def test_load_plan_normalizes(tmp_path: Path) -> None:
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({
        "plan_id": 7,
        "name": "old ckpts",
        "bucket": BUCKET,
        "sweep": [f"s3://{BUCKET}/marin/ckpt/", "marin/scratch"],
    }))
    plan = load_plan(p)
    assert plan == Plan(
        name="old ckpts",
        bucket=BUCKET,
        sweep=["marin/ckpt/", "marin/scratch/"],
        plan_id=7,
    )


class _FakeS3:
    def __init__(self, status: str | None) -> None:
        self._status = status

    def get_bucket_versioning(self, Bucket: str) -> dict:  # noqa: N803 (boto3 kwarg name)
        return {"Status": self._status} if self._status else {}


def test_versioning_guard() -> None:
    assert versioning_enabled(_FakeS3("Enabled"), BUCKET) is True
    assert versioning_enabled(_FakeS3("Suspended"), BUCKET) is False
    assert versioning_enabled(_FakeS3(None), BUCKET) is False


def _write_l2(path: Path, rows: list[tuple[str, int, int, str]]) -> None:
    """Write a minimal layer-2 parquet: (path, size, mtime, kind)."""
    tbl = pa.table({
        "path": [r[0] for r in rows],
        "size": pa.array([r[1] for r in rows], pa.int64()),
        "mtime": pa.array([r[2] for r in rows], pa.int64()),
        "kind": [r[3] for r in rows],
    })
    pq.write_table(tbl, path)


def _read_manifest(path: Path) -> list[tuple]:
    return duckdb.connect().execute(
        f"SELECT name, size_bytes, mtime, dir FROM read_parquet('{path}') ORDER BY name"
    ).fetchall()


def test_build_manifest_covers_staged_prefixes(tmp_path: Path) -> None:
    l2 = tmp_path / "l2.parquet"
    _write_l2(l2, [
        (".", 999, 0, "dir"),                     # bucket root — excluded (not a file)
        ("marin/ckpt", 500, 0, "dir"),            # dir row — excluded
        ("marin/ckpt/a", 100, 111, "file"),       # swept
        ("marin/ckpt/sub/d", 200, 222, "file"),   # swept (deeper under the sweep prefix)
        ("marin/other/c", 800, 444, "file"),      # not under any sweep prefix
    ])
    plan = Plan(name="p", bucket=BUCKET, sweep=["marin/ckpt/"], plan_id=3)
    out = tmp_path / "run"
    summary = build_manifest(str(l2), plan, str(out))

    assert _read_manifest(out / "manifest" / f"{BUCKET}.parquet") == [
        ("marin/ckpt/a", 100, 111, "marin/ckpt/"),
        ("marin/ckpt/sub/d", 200, 222, "marin/ckpt/sub/"),
    ]
    assert summary == {
        "plan_id": 3,
        "name": "p",
        "bucket": BUCKET,
        "sweep": ["marin/ckpt/"],
        "objects": 2,
        "bytes": 300,
        "manifest": str(out / "manifest" / f"{BUCKET}.parquet"),
    }
    assert json.loads((out / "plan-summary.json").read_text()) == summary


def test_prefix_free_and_eligible() -> None:
    assert prefix_free(["a/b/", "a/", "c/"]) == ["a/", "c/"]
    assert eligible("a/b/x", ["a/"]) is True
    assert eligible("a/y", ["a/", "c/"]) is True
    assert eligible("z/q", ["a/"]) is False  # no sweep match


class _FakeStore:
    """A minimal in-memory S3 for the executor: versioning, paginated list, batch delete."""

    def __init__(self, objects: dict[str, tuple[int, int]], versioning: str | None = "Enabled",
                 page: int = 2, fail_keys: set[str] | None = None) -> None:
        # objects: key -> (size, mtime_epoch)
        self.objects = dict(objects)
        self._versioning = versioning
        self._page = page
        self._fail = fail_keys or set()
        self.delete_calls = 0

    def get_bucket_versioning(self, Bucket: str) -> dict:  # noqa: N803
        return {"Status": self._versioning} if self._versioning else {}

    def list_objects_v2(self, Bucket: str, Prefix: str = "", ContinuationToken: str | None = None) -> dict:  # noqa: N803
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken) if ContinuationToken else 0
        chunk = keys[start:start + self._page]
        contents = [
            {"Key": k, "Size": self.objects[k][0],
             "LastModified": dt.datetime.fromtimestamp(self.objects[k][1], tz=dt.timezone.utc)}
            for k in chunk
        ]
        nxt = start + self._page
        truncated = nxt < len(keys)
        return {"Contents": contents, "IsTruncated": truncated,
                **({"NextContinuationToken": str(nxt)} if truncated else {})}

    def delete_objects(self, Bucket: str, Delete: dict) -> dict:  # noqa: N803
        self.delete_calls += 1
        deleted, errors = [], []
        for o in Delete["Objects"]:
            k = o["Key"]
            if k in self._fail:
                errors.append({"Key": k, "Code": "AccessDenied"})
            else:
                self.objects.pop(k, None)
                deleted.append({"Key": k})
        return {"Deleted": deleted, "Errors": errors}


def _run_with_manifest(tmp_path: Path) -> Path:
    """Build a run dir whose manifest = {a:(100,111), sub/d:(200,222), gone:(300,333)}."""
    l2 = tmp_path / "l2.parquet"
    _write_l2(l2, [
        ("marin/ckpt/a", 100, 111, "file"),
        ("marin/ckpt/sub/d", 200, 222, "file"),
        ("marin/ckpt/gone", 300, 333, "file"),
        ("marin/other/c", 400, 444, "file"),  # outside the plan — not in manifest
    ])
    plan = Plan(name="p", bucket=BUCKET, sweep=["marin/ckpt/"], plan_id=3)
    out = tmp_path / "run"
    build_manifest(str(l2), plan, str(out))
    return out


# Live store: a matches; sub/d overwritten (mtime drift); gone is absent;
# other/c live but outside the plan (ignored); new live + eligible → drift.
_LIVE = {
    "marin/ckpt/a": (100, 111),
    "marin/ckpt/sub/d": (200, 999),
    "marin/other/c": (400, 444),
    "marin/ckpt/new": (50, 555),
}
_COUNTS = {"deleted_objects": 1, "deleted_bytes": 100, "skipped_gone": 1,
           "skipped_overwritten": 1, "drift_new": 1, "delete_failed": 0}


def test_execute_dry_run_decides_without_deleting(tmp_path: Path) -> None:
    run = _run_with_manifest(tmp_path)
    store = _FakeStore(_LIVE, versioning=None)  # dry run needs no versioning
    s = execute_plan(str(run), for_real=False, client=store)

    assert {k: s[k] for k in _COUNTS} == _COUNTS
    assert s["mode"] == "dry"
    assert store.delete_calls == 0 and "marin/ckpt/a" in store.objects  # nothing deleted
    # decision log: exact (name, decision) set
    log = duckdb.connect().execute(
        f"SELECT name, decision FROM read_parquet('{run}/would-delete/{BUCKET}/part-00000.parquet') ORDER BY name"
    ).fetchall()
    assert log == [
        ("marin/ckpt/a", "delete"),
        ("marin/ckpt/gone", "skipped_gone"),
        ("marin/ckpt/sub/d", "skipped_overwritten"),
    ]


def test_execute_for_real_deletes_matched(tmp_path: Path) -> None:
    run = _run_with_manifest(tmp_path)
    store = _FakeStore(_LIVE, versioning="Enabled")
    s = execute_plan(str(run), for_real=True, client=store)

    assert {k: s[k] for k in _COUNTS} == _COUNTS
    assert s["mode"] == "real"
    assert "marin/ckpt/a" not in store.objects  # the one matched key is gone
    assert set(store.objects) == {"marin/ckpt/sub/d", "marin/other/c", "marin/ckpt/new"}
    assert s["bands"] == [{"prefix": "marin/ckpt/", "bytes": 100, "objects": 1,
                           "gone": 1, "overwritten": 1, "drift_new": 1}]


def test_execute_for_real_refused_without_versioning(tmp_path: Path) -> None:
    run = _run_with_manifest(tmp_path)
    store = _FakeStore(_LIVE, versioning="Suspended")
    with pytest.raises(SweepError, match="versioning"):
        execute_plan(str(run), for_real=True, client=store)
    assert store.delete_calls == 0


def test_execute_records_delete_failure(tmp_path: Path) -> None:
    run = _run_with_manifest(tmp_path)
    store = _FakeStore(_LIVE, versioning="Enabled", fail_keys={"marin/ckpt/a"})
    s = execute_plan(str(run), for_real=True, client=store)
    assert (s["deleted_objects"], s["delete_failed"]) == (0, 1)
    assert "marin/ckpt/a" in store.objects  # failed delete left it in place


class _FakeVersionedStore:
    """Versioned in-memory S3 for the delete→undo→purge lifecycle: a delete with
    no VersionId appends a delete marker; a delete with VersionId drops that version."""

    def __init__(self, objects: dict[str, tuple[int, int]], page: int = 100) -> None:
        self._n = 0
        self._page = page
        # key -> list of versions {VersionId, Size, mtime, dm}, oldest first (last = latest)
        self.versions: dict[str, list[dict]] = {}
        for k, (size, mtime) in objects.items():
            self.versions[k] = [self._ver(size, mtime, False)]

    def _ver(self, size: int, mtime: int, dm: bool) -> dict:
        self._n += 1
        return {"VersionId": f"v{self._n}", "Size": size, "mtime": mtime, "dm": dm}

    def get_bucket_versioning(self, Bucket: str) -> dict:  # noqa: N803
        return {"Status": "Enabled"}

    def _live(self, key: str) -> dict | None:
        vs = self.versions.get(key)
        if vs and not vs[-1]["dm"]:
            return vs[-1]
        return None

    def list_objects_v2(self, Bucket: str, Prefix: str = "", ContinuationToken: str | None = None) -> dict:  # noqa: N803
        keys = sorted(k for k in self.versions if k.startswith(Prefix) and self._live(k))
        contents = [
            {"Key": k, "Size": self._live(k)["Size"],  # type: ignore[index]
             "LastModified": dt.datetime.fromtimestamp(self._live(k)["mtime"], tz=dt.timezone.utc)}  # type: ignore[index]
            for k in keys
        ]
        return {"Contents": contents, "IsTruncated": False}

    def list_object_versions(self, Bucket: str, Prefix: str = "",  # noqa: N803
                             KeyMarker: str | None = None, VersionIdMarker: str | None = None) -> dict:
        versions, markers = [], []
        for k in sorted(self.versions):
            if not k.startswith(Prefix):
                continue
            vs = self.versions[k]
            for i, v in enumerate(vs):
                latest = i == len(vs) - 1
                row = {"Key": k, "VersionId": v["VersionId"], "IsLatest": latest}
                if v["dm"]:
                    markers.append(row)
                else:
                    versions.append({**row, "Size": v["Size"]})
        return {"Versions": versions, "DeleteMarkers": markers, "IsTruncated": False}

    def delete_objects(self, Bucket: str, Delete: dict) -> dict:  # noqa: N803
        deleted = []
        for o in Delete["Objects"]:
            k, vid = o["Key"], o.get("VersionId")
            vs = self.versions.get(k)
            if vs is None:
                continue
            if vid is None:
                vs.append(self._ver(0, 0, True))  # write a delete marker
            else:
                self.versions[k] = [v for v in vs if v["VersionId"] != vid]
            deleted.append({"Key": k})
        return {"Deleted": deleted, "Errors": []}


def test_delete_undo_purge_lifecycle(tmp_path: Path) -> None:
    run = _run_with_manifest(tmp_path)  # manifest = a(100,111), sub/d(200,222), gone(300,333)
    store = _FakeVersionedStore({
        "marin/ckpt/a": (100, 111),         # matches → deleted
        "marin/ckpt/sub/d": (200, 999),     # overwritten → skipped
        "marin/other/c": (400, 444),        # outside the plan → ignored
        "marin/ckpt/new": (50, 555),        # drift → ignored
    })

    # 1. real delete → `a` gets a delete marker; it disappears from a live list.
    s1 = execute_plan(str(run), for_real=True, client=store)
    assert s1["deleted_objects"] == 1
    assert store._live("marin/ckpt/a") is None
    assert [v["dm"] for v in store.versions["marin/ckpt/a"]] == [False, True]

    # 2. undo → the delete marker is removed; `a` is live again.
    u = undo_run(str(run), client=store)
    assert (u["restored"], u["restore_failed"]) == (1, 0)
    assert store._live("marin/ckpt/a") is not None
    assert [v["dm"] for v in store.versions["marin/ckpt/a"]] == [False]

    # 3. re-delete, then purge → every version of `a` is gone (data + marker).
    execute_plan(str(run), for_real=True, client=store)
    p = purge_run(str(run), client=store)
    assert (p["purged_versions"], p["purged_bytes"]) == (2, 100)
    assert store.versions["marin/ckpt/a"] == []


def test_undo_dry_run_touches_nothing(tmp_path: Path) -> None:
    run = _run_with_manifest(tmp_path)
    store = _FakeVersionedStore({"marin/ckpt/a": (100, 111)})
    execute_plan(str(run), for_real=True, client=store)
    before = list(store.versions["marin/ckpt/a"])
    u = undo_run(str(run), client=store, dry_run=True)
    assert u["restored"] == 1 and u["dry_run"] is True
    assert store.versions["marin/ckpt/a"] == before  # unchanged


# --- versioning guard opt-out ---------------------------------------------


def test_execute_for_real_without_versioning_guard(tmp_path: Path, capsys) -> None:
    # versioning off, guard disabled: the deletes proceed, loudly, and the summary says so
    run = _run_with_manifest(tmp_path)
    store = _FakeStore(_LIVE, versioning="Suspended")
    s = execute_plan(str(run), for_real=True, client=store, require_versioning=False)
    assert {k: s[k] for k in _COUNTS} == _COUNTS
    assert s["versioning_guard"] is False
    assert "marin/ckpt/a" not in store.objects
    assert capsys.readouterr().err == "WARNING: versioning guard disabled — deletes are permanent (bucket marin-us-east-02a)\n"
    assert json.loads((run / "deleted-summary.json").read_text())["versioning_guard"] is False
    # the default path records the guard as on (and stays silent)
    (tmp_path / "again").mkdir()
    run2 = _run_with_manifest(tmp_path / "again")
    s2 = execute_plan(str(run2), for_real=True, client=_FakeStore(_LIVE, versioning="Enabled"))
    assert (s2["versioning_guard"], capsys.readouterr().err) == (True, "")
    assert execute_plan(str(run2), for_real=False, client=_FakeStore(_LIVE, versioning=None))["versioning_guard"] is True
