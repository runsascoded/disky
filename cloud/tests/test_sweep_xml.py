"""Exact identity, response attribution and retry semantics for XML deletes."""

from xml.etree import ElementTree as ET

import pytest
from requests import Response
from requests.exceptions import ConnectionError, HTTPError

from dt_cloud.sweep_xml import XmlDeleter, delete_payload, delete_results
from dt_cloud.sweep_xml import retry_after_seconds
from test_sweep_exec import FakeBlob, T0


def blobs():
    return [FakeBlob("a/é &%.txt", 1, 11, T0), FakeBlob("b/x", 2, 22, T0)]


def reply(status: int, content: str) -> Response:
    response = Response()
    response.status_code = status
    response._content = content.encode()
    return response


class Http:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def post(self, url, data, headers, timeout):
        self.requests.append((url, data, headers, timeout))
        response = self.replies.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_payload_preserves_xml_escaped_names_and_exact_generations():
    root = ET.fromstring(delete_payload(blobs()))
    assert [(item.findtext("Key"), item.findtext("VersionId")) for item in root.findall("Object")] == [
        ("a/é &%.txt", "11"), ("b/x", "22"),
    ]
    assert root.findtext("Quiet") == "False"


def test_percent_encoded_looking_key_is_a_literal_name():
    item = FakeBlob("a/literal%2Fname", 1, 11, T0)
    assert ET.fromstring(delete_payload([item])).findtext("Object/Key") == "a/literal%2Fname"
    assert delete_results(b'<DeleteResult><Deleted><Key>a/literal%2Fname</Key><VersionId>11</VersionId></Deleted></DeleteResult>', [item]) == {("a/literal%2Fname", 11): "delete"}


@pytest.mark.parametrize("items,message", [
    ([], "XML delete needs 1..1000 objects"),
    ([FakeBlob("x", 1, 0, T0)], "XML delete requires a positive generation for every object"),
    ([FakeBlob("x", 1, 1, T0)] * 2, "XML delete identities must be unique"),
    ([FakeBlob(str(i), 1, i + 1, T0) for i in range(1001)], "XML delete needs 1..1000 objects"),
])
def test_invalid_request_refused_before_http(items, message):
    http = Http([])
    with pytest.raises(ValueError) as error:
        XmlDeleter(http=http)(None, "bucket", items)
    assert str(error.value) == message
    assert http.requests == []


def test_partial_transient_retries_only_unsettled_identity(monkeypatch):
    import dt_cloud.sweep_xml as module
    monkeypatch.setattr(module, "_sleep", lambda _: None)
    http = Http([
        reply(200, '<DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Deleted><Key>a/é &amp;%.txt</Key><VersionId>11</VersionId></Deleted><Error><Key>b/x</Key><VersionId>22</VersionId><Code>SlowDown</Code></Error></DeleteResult>'),
        reply(200, '<DeleteResult><Error><Key>b/x</Key><VersionId>22</VersionId><Code>NoSuchVersion</Code></Error></DeleteResult>'),
    ])
    result = XmlDeleter(http=http)(None, "bucket", blobs())
    assert [(blob.name, decision) for blob, decision in result] == [("a/é &%.txt", "delete"), ("b/x", "skipped_gone")]
    assert [[(item.findtext("Key"), item.findtext("VersionId")) for item in ET.fromstring(request[1]).findall("Object")] for request in http.requests] == [
        [("a/é &%.txt", "11"), ("b/x", "22")], [("b/x", "22")],
    ]
    assert [(request[0], request[2]["Content-Type"], request[3]) for request in http.requests] == [
        ("https://storage.googleapis.com/bucket?delete", "application/xml", (15, 120)),
        ("https://storage.googleapis.com/bucket?delete", "application/xml", (15, 120)),
    ]


def test_lost_or_incomplete_reply_retries_exact_versions(monkeypatch):
    import dt_cloud.sweep_xml as module
    monkeypatch.setattr(module, "_sleep", lambda _: None)
    complete = '<DeleteResult><Deleted><Key>a/é &amp;%.txt</Key><VersionId>11</VersionId></Deleted><Deleted><Key>b/x</Key><VersionId>22</VersionId></Deleted></DeleteResult>'
    http = Http([ConnectionError("lost"), reply(503, "unavailable"), reply(200, "<DeleteResult/>"), reply(200, complete)])
    assert [(blob.name, decision) for blob, decision in XmlDeleter(http=http)(None, "bucket", blobs())] == [("a/é &%.txt", "delete"), ("b/x", "delete")]
    assert [request[1] for request in http.requests] == [delete_payload(blobs())] * 4


def test_exhausted_retries_are_failed_and_http_denial_is_fatal(monkeypatch):
    import dt_cloud.sweep_xml as module
    monkeypatch.setattr(module, "_sleep", lambda _: None)
    http = Http([reply(503, "unavailable") for _ in range(module.DELETE_ATTEMPTS)])
    assert [(blob.name, decision) for blob, decision in XmlDeleter(http=http)(None, "bucket", blobs())] == [("a/é &%.txt", "delete_failed"), ("b/x", "delete_failed")]
    assert len(http.requests) == module.DELETE_ATTEMPTS
    with pytest.raises(HTTPError):
        XmlDeleter(http=Http([reply(403, "denied")]))(None, "bucket", blobs())


def test_unknown_identity_and_permanent_item_error_are_not_success():
    with pytest.raises(ValueError, match="^XML delete response has an unknown or duplicate identity$"):
        delete_results(b'<DeleteResult><Deleted><Key>other</Key><VersionId>11</VersionId></Deleted></DeleteResult>', blobs())
    assert delete_results('<DeleteResult><Deleted><Key>a/é &amp;%.txt</Key><VersionId>11</VersionId></Deleted><Error><Key>b/x</Key><VersionId>22</VersionId><Code>AccessDenied</Code><Message>denied</Message></Error></DeleteResult>'.encode(), blobs()) == {("a/é &%.txt", 11): "delete", ("b/x", 22): "delete_failed"}


def test_each_http_attempt_is_paced_and_reports_partial_retry_backpressure(monkeypatch):
    monkeypatch.setattr("dt_cloud.sweep_xml._sleep", lambda _: None)
    attempts = []
    admitted = []
    http = Http([
        reply(200, '<DeleteResult><Deleted><Key>a/é &amp;%.txt</Key><VersionId>11</VersionId></Deleted><Error><Key>b/x</Key><VersionId>22</VersionId><Code>SlowDown</Code></Error></DeleteResult>'),
        reply(200, '<DeleteResult><Deleted><Key>b/x</Key><VersionId>22</VersionId></Deleted></DeleteResult>'),
    ])
    result = XmlDeleter(http=http, before_attempt=admitted.append, on_attempt=attempts.append)(None, "b", blobs())
    assert admitted == [2, 1]
    assert [(a.objects, a.outcome, a.reason, a.retry_after) for a in attempts] == [(2, "backpressure", "xml-transient", 0), (1, "ok", "", 0)]
    assert [(b.name, d) for b, d in result] == [("a/é &%.txt", "delete"), ("b/x", "delete")]


def test_http_retry_after_is_observed_and_honored(monkeypatch):
    sleeps = []
    attempts = []
    monkeypatch.setattr("dt_cloud.sweep_xml._sleep", sleeps.append)
    limited = reply(429, "limited")
    limited.headers["Retry-After"] = "120"
    http = Http([limited, reply(200, '<DeleteResult><Deleted><Key>a/é &amp;%.txt</Key><VersionId>11</VersionId></Deleted><Deleted><Key>b/x</Key><VersionId>22</VersionId></Deleted></DeleteResult>')])
    result = XmlDeleter(http=http, on_attempt=attempts.append)(None, "b", blobs())
    assert sleeps == [120]
    assert [(a.objects, a.outcome, a.reason, a.retry_after) for a in attempts] == [(2, "backpressure", "http-429", 120), (2, "ok", "", 0)]
    assert [d for _, d in result] == ["delete", "delete"]


def test_fatal_retry_preserves_already_acknowledged_generations(monkeypatch):
    monkeypatch.setattr("dt_cloud.sweep_xml._sleep", lambda _: None)
    attempts = []
    http = Http([
        reply(200, '<DeleteResult><Deleted><Key>a/é &amp;%.txt</Key><VersionId>11</VersionId></Deleted><Error><Key>b/x</Key><VersionId>22</VersionId><Code>SlowDown</Code></Error></DeleteResult>'),
        reply(403, "denied"),
    ])
    result = XmlDeleter(http=http, on_attempt=attempts.append)(None, "b", blobs())
    assert [(b.name, b.generation, d) for b, d in result] == [("a/é &%.txt", 11, "delete"), ("b/x", 22, "delete_failed")]
    assert [(a.objects, a.outcome, a.reason) for a in attempts] == [(2, "backpressure", "xml-transient"), (1, "fatal", "http-403")]
    assert [request[1] for request in http.requests] == [delete_payload(blobs()), delete_payload(blobs()[1:])]


def test_retry_after_date_and_invalid_values(monkeypatch):
    monkeypatch.setattr("dt_cloud.sweep_xml.time.time", lambda: 60)
    assert [retry_after_seconds(v) for v in (None, "nonsense", "nan", "-1", "2.5", "Thu, 01 Jan 1970 00:02:00 GMT")] == [0, 0, 0, 0, 2.5, 60]
