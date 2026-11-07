"""`dt-cloud l2-listing` (`dt_cloud.l2_listing`): a layer-2's file rows back to a listing that `disk-tree import`
re-aggregates to the same layer-2 (the path for re-cutting a scan whose raw listing is gone)."""
from __future__ import annotations

from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from disk_tree.cli.main import cli as disk_tree
from dt_cloud.l2_listing import l2_listing


def T(day: int, sec: int = 0) -> datetime:
    return datetime(2026, 10, day, 0, 0, sec, tzinfo=timezone.utc)


def _import(listing, root, monkeypatch) -> pa.Table:
    res = CliRunner().invoke(disk_tree, ["import", "-e", "stream", "-m", "-p", "storage_class_id", "-s", "gcs", "--to", str(root),
                                         "-t", "2026-10-12T00:00:00+00:00", "-l", str(listing), "-b", "bk"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    (blob,) = sorted(root.rglob("*.parquet"))
    return pq.read_table(blob)


def test_a_layer_2_round_trips_through_its_listing(tmp_path, monkeypatch):
    """listing → import → layer-2 → `l2_listing` → import → the same layer-2 (every column, pivots and `mtime_mean`
    included). A class only a 0-byte file had (2) can't be read back, but a 0-byte file carries it so its column
    (all zeros) is written again."""
    names = ["a/x.bin", "a/y.bin", "a/b/z.bin", "c/empty", "top.bin"]
    listing = tmp_path / "listing.parquet"
    pq.write_table(pa.table({
        "bucket": ["bk"] * 5, "name": names, "size_bytes": [10, 20, 30, 0, 5],
        "created": pa.array([T(1), T(2, 7), T(3), T(4), T(5)], pa.timestamp("us", "UTC")),
        "storage_class_id": pa.array([1, 3, 1, 2, 4], pa.int32()), "generation": pa.array([11, 12, 13, 14, 15], pa.int64()),
    }), listing)
    l2 = _import(listing, tmp_path / "r1", monkeypatch)
    pq.write_table(l2, tmp_path / "l2.parquet")
    back = tmp_path / "back.parquet"
    assert l2_listing(str(tmp_path / "l2.parquet"), back, "bk") == (5, 65)
    assert pq.read_table(back).select(["name", "size_bytes", "storage_class_id"]).to_pylist() == [
        {"name": "a/b/z.bin", "size_bytes": 30, "storage_class_id": 1}, {"name": "a/x.bin", "size_bytes": 10, "storage_class_id": 1},
        {"name": "a/y.bin", "size_bytes": 20, "storage_class_id": 3}, {"name": "c/empty", "size_bytes": 0, "storage_class_id": 2},
        {"name": "top.bin", "size_bytes": 5, "storage_class_id": 4},
    ]
    again = _import(back, tmp_path / "r2", monkeypatch)
    assert (again.schema, again.sort_by([("depth", "ascending"), ("path", "ascending")]).to_pylist()) == (
        l2.schema, l2.sort_by([("depth", "ascending"), ("path", "ascending")]).to_pylist())


def test_keep_prefixes(tmp_path, monkeypatch):
    listing = tmp_path / "listing.parquet"
    pq.write_table(pa.table({"bucket": ["bk"] * 3, "name": ["cw-l2/a", "gcs/b", "cw-sweep/c"], "size_bytes": [1, 2, 4],
                             "created": pa.array([T(1)] * 3, pa.timestamp("us", "UTC")),
                             "storage_class_id": pa.array([1, 1, 1], pa.int32())}), listing)
    pq.write_table(_import(listing, tmp_path / "r", monkeypatch), tmp_path / "l2.parquet")
    out = tmp_path / "kept.parquet"
    assert (l2_listing(str(tmp_path / "l2.parquet"), out, "bk", ("cw-l2/", "cw-sweep/")),
            pq.read_table(out).column("name").to_pylist()) == ((2, 5), ["cw-l2/a", "cw-sweep/c"])
