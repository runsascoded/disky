import datetime as dt
import json
import random

from dt_cloud.probe import Resp, Result, Targets, cold_min_area, record, resolve_targets, run, scenarios, server_total, summarize

T = Targets(date="2026-10-01", prev="2026-09-30", bucket="bk-a", hit="ckpt")


def ok(body: object = None, ms: int = 100, cache: str | None = "miss", server_ms: int | None = 80) -> Resp:
    b = json.dumps(body).encode() if body is not None else b"{}"
    return Resp(200, ms, len(b), cache, server_ms, b)


def test_server_total():
    assert [
        server_total('auth;dur=22, scans;dur=11, points;dur=7037, total;dur=7200.4'),
        server_total('total;dur=12'),
        server_total('subtotal;dur=5'),
        server_total('cfCacheStatus;desc="DYNAMIC"'),
        server_total(None),
    ] == [7200, 12, None, None, None]


def test_scenarios():
    W = "cv=2&w=1408&h=896"
    D = "from=2026-09-30&to=2026-10-01"
    assert scenarios(T) == {
        "root": [
            f"/api/subtree?{W}&date=2026-10-01&path=&depth=1",
            f"/api/subtree?{W}&date=2026-10-01&path=",
            f"/api/diff?{W}&{D}&path=&depth=1",
            f"/api/diff?{W}&{D}&path=",
            "/api/series?path=",
        ],
        "bucket": [
            f"/api/subtree?{W}&date=2026-10-01&path=bk-a&depth=1",
            f"/api/subtree?{W}&date=2026-10-01&path=bk-a",
            f"/api/diff?{W}&{D}&path=bk-a&depth=1",
            f"/api/diff?{W}&{D}&path=bk-a",
            "/api/series?path=bk-a",
        ],
        "filter-hit": [
            f"/api/subtree?{W}&date=2026-10-01&path=&q=ckpt",
            f"/api/subtree?{W}&date=2026-10-01&path=&q=ckpt&full=1",
            f"/api/diff?{W}&{D}&path=&q=ckpt",
        ],
        "filter-miss": [
            f"/api/subtree?{W}&date=2026-10-01&path=bk-a&q=zz-probe-no-match-zz",
            f"/api/subtree?{W}&date=2026-10-01&path=bk-a&q=zz-probe-no-match-zz&full=1",
            f"/api/diff?{W}&{D}&path=bk-a&q=zz-probe-no-match-zz",
        ],
    }


def test_scenarios_cold_and_single_scan():
    s = scenarios(Targets("2026-10-01", None, "bk/x", "a b"), cold="12.000042")
    assert s["bucket"] == [
        "/api/subtree?cv=2&w=1408&h=896&minArea=12.000042&date=2026-10-01&path=bk%2Fx&depth=1",
        "/api/subtree?cv=2&w=1408&h=896&minArea=12.000042&date=2026-10-01&path=bk%2Fx",
        "/api/series?path=bk%2Fx",
    ]
    assert s["filter-hit"] == [
        "/api/subtree?cv=2&w=1408&h=896&minArea=12.000042&date=2026-10-01&path=&q=a%20b",
        "/api/subtree?cv=2&w=1408&h=896&minArea=12.000042&date=2026-10-01&path=&q=a%20b&full=1",
    ]


def test_cold_min_area():
    assert [cold_min_area(random.Random(s)) for s in (1, 2)] == ["12.140892", "12.905036"]


def test_resolve_targets():
    canned = {
        "/data/scans.json": ["2026-10-01", "2026-09-30", "2026-09-29"],
        "/api/subtree?date=2026-10-01&path=&depth=1&w=1408&h=896": {"tree": {"c": [{"n": "bk-small", "b": 5}, {"n": "bk-big", "b": 9}]}},
        "/api/subtree?date=2026-10-01&path=bk-big&depth=1&w=1408&h=896": {"tree": {"c": [{"n": "(other)", "b": 99}, {"n": "tmp", "b": 3}, {"n": "ckpt", "b": 7}]}},
    }
    seen = []

    def fetch(path: str) -> Resp:
        seen.append(path)
        return ok(canned[path])

    assert resolve_targets(fetch) == Targets("2026-10-01", "2026-09-30", "bk-big", "ckpt")
    assert resolve_targets(fetch, hit="given") == Targets("2026-10-01", "2026-09-30", "bk-big", "given")
    assert seen == [*canned, "/data/scans.json", "/api/subtree?date=2026-10-01&path=&depth=1&w=1408&h=896"]


def test_run_and_summarize():
    resp = {
        "/a": ok(ms=1200, cache="hit", server_ms=None),
        "/b": ok(ms=3400),
        "/c": Resp(503, 22000, 17, None, None, b"error code: 1102"),
        "/d": ok(ms=900),
    }
    results = run(resp.__getitem__, {"one": ["/a", "/b"], "two": ["/c", "/d"]})
    assert results == [
        Result("one", "/a", 200, 1200, 2, "hit", None),
        Result("one", "/b", 200, 3400, 2, "miss", 80),
        Result("two", "/c", 503, 22000, 17, None, None),
        Result("two", "/d", 200, 900, 2, "miss", 80),
    ]
    assert summarize(results) == (False, [
        "  ✓ one            3.40s  2 reqs, 1 cached",
        "  ✗ two           22.00s  2 reqs, 0 cached, 1 failed",
        "      503 22.00s /c",
    ])
    assert summarize(results[:2], budget_ms=3000) == (False, ["  ✗ one            3.40s  2 reqs, 1 cached"])
    assert summarize(results[:2], budget_ms=4000) == (True, ["  ✓ one            3.40s  2 reqs, 1 cached"])


def test_record():
    r = [Result("one", "/a", 200, 5, 2, None, None)]
    now = dt.datetime(2026, 10, 2, 1, 2, 3, tzinfo=dt.timezone.utc)
    assert record("https://x.test", T, r, now) == {
        "ts": "2026-10-02T01:02:03Z", "base": "https://x.test",
        "date": "2026-10-01", "prev": "2026-09-30", "bucket": "bk-a", "hit": "ckpt",
        "results": [{"scenario": "one", "path": "/a", "status": 200, "ms": 5, "bytes": 2, "cache": None, "server_ms": None}],
    }
