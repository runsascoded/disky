"""`dt-cloud interval-store export` (`dt_cloud.interval_export`): every scan of a store — v1 indexes in the base, a v2
scan in the base, v2 scans appended as runs (merged by carries) — exported from the store reads back as its original
per-scan sort, in the store's terms (count and Σ hash of its owner slices, `interval_store.slice_rows_sql`): export ∘
append is the identity. The v2 exports are `write_tiers`' cut (`path` and `bysize`, the same rows)."""
from __future__ import annotations

import json

import duckdb
import pyarrow.parquet as pq
from click.testing import CliRunner
from test_interval_local import DATES, local, world  # noqa: F401 (fixtures)

from dt_cloud import interval_export as ie
from dt_cloud import interval_store as ist
from dt_cloud.static_names import U64


def _digest(path: str, version: int) -> tuple[int, int]:
    con = duckdb.connect()
    n, s = con.execute(f"SELECT count(*), coalesce(sum({ist.SV_HASH}::HUGEINT), 0) FROM ({ist.slice_rows_sql(con, path, ['true'], version)})").fetchone()
    return n, int(s) % U64


def test_export_reproduces_every_scan(world, local, tmp_path):  # noqa: F811
    by_id = {s["id"]: s for s in world["scans"]}
    got, want, kv = {}, {}, {}
    for d in DATES:
        dest = tmp_path / "out" / d
        res = CliRunner().invoke(ie.export_cmd, ["-o", str(local["store"]), "-w", str(tmp_path / "work" / d), d, str(dest)], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        doc = json.loads(res.stdout)
        v = by_id[d]["version"]
        names = ["path-index.parquet", "path-index-bysize.parquet"] if v == 2 else ["path-index.parquet"]
        assert (doc["scan"], doc["version"], doc["sliced"], sorted(doc["files"])) == (d, v, True, sorted(names))
        assert sorted(p.name for p in dest.iterdir()) == sorted(names)
        want[d] = _digest(str(world["root"] / by_id[d]["src"]), v)
        got[d] = _digest(str(dest / "path-index.parquet"), v)
        assert doc["rows"] == want[d][0]
        if v == 2:
            assert _digest(str(dest / "path-index-bysize.parquet"), 2) == want[d]
            md = pq.read_metadata(dest / "path-index-bysize.parquet").metadata
            kv[d] = {k.decode(): md[k].decode() for k in (b"tier", b"sort", b"bucket")}
    assert got == want
    assert set(map(json.dumps, kv.values())) == {json.dumps({"tier": "bysize", "sort": "tot_bucket desc,path,usr", "bucket": "log2(tot)"})}


def test_export_refuses_a_scan_the_store_lacks(local, tmp_path):  # noqa: F811
    res = CliRunner().invoke(ie.export_cmd, ["-o", str(local["store"]), "2026-07-29", str(tmp_path / "x")])
    assert (res.exit_code, res.output.strip()) == (1, f"{local['store']}/interval-store/g: holds no scan 2026-07-29")
