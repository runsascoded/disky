"""`meta.started`: a scan's start from its listings' `_SUCCESS.json` markers (`scan_started`), written by
`path-index` and back-stamped by `stamp-started`."""
import json
from pathlib import Path

import pandas as pd
from click.testing import CliRunner

from dt_cloud.scan_started import listing_started, stamp_started
from dt_cloud.viz import write_path_index

STARTS = {"b1": "2026-10-09T04:31:02.123456+00:00", "b2": "2026-10-09T04:30:12.345678+00:00"}


def _listing(root: Path, scan: str) -> Path:
    """`<root>/listing/<scan>/<bucket>/shard-0.parquet` + `_SUCCESS.json` per bucket, as `bulk-list` writes them."""
    for b, started in STARTS.items():
        d = root / "listing" / scan / b
        d.mkdir(parents=True)
        pd.DataFrame({
            "bucket": [b], "name": ["a/x.bin"], "size_bytes": [10], "created": [pd.Timestamp("2026-10-01", tz="UTC")],
            "storage_class_id": [1],
        }).to_parquet(d / "shard-0.parquet")
        (d / "_SUCCESS.json").write_text(json.dumps({"bucket": b, "objects": 1, "started": started, "finished": started}))
    return root / "listing" / scan


def test_listing_started_is_the_earliest_marker(tmp_path: Path):
    lst = _listing(tmp_path, "2026-10-09")
    assert [
        listing_started((f"{lst}/*/shard-*.parquet",)),
        listing_started((f"{lst}/b1/*.parquet",)),
        listing_started((f"{tmp_path}/nowhere/*.parquet",)),
    ] == ["2026-10-09T04:30:12.345Z", "2026-10-09T04:31:02.123Z", None]


def test_path_index_writes_started(tmp_path: Path):
    lst = _listing(tmp_path, "2026-10-09")
    meta = write_path_index((f"{lst}/b1/*.parquet", f"{lst}/b2/*.parquet"), tmp_path / "out", "2026-10-09", (), None)
    assert meta["started"] == "2026-10-09T04:30:12.345Z"
    assert json.loads((tmp_path / "out" / "meta.json").read_text())["started"] == "2026-10-09T04:30:12.345Z"


def test_stamp_started_backstamps_date_only_metas(tmp_path: Path):
    _listing(tmp_path, "2026-10-09")
    metas = {
        "2026-10-08": {"asof": "2026-10-08", "published": "2026-10-08T06:00:00.000Z"},  # no listing markers (none carrying `started`)
        "2026-10-09": {"asof": "2026-10-09", "published": "2026-10-09T07:00:00.000Z"},
        "2026-10-09T1236": {"asof": "2026-10-09", "published": "2026-10-09T15:00:00.000Z"},  # timed: skipped by default
    }
    for sid, m in metas.items():
        (tmp_path / "snapshots" / sid).mkdir(parents=True)
        (tmp_path / "snapshots" / sid / "meta.json").write_text(json.dumps(m, indent=2) + "\n")
    meta_09 = tmp_path / "snapshots" / "2026-10-09" / "meta.json"
    before = meta_09.read_text()

    dry = CliRunner().invoke(stamp_started, ["-n", str(tmp_path)])
    assert dry.exit_code == 0, dry.output
    assert dry.stdout.splitlines() == [
        "2026-10-08\t-\t0\tno-started",
        "2026-10-09\t2026-10-09T04:30:12.345Z\t2\tstamp",
    ]
    assert dry.stderr == "2 scans: no-started 1, stamp 1 (dry run)\n"
    assert meta_09.read_text() == before

    run = CliRunner().invoke(stamp_started, [str(tmp_path)])
    assert run.exit_code == 0, run.output
    assert run.stdout.splitlines() == [
        "2026-10-08\t-\t0\tno-started",
        "2026-10-09\t2026-10-09T04:30:12.345Z\t2\tstamped",
    ]
    assert json.loads(meta_09.read_text()) == {**metas["2026-10-09"], "started": "2026-10-09T04:30:12.345Z"}

    again = CliRunner().invoke(stamp_started, ["-a", str(tmp_path), "2026-10-09", "2026-10-09T1236"])
    assert again.stdout.splitlines() == [
        "2026-10-09\t2026-10-09T04:30:12.345Z\t0\thas",
        "2026-10-09T1236\t-\t0\tno-started",
    ]
