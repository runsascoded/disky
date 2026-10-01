"""`buckets.yml` parsing (`disk_tree.cli.sync.load_config`)."""

from pathlib import Path

import pytest

from disk_tree.cli.sync import load_config


def _write_cfg(path: Path, text: str) -> str:
    path.write_text(text)
    return str(path)


def test_load_config_full(tmp_path: Path):
    cfg = load_config(_write_cfg(tmp_path / 'buckets.yml', """\
listings: /data/listings
defaults:
  procs: 4
  engine: duckdb
buckets:
  - s3://plain
  - uri: r2://mine
    endpoint_url: https://acct.r2.cloudflarestorage.com
    engine: stream
    pivot_sums: [storage_class_id]
    mean_mtime: true
"""))
    assert cfg.listings == '/data/listings'
    assert [(b.uri, b.host, b.scheme) for b in cfg.buckets] == [
        ('s3://plain', 'plain', 's3'),
        ('r2://mine', 'mine', 'r2'),
    ]
    plain, mine = cfg.buckets
    assert (plain.procs, plain.engine) == (4, 'duckdb')  # defaults applied
    assert (mine.engine, mine.pivot_sums, mine.mean_mtime) == ('stream', ('storage_class_id',), True)
    assert mine.endpoint_url == 'https://acct.r2.cloudflarestorage.com'


def test_load_config_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match='no config at'):
        load_config(str(tmp_path / 'nope.yml'))


@pytest.mark.parametrize('text,match', [
    ("buckets: []\n", r'`buckets:` list is empty'),
    ("bukets:\n  - s3://b\n", r"unknown top-level key\(s\) \['bukets'\]"),
    ("buckets:\n  - uri: s3://b\n    engin: stream\n", r"unknown key\(s\) \['engin'\]"),
    ("defaults:\n  uri: s3://x\nbuckets:\n  - s3://b\n", r"unknown `defaults` key\(s\) \['uri'\]"),
    ("buckets:\n  - uri: s3://b\n    engine: fast\n", r"engine must be one of"),
    ("buckets:\n  - /local/path\n", r'must be cloud URIs'),
])
def test_load_config_schema_errors(tmp_path: Path, text: str, match: str):
    with pytest.raises((ValueError, FileNotFoundError), match=match):
        load_config(_write_cfg(tmp_path / 'buckets.yml', text))
