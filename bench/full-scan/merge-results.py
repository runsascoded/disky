#!/usr/bin/env python3
"""Merge bench result files into one `results/<date>.jsonl`: later files' query lines replace earlier ones with the same
(label, id, threads); a later file's load line replaces an earlier one only with `-L`.
    merge-results.py OUT FILE… [-L FILE…]"""
import json
import sys

out, args = sys.argv[1], sys.argv[2:]
loads, queries, order = {}, {}, []
keep_load = True
for a in args:
    if a == "-L":
        keep_load = False
        continue
    for line in open(a):
        r = json.loads(line)
        if r["kind"] == "load":
            if r["label"] not in loads or not keep_load:
                loads[r["label"]] = r
            continue
        k = (r["label"], r["id"], r["threads"])
        if k not in queries:
            order.append(k)
        queries[k] = r
with open(out, "w") as f:
    for label, l in loads.items():
        f.write(json.dumps(l) + "\n")
        for k in order:
            if k[0] == label:
                f.write(json.dumps(queries[k]) + "\n")
