"""Exercise archive retries through the real Storage SDK with a fake HTTP transport."""
from __future__ import annotations

import json
from unittest.mock import Mock, call
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
from google.api_core.exceptions import Forbidden, NotFound, PreconditionFailed
from google.auth.credentials import AnonymousCredentials
from google.cloud.storage import Client
from google.cloud.storage import retry as storage_retry
from requests import Request, Response

from dt_cloud.access import sweep_ingested


SOURCE = "usage/bucket_usage_2026_11_08_11_00_00_source_v0"
DESTINATION = "ingested/bucket_usage_2026_11_08_11_00_00_source_v0"
WATERMARK = "bucket_usage_2026_11_08_18_00_00_wm_v0"
COPY = (
    "POST",
    f"/storage/v1/b/logs/o/{SOURCE}/copyTo/b/logs/o/{DESTINATION}",
    {"sourceGeneration": "7", "prettyPrint": "false"},
)
DELETE = (
    "DELETE",
    f"/storage/v1/b/logs/o/{SOURCE}",
    {"generation": "7", "ifGenerationMatch": "7", "prettyPrint": "false"},
)


class Transport:
    is_mtls = False

    def __init__(self, statuses: list[int]):
        self.statuses = list(statuses)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        parsed = urlsplit(url)
        self.calls.append((method, unquote(parsed.path), dict(parse_qsl(parsed.query))))
        status = self.statuses.pop(0)
        response = Response()
        response.status_code = status
        response.url = url
        response.request = Request(method, url).prepare()
        response.headers["content-type"] = "application/json"
        body = (
            {"error": {"code": status, "message": "fixture failure"}}
            if status >= 400
            else {"name": DESTINATION, "bucket": "logs", "generation": "9"}
        )
        response._content = b"" if status == 204 else json.dumps(body).encode()
        return response


def storage(
    monkeypatch: pytest.MonkeyPatch,
    statuses: list[int],
    generation: int | None = 7,
) -> tuple[Client, Transport]:
    # Tracing's asynchronous bucket-location lookup is unrelated to archive I/O.
    monkeypatch.setenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "true")
    # Keep retry behavior real, with negligible delay and a short test deadline.
    monkeypatch.setattr(
        storage_retry,
        "DEFAULT_RETRY",
        storage_retry.DEFAULT_RETRY.with_delay(initial=0.001, maximum=0.001).with_timeout(1),
    )
    transport = Transport(statuses)
    client = Client(project="fixture", credentials=AnonymousCredentials(), _http=transport)
    blob = client.bucket("logs").blob(SOURCE, generation=generation)
    monkeypatch.setattr(client, "list_blobs", Mock(return_value=iter([blob])))
    return client, transport


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([503, 200, 204], [COPY, COPY, DELETE]),
        ([200, 503, 204], [COPY, DELETE, DELETE]),
        ([503, 200, 503, 204], [COPY, COPY, DELETE, DELETE]),
    ],
)
def test_transient_archive_errors_retry_the_same_generation(
    monkeypatch: pytest.MonkeyPatch,
    statuses: list[int],
    expected: list[tuple[str, str, dict[str, str]]],
) -> None:
    client, transport = storage(monkeypatch, statuses)

    assert sweep_ingested(client, "logs", "bucket", WATERMARK, workers=1) == 1
    assert transport.calls == expected
    assert transport.statuses == []
    assert client.list_blobs.call_args_list == [
        call("logs", prefix="usage/bucket_usage_", end_offset="usage/bucket_usage_2026_11_08_12_00_00"),
    ]


@pytest.mark.parametrize(("status", "error"), [(403, Forbidden), (404, NotFound), (412, PreconditionFailed)])
def test_permanent_copy_failure_preserves_the_source(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    error: type[Exception],
) -> None:
    client, transport = storage(monkeypatch, [status])

    with pytest.raises(error):
        sweep_ingested(client, "logs", "bucket", WATERMARK, workers=1)

    assert transport.calls == [COPY]
    assert transport.statuses == []


def test_archive_requires_a_listed_source_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    client, transport = storage(monkeypatch, [], generation=None)

    with pytest.raises(ValueError) as raised:
        sweep_ingested(client, "logs", "bucket", WATERMARK, workers=1)

    assert raised.value.args == (f"usage log has no listed generation: {SOURCE}",)
    assert transport.calls == []


def test_changed_source_generation_is_not_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    client, transport = storage(monkeypatch, [200, 412])

    with pytest.raises(PreconditionFailed):
        sweep_ingested(client, "logs", "bucket", WATERMARK, workers=1)

    assert transport.calls == [COPY, DELETE]
    assert transport.statuses == []


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [([200, 404], [COPY, DELETE]), ([200, 503, 404], [COPY, DELETE, DELETE])],
)
def test_already_deleted_source_generation_completes_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    statuses: list[int],
    expected: list[tuple[str, str, dict[str, str]]],
) -> None:
    client, transport = storage(monkeypatch, statuses)

    assert sweep_ingested(client, "logs", "bucket", WATERMARK, workers=1) == 1
    assert transport.calls == expected
    assert transport.statuses == []
