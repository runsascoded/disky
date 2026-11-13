#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click>=8"]
# ///
"""The slim-index measurements' Markdown tables (`run-slim`'s JSONL), for specs/full-scan-serving-bench.md."""
import json
from collections import defaultdict
from statistics import median

import click


def lines(*files):
    for f in files:
        for line in open(f):
            if line.strip():
                yield json.loads(line)


def ms(x: float) -> str:
    return f"{x:,.0f}" if x >= 100 else f"{x:.1f}" if x >= 1 else f"{x:.2f}"


def gb(b: float) -> str:
    return f"{b / 1e9:.2f}"


def kind(tags: list[str]) -> str:
    return "/".join(tags[:2])


@click.group()
def cli():
    pass


@cli.command()
@click.argument("slim_json")
def groups(slim_json):
    """Bytes per column group (`fsb slim`'s output)."""
    s = json.load(open(slim_json))
    c = s["columns"]
    print(f"{c['rows']:,} object versions, {c['dirs']:,} directories, {c['names']:,} names")
    print()
    print("| Column group | GB | B / object version |")
    print("|---|---:|---:|")
    for g in c["groups"]:
        print(f"| {g['group']} | {gb(g['bytes'])} | {g['per_row']:.2f} |")
    print(f"| **total** | **{gb(c['total'])}** | **{c['per_row']:.2f}** |")


@cli.command()
@click.argument("files", nargs=-1)
def cold(files):
    """Time to the first answer per pack and storage (`run-slim cold`)."""
    print("| Run | Pack GB | Map + prefault s | MiB/s | First answer, s since process start | First query ms | Peak RSS GB | Peak anonymous GB |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for f in files:
        load = first = None
        for r in lines(f):
            if r["kind"] == "load":
                load = r
            elif r["kind"] == "first":
                first = r
        o = load["open"]
        pf = o.get("prefault_s")
        load_s = f"{o['load_s']:.1f}" if pf else "–"
        mib_s = f"{o['prefault_mib_s']:,.0f}" if pf else "–"
        print(
            f"| {load['label']} | {gb(o['file_bytes'])} | {load_s} | {mib_s} "
            f"| {first['proc']['since_start_s']:.2f} | {ms(first['ms'])} | {first['proc']['hwm_gb']:.1f} | {first['proc']['anon_gb']:.2f} |"
        )


@cli.command()
@click.option("-b", "--baseline", help="The first run's results (full index from the store), for its 16-thread column")
@click.argument("files", nargs=-1)
def threads(baseline, files):
    """Warm median latency by query kind and thread count; bytes each kind reads."""
    rs = [r for r in lines(*files) if r["kind"] == "query"]
    base = {}
    if baseline:
        base = {r["id"]: r for r in lines(baseline) if r["kind"] == "query" and r["threads"] == 16}
    labels = sorted({r["label"] for r in rs}, key=lambda l: (l.startswith("full"), int(l.rsplit("t", 1)[1])))
    by = defaultdict(dict)
    for r in rs:
        by[r["id"]][r["label"]] = r
    kinds = defaultdict(list)
    for qid, d in by.items():
        kinds[kind(next(iter(d.values()))["tags"])].append(qid)
    cols = labels + (["first run, full, 16 threads"] if base else [])
    print("Warm median per query, then the median (max) over a kind's queries, ms:")
    print()
    print("| Kind | Queries | " + " | ".join(cols) + " | Slim reads (GB / query, median) |")
    print("|---|---:|" + "---:|" * (len(cols) + 1))
    for k, ids in kinds.items():
        cells = []
        for l in labels:
            v = [by[i][l]["warm_median_ms"] for i in ids if l in by[i]]
            cells.append(f"{ms(median(v))} ({ms(max(v))})" if v else "–")
        if base:
            v = [base[i]["warm_median_ms"] for i in ids if i in base]
            cells.append(f"{ms(median(v))} ({ms(max(v))})" if v else "–")
        sl = next(l for l in labels if l.startswith("slim"))
        rd = median(by[i][sl]["bytes"] for i in ids if sl in by[i])
        print(f"| {k} | {len(ids)} | " + " | ".join(cells) + f" | {gb(rd)} |")


@cli.command()
@click.argument("files", nargs=-1)
def cap(files):
    """Per query under each memory cap: cold (first touch) and warm, with what each read from storage."""
    rs = [r for r in lines(*files) if r["kind"] == "query"]
    labels = list(dict.fromkeys(r["label"] for r in rs))
    by = defaultdict(dict)
    for r in rs:
        by[r["id"]][r["label"]] = r
    print("| Query | " + " | ".join(f"{l}: cold ms (GB read) | {l}: warm ms (GB read)" for l in labels) + " |")
    print("|---|" + "---:|---:|" * len(labels))
    for qid, d in by.items():
        cells = []
        for l in labels:
            r = d.get(l)
            if not r:
                cells += ["–", "–"]
                continue
            n = len(r["warm_ms"])
            cells.append(f"{ms(r['cold_ms'])} ({gb(r['cold_read_bytes'])})")
            cells.append(f"{ms(r['warm_median_ms'])} ({gb(r['warm_read_bytes'] / n)})")
        print(f"| {qid} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    cli()
