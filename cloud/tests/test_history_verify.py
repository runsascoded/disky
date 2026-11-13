"""Exact event verification agrees with independent, literal snapshot EXCEPT ALL."""
from __future__ import annotations

import json
from itertools import product
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import interval_store as ist
from dt_cloud.history_verify import IGNORED, VerificationError, verify_ranges, verify_static
from dt_cloud.scan_id import scan_epoch
from dt_cloud.static_names import OPEN, q


SCANS = ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]
STAMPS = [scan_epoch(scan) for scan in SCANS]
SCHEMA = pa.schema([
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("vf", pa.int64(), nullable=False),
    pa.field("vt", pa.int64(), nullable=False),
    pa.field("size", pa.int64(), nullable=False),
    pa.field("wts", pa.float64()),
    pa.field("last_read", pa.int32()),
    pa.field("version", pa.string()),
])


def _row(vf: int = STAMPS[0], vt: int = OPEN, **values) -> dict:
    return {"depth": 2, "path": "b/x", "vf": vf, "vt": vt, "size": 1, "wts": 17.125, "last_read": 9, "version": None, **values}


def _write(root: Path, rows: list[dict], schema: pa.Schema = SCHEMA, table: str = "cintervals", name: str = "r0000.parquet") -> Path:
    path = root / table / name
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path, row_group_size=2)
    return path


def _audit(before: int, after: int, before_events: int, after_events: int) -> dict:
    return {
        "range": "r0000.parquet",
        "columns": ["depth", "last_read", "path", "size", "version", "wts"],
        "source_intervals": before,
        "candidate_intervals": after,
        "source_events": before_events,
        "candidate_events": after_events,
        "missing_events": 0,
        "extra_events": 0,
    }


def test_equal_retained_snapshots_allow_reversals_segmentation_and_transients(tmp_path):
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    _write(source, [
        _row(STAMPS[0], STAMPS[1]),
        _row(STAMPS[1], STAMPS[2], size=2, version="v2"),
        _row(STAMPS[2]),
        _row(STAMPS[1], STAMPS[2], path="b/transient", size=5),
    ])
    _write(candidate, [_row()])
    with duckdb.connect() as con:
        result = verify_static(con, source, candidate, [SCANS[0], SCANS[2]])
    assert result == {
        "verified": True,
        "scope": "canonical-retained-snapshots",
        "method": "exact-observation-events-except-all",
        "retained": [SCANS[0], SCANS[2]],
        "ignored_columns": list(IGNORED),
        "absent_tables": [],
        "tables": {"cintervals": [_audit(4, 1, 1, 1)]},
    }


@pytest.mark.parametrize("changed", [
    {"size": 2},
    {"version": ""},
    {"version": '"opaque-etag"'},
    {"wts": 17.125000000000004},
    {"last_read": 10},
    {"last_read": None},
    {"path": "b/y"},
])
def test_every_semantic_value_including_null_tokens_and_precise_floats_is_checked(tmp_path, changed):
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    _write(source, [_row()])
    _write(candidate, [_row(**changed)])
    with duckdb.connect() as con, pytest.raises(VerificationError) as error:
        verify_static(con, source, candidate, SCANS[:3])
    assert error.value.audit == {
        **_audit(1, 1, 1, 1),
        "missing_events": 1,
        "extra_events": 1,
        "first_differing_scan": SCANS[0],
        "table": "cintervals",
    }
    assert str(error.value) == (
        "retained snapshot mismatch: cintervals/r0000.parquet: "
        f"1 missing and 1 extra event rows; first differing scan {SCANS[0]}"
    )


def test_one_missing_observation_is_detected_at_the_first_wrong_scan(tmp_path):
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    _write(source, [_row()])
    _write(candidate, [_row(vt=STAMPS[1])])
    with duckdb.connect() as con, pytest.raises(VerificationError) as error:
        verify_static(con, source, candidate, SCANS[:3])
    assert error.value.audit == {
        **_audit(1, 1, 1, 2),
        "extra_events": 1,
        "first_differing_scan": SCANS[1],
        "table": "cintervals",
    }


@pytest.mark.parametrize("rows,missing,extra,source_events,candidate_events", [
    ([], 1, 0, 1, 0),
    ([_row(), _row()], 1, 1, 1, 1),
])
def test_missing_objects_and_duplicate_multiplicities_are_not_hidden(tmp_path, rows, missing, extra, source_events, candidate_events):
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    _write(source, [_row()])
    _write(candidate, rows)
    with duckdb.connect() as con, pytest.raises(VerificationError) as error:
        verify_static(con, source, candidate, SCANS[:3])
    assert error.value.audit == {
        **_audit(1, len(rows), source_events, candidate_events),
        "missing_events": missing,
        "extra_events": extra,
        "first_differing_scan": SCANS[0],
        "table": "cintervals",
    }


def test_owner_null_and_empty_slice_are_distinct(tmp_path):
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    schema = SCHEMA.append(pa.field("usr", pa.string()))
    _write(source, [_row(usr=None), _row(usr="")], schema)
    _write(candidate, [_row(usr=None)], schema)
    with duckdb.connect() as con, pytest.raises(VerificationError) as error:
        verify_static(con, source, candidate, SCANS[:3])
    assert (error.value.audit["missing_events"], error.value.audit["extra_events"], error.value.audit["columns"]) == (
        1, 0, ["depth", "last_read", "path", "size", "usr", "version", "wts"],
    )


def test_churn_is_explicitly_excluded_and_other_columns_remain_exact(tmp_path):
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    schema = SCHEMA.append(pa.field("churn_cum", pa.int64())).append(pa.field("churn_end", pa.int64()))
    _write(source, [_row(churn_cum=17, churn_end=99)], schema)
    _write(candidate, [_row()])
    with duckdb.connect() as con:
        result = verify_static(con, source, candidate, SCANS[:3])
    assert (result["verified"], result["ignored_columns"], result["tables"]) == (
        True, ["churn_cum", "churn_end"], {"cintervals": [_audit(1, 1, 1, 1)]},
    )


def test_all_six_interval_tables_compare_complete_real_schemas(tmp_path):
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    values = {
        **_row(), "kind": "file", "n_files": 1, "n_children": 0, "n_desc": 0,
        "mtime": 17, "wb": 1, **{c: 0 for c in ist.CLS}, "us": "[]", "usr": None,
        "tot": 1, "user": None, "source": "manual", "evidence": "review",
    }
    expected = {}
    for table, base in ist.SUB_SCHEMA.items():
        schema = base.append(ist.OBJECT_VERSION_FIELD) if table in ("pv", "pvl") else base
        row = {name: values[name] for name in schema.names}
        _write(source, [{**row, "vt": STAMPS[1]}, {**row, "vf": STAMPS[1]}], schema, table)
        _write(candidate, [row], schema, table)
        expected[table] = [{
            "range": "r0000.parquet",
            "columns": sorted(name for name in schema.names if name not in ("vf", "vt")),
            "source_intervals": 2,
            "candidate_intervals": 1,
            "source_events": 1,
            "candidate_events": 1,
            "missing_events": 0,
            "extra_events": 0,
        }]
    before = {str(path): path.read_bytes() for root in (source, candidate) for path in root.rglob("*.parquet")}
    with duckdb.connect() as con:
        result = verify_ranges(con, source, candidate, SCANS[:3])
    assert result == {
        "verified": True,
        "scope": "canonical-retained-snapshots",
        "method": "exact-observation-events-except-all",
        "retained": SCANS[:3],
        "ignored_columns": list(IGNORED),
        "absent_tables": [],
        "tables": expected,
    }
    assert {str(path): path.read_bytes() for root in (source, candidate) for path in root.rglob("*.parquet")} == before


def _explicit_differences(con, source: Path, candidate: Path, stamps: list[int]) -> list[tuple[int, int]]:
    cols = ", ".join(f'"{name}"' for name in SCHEMA.names if name not in ("vf", "vt"))
    out = []
    for stamp in stamps:
        left, right = [f"SELECT {cols} FROM read_parquet({q(str(path))}) WHERE vf <= {stamp} AND vt > {stamp}" for path in (source, candidate)]
        out.append(tuple(con.execute(f"SELECT count(*) FROM ({a} EXCEPT ALL {b})").fetchone()[0] for a, b in ((left, right), (right, left))))
    return out


def _versions(states: tuple[int, ...], observations: list[int]) -> list[dict]:
    rows = []
    for i, (value, stamp) in enumerate(zip(states, observations)):
        if value:
            rows.append(_row(vf=stamp, vt=observations[i + 1] if i + 1 < len(observations) else OPEN, size=value))
    return rows


def test_event_proof_matches_explicit_snapshot_except_all_for_exhaustive_small_histories(tmp_path):
    retained = [SCANS[0], SCANS[-1]]
    kept = [STAMPS[0], STAMPS[-1]]
    with duckdb.connect() as con:
        for states in product(range(3), repeat=4):
            case = tmp_path / "".join(str(state) for state in states)
            source_root = case / "source"
            original = _versions(states, STAMPS)
            selected = _versions((states[0], states[-1]), kept)
            source = _write(source_root, original)
            for trial, rows in enumerate((selected, [*selected, _row(path="b/extra")])):
                candidate_root = case / f"candidate-{trial}"
                candidate = _write(candidate_root, rows)
                differences = _explicit_differences(con, source, candidate, kept)
                try:
                    result = verify_static(con, source_root, candidate_root, retained)
                    actual = (result["verified"], None)
                except VerificationError as error:
                    actual = (False, error.audit["first_differing_scan"])
                expected = (True, None)
                if differences != [(0, 0), (0, 0)]:
                    first = next(i for i, difference in enumerate(differences) if difference != (0, 0))
                    expected = (False, retained[first])
                assert (states, trial, *actual) == (states, trial, *expected)


def test_verification_bypasses_stale_writer_caches_and_restores_settings(tmp_path):
    source_root, candidate_root = tmp_path / "source", tmp_path / "candidate"
    _write(source_root, [_row()])
    candidate = _write(candidate_root, [_row()])
    with duckdb.connect() as con:
        con.execute("SET parquet_metadata_cache=true")
        con.execute("SET enable_external_file_cache=true")
        assert con.execute(f"SELECT size FROM read_parquet({q(str(candidate))})").fetchall() == [(1,)]
        _write(candidate_root, [_row(size=2)])
        with pytest.raises(VerificationError) as error:
            verify_static(con, source_root, candidate_root, SCANS[:3])
        assert (error.value.audit["missing_events"], error.value.audit["extra_events"]) == (1, 1)
        assert con.execute("SELECT current_setting('parquet_metadata_cache'), current_setting('enable_external_file_cache')").fetchone() == (True, True)


def test_missing_range_and_missing_core_table_never_verify(tmp_path):
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    _write(source, [_row()])
    _write(candidate, [_row()], name="r0001.parquet")
    with duckdb.connect() as con, pytest.raises(ValueError) as error:
        verify_static(con, source, candidate, SCANS[:3])
    assert str(error.value) == "canonical ranges mismatch: cintervals: source=['r0000.parquet'], candidate=['r0001.parquet'], expected=['r0000.parquet']"
    with duckdb.connect() as con, pytest.raises(ValueError) as error:
        verify_ranges(con, source, candidate, SCANS[:3])
    assert str(error.value) == "required canonical table is absent: pv"


def test_missing_semantic_column_fails_schema_check(tmp_path):
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    _write(source, [_row()])
    _write(candidate, [_row()], SCHEMA.remove(SCHEMA.get_field_index("version")))
    with duckdb.connect() as con, pytest.raises(ValueError) as error:
        verify_static(con, source, candidate, SCANS[:3])
    assert str(error.value) == (
        "semantic schema mismatch: r0000.parquet: "
        "{'depth': 'uint8', 'last_read': 'int32', 'path': 'string', 'size': 'int64', 'version': 'string', 'wts': 'double'} != "
        "{'depth': 'uint8', 'last_read': 'int32', 'path': 'string', 'size': 'int64', 'wts': 'double'}"
    )


def test_invalid_intervals_do_not_disappear_silently(tmp_path):
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    _write(source, [_row()])
    path = _write(candidate, [_row(vt=STAMPS[0])])
    with duckdb.connect() as con, pytest.raises(ValueError) as error:
        verify_static(con, source, candidate, SCANS[:3])
    assert str(error.value) == f"{path}: 1 invalid validity intervals"


def test_declared_range_missing_from_both_copies_cannot_verify(tmp_path):
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    _write(source, [_row()])
    _write(candidate, [_row()])
    metadata = candidate / "ranges.json"
    metadata.write_text(json.dumps({"ranges": [{"i": 0}, {"i": 1}]}) + "\n")
    with duckdb.connect() as con, pytest.raises(ValueError) as error:
        verify_static(con, source, candidate, SCANS[:3])
    assert str(error.value) == (
        f"canonical range inventory differs from {metadata}: "
        "found=['r0000.parquet'], declared=['r0000.parquet', 'r0001.parquet']"
    )
