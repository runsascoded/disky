"""Tests for listing-source normalization (scan_gcs + SII inventory schemas)."""

import datetime as dt
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from disk_tree.listing import prepare_listing

TS1 = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
TS2 = dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc)


@pytest.fixture
def scan(tmp_path: Path) -> str:
    """scan_gcs schema: buckets b1 (stale copy) + c2 (only source for c2)."""
    path = tmp_path / "scan.parquet"
    pd.DataFrame(
        {
            "bucket": ["b1", "c2"],
            "name": ["x/stale.bin", "y/c2.bin"],
            "size_bytes": [11, 22],
            "created": [TS1, TS1],
            "storage_class_id": [1, 2],
        }
    ).to_parquet(path)
    return str(path)


@pytest.fixture
def sii(tmp_path: Path) -> str:
    """SII inventory schema for bucket b1, incl. a soft-deleted row to drop."""
    path = tmp_path / "sii.parquet"
    pd.DataFrame(
        {
            "bucket": ["b1", "b1", "b1"],
            "name": ["x/fresh.bin", "z/cold.bin", "gone.bin"],
            "size": [100, 200, 300],
            "timeCreated": [TS2, TS2, TS2],
            "timeDeleted": [None, None, TS2],
            "storageClass": ["STANDARD", "COLDLINE", "STANDARD"],
        }
    ).to_parquet(path)
    return str(path)


def rows(con: duckdb.DuckDBPyConnection, src: str) -> list[tuple]:
    # epoch() rather than raw TIMESTAMPTZ: materializing tz-aware values into
    # Python needs pytz, which the pipeline itself never requires
    return con.execute(
        f"SELECT bucket, name, size_bytes, epoch(created)::BIGINT, storage_class_id FROM {src} ORDER BY bucket, name"
    ).fetchall()


def test_sii_normalization(sii: str):
    con = duckdb.connect()
    assert rows(con, prepare_listing(con, (sii,))) == [
        ("b1", "x/fresh.bin", 100, int(TS2.timestamp()), 1),
        ("b1", "z/cold.bin", 200, int(TS2.timestamp()), 3),
    ]  # soft-deleted gone.bin dropped; class strings mapped to ids


def test_scan_passthrough(scan: str):
    con = duckdb.connect()
    assert rows(con, prepare_listing(con, (scan,))) == [
        ("b1", "x/stale.bin", 11, int(TS1.timestamp()), 1),
        ("c2", "y/c2.bin", 22, int(TS1.timestamp()), 2),
    ]


def test_multi_source_bucket_priority(sii: str, scan: str):
    # earlier source (SII) wins for b1; scan contributes only its c2 rows
    con = duckdb.connect()
    assert rows(con, prepare_listing(con, (sii, scan))) == [
        ("b1", "x/fresh.bin", 100, int(TS2.timestamp()), 1),
        ("b1", "z/cold.bin", 200, int(TS2.timestamp()), 3),
        ("c2", "y/c2.bin", 22, int(TS1.timestamp()), 2),
    ]


def test_updated_passes_through_where_the_listing_has_it(tmp_path: Path, scan: str, sii: str):
    """`updated` (GCS's second stamp) is a canonical column: verbatim from a
    listing that carries it, NULL from one that doesn't — every source
    exposes it, so a multi-source union lines up."""
    path = tmp_path / "scan-updated.parquet"
    pd.DataFrame(
        {
            "bucket": ["d3"], "name": ["w.bin"], "size_bytes": [5], "created": [TS1], "storage_class_id": [1], "updated": [TS2],
        }
    ).to_parquet(path)
    con = duckdb.connect()
    q = "SELECT bucket, name, epoch(created)::BIGINT, epoch(updated)::BIGINT FROM {src} ORDER BY bucket, name"
    assert con.execute(q.format(src=prepare_listing(con, (str(path), sii, scan)))).fetchall() == [
        ("b1", "x/fresh.bin", int(TS2.timestamp()), None),
        ("b1", "z/cold.bin", int(TS2.timestamp()), None),
        ("c2", "y/c2.bin", int(TS1.timestamp()), None),
        ("d3", "w.bin", int(TS1.timestamp()), int(TS2.timestamp())),
    ]


def test_unrecognized_schema_raises(tmp_path: Path):
    path = tmp_path / "junk.parquet"
    pd.DataFrame({"foo": [1]}).to_parquet(path)
    con = duckdb.connect()
    with pytest.raises(ValueError, match="unrecognized listing schema"):
        prepare_listing(con, (str(path),))
