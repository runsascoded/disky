#!/usr/bin/env python3
"""specs/vm-mode.md §11's tables from `run-vmd`'s results (`vmd_bench.py` lines): per query, each run's warm median
(ms, client wall time over HTTP) and its cold time; memory before and after.

    vmd_summary.py DIR
"""
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def lines(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def fmt(x: float | None) -> str:
    if x is None:
        return "–"
    return f"{x:,.0f}" if x >= 100 else f"{x:.1f}"


def main() -> None:
    d = Path(sys.argv[1])
    runs = {p.stem.removeprefix("bench-").removeprefix("pair-"): lines(p) for p in sorted(d.glob("bench-*.jsonl")) + sorted(d.glob("pair-*.jsonl"))}
    for name, ls in runs.items():
        qs = [l for l in ls if l["kind"] == "query"]
        if not qs:
            continue
        tenants = sorted({l["tenant"] for l in qs}, key=[l["tenant"] for l in qs].index)
        by = defaultdict(dict)
        for l in qs:
            by[l["id"]][l["tenant"]] = l
        print(f"\n### {name}\n")
        print("| Query | " + " | ".join(f"{t}: warm (cold) ms" for t in tenants) + " | Rows | Nodes |")
        print("|---|" + "---:|" * (len(tenants) + 2))
        for q, m in by.items():
            first = next(iter(m.values()))
            cells = [f"{fmt(m[t]['warm_median_ms'])} ({fmt(m[t]['cold_ms'])})" if t in m else "–" for t in tenants]
            print(f"| `{q}` | " + " | ".join(cells) + f" | {first['rows']:,} | {first['nodes']} |")
        for t in tenants:
            w = [l["warm_median_ms"] for l in qs if l["tenant"] == t and l["warm_median_ms"] is not None]
            if w:
                q = statistics.quantiles(w, n=10) if len(w) >= 10 else None
                print(f"\n{t}: {len(w)} queries, warm p50 {fmt(statistics.median(w))} ms" + (f", p90 {fmt(q[8])} ms" if q else "") + f", max {fmt(max(w))} ms")
        mem = [l for l in ls if l["kind"] in ("start", "end")]
        for l in mem:
            if l.get("mem"):
                print(f"{l['kind']}: " + ", ".join(f"{k} {v:.1f} GB" for k, v in l["mem"].items()))
        bad = [l["bad"] for l in ls if l["kind"] == "end" and l.get("bad")]
        checked = sum(1 for l in qs if l.get("checked"))
        print(f"reference-checked answers: {checked}; problems: {bad or 'none'}")
    for p in sorted(d.glob("cold-*.jsonl")):
        ls = lines(p)
        hot = [l["hot_s"] for l in ls if l["kind"] == "hot"]
        first = [(l["tenant"], l["cold_ms"]) for l in ls if l["kind"] == "query"]
        end = [l["mem"] for l in ls if l["kind"] == "end"]
        print(f"\ncold {p.stem}: every tenant hot in " + (f"{hot[0]:.1f} s" if hot else "(not recorded)") + "; first query " + ", ".join(f"{t} {ms:.0f} ms" for t, ms in first) + (f"; {end[0]}" if end else ""))


if __name__ == "__main__":
    main()
