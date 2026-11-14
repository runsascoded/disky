#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.1", "click>=8"]
# ///
"""The scanner reads owners from the folded `path` sort's `us` (its owner slices as one string); a sliced generation
also serves them as rows, the `slices` sort (one row per `(depth, path, usr)` version). This checks the two agree on
objects: at each given scan, per (bucket, owner), the bytes and objects `us` assigns equal the `slices` rows' (base +
the newest manifest's runs, each combined as `combineLive`). Exit 1 on any difference."""
import json
import sys
from functools import partial
from pathlib import Path

import click
import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from oracle import generation, lit  # noqa: E402

err = partial(print, file=sys.stderr)


@click.command()
@click.option("-m", "--memory", default="80GB", help="DuckDB memory limit")
@click.option("-s", "--scan", "scan_ids", multiple=True, help="Scan ids to check (default: the newest)")
@click.option("-t", "--threads", default=16, type=int)
@click.option("-T", "--tmp", default="tmp/duckdb")
@click.argument("gen")
def main(memory, scan_ids, threads, tmp, gen):
    gen = Path(gen)
    scans, files = generation(gen)
    stamps = {s["id"]: s["ts"] for s in scans}
    slices = [f.replace("/served/path.parquet", "/served/slices.parquet") for f in files]
    con = duckdb.connect()
    con.execute(f"SET memory_limit = {lit(memory)}; SET threads = {threads}; SET temp_directory = {lit(tmp)}; SET preserve_insertion_order = false")
    bad = 0
    for sid in scan_ids or [scans[-1]["id"]]:
        t = stamps[sid]
        live = f"vf <= {t} AND {t} < vt AND kind = 'file'"

        def combined(srcs: list[str], key: str, cols: str) -> str:
            src = "[" + ", ".join(lit(f) for f in srcs) + "]"
            return f"""(SELECT {key}, min(vt) AS vt, {cols} FROM read_parquet({src}, union_by_name = true)
                       WHERE vf <= {t} AND kind = 'file' GROUP BY {key})"""

        us = con.execute(f"""
            WITH v AS {combined(files, 'depth, path, vf', 'any_value(kind) AS kind, any_value(size) AS size, any_value(us) AS us')},
            l AS (SELECT split_part(path, '/', 1) AS bucket, size, us FROM v WHERE {t} < vt),
            j AS (SELECT bucket, size, us, CASE WHEN starts_with(us, '[') THEN from_json(us, '[["VARCHAR"]]') END AS js FROM l),
            sl AS (
                SELECT bucket, '' AS u, size AS b, 1 AS n FROM j WHERE us = ''
                UNION ALL SELECT bucket, us, size, 1 FROM j WHERE us <> '' AND NOT starts_with(us, '[')
                UNION ALL SELECT bucket, e[1], e[2]::BIGINT, 1 FROM (SELECT bucket, unnest(js) AS e FROM j WHERE js IS NOT NULL)
                UNION ALL SELECT bucket, '', size - list_sum(list_transform(js, e -> e[2]::BIGINT)), 1 FROM j
                    WHERE js IS NOT NULL AND size > list_sum(list_transform(js, e -> e[2]::BIGINT)))
            SELECT bucket, u, sum(b)::BIGINT, sum(n)::BIGINT FROM sl GROUP BY ALL ORDER BY ALL""").fetchall()
        sv = con.execute(f"""
            WITH v AS {combined(slices, 'depth, path, usr, vf', 'any_value(kind) AS kind, any_value(size) AS size')}
            SELECT split_part(path, '/', 1), coalesce(usr, ''), sum(size)::BIGINT, count(*)::BIGINT FROM v WHERE {t} < vt
            GROUP BY ALL ORDER BY ALL""").fetchall()
        same = us == sv
        bad += not same
        err(f"{sid}: {len(us)} (bucket, owner) cells from `us`, {len(sv)} from `slices`: {'equal' if same else 'DIFFERENT'}")
        if not same:
            a, b = set(us), set(sv)
            err(f"  us-only {sorted(a - b)[:8]}\n  slices-only {sorted(b - a)[:8]}")
        print(json.dumps({"scan": sid, "cells": len(us), "equal": same}))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
