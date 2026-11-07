"""Multi-store index rows (specs/done/multi-store.md phase 1): a secondary
store's rows are namespaced (`<store>:<variant>` + the `store` column) beside
the primary's."""

from __future__ import annotations

import sqlite3

import pytest

from dt_cloud import index_footer
from dt_cloud.index_footer import gc_d1, index_dir, retire_d1, sync_d1, synced_variants

ROWS = [
    {"rg": i, "d_min": 1, "d_max": 2, "p_min": "a", "p_max": "b", "b_max": 10, "u_min": None, "u_max": None, "row_start": 0, "row_end": 1, "rg_json": "[1]"}
    for i in range(2)
]


def _db(monkeypatch, con: sqlite3.Connection) -> sqlite3.Connection:
    """`con` (the D1 schema, FKs ON, as D1) wired in as the D1 every
    `index_footer` query runs against."""

    def fake_query(sql, acct, tok, db_id=None):
        cur = con.execute(sql)
        return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()] if cur.description else []

    monkeypatch.setattr(index_footer, "_d1_query", fake_query)
    monkeypatch.setattr(index_footer, "_creds", lambda: ("tok", "acct"))
    monkeypatch.setattr(index_footer, "extract", lambda _p: ({"version": 1, "schema": []}, ROWS))
    monkeypatch.setattr(index_footer, "write_groups_blob", lambda path, schema, rs: (path, 0))
    monkeypatch.setattr(index_footer, "write_groups_parquet", lambda path, schema, rs: (path, 0))
    return con


def test_primary_syncs_and_reads(monkeypatch, d1_db):
    con = _db(monkeypatch, d1_db)
    assert sync_d1("2026-09-01", "x.parquet", gen="g1", key="listing/2026-09-01/index/g1") == 2
    assert sync_d1("2026-09-01", "x.parquet", gen="g2", key="listing/2026-09-01/index/g2") == 2
    assert index_dir("2026-09-01") == "listing/2026-09-01/index/g2"
    assert synced_variants() == [("2026-09-01", "path")]
    assert gc_d1("2026-09-01") == 2
    assert con.execute("SELECT date, variant, gen, rg FROM index_row_groups ORDER BY rg").fetchall() == [
        ("2026-09-01", "path", "g2", 0), ("2026-09-01", "path", "g2", 1),
    ]


def test_secondary_store_is_namespaced_beside_the_primary(monkeypatch, d1_db):
    con = _db(monkeypatch, d1_db)
    sync_d1("2026-09-01", "x.parquet", gen="g1", key="listing/2026-09-01/index/g1")
    # Same scan id, same run's gen: the namespaced variant keeps every key disjoint.
    sync_d1("2026-09-01", "x.parquet", gen="g1", key="meta-l2/2026-09-01/index/g1", store="meta")
    sync_d1("2026-08-01", "x.parquet", gen="g0", key="meta-l2/2026-08-01/index/g0", store="meta")
    assert con.execute("SELECT store, date, variant, gen, dir FROM index_schema ORDER BY store, date").fetchall() == [
        ("meta", "2026-08-01", "meta:path", "g0", "meta-l2/2026-08-01/index/g0"),
        ("meta", "2026-09-01", "meta:path", "g1", "meta-l2/2026-09-01/index/g1"),
        ("primary", "2026-09-01", "path", "g1", "listing/2026-09-01/index/g1"),
    ]
    assert con.execute("SELECT store, date, variant, count(*) FROM index_row_groups GROUP BY 1, 2, 3 ORDER BY 1, 2").fetchall() == [
        ("meta", "2026-08-01", "meta:path", 2),
        ("meta", "2026-09-01", "meta:path", 2),
        ("primary", "2026-09-01", "path", 2),
    ]
    # Each store sees only its own pointers and scans (variants as it names them).
    assert [index_dir("2026-09-01"), index_dir("2026-09-01", store="meta"), index_dir("2026-08-01")] == [
        "listing/2026-09-01/index/g1", "meta-l2/2026-09-01/index/g1", None,
    ]
    assert synced_variants() == [("2026-09-01", "path")]
    assert synced_variants(store="meta") == [("2026-08-01", "path"), ("2026-09-01", "path")]
    # gc / retention never touch the other store's live rows.
    assert [gc_d1("2026-09-01"), gc_d1("2026-09-01", store="meta")] == [0, 0]
    assert retire_d1(1, store="meta", has_cold=lambda _p: True) == ([("2026-08-01", "path", 2)], [])
    assert retire_d1(0, has_cold=lambda _p: True) == ([("2026-09-01", "path", 2)], [])
    assert con.execute("SELECT store, date, count(*) FROM index_row_groups GROUP BY 1, 2 ORDER BY 1, 2").fetchall() == [
        ("meta", "2026-09-01", 2),
    ]
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []


def test_bad_store_key_is_refused(monkeypatch, d1_db):
    _db(monkeypatch, d1_db)
    with pytest.raises(ValueError, match=r"^bad store 'Meta' \(want 'primary' or \[a-z0-9\]\[a-z0-9-\]\*\)$"):
        sync_d1("2026-09-01", "x.parquet", gen="g1", key="k", store="Meta")
