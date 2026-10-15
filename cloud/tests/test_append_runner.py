"""`dt_cloud.append_runner`, the machinery both run stores share, where the stores' own tests don't reach: the R2 copy of
one manifest (its runs, each run's liveness markers after the rest of it, a check, the manifest last; nothing of the
manifest when the check fails), the Batch poll's cap, `exit_on`'s exit codes, and the merge status record's R2 sink."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

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


# ── Prune: the runs a carry folded ─────────────────────────────────────────

S1, S2, S3_, S4, S5 = "2026-10-01T0001", "2026-10-01T0601", "2026-10-01T1201", "2026-10-01T1801", "2026-10-02T0001"
#: Each run dir's files (its served file, its version deltas, its pinned scan).
RUN_FILES = ("served/path.parquet", "cdelta/r0000.parquet", "scans.json")
#: The base generation's files (a manifest-shaped name among them): never deleted.
BASE = ["scans.json", "ranges.json", "cintervals/r0000.parquet", "drill/meta.json", f"{S1}.json"]


def _run(*scans: str) -> str:
    return ar.run_key(scans[0], scans[-1])


def _listing(runs: list[str], manifests: dict[str, list[str]]) -> dict[str, int]:
    """A store: the base's files (size 5), each run's `RUN_FILES` (sizes 1, 2, 3), the manifests (size 9)."""
    return {**{k: 5 for k in BASE}, **{f"{r}/{f}": i + 1 for r in runs for i, f in enumerate(RUN_FILES)}, **{m: 9 for m in manifests}}



#: The binary counter over five scans, a carry after the 2nd and the 4th: each manifest's runs.
CHAIN = {
    f"{S1}.json": [_run(S1)],
    f"{S2}.json": [_run(S1), _run(S2)],
    f"{S2}.m001.json": [_run(S1, S2)],
    f"{S3_}.json": [_run(S1, S2), _run(S3_)],
    f"{S4}.json": [_run(S1, S2), _run(S3_), _run(S4)],
    f"{S4}.m001.json": [_run(S1, S4)],
    f"{S5}.json": [_run(S1, S4), _run(S5)],
}
ALL_RUNS = [_run(S1), _run(S2), _run(S1, S2), _run(S3_), _run(S4), _run(S1, S4), _run(S5)]
#: Now, and a day.
T, DAY = 1_800_000_000, 86_400


def _upto(last: str) -> dict[str, list[str]]:
    """`manifests/<name>` → its runs, for the chain's manifests through `last`."""
    names = list(CHAIN)
    return {f"manifests/{m}": CHAIN[m] for m in names[:names.index(last) + 1]}


def _times(m: dict[str, list[str]], recent: dict[str, float] | None = None) -> list[tuple[str, float]]:
    """`(key, mtime)` of manifests `m`: a day apart, the last a day before `T`, but those `recent` names (`<name>.json`)
    at the times it gives."""
    recent = recent or {}
    return [(k, recent.get(k.removeprefix("manifests/"), T - (len(m) - i) * DAY)) for i, k in enumerate(m)]


def _files(*runs: str, files: tuple[str, ...] = RUN_FILES) -> list[str]:
    return sorted(f"{r}/{f}" for r in runs for f in files)


#: A catch-up: three manifests within a minute (`S4`'s, the carry's revision of it, `S5`'s), the rest days old.
CATCH_UP = {f"{S4}.json": T, f"{S4}.m001.json": T + 20, f"{S5}.json": T + 40}


def test_a_catch_up_within_a_minute_prunes_nothing_they_list():
    """A minute after a catch-up of three manifests, every manifest superseded within the grace is kept (`S3`'s too:
    superseded by `S4`'s, a minute ago), so a reader holding any of them never 404s: only `S1`, `S2` go, folded by `S2`'s
    revision days ago. (One revision of grace would have kept only `S4.m001` and `S5`, deleting `S1_S2`, `S3` and `S4`,
    which a reader holding `S4`'s manifest, cached 40 s ago, still reads.)"""
    m = _upto(f"{S5}.json")
    times = _times(m, CATCH_UP)
    assert ar.grace_manifests(times, T + 60) == [f"manifests/{x}" for x in (f"{S3_}.json", f"{S4}.json", f"{S4}.m001.json", f"{S5}.json")]
    assert ar.plan_prune(_listing(ALL_RUNS, m), times, m, T + 60) == _files(_run(S1), _run(S2))


def test_past_the_grace_the_folded_runs_go():
    """The same manifests, `now` past the grace window: only the newest is kept, and every run folded into its runs goes.
    A manifest is kept while its successor's write time is less than `grace_s` before `now`."""
    m = _upto(f"{S5}.json")
    times = _times(m, CATCH_UP)
    assert ar.grace_manifests(times, T + 40 + ar.GRACE_S - 1) == [f"manifests/{S4}.m001.json", f"manifests/{S5}.json"]
    assert ar.grace_manifests(times, T + 40 + ar.GRACE_S) == [f"manifests/{S5}.json"]
    assert ar.plan_prune(_listing(ALL_RUNS, m), times, m, T + 40 + ar.GRACE_S) == _files(_run(S1), _run(S2), _run(S1, S2), _run(S3_), _run(S4))
    # a shorter grace: past it sooner
    assert ar.plan_prune(_listing(ALL_RUNS, m), times, m, T + 40 + 600, grace_s=600) == _files(_run(S1), _run(S2), _run(S1, S2), _run(S3_), _run(S4))


def test_plan_prune_reads_only_the_manifests_in_the_window():
    """Runs of manifests outside the window are irrelevant: passing only those `grace_manifests` keeps plans the same; one
    of those missing raises."""
    m = _upto(f"{S5}.json")
    times = _times(m, CATCH_UP)
    kept = ar.grace_manifests(times, T + 60)
    listing = _listing(ALL_RUNS, m)
    assert ar.plan_prune(listing, times, {k: m[k] for k in kept}, T + 60) == ar.plan_prune(listing, times, m, T + 60) == _files(_run(S1), _run(S2))
    with pytest.raises(KeyError):
        ar.plan_prune(listing, times, {k: m[k] for k in kept[1:]}, T + 60)


def test_plan_prune_keeps_runs_in_flight():
    """No manifest lists them, and none is a run a carry folded: an append's run past the newest manifest (`S5`), and a
    merge's output spanning several of its runs (`S3_S4`, until its revision lists it), are kept. A dir inside one of the
    newest's runs that no manifest lists (a superseded merge's output: `S3_S4` under `S1_S4`) is garbage: deleted."""
    m = _upto(f"{S4}.json")
    runs = [_run(S1, S2), _run(S3_), _run(S4), _run(S3_, S4), _run(S5)]
    assert ar.plan_prune(_listing(runs, m), _times(m), m, T) == []
    m = _upto(f"{S4}.m001.json")
    runs = [_run(S1, S2), _run(S3_), _run(S4), _run(S1, S4), _run(S3_, S4), _run(S5)]
    assert ar.plan_prune(_listing(runs, m), _times(m), m, T) == _files(_run(S1, S2), _run(S3_), _run(S4), _run(S3_, S4))


def test_plan_prune_never_deletes_the_base_manifests_or_retained_files():
    """The base generation's files (outside `deltas/`) and every manifest stay, whatever is folded; `retain` keeps those
    run-relative paths (a prefix ending `/`, or an exact name) of a folded run. A file directly under `deltas/`, or a dir
    there that isn't a run's, is no run's: kept."""
    m = _upto(f"{S3_}.json")
    runs = [_run(S1), _run(S2), _run(S1, S2), _run(S3_)]
    listing = {**_listing(runs, m), "deltas/README": 1, "deltas/tmp/x.parquet": 1}
    assert ar.plan_prune(listing, _times(m), m, T, retain=("cdelta/", "scans.json")) == _files(_run(S1), _run(S2), files=("served/path.parquet",))
    # neither a prefix (no trailing `/`) nor a whole name: retains nothing
    assert ar.plan_prune(listing, _times(m), m, T, retain=("cdelta", "scans")) == _files(_run(S1), _run(S2))
    assert ar.plan_prune({k: 1 for k in BASE}, [], {}, T) == []


def _write_store(root, listing: dict[str, int], manifests: dict[str, list[str]], times: list[tuple[str, float]]) -> None:
    for k, n in listing.items():
        p = root / k
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"runs": [{"key": r} for r in manifests[k]]}) if k in manifests else "x" * n)
    for k, t in times:
        os.utime(root / k, (t, t))


def test_prune_folded_deletes_with_bytes_logged(tmp_path):
    """Over a local store, its manifests' mtimes the write times: the plan's files deleted (emptied dirs too), each run
    logged with its objects and bytes; a dry run logs the same and deletes nothing; a second prune finds nothing."""
    m = _upto(f"{S3_}.json")
    times = _times(m, {f"{S3_}.json": T - 10})  # `S2`'s revision superseded 10 s ago: in the grace
    _write_store(tmp_path, _listing([_run(S1), _run(S2), _run(S1, S2), _run(S3_)], m), m, times)
    store = ar.LocalRunStore(tmp_path, tmp_path / "scratch")
    assert sorted(store.mtimes("manifests/").items()) == sorted(times)
    before = sorted(store.listing(""))
    logs = []
    doc = ar.prune_folded(store, now=T, retain=("cdelta/",), dry_run=True, log=logs.append)
    keys = _files(_run(S1), _run(S2), files=("served/path.parquet", "scans.json"))
    assert doc == {"manifest": f"manifests/{S3_}.json", "grace": [f"manifests/{S2}.m001.json"],
                   "runs": [{"key": _run(S1), "objects": 2, "bytes": 4}, {"key": _run(S2), "objects": 2, "bytes": 4}],
                   "objects": 4, "bytes": 8, "deleted": 0, "keys": keys}
    kept = f"(newest manifests/{S3_}.json; superseded within 3600 s: manifests/{S2}.m001.json)"
    assert logs == [f"prune: would delete deltas/{S1}/ (2 objects, 4 B)", f"prune: would delete deltas/{S2}/ (2 objects, 4 B)",
                    f"prune: would delete 2 folded runs, 4 objects, 8 B {kept}"]
    assert sorted(store.listing("")) == before
    logs.clear()
    doc = ar.prune_folded(store, now=T, retain=("cdelta/",), where=" gcs", log=logs.append)
    assert (doc["deleted"], logs[-1]) == (4, f"prune gcs: deleting 2 folded runs, 4 objects, 8 B {kept}")
    assert sorted(store.listing("")) == sorted(set(before) - set(keys))
    assert sorted(p.relative_to(tmp_path).as_posix() for p in (tmp_path / "deltas" / S1).rglob("*")) == [
        f"deltas/{S1}/cdelta", f"deltas/{S1}/cdelta/r0000.parquet"]
    assert ar.prune_folded(store, now=T, retain=("cdelta/",), log=logs.append)["deleted"] == 0


class R2Fake:
    """A boto3 S3 client over `objects` (`{key: (bytes, mtime)}`): `list_objects_v2` in pages of `page` keys,
    `get_object`, and `delete_objects` (each batch recorded)."""

    def __init__(self, objects: dict[str, tuple[bytes, float]], page: int = 2):
        self.objects, self.page, self.batches = dict(objects), page, []

    def get_paginator(self, name: str):
        fake = self

        class Pages:
            def paginate(self, Bucket, Prefix):
                keys = sorted(k for k in fake.objects if k.startswith(Prefix))
                for i in range(0, len(keys), fake.page):
                    yield {"Contents": [{"Key": k, "Size": len(fake.objects[k][0]),
                                         "LastModified": datetime.fromtimestamp(fake.objects[k][1], timezone.utc)} for k in keys[i:i + fake.page]]}
        assert name == "list_objects_v2"
        return Pages()

    def get_object(self, Bucket, Key):
        body = self.objects[Key][0]
        return {"Body": type("Body", (), {"read": lambda s: body})()}

    def delete_objects(self, Bucket, Delete):
        self.batches.append([o["Key"] for o in Delete["Objects"]])
        for o in Delete["Objects"]:
            del self.objects[o["Key"]]
        return {}


def test_prune_folded_on_r2_plans_by_r2s_own_manifests():
    """R2 is planned by its own manifests and their `LastModified`, which may lag GCS's: here R2's newest is `S4`'s own
    (GCS's revision, folding `S1_S2`, `S3`, `S4`, not copied yet), so only what `S2`'s revision folded goes."""
    m = _upto(f"{S4}.json")
    times = dict(_times(m))
    r2 = R2Fake({f"{ROOT}/{k}": ((json.dumps({"runs": [{"key": r} for r in m[k]]}).encode(), times[k]) if k in m else (b"x" * n, T - DAY))
                 for k, n in _listing(ALL_RUNS[:5], m).items()})
    store = ar.R2RunStore(r2, "r2b", ROOT)
    doc = ar.prune_folded(store, now=T, log=lambda s: None)
    assert (doc["keys"], r2.batches) == (_files(_run(S1), _run(S2)), [[f"{ROOT}/{k}" for k in _files(_run(S1), _run(S2))]])
    assert sorted(store.listing("deltas/")) == _files(_run(S1, S2), _run(S3_), _run(S4))


def test_r2_publish_refuses_a_run_without_served_files(monkeypatch):
    """A manifest whose run is gone from GCS (folded and pruned) never reaches R2."""
    r2 = R2([f"{ROOT}/deltas/a_b/{f}" for f in FILES] + [f"{ROOT}/manifests/c.json"])
    r2.patch(monkeypatch, {"runs": RUNS})
    with pytest.raises(SystemExit) as e:
        ar.r2_publish("data", ROOT, "c", served=("served/",), workers=1, log=lambda m: None)
    assert str(e.value) == "r2 c: deltas/c has no served files on GCS (folded and pruned?): manifest not copied"
    assert r2.r2 == []
