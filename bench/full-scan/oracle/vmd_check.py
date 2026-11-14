#!/usr/bin/env python3
"""Ask a running `vmd` every query of a bench query file through its HTTP API, once per tenant (the same store
served whole, as logical shards, as physical shard packs), and require every tenant's answer to equal the first's.
Writes the answers (`-o`) and the queries they answer (`-Q`) for the oracle (`oracle.py`) to check.

    vmd_check.py -u URL -k KEY_ENV -t TENANT [-t TENANT…] -o ANSWERS -Q QUERIES_OUT QUERIES

Exit 1 on any difference, an unexpected refusal, or a regex that wasn't refused.
"""
import json
import os
import sys
import time
from argparse import ArgumentParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vmd_client import Vmd, body_of  # noqa: E402


def main() -> int:
    ap = ArgumentParser()
    ap.add_argument("-a", "--across", action="store_true", help="Also merge the last tenant's shards across HTTP")
    ap.add_argument("-k", "--key-env", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("-Q", "--queries-out", required=True)
    ap.add_argument("-t", "--tenant", action="append", required=True)
    ap.add_argument("-u", "--url", default="http://127.0.0.1:7340")
    ap.add_argument("queries")
    a = ap.parse_args()
    key = os.environ[a.key_env].encode()
    vs = [Vmd(a.url, t, key) for t in a.tenant]
    pins = []
    for v in vs:
        for _ in range(6000):
            st, s = v.get("/v1/status")
            if st == 200 and s["state"] == "hot":
                break
            time.sleep(0.1)
        else:
            print(f"{v.tenant}: not hot: {s}", file=sys.stderr)
            return 1
        pins.append(s["current"]["pins"])
    if any(p != pins[0] for p in pins):
        print(f"tenants on different pins: {pins}", file=sys.stderr)
        return 1
    qs = [json.loads(l) for l in Path(a.queries).read_text().splitlines() if l.strip()]
    bad, refused, skipped, n, across = [], 0, 0, 0, 0
    with open(a.out, "w") as fa, open(a.queries_out, "w") as fq:
        for q in qs:
            b = body_of(q, pins[0])
            if b is None:
                skipped += 1
                continue
            regex = any(m.get("kind") == "regex" for g in (b.get("q") or {}).get("alts", []) for m in g)
            got = [v.post("/v1/view", b) for v in vs]
            if regex:
                if any(st != 422 for st, _ in got):
                    bad.append((q["id"], "a regex wasn't refused", [st for st, _ in got]))
                refused += 1
                continue
            if any(st != 200 for st, _ in got):
                bad.append((q["id"], "refused", [(st, r.get("message")) for st, r in got]))
                continue
            ans = [r["answer"] for _, r in got]
            if any(x != ans[0] for x in ans[1:]):
                bad.append((q["id"], "tenants differ", [t for t, x in zip(a.tenant, ans) if x != ans[0]]))
                continue
            # Every 5th query: the shard and merge layers across HTTP, as a fan-out would run them (each physical
            # shard of the last tenant asked alone, then `/v1/merge`), in one round and in two; equal to the view.
            if a.across and n % 5 == 0:
                v = vs[-1]
                k = v.get("/v1/status")[1]["current"]["shards"]
                for rounds in (1, 2):
                    if rounds == 1:
                        parts = [v.post("/v1/shard", {**b, "shard": i, "floor": "local"}) for i in range(k)]
                    else:
                        tot = [v.post("/v1/shard", {**b, "shard": i, "floor": "totals"})[1]["answer"]["total"] for i in range(k)]
                        g = max(sum(t[0] for t in tot), sum(t[2] for t in tot))
                        parts = [v.post("/v1/shard", {**b, "shard": i, "floor": {"total": g}}) for i in range(k)]
                    if any(st != 200 for st, _ in parts):
                        bad.append((q["id"], f"shard refused ({rounds} rounds)", [st for st, _ in parts]))
                        continue
                    m = {"answers": [r["answer"] for _, r in parts], "path": b["path"], "cell": b["cell"], "diff": "from" in b, "flat": True}
                    st, r = v.post("/v1/merge", m)
                    if st != 200 or r["answer"] != got[0][1]["answer"] or r["tree"] != got[0][1]["tree"]:
                        bad.append((q["id"], f"merged across HTTP ({rounds} rounds), it differs", st))
                across += 1
            n += 1
            fa.write(json.dumps({**ans[0], "id": q["id"]}) + "\n")
            fq.write(json.dumps(q) + "\n")
    for x in bad[:20]:
        print(f"DIFF {x}", file=sys.stderr)
    print(json.dumps({"answered": n, "tenants": a.tenant, "merged_across_http": across, "refused_regex": refused, "skipped": skipped, "bad": len(bad)}))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
