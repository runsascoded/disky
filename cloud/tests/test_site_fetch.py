import io
from http.client import IncompleteRead

import pytest

from dt_cloud import site


class Truncated(io.BytesIO):
    """A response whose body ends short of its Content-Length (a Worker isolate cut off mid-response)."""

    def read(self, *a):
        raise IncompleteRead(b"\x89PN", 9)


def responses(monkeypatch, *bodies):
    calls = []

    def urlopen(req, timeout):
        calls.append((req.get_method(), req.full_url))
        return bodies[len(calls) - 1]

    monkeypatch.setattr(site, "urlopen", urlopen)
    return calls


def test_a_truncated_body_is_fetched_again(monkeypatch):
    # cw's 2026-10-23T0001 digest died on `/api/digest-image`'s 64,770 of 230,883 bytes
    calls = responses(monkeypatch, Truncated(), io.BytesIO(b"\x89PNG-whole"))
    assert site.post_json("https://site.test/", "t", "/api/digest-image", {"m": 1}) == b"\x89PNG-whole"
    assert calls == [("POST", "https://site.test/api/digest-image")] * 2


def test_a_body_truncated_every_time_still_fails(monkeypatch):
    calls = responses(monkeypatch, *[Truncated() for _ in range(site.TRUNCATED_RETRIES + 1)])
    with pytest.raises(site.SiteError, match=r"^https://site\.test/api/x → truncated body \(3 of 12 bytes\), 3 tries$"):
        site.get_json("https://site.test", "t", "/api/x")
    assert len(calls) == site.TRUNCATED_RETRIES + 1 == 3
