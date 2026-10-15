"""`dispatch --serve` (the laptop drainer) rides out a D1 outage: transient errors back off and retry, others exit."""
from __future__ import annotations

import errno
import re
from email.message import Message
from socket import gaierror
from urllib.error import HTTPError, URLError

import pytest

from disk_tree import drain
from disk_tree.cli import staged
from disk_tree.d1 import D1Client, transient


def http(code: int) -> HTTPError:
    return HTTPError("https://api.cloudflare.com/client/v4/…/query", code, "x", Message(), None)


@pytest.mark.parametrize("e, want", [
    (http(504), True),
    (http(500), True),
    (http(429), True),
    (http(403), False),
    (http(400), False),
    (URLError(gaierror(8, "nodename nor servname provided, or not known")), True),
    (TimeoutError(60, "Operation timed out"), True),
    (ConnectionResetError(54, "Connection reset by peer"), True),
    (OSError(errno.EHOSTUNREACH, "No route to host"), True),
    (PermissionError(1, "Operation not permitted"), False),
    (ValueError("bad"), False),
])
def test_transient(e, want):
    assert transient(e) is want


def serve(monkeypatch, outcomes: list, once: bool = False) -> tuple[list[float], list[str]]:
    """Run `_serve` against a fake D1 whose polls yield `outcomes` in turn (an exception is raised); returns the
    sleeps and the drainer's log lines (its run summaries aside)."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setattr(staged, "_delete_cfg", lambda _: {})
    monkeypatch.setattr(drain.Schema, "detect", classmethod(lambda cls, c: "schema"))
    left = iter(outcomes)

    def drain_once(c: D1Client, **_) -> list:
        o = next(left)
        if isinstance(o, BaseException):
            raise o
        return o
    monkeypatch.setattr(drain, "drain_once", drain_once)
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    logs: list[str] = []
    monkeypatch.setattr(staged, "err", logs.append)
    staged._serve(None, 30, once, d1_ids=["db"])
    return sleeps, [re.sub(r"^dispatch --serve: draining D1 .*", "<start>", line) for line in logs]


def test_serve_backs_off_through_an_outage(monkeypatch):
    offline = URLError(gaierror(8, "nodename nor servname provided, or not known"))
    sleeps, logs = serve(monkeypatch, [[], offline, http(504), [], KeyboardInterrupt()])
    assert sleeps == [60, 60, 120, 90]
    assert logs == [
        "<start>",
        "dispatch --serve: D1 unreachable (URLError: <urlopen error [Errno 8] nodename nor servname provided, or not known>); retrying in 60s",
        "dispatch --serve: D1 unreachable (HTTPError: HTTP Error 504: x); retrying in 120s",
        "dispatch --serve: D1 reachable again after 2 failed poll(s)",
        "dispatch --serve: stopped",
    ]


def test_serve_exits_on_a_lasting_error(monkeypatch):
    with pytest.raises(PermissionError):
        serve(monkeypatch, [[], PermissionError(1, "Operation not permitted")])


def test_serve_once_reports_a_transient_error(monkeypatch):
    with pytest.raises(HTTPError):
        serve(monkeypatch, [http(504)], once=True)
