"""Retained snapshots are exact, while validity boundaries and redundant pieces can disappear."""
from __future__ import annotations

import re

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import interval_store as ist
from dt_cloud.interval_thinning import project_parquet, project_ranges, project_sql, retained_stamps
from dt_cloud.static_names import OPEN


SCHEMA = pa.schema([
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("vf", pa.int64(), nullable=False),
    pa.field("vt", pa.int64(), nullable=False),
    pa.field("size", pa.int64(), nullable=False),
])


def _row(path: str, vf: int, vt: int, size: int = 1, **values) -> dict:
    return {"depth": path.count("/") + 1, "path": path, "vf": vf, "vt": vt, "size": size, **values}


def _project(rows: list[dict], kept: list[int], schema: pa.Schema = SCHEMA, key=("depth", "path")) -> list[dict]:
    with duckdb.connect() as con:
        con.register("input_rows", pa.Table.from_pylist(rows, schema=schema))
        return con.execute(project_sql("SELECT * FROM input_rows", schema.names, kept, key=key)).to_arrow_table().to_pylist()


def _snapshot(rows: list[dict], stamp: int) -> list[dict]:
    live = [{k: v for k, v in row.items() if k not in ("vf", "vt")} for row in rows if row["vf"] <= stamp < row["vt"]]
    return sorted(live, key=lambda r: (r["depth"], r["path"], repr(r.get("usr"))))


def test_projects_bounds_and_removes_transient_objects_and_reversions():
    source = [
        _row("b/reverted", 10, 20), _row("b/reverted", 20, 30, 2), _row("b/reverted", 30, OPEN),
        _row("b/transient", 20, 30, 5),
        _row("b/gone", 10, 20, 3),
        _row("b/late", 20, OPEN, 4),
        _row("b/gap", 10, 20, 6), _row("b/gap", 30, OPEN, 6),
    ]
    projected = _project(source, [10, 30])
    assert projected == [
        _row("b/gap", 10, OPEN, 6),
        _row("b/gone", 10, 30, 3),
        _row("b/late", 30, OPEN, 4),
        _row("b/reverted", 10, OPEN),
    ]
    assert [_snapshot(projected, t) for t in (10, 30)] == [_snapshot(source, t) for t in (10, 30)]


def test_non_scan_bounds_map_forward_and_objects_after_last_kept_scan_disappear():
    source = [_row("a", 0, 15), _row("b", 15, 25), _row("c", 25, 45), _row("d", 45, OPEN)]
    assert _project(source, [10, 30, 40]) == [_row("a", 10, 30), _row("c", 30, OPEN)]


def test_observed_timestamps_exact_floats_and_nullable_identity_are_semantic_state():
    schema = SCHEMA.append(pa.field("mtime", pa.int64())).append(pa.field("wts", pa.float64()))
    schema = schema.append(pa.field("last_read", pa.int32())).append(pa.field("version", pa.string()))
    old = {"mtime": 17, "wts": 17.125, "last_read": 9, "version": None}
    token = {**old, "version": '"opaque-etag"'}
    precise = {**token, "wts": 17.25}
    source = [_row("x", 10, 20, **old), _row("x", 20, 30, **token), _row("x", 30, OPEN, **precise)]
    assert _project(source, [10, 20, 30], schema) == source
    assert _project(source, [10, 30], schema) == [_row("x", 10, 30, **old), _row("x", 30, OPEN, **precise)]


def test_owner_keys_distinguish_null_empty_and_named_slices():
    schema = SCHEMA.append(pa.field("usr", pa.string())).append(pa.field("evidence", pa.string()))
    source = [
        _row("x", 10, 20, usr=None, evidence=None), _row("x", 20, OPEN, usr=None, evidence=None),
        _row("x", 10, OPEN, 2, usr="", evidence=""),
        _row("x", 10, OPEN, 3, usr="u", evidence="review"),
    ]
    projected = _project(source, [10, 30], schema, key=("depth", "path", "usr"))
    assert projected == [
        _row("x", 10, OPEN, usr=None, evidence=None),
        _row("x", 10, OPEN, 2, usr="", evidence=""),
        _row("x", 10, OPEN, 3, usr="u", evidence="review"),
    ]
    assert [_snapshot(projected, t) for t in (10, 30)] == [_snapshot(source, t) for t in (10, 30)]


def test_churn_columns_are_opaque_and_not_claimed_to_be_recomputed():
    schema = SCHEMA.append(pa.field("churn_cum", pa.int64())).append(pa.field("churn_end", pa.int64()))
    source = [_row("x", 10, 20, churn_cum=7, churn_end=9)]
    assert _project(source, [10, 30], schema) == [_row("x", 10, 30, churn_cum=7, churn_end=9)]


def test_empty_relation_has_no_projected_rows():
    assert _project([], [10, 30]) == []


def test_project_parquet_preserves_schema_metadata_and_supports_private_in_place_rewrite(tmp_path):
    schema = SCHEMA.with_metadata({b"origin": b"canonical-range"})
    path = tmp_path / "r0000.parquet"
    pq.write_table(pa.Table.from_pylist([_row("x", 10, 20), _row("x", 20, OPEN)], schema=schema), path)
    with duckdb.connect() as con:
        assert project_parquet(con, path, path, [10, 30], rg_rows=1) == {"before": 2, "after": 1}
    assert pq.read_schema(path).equals(schema, check_metadata=True) is True
    assert pq.read_table(path).to_pylist() == [_row("x", 10, OPEN)]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["r0000.parquet"]


def test_every_canonical_table_preserves_all_values_and_owner_keys(tmp_path):
    values = {
        "depth": 2, "path": "b/x", "vf": 10, "vt": OPEN, "kind": "file", "size": 3,
        "n_files": 1, "n_children": 0, "n_desc": 0, "mtime": 17, "wts": 17.125, "wb": 1,
        **{c: 0 for c in ist.CLS}, "us": "[]", "last_read": 9, "usr": None, "tot": 3,
        "user": None, "source": "manual", "evidence": "review", "version": '"etag"',
    }
    expected = {}
    for table, base in ist.SUB_SCHEMA.items():
        schema = base.append(ist.OBJECT_VERSION_FIELD) if table in ("pv", "pvl") else base
        path = tmp_path / table / "r0000.parquet"
        path.parent.mkdir()
        row = {c: values[c] for c in schema.names}
        expected[table] = [row]
        pq.write_table(pa.Table.from_pylist([{**row, "vt": 20}, {**row, "vf": 20}], schema=schema), path)
    with duckdb.connect() as con:
        totals = project_ranges(con, tmp_path, [10, 30], rg_rows=1)
    assert totals == {table: {"before": 2, "after": 1} for table in expected}
    assert {table: pq.read_table(tmp_path / table / "r0000.parquet").to_pylist() for table in expected} == expected


@pytest.mark.parametrize("kept,message", [
    ([], "kept timestamps must be nonempty integers in [0, OPEN)"),
    ([False, 10], "kept timestamps must be nonempty integers in [0, OPEN)"),
    ([-1, 10], "kept timestamps must be nonempty integers in [0, OPEN)"),
    ([10, OPEN], "kept timestamps must be nonempty integers in [0, OPEN)"),
    ([10, 10], "kept timestamps must be strictly increasing"),
    ([30, 10], "kept timestamps must be strictly increasing"),
])
def test_invalid_retained_timestamps_are_rejected(kept, message):
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        retained_stamps(kept)


@pytest.mark.parametrize("columns,key,message", [
    (["depth", "path", "vf", "vt", "vf"], ["depth", "path"], "columns and key must not contain duplicates"),
    (["depth", "path", "vf", "vt"], ["path", "path"], "columns and key must not contain duplicates"),
    (["depth", "path", "vf"], ["depth", "path"], "columns must contain a nonempty key and distinct vf/vt bounds"),
    (["depth", "path", "vf", "vt"], ["vf"], "columns must contain a nonempty key and distinct vf/vt bounds"),
])
def test_invalid_projection_schema_is_rejected(columns, key, message):
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        project_sql("SELECT * FROM x", columns, [10], key=key)
