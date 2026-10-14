import json
from urllib.parse import parse_qs

import pytest
from click.testing import CliRunner

from disk_tree.cli.base import cli
from disk_tree.notify import push
from disk_tree.notify.push import Notice, configured, send

ENV = {'PUSHOVER_TOKEN': 'tok', 'PUSHOVER_USER': 'usr', 'DISCORD_NOTIFY_WEBHOOK': 'https://discord.test/hook'}


class Recorder:
    """A `Post` seam: records each request, answers from `statuses` (default 200 / 204)."""

    def __init__(self, statuses: dict[str, int] | None = None):
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []
        self.statuses = statuses or {}

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
        self.calls.append((url, body, headers))
        default = 200 if url == push.PUSHOVER_URL else 204
        status = self.statuses.get(url, default)
        return status, '{"user":"invalid","status":0}' if status >= 400 else ''


def test_configured_defaults_to_every_channel_with_credentials():
    assert [
        configured(ENV),
        configured({'PUSHOVER_TOKEN': 'tok'}),
        configured({'DISCORD_NOTIFY_WEBHOOK': 'https://discord.test/hook'}),
        configured({**ENV, 'DISK_TREE_NOTIFY': 'discord, pushover'}),
    ] == [['pushover', 'discord'], [], ['discord'], ['discord', 'pushover']]


def test_configured_refuses_unknown_or_uncredentialed_channels():
    with pytest.raises(ValueError, match="unknown channel 'slack'"):
        configured({**ENV, 'DISK_TREE_NOTIFY': 'slack'})
    with pytest.raises(ValueError, match="names 'pushover', but its credentials are not set"):
        configured({'DISK_TREE_NOTIFY': 'pushover'})


def test_send_posts_each_channel_its_shape():
    rec = Recorder()
    n = Notice('ingest failed', '2026-10-14T1003 at path-index (exit 137)', 'https://disk.rbw.sh/scans/r1', 'run', 1)
    assert send(n, ['pushover', 'discord'], env=ENV, post=rec) == [('pushover', None), ('discord', None)]
    (p_url, p_body, p_hdr), (d_url, d_body, d_hdr) = rec.calls
    assert (p_url, p_hdr, parse_qs(p_body.decode())) == (push.PUSHOVER_URL, {'Content-Type': 'application/x-www-form-urlencoded'}, {
        'token': ['tok'], 'user': ['usr'], 'title': ['ingest failed'], 'message': ['2026-10-14T1003 at path-index (exit 137)'],
        'priority': ['1'], 'url': ['https://disk.rbw.sh/scans/r1'], 'url_title': ['run'],
    })
    assert (d_url, d_hdr, json.loads(d_body)) == ('https://discord.test/hook', {'Content-Type': 'application/json'}, {
        'content': '**ingest failed**\n2026-10-14T1003 at path-index (exit 137)\n[run](<https://disk.rbw.sh/scans/r1>)',
        'username': 'disky', 'allowed_mentions': {'parse': []}, 'flags': 4,
    })


def test_send_first_stops_at_the_first_success_and_falls_back_past_failures():
    n = Notice('t', 'm')
    ok = Recorder()
    assert (send(n, ['pushover', 'discord'], first=True, env=ENV, post=ok), len(ok.calls)) == ([('pushover', None)], 1)
    bad = Recorder({push.PUSHOVER_URL: 400})
    assert send(n, ['pushover', 'discord'], first=True, env=ENV, post=bad) == [
        ('pushover', 'pushover: HTTP 400: {"user":"invalid","status":0}'),
        ('discord', None),
    ]


def test_cli_exit_codes(monkeypatch):
    for k in [*ENV, 'DISK_TREE_NOTIFY']:
        monkeypatch.delenv(k, raising=False)
    r = CliRunner().invoke(cli, ['notify', 'hello'])
    assert (r.exit_code, r.output) == (2, 'notify: no channel configured (PUSHOVER_TOKEN + PUSHOVER_USER, or DISCORD_NOTIFY_WEBHOOK)\n')

    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    rec = Recorder({push.PUSHOVER_URL: 500, 'https://discord.test/hook': 404})
    monkeypatch.setattr(push, 'http_post', rec)
    r = CliRunner().invoke(cli, ['notify', '-c', 'discord,pushover', 'hello'])
    assert (r.exit_code, r.output.splitlines()) == (1, [
        'notify: discord: discord: HTTP 404: {"user":"invalid","status":0}',
        'notify: pushover: pushover: HTTP 500: {"user":"invalid","status":0}',
    ])
