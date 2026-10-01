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
budget: the client sends `w = ceil(innerWidth / 128) * 128`, `h = round(0.6 w)`,
so warming a width only helps viewers whose window quantizes to it.

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
# The root, the (one) bucket, and the top-level dirs everyone drills into.
# Deployment config: `WARM_PATHS` (comma-separated; `""` = the root) — the
# CoreWeave job passes its bucket + top-level dirs. Default: the GCS fleet.
PATHS = tuple(os.environ["WARM_PATHS"].split(",")) if os.environ.get("WARM_PATHS") else ("", "marin-us-central2", "marin-us-east5", "marin-us-central1", "marin-eu-west4", "marin-us-west4", "marin-us-east1")


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


def plan(date: str, dates: list[str], widths: tuple[int, ...] = WIDTHS, spans: tuple[int, ...] = SPANS, paths: tuple[str, ...] = PATHS) -> list[str]:
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
