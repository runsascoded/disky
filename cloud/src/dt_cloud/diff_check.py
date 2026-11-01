"""`dt-cloud diff-check`: a Δ-mode diff reader against the as-of walk on a deployment, case by case — what each
deployment runs before turning a Δ reader on in prod (specs/diff-from-changes.md). Started from hf's `cmp.py`.

A is asked for `dm=max` (the as-of walk), B for `dm=delta` (the reader under test); they may be one host. Per case:
- totals (bytes, objects) equal;
- every changed row B draws that A names has A's values; rows A doesn't name at its floor are counted, not failed;
- every changed A row over B's floor at its depth, under a row B expanded (or the root), is drawn by B;
- at every expanded row and the root, on each side, the children's Δ (bytes and objects) sums to the parent's;
- B's `tier` is the expected one (`-t`), so a silent fallback fails;
and the `Server-Timing` of each. Exit 1 on any failure."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from click import IntRange, argument, command, option

TOTALS = ("total_a", "total_b", "objects_a", "objects_b")


def is_other(p: str) -> bool:
    return p == "(other)" or p.endswith("/(other)")


def parent_of(p: str) -> str:
    return p.rsplit("/", 1)[0] if "/" in p else ""


def bad_sums(d: dict) -> list[str]:
    """Expanded rows (and the root) whose drawn children's Δ (bytes or objects) doesn't sum to their own."""
    rows = d["rows"]
    tops = [("", d["total_b"] - d["total_a"], d["objects_b"] - d["objects_a"])]
    tops += [(r["p"], r["b"] - r["a"], r["ob"] - r["oa"]) for r in rows if r.get("x")]
    out = []
    for p, db, do in tops:
        kids = [r for r in rows if parent_of(r["p"]) == p and r["p"] != p]
        if kids and (sum(r["b"] - r["a"] for r in kids), sum(r["ob"] - r["oa"] for r in kids)) != (db, do):
            out.append(p or "/")
    return out


def check(a: dict, b: dict, tier: str | None, atten: float = 2.0) -> dict:
    """The findings for one case: `ok`, and what failed."""
    key = lambda r: (r["k"], r["s"], r["a"], r["b"], r["oa"], r["ob"])  # noqa: E731
    changed = lambda r: r["s"] != "unchanged" and not is_other(r["p"])  # noqa: E731
    ia = {r["p"]: r for r in a["rows"] if changed(r)}
    ib = {r["p"]: r for r in b["rows"] if changed(r)}
    differ = [p for p, r in ib.items() if p in ia and key(ia[p]) != key(r)]
    only_b = [p for p in ib if p not in ia]
    opened = {""} | {r["p"] for r in b["rows"] if r.get("x")}
    floor = b.get("threshold", 0)
    # A row B cut to `top` is in its parent's `(other)` there: excused when B is truncated and that `(other)` covers it
    # (both sides at least the row's).
    others = {parent_of(r["p"]): r for r in b["rows"] if is_other(r["p"])}
    folded = lambda r: b.get("truncated") and (o := others.get(parent_of(r["p"]))) is not None and o["a"] >= r["a"] and o["b"] >= r["b"]  # noqa: E731
    missed = [p for p, r in ia.items() if p not in ib and parent_of(p) in opened and max(r["a"], r["b"]) >= floor * atten ** max(0, r["d"] - 1)
              and not folded(r)]
    out = {
        "totals": all(a.get(k) == b.get(k) for k in TOTALS),
        "differ": differ, "missed": missed, "only_b": len(only_b),
        "sums_a": bad_sums(a), "sums_b": bad_sums(b),
        "tier": b.get("tier"), "tier_ok": tier is None or b.get("tier") in tier.split(","),
    }
    out["ok"] = out["totals"] and not differ and not missed and not out["sums_a"] and not out["sums_b"] and out["tier_ok"]
    return out


def fetch(host: str, q: dict, token: str, retries: int = 6) -> tuple[int, dict | str, float, str, str]:
    """`/api/diff` on `host` (a 202 / 503 retried): status, body, seconds, `Server-Timing`, `x-cache` (`hit` / `kv`: an
    edge copy, not a compute; `miss`)."""
    url = f"{host.rstrip('/') if '://' in host else 'https://' + host}/api/diff?{urllib.parse.urlencode(q)}"
    for i in range(retries):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "dt-cloud-diff-check/1"})
        t = time.time()
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.status, json.loads(r.read()), time.time() - t, r.headers.get("server-timing") or "", r.headers.get("x-cache") or "-"
        except urllib.error.HTTPError as e:
            if e.code in (202, 503) and i + 1 < retries:
                time.sleep(5)
                continue
            return e.code, e.read().decode()[:200], time.time() - t, "", "-"
    raise AssertionError("unreachable")


def timing(st: str, keep: tuple[str, ...] = ("total", "views", "walk", "plan", "descent-rows", "descent-planned")) -> str:
    """The `Server-Timing` phases worth a line: `keep` (`descent-rows` is the descent's rows read, a count), the descent's levels
    (`L1`, `L2`, …) and its `handoff` (which bail-out declined: `breadth`, `budget` or `work`)."""
    out = []
    for part in st.split(","):
        name, *kv = [x.strip() for x in part.split(";")]
        dur = next((x[4:] for x in kv if x.startswith("dur=")), "")
        if name == "handoff":
            desc = next((x[5:].strip('"') for x in kv if x.startswith("desc=")), "")
            out.append(f"handoff[{desc.split(':')[0]}]={dur}")
        elif name in keep or (name[:1] == "L" and name[1:].isdigit()):
            out.append(f"{name}={dur}")
    return " ".join(out)


@command("diff-check")
@option("-a", "--host-a", required=True, help="The reference host (asked for `dm=max`: the as-of walk)")
@option("-b", "--host-b", required=True, help="The host under test (asked for `dm=delta`); may be A")
@option("-H", "--height", default=307, type=IntRange(min=1), help="Canvas height")
@option("-n", "--top", type=IntRange(min=1), help="Frontier rows each diff keeps (`top`; default: the API's)")
@option("-t", "--tier", help="The `tier`(s) B may answer with, comma-separated (`descent,bysize,path`: the descent or its planner's handoff to the walk); any other fails the case")
@option("-w", "--width", default=512, type=IntRange(min=1), help="Canvas width (an uncached one measures cold)")
@argument("cases")
def diff_check_cmd(host_a, host_b, height, top, tier, width, cases) -> None:
    """Check CASES (a JSON list of `{from, to, path}`, inline or a file) on both hosts; `$SITE_TOKEN` authenticates."""
    token = os.environ["SITE_TOKEN"].strip()
    cs = json.loads(Path(cases).read_text() if Path(cases).is_file() else cases)
    bad = 0
    for c in cs:
        q = {"from": c["from"], "to": c["to"], "path": c.get("path", ""), "w": width, "h": height, **({"top": top} if top else {})}
        sa, a, ta, _, ca = fetch(host_a, {**q, "dm": "max"}, token)
        sb, b, tb, st, cb = fetch(host_b, {**q, "dm": "delta"}, token)
        name = f"{c['from']}→{c['to']} /{q['path']}"
        if sa != 200 or sb != 200:
            print(f"ERR  {name}: A {sa} {a if sa != 200 else ''} | B {sb} {b if sb != 200 else ''}", flush=True)
            bad += 1
            continue
        f = check(a, b, tier)
        bad += not f["ok"]
        print(f"{'OK  ' if f['ok'] else 'FAIL'} {name}: totals {'=' if f['totals'] else '≠'}, differ {len(f['differ'])}, missed {len(f['missed'])}, "
              f"B-only {f['only_b']}, sums A {f['sums_a'][:2] or 'ok'} B {f['sums_b'][:2] or 'ok'} | A {ta:.1f}s {ca} {a.get('tier')} "
              f"| B {tb:.1f}s {cb} {f['tier']}{'' if f['tier_ok'] else ' (want ' + tier + ')'} lookups={b.get('lookups')} trunc={b.get('truncated')} | {timing(st)}", flush=True)
        for k in ("differ", "missed"):
            if f[k]:
                print(f"   {k}: {f[k][:3]}", flush=True)
    print(f"{len(cs) - bad}/{len(cs)} ok", flush=True)
    sys.exit(1 if bad else 0)
