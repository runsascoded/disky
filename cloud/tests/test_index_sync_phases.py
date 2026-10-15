"""`dt-cloud index-sync -r` / `-p`: the sync's two halves run apart. `-r`
stages a generation's row groups (invisible: readers join on the pointer's
gen), `-p` flips the pointers once every variant's rows match its footer, and
the default still does both, in that order. Against a real SQLite carrying the
branch's D1 schema (`site/schema.sql`)."""
import json
import sqlite3
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from dt_cloud import cli as C
from dt_cloud import index_footer as F
from dt_cloud.cli import main

DATE = "2026-10-10T2022"
OLD, NEW = "G1", "G2"
OLD_KEY, NEW_KEY = f"listing/{DATE}/index/{OLD}", f"listing/{DATE}/index/{NEW}"


def _write_sort(path: Path, n_rows: int, group_rows: int) -> None:
    t = pa.table({
        "path": [f"b/d{i:03d}" for i in range(n_rows)],
        "size": pa.array(range(n_rows, 0, -1), pa.int64()),
        "depth": pa.array([1] * n_rows, pa.int64()),
        "kind": ["dir"] * n_rows,
    }).replace_schema_metadata({b"tier": b"path", b"sort": b"depth,path"})
    pq.write_table(t, path, row_group_size=group_rows, compression="snappy")


class D1:
    """The fake D1: every statement runs on SQLite and is recorded."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.sent: list[str] = []

    def __call__(self, sql, acct, tok, db_id):
        self.sent.append(sql)
        cur = self.con.execute(sql)
        rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()] if cur.description else []
        self.con.commit()
        return rows

    def writes(self) -> list[str]:
        return [s for s in self.sent if not s.startswith("SELECT")]

    def pointers(self) -> list[tuple]:
        return self.con.execute("SELECT date, variant, gen, dir FROM index_schema ORDER BY variant").fetchall()

    def groups(self) -> list[tuple]:
        return self.con.execute("SELECT variant, gen, COUNT(*) FROM index_row_groups GROUP BY variant, gen ORDER BY variant, gen").fetchall()

    def visible(self) -> list[tuple]:
        """What a reader can reach: every `index.ts` row-group query names
        `gen = <the pointer's gen>` for its (date, variant)."""
        return self.con.execute(
            "SELECT r.variant, r.gen, COUNT(*) FROM index_row_groups r JOIN index_schema s "
            "ON s.date = r.date AND s.variant = r.variant AND s.gen = r.gen GROUP BY r.variant, r.gen ORDER BY r.variant"
        ).fetchall()


@pytest.fixture
def d1(monkeypatch, d1_db) -> D1:
    db = D1(d1_db)
    monkeypatch.setattr(F, "_d1_query", db)
    monkeypatch.setattr(F, "_creds", lambda: ("tok", "acct"))
    monkeypatch.setattr(F, "_now", lambda: 1_790_000_000)
    return db


@pytest.fixture
def logged(monkeypatch) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr(C, "err", lambda *a: lines.append(" ".join(map(str, a))))
    return lines


@pytest.fixture
def gens(tmp_path: Path, d1: D1) -> tuple[Path, Path]:
    """The previous generation `G1` synced (and pointed at: 2 + 1 groups);
    the new one, `G2`, cut on disk with 3 `path` groups and 2 `bysize`."""
    old, new = tmp_path / OLD, tmp_path / NEW
    for d, (np, nb) in ((old, (2, 1)), (new, (3, 2))):
        d.mkdir()
        _write_sort(d / "path-index.parquet", 4 * np, 4)
        _write_sort(d / "path-index-bysize.parquet", 4 * nb, 4)
    r = CliRunner().invoke(main, ["index-sync", "-d", str(old), "-g", OLD, "-k", OLD_KEY, DATE])
    assert r.exit_code == 0, r.output
    d1.sent.clear()
    return old, new


def sync(new: Path, *flags: str):
    return CliRunner().invoke(main, ["index-sync", *flags, "-d", str(new), "-g", NEW, "-k", NEW_KEY, DATE])


SKIPPED = (
    f"index-sync: {DATE} gen {NEW} @ {NEW_KEY} — skipped 11 absent variant(s): user, bysize-user, "
    "age-pyramid-1h, age-pyramid-3h, age-pyramid-6h, age-pyramid-12h, age-pyramid-1d, age-pyramid-2d, age-pyramid-4d, age-pyramid-8d, "
    "over-time"
)
BOTH_POINTED_OLD = [(DATE, "bysize", OLD, OLD_KEY), (DATE, "path", OLD, OLD_KEY)]
BOTH_POINTED_NEW = [(DATE, "bysize", NEW, NEW_KEY), (DATE, "path", NEW, NEW_KEY)]


def test_rows_only_stages_an_invisible_generation(gens, d1, logged):
    _, new = gens
    r = sync(new, "-r")
    assert r.exit_code == 0, r.output
    assert logged == [
        f"index-sync: {DATE} [path] gen {NEW} @ {NEW_KEY} — 3 row groups (remote), pointer not flipped",
        f"index-sync: {DATE} [bysize] gen {NEW} @ {NEW_KEY} — 2 row groups (remote), pointer not flipped",
        SKIPPED,
    ]
    # No pointer written: the site still serves G1, G2's rows sit beside it unseen.
    assert [s for s in d1.writes() if "index_schema" in s and not s.startswith(("DELETE FROM index_row_groups", "DELETE FROM footer_cache"))] == []
    assert d1.pointers() == BOTH_POINTED_OLD
    assert d1.groups() == [("bysize", OLD, 1), ("bysize", NEW, 2), ("path", OLD, 2), ("path", NEW, 3)]
    # The footer LRU's entries: each complete set `hot` (the reader only consults the pointer's).
    assert d1.con.execute("SELECT variant, gen, state, rows FROM footer_cache ORDER BY variant, gen").fetchall() == [
        ("bysize", OLD, "hot", 1), ("bysize", NEW, "hot", 2), ("path", OLD, "hot", 2), ("path", NEW, "hot", 3),
    ]
    assert d1.visible() == [("bysize", OLD, 1), ("path", OLD, 2)]
    # The cold footer files land with the rows.
    assert sorted(p.name for p in new.iterdir()) == [
        "path-index-bysize.groups.json", "path-index-bysize.groups.parquet", "path-index-bysize.parquet",
        "path-index.groups.json", "path-index.groups.parquet", "path-index.parquet",
    ]
    # Re-running is clean: same rows, nothing doubled.
    logged.clear()
    assert sync(new, "-r").exit_code == 0
    assert d1.groups() == [("bysize", OLD, 1), ("bysize", NEW, 2), ("path", OLD, 2), ("path", NEW, 3)]
    assert d1.pointers() == BOTH_POINTED_OLD


def test_pointer_only_flips_complete_rows_then_is_a_noop(gens, d1, logged):
    _, new = gens
    assert sync(new, "-r").exit_code == 0
    logged.clear()
    d1.sent.clear()
    r = sync(new, "-p")
    assert r.exit_code == 0, r.output
    assert logged == [
        f"index-sync: {DATE} [path] gen {NEW} @ {NEW_KEY} — pointer flipped (3 row groups)",
        f"index-sync: {DATE} [bysize] gen {NEW} @ {NEW_KEY} — pointer flipped (2 row groups)",
        SKIPPED,
    ]
    # Every check before any flip; only the pointers (+ the v2 `path` retirements) are written, no rows.
    assert [s.split(" (")[0] for s in d1.sent] == [
        f"SELECT gen, dir FROM index_schema WHERE date='{DATE}' AND variant='path';",
        f"SELECT COUNT(*) AS n, MIN(rg) AS lo, MAX(rg) AS hi FROM index_row_groups WHERE date='{DATE}' AND variant='path' AND gen='{NEW}';",
        f"SELECT gen, dir FROM index_schema WHERE date='{DATE}' AND variant='bysize';",
        f"SELECT COUNT(*) AS n, MIN(rg) AS lo, MAX(rg) AS hi FROM index_row_groups WHERE date='{DATE}' AND variant='bysize' AND gen='{NEW}';",
        "INSERT INTO index_schema",
        f"DELETE FROM index_row_groups WHERE date='{DATE}' AND variant LIKE 'coarse%';",
        f"DELETE FROM index_row_groups WHERE date='{DATE}' AND variant='user' AND gen IN",
        f"DELETE FROM index_schema WHERE date='{DATE}' AND variant LIKE 'coarse%';",
        f"DELETE FROM index_schema WHERE date='{DATE}' AND variant='user' AND version < 2;",
        "INSERT INTO index_schema",
    ]
    assert d1.pointers() == BOTH_POINTED_NEW
    assert d1.visible() == [("bysize", NEW, 2), ("path", NEW, 3)]
    # Again: already pointed, nothing written.
    logged.clear()
    d1.sent.clear()
    assert sync(new, "-p").exit_code == 0
    assert logged == [
        f"index-sync: {DATE} [path] gen {NEW} @ {NEW_KEY} — already pointed (3 row groups)",
        f"index-sync: {DATE} [bysize] gen {NEW} @ {NEW_KEY} — already pointed (2 row groups)",
        SKIPPED,
    ]
    assert d1.writes() == []
    assert d1.pointers() == BOTH_POINTED_NEW


@pytest.mark.parametrize("damage, found", [
    (None, "0 row groups (rg None..None)"),  # `-r` never ran
    (f"DELETE FROM index_row_groups WHERE variant='bysize' AND gen='{NEW}' AND rg = 1", "1 row groups (rg 0..0)"),  # died part-way
    (f"UPDATE index_row_groups SET rg = 5 WHERE variant='bysize' AND gen='{NEW}' AND rg = 1", "2 row groups (rg 0..5)"),  # wrong groups
])
def test_pointer_only_refuses_incomplete_rows(gens, d1, logged, damage, found):
    """A variant whose rows don't match its footer flips nothing — not even
    the complete `path` checked before it."""
    _, new = gens
    if damage is None:
        F.sync_d1(DATE, str(new / "path-index.parquet"), variant="path", gen=NEW, key=NEW_KEY, rows_only=True)
    else:
        assert sync(new, "-r").exit_code == 0
        d1.con.execute(damage)
    logged.clear()
    d1.sent.clear()
    r = sync(new, "-p")
    assert r.exit_code == 1
    assert logged == [
        f"index-sync: refusing to flip any pointer — {DATE} [bysize] gen {NEW}: D1 holds {found}, the footer 2 — "
        "not flipping; re-run the rows phase (`index-sync -r`)",
    ]
    assert d1.writes() == []
    assert d1.pointers() == BOTH_POINTED_OLD


def test_default_is_rows_then_pointer_unchanged(gens, d1, logged):
    """No flag: per variant, the gc sweep, the rows, the flip — the same
    statements `-r` then `-p` write, in today's order, to the same end state."""
    _, new = gens
    r = sync(new)
    assert r.exit_code == 0, r.output
    assert logged == [
        f"index-sync: {DATE} [path] gen {NEW} @ {NEW_KEY} — 3 row groups (remote)",
        f"index-sync: {DATE} [bysize] gen {NEW} @ {NEW_KEY} — 2 row groups (remote)",
        SKIPPED,
    ]
    default_sent = list(d1.sent)
    # Per variant: the LRU table probe, the swept generations' entries then rows, the claim, the rows, `hot`, the flip.
    rows_phase = [
        "SELECT name FROM sqlite_master", "DELETE FROM footer_cache", "DELETE FROM index_row_groups", "INSERT INTO footer_cache",
        "INSERT OR REPLACE INTO index_row_groups", "UPDATE footer_cache SET state = 'hot', rows = 3, bytes = 427, filled_ts = 1790000000, last_access_day = 20717",
    ]
    assert [s.split(" (")[0].split(" WHERE")[0] for s in default_sent] == [
        *rows_phase, "INSERT INTO index_schema",
        *["DELETE FROM index_row_groups"] * 2, *["DELETE FROM index_schema"] * 2,
        *rows_phase[:-1], "UPDATE footer_cache SET state = 'hot', rows = 2, bytes = 282, filled_ts = 1790000000, last_access_day = 20717",
        "INSERT INTO index_schema",
    ]
    assert d1.pointers() == BOTH_POINTED_NEW
    assert d1.groups() == [("bysize", OLD, 1), ("bysize", NEW, 2), ("path", OLD, 2), ("path", NEW, 3)]
    # An entry's `bytes` is its rows' text columns + 64 each, as the reader's fill counts them.
    assert d1.con.execute(
        "SELECT variant, SUM(LENGTH(p_min) + LENGTH(p_max) + COALESCE(LENGTH(u_min), 0) + COALESCE(LENGTH(u_max), 0) + LENGTH(rg_json) + 64) "
        f"FROM index_row_groups WHERE gen = '{NEW}' GROUP BY variant ORDER BY variant"
    ).fetchall() == [("bysize", 282), ("path", 427)]
    end = d1.con.execute("SELECT * FROM index_row_groups ORDER BY 1, 2, 3, 4").fetchall()

    # Back to G1 pointed, G2 unsynced; then the split run writes the default's statements, to the same end state.
    d1.con.execute(f"DELETE FROM index_row_groups WHERE gen='{NEW}'")
    d1.con.execute(f"UPDATE index_schema SET gen='{OLD}', dir='{OLD_KEY}'")
    d1.sent.clear()
    assert sync(new, "-r").exit_code == 0
    assert sync(new, "-p").exit_code == 0
    by_kind = lambda ss: sorted(s for s in ss if not s.startswith("SELECT"))  # noqa: E731
    assert by_kind(d1.sent) == by_kind(default_sent)
    assert d1.pointers() == BOTH_POINTED_NEW
    assert d1.con.execute("SELECT * FROM index_row_groups ORDER BY 1, 2, 3, 4").fetchall() == end


def test_rows_and_pointer_flags_exclude_each_other_and_local(tmp_path, d1):
    for flags in (["-r", "-p"], ["-r", "-L"], ["-p", "-L"]):
        r = CliRunner().invoke(main, ["index-sync", *flags, "-d", str(tmp_path), "-g", NEW, DATE])
        assert r.exit_code == 2, flags
    assert d1.sent == []


# ── `index_schema.started`: `-m <meta.json>` and the `index-started` backfill ──

STARTED = "2026-10-10T20:22:05.123Z"


def _meta(path: Path, started: str | None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"asof": path.parent.name, **({} if started is None else {"started": started})}))
    return path


def starts(d1: D1) -> list[tuple]:
    return d1.con.execute("SELECT variant, gen, started FROM index_schema ORDER BY variant").fetchall()


def test_meta_records_the_start_on_every_pointer_and_a_bare_resync_keeps_it(gens, d1, logged, tmp_path):
    _, new = gens
    assert starts(d1) == [("bysize", OLD, None), ("path", OLD, None)]
    meta = _meta(tmp_path / "snap" / DATE / "meta.json", STARTED)
    assert sync(new, "-m", str(meta)).exit_code == 0
    assert starts(d1) == [("bysize", NEW, STARTED), ("path", NEW, STARTED)]
    # A re-sync naming no meta (or a meta without `started`) flips the pointers and keeps the start.
    for flags in ([], ["-m", str(_meta(tmp_path / "bare" / DATE / "meta.json", None))]):
        assert sync(new, *flags).exit_code == 0
        assert starts(d1) == [("bysize", NEW, STARTED), ("path", NEW, STARTED)]
    assert d1.pointers() == BOTH_POINTED_NEW


def test_pointer_only_writes_a_new_start_onto_an_already_pointed_generation(gens, d1, logged, tmp_path):
    _, new = gens
    assert sync(new).exit_code == 0
    meta = _meta(tmp_path / "snap" / DATE / "meta.json", STARTED)
    logged.clear()
    assert sync(new, "-p", "-m", str(meta)).exit_code == 0
    assert logged == [
        f"index-sync: {DATE} [path] gen {NEW} @ {NEW_KEY} — pointer flipped (3 row groups)",
        f"index-sync: {DATE} [bysize] gen {NEW} @ {NEW_KEY} — pointer flipped (2 row groups)",
        SKIPPED,
    ]
    assert starts(d1) == [("bysize", NEW, STARTED), ("path", NEW, STARTED)]
    logged.clear()
    d1.sent.clear()
    assert sync(new, "-p", "-m", str(meta)).exit_code == 0
    assert logged == [
        f"index-sync: {DATE} [path] gen {NEW} @ {NEW_KEY} — already pointed (3 row groups)",
        f"index-sync: {DATE} [bysize] gen {NEW} @ {NEW_KEY} — already pointed (2 row groups)",
        SKIPPED,
    ]
    assert d1.writes() == []


def test_meta_with_rows_only_is_a_usage_error(tmp_path, d1):
    meta = _meta(tmp_path / "snap" / DATE / "meta.json", STARTED)
    r = CliRunner().invoke(main, ["index-sync", "-r", "-m", str(meta), "-d", str(tmp_path), "-g", NEW, "-k", NEW_KEY, DATE])
    assert r.exit_code == 2
    assert d1.sent == []


def test_index_started_prints_the_backfill_for_each_meta_with_a_start(tmp_path, d1, logged):
    snaps = tmp_path / "snapshots" / "nas"
    _meta(snaps / "2026-10-09" / "meta.json", "2026-10-09T04:30:12.345Z")
    _meta(snaps / "2026-10-10" / "meta.json", None)
    _meta(snaps / "2026-10-11T1000" / "meta.json", "2026-10-11T10:00:01.000Z")
    _meta(snaps / "rules" / "meta.json", "2026-10-11T10:00:01.000Z")  # not a scan dir
    r = CliRunner().invoke(main, ["index-started", "-s", "nas", str(snaps)])
    assert r.exit_code == 0, r.output
    assert r.output.splitlines() == [
        "UPDATE index_schema SET started = '2026-10-09T04:30:12.345Z' WHERE store = 'nas' AND date = '2026-10-09' AND variant = 'nas:path' AND started IS NULL;",
        "UPDATE index_schema SET started = '2026-10-11T10:00:01.000Z' WHERE store = 'nas' AND date = '2026-10-11T1000' AND variant = 'nas:path' AND started IS NULL;",
    ]
    assert logged == [f"index-started: 3 scans under {snaps}, 2 with a start; none for 1: 2026-10-10"]
    # Applied: only the store's `path` pointers without a start take one.
    d1.con.executescript("""
        INSERT INTO index_schema (store, date, variant, version, schema_json, started) VALUES
          ('nas', '2026-10-09', 'nas:path', 2, '[]', NULL),
          ('nas', '2026-10-09', 'nas:bysize', 2, '[]', NULL),
          ('nas', '2026-10-11T1000', 'nas:path', 2, '[]', 'kept'),
          ('primary', '2026-10-09', 'path', 2, '[]', NULL);
    """)
    d1.con.executescript(r.output)
    assert d1.con.execute("SELECT store, date, variant, started FROM index_schema ORDER BY store, date, variant").fetchall() == [
        ("nas", "2026-10-09", "nas:bysize", None),
        ("nas", "2026-10-09", "nas:path", "2026-10-09T04:30:12.345Z"),
        ("nas", "2026-10-11T1000", "nas:path", "kept"),
        ("primary", "2026-10-09", "path", None),
    ]


def test_snapshots_dir_starts_this_scan_and_fills_earlier_date_only_ones(gens, d1, logged, tmp_path):
    _, new = gens
    snaps = tmp_path / "snapshots" / "laptop"
    _meta(snaps / DATE / "meta.json", STARTED)
    _meta(snaps / "2026-10-08" / "meta.json", "2026-10-08T22:13:50.123Z")
    _meta(snaps / "2026-10-09" / "meta.json", None)
    d1.con.executescript("""
        INSERT INTO index_schema (store, date, variant, version, schema_json, started) VALUES
          ('primary', '2026-10-07', 'path', 2, '[]', NULL),
          ('primary', '2026-10-08', 'path', 2, '[]', NULL),
          ('primary', '2026-10-08', 'bysize', 2, '[]', NULL),
          ('primary', '2026-10-09', 'path', 2, '[]', NULL),
          ('primary', '2026-10-06', 'path', 2, '[]', 'kept'),
          ('nas', '2026-10-08', 'nas:path', 2, '[]', NULL);
    """)
    logged.clear()
    assert sync(new, "-M", str(snaps)).exit_code == 0
    assert logged[-1] == "index-sync: filled `started` on 1 earlier scan(s): 2026-10-08"
    assert d1.con.execute("SELECT store, date, variant, started FROM index_schema ORDER BY store, date, variant").fetchall() == [
        ("nas", "2026-10-08", "nas:path", None),
        ("primary", "2026-10-06", "path", "kept"),
        ("primary", "2026-10-07", "path", None),  # no meta
        ("primary", "2026-10-08", "bysize", None),  # only the `path` pointer carries a backfilled start
        ("primary", "2026-10-08", "path", "2026-10-08T22:13:50.123Z"),
        ("primary", "2026-10-09", "path", None),  # its meta has no `started`
        ("primary", DATE, "bysize", STARTED),
        ("primary", DATE, "path", STARTED),
    ]
    # Filled: the next sync reads D1 once and writes no start.
    d1.sent.clear()
    logged.clear()
    assert sync(new, "-M", str(snaps)).exit_code == 0
    assert [s for s in d1.sent if "started IS NULL" in s] == [
        "SELECT date FROM index_schema WHERE store = 'primary' AND variant = 'path' AND started IS NULL;",
    ]
    assert not any(line.startswith("index-sync: filled") for line in logged)


def test_snapshots_dir_with_rows_only_or_local_is_a_usage_error(tmp_path, d1):
    for flag in ("-r", "-L"):
        r = CliRunner().invoke(main, ["index-sync", flag, "-M", str(tmp_path), "-d", str(tmp_path), "-g", NEW, "-k", NEW_KEY, DATE])
        assert r.exit_code == 2, (flag, r.output)
    assert d1.sent == []
