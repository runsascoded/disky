"""`dt_cloud.append_runner`, the machinery both run stores share, where the stores' own tests don't reach: the R2 copy of
one manifest (its runs, each run's liveness markers after the rest of it, a check, the manifest last; nothing of the
manifest when the check fails), the Batch poll's cap, `exit_on`'s exit codes, and the merge status record's R2 sink."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from dt_cloud import append_runner as ar
from dt_cloud import publish as pub
from dt_cloud import static_names as sn

ROOT = "interval-store/g1"


@dataclass(frozen=True)
class Obj:
    key: str
    size: int
    md5: str | None = None
    content_type: str | None = None


class R2:
    """GCS objects by key, and R2 as the keys copied there in order; `lose`: keys a copy "succeeds" on but never lands."""

    def __init__(self, keys: list[str], lose: frozenset = frozenset()):
        self.gcs = {k: Obj(k, 10) for k in keys}
        self.r2: list[str] = []
        self.lose = lose

    def patch(self, monkeypatch, manifest: dict) -> None:
        monkeypatch.setattr(sn, "read_json", lambda uri: manifest)
        monkeypatch.setattr(pub, "list_source", lambda bucket, prefixes: sorted((o for o in self.gcs.values() if any(o.key.startswith(p) for p in prefixes)), key=lambda o: o.key))
        monkeypatch.setattr(pub, "r2_client", lambda: None)
        monkeypatch.setattr(pub, "r2_bucket", lambda: "r2b")
        monkeypatch.setattr(pub, "head_dest", lambda s3, bucket, key: key if key in self.r2 else None)
        monkeypatch.setattr(pub, "should_copy", lambda o, dst: dst is None)
        monkeypatch.setattr(pub, "copy_one", lambda bucket, s3, r2, o: None if o.key in self.lose else self.r2.append(o.key))


RUNS = [{"key": "deltas/a_b"}, {"key": "deltas/c"}]
FILES = ["served/path.parquet", "served/path.groups.parquet", "drill/meta.json", "drill/x.parquet", "pvl/r0000.parquet"]


def test_r2_publish_copies_each_run_then_its_markers_then_checks_then_the_manifest(monkeypatch):
    keys = [f"{ROOT}/{r['key']}/{f}" for r in RUNS for f in FILES] + [f"{ROOT}/manifests/c.m001.json", f"{ROOT}/manifests/c.json"]
    r2 = R2(keys)
    r2.patch(monkeypatch, {"runs": RUNS})
    doc = ar.r2_publish("data", ROOT, "c.m001", served=("served/", "drill/"), markers=("drill/meta.json",), workers=1, log=lambda m: None)
    body = [f"{ROOT}/{r['key']}/{f}" for r in RUNS for f in ("drill/x.parquet", "served/path.groups.parquet", "served/path.parquet")]
    assert r2.r2 == [*body, *(f"{ROOT}/{r['key']}/drill/meta.json" for r in RUNS), f"{ROOT}/manifests/c.m001.json"]
    assert doc == {"manifest": "c.m001", "runs": ["deltas/a_b", "deltas/c"], "objects": 8, "copied": 8, "bytes": 80, "s": doc["s"]}
    # A rerun copies only the manifest (`should_copy` skips what R2 holds), the check passing again.
    r2.r2 = [k for k in r2.r2 if "/manifests/" not in k]
    ar.r2_publish("data", ROOT, "c.m001", served=("served/", "drill/"), markers=("drill/meta.json",), workers=1, log=lambda m: None)
    assert r2.r2[-1:] == [f"{ROOT}/manifests/c.m001.json"]


def test_r2_publish_never_copies_the_manifest_when_a_run_is_missing_there(monkeypatch):
    keys = [f"{ROOT}/{r['key']}/{f}" for r in RUNS for f in FILES] + [f"{ROOT}/manifests/c.json"]
    lost = f"{ROOT}/deltas/c/served/path.parquet"
    r2 = R2(keys, lose=frozenset({lost}))
    r2.patch(monkeypatch, {"runs": RUNS})
    with pytest.raises(SystemExit) as e:
        ar.r2_publish("data", ROOT, "c", served=("served/",), workers=1, log=lambda m: None)
    assert str(e.value) == f"r2 c: 1 of 4 served files not on R2 after the copy (e.g. {lost}): manifest not copied"
    assert [k for k in r2.r2 if "/manifests/" in k] == []


def test_batch_runner_polls_at_most_every_30s(monkeypatch):
    from dt_cloud import batch, gcp

    sleeps = []
    states = iter(["QUEUED", "RUNNING", "RUNNING", "RUNNING", "RUNNING", "SUCCEEDED"])
    monkeypatch.setattr(batch, "submit_job", lambda spec, name, region: None)
    monkeypatch.setattr(gcp, "batch_job", lambda name, project, region: {"status": {"state": next(states)}})
    monkeypatch.setattr(ar.time, "sleep", sleeps.append)
    ar.BatchRunner(ar.Profile(region="r", project="p"), lambda m: None)("j", {})
    assert sleeps == [10, 20, 30, 30, 30]


@pytest.mark.parametrize("raise_, code, logged", [
    (ar.NotNext("not yet"), ar.NOT_NEXT, ["x: not yet"]),
    (RuntimeError("Batch job j: FAILED"), 1, ["x: Batch job j: FAILED"]),
])
def test_exit_on(raise_, code, logged):
    logs = []

    def fn():
        raise raise_

    with pytest.raises(SystemExit) as e:
        ar.exit_on("x", fn, logs.append)
    assert (e.value.code, logs) == (code, logged)
    assert ar.exit_on("x", lambda: [1], logs.append) == [1]


class S3:
    """A boto3 S3 client recording its calls."""

    def __init__(self):
        self.calls: list[tuple] = []

    def put_object(self, **kw):
        self.calls.append(("put_object", kw))

    def delete_object(self, **kw):
        self.calls.append(("delete_object", kw))


def test_r2_merge_status_writes_beside_the_store():
    """The record goes to `<prefix>/<gen>/merging.json` in the serving bucket (no reader lists the generation's root),
    uncached; a delete removes it."""
    s3 = S3()
    st = ar.R2MergeStatus(s3, "r2b", f"{ROOT}/{ar.MERGE_STATUS}")
    st.put({"holder": "iv-merge-x", "merges": []})
    st.delete()
    assert s3.calls == [
        ("put_object", {"Bucket": "r2b", "Key": "interval-store/g1/merging.json", "Body": b'{\n "holder": "iv-merge-x",\n "merges": []\n}\n',
                        "ContentType": "application/json", "CacheControl": "no-store"}),
        ("delete_object", {"Bucket": "r2b", "Key": "interval-store/g1/merging.json"}),
    ]


def test_r2_merge_status_from_the_env():
    """Built from the R2 copy's env; without an endpoint or bucket, None (logged): the merge runs, /health doesn't see it."""
    logs = []
    assert ar.r2_merge_status(ROOT, env={"R2_BUCKET": "r2b"}, log=logs.append) is None
    assert logs == ["merge status: no R2_ENDPOINT in the env; /health won't show this merge"]
    st = ar.r2_merge_status(ROOT, env={"R2_ENDPOINT": "https://acct.r2.cloudflarestorage.com", "R2_BUCKET": "r2b", "R2_ACCESS_KEY_ID": "k",
                                       "R2_SECRET_ACCESS_KEY": "s"}, log=logs.append)
    assert (type(st), st.bucket, st.key, st.client.meta.endpoint_url, len(logs)) == (
        ar.R2MergeStatus, "r2b", "interval-store/g1/merging.json", "https://acct.r2.cloudflarestorage.com", 1)
