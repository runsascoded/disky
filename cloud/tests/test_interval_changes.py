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
    assert {k: v for k, v in stats.items() if k not in ("bytes", "groups_bytes")} == {"rows": 6, "appeared": 2, "went": 2, "changed": 2, "groups": 4}
    # One group per scan: its scan in the time bounds, its rows' larger side in `b_min`/`b_max`.
    groups = pq.read_table(tmp_path / "changes.groups.parquet",
                           columns=["rg", "vf_min", "vf_max", "vt_min", "vt_max", "b_min", "b_max", "p_min", "p_max", "row_start", "row_end"])
    assert groups.to_pylist() == [
        {"rg": 0, "vf_min": 150, "vf_max": 150, "vt_min": 150, "vt_max": 150, "b_min": 4, "b_max": 4, "p_min": "back", "p_max": "back", "row_start": 0, "row_end": 1},
        {"rg": 1, "vf_min": 200, "vf_max": 200, "vt_min": 200, "vt_max": 200, "b_min": 5, "b_max": GB, "p_min": "grow", "p_max": "new", "row_start": 1, "row_end": 3},
        {"rg": 2, "vf_min": 250, "vf_max": 250, "vt_min": 250, "vt_max": 250, "b_min": 6, "b_max": 7, "p_min": "back", "p_max": "gone", "row_start": 3, "row_end": 5},
        {"rg": 3, "vf_min": 300, "vf_max": 300, "vt_min": 300, "vt_max": 300, "b_min": GB, "b_max": GB, "p_min": "shrink", "p_max": "shrink", "row_start": 5, "row_end": 6},
    ]


def test_files_appearing_where_objects_are_first_listed_are_no_transitions(tmp_path):
    """At the first scan listing objects after dirs-only scans, a file's opening is the format's, not a change; its
    dir's change there and the file's later changes are transitions."""
    scans = [{"ts": 100, "version": 1}, {"ts": 200, "version": 1}, {"ts": 300, "version": 2}, {"ts": 400, "version": 2}]
    assert listed_from(scans) == [300]
    src = tmp_path / "pv.parquet"
    # `d/grow` changed after the switch: its one transition carries the size it had since the switch (`size_prev`),
    # what a diff from a v1 scan takes as its old side; it never reads as appearing at 300.
    t = versions(("d/old", 300, 400, 5), ("d/kept", 300, OPEN, 8), ("d/later", 400, OPEN, 2),
                 ("d/grow", 300, 400, 5), ("d/grow", 400, OPEN, 9))
    pq.write_table(t, src)
    out = tmp_path / "changes.parquet"
    cut_changes(connect(1, "1GB", None), f"read_parquet('{src}')", out, 100, listed_from(scans))
    assert pq.read_table(out).to_pylist() == [T("d/grow", 400, 9, 5), T("d/old", 400, None, 5), T("d/later", 400, 2, None)]
