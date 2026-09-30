"""Specs for the cross-scan over-time index (`dt_cloud.overtime`): synthetic
per-scan `path-index`es → an SCD-2 interval table whose runs reconstruct every
scan's `(depth, path)` totals, with a synthesized depth-0 fleet root."""
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from dt_cloud import overtime as OT
from dt_cloud.cli import main


def _write_path_index(path: Path, rows: list[tuple]) -> None:
    """(path, depth, usr, b, o) — a pre-store index (dir rows, wire names)."""
    tbl = pa.table({
        "path": [r[0] for r in rows],
        "depth": pa.array([r[1] for r in rows], pa.int32()),
        "usr": pa.array([r[2] for r in rows], pa.string()),
        "b": pa.array([r[3] for r in rows], pa.int64()),
        "o": pa.array([r[4] for r in rows], pa.int64()),
    })
    pq.write_table(tbl, path)


def _write_store_index(path: Path, rows: list[tuple]) -> None:
    """The same rows as a store generation's `path` sort (`kind`, `size`,
    `n_files`), each dir's objects present as object rows under it — which the
    roll-up must leave out."""
    dirs = [(p, d, u, "dir", b, o) for p, d, u, b, o in rows]
    objs = [(f"{p}/obj{i}.bin", d + 1, u, "file", b // o, 1) for p, d, u, b, o in rows for i in range(o)]
    tbl = pa.table({
        "path": [r[0] for r in dirs + objs],
        "depth": pa.array([r[1] for r in dirs + objs], pa.int32()),
        "usr": pa.array([r[2] for r in dirs + objs], pa.string()),
        "kind": [r[3] for r in dirs + objs],
        "size": pa.array([r[4] for r in dirs + objs], pa.int64()),
        "n_files": pa.array([r[5] for r in dirs + objs], pa.int64()),
    })
    pq.write_table(tbl, path)


B = "marin-us-east-02a"

# Four scans, oldest first. Exercises: a changing value (B), a long static run
# (B/a, split into two owner slices at s0 that must sum), and an absence gap
# (B/b present only s1–s2). Fleet root (depth 0) = the depth-1 rows summed.
SCANS = {
    "s0": [(B, 1, None, 100, 10), (f"{B}/a", 2, "u1", 30, 3), (f"{B}/a", 2, "u2", 20, 2)],
    "s1": [(B, 1, None, 100, 10), (f"{B}/a", 2, None, 50, 5), (f"{B}/b", 2, None, 7, 1)],
    "s2": [(B, 1, None, 200, 20), (f"{B}/a", 2, None, 50, 5), (f"{B}/b", 2, None, 7, 1)],
    "s3": [(B, 1, None, 200, 20), (f"{B}/a", 2, None, 50, 5)],
}


def _intervals(path: Path) -> list[tuple]:
    return duckdb.connect().execute(
        f"SELECT depth, path, b, o, {OT.SCAN_LO}, {OT.SCAN_HI} "
        f"FROM read_parquet('{path}') ORDER BY depth, path, {OT.SCAN_LO}"
    ).fetchall()


def test_write_over_time_index(tmp_path: Path):
    scans = []
    for sid, rows in SCANS.items():
        pi = tmp_path / f"{sid}.parquet"
        _write_path_index(pi, rows)
        scans.append((sid, str(pi)))
    s = OT.write_over_time_index(scans, tmp_path / "ot", mem="1GB", threads=2)

    assert s == {
        "rows": 6,
        "paths": 4,
        "scans": ["s0", "s1", "s2", "s3"],
        "intervals_per_path": 6 / 4,
        "file": str(tmp_path / "ot" / "over-time.parquet"),
        "scans_file": str(tmp_path / "ot" / "over-time.scans.json"),
    }
    assert json.loads((tmp_path / "ot" / "over-time.scans.json").read_text()) == ["s0", "s1", "s2", "s3"]
    # SCD-2 runs (pyrmts' interval kernel): fleet root + B change at s2, B/a
    # static, B/b only s1–s2.
    assert _intervals(tmp_path / "ot" / "over-time.parquet") == [
        (0, "", 100, 10, 0, 1),
        (0, "", 200, 20, 2, 3),
        (1, B, 100, 10, 0, 1),
        (1, B, 200, 20, 2, 3),
        (2, f"{B}/a", 50, 5, 0, 3),
        (2, f"{B}/b", 7, 1, 1, 2),
    ]


def test_store_generations_roll_up_like_pre_store_ones(tmp_path: Path):
    """Scans across the store switch — s0/s1 dir-only indexes, s2/s3 store
    sorts with object rows — consolidate to exactly the intervals the
    all-legacy run gives: dir rows' `size`/`n_files` are the old `b`/`o`,
    object rows are not series."""
    scans = []
    for i, (sid, rows) in enumerate(SCANS.items()):
        pi = tmp_path / f"{sid}.parquet"
        (_write_path_index if i < 2 else _write_store_index)(pi, rows)
        scans.append((sid, str(pi)))
    s = OT.write_over_time_index(scans, tmp_path / "ot", mem="1GB", threads=2)
    assert (s["rows"], s["paths"]) == (6, 4)
    assert _intervals(tmp_path / "ot" / "over-time.parquet") == [
        (0, "", 100, 10, 0, 1),
        (0, "", 200, 20, 2, 3),
        (1, B, 100, 10, 0, 1),
        (1, B, 200, 20, 2, 3),
        (2, f"{B}/a", 50, 5, 0, 3),
        (2, f"{B}/b", 7, 1, 1, 2),
    ]


def test_intervals_reconstruct_each_scan(tmp_path: Path):
    """Every scan `k`'s `(depth, path)` totals = the intervals covering `k`."""
    scans = []
    for sid, rows in SCANS.items():
        pi = tmp_path / f"{sid}.parquet"
        _write_path_index(pi, rows)
        scans.append((sid, str(pi)))
    OT.write_over_time_index(scans, tmp_path / "ot", mem="1GB", threads=2)
    ivals = _intervals(tmp_path / "ot" / "over-time.parquet")

    for k, (sid, rows) in enumerate(SCANS.items()):
        expected = {}
        for p, d, _usr, b, o in rows:
            e = expected.setdefault((d, p), [0, 0])
            e[0] += b
            e[1] += o
        # add the synthesized fleet root (sum of depth-1)
        root = [0, 0]
        for (d, _p), (b, o) in expected.items():
            if d == 1:
                root[0] += b
                root[1] += o
        expected[(0, "")] = root
        got = {
            (d, p): [b, o]
            for d, p, b, o, lo, hi in ivals
            if lo <= k <= hi
        }
        assert got == expected, f"scan {sid} (idx {k})"


def test_write_over_time_groups(tmp_path: Path):
    """Fixed K=2 groups: two self-contained MSs, each with its own scan list and
    interval bounds indexing that group (0-based)."""
    scans = []
    for sid, rows in SCANS.items():
        pi = tmp_path / f"{sid}.parquet"
        _write_path_index(pi, rows)
        scans.append((sid, str(pi)))
    s = OT.write_over_time_groups(scans, tmp_path / "g", group_size=2)
    assert s["group_size"] == 2
    assert [(g["group"], g["first"], g["last"], g["n"]) for g in s["groups"]] == [
        ("s1", "s0", "s1", 2),
        ("s3", "s2", "s3", 2),
    ]
    # group s0–s1: B/b appears at s1 only; bounds are 0-based within the group.
    assert json.loads((tmp_path / "g" / "s1" / "over-time.scans.json").read_text()) == ["s0", "s1"]
    assert _intervals(tmp_path / "g" / "s1" / "over-time.parquet") == [
        (0, "", 100, 10, 0, 1),
        (1, B, 100, 10, 0, 1),
        (2, f"{B}/a", 50, 5, 0, 1),
        (2, f"{B}/b", 7, 1, 1, 1),
    ]
    # group s2–s3: B/b present s2, gone s3.
    assert json.loads((tmp_path / "g" / "s3" / "over-time.scans.json").read_text()) == ["s2", "s3"]
    assert _intervals(tmp_path / "g" / "s3" / "over-time.parquet") == [
        (0, "", 200, 20, 0, 1),
        (1, B, 200, 20, 0, 1),
        (2, f"{B}/a", 50, 5, 0, 1),
        (2, f"{B}/b", 7, 1, 0, 0),
    ]


def test_cli_over_time_write_explicit(tmp_path: Path):
    """`over-time-write <date>=<parquet> …` bypasses D1 and builds the index."""
    args = []
    for sid, rows in SCANS.items():
        pi = tmp_path / f"{sid}.parquet"
        _write_path_index(pi, rows)
        args.append(f"{sid}={pi}")
    out = tmp_path / "ot"
    r = CliRunner().invoke(main, ["over-time-write", "-o", str(out), *args])
    assert r.exit_code == 0, r.output
    summary = json.loads(r.output.strip().splitlines()[-1])
    assert summary["rows"] == 6
    assert summary["paths"] == 4
    assert summary["scans"] == ["s0", "s1", "s2", "s3"]
    assert _intervals(out / "over-time.parquet") == [
        (0, "", 100, 10, 0, 1),
        (0, "", 200, 20, 2, 3),
        (1, B, 100, 10, 0, 1),
        (1, B, 200, 20, 2, 3),
        (2, f"{B}/a", 50, 5, 0, 3),
        (2, f"{B}/b", 7, 1, 1, 2),
    ]


def test_sealed_groups_are_fixed_k_runs_from_the_oldest_scan():
    dates = [f"2026-09-{d:02d}T{h}" for d in range(1, 11) for h in ("0001", "1201")]  # 20 scans
    assert OT.sealed_groups(dates, 8) == [dates[0:8], dates[8:16]]      # the 4-scan tail is not a group
    assert OT.sealed_groups(list(reversed(dates)), 8) == [dates[0:8], dates[8:16]]  # order-insensitive
    assert OT.sealed_groups(dates[:7], 8) == []
    assert OT.sealed_groups(dates[:16] + dates[:3], 8) == [dates[0:8], dates[8:16]]  # duplicates collapse


def test_scan_ms_reads_both_scan_id_shapes_as_utc():
    assert OT.scan_ms("2026-09-28") == 1_790_553_600_000
    assert OT.scan_ms("2026-09-28T1201") == 1_790_553_600_000 + (12 * 3600 + 60) * 1000


def test_multiscan_row_is_pyrmts_row_shape_keyed_by_the_last_scan():
    scans = ["2026-09-01T0001", "2026-09-01T1201", "2026-09-02T0001"]
    assert OT.multiscan_row(scans, written_at_ms=1_790_000_000_000) == {
        "dataset": "over-time",
        "tier": "over-time",
        "shard_dur": "3scans",
        "period_start": OT.scan_ms("2026-09-01T0001"),
        "period_end": OT.scan_ms("2026-09-02T0001"),
        "key": "2026-09-02T0001",
        "scans": '["2026-09-01T0001", "2026-09-01T1201", "2026-09-02T0001"]',
        "encoder": "interval",
        "digests": None,
        "written_at": 1_790_000_000_000,
    }


def test_multiscan_dataset_namespaces_secondary_stores():
    """The manifest's `dataset` keeps stores apart (pyrmts owns the table; its
    PK is `(dataset, key)`): the primary's is unchanged."""
    assert [OT.multiscan_dataset(), OT.multiscan_dataset("primary"), OT.multiscan_dataset("meta")] == ["over-time", "over-time", "meta:over-time"]
    assert OT.multiscan_row(["2026-09-01T0001"], written_at_ms=1, store="meta")["dataset"] == "meta:over-time"


def test_manifest_sql_is_one_idempotent_upsert():
    row = {
        "dataset": "over-time", "tier": "over-time", "shard_dur": "2scans", "period_start": 1, "period_end": 2,
        "key": "2026-09-02T0001", "scans": '["a", "b"]', "encoder": "interval", "digests": None, "written_at": 7,
    }
    assert OT.manifest_sql(row) == (
        "INSERT OR REPLACE INTO pyramid_multiscans (dataset, tier, shard_dur, period_start, period_end, key, scans, encoder, digests, written_at) "
        "VALUES ('over-time', 'over-time', '2scans', 1, 2, '2026-09-02T0001', '[\"a\", \"b\"]', 'interval', NULL, 7);"
    )


def test_over_time_groups_cli_dry_run_lists_unsealed_groups(monkeypatch, tmp_path: Path):
    from dt_cloud import index_footer as IF

    dates = [f"2026-09-{d:02d}T0001" for d in range(1, 19)]  # 18 scans → 2 groups of 8, tail of 2
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(IF, "synced_variants", lambda store: asked.append(("variants", store)) or [(d, "path") for d in dates])
    monkeypatch.setattr(OT, "synced_groups", lambda store: asked.append(("groups", store)) or {dates[7]})  # first group already sealed
    r = CliRunner().invoke(main, ["over-time-groups", "-g", "G", "-K", "8", "-n", "-o", str(tmp_path)])
    assert r.exit_code == 0, r.output + r.stderr
    assert json.loads(r.output) == {"groups": [dates[15]], "dry_run": True}
    # `-s` scopes both reads to that store; the default is the primary.
    r = CliRunner().invoke(main, ["over-time-groups", "-g", "G", "-K", "8", "-n", "-s", "meta", "-o", str(tmp_path)])
    assert r.exit_code == 0, r.output + r.stderr
    assert asked == [("variants", "primary"), ("groups", "primary"), ("variants", "meta"), ("groups", "meta")]
    # (the plan lines go to the real stderr via `err`, outside CliRunner's capture)
