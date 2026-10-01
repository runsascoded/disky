"""Reachability-aware scan selection (spec `r2-scan-target.md`).

When the freshest scan's parquet lives only on an unmounted volume, the read
paths must fall back to an older *reachable* scan (or skip it) instead of
crashing on the missing blob. `blob_reachable` is monkeypatched per-test to a
name allow-list so no real filesystem/network is touched.
"""

from __future__ import annotations

import sqlite3


from disk_tree import config, registry


def _db(rows: list[tuple[int, str, float, str]]) -> sqlite3.Connection:
    con = sqlite3.connect(':memory:')
    con.row_factory = sqlite3.Row
    con.execute('CREATE TABLE scan (id INTEGER PRIMARY KEY, path TEXT, time REAL, blob TEXT)')
    con.executemany('INSERT INTO scan (id, path, time, blob) VALUES (?, ?, ?, ?)', rows)
    return con


def _reachable(monkeypatch, names: set[str]) -> None:
    monkeypatch.setattr(config, 'blob_reachable', lambda blob, prefer=None: blob in names)


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
