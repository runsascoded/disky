"""URL entries on the blob search path (spec `remote-scan-targets.md`, Phase 1).

A remote scans dir is just another search-path entry: `Scan.blob` stays a
basename, the write target may be a URL, and reads resolve across the path —
local dirs first, so a local hit never costs a network round-trip.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from disk_tree import blobfs, config

pytest.importorskip('fsspec')


def _mem() -> str:
    return f'memory://{uuid4()}'


def test_split_dirs_keeps_urls_intact():
    assert config.split_dirs('r2://b/p:/local/x:memory://m') == ['r2://b/p', '/local/x', 'memory://m']
    assert config.split_dirs('/a:/b') == ['/a', '/b']
    assert config.split_dirs('') == []


def test_url_entries_are_kept_verbatim(monkeypatch, tmp_path: Path):
    m = _mem()
    monkeypatch.setenv(config.DISK_TREE_SCAN_DIRS_VAR, f'{m}:{tmp_path}')
    assert config.configured_scan_dirs() == [m, str(tmp_path)]


def test_url_entry_counts_as_mounted():
    assert config._volume_mounted('memory://x/y') is True


def test_url_first_entry_is_the_write_target(monkeypatch, tmp_path: Path):
    m = _mem()
    monkeypatch.setenv(config.DISK_TREE_SCAN_DIRS_VAR, f'{m}:{tmp_path}')
    assert config.scan_write_dir() == m


def test_resolve_prefers_a_local_hit_without_probing_remote(monkeypatch, tmp_path: Path):
    m = _mem()
    (tmp_path / 'b.parquet').write_bytes(b'x')
    monkeypatch.setattr(config, 'SCANS_DIR', m)
    monkeypatch.setenv(config.DISK_TREE_SCAN_DIRS_VAR, f'{m}:{tmp_path}')
    monkeypatch.setattr(config, 'blob_exists', lambda p: pytest.fail(f'probed remote {p}'))
    assert config.resolve_scan_blob('b.parquet') == str(tmp_path / 'b.parquet')


def test_resolve_finds_a_remote_only_blob(monkeypatch, tmp_path: Path):
    m = _mem()
    fs, root = blobfs.fs_for(m)
    fs.pipe(f'{root}/b.parquet', b'x')
    monkeypatch.setattr(config, 'SCANS_DIR', str(tmp_path))
    monkeypatch.setenv(config.DISK_TREE_SCAN_DIRS_VAR, f'{tmp_path}:{m}')
    assert config.resolve_scan_blob('b.parquet') == f'{m}/b.parquet'


def test_resolve_falls_back_to_a_url_write_dir(monkeypatch):
    m = _mem()
    monkeypatch.setattr(config, 'SCANS_DIR', m)
    monkeypatch.setenv(config.DISK_TREE_SCAN_DIRS_VAR, m)
    assert config.resolve_scan_blob('nope.parquet') == f'{m}/nope.parquet'


def test_blob_reachable_distinguishes_present_from_write_dir_fallback(monkeypatch, tmp_path: Path):
    """`blob_reachable` is True only for a blob that actually resolves; a name
    that would only hit the write-dir fallback (the raw-`FileNotFoundError`
    case, spec `r2-scan-target.md`) is False."""
    m = _mem()
    (tmp_path / 'here.parquet').write_bytes(b'x')
    monkeypatch.setattr(config, 'SCANS_DIR', m)
    monkeypatch.setenv(config.DISK_TREE_SCAN_DIRS_VAR, f'{m}:{tmp_path}')
    assert config.blob_reachable('here.parquet') is True
    assert config.blob_reachable('gone.parquet') is False
    assert config._find_blob('gone.parquet') is None
    # resolve still hands back the write-dir path for a would-be create.
    assert config.resolve_scan_blob('gone.parquet') == f'{m}/gone.parquet'


def test_set_write_target_prepends_rebinds_and_fires_hooks(monkeypatch, tmp_path: Path):
    m = _mem()
    monkeypatch.setenv(config.DISK_TREE_SCAN_DIRS_VAR, str(tmp_path))
    monkeypatch.setattr(config, 'SCANS_DIR', str(tmp_path))
    fired = []
    monkeypatch.setattr(config, '_write_target_hooks', [lambda: fired.append(1)])

    assert config.set_write_target(m) == m
    assert config.SCANS_DIR == m
    assert config.scan_write_dir() == m
    assert config.configured_scan_dirs() == [m, str(tmp_path)]
    assert config.scan_read_dirs()[:2] == [m, str(tmp_path)]
    assert fired == [1]


def test_set_write_target_validates_the_scheme_up_front(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(config.DISK_TREE_SCAN_DIRS_VAR, str(tmp_path))
    with pytest.raises(ValueError, match='Protocol not known: bogus'):
        config.set_write_target('bogus://x')
    assert config.configured_scan_dirs() == [str(tmp_path)]
