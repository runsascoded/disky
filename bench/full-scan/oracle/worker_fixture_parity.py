#!/usr/bin/env python3
"""Today's Worker against `vmd` on the interval-store fixture (specs/vm-mode.md §8.3): `site/functions/_lib/fixtures/
iv/expected.json` holds every view the Worker's `buildView` must draw over fixture `g1` (`intervalStore.test.ts`
asserts it). Each view of a scan with object rows (v2; a v1 scan holds directories only) is asked of `vmd` (tenant
`-t`, serving g1's pack) with a floor low enough that every tile the Worker drew passes; every drawn tile (not
`(other)`) must be in `vmd`'s answer with equal bytes and objects, and the totals equal.

    worker_fixture_parity.py -u URL -k KEY_ENV -t TENANT EXPECTED
"""
import json
import os
import sys
from argparse import ArgumentParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vmd_client import Vmd  # noqa: E402


def main() -> int:
    ap = ArgumentParser()
    ap.add_argument("-k", "--key-env", required=True)
    ap.add_argument("-t", "--tenant", required=True)
    ap.add_argument("-u", "--url", default="http://127.0.0.1:7340")
    ap.add_argument("expected")
    a = ap.parse_args()
    v = Vmd(a.url, a.tenant, os.environ[a.key_env].encode())
    pins = v.get("/v1/status")[1]["current"]["pins"]
    cases = json.loads(Path(a.expected).read_text())
    bad, n_cases, n_tiles = [], 0, 0
    for c in cases:
        if c["v"] != 2:
            continue
        p = c["path"]
        tiles = {k: t for k, t in c["tiles"].items() if k and t["name"] != "(other)"}
        rel = {k: k.count("/") for k in tiles}
        xs = [t["bytes"] >> (rel[k] - 1) for k, t in tiles.items() if t["bytes"] > 0]
        depth = c["depth"] or max(rel.values(), default=1)
        body = {"pins": pins, "scan": c["date"], "path": p, "flat": True,
                "cell": {"atten_log2": 1, "depth": max(1, depth), "floor_bytes": max(1, min(xs, default=1))}}
        st, r = v.post("/v1/view", body)
        if st != 200:
            bad.append((c["date"], p, st, r))
            continue
        vm = {n[0]: (n[1], n[2]) for n in r["answer"]["nodes"]}
        root = c["tiles"][""]
        got_total = tuple(r["answer"]["total"][:2])
        if got_total != (root["bytes"], root["objects"]):
            bad.append((c["date"], p, "total", (root["bytes"], root["objects"]), got_total))
        for k, t in tiles.items():
            full = f"{p}{k}" if p else k[1:]
            n_tiles += 1
            if vm.get(full) != (t["bytes"], t["objects"]):
                bad.append((c["date"], p, full, (t["bytes"], t["objects"]), vm.get(full)))
        n_cases += 1
    for b in bad[:20]:
        print(f"DIFF {b}", file=sys.stderr)
    print(json.dumps({"cases": n_cases, "tiles": n_tiles, "different": len(bad)}))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
