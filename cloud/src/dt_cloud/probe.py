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

For a captured page load, repeat `-R/--request` with relative `/api/` URLs to
replay exactly those requests without discovery. `-S` serializes them;
otherwise they run concurrently. `-c` changes subtree/diff response-cache
keys only: it does not flush underlying metadata/row-group caches or the
series cache. Records retain the complete Server-Timing header and a bounded
error-body excerpt for failed responses.

A search refusal for a scan the static name index doesn't hold (400 `scan-not-indexed`) is a FAILED request, unless
the run's own phase record says its static-names step was skipped (`-i REASON`, which the scan step passes from that
record): then it is SKIPPED, for that reason. Skips are counted apart (`n passed, n skipped (reason), n failed`) and
never read as passes; 5xx, timeouts and every other refusal stay failed.

That refusal on a scan the static name index does list (`/api/filter-scans`) is not a lag but a broken index, and is
never skipped: cw 10-24 → 10-29, `merge: false` left 37 unmerged runs, the reader's light-read budget declined nearly
every literal over them, and every search refused every scan while the healthcheck said the index "lags this scan".
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
NOT_INDEXED = "scan-not-indexed"  # the search's refusal for a scan the static name index doesn't hold (`indexedOnly.ts`)


@dataclass(frozen=True)
class Resp:
    status: int  # 0 = transport failure (timeout, reset)
    ms: int
    bytes: int
    cache: str | None  # `x-cache`: `hit` (edge cache), `kv`, `miss`; None when absent
    server_ms: int | None  # `total` from `server-timing`
    body: bytes
    server_timing: str | None = None


@dataclass(frozen=True)
class Result:
    scenario: str
    path: str
    status: int
    ms: int
    bytes: int
    cache: str | None
    server_ms: int | None
    server_timing: str | None = None
    error: str | None = None
    #: Why this request's refusal is skipped, not failed (`run`'s `unindexed`); None: not skipped
    skip: str | None = None
    #: A `scan-not-indexed` refusal's: whether the static name index lists every scan the request names (`indexed_scans`);
    #: None for any other response, or when unknown (no scan named, or no list)
    indexed: bool | None = None


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
        timing = hd.get("server-timing")
        return Resp(status, ms, len(body), hd.get("x-cache"), server_total(timing), body, timing)

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


def request_paths(paths: tuple[str, ...], cold: str = "") -> list[str]:
    """Replay exact same-site API GETs; only subtree/diff support a cold key."""
    out = []
    for path in paths:
        url = urllib.parse.urlsplit(path)
        if url.scheme or url.netloc or url.fragment or not url.path.startswith("/api/") or ".." in url.path:
            raise ValueError("probe requests must be relative /api/ paths without a fragment")
        if cold and url.path in ("/api/subtree", "/api/diff"):
            query = [(k, v) for k, v in urllib.parse.parse_qsl(url.query, keep_blank_values=True) if k != "minArea"]
            query.append(("minArea", cold))
            path = urllib.parse.urlunsplit(("", "", url.path, urllib.parse.urlencode(query), ""))
        out.append(path)
    return out


def not_indexed(r: Resp) -> bool:
    """Whether `r` is the search's `scan-not-indexed` refusal: a 400 whose JSON body carries that code."""
    if r.status != 400:
        return False
    try:
        body = json.loads(r.body)
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(body, dict) and body.get("code") == NOT_INDEXED


def named_scans(path: str) -> list[str]:
    """The scans a request names: its `date`, `from` and `to` params."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    return [v for k in ("date", "from", "to") for v in q.get(k, [])]


def indexed_scans(fetch: Fetch) -> list[str] | None:
    """The scans the static name index lists (`/api/filter-scans`); None when it answers otherwise (the static filter
    off, an error)."""
    r = fetch("/api/filter-scans")
    try:
        scans = json.loads(r.body)["scans"] if r.status == 200 else None
    except (ValueError, UnicodeDecodeError, KeyError, TypeError):
        return None
    return scans if isinstance(scans, list) else None


def run(fetch: Fetch, scens: dict[str, list[str]], *, parallel: bool = True, unindexed: str | None = None) -> list[Result]:
    """Scenarios in turn; each one's requests concurrently (a page load). `unindexed`: the run's static-names step was
    skipped, for this reason (from its phase record), so a `scan-not-indexed` refusal is skipped for it, not failed. A
    refusal naming scans checks them against `indexed_scans` (fetched once, on the first): on scans it lists, the
    refusal is `indexed` and never skipped."""
    out: list[Result] = []
    held: list[list[str] | None] = []
    for name, paths in scens.items():
        with ThreadPoolExecutor(len(paths) if parallel else 1) as ex:
            for path, r in zip(paths, ex.map(fetch, paths)):
                error = r.body.decode(errors="replace")[:1000] if r.status not in (200, 409) else None
                if r.status == 200:
                    try:
                        json.loads(r.body)
                    except (ValueError, UnicodeDecodeError):
                        error = "invalid JSON response: " + r.body.decode(errors="replace")[:1000]
                indexed = None
                if not_indexed(r) and (named := named_scans(path)):
                    if not held:
                        held.append(indexed_scans(fetch))
                    indexed = None if held[0] is None else all(s in held[0] for s in named)
                skip = unindexed if unindexed and not_indexed(r) and not indexed else None
                out.append(Result(name, path, r.status, r.ms, r.bytes, r.cache, r.server_ms, r.server_timing, error, skip, indexed))
    return out


def compare_requests(fetch: Fetch, baseline: Fetch, paths: list[str], *, parallel: bool = True) -> tuple[list[Result], list[Result], list[dict[str, object]]]:
    """Compare complete JSON responses; retain timings, never successful bodies."""
    def captured(f: Fetch) -> tuple[Fetch, dict[str, Resp]]:
        responses: dict[str, Resp] = {}
        def get(path: str) -> Resp:
            response = f(path)
            responses[path] = response
            return response
        return get, responses
    first, actual = captured(fetch)
    second, expected = captured(baseline)
    results = run(first, {"requests": paths}, parallel=parallel)
    before = run(second, {"requests": paths}, parallel=parallel)
    parity = []
    for path in paths:
        a, b = actual[path], expected[path]
        equal = False
        if a.status == b.status == 200:
            try:
                equal = json.loads(a.body) == json.loads(b.body)
            except (ValueError, UnicodeDecodeError):
                pass
        parity.append({"path": path, "equal": equal})
    return results, before, parity


def unindexed_refusal(r: Result) -> bool:
    """Whether `r` failed as the search's `scan-not-indexed` refusal (`lagged` or `refused_indexed`)."""
    try:
        body = json.loads(r.error or "")
    except ValueError:
        return False
    return r.status == 400 and isinstance(body, dict) and body.get("code") == NOT_INDEXED


def failed(r: Result) -> bool:
    return r.skip is None and (r.status not in (200, 409) or r.error is not None)


def lagged(r: Result) -> bool:
    """A failed `scan-not-indexed` refusal on a scan the static name index doesn't list (or not known to): the index lags
    this scan (a real gap, named apart from other failures so a lag doesn't read as a broken site)."""
    return failed(r) and unindexed_refusal(r) and r.indexed is not True


def refused_indexed(r: Result) -> bool:
    """A failed `scan-not-indexed` refusal on scans the static name index lists: the reader declined it (over the tiers'
    light-read row budget, cw's unmerged runs 10-24 → 10-29), not a lag."""
    return failed(r) and unindexed_refusal(r) and r.indexed is True


#: `summary_line`'s note on `refused_indexed` requests
REFUSED_INDEXED = "refused on an indexed scan (over the tiers' row budget? check the static index's unmerged runs)"


def counts(results: list[Result]) -> tuple[int, int, int]:
    """(passed, skipped, failed) requests."""
    skipped = sum(r.skip is not None for r in results)
    bad = sum(failed(r) for r in results)
    return len(results) - skipped - bad, skipped, bad


def summary_line(results: list[Result]) -> str:
    """`n passed, n skipped (reason), n failed`: each distinct skip reason named, so a skip never reads as a pass."""
    passed, skipped, bad = counts(results)
    reasons = "; ".join(dict.fromkeys(r.skip for r in results if r.skip is not None))
    lag, broken = sum(map(lagged, results)), sum(map(refused_indexed, results))
    return (f"{passed} passed, {skipped} skipped{f' ({reasons})' if reasons else ''}, {bad} failed"
            + (f" ({lag} {NOT_INDEXED}: the static name index lags this scan)" if lag else "")
            + (f" ({broken} {NOT_INDEXED} {REFUSED_INDEXED})" if broken else ""))


def summarize(results: list[Result], budget_ms: int | None = None) -> tuple[bool, list[str]]:
    """Per-scenario lines (the slowest request = the page's wall time) and
    pass/fail: only 200 and the site's explicit view refusal (409) pass; a skipped request (`run`'s `unindexed`) is
    listed with its reason and neither passes nor fails (`–`: every request skipped). With `budget_ms`, a slower
    scenario fails."""
    ok = True
    lines = []
    for name in dict.fromkeys(r.scenario for r in results):
        rs = [r for r in results if r.scenario == name]
        wall = max(r.ms for r in rs)
        bad = [r for r in rs if failed(r)]
        over = budget_ms is not None and wall > budget_ms
        ok &= not bad and not over
        hits = sum(r.cache in CACHED for r in rs)
        skipped = [r for r in rs if r.skip is not None]
        mark = "✗" if bad or over else "–" if len(skipped) == len(rs) else "✓"
        lines.append(f"  {mark} {name:<12} {wall / 1000:6.2f}s  {len(rs)} reqs, {hits} cached"
                     + (f", {len(skipped)} skipped" if skipped else "") + (f", {len(bad)} failed" if bad else ""))
        for r in bad:
            lines.append(f"      {r.status} {r.ms / 1000:.2f}s {r.path}" + (f" ({NOT_INDEXED} on an indexed scan)" if refused_indexed(r) else
                                                                           f" ({NOT_INDEXED})" if unindexed_refusal(r) else ""))
        for r in skipped:
            lines.append(f"      skipped {r.status} {r.ms / 1000:.2f}s {r.path}: {r.skip}")
    return ok, lines


def record(base: str, t: Targets, results: list[Result], now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    passed, skipped, bad = counts(results)
    return {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "base": base, **asdict(t), "results": [asdict(r) for r in results],
            "summary": {"passed": passed, "skipped": skipped, "failed": bad, "lag": sum(map(lagged, results)),
                        "refused_indexed": sum(map(refused_indexed, results)), "line": summary_line(results)}}
