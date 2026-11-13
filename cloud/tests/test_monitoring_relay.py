"""`dt-cloud monitoring-relay`: a Cloud Monitoring incident (a Pub/Sub notification channel's message) rendered for the
deployment's Slack and posted through `disk-tree notify`'s channels; a push delivery answered 204 once posted, 500 when
no channel took it (Pub/Sub redelivers)."""

from __future__ import annotations

import base64
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from click.testing import CliRunner

from disk_tree.notify import push
from dt_cloud.cli import main
from dt_cloud.monitoring_relay import duration, handle, notice_for, render, server

URL = "https://console.cloud.google.com/monitoring/alerting/incidents/0.nabc123?project=proj"
DOC = ("A run of the Cloud Run job `daily-submit` failed, so this period's Batch job may not exist. Its logs say why; "
       "re-run it once fixed (a no-op if the job exists): `gcloud run jobs execute daily-submit --region <region> --project proj`")
OPEN = {
    "version": "1.2",
    "incident": {
        "incident_id": "0.nabc123",
        "scoping_project_id": "proj",
        "scoping_project_number": 123,
        "url": URL,
        "started_at": 1794380700,  # 2026-11-11 07:05 UTC
        "ended_at": None,
        "state": "open",
        "summary": "Completed execution count for daily-submit with metric labels {result=failed} is above the threshold of 0.000 with a value of 1.000.",
        "policy_name": "daily-submit failed",
        "condition_name": "a failed execution",
        "documentation": {"content": DOC, "mime_type": "text/markdown"},
        "resource": {"type": "cloud_run_job", "labels": {"job_name": "daily-submit", "location": "us-central1", "project_id": "proj"}},
        "metric": {"type": "run.googleapis.com/job/completed_execution_count", "displayName": "Completed execution count", "labels": {"result": "failed"}},
        "threshold_value": "0",
        "observed_value": "1.000",
        "severity": "No severity",
    },
}
CLOSED = {"version": "1.2", "incident": {**OPEN["incident"], "state": "closed", "ended_at": 1794382500}}

OPEN_TEXT = [
    "*⚠️ daily-submit failed*",
    "a failed execution · opened 2026-11-11 07:05 UTC",
    "A run of the Cloud Run job `daily-submit` failed, so this period's Batch job may not exist. Its logs say why; re-run it once "
    "fixed (a no-op if the job exists): `gcloud run jobs execute daily-submit --region &lt;region&gt; --project proj`",
    f"<{URL}|incident>",
]
CLOSED_TEXT = [
    "*✅ resolved: daily-submit failed*",
    "a failed execution · open 2026-11-11 07:05 → 07:35 UTC (30m)",
    f"<{URL}|incident>",
]


def text(payload: dict) -> list[str]:
    return push.slack_text(render(payload)).split("\n")


def test_an_open_incident_renders_policy_condition_time_documentation_and_link():
    assert text(OPEN) == OPEN_TEXT
    assert render(OPEN).priority == 1


def test_a_closed_incident_renders_one_resolved_line():
    assert text(CLOSED) == CLOSED_TEXT
    assert render(CLOSED).priority == 0


def test_a_renotified_incident_says_still_open_and_markdown_links_become_slack_links():
    inc = {**OPEN["incident"], "renotify": True, "documentation": {"content": "See [the runbook](https://example.test/r?a=1&b=2) & retry."}}
    assert text({"incident": inc}) == [
        "*⚠️ daily-submit failed (still open)*",
        "a failed execution · opened 2026-11-11 07:05 UTC",
        "See <https://example.test/r?a=1&amp;b=2|the runbook> &amp; retry.",
        f"<{URL}|incident>",
    ]


def test_a_closed_incident_spanning_days_dates_its_end():
    inc = {**CLOSED["incident"], "ended_at": 1794380700 + 2 * 86400 + 3600}
    assert text({"incident": inc})[1] == "a failed execution · open 2026-11-11 07:05 → 2026-11-13 08:05 UTC (2d1h)"


def test_durations():
    assert [duration(s) for s in (0, 45, 60, 1800, 3600, 7500, 86400, 3 * 86400 + 4 * 3600 + 59)] == ["0s", "45s", "1m", "30m", "1h00m", "2h05m", "1d0h", "3d4h"]


def test_a_body_that_is_not_a_notification_is_posted_raw():
    assert [push.slack_text(notice_for(b)).split("\n") for b in (b'{"incident": {"state": "open"}}', b"<html>", b"[1]")] == [
        ["*⚠️ Monitoring notification (unrecognized)*", "incident without a policy name or a known state (state='open')", '```{"incident": {"state": "open"}}```'],
        ["*⚠️ Monitoring notification (unrecognized)*", "not JSON: Expecting value: line 1 column 1 (char 0)", "```&lt;html&gt;```"],
        ["*⚠️ Monitoring notification (unrecognized)*", "not a JSON object", "```[1]```"],
    ]


def envelope(payload: dict) -> bytes:
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return json.dumps({"message": {"data": data, "messageId": "42"}, "subscription": "projects/proj/subscriptions/relay"}).encode()


class Sender:
    def __init__(self, results: list[tuple[str, str | None]]):
        self.results = results
        self.sent: list[push.Notice] = []

    def __call__(self, n: push.Notice) -> list[tuple[str, str | None]]:
        self.sent.append(n)
        return self.results


def test_a_delivery_posted_is_acked():
    s = Sender([("slack", None)])
    assert handle(envelope(OPEN), s) == 204
    assert [push.slack_text(n).split("\n") for n in s.sent] == [OPEN_TEXT]


def test_a_delivery_no_channel_took_is_redelivered():
    assert [handle(envelope(CLOSED), Sender(r)) for r in ([("slack", "slack: HTTP 200: {\"ok\":false}")], [])] == [500, 500]


def test_a_body_that_is_no_push_envelope_is_refused_unposted():
    s = Sender([("slack", None)])
    assert [handle(b, s) for b in (b"{}", b"nope", b'{"message": {"data": "%%%"}}')] == [400, 400, 400]
    assert s.sent == []


def test_the_server_answers_a_push_with_handles_status():
    s = Sender([("slack", None)])
    srv = server(0, s, host="127.0.0.1")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    try:
        with urlopen(Request(url, data=envelope(CLOSED), method="POST", headers={"Content-Type": "application/json"}), timeout=10) as r:
            ok = r.status
        try:
            urlopen(Request(url, data=b"nope", method="POST"), timeout=10)
            bad = None
        except HTTPError as e:
            bad = e.code
    finally:
        srv.shutdown()
        srv.server_close()
    assert (ok, bad, [push.slack_text(n).split("\n") for n in s.sent]) == (204, 400, [CLOSED_TEXT])


def test_render_prints_the_slack_text(tmp_path):
    f = tmp_path / "closed.json"
    f.write_text(json.dumps(CLOSED))
    res = CliRunner().invoke(main, ["monitoring-relay", "render", str(f)], catch_exceptions=False)
    assert (res.exit_code, res.output.split("\n")) == (0, [*CLOSED_TEXT, ""])
