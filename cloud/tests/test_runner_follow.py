"""The real runners' local stages after `follow`: a generation compacted since the profile named it moves the runner to
its successor (`Runner.follow` replaces `cfg`), and `prepare` / `publish` / `prune` must write there too, not under the
profile's generation (gcs, 2026-10-22: the interval store's `prepare` wrote `deltas/<D>/scans.json` under the old
generation while the `ranges` job read the successor's, and every task failed)."""
from __future__ import annotations

import pytest

from dt_cloud import append_runner as ar
from dt_cloud import interval_append as ia
from dt_cloud import static_append as sa
from dt_cloud import static_runner as sd

from test_interval_append import _cfg
from test_static_runner import CFG, GEN


class _Client:
    def __init__(self, *a, **kw):
        pass

    def bucket(self, name):
        return self

    def list_blobs(self, *a, **kw):
        return []


@pytest.fixture
def calls(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr("google.cloud.storage.Client", _Client)
    monkeypatch.setattr(ar, "prune_folded", lambda store, **kw: {"keys": []})
    monkeypatch.setattr(ar, "GcsRunStore", lambda bucket, scratch, gen, **kw: calls.append(("folded", gen)))
    return calls


def test_interval_stages_use_the_followed_generation(monkeypatch, calls):
    monkeypatch.setattr(ia, "prepare_scan", lambda bucket, gen, d, layouts, attribution=False: calls.append(("prepare", gen, d)))
    monkeypatch.setattr(ia, "prune_state", lambda client, gen, d, k, **kw: calls.append(("prune", gen, d)) or {"delete": []})
    keys = {"interval-store/g1.c001/ranges.json": {"k": 4}, "interval-store/g1.c001/scans.json": {"scans": []}}

    class _Store:
        def read_json(self, key):
            return keys[key]

        def exists(self, key):
            return key in keys

        def keys(self, prefix):
            return []

    monkeypatch.setattr(ia, "Store", lambda url: _Store())
    r = ia.store_runner(_cfg())
    r.cfg = r.with_gen("g1.c001")
    r.prepare("2026-08-06")
    r.prune("2026-08-06")
    assert calls == [("prepare", "g1.c001", "2026-08-06"), ("prune", "g1.c001", "2026-08-06"), ("folded", "g1.c001")]


def test_static_stages_use_the_followed_generation(monkeypatch, calls):
    monkeypatch.setattr(sa.prepare_cmd, "callback", lambda bucket, date, gen: calls.append(("prepare", gen, date)))
    monkeypatch.setattr(sa.publish_cmd, "callback", lambda bucket, date, gen, dry_run: calls.append(("publish", gen, date)))
    monkeypatch.setattr(sa, "prune_state", lambda client, gen, d, k, **kw: calls.append(("prune", gen, d)) or {})
    monkeypatch.setattr("dt_cloud.static_names.read_json", lambda url: {"k": 4})
    r = sd.gcs_runner(CFG)
    nxt = f"{GEN}.c001"
    r.cfg = r.with_gen(nxt)
    r.prepare("2026-10-10")
    r.publish("2026-10-10")
    r.prune("2026-10-10")
    assert calls == [("prepare", nxt, "2026-10-10"), ("publish", nxt, "2026-10-10"), ("prune", nxt, "2026-10-10"), ("folded", nxt)]
