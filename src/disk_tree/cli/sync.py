"""`disk-tree fetch` / `pull` / `sync` — config-driven refresh of the buckets you track.

Git-shaped verbs over a one-way mirror (bucket → local index):

- ``fetch [BUCKET…]``  — bulk-list to dated raw-listing shards (layer-1); no import
- ``pull  [BUCKET…]``  — fetch + import (layer-2 scan registered in SQLite)
- ``sync``             — pull every configured bucket (the cron entrypoint)

There's deliberately no ``push`` — nothing flows back to the bucket.

Cadence lives in your scheduler, not here: both stages are idempotent per
``(bucket, --date)`` (fetch skips when the dated listing dir has a
``_SUCCESS.json``; pull skips when a Scan row exists at that path + date), so
crontab / launchd / systemd-timer entries at any frequency are safe to re-run.

Config: ``<DISK_TREE_ROOT>/buckets.yml``::

    listings: /path/or/url     # optional; default <DISK_TREE_ROOT>/listings
    defaults:                  # optional; per-bucket keys win
      procs: 6
      threads: 8
      engine: stream
    buckets:
      - s3://my-bucket         # bare-string shorthand
      - uri: r2://my-r2-bucket
        endpoint_url: https://<acct>.r2.cloudflarestorage.com
        profile: my-r2         # AWS credential profile (cross-account source/target)
      - uri: gcs://my-gcs-bucket
        prefix: some/subdir
        pivot_sums: [storage_class_id]
        mean_mtime: true

`profile` (per-bucket, or under `defaults`) names an AWS credential profile — how
a source and a target in *different* accounts each authenticate within one run.
Omit it for the single-account case (ambient env / default profile).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from os.path import join

from click import argument, option
from utz import err

from disk_tree.cli.base import cli
from disk_tree.config import ROOT_DIR

CONFIG_BASENAME = 'buckets.yml'
SUCCESS_MARKER = '_SUCCESS.json'
_ENGINES = ('pandas', 'duckdb', 'stream')


@dataclass
class BucketCfg:
    uri: str
    prefix: str | None = None
    endpoint_url: str | None = None
    region: str | None = None
    profile: str | None = None
    procs: int = 6
    threads: int = 8
    engine: str = 'stream'
    pivot_sums: tuple[str, ...] = ()
    mean_mtime: bool = False
    digest: dict | None = None  # passthrough for `disk-tree digest` (see cli/digest.py); sync ignores it

    def __post_init__(self):
        from disk_tree.backends.url import parse_url
        if self.engine not in _ENGINES:
            raise ValueError(f"{self.uri}: engine must be one of {_ENGINES}; got {self.engine!r}")
        self.pivot_sums = tuple(self.pivot_sums)
        parsed = parse_url(self.uri)
        self.scheme = parsed.scheme
        self.host = parsed.host
        if self.scheme not in ('gcs', 's3', 'r2'):
            raise ValueError(f"buckets.yml entries must be cloud URIs (gcs://, s3://, r2://); got {self.uri!r}")


@dataclass
class SyncCfg:
    listings: str
    buckets: list[BucketCfg]
    delete: dict | None = None  # deployment-wide staged-delete policy (CP4): chat/undo/database_id


def load_config(path: str | None) -> SyncCfg:
    import yaml
    cfg_path = path or join(ROOT_DIR, CONFIG_BASENAME)
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(
            f"no config at {cfg_path} — create it with a `buckets:` list "
            f"(see `disk-tree sync --help` for the schema)"
        )
    with open(cfg_path) as f:
        raw = yaml.safe_load(f) or {}
    unknown = set(raw) - {'listings', 'defaults', 'buckets', 'delete'}
    if unknown:
        raise ValueError(f"{cfg_path}: unknown top-level key(s) {sorted(unknown)}")
    defaults = raw.get('defaults') or {}
    entries = raw.get('buckets') or []
    if not entries:
        raise ValueError(f"{cfg_path}: `buckets:` list is empty")
    valid_keys = {f.name for f in fields(BucketCfg)}
    bad_defaults = set(defaults) - (valid_keys - {'uri'})
    if bad_defaults:
        raise ValueError(f"{cfg_path}: unknown `defaults` key(s) {sorted(bad_defaults)}")
    buckets = []
    for e in entries:
        if isinstance(e, str):
            e = {'uri': e}
        if not isinstance(e, dict) or 'uri' not in e:
            raise ValueError(f"{cfg_path}: each bucket entry must be a URI string or a dict with `uri`; got {e!r}")
        bad = set(e) - valid_keys
        if bad:
            raise ValueError(f"{cfg_path}: bucket {e['uri']!r} has unknown key(s) {sorted(bad)}")
        buckets.append(BucketCfg(**{**defaults, **e}))
    listings = os.path.expanduser(raw.get('listings') or join(ROOT_DIR, 'listings'))
    return SyncCfg(listings=listings, buckets=buckets, delete=raw.get('delete'))


def select_buckets(cfg: SyncCfg, names: tuple[str, ...]) -> list[BucketCfg]:
    """Match CLI args against bucket host names or full URIs; no args → all."""
    if not names:
        return cfg.buckets
    by_key = {}
    for b in cfg.buckets:
        by_key[b.host] = b
        by_key[b.uri] = b
    missing = [n for n in names if n not in by_key]
    if missing:
        known = ', '.join(b.uri for b in cfg.buckets)
        raise ValueError(f"unknown bucket(s) {missing} — configured: {known}")
    # De-dupe while preserving arg order (host + uri may both be given).
    seen, out = set(), []
    for n in names:
        b = by_key[n]
        if b.uri not in seen:
            seen.add(b.uri)
            out.append(b)
    return out


def listing_dir(cfg: SyncCfg, b: BucketCfg, date: str) -> str:
    return f'{cfg.listings}/{date}/{b.host}'


def _has_success(dir_url: str) -> bool:
    marker = f'{dir_url}/{SUCCESS_MARKER}'
    if '://' in dir_url:
        import fsspec
        fs, path = fsspec.core.url_to_fs(marker)
        return fs.exists(path)
    return os.path.exists(marker)


def fetch_bucket(cfg: SyncCfg, b: BucketCfg, date: str, force: bool) -> str:
    """Bulk-list one bucket to its dated listing dir (idempotent). Returns the dir."""
    out_dir = listing_dir(cfg, b, date)
    if not force and _has_success(out_dir):
        err(f"{b.uri}: listing {date} already complete → {out_dir} (use -f to re-list)")
        return out_dir
    from disk_tree.cli.bulk_list import bulk_list_uri
    err(f"{b.uri}: listing → {out_dir}")
    total = bulk_list_uri(
        b.uri, out_dir=out_dir,
        prefix=b.prefix, procs=b.procs, threads=b.threads,
        exists='clear' if force else 'reuse',
        endpoint_url=b.endpoint_url, region=b.region, profile=b.profile,
    )
    err(f"{b.uri}: listed {total:,} objects")
    return out_dir


def _run(
    config: str | None,
    date: str | None,
    names: tuple[str, ...],
    do_import: bool,
    force_fetch: bool = False,
    force_import: bool = False,
    diff_index: bool = True,
) -> None:
    cfg = load_config(config)
    date = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    picked = select_buckets(cfg, names)

    if do_import:
        import duckdb
        from disk_tree.cli.import_listing import import_bucket
        from disk_tree.sqla.db import init
        from disk_tree.sqla.model import Scan
        from disk_tree.storage import get_backend
        db = init()
        db.create_all()
        storage = get_backend()
        con = duckdb.connect()
        # Scans from sync are dated, not timestamped: midnight UTC of --date,
        # which doubles as the idempotency key (one scan per bucket per date).
        snap_time = datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=timezone.utc)

    for b in picked:
        d = fetch_bucket(cfg, b, date, force_fetch)
        if not do_import:
            continue
        scan_path = f'{b.scheme}://{b.host}'
        existing = db.session.query(Scan).filter_by(path=scan_path, time=snap_time).first()
        if existing is not None and not force_import:
            err(f"{b.uri}: scan for {date} already imported (id={existing.id}; use -f to re-import)")
            continue
        err(f"{b.uri}: importing (engine={b.engine})…")
        scan = import_bucket(
            db=db, storage=storage, con=con,
            engine=b.engine, listings=(f'{d}/*.parquet',),
            bucket=b.host, scheme=b.scheme, snap_time=snap_time,
            pivot_sums=b.pivot_sums, mean_mtime=b.mean_mtime,
            replace=existing,
        )
        if diff_index and scan is not None:
            # Overnight prep: yesterday→today is a parquet slice by morning,
            # not a walk (spec: done/diff-index.md).
            from disk_tree.cli.diff_index import build_previous
            build_previous(scan.id)


_OPT_CONFIG = option('-c', '--config', default=None, help=f'Config path (default: <DISK_TREE_ROOT>/{CONFIG_BASENAME})')
_OPT_DATE = option('-d', '--date', default=None, help='Snapshot date YYYY-MM-DD (default: today UTC). Both stages are idempotent per (bucket, date).')


@cli.command('fetch')
@_OPT_CONFIG
@_OPT_DATE
@option('-f', '--force', is_flag=True, help='Re-list even when the dated listing dir is already complete')
@argument('buckets', nargs=-1)
def fetch_cmd(config: str | None, date: str | None, force: bool, buckets: tuple[str, ...]):
    """Bulk-list configured BUCKETS (default: all) to dated raw-listing shards; no import."""
    _run(config, date, buckets, do_import=False, force_fetch=force)


@cli.command('pull')
@_OPT_CONFIG
@_OPT_DATE
@option('-D', '--no-diff', is_flag=True, help="Skip building each bucket's diff index against its previous scan")
@option('-f', '--force', is_flag=True, help='Re-import even when a scan exists for (bucket, date). Reuses a complete listing — run `fetch -f` first to also re-list.')
@argument('buckets', nargs=-1)
def pull_cmd(config: str | None, date: str | None, no_diff: bool, force: bool, buckets: tuple[str, ...]):
    """Fetch + import configured BUCKETS (default: all) as dated scans."""
    _run(config, date, buckets, do_import=True, force_import=force, diff_index=not no_diff)


@cli.command('sync')
@_OPT_CONFIG
@_OPT_DATE
@option('-D', '--no-diff', is_flag=True, help="Skip building each bucket's diff index against its previous scan")
@option('-f', '--force', is_flag=True, help='Re-import even when a scan exists for (bucket, date). Reuses complete listings — run `fetch -f` first to also re-list.')
def sync_cmd(config: str | None, date: str | None, no_diff: bool, force: bool):
    """Pull every configured bucket — the cron/launchd entrypoint.

    Config schema (<DISK_TREE_ROOT>/buckets.yml):

    \b
      listings: /path/or/url     # optional; default <DISK_TREE_ROOT>/listings
      defaults:                  # optional; per-bucket keys win
        procs: 6
        threads: 8
        engine: stream           # pandas | duckdb | stream
      buckets:
        - s3://my-bucket         # bare-string shorthand
        - uri: r2://my-r2-bucket
          endpoint_url: https://<acct>.r2.cloudflarestorage.com
        - uri: gcs://my-gcs-bucket
          prefix: some/subdir
          region: us-east-1
          pivot_sums: [storage_class_id]
          mean_mtime: true
    """
    _run(config, date, (), do_import=True, force_import=force, diff_index=not no_diff)
