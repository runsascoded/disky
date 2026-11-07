from re import sub

import pytest
from click.testing import CliRunner

from dt_cloud import cli
from dt_cloud.gcp import task_log_filter


@pytest.mark.parametrize("state, exit_code", [("SUCCEEDED", 0), ("FAILED", 1), ("CANCELLED", 1)])
def test_status_only_watch_never_reads_logs(monkeypatch, state, exit_code):
    monkeypatch.setattr(cli, "_resolve_job", lambda name: {"name": "projects/p/locations/r/jobs/test-job", "uid": "uid", "status": {"state": state}})
    messages = []
    monkeypatch.setattr(cli, "err", messages.append)

    def forbidden(**kwargs):
        raise AssertionError("status-only watcher must not request logging permissions")

    monkeypatch.setattr(cli.job_logs, "callback", forbidden)
    result = CliRunner().invoke(cli.main, ["job", "watch", "-L", "test-job"])
    assert result.exit_code == exit_code
    assert result.output == ""
    assert [sub(r"\d{2}:\d{2}:\d{2}", "<time>", line) for line in messages] == [
        "watching test-job (uid=uid)", f"<time> {state}",
    ]


def test_log_filter_escapes_structured_query_patterns():
    assert task_log_filter("uid", r'"query": 52,.*\.json') == (
        'labels.job_uid="uid" log_id("batch_task_logs") '
        '(textPayload=~"\\"query\\": 52,.*\\\\.json" OR jsonPayload.message=~"\\"query\\": 52,.*\\\\.json")'
    )
