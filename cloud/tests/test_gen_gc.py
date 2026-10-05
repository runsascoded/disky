"""`index-gc -F` (`dt_cloud.gen_gc`): delete the files of index generations no
D1 pointer names — only under scans that have a `path` pointer, never a
pointed dir, never one inside the grace period; dry runs delete nothing."""
import pytest
from click.testing import CliRunner

from dt_cloud import cli as C
from dt_cloud import gen_gc as G
from dt_cloud import index_footer as F
from dt_cloud.cli import main

DAY = 86400.0
NOW = 1_790_000_000.0
OLD = NOW - 10 * DAY
NEW = NOW - 3600.0  # an hour ago: inside the 2-day grace period

S1, S2, S3, S4 = "2026-09-16T0001", "2026-09-17T0001", "2026-09-18T0001", "2026-10-02T1201"

POINTERS = [
    (S1, "path", f"cw-l2/{S1}/index/g2"),
    (S1, "bysize", f"cw-l2/{S1}/index/g2"),
    (S2, "path", f"cw-l2/{S2}/index/g3"),
    (S2, "over-time", f"cw-l2/{S2}/index/g4/"),  # an over-time group sealed under S2 (trailing slash tolerated)
    (S4, "path", f"cw-l2/{S4}/index/g6"),
    ("2026-08-01", "path", "listing/2026-08-01"),  # the base's legacy layout: not a generation root
    (S1, "meta:path", f"meta-l2/{S1}/index/m1"),  # a secondary store's pointer
]


class FakeStore:
    def __init__(self, name: str, objs: dict[str, tuple[int, float]]):
        self.name = name
        self.objs = dict(objs)
        self.deleted: list[list[str]] = []

    def ls(self, prefix: str) -> list[G.Blob]:
        return [G.Blob(key=k, size=s, mtime=m) for k, (s, m) in sorted(self.objs.items()) if k.startswith(prefix)]

    def delete(self, keys: list[str]) -> None:
        self.deleted.append(keys)
        for k in keys:
            del self.objs[k]


def objects() -> dict[str, tuple[int, float]]:
    return {
        # S1: g1 superseded by the pointed g2; a loose pre-generation file under index/
        f"cw-l2/{S1}/index/g1/path-index.parquet": (100, OLD),
        f"cw-l2/{S1}/index/g1/path-index.groups.json": (5, OLD),
        f"cw-l2/{S1}/index/g2/path-index.parquet": (110, OLD),
        f"cw-l2/{S1}/index/path-index.parquet": (7, OLD),
        f"cw-l2/{S1}/marin-us-east-02a.parquet": (1000, OLD),
        # S2: path gen + over-time gen, both pointed
        f"cw-l2/{S2}/index/g3/path-index.parquet": (120, OLD),
        f"cw-l2/{S2}/index/g4/over-time.parquet": (30, OLD),
        # S3: no pointer at all — its only generation is never touched
        f"cw-l2/{S3}/index/g5/path-index.parquet": (130, OLD),
        # S4: an unpointed generation still being written (a reindex in flight)
        f"cw-l2/{S4}/index/g6/path-index.parquet": (140, OLD),
        f"cw-l2/{S4}/index/g7/path-index.parquet": (150, OLD),
        f"cw-l2/{S4}/index/g7/age-pyramid-1d.parquet": (15, NEW),
        # the secondary store's superseded generation
        f"meta-l2/{S1}/index/m0/path-index.parquet": (9, OLD),
        f"meta-l2/{S1}/index/m1/path-index.parquet": (9, OLD),
    }


def sweep(stores, *, dry_run, ptrs=POINTERS, reread=None, **kw):
    lines: list[str] = []
    p = G.sweep(ptrs, stores, now=NOW, min_age=2 * DAY, dry_run=dry_run, reread=reread, log=lines.append, **kw)
    return p, lines


def test_parse_age():
    assert [G.parse_age(s) for s in ("0", "90s", "90m", "36h", "2d", "1w", "1.5d")] == [0, 90, 5400, 129600, 172800, 604800, 129600]
    with pytest.raises(ValueError):
        G.parse_age("2")


def test_roots_are_the_path_pointers_parents():
    assert G.roots(POINTERS) == {f"cw-l2/{S1}/index/": S1, f"cw-l2/{S2}/index/": S2, f"cw-l2/{S4}/index/": S4}
    assert G.roots(POINTERS, dates=[S2]) == {f"cw-l2/{S2}/index/": S2}
    assert G.roots(POINTERS, path_variant="meta:path") == {f"meta-l2/{S1}/index/": S1}


def test_plan_deletes_only_unpointed_old_generations_of_pointed_scans():
    gcs = FakeStore("gs://data", objects())
    p = G.plan(POINTERS, [gcs], now=NOW, min_age=2 * DAY)
    assert [(g.store, g.scan, g.dir, g.objects, g.bytes) for g in p.doomed] == [
        ("gs://data", S1, f"cw-l2/{S1}/index/g1", 2, 105),
    ]
    assert p.doomed[0].keys == (f"cw-l2/{S1}/index/g1/path-index.groups.json", f"cw-l2/{S1}/index/g1/path-index.parquet")
    assert [(g.scan, g.dir, g.bytes, g.newest) for g in p.young] == [(S4, f"cw-l2/{S4}/index/g7", 165, NEW)]


def test_dry_run_deletes_nothing_and_logs_what_would_go():
    gcs, r2 = FakeStore("gs://data", objects()), FakeStore("r2://serve", objects())
    p, lines = sweep([gcs, r2], dry_run=True)
    assert [g.dir for g in p.doomed] == [f"cw-l2/{S1}/index/g1"] * 2
    assert (gcs.deleted, r2.deleted, gcs.objs, r2.objs) == ([], [], objects(), objects())
    assert lines == [
        f"index-gc: would delete gs://data/cw-l2/{S1}/index/g1/ (scan {S1}, gen g1): 2 objects, 105 B (0.0 GB)",
        f"index-gc: would delete r2://serve/cw-l2/{S1}/index/g1/ (scan {S1}, gen g1): 2 objects, 105 B (0.0 GB)",
        f"index-gc: kept gs://data/cw-l2/{S4}/index/g7/ (scan {S4}, gen g7): unpointed but younger than the grace period, 165 B (0.0 GB)",
        f"index-gc: kept r2://serve/cw-l2/{S4}/index/g7/ (scan {S4}, gen g7): unpointed but younger than the grace period, 165 B (0.0 GB)",
        "index-gc: gs://data: would delete 1 generations in 1 scans, 2 objects, 105 B (0.0 GB); kept 1 too young",
        "index-gc: r2://serve: would delete 1 generations in 1 scans, 2 objects, 105 B (0.0 GB); kept 1 too young",
    ]


def test_real_run_deletes_in_every_store_and_is_idempotent():
    gcs, r2 = FakeStore("gs://data", objects()), FakeStore("r2://serve", objects())
    sweep([gcs, r2], dry_run=False, reread=lambda: POINTERS)
    doomed = [f"cw-l2/{S1}/index/g1/path-index.groups.json", f"cw-l2/{S1}/index/g1/path-index.parquet"]
    assert (gcs.deleted, r2.deleted) == ([doomed], [doomed])
    left = {k: v for k, v in objects().items() if k not in doomed}
    assert (gcs.objs, r2.objs) == (left, left)
    p, lines = sweep([gcs, r2], dry_run=False, reread=lambda: POINTERS)
    assert (p.doomed, gcs.deleted, r2.deleted) == ([], [doomed], [doomed])
    assert lines[-2:] == [
        "index-gc: gs://data: deleted 0 generations in 0 scans, 0 objects, 0 B (0.0 GB); kept 1 too young",
        "index-gc: r2://serve: deleted 0 generations in 0 scans, 0 objects, 0 B (0.0 GB); kept 1 too young",
    ]


def test_a_pointer_flipped_during_the_listing_keeps_its_target():
    gcs = FakeStore("gs://data", objects())
    flipped = POINTERS + [(S1, "bysize", f"cw-l2/{S1}/index/g1")]
    p, lines = sweep([gcs], dry_run=False, reread=lambda: flipped)
    assert (p.doomed, gcs.deleted) == ([], [])
    assert lines[0] == f"index-gc: kept gs://data/cw-l2/{S1}/index/g1/ — pointed since the listing"


def test_dates_and_store_scope_the_sweep():
    gcs = FakeStore("gs://data", objects())
    assert G.plan(POINTERS, [gcs], now=NOW, min_age=2 * DAY, dates=[S2, S4]).doomed == []
    meta = G.plan(POINTERS, [gcs], now=NOW, min_age=2 * DAY, path_variant="meta:path").doomed
    assert [(g.scan, g.dir) for g in meta] == [(S1, f"meta-l2/{S1}/index/m0")]
    # a zero grace period takes the in-flight generation too
    assert [g.dir for g in G.plan(POINTERS, [gcs], now=NOW, min_age=0).doomed] == [f"cw-l2/{S1}/index/g1", f"cw-l2/{S4}/index/g7"]


def test_open_store_rejects_unknown_targets():
    with pytest.raises(ValueError):
        G.open_store("s3://x")


def test_cli_dry_run_counts_rows_and_lists_files(monkeypatch):
    """`index-gc -n -F …`: the D1 row sweep is a COUNT, the file sweep a plan;
    nothing is deleted anywhere."""
    sent: list[str] = []

    def fake_query(sql, acct, tok, db_id):
        sent.append(sql)
        if sql.startswith("SELECT COUNT(*)"):
            return [{"n": 4}]
        if sql.startswith("SELECT date, variant, dir FROM index_schema"):
            return [{"date": d, "variant": v, "dir": k} for d, v, k in POINTERS]
        raise AssertionError(f"unexpected D1 statement: {sql}")

    monkeypatch.setattr(F, "_creds", lambda: ("tok", "acct"))
    monkeypatch.setattr(F, "_d1_query", fake_query)
    gcs = FakeStore("gs://data", objects())
    monkeypatch.setattr(G, "open_store", lambda t: {"gs://data": gcs}[t])
    monkeypatch.setattr(G, "err", lambda *a: logged.append(" ".join(map(str, a))))
    logged: list[str] = []
    monkeypatch.setattr(C, "err", lambda *a: logged.append(" ".join(map(str, a))))
    import time
    monkeypatch.setattr(time, "time", lambda: NOW)
    r = CliRunner().invoke(main, ["index-gc", "-n", "-F", "gs://data", S1])
    assert r.exit_code == 0, r.output
    assert sent == [
        f"SELECT COUNT(*) AS n FROM index_row_groups WHERE date = '{S1}' AND gen <> COALESCE("
        "(SELECT s.gen FROM index_schema s WHERE s.date = index_row_groups.date AND s.variant = index_row_groups.variant), '');",
        "SELECT date, variant, dir FROM index_schema ORDER BY date, variant;",
    ]
    assert logged == [
        f"index-gc: {S1} — 4 stale row groups would be deleted",
        f"index-gc: would delete gs://data/cw-l2/{S1}/index/g1/ (scan {S1}, gen g1): 2 objects, 105 B (0.0 GB)",
        "index-gc: gs://data: would delete 1 generations in 1 scans, 2 objects, 105 B (0.0 GB); kept 0 too young",
    ]
    assert gcs.deleted == []


def test_cli_dry_run_refuses_retention():
    r = CliRunner().invoke(main, ["index-gc", "-n", "-r", "4"])
    assert (r.exit_code, r.output.splitlines()[-1]) == (2, "Error: -n covers the row sweep and -F, not -r")
