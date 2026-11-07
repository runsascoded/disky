"""Tests for multi-directory blob storage (`disk_tree.config` search path).

Blobs are referenced by basename, so they may live on any dir in the search
path (`DISK_TREE_SCAN_DIRS`, else the root's `scans/`).
"""

from __future__ import annotations

from pathlib import Path

from disk_tree import config


def test_explicit_scan_dirs_win(monkeypatch, tmp_path: Path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    monkeypatch.setenv(config.SCAN_DIRS_VAR, f'{a}:{b}')
    assert config.configured_scan_dirs() == [str(a), str(b)]
    assert config.scan_write_dir() == str(a)


def test_default_is_the_roots_scans_dir(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(config.SCAN_DIRS_VAR, raising=False)
    monkeypatch.setattr(config, 'DEFAULT_SCANS_DIR', str(tmp_path / 'scans'))
    assert config.configured_scan_dirs() == [str(tmp_path / 'scans')]
    assert config.scan_write_dir() == str(tmp_path / 'scans')


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
