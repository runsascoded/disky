"""The footer LRU's writer side (specs/done/footer-lru.md): `evict` (the site's
`evictFooters` pass), `adopt`, `dt-cloud footer-evict`, and `retire_d1` /
`gc_d1` keeping `footer_cache` coherent — against a real SQLite carrying the
cw lineage's migrations (the D1 schema)."""
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud import cli as C
from dt_cloud import index_footer as F
from dt_cloud.cli import main
from dt_cloud.index_footer import Entry, Eviction, adopt, evict, gc_d1, retire_d1

MIGRATIONS = Path(__file__).parents[2] / "site/migrations/cw"
NOW = 1_790_000_000
TODAY = NOW // 86400


class D1:
    """Every statement runs on SQLite (as D1 runs it) and is recorded."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.sent: list[str] = []

    def __call__(self, sql, acct, tok, db_id=None):
        self.sent.append(sql)
        cur = self.con.execute(sql)
        rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()] if cur.description else []
        self.con.commit()
        return rows

    def entries(self) -> list[tuple]:
        return self.con.execute("SELECT date, variant, gen, state, rows, last_access_day FROM footer_cache ORDER BY date, variant, gen").fetchall()

    def counts(self) -> list[tuple]:
        return self.con.execute("SELECT date, variant, gen, COUNT(*) FROM index_row_groups GROUP BY 1, 2, 3 ORDER BY 1, 2, 3").fetchall()


def _db(before: str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if before is None or f.name < before:
            con.executescript(f.read_text())
    return con


@pytest.fixture
def d1(monkeypatch) -> D1:
    db = D1(_db())
    monkeypatch.setattr(F, "_d1_query", db)
    monkeypatch.setattr(F, "_creds", lambda: ("tok", "acct"))
    monkeypatch.setattr(F, "_now", lambda: NOW)
    return db


def _rows(con, date: str, variant: str, gen: str, n: int) -> None:
    con.executemany(
        "INSERT INTO index_row_groups (date, variant, gen, rg, d_min, d_max, p_min, p_max, b_max, row_start, row_end, rg_json) "
        "VALUES (?, ?, ?, ?, 1, 1, 'ab', 'cd', 1, 0, 1, '[1]')",
        [(date, variant, gen, i) for i in range(n)],
    )


def _hot(con, date: str, n: int, day: int, *, variant: str = "path", gen: str = "g1", nbytes: int = 0) -> None:
    _rows(con, date, variant, gen, n)
    con.execute(
        "INSERT INTO footer_cache (date, variant, gen, state, rows, bytes, started_ts, filled_ts, last_access_day) VALUES (?, ?, ?, 'hot', ?, ?, 0, 0, ?)",
        (date, variant, gen, n, nbytes, day),
    )


def _pointer(con, date: str, variant: str, gen: str) -> None:
    con.execute("INSERT INTO index_schema (date, variant, version, schema_json, gen, dir) VALUES (?, ?, 2, '[]', ?, ?)", (date, variant, gen, f"l2/{date}/index/{gen}"))


def test_evict_demotes_least_recently_read_past_the_budget_then_deletes_after_the_grace(d1):
    for date, n, day in [("s1", 3, 10), ("s2", 2, 12), ("s3", 4, 10), ("s4", 1, 15)]:
        _hot(d1.con, date, n, day, nbytes=10 * n)
    ev = evict(5, now=NOW)
    assert ev == Eviction(
        hot=(4, 10, 100),
        demoted=[Entry("s1", "path", "g1", 3, 30, 10), Entry("s3", "path", "g1", 4, 40, 10)],
        deleted=[],
    )
    assert [(e[0], e[3]) for e in d1.entries()] == [("s1", "evicting"), ("s2", "hot"), ("s3", "evicting"), ("s4", "hot")]
    assert evict(5, now=NOW + F.EVICT_GRACE_S - 1) == Eviction(hot=(2, 3, 30), demoted=[], deleted=[])
    done = evict(5, now=NOW + F.EVICT_GRACE_S)
    assert [e.date for e in done.deleted] == ["s1", "s3"]
    assert d1.entries() == [("s2", "path", "g1", "hot", 2, 12), ("s4", "path", "g1", "hot", 1, 15)]
    assert d1.counts() == [("s2", "path", "g1", 2), ("s4", "path", "g1", 1)]


def test_evict_by_bytes_and_dry_run(d1):
    for date, day in [("s1", 10), ("s2", 11), ("s3", 12)]:
        _hot(d1.con, date, 1, day, nbytes=50)
    assert [e.date for e in evict(100, 60, now=NOW, dry_run=True).demoted] == ["s1", "s2"]
    assert [e[3] for e in d1.entries()] == ["hot", "hot", "hot"]
    ev = evict(100, 60, grace=0, now=NOW)
    assert [[e.date for e in ev.demoted], [e.date for e in ev.deleted]] == [["s1", "s2"], ["s1", "s2"]]
    assert d1.counts() == [("s3", "path", "g1", 1)]


def test_evict_refuses_without_the_table(monkeypatch):
    db = D1(_db(before="0022"))
    monkeypatch.setattr(F, "_d1_query", db)
    monkeypatch.setattr(F, "_creds", lambda: ("tok", "acct"))
    with pytest.raises(RuntimeError, match=r"^no footer_cache table in this D1: apply site/migrations/cw/0022_footer_cache.sql first$"):
        evict()


def test_retire_d1_evicts_through_the_lru_states(d1):
    """`index-gc -r` keeps working on a migrated D1: a retired sort's entries go
    with its rows (none left `hot` over missing rows), the retained scan's stay."""
    for date in ("2026-09-01", "2026-09-02"):
        _pointer(d1.con, date, "path", "g1")
        _hot(d1.con, date, 2, 5)
    # A fill of the old scan in flight: its entry is retired too.
    _pointer(d1.con, "2026-09-01", "bysize", "g1")
    _rows(d1.con, "2026-09-01", "bysize", "g1", 1)
    d1.con.execute("INSERT INTO footer_cache (date, variant, gen, state, started_ts) VALUES ('2026-09-01', 'bysize', 'g1', 'filling', 0)")
    assert retire_d1(1, base="gs://bk", has_cold=lambda _p: True) == ([("2026-09-01", "path", 2), ("2026-09-01", "bysize", 1)], [])
    assert d1.entries() == [("2026-09-02", "path", "g1", "hot", 2, 5)]
    assert d1.counts() == [("2026-09-02", "path", "g1", 2)]
    marks = [s for s in d1.sent if "footer_cache" in s and not s.startswith("SELECT name")]
    assert [s.split(" (")[0].split(" WHERE")[0] for s in marks] == ["INSERT INTO footer_cache", "DELETE FROM footer_cache"] * 2


def test_gc_d1_drops_the_swept_generations_entries(d1):
    _pointer(d1.con, "2026-09-01", "path", "g2")
    _hot(d1.con, "2026-09-01", 2, 5, gen="g1")
    _hot(d1.con, "2026-09-01", 3, 5, gen="g2")
    assert gc_d1("2026-09-01") == 2
    assert d1.entries() == [("2026-09-01", "path", "g2", "hot", 3, 5)]
    assert d1.counts() == [("2026-09-01", "path", "g2", 3)]


def test_adopt_tracks_pre_lru_sorts_with_a_cold_footer(d1):
    for date in ("2026-09-01", "2026-09-02"):
        for v in ("path", "bysize", "age-pyramid-1d"):
            _pointer(d1.con, date, v, "g1")
            _rows(d1.con, date, v, "g1", 2)
    _hot(d1.con, "2026-09-03", 1, 7)  # already tracked
    _pointer(d1.con, "2026-09-03", "path", "g1")
    adopted, skipped = adopt(base="gs://bk", has_cold=lambda p: "2026-09-02/index/g1/path-index-bysize" not in p)
    # Two rows each: 'ab' + 'cd' + '[1]' + 64.
    assert adopted == [
        Entry("2026-09-01", "bysize", "g1", 2, 2 * 71, TODAY),
        Entry("2026-09-01", "path", "g1", 2, 2 * 71, TODAY),
        Entry("2026-09-02", "path", "g1", 2, 2 * 71, TODAY),
    ]
    assert skipped == [("2026-09-02", "bysize", "gs://bk/l2/2026-09-02/index/g1/path-index-bysize.groups.parquet")]
    assert d1.entries() == [
        ("2026-09-01", "bysize", "g1", "hot", 2, TODAY),
        ("2026-09-01", "path", "g1", "hot", 2, TODAY),
        ("2026-09-02", "path", "g1", "hot", 2, TODAY),
        ("2026-09-03", "path", "g1", "hot", 1, 7),
    ]
    assert adopt(base="gs://bk", has_cold=lambda _p: False) == ([], [("2026-09-02", "bysize", "gs://bk/l2/2026-09-02/index/g1/path-index-bysize.groups.parquet")])


def test_footer_evict_cli_one_pass_and_waiting(d1, monkeypatch):
    for date, day in [("s1", 10), ("s2", 11)]:
        _hot(d1.con, date, 2, day)
    logged: list[str] = []
    monkeypatch.setattr(C, "err", lambda *a: logged.append(" ".join(map(str, a))))
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    r = CliRunner().invoke(main, ["footer-evict", "-B", "2", "-W"])
    assert r.exit_code == 0, r.output
    assert logged == [
        "footer-evict: 2 hot entries, 4 rows, 0 bytes (budget 2 rows)",
        "footer-evict: demoted s1 [path] gen g1 — 2 rows, last read day 10",
    ]
    assert [slept, [e[3] for e in d1.entries()]] == [[], ["evicting", "hot"]]
    # Waiting: the second pass (the clock moved past the grace) deletes it.
    _hot(d1.con, "s3", 1, 12)
    clock = iter([NOW + 1, NOW + 1 + F.EVICT_GRACE_S])
    monkeypatch.setattr(F, "_now", lambda: next(clock))
    logged.clear()
    r = CliRunner().invoke(main, ["footer-evict", "-B", "1"])
    assert r.exit_code == 0, r.output
    assert logged == [
        "footer-evict: 2 hot entries, 3 rows, 0 bytes (budget 1 rows)",
        "footer-evict: demoted s2 [path] gen g1 — 2 rows, last read day 11",
        "footer-evict: waiting 120s for readers' handles before deleting the demoted rows",
        "footer-evict: 1 hot entries, 1 rows, 0 bytes (budget 1 rows)",
        "footer-evict: deleted s1 [path] gen g1 — 2 rows",
        "footer-evict: deleted s2 [path] gen g1 — 2 rows",
    ]
    assert [slept, d1.entries(), d1.counts()] == [[120], [("s3", "path", "g1", "hot", 1, 12)], [("s3", "path", "g1", 1)]]
