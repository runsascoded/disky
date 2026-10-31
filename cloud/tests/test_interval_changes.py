"""`interval_changes` (specs/diff-from-changes.md §1–2): a generation's versions as transitions, both sides of a change
on one row, sorted `(b, size bucket desc, path)`."""
from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq

from dt_cloud.interval_changes import cut_changes, listed_from
from dt_cloud.interval_store import CLS
from dt_cloud.static_names import OPEN, connect

KB, GB = 1 << 10, 10 * (1 << 30)


def versions(*rows) -> pa.Table:
    """`(path, vf, vt, size)` versions of objects (one file each, no storage-class bytes)."""
    return pa.table({
        "depth": pa.array([1] * len(rows), pa.uint8()),
        "path": [r[0] for r in rows], "vf": [r[1] for r in rows], "vt": [r[2] for r in rows],
        "kind": ["file"] * len(rows), "size": [r[3] for r in rows], "n_files": [1] * len(rows),
        **{c: [0] * len(rows) for c in CLS},
        "mtime": list(range(len(rows))),
    })


def T(path, b, size, size_prev) -> dict:
    new, old = size is not None, size_prev is not None
    return {"depth": 1, "path": path, "b": b, "kind": "file", "size": size, "n_files": 1 if new else None,
            **{c: 0 if new else None for c in CLS},
            "size_prev": size_prev, "n_files_prev": 1 if old else None, **{f"{c}_prev": 0 if old else None for c in CLS}}


def test_transitions_carry_both_sides_and_sort_by_scan_size_path(tmp_path):
    src = tmp_path / "pv.parquet"
    pq.write_table(versions(
        ("grow", 100, 200, KB), ("grow", 200, OPEN, GB),          # 1 KB → 10 GB at 200: one row, both sides
        ("shrink", 100, 300, GB), ("shrink", 300, OPEN, KB),      # 10 GB → 1 KB at 300
        ("new", 200, OPEN, 5),                                    # appeared at 200: no old side
        ("gone", 100, 250, 7),                                    # went away at 250: no new side
        ("touch", 100, 150, 3), ("touch", 150, OPEN, 3),          # only `mtime` changed: no transition
        ("still", 100, OPEN, 9),                                  # open since the first scan: none
        ("back", 100, 150, 4), ("back", 250, OPEN, 6),            # gone at 150, back at 250: two rows
    ), src)
    out = tmp_path / "changes.parquet"
    stats = cut_changes(connect(1, "1GB", None), f"read_parquet('{src}')", out, first=100)
    assert pq.read_table(out).to_pylist() == [
        T("back", 150, None, 4),
        T("grow", 200, GB, KB),
        T("new", 200, 5, None),
        T("back", 250, 6, None),
        T("gone", 250, None, 7),
        T("shrink", 300, KB, GB),
    ]
    assert {k: v for k, v in stats.items() if k != "bytes"} == {"rows": 6, "appeared": 2, "went": 2, "changed": 2, "groups": 1}


def test_files_appearing_where_objects_are_first_listed_are_no_transitions(tmp_path):
    """At the first scan listing objects after dirs-only scans, a file's opening is the format's, not a change; its
    dir's change there and the file's later changes are transitions."""
    scans = [{"ts": 100, "version": 1}, {"ts": 200, "version": 1}, {"ts": 300, "version": 2}, {"ts": 400, "version": 2}]
    assert listed_from(scans) == [300]
    src = tmp_path / "pv.parquet"
    t = versions(("d/old", 300, 400, 5), ("d/kept", 300, OPEN, 8), ("d/later", 400, OPEN, 2))
    pq.write_table(t, src)
    out = tmp_path / "changes.parquet"
    cut_changes(connect(1, "1GB", None), f"read_parquet('{src}')", out, 100, listed_from(scans))
    assert pq.read_table(out).to_pylist() == [T("d/old", 400, None, 5), T("d/later", 400, 2, None)]
