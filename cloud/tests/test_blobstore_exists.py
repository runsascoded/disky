"""`blobstore.Store.exists` answers only what it knows: a key there, a key not there — and raises when the store
refuses to say (a credential without access). fsspec's `isfile` swallows every error into `False`, so a job deciding
`build` vs `add` by `exists` read a 403 as "no generation yet", chose `build`, and was refused (r2.oa.dev)."""
from __future__ import annotations

import pytest

from dt_cloud.blobstore import Store


class InfoFs:
    """`info` answers from `entries`, or raises `error` for every key."""

    def __init__(self, entries: dict[str, str] | None = None, error: BaseException | None = None):
        self.entries, self.error = entries or {}, error

    def invalidate_cache(self, path: str | None = None) -> None:
        pass

    def info(self, path: str) -> dict:
        if self.error is not None:
            raise self.error
        if path not in self.entries:
            raise FileNotFoundError(path)
        return {"name": path, "type": self.entries[path]}

    def isfile(self, path: str) -> bool:
        try:
            return self.info(path)["type"] == "file"
        except Exception:
            return False


def store(fs: InfoFs) -> Store:
    s = Store("memory://bkt/root")
    s.fs = fs
    return s


def test_exists_answers_present_and_absent():
    s = store(InfoFs())
    s.fs.entries = {s._p("g/scans.json"): "file", s._p("g"): "directory"}
    assert [s.exists("g/scans.json"), s.exists("g/missing.json"), s.exists("g")] == [True, False, False]


@pytest.mark.parametrize("error", [PermissionError("403 AccessDenied"), OSError("connection reset")])
def test_exists_raises_when_the_store_refuses_to_say(error: BaseException):
    s = store(InfoFs(error=error))
    with pytest.raises(type(error)) as got:
        s.exists("g/scans.json")
    assert got.value is error


def test_a_local_store(tmp_path):
    (tmp_path / "g").mkdir()
    (tmp_path / "g" / "scans.json").write_text("{}")
    s = Store(str(tmp_path))
    assert [s.exists("g/scans.json"), s.exists("g/missing.json"), s.exists("g")] == [True, False, False]
