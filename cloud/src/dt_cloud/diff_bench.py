"""`dt-cloud diff-bench`: time the reads the site's diff and map views make on two hosts, the before / after table a
store-layout change ships with (a new generation pinned on one host, the current one on the other; same code, same
reader flags — `DIFF_DESCENT` changes which tier answers, so a host with it on is not comparable to one without).

Per request (the browser's mix: a diff's first paint `depth=1` and its full `dm=delta` walk; a map view's `depth=1`
and full `/api/subtree`; with `-s`, also each diff's `summary=1`, its totals alone, which the API answers and no page
asks) and per canvas bucket: A and B cold, then A and B warm (their order
alternating by request). Cold means an edge-cache key nobody asked for: the cache key carries `w`×`h` quantized up to
128 px (`view.QUANT`), so each bucket is a random canvas taller than it is wide (the site asks for h = 0.6 w, diff-check
for h = 307), ~180 of them, drawn without replacement; `nc=` does not bust it. A cold sample the edge answered (`x-cache` not `miss`: the bucket was used before) is dropped
from the pairs and counted. Per request kind: medians, the paired cold Δ (B − A) of `Server-Timing` `total`, how many
pairs B was slower in and that count's two-sided sign-test p. Both hosts' answers must agree (totals and drawn rows:
`answer_digest`). Exit 1 on an error or a disagreement, 2 when a kind is slower on B (p < `-p`, mean Δ > 0)."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import statistics
import sys
import urllib.parse
from dataclasses import asdict, dataclass
from math import comb
from pathlib import Path

from click import FloatRange, IntRange, argument, command, option

from . import api_names
from .diff_check import fetch

QUANT = 128
# The site's API cache version (`API_CV`, site/src/App.tsx): asked as the browser asks.
CV = 6


@dataclass(frozen=True)
class Req:
    name: str
    kind: str
    route: str
    q: dict


@dataclass(frozen=True)
class Sample:
    name: str
    kind: str
    w: int
    h: int
    host: str
    phase: str
    status: int
    wall: float
    total: float | None
    cache: str
    digest: str


def random_buckets(n: int) -> list[tuple[int, int]]:
    """`n` distinct canvas buckets no page asks for: w in [640, 2048], h in (w, 4096], multiples of `QUANT`."""
    space = [(w, h) for w in range(640, 2049, QUANT) for h in range(w + QUANT, 4097, QUANT)]
    return random.SystemRandom().sample(space, n)


def requests_for(
    cases: list[dict],
    views: list[str],
    w: int,
    h: int,
    store: str | None = None,
    summary: bool = False,
) -> list[Req]:
    """The browser's requests for each diff case (`{from, to, path[, q]}`) and map view (`DATE:PATH[:Q]`) at one canvas
    (`q`: a name filter, as `?f=` sends it); `store`:
    a secondary store's (`?store=`, e.g. `meta`), on every request. `summary`: also each diff's `summary=1` (its totals
    alone), which no page asks."""
    out = []
    st = {"store": store} if store else {}
    for c in cases:
        p = c.get("path", "")
        q = {"q": c["q"]} if c.get("q") else {}
        base = {"cv": CV, **st, "from": c["from"], "to": c["to"], "path": p, **q, "w": w, "h": h}
        name = f"diff {c['from']}→{c['to']} /{p}" + (f" q={c['q']}" if q else "")
        out += [Req(name, "diff:d1", "/api/diff", {**base, "dm": "delta", "depth": 1}),
                Req(name, "diff:full", "/api/diff", {**base, "dm": "delta"})]
        if summary:
            out.append(Req(name, "diff:summary", "/api/diff", {**base, "summary": 1}))
    for v in views:
        d, p, *f = v.split(":", 2)
        q = {"q": f[0]} if f and f[0] else {}
        base = {"cv": CV, **st, "date": d, "path": p, **q, "w": w, "h": h}
        name = f"subtree {d} /{p}" + (f" q={f[0]}" if q else "")
        out += [Req(name, "subtree:d1", "/api/subtree", {**base, "depth": 1}),
                Req(name, "subtree:full", "/api/subtree", base)]
    return out


def uri_component(s: object) -> str:
    """`s` escaped as the browser's `encodeURIComponent` escapes it."""
    return urllib.parse.quote(str(s), safe="!*'()")


def browser_url(r: Req) -> str:
    """`r` as the page's `fetch` sends it (path and query, no host): its parameters in order, each value escaped as
    `encodeURIComponent` does. `warm-cache` replays these (`warm.plan`), so what it warms is what a page asks."""
    return f"{r.route}?{'&'.join(f'{k}={uri_component(v)}' for k, v in r.q.items())}"


def answer_digest(route: str, body: dict) -> str:
    """What both hosts must answer alike: a diff's totals and drawn rows, a view's drawn nodes and tree. Not the
    reader's own fields (`tier`, `index`, `lookups`, `budgetCut`), which a layout change may move, nor the root's
    display name (`tree.name`: the host's `ROOT_LABEL`, "… (dev)" on a dev stack). Either host's short-named body
    (`api_names`) digests as its descriptive one."""
    tree = api_names.tree(body) if isinstance(body.get("tree"), dict) else body.get("tree")
    keep = ({k: api_names.diff(body).get(k) for k in ("bytes_before", "bytes_after", "objects_before", "objects_after", "rows")} if route == "/api/diff"
            else {"nodes": body.get("nodes"), "tree": {k: v for k, v in tree.items() if k != "name"} if isinstance(tree, dict) else tree})
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:12]


def st_total(st: str) -> float | None:
    """`Server-Timing`'s `total` (ms), or None (an edge hit carries none)."""
    for part in st.split(","):
        name, *kv = [x.strip() for x in part.split(";")]
        if name == "total":
            return next((float(x[4:]) for x in kv if x.startswith("dur=")), None)
    return None


def sign_test(deltas: list[float]) -> float:
    """Two-sided exact sign test over the nonzero `deltas`: the chance of a split at least this lopsided under H0."""
    xs = [d for d in deltas if d]
    n, k = len(xs), sum(d > 0 for d in xs)
    if not n:
        return 1.0
    return min(1.0, 2 * sum(comb(n, i) for i in range(min(k, n - k) + 1)) / 2 ** n)


@dataclass(frozen=True)
class Kind:
    kind: str
    n: int
    a_med: float
    b_med: float
    d_mean: float
    d_med: float
    b_slower: int
    p: float
    warm_a: float
    warm_b: float


def summarize(samples: list[Sample], a: str, b: str) -> tuple[list[Kind], list[tuple[str, str, tuple[int, int]]], int]:
    """Per kind: cold `total` medians, paired Δ (B − A, over buckets both answered from compute), B-slower count and
    its p, warm wall medians. Also the (name, kind, (w, h)) whose answers disagree, and the cold samples the edge answered."""
    by: dict[tuple, dict[str, Sample]] = {}
    for s in samples:
        by.setdefault((s.kind, s.name, (s.w, s.h), s.phase), {})[s.host] = s
    disagree = sorted({(n, k, w) for (k, n, w, ph), v in by.items() if ph == "cold" and len(v) == 2
                       and v[a].status == v[b].status == 200 and v[a].digest != v[b].digest})
    warmed = sum(s.phase == "cold" and s.cache != "miss" for s in samples)
    kinds = []
    for kind in sorted({s.kind for s in samples}):
        pairs = [(v[a].total, v[b].total) for (k, _, _, ph), v in by.items() if k == kind and ph == "cold" and len(v) == 2
                 and all(x.status == 200 and x.cache == "miss" and x.total is not None for x in v.values())]
        warm = [v for (k, _, _, ph), v in by.items() if k == kind and ph == "warm" and len(v) == 2]
        if not pairs:
            continue
        ds = [y - x for x, y in pairs]
        kinds.append(Kind(kind, len(pairs), statistics.median(x for x, _ in pairs), statistics.median(y for _, y in pairs),
                          statistics.mean(ds), statistics.median(ds), sum(d > 0 for d in ds), sign_test(ds),
                          statistics.median(v[a].wall for v in warm) if warm else float("nan"),
                          statistics.median(v[b].wall for v in warm) if warm else float("nan")))
    return kinds, disagree, warmed


def table(kinds: list[Kind]) -> list[str]:
    out = [f"{'request':13} {'n':>3} {'cold ms A':>9} {'B':>6} {'Δ mean':>7} {'Δ med':>6} {'B slower':>9} {'p':>6} {'warm s A':>8} {'B':>6}"]
    out += [f"{k.kind:13} {k.n:3} {k.a_med:9.0f} {k.b_med:6.0f} {k.d_mean:+7.1f} {k.d_med:+6.1f} {f'{k.b_slower}/{k.n}':>9} {k.p:6.3f} "
            f"{k.warm_a:8.3f} {k.warm_b:6.3f}" for k in kinds]
    return out


@command("diff-bench")
@option("-a", "--host-a", required=True, help="The before host (e.g. dev pinned to the current generation via `INTERVAL_STORE_GEN`)")
@option("-b", "--host-b", required=True, help="The after host (e.g. dev pinned to the new generation); not A: one host shares one cache")
@option("-H", "--height", type=IntRange(min=1), help="Canvas height with `-w` (default: each bucket's own, drawn with its width)")
@option("-n", "--buckets", default=2, type=IntRange(min=1), help="Cold canvas buckets: random w×h, w in [640, 2048], h in (w, 4096] (128 px apart)")
@option("-o", "--out", type=Path, help="Every sample as TSV")
@option("-p", "--alpha", default=0.05, type=FloatRange(0, 1), help="Sign-test level at which a kind slower on B fails (exit 2)")
@option("-s", "--summary", is_flag=True, help="Also time each diff's `summary=1` (its totals alone): an API request, not one a page makes")
@option("-S", "--store", help="A secondary store's name (`?store=` on every request, e.g. `meta`); default the primary")
@option("-v", "--view", "views", multiple=True, help="A map view `DATE:PATH` (`2026-10-24T0801:` is the root), repeatable")
@option("-w", "--widths", help="Comma-separated canvas widths instead of `-n` random buckets (at `-H`, default 307)")
@argument("cases", required=False)
def diff_bench_cmd(host_a, host_b, height, buckets, out, alpha, summary, store, views, widths, cases) -> None:
    """Time CASES (diff-check's `{from, to, path}` list, inline or a file) and `-v` views on A and B, as a page asks
    them; `$SITE_TOKEN` authenticates."""
    if host_a == host_b:
        raise SystemExit("A and B share an edge cache: B's cold reads would be A's warm ones")
    token = os.environ["SITE_TOKEN"].strip()
    cs = json.loads(Path(cases).read_text() if Path(cases).is_file() else cases) if cases else []
    canvases = [(int(x), height or 307) for x in widths.split(",")] if widths else random_buckets(buckets)
    samples: list[Sample] = []
    i = 0
    for w, h in canvases:
        for r in requests_for(cs, list(views), w, h, store, summary):
            hosts = (host_a, host_b) if i % 2 == 0 else (host_b, host_a)
            i += 1
            for phase in ("cold", "warm"):
                for host in hosts:
                    status, body, secs, st, cache = fetch(host, r.q, token, retries=1, route=r.route)
                    s = Sample(r.name, r.kind, w, h, host, phase, status, secs, st_total(st), cache,
                               answer_digest(r.route, body) if status == 200 and isinstance(body, dict) else str(body)[:80])
                    samples.append(s)
                    if status != 200:
                        print(f"ERR  {r.kind} {r.name} {w}×{h} {host} {phase}: {status} {s.digest}", file=sys.stderr, flush=True)
        print(f"{w}×{h}: {i} requests", file=sys.stderr, flush=True)
    if out:
        with out.open("w") as f:
            wr = csv.DictWriter(f, fieldnames=list(Sample.__dataclass_fields__), delimiter="\t")
            wr.writeheader()
            wr.writerows(asdict(s) for s in samples)
    kinds, disagree, warmed = summarize(samples, host_a, host_b)
    print(f"A {host_a}  B {host_b}  canvases {' '.join(f'{w}×{h}' for w, h in canvases)}")
    print("\n".join(table(kinds)))
    errors = sum(s.status != 200 for s in samples)
    print(f"{errors} errors, {len(disagree)} disagreeing answers, {warmed} cold samples the edge answered (dropped)")
    for n, k, (w, h) in disagree[:10]:
        print(f"  differ: {k} {n} {w}×{h}")
    slower = [k.kind for k in kinds if k.p < alpha and k.d_mean > 0]
    if slower:
        print(f"slower on B (p < {alpha}): {', '.join(slower)}")
    sys.exit(1 if errors or disagree else 2 if slower else 0)
