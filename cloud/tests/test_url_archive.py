"""An archive on R2 (or any URL `disk_tree.blobfs` resolves), no GCS: the
archive-reading `dt-cloud` commands take a URL root wherever they took a
`/gcs/…` mount or a `gs://` bucket. Each runs twice, over a local dir and over
`r2://` (the `fake_r2` fixture: blobfs's real `r2://` resolution, its s3fs
swapped for a local dir), and must do exactly the same thing."""
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from dt_cloud import cli as C
from dt_cloud import index_footer as F
from dt_cloud import overtime as OT
from dt_cloud.cli import main

KINDS = ["dir", "r2"]


@pytest.fixture(params=KINDS)
def archive(request, tmp_path) -> tuple[str, Path]:
    """``(root, local)``: the root a command is given (a dir, or
    `r2://archive`) and the local dir its objects live in."""
    if request.param == "dir":
        local = tmp_path / "archive"
        local.mkdir()
        return str(local), local
    local = request.getfixturevalue("fake_r2") / "archive"
    local.mkdir()
    return "r2://archive", local


@pytest.fixture
def logged(monkeypatch) -> list[str]:
    """`err` binds stderr at import time: record what the command logs instead."""
    lines: list[str] = []
    monkeypatch.setattr(C, "err", lambda *a: lines.append(" ".join(map(str, a))))
    return lines


def files(local: Path) -> list[str]:
    return sorted(str(p.relative_to(local)) for p in local.rglob("*") if p.is_file())


# ── index-sync ──────────────────────────────────────────────────────────────

DATE, GEN = "2026-09-30", "G1"
KEY = f"l2/{DATE}/index/{GEN}"
STARTED = "2026-09-30T00:01:02Z"


def _write_sort(path: Path) -> None:
    """A store `path` sort (`tier` metadata, `size`/`kind`), two row groups."""
    t = pa.table({
        "path": ["b", "b/x.bin", "b/y", "b/y/z.bin"],
        "size": pa.array([30, 20, 10, 10], pa.int64()),
        "depth": pa.array([1, 2, 2, 3], pa.int64()),
        "kind": ["dir", "file", "dir", "file"],
    }).replace_schema_metadata({b"tier": b"path", b"sort": b"depth,path"})
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(t, path, row_group_size=2, compression="snappy")


@pytest.mark.parametrize("form", ["dir", "base"])
def test_index_sync_reads_and_writes_the_generation_in_place(monkeypatch, archive, logged, form):
    """`index-sync -d <root>/<key>` (or `-b <root> -k <key>`) with `-M
    <root>/snapshots`: the footer is read from the archive, the cold footer +
    `.groups.json` land beside the sort there, and the pointer records the
    meta's `started` — the same D1 statements over a dir and over `r2://`."""
    root, local = archive
    _write_sort(local / KEY / "path-index.parquet")
    (local / "snapshots" / DATE).mkdir(parents=True)
    (local / "snapshots" / DATE / "meta.json").write_text(json.dumps({"started": STARTED}))
    monkeypatch.setattr(F, "_creds", lambda: ("tok", "acct"))
    sent: list[str] = []
    monkeypatch.setattr(F, "_d1_query", lambda sql, acct, tok, db_id: sent.append(sql) or [])
    monkeypatch.delenv("DATA_BUCKET")
    where = ["-d", f"{root}/{KEY}"] if form == "dir" else ["-b", root]
    r = CliRunner().invoke(main, ["index-sync", *where, "-g", GEN, "-k", KEY, "-v", "path", "-M", f"{root}/snapshots", DATE])
    assert r.exit_code == 0, r.output
    assert logged == [f"index-sync: {DATE} [path] gen {GEN} @ {KEY} — 2 row groups (remote)"]
    assert files(local) == [
        f"{KEY}/path-index.groups.json",
        f"{KEY}/path-index.groups.parquet",
        f"{KEY}/path-index.parquet",
        f"snapshots/{DATE}/meta.json",
    ]
    schema, rows = F.extract(str(local / KEY / "path-index.parquet"))
    assert F.read_groups_blob(f"{root}/{KEY}/path-index.parquet") == (schema, rows)
    pointer = F._pointer_sql(DATE, "path", GEN, KEY, schema, "primary", STARTED)
    assert sent[-len(pointer) - 1 :] == [
        *pointer,
        # -M's backfill: one read, nothing left to fill
        "SELECT date FROM index_schema WHERE store = 'primary' AND variant = 'path' AND started IS NULL;",
    ]


def test_index_sync_needs_a_dir_or_a_root(monkeypatch):
    monkeypatch.delenv("DATA_BUCKET")
    r = CliRunner().invoke(main, ["index-sync", "-g", GEN, "-k", KEY, DATE])
    assert (r.exit_code, r.output.splitlines()[-1]) == (2, "Error: need -d (the generation dir) or -b/$DATA_BUCKET (its root)")


# ── over-time-groups ────────────────────────────────────────────────────────

B = "bkt"
# Four scans; K=2 → two sealed groups. B changes at s2, B/a is static.
OT_SCANS = {
    "2026-09-01T0001": [(B, 1, 100, 10), (f"{B}/a", 2, 50, 5)],
    "2026-09-02T0001": [(B, 1, 100, 10), (f"{B}/a", 2, 50, 5)],
    "2026-09-03T0001": [(B, 1, 200, 20), (f"{B}/a", 2, 50, 5)],
    "2026-09-04T0001": [(B, 1, 200, 20), (f"{B}/a", 2, 50, 5)],
}


def _write_dir_index(path: Path, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "path": [r[0] for r in rows],
        "depth": pa.array([r[1] for r in rows], pa.int32()),
        "b": pa.array([r[2] for r in rows], pa.int64()),
        "o": pa.array([r[3] for r in rows], pa.int64()),
    }), path)


def test_over_time_groups_reads_and_publishes_under_the_root(monkeypatch, archive, tmp_path):
    """`over-time-groups -p <root>`: each scan's `path` sort is read from the
    archive (a URL through `blobfs`, no DuckDB secret), and each sealed group is
    published under its last scan's layer 2 there and synced from that path."""
    from dt_cloud import index_footer as IF

    root, local = archive
    for d, rows in OT_SCANS.items():
        _write_dir_index(local / f"l2/{d}/index/G0/path-index.parquet", rows)
    dates = list(OT_SCANS)
    monkeypatch.setattr(IF, "synced_variants", lambda store: [(d, "path") for d in dates])
    monkeypatch.setattr(IF, "index_dir", lambda d, variant, store: f"l2/{d}/index/G0")
    monkeypatch.setattr(OT, "synced_groups", lambda store: set())
    synced: list[tuple] = []
    monkeypatch.setattr(IF, "sync_d1", lambda date, path, *, variant, gen, key, store: synced.append((date, path, variant, gen, key)) or 1)
    manifest: list[dict] = []
    monkeypatch.setattr(OT, "sync_manifest", lambda row: manifest.append(row))
    monkeypatch.delenv("DATA_BUCKET")
    r = CliRunner().invoke(main, ["over-time-groups", "-g", "G9", "-K", "2", "-l", "l2/{scan}/", "-p", root, "-o", str(tmp_path / "work"), "-m", "1GB", "-t", "2"])
    assert r.exit_code == 0, r.output
    g1, g2 = dates[1], dates[3]
    assert json.loads(r.output) == {"groups": [g1, g2]}
    assert synced == [
        (g, f"{root}/l2/{g}/index/G9/over-time.parquet", "over-time", "G9", f"l2/{g}/index/G9") for g in (g1, g2)
    ]
    assert [(m["key"], json.loads(m["scans"])) for m in manifest] == [(g1, dates[:2]), (g2, dates[2:])]
    assert files(local) == sorted(
        [f"l2/{d}/index/G0/path-index.parquet" for d in dates]
        + [f"l2/{g}/index/G9/{f}" for g in (g1, g2) for f in ("over-time.parquet", "over-time.scans.json")]
    )
    got = pq.read_table(local / f"l2/{g2}/index/G9/over-time.parquet").to_pylist()
    assert [(r["depth"], r["path"], r["b"], r["o"], r[OT.SCAN_LO], r[OT.SCAN_HI]) for r in got] == [
        (0, "", 200, 20, 0, 1),
        (1, B, 200, 20, 0, 1),
        (2, f"{B}/a", 50, 5, 0, 1),
    ]
    assert json.loads((local / f"l2/{g2}/index/G9/over-time.scans.json").read_text()) == dates[2:]


def test_over_time_index_reads_url_sorts_like_local_ones(fake_r2, tmp_path):
    """`write_over_time_index` over `r2://` sorts gives exactly the local run's intervals."""
    out = {}
    for kind in KINDS:
        scans = []
        for d, rows in OT_SCANS.items():
            p = (fake_r2 / "archive" if kind == "r2" else tmp_path / "local") / f"{d}.parquet"
            _write_dir_index(p, rows)
            scans.append((d, f"r2://archive/{d}.parquet" if kind == "r2" else str(p)))
        s = OT.write_over_time_index(scans, tmp_path / f"ot-{kind}", mem="1GB", threads=2)
        out[kind] = [(r["depth"], r["path"], r["b"], r["o"], r[OT.SCAN_LO], r[OT.SCAN_HI]) for r in pq.read_table(s["file"]).to_pylist()]
    assert out == {kind: [
        (0, "", 100, 10, 0, 1),
        (0, "", 200, 20, 2, 3),
        (1, B, 100, 10, 0, 1),
        (1, B, 200, 20, 2, 3),
        (2, f"{B}/a", 50, 5, 0, 3),
    ] for kind in KINDS}


# ── warm-cache / digest ─────────────────────────────────────────────────────

def _publish(snapshots: Path, dated_meta: list[tuple[str, dict]]) -> None:
    for scan, m in dated_meta:
        (snapshots / scan).mkdir(parents=True, exist_ok=True)
        (snapshots / scan / "meta.json").write_text(json.dumps(m))


def test_warm_cache_lists_the_scans_under_the_root(monkeypatch, archive):
    """`warm-cache -n -r <root>/snapshots`: the newest scan, its prior pairs
    from the scans under the root."""
    root, local = archive
    _publish(local / "snapshots", [(d, {}) for d in ("2026-09-13", "2026-09-14", "2026-09-15")])
    (local / "snapshots" / "not-a-scan").mkdir()
    monkeypatch.setenv("WARM_PATHS", "b1")
    r = CliRunner().invoke(main, ["warm-cache", "-n", "-r", f"{root}/snapshots", "-W", "1280"])
    assert r.exit_code == 0, r.output
    assert r.output.splitlines() == [
        "/api/series?path=b1",
        "/api/subtree?date=2026-09-15&path=b1&w=1280&h=768",
        "/api/diff?from=2026-09-14&to=2026-09-15&path=b1&w=1280&h=768&summary=1",
        "/api/diff?from=2026-09-14&to=2026-09-15&path=b1&w=1280&h=768",
        "/api/diff?from=2026-09-13&to=2026-09-15&path=b1&w=1280&h=768&summary=1",
        "/api/diff?from=2026-09-13&to=2026-09-15&path=b1&w=1280&h=768",
    ]


def test_digest_reads_scans_and_keeps_state_under_the_root(archive):
    """The digest's root IO — scan listing, metas, `tree.json`, the month's
    converge state beside `snapshots/` — over the archive root."""
    from datetime import date

    from dt_cloud import digest as E
    from dt_cloud import digest_cw as D

    from test_digest_cw import LEAD, P, SEPT, _meta

    root, local = archive
    snaps = f"{root}/snapshots/cw"
    _publish(local / "snapshots" / "cw", [("2026-08-30T1200", _meta(690))] + LEAD + SEPT)
    (local / "snapshots" / "cw" / SEPT[0][0] / "tree.json").write_text(json.dumps({"n": "root", "c": [{"n": "other"}, {"n": P, "b": 7}]}))
    month = D.load_month(snaps, date(2026, 9, 1), P)
    assert [r.scan for r in month.lead] == [s for s, _ in LEAD]
    assert [r.scan for r in month.rows] == [s for s, _ in SEPT]
    assert D.load_tree(snaps, SEPT[0][0], P) == {"n": P, "b": 7}
    path = E.state_path(snaps, date(2026, 9, 1), "digest/cw/C1/sender")
    assert path == f"{root}/digest/cw/C1/sender/2026-09.json"
    assert E.load_state(path) == {}
    E.save_state(path, {"op_ts": "m1", "posted": {}})
    assert E.load_state(path) == {"op_ts": "m1", "posted": {}}
    assert json.loads((local / "digest/cw/C1/sender/2026-09.json").read_text()) == {"op_ts": "m1", "posted": {}}
