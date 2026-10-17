"""The access ingest's single-writer lease (`access/state/.lease`): released on a TERM mid-run, and only ever our own."""
from __future__ import annotations

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
