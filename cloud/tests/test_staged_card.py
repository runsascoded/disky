"""`dt-cloud staged-card`: the job's post-scan ping of `POST /api/plans/staged/card`."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from dt_cloud import cli, site

SITE = "https://site.test"
ENDPOINT = f"{SITE}/api/plans/staged/card"


@pytest.fixture
def card(monkeypatch):
    """The site's answer to the ping (`card.answer`: its JSON body, or a `SiteError` to raise); `card.calls`, `card.logged`."""
    state = SimpleNamespace(answer={"refreshed": True}, calls=[], logged=[])

    def post_json(url: str, token: str, path: str, body: dict, timeout: int = 120) -> bytes:
        state.calls.append((url, token, path, body))
        if isinstance(state.answer, site.SiteError):
            raise state.answer
        return json.dumps(state.answer).encode()

    monkeypatch.setattr(site, "post_json", post_json)
    monkeypatch.setattr(cli, "err", state.logged.append)
    monkeypatch.setenv("SITE_URL", SITE)
    monkeypatch.setenv("SITE_TOKEN", "t")
    return state


def run(*args: str) -> tuple[int, str | None]:
    result = CliRunner().invoke(cli.main, ["staged-card", *args])
    return result.exit_code, None if result.exception is None else str(result.exception)


@pytest.mark.parametrize("refreshed,said", [(True, "staged-card: edited"), (False, "staged-card: nothing to edit")])
def test_says_whether_the_card_was_edited(card, refreshed: bool, said: str) -> None:
    card.answer = {"refreshed": refreshed}
    assert run() == (0, None)
    assert card.calls == [(SITE, "t", "/api/plans/staged/card", {})]
    assert card.logged == [said]


def test_flags_override_the_env(card) -> None:
    assert run("-t", "other", "-u", "https://dev.site.test") == (0, None)
    assert card.calls == [("https://dev.site.test", "other", "/api/plans/staged/card", {})]
    assert card.logged == ["staged-card: edited"]


@pytest.mark.parametrize("error", [
    site.SiteError(f"{ENDPOINT} → HTTP 403: forbidden", status=403),
    site.SiteError(f"{ENDPOINT} → HTTP 404: no staged-card grant on this deployment", status=404),
    site.SiteError(f"{ENDPOINT} unreachable: timed out"),
])
def test_a_refusal_or_an_unreachable_site_exits_1_with_its_reason(card, error: site.SiteError) -> None:
    card.answer = error
    assert run() == (1, f"staged-card: {error}")
    assert card.calls == [(SITE, "t", "/api/plans/staged/card", {})]
    assert card.logged == []


def test_no_token_exits_1_before_any_call(card, monkeypatch) -> None:
    monkeypatch.delenv("SITE_TOKEN")
    monkeypatch.delenv("GCS_USAGE_TOKEN", raising=False)  # (`site_token`'s deprecated fallback)
    assert run() == (1, "staged-card: no token (-t or $SITE_TOKEN)")
    assert card.calls == []
    assert card.logged == []
