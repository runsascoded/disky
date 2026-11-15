#!/usr/bin/env python3
"""For each unfiltered view answered both ways (`X` summing object versions, `X@dirs` from directories' own rows), how
far the two answers are apart: totals, the kept nodes, and the largest per-node byte difference. Prints JSONL."""
import json
import sys

ans = {a["id"]: a for a in (json.loads(l) for l in open(sys.argv[1]))}
for i, d in ans.items():
    if not i.endswith("@dirs") or i[:-5] not in ans:
        continue
    f = ans[i[:-5]]
    fn = {n[0]: n[1:] for n in f["nodes"]}
    dn = {n[0]: n[1:] for n in d["nodes"]}
    both = fn.keys() & dn.keys()
    diff = [(abs(fn[p][0] - dn[p][0]), p) for p in both if fn[p] != dn[p]]
    print(json.dumps({
        "id": i[:-5], "equal": f["nodes"] == d["nodes"] and f["total"] == d["total"] and f["rest"] == d["rest"],
        "total_files": f["total"], "total_dirs": d["total"], "nodes_files": len(fn), "nodes_dirs": len(dn),
        "only_files": len(fn.keys() - dn.keys()), "only_dirs": len(dn.keys() - fn.keys()),
        "nodes_differing": len(diff), "max_byte_diff": max(diff)[0] if diff else 0,
    }))
