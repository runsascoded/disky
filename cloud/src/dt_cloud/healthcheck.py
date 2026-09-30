"""Live-site health checks — is the latest scan actually *servable* end-to-end?

Split from the CLI so the check/parse logic is unit-testable without hitting the
network: the pure ``check_*`` functions take already-fetched data and return a
:class:`Check`; :func:`run_checks` does the I/O and calls them.

Motivating failure (2026-08-31 ``/users`` outage): the data pipeline succeeded
but a scan's path-index footer never synced to D1, so the site fell back to
parsing the parquet footer on a cold isolate, blew the Worker CPU budget
(``1102``), and the page went blank. No unit test could catch that — only
something that exercises the running site against the published scan.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.request
from dataclasses import asdict, dataclass
from typing import Callable

UA = "gcs-usage-healthcheck/1.0"  # a real UA — CF edge-blocks bot UAs (1010)
# One retry after a transport-level failure (status 0) — a single timed-out
# probe shouldn't page (2026-09-01: a transient subtree stall alerted while
# every other check passed). Tests monkeypatch this to 0.
RETRY_SLEEP = 5.0


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


# HTTP fetch injected into run_checks so tests can supply canned responses.
# Returns (status, body_bytes); status 0 marks a transport-level failure.
Getter = Callable[[str, str | None], "tuple[int, bytes]"]


def _http_get(url: str, range_header: str | None = None, *, token: str | None, timeout: int = 90) -> tuple[int, bytes]:
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if range_header:
        headers["Range"] = range_header
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:  # noqa: BLE001 — any transport error is a failed check, not a crash
        return 0, b""


# ---- pure checkers (the unit-tested logic) --------------------------------

def check_freshness(scans: list[str], max_age_days: int, today: dt.date) -> Check:
    """scans.json is newest-first; the latest scan must be recent enough."""
    if not scans:
        return Check("freshness", False, "scans.json empty or unreadable")
    latest = scans[0]
    try:
        d = dt.date.fromisoformat(latest[:10])
    except ValueError:
        return Check("freshness", False, f"unparseable scan date {latest!r}")
    age = (today - d).days
    return Check("freshness", age <= max_age_days, f"latest scan {latest} ({age}d old, limit {max_age_days}d)")


def check_status(name: str, status: int, ok_codes: tuple[int, ...] = (200, 206)) -> Check:
    return Check(name, status in ok_codes, f"HTTP {status} (want {'/'.join(map(str, ok_codes))})")


# ---- orchestration (I/O) --------------------------------------------------

def run_checks(
    base: str,
    token: str | None,
    date: str | None = None,
    *,
    max_age_days: int = 2,
    today: dt.date | None = None,
    get: Getter | None = None,
    subdir: str = "",
) -> tuple[str | None, list[Check]]:
    """Fetch + evaluate. Returns (resolved_date, checks). ``get`` is injected in
    tests; in production it defaults to a token-bound urllib fetch. ``subdir``
    is the store's snapshot subdir under ``/data/`` (the site's
    ``SNAPSHOTS_SUBDIR``: ``cw`` for the CoreWeave deployment, empty for the
    default store) — without it the bare ``/data/scans.json`` is the root
    listing, empty on a store that only publishes under a subdir."""
    base = base.rstrip("/")
    sub = f"{subdir.strip('/')}/" if subdir.strip("/") else ""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    if get is None:
        def get(url: str, rng: str | None = None) -> tuple[int, bytes]:
            return _http_get(url, rng, token=token)

    # status 0 = transport-level failure (timeout, conn reset): retry once
    # after RETRY_SLEEP so a single blip doesn't fail the check and alert.
    raw_get = get

    def get(url: str, rng: str | None = None) -> tuple[int, bytes]:  # noqa: F811
        status, body = raw_get(url, rng)
        if status == 0:
            import time

            time.sleep(RETRY_SLEEP)
            status, body = raw_get(url, rng)
        return status, body

    def get_json(path: str) -> tuple[int, dict | None]:
        status, body = get(f"{base}{path}", None)
        try:
            return status, json.loads(body)
        except (ValueError, TypeError):
            return status, None

    checks: list[Check] = []

    # 1. Freshness (also resolves the scan when --date is omitted).
    s_status, scans = get_json(f"/data/{sub}scans.json")
    scans = scans if isinstance(scans, list) else []
    checks.append(check_freshness(scans, max_age_days, today))
    if date is None:
        date = scans[0] if scans else None
    if date is None:
        checks.append(Check("resolve-scan", False, "no --date and scans.json gave none"))
        return None, checks

    # 2. subtree — floor-free drill (root, default pixel budget), the D1-index
    # serving path.
    st_status, _ = get(f"{base}/api/subtree?date={date}&w=128&h=128", None)
    checks.append(check_status("subtree", st_status, (200,)))

    # 3. the published scan meta.
    m_status, _ = get(f"{base}/data/{sub}{date}/meta.json", None)
    checks.append(check_status("data/meta.json", m_status, (200,)))

    return date, checks


def as_dict(date: str | None, checks: list[Check]) -> dict:
    return {"date": date, "ok": all(c.ok for c in checks), "checks": [asdict(c) for c in checks]}
