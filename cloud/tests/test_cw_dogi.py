from pathlib import Path
from runpy import run_path
from struct import pack

from click import ClickException
import pytest
from requests import ConnectionError, Response, Session

MODULE = run_path(str(Path(__file__).resolve().parents[2] / "job/cw-dogi.py"))


def test_metadata_has_exact_og_and_twitter_fields() -> None:
    assert MODULE["metadata"]('<meta property="og:title" content="CW &amp; storage"><meta name="twitter:title" content="CW"><meta name="viewport" content="ignored">') == {
        "og:title": "CW & storage", "twitter:title": "CW",
    }


def test_duplicate_metadata_is_rejected() -> None:
    with pytest.raises(ClickException, match="^Duplicate metadata field: og:title$"):
        MODULE["metadata"]('<meta property="og:title" content="CW"><meta property="og:title" content="different">')


@pytest.mark.parametrize("tier", ["anon", "full"])
def test_png_requires_exact_dimensions_tier_and_privacy_headers(tier: str) -> None:
    response = Response()
    response.status_code = 200
    response.headers.update({"content-type": "image/png", "x-og-tier": tier,
                             "x-og-policy": "generic", "cache-control": "private, no-store"})
    response._content = b"\x89PNG\r\n\x1a\n" + pack(">I", 13) + b"IHDR" + pack(">II", 1200, 630)
    assert MODULE["png"](response, tier) == response.content
    response.headers["x-og-tier"] = "other"
    with pytest.raises(ClickException, match="^Unexpected card disclosure tier$"):
        MODULE["png"](response, tier)


def test_transport_failures_do_not_print_preview_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise ConnectionError("https://cw.example/?og=temporary-private-token")

    monkeypatch.setattr(Session, "request", fail)
    with pytest.raises(ClickException) as error:
        MODULE["SafeSession"]().get("https://cw.example/?og=temporary-private-token")
    assert str(error.value) == "CW verification transport failed; credential URLs are not logged"
