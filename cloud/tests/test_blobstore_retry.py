"""`blobstore.Store`'s transfers retry a transient failure (an R2 read stalled past fsspec's socket timeout aborted
cw's 153-scan interval-store build after ~100 GB of downloads), and never retry a missing key or a refused credential."""
from __future__ import annotations

from pathlib import Path

import pytest
from fsspec.exceptions import FSTimeoutError

from dt_cloud import blobstore
from dt_cloud.blobstore import Store


class FlakyFs:
    """`get`/`put` raise each queued error in turn, then succeed (recording the calls)."""

    def __init__(self, errors: list[BaseException]):
        self.errors = list(errors)
        self.calls: list[tuple[str, str, str]] = []

    def _go(self, op: str, a: str, b: str) -> None:
        self.calls.append((op, a, b))
        if self.errors:
            raise self.errors.pop(0)

    def get(self, rpath: str, lpath: str) -> None:
        self._go("get", rpath, lpath)
        Path(lpath).write_bytes(b"x")

    def put(self, lpath: str, rpath: str) -> None:
        self._go("put", lpath, rpath)


@pytest.fixture
def store(monkeypatch) -> Store:
    monkeypatch.setattr(blobstore, "RETRY_BASE_S", 0)
    s = Store("memory://bkt/root")
    return s


def test_get_retries_a_timeout(store: Store, tmp_path: Path):
    store.fs = FlakyFs([FSTimeoutError(), FSTimeoutError()])
    store.get("a/b.parquet", tmp_path / "b.parquet")
    assert [c[0] for c in store.fs.calls] == ["get", "get", "get"]
    assert (tmp_path / "b.parquet").read_bytes() == b"x"


def test_put_retries_a_connection_error(store: Store, tmp_path: Path):
    (tmp_path / "f").write_bytes(b"y")
    store.fs = FlakyFs([ConnectionResetError()])
    store.put(tmp_path / "f", "k")
    assert [c[0] for c in store.fs.calls] == ["put", "put"]


def test_get_gives_up_after_the_last_attempt(store: Store, tmp_path: Path):
    store.fs = FlakyFs([FSTimeoutError()] * blobstore.RETRIES)
    with pytest.raises(FSTimeoutError):
        store.get("k", tmp_path / "k")
    assert len(store.fs.calls) == blobstore.RETRIES


@pytest.mark.parametrize("e", [FileNotFoundError("k"), PermissionError("denied")])
def test_a_missing_key_or_refused_credential_is_not_retried(store: Store, tmp_path: Path, e: OSError):
    store.fs = FlakyFs([e])
    with pytest.raises(type(e)):
        store.get("k", tmp_path / "k")
    assert len(store.fs.calls) == 1
