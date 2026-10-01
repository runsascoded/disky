"""`<DISK_TREE_ROOT>/buckets.yml` — the tracked-bucket registry (``load_config``).

Read by the staged-delete drainer (``dispatch --serve``: the deployment-wide
``delete:`` block); ``blobfs.bucket_profile`` / ``r2_endpoint`` read the same
file for per-bucket credentials and endpoints::

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
    delete:                    # optional; staged-delete policy (chat/undo/database_id)

`profile` (per-bucket, or under `defaults`) names an AWS credential profile — how
a source and a target in *different* accounts each authenticate within one run.
Omit it for the single-account case (ambient env / default profile).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from os.path import join

from disk_tree.config import ROOT_DIR

CONFIG_BASENAME = 'buckets.yml'
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
            f"(see `disk_tree.cli.sync` for the schema)"
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
