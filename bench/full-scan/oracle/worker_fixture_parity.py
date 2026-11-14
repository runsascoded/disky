#!/usr/bin/env python3
"""Today's Worker against `vmd` on the interval-store fixture (specs/vm-mode.md §8.3): `site/functions/_lib/fixtures/
iv/expected.json` holds every view the Worker's `buildView` must draw over fixture `g1` (`intervalStore.test.ts`
asserts it). Each view of a scan with object rows (v2; a v1 scan holds directories only) is asked of `vmd` (tenant
`-t`, serving g1's pack) with every node drawn; every object tile the Worker drew must be in `vmd`'s answer with equal
bytes and objects. Directory tiles are counted, not required: the Worker draws a directory's own rolled-up row, `vmd`
the sum of its objects, and the fixture's directory rows deliberately don't add up (`gen.py`: "inconsistent directory
totals"); on gcs they do (the bench's `dirs_vs_files.py`: 27 / 27 equal).

    worker_fixture_parity.py -u URL -k KEY_ENV -t TENANT EXPECTED
"""
import json
import os
import sys
import time
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
    for _ in range(6000):
        st, s = v.get("/v1/status")
        if st == 200 and s["state"] == "hot":
            break
        time.sleep(0.1)
    pins = s["current"]["pins"]
    cases = json.loads(Path(a.expected).read_text())
    bad, n_cases, n_tiles, n_dirs, dirs_equal = [], 0, 0, 0, 0
    for c in cases:
        if c["v"] != 2:
            continue
        p = c["path"]
        # A name repeated under one parent (`#2`: a path both a directory and an object) has no single sum to compare.
        tiles = {k: t for k, t in c["tiles"].items() if k and t["name"] != "(other)" and "#" not in k}
        body = {"pins": pins, "scan": c["date"], "path": p, "flat": True, "cell": {"depth": 64}}
        st, r = v.post("/v1/view", body)
        if st != 200:
            bad.append((c["date"], p, st, r))
            continue
        vm = {n[0]: (n[1], n[2]) for n in r["answer"]["nodes"]}
        for k, t in tiles.items():
            full = f"{p}{k}" if p else k[1:]
            if t["kind"] == "file":
                n_tiles += 1
                if vm.get(full) != (t["bytes"], t["objects"]):
                    bad.append((c["date"], p, full, (t["bytes"], t["objects"]), vm.get(full)))
            else:
                n_dirs += 1
                dirs_equal += vm.get(full) == (t["bytes"], t["objects"])
        n_cases += 1
    for b in bad[:20]:
        print(f"DIFF {b}", file=sys.stderr)
    print(json.dumps({"cases": n_cases, "object_tiles": n_tiles, "different": len(bad), "directory_tiles": n_dirs, "directory_tiles_equal": dirs_equal}))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
