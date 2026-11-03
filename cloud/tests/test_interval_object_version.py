"""Opaque object versions survive the same IS pipeline as native GCS generations."""
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from dt_cloud import interval_compact as ic
from dt_cloud import interval_local as il
from dt_cloud import interval_store as ist
from dt_cloud import static_names as sn
from test_interval_generation import _newest_runs


def test_etag_only_changes_survive_build_append_merge_and_compaction(tmp_path: Path) -> None:
    source, store, work = (tmp_path / name for name in ("source", "store", "work"))
    dates = ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]
    for date, token in zip(dates, ['"a"', '"b"', '"b"', '"c"']):
        path = source / "index" / date / "path-index.parquet"
        path.parent.mkdir(parents=True)
        pq.write_table(pa.table({
            "path": ["b", "b/f"], "depth": [1, 2], "kind": ["dir", "file"],
            "size": [7, 7], "n_files": [1, 1], "n_children": [1, 0], "n_desc": [1, 0],
            "mtime": [100, 100], "version": pa.array([None, token], pa.string()),
        }), path)
    common = ["-g", "g", "-L", "index/{id}/path-index.parquet", "-s", str(source), "-o", str(store),
              "-w", str(work), "-r", "4", "-p", "1", "-M", "512MB"]
    result = CliRunner().invoke(il.build_cmd, [*common, "-k", "1", "-t", dates[1]], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    gen = store / "interval-store/g"
    assert json.loads((gen / "scans.json").read_text())["object_version"] == {"name": "version", "type": "string"}
    result = CliRunner().invoke(il.add_cmd, [*common, "-C", "none"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    runs = _newest_runs(gen)
    assert [p.name for p in runs] == [f"{dates[2]}_{dates[3]}"]
    compacted = tmp_path / "compacted"
    ic.compact_gen(gen, compacted, tmp_path / "compact-work", gen="g", new_gen="g.c001", level=None,
                   rg_rows=4, threads=1, mem="512MB")
    rows = duckdb.connect().execute(
        f"SELECT vf, vt, version FROM read_parquet('{compacted}/pvl/*.parquet') WHERE kind = 'file' ORDER BY vf"
    ).fetchall()
    ts = [sn.scan_epoch(d) for d in dates]
    assert rows == [(ts[0], ts[1], '"a"'), (ts[1], ts[3], '"b"'), (ts[3], sn.OPEN, '"c"')]
    assert pq.read_schema(compacted / "served/path.parquet").field("version") == ist.OBJECT_VERSION_FIELD


@pytest.mark.parametrize("legacy", [None, 9007199254740993])
def test_adopting_shared_versions_preserves_unknowns_and_opens_at_first_observation(tmp_path: Path, legacy) -> None:
    from dt_cloud import interval_export as ie
    from dt_cloud.interval_identity import migrate
    from dt_cloud.blobstore import Store

    source, store = tmp_path / "source", tmp_path / "store"
    dates = ["2026-10-01", "2026-10-02"]
    for i, date in enumerate(dates):
        file = source / "index" / date / "path-index.parquet"
        file.parent.mkdir(parents=True)
        table = pa.table({"path": ["b", "b/f"], "depth": [1, 2], "kind": ["dir", "file"],
                          "size": [7, 7], "n_files": [1, 1], "n_children": [1, 0], "n_desc": [1, 0], "mtime": [100, 100]})
        if i == 0 and legacy is not None:
            table = table.append_column("generation", pa.array([None, legacy], pa.int64()))
        elif i == 1:
            table = table.append_column("version", pa.array([None, '"first-seen"'], pa.string()))
        pq.write_table(table, file)
    common = ["-L", "index/{id}/path-index.parquet", "-s", str(source), "-o", str(store), "-r", "4", "-p", "1", "-M", "512MB"]
    result = CliRunner().invoke(il.build_cmd, ["-g", "old", *common, "-k", "1", "-t", dates[0]], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    data = Store(str(store))
    pointer = data.read_bytes("interval-store/CURRENT.json")
    migrate(data, "old", "converted", tmp_path / "migrate", threads=1, mem="512MB", rg_rows=4)
    assert data.read_bytes("interval-store/CURRENT.json") == pointer
    result = CliRunner().invoke(il.add_cmd, ["-g", "converted", *common, "-C", "none"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    expected = None if legacy is None else str(legacy)
    identities = [ie.object_identities(str(store), date, "b", [""], gen="converted").select(["name", "version"]).to_pylist() for date in dates]
    assert identities == [[{"name": "f", "version": expected}], [{"name": "f", "version": '"first-seen"'}]]
    exported = tmp_path / "exported"
    ie.export_scan(str(store), dates[0], str(exported), gen="converted", tiers=("path",), threads=1, mem="512MB")
    assert pq.read_table(exported / "path-index.parquet").select(["path", "version"]).to_pylist() == [
        {"path": "b", "version": None}, {"path": "b/f", "version": expected},
    ]
