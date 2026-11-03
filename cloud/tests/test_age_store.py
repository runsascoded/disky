"""Histogram history preserves every cell while unchanged cells open no new versions."""
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import age_store as age
from dt_cloud.blobstore import Store
from dt_cloud.interval_export import export_scan, store_gen

LAYOUT = "listing/{id}/index/{gen}/age-pyramid-1h.parquet"


def source(root: Path, scan: str, rows: list[tuple[str, int, int, int]]) -> None:
    out = root / "listing" / scan / "index" / "g"
    out.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist([
        {"path": p, "depth": 0 if not p else p.count("/") + 1, "binstart": t, "b": b, "o": o}
        for p, t, b, o in rows
    ])
    for tier in age.BINS:
        pq.write_table(table, out / f"age-pyramid-{tier}.parquet")


def rows(root: Path, scan: str, work: Path) -> list[tuple]:
    export_scan(str(root), scan, str(work / "out"), tiers=("path",), work=work / "read", threads=2, mem="1GB")
    with duckdb.connect() as con:
        return con.execute("SELECT path,size,n_files FROM read_parquet(?) ORDER BY path", [str(work / "out/path-index.parquet")]).fetchall()


def test_append_compact_and_zero_bytes(tmp_path: Path) -> None:
    src, dst = tmp_path / "source", tmp_path / "age"
    first = [("", 0, 10, 2), ("a/b !", 3600, 0, 3), ("gone", 0, 4, 1)]
    second = [("", 0, 10, 5), ("a/b !", 3600, 0, 3), ("new", 7200, 6, 1)]
    source(src, "2026-10-01", first)
    source(src, "2026-10-02", first)
    source(src, "2026-10-03", second)
    result = age.update(str(src), LAYOUT, str(dst), "g", tmp_path / "build", through="2026-10-02")
    assert result["added"] == [
        {"scan": "2026-10-01", "pass": True, "cells": 24},
        {"scan": "2026-10-02", "pass": True, "cells": 24},
    ]
    with duckdb.connect() as con:
        assert con.execute("SELECT count(*) FROM read_parquet(?)", [str(dst / "interval-store/g/pvl/*.parquet")]).fetchone() == (24,)
    before = rows(dst, "2026-10-01", tmp_path / "before")
    age.update(str(src), LAYOUT, str(dst), "g", tmp_path / "append", compact="every:1s")
    assert rows(dst, "2026-10-01", tmp_path / "old") == before
    assert rows(dst, "2026-10-02", tmp_path / "same") == before
    assert rows(dst, "2026-10-03", tmp_path / "new") == sorted([
        (f"{t}/p=/0", 10, 5) for t in age.BINS
    ] + [
        (f"{t}/p=a%2Fb%20%21/3600", 0, 3) for t in age.BINS
    ] + [
        (f"{t}/p=new/7200", 6, 1) for t in age.BINS
    ])
    assert store_gen(Store(str(dst)), None) != "g"
    assert [age.covered(str(dst), scan) for scan in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04")] == [True, True, True, False]
    assert age.update(str(src), LAYOUT, str(dst), "g", tmp_path / "repeat")["added"] == []


def test_failed_verification_publishes_nothing(tmp_path: Path, monkeypatch) -> None:
    src, dst = tmp_path / "source", tmp_path / "age"
    source(src, "2026-10-01", [("", 0, 1, 1)])

    def fail(*args, **kwargs):
        raise ValueError("comparison failed")

    monkeypatch.setattr(age, "verify", fail)
    with pytest.raises(ValueError, match="^comparison failed$"):
        age.update(str(src), LAYOUT, str(dst), "g", tmp_path / "build")
    assert Store(str(dst)).listing("") == {}


@pytest.mark.parametrize("retry_bytes", [1, 2])
def test_first_publication_can_retry_after_data_uploaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_bytes: int,
) -> None:
    src, dst = tmp_path / "source", tmp_path / "age"
    source(src, "2026-10-01", [("", 0, 1, 1)])
    put = Store.put

    def fail_catalog(
        self: Store,
        path: Path,
        key: str,
    ) -> None:
        if self.root == str(dst) and key == "interval-store/g/scans.json":
            raise OSError("upload interrupted")
        return put(self, path, key)

    monkeypatch.setattr(Store, "put", fail_catalog)
    with pytest.raises(OSError, match="^upload interrupted$"):
        age.update(str(src), LAYOUT, str(dst), "g", tmp_path / "build")
    assert Store(str(dst)).exists("interval-store/CURRENT.json") is False
    assert age.covered(str(dst), "2026-10-01") is False
    monkeypatch.setattr(Store, "put", put)
    source(src, "2026-10-01", [("", 0, retry_bytes, 1)])
    result = age.update(str(src), LAYOUT, str(dst), "g", tmp_path / "build")
    assert result["added"] == [{"scan": "2026-10-01", "pass": True, "cells": 8}]
    assert age.covered(str(dst), "2026-10-01") is True
    assert rows(dst, "2026-10-01", tmp_path / "retried") == sorted([
        (f"{tier}/p=/0", retry_bytes, 1) for tier in age.BINS
    ])


def test_retirement_requires_marker_manifest_and_d1_before_deleting(tmp_path: Path, monkeypatch) -> None:
    from dt_cloud import index_footer as F

    src, dst = tmp_path / "source", tmp_path / "age"
    dates = ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]
    for date in dates:
        source(src, date, [("", 0, 1, 1)])
        (src / "listing" / date / "index/g/attr.tsv").write_text("provenance\n")
    age.update(str(src), LAYOUT, str(dst), "g", tmp_path / "build")
    (dst / "verified/2026-10-02.json").unlink()
    # A forged marker for a scan the manifest doesn't hold is insufficient.
    source(src, "2026-09-30", [("", 0, 1, 1)])
    (dst / "verified/2026-09-30.json").write_text('{"pass":true}\n')
    first_keys = [f"listing/2026-10-01/index/g/age-pyramid-{tier}.parquet" for tier in sorted(age.BINS)]
    plan = age.retire(str(src), LAYOUT, str(dst), 2)
    assert (plan["execute"], plan["scans"], plan["files"], plan["d1_rows"]) == (False, ["2026-10-01"], first_keys, 0)
    before = Store(str(src)).listing("")
    monkeypatch.setattr(F, "_creds", lambda: ("", ""))
    with pytest.raises(ValueError, match=r"^age-store retirement requires D1 credentials \(or explicit --no-d1\)$"):
        age.retire(str(src), LAYOUT, str(dst), 2, execute=True)
    assert Store(str(src)).listing("") == before
    result = age.retire(str(src), LAYOUT, str(dst), 2, execute=True, site_store=None)
    assert (result["scans"], result["files"]) == (["2026-10-01"], first_keys)
    assert sorted(Store(str(src)).listing("")) == sorted(set(before) - set(first_keys))


@pytest.mark.parametrize("argv, threads, mem", [([], age.THREADS, age.MEM), (["-p", "16", "-M", "96GB"], 16, "96GB")])
def test_cli_passes_duckdb_sizing_to_every_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], threads: int, mem: str) -> None:
    """`-p` / `-M` reach `update` (its cells, interval build / add and verification); the defaults stay hf's."""
    from click.testing import CliRunner

    got: list[dict] = []
    monkeypatch.setattr(age, "update", lambda *a, **kw: got.append(kw) or {"gen": "g", "added": [], "files": 0})
    res = CliRunner().invoke(age.age_store_cmd, ["-g", "g", "-L", LAYOUT, "-o", str(tmp_path / "o"), "-s", str(tmp_path / "s"),
                                                 "-w", str(tmp_path / "w"), *argv], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert got == [{"through": None, "compact": "level:5", "threads": threads, "mem": mem}]
