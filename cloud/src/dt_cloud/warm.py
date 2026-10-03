"""Warm the site's caches for a freshly published scan (`dt-cloud warm-cache`).

The site's `/api/subtree` and `/api/diff` are auth-gated, immutable per scan,
and cached in two tiers (colo cache + global KV, `site/functions/_lib/edgeCache.ts`).
The first viewer of a scan would otherwise pay the compute (a root diff is
several seconds; it was 20 s before the batched lookups). So the daily job,
once the scan is servable, replays the requests the home page makes for its
default views — the size-over-time series per path (width-independent; a new
scan is a new cache key, so its first viewer otherwise pays the over-time
reads), then one subtree + the diff span chips (1d/3d/7d/14d/30d, plus the
plain previous-scan pair) each with its `summary=1` twin — at the canvas
widths common laptops and phones produce. The cache keys include the pixel
budget: the client snaps its window up to the nearest of these WIDTHS
(`site/src/canvas.ts` `canvasWidth`; past the widest, the 128-px step) and
sends `h = round(0.6 w)`, so every window up to 1920 px lands on a warmed key.
Keep the two lists equal.

Pure planning (`nearest_prior`, `plan`) is unit-tested; `scan_dates` lists the
published scans, `warm` does the HTTP."""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

WIDTHS = (512, 1280, 1536, 1792, 1920)
SPANS = (1, 3, 7, 14, 30)


def env_paths() -> tuple[str, ...] | None:
    """The paths to warm from `$WARM_PATHS` (comma-separated; `""` = the root),
    e.g. a single-bucket deployment's bucket + its top-level dirs. Unset → None
    (the caller asks the site: `top_paths`)."""
    v = os.environ.get("WARM_PATHS")
    return tuple(v.split(",")) if v else None


def root_children(subtree: dict) -> tuple[str, ...]:
    """The root + its children's names from an `/api/subtree` response, folds
    (`(other)`) left out: the views everyone opens first."""
    kids = (subtree.get("tree") or {}).get("c") or []
    return ("", *(k["n"] for k in kids if not k["n"].startswith("(")))


def top_paths(base_url: str, headers: dict[str, str], date: str, timeout: float = 120) -> tuple[str, ...]:
    """The default paths to warm: the store root and its top level, as the
    site's root view of ``date`` lists them."""
    import json

    req = urllib.request.Request(f"{base_url.rstrip('/')}/api/subtree?date={date}&path=&w=1280&h=768&depth=1", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return root_children(json.load(r))


def scan_dates(root: str) -> list[str]:
    """Published scan dates under ``root`` (``gs://<bucket>/snapshots``), ascending."""
    import re

    import fsspec

    fs, _, _ = fsspec.get_fs_token_paths(root)
    return sorted(
        m.group(1)
        for p in fs.glob(f"{root.split('://', 1)[-1]}/*/meta.json")
        if (m := re.search(r"/(\d{4}-\d{2}-\d{2}(?:T\d{4})?)/meta\.json$", p))  # date-only or sub-daily ids
    )


def _ts(date: str) -> int:
    return int(dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc).timestamp())


def nearest_prior(dates: list[str], date: str, days: int) -> str | None:
    """The scan nearest to ``date - days`` among the scans before ``date`` —
    what the site's span chips resolve to (`scan.ts` `nearestScan` over the
    earlier scans)."""
    earlier = [d for d in dates if d < date]
    if not earlier:
        return None
    t = _ts(date) - days * 86400
    return min(earlier, key=lambda d: abs(_ts(d) - t))


def series_url(path: str) -> str:
    """The size-over-time request `SizeOverTime.tsx` makes for an unscoped
    ``path``: the store root asks for one trace per root (`split=roots`)."""
    return "/api/series?path=&split=roots" if path == "" else f"/api/series?path={path}"


def plan(date: str, dates: list[str], paths: tuple[str, ...], widths: tuple[int, ...] = WIDTHS, spans: tuple[int, ...] = SPANS) -> list[str]:
    """The request paths (no host) to replay for ``date``, deduplicated: each
    path's series first, then, in the order the page issues them, per width
    the root's subtree and each diff pair's summary + full rows, then the same
    for each bucket drill."""
    earlier = [d for d in dates if d < date]
    pairs: list[str] = []
    if earlier:
        pairs.append(earlier[-1])  # the default pair: the previous scan
    for s in spans:
        p = nearest_prior(dates, date, s)
        if p and p not in pairs:
            pairs.append(p)
    out: list[str] = [series_url(path) for path in paths]
    for w in widths:
        h = round(w * 0.6)
        for path in paths:
            out.append(f"/api/subtree?date={date}&path={path}&w={w}&h={h}")
            for p in pairs:
                base = f"/api/diff?from={p}&to={date}&path={path}&w={w}&h={h}"
                out.append(base + "&summary=1")
                out.append(base)
    return out


def warm(url: str, headers: dict[str, str], paths: list[str], jobs: int = 4, timeout: float = 120) -> list[tuple[str, int, float, str]]:
    """GET each path with ``headers`` (the Access service token pair), ``jobs``
    at a time; returns
    ``(path, status, seconds, x-cache)`` per request (status 0 = transport
    error). Logs one line per request to stderr."""
    def one(path: str) -> tuple[str, int, float, str]:
        req = urllib.request.Request(url.rstrip("/") + path, headers={**headers, "User-Agent": "gcs-usage-warm/1.0"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read()
                status, tier = r.status, r.headers.get("x-cache", "")
        except urllib.error.HTTPError as e:
            status, tier = e.code, ""
        except Exception as e:  # noqa: BLE001 — a warm-up must never fail the job; the miss is just cold for the first viewer
            status, tier = 0, type(e).__name__
        secs = time.time() - t0
        print(f"warm: {status} {secs:5.1f}s {tier:4s} {path}", file=sys.stderr)
        return path, status, secs, tier

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        return list(ex.map(one, paths))
