"""A retired scan remains usable by the over-time grouper through the interval store."""
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from dt_cloud import overtime as ot
from test_interval_generation import ON, SCANS, _build, world  # noqa: F401


def test_retired_inputs_produce_identical_over_time_groups(world, tmp_path: Path) -> None:  # noqa: F811
    store, work = tmp_path / "store", tmp_path / "work"
    _build(world, store, work / "build", ON, len(SCANS) - 1)
    dates = [SCANS[0], SCANS[3], SCANS[-1]]
    originals = [(date, str(world / ON.format(id=date))) for date in dates]
    assert ot.group_source(dates[0], originals[0][1], work, interval_store=None, mem="512MB", threads=1) == originals[0][1]
    reconstructed = [(date, ot.group_source(date, str(tmp_path / "retired" / date / "path-index.parquet"), work,
                                            interval_store=str(store), mem="512MB", threads=1)) for date in dates]
    ref = ot.write_over_time_index(originals, tmp_path / "reference", mem="512MB", threads=1)
    actual = ot.write_over_time_index(reconstructed, tmp_path / "actual", mem="512MB", threads=1)
    assert pq.read_table(actual["file"]).to_pylist() == pq.read_table(ref["file"]).to_pylist()
    assert Path(actual["scans_file"]).read_bytes() == Path(ref["scans_file"]).read_bytes()


def test_missing_secondary_input_cannot_read_the_primary_interval_store(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError) as failure:
        ot.group_source("2026-10-01", "missing/index.parquet", tmp_path,
                        interval_store=None, mem="512MB", threads=1)
    assert str(failure.value) == "missing/index.parquet: per-scan index is unavailable and no interval-store source is configured"
