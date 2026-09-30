"""Reachability-aware scan selection (spec `r2-scan-target.md`).

When the freshest scan's parquet lives only on an unmounted volume, the read
paths must fall back to an older *reachable* scan (or skip it) instead of
crashing on the missing blob. `blob_reachable` is monkeypatched per-test to a
name allow-list so no real filesystem/network is touched.
"""

from __future__ import annotations

import sqlite3

import pytest

from disk_tree import config, registry
from disk_tree.diff_index import previous_scan


def _db(rows: list[tuple[int, str, float, str]]) -> sqlite3.Connection:
    con = sqlite3.connect(':memory:')
    con.row_factory = sqlite3.Row
    con.execute('CREATE TABLE scan (id INTEGER PRIMARY KEY, path TEXT, time REAL, blob TEXT)')
    con.executemany('INSERT INTO scan (id, path, time, blob) VALUES (?, ?, ?, ?)', rows)
    return con


def _reachable(monkeypatch, names: set[str]) -> None:
    monkeypatch.setattr(config, 'blob_reachable', lambda blob, prefer=None: blob in names)


def test_previous_scan_skips_unreachable_blob(monkeypatch):
    # Scan 3 is newest-earlier but its blob is on an unmounted volume; the diff
    # should pair against scan 2 (the newest earlier scan that is reachable).
    con = _db([
        (1, '/h', 10.0, 'a.parquet'),
        (2, '/h', 20.0, 'b.parquet'),
        (3, '/h', 30.0, 'gone.parquet'),
        (4, '/h', 40.0, 'd.parquet'),
    ])
    _reachable(monkeypatch, {'a.parquet', 'b.parquet', 'd.parquet'})
    assert previous_scan(con, 4)['id'] == 2


def test_previous_scan_none_when_no_earlier_blob_is_reachable(monkeypatch):
    con = _db([
        (1, '/h', 10.0, 'x.parquet'),
        (2, '/h', 20.0, 'y.parquet'),
    ])
    _reachable(monkeypatch, set())
    assert previous_scan(con, 2) is None


def test_freshest_scan_covering_skips_unreachable_to_an_older_scan(monkeypatch):
    con = _db([
        (1, '/h', 10.0, 'old.parquet'),
        (2, '/h', 30.0, 'gone.parquet'),
    ])
    _reachable(monkeypatch, {'old.parquet'})
    got = registry.freshest_scan_covering(con, '/h/sub/dir')
    assert (got['id'], got['blob']) == (1, 'old.parquet')


def test_freshest_scan_covering_none_when_all_unreachable(monkeypatch):
    con = _db([(1, '/h', 10.0, 'gone.parquet')])
    _reachable(monkeypatch, set())
    assert registry.freshest_scan_covering(con, '/h') is None


def test_freshest_scan_covering_honors_explicit_unreachable_id(monkeypatch):
    con = _db([(7, '/h', 10.0, 'gone.parquet')])
    _reachable(monkeypatch, set())
    got = registry.freshest_scan_covering(con, '/h', scan_id='7')
    assert got['id'] == 7
