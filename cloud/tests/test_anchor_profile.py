from json import loads

import duckdb
import pytest

from dt_cloud.anchor_profile import AnchorConnection, profile_summary
from dt_cloud import static_anchors


def test_operator_summary_is_bounded_and_exact():
    assert profile_summary({
        "cpu_time": 4, "system_peak_buffer_memory": 100, "system_peak_temp_dir_size": 200,
        "children": [{"operator_name": "SCAN", "operator_timing": 3, "operator_cardinality": 10,
            "operator_rows_scanned": 20, "children": [{"operator_name": "FILTER", "operator_timing": 1}]}],
    }) == {
        "cpu_s": 4, "peak_memory_bytes": 100, "peak_spill_bytes": 200,
        "operators": [{"name": "SCAN", "seconds": 3, "rows": 10, "scanned": 20},
            {"name": "FILTER", "seconds": 1, "rows": 0, "scanned": 0}],
    }


def test_connection_keeps_eager_query_profile_and_propagates_errors(tmp_path):
    logs = []
    con = AnchorConnection(duckdb.connect(), tmp_path, log=logs.append, keep_seconds=0)
    con.execute("CREATE TABLE x AS SELECT 42 AS value")
    assert con.execute("SELECT value FROM x").fetchall() == [(42,)]
    profile = loads((tmp_path / "q000001.json").read_text())
    assert profile["query_name"] == "CREATE TABLE x AS SELECT 42 AS value"
    assert [(loads(line.removeprefix("ANCHOR_QUERY "))["query"], loads(line.removeprefix("ANCHOR_QUERY "))["event"]) for line in logs] == [
        (1, "start"), (1, "done"), (2, "start"), (2, "done"),
    ]
    with pytest.raises(duckdb.CatalogException):
        con.execute("SELECT * FROM missing")
    assert [(loads(line.removeprefix("ANCHOR_QUERY "))["query"], loads(line.removeprefix("ANCHOR_QUERY "))["event"]) for line in logs[-2:]] == [(3, "start"), (3, "failed")]


def test_upload_progress_keeps_liveness_markers_last(tmp_path, monkeypatch):
    uploads, stages = [], []
    for name in ["meta.json", "part.parquet"]:
        (tmp_path / name).write_text("data\n")

    class Blob:
        def __init__(self, key):
            self.key = key

        def upload_from_filename(self, filename):
            uploads.append((self.key, filename))

    class Bucket:
        blob = staticmethod(Blob)

    monkeypatch.setattr(static_anchors, "resource_stage", lambda name, **kwargs: stages.append((name, kwargs)))
    assert static_anchors._upload_dir(Bucket(), tmp_path, "anchors", last=("meta.json",)) == 2
    assert uploads == [("anchors/part.parquet", str(tmp_path / "part.parquet")), ("anchors/meta.json", str(tmp_path / "meta.json"))]
    assert stages == [
        ("anchors upload", {"prefix": "anchors", "files": 2, "completed": 0}),
        ("anchors upload", {"prefix": "anchors", "files": 2, "completed": 2}),
    ]
