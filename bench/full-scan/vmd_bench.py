#!/usr/bin/env python3
"""Time a bench query file through a running `vmd`'s HTTP API (specs/vm-mode.md §11): each query once cold, then
`-r` warm runs per tenant; every warm answer equal to the cold one, and (`-A`) to a reference answer file's (the
bench's oracle-checked answers). One JSONL line per (tenant, query) to `-o`; the process's memory (`-p`: vmd's pid,
from /proc) before and after; a summary to stderr.

    vmd_bench.py -u URL -k KEY_ENV -t TENANT [-t …] [-r 5] [-A REF] [-p PID] [-l LABEL] -o OUT QUERIES
"""
import json
import os
import statistics
import sys
import time
from argparse import ArgumentParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vmd_client import Vmd, body_of  # noqa: E402


def mem(pid: int | None) -> dict | None:
    """vmd's resident memory (GB): all, anonymous, file-backed (the mapped packs' pages)."""
    if pid is None:
        return None
    out = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        k, _, v = line.partition(":")
        if k in ("VmRSS", "RssAnon", "RssFile", "VmHWM"):
            out[k] = int(v.split()[0]) * 1024 / 1e9
    return out


def main() -> int:
    ap = ArgumentParser()
    ap.add_argument("-a", "--answers", help="Write each tenant's answers here (JSONL, `tenant` added)")
    ap.add_argument("-A", "--ref")
    ap.add_argument("-k", "--key-env", required=True)
    ap.add_argument("-l", "--label", default="")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("-p", "--pid", type=int)
    ap.add_argument("-r", "--reps", type=int, default=5)
    ap.add_argument("-t", "--tenant", action="append", required=True)
    ap.add_argument("-u", "--url", default="http://127.0.0.1:7340")
    ap.add_argument("queries")
    a = ap.parse_args()
    key = os.environ[a.key_env].encode()
    ref = {}
    if a.ref:
        for line in Path(a.ref).read_text().splitlines():
            if line.strip():
                x = json.loads(line)
                ref[x["id"]] = x
    qs = [json.loads(l) for l in Path(a.queries).read_text().splitlines() if l.strip()]
    bad = []
    fa = open(a.answers, "w") if a.answers else None
    with open(a.out, "w") as f:
        f.write(json.dumps({"kind": "start", "label": a.label, "mem": mem(a.pid)}) + "\n")
        for t in a.tenant:
            v = Vmd(a.url, t, key)
            st, s = v.get("/v1/status")
            if st != 200 or s["state"] != "hot":
                print(f"{t}: not hot: {s}", file=sys.stderr)
                return 1
            pins = s["current"]["pins"]
            for q in qs:
                b = body_of(q, pins)
                if b is None:
                    continue
                runs = []
                for i in range(1 + a.reps):
                    t0 = time.perf_counter()
                    code, r = v.post("/v1/view", b)
                    wall = (time.perf_counter() - t0) * 1e3
                    if code != 200:
                        bad.append((t, q["id"], code, r.get("message")))
                        break
                    runs.append((wall, r))
                if len(runs) != 1 + a.reps:
                    continue
                ans = [{**r["answer"], "id": q["id"]} for _, r in runs]
                if any(x != ans[0] for x in ans[1:]):
                    bad.append((t, q["id"], "a warm answer differs from the cold one"))
                if q["id"] in ref and ref[q["id"]] != ans[0]:
                    bad.append((t, q["id"], "differs from the reference"))
                if fa:
                    fa.write(json.dumps({**ans[0], "tenant": t}) + "\n")
                cold_wall, cold = runs[0]
                warm = [w for w, _ in runs[1:]]
                line = {
                    "kind": "query", "label": a.label, "tenant": t, "id": q["id"], "tags": q.get("tags", []),
                    "cold_ms": cold_wall, "cold_server_ms": cold["timing"]["total_ms"],
                    "warm_ms": warm, "warm_median_ms": statistics.median(warm) if warm else None,
                    "warm_server_median_ms": statistics.median(r["timing"]["total_ms"] for _, r in runs[1:]) if warm else None,
                    "rows": cold["timing"]["rows"], "nodes": cold["nodes"], "checked": q["id"] in ref,
                }
                f.write(json.dumps(line) + "\n")
                print(f"{a.label} {t} {q['id']}: cold {cold_wall:.1f} ms, warm {line['warm_median_ms'] or 0:.1f} ms, {cold['nodes']} nodes", file=sys.stderr)
        f.write(json.dumps({"kind": "end", "label": a.label, "mem": mem(a.pid), "bad": bad}) + "\n")
    if fa:
        fa.close()
    checked = sum(1 for q in qs if q["id"] in ref and body_of(q, {}) is not None)
    print(json.dumps({"label": a.label, "tenants": a.tenant, "queries": len(qs), "reference_checked": checked * len(a.tenant), "bad": bad[:10]}))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
