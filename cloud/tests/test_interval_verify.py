"""The parity check's per-scan reference (`interval_verify.Scan`) over a v2 sort that carries only some of the
optional columns, as cw's do: no owner (`usr`) and a single storage-class pivot (`sum_storage_class_id_1`)."""
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from dt_cloud import interval_read as ir
from dt_cloud.interval_verify import Scan, verify_budget


def test_scan_lookup_v2_without_optional_columns(tmp_path):
    f = tmp_path / "path-index.parquet"
    pq.write_table(pa.table({
        "path": ["b1", "b1/x", "b1/y"],
        "size": [30, 10, 20],
        "depth": [1, 2, 2],
        "kind": ["dir", "file", "file"],
        "n_files": [2, 1, 1],
        "n_children": [2, None, None],
        "n_desc": [2, 0, 0],
        "mtime": [200, 100, 200],
        "mtime_mean": [None, None, None],
        "created": [None, None, None],
        "last_read": [None, None, None],
        "sum_storage_class_id_1": [30, 10, 20],
    }), f)
    s = Scan(duckdb.connect(), str(f), 2)
    assert s.lookup("b1") == ir.Agg(b=30, o=2, kind="dir", nc=2, m=200)
    assert s.lookup("b1/y") == ir.Agg(b=20, o=1, kind="file", m=200)


def test_verify_budget_fits_the_machine():
    """The dedicated verify VM's 16 threads / 80 GB, capped at a smaller machine's cores and 3/4 of its RAM."""
    assert verify_budget(256 << 30, 32) == (16, 80 << 30)
    assert verify_budget(32 << 30, 4) == (4, 24 << 30)
    assert verify_budget(128 << 30, 16) == (16, 80 << 30)
