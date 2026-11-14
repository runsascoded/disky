#!/usr/bin/env python3
"""Today's Worker against `vmd` on real requests (specs/vm-mode.md §8.3): each request asked of the live site
(`/api/subtree`, `/api/diff`, with a read grant), then of `vmd` at the same scan(s), path and scope, with a floor low
enough that every node the Worker drew passes; every node the Worker drew must be in `vmd`'s tree with equal bytes and
objects (both sides, in a diff), and the totals equal. One JSONL line per request (`-o`); a summary to stdout.

    worker_parity.py -s SITE -T TOKEN_FILE -k KEY_ENV -t TENANT -d SCAN [-m FROM] [-p PATH …] [-n N [-D]] -o OUT

TOKEN_FILE holds a Bearer token (a personal agent token), or a `?key=` share link, which is exchanged once
(`POST /api/auth/exchange`) for a session cookie. Neither is ever printed. `-n N`: also the root's N largest children
(from the Worker's root view), and with `-D` each one's largest child, so no path need be named on the command line.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from argparse import ArgumentParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vmd_client import Vmd  # noqa: E402

W = "w=1408&h=896"


UA = "disky-vm-parity/1"


def auth_headers(site: str, secret: str) -> dict[str, str]:
    """The headers that authorize a site request: a `?key=` share link exchanged for its session cookie(s), else the
    secret as a Bearer token. The secret never reaches stdout, stderr or an exception message."""
    u = urllib.parse.urlparse(secret)
    key = urllib.parse.parse_qs(u.query).get("key", [None])[0] if u.scheme in ("http", "https") else None
    if not key:
        return {"Authorization": f"Bearer {secret}"}
    rq = urllib.request.Request(f"{site.rstrip('/')}/api/auth/exchange", data=json.dumps({"token": key}).encode(), method="POST",
                                headers={"content-type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(rq, timeout=60) as r:
            cookies = r.headers.get_all("set-cookie") or []
    except urllib.error.HTTPError as e:
        raise SystemExit(f"share-link exchange refused: HTTP {e.code} {e.read().decode()[:200]}") from None
    pairs = [c.split(";", 1)[0].strip() for c in cookies]
    pairs = [p for p in pairs if "=" in p and p.split("=", 1)[1]]
    if not pairs:
        raise SystemExit("share-link exchange: no session cookie in the reply")
    print(f"share link exchanged: {len(pairs)} cookie(s) ({', '.join(p.split('=', 1)[0] for p in pairs)})", file=sys.stderr)
    return {"Cookie": "; ".join(pairs)}


def site_get(site: str, auth: dict[str, str], route: str, q: str) -> tuple[int, dict | str, float]:
    url = f"{site.rstrip('/')}{route}?{q}"
    rq = urllib.request.Request(url, headers={**auth, "User-Agent": UA})
    for i in range(8):
        t = time.time()
        try:
            with urllib.request.urlopen(rq, timeout=300) as r:
                return r.status, json.loads(r.read()), time.time() - t
        except urllib.error.HTTPError as e:
            if e.code in (202, 503) and i < 7:
                time.sleep(10)
                continue
            return e.code, e.read().decode()[:300], time.time() - t
    raise AssertionError("unreachable")


def flat_tree(t: dict, base: str) -> dict[str, dict]:
    """`{path: node}` of a subtree body's drawn nodes (no `(other)`), paths full."""
    out = {}

    def walk(n: dict, p: str, d: int):
        for c in n.get("children", []):
            if c["name"] == "(other)":
                continue
            q = f"{p}/{c['name']}" if p else c["name"]
            out[q] = {**c, "depth": d + 1}
            walk(c, q, d + 1)
    walk(t, base, 0)
    return out


def scope_body(scope: dict) -> dict:
    """A site request's scope as the VM's (`vmAdapter.ts` `vmOwners`, classes)."""
    b = {}
    o = scope.get("o")
    if o is not None:
        b["owners"] = {"not_in": [""]} if o in ("owned", "*") else {"in": [""]}
    if scope.get("cl"):
        b["classes"] = sorted({"s": 1, "n": 2, "c": 3, "a": 4}[c] for c in scope["cl"])
    if scope.get("q"):
        b["q"] = {"alts": [[{"kind": "sub", "text": scope["q"]}]], "neg": []}
    return b


def compare(worker: dict[str, tuple], vm: dict[str, tuple]) -> dict:
    """Worker nodes `{path: sides}` against the VM's: missing in the VM, unequal, equal."""
    missing = [p for p in worker if p not in vm]
    unequal = [(p, worker[p], vm[p]) for p in worker if p in vm and worker[p] != vm[p]]
    return {"worker_nodes": len(worker), "vm_nodes": len(vm), "equal": len(worker) - len(missing) - len(unequal),
            "missing": missing[:5], "n_missing": len(missing), "unequal": unequal[:5], "n_unequal": len(unequal)}


def main() -> int:
    ap = ArgumentParser()
    ap.add_argument("-d", "--date", required=True)
    ap.add_argument("-D", "--drill", action="store_true", help="With -n: each of those children's largest child too")
    ap.add_argument("-k", "--key-env", required=True)
    ap.add_argument("-m", "--month-from", help="A diff's from scan")
    ap.add_argument("-n", "--top", type=int, default=0, help="Also the root's N largest children (from the Worker's root view)")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("-p", "--path", action="append", default=[])
    ap.add_argument("-s", "--site", required=True)
    ap.add_argument("-t", "--tenant", required=True)
    ap.add_argument("-T", "--token-file", required=True)
    ap.add_argument("-u", "--url", default="http://127.0.0.1:7340")
    a = ap.parse_args()
    auth = auth_headers(a.site, Path(a.token_file).read_text().strip())
    v = Vmd(a.url, a.tenant, os.environ[a.key_env].encode())
    pins = v.get("/v1/status")[1]["current"]["pins"]
    scopes = [{}, {"o": "owned"}, {"o": "unowned"}, {"cl": "n"}, {"q": "ckpt"}, {"q": "zarr.json"}]

    def reqs_for(p: str) -> list[tuple[str, str, dict]]:
        return [("subtree", p, s) for s in scopes] + ([("diff", p, {})] if a.month_from else [])
    reqs = [r for p in ["", *a.path] for r in reqs_for(p)]
    tally = {"requests": 0, "worker_errors": 0, "vm_errors": 0, "equal_requests": 0, "nodes_compared": 0, "nodes_equal": 0}
    with open(a.out, "w") as f:
        while reqs:
            kind, p, s = reqs.pop(0)
            qs = "&".join(f"{k}={urllib.parse.quote(str(x))}" for k, x in s.items())
            if kind == "subtree":
                q = f"{W}&date={a.date}&path={urllib.parse.quote(p)}" + (f"&{qs}" if qs else "") + "&full=1"
                st, wb, ws = site_get(a.site, auth, "/api/subtree", q)
                if st == 200 and p == "" and not s and a.top:
                    # the root's largest children (and each one's largest child): paths found, never named
                    kids = sorted((c for c in wb["tree"].get("children", []) if c["name"] != "(other)"), key=lambda c: -c["bytes"])[:a.top]
                    more = [c["name"] for c in kids]
                    if a.drill:
                        more += [f"{c['name']}/{g['name']}" for c in kids
                                 for g in sorted((g for g in c.get("children", []) if g["name"] != "(other)"), key=lambda g: -g["bytes"])[:1]]
                    reqs += [r for m in more for r in reqs_for(m)]
            else:
                q = f"{W}&from={a.month_from}&to={a.date}&path={urllib.parse.quote(p)}&dm=delta"
                st, wb, ws = site_get(a.site, auth, "/api/diff", q)
            line = {"kind": kind, "path": p, "scope": s, "worker_status": st, "worker_s": round(ws, 3)}
            tally["requests"] += 1
            if st != 200:
                line["worker_error"] = wb
                tally["worker_errors"] += 1
                f.write(json.dumps(line) + "\n")
                print(json.dumps(line), file=sys.stderr)
                continue
            dp = 0 if p == "" else p.count("/") + 1
            if kind == "subtree":
                wn = flat_tree(wb["tree"], p)
                worker = {k: (n["bytes"], n["objects"]) for k, n in wn.items()}
                wtot = (wb["tree"]["bytes"], wb["tree"]["objects"])
                depth = max([n["depth"] for n in wn.values()], default=1)
                xs = [n["bytes"] >> (n["depth"] - 1) for n in wn.values() if n["bytes"] > 0]
                body = {"pins": pins, "scan": a.date, "path": p, **scope_body(s)}
            else:
                rows = [r for r in wb["rows"] if not r["path"].endswith("(other)")]
                worker = {(f"{p}/{r['path']}" if p else r["path"]): (r["bytes_before"], r["objects_before"], r["bytes_after"], r["objects_after"]) for r in rows}
                wtot = (wb["bytes_before"], wb["objects_before"], wb["bytes_after"], wb["objects_after"])
                depth = max([r["depth"] for r in rows], default=1)
                xs = [max(r["bytes_before"], r["bytes_after"]) >> (r["depth"] - 1) for r in rows if max(r["bytes_before"], r["bytes_after"]) > 0]
                body = {"pins": pins, "from": a.month_from, "to": a.date, "path": p}
            body["cell"] = {"atten_log2": 1, "depth": max(1, depth), "floor_bytes": max(1, min(xs, default=1))}
            body["flat"] = True
            t = time.time()
            vs, vb = v.post("/v1/view", body)
            line["vm_status"], line["vm_s"] = vs, round(time.time() - t, 3)
            if vs != 200:
                line["vm_error"] = vb
                tally["vm_errors"] += 1
                f.write(json.dumps(line) + "\n")
                continue
            ans = vb["answer"]
            vm = {n[0]: (tuple(n[1:3]) if kind == "subtree" else tuple(n[1:5])) for n in ans["nodes"]}
            vtot = tuple(ans["total"][:2]) if kind == "subtree" else tuple(ans["total"])
            c = compare(worker, vm)
            line.update(c, totals_equal=wtot == vtot, worker_total=wtot, vm_total=vtot, worker_threshold=wb.get("threshold"),
                        floor=body["cell"]["floor_bytes"], depth=body["cell"]["depth"], worker_extra={k: wb[k] for k in ("tier", "index", "partial", "firstPaint", "matchCount") if k in wb})
            tally["nodes_compared"] += c["worker_nodes"]
            tally["nodes_equal"] += c["equal"]
            if c["equal"] == c["worker_nodes"] and wtot == vtot:
                tally["equal_requests"] += 1
            f.write(json.dumps(line) + "\n")
            print(f"{kind} {p!r} {s}: worker {c['worker_nodes']} nodes, {c['equal']} equal, {c['n_missing']} missing, {c['n_unequal']} unequal; totals {'equal' if wtot == vtot else f'{wtot} vs {vtot}'}; worker {ws:.2f} s, vm {line['vm_s']:.2f} s", file=sys.stderr)
    print(json.dumps(tally))
    return 0


if __name__ == "__main__":
    sys.exit(main())
