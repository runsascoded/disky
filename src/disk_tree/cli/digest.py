"""`disk-tree digest` — post a bucket's usage digest to Slack or Discord.

Config lives in a per-bucket ``digest:`` block in ``buckets.yml`` (spec
``comms-notify.md``)::

    buckets:
      - uri: gcs://my-bucket
        digest:
          profile: bytes                 # built-in name or an import path
          period: month                  # month | week | day
          site_url: https://my.dashboard
          icons_base: https://my.icons   # av_deg{N}.png trend arrows (optional)
          state: gcs://my-bucket         # where converge JSON lives (default: the bucket)
          slack:   { channel: "#usage", token_env: SLACK_BOT_TOKEN, plot_url: https://... }
          discord: { webhook_env: DISCORD_WEBHOOK_URL, bot_token_env: DISCORD_BOT_TOKEN }

Secrets are named by their *env var* (``*_env``), never inlined — the command
reads ``os.environ`` at post time. ``--dry-run`` builds the rows, renders the
plot, and prints the OP body without posting or reading any secret.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from click import Choice, argument, option
from utz import err

from disk_tree.cli.base import cli


def _select_bucket(cfg, bucket: str | None):
    with_digest = [b for b in cfg.buckets if b.digest]
    if bucket:
        matches = [b for b in with_digest if bucket in b.uri]
        if len(matches) != 1:
            raise SystemExit(f"digest: {bucket!r} matched {len(matches)} configured buckets with a `digest:` block")
        return matches[0]
    if len(with_digest) != 1:
        raise SystemExit(f"digest: {len(with_digest)} buckets have a `digest:` block — name one")
    return with_digest[0]


@cli.command('digest')
@option('-c', '--config', 'config_path', default=None, help='buckets.yml path (default: <DISK_TREE_ROOT>/buckets.yml)')
@option('-m', '--month', default=None, help='Target month YYYY-MM (default: current UTC month)')
@option('-n', '--dry-run', is_flag=True, help='Build rows + render plot + print the OP body; do not post or read secrets')
@option('-p', '--platform', type=Choice(['slack', 'discord']), default='discord', help='Post target (default discord)')
@argument('bucket', required=False)
def digest(config_path: str | None, month: str | None, dry_run: bool, platform: str, bucket: str | None):
    """Converge BUCKET's monthly usage thread on Slack/Discord (one OP edited in
    place + one reply per scan). BUCKET is a substring of a configured bucket
    uri; omit it when exactly one bucket has a `digest:` block."""
    import os
    import tempfile
    from pathlib import Path

    from disk_tree.cli.sync import load_config
    from disk_tree.notify.digest import period_of, post_digest_discord, post_digest_slack
    from disk_tree.notify.profile import build_profile
    from disk_tree.notify.sources import scan_rows

    cfg = load_config(config_path)
    b = _select_bucket(cfg, bucket)
    block = b.digest
    profile = build_profile(block)
    kind = block.get('period', 'month')
    anchor = date(int(month[:4]), int(month[5:7]), 1) if month else datetime.now(timezone.utc).date()
    period = period_of(anchor, kind)
    root = block.get('state') or b.uri

    rows = scan_rows(profile, b.uri, period)
    if not rows:
        raise SystemExit(f"digest: no scans for {b.uri} in {period.key}")
    err(f"digest: {b.uri} {period.key} — {len(rows)} scan(s)")

    if dry_run:
        out = Path(tempfile.gettempdir()) / f"digest-{period.key}.png"
        profile.render_plot(rows, period, out)
        err(f"digest: rendered plot -> {out}")
        print(profile.op_body(rows, period, None))
        return

    if platform == 'discord':
        d = block['discord']
        post_digest_discord(profile, root, period, rows, os.environ[d['webhook_env']], os.environ[d['bot_token_env']])
    else:
        s = block['slack']
        post_digest_slack(profile, root, period, rows, os.environ[s['token_env']], s['channel'], plot_url=s.get('plot_url'))
    err(f"digest: posted to {platform}")
