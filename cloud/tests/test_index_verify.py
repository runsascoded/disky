"""`dt-cloud age-pyramid` and `dt-cloud index-verify` (`index_verify.py`) over a tiny scan: two buckets' listing, the
`path` sort it aggregates to, and the snapshot JSONs."""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner

from dt_cloud import cli as C
from dt_cloud.cli import main
from dt_cloud.index import AGE_PYRAMID_BINS

T1, T2 = 1_790_000_000, 1_790_100_000


@pytest.fixture
def scan(tmp_path: Path) -> Path:
    """`listing/shard-0.parquet`, `index/path-index.parquet`, `snap/{meta,age}.json`: bucket `a` holds two dated files
    (10 + 20 B), `b` one undated (5 B), `c` (a source) nothing."""
    (tmp_path / "listing").mkdir()
    (tmp_path / "index").mkdir()
    (tmp_path / "snap").mkdir()
    con = duckdb.connect()
    con.execute(f"""COPY (SELECT * FROM (VALUES
        ('a', 'x', 10::BIGINT, to_timestamp({T1})),
        ('a', 'y', 20::BIGINT, to_timestamp({T2})),
        ('b', 'z', 5::BIGINT, NULL::TIMESTAMPTZ)
      ) t(bucket, name, size_bytes, created)) TO '{tmp_path}/listing/shard-0.parquet' (FORMAT parquet)""")
    con.execute(f"""COPY (SELECT * FROM (VALUES
        ('a', 1, 30::BIGINT, 2::BIGINT, 'dir', {T2}::BIGINT),
        ('b', 1, 5::BIGINT, 1::BIGINT, 'dir', 0::BIGINT),
        ('a/x', 2, 10::BIGINT, 0::BIGINT, 'file', {T1}::BIGINT),
        ('a/y', 2, 20::BIGINT, 0::BIGINT, 'file', {T2}::BIGINT),
        ('b/z', 2, 5::BIGINT, 0::BIGINT, 'file', 0::BIGINT)
      ) t(path, depth, size, n_files, kind, mtime)) TO '{tmp_path}/index/path-index.parquet' (FORMAT parquet)""")
    (tmp_path / "snap/meta.json").write_text(json.dumps({"total_bytes": 35, "total_objects": 3}))
    (tmp_path / "snap/age.json").write_text(json.dumps([{"b": 10, "o": 1}, {"b": 20, "o": 1}]))
    return tmp_path


@pytest.fixture
def logged(monkeypatch) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr(C, "err", lambda *a: lines.append(" ".join(map(str, a))))
    return lines


def verify_argv(scan: Path, buckets: str = "abc") -> list[str]:
    return ["index-verify", "-d", "2026-10-16T1200", "-g", "G1", *[a for b in buckets for a in ("-b", b)], "-l", f"{scan}/listing/*.parquet",
            "-s", str(scan / "snap"), "-o", str(scan / "index/verification.json"), str(scan / "index/path-index.parquet")]


def test_age_pyramid_then_verify(scan, logged):
    pyr = CliRunner().invoke(main, ["age-pyramid", "-c", "-t", "2", "-o", str(scan / "index"), str(scan / "index/path-index.parquet")])
    assert pyr.exit_code == 0, pyr.output
    assert (sorted(p.name for p in (scan / "index").iterdir()), logged) == (
        sorted(["path-index.parquet", *(f"age-pyramid-{b}.parquet" for b in AGE_PYRAMID_BINS)]),
        [f"age-pyramid: floor 1, 8 tiers → {scan}/index (checked)"],
    )
    logged.clear()
    r = CliRunner().invoke(main, verify_argv(scan))
    assert r.exit_code == 0, r.output
    report = {"scan": "2026-10-16T1200", "generation": "G1",
              "buckets": {"a": {"bytes": 30, "objects": 2}, "b": {"bytes": 5, "objects": 1}, "c": {"bytes": 0, "objects": 0}},
              "totals": {"bytes": 35, "objects": 3}}
    assert (json.loads(r.stdout), json.loads((scan / "index/verification.json").read_text()), logged) == (
        report, report, ["index-verify 2026-10-16T1200: 3 objects, 35 B over 3 buckets: ok"])


def test_verify_refuses_a_mismatch(scan, logged):
    (scan / "snap/meta.json").write_text(json.dumps({"total_bytes": 34, "total_objects": 3}))
    r = CliRunner().invoke(main, verify_argv(scan))
    assert (r.exit_code, logged, (scan / "index/verification.json").exists()) == (
        1, ["index-verify 2026-10-16T1200: snapshot totals 34 B / 3 objects ≠ the listings' {'bytes': 35, 'objects': 3}"], False)


def test_verify_refuses_an_unknown_bucket(scan, logged):
    r = CliRunner().invoke(main, verify_argv(scan, "ac"))
    assert (r.exit_code, logged) == (1, ["index-verify 2026-10-16T1200: listing bucket 'b' is not one of the scan's (a, c)"])


def test_age_pyramid_check_catches_a_wrong_tier(scan, tmp_path):
    """A tier that doesn't sum to the path sort's dated files fails the check."""
    from dt_cloud.index_verify import Mismatch, check_age_pyramid, connect

    assert CliRunner().invoke(main, ["age-pyramid", "-o", str(scan / "index"), str(scan / "index/path-index.parquet")]).exit_code == 0
    con = connect()
    tier = scan / "index/age-pyramid-8d.parquet"
    floor = con.execute(f"SELECT value FROM parquet_kv_metadata('{tier}') WHERE key = 'coarse_floor'").fetchone()[0].decode()
    con.execute(f"COPY (SELECT path, depth, binstart, b * 2 AS b, o FROM read_parquet('{tier}')) TO '{tmp_path}/t.parquet' "
                f"(FORMAT parquet, KV_METADATA {{coarse_floor: '{floor}'}})")
    Path(f"{tmp_path}/t.parquet").replace(tier)
    with pytest.raises(Mismatch) as e:
        check_age_pyramid(con, scan / "index/path-index.parquet", scan / "index")
    assert str(e.value) == ("age pyramid 8d: depth-1 [('a', 60, 2)] / root (60, 2); the path sort's dated files give "
                            "[('a', 30, 2)] / (30, 2)")
