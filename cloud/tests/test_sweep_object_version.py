"""Deletion uses the reviewed token, including when an object changes after LIST."""
import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from botocore.exceptions import ClientError

from dt_cloud.sweep import Plan, SweepError, build_manifest, execute_plan


def run_dir(tmp_path: Path, token: str | None = '"reviewed"') -> Path:
    path = tmp_path / "l2.parquet"
    pq.write_table(pa.table({"path": ["p/f"], "kind": ["file"], "size": [7], "mtime": [100],
                             "version": pa.array([token], pa.string())}), path)
    out = tmp_path / "run"
    build_manifest(str(path), Plan(name="p", bucket="b", sweep=["p/"]), str(out))
    return out


class ConditionalStore:
    def __init__(self, code: str | None = None) -> None:
        self.code = code
        self.calls = []

    def list_objects_v2(self, **kwargs) -> dict:
        return {"Contents": [{"Key": "p/f", "Size": 7, "ETag": '"reviewed"',
                              "LastModified": dt.datetime.fromtimestamp(100, dt.timezone.utc)}]}

    def delete_object(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self.code:
            raise ClientError({"Error": {"Code": self.code}}, "DeleteObject")
        return {}


@pytest.mark.parametrize("code,decision,counts", [
    (None, "delete", (1, 0, 0)),
    ("PreconditionFailed", "skipped_overwritten", (0, 1, 0)),
    ("AccessDenied", "delete_failed", (0, 0, 1)),
])
def test_reviewed_token_is_the_atomic_delete_precondition(tmp_path: Path, code, decision, counts) -> None:
    out = run_dir(tmp_path)
    store = ConditionalStore(code)
    summary = execute_plan(str(out), for_real=True, client=store)
    assert store.calls == [{"Bucket": "b", "Key": "p/f", "IfMatch": '"reviewed"'}]
    assert tuple(summary[k] for k in ("deleted_objects", "skipped_overwritten", "delete_failed")) == counts
    assert pq.read_table(out / "deleted/b/part-00000.parquet")["decision"].to_pylist() == [decision]


@pytest.mark.parametrize("token", [None, "", "*"])
def test_unknown_or_wildcard_identity_refuses_execution(tmp_path: Path, token) -> None:
    out = run_dir(tmp_path, token)
    store = ConditionalStore()
    with pytest.raises(SweepError, match="^manifest has 1 object without a usable reviewed ETag; take a new scan and re-stage$"):
        execute_plan(str(out), for_real=True, client=store)
    assert store.calls == []


def test_staging_older_scan_holds_etag_only_replacement(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("then.parquet", "now.parquet")]
    for path, token in zip(paths, ['"old"', '"replacement"']):
        pq.write_table(pa.table({"path": ["p/f"], "kind": ["file"], "size": [7], "mtime": [100],
                                 "version": [token]}), path)
    summary = build_manifest(str(paths[1]), Plan(name="p", bucket="b", sweep=["p/"], as_of={"p/": "then"}),
                             str(tmp_path / "run"), date="now", l2_for=lambda _: str(paths[0]))
    assert summary["skipped_after_as_of"] == {"objects": 1, "bytes": 7, "new_objects": 0, "changed_objects": 1}
    assert summary["objects"] == 0


def test_preserved_analytics_distinguishes_new_changed_and_post_scan_keys(tmp_path: Path) -> None:
    then, now = tmp_path / "then.parquet", tmp_path / "now.parquet"
    def write(path: Path, names: list[str], times: list[int], tokens: list[str]) -> None:
        pq.write_table(pa.table({"path": names, "kind": ["file"] * len(names), "size": [7] * len(names),
                                 "mtime": times, "version": tokens}), path)
    write(then, ["p/f", "p/replaced"], [100, 100], ['"reviewed"', '"old"'])
    write(now, ["p/f", "p/new", "p/replaced"], [100, 200, 200], ['"reviewed"', '"new"', '"changed"'])
    out = tmp_path / "run"
    plan = build_manifest(str(now), Plan(name="p", bucket="b", sweep=["p/"], as_of={"p/": "then"}),
                          str(out), date="now", l2_for=lambda _: str(then))
    assert plan["skipped_after_as_of"] == {"objects": 2, "bytes": 14, "new_objects": 1, "changed_objects": 1}
    assert pq.read_table(out / "manifest/b-preserved.parquet").to_pylist() == [
        {"name": "p/new", "reason": "new_since_review"},
        {"name": "p/replaced", "reason": "changed_since_review"},
    ]
    class Store(ConditionalStore):
        def list_objects_v2(self, **kwargs) -> dict:
            return {"Contents": [
                {"Key": name, "Size": 7, "ETag": token, "LastModified": dt.datetime.fromtimestamp(ts, dt.timezone.utc)}
                for name, ts, token in [("p/f", 100, '"reviewed"'), ("p/new", 200, '"new"'),
                                       ("p/replaced", 200, '"changed"'), ("p/after", 300, '"after"')]
            ]}
    store = Store()
    result = execute_plan(str(out), for_real=True, client=store)
    assert result["preserved_breakdown"] == {"new_since_review": 1, "changed_since_review": 1, "new_since_scan": 1, "unclassified": 0}
    assert (result["deleted_objects"], result["drift_new"], result["delete_failed"]) == (1, 3, 0)
    assert store.calls == [{"Bucket": "b", "Key": "p/f", "IfMatch": '"reviewed"'}]
