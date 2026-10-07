import json
import sqlite3
from pathlib import Path

import pytest
from requests import Response
from requests.exceptions import ConnectionError, HTTPError

from dt_cloud.sweep_exec import execution_progress_message, log_execution_progress
from dt_cloud.sweep_progress import log_sample, retry_read, sample, write_samples


def snapshot() -> dict:
    return {"bucket": "b", "mode": "deleted", "roots_done": 20, "roots": 1000, "decisions": {"delete": 2000, "skipped_gone": 2, "skipped_overwritten": 3, "delete_failed": 0}, "delete_bytes": 2345678901234, "updated": "2026-10-05T10:00:00Z", "done": False}


def test_structured_and_legacy_logs_have_explicit_precision(capsys) -> None:
    snap = snapshot()
    expected = {"bucket": "b", "ts": 1791194400.0, "deletes": 2000, "bytes": 2345678901234, "gone": 2, "overwritten": 3, "failed": 0, "done": 0, "bytes_exact": 1}
    assert sample(snap) == expected
    log_execution_progress(snap)
    captured = capsys.readouterr()
    assert captured.out == ""
    event = json.loads(captured.err)
    assert event == {"severity": "INFO", "event": "sweep_progress", "message": execution_progress_message(snap), "progress": snap}
    assert log_sample({"jsonPayload": event}) == expected
    assert log_sample({"textPayload": captured.err}) == expected
    assert log_sample({"textPayload": execution_progress_message(snap), "timestamp": snap["updated"]}) == {**expected, "bytes": 2350000000000, "bytes_exact": 0}
    assert log_sample({"textPayload": "ordinary startup"}) is None


def test_failed_progress_is_error(capsys) -> None:
    snap = snapshot()
    snap["decisions"]["delete_failed"] = 1
    log_execution_progress(snap)
    assert json.loads(capsys.readouterr().err) == {"severity": "ERROR", "event": "sweep_progress", "message": execution_progress_message(snap), "progress": snap}


def test_d1_history_insert_is_idempotent_and_quotes_ids(monkeypatch) -> None:
    from dt_cloud import index_footer

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE deletion_runs(run_id TEXT PRIMARY KEY)")
    sql = (Path(__file__).parents[2] / "site/migrations/cw/0011_run_progress.sql").read_text()
    db.executescript(sql)
    monkeypatch.setattr(index_footer, "_creds", lambda: ("token", "account"))
    monkeypatch.setattr("dt_cloud.sweep_progress._creds", lambda: ("token", "account"))
    monkeypatch.setattr("dt_cloud.sweep_progress._d1_query", lambda sql, *_: db.executescript(sql))
    row = sample(snapshot())
    write_samples("r'1", [row, row])
    assert db.execute("SELECT * FROM deletion_progress").fetchall() == [("r'1", "b", 1791194400.0, 2000, 2345678901234, 2, 3, 0, 0, 1)]


def test_monitoring_read_retries_transient_failures_only(monkeypatch, capsys) -> None:
    sleeps = []
    monkeypatch.setattr("dt_cloud.sweep_progress.time.sleep", sleeps.append)
    results = iter([ConnectionError("reset"), HTTPError(response=response(429)), {"ok": True}])

    def operation() -> dict:
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    assert retry_read(operation) == {"ok": True}
    assert sleeps == [1, 2]
    assert capsys.readouterr().err.splitlines() == [
        "progress sync: transient ConnectionError; retry 1/6 in 1s",
        "progress sync: transient HTTPError; retry 2/6 in 2s",
    ]
    with pytest.raises(HTTPError) as denied:
        retry_read(lambda: response(403).raise_for_status())
    assert denied.value.response.status_code == 403
    assert sleeps == [1, 2]


def response(status: int) -> Response:
    result = Response()
    result.status_code = status
    return result


def test_monitoring_retry_limit_is_bounded(monkeypatch, capsys) -> None:
    sleeps = []
    monkeypatch.setattr("dt_cloud.sweep_progress.time.sleep", sleeps.append)

    def fail() -> None:
        raise ConnectionError("reset")

    with pytest.raises(ConnectionError, match="^reset$"):
        retry_read(fail)
    assert sleeps == [1, 2, 4, 8, 16, 30]
    assert capsys.readouterr().err.splitlines() == [f"progress sync: transient ConnectionError; retry {i}/6 in {delay}s" for i, delay in enumerate(sleeps, 1)]
