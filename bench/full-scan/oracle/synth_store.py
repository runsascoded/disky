#!/usr/bin/env python
"""Cut `fsb synth`'s staged history into a real interval-store generation with the repo's own code (run in the repo's
venv): the base's range files (`pvl/r<depth>.parquet`, one disjoint `(depth, path)` range per depth, each sorted), its
served `path` sort by `interval_store.cut_gen` (dyadic segments, `.groups.parquet`), each run scan's served `path` sort by
`interval_store.write_served` (open versions then close records, as `interval_append.cut_run`), and a manifest per run
scan (each listing every run so far at level 0)."""
import json
import shutil
import sys
from functools import partial
from pathlib import Path
from time import monotonic

import click
import duckdb

from dt_cloud import interval_store as ist

err = partial(print, file=sys.stderr)


def q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


@click.command()
@click.option("-m", "--memory", default="40GB", help="DuckDB memory limit")
@click.option("-t", "--threads", default=16, type=int, help="DuckDB threads")
@click.argument("stage")
@click.argument("gen")
def main(memory, threads, stage, gen):
    """STAGE (`fsb synth -o`) → generation dir GEN (must not exist)."""
    stage, gen = Path(stage), Path(gen)
    if gen.exists():
        raise SystemExit(f"{gen} exists")
    doc = json.loads((stage / "scans.json").read_text())
    k = doc["base_scans"]
    base_scans, run_scans = doc["scans"][:k], doc["scans"][k:]
    (gen / "pvl").mkdir(parents=True)
    tmp = gen.parent / f".{gen.name}.duckdb-tmp"
    con = duckdb.connect()
    con.execute(f"SET memory_limit = {q(memory)}; SET threads = {threads}; SET temp_directory = {q(str(tmp))}; SET preserve_insertion_order = false")
    cols = ", ".join(f"{c}::{t} AS {c}" for c, t in [
        ("depth", "UTINYINT"), ("path", "VARCHAR"), ("vf", "BIGINT"), ("vt", "BIGINT"), ("kind", "VARCHAR"), ("size", "BIGINT"),
        ("n_files", "BIGINT"), ("n_children", "BIGINT"), ("n_desc", "BIGINT"), ("mtime", "BIGINT"), ("wts", "DOUBLE"),
        ("wb", "BIGINT"), ("c2", "BIGINT"), ("c3", "BIGINT"), ("c4", "BIGINT"), ("us", "VARCHAR"), ("last_read", "INTEGER")])
    t0 = monotonic()
    for f in sorted((stage / "base").glob("d*.parquet")):
        d = int(f.stem[1:])
        con.execute(f"COPY (SELECT {cols} FROM read_parquet({q(str(f))}) ORDER BY path, vf) TO {q(str(gen / 'pvl' / f'r{d:04d}.parquet'))} (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 65536)")
    err(f"range files: {monotonic() - t0:.1f}s")
    scans = {"scans": base_scans, "slices": True}
    (gen / "scans.json").write_text(json.dumps(scans, indent=1) + "\n")
    t0 = monotonic()
    cut, _ = ist.cut_gen(con, gen, scans, sorts=["path"])
    err(f"base path sort: {cut} ({monotonic() - t0:.1f}s)")
    runs = []
    stamps = {}
    for s in run_scans:
        key = f"deltas/{s['id']}"
        out = gen / key / "served"
        out.mkdir(parents=True)
        t0 = monotonic()
        src = f"(SELECT {cols} FROM read_parquet({q(str(stage / 'runs' / (s['id'] + '.parquet')))}))"
        d = ist.write_served(con, src, "path", out / "path.parquet", ist.served_schema("path"), page_rows=ist.served_page_rows("path"))
        stamps[s["id"]] = s["ts"]
        runs.append({"key": key, "first": s["id"], "last": s["id"], "level": 0, "scans": [s["id"]], "rows": d["rows"], "bytes": d["bytes"]})
        m = {"gen": gen.name, "date": s["id"], "base_scans": len(base_scans), "scans": [x["id"] for x in base_scans] + list(stamps),
             "stamps": dict(stamps), "runs": list(runs)}
        (gen / "manifests").mkdir(exist_ok=True)
        (gen / "manifests" / f"{s['id']}.json").write_text(json.dumps(m, indent=1) + "\n")
        err(f"run {s['id']}: {d['rows']:,} rows ({monotonic() - t0:.1f}s)")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
