"""`disk-tree notify MESSAGE` — push a notification to a person (Pushover, a
Discord webhook), configured by env (`disk_tree.notify.push`). The ingest's
failure trap sends through it."""

from __future__ import annotations

import sys

from click import argument, option

from disk_tree.cli.base import cli


@cli.command('notify')
@option('-c', '--channels', help='Comma-separated channels, in order (default: `$DISK_TREE_NOTIFY`, else every configured one)')
@option('-f', '--first', is_flag=True, help='Stop at the first channel that succeeds (a fallback chain) instead of posting to all')
@option('-l', '--url-title', help='Label for `-u`')
@option('-p', '--priority', type=int, default=0, help="Pushover priority: -2 (silent) … 1 (high)")
@option('-t', '--title', default='disky', help='Notification title')
@option('-u', '--url', help='A link to attach (e.g. the run on the site)')
@argument('message')
def notify_cmd(channels: str | None, first: bool, url_title: str | None, priority: int, title: str, url: str | None, message: str):
    """Push MESSAGE to the configured channels. Exit 2 when none is configured, 1 when every attempt failed."""
    import os

    from disk_tree.notify import push

    env = dict(os.environ)
    if channels is not None:
        env['DISK_TREE_NOTIFY'] = channels
    chans = push.configured(env)
    if not chans:
        print('notify: no channel configured (PUSHOVER_TOKEN + PUSHOVER_USER, or DISCORD_NOTIFY_WEBHOOK)', file=sys.stderr)
        sys.exit(2)
    results = push.send(push.Notice(title, message, url, url_title, priority), chans, first=first, env=env, post=push.http_post)
    for c, e in results:
        print(f'notify: {c}: {e or "sent"}', file=sys.stderr)
    if all(e for _, e in results):
        sys.exit(1)
