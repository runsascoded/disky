"""Users' end-to-end API latency, from the session log (`@open-athena/auth`'s, D1 `session_log_batches`): each batch's
`api` events carry the request URL (secrets masked), status and the browser's wall ms. Read-only: `wrangler d1
execute <db> --remote --json`, run from `site/`.

`dt-cloud session-latency -d <db>` is the routes' percentiles over a window; `-r ROUTE` one route's per day. The
probe's records (`probe-report`) are the fixed, cold half of the picture; this is what people actually waited."""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

#: A scan id inside a route's path (`/api/scans/2026-11-13T0703/…`): one route whatever the scan.
SCAN_SEGMENT = re.compile(r"/\d{4}-\d\d-\d\d(?:T\d{4,6})?(?=/|$)")
API_PATH = re.compile(r"^(?:https?://[^/]+)?(/api/[^?#]*)")


def route_of(url: str) -> str | None:
    """`/api/<path>` of a logged request URL, scan ids folded to `<scan>`; None for anything not the site's API."""
    m = API_PATH.match(url)
    return SCAN_SEGMENT.sub("/<scan>", m.group(1).rstrip("/")) if m else None


@dataclass(frozen=True)
class Sample:
    day: str  # UTC day of the request's start
    route: str
    ms: int


def samples(batches: Iterable[str]) -> list[Sample]:
    """The answered API requests (a status below 400, a `ms`) in batches' `events` JSON."""
    out = []
    for events in batches:
        for e in json.loads(events):
            if e.get("k") != "api" or not isinstance(e.get("ms"), (int, float)) or not 0 < int(e.get("s") or 0) < 400:
                continue
            route = route_of(str(e.get("u", "")))
            if route is None:
                continue
            day = dt.datetime.fromtimestamp(int(e["t"]) / 1000, dt.timezone.utc).date().isoformat() if e.get("t") else ""
            out.append(Sample(day, route, int(e["ms"])))
    return out


def pct(xs: list[int], p: float) -> int:
    """Nearest-rank percentile of a non-empty list."""
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * len(s)))]


@dataclass(frozen=True)
class Row:
    key: str  # a route, or a day
    n: int
    p50: int
    p90: int
    max: int


def table(ss: list[Sample], by: str = "route") -> list[Row]:
    """Per route (most requested first) or per day (`by="day"`, oldest first): count, p50, p90, max ms."""
    groups: dict[str, list[int]] = {}
    for s in ss:
        groups.setdefault(s.route if by == "route" else s.day, []).append(s.ms)
    order = sorted(groups, key=lambda k: (-len(groups[k]), k)) if by == "route" else sorted(groups)
    return [Row(k, len(groups[k]), pct(groups[k], 0.5), pct(groups[k], 0.9), max(groups[k])) for k in order]


def fetch_batches(db: str, days: int, *, config: Path | None = None, site: Path) -> list[str]:
    """The `events` of every batch received in the last `days` days, read-only."""
    sql = f"SELECT events FROM session_log_batches WHERE last_ms > (strftime('%s','now') - {int(days)} * 86400) * 1000"
    cmd = ["pnpm", "exec", "wrangler", "d1", "execute", db, "--remote", "--json", "--command", sql]
    if config is not None:
        cmd += ["-c", str(config)]
    out = subprocess.run(cmd, cwd=site, check=True, capture_output=True, text=True).stdout
    (res,) = json.loads(out)
    return [r["events"] for r in res["results"]]


def render(rows: list[Row], head: str) -> list[str]:
    width = max([len(head), *(len(r.key) for r in rows)])
    return [f"{head:{width}}  {'n':>6}  {'p50':>7}  {'p90':>7}  {'max':>7}"] + [
        f"{r.key:{width}}  {r.n:>6}  {r.p50:>7}  {r.p90:>7}  {r.max:>7}" for r in rows
    ]
