"""`dt-cloud healthcheck` — the live-site serving invariant (`dt_cloud.healthcheck`).

Pins the pass/fail logic: scan freshness, the index-served subtree, the
published meta. The HTTP layer is injected, so these run offline against
canned responses — no network, no prod.
"""

from __future__ import annotations

import datetime as dt
import json

from dt_cloud.healthcheck import (
    Check,
    check_freshness,
    check_status,
    run_checks,
)

TODAY = dt.date(2026, 8, 31)


def test_freshness_fresh_stale_empty():
    assert check_freshness(["2026-08-31", "2026-08-30"], 2, TODAY) == Check(
        "freshness", True, "latest scan 2026-08-31 (0d old, limit 2d)")
    assert check_freshness(["2026-08-28"], 2, TODAY) == Check(
        "freshness", False, "latest scan 2026-08-28 (3d old, limit 2d)")
    assert check_freshness([], 2, TODAY) == Check("freshness", False, "scans.json empty or unreadable")


def test_status_ok_codes():
    assert check_status("subtree", 200, (200,)) == Check("subtree", True, "HTTP 200 (want 200)")
    assert check_status("subtree", 500, (200,)) == Check("subtree", False, "HTTP 500 (want 200)")


def _fake_site(*, subtree=200, meta=200, scans=("2026-08-31", "2026-08-30")):
    """A getter over a canned site: maps request URL → (status, body_bytes)."""
    routes = {
        "/data/scans.json": (200, json.dumps(list(scans)).encode()),
        "/api/subtree?date=2026-08-31&w=128&h=128": (subtree, b"{}"),
        "/data/2026-08-31/meta.json": (meta, b"{}"),
    }

    def get(url: str, rng: str | None) -> tuple[int, bytes]:
        path = url.replace("https://gcs.oa.dev", "")
        return routes.get(path, (404, b""))

    return get


def test_run_checks_all_green_resolves_latest_scan():
    get = _fake_site()
    date, checks = run_checks("https://gcs.oa.dev", "tok", None, today=TODAY, get=get)
    assert date == "2026-08-31"
    assert checks == [
        Check("freshness", True, "latest scan 2026-08-31 (0d old, limit 2d)"),
        Check("subtree", True, "HTTP 200 (want 200)"),
        Check("data/meta.json", True, "HTTP 200 (want 200)"),
    ]


def test_run_checks_flags_a_failed_subtree_and_missing_data():
    # subtree 500 + a missing meta.json → two failed checks.
    get = _fake_site(subtree=500, meta=404)
    date, checks = run_checks("https://gcs.oa.dev", "tok", None, today=TODAY, get=get)
    assert date == "2026-08-31"
    assert checks == [
        Check("freshness", True, "latest scan 2026-08-31 (0d old, limit 2d)"),
        Check("subtree", False, "HTTP 500 (want 200)"),
        Check("data/meta.json", False, "HTTP 404 (want 200)"),
    ]


def test_run_checks_retries_transport_blip_once(monkeypatch):
    # First subtree probe dies at the transport level (status 0), the retry
    # succeeds — the check passes and the probe was fetched exactly twice.
    import dt_cloud.healthcheck as hc

    monkeypatch.setattr(hc, "RETRY_SLEEP", 0)
    inner = _fake_site()
    subtree_calls = []

    def get(url: str, rng: str | None) -> tuple[int, bytes]:
        if "/api/subtree" in url:
            subtree_calls.append(url)
            if len(subtree_calls) == 1:
                return 0, b""
        return inner(url, rng)

    date, checks = run_checks("https://gcs.oa.dev", "tok", None, today=TODAY, get=get)
    assert date == "2026-08-31"
    assert len(subtree_calls) == 2
    assert checks == [
        Check("freshness", True, "latest scan 2026-08-31 (0d old, limit 2d)"),
        Check("subtree", True, "HTTP 200 (want 200)"),
        Check("data/meta.json", True, "HTTP 200 (want 200)"),
    ]


def test_run_checks_transport_failure_persists_after_retry():
    # Both attempts fail at the transport level → the check fails as HTTP 0.
    import dt_cloud.healthcheck as hc

    assert hc.RETRY_SLEEP == 5.0  # prod pause between attempts

    inner = _fake_site()
    hc.RETRY_SLEEP = 0
    try:
        def get(url: str, rng: str | None) -> tuple[int, bytes]:
            if "/api/subtree" in url:
                return 0, b""
            return inner(url, rng)

        date, checks = run_checks("https://gcs.oa.dev", "tok", None, today=TODAY, get=get)
    finally:
        hc.RETRY_SLEEP = 5.0
    assert date == "2026-08-31"
    assert checks == [
        Check("freshness", True, "latest scan 2026-08-31 (0d old, limit 2d)"),
        Check("subtree", False, "HTTP 0 (want 200)"),
        Check("data/meta.json", True, "HTTP 200 (want 200)"),
    ]


def test_run_checks_subdir_scopes_the_data_routes():
    """A store that publishes under `/data/<subdir>/` (the CoreWeave deployment's
    `SNAPSHOTS_SUBDIR=cw`): scans.json and meta.json come from the subdir; the
    API routes are unchanged."""
    routes = {
        "/data/cw/scans.json": (200, json.dumps(["2026-08-31"]).encode()),
        "/api/subtree?date=2026-08-31&w=128&h=128": (200, b"{}"),
        "/data/cw/2026-08-31/meta.json": (200, b"{}"),
    }
    seen: list[str] = []

    def get(url: str, rng: str | None) -> tuple[int, bytes]:
        path = url.replace("https://cw-s3.oa.dev", "")
        seen.append(path)
        return routes.get(path, (404, b""))

    date, checks = run_checks("https://cw-s3.oa.dev", "tok", None, today=TODAY, get=get, subdir="cw/")
    assert date == "2026-08-31"
    assert [c.ok for c in checks] == [True, True, True]
    assert seen == [
        "/data/cw/scans.json",
        "/api/subtree?date=2026-08-31&w=128&h=128",
        "/data/cw/2026-08-31/meta.json",
    ]


def _subdaily_site():
    """Two scans on 8/31 (06:01, 18:02) and one on 8/30: every route keyed by scan id."""
    scans = ["2026-08-31T1802", "2026-08-31T0601", "2026-08-30T0600"]
    routes = {"/data/scans.json": (200, json.dumps(scans).encode())}
    for s in scans:
        routes[f"/api/subtree?date={s}&w=128&h=128"] = (200, b"{}")
        routes[f"/data/{s}/meta.json"] = (200, b"{}")
    return lambda url, rng: routes.get(url.replace("https://gcs.oa.dev", ""), (404, b""))


def test_run_checks_sub_daily_ids():
    get = _subdaily_site()
    green = [
        Check("freshness", True, "latest scan 2026-08-31T1802 (0d old, limit 2d)"),
        Check("subtree", True, "HTTP 200 (want 200)"),
        Check("data/meta.json", True, "HTTP 200 (want 200)"),
    ]
    # No --date: the newest scan.
    assert run_checks("https://gcs.oa.dev", "tok", None, today=TODAY, get=get) == ("2026-08-31T1802", green)
    # An exact sub-daily id: that scan, not the day's latest.
    assert run_checks("https://gcs.oa.dev", "tok", "2026-08-31T0601", today=TODAY, get=get) == ("2026-08-31T0601", green)
    # A day (or an hour) is a prefix: the latest scan it matches.
    assert run_checks("https://gcs.oa.dev", "tok", "2026-08-31", today=TODAY, get=get) == ("2026-08-31T1802", green)
    assert run_checks("https://gcs.oa.dev", "tok", "2026-08-30", today=TODAY, get=get) == ("2026-08-30T0600", green)
    assert run_checks("https://gcs.oa.dev", "tok", "2026-08-31T06", today=TODAY, get=get) == ("2026-08-31T0601", green)
    # dashless compact slugs, as the site writes them
    assert run_checks("https://gcs.oa.dev", "tok", "260831", today=TODAY, get=get) == ("2026-08-31T1802", green)
    assert run_checks("https://gcs.oa.dev", "tok", "26083106", today=TODAY, get=get) == ("2026-08-31T0601", green)
    assert run_checks("https://gcs.oa.dev", "tok", "2608311802", today=TODAY, get=get) == ("2026-08-31T1802", green)


def test_run_checks_slug_miss_fails():
    # a slug naming no scan is a failed check, never the nearest or latest scan
    get = _subdaily_site()
    fresh = Check("freshness", True, "latest scan 2026-08-31T1802 (0d old, limit 2d)")
    assert [run_checks("https://gcs.oa.dev", "tok", d, today=TODAY, get=get) for d in ["26083103", "260901", "junk"]] == [
        (None, [fresh, Check("resolve-scan", False, f"no scan matches {d}")]) for d in ["26083103", "260901", "junk"]
    ]


def test_run_checks_date_only_scan_by_its_start():
    # 8/31: a date-only scan that started 04:30Z (`meta.started`) and 18:02Z — its start minute and hour pick it, the
    # midnight alias too, the day the latest.
    scans = ["2026-08-31T1802", "2026-08-31"]
    routes = {"/data/scans.json": (200, json.dumps(scans).encode())}
    for s in scans:
        routes[f"/api/subtree?date={s}&w=128&h=128"] = (200, b"{}")
        routes[f"/data/{s}/meta.json"] = (200, json.dumps({"started": "2026-08-31T04:30:12.345Z"} if len(s) == 10 else {}).encode())
    get = lambda url, rng: routes.get(url.replace("https://gcs.oa.dev", ""), (404, b""))  # noqa: E731
    assert [run_checks("https://gcs.oa.dev", "tok", d, today=TODAY, get=get)[0] for d in ["2608310430", "26083104", "2608310000", "260831", "26083105"]] == [
        "2026-08-31", "2026-08-31", "2026-08-31", "2026-08-31T1802", None,
    ]
