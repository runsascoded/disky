"""Serving probes: replay the site's page loads against a live deployment.

`healthcheck` asks "is the latest scan servable at all" (one cheap request per
surface). A probe asks how the pages actually behave: each scenario is the set
of API requests one page view fires, sent concurrently as the browser does
(they share a Worker isolate, so one heavy request can sink the batch), and
every response's status, wall time, cache tier and server timing is recorded.

Motivating failure (2026-10-01): a path filter with no match inside a bucket
read the whole bucket on a v1 scan, hit Cloudflare 1102, and took the six
other requests of its page load down with it; the diff of the same view
500'd. The healthcheck passed throughout.

The scenarios are generic: the bucket, the filter term and the dates are
resolved from the deployment itself (`resolve_targets`), so the same probe
runs against any store. Records are one JSON object per run (`-o`, local or
`gs://`), so a prefix of them is a latency time series.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Callable

UA = "dt-cloud-probe/1.0"  # a real UA — CF edge-blocks bot UAs (1010)
MISS_TERM = "zz-probe-no-match-zz"
CACHED = ("hit", "kv")  # `x-cache` values served without a read


@dataclass(frozen=True)
class Resp:
    status: int  # 0 = transport failure (timeout, reset)
    ms: int
    bytes: int
    cache: str | None  # `x-cache`: `hit` (edge cache), `kv`, `miss`; None when absent
    server_ms: int | None  # `total` from `server-timing`
    body: bytes


@dataclass(frozen=True)
class Result:
    scenario: str
    path: str
    status: int
    ms: int
    bytes: int
    cache: str | None
    server_ms: int | None


# Injected in tests: (path) -> Resp.
Fetch = Callable[[str], Resp]


def http_fetch(base: str, token: str | None, timeout: int = 120) -> Fetch:
    headers = {"User-Agent": UA, **({"Authorization": f"Bearer {token}"} if token else {})}

    def fetch(path: str) -> Resp:
        t0 = time.monotonic()
        req = urllib.request.Request(base.rstrip("/") + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status, body, hd = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, body, hd = e.code, e.read(), e.headers
        except Exception:  # noqa: BLE001 — a transport error is a failed request, not a crash
            return Resp(0, round((time.monotonic() - t0) * 1000), 0, None, None, b"")
        ms = round((time.monotonic() - t0) * 1000)
        return Resp(status, ms, len(body), hd.get("x-cache"), server_total(hd.get("server-timing")), body)

    return fetch


def server_total(header: str | None) -> int | None:
    """`total;dur=N` from a `server-timing` header, in ms."""
    m = re.search(r"(?:^|,)\s*total;dur=([\d.]+)", header or "")
    return round(float(m.group(1))) if m else None


@dataclass(frozen=True)
class Targets:
    date: str
    prev: str | None
    bucket: str
    hit: str  # a name the filter-hit scenario searches for


def resolve_targets(fetch: Fetch, subdir: str = "", hit: str | None = None) -> Targets:
    """The latest two scans, the largest bucket, and (unless given) a filter
    term that matches: the largest bucket's largest child's name."""
    sub = f"{subdir.strip('/')}/" if subdir.strip("/") else ""
    scans = json.loads(fetch(f"/data/{sub}scans.json").body)
    if not scans:
        raise ValueError("scans.json is empty")
    date, prev = scans[0], (scans[1] if len(scans) > 1 else None)
    W = "w=1408&h=896"
    root = json.loads(fetch(f"/api/subtree?date={date}&path=&depth=1&{W}").body)
    bucket = max(root["tree"]["c"], key=lambda n: n["b"])["n"]
    if hit is None:
        b = json.loads(fetch(f"/api/subtree?date={date}&path={_q(bucket)}&depth=1&{W}").body)
        named = [n for n in b["tree"].get("c", []) if not n["n"].startswith("(")]
        hit = max(named, key=lambda n: n["b"])["n"]
    return Targets(date, prev, bucket, hit)


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def scenarios(t: Targets, cold: str = "") -> dict[str, list[str]]:
    """Each page view's requests, as `site/src/App.tsx` issues them (the
    depth-1 first paint, the full tree, the diff's first paint and full walk,
    the size-over-time series), on the browser's default canvas. ``cold`` is
    a `minArea` (see `cold_min_area`) that keys subtree and diff reads past
    the edge cache."""
    W = "cv=2&w=1408&h=896" + (f"&minArea={cold}" if cold else "")
    def page(path: str, scope: str = "") -> list[str]:
        p = _q(path)
        if "q=" in scope:
            # A filtered page: the coarsest-tier forest first, then the planned
            # read (`full=1`); no `depth=1` diff (App.tsx skips it under a filter).
            reqs = [
                f"/api/subtree?{W}&date={t.date}&path={p}{scope}",
                f"/api/subtree?{W}&date={t.date}&path={p}{scope}&full=1",
            ]
            if t.prev:
                reqs.append(f"/api/diff?{W}&from={t.prev}&to={t.date}&path={p}{scope}")
            return reqs
        reqs = [
            f"/api/subtree?{W}&date={t.date}&path={p}&depth=1",
            f"/api/subtree?{W}&date={t.date}&path={p}",
        ]
        if t.prev:
            reqs += [
                f"/api/diff?{W}&from={t.prev}&to={t.date}&path={p}&depth=1",
                f"/api/diff?{W}&from={t.prev}&to={t.date}&path={p}",
            ]
        reqs.append(f"/api/series?path={p}")
        return reqs

    return {
        "root": page(""),
        "bucket": page(t.bucket),
        "filter-hit": page("", f"&q={_q(t.hit)}"),
        "filter-miss": page(t.bucket, f"&q={MISS_TERM}"),
    }


def cold_min_area(rng: random.Random) -> str:
    """`minArea` is in the subtree/diff cache key but the browser never sends
    it (the server's default is 12 px²): a random `12.xxxxxx` misses the edge
    cache at practically the default's cost. The series takes no `minArea`,
    so it stays cached after the day's first run."""
    return f"12.{rng.randrange(1, 10**6):06d}"


def run(fetch: Fetch, scens: dict[str, list[str]], *, parallel: bool = True) -> list[Result]:
    """Scenarios in turn; each one's requests concurrently (a page load)."""
    out: list[Result] = []
    for name, paths in scens.items():
        with ThreadPoolExecutor(len(paths) if parallel else 1) as ex:
            for path, r in zip(paths, ex.map(fetch, paths)):
                out.append(Result(name, path, r.status, r.ms, r.bytes, r.cache, r.server_ms))
    return out


def failed(r: Result) -> bool:
    return r.status == 0 or r.status >= 500


def summarize(results: list[Result], budget_ms: int | None = None) -> tuple[bool, list[str]]:
    """Per-scenario lines (the slowest request = the page's wall time) and
    pass/fail: any 5xx or transport failure fails; with `budget_ms`, so does
    a scenario slower than it."""
    ok = True
    lines = []
    for name in dict.fromkeys(r.scenario for r in results):
        rs = [r for r in results if r.scenario == name]
        wall = max(r.ms for r in rs)
        bad = [r for r in rs if failed(r)]
        over = budget_ms is not None and wall > budget_ms
        ok &= not bad and not over
        hits = sum(r.cache in CACHED for r in rs)
        lines.append(f"  {'✗' if bad or over else '✓'} {name:<12} {wall / 1000:6.2f}s  {len(rs)} reqs, {hits} cached" + (f", {len(bad)} failed" if bad else ""))
        for r in bad:
            lines.append(f"      {r.status} {r.ms / 1000:.2f}s {r.path}")
    return ok, lines


def record(base: str, t: Targets, results: list[Result], now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    return {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "base": base, **asdict(t), "results": [asdict(r) for r in results]}
