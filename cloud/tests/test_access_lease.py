"""The access ingest as an hourly poller (specs/access-log-freshness.md §A): its single-writer lease
(`access/state/.lease`: taken, held, an expired one taken over, released on a TERM mid-run and only ever our own), the
no-pending fast exit, the stalled-delivery report, and the per-bucket fan-out."""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import sys
import types

import pytest

from dt_cloud import access as A


class PreconditionFailed(Exception):
    pass


class NotFound(Exception):
    pass


class Blob:
    def __init__(self, store: dict, name: str):
        self.store, self.name, self.generation = store, name, None

    def upload_from_string(self, body, content_type=None, if_generation_match=None):
        if if_generation_match == 0 and self.name in self.store:
            raise PreconditionFailed()
        self.store["_gen"] = self.store.get("_gen", 0) + 1
        self.store[self.name] = (body, self.store["_gen"])
        self.generation = self.store["_gen"]

    def reload(self):
        self.generation = self.store[self.name][1]

    def exists(self):
        return self.name in self.store

    def download_as_bytes(self):
        return self.store[self.name][0].encode()

    def delete(self, if_generation_match=None):
        if self.name not in self.store:
            raise NotFound()
        if if_generation_match is not None and self.store[self.name][1] != if_generation_match:
            raise PreconditionFailed()
        del self.store[self.name]


class Client:
    def __init__(self, store: dict):
        self.store = store

    def bucket(self, name):
        return types.SimpleNamespace(blob=lambda key: Blob(self.store, key))

    def list_blobs(self, bucket, prefix="", start_offset=None, end_offset=None):
        """The log bucket's CSVs (`store["_logs"]`: name → delivery time), as `google.cloud.storage` lists them."""
        self.store.setdefault("_lists", []).append((bucket, prefix, start_offset, end_offset))
        return [
            types.SimpleNamespace(name=n, size=1, time_created=t)
            for n, t in sorted(self.store.get("_logs", {}).items())
            if n.startswith(prefix) and (start_offset is None or n >= start_offset) and (end_offset is None or n < end_offset)
        ]


@pytest.fixture
def gcs(monkeypatch):
    store: dict = {}
    exc = types.ModuleType("google.api_core.exceptions")
    exc.PreconditionFailed, exc.NotFound = PreconditionFailed, NotFound
    storage = types.ModuleType("google.cloud.storage")
    storage.Client = lambda: Client(store)
    monkeypatch.setitem(sys.modules, "google.api_core.exceptions", exc)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage)
    import google.cloud

    monkeypatch.setattr(google.cloud, "storage", storage, raising=False)
    return store


def test_a_term_mid_ingest_releases_the_lease(gcs, monkeypatch):
    def killed(*a, **kw):
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(A, "ingest_bucket", killed)
    with pytest.raises(SystemExit) as e:
        A.ingest(("b",), log_bucket="logs", data_bucket="data", stage_dir="/tmp/x", memory_limit="1GB", max_chunk_gb=1.0, workers=1)
    assert (e.value.code, "access/state/.lease" in gcs, signal.getsignal(signal.SIGTERM)) == (128 + signal.SIGTERM, False, signal.SIG_DFL)


def test_release_drops_only_our_own_lease(gcs):
    client = Client(gcs)
    ours = A.acquire_lease(client, "data", "me")
    assert (ours, A.acquire_lease(client, "data", "other")) == (1, None)
    gcs["access/state/.lease"] = (gcs["access/state/.lease"][0], 7)  # a later holder took it after ours expired
    A.release_lease(client, "data", ours)
    assert gcs["access/state/.lease"][1] == 7
    A.release_lease(client, "data", 7)
    assert "access/state/.lease" not in gcs


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 18, 12, tzinfo=UTC)
WM = "b_usage_2026_10_17_12_00_00_x_v0"


def ingest(**kw):
    return A.ingest(**{"buckets": ("b",), "log_bucket": "logs", "data_bucket": "data", "stage_dir": "/nonexistent",
                       "memory_limit": "1GB", "max_chunk_gb": 1.0, "workers": 1, "now": NOW, **kw})


def lease(gcs: dict) -> dict:
    return json.loads(gcs["access/state/.lease"][0])


def ingested(gcs: dict, *buckets: str) -> None:
    """Each bucket's state: its watermark CSV (`WM`, renamed per bucket) ingested."""
    for b in buckets:
        w = WM.replace("b_", f"{b}_", 1)
        gcs[f"access/state/{b}.json"] = (json.dumps({"watermark": w, "ingested_tail": [w]}), 1)


def test_a_free_lease_is_taken_for_its_ttl(gcs):
    before = dt.datetime.now(UTC).replace(microsecond=0)
    gen = A.acquire_lease(Client(gcs), "data", "me")
    held = lease(gcs)
    ttl = dt.datetime.fromisoformat(held["expires"]) - before
    assert (gen, held["owner"], dt.timedelta(hours=8) <= ttl <= dt.timedelta(hours=8, seconds=5)) == (1, "me", True)


def test_a_held_lease_skips_at_once(gcs):
    """The hourly poller's overlap case: a slow run holds the lease; the next poll lists nothing, leaves the lease as
    it was, and returns `held` (the CLI exits 0)."""
    A.acquire_lease(Client(gcs), "data", "slow-run")
    body = gcs["access/state/.lease"]
    assert (ingest(), gcs["access/state/.lease"], gcs.get("_lists")) == ({"lease": "held", "buckets": [], "stalled": []}, body, None)


def test_an_expired_lease_is_taken_over(gcs):
    gcs["access/state/.lease"] = (json.dumps({"owner": "crashed", "expires": "2026-10-01T00:00:00+00:00"}), 5)
    gcs["_gen"] = 5
    gen = A.acquire_lease(Client(gcs), "data", "me")
    assert (gen, lease(gcs)["owner"]) == (6, "me")


def test_nothing_pending_exits_after_a_listing(gcs, monkeypatch):
    """Nothing past the watermark: one listing from `watermark − LAG_HOURS` (finding only the watermark's own CSV,
    already ingested) and the sweep's listing below that floor; no DuckDB, the state unchanged, the lease released.
    `newest_delivery` is the watermark CSV's delivery, inside the stall window."""
    monkeypatch.setitem(sys.modules, "duckdb", None)  # an `import duckdb` raises
    ingested(gcs, "b")
    state = gcs["access/state/b.json"]
    gcs["_logs"] = {f"usage/{WM}": dt.datetime(2026, 10, 18, 1, tzinfo=UTC)}
    floor = "usage/b_usage_2026_10_17_06_00_00"
    assert ingest() == {
        "lease": "taken",
        "buckets": [{"bucket": "b", "files": 0, "bytes": 0, "chunks": 0, "rows": 0, "newest_delivery": "2026-10-18T01:00:00+00:00"}],
        "stalled": [],
    }
    assert (gcs["_lists"], gcs["access/state/b.json"], "access/state/.lease" in gcs) == (
        [("logs", "usage/b_usage_", floor, None), ("logs", "usage/b_usage_", None, floor)], state, False,
    )


def test_stalled_buckets(gcs):
    """A bucket whose newest CSV landed more than `stall_hours` ago is stalled, and so is one listing none."""
    ingested(gcs, "b", "c", "d")
    gcs["_logs"] = {
        f"usage/{WM}": NOW - dt.timedelta(hours=3),
        f"usage/{WM.replace('b_', 'c_', 1)}": NOW - dt.timedelta(hours=25),
    }
    got = ingest(buckets=("b", "c", "d"))
    assert ([(r["bucket"], r["newest_delivery"]) for r in got["buckets"]], got["stalled"]) == (
        [("b", "2026-10-18T09:00:00+00:00"), ("c", "2026-10-17T11:00:00+00:00"), ("d", None)], ["c", "d"],
    )
    assert A.stalled(got["buckets"], NOW, stall_hours=26) == ["d"]


def test_cli_reports_stalled_as_json_and_exit_3_under_x(gcs):
    from click.testing import CliRunner

    from dt_cloud.cli import main

    ingested(gcs, "b", "c")
    gcs["_logs"] = {f"usage/{WM}": dt.datetime.now(UTC) - dt.timedelta(hours=2)}
    args = ["access", "ingest", "-d", "data", "-l", "logs", "-b", "b", "-b", "c", "-j", "-s", "/nonexistent"]
    plain = CliRunner().invoke(main, args)
    alerting = CliRunner().invoke(main, [*args, "-x"])
    out = json.loads(plain.stdout)
    assert (plain.exit_code, alerting.exit_code, out["lease"], [r["bucket"] for r in out["buckets"]], out["stalled"]) == (
        0, 3, "taken", ["b", "c"], ["c"],
    )


def test_parallel_ingests_every_bucket_and_raises_the_first_failure_after(gcs, monkeypatch):
    import threading

    ran: dict[str, str] = {}

    def fake(client, b, **kw):
        ran[b] = threading.current_thread().name.split("_")[0]
        if b == "c":
            raise RuntimeError("c broke")
        return {"bucket": b, "files": 1, "bytes": 1, "chunks": 1, "rows": 1, "newest_delivery": NOW.isoformat()}

    monkeypatch.setattr(A, "ingest_bucket", fake)
    row = lambda b: {"bucket": b, "files": 1, "bytes": 1, "chunks": 1, "rows": 1, "newest_delivery": NOW.isoformat()}  # noqa: E731
    assert (ingest(buckets=("b", "d"), parallel=2), ran) == (
        {"lease": "taken", "buckets": [row("b"), row("d")], "stalled": []},
        {"b": "access-ingest", "d": "access-ingest"},
    )
    ran.clear()
    with pytest.raises(RuntimeError, match="^c broke$"):
        ingest(buckets=("b", "c", "d"), parallel=2)
    assert (sorted(ran), "access/state/.lease" in gcs) == (["b", "c", "d"], False)
