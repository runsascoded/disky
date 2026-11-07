"""Upload backpressure is bounded and never silently discards undo records."""

import threading
from concurrent.futures import ThreadPoolExecutor

import fsspec
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.sweep_journal import DecisionJournal


def test_blocked_uploader_bounds_spooled_parts(tmp_path, monkeypatch):
    fs = fsspec.filesystem("file")
    blocked = threading.Event()
    release = threading.Event()
    upload = fs.put_file

    def put_file(local, remote):
        blocked.set()
        assert release.wait(timeout=5) is True
        upload(local, remote)

    monkeypatch.setattr(fs, "put_file", put_file)
    journal = DecisionJournal(fs, str(tmp_path / "remote"), tmp_path / "local", pa.schema([("id", pa.int64())]), threading.Event(), rows=2, workers=1, window=2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(journal.append, [(i,) for i in range(6)])
        try:
            assert blocked.wait(timeout=5) is True
            # One upload can run and one can queue, but no third spool part.
            with pytest.raises(TimeoutError):
                future.result(timeout=.1)
            assert sorted(path.name for path in (tmp_path / "local").iterdir()) == ["part-00000.parquet", "part-00001.parquet"]
        finally:
            release.set()
        future.result(timeout=5)
    journal.close()
    assert pq.read_table(tmp_path / "remote").to_pylist() == [{"id": i} for i in range(6)]
    assert {key: journal.metrics()[key] for key in ("parts_spooled", "parts_uploaded", "rows_uploaded")} == {"parts_spooled": 3, "parts_uploaded": 3, "rows_uploaded": 6}


def test_upload_failure_stops_deletes_and_retains_complete_local_parts(tmp_path, monkeypatch):
    fs = fsspec.filesystem("file")
    stop = threading.Event()

    def put_file(local, remote):
        raise OSError("upload unavailable")

    monkeypatch.setattr(fs, "put_file", put_file)
    journal = DecisionJournal(fs, str(tmp_path / "remote"), tmp_path / "local", pa.schema([("id", pa.int64())]), stop, rows=2)
    journal.append([(i,) for i in range(5)])
    with pytest.raises(RuntimeError) as error:
        journal.close()
    assert str(error.value) == f"decision upload failed; complete local parts retained at {tmp_path / 'local'}"
    assert stop.is_set() is True
    assert pq.read_table(tmp_path / "local").to_pylist() == [{"id": i} for i in range(5)]
    assert {key: journal.metrics()[key] for key in ("parts_spooled", "parts_uploaded", "rows_uploaded")} == {"parts_spooled": 3, "parts_uploaded": 0, "rows_uploaded": 0}
