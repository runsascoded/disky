"""Specs for scan-to-scan churn (`dt_cloud.churn`): two synthetic `path` sorts →
exact added / removed / changed / same counts per kind, per-column change
counts, and a delta parquet holding exactly B's new/changed rows plus removed
tombstones; and the per-boundary dir churn of a sealed over-time group."""
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from dt_cloud import overtime as OT
from dt_cloud.churn import group_churn, scan_churn
from dt_cloud.cli import main

B = "bkt"


def _write(path: Path, rows: list[tuple]) -> Path:
    """(path, depth, kind, size, mtime, n_files) → a store-generation `path` sort."""
    rows = sorted(rows, key=lambda r: (r[1], r[0]))
    pq.write_table(pa.table({
        "path": [r[0] for r in rows],
        "depth": pa.array([r[1] for r in rows], pa.int32()),
        "kind": [r[2] for r in rows],
        "size": pa.array([r[3] for r in rows], pa.int64()),
        "mtime": pa.array([r[4] for r in rows], pa.int64()),
        "n_files": pa.array([r[5] for r in rows], pa.int64()),
    }), path)
    return path


# A → B: `x/new.bin` added (its dirs grow), `y/old.bin` removed (y/ shrinks
# and loses a file), `x/keep.bin` re-written (mtime only), `z/same.bin` and
# `z/` untouched.
A_ROWS = [
    (B, 1, "dir", 60, 5, 4),
    (f"{B}/x", 2, "dir", 10, 3, 1),
    (f"{B}/y", 2, "dir", 30, 4, 2),
    (f"{B}/z", 2, "dir", 20, 5, 1),
    (f"{B}/x/keep.bin", 3, "file", 10, 3, 1),
    (f"{B}/y/old.bin", 3, "file", 5, 4, 1),
    (f"{B}/y/stay.bin", 3, "file", 25, 2, 1),
    (f"{B}/z/same.bin", 3, "file", 20, 5, 1),
]
B_ROWS = [
    (B, 1, "dir", 62, 9, 4),
    (f"{B}/x", 2, "dir", 17, 9, 2),
    (f"{B}/y", 2, "dir", 25, 2, 1),
    (f"{B}/z", 2, "dir", 20, 5, 1),
    (f"{B}/x/keep.bin", 3, "file", 10, 8, 1),
    (f"{B}/x/new.bin", 3, "file", 7, 9, 1),
    (f"{B}/y/stay.bin", 3, "file", 25, 2, 1),
    (f"{B}/z/same.bin", 3, "file", 20, 5, 1),
]


def test_scan_churn_counts_and_delta(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.parquet", A_ROWS)
    b = _write(tmp_path / "b.parquet", B_ROWS)
    res = scan_churn(a, b, out_dir=tmp_path / "delta")
    delta = res.pop("delta")
    assert res == {
        "a_rows": 8,
        "b_rows": 8,
        "key": ["depth", "path"],
        "columns": ["kind", "size", "mtime", "n_files"],
        "by_kind": {
            "dir": {"added": 0, "removed": 0, "changed": 3, "same": 1},
            "file": {"added": 1, "removed": 1, "changed": 1, "same": 2},
        },
        "changed_columns": {
            "dir": {"size": 3, "mtime": 3, "n_files": 2},
            "file": {"size": 0, "mtime": 1, "n_files": 0},
        },
    }
    assert {k: v for k, v in delta.items() if not k.endswith("bytes")} == {"rows": 6, "objects_rows": 3}
    rows = duckdb.connect().execute(
        f"SELECT op, path, depth, kind, size, mtime, n_files FROM read_parquet('{tmp_path}/delta/delta.parquet')"
    ).fetchall()
    assert rows == [
        ("c", B, 1, "dir", 62, 9, 4),
        ("c", f"{B}/x", 2, "dir", 17, 9, 2),
        ("c", f"{B}/y", 2, "dir", 25, 2, 1),
        ("c", f"{B}/x/keep.bin", 3, "file", 10, 8, 1),
        ("a", f"{B}/x/new.bin", 3, "file", 7, 9, 1),
        ("d", f"{B}/y/old.bin", 3, "file", None, None, None),
    ]
    objs = duckdb.connect().execute(
        f"SELECT op, path FROM read_parquet('{tmp_path}/delta/delta-objects.parquet')"
    ).fetchall()
    assert objs == [("c", f"{B}/x/keep.bin"), ("a", f"{B}/x/new.bin"), ("d", f"{B}/y/old.bin")]


def test_scan_churn_identical_scans(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.parquet", A_ROWS)
    res = scan_churn(a, a, out_dir=tmp_path / "delta", columns=["size"])
    assert {k: v for k, v in res.pop("delta").items() if not k.endswith("bytes")} == {"rows": 0, "objects_rows": 0}
    assert res == {
        "a_rows": 8,
        "b_rows": 8,
        "key": ["depth", "path"],
        "columns": ["size"],
        "by_kind": {
            "dir": {"added": 0, "removed": 0, "changed": 0, "same": 4},
            "file": {"added": 0, "removed": 0, "changed": 0, "same": 4},
        },
        "changed_columns": {},
    }


def test_churn_cli(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.parquet", A_ROWS)
    b = _write(tmp_path / "b.parquet", B_ROWS)
    r = CliRunner().invoke(main, ["churn", "-c", "size", str(a), str(b)])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == {
        "a_rows": 8,
        "b_rows": 8,
        "key": ["depth", "path"],
        "columns": ["size"],
        "by_kind": {
            "dir": {"added": 0, "removed": 0, "changed": 3, "same": 1},
            "file": {"added": 1, "removed": 1, "changed": 0, "same": 3},
        },
        "changed_columns": {"dir": {"size": 3}},
    }


def _write_group(path: Path, rows: list[tuple]) -> Path:
    """(depth, path, b, o, lo, hi) intervals — a sealed over-time group."""
    rows = sorted(rows, key=lambda r: (r[0], r[1], r[4]))
    pq.write_table(pa.table({
        "depth": pa.array([r[0] for r in rows], pa.int64()),
        "path": [r[1] for r in rows],
        "b": pa.array([r[2] for r in rows], pa.int64()),
        "o": pa.array([r[3] for r in rows], pa.int64()),
        OT.SCAN_LO: pa.array([r[4] for r in rows], pa.int64()),
        OT.SCAN_HI: pa.array([r[5] for r in rows], pa.int64()),
    }), path)
    return path


def test_group_churn(tmp_path: Path) -> None:
    # 4 scans. `B` changes at 2; `B/a` static; `B/b` appears at 1, gone at 3;
    # `B/c` present at 0, absent at 1, back at 2 (a gap = removed + re-added).
    g = _write_group(tmp_path / "over-time.parquet", [
        (0, "", 100, 10, 0, 1),
        (0, "", 200, 20, 2, 3),
        (1, B, 100, 10, 0, 1),
        (1, B, 200, 20, 2, 3),
        (2, f"{B}/a", 50, 5, 0, 3),
        (2, f"{B}/b", 7, 1, 1, 2),
        (2, f"{B}/c", 3, 1, 0, 0),
        (2, f"{B}/c", 3, 1, 2, 3),
    ])
    assert group_churn(g) == {
        "rows": 8,
        "paths": 5,
        "scans": 4,
        "per_scan": [
            {"scan": 1, "present": 4, "changed": 0, "added": 1, "removed": 1},
            {"scan": 2, "present": 5, "changed": 2, "added": 1, "removed": 0},
            {"scan": 3, "present": 4, "changed": 0, "added": 0, "removed": 1},
        ],
    }
    r = CliRunner().invoke(main, ["over-time-churn", str(g)])
    assert r.exit_code == 0, r.output
    assert [json.loads(l)["per_scan"][1] for l in r.output.splitlines()] == [
        {"scan": 2, "present": 5, "changed": 2, "added": 1, "removed": 0},
    ]
