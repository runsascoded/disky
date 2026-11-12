#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click>=8"]
# ///
"""Summarize `fsb bench` results (JSONL) as the spec's Markdown tables: per store, its size and the root's times; per
store and thread count, the query mix's warm latency by kind."""
import json
from collections import defaultdict
from statistics import median

import click


def gb(b: float) -> str:
    return f"{b / 1e9:.2f} GB"


def ms(x: float) -> str:
    return f"{x:,.0f}" if x >= 100 else f"{x:.1f}"


@click.command()
@click.option("-k", "--kinds", is_flag=True, help="Also the per-kind latency table")
@click.argument("files", nargs=-1)
def main(kinds, files):
    loads, qs = {}, defaultdict(list)
    for f in files:
        for line in open(f):
            r = json.loads(line)
            if r["kind"] == "load":
                loads[r["label"]] = r
            else:
                qs[r["label"]].append(r)
    print("| Store | Versions read | Rows (objects / dirs) | Resident | B / object version | Load | Root rows/s/thread | Root 1 thread | Root 16 threads | All queries, 16 threads: p50 / p90 / max |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for label, l in loads.items():
        s, c = l["stats"], l["columns"]
        rs = {(r["id"], r["threads"]): r for r in qs[label]}
        tmax = max(r["threads"] for r in qs[label])
        r1, rn = rs.get(("root", 1)), rs.get(("root", tmax))
        warm = sorted(r["warm_median_ms"] for r in qs[label] if r["threads"] == tmax)
        p = lambda q: warm[min(len(warm) - 1, int(q * len(warm)))]
        print(f"| {label} | {s['versions_read']:,} | {s['rows']:,} ({s['file_rows']:,} / {s['dir_rows']:,}) | {gb(c['total'])} "
              f"| {c['total'] / s['file_rows']:.1f} | {l['load_s']:.0f} s | {r1['rows_per_s'] / 1e6:.0f}M | {ms(r1['warm_median_ms'])} ms "
              f"| {ms(rn['warm_median_ms'])} ms | {ms(p(0.5))} / {ms(p(0.9))} / {ms(warm[-1])} ms |")
    if kinds:
        print()
        print("| Store | Threads | Kind | Queries | Warm median (p50 / max) | Cold (max) | Rows |")
        print("|---|---:|---|---:|---:|---:|---:|")
        for label, rows in qs.items():
            by = defaultdict(list)
            for r in rows:
                kind = r["tags"][0] if r["tags"] else "other"
                by[(r["threads"], kind)].append(r)
            for (t, kind), rs in sorted(by.items()):
                w = [r["warm_median_ms"] for r in rs]
                print(f"| {label} | {t} | {kind} | {len(rs)} | {ms(median(w))} / {ms(max(w))} ms | {ms(max(r['cold_ms'] for r in rs))} ms "
                      f"| {median(r['rows'] for r in rs):,.0f} |")


if __name__ == "__main__":
    main()
