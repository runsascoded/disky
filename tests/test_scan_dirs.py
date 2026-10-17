"""Tests for multi-directory blob storage (`disk_tree.config` search path).

Blobs are referenced by basename, so they may live on any dir in the search
path — typically an external volume, which can be unplugged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from disk_tree import config


def test_explicit_scan_dirs_win(monkeypatch, tmp_path: Path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    monkeypatch.setenv(config.SCAN_DIRS_VAR, f'{a}:{b}')
    assert config.configured_scan_dirs() == [str(a), str(b)]
    assert config.scan_write_dir() == str(a)


def test_explicit_root_disables_discovery(monkeypatch, tmp_path: Path):
    """An explicit root is a deliberate choice; discovery must not redirect it."""
    monkeypatch.delenv(config.SCAN_DIRS_VAR, raising=False)
    monkeypatch.setenv(config.ROOT_VAR, str(tmp_path))
    monkeypatch.setattr(config, 'DEFAULT_SCANS_DIR', str(tmp_path / 'scans'))
    monkeypatch.setattr(config, 'discovered_scan_dirs', lambda: ['/Volumes/x/disk-tree/scans'])
    assert config.configured_scan_dirs() == [str(tmp_path / 'scans')]


def test_discovery_used_when_no_explicit_config(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(config.SCAN_DIRS_VAR, raising=False)
    monkeypatch.delenv(config.ROOT_VAR, raising=False)
    monkeypatch.setattr(config, 'DEFAULT_SCANS_DIR', str(tmp_path / 'scans'))
    monkeypatch.setattr(config, 'discovered_scan_dirs', lambda: ['/Volumes/x6/disk-tree/scans'])
    assert config.configured_scan_dirs() == ['/Volumes/x6/disk-tree/scans', str(tmp_path / 'scans')]


def test_unmounted_volume_is_never_a_write_target(monkeypatch, tmp_path: Path):
    """Writing under an absent mount point would land on the boot disk."""
    monkeypatch.setenv(config.SCAN_DIRS_VAR, f'/Volumes/gone/disk-tree/scans:{tmp_path}')
    monkeypatch.setattr(config, 'ismount', lambda p: False)
    assert config._volume_mounted('/Volumes/gone/disk-tree/scans') is False
    assert config.scan_write_dir() == str(tmp_path)


def test_mounted_volume_is_preferred(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(config.SCAN_DIRS_VAR, f'/Volumes/here/disk-tree/scans:{tmp_path}')
    monkeypatch.setattr(config, 'ismount', lambda p: p == '/Volumes/here')
    assert config.scan_write_dir() == '/Volumes/here/disk-tree/scans'


def test_non_volume_paths_are_always_available():
    assert config._volume_mounted('/Users/someone/.config/disk-tree/scans') is True


def test_resolve_finds_a_blob_in_a_secondary_dir(monkeypatch, tmp_path: Path):
    near, far = tmp_path / 'near', tmp_path / 'far'
    near.mkdir()
    far.mkdir()
    (far / 'b.parquet').write_bytes(b'x')
    monkeypatch.setattr(config, 'SCANS_DIR', str(near))
    monkeypatch.setenv(config.SCAN_DIRS_VAR, f'{near}:{far}')
    assert config.resolve_scan_blob('b.parquet') == str(far / 'b.parquet')


def test_resolve_falls_back_to_the_write_dir(monkeypatch, tmp_path: Path):
    """A missing blob resolves to where it *would* be, so the eventual open
    error names a useful path."""
    monkeypatch.setattr(config, 'SCANS_DIR', str(tmp_path))
    monkeypatch.setenv(config.SCAN_DIRS_VAR, str(tmp_path))
    assert config.resolve_scan_blob('nope.parquet') == str(tmp_path / 'nope.parquet')


def test_read_dirs_lead_with_the_write_dir_and_dedupe(monkeypatch, tmp_path: Path):
    a, b = str(tmp_path / 'a'), str(tmp_path / 'b')
    monkeypatch.setattr(config, 'SCANS_DIR', b)
    monkeypatch.setattr(config, 'DEFAULT_SCANS_DIR', a)
    monkeypatch.setenv(config.SCAN_DIRS_VAR, f'{a}:{b}')
    assert config.scan_read_dirs() == [b, a]


def test_index_require_external_skips_when_write_target_is_boot_disk(tmp_path: Path):
    """`index --require-external` exits 0 without scanning when no external
    volume is mounted — an explicit DISK_TREE_ROOT forces the boot-disk default,
    which is exactly the fall-through the flag guards against. See the flag's
    use in the scheduled-scan LaunchAgent."""
    import os
    import subprocess
    import sys

    env = {**os.environ, config.ROOT_VAR: str(tmp_path)}
    env.pop(config.SCAN_DIRS_VAR, None)
    r = subprocess.run(
        [sys.executable, '-m', 'disk_tree.cli.main', 'index', '-e', str(tmp_path)],
        env=env, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0
    scans = tmp_path / 'scans'
    assert list(scans.glob('*.parquet')) == []
    last = r.stderr.rstrip('\n').split('\n')[-1]
    assert last == (
        f"--require-external: write target is the boot disk ({scans}); "
        "no external scans volume mounted — skipping"
    )
