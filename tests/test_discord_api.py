"""Specs for the stdlib Discord REST helpers (``disk_tree.notify.discord_api``).

No network: ``urllib.request.urlopen`` is stubbed to capture the outgoing
``Request`` and return a canned JSON body, so each test asserts the exact method
/ URL / headers / body the helper builds and the value it parses back."""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from disk_tree.notify import discord_api as da


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


@pytest.fixture
def capture(monkeypatch):
    """Stub ``urlopen``; record each ``Request``, reply with queued JSON."""
    sent: list[urllib.request.Request] = []
    replies: list[object] = []

    def fake_urlopen(req, timeout=None):
        sent.append(req)
        return FakeResp(json.dumps(replies.pop(0)).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    def req_of(r: urllib.request.Request) -> dict:
        return {
            "method": r.get_method(),
            "url": r.full_url,
            "headers": dict(r.header_items()),
            "body": r.data.decode() if r.data is not None else None,
        }

    return sent, replies, req_of


def test_webhook_info_unauthenticated_get(capture):
    sent, replies, req_of = capture
    replies.append({"channel_id": "42", "name": "hook"})
    out = da.webhook_info("https://discord.com/api/webhooks/9/secrettoken")
    assert out == {"channel_id": "42", "name": "hook"}
    assert req_of(sent[0]) == {
        "method": "GET",
        "url": "https://discord.com/api/webhooks/9/secrettoken",
        "headers": {"User-agent": da.DEFAULT_USER_AGENT},
        "body": None,
    }


def test_app_emojis_bot_auth_and_name_id_map(capture):
    sent, replies, req_of = capture
    replies.append({"items": [{"name": "av_deg10", "id": "111"}, {"name": "av_deg20", "id": "222"}]})
    out = da.app_emojis("tok", app="APP")
    assert out == {"av_deg10": "111", "av_deg20": "222"}
    assert req_of(sent[0]) == {
        "method": "GET",
        "url": "https://discord.com/api/v10/applications/APP/emojis",
        "headers": {"User-agent": da.DEFAULT_USER_AGENT, "Authorization": "Bot tok"},
        "body": None,
    }


def test_app_emojis_resolves_app_id_when_omitted(capture):
    sent, replies, req_of = capture
    replies.append({"id": "APP99"})          # GET /applications/@me
    replies.append({"items": []})            # GET /applications/APP99/emojis
    out = da.app_emojis("tok")
    assert out == {}
    assert [req_of(r)["url"] for r in sent] == [
        "https://discord.com/api/v10/applications/@me",
        "https://discord.com/api/v10/applications/APP99/emojis",
    ]


def test_upload_app_emoji_posts_data_uri(capture, tmp_path):
    sent, replies, req_of = capture
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG\r\n")
    replies.append({"id": "emoji1"})
    out = da.upload_app_emoji("tok", "APP", "av_deg10", png)
    assert out == "emoji1"
    r = req_of(sent[0])
    assert (r["method"], r["url"]) == ("POST", "https://discord.com/api/v10/applications/APP/emojis")
    assert r["headers"] == {
        "User-agent": da.DEFAULT_USER_AGENT,
        "Authorization": "Bot tok",
        "Content-type": "application/json",
    }
    assert json.loads(r["body"]) == {
        "name": "av_deg10",
        "image": "data:image/png;base64,iVBORw0K",  # base64 of b"\x89PNG\r\n"
    }


def test_bot_prefix_not_doubled(capture):
    sent, replies, req_of = capture
    replies.append({"id": "x"})
    da.app_id("Bot alreadyprefixed")
    assert req_of(sent[0])["headers"]["Authorization"] == "Bot alreadyprefixed"


def test_custom_user_agent_threads_through(capture):
    sent, replies, req_of = capture
    replies.append({"id": "x"})
    da.app_id("tok", user_agent="DiscordBot (https://example.test, 9)")
    assert req_of(sent[0])["headers"]["User-agent"] == "DiscordBot (https://example.test, 9)"


def test_http_error_label_never_leaks_the_url(monkeypatch):
    """The error carries the caller's label + status, not the webhook URL (its
    token is a secret)."""
    url = "https://discord.com/api/webhooks/9/secrettoken"

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b"blocked"))

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as ei:
        da.webhook_info(url)
    assert str(ei.value) == "Discord GET webhook: HTTP 403: blocked"
    assert "secrettoken" not in str(ei.value)
